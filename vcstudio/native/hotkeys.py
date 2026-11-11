"""Global (system-wide) hotkeys through libVCSNative's Carbon RegisterEventHotKey wrapper.

Qt key sequences (portable text, e.g. "Ctrl+Alt+P") are mapped to macOS virtual key codes and Carbon
modifier masks. On macOS, Qt's "Ctrl" is the Command key and "Meta" is the Control key.
"""
from __future__ import annotations

import ctypes
import logging
from typing import Callable

log = logging.getLogger(__name__)

# Carbon modifier masks
CMD, SHIFT, OPT, CTRL = 0x0100, 0x0200, 0x0800, 0x1000
QT_MODS = {"ctrl": CMD, "meta": CTRL, "alt": OPT, "shift": SHIFT}

# macOS virtual key codes (ANSI layout)
KEYCODES = {
    **{c: k for c, k in zip("ASDFHGZXCVBQWERYT123465=9780]OU[IP", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17,
                                                                   18, 19, 20, 21, 22, 23, 24, 25, 26, 28, 29, 30, 31, 32, 33, 34, 35])},
    "L": 37, "J": 38, "'": 39, "K": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "N": 45, "M": 46, ".": 47, "`": 50,
    "-": 27, "SPACE": 49, "RETURN": 36, "TAB": 48, "ESC": 53, "BACKSPACE": 51, "DEL": 117,
    "LEFT": 123, "RIGHT": 124, "DOWN": 125, "UP": 126, "HOME": 115, "END": 119, "PGUP": 116, "PGDOWN": 121,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97, "F7": 98, "F8": 100, "F9": 101, "F10": 109,
    "F11": 103, "F12": 111, "F13": 105, "F14": 107, "F15": 113,
}


def parse(seq: str) -> tuple[int, int] | None:
    """Qt portable key sequence -> (keycode, carbon modifiers); None if unmappable/empty."""
    if not seq:
        return None
    parts = seq.split("+")
    key = parts[-1].strip().upper() or "+"
    mods = 0
    for p in parts[:-1]:
        m = QT_MODS.get(p.strip().lower())
        if m is None:
            return None
        mods |= m
    code = KEYCODES.get(key)
    if code is None or mods == 0:          # require a modifier: never steal plain keys system-wide
        return None
    return code, mods


class GlobalHotkeys:
    def __init__(self):
        from vcstudio.native import metal
        self.lib = metal._load()
        self.available = self.lib is not None
        self._cb_ref = None
        self._ids: dict[int, str] = {}
        self.errors: dict[str, str] = {}
        if self.available:
            self.lib.vcs_hotkey_register.argtypes = [ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32]
            self.lib.vcs_hotkey_register.restype = ctypes.c_int32
            self.lib.vcs_hotkey_count.restype = ctypes.c_int32

    def apply(self, bindings: dict[str, str], on_action: Callable[[str], None]) -> dict[str, str]:
        """Register all bindings; returns {action: error} for those that could not be registered."""
        self.errors = {}
        if not self.available:
            return {a: "native library unavailable" for a in bindings}
        CB = ctypes.CFUNCTYPE(None, ctypes.c_int32)

        def fire(hid: int) -> None:
            action = self._ids.get(int(hid))
            if action:
                try:
                    on_action(action)
                except Exception:
                    log.exception("global hotkey handler failed")
        self._cb_ref = CB(fire)                        # keep a reference: ctypes callbacks must outlive use
        self.lib.vcs_hotkeys_set_callback(self._cb_ref)
        self.lib.vcs_hotkey_unregister_all()
        self._ids = {}
        for i, (action, seq) in enumerate(sorted(bindings.items()), start=1):
            if not seq:
                continue
            p = parse(seq)
            if p is None:
                self.errors[action] = f"'{seq}' cannot be a global hotkey (needs a modifier and a standard key)"
                continue
            rc = self.lib.vcs_hotkey_register(i, p[0], p[1])
            if rc != 0:
                self.errors[action] = f"'{seq}' is already used by the system or another app (OSStatus {rc})"
                continue
            self._ids[i] = action
        log.info("Global hotkeys registered: %d (%d errors)", len(self._ids), len(self.errors))
        return self.errors

    def clear(self) -> None:
        if self.available:
            self.lib.vcs_hotkey_unregister_all()
        self._ids = {}

    @property
    def count(self) -> int:
        return int(self.lib.vcs_hotkey_count()) if self.available else 0
