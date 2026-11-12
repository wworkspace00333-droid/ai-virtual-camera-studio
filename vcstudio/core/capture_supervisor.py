"""Capture supervisor: lifecycle, health monitoring, frozen/missing detection, auto-reconnect.

States
  stopped       not capturing (user request)
  starting      opening the device
  running       frames arriving normally
  frozen        frames arriving but content identical for >= frozen timeout (warning only:
                a static OBS scene or OBS's "not started" placeholder also looks like this)
  no_signal     no frames for >= missing timeout -> triggers reconnect
  reconnecting  waiting for backoff / retrying
  error         unrecoverable without user action (permission denied, feedback loop)
"""
from __future__ import annotations

import copy
import logging
import threading
from dataclasses import asdict, dataclass
from typing import Callable

from vcstudio.core.frame import Frame, host_time_s
from vcstudio.core.frame_queue import LatestSlot
from vcstudio.core.performance import FpsCounter
from vcstudio.core.video_capture import (CaptureSource, FeedbackLoopRefused, PermissionDenied)

log = logging.getLogger(__name__)

SourceFactory = Callable[[], CaptureSource]
EventSink = Callable[[str, str, str], None]  # (level, component, message)

TERMINAL_ERRORS = (PermissionDenied, FeedbackLoopRefused)


@dataclass
class CaptureStats:
    state: str = "stopped"
    source_name: str = ""
    source_kind: str = ""
    width: int = 0
    height: int = 0
    pixel_format: str = ""
    nominal_fps: float = 0.0
    measured_fps: float = 0.0
    frames: int = 0
    dropped_by_source: int = 0
    replaced_unconsumed: int = 0
    latency_ms: float = 0.0
    last_frame_age_ms: float = 0.0
    frozen_for_s: float = 0.0
    reconnects: int = 0
    session_uptime_s: float = 0.0
    last_error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class CaptureSupervisor:
    def __init__(self, *, missing_timeout_ms: int = 1500, frozen_timeout_ms: int = 2000,
                 auto_reconnect: bool = True, event_sink: EventSink | None = None,
                 backoff: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 5.0), tick_s: float = 0.05):
        self.slot: LatestSlot[Frame] = LatestSlot()
        self.missing_timeout = missing_timeout_ms / 1000
        self.frozen_timeout = frozen_timeout_ms / 1000
        self.auto_reconnect = auto_reconnect
        self.backoff = backoff
        self.tick_s = tick_s
        self._sink = event_sink or (lambda lv, c, m: None)
        self._factory: SourceFactory | None = None
        self._source: CaptureSource | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()
        self._stats = CaptureStats()
        self._fps = FpsCounter(1.0)
        self._fps_lock = threading.Lock()
        self._last_arrival = 0.0
        self._lat_ema = 0.0
        self._pending_event: tuple[str, str] | None = None
        self._sig = None
        self._sig_since = 0.0
        self._sig_checked_version = -1
        self._listeners: list[Callable[[str], None]] = []

    # ------------------------------------------------------------------ public API
    def start(self, factory: SourceFactory) -> None:
        from vcstudio.core.pyobjc_prewarm import prewarm
        prewarm()
        self.stop()
        with self._lock:
            self._factory = factory
            self._stats = CaptureStats(state="starting")
            self._stats.reconnects = 0
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._run, name="capture-supervisor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        t = self._thread
        if t is not None:
            self._stop_evt.set()
            t.join(timeout=5)
            if t.is_alive():
                log.error("capture supervisor did not stop within 5 s")
        self._thread = None
        self._close_source()
        self.slot.clear()
        self._set_state("stopped")

    def stats(self) -> CaptureStats:
        with self._lock:
            s = copy.copy(self._stats)
        with self._fps_lock:
            s.measured_fps = self._fps.rate()
        if self._last_arrival:
            s.last_frame_age_ms = (host_time_s() - self._last_arrival) * 1000
        s.replaced_unconsumed = self.slot.overwritten
        return s

    def latest(self) -> tuple[Frame | None, int]:
        return self.slot.peek()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def add_state_listener(self, cb: Callable[[str], None]) -> None:
        self._listeners.append(cb)

    # ------------------------------------------------------------------ internals
    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._lock:
            prev = self._stats.state
            self._stats.state = state
            if error is not None:
                self._stats.last_error = error
        if prev != state:
            lvl = "WARNING" if state in ("frozen", "no_signal", "reconnecting") else ("ERROR" if state == "error" else "INFO")
            msg = f"state {prev} -> {state}" + (f": {error}" if error else "")
            getattr(log, lvl.lower())("Capture %s", msg)
            self._sink(lvl, "capture", msg)
            for cb in list(self._listeners):
                try:
                    cb(state)
                except Exception:
                    log.exception("capture state listener failed")

    def _on_frame(self, f: Frame) -> None:
        self.slot.put(f)
        with self._fps_lock:
            self._fps.tick()
        now = f.arrival
        lat = max(0.0, (now - f.pts) * 1000)
        self._lat_ema = lat if self._lat_ema == 0 else 0.9 * self._lat_ema + 0.1 * lat
        self._last_arrival = now
        with self._lock:
            st = self._stats
            st.frames += 1
            st.width, st.height, st.pixel_format = f.width, f.height, f.pixel_format
            st.latency_ms = self._lat_ema

    def _on_source_event(self, kind: str, msg: str) -> None:
        if kind == "dropped":
            with self._lock:
                self._stats.dropped_by_source += 1
            return
        self._pending_event = (kind, msg)

    def _close_source(self) -> None:
        src, self._source = self._source, None
        if src is not None:
            try:
                src.stop()
            except Exception:
                log.exception("error stopping capture source")

    def _open_source(self) -> None:
        assert self._factory is not None
        self._set_state("starting")
        src = self._factory()
        self._last_arrival = 0.0
        self._sig = None
        self._pending_event = None
        src.start(self._on_frame, self._on_source_event)
        self._source = src
        w, h, fps = src.nominal
        with self._lock:
            st = self._stats
            st.source_name, st.source_kind = src.name, src.kind
            st.width, st.height, st.nominal_fps = w, h, fps
            st.last_error = ""
        self._opened_at = host_time_s()

    def _check_frozen(self, now: float) -> bool:
        frame, ver = self.slot.peek()
        if frame is None or ver == self._sig_checked_version:
            return self._sig is not None and now - self._sig_since >= self.frozen_timeout
        self._sig_checked_version = ver
        try:
            sig = frame.signature()
        except Exception as e:
            log.debug("signature failed: %s", e)
            return False
        if sig != self._sig:
            self._sig, self._sig_since = sig, now
        return now - self._sig_since >= self.frozen_timeout

    def _run(self) -> None:
        attempt = 0
        sig_period, next_sig = 0.1, 0.0
        while not self._stop_evt.is_set():
            # ---------- (re)open ----------
            if self._source is None:
                try:
                    self._open_source()
                    attempt = 0
                except TERMINAL_ERRORS as e:
                    self._set_state("error", str(e))
                    return
                except Exception as e:
                    self._close_source()
                    if not self.auto_reconnect:
                        self._set_state("error", str(e))
                        return
                    delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                    attempt += 1
                    self._set_state("reconnecting", f"{e} (retry in {delay:g} s)")
                    self._stop_evt.wait(delay)
                    continue
            # ---------- monitor ----------
            self._stop_evt.wait(self.tick_s)
            now = host_time_s()
            with self._lock:
                self._stats.session_uptime_s = now - self._opened_at
            reason = None
            ev = self._pending_event
            if ev is not None:
                self._pending_event = None
                reason = ev[1]
            elif self._last_arrival == 0.0:
                if now - self._opened_at >= max(self.missing_timeout, 3.0):  # first frame can be slow
                    reason = "no frames received after start"
            elif now - self._last_arrival >= self.missing_timeout:
                reason = f"no frames for {now - self._last_arrival:.1f} s"
            if reason:
                self._set_state("no_signal", reason)
                if not self.auto_reconnect:
                    continue
                self._close_source()
                with self._lock:
                    self._stats.reconnects += 1
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                attempt += 1
                self._set_state("reconnecting", f"{reason}; reconnecting in {delay:g} s")
                self._stop_evt.wait(delay)
                continue
            if self._last_arrival == 0.0:
                continue
            frozen = False
            if now >= next_sig:
                next_sig = now + sig_period
                frozen = self._check_frozen(now)
            else:
                frozen = self._sig is not None and now - self._sig_since >= self.frozen_timeout
            with self._lock:
                self._stats.frozen_for_s = (now - self._sig_since) if frozen else 0.0
            self._set_state("frozen" if frozen else "running")
        self._close_source()
