from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                               QSlider, QSpinBox, QVBoxLayout, QWidget)

from vcstudio.core.frame import host_time_s
from vcstudio.replacement.assets import Asset, AssetError, IMAGE_EXTS, VIDEO_EXTS, assets_from_profile
from vcstudio.replacement.spec import EFFECTS, FITS, MODES, VIDEO_SYNC, ReplacementSpec
from vcstudio.roi.model import rois_from_profile
from vcstudio.ui.video_view import VideoView
from vcstudio.ui.widgets import Card, page_scaffold

log = logging.getLogger(__name__)
PREVIEW_FPS = 15


def _qimage_bgra(arr) -> QImage:
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, arr.strides[0], QImage.Format_ARGB32_Premultiplied).copy()


class ReplacementPage:
    key, title = "replacement", "Replacement"

    def __init__(self, ctx):
        self.ctx = ctx
        self.widget, lay = page_scaffold("Replacement", "Each ROI can replace its region, replace a tracked object "
                                         "through its segmentation mask, or overlay an image or a video.")
        top = QHBoxLayout()

        # ---------------- assets
        lib = Card("Asset library (this profile)")
        self.assets = QListWidget()
        self.assets.setViewMode(QListWidget.IconMode); self.assets.setIconSize(QSize(88, 88))
        self.assets.setResizeMode(QListWidget.Adjust); self.assets.setMinimumHeight(220); self.assets.setSpacing(6)
        lib.layout_.addWidget(self.assets)
        ar = QHBoxLayout()
        self.add_btn = QPushButton("Add images / videos…"); self.rm_btn = QPushButton("Remove")
        ar.addWidget(self.add_btn); ar.addWidget(self.rm_btn); ar.addStretch()
        lib.layout_.addLayout(ar)
        self.asset_info = QLabel(); self.asset_info.setObjectName("muted"); self.asset_info.setWordWrap(True)
        lib.layout_.addWidget(self.asset_info)
        top.addWidget(lib, 2)

        # ---------------- settings
        st = Card("Replacement for ROI")
        f = QFormLayout()
        self.roi = QComboBox()
        self.enabled = QCheckBox("Enabled")
        self.mode = QComboBox()
        for k, v in MODES.items():
            self.mode.addItem(v, k)
        self.asset = QComboBox()
        self.fit = QComboBox()
        for k, v in FITS.items():
            self.fit.addItem(v, k)
        self.scale = QDoubleSpinBox(); self.scale.setRange(0.05, 10); self.scale.setSingleStep(0.05)
        self.offx = QDoubleSpinBox(); self.offy = QDoubleSpinBox()
        for sp in (self.offx, self.offy):
            sp.setRange(-2, 2); sp.setSingleStep(0.02); sp.setDecimals(3)
        self.rot = QDoubleSpinBox(); self.rot.setRange(-180, 180); self.rot.setSuffix("°")
        self.opacity = QSlider(Qt.Horizontal); self.opacity.setRange(0, 100)
        self.crop = [QDoubleSpinBox() for _ in range(4)]
        for sp in self.crop:
            sp.setRange(0, 1); sp.setSingleStep(0.01); sp.setDecimals(3)
        self.feather = QDoubleSpinBox(); self.feather.setRange(0, 100); self.feather.setSuffix(" px")
        self.bg_btn = QPushButton(); self.bg_btn.setMinimumWidth(120)
        self.thresh = QDoubleSpinBox(); self.thresh.setRange(0.1, 0.9); self.thresh.setSingleStep(0.05)
        self.refine = QCheckBox("Refine edges (remove specks, close holes)")
        self.fallback = QComboBox()
        self.fallback.addItem("Skip the object (no fake silhouette)", "skip")
        self.fallback.addItem("Use its box (explicitly rectangular)", "box")
        r_off = QHBoxLayout(); r_off.addWidget(self.offx); r_off.addWidget(self.offy)
        r_crop = QHBoxLayout()
        for sp in self.crop:
            r_crop.addWidget(sp)
        f.addRow("ROI", self.roi); f.addRow("", self.enabled); f.addRow("Mode", self.mode); f.addRow("Asset", self.asset)
        f.addRow("Fit", self.fit); f.addRow("Scale", self.scale); f.addRow("Offset x, y (× region)", r_off)
        f.addRow("Rotation", self.rot); f.addRow("Opacity", self.opacity)
        f.addRow("Crop x0, y0, x1, y1", r_crop); f.addRow("Edge feather", self.feather)
        f.addRow("Background fill", self.bg_btn)
        f.addRow("Mask threshold", self.thresh); f.addRow("", self.refine); f.addRow("Without mask", self.fallback)
        st.layout_.addLayout(f)
        # ---- effects
        self.effect_box = QWidget(); ef = QFormLayout(self.effect_box); ef.setContentsMargins(0, 6, 0, 0)
        self.effect = QComboBox()
        for k, v in EFFECTS.items():
            self.effect.addItem(v, k)
        self.sigma = QDoubleSpinBox(); self.sigma.setRange(0.5, 100); self.sigma.setSuffix(" px σ")
        self.pix = QSpinBox(); self.pix.setRange(2, 256); self.pix.setSuffix(" px")
        self.bright = QDoubleSpinBox(); self.bright.setRange(-1, 1); self.bright.setSingleStep(0.05)
        self.contrast = QDoubleSpinBox(); self.contrast.setRange(0, 3); self.contrast.setSingleStep(0.05)
        self.sat = QDoubleSpinBox(); self.sat.setRange(0, 3); self.sat.setSingleStep(0.05)
        ef.addRow("Effect", self.effect); ef.addRow("Blur strength", self.sigma); ef.addRow("Pixel size", self.pix)
        ef.addRow("Brightness", self.bright); ef.addRow("Contrast", self.contrast); ef.addRow("Saturation", self.sat)
        st.layout_.addWidget(self.effect_box)
        # ---- video playback
        self.video_box = QWidget(); vf = QFormLayout(self.video_box); vf.setContentsMargins(0, 6, 0, 0)
        self.v_loop = QCheckBox("Loop")
        self.v_end = QComboBox(); self.v_end.addItem("Hold last frame", "hold"); self.v_end.addItem("Hide", "hide")
        self.v_off = QDoubleSpinBox(); self.v_off.setRange(0, 36000); self.v_off.setSuffix(" s"); self.v_off.setDecimals(2)
        self.v_rate = QDoubleSpinBox(); self.v_rate.setRange(0.1, 8); self.v_rate.setSingleStep(0.25); self.v_rate.setSuffix("×")
        self.v_interp = QComboBox(); self.v_interp.addItem("Nearest frame (drop/repeat)", "nearest")
        self.v_interp.addItem("Blend neighbouring frames", "blend")
        self.v_sync = QComboBox()
        for k, v in VIDEO_SYNC.items():
            self.v_sync.addItem(v, k)
        self.v_max = QComboBox()
        for v in (640, 960, 1280, 1920, 3840):
            self.v_max.addItem(f"{v} px", v)
        r_end = QHBoxLayout(); r_end.addWidget(self.v_loop); r_end.addWidget(QLabel("At end:")); r_end.addWidget(self.v_end)
        vf.addRow("Playback", r_end); vf.addRow("Start offset", self.v_off); vf.addRow("Speed", self.v_rate)
        vf.addRow("Frame-rate conversion", self.v_interp); vf.addRow("Synchronisation", self.v_sync)
        vf.addRow("Decode size (long side)", self.v_max)
        tr = QHBoxLayout()
        self.v_play = QPushButton("Pause / Play"); self.v_restart = QPushButton("Restart")
        tr.addWidget(self.v_play); tr.addWidget(self.v_restart); tr.addStretch()
        vf.addRow("Transport", tr)
        self.v_status = QLabel(); self.v_status.setObjectName("muted"); self.v_status.setWordWrap(True)
        vf.addRow("", self.v_status)
        st.layout_.addWidget(self.video_box)
        self.warn = QLabel(); self.warn.setWordWrap(True); self.warn.setStyleSheet("color: #d29922;")
        st.layout_.addWidget(self.warn)
        top.addWidget(st, 3)
        lay.addLayout(top)

        pv = Card("Live replacement preview")
        note = QLabel(f"Rendered here at ≤{PREVIEW_FPS} fps with the active compositor (Metal GPU, or CPU fallback; "
                      "see Settings). The 60 fps output pipeline and virtual camera come in Stages 9–10.")
        note.setObjectName("muted"); note.setWordWrap(True)
        pv.layout_.addWidget(note)
        self.view = VideoView("REPLACEMENT PREVIEW"); self.view.setMinimumHeight(420)
        pv.layout_.addWidget(self.view)
        sr = QHBoxLayout()
        self.stats = QLabel(); self.stats.setObjectName("muted")
        self.reset_btn = QPushButton("Re-enable failed ROIs")
        sr.addWidget(self.stats, 1); sr.addWidget(self.reset_btn)
        pv.layout_.addLayout(sr)
        lay.addWidget(pv)
        lay.addStretch()

        self._bg = [0, 0, 0, 255]
        self._loading = False
        self._controls = [self.enabled, self.mode, self.asset, self.fit, self.scale, self.offx, self.offy, self.rot,
                          self.opacity, *self.crop, self.feather, self.thresh, self.refine, self.fallback,
                          self.v_loop, self.v_end, self.v_off, self.v_rate, self.v_interp, self.v_sync, self.v_max,
                          self.effect, self.sigma, self.pix, self.bright, self.contrast, self.sat]
        self.add_btn.clicked.connect(lambda: self.add_assets())   # clicked(bool) must not reach `paths`
        self.rm_btn.clicked.connect(self.remove_asset)
        self.assets.currentItemChanged.connect(self._asset_selected)
        self.roi.currentIndexChanged.connect(self._load_spec)
        for w in (self.enabled, self.refine, self.v_loop):
            w.toggled.connect(self._save)
        for w in (self.mode, self.asset, self.fit, self.fallback, self.v_end, self.v_interp, self.v_sync, self.v_max,
                  self.effect):
            w.currentIndexChanged.connect(self._save)
        for w in (self.scale, self.offx, self.offy, self.rot, self.feather, self.thresh, *self.crop, self.v_off, self.v_rate,
                  self.sigma, self.pix, self.bright, self.contrast, self.sat):
            w.valueChanged.connect(self._save)
        self.v_play.clicked.connect(lambda: self._transport("toggle"))
        self.v_restart.clicked.connect(lambda: self._transport("restart"))
        self.opacity.valueChanged.connect(self._save)
        self.bg_btn.clicked.connect(self._pick_bg)
        self.reset_btn.clicked.connect(self.ctx.replacement.compositor.reset_failures)
        ctx.profiles.add_listener(self._on_profile)
        self.timer = QTimer(self.widget); self.timer.timeout.connect(self._render); self.timer.start(int(1000 / PREVIEW_FPS))
        self.reload()

    # ---------------------------------------------------------------- profile data
    def _body(self) -> dict:
        return self.ctx.profiles.active()["body"]

    def _on_profile(self, event, name):
        if event == "switched":
            self.reload()
        elif event == "updated" and not self._loading:
            self._refresh_roi_list(keep=True)

    def reload(self):
        self._refresh_assets()
        self._refresh_roi_list(keep=False)

    def _refresh_assets(self):
        assets = assets_from_profile(self._body())
        cur = self.assets.currentItem().data(Qt.UserRole) if self.assets.currentItem() else None
        self.assets.blockSignals(True)
        self.assets.clear()
        for a in assets.values():
            it = QListWidgetItem(a.name)
            it.setData(Qt.UserRole, a.id)
            try:
                th = self.ctx.assets.thumbnail(a, 88)
                it.setIcon(QIcon(QPixmap.fromImage(_qimage_bgra(th))))
            except AssetError as e:
                it.setText(f"{a.name} (missing)"); it.setToolTip(str(e))
            self.assets.addItem(it)
            if a.id == cur:
                self.assets.setCurrentItem(it)
        self.assets.blockSignals(False)
        self.asset.blockSignals(True)
        sel = self.asset.currentData()
        self.asset.clear()
        self.asset.addItem("— none —", "")
        for a in assets.values():
            label = (f"{a.name} (image, {a.width}×{a.height})" if a.kind == "image" else
                     f"{a.name} (video, {a.width}×{a.height}, {a.duration:.1f} s, {a.fps:g} fps)")
            self.asset.addItem(label, a.id)
        self.asset.setCurrentIndex(max(0, self.asset.findData(sel)))
        self.asset.blockSignals(False)
        self._asset_selected()

    def _refresh_roi_list(self, keep: bool):
        rois = rois_from_profile(self._body())
        cur = self.roi.currentData() if keep else None
        self.roi.blockSignals(True)
        self.roi.clear()
        for r in sorted(rois, key=lambda r: (-r.priority, r.name)):
            self.roi.addItem(f"{r.name}  [{r.mode}]", r.id)
        idx = self.roi.findData(cur) if cur else 0
        self.roi.setCurrentIndex(max(0, idx))
        self.roi.blockSignals(False)
        if not keep:
            self._load_spec()
        for w in self._controls + [self.bg_btn]:
            w.setEnabled(bool(rois))
        if not rois:
            self.warn.setText("No ROIs in this profile yet. Draw one on the ROI Editor page.")

    def _roi_dict(self) -> dict | None:
        rid = self.roi.currentData()
        return next((r for r in self._body().get("rois", []) if r.get("id") == rid), None)

    def _load_spec(self, *_):
        d = self._roi_dict()
        if d is None:
            return
        s = ReplacementSpec.from_dict(d.get("replacement"))
        self._loading = True
        for w in self._controls:
            w.blockSignals(True)
        self.enabled.setChecked(s.enabled)
        self.mode.setCurrentIndex(self.mode.findData(s.mode))
        self.asset.setCurrentIndex(max(0, self.asset.findData(s.asset_id)))
        self.fit.setCurrentIndex(self.fit.findData(s.fit))
        self.scale.setValue(s.scale); self.offx.setValue(s.offset_x); self.offy.setValue(s.offset_y)
        self.rot.setValue(s.rotation); self.opacity.setValue(int(round(s.opacity * 100)))
        for sp, v in zip(self.crop, s.crop):
            sp.setValue(v)
        self.feather.setValue(s.feather_px); self.thresh.setValue(s.mask_threshold)
        self.refine.setChecked(s.refine_edges); self.fallback.setCurrentIndex(self.fallback.findData(s.fallback_no_mask))
        self._bg = list(s.background)
        self.v_loop.setChecked(s.video_loop); self.v_end.setCurrentIndex(self.v_end.findData(s.video_end_action))
        self.v_off.setValue(s.video_start_offset_s); self.v_rate.setValue(s.video_rate)
        self.v_interp.setCurrentIndex(self.v_interp.findData(s.video_interpolation))
        self.v_sync.setCurrentIndex(self.v_sync.findData(s.video_sync))
        self.v_max.setCurrentIndex(max(0, self.v_max.findData(s.video_max_side)))
        self.effect.setCurrentIndex(self.effect.findData(s.effect)); self.sigma.setValue(s.blur_sigma)
        self.pix.setValue(s.pixel_size); self.bright.setValue(s.brightness); self.contrast.setValue(s.contrast)
        self.sat.setValue(s.saturation)
        for w in self._controls:
            w.blockSignals(False)
        self._loading = False
        self._update_bg_button()
        self._update_enabled_state(d)

    def _update_bg_button(self):
        b, g, r, a = self._bg
        self.bg_btn.setText(f"#{r:02x}{g:02x}{b:02x}  α {a}")
        txt = "white" if (r + g + b) < 380 else "black"
        self.bg_btn.setStyleSheet(f"background: rgba({r},{g},{b},{a}); color: {txt};")

    def _pick_bg(self):
        b, g, r, a = self._bg
        c = QColorDialog.getColor(QColor(r, g, b, a), self.widget, "Background fill",
                                  QColorDialog.ShowAlphaChannel)
        if c.isValid():
            self._bg = [c.blue(), c.green(), c.red(), c.alpha()]
            self._update_bg_button()
            self._save()

    def _is_video(self) -> bool:
        a = assets_from_profile(self._body()).get(self.asset.currentData() or "")
        return a is not None and a.kind == "video"

    def _transport(self, action: str):
        t = host_time_s()
        f, _ = self.ctx.capture.latest()
        if f is not None:
            t = f.pts
        for p in self.ctx.replacement.players_for_roi(self.roi.currentData()):
            p.toggle(t) if action == "toggle" else p.restart(t)

    def _update_enabled_state(self, d: dict):
        mode = self.mode.currentData()
        is_effect = mode == "effect"
        self.effect_box.setVisible(is_effect)
        self.video_box.setVisible(self._is_video() and not is_effect)
        self.v_end.setEnabled(not self.v_loop.isChecked())
        for w in (self.asset, self.fit, self.scale, self.offx, self.offy, self.rot, *self.crop):
            w.setEnabled(not is_effect)
        eff = self.effect.currentData()
        self.sigma.setEnabled(eff == "blur"); self.pix.setEnabled(eff == "pixelate")
        for w in (self.bright, self.contrast, self.sat):
            w.setEnabled(eff == "color")
        is_obj = mode == "object"
        for w in (self.thresh, self.refine, self.fallback):
            w.setEnabled(is_obj)
        self.bg_btn.setEnabled(mode in ("rect", "object"))
        msgs = []
        if is_obj and d.get("mode") == "manual":
            msgs.append("Object replacement needs an Automatic or Hybrid ROI (it follows a tracked object). "
                        "Change the ROI mode on the ROI Editor page.")
        if is_obj and not any(m.task == "segment" for m in self.ctx.models.available().values()):
            msgs.append("No segmentation model installed: run scripts/convert_models.py yolo11n-seg.")
        if is_obj and self.enabled.isChecked():
            msgs.append("While enabled, the AI engine runs the segmentation model (yolo11n-seg) instead of the "
                        "plain detector.")
        if self.enabled.isChecked() and not self.asset.currentData() and not is_effect:
            msgs.append("Choose an asset.")
        if is_effect and d.get("mode") != "manual":
            msgs.append("On object regions the effect follows the segmentation silhouette when masks are "
                        "available (object replacement elsewhere enables segmentation); otherwise it covers the box.")
        self.warn.setText(" ".join(msgs))

    def _save(self, *_):
        if self._loading:
            return
        rid = self.roi.currentData()
        if not rid:
            return
        spec = ReplacementSpec.from_dict({
            "enabled": self.enabled.isChecked(), "mode": self.mode.currentData(), "asset_id": self.asset.currentData() or "",
            "fit": self.fit.currentData(), "scale": self.scale.value(), "offset_x": self.offx.value(),
            "offset_y": self.offy.value(), "rotation": self.rot.value(), "opacity": self.opacity.value() / 100,
            "crop": [sp.value() for sp in self.crop], "feather_px": self.feather.value(), "background": self._bg,
            "mask_threshold": self.thresh.value(), "refine_edges": self.refine.isChecked(),
            "fallback_no_mask": self.fallback.currentData(),
            "video_loop": self.v_loop.isChecked(), "video_end_action": self.v_end.currentData(),
            "video_start_offset_s": self.v_off.value(), "video_rate": self.v_rate.value(),
            "video_interpolation": self.v_interp.currentData(), "video_sync": self.v_sync.currentData(),
            "video_max_side": self.v_max.currentData(),
            "effect": self.effect.currentData(), "blur_sigma": self.sigma.value(), "pixel_size": self.pix.value(),
            "brightness": self.bright.value(), "contrast": self.contrast.value(), "saturation": self.sat.value()}).to_dict()

        def mutate(b):
            for r in b["rois"]:
                if r.get("id") == rid:
                    r["replacement"] = spec
        self._loading = True
        try:
            self.ctx.update_active_profile(mutate)
        finally:
            self._loading = False
        d = self._roi_dict()
        if d:
            self._update_enabled_state(d)

    # ---------------------------------------------------------------- assets
    def add_assets(self, paths: list[str] | None = None):
        if paths is None:
            exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS | VIDEO_EXTS))
            paths, _ = QFileDialog.getOpenFileNames(self.widget, "Add assets", str(Path.home()), f"Media ({exts})")
        added, errors = [], []
        for p in paths:
            try:
                added.append(self.ctx.assets.import_file(Path(p)).to_dict())
            except (AssetError, OSError) as e:
                errors.append(f"{Path(p).name}: {e}")
        if added:
            self.ctx.update_active_profile(lambda b: b.setdefault("assets", []).extend(added))
            self._refresh_assets()
            if self.asset.currentData() == "" and len(added) == 1:
                self.asset.setCurrentIndex(self.asset.findData(added[0]["id"]))
        if errors:
            QMessageBox.warning(self.widget, "Assets", "\n".join(errors))
        return added

    def remove_asset(self):
        it = self.assets.currentItem()
        if it is None:
            return
        aid = it.data(Qt.UserRole)
        used = [r["name"] for r in self._body().get("rois", []) if (r.get("replacement") or {}).get("asset_id") == aid]
        if used and QMessageBox.question(self.widget, "Remove asset",
                                         f"Used by: {', '.join(used)}. Remove anyway?") != QMessageBox.Yes:
            return
        asset = assets_from_profile(self._body()).get(aid)

        def mutate(b):
            b["assets"] = [a for a in b.get("assets", []) if a.get("id") != aid]
            for r in b["rois"]:
                if (r.get("replacement") or {}).get("asset_id") == aid:
                    r["replacement"]["asset_id"] = ""
                    r["replacement"]["enabled"] = False
        self.ctx.update_active_profile(mutate)
        if asset and not self.ctx.asset_in_use_elsewhere(aid, self.ctx.profiles.active_name):
            self.ctx.assets.remove_file(asset)
        self._refresh_assets()
        self._load_spec()

    def _asset_selected(self, *_):
        it = self.assets.currentItem()
        a = assets_from_profile(self._body()).get(it.data(Qt.UserRole)) if it else None
        if a is not None and a.kind == "video":
            dec = "FFmpeg (software)" if a.decoder == "ffmpeg" else ("VideoToolbox hardware" if a.hardware else "AVFoundation")
            self.asset_info.setText(f"{a.name}: video {a.width}×{a.height}, {a.fps:g} fps, {a.duration:.2f} s, "
                                    f"codec {a.codec}, decoder {dec}")
        else:
            self.asset_info.setText(f"{a.name}: image {a.width}×{a.height}, stored as {a.file}" if a else
                                    "Images: PNG / JPG / WEBP (alpha). Videos: MP4 / MOV (hardware decode); other "
                                    "containers via FFmpeg if installed. Files are copied into the app's assets folder.")

    # ---------------------------------------------------------------- preview
    def _render(self):
        if not self.widget.isVisible():
            return
        f, _ = self.ctx.capture.latest()
        if f is None:
            self.view.set_image(None)
            self.view.set_overlay("Start capture to preview replacements", "idle")
            self.stats.setText("")
            return
        tracked = self.ctx.ai.tracked_at(f.pts)
        regions = self.ctx.rois.resolve(tracked, f.width, f.height)
        try:
            with f.bgra() as a:
                out = self.ctx.replacement.render(a, regions, f.pts)
        except Exception as e:
            log.exception("preview render failed")
            self.stats.setText(f"Render failed: {e}")
            return
        step = max(1, -(-out.shape[1] // max(640, self.view.width())))
        small = out[::step, ::step].copy()
        self.view.set_image(_qimage_bgra(small))
        self.view.set_overlay("", "ok")
        cs = self.ctx.replacement.compositor.stats
        fail = f" · failed: {', '.join(cs.failed_rois)}" if cs.failed_rois else ""
        gpu = f" (GPU {cs.gpu_ms:.2f} ms)" if cs.backend == "metal" else ""
        self.stats.setText(f"{cs.backend.upper()} compositor{gpu} · render {cs.render_ms:.1f} ms · {cs.regions_drawn} region(s) drawn · "
                           f"{cs.skipped_no_mask} skipped (no mask) · {len(regions)} active region(s) · "
                           f"{cs.video_players} video player(s){fail}")
        players = self.ctx.replacement.players_for_roi(self.roi.currentData())
        if players and self.video_box.isVisible():
            p = players[0].stats
            more = f" (+{len(players) - 1} more)" if len(players) > 1 else ""
            self.v_status.setText(f"{'Paused' if p.paused else ('Ended' if p.ended else 'Playing')}{more} · "
                                  f"{p.position_s:.2f} / {p.duration_s:.2f} s · decoder {p.decoder} · buffer {p.buffered} · "
                                  f"decoded {p.decoded} · late {p.late_frames} · loops {p.loops}" +
                                  (f" · error: {p.error}" if p.error else ""))
        elif self.video_box.isVisible():
            self.v_status.setText("Not playing (the ROI has no active region right now)")
