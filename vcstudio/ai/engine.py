"""AI engine worker: newest-frame inference, rate-limited, failure-isolated.

- Pulls only the newest captured frame (never queues frames); capture never waits for AI.
- Rate is set by the performance mode (or the profile's fixed rate); Maximum Quality runs as
  fast as the hardware sustains.
- With detector_model == "auto", every mode except Low Resource uses the largest model whose
  measured p95 inference time fits that mode's budget on this Mac, and steps down when it
  doesn't (QUALITY_BUDGET_MS). Low Resource always uses the smallest model.
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
from dataclasses import asdict, dataclass, field
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
    quality_note_tpl: str = ""      # translatable template + args for the UI
    quality_note_args: dict = field(default_factory=dict)
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


# p95 inference budget per mode for "auto" model choice. A model's measured p95 must fit, so slower
# Macs settle on smaller models by themselves. Low Resource is absent: it always uses the smallest.
QUALITY_BUDGET_MS = {"performance": 20.0, "balanced": 25.0, "max_quality": 25.0}
MQ_BUDGET_MS = QUALITY_BUDGET_MS["max_quality"]
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
        # per task, models measured too slow for some mode's budget on this machine (p95 ms)
        self.mq_rejected: dict[str, dict[str, float]] = {"detect": {}, "segment": {}}
        self.mq_store = None   # Path: remembers too-slow models across launches (no reload ~30 s after start)
        self.model_failures: dict[str, str] = {}   # chosen models that failed to load (cleared on reconfigure)
        self.mq_note = ""
        self.mq_note_tpl, self.mq_note_args = "", {}
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
            self.model_failures.clear()      # a new choice (or a fixed model) gets a fresh try

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
        s.quality_note_tpl, s.quality_note_args = self.mq_note_tpl, dict(self.mq_note_args)
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

    def _note(self, tpl: str, **kw) -> None:
        self.mq_note_tpl, self.mq_note_args = tpl, kw
        self.mq_note = tpl.format(**kw)

    def _judge_max_quality(self, cfg: DetectionConfig, mode: str) -> None:
        """Step down to a smaller model if the current one cannot sustain this mode's budget here."""
        budget = QUALITY_BUDGET_MS.get(mode)
        if budget is None or self._detector is None:
            return
        name = cfg.segmentation_model if cfg.need_masks else cfg.detector_model
        if name not in ("auto", "", None) or len(self._lat) < MQ_SAMPLES:
            return
        lat = sorted(list(self._lat)[-MQ_SAMPLES:])
        p95 = lat[int(0.95 * (len(lat) - 1))]
        task = "segment" if cfg.need_masks else "detect"
        cur = self._detector.name
        ranked = [m.name for m in self.models.ranked(task)]
        if p95 > budget and ranked and cur != ranked[-1]:
            self.mq_rejected.setdefault(task, {})[cur] = round(p95, 1)
            self._save_mq()
            self._note("{model} p95 {p95} ms > {budget} ms budget: stepping down", model=cur, p95=f"{p95:.1f}",
                       budget=f"{budget:g}")
            self._sink("WARNING", "ai", f"Auto model: {self.mq_note}")
            log.warning("Auto model (%s): %s", mode, self.mq_note)
            self._lat.clear()                     # next _ensure_models picks the next smaller model
            self._detector = None
        elif p95 <= budget and not self.mq_note.startswith(f"{cur} sustains"):
            self._note("{model} sustains p95 {p95} ms (budget {budget} ms)", model=cur, p95=f"{p95:.1f}",
                       budget=f"{budget:g}")

    def load_mq(self, path) -> None:
        """Restore models measured as too slow on this Mac (file in the app's data directory)."""
        import json
        self.mq_store = path
        try:
            data = json.loads(path.read_text())
            for task in ("detect", "segment"):
                self.mq_rejected[task].update({str(k): float(v) for k, v in data.get(task, {}).items()})
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def _save_mq(self) -> None:
        import json
        if self.mq_store is None:
            return
        try:
            self.mq_store.write_text(json.dumps(self.mq_rejected, indent=1))
        except OSError as e:
            log.warning("Could not save auto-model measurements: %s", e)

    def _max_quality_choice(self, task: str, budget: float = MQ_BUDGET_MS):
        """Largest model of `task` not measured as too slow for `budget` on this hardware."""
        ranked = self.models.ranked(task)
        if not ranked:
            return None
        rejected = self.mq_rejected.setdefault(task, {})
        for m in ranked:
            p95 = rejected.get(m.name)
            if p95 is None or p95 <= budget:
                return m
        return ranked[-1]                        # everything too slow: use the smallest anyway

    def _ensure_models(self, cfg: DetectionConfig, mode: str) -> None:
        # Silhouette replacement needs masks: the segmentation model replaces the detector
        # (it also returns boxes and classes), so only one model is resident.
        task, name = ("segment", cfg.segmentation_model) if cfg.need_masks else ("detect", cfg.detector_model)
        if name not in ("auto", "", None) and name in self.model_failures:
            name = "auto"            # the chosen model failed before: keep running on the automatic choice
        try:
            self._switch_to(self._wanted(task, name, mode), cfg)
        except Exception as e:
            if name in ("auto", "", None):
                raise
            # Safe switching: a chosen model that is missing or does not load must not stop detection.
            self.model_failures[name] = f"{type(e).__name__}: {e}"
            self._note("{model} could not be used ({err}); using the automatic choice", model=name, err=str(e)[:160])
            self._sink("ERROR", "ai", f"Model {name} failed: {e}; falling back to the automatic choice")
            log.error("Model %s failed (%s); falling back to the automatic choice", name, e)
            self._switch_to(self._wanted(task, "auto", mode), cfg)

    def _wanted(self, task: str, name, mode: str):
        want = None
        if mode in QUALITY_BUDGET_MS and name in ("auto", "", None):
            want = self._max_quality_choice(task, QUALITY_BUDGET_MS[mode])
        if want is None:
            want = self.models.resolve(name, task, prefer_large=(mode == "max_quality"))
        return want

    def _switch_to(self, want, cfg: DetectionConfig) -> None:
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
