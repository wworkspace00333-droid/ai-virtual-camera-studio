"""Application context: wires persistence, logging, monitoring. UI-agnostic (testable headless)."""
from __future__ import annotations

import logging

from vcstudio import __version__
from vcstudio.core.capture_supervisor import CaptureSupervisor
from vcstudio.core.config import Settings
from vcstudio.core.database import Database
from vcstudio.core.logging_setup import RingBufferHandler, setup_logging
from vcstudio.core.paths import AppPaths
from vcstudio.core.performance import SystemMonitor
from vcstudio.core.profiles import ProfileError, ProfileManager

log = logging.getLogger("vcstudio.app")


class AppContext:
    def __init__(self, paths: AppPaths | None = None, *, configure_logging: bool = True):
        from vcstudio.core.pyobjc_prewarm import prewarm
        prewarm()                     # before any worker thread touches PyObjC (lazy symbols are not thread-safe)
        self.paths = (paths or AppPaths.default()).ensure()
        self.settings = Settings(self.paths)
        problems = self.settings.load()
        self.ring: RingBufferHandler | None = None
        if configure_logging:
            lc = self.settings.get("logging")
            self.ring = setup_logging(self.paths.logs_dir, lc["level"], lc["max_bytes"], lc["backup_count"])
        for p in problems:
            log.warning("settings: %s", p)
        self.db = Database(self.paths.database_file)
        self.profiles = ProfileManager(self.paths)
        self.profiles.load_all()
        wanted = self.settings.get("active_profile")
        try:
            self.profiles.switch(wanted)
        except ProfileError:
            log.warning("Active profile %r missing; using %r", wanted, self.profiles.names()[0])
            self.profiles.switch(self.profiles.names()[0])
        self.profiles.add_listener(self._on_profile_event)
        self.monitor = SystemMonitor(self.settings.get("performance.memory_warning_mb"))
        self.capture = CaptureSupervisor(event_sink=self.db.log_event)
        from vcstudio.ai.engine import AIEngine
        from vcstudio.ai.model_manager import ModelManager
        self.models = ModelManager(self.paths.models_dir)
        self.ai = AIEngine(self.capture, self.models, event_sink=self.db.log_event)
        from vcstudio.roi.resolver import RoiService
        self.rois = RoiService()
        from vcstudio.replacement.assets import AssetLibrary
        from vcstudio.replacement.service import ReplacementService
        self.assets = AssetLibrary(self.paths.assets_dir)
        self.replacement = ReplacementService(self.assets, self.settings.get("processing.compositor"))
        from vcstudio.core.video_pipeline import CameraOutputController, VideoPipeline
        from vcstudio.core.watchdog import Watchdog
        o = self.settings.get("output")
        self.camera_out = CameraOutputController(o["camera_device"], o["camera_sink"], o["camera_enabled"])
        self.pipeline = VideoPipeline(self.capture, self.ai, self.rois, self.replacement, self.camera_out,
                                      event_sink=self.db.log_event)
        self.watchdog = Watchdog(self)
        mode = self.profiles.active()["body"].get("performance_mode")
        if mode and mode != self.settings.get("performance.mode"):
            self.settings.set("performance.mode", mode)
        self.apply_ai_config()
        self.settings.add_listener(self._on_setting)
        crashed = self.db.unclean_previous_sessions()
        self.db.begin_session(__version__, self.profiles.active_name)
        if crashed:
            self.db.log_event("WARNING", "app", f"{crashed} previous session(s) did not exit cleanly")
            log.warning("%d previous session(s) did not exit cleanly", crashed)
        self.db.log_event("INFO", "app", "started", {"version": __version__})
        log.info("AI Virtual Camera Studio %s started; data dir %s", __version__, self.paths.root)

    def apply_ai_config(self) -> None:
        from vcstudio.ai.detection import DetectionConfig
        from vcstudio.ai.tracking import TrackerConfig
        body = self.profiles.active()["body"]
        self.rois.load_profile(body)
        self.replacement.load_profile(body)
        self.ai.configure(DetectionConfig.from_profile(body), self.settings.get("performance.mode"),
                          TrackerConfig.from_profile(body))

    def _on_setting(self, key: str, _value) -> None:
        if key.startswith("performance"):
            self.apply_ai_config()
        if key.startswith("output"):
            o = self.settings.get("output")
            self.camera_out.configure(o["camera_device"], o["camera_sink"], o["camera_enabled"])
        if key.startswith("processing"):
            used = self.replacement.compositor.set_backend(self.settings.get("processing.compositor"))
            log.info("Compositor backend: %s", used)

    def asset_in_use_elsewhere(self, asset_id: str, except_profile: str | None = None) -> bool:
        for n in self.profiles.names():
            if n == except_profile:
                continue
            if any(a.get("id") == asset_id for a in self.profiles.get(n)["body"].get("assets", [])):
                return True
        return False

    def update_active_profile(self, mutate) -> None:
        """Apply `mutate(body)` to the active profile, persist, and reconfigure engines."""
        name = self.profiles.active_name
        body = self.profiles.get(name)["body"]
        mutate(body)
        self.profiles.update_body(name, body)

    def _on_profile_event(self, event: str, name: str) -> None:
        if event in ("switched", "updated", "renamed") and hasattr(self, "ai"):
            self.apply_ai_config()
        if event in ("switched", "renamed") and self.profiles.active_name:
            self.settings.set("active_profile", self.profiles.active_name)
        if event == "switched":
            mode = self.profiles.active()["body"].get("performance_mode")
            if mode and mode != self.settings.get("performance.mode"):
                self.settings.set("performance.mode", mode)
        self.db.log_event("INFO", "profiles", f"{event}: {name}")

    # ---------------- capture ----------------
    def capture_factory(self):
        """Build a source factory from current input settings."""
        from vcstudio.core.video_capture import AVFoundationSource, CaptureError, TestPatternSource
        i = self.settings.get("input")
        w, h, fps = i["preferred_width"], i["preferred_height"], i["preferred_fps"]
        if i["method"] == "test_pattern":
            return lambda: TestPatternSource(w, h, fps)
        if i["method"] == "obs_ipc":
            from vcstudio.core.obs_shm_source import ObsShmSource
            return lambda: ObsShmSource()
        return lambda: AVFoundationSource(i["device_unique_id"], i["device_name_hint"], w, h, fps)

    def start_capture(self) -> None:
        risk = self.feedback_loop_risk()
        if risk:
            log.error(risk)
            self.db.log_event("ERROR", "pipeline", risk)
            raise RuntimeError(risk)
        i = self.settings.get("input")
        sup = self.capture
        sup.missing_timeout = i["missing_input_timeout_ms"] / 1000
        sup.frozen_timeout = i["frozen_frame_timeout_ms"] / 1000
        sup.auto_reconnect = i["auto_reconnect"]
        log.info("Starting capture (%s)", i["method"])
        sup.start(self.capture_factory())
        self.ai.start()
        self.pipeline.start()
        self.watchdog.start()

    def feedback_loop_risk(self) -> str:
        """Non-empty if the configured input is the device we output to (would feed back into itself)."""
        i, o = self.settings.get("input"), self.settings.get("output")
        if i["method"] == "avfoundation" and o["camera_enabled"] and \
                (i.get("device_name_hint") or "").casefold() == o["camera_device"].casefold():
            return f"Input '{i['device_name_hint']}' is also the output camera: this would feed back into itself."
        return ""

    def stop_capture(self) -> None:
        self.watchdog.stop()
        self.pipeline.stop()
        self.ai.stop()
        self.capture.stop()
        self.replacement.compositor.release_resources()

    def shutdown(self) -> None:
        log.info("Shutting down")
        self.watchdog.stop()
        self.pipeline.stop()
        self.replacement.close()
        self.ai.stop()
        self.capture.stop()
        self.db.log_event("INFO", "app", "stopped")
        self.db.end_session()
        self.settings.save()
        self.db.close()
