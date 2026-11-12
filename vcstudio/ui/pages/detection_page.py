from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QHeaderView, QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QRadioButton, QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QTreeWidget,
                               QTreeWidgetItem)

from vcstudio.ai.labels import FACE, GROUPS, group_of
from vcstudio.core.config import PERFORMANCE_MODES
from vcstudio.ui.widgets import Card, page_scaffold
from vcstudio.i18n import tr


class DetectionPage:
    key, title = "detection", "Detection"

    def __init__(self, ctx, feeder):
        self.ctx, self.feeder = ctx, feeder
        self.widget, lay = page_scaffold("Detection", "Thresholds, rate and class filters for the active profile. "
                                         "Changes apply immediately.")
        top = QHBoxLayout()
        params = Card("Parameters")
        f = QFormLayout()
        self.conf = QSlider(Qt.Horizontal); self.conf.setRange(5, 95)
        self.conf_lbl = QLabel()
        self.iou = QSlider(Qt.Horizontal); self.iou.setRange(10, 95)
        self.iou_lbl = QLabel()
        r1 = QHBoxLayout(); r1.addWidget(self.conf); r1.addWidget(self.conf_lbl)
        r2 = QHBoxLayout(); r2.addWidget(self.iou); r2.addWidget(self.iou_lbl)
        self.freq_auto = QRadioButton(tr("From performance mode"))
        self.freq_fixed = QRadioButton(tr("Fixed rate"))
        self.freq_group = QButtonGroup(self.widget)
        self.freq_group.addButton(self.freq_auto); self.freq_group.addButton(self.freq_fixed)
        self.fixed = QDoubleSpinBox(); self.fixed.setRange(1, 60); self.fixed.setSuffix(tr(" fps")); self.fixed.setDecimals(0)
        r3 = QHBoxLayout(); r3.addWidget(self.freq_auto); r3.addWidget(self.freq_fixed); r3.addWidget(self.fixed); r3.addStretch()
        self.mode_lbl = QLabel(); self.mode_lbl.setObjectName("muted"); self.mode_lbl.setWordWrap(True)
        self.show = QCheckBox(tr("Show detection boxes on the original preview"))
        self.show.setChecked(True)
        f.addRow(tr("Confidence threshold"), r1)
        f.addRow(tr("NMS IoU threshold"), r2)
        f.addRow(tr("Detection frequency"), r3)
        f.addRow(tr(""), self.mode_lbl)
        f.addRow(tr(""), self.show)
        params.layout_.addLayout(f)
        params.layout_.addStretch()
        top.addWidget(params, 3)

        classes = Card("Classes")
        self.cls_all = QRadioButton(tr("Detect all classes except the checked ones"))
        self.cls_only = QRadioButton(tr("Detect only the checked classes"))
        self.cls_group = QButtonGroup(self.widget)
        self.cls_group.addButton(self.cls_all); self.cls_group.addButton(self.cls_only)
        classes.layout_.addWidget(self.cls_all); classes.layout_.addWidget(self.cls_only)
        sr = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText(tr("Search classes…"))
        clear = QPushButton(tr("Uncheck all"))
        sr.addWidget(self.search); sr.addWidget(clear)
        classes.layout_.addLayout(sr)
        self.tree = QTreeWidget(); self.tree.setHeaderHidden(True); self.tree.setMinimumHeight(260)
        classes.layout_.addWidget(self.tree)
        top.addWidget(classes, 2)
        lay.addLayout(top)

        live = Card("Live detections")
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([tr("ID"), tr("Class"), tr("State"), tr("Confidence"), tr("Box (x0, y0, x1, y1)"), tr("Size"), tr("Age")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(200)
        self.live_info = QLabel(); self.live_info.setObjectName("muted")
        live.layout_.addWidget(self.live_info)
        live.layout_.addWidget(self.table)
        lay.addWidget(live)
        trk = Card("Tracking (ByteTrack)")
        tf = QFormLayout()
        self.trk_on = QCheckBox(tr("Track objects"))
        self.trk_on.setToolTip(tr("Track objects (stable IDs, smoothing, occlusion recovery, per-frame interpolation)"))
        self.trk_low = QDoubleSpinBox(); self.trk_low.setRange(0.01, 0.5); self.trk_low.setSingleStep(0.05)
        self.trk_buf = QDoubleSpinBox(); self.trk_buf.setRange(0.1, 10); self.trk_buf.setSuffix(tr(" s")); self.trk_buf.setSingleStep(0.5)
        self.trk_hits = QSpinBox(); self.trk_hits.setRange(1, 10)
        self.trk_hits.setToolTip(tr("Higher hides more one-off false detections but shows new objects slightly later."))
        self.trk_coast = QDoubleSpinBox(); self.trk_coast.setRange(0.0, 3); self.trk_coast.setSuffix(tr(" s")); self.trk_coast.setSingleStep(0.1)
        self.trk_smooth = QSlider(Qt.Horizontal); self.trk_smooth.setRange(0, 100)
        self.trk_smooth_lbl = QLabel()
        rs = QHBoxLayout(); rs.addWidget(self.trk_smooth); rs.addWidget(self.trk_smooth_lbl)
        self.trk_class = QCheckBox(tr("Class-aware matching (an ID never changes class)"))
        tf.addRow(tr(""), self.trk_on)
        tf.addRow(tr("Low-score threshold"), self.trk_low)
        tf.addRow(tr("Lost-object recovery window"), self.trk_buf)
        tf.addRow(tr("Keep showing lost objects for"), self.trk_coast)
        tf.addRow(tr("Show a new object after N detections in a row"), self.trk_hits)
        tf.addRow(tr("Smoothing"), rs)
        tf.addRow(tr(""), self.trk_class)
        trk.layout_.addLayout(tf)
        note = QLabel(tr("The confidence threshold above is ByteTrack's high threshold. Detections between the "
                      "low and high thresholds can only continue existing tracks."))
        note.setObjectName("muted"); note.setWordWrap(True)
        trk.layout_.addWidget(note)
        self.trk_stats = QLabel(); self.trk_stats.setObjectName("muted")
        trk.layout_.addWidget(self.trk_stats)
        lay.insertWidget(lay.indexOf(live), trk)
        lay.addStretch()

        self._build_tree()
        self._load()
        self._save_timer = QTimer(self.widget); self._save_timer.setSingleShot(True); self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self._save)
        for s in (self.conf, self.iou):
            s.valueChanged.connect(self._changed)
        self.fixed.valueChanged.connect(self._changed)
        for b in (self.freq_auto, self.freq_fixed, self.cls_all, self.cls_only, self.trk_on, self.trk_class):
            b.toggled.connect(self._changed)
        for w in (self.trk_low, self.trk_buf, self.trk_coast, self.trk_hits):
            w.valueChanged.connect(self._changed)
        self.trk_smooth.valueChanged.connect(self._changed)
        self.tree.itemChanged.connect(self._item_changed)
        self.search.textChanged.connect(self._filter_tree)
        clear.clicked.connect(self._uncheck_all)
        self.show.toggled.connect(lambda v: setattr(self.feeder, "show_detections", v))
        ctx.profiles.add_listener(lambda e, n: self._load() if e == "switched" else None)
        ctx.settings.add_listener(lambda k, v: self._update_labels() if k.startswith("performance") else None)
        self.timer = QTimer(self.widget); self.timer.timeout.connect(self.refresh_live); self.timer.start(250)

    def _build_tree(self):
        self._items: dict[str, QTreeWidgetItem] = {}
        self.tree.blockSignals(True)
        names = list(self.ctx.models.available().get("yolo11n").classes) if self.ctx.models.available().get("yolo11n") else []
        allnames = set(names) | {FACE}
        groups: dict[str, list[str]] = {}
        for n in sorted(allnames):
            groups.setdefault(group_of(n), []).append(n)
        for g in list(GROUPS) + ["Other"]:
            if g not in groups:
                continue
            gi = QTreeWidgetItem([tr(g)])
            gi.setFlags(gi.flags() | Qt.ItemIsAutoTristate | Qt.ItemIsUserCheckable)
            self.tree.addTopLevelItem(gi)
            for n in groups[g]:
                it = QTreeWidgetItem([tr(n) + ("  (" + tr("Vision; requires face detection on AI Engine page") + ")" if n == FACE else "")])
                it.setData(0, Qt.UserRole, n)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Unchecked)
                gi.addChild(it)
                self._items[n] = it
        self.tree.blockSignals(False)

    def _load(self):
        body = self.ctx.profiles.active()["body"]
        det, trk = body["detection"], body["tracking"]
        widgets = (self.conf, self.iou, self.fixed, self.freq_auto, self.freq_fixed, self.cls_all, self.cls_only, self.tree,
                   self.trk_on, self.trk_low, self.trk_buf, self.trk_coast, self.trk_smooth, self.trk_class, self.trk_hits)
        for w in widgets:
            w.blockSignals(True)
        self.conf.setValue(int(round(det["confidence"] * 100)))
        self.iou.setValue(int(round(det["iou"] * 100)))
        self.fixed.setValue(det.get("fixed_fps", 15))
        (self.freq_fixed if det.get("frequency_mode") == "fixed" else self.freq_auto).setChecked(True)
        include = det.get("include_classes", [])
        (self.cls_only if include else self.cls_all).setChecked(True)
        checked = set(include or det.get("exclude_classes", []))
        for n, it in self._items.items():
            it.setCheckState(0, Qt.Checked if n in checked else Qt.Unchecked)
        self.trk_on.setChecked(trk.get("enabled", True))
        self.trk_low.setValue(trk.get("low_thresh", 0.1))
        self.trk_buf.setValue(trk.get("track_buffer_s", 1.5))
        self.trk_coast.setValue(trk.get("coast_s", 0.4))
        self.trk_hits.setValue(int(trk.get("min_hits", 3)))
        self.trk_smooth.setValue(int(round(trk.get("smoothing", 0.5) * 100)))
        self.trk_class.setChecked(trk.get("class_aware", True))
        for w in widgets:
            w.blockSignals(False)
        self._update_labels()

    def _update_labels(self):
        self.conf_lbl.setText(f"{self.conf.value() / 100:.2f}")
        self.iou_lbl.setText(f"{self.iou.value() / 100:.2f}")
        self.fixed.setEnabled(self.freq_fixed.isChecked())
        self.trk_smooth_lbl.setText(f"{self.trk_smooth.value() / 100:.2f}")
        for w in (self.trk_low, self.trk_buf, self.trk_coast, self.trk_smooth, self.trk_class, self.trk_hits):
            w.setEnabled(self.trk_on.isChecked())
        mode = PERFORMANCE_MODES[self.ctx.settings.get("performance.mode")]
        tgt = tr("as fast as sustainable") if mode.ai_fps is None else f"{mode.ai_fps:g} " + tr("fps")
        self.mode_lbl.setText(tr("Current performance mode: {mode} → AI target {tgt}", mode=tr(mode.label), tgt=tgt))

    def _changed(self, *_):
        self._update_labels()
        self._save_timer.start()

    def _item_changed(self, *_):
        self._save_timer.start()

    def _checked(self) -> list[str]:
        return sorted(n for n, it in self._items.items() if it.checkState(0) == Qt.Checked)

    def _uncheck_all(self):
        for it in self._items.values():
            it.setCheckState(0, Qt.Unchecked)

    def _filter_tree(self, text):
        t = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            any_vis = False
            for j in range(g.childCount()):
                c = g.child(j)
                vis = not t or t in c.data(0, Qt.UserRole)
                c.setHidden(not vis)
                any_vis |= vis
            g.setHidden(not any_vis)
            g.setExpanded(bool(t))

    def _save(self):
        checked = self._checked()

        def mutate(b):
            d = b["detection"]
            d["confidence"] = self.conf.value() / 100
            d["iou"] = self.iou.value() / 100
            d["frequency_mode"] = "fixed" if self.freq_fixed.isChecked() else "auto"
            d["fixed_fps"] = self.fixed.value()
            d["include_classes"] = checked if self.cls_only.isChecked() else []
            d["exclude_classes"] = [] if self.cls_only.isChecked() else checked
            t = b["tracking"]
            t["enabled"] = self.trk_on.isChecked()
            t["low_thresh"] = round(self.trk_low.value(), 3)
            t["track_buffer_s"] = round(self.trk_buf.value(), 2)
            t["coast_s"] = round(self.trk_coast.value(), 2)
            t["min_hits"] = int(self.trk_hits.value())
            t["smoothing"] = self.trk_smooth.value() / 100
            t["class_aware"] = self.trk_class.isChecked()
        self.ctx.update_active_profile(mutate)

    def refresh_live(self):
        if not self.widget.isVisible():
            return
        res = self.ctx.ai.results.peek()[0]
        s = self.ctx.ai.stats()
        if res is None:
            self.table.setRowCount(0)
            self.live_info.setText(tr("AI state: {state}", state=tr(s.state)))
            return
        self.trk_stats.setText(tr("Tracked {a} · lost (recoverable) {l} · IDs created {c} · recoveries {r} · update {ms} ms",
                                  a=s.tracks_active, l=s.tracks_lost, c=s.track_ids_created, r=s.tracks_recovered,
                                  ms=f"{s.tracker_ms:.2f}"))
        if self.trk_on.isChecked():
            from vcstudio.core.frame import host_time_s
            objs = sorted(self.ctx.ai.tracked_at(host_time_s()), key=lambda o: o.track_id)
            self.live_info.setText(tr("{t} tracked objects · {d} detections ≥ threshold", t=len(objs), d=len(res.detections))
                                   + f" · {res.model} {res.inference_ms:.1f} " + tr("ms"))
            rows = [(f"#{o.track_id}", tr(o.label), tr(o.state), f"{o.confidence:.2f}",
                     f"{o.x0:.0f}, {o.y0:.0f}, {o.x1:.0f}, {o.y1:.0f}", f"{o.x1 - o.x0:.0f}×{o.y1 - o.y0:.0f}",
                     f"{o.age_s:.1f} s") for o in objs]
        else:
            self.live_info.setText(tr("{n} shown of {raw} detected · frame #{seq}", n=len(res.detections),
                                      raw=res.raw_count, seq=res.frame_seq) + f" · {res.model} {res.inference_ms:.1f} " + tr("ms"))
            rows = [("—", tr(d.label), tr("detected"), f"{d.confidence:.2f}",
                     f"{d.x0:.0f}, {d.y0:.0f}, {d.x1:.0f}, {d.y1:.0f}", f"{d.w:.0f}×{d.h:.0f}", "")
                    for d in sorted(res.detections, key=lambda d: -d.confidence)]
        self.table.setRowCount(len(rows))
        for i, vals in enumerate(rows):
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(v))
