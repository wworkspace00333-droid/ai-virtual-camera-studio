from __future__ import annotations

import os

import logging

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QMainWindow, QStackedWidget, QWidget

from vcstudio import APP_NAME, __version__
from vcstudio.core.profiles import ProfileError
from vcstudio.i18n import tr
from vcstudio.ui.actions import ActionRegistry
from vcstudio.ui.pages.dashboard import DashboardPage
from vcstudio.ui.pages.logs import LogsPage
from vcstudio.ui.pages.performance_page import PerformancePage
from vcstudio.ui.pages.profiles import ProfilesPage
from vcstudio.ui.pages.settings_page import SettingsPage
from vcstudio.ui.pages.input_page import InputPage
from vcstudio.ui.pages.preview_page import PreviewPage
from vcstudio.ui.pages.ai_page import AIEnginePage
from vcstudio.ui.pages.detection_page import DetectionPage
from vcstudio.ui.pages.roi_page import ROIPage
from vcstudio.ui.pages.replacement_page import ReplacementPage
from vcstudio.ui.pages.camera_page import VirtualCameraPage
from vcstudio.ui.video_view import PreviewFeeder

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1360, 860)
        self.setMinimumSize(1100, 700)
        self.actions_ = ActionRegistry(self)
        self.actions_.triggered.connect(self.on_action)
        from vcstudio.native.hotkeys import GlobalHotkeys
        self.global_hotkeys = GlobalHotkeys()
        self.apply_hotkeys()

        self.feeder = PreviewFeeder(ctx.capture, ctx.settings.get("ui.preview_max_fps"), self)
        self.feeder.ai = ctx.ai
        self.dashboard = DashboardPage(ctx)
        self.performance = PerformancePage(ctx)
        self.logs = LogsPage(ctx)
        self.input_page = InputPage(ctx, self.feeder)
        self.pages = [self.dashboard, self.input_page, PreviewPage(ctx, self.feeder, lambda: self.goto("input"), self.start_processing), AIEnginePage(ctx), DetectionPage(ctx, self.feeder),
                      ROIPage(ctx, self.feeder), ReplacementPage(ctx), ProfilesPage(ctx, self), VirtualCameraPage(ctx),
                      self.performance, self.logs, SettingsPage(ctx, self.actions_, self)]
        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(210)
        self.stack = QStackedWidget()
        for i, p in enumerate(self.pages, 1):
            self.nav.addItem(f"{i:>2}   {tr(p.title)}")
            self.stack.addWidget(p.widget)
        self.nav.currentRowChanged.connect(self._goto)
        h.addWidget(self.nav)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.status_profile = QLabel()
        self.status_res = QLabel()
        self.status_capture = QLabel(tr("Capture: {state}", state=tr("stopped")))
        self.statusBar().addWidget(self.status_capture)
        self.statusBar().addPermanentWidget(self.status_res)
        self.statusBar().addPermanentWidget(self.status_profile)
        ctx.profiles.add_listener(lambda *_: self._profile_changed())
        self._profile_changed()

        keys = [p.key for p in self.pages]
        last = ctx.settings.get("ui.last_page")
        self.nav.setCurrentRow(keys.index(last) if last in keys else 0)
        geo = ctx.settings.get("ui.window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(ctx.settings.get("performance.monitor_interval_ms"))
        self._slow = 0
        self._tick()
        if ctx.settings.get("input.autostart") and not os.environ.get("VCS_NO_AUTOSTART"):
            QTimer.singleShot(500, self._autostart)

    def _autostart(self) -> None:
        """Start capture on launch; failures (no OBS, feedback-loop risk) are shown on the pages, not raised."""
        if self.ctx.capture.stats().state not in ("stopped", "error"):
            return
        try:
            self.ctx.start_capture()
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning("Autostart skipped: %s", e)

    def apply_hotkeys(self) -> dict[str, str]:
        """Global (system-wide) hotkeys when enabled and possible; in-app shortcuts for the rest."""
        bindings = self.ctx.settings.get("hotkeys")
        errors: dict[str, str] = {}
        in_app = dict(bindings)
        import os
        if self.ctx.settings.get("ui.global_hotkeys") and not os.environ.get("VCS_DISABLE_GLOBAL_HOTKEYS"):
            errors = self.global_hotkeys.apply(bindings, self.on_action)
            for action in bindings:
                if action not in errors:
                    in_app[action] = ""          # handled globally; avoid double triggering
        else:
            self.global_hotkeys.clear()
        self.actions_.apply_bindings(in_app)
        for a, e in errors.items():
            log.warning("Hotkey %s: %s", a, e)
        self.hotkey_errors = errors
        return errors

    def start_processing(self) -> bool:
        from PySide6.QtWidgets import QMessageBox
        try:
            self.ctx.start_capture()
            return True
        except RuntimeError as e:
            QMessageBox.warning(self, tr("Cannot start"), tr(str(e)))
            return False

    def page_keys(self) -> list[str]:
        return [p.key for p in self.pages]

    def goto(self, key: str) -> None:
        self.nav.setCurrentRow(self.page_keys().index(key))

    def _goto(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        self.ctx.settings.set("ui.last_page", self.pages[row].key, persist=False)
        if self.pages[row] is self.logs:
            self.logs.refresh_events()

    def _profile_changed(self) -> None:
        self.status_profile.setText(tr("Profile: {name}", name=self.ctx.profiles.active_name))
        self.dashboard.refresh_profile()
        self.performance.sync_mode()

    def _tick(self) -> None:
        cs = self.ctx.capture.stats()
        self.dashboard.on_capture(cs)
        ps = self.ctx.pipeline.stats()
        self.dashboard.on_pipeline(ps)
        self.performance.on_pipeline(ps, self.ctx.ai.stats(), self.ctx.watchdog.stats)
        self.dashboard.on_ai(self.ctx.ai.stats())
        self.status_capture.setText(tr("Capture: {state}", state=tr(cs.state.replace('_', ' ')))
                                    + (f" · {cs.measured_fps:.0f} fps" if cs.state in ("running", "frozen") else "")
                                    + ("   |   " + tr("Output: {state} · {fps} fps · camera {camera}", state=tr(ps.state),
                                                       fps=f"{ps.output_fps:.0f}",
                                                       camera=tr(ps.camera_state.replace('_', ' ')))
                                       if ps.state != "stopped" else ""))
        s = self.ctx.monitor.sample()
        self.dashboard.on_sample(s)
        self.performance.on_sample(s)
        self.status_res.setText(tr("CPU {cpu}% · App {mb} MB", cpu=f"{s.cpu_percent:.0f}", mb=f"{s.process_rss / 2**20:.0f}"))
        self._slow += 1
        if self._slow % 30 == 0:
            self.dashboard.refresh_extension()
            self.dashboard.refresh_profile()

    def on_action(self, aid: str) -> None:
        log.info("Action: %s", aid)
        pipe = self.ctx.pipeline
        if aid == "start_processing":
            if self.ctx.capture.stats().state == "stopped":
                self.start_processing()
        elif aid == "stop_processing":
            self.ctx.stop_capture()
        elif aid == "emergency_passthrough":
            pipe.set_passthrough(not pipe.passthrough, "manual (hotkey/button)")
        elif aid == "toggle_replacement":
            pipe.set_replacement_enabled(not pipe.replacement_enabled)
        elif aid == "toggle_ai":
            enabled = not self.ctx.profiles.active()["body"]["ai"].get("enabled", True)
            self.ctx.update_active_profile(lambda b: b["ai"].__setitem__("enabled", enabled))
            log.info("AI %s", "enabled" if enabled else "disabled")
        elif aid == "next_profile":
            try:
                self.ctx.profiles.switch(self.ctx.profiles.next_name())
            except ProfileError as e:
                log.error("switch profile failed: %s", e)
        elif aid == "toggle_preview":
            if self.stack.currentWidget() is self.pages[self.page_keys().index("preview")].widget:
                self.goto("dashboard")
            else:
                self.goto("preview")

    def closeEvent(self, e):  # noqa: N802
        self.timer.stop()
        self.global_hotkeys.clear()
        self.pages[self.page_keys().index("camera")].shutdown()
        self.ctx.stop_capture()
        self.ctx.settings.set("ui.window_geometry", bytes(self.saveGeometry().toBase64()).decode(), persist=False)
        self.ctx.settings.save()
        super().closeEvent(e)
