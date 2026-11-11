"""Watchdog: detects stalls and resource pressure and applies the least disruptive recovery.

Checks (every `interval` seconds):
  pipeline   heartbeat older than PIPELINE_STALL_S while running        -> restart pipeline thread
  ai         enabled, frames flowing, no inference for AI_STALL_S        -> restart AI engine
             (output continues meanwhile: tracks coast, then regions disappear; nothing blocks)
  memory     process RSS above warning -> event; above critical -> shed load: Low Resource mode,
             close video players, drop caches; restored automatically when pressure falls
Capture loss/frozen frames and camera reconnects are handled by CaptureSupervisor and
CameraOutputController; the watchdog records their state changes.
"""
from __future__ import annotations

import gc
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import psutil

log = logging.getLogger(__name__)

PIPELINE_STALL_S = 2.0
AI_STALL_S = 5.0


@dataclass
class WatchdogStats:
    checks: int = 0
    pipeline_restarts: int = 0
    ai_restarts: int = 0
    memory_state: str = "normal"      # normal | warning | critical
    rss_mb: float = 0.0
    load_shed: bool = False
    events: list[str] = field(default_factory=list)


class Watchdog:
    def __init__(self, ctx, interval: float = 0.5, warn_mb: float | None = None, critical_mb: float | None = None):
        self.ctx = ctx
        self.interval = interval
        base = ctx.settings.get("performance.memory_warning_mb")
        self.warn_mb = warn_mb if warn_mb is not None else base
        self.critical_mb = critical_mb if critical_mb is not None else base * 2
        self.stats = WatchdogStats()
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._proc = psutil.Process(os.getpid())
        self._mode_before_shed: str | None = None
        from vcstudio.core.performance import phys_footprint
        # physical footprint (Activity Monitor "Memory"); RSS misses IOSurface/GPU allocations
        self.rss_fn = lambda: (phys_footprint() or self._proc.memory_info().rss) / 2**20   # injectable for tests

    def start(self) -> None:
        if self._t and self._t.is_alive():
            return
        self._stop.clear()
        self._t = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(2)
        self._t = None

    def _event(self, level: str, msg: str) -> None:
        getattr(log, level.lower())("Watchdog: %s", msg)
        self.ctx.db.log_event(level, "watchdog", msg)
        self.stats.events = (self.stats.events + [msg])[-20:]

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check()
            except Exception:
                log.exception("watchdog check failed")

    def check(self) -> None:
        self.stats.checks += 1
        now = time.monotonic()
        ctx = self.ctx
        pipe = ctx.pipeline
        cap_running = ctx.capture.stats().state in ("running", "frozen")
        # ---- pipeline
        if pipe.running is False and pipe._thread is not None:
            pass
        if pipe._thread is not None and cap_running and now - pipe.heartbeat > PIPELINE_STALL_S:
            self.stats.pipeline_restarts += 1
            self._event("ERROR", f"pipeline stalled for {now - pipe.heartbeat:.1f} s; restarting it")
            pipe.restart("watchdog: stalled")
        # ---- AI
        ai = ctx.ai
        cfg, _ = ai.config()
        if ai.running and cfg.enabled and cap_running and ai.stats().state not in ("loading", "disabled"):
            last = ai.last_inference or now
            target = ai.target_fps() or 30
            if now - last > max(AI_STALL_S, 10.0 / target):
                self.stats.ai_restarts += 1
                self._event("ERROR", f"AI produced no result for {now - last:.1f} s; restarting the engine")
                ai.restart("watchdog: stalled")
        # ---- memory
        rss = self.rss_fn()
        self.stats.rss_mb = rss
        state = "critical" if rss > self.critical_mb else ("warning" if rss > self.warn_mb else "normal")
        if state != self.stats.memory_state:
            self._event("WARNING" if state != "normal" else "INFO", f"memory {state}: {rss:.0f} MB")
            self.stats.memory_state = state
        if state == "critical" and not self.stats.load_shed:
            self._shed_load()
        elif state == "normal" and self.stats.load_shed:
            self._restore()

    def _shed_load(self) -> None:
        ctx = self.ctx
        self._mode_before_shed = ctx.settings.get("performance.mode")
        if self._mode_before_shed != "low_resource":
            ctx.settings.set("performance.mode", "low_resource")
        ctx.replacement.compositor.videos.close_all()
        ctx.replacement.compositor._mask_cache.clear()
        from vcstudio.replacement.assets import image_cache
        image_cache.cache_clear()
        gc.collect()
        self.stats.load_shed = True
        self._event("WARNING", "critical memory: switched to Low Resource mode, closed video players, cleared caches")

    def _restore(self) -> None:
        if self._mode_before_shed and self._mode_before_shed != "low_resource":
            self.ctx.settings.set("performance.mode", self._mode_before_shed)
        self.stats.load_shed = False
        self._event("INFO", f"memory back to normal; performance mode restored to {self._mode_before_shed}")
        self._mode_before_shed = None
