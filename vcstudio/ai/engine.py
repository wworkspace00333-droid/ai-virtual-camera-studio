"""AI engine worker: newest-frame inference, rate-limited, failure-isolated.

- Pulls only the newest captured frame (never queues frames); capture never waits for AI.
- Rate is set by the performance mode (or the profile's fixed rate); Maximum Quality runs as
  fast as the hardware sustains and prefers the larger model when detector_model == "auto".
- Results are published into a LatestSlot; consumers (tracker, overlays) read the newest.
- Errors are contained: after repeated failures the model is unloaded and reloaded with backoff;
  capture/passthrough is unaffected.
"""
from __future__ import annotations

import collections
import copy
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from vcstudio.ai.coreml_engine import CoreMLDetector, VisionFaceDetector
from vcstudio.ai.segmentation import SegmentationDetector
from vcstudio.ai.detection import DetectionConfig, DetectionResult, filter_detections
from vcstudio.ai.model_manager import ModelManager
from vcstudio.ai.tracking import ByteTracker, TrackedObject, TrackerConfig
from vcstudio.core.config import PERFORMANCE_MODES
from vcstudio.core.frame import host_time_s
from vcstudio.core.frame_queue import LatestSlot
from vcstudio.core.performance import FpsCounter

log = logging.getLogger(__name__)

EventSink = Callable[[str, str, str], None]


@dataclass
class AIStats:
    state: str = "stopped"          # stopped | loading | running | idle | disabled | degraded | error
    model: str = ""
    compute_units: str = ""
    target_fps: float | None = None
    achieved_fps: float = 0.0
    inference_ms_avg: float = 0.0
    inference_ms_p95: float = 0.0
    face_ms_avg: float = 0.0
    detections: int = 0
    inferences: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    model_load_s: float = 0.0
    quality_note: str = ""
    frame_age_ms: float = 0.0       # capture-to-result latency of the last result
    tracking: bool = False
    tracks_active: int = 0
    tracks_lost: int = 0
    track_ids_created: int = 0
    tracks_recovered: int = 0
    tracker_ms: float = 0.0
    last_error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


MQ_BUDGET_MS = 25.0      # Maximum Quality: p95 inference budget (keeps >= 30 fps AI with headroom)
MQ_SAMPLES = 40          # inferences measured before judging a model


class AIEngine:
    MAX_CONSECUTIVE_ERRORS = 3

    def __init__(self, capture, models: ModelManager, event_sink: EventSink | None = None):
        self.capture = capture
        self.models = models
        self.results: LatestSlot[DetectionResult] = LatestSlot()
        self._sink = event_sink or (lambda lv, c, m: None)
        self._cfg = DetectionConfig()
        self._tcfg = TrackerConfig()
        self.tracker = ByteTracker(self._tcfg)
        self._mode = "balanced"
        self._cfg_version = 0
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stats = AIStats()
        self._lat = collections.deque(maxlen=120)
        self._face_lat = collections.deque(maxlen=120)
        self._fps = FpsCounter(2.0)
        self._detector: CoreMLDetector | SegmentationDetector | None = None
        self.last_inference = 0.0       # monotonic time of the last successful inference (watchdog)
        self.restarts = 0
        # Maximum Quality: per task, models judged too slow on this machine (measured p95 ms)
        self.mq_rejected: dict[str, dict[str, float]] = {"detect": {}, "segment": {}}
        self.mq_note = ""
        self._face: VisionFaceDetector | None = None

    # ------------------------------------------------------------- config
    def configure(self, cfg: DetectionConfig, performance_mode: str, tracking: TrackerConfig | None = None) -> None:
        with self._lock:
            self._cfg = cfg
            self._mode = performance_mode
            if tracking is not None:
                self._tcfg = tracking
            # The user's confidence threshold is ByteTrack's high threshold; new tracks need a bit more.
            t = self._tcfg
            t.high_thresh = cfg.confidence
            t.new_track_thresh = min(0.95, cfg.confidence + 0.1)
            t.low_thresh = min(t.low_thresh, cfg.confidence)
            self.tracker.configure(t)
            self._cfg_version += 1

    def tracker_config(self) -> TrackerConfig:
        with self._lock:
            return self._tcfg

    def tracked_at(self, t: float) -> list[TrackedObject]:
        """Tracked objects predicted to output time t (host-clock seconds). Thread-safe."""
        if not self._tcfg.enabled or self._stats.state in ("stopped", "disabled"):
            return []
        return self.tracker.snapshot(t)

    def config(self) -> tuple[DetectionConfig, str]:
        with self._lock:
            return self._cfg, self._mode

    def target_fps(self) -> float | None:
        cfg, mode = self.config()
        if cfg.frequency_mode == "fixed":
            return max(0.5, cfg.fixed_fps)
        return PERFORMANCE_MODES[mode].ai_fps

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._cfg.enabled = enabled
            self._cfg_version += 1

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        from vcstudio.core.pyobjc_prewarm import prewarm
        prewarm()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ai-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._detector = None
        self._face = None
        self.models.unload()
        self.results.clear()
        self.tracker.reset()
        self._set_state("stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def restart(self, reason: str) -> None:
        """Watchdog recovery: drop the (possibly stuck) worker and models, start fresh."""
        log.warning("AI engine restart: %s", reason)
        self._sink("WARNING", "ai", f"restart: {reason}")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)    # a hung Core ML call cannot be interrupted; abandon it
        self._thread = None
        self._detector = None
        self._face = None
        self.models.unload()
        self.restarts += 1
        self.last_inference = time.monotonic()
        self.start()

    def stats(self) -> AIStats:
        with self._lock:
            s = copy.copy(self._stats)
        s.achieved_fps = self._fps.rate()
        lat = sorted(self._lat)
        if lat:
            s.inference_ms_avg = sum(lat) / len(lat)
            s.inference_ms_p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
        if self._face_lat:
            s.face_ms_avg = sum(self._face_lat) / len(self._face_lat)
        s.target_fps = self.target_fps()
        s.quality_note = self.mq_note
        ts = self.tracker.stats
        s.tracking = self._tcfg.enabled
        s.tracks_active, s.tracks_lost = ts.active, ts.lost
        s.track_ids_created, s.tracks_recovered, s.tracker_ms = ts.ids_created, ts.recovered, ts.update_ms
        return s

    # ------------------------------------------------------------- internals
    def _set_state(self, state: str, err: str | None = None) -> None:
        with self._lock:
            prev = self._stats.state
            self._stats.state = state
            if err is not None:
                self._stats.last_error = err
        if prev != state:
            lvl = "ERROR" if state == "error" else ("WARNING" if state == "degraded" else "INFO")
            msg = f"state {prev} -> {state}" + (f": {err}" if err else "")
            getattr(log, lvl.lower())("AI %s", msg)
            self._sink(lvl, "ai", msg)

    def _judge_max_quality(self, cfg: DetectionConfig, mode: str) -> None:
        """Step down to a smaller model if the current one cannot sustain the budget here."""
        if mode != "max_quality" or self._detector is None:
            return
        name = cfg.segmentation_model if cfg.need_masks else cfg.detector_model
        if name not in ("auto", "", None) or len(self._lat) < MQ_SAMPLES:
            return
        lat = sorted(list(self._lat)[-MQ_SAMPLES:])
        p95 = lat[int(0.95 * (len(lat) - 1))]
        task = "segment" if cfg.need_masks else "detect"
        cur = self._detector.name
        ranked = [m.name for m in self.models.ranked(task)]
        if p95 > MQ_BUDGET_MS and ranked and cur != ranked[-1]:
            self.mq_rejected.setdefault(task, {})[cur] = round(p95, 1)
            self.mq_note = f"{cur} p95 {p95:.1f} ms > {MQ_BUDGET_MS:g} ms budget: stepping down"
            self._sink("WARNING", "ai", f"Maximum Quality: {self.mq_note}")
            log.warning("Maximum Quality: %s", self.mq_note)
            self._lat.clear()                     # next _ensure_models picks the next smaller model
            self._detector = None
        elif p95 <= MQ_BUDGET_MS and not self.mq_note.startswith(cur):
            self.mq_note = f"{cur} sustains p95 {p95:.1f} ms (budget {MQ_BUDGET_MS:g} ms)"

    def _max_quality_choice(self, task: str):
        """Largest model of `task` not yet measured as too slow on this hardware."""
        ranked = self.models.ranked(task)
        if not ranked:
            return None
        rejected = self.mq_rejected.setdefault(task, {})
        for m in ranked:
            if m.name not in rejected:
                return m
        return ranked[-1]                        # everything too slow: use the smallest anyway

    def _ensure_models(self, cfg: DetectionConfig, mode: str) -> None:
        # Silhouette replacement needs masks: the segmentation model replaces the detector
        # (it also returns boxes and classes), so only one model is resident.
        task, name = ("segment", cfg.segmentation_model) if cfg.need_masks else ("detect", cfg.detector_model)
        want = None
        if mode == "max_quality" and name in ("auto", "", None):
            want = self._max_quality_choice(task)
        if want is None:
            want = self.models.resolve(name, task, prefer_large=(mode == "max_quality"))
        cur = self._detector
        if cur is None or cur.name != want.name or cur.loaded.compute_units != cfg.compute_units:
            self._set_state("loading")
            self._detector = None
            self.models.unload()   # keep only one detector resident (16 GB budget)
            lm = self.models.load(want, cfg.compute_units)
            det = SegmentationDetector(lm) if want.task == "segment" else CoreMLDetector(lm)
            with self._lock:
                self._stats.model, self._stats.compute_units = want.name, cfg.compute_units
                self._stats.model_load_s = lm.load_seconds
            self._lat.clear()
            self._detector = det
        if cfg.face_detection and self._face is None:
            self._face = VisionFaceDetector()
        elif not cfg.face_detection:
            self._face = None

    def _run(self) -> None:
        import objc  # type: ignore
        # Plain Python threads have no Objective-C autorelease pool. Without draining one per
        # iteration, Core ML / Vision results (MLMultiArray, observations, pixel buffers) live for
        # the lifetime of the thread (measured: footprint +65 MB/s with the segmentation model).
        st = {"last_ver": -1, "next_due": 0.0, "backoff": 0.5}
        while not self._stop.is_set():
            with objc.autorelease_pool():
                self._iteration(st)
        self._detector = None

    def _iteration(self, st: dict) -> None:
        cfg, mode = self.config()
        if not cfg.enabled:
            self._set_state("disabled")
            self.results.clear()
            self.tracker.reset()
            self._stop.wait(0.1)
            return
        frame, ver = self.capture.slot.wait_newer(st["last_ver"], timeout=0.25)
        if frame is None or ver == st["last_ver"]:
            self._set_state("idle")
            return
        try:
            self._ensure_models(cfg, mode)   # lazy: models load only once frames flow
        except Exception as e:
            self._set_state("error", f"model load failed: {e}")
            self._stop.wait(min(st["backoff"], 10))
            st["backoff"] *= 2
            return
        target = self.target_fps()
        now = time.perf_counter()
        if target and now < st["next_due"]:
            self._stop.wait(st["next_due"] - now)
            frame, ver = self.capture.slot.peek()   # newest frame after waiting
            if frame is None:
                return
        st["last_ver"] = ver
        if target:
            # Fixed cadence: advance the schedule by one period; re-anchor only if far behind.
            period = 1.0 / target
            now = time.perf_counter()
            nd = st["next_due"]
            st["next_due"] = nd + period if nd > now - period else now + period
        else:
            st["next_due"] = 0.0
        tcfg = self.tracker_config()
        model_conf = min(cfg.confidence, tcfg.low_thresh) if tcfg.enabled else cfg.confidence
        try:
            dets, ms = self._detector.detect(frame, model_conf, cfg.iou)
            face_ms = 0.0
            if self._face is not None:
                fd, face_ms = self._face.detect(frame)
                dets.extend(fd)
                self._face_lat.append(face_ms)
            candidates = filter_detections(dets, cfg, min_conf=model_conf)   # class filters only
            kept = [d for d in candidates if d.confidence >= cfg.confidence]
            tracked = []
            if tcfg.enabled:
                self.tracker.update(candidates, frame.pts, (frame.width, frame.height))
                tracked = self.tracker.snapshot(frame.pts)
            res = DetectionResult(frame.seq, frame.pts, frame.width, frame.height, kept, ms, face_ms,
                                  self._detector.name, host_time_s(), len(dets), tracked)
            self.results.put(res)
            self._lat.append(ms)
            self._fps.tick()
            self.last_inference = time.monotonic()
            self._judge_max_quality(cfg, mode)
            with self._lock:
                s = self._stats
                s.inferences += 1
                s.detections = len(kept)
                s.consecutive_errors = 0
                s.frame_age_ms = (res.completed - frame.arrival) * 1000
            self._set_state("running")
            st["backoff"] = 0.5
        except Exception as e:
            with self._lock:
                self._stats.errors += 1
                self._stats.consecutive_errors += 1
                n = self._stats.consecutive_errors
            log.exception("inference failed")
            if n >= self.MAX_CONSECUTIVE_ERRORS:
                self._set_state("degraded", f"{n} consecutive failures ({e}); reloading model")
                self._detector = None
                self._face = None
                self.models.unload()
                self.results.clear()
                self._stop.wait(min(st["backoff"], 10))
                st["backoff"] *= 2
