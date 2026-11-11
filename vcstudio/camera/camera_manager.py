"""Virtual camera installation status and control (host app + CMIO system extension)."""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vcstudio import CAMERA_NAME
from vcstudio.core.system_info import camera_extensions

log = logging.getLogger(__name__)
EXTENSION_ID = "com.aivcstudio.app.camera"
DEFAULT_HOST_APP = Path("/Applications/AI Virtual Camera Studio.app")
APPROVAL_PANE = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


@dataclass
class CameraStatus:
    host_app: str | None          # path of the signed host app, if found
    extension_state: str          # not_installed | activated_enabled | waiting_for_user | ... (systemextensionsctl)
    extension_enabled: bool
    extension_active: bool
    device_visible: bool          # "AI Virtual Camera" present in AVFoundation's device list
    team_id: str = ""
    message: str = ""

    @property
    def ready(self) -> bool:
        return self.extension_enabled and self.extension_active and self.device_visible


def host_app() -> Path | None:
    env = os.environ.get("VCSTUDIO_HOST_APP")
    for p in ([Path(env)] if env else []) + [DEFAULT_HOST_APP]:
        if (p / "Contents" / "MacOS" / "AIVirtualCameraStudio").exists():
            return p
    return None


def status() -> CameraStatus:
    ext = next((e for e in camera_extensions() if e["bundle_id"] == EXTENSION_ID), None)
    try:
        from vcstudio.core.devices import list_video_devices
        visible = any(d.name == CAMERA_NAME for d in list_video_devices())
    except Exception:
        visible = False
    app = host_app()
    st = CameraStatus(str(app) if app else None, (ext or {}).get("state", "not_installed").replace(" ", "_"),
                      bool(ext and ext["enabled"]), bool(ext and ext["active"]), visible, (ext or {}).get("team_id", ""))
    if st.ready:
        st.message = f"{CAMERA_NAME} is installed and visible to other apps."
    elif ext and not ext["enabled"]:
        st.message = ("Waiting for approval: System Settings > General > Login Items & Extensions > Camera "
                      f"Extensions > enable “{CAMERA_NAME}”.")
    elif ext:
        st.message = f"Extension state: {ext['state']}. A restart may be required."
    elif app is None:
        st.message = ("Not installed. Build and install the signed app with scripts/build_camera.sh "
                      "(needs Xcode and an Apple Developer team; see docs/CAMERA_SETUP.md).")
    else:
        st.message = "Host app found but the camera extension is not activated yet: click Install."
    return st


def _run_host(flag: str, timeout: float = 150) -> tuple[int, str]:
    app = host_app()
    if app is None:
        return 1, "Signed host app not found in /Applications (run scripts/build_camera.sh)."
    try:
        p = subprocess.run([str(app / "Contents" / "MacOS" / "AIVirtualCameraStudio"), flag],
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        log.info("%s -> exit %s: %s", flag, p.returncode, out)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 1, "Timed out waiting for the system extension request."


def activate() -> tuple[int, str]:
    """0 active, 2 needs user approval, 3 needs reboot, other = error."""
    return _run_host("--activate-camera")


def deactivate() -> tuple[int, str]:
    return _run_host("--deactivate-camera")


def open_approval_settings() -> None:
    subprocess.Popen(["open", APPROVAL_PANE])
