from __future__ import annotations

import datetime
import logging
import subprocess

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem

from vcstudio.ui.widgets import Card, page_scaffold
from vcstudio.i18n import tr

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class _Bridge(QObject):
    line = Signal(int, str)


class LogsPage:
    key, title = "logs", "Logs"

    def __init__(self, ctx):
        self.ctx = ctx
        self.widget, lay = page_scaffold("Logs", tr("Live application log. Files: {path}", path=ctx.paths.logs_dir))
        bar = QHBoxLayout()
        self.level = QComboBox()
        self.level.addItems(list(LEVELS))
        self.level.setCurrentText("INFO")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filter text…"))
        self.pause_btn = QPushButton(tr("Pause"))
        self.pause_btn.setCheckable(True)
        clear = QPushButton(tr("Clear view"))
        reveal = QPushButton(tr("Open log folder"))
        for w in (QLabel(tr("Level")), self.level, self.filter, self.pause_btn, clear, reveal):
            bar.addWidget(w)
        lay.addLayout(bar)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        self.view.setStyleSheet("font-family: Menlo, monospace; font-size: 11px;")
        self.view.setMinimumHeight(380)
        lay.addWidget(self.view)
        ev = Card("Recorded events (SQLite)")
        self.events = QTableWidget(0, 4)
        self.events.setHorizontalHeaderLabels([tr("Time"), tr("Level"), tr("Component"), tr("Message")])
        self.events.horizontalHeader().setStretchLastSection(True)
        self.events.setMinimumHeight(200)
        ev.layout_.addWidget(self.events)
        lay.addWidget(ev)
        self._bridge = _Bridge()
        self._bridge.line.connect(self._append)
        self.level.currentTextChanged.connect(self._rebuild)
        self.filter.textChanged.connect(self._rebuild)
        clear.clicked.connect(self.view.clear)
        reveal.clicked.connect(lambda: subprocess.Popen(["open", str(ctx.paths.logs_dir)]))
        if ctx.ring:
            ctx.ring.subscribe(lambda lv, ln: self._bridge.line.emit(lv, ln))
        self._rebuild()
        self.refresh_events()

    def _passes(self, lv: int, ln: str) -> bool:
        f = self.filter.text().strip().lower()
        return lv >= LEVELS[self.level.currentText()] and (not f or f in ln.lower())

    def _append(self, lv: int, ln: str) -> None:
        if self.pause_btn.isChecked() or not self._passes(lv, ln):
            return
        self.view.appendPlainText(ln)
        self.view.moveCursor(QTextCursor.End)

    def _rebuild(self, *_):
        self.view.clear()
        if self.ctx.ring:
            self.view.setPlainText("\n".join(ln for lv, ln in self.ctx.ring.records if self._passes(lv, ln)))
            self.view.moveCursor(QTextCursor.End)

    def refresh_events(self):
        rows = self.ctx.db.recent_events(200)
        self.events.setRowCount(len(rows))
        for i, r in enumerate(rows):
            t = datetime.datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
            for j, v in enumerate((t, r["level"], r["component"], r["message"])):
                self.events.setItem(i, j, QTableWidgetItem(v))
