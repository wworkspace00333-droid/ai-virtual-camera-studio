"""Application-wide settings (settings.json).

Settings are deep-merged over DEFAULT_SETTINGS on load, validated, and written atomically.
Profile-specific configuration lives in profiles (see profiles.py), not here.
"""
from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .jsonio import atomic_write_json, deep_merge, read_json
from .paths import AppPaths

log = logging.getLogger(__name__)

SETTINGS_VERSION = 1


@dataclass(frozen=True)
class PerformanceMode:
    key: str
    label: str
    ai_fps: float | None  # None => adaptive (max sustainable)
    output_fps: int
    description: str


PERFORMANCE_MODES: dict[str, PerformanceMode] = {
    m.key: m
    for m in (
        PerformanceMode("performance", "Performance", 30, 60, "AI inference target 30 FPS, output 60 FPS."),
        PerformanceMode("balanced", "Balanced", 20, 60, "AI inference target 20 FPS, output 60 FPS."),
        PerformanceMode("low_resource", "Low Resource", 12, 60, "AI inference target 10–15 FPS, output 60 FPS."),
        PerformanceMode(
            "max_quality",
            "Maximum Quality",
            None,
            60,
            "Larger model input / highest sustainable inference rate, measured at runtime.",
        ),
    )
}

# Action id -> default key sequence (Qt portable text). Actions are defined in ui/actions.py.
DEFAULT_HOTKEYS: dict[str, str] = {
    "start_processing": "Ctrl+Alt+S",
    "stop_processing": "Ctrl+Alt+X",
    "toggle_ai": "Ctrl+Alt+A",
    "toggle_replacement": "Ctrl+Alt+R",
    "emergency_passthrough": "Ctrl+Alt+P",
    "next_profile": "Ctrl+Alt+N",
    "toggle_preview": "Ctrl+Alt+V",
    "card_next": "Ctrl+Alt+Right",
    "card_prev": "Ctrl+Alt+Left",
    "card_toggle": "Ctrl+Alt+C",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": SETTINGS_VERSION,
    "active_profile": "Default",
    "input": {
        "method": "avfoundation",  # avfoundation | test_pattern | obs_ipc
        "device_unique_id": None,
        "device_name_hint": "OBS Virtual Camera",
        "preferred_width": 1920,
        "preferred_height": 1080,
        "preferred_fps": 60,
        "auto_reconnect": True,
        "autostart": True,
        "frozen_frame_timeout_ms": 2000,
        "missing_input_timeout_ms": 1500,
    },
    "output": {
        "width": 1920,
        "height": 1080,
        "fps": 60,
        "pixel_format": "BGRA",  # BGRA | NV12
        "camera_enabled": True,
        "camera_device": "AI Virtual Camera",          # advanced: CMIO device that receives frames
        "camera_sink": "AI Virtual Camera Input",
    },
    "performance": {
        "mode": "balanced",
        "memory_limits": "auto",     # auto: thresholds scale with installed RAM | manual: use memory_warning_mb
        "memory_warning_mb": 3072,   # manual mode only; critical (load shedding) is 2x
        "monitor_interval_ms": 1000,
    },
    "ui": {
        "last_page": "dashboard",
        "window_geometry": None,
        "show_preview": True,
        "preview_max_fps": 30,
        "global_hotkeys": True,
        "language": "auto",          # auto (macOS preferred language) | en | ru; applied at next start
    },
    "processing": {"compositor": "auto"},     # auto (Metal if available) | metal | cpu
    "hotkeys": dict(DEFAULT_HOTKEYS),
    "logging": {"level": "INFO", "max_bytes": 5_000_000, "backup_count": 5},
}

_ALLOWED = {
    ("input", "method"): {"avfoundation", "test_pattern", "obs_ipc"},
    ("output", "pixel_format"): {"BGRA", "NV12"},
    ("performance", "mode"): set(PERFORMANCE_MODES),
    ("performance", "memory_limits"): {"auto", "manual"},
    ("logging", "level"): {"DEBUG", "INFO", "WARNING", "ERROR"},
    ("processing", "compositor"): {"auto", "metal", "cpu"},
    ("ui", "language"): {"auto", "en", "ru"},
}
_RANGES = {
    ("input", "preferred_fps"): (1, 240),
    ("output", "fps"): (1, 240),
    ("output", "width"): (160, 7680),
    ("output", "height"): (120, 4320),
    ("input", "frozen_frame_timeout_ms"): (100, 60000),
    ("input", "missing_input_timeout_ms"): (100, 60000),
    ("ui", "preview_max_fps"): (1, 60),
    ("performance", "memory_warning_mb"): (256, 262144),
}


class SettingsError(ValueError):
    pass


def validate_settings(data: dict) -> list[str]:
    """Return a list of problems; empty means valid."""
    problems: list[str] = []
    for (sec, key), allowed in _ALLOWED.items():
        v = data.get(sec, {}).get(key)
        if v not in allowed:
            problems.append(f"{sec}.{key}={v!r} not in {sorted(allowed)}")
    for (sec, key), (lo, hi) in _RANGES.items():
        v = data.get(sec, {}).get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            problems.append(f"{sec}.{key}={v!r} outside [{lo}, {hi}]")
    hk = data.get("hotkeys", {})
    if not isinstance(hk, dict) or any(not isinstance(v, str) for v in hk.values()):
        problems.append("hotkeys must map action id -> key sequence string")
    return problems


class Settings:
    """Thread-safe settings store with change listeners."""

    def __init__(self, paths: AppPaths):
        self._paths = paths
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self._listeners: list[Callable[[str, Any], None]] = []

    # ---- persistence ----
    def load(self) -> list[str]:
        """Load from disk. Invalid or corrupt files fall back to defaults (backup kept)."""
        path = self._paths.settings_file
        problems: list[str] = []
        with self._lock:
            if not path.exists():
                self._data = copy.deepcopy(DEFAULT_SETTINGS)
                self.save()
                return problems
            try:
                raw = read_json(path)
                if not isinstance(raw, dict):
                    raise SettingsError("settings root is not an object")
                merged = deep_merge(DEFAULT_SETTINGS, raw)
                problems = validate_settings(merged)
                if problems:
                    # Repair only the offending keys back to defaults.
                    for p in problems:
                        dotted = p.split("=")[0].split(" ")[0]
                        if "." in dotted:
                            sec, key = dotted.split(".", 1)
                            merged[sec][key] = copy.deepcopy(DEFAULT_SETTINGS[sec][key])
                        elif dotted == "hotkeys":
                            merged["hotkeys"] = dict(DEFAULT_HOTKEYS)
                    log.warning("Settings repaired: %s", "; ".join(problems))
                self._data = merged
            except (ValueError, OSError) as e:
                backup = path.with_suffix(".corrupt.json")
                try:
                    path.replace(backup)
                except OSError:
                    pass
                log.error("Settings file unreadable (%s); reset to defaults, backup at %s", e, backup)
                problems.append(f"corrupt settings: {e}")
                self._data = copy.deepcopy(DEFAULT_SETTINGS)
            self.save()
        return problems

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self._paths.settings_file, self._data)

    # ---- access ----
    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def set(self, dotted: str, value: Any, *, persist: bool = True) -> None:
        with self._lock:
            parts = dotted.split(".")
            candidate = copy.deepcopy(self._data)
            node = candidate
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            problems = validate_settings(candidate)
            if problems:
                raise SettingsError("; ".join(problems))
            self._data = candidate
            if persist:
                self.save()
        for cb in list(self._listeners):
            try:
                cb(dotted, value)
            except Exception:  # listener bugs must not break settings
                log.exception("settings listener failed")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def reset_hotkeys(self) -> None:
        self.set("hotkeys", dict(DEFAULT_HOTKEYS))

    def add_listener(self, cb: Callable[[str, Any], None]) -> None:
        self._listeners.append(cb)

    @property
    def performance_mode(self) -> PerformanceMode:
        return PERFORMANCE_MODES[self.get("performance.mode")]
