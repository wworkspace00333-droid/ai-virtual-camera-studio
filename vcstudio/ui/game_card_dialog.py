"""“Card for a game” window: pick card photos and place them into the frame a game draws over the camera.

Automatic: hides the app, shows calibration markers and then a flat grey picture in the camera, takes
two screenshots and finds where the game shows the camera and where its frame is
(vcstudio/game/cardframe.py). Manual: the user draws the frame on the screenshot.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from vcstudio.game import cardframe as cf
from vcstudio.i18n import tr

log = logging.getLogger(__name__)

SETTLE_MS = 1500     # time for the game to show the new camera picture (browser/emulator latency)


def _qimage(bgr: np.ndarray) -> QImage:
    rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    return QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()


class ScreenPicker(QWidget):
    """Shows a screenshot (or a crop of it); the user drags a rectangle. Emits it in screenshot pixels."""
    picked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(560, 320)
        self.img: QImage | None = None
        self.origin = (0, 0)          # screenshot pixel of the image's top-left (crop offset)
        self.rect_img: QRectF | None = None   # in image pixels
        self.area: np.ndarray | None = None   # camera area polygon, screenshot pixels
        self._drag: QPointF | None = None
        self.editable = False
        self.setCursor(Qt.CrossCursor)

    def set_image(self, bgr: np.ndarray, origin=(0, 0)) -> None:
        self.img, self.origin = _qimage(bgr), origin
        self.update()

    def set_rect_screen(self, quad) -> None:
        if quad is None:
            self.rect_img = None
        else:
            q = np.asarray(quad, float) - self.origin
            self.rect_img = QRectF(QPointF(q[:, 0].min(), q[:, 1].min()), QPointF(q[:, 0].max(), q[:, 1].max()))
        self.update()

    def _scale(self):
        iw, ih = self.img.width(), self.img.height()
        s = min(self.width() / iw, self.height() / ih)
        return s, (self.width() - iw * s) / 2, (self.height() - ih * s) / 2

    def _to_img(self, p) -> QPointF:
        s, ox, oy = self._scale()
        return QPointF((p.x() - ox) / s, (p.y() - oy) / s)

    def mousePressEvent(self, e):  # noqa: N802
        if self.img is not None and self.editable:
            self._drag = self._to_img(e.position())
            self.rect_img = QRectF(self._drag, self._drag)
            self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag is not None:
            self.rect_img = QRectF(self._drag, self._to_img(e.position())).normalized()
            self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._drag is None:
            return
        self._drag = None
        r = self.rect_img
        if r is not None and r.width() > 8 and r.height() > 8:
            ox, oy = self.origin
            self.picked.emit(np.array([[r.left() + ox, r.top() + oy], [r.right() + ox, r.top() + oy],
                                       [r.right() + ox, r.bottom() + oy], [r.left() + ox, r.bottom() + oy]]))

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        if self.img is None:
            p.setPen(QColor("#8b949e"))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap,
                       tr("Open the game with the camera on, then click “Find the frame automatically”."))
            return
        s, ox, oy = self._scale()
        p.drawImage(QRectF(ox, oy, self.img.width() * s, self.img.height() * s), self.img)
        if self.rect_img is not None:
            r = self.rect_img
            p.setPen(QPen(QColor("#3fb950"), 3))
            p.drawRect(QRectF(ox + r.x() * s, oy + r.y() * s, r.width() * s, r.height() * s))


class GameCardDialog(QDialog):
    def __init__(self, ctx, main_window=None):
        super().__init__(main_window)
        self.ctx, self.main_window = ctx, main_window
        self.setWindowTitle(tr("Card for a game"))
        self.resize(780, 720)
        lay = QVBoxLayout(self)
        intro = QLabel(tr("A photo goes into the camera picture exactly where the game draws its frame. Keep the game "
                          "open on screen with this camera selected (OBS Virtual Camera), then find the frame "
                          "automatically or draw it yourself."))
        intro.setWordWrap(True); intro.setObjectName("muted")
        lay.addWidget(intro)

        # cards
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Cards")))
        add = QPushButton(tr("Add photos…")); add.clicked.connect(self.add_cards)
        row.addStretch(); row.addWidget(add)
        lay.addLayout(row)
        self.cards = QListWidget(); self.cards.setViewMode(QListWidget.IconMode); self.cards.setIconSize(QSize(96, 96))
        self.cards.setMaximumHeight(150); self.cards.setMovement(QListWidget.Static)
        self.cards.currentItemChanged.connect(lambda cur, _p: self._choose(cur))
        lay.addWidget(self.cards)

        # frame
        row = QHBoxLayout()
        self.auto_btn = QPushButton(tr("Find the frame automatically")); self.auto_btn.setObjectName("primary")
        self.manual_btn = QPushButton(tr("Draw the frame myself"))
        self.auto_btn.clicked.connect(self.find_automatically)
        self.manual_btn.clicked.connect(self.draw_manually)
        row.addWidget(self.auto_btn); row.addWidget(self.manual_btn); row.addStretch()
        lay.addLayout(row)
        self.status = QLabel(); self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.picker = ScreenPicker(); self.picker.picked.connect(self._picked)
        lay.addWidget(self.picker, 1)

        # options
        row = QHBoxLayout()
        self.show_card = QCheckBox(tr("Show the card")); self.show_card.setChecked(True)
        self.fit = QComboBox()
        for k, label in (("fit", "Fit (contain)"), ("fill", "Fill (cover)"), ("stretch", "Stretch")):
            self.fit.addItem(tr(label), k)
        self.flip = QCheckBox(tr("Mirror the card"))
        self.flip.setToolTip(tr("Turn on if the card looks mirrored in the game."))
        for w in (self.show_card, QLabel(tr("Fit")), self.fit, self.flip):
            row.addWidget(w)
        row.addStretch()
        lay.addLayout(row)
        self.show_card.toggled.connect(lambda on: self._update(enabled=on))
        self.fit.currentIndexChanged.connect(lambda _i: self._update(fit=self.fit.currentData()))
        self.flip.toggled.connect(lambda on: self._update(flip=on))

        self.cal: cf.Calibration | None = None
        self.shot: np.ndarray | None = None
        self._shots: dict[str, np.ndarray] = {}
        self._load_state()

    # ------------------------------------------------------------------ cards
    def _body(self) -> dict:
        return self.ctx.profiles.active()["body"]

    def _load_state(self) -> None:
        from vcstudio.replacement.assets import Asset
        body = self._body()
        roi = cf.game_card_roi(body)
        rep = (roi or {}).get("replacement", {})
        self.cards.blockSignals(True)
        self.cards.clear()
        for d in body.get("assets", []):
            if d.get("kind") != "image":
                continue
            a = Asset.from_dict(d)
            it = QListWidgetItem(a.name)
            it.setData(Qt.UserRole, a.id)
            try:
                th = self.ctx.assets.thumbnail(a, 96)
                rgb = cv2.cvtColor(th, cv2.COLOR_BGRA2RGBA if th.shape[2] == 4 else cv2.COLOR_BGR2RGB)
                qi = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                            QImage.Format_RGBA8888 if rgb.shape[2] == 4 else QImage.Format_RGB888).copy()
                it.setIcon(QIcon(QPixmap.fromImage(qi)))
            except Exception:  # noqa: BLE001 - a broken file still shows by name
                pass
            self.cards.addItem(it)
            if a.id == rep.get("asset_id"):
                self.cards.setCurrentItem(it)
        self.cards.blockSignals(False)
        for w, v in ((self.show_card, (roi or {}).get("enabled", True)), (self.flip, rep.get("flip_h", False))):
            w.blockSignals(True); w.setChecked(bool(v)); w.blockSignals(False)
        self.fit.blockSignals(True)
        self.fit.setCurrentIndex(max(0, self.fit.findData(rep.get("fit", "fit"))))
        self.fit.blockSignals(False)
        if roi is None:
            self.status.setText(tr("No frame yet."))
        elif not rep.get("asset_id"):
            self.status.setText(tr("Frame set. Add or choose a card photo."))
        else:
            self.status.setText(tr("The card is in the frame."))

    def add_cards(self, paths=None) -> None:
        from vcstudio.replacement.assets import IMAGE_EXTS, AssetError
        if not paths:
            exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
            paths, _ = QFileDialog.getOpenFileNames(self, tr("Add photos…"), str(Path.home()), f"Images ({exts})")
        added = []
        for p in paths or []:
            try:
                added.append(self.ctx.assets.import_file(Path(p)).to_dict())
            except (AssetError, OSError) as e:
                self.status.setText(f"{Path(p).name}: {e}")
        if not added:
            return
        first = added[0]["id"]

        def mut(b):
            b.setdefault("assets", []).extend(added)
            if cf.game_card_roi(b) is not None and not cf.game_card_roi(b)["replacement"].get("asset_id"):
                cf.set_game_card(b, asset_id=first)
        self.ctx.update_active_profile(mut)
        self._load_state()
        for i in range(self.cards.count()):
            if self.cards.item(i).data(Qt.UserRole) == first and self.cards.currentItem() is None:
                self.cards.setCurrentRow(i)

    def _choose(self, item) -> None:
        if item is not None:
            self._update(asset_id=item.data(Qt.UserRole))

    def _update(self, **kw) -> None:
        if cf.game_card_roi(self._body()) is None and "rect" not in kw:
            return                                   # no frame yet: nothing to update
        self.ctx.update_active_profile(lambda b: cf.set_game_card(b, **kw))
        if not self.ctx.pipeline.replacement_enabled:
            self.ctx.pipeline.set_replacement_enabled(True)
        if "asset_id" in kw or "rect" in kw:
            self._load_state()

    # ------------------------------------------------------------------ frame
    def _ensure_running(self) -> bool:
        if self.ctx.capture.stats().state in ("stopped", "error"):
            try:
                self.ctx.start_capture()
            except Exception as e:  # noqa: BLE001
                self.status.setText(tr("Cannot start: {e}", e=e))
                return False
        return True

    def find_automatically(self) -> None:
        if not self._ensure_running():
            return
        self.auto_btn.setEnabled(False); self.manual_btn.setEnabled(False)
        self.status.setText(tr("Looking at the screen… (the app hides for about 4 seconds)"))
        self._hidden = [w for w in (self.main_window, self) if w is not None and w.isVisible()]
        for w in self._hidden:
            w.hide()
        self.ctx.pipeline.override = cf.calibration_frame
        self._grid = "dense"
        QTimer.singleShot(SETTLE_MS, lambda: self._shoot("markers", self._after_markers))

    def _shoot(self, key: str, then) -> None:
        try:
            self._shots[key] = cf.screenshot()
        except cf.CalibrationError as e:
            self._finish(str(e))
            return
        then()

    def _after_markers(self) -> None:
        """Dense grid first; if the game shows the camera too small for it, try the coarse grid."""
        try:
            self.cal = cf.calibrate(self._shots["markers"], self._camera_size(), grid="dense")
            self._step_grey()
        except cf.CalibrationError:
            self.cal = None
            self._grid = "coarse"
            self.ctx.pipeline.override = cf.coarse_calibration_frame
            QTimer.singleShot(SETTLE_MS, lambda: self._shoot("markers", self._step_grey))

    def _step_grey(self) -> None:
        self.ctx.pipeline.override = cf.grey_frame
        QTimer.singleShot(SETTLE_MS, lambda: self._shoot("grey", self._analyse))

    def _finish(self, error: str | None) -> None:
        self.ctx.pipeline.override = None
        for w in getattr(self, "_hidden", []):
            w.show()
        self.raise_(); self.activateWindow()
        self.auto_btn.setEnabled(True); self.manual_btn.setEnabled(True)
        if error:
            self.status.setText(error)

    def _analyse(self) -> None:
        self._finish(None)
        size = self._camera_size()
        try:
            if self.cal is None:
                self.cal = cf.calibrate(self._shots["markers"], size, grid=self._grid)
        except cf.CalibrationError as e:
            self.cal = None
            self.status.setText(tr("The game's camera picture was not found on screen ({e}). Make sure the game is "
                                   "visible on the main display, shows this camera, and that Screen Recording is "
                                   "allowed for this app (System Settings > Privacy & Security).", e=e))
            self._show_shot(self._shots.get("grey"))
            return
        self.shot = self._shots["grey"]
        quad = cf.find_frame(self.shot, self.cal)
        self._show_shot(self.shot)
        note = tr(" The game shows the camera mirrored; if the card looks mirrored there, tick “Mirror the card”.") \
            if self.cal.mirrored else ""
        if quad is None:
            self.picker.editable = True
            self.status.setText(tr("Found the camera picture in the game, but not a frame in it. Draw the frame on the "
                                   "picture below.") + note)
            return
        self._picked(quad)
        self.status.setText(tr("Frame found and the card placed. If it is not the right frame, draw it below.") + note)

    def draw_manually(self) -> None:
        if self.cal is None:
            self.status.setText(tr("First the app has to see how the game shows the camera: click “Find the frame "
                                   "automatically”, then draw the frame on the picture if it is wrong."))
            return
        self.picker.editable = True
        self.status.setText(tr("Drag a rectangle over the game's frame in the picture below."))

    def _camera_size(self) -> tuple[int, int]:
        f, _ = self.ctx.capture.slot.peek()
        if f is not None:
            return f.width, f.height
        return self.ctx.settings.get("output.width"), self.ctx.settings.get("output.height")

    def _show_shot(self, shot) -> None:
        if shot is None:
            return
        x0, y0 = 0, 0
        if self.cal is not None:   # crop to the camera picture (plus a margin) so the frame is easy to see
            a = self.cal.camera_area()
            h, w = shot.shape[:2]
            x0 = int(max(0, a[:, 0].min() - 40)); y0 = int(max(0, a[:, 1].min() - 40))
            x1 = int(min(w, a[:, 0].max() + 40)); y1 = int(min(h, a[:, 1].max() + 40))
            if x1 - x0 > 50 and y1 - y0 > 50:
                shot = shot[y0:y1, x0:x1]
            else:
                x0 = y0 = 0
        self.picker.set_image(shot, (x0, y0))
        self.picker.editable = True

    def _picked(self, quad) -> None:
        if self.cal is None:
            return
        self.picker.set_rect_screen(quad)
        rect = cf.frame_to_rect(self.cal, quad)
        item = self.cards.currentItem()
        kw = {"rect": rect, "enabled": True}
        if item is not None:
            kw["asset_id"] = item.data(Qt.UserRole)
        self._update(**kw)
        if item is None:
            self.status.setText(tr("Frame set. Add or choose a card photo."))
        else:
            self.status.setText(tr("The card is in the frame."))

    def closeEvent(self, e):  # noqa: N802
        self.ctx.pipeline.override = None
        super().closeEvent(e)
