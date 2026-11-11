from __future__ import annotations

import datetime
from pathlib import Path

from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from vcstudio.core.profiles import ProfileError
from vcstudio.ui.widgets import Card, page_scaffold


class ProfilesPage:
    key, title = "profiles", "Profiles"

    def __init__(self, ctx, parent=None):
        self.ctx, self.parent = ctx, parent
        self.widget, lay = page_scaffold("Profiles", "A profile holds models, detection classes, thresholds, ROIs, "
                                         "replacement assets, mask/playback settings, performance mode and output "
                                         "settings. Switching takes effect immediately, no restart needed.")
        row = QHBoxLayout()
        left = Card("Profiles")
        self.list = QListWidget()
        self.list.setMinimumWidth(240)
        left.layout_.addWidget(self.list)
        btns = QVBoxLayout()
        self.buttons = {}
        for key, label in [("activate", "Activate"), ("new", "New…"), ("rename", "Rename…"),
                           ("duplicate", "Duplicate"), ("delete", "Delete"), ("import", "Import…"),
                           ("export", "Export…")]:
            b = QPushButton(label)
            if key == "activate":
                b.setObjectName("primary")
            if key == "delete":
                b.setObjectName("danger")
            btns.addWidget(b)
            self.buttons[key] = b
        btns.addStretch()
        h = QHBoxLayout()
        h.addLayout(btns)
        left.layout_.addLayout(h)
        row.addWidget(left, 1)
        right = Card("Details")
        self.info = QLabel()
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        self.json = QPlainTextEdit()
        self.json.setReadOnly(True)
        self.json.setMinimumHeight(360)
        right.layout_.addWidget(self.info)
        right.layout_.addWidget(self.json)
        row.addWidget(right, 2)
        lay.addLayout(row)
        b = self.buttons
        b["activate"].clicked.connect(self.activate)
        b["new"].clicked.connect(self.new)
        b["rename"].clicked.connect(self.rename)
        b["duplicate"].clicked.connect(self.duplicate)
        b["delete"].clicked.connect(self.delete)
        b["import"].clicked.connect(self.import_)
        b["export"].clicked.connect(self.export)
        self.list.currentTextChanged.connect(self.show_details)
        ctx.profiles.add_listener(lambda *_: self.reload())
        self.reload()

    def selected(self) -> str | None:
        it = self.list.currentItem()
        return it.data(256) if it else None

    def reload(self) -> None:
        cur = self.selected() or self.ctx.profiles.active_name
        self.list.blockSignals(True)
        self.list.clear()
        for n in self.ctx.profiles.names():
            label = f"{n}   ● active" if n == self.ctx.profiles.active_name else n
            self.list.addItem(label)
            self.list.item(self.list.count() - 1).setData(256, n)
            if n == cur:
                self.list.setCurrentRow(self.list.count() - 1)
        self.list.blockSignals(False)
        self.show_details()

    def show_details(self, *_):
        name = self.selected()
        if not name:
            return
        import json
        doc = self.ctx.profiles.get(name)
        ts = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
        body = doc["body"]
        self.info.setText(f"<b>{name}</b> · created {ts(doc['created'])} · modified {ts(doc['modified'])} · "
                          f"{len(body['rois'])} ROI(s) · mode {body['performance_mode']}")
        self.json.setPlainText(json.dumps(body, indent=2))

    def _guard(self, fn, *a):
        try:
            return fn(*a)
        except (ProfileError, OSError, ValueError) as e:
            QMessageBox.warning(self.widget, "Profiles", str(e))

    def activate(self):
        if n := self.selected():
            self._guard(self.ctx.profiles.switch, n)

    def new(self):
        name, ok = QInputDialog.getText(self.widget, "New profile", "Name:")
        if ok:
            self._guard(self.ctx.profiles.create, name)

    def rename(self):
        if not (n := self.selected()):
            return
        name, ok = QInputDialog.getText(self.widget, "Rename profile", "New name:", text=n)
        if ok:
            self._guard(self.ctx.profiles.rename, n, name)

    def duplicate(self):
        if n := self.selected():
            self._guard(self.ctx.profiles.duplicate, n)

    def delete(self):
        if not (n := self.selected()):
            return
        if QMessageBox.question(self.widget, "Delete profile",
                                f"Delete profile “{n}”? This cannot be undone.") == QMessageBox.Yes:
            self._guard(self.ctx.profiles.delete, n)

    def import_(self, path: str | None = None):
        from vcstudio.core.profile_bundle import import_bundle
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self.widget, "Import profile", str(Path.home()),
                                                  "Profiles (*.vcsprofile *.json)")
        if path:
            fn = import_bundle if path.endswith(".vcsprofile") else self.ctx.profiles.import_file
            return self._guard(fn, *( (self.ctx, Path(path)) if fn is import_bundle else (Path(path),) ))

    def export(self, path: str | None = None):
        from vcstudio.core.profile_bundle import export_bundle
        if not (n := self.selected()):
            return
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self.widget, "Export profile", str(Path.home() / f"{n}.vcsprofile"),
                                                  "Profile with assets (*.vcsprofile);;Profile settings only (*.json)")
        if path:
            if path.endswith(".json"):
                return self._guard(self.ctx.profiles.export, n, Path(path))
            return self._guard(export_bundle, self.ctx, n, Path(path))
