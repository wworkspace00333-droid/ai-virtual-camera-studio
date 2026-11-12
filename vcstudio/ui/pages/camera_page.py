from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem)

from vcstudio import CAMERA_NAME
from vcstudio.camera import camera_manager as cm
from vcstudio.camera.output import OUTPUT_TARGETS, target_for
from vcstudio.core.system_info import camera_extensions
from vcstudio.ui.widgets import Card, StatCard, page_scaffold
from vcstudio.i18n import tr


class _Sig(QObject):
    done = Signal(str, str)     # (kind, message)


class VirtualCameraPage:
    key, title = "camera", "Virtual Camera"

    def __init__(self, ctx):
        self.ctx = ctx
        self.widget, lay = page_scaffold("Virtual Camera", tr(
            "Where processed video goes. “{camera}” is this app's own CoreMediaIO camera extension; OBS Virtual "
            "Camera works without an Apple Developer account.", camera=CAMERA_NAME))
        tgt = Card("Output camera")
        tf = QFormLayout()
        self.target = QComboBox()
        for k, t in OUTPUT_TARGETS.items():
            self.target.addItem(tr(t.label), k)
        tf.addRow(tr("Send processed video to"), self.target)
        tgt.layout_.addLayout(tf)
        self.target_note = QLabel(); self.target_note.setObjectName("muted"); self.target_note.setWordWrap(True)
        tgt.layout_.addWidget(self.target_note)
        lay.addWidget(tgt)
        self._sync_target()
        self.target.currentIndexChanged.connect(self._target_changed)

        st = Card("Status")
        g = QGridLayout(); g.setSpacing(10)
        self.cards = {n: StatCard(n) for n in ("Host app", "Extension", "Camera device", "Output")}
        for i, c in enumerate(self.cards.values()):
            g.addWidget(c, 0, i)
        st.layout_.addLayout(g)
        self.msg = QLabel(); self.msg.setWordWrap(True)
        st.layout_.addWidget(self.msg)
        row = QHBoxLayout()
        self.install_btn = QPushButton(tr("Install / update")); self.install_btn.setObjectName("primary")
        self.uninstall_btn = QPushButton(tr("Uninstall")); self.uninstall_btn.setObjectName("danger")
        self.approve_btn = QPushButton(tr("Open approval settings"))
        self.refresh_btn = QPushButton(tr("Refresh"))
        for b in (self.install_btn, self.uninstall_btn, self.approve_btn, self.refresh_btn):
            row.addWidget(b)
        row.addStretch()
        st.layout_.addLayout(row)
        lay.addWidget(st)

        chain = Card("Check the whole chain")
        chint = QLabel(tr("One click: the app pauses its own processing, sends a 1080p60 test scene and then your OBS "
                          "video through AI and Metal into the selected camera, receives it like QuickTime or Zoom "
                          "would, and shows frame rate, dropped frames, latency and whether the processing is "
                          "visible. About 30 seconds."))
        chint.setObjectName("muted"); chint.setWordWrap(True)
        chain.layout_.addWidget(chint)
        crow = QHBoxLayout()
        self.chain_btn = QPushButton(tr("Check the whole chain")); self.chain_btn.setObjectName("primary")
        self.reports_btn = QPushButton(tr("Open reports folder"))
        crow.addWidget(self.chain_btn); crow.addWidget(self.reports_btn); crow.addStretch()
        chain.layout_.addLayout(crow)
        self.chain_info = QLabel(); self.chain_info.setWordWrap(True)
        self.chain_info.setTextInteractionFlags(self.chain_info.textInteractionFlags() | Qt.TextSelectableByMouse)
        chain.layout_.addWidget(self.chain_info)
        lay.addWidget(chain)

        test = Card("Verify in other apps")
        hint = QLabel(tr("Send a moving, numbered test pattern to the selected output camera, then select that camera "
                      "in FaceTime, QuickTime Player (File > New Movie Recording), Photo Booth or Zoom. The loopback "
                      "test captures the camera like a client app and measures delivered frames and latency."))
        self.hint = hint
        hint.setObjectName("muted"); hint.setWordWrap(True)
        test.layout_.addWidget(hint)
        trow = QHBoxLayout()
        self.pattern_btn = QPushButton(tr("Start test pattern")); self.pattern_btn.setCheckable(True)
        self.loop_btn = QPushButton(tr("Run loopback test"))
        trow.addWidget(self.pattern_btn); trow.addWidget(self.loop_btn); trow.addStretch()
        test.layout_.addLayout(trow)
        self.test_info = QLabel(); self.test_info.setObjectName("muted"); self.test_info.setWordWrap(True)
        test.layout_.addWidget(self.test_info)
        lay.addWidget(test)

        ext = Card("Installed camera extensions (systemextensionsctl)")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("Name"), tr("Bundle ID"), tr("Team"), tr("State")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(110)
        ext.layout_.addWidget(self.table)
        lay.addWidget(ext)
        lay.addStretch()

        self._sig = _Sig(); self._sig.done.connect(self._done)
        self.sender = None
        self.install_btn.clicked.connect(lambda: self._async("install", cm.activate))
        self.uninstall_btn.clicked.connect(self._uninstall)
        self.approve_btn.clicked.connect(cm.open_approval_settings)
        self.refresh_btn.clicked.connect(self.refresh)
        self.pattern_btn.toggled.connect(self._toggle_pattern)
        self.loop_btn.clicked.connect(self._loopback)
        self.chain_btn.clicked.connect(self.check_chain)
        self.reports_btn.clicked.connect(self._open_reports)
        self._chain = None
        self.timer = QTimer(self.widget); self.timer.timeout.connect(self._tick); self.timer.start(500)
        self.refresh()

    # ---- output target
    def output_device(self) -> tuple[str, str]:
        o = self.ctx.settings.get("output")
        return o["camera_device"], o["camera_sink"]

    def _sync_target(self):
        dev, sink = self.output_device()
        key = target_for(dev, sink)
        if key == "custom" and self.target.findData("custom") < 0:
            self.target.addItem(tr("Custom: {dev} / {sink}", dev=dev, sink=sink), "custom")
        self.target.blockSignals(True)
        self.target.setCurrentIndex(self.target.findData(key))
        self.target.blockSignals(False)
        t = OUTPUT_TARGETS.get(key)
        note = tr(t.note) if t else tr("Set in settings.json (output.camera_device / output.camera_sink).")
        risk = self.ctx.feedback_loop_risk()
        if risk:   # shown inline (no modal): start_capture refuses this configuration anyway
            note = (f"<span style='color:#f85149'>{tr(risk)} " + tr("Switch the input to Direct OBS frame transfer "
                    "on the Input page.") + f"</span><br>{note}")
        self.target_note.setText(note)

    def _target_changed(self, _i):
        key = self.target.currentData()
        t = OUTPUT_TARGETS.get(key)
        if t is None:
            return
        if self.sender:
            self.pattern_btn.setChecked(False)
        s = self.ctx.settings
        s.set("output.camera_device", t.device, persist=False)
        s.set("output.camera_sink", t.sink, persist=False)
        if key == "obs" and self.ctx.feedback_loop_risk():
            # OBS Virtual Camera can't be input and output at once: read OBS through the plugin instead
            s.set("input.method", "obs_ipc", persist=False)
        s.save()
        self._sync_target()
        self.refresh()

    def selected_device_visible(self) -> bool:
        dev, _ = self.output_device()
        if dev == CAMERA_NAME:
            return self.status.device_visible
        try:
            from vcstudio.core.devices import list_video_devices
            return any(d.name == dev for d in list_video_devices())
        except Exception:
            return False

    def refresh(self):
        s = cm.status()
        self.status = s
        self._sync_target()
        self.cards["Host app"].set("Installed" if s.host_app else "Not found", s.host_app or "/Applications", "ok" if s.host_app else "warn")
        state = {"activated_enabled": "ok"}.get(s.extension_state, "warn" if s.extension_state != "not_installed" else "idle")
        self.cards["Extension"].set(s.extension_state.replace("_", " ").title(), s.team_id and tr("team {id}", id=s.team_id), state)
        self.cards["Camera device"].set("Visible" if s.device_visible else "Not visible", CAMERA_NAME,
                                        "ok" if s.device_visible else "idle")
        self.msg.setText(tr(s.message))
        self.install_btn.setEnabled(s.host_app is not None)
        self.uninstall_btn.setEnabled(s.extension_state != "not_installed")
        visible = self.selected_device_visible()
        dev, _ = self.output_device()
        if dev != CAMERA_NAME:
            self.cards["Camera device"].set("Visible" if visible else "Not visible", dev, "ok" if visible else "warn")
        for b in (self.pattern_btn, self.loop_btn):
            b.setEnabled(visible)
        exts = camera_extensions()
        self.table.setRowCount(len(exts))
        for i, e in enumerate(exts):
            for j, v in enumerate((e["name"], e["bundle_id"], e["team_id"], e["state"])):
                self.table.setItem(i, j, QTableWidgetItem(v))
        self.table.resizeColumnsToContents()

    def _async(self, kind, fn):
        self.install_btn.setEnabled(False)
        self.msg.setText(tr("Working… macOS may ask you to approve the extension."))

        def work():
            rc, out = fn()
            self._sig.done.emit(kind, f"{rc}|{out}")
        threading.Thread(target=work, daemon=True).start()

    def _uninstall(self):
        if QMessageBox.question(self.widget, tr("Uninstall"), tr("Remove “{camera}” from this Mac?", camera=CAMERA_NAME)) == QMessageBox.Yes:
            self._async("uninstall", cm.deactivate)

    def _done(self, kind, payload):
        rc, _, out = payload.partition("|")
        self.refresh()
        if kind in ("install", "uninstall"):
            extra = {"2": "Approve it in System Settings > General > Login Items & Extensions > Camera Extensions.",
                     "3": "Restart the Mac to finish."}.get(rc, "")
            self.msg.setText((f"{tr(kind.title())}: " + tr("exit {rc}.", rc=rc) + f" {out} {tr(extra)}").strip())
        elif kind == "loopback":
            self.test_info.setText(out)
            self.loop_btn.setEnabled(True)

    def _toggle_pattern(self, on: bool):
        from vcstudio.camera.output import CameraOutputError, VirtualCameraOutput
        from vcstudio.camera.test_sender import TestPatternSender
        if on:
            try:
                self.sender = TestPatternSender(VirtualCameraOutput(*self.output_device()),
                                                fps=self.ctx.settings.get("output.fps"))
                self.sender.start()
                self.pattern_btn.setText(tr("Stop test pattern"))
            except CameraOutputError as e:
                self.pattern_btn.setChecked(False)
                QMessageBox.warning(self.widget, tr("Virtual camera"), tr(str(e)))
        elif self.sender:
            self.sender.stop()
            self.sender = None
            self.pattern_btn.setText(tr("Start test pattern"))

    def _loopback(self):
        self.loop_btn.setEnabled(False)
        self.test_info.setText(tr("Running loopback test (6 s)…"))

        def work():
            from vcstudio.app.selftest import camera_loopback_selftest
            out = Path(tempfile.mkdtemp()) / "loop.json"
            dev, sink = self.output_device()
            camera_loopback_selftest(str(out), 6.0, dev, sink)
            r = json.loads(out.read_text())
            msg = (tr("PASS") if r.get('ok') else tr("FAIL")) + ": " + tr(
                "{got} of {sent} frames received in order={order}, latency median {med} ms / p95 {p95} ms, client "
                "resolution {res}", got=r.get('distinct_received'), sent=r.get('sent'), order=r.get('order_ok'),
                med=r.get('latency_ms_median'), p95=r.get('latency_ms_p95'), res=r.get('capture_resolution')) + (
                ". " + tr("Error: {e}", e=r['error']) if r.get("error") else "")
            self._sig.done.emit("loopback", f"0|{msg}")
        threading.Thread(target=work, daemon=True).start()

    def check_chain(self):
        from vcstudio.ui.chain_check import ChainCheck
        if self.sender:
            self.pattern_btn.setChecked(False)
        dev, sink = self.output_device()
        self.chain_btn.setEnabled(False)
        self.chain_info.setStyleSheet("")
        self._chain = ChainCheck(self.ctx, dev, sink, self.widget)
        self._chain.progress.connect(self.chain_info.setText)
        self._chain.finished.connect(self._chain_done)
        self._chain.start()

    def _chain_done(self, results):
        from vcstudio.ui.chain_check import summarize
        self.chain_btn.setEnabled(True)
        ok = bool(results) and all(r.get("ok") for _, r, _ in results)
        lines = [f"{title}: {summarize(r)}" for title, r, _ in results]
        if len(results) == 1:
            lines.append(tr("Your OBS video was not checked: OBS is not sharing frames (open OBS with the "
                            "vcstudio-obs plugin)."))
        lines.append(tr("Reports: {path}", path=str(self.ctx.paths.root / "reports")))
        self.chain_info.setStyleSheet("color: #3fb950;" if ok else "color: #f85149;")
        self.chain_info.setText("\n".join(lines))
        self.last_chain = results

    def _open_reports(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        d = self.ctx.paths.root / "reports"
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _tick(self):
        if not self.widget.isVisible():
            return
        if self.sender and self.sender.running:
            self.cards["Output"].set("Test pattern", tr("sent {s} · dropped {d}", s=self.sender.sent, d=self.sender.dropped), "ok")
        elif self.sender and self.sender.error:
            self.cards["Output"].set("Error", self.sender.error, "error")
        else:
            self.cards["Output"].set("Idle", "start capture to feed the camera")

    def shutdown(self):
        if self.sender:
            self.sender.stop()
