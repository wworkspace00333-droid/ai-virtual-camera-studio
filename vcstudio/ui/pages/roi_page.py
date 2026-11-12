from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget)

from vcstudio.core.frame import host_time_s
from vcstudio.roi.model import MODES, ROI, ROIError
from vcstudio.ui.roi_canvas import MODE_COLORS, RoiCanvas
from vcstudio.ui.widgets import Card
from vcstudio.i18n import region_name, tr

log = logging.getLogger(__name__)

MODE_LABELS = {"manual": "Manual (drawn area)", "auto": "Automatic AI (tracked objects)",
               "hybrid": "Hybrid (objects inside area)"}


class ROIPage:
    key, title = "roi", "ROI Editor"

    def __init__(self, ctx, feeder):
        self.ctx, self.feeder = ctx, feeder
        self.widget = QWidget(); self.widget.setObjectName("page")
        outer = QHBoxLayout(self.widget)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(14)

        # ---------------- left: toolbar + canvas
        left = QVBoxLayout()
        head = QLabel(tr("ROI Editor")); head.setObjectName("h1")
        left.addWidget(head)
        bar = QHBoxLayout()
        self.tools = QButtonGroup(self.widget)
        self.tool_btns = {}
        for key, label, tip in (("select", "Select", "Select, move, resize (Shift keeps aspect), rotate (Shift snaps 15°)"),
                                ("rect", "Rectangle", "Drag to draw a rectangle"),
                                ("polygon", "Polygon", "Click points; double-click / Enter / click first point to close")):
            b = QToolButton(); b.setText(tr(label)); b.setCheckable(True); b.setToolTip(tr(tip))
            b.setMinimumWidth(90)
            self.tools.addButton(b); self.tool_btns[key] = b
            bar.addWidget(b)
        self.tool_btns["select"].setChecked(True)
        self.add_auto = QPushButton(tr("+ Automatic AI region"))
        bar.addWidget(self.add_auto)
        bar.addStretch()
        left.addLayout(bar)
        # own line, wrapped: in the button row the long Russian hint made the page wider than the window
        self.hint = QLabel(); self.hint.setObjectName("muted"); self.hint.setWordWrap(True)
        left.addWidget(self.hint)
        self.canvas = RoiCanvas()
        self.canvas.setMinimumSize(640, 360)
        feeder.add_view(self.canvas)
        left.addWidget(self.canvas, 1)
        legend = QLabel(" · ".join(f"<span style='color:{MODE_COLORS[m].name()}'>■</span> {tr(m)}" for m in MODES) +
                        " · " + tr("dashed = live regions resolved from tracked objects"))
        legend.setObjectName("muted"); legend.setWordWrap(True)
        left.addWidget(legend)
        outer.addLayout(left, 3)

        # ---------------- right: list + properties
        right = QVBoxLayout()
        lst = Card("Regions (top = highest priority)")
        self.list = QListWidget(); self.list.setMinimumHeight(150)
        lst.layout_.addWidget(self.list)
        row = QHBoxLayout()
        self.btn_dup = QPushButton(tr("Duplicate")); self.btn_del = QPushButton(tr("Delete")); self.btn_del.setObjectName("danger")
        self.btn_up = QPushButton(tr("▲")); self.btn_down = QPushButton(tr("▼"))
        for b in (self.btn_up, self.btn_down, self.btn_dup, self.btn_del):
            row.addWidget(b)
        lst.layout_.addLayout(row)
        right.addWidget(lst)

        props = Card("Properties")
        f = QFormLayout()
        self.name = QLineEdit()
        self.mode = QComboBox()
        for m in MODES:
            self.mode.addItem(tr(MODE_LABELS[m]), m)
        self.enabled = QCheckBox(tr("Enabled"))
        self.priority = QSpinBox(); self.priority.setRange(-999, 999)
        self.shape = QLabel()
        f.addRow(tr("Name"), self.name)
        f.addRow(tr("Mode"), self.mode)
        f.addRow(tr(""), self.enabled)
        f.addRow(tr("Priority"), self.priority)
        f.addRow(tr("Shape"), self.shape)
        props.layout_.addLayout(f)
        # geometry (manual coordinate entry, frame pixels)
        self.geo_rect = QWidget(); gr = QFormLayout(self.geo_rect); gr.setContentsMargins(0, 0, 0, 0)
        self.x = QDoubleSpinBox(); self.y = QDoubleSpinBox(); self.w = QDoubleSpinBox(); self.h = QDoubleSpinBox()
        for sp in (self.x, self.y, self.w, self.h):
            sp.setRange(-10000, 10000); sp.setDecimals(1); sp.setSuffix(tr(" px"))
        self.rot = QDoubleSpinBox(); self.rot.setRange(-180, 180); self.rot.setDecimals(1); self.rot.setSuffix(tr("°"))
        r1 = QHBoxLayout(); r1.addWidget(self.x); r1.addWidget(self.y)
        r2 = QHBoxLayout(); r2.addWidget(self.w); r2.addWidget(self.h)
        gr.addRow(tr("Left, top"), r1); gr.addRow(tr("Width, height"), r2); gr.addRow(tr("Rotation"), self.rot)
        self.geo_poly = QWidget(); gp = QVBoxLayout(self.geo_poly); gp.setContentsMargins(0, 0, 0, 0)
        gp.addWidget(QLabel(tr("Vertices (x, y px; one per line)")))
        self.pts = QPlainTextEdit(); self.pts.setMaximumHeight(110)
        self.pts_apply = QPushButton(tr("Apply vertices"))
        gp.addWidget(self.pts); gp.addWidget(self.pts_apply)
        props.layout_.addWidget(self.geo_rect); props.layout_.addWidget(self.geo_poly)
        right.addWidget(props)

        ai = Card("AI selection (Automatic / Hybrid)")
        fa = QFormLayout()
        self.classes = QLineEdit(); self.classes.setPlaceholderText(tr("any class (e.g. person, car)"))
        self.classes.setToolTip(tr("Comma-separated detector classes; empty means any class"))
        self.excl = QLineEdit(); self.excl.setPlaceholderText(tr("none"))
        self.minconf = QDoubleSpinBox(); self.minconf.setRange(0, 1); self.minconf.setSingleStep(0.05)
        self.inclusion = QComboBox(); self.inclusion.addItem(tr("Centre inside area"), "center")
        self.inclusion.addItem(tr("Overlap ≥ threshold"), "overlap")
        self.overlap = QDoubleSpinBox(); self.overlap.setRange(0.05, 1); self.overlap.setSingleStep(0.05)
        self.maxobj = QSpinBox(); self.maxobj.setRange(0, 100); self.maxobj.setSpecialValueText(tr("unlimited"))
        self.padding = QSpinBox(); self.padding.setRange(0, 200); self.padding.setSuffix(tr(" %"))
        self.clip = QCheckBox(tr("Clip regions to the area"))
        self.coast = QCheckBox(tr("Keep while object is briefly lost"))
        fa.addRow(tr("Classes"), self.classes); fa.addRow(tr("Exclude"), self.excl); fa.addRow(tr("Min confidence"), self.minconf)
        fa.addRow(tr("Inside rule"), self.inclusion); fa.addRow(tr("Overlap"), self.overlap)
        fa.addRow(tr("Max objects"), self.maxobj); fa.addRow(tr("Padding"), self.padding)
        fa.addRow(tr(""), self.clip); fa.addRow(tr(""), self.coast)
        ai.layout_.addLayout(fa)
        self.warn = QLabel(); self.warn.setWordWrap(True); self.warn.setStyleSheet("color: #d29922;")
        ai.layout_.addWidget(self.warn)
        self.ai_card = ai
        right.addWidget(ai)
        right.addStretch()
        rw = QWidget(); rw.setLayout(right); rw.setMaximumWidth(460); rw.setMinimumWidth(400)
        outer.addWidget(rw, 1)

        # ---------------- wiring
        self._loading = False
        self._save_timer = QTimer(self.widget); self._save_timer.setSingleShot(True); self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self._persist)
        for k, b in self.tool_btns.items():
            b.toggled.connect(lambda on, k=k: on and self._set_tool(k))
        self.add_auto.clicked.connect(self._add_auto)
        self.canvas.rois_changed.connect(self._canvas_changed)
        self.canvas.selection_changed.connect(self._select)
        self.canvas.created.connect(lambda _id: self.tool_btns["select"].setChecked(True))
        self.list.currentRowChanged.connect(self._list_row)
        self.list.itemChanged.connect(self._list_item_changed)
        self.btn_dup.clicked.connect(self._duplicate)
        self.btn_del.clicked.connect(self._delete)
        self.btn_up.clicked.connect(lambda: self._move(+1))
        self.btn_down.clicked.connect(lambda: self._move(-1))
        for w in (self.name, self.classes, self.excl):
            w.editingFinished.connect(self._props_changed)
        for w in (self.mode, self.inclusion):
            w.currentIndexChanged.connect(self._props_changed)
        for w in (self.enabled, self.clip, self.coast):
            w.toggled.connect(self._props_changed)
        for w in (self.priority, self.maxobj, self.padding):
            w.valueChanged.connect(self._props_changed)
        for w in (self.x, self.y, self.w, self.h, self.rot, self.minconf, self.overlap):
            w.valueChanged.connect(self._props_changed)
        self.pts_apply.clicked.connect(self._apply_points)
        ctx.profiles.add_listener(lambda e, n: self.load() if e == "switched" else None)
        self.timer = QTimer(self.widget); self.timer.timeout.connect(self._live); self.timer.start(66)
        self._set_tool("select")
        self.load()

    # ---------------------------------------------------------------- data
    def load(self) -> None:
        from vcstudio.roi.model import rois_from_profile
        self.canvas.set_rois(rois_from_profile(self.ctx.profiles.active()["body"]))
        self._refresh_list()
        self._show_props()

    def _persist(self) -> None:
        dicts = [r.to_dict() for r in self.canvas.rois]
        self.ctx.update_active_profile(lambda b: b.__setitem__("rois", dicts))

    def _commit(self) -> None:
        self._refresh_list()
        self._show_props()
        self.canvas.update()
        self._save_timer.start()

    def _canvas_changed(self) -> None:
        self._commit()

    def _selected(self) -> ROI | None:
        return self.canvas.roi(self.canvas.selected)

    # ---------------------------------------------------------------- list
    def _ordered(self) -> list[ROI]:
        return sorted(self.canvas.rois, key=lambda r: (-r.priority, r.name))

    def _refresh_list(self) -> None:
        self._loading = True
        self.list.clear()
        sel_row = -1
        for i, r in enumerate(self._ordered()):
            it = QListWidgetItem(f"{region_name(r.name)}   [{tr(r.mode)}, {tr(r.kind if r.mode != 'auto' else 'objects')}]")
            it.setData(Qt.UserRole, r.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if r.enabled else Qt.Unchecked)
            it.setForeground(MODE_COLORS[r.mode])
            self.list.addItem(it)
            if r.id == self.canvas.selected:
                sel_row = i
        self.list.setCurrentRow(sel_row)
        self._loading = False
        has = self._selected() is not None
        for b in (self.btn_dup, self.btn_del, self.btn_up, self.btn_down):
            b.setEnabled(has)

    def _list_row(self, row: int) -> None:
        if self._loading:
            return
        it = self.list.item(row)
        self.canvas.selected = it.data(Qt.UserRole) if it else ""
        self.canvas.update()
        self._show_props()
        has = self._selected() is not None
        for b in (self.btn_dup, self.btn_del, self.btn_up, self.btn_down):
            b.setEnabled(has)

    def _list_item_changed(self, it: QListWidgetItem) -> None:
        if self._loading:
            return
        r = self.canvas.roi(it.data(Qt.UserRole))
        if r is not None and r.enabled != (it.checkState() == Qt.Checked):
            r.enabled = it.checkState() == Qt.Checked
            self._commit()

    def _select(self, rid: str) -> None:
        self._refresh_list()
        self._show_props()

    def _duplicate(self) -> None:
        r = self._selected()
        if r is None:
            return
        import uuid
        c = r.copy(id=uuid.uuid4().hex[:10], name=self.canvas._next_name(r.name.rsplit(" ", 1)[0] or r.name),
                   priority=max(q.priority for q in self.canvas.rois) + 1)
        if c.mode != "auto":
            c.translate(0.03, 0.03)
        self.canvas.rois.append(c)
        self.canvas.selected = c.id
        self._commit()

    def _delete(self) -> None:
        r = self._selected()
        if r is None:
            return
        self.canvas.rois = [q for q in self.canvas.rois if q.id != r.id]
        self.canvas.selected = ""
        self._commit()

    def _move(self, direction: int) -> None:
        """Swap priority with the neighbour above (+1) or below (-1)."""
        r = self._selected()
        order = self._ordered()
        if r is None:
            return
        i = order.index(r)
        j = i - direction
        if not 0 <= j < len(order):
            return
        # normalise priorities to list order, then swap
        n = len(order)
        for k, q in enumerate(order):
            q.priority = n - k
        order[i].priority, order[j].priority = order[j].priority, order[i].priority
        self._commit()

    def _add_auto(self) -> None:
        r = ROI(name=self.canvas._next_name("Auto"), mode="auto", kind="rect", classes=["person"],
                priority=max((q.priority for q in self.canvas.rois), default=-1) + 1)
        self.canvas.rois.append(r)
        self.canvas.selected = r.id
        self._commit()

    def _set_tool(self, tool: str) -> None:
        self.canvas.set_tool(tool)
        self.hint.setText(tr({"select": "Drag to move · handles resize/rotate · Delete removes · arrows nudge",
                              "rect": "Drag on the video to draw a rectangle",
                              "polygon": "Click to add points · double-click/Enter closes · Esc cancels"}[tool]))

    # ---------------------------------------------------------------- properties
    def _show_props(self) -> None:
        r = self._selected()
        self._loading = True
        widgets = [self.name, self.mode, self.enabled, self.priority, self.x, self.y, self.w, self.h, self.rot,
                   self.classes, self.excl, self.minconf, self.inclusion, self.overlap, self.maxobj, self.padding,
                   self.clip, self.coast, self.pts, self.pts_apply]
        for w in widgets:
            w.setEnabled(r is not None)
            w.blockSignals(True)
        if r is not None:
            fw, fh = self.canvas.frame_size
            self.name.setText(region_name(r.name))
            self.mode.setCurrentIndex(self.mode.findData(r.mode))
            self.enabled.setChecked(r.enabled)
            self.priority.setValue(r.priority)
            self.shape.setText(tr("tracked object boxes") if r.mode == "auto" else
                               (tr("rectangle") if r.kind == "rect" else tr("polygon ({n} points)", n=len(r.points))))
            self.x.setValue((r.cx - r.w / 2) * fw); self.y.setValue((r.cy - r.h / 2) * fh)
            self.w.setValue(r.w * fw); self.h.setValue(r.h * fh); self.rot.setValue(r.rotation)
            self.pts.setPlainText("\n".join(f"{x * fw:.1f}, {y * fh:.1f}" for x, y in r.points))
            self.classes.setText(", ".join(r.classes)); self.excl.setText(", ".join(r.exclude_classes))
            self.minconf.setValue(r.min_confidence)
            self.inclusion.setCurrentIndex(self.inclusion.findData(r.inclusion))
            self.overlap.setValue(r.overlap_threshold)
            self.maxobj.setValue(r.max_objects); self.padding.setValue(int(round(r.padding * 100)))
            self.clip.setChecked(r.clip_to_area); self.coast.setChecked(r.include_coasting)
        else:
            self.shape.setText(tr(""))
        for w in widgets:
            w.blockSignals(False)
        geo = r is not None and r.mode != "auto"
        self.geo_rect.setVisible(geo and r.kind == "rect")
        self.geo_poly.setVisible(geo and r.kind == "polygon")
        self.ai_card.setEnabled(r is not None and r.mode != "manual")
        self.overlap.setEnabled(r is not None and r.inclusion == "overlap")
        self._loading = False
        self._update_warning()

    def _split(self, text: str) -> list[str]:
        return [t.strip() for t in text.split(",") if t.strip()]

    def _props_changed(self, *_):
        if self._loading:
            return
        r = self._selected()
        if r is None:
            return
        fw, fh = self.canvas.frame_size
        before = r.to_dict()
        new = self.name.text().strip()
        if new and new != region_name(r.name):   # unchanged shown name keeps the stored (English) built-in name
            r.name = new
        r.mode = self.mode.currentData()
        r.enabled = self.enabled.isChecked()
        r.priority = self.priority.value()
        if r.kind == "rect":
            w, h = max(self.w.value(), 4.0), max(self.h.value(), 4.0)
            r.w, r.h = w / fw, h / fh
            r.cx, r.cy = (self.x.value() + w / 2) / fw, (self.y.value() + h / 2) / fh
            r.rotation = self.rot.value()
        r.classes = self._split(self.classes.text())
        r.exclude_classes = self._split(self.excl.text())
        r.min_confidence = self.minconf.value()
        r.inclusion = self.inclusion.currentData()
        r.overlap_threshold = self.overlap.value()
        r.max_objects = self.maxobj.value()
        r.padding = self.padding.value() / 100
        r.clip_to_area = self.clip.isChecked()
        r.include_coasting = self.coast.isChecked()
        try:
            r.validate()
        except ROIError as e:
            for k, v in before.items():
                setattr(r, k, v)
            self.warn.setText(str(e))
            return
        if r.to_dict() != before:
            self._commit()

    def _apply_points(self) -> None:
        r = self._selected()
        if r is None or r.kind != "polygon":
            return
        fw, fh = self.canvas.frame_size
        try:
            pts = []
            for line in self.pts.toPlainText().splitlines():
                if line.strip():
                    x, y = (float(v) for v in line.replace(";", ",").split(",")[:2])
                    pts.append([x / fw, y / fh])
            old = r.points
            r.points = pts
            r.validate()
        except (ValueError, ROIError) as e:
            r.points = old if "old" in locals() else r.points
            self.warn.setText(tr("Vertices not applied: {e}", e=e))
            return
        self._commit()

    def _update_warning(self) -> None:
        r = self._selected()
        msgs = []
        body = self.ctx.profiles.active()["body"]
        if r is not None and r.mode != "manual":
            if not body["ai"].get("enabled", True):
                msgs.append("AI is disabled in this profile: this region will stay empty.")
            det = body["detection"]
            inc, exc = set(det.get("include_classes", [])), set(det.get("exclude_classes", []))
            known = set(self.ctx.models.available().get("yolo11n").classes) | {"face"} if self.ctx.models.available().get("yolo11n") else set()
            for c in r.classes:
                if known and c not in known:
                    msgs.append(tr("'{c}' is not a class of the detector.", c=tr(c)))
                elif (inc and c not in inc) or c in exc:
                    msgs.append(tr("'{c}' is filtered out on the Detection page.", c=tr(c)))
                elif c == "face" and body["ai"].get("face_detection") != "vision":
                    msgs.append("Face regions need Apple Vision face detection (AI Engine page).")
        self.warn.setText(" ".join(msgs))

    # ---------------------------------------------------------------- live
    def _live(self) -> None:
        if not self.widget.isVisible():
            return
        f, _ = self.ctx.capture.latest()
        if f is not None:
            self.canvas.frame_size = (f.width, f.height)
            t = f.pts
        else:
            self.canvas.frame_size = (self.ctx.settings.get("output.width"), self.ctx.settings.get("output.height"))
            t = host_time_s()
        tracked = self.ctx.ai.tracked_at(t)
        self.canvas.regions = self.ctx.rois.resolve(tracked, *self.canvas.frame_size) if tracked else []
        n_obj = sum(1 for r in self.canvas.regions if r.is_object)
        self.canvas.overlay = tr("{n} live object region(s)", n=n_obj) if n_obj else ""
        self.canvas.update()
