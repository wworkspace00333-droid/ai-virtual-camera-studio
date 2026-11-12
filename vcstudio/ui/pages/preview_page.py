from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton

from vcstudio.ui.video_view import BLACK_INPUT_HINT, STATE_OVERLAY, VideoView, frame_is_black, frame_to_qimage
from vcstudio.ui.widgets import page_scaffold
from vcstudio.i18n import tr


class PreviewPage:
    """Original | processed, both taken from the same pipeline pair, so they show the same moment."""
    key, title = "preview", "Preview"

    def __init__(self, ctx, feeder, goto_input, start_fn):
        self.ctx, self.feeder = ctx, feeder
        self.widget, lay = page_scaffold("Preview", "Left: the captured input. Right: exactly what the virtual camera "
                                         "receives. Both previews show the same frame.")
        bar = QHBoxLayout()
        self.start_btn = QPushButton(tr("Start")); self.start_btn.setObjectName("primary")
        self.stop_btn = QPushButton(tr("Stop"))
        self.pause_btn = QPushButton(tr("Pause output")); self.pause_btn.setCheckable(True)
        self.pass_btn = QPushButton(tr("Emergency passthrough")); self.pass_btn.setObjectName("danger"); self.pass_btn.setCheckable(True)
        self.repl_btn = QPushButton(tr("Replacement on")); self.repl_btn.setCheckable(True); self.repl_btn.setChecked(True)
        self.face_btn = QPushButton(tr("Hide face")); self.face_btn.setCheckable(True)
        self.face_btn.setToolTip(tr("Pixelates every detected face in the camera output (adds a region on the "
                                    "Region Editor page; click again to remove it)."))
        self.card_btn = QPushButton(tr("Card for a game…"))
        self.card_btn.setToolTip(tr("Put a photo into the frame a browser game or an emulator draws over the camera."))
        self.boxes = QCheckBox(tr("Show detections")); self.boxes.setChecked(True)
        src_btn = QPushButton(tr("Input settings…"))
        # two rows: one row of all controls is wider than a 1100 px window in Russian
        for b in (self.start_btn, self.stop_btn, self.pause_btn, self.pass_btn):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(src_btn)
        lay.addLayout(bar)
        bar2 = QHBoxLayout()
        for b in (self.repl_btn, self.face_btn, self.card_btn, self.boxes):
            bar2.addWidget(b)
        bar2.addStretch()
        lay.addLayout(bar2)
        # own line: a long source description in the button bar would widen the page past the window
        self.info = QLabel(); self.info.setObjectName("muted"); self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.note = QLabel(); self.note.setWordWrap(True); self.note.setStyleSheet("color: #d29922;")
        self.note.hide()
        lay.addWidget(self.note)
        views = QHBoxLayout()
        self.original = VideoView("ORIGINAL")
        self.processed = VideoView("PROCESSED  →  AI Virtual Camera")
        for v in (self.original, self.processed):
            v.setMinimumHeight(380)
            views.addWidget(v)
        lay.addLayout(views, 1)
        self.stats = QLabel(); self.stats.setObjectName("muted"); self.stats.setWordWrap(True)
        lay.addWidget(self.stats)

        self.start_btn.clicked.connect(start_fn)
        self.stop_btn.clicked.connect(ctx.stop_capture)
        self.pause_btn.toggled.connect(lambda on: ctx.pipeline.set_paused(on))
        self.pass_btn.toggled.connect(lambda on: ctx.pipeline.set_passthrough(on, "manual (button)")
                                      if on != ctx.pipeline.passthrough else None)
        self.repl_btn.toggled.connect(lambda on: ctx.pipeline.set_replacement_enabled(on)
                                      if on != ctx.pipeline.replacement_enabled else None)
        self.boxes.toggled.connect(lambda on: setattr(self.feeder, "show_detections", on))
        self.face_btn.toggled.connect(self._toggle_face)
        self.card_btn.clicked.connect(self.open_game_card)
        self._card_dialog = None
        src_btn.clicked.connect(goto_input)
        self._last_ver = -1
        self.timer = QTimer(self.widget)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(1000 / max(1, ctx.settings.get("ui.preview_max_fps"))))

    def _sync_buttons(self):
        p = self.ctx.pipeline
        for b, v in ((self.pause_btn, p.paused), (self.pass_btn, p.passthrough), (self.repl_btn, p.replacement_enabled)):
            if b.isChecked() != v:
                b.blockSignals(True); b.setChecked(v); b.blockSignals(False)
        self.repl_btn.setText(tr("Replacement on") if p.replacement_enabled else tr("Replacement off"))
        on = self._quick_face_on()
        if self.face_btn.isChecked() != on:
            self.face_btn.blockSignals(True); self.face_btn.setChecked(on); self.face_btn.blockSignals(False)

    def _quick_face_on(self) -> bool:
        from vcstudio.roi.model import QUICK_FACE_ID
        return any(r.id == QUICK_FACE_ID and r.enabled for r in self.ctx.rois.rois())

    def _toggle_face(self, on: bool) -> None:
        from vcstudio.roi.model import set_quick_face
        self.ctx.update_active_profile(lambda b: set_quick_face(b, on))
        if on and not self.ctx.pipeline.replacement_enabled:
            self.ctx.pipeline.set_replacement_enabled(True)

    def open_game_card(self) -> None:
        from vcstudio.ui.game_card_dialog import GameCardDialog
        if self._card_dialog is None:
            self._card_dialog = GameCardDialog(self.ctx, self.widget.window())
        self._card_dialog.show(); self._card_dialog.raise_(); self._card_dialog.activateWindow()

    def output_note(self) -> str:
        """Why the camera output looks unchanged, if it does."""
        p = self.ctx.pipeline
        if p.passthrough:
            return tr("Emergency passthrough is on: the camera gets the original video.")
        if not p.replacement_enabled:
            return tr("Replacement is off: the camera gets the original video.")
        if not [r for r in self.ctx.rois.rois() if r.enabled]:
            return tr("No regions yet, so the camera gets the original video. Click “Hide face”, or create a "
                      "region on the Region Editor page.")
        return ""

    def _black_input(self) -> bool:
        """Checked at most twice a second while the input is frozen."""
        import time
        now = time.monotonic()
        if now - getattr(self, "_black_t", 0) > 0.5:
            self._black_t = now
            f, _ = self.ctx.capture.slot.peek()
            self._black = f is not None and frame_is_black(f)
        return self._black

    def tick(self):
        cs = self.ctx.capture.stats()
        live = cs.state not in ("stopped", "error")
        self.start_btn.setEnabled(not live)
        for b in (self.stop_btn, self.pause_btn, self.pass_btn, self.repl_btn):
            b.setEnabled(live)
        self._sync_buttons()
        text, level = STATE_OVERLAY.get(cs.state, (cs.state, "idle"))
        if cs.state == "frozen" and self._black_input():
            text, level = BLACK_INPUT_HINT, "warn"
        self.original.set_overlay(text, level)
        if not live:
            for v in (self.original, self.processed):
                v.set_image(None); v.set_boxes([])
            self.processed.set_overlay("Not running", "idle")
            self.info.setText(tr(cs.state.replace("_", " ")))
            self.note.hide()
            self.stats.setText(tr(""))
            return
        if not self.widget.isVisible():
            return
        pair, ver = self.ctx.pipeline.processed.peek()
        if pair is None or ver == self._last_ver:
            return
        self._last_ver = ver
        src, out = pair
        maxw = max(320, int(max(self.original.width(), self.processed.width()) * self.original.devicePixelRatioF()))
        try:
            self.original.set_image(frame_to_qimage(src, maxw))
            self.processed.set_image(frame_to_qimage(out, maxw))
        except Exception as e:
            self.stats.setText(tr("Preview conversion failed: {e}", e=e))
            return
        self.feeder._update_boxes_for([self.original], src)
        p = self.ctx.pipeline.stats()
        po = {"paused": ("PAUSED: clients see a frozen frame", "warn"),
              "passthrough": (tr("PASSTHROUGH ({reason})", reason=tr(p.passthrough_reason)), "error")}.get(p.state, ("", "ok"))
        self.processed.set_overlay(*po)
        note = self.output_note()
        self.note.setText(note); self.note.setVisible(bool(note))
        self.info.setText(f"{cs.source_name} · {cs.width}×{cs.height} · " +
                          tr("in {a} fps · out {b} fps", a=f"{cs.measured_fps:.0f}", b=f"{p.output_fps:.0f}"))
        cam = {"connected": tr("camera {name} connected (sent {s}, dropped {d})", name=p.camera_device,
                               s=p.camera_sent, d=p.camera_dropped),
               "not_installed": tr("camera {name} not available: output visible here only", name=p.camera_device),
               "disabled": tr("camera output disabled"),
               "error": tr("camera error: {e}", e=p.camera_error)}.get(p.camera_state, p.camera_state)
        self.stats.setText(tr("Pipeline {state} · latency {lat} ms (p95 {p95}) · processing {proc} ms ({backend} render "
                              "{render} ms, GPU {gpu} ms) · {regions} region(s) · skipped inputs {skipped}",
                              state=tr(p.state), lat=f"{p.latency_ms:.1f}", p95=f"{p.latency_ms_p95:.1f}",
                              proc=f"{p.process_ms:.1f}", backend=p.backend.upper() or "—", render=f"{p.render_ms:.1f}",
                              gpu=f"{p.gpu_ms:.2f}", regions=p.regions, skipped=p.inputs_skipped) + f" · {cam}")
