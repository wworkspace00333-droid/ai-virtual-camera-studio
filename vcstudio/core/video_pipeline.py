"""Real-time processing pipeline: newest captured frame -> tracked regions -> compositor -> outputs.

One thread, paced by capture (one output per new input frame, newest frame wins; nothing queues).
AI runs on its own thread; here we only *query* the tracker for boxes predicted to this frame's
timestamp, so AI latency never delays output.

Output modes
  normal       Metal (or CPU) compositing of the active profile's replacements
  passthrough  input pixel buffer forwarded untouched (zero-copy) — emergency switch, automatic when
               compositing keeps failing, and the natural path when no replacement is active
  paused       the last output frame is re-sent (clients see a frozen image; capture continues)

Outputs: the virtual camera (CMIO sink; reconnects automatically) and a (input, output) pair slot
for synchronised original/processed previews.
"""
from __future__ import annotations

import collections
import copy
import logging
import threading
import time
from dataclasses import asdict, dataclass

import numpy as np

from vcstudio.core.frame import Frame, host_time_s
from vcstudio.core.frame_queue import LatestSlot
from vcstudio.core.performance import FpsCounter

log = logging.getLogger(__name__)

AUTO_PASSTHROUGH_ERRORS = 5          # consecutive compositor failures before automatic passthrough
CAMERA_RETRY_S = 3.0


@dataclass
class PipelineStats:
    state: str = "stopped"            # stopped | running | paused | passthrough
    output_fps: float = 0.0
    frames_out: int = 0
    inputs_skipped: int = 0           # newer input arrived before the previous one was processed
    latency_ms: float = 0.0           # capture arrival -> output ready (EMA)
    latency_ms_p95: float = 0.0
    process_ms: float = 0.0           # per-frame processing time (EMA)
    render_ms: float = 0.0
    gpu_ms: float = 0.0
    backend: str = ""
    regions: int = 0
    passthrough_reason: str = ""
    render_errors: int = 0
    camera_state: str = "disabled"    # disabled | not_installed | connected | error
    camera_device: str = ""
    camera_sent: int = 0
    camera_dropped: int = 0
    camera_error: str = ""
    restarts: int = 0
    last_error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class CameraOutputController:
    """Owns the CMIO sink connection: lazy open, retry with backoff, reconnect after errors."""

    def __init__(self, device: str, sink: str, enabled: bool = True):
        self.device, self.sink, self.enabled = device, sink, enabled
        self._out = None
        self._next_try = 0.0
        self.state = "disabled" if not enabled else "not_installed"
        self.error = ""

    def configure(self, device: str, sink: str, enabled: bool) -> None:
        if (device, sink, enabled) != (self.device, self.sink, self.enabled):
            self.close()
            self.device, self.sink, self.enabled = device, sink, enabled
            self._next_try = 0.0
            self.state = "disabled" if not enabled else "not_installed"

    def send(self, pb, pts: float) -> bool | None:
        if not self.enabled:
            self.state = "disabled"
            return None
        if self._out is None:
            if time.monotonic() < self._next_try:
                return None
            from vcstudio.camera.output import CameraOutputError, VirtualCameraOutput
            try:
                out = VirtualCameraOutput(self.device, self.sink)
                out.open()
                self._out = out
                self.state, self.error = "connected", ""
            except CameraOutputError as e:
                self.state = "not_installed" if e.code in (-1, -2) else "error"
                self.error = str(e)
                self._next_try = time.monotonic() + CAMERA_RETRY_S
                return None
        try:
            return self._out.send(pb, pts)
        except Exception as e:
            log.warning("Camera output error, reconnecting: %s", e)
            self.error = str(e)
            self.state = "error"
            self.close()
            self._next_try = time.monotonic() + 0.5
            return None

    def stats(self) -> tuple[int, int]:
        return self._out.stats() if self._out else (0, 0)

    def close(self) -> None:
        if self._out is not None:
            try:
                self._out.close()
            except Exception:
                pass
            self._out = None


class VideoPipeline:
    def __init__(self, capture, ai, rois, replacement, camera: CameraOutputController,
                 event_sink=None):
        self.capture, self.ai, self.rois, self.replacement, self.camera = capture, ai, rois, replacement, camera
        self._sink = event_sink or (lambda lv, c, m: None)
        self.processed: LatestSlot[tuple[Frame, Frame]] = LatestSlot()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stats = PipelineStats()
        self._fps = FpsCounter(1.0)
        self._lat = collections.deque(maxlen=240)
        self.paused = False
        self.passthrough = False
        self.passthrough_reason = ""
        self.replacement_enabled = True
        self.heartbeat = 0.0              # monotonic time of the last loop iteration (watchdog)
        self._last_out: Frame | None = None
        self.override = None              # callable(w, h) -> BGRA array sent instead of the video (calibration)
        self._override_cache: tuple | None = None
        self._consecutive_errors = 0
        self._generation = 0

    # ------------------------------------------------------------- control
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._generation += 1
        gen = self._generation
        self.heartbeat = time.monotonic()
        self._thread = threading.Thread(target=self._run, args=(gen,), name="video-pipeline", daemon=True)
        self._thread.start()
        self._event("INFO", "started")

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3)
        self._thread = None
        self.camera.close()
        self.processed.clear()
        self._last_out = None
        with self._lock:
            self._stats.state = "stopped"
        self._event("INFO", "stopped")

    def restart(self, reason: str) -> None:
        """Watchdog recovery: abandon a stuck thread (daemon) and start a fresh one."""
        self._event("WARNING", f"restart: {reason}")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        self._thread = None
        with self._lock:
            self._stats.restarts += 1
        self.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._event("INFO", "output paused" if paused else "output resumed")

    def set_passthrough(self, on: bool, reason: str = "manual") -> None:
        self.passthrough = on
        self.passthrough_reason = reason if on else ""
        self._consecutive_errors = 0
        self._event("WARNING" if on else "INFO", f"passthrough {'ON (' + reason + ')' if on else 'OFF'}")

    def set_replacement_enabled(self, on: bool) -> None:
        self.replacement_enabled = on
        self._event("INFO", f"replacement {'enabled' if on else 'disabled'}")

    def stats(self) -> PipelineStats:
        with self._lock:
            s = copy.copy(self._stats)
        s.output_fps = self._fps.rate()
        if self._lat:
            lat = sorted(self._lat)
            s.latency_ms_p95 = lat[int(0.95 * (len(lat) - 1))]
        s.camera_state, s.camera_error, s.camera_device = self.camera.state, self.camera.error, self.camera.device
        s.camera_sent, s.camera_dropped = self.camera.stats()
        if s.state != "stopped":
            s.state = "paused" if self.paused else ("passthrough" if self.passthrough else "running")
        s.passthrough_reason = self.passthrough_reason
        return s

    def _event(self, level: str, msg: str) -> None:
        getattr(log, level.lower())("Pipeline: %s", msg)
        self._sink(level, "pipeline", msg)

    # ------------------------------------------------------------- loop
    def _run(self, gen: int) -> None:
        import objc  # type: ignore
        st = {"last_ver": -1}
        with self._lock:
            self._stats.state = "running"
        while not self._stop.is_set() and gen == self._generation:
            # drain Objective-C autoreleased objects (CMSampleBuffers, Metal objects) every frame
            with objc.autorelease_pool():
                self._iteration(st)
        self.heartbeat = time.monotonic()

    def _iteration(self, st: dict) -> None:
        from vcstudio.core.pixelbuffer import pixel_buffer_for
        last_ver = st["last_ver"]
        self.heartbeat = time.monotonic()
        frame, ver = self.capture.slot.wait_newer(last_ver, timeout=0.25)
        if frame is None or ver == last_ver:
            return
        if last_ver >= 0 and ver - last_ver > 1:
            with self._lock:
                self._stats.inputs_skipped += ver - last_ver - 1
        st["last_ver"] = ver
        t0 = time.perf_counter()
        regions_n = 0
        try:
            ov = self.override
            if ov is not None:
                key = (id(ov), frame.width, frame.height)
                if self._override_cache is None or self._override_cache[0] != key:
                    self._override_cache = (key, np.ascontiguousarray(ov(frame.width, frame.height)))
                arr = self._override_cache[1]
                out = Frame(frame.seq, frame.pts, frame.arrival, frame.width, frame.height,
                            pixel_buffer=pixel_buffer_for(Frame(frame.seq, frame.pts, frame.arrival, frame.width,
                                                                frame.height, array=arr, source="override")),
                            source="override")
            elif self.paused and self._last_out is not None:
                out = self._last_out
            else:
                out = None
                if not self.passthrough and self.replacement_enabled and self.replacement.active():
                    try:
                        tracked = self.ai.tracked_at(frame.pts)
                        regions = self.rois.resolve(tracked, frame.width, frame.height)
                        regions_n = len(regions)
                        pb = self.replacement.render_pb(frame, regions, frame.pts)
                        out = Frame(frame.seq, frame.pts, frame.arrival, frame.width, frame.height,
                                    pixel_buffer=pb, source="processed")
                        self._consecutive_errors = 0
                    except Exception as e:
                        self._consecutive_errors += 1
                        with self._lock:
                            self._stats.render_errors += 1
                            self._stats.last_error = f"{type(e).__name__}: {e}"
                        log.exception("frame processing failed; passing this frame through")
                        if self._consecutive_errors >= AUTO_PASSTHROUGH_ERRORS:
                            self.set_passthrough(True, f"automatic: {self._consecutive_errors} consecutive render failures")
                if out is None:   # passthrough: forward the input buffer itself (zero-copy for camera frames)
                    out = Frame(frame.seq, frame.pts, frame.arrival, frame.width, frame.height,
                                pixel_buffer=pixel_buffer_for(frame), source="passthrough")
                self._last_out = out
            self.camera.send(out.pixel_buffer, frame.pts)
            self.processed.put((frame, out))
            done = host_time_s()
            self._fps.tick()
            ms = (time.perf_counter() - t0) * 1000
            lat = (done - frame.arrival) * 1000
            self._lat.append(lat)
            cs = self.replacement.compositor.stats
            with self._lock:
                st = self._stats
                st.frames_out += 1
                st.process_ms = ms if st.frames_out == 1 else 0.9 * st.process_ms + 0.1 * ms
                st.latency_ms = lat if st.frames_out == 1 else 0.9 * st.latency_ms + 0.1 * lat
                st.render_ms, st.gpu_ms, st.backend = cs.render_ms, cs.gpu_ms, cs.backend
                st.regions = regions_n
        except Exception as e:      # never let the output thread die
            log.exception("pipeline iteration failed")
            with self._lock:
                self._stats.last_error = f"{type(e).__name__}: {e}"
            time.sleep(0.01)
