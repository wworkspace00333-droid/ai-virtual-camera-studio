"""Filesystem locations. Everything is local; override root with VCSTUDIO_HOME."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_root() -> Path:
    env = os.environ.get("VCSTUDIO_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "AIVirtualCameraStudio"


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def database_file(self) -> Path:
        return self.root / "studio.db"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    def ensure(self) -> "AppPaths":
        for p in (self.root, self.profiles_dir, self.logs_dir, self.models_dir, self.assets_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls) -> "AppPaths":
        return cls(_default_root())
