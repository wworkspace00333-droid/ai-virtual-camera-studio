"""Reusable widgets."""
from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from vcstudio.i18n import tr

from . import theme


def page_scaffold(title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
    """Returns (page widget, content layout) with a header and scrolling body."""
    page = QWidget()
    page.setObjectName("page")
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    body = QWidget()
    body.setObjectName("page")
    lay = QVBoxLayout(body)
    lay.setContentsMargins(28, 24, 28, 24)
    lay.setSpacing(16)
    h = QLabel(tr(title))
    h.setObjectName("h1")
    lay.addWidget(h)
    if subtitle:
        s = QLabel(tr(subtitle))
        s.setObjectName("muted")
        s.setWordWrap(True)
        lay.addWidget(s)
    scroll.setWidget(body)
    outer.addWidget(scroll)
    return page, lay


class Card(QFrame):
    def __init__(self, title: str | None = None):
        super().__init__()
        self.setObjectName("card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(16, 14, 16, 14)
        self.layout_.setSpacing(8)
        if title:
            t = QLabel(tr(title))
            t.setObjectName("h2")
            self.layout_.addWidget(t)


class StatCard(QFrame):
    """Label / big value / detail line, with a status dot."""

    def __init__(self, label: str):
        super().__init__()
        self.setObjectName("card")
        self.setMinimumWidth(180)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        top = QHBoxLayout()
        self._dot = QLabel("●")
        self._label = QLabel(tr(label))
        self._label.setObjectName("muted")
        top.addWidget(self._label)
        top.addStretch()
        top.addWidget(self._dot)
        lay.addLayout(top)
        self.value = QLabel("—")
        self.value.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.detail = QLabel("")
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        lay.addWidget(self.value)
        lay.addWidget(self.detail)
        self.set_state("idle")

    def set(self, value: str, detail: str = "", state: str | None = None) -> None:
        # exact-match strings ("Running", "Not installed", ...) are translated here once for every card
        self.value.setText(tr(value))
        self.detail.setText(tr(detail))
        if state:
            self.set_state(state)

    def set_state(self, state: str) -> None:
        color = {"ok": theme.OK, "warn": theme.WARN, "error": theme.ERR}.get(state, theme.MUTED)
        self._dot.setStyleSheet(f"color: {color}; font-size: 14px;")
        self.setProperty("state", state)


class StagePlaceholder(QFrame):
    """Honest marker for functionality scheduled in a later stage."""

    def __init__(self, stage: int, feature: str, details: list[str]):
        super().__init__()
        self.setObjectName("placeholder")
        self.stage = stage
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        t = QLabel(f"Not implemented yet — scheduled for Stage {stage}")
        t.setStyleSheet(f"color: {theme.WARN}; font-weight: 600;")
        lay.addWidget(t)
        f = QLabel(feature)
        f.setObjectName("h2")
        f.setWordWrap(True)
        lay.addWidget(f)
        for d in details:
            l = QLabel("• " + d)
            l.setObjectName("muted")
            l.setWordWrap(True)
            lay.addWidget(l)


class Sparkline(QWidget):
    """Minimal live line chart (0..max_value)."""

    def __init__(self, label: str, max_value: float = 100.0, capacity: int = 120,
                 color: str = theme.ACCENT, unit: str = "%"):
        super().__init__()
        self.label, self.max_value, self.color, self.unit = tr(label), max_value, QColor(color), unit
        self.values: deque[float] = deque(maxlen=capacity)
        self.setMinimumHeight(90)

    def push(self, v: float | None) -> None:
        if v is not None:
            self.values.append(float(v))
            self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 18, -1, -1)
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(QColor(theme.PANEL_2))
        p.drawRoundedRect(r, 6, 6)
        p.setPen(QColor(theme.MUTED))
        cur = f"{self.values[-1]:.1f}{self.unit}" if self.values else "—"
        p.drawText(self.rect().adjusted(2, 0, -2, 0), Qt.AlignLeft | Qt.AlignTop, self.label)
        p.drawText(self.rect().adjusted(2, 0, -2, 0), Qt.AlignRight | Qt.AlignTop, cur)
        n = self.values.maxlen or 1
        if len(self.values) < 2:
            return
        path = QPainterPath()
        for i, v in enumerate(self.values):
            x = r.left() + r.width() * (i + n - len(self.values)) / (n - 1)
            y = r.bottom() - r.height() * min(max(v / self.max_value, 0), 1)
            path.lineTo(QPointF(x, y)) if i else path.moveTo(QPointF(x, y))
        p.setPen(QPen(self.color, 2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {tr(unit)}" if unit != "B" else f"{int(n)} {tr('B')}"
        n /= 1024
    return str(n)
