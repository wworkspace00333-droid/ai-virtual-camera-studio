"""Profile management: create / rename / duplicate / delete / export / import / switch.

Each profile is one JSON file in <root>/profiles/. File names are derived from a slug of the
profile name; the authoritative name is stored inside the file.
"""
from __future__ import annotations

import copy
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .jsonio import atomic_write_json, deep_merge, read_json
from .paths import AppPaths

log = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 1
PROFILE_EXPORT_KIND = "aivcstudio.profile"
DEFAULT_PROFILE_NAME = "Default"
_NAME_RE = re.compile(r"^[^/\\:\x00-\x1f]{1,64}$")


def default_profile_body() -> dict[str, Any]:
    """The full profile structure. Later stages read/write these sections."""
    return {
        "ai": {
            "detector_model": "auto",           # auto => smallest model (largest in Maximum Quality mode)
            "face_detection": None,             # "vision" => Apple Vision face rectangles (detection only)
            "segmentation_model": None,         # optional, loaded only if a ROI needs masks
            "compute_units": "all",             # all | cpu_and_ne | cpu_and_gpu | cpu_only
            "enabled": True,
        },
        "detection": {
            "confidence": 0.35,
            "iou": 0.5,
            "include_classes": [],               # empty => all classes
            "exclude_classes": [],
            "frequency_mode": "auto",           # auto => from performance mode, or fixed fps
            "fixed_fps": 15,
            "smoothing": 0.6,
            "lost_track_buffer_frames": 30,
        },
        "tracking": {
            "enabled": True,
            "low_thresh": 0.1,          # ByteTrack second-stage detections (high = detection.confidence)
            "match_iou": 0.2,
            "track_buffer_s": 1.5,      # lost-object recovery window (same ID)
            "coast_s": 0.4,             # lost objects stay visible (predicted) this long
            "max_extrapolate_s": 0.3,
            "smoothing": 0.5,
            "class_aware": True,
        },
        "rois": [],                              # list of ROI dicts (schema in roi/roi_manager, Stage 5)
        "assets": [],                            # replacement asset registry: {id, path, kind}
        "masks": {"feather_px": 4, "refine_edges": True, "threshold": 0.5},
        "playback": {"loop": True, "start_offset_s": 0.0, "sync": "wallclock"},
        "performance_mode": "balanced",
        "output": {"width": 1920, "height": 1080, "fps": 60, "pixel_format": "BGRA"},
    }


class ProfileError(ValueError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.").lower()
    return s or "profile"


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ProfileError("Profile name must be 1–64 characters and must not contain / \\ : or control characters")
    return name


def _validate_profile_doc(doc: Any) -> dict:
    if not isinstance(doc, dict):
        raise ProfileError("Profile must be a JSON object")
    if not isinstance(doc.get("name"), str):
        raise ProfileError("Profile is missing 'name'")
    body = doc.get("body")
    if not isinstance(body, dict):
        raise ProfileError("Profile is missing 'body'")
    if not isinstance(body.get("rois", []), list):
        raise ProfileError("'rois' must be a list")
    ver = doc.get("schema_version", 1)
    if not isinstance(ver, int) or ver > PROFILE_SCHEMA_VERSION:
        raise ProfileError(f"Unsupported profile schema version {ver!r}")
    return doc


class ProfileManager:
    def __init__(self, paths: AppPaths):
        self._dir = paths.profiles_dir
        self._lock = threading.RLock()
        self._profiles: dict[str, dict] = {}   # name -> doc
        self._files: dict[str, Path] = {}      # name -> path
        self._active: str | None = None
        self._listeners: list[Callable[[str, str], None]] = []  # (event, name)

    # ---------- loading ----------
    def load_all(self) -> None:
        with self._lock:
            self._profiles.clear()
            self._files.clear()
            self._dir.mkdir(parents=True, exist_ok=True)
            for f in sorted(self._dir.glob("*.json")):
                try:
                    doc = _validate_profile_doc(read_json(f))
                    doc["body"] = deep_merge(default_profile_body(), doc["body"])
                    if doc["name"] in self._profiles:
                        log.warning("Duplicate profile name %r in %s; skipped", doc["name"], f)
                        continue
                    self._profiles[doc["name"]] = doc
                    self._files[doc["name"]] = f
                except (ValueError, OSError) as e:
                    log.error("Skipping invalid profile %s: %s", f.name, e)
            if not self._profiles:
                self.create(DEFAULT_PROFILE_NAME)

    # ---------- queries ----------
    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._profiles, key=str.casefold)

    def get(self, name: str) -> dict:
        with self._lock:
            if name not in self._profiles:
                raise ProfileError(f"No profile named {name!r}")
            return copy.deepcopy(self._profiles[name])

    @property
    def active_name(self) -> str | None:
        return self._active

    def active(self) -> dict:
        if self._active is None:
            raise ProfileError("No active profile")
        return self.get(self._active)

    # ---------- mutations ----------
    def _unique_path(self, name: str) -> Path:
        base = slugify(name)
        p = self._dir / f"{base}.json"
        i = 2
        used = set(self._files.values())
        while p in used or (p.exists() and p not in used):
            p = self._dir / f"{base}-{i}.json"
            i += 1
        return p

    def _write(self, name: str) -> None:
        doc = self._profiles[name]
        doc["modified"] = time.time()
        atomic_write_json(self._files[name], doc)

    def create(self, name: str, body: dict | None = None) -> dict:
        name = validate_name(name)
        with self._lock:
            if name in self._profiles:
                raise ProfileError(f"Profile {name!r} already exists")
            now = time.time()
            doc = {
                "kind": PROFILE_EXPORT_KIND,
                "schema_version": PROFILE_SCHEMA_VERSION,
                "id": uuid.uuid4().hex,
                "name": name,
                "created": now,
                "modified": now,
                "body": deep_merge(default_profile_body(), body or {}),
            }
            self._profiles[name] = doc
            self._files[name] = self._unique_path(name)
            self._write(name)
        self._emit("created", name)
        return copy.deepcopy(doc)

    def update_body(self, name: str, body: dict) -> None:
        with self._lock:
            if name not in self._profiles:
                raise ProfileError(f"No profile named {name!r}")
            self._profiles[name]["body"] = deep_merge(default_profile_body(), body)
            self._write(name)
        self._emit("updated", name)

    def rename(self, old: str, new: str) -> None:
        new = validate_name(new)
        with self._lock:
            if old not in self._profiles:
                raise ProfileError(f"No profile named {old!r}")
            if new == old:
                return
            if new in self._profiles:
                raise ProfileError(f"Profile {new!r} already exists")
            doc = self._profiles.pop(old)
            old_path = self._files.pop(old)
            doc["name"] = new
            self._profiles[new] = doc
            self._files[new] = self._unique_path(new)
            self._write(new)
            if old_path != self._files[new]:
                old_path.unlink(missing_ok=True)
            if self._active == old:
                self._active = new
        self._emit("renamed", new)

    def duplicate(self, name: str, new_name: str | None = None) -> dict:
        with self._lock:
            src = self.get(name)
            if new_name is None:
                i = 1
                new_name = f"{name} copy"
                while new_name in self._profiles:
                    i += 1
                    new_name = f"{name} copy {i}"
            return self.create(new_name, src["body"])

    def delete(self, name: str) -> None:
        with self._lock:
            if name not in self._profiles:
                raise ProfileError(f"No profile named {name!r}")
            if len(self._profiles) == 1:
                raise ProfileError("Cannot delete the last remaining profile")
            del self._profiles[name]
            self._files.pop(name).unlink(missing_ok=True)
            if self._active == name:
                self._active = None
        self._emit("deleted", name)
        if self._active is None:
            self.switch(self.names()[0])

    def switch(self, name: str) -> dict:
        with self._lock:
            if name not in self._profiles:
                raise ProfileError(f"No profile named {name!r}")
            self._active = name
            doc = copy.deepcopy(self._profiles[name])
        self._emit("switched", name)
        return doc

    def next_name(self) -> str:
        names = self.names()
        if self._active not in names:
            return names[0]
        return names[(names.index(self._active) + 1) % len(names)]

    # ---------- import / export ----------
    def export(self, name: str, dest: Path) -> Path:
        doc = self.get(name)
        doc["kind"] = PROFILE_EXPORT_KIND
        atomic_write_json(Path(dest), doc)
        return Path(dest)

    def import_file(self, src: Path, *, rename_on_conflict: bool = True) -> dict:
        doc = _validate_profile_doc(read_json(Path(src)))
        if doc.get("kind", PROFILE_EXPORT_KIND) != PROFILE_EXPORT_KIND:
            raise ProfileError("File is not an AI Virtual Camera Studio profile")
        name = validate_name(doc["name"])
        with self._lock:
            if name in self._profiles:
                if not rename_on_conflict:
                    raise ProfileError(f"Profile {name!r} already exists")
                i = 2
                base = name
                while name in self._profiles:
                    name = f"{base} ({i})"
                    i += 1
            return self.create(name, doc["body"])

    # ---------- events ----------
    def add_listener(self, cb: Callable[[str, str], None]) -> None:
        self._listeners.append(cb)

    def _emit(self, event: str, name: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(event, name)
            except Exception:
                log.exception("profile listener failed")
