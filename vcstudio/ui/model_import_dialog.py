"""Import your own Core ML model: validate in the background, review classes and threshold, install."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHeaderView,
                               QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout)

from vcstudio.ai import model_import as mi
from vcstudio.i18n import tr


class _Signals(QObject):
    done = Signal(object, object)       # report, error


def inspect_in_background(path: Path, on_done) -> threading.Thread:
    """Run model_import.inspect_model off the UI thread; on_done(report, error) is called on the UI thread."""
    sig = _Signals()
    sig.done.connect(on_done)

    def work():
        import objc  # type: ignore
        with objc.autorelease_pool():
            try:
                rep = mi.inspect_model(path)
                sig.done.emit(rep, None)
            except Exception as e:  # noqa: BLE001 - shown to the user
                sig.done.emit(None, e)
    t = threading.Thread(target=work, name="model-import", daemon=True)
    t._sig = sig                 # keep the QObject alive with the thread
    t.start()
    return t


class ModelImportDialog(QDialog):
    """Review a validated model before installing it."""

    def __init__(self, report: mi.ModelReport, builtin_names: set[str], parent=None):
        super().__init__(parent)
        self.report, self.builtin_names = report, builtin_names
        self.setWindowTitle(tr("Import model"))
        self.resize(560, 620)
        lay = QVBoxLayout(self)
        task = tr("object detector") if report.task == "detect" else tr("segmentation (object masks)")
        info = QLabel(tr("{file}: {task}, input {size}×{size}, {n} classes. On this Mac: {med} ms per frame "
                         "(p95 {p95} ms). Test run on the sample photo found {dets} object(s){labels}.",
                         file=Path(report.source).name, task=task, size=report.imgsz, n=len(report.classes),
                         med=report.median_ms, p95=report.p95_ms, dets=report.sample_detections,
                         labels=(": " + ", ".join(report.sample_labels[:6])) if report.sample_labels else ""))
        info.setWordWrap(True)
        lay.addWidget(info)
        if report.median_ms > 33:
            slow = QLabel(tr("Slower than 33 ms: detection will run below 30 times a second on this Mac."))
            slow.setWordWrap(True); slow.setStyleSheet("color: #d29922;")
            lay.addWidget(slow)
        f = QFormLayout()
        self.name = QLineEdit(report.suggested_name)
        self.conf = QDoubleSpinBox(); self.conf.setRange(0.01, 0.99); self.conf.setSingleStep(0.05); self.conf.setDecimals(2)
        self.conf.setValue(min(0.99, max(0.01, report.suggested_confidence)))
        self.use_now = QCheckBox(tr("Use this model now in the active profile"))
        self.use_now.setChecked(report.task == "detect")
        self.use_now.setEnabled(report.task == "detect")
        f.addRow(tr("Name"), self.name)
        f.addRow(tr("Confidence threshold"), self.conf)
        f.addRow(tr(""), self.use_now)
        lay.addLayout(f)
        lay.addWidget(QLabel(tr("Classes (rename to map them to names you use in regions):")))
        self.classes = QTableWidget(len(report.classes), 2)
        self.classes.setHorizontalHeaderLabels([tr("Model's name"), tr("Name in the app")])
        self.classes.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for i, c in enumerate(report.classes):
            a = QTableWidgetItem(c); a.setFlags(a.flags() & ~Qt.ItemIsEditable)
            self.classes.setItem(i, 0, a)
            self.classes.setItem(i, 1, QTableWidgetItem(c))
        lay.addWidget(self.classes, 1)
        self.error = QLabel(); self.error.setWordWrap(True); self.error.setStyleSheet("color: #f85149;")
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("Install the model"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.installed = None

    def class_map(self) -> dict[str, str]:
        out = {}
        for i in range(self.classes.rowCount()):
            src, new = self.classes.item(i, 0).text(), self.classes.item(i, 1).text().strip()
            if new and new != src:
                out[src] = new
        return out

    def install_into(self, models_dir: Path):
        return mi.install(self.report, models_dir, self.name.text(), self.class_map(), self.conf.value(),
                          self.builtin_names)

    def accept(self):  # noqa: D401 - Qt override
        mapped = [self.class_map().get(c, c) for c in self.report.classes]
        dupes = sorted({m for m in mapped if mapped.count(m) > 1})
        if dupes:
            self.error.setText(tr("Two classes would get the same name: {names}", names=", ".join(dupes)))
            return
        self.models_dir = getattr(self, "models_dir", None)
        if self.models_dir is not None:
            try:
                self.installed = self.install_into(self.models_dir)
            except mi.ModelImportError as e:
                self.error.setText(tr(str(e)))
                return
        super().accept()
