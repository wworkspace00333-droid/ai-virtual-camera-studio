from __future__ import annotations

import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QWidget)

from vcstudio import CAMERA_NAME
from vcstudio.core.config import SettingsError
from vcstudio.core.devices import camera_authorization_status, list_video_devices
from vcstudio.core.video_capture import formats_for_device_id
from vcstudio.ui.video_view import VideoView
from vcstudio.ui.widgets import Card, StatCard, page_scaffold

log = logging.getLogger(__name__)

METHODS = [("avfoundation", "Camera device (AVFoundation): OBS Virtual Camera"),
           ("obs_ipc", "Direct OBS frame transfer (vcstudio-obs plugin)"),
           ("test_pattern", "Test pattern (diagnostics)")]
PERMISSION_HINT = {
    "not_determined": "macOS will ask for camera permission when capture starts.",
    "denied": "Denied. Enable it in System Settings → Privacy & Security → Camera, then restart the app.",
    "restricted": "Restricted by a device-management policy.",
    "authorized": "Granted.",
}


class InputPage:
    key, title = "input", "Input"

    def __init__(self, ctx, feeder):
        self.ctx = ctx
        self.widget, lay = page_scaffold("Input", "Choose the video source and capture format. "
                                         f"The app never captures from its own output camera ({CAMERA_NAME}).")
        top = QHBoxLayout()
        src = Card("Source")
        f = QFormLayout()
        self.method = QComboBox()
        for key, label in METHODS:
            self.method.addItem(label, key)
        self.device = QComboBox()
        self.device.setMinimumWidth(320)
        self.format = QComboBox()
        self.fps = QComboBox()
        for v in (60, 30):
            self.fps.addItem(f"{v} FPS", v)
        self.reconnect = QCheckBox("Reconnect automatically")
        self.missing = QSpinBox(); self.missing.setRange(200, 60000); self.missing.setSuffix(" ms")
        self.frozen = QSpinBox(); self.frozen.setRange(200, 60000); self.frozen.setSuffix(" ms")
        f.addRow("Method", self.method)
        f.addRow("Device", self.device)
        f.addRow("Format", self.format)
        f.addRow("Frame rate", self.fps)
        f.addRow("", self.reconnect)
        f.addRow("No-signal timeout", self.missing)
        f.addRow("Frozen-frame timeout", self.frozen)
        src.layout_.addLayout(f)
        btns = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh devices")
        self.start_btn = QPushButton("Start capture"); self.start_btn.setObjectName("primary")
        self.stop_btn = QPushButton("Stop capture")
        for b in (self.refresh_btn, self.start_btn, self.stop_btn):
            btns.addWidget(b)
        btns.addStretch()
        src.layout_.addLayout(btns)
        self.perm = QLabel(); self.perm.setObjectName("muted"); self.perm.setWordWrap(True)
        src.layout_.addWidget(self.perm)
        # direct OBS transfer helpers
        self.obs_row = QWidget(); orow = QHBoxLayout(self.obs_row); orow.setContentsMargins(0, 0, 0, 0)
        self.obs_status = QLabel(); self.obs_status.setWordWrap(True)
        self.obs_install = QPushButton("Install OBS plugin")
        orow.addWidget(self.obs_status, 1); orow.addWidget(self.obs_install)
        src.layout_.addWidget(self.obs_row)
        self.obs_install.clicked.connect(self._install_obs_plugin)
        top.addWidget(src, 3)
        prev = Card("Input monitor")
        self.view = VideoView()
        self.view.setMinimumHeight(240)
        feeder.add_view(self.view, detections=True)
        prev.layout_.addWidget(self.view)
        top.addWidget(prev, 4)
        lay.addLayout(top)

        stats = Card("Input statistics")
        g = QGridLayout(); g.setSpacing(10)
        names = ["State", "Resolution", "Measured FPS", "Latency", "Frames", "Dropped", "Last frame", "Reconnects"]
        self.cards = {n: StatCard(n) for n in names}
        for i, n in enumerate(names):
            g.addWidget(self.cards[n], i // 4, i % 4)
        stats.layout_.addLayout(g)
        self.err = QLabel(); self.err.setWordWrap(True); self.err.setStyleSheet("color: #f85149;")
        stats.layout_.addWidget(self.err)
        lay.addWidget(stats)

        devs = Card("All video devices")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Model", "Usable as input", "Unique ID"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(140)
        devs.layout_.addWidget(self.table)
        lay.addWidget(devs)
        lay.addStretch()

        self._load_settings()
        self.refresh_devices()
        self.method.currentIndexChanged.connect(self._method_changed)
        self.device.currentIndexChanged.connect(self._device_changed)
        self.refresh_btn.clicked.connect(self.refresh_devices)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.ctx.stop_capture)
        self.timer = QTimer(self.widget)
        self.timer.timeout.connect(self.update_stats)
        self.timer.start(250)
        self._method_changed()

    # ------------------------------------------------------------------
    def _load_settings(self):
        s = self.ctx.settings
        self.method.setCurrentIndex(max(0, self.method.findData(s.get("input.method"))))
        self.fps.setCurrentIndex(max(0, self.fps.findData(s.get("input.preferred_fps"))))
        self.reconnect.setChecked(s.get("input.auto_reconnect"))
        self.missing.setValue(s.get("input.missing_input_timeout_ms"))
        self.frozen.setValue(s.get("input.frozen_frame_timeout_ms"))

    def _method_changed(self, *_):
        m = self.method.currentData()
        is_dev = m == "avfoundation"
        self.device.setEnabled(is_dev)
        self.format.setEnabled(is_dev)
        self.fps.setEnabled(m != "obs_ipc")          # OBS decides resolution and frame rate
        self.obs_row.setVisible(m == "obs_ipc")
        self._refresh_obs_status()

    def _refresh_obs_status(self):
        if self.method.currentData() != "obs_ipc":
            return
        from vcstudio.core import obs_plugin
        from vcstudio.core.obs_shm_source import obs_sharing_status
        st = obs_sharing_status()
        inst = obs_plugin.installed_plugin()
        ver = obs_plugin.obs_version()
        if st["available"]:
            self.obs_status.setText(f"<span style='color:#3fb950'>●</span> OBS is sharing {st['width']}×{st['height']} "
                                    f"@ {st['fps']:.0f} fps (OBS pid {st['writer_pid']}).")
        elif inst is None:
            self.obs_status.setText("Plugin not installed in OBS" + (f" {ver}" if ver else "") +
                                    ". Click Install, restart OBS, then Tools > AI Virtual Camera Studio: share frames.")
        else:
            self.obs_status.setText(f"Plugin installed. {st['reason']}.")
        self.obs_install.setText("Reinstall OBS plugin" if inst else "Install OBS plugin")
        self.obs_install.setEnabled(ver is not None)

    def _install_obs_plugin(self):
        from vcstudio.core import obs_plugin
        try:
            QMessageBox.information(self.widget, "OBS plugin", obs_plugin.install())
        except Exception as e:
            QMessageBox.warning(self.widget, "OBS plugin", str(e))
        self._refresh_obs_status()

    def refresh_devices(self):
        devs = list_video_devices()
        want = self.ctx.settings.get("input.device_unique_id")
        hint = self.ctx.settings.get("input.device_name_hint") or ""
        self.device.blockSignals(True)
        self.device.clear()
        sel = -1
        for d in devs:
            label = d.name + ("  (this app's output, unusable)" if d.is_own_output else "")
            self.device.addItem(label, d.unique_id)
            if d.is_own_output:
                self.device.model().item(self.device.count() - 1).setEnabled(False)
            elif sel < 0 and (d.unique_id == want or (not want and hint and hint.casefold() in d.name.casefold())):
                sel = self.device.count() - 1
        if sel < 0:
            sel = next((i for i, d in enumerate(devs) if d.is_obs and not d.is_own_output),
                       next((i for i, d in enumerate(devs) if not d.is_own_output), -1))
        self.device.setCurrentIndex(sel)
        self.device.blockSignals(False)
        self._device_changed()
        self.table.setRowCount(len(devs))
        for i, d in enumerate(devs):
            usable = f"No: this is {CAMERA_NAME} (feedback loop)" if d.is_own_output else ("Yes (OBS)" if d.is_obs else "Yes")
            for j, v in enumerate((d.name, d.model_id, usable, d.unique_id)):
                self.table.setItem(i, j, QTableWidgetItem(v))
        self.table.resizeColumnsToContents()
        st = camera_authorization_status()
        self.perm.setText(f"Camera permission: {st}. {PERMISSION_HINT.get(st, '')}")

    def _device_changed(self, *_):
        uid = self.device.currentData()
        self.format.clear()
        fmts = formats_for_device_id(uid) if uid else []
        seen = set()
        for fm in sorted(fmts, key=lambda x: (-x.width * x.height, -x.max_fps)):
            if (fm.width, fm.height) in seen:
                continue
            seen.add((fm.width, fm.height))
            self.format.addItem(fm.label, (fm.width, fm.height))
        if not fmts:
            for w, h in ((1920, 1080), (1280, 720)):
                self.format.addItem(f"{w}×{h}", (w, h))
        want = (self.ctx.settings.get("input.preferred_width"), self.ctx.settings.get("input.preferred_height"))
        idx = self.format.findData(want)
        self.format.setCurrentIndex(idx if idx >= 0 else 0)

    def apply_to_settings(self) -> bool:
        s = self.ctx.settings
        try:
            s.set("input.method", self.method.currentData(), persist=False)
            if self.method.currentData() == "avfoundation":
                s.set("input.device_unique_id", self.device.currentData(), persist=False)
                name = self.device.currentText().split("  (")[0]
                s.set("input.device_name_hint", name, persist=False)
            w, h = self.format.currentData() or (1920, 1080)
            s.set("input.preferred_width", w, persist=False)
            s.set("input.preferred_height", h, persist=False)
            s.set("input.preferred_fps", self.fps.currentData(), persist=False)
            s.set("input.auto_reconnect", self.reconnect.isChecked(), persist=False)
            s.set("input.missing_input_timeout_ms", self.missing.value(), persist=False)
            s.set("input.frozen_frame_timeout_ms", self.frozen.value(), persist=False)
            s.save()
            return True
        except SettingsError as e:
            QMessageBox.warning(self.widget, "Input", str(e))
            return False

    def start(self):
        if self.method.currentData() == "avfoundation" and self.device.currentData() is None:
            QMessageBox.warning(self.widget, "Input", "No usable video device selected.")
            return
        if self.apply_to_settings():
            try:
                self.ctx.start_capture()
            except RuntimeError as e:
                QMessageBox.warning(self.widget, "Input", str(e))

    def update_stats(self):
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        if self._tick_n % 8 == 0:
            self._refresh_obs_status()
        s = self.ctx.capture.stats()
        state_level = {"running": "ok", "frozen": "warn", "reconnecting": "warn", "no_signal": "error",
                       "error": "error"}.get(s.state, "idle")
        self.cards["State"].set(s.state.replace("_", " ").title(), s.source_name or "", state_level)
        live = s.state not in ("stopped", "error")
        if live and s.width:
            self.cards["Resolution"].set(f"{s.width}×{s.height}", f"{s.pixel_format} · nominal {s.nominal_fps:g} fps")
        else:
            self.cards["Resolution"].set("—", "")
        fps_level = "ok" if live and s.measured_fps >= 0.9 * max(1, s.nominal_fps) else ("warn" if live else "idle")
        self.cards["Measured FPS"].set(f"{s.measured_fps:.1f}" if live else "—", "1 s sliding window", fps_level)
        self.cards["Latency"].set(f"{s.latency_ms:.1f} ms" if live and s.frames else "—", "frame timestamp → app")
        self.cards["Frames"].set(f"{s.frames:,}", f"uptime {s.session_uptime_s:.0f} s" if live else "")
        self.cards["Dropped"].set(f"{s.dropped_by_source}", f"late-frame discards; {s.replaced_unconsumed} superseded in slot")
        age = f"{s.last_frame_age_ms:.0f} ms ago" if live and s.frames else "—"
        frozen = f"identical for {s.frozen_for_s:.1f} s" if s.frozen_for_s else ""
        self.cards["Last frame"].set(age, frozen, "warn" if frozen else None)
        self.cards["Reconnects"].set(str(s.reconnects), "")
        self.err.setText(s.last_error if s.state in ("error", "reconnecting", "no_signal") else "")
        self.start_btn.setEnabled(s.state in ("stopped", "error"))
        self.stop_btn.setEnabled(s.state not in ("stopped",))
