"""Hotkey-bindable actions. Pipeline actions stay disabled until their stage exists."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence

from vcstudio.i18n import tr

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    available_in_stage: int  # stage in which the underlying feature exists


ACTION_SPECS: list[ActionSpec] = [
    ActionSpec("start_processing", "Start processing", 1),
    ActionSpec("stop_processing", "Stop processing", 1),
    ActionSpec("toggle_ai", "Toggle AI", 1),
    ActionSpec("toggle_replacement", "Toggle replacement", 1),
    ActionSpec("emergency_passthrough", "Emergency passthrough", 1),
    ActionSpec("next_profile", "Switch to next profile", 1),
    ActionSpec("toggle_preview", "Show / hide preview", 1),
    ActionSpec("card_next", "Game card: next", 1),
    ActionSpec("card_prev", "Game card: previous", 1),
    ActionSpec("card_toggle", "Game card: show / hide", 1),
]
CURRENT_STAGE = 10


class ActionRegistry(QObject):
    triggered = Signal(str)

    def __init__(self, parent_widget):
        super().__init__(parent_widget)
        self.actions: dict[str, QAction] = {}
        for spec in ACTION_SPECS:
            a = QAction(tr(spec.label), parent_widget)
            a.setShortcutContext(Qt.ApplicationShortcut)
            a.setEnabled(spec.available_in_stage <= CURRENT_STAGE)
            if not a.isEnabled():
                a.setToolTip(f"Available from Stage {spec.available_in_stage}")
            a.triggered.connect(lambda _=False, i=spec.id: self.triggered.emit(i))
            parent_widget.addAction(a)
            self.actions[spec.id] = a

    def apply_bindings(self, bindings: dict[str, str]) -> list[str]:
        """Apply key bindings; returns list of conflict descriptions (conflicting ones are cleared)."""
        seen: dict[str, str] = {}
        conflicts = []
        for aid, act in self.actions.items():
            seq = QKeySequence(bindings.get(aid, ""))
            key = seq.toString(QKeySequence.PortableText)
            if key and key in seen:
                conflicts.append(f"{key}: {seen[key]} / {aid}")
                act.setShortcut(QKeySequence())
                continue
            if key:
                seen[key] = aid
            act.setShortcut(seq)
        if conflicts:
            log.warning("Hotkey conflicts: %s", "; ".join(conflicts))
        return conflicts
