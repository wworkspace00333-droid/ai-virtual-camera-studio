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
        self.boxes = QCheckBox(tr("Show detections")); self.boxes.setChecked(True)
        src_btn = QPushButton(tr("Input settings…"))
        for b in (self.start_btn, self.stop_btn, self.pause_btn, self.pass_btn, self.repl_btn, self.boxes):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(src_btn)
        lay.addLayout(bar)
        # own line: a long source description in the button bar would widen the page past the window
        self.info = QLabel(); self.info.setObjectName("muted"); self.info.setWordWrap(True)
        lay.addWidget(self.info)
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
