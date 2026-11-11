"""Locate / install / inspect the vcstudio-obs OBS plugin (input method 2)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PLUGIN_NAME = "vcstudio-obs.plugin"
OBS_APP = Path("/Applications/OBS.app")
INSTALL_DIR = Path.home() / "Library" / "Application Support" / "obs-studio" / "plugins"


def built_plugin() -> Path | None:
    from vcstudio.core.resources import obs_plugin_bundle
    p = obs_plugin_bundle()
    return p if (p / "Contents" / "MacOS" / "vcstudio-obs").exists() else None


def installed_plugin() -> Path | None:
    p = INSTALL_DIR / PLUGIN_NAME
    return p if (p / "Contents" / "MacOS" / "vcstudio-obs").exists() else None


def obs_version() -> str | None:
    try:
        out = subprocess.run(["defaults", "read", str(OBS_APP / "Contents" / "Info.plist"), "CFBundleShortVersionString"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def obs_running() -> bool:
    return subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0


def install() -> str:
    """Copy the plugin into OBS's per-user plugin folder. Returns a user-facing message."""
    src = built_plugin()
    if src is None:
        raise RuntimeError("Plugin not built: run scripts/build_obs_plugin.sh")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    dest = INSTALL_DIR / PLUGIN_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    msg = f"Installed to {dest}."
    msg += (" Restart OBS, then enable Tools > AI Virtual Camera Studio: share frames." if obs_running()
            else " Start OBS, then enable Tools > AI Virtual Camera Studio: share frames.")
    return msg


def uninstall() -> None:
    dest = INSTALL_DIR / PLUGIN_NAME
    if dest.exists():
        shutil.rmtree(dest)
