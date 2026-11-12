"""“Card for a game” window: card photos placed into the frames a game draws over the camera.

Automatic: hides the app, shows calibration markers and then a flat grey picture in the camera, takes
screenshots and finds where the game shows the camera and where its frames are
(vcstudio/game/cardframe.py). Manual: the user draws a frame on the screenshot.

Several frames ("slots") per game, one card each; frames are saved per game as named layouts. Cards
are added with the button, by dropping image files on the window or by pasting (⌘V). Hotkeys
(Settings > Hotkeys) switch the card in the active frame and show/hide all cards from inside the game.
"""
from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from vcstudio.game import cardframe as cf
from vcstudio.i18n import tr

log = logging.getLogger(__name__)

SETTLE_MS = 1500     # time for the game to show the new camera picture (browser/emulator latency)


def _qimage(bgr: np.ndarray) -> QImage:
    rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    return QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()


class ScreenPicker(QWidget):
    """Shows a screenshot crop with the card frames; the user drags a rectangle (screenshot pixels)."""
    picked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 280)
        self.img: QImage | None = None
        self.origin = (0, 0)                     # screenshot pixel of the image's top-left (crop offset)
        self.frames: dict[int, np.ndarray] = {}  # slot -> screen quad
        self.active = 1
        self._drag: QPointF | None = None
        self._drag_rect: QRectF | None = None
        self.editable = False
        self.setCursor(Qt.CrossCursor)

    def set_image(self, bgr: np.ndarray, origin=(0, 0)) -> None:
        self.img, self.origin = _qimage(bgr), origin
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
            self._drag_rect = QRectF(self._drag, self._drag)
            self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag is not None:
            self._drag_rect = QRectF(self._drag, self._to_img(e.position())).normalized()
            self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._drag is None:
            return
        self._drag = None
        r, self._drag_rect = self._drag_rect, None
        if r is not None and r.width() > 8 and r.height() > 8:
            ox, oy = self.origin
            self.picked.emit(np.array([[r.left() + ox, r.top() + oy], [r.right() + ox, r.top() + oy],
                                       [r.right() + ox, r.bottom() + oy], [r.left() + ox, r.bottom() + oy]]))
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        if self.img is None:
            p.setPen(QColor("#8b949e"))
            p.drawText(self.rect().adjusted(20, 0, -20, 0), Qt.AlignCenter | Qt.TextWordWrap,
                       tr("Open the game with the camera on, then click “Find frames automatically”."))
            return
        s, ox, oy = self._scale()
        p.drawImage(QRectF(ox, oy, self.img.width() * s, self.img.height() * s), self.img)

        def box(q):
            q = np.asarray(q, float) - self.origin
            return QRectF(ox + q[:, 0].min() * s, oy + q[:, 1].min() * s,
                          (q[:, 0].max() - q[:, 0].min()) * s, (q[:, 1].max() - q[:, 1].min()) * s)
        for n, q in self.frames.items():
            c = QColor("#3fb950") if n == self.active else QColor("#d29922")
            r = box(q)
            p.setPen(QPen(c, 3 if n == self.active else 2))
            p.drawRect(r)
            tag = QRectF(r.x(), r.y(), 22, 20)
            p.fillRect(tag, c)
            p.setPen(QColor("#000000"))
            p.drawText(tag, Qt.AlignCenter, str(n))
        if self._drag_rect is not None:
            r = self._drag_rect
            p.setPen(QPen(QColor("#58a6ff"), 2, Qt.DashLine))
            p.drawRect(QRectF(ox + r.x() * s, oy + r.y() * s, r.width() * s, r.height() * s))


class CardList(QListWidget):
    """Card thumbnails; accepts dropped image files."""
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(88, 88))
        self.setMovement(QListWidget.Static)
        self.setMaximumHeight(140)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):  # noqa: N802
        self.dropped.emit(_paths_from_mime(e.mimeData()))
        e.acceptProposedAction()


def _paths_from_mime(md) -> list[str]:
    """Image files from a drop/paste: file URLs, or a raw image saved to a temporary PNG."""
    paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []
    if not paths and md.hasImage():
        img = QImage(md.imageData())
        if not img.isNull():
            out = Path(tempfile.mkdtemp()) / f"card-{time.strftime('%H%M%S')}.png"
            img.save(str(out))
            paths = [str(out)]
    return paths


class GameCardDialog(QDialog):
    def __init__(self, ctx, main_window=None):
        super().__init__(main_window)
        self.ctx, self.main_window = ctx, main_window
        self.setWindowTitle(tr("Card for a game"))
        self.resize(820, 780)
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        intro = QLabel(tr("A photo goes into the camera picture exactly where the game draws its frame. Keep the game "
                          "open on screen with this camera selected (OBS Virtual Camera), then find the frames "
                          "automatically or draw them yourself."))
        intro.setWordWrap(True); intro.setObjectName("muted")
        lay.addWidget(intro)

        # game layouts
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Game")))
        self.layouts = QComboBox(); self.layouts.setMinimumWidth(220)
        self.layouts.activated.connect(self._apply_layout)
        save = QPushButton(tr("Save frames as…")); save.clicked.connect(self.save_layout)
        self.del_layout = QPushButton(tr("Delete")); self.del_layout.clicked.connect(self.delete_layout)
        for w in (self.layouts, save, self.del_layout):
            row.addWidget(w)
        row.addStretch()
        lay.addLayout(row)

        # cards
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Cards")))
        hint = QLabel(tr("drop photos here or paste with ⌘V")); hint.setObjectName("muted")
        row.addWidget(hint)
        row.addStretch()
        add = QPushButton(tr("Add photos…")); add.clicked.connect(lambda: self.add_cards())
        self.rm_card = QPushButton(tr("Remove from list")); self.rm_card.clicked.connect(self.remove_card)
        row.addWidget(add); row.addWidget(self.rm_card)
        lay.addLayout(row)
        self.cards = CardList()
        self.cards.dropped.connect(self.add_cards)
        self.cards.itemClicked.connect(self._choose)
        lay.addWidget(self.cards)
        QShortcut(QKeySequence.Paste, self, activated=self.paste)

        # frames
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Card frame")))
        self.slots = QComboBox(); self.slots.setMinimumWidth(140)
        self.slots.activated.connect(lambda _i: self._set_active(self.slots.currentData()))
        new_slot = QPushButton(tr("New frame")); new_slot.clicked.connect(self.new_slot)
        self.del_slot = QPushButton(tr("Delete frame")); self.del_slot.clicked.connect(self.delete_slot)
        self.auto_btn = QPushButton(tr("Find frames automatically")); self.auto_btn.setObjectName("primary")
        self.manual_btn = QPushButton(tr("Draw the frame myself"))
        self.auto_btn.clicked.connect(self.find_automatically)
        self.manual_btn.clicked.connect(self.draw_manually)
        for w in (self.slots, new_slot, self.del_slot, self.auto_btn, self.manual_btn):
            row.addWidget(w)
        row.addStretch()
        lay.addLayout(row)
        self.status = QLabel(); self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.picker = ScreenPicker(); self.picker.picked.connect(self._picked)
        lay.addWidget(self.picker, 1)

        # options of the active frame
        row = QHBoxLayout()
        self.show_card = QCheckBox(tr("Show the card"))
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
        self.keys = QLabel(); self.keys.setObjectName("muted"); self.keys.setWordWrap(True)
        lay.addWidget(self.keys)

        self.cal: cf.Calibration | None = None
        self.shot: np.ndarray | None = None
        self._shots: dict[str, np.ndarray] = {}
        self._grid = "dense"
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)          # Enter in the dialog must not trigger a random button
        self.refresh()

    # ------------------------------------------------------------------ state
    def _body(self) -> dict:
        return self.ctx.profiles.active()["body"]

    def _active(self) -> int:
        return int(cf.game_state(dict(self._body())).get("active_slot", 1))

    def _mutate(self, fn) -> None:
        self.ctx.update_active_profile(fn)
        if not self.ctx.pipeline.replacement_enabled:
            self.ctx.pipeline.set_replacement_enabled(True)

    def refresh(self) -> None:
        """Rebuild every control from the profile (also called after hotkeys change the cards)."""
        from vcstudio.replacement.assets import Asset
        body = self._body()
        g = body.get("game", {})
        active = int(g.get("active_slot", 1))
        slots = cf.game_slots(body)
        roi = slots.get(active)
        rep = (roi or {}).get("replacement", {})
        assets = {d["id"]: d for d in body.get("assets", []) if d.get("kind") == "image"}

        self.layouts.blockSignals(True)
        self.layouts.clear()
        self.layouts.addItem(tr("— not saved —"), "")
        for name in sorted(g.get("layouts", {})):
            self.layouts.addItem(name, name)
        self.layouts.setCurrentIndex(max(0, self.layouts.findData(g.get("last_layout", ""))))
        self.layouts.blockSignals(False)
        self.del_layout.setEnabled(bool(self.layouts.currentData()))

        self.cards.clear()
        for aid in [a for a in g.get("cards", []) if a in assets]:
            a = Asset.from_dict(assets[aid])
            it = QListWidgetItem(a.name)
            it.setData(Qt.UserRole, a.id)
            try:
                th = self.ctx.assets.thumbnail(a, 88)
                rgb = cv2.cvtColor(th, cv2.COLOR_BGRA2RGBA if th.shape[2] == 4 else cv2.COLOR_BGR2RGB)
                qi = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                            QImage.Format_RGBA8888 if rgb.shape[2] == 4 else QImage.Format_RGB888).copy()
                it.setIcon(QIcon(QPixmap.fromImage(qi)))
            except Exception:  # noqa: BLE001 - a broken file still shows by name
                pass
            self.cards.addItem(it)
            if a.id == rep.get("asset_id"):
                self.cards.setCurrentItem(it)
        self.rm_card.setEnabled(self.cards.count() > 0)

        self.slots.blockSignals(True)
        self.slots.clear()
        for n in slots:
            self.slots.addItem(tr("Frame {n}", n=n), n)
        if not slots:
            self.slots.addItem(tr("Frame {n}", n=1), 1)
        self.slots.setCurrentIndex(max(0, self.slots.findData(active)))
        self.slots.blockSignals(False)
        self.del_slot.setEnabled(roi is not None)

        for w, v in ((self.show_card, (roi or {}).get("enabled", True)), (self.flip, rep.get("flip_h", False))):
            w.blockSignals(True); w.setChecked(bool(v)); w.blockSignals(False)
        self.fit.blockSignals(True)
        self.fit.setCurrentIndex(max(0, self.fit.findData(rep.get("fit", "fit"))))
        self.fit.blockSignals(False)
        for w in (self.show_card, self.fit, self.flip):
            w.setEnabled(roi is not None)

        if self.cal is not None:
            self.picker.frames = {n: self.cal.to_screen(_rect_corners(d, self.cal.camera_size)) for n, d in slots.items()}
        self.picker.active = active
        self.picker.update()
        hk = self.ctx.settings.get("hotkeys")
        show = lambda aid: QKeySequence(hk.get(aid, "")).toString(QKeySequence.NativeText) or "—"
        self.keys.setText(tr("Hotkeys (work inside the game too): next card {n} · previous {p} · show/hide {t}. "
                             "They act on the selected frame; change them in Settings > Hotkeys.",
                             n=show("card_next"), p=show("card_prev"), t=show("card_toggle")))
        if not self.status.text():
            if roi is None:
                self.status.setText(tr("No frame yet."))
            elif not rep.get("asset_id"):
                self.status.setText(tr("Frame set. Add or choose a card photo."))
            else:
                self.status.setText(tr("The card is in the frame."))

    def _set_active(self, n: int) -> None:
        self.ctx.update_active_profile(lambda b: cf.game_state(b).__setitem__("active_slot", int(n)))
        self.status.setText("")
        self.refresh()

    def _update(self, **kw) -> None:
        slot = self._active()
        if cf.game_card_roi(self._body(), slot) is None and "rect" not in kw:
            return                                   # no frame yet: nothing to update
        self._mutate(lambda b: cf.set_game_card(b, slot=slot, **kw))
        if "asset_id" in kw or "rect" in kw:
            self.refresh()

    # ------------------------------------------------------------------ cards
    def add_cards(self, paths=None) -> None:
        from vcstudio.replacement.assets import IMAGE_EXTS, AssetError
        if not paths:
            exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
            paths, _ = QFileDialog.getOpenFileNames(self, tr("Add photos…"), str(Path.home()), f"Images ({exts})")
        added, errors = [], []
        for p in paths or []:
            if Path(p).suffix.lower() not in IMAGE_EXTS:
                errors.append(tr("{name}: not an image", name=Path(p).name))
                continue
            try:
                added.append(self.ctx.assets.import_file(Path(p)).to_dict())
            except (AssetError, OSError) as e:
                errors.append(f"{Path(p).name}: {e}")
        if added:
            ids = [d["id"] for d in added]
            slot = self._active()

            def mut(b):
                b.setdefault("assets", []).extend(added)
                cf.add_cards(b, ids)
                d = cf.game_card_roi(b, slot)
                if d is not None and not d["replacement"].get("asset_id"):
                    cf.set_game_card(b, slot=slot, asset_id=ids[0])
            self._mutate(mut)
            self.status.setText(tr("Added {n} card(s).", n=len(added)) + (" " + "; ".join(errors) if errors else ""))
        elif errors:
            self.status.setText("; ".join(errors))
        self.refresh()

    def paste(self) -> None:
        paths = _paths_from_mime(QApplication.clipboard().mimeData())
        if paths:
            self.add_cards(paths)
        else:
            self.status.setText(tr("The clipboard has no picture."))

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        self.add_cards(_paths_from_mime(e.mimeData()))

    def remove_card(self) -> None:
        it = self.cards.currentItem()
        if it is not None:
            aid = it.data(Qt.UserRole)
            self._mutate(lambda b: cf.remove_card(b, aid))
            self.refresh()

    def _choose(self, item) -> None:
        if item is not None:
            self.status.setText("")
            self._update(asset_id=item.data(Qt.UserRole), enabled=True)

    # ------------------------------------------------------------------ frames / layouts
    def new_slot(self) -> None:
        n = cf.next_free_slot(self._body())
        self.ctx.update_active_profile(lambda b: cf.game_state(b).__setitem__("active_slot", n))
        self.status.setText(tr("Frame {n}: draw it on the picture below, or find frames automatically.", n=n))
        self.refresh()
        self.draw_manually()

    def delete_slot(self) -> None:
        n = self._active()
        self._mutate(lambda b: cf.remove_slot(b, n))
        self.status.setText("")
        self.refresh()

    def save_layout(self) -> None:
        cur = self.layouts.currentData() or ""
        name, ok = QInputDialog.getText(self, tr("Save frames as…"), tr("Game name"), text=cur)
        if ok and name.strip():
            self._save_layout(name)

    def _save_layout(self, name: str) -> None:
        def mut(b):
            cf.save_layout(b, name)
            cf.game_state(b)["last_layout"] = name.strip()
        self.ctx.update_active_profile(mut)
        self.status.setText(tr("Frames saved for “{name}”.", name=name.strip()))
        self.refresh()

    def _apply_layout(self, _i=None) -> None:
        name = self.layouts.currentData()
        if not name:
            return
        self._mutate(lambda b: cf.apply_layout(b, name))
        self.status.setText(tr("Frames of “{name}” applied.", name=name))
        self.refresh()

    def delete_layout(self) -> None:
        name = self.layouts.currentData()
        if name:
            self.ctx.update_active_profile(lambda b: (cf.delete_layout(b, name), cf.game_state(b).pop("last_layout", None)))
            self.refresh()

    # ------------------------------------------------------------------ calibration
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
        self.cal = None
        self._grid = "dense"
        self.ctx.pipeline.override = cf.calibration_frame
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
        try:
            if self.cal is None:
                self.cal = cf.calibrate(self._shots["markers"], self._camera_size(), grid=self._grid)
        except cf.CalibrationError as e:
            self.cal = None
            self.status.setText(tr("The game's camera picture was not found on screen ({e}). Make sure the game is "
                                   "visible on the main display, shows this camera, and that Screen Recording is "
                                   "allowed for this app (System Settings > Privacy & Security).", e=e))
            self._show_shot(self._shots.get("grey"))
            return
        self.shot = self._shots["grey"]
        frames = cf.find_frames(self.shot, self.cal)
        self._show_shot(self.shot)
        note = tr(" The game shows the camera mirrored; if the card looks mirrored there, tick “Mirror the card”.") \
            if self.cal.mirrored else ""
        if not frames:
            self.status.setText(tr("Found the camera picture in the game, but not a frame in it. Draw the frame on the "
                                   "picture below.") + note)
            self.refresh()
            return
        cal = self.cal
        rects = [cf.frame_to_rect(cal, q) for q in frames]

        def mut(b):
            # frames in reading order fill frames 1..k; each keeps its card, new ones get the next card
            g = cf.game_state(b)
            pool = [a for a in g["cards"] if any(x.get("id") == a for x in b.get("assets", []))]
            for i, r in enumerate(rects, 1):
                d = cf.game_card_roi(b, i)
                kw = {"rect": r, "enabled": True}
                if (d is None or not d.get("replacement", {}).get("asset_id")) and pool:
                    kw["asset_id"] = pool[(i - 1) % len(pool)]
                cf.set_game_card(b, slot=i, **kw)
            for n in [n for n in cf.game_slots(b) if n > len(rects)]:
                cf.remove_slot(b, n)
            g["active_slot"] = 1
        self._mutate(mut)
        self.status.setText(tr("Frames found: {n}. Cards placed. If a frame is wrong, select it and draw it below.",
                               n=len(frames)) + note)
        self.refresh()

    def draw_manually(self) -> None:
        if self.cal is None:
            self.status.setText(tr("First the app has to see how the game shows the camera: click “Find frames "
                                   "automatically”, then draw the frame on the picture if it is wrong."))
            return
        self.picker.editable = True
        self.status.setText(tr("Drag a rectangle over the game's frame in the picture below (frame {n}).",
                               n=self._active()))

    def _camera_size(self) -> tuple[int, int]:
        f, _ = self.ctx.capture.slot.peek()
        if f is not None:
            return f.width, f.height
        return self.ctx.settings.get("output.width"), self.ctx.settings.get("output.height")

    def _show_shot(self, shot) -> None:
        if shot is None:
            return
        x0, y0 = 0, 0
        if self.cal is not None:   # crop to the camera picture (plus a margin) so the frames are easy to see
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
        rect = cf.frame_to_rect(self.cal, quad)
        slot = self._active()
        item = self.cards.currentItem()
        aid = item.data(Qt.UserRole) if item is not None else None

        def mut(b):
            kw = {"rect": rect, "enabled": True}
            d = cf.game_card_roi(b, slot)
            if aid and (d is None or not d.get("replacement", {}).get("asset_id")):
                kw["asset_id"] = aid
            cf.set_game_card(b, slot=slot, **kw)
        self._mutate(mut)
        self.status.setText(tr("Frame {n} set.", n=slot))
        self.refresh()

    def closeEvent(self, e):  # noqa: N802
        self.ctx.pipeline.override = None
        super().closeEvent(e)


def _rect_corners(d: dict, camera_size) -> np.ndarray:
    """Camera-pixel corners of a stored rect region."""
    from vcstudio.roi.model import rect_corners
    W, H = camera_size
    c = rect_corners(d["cx"] * W, d["cy"] * H, d["w"] * W, d["h"] * H, d.get("rotation", 0.0))
    return np.asarray(c, float)
