"""Virtual camera: status reporting, sink client errors, and live loopback through a real CMIO extension.

Live tests (VCS_LIVE=1) run through the dev app bundle because camera access needs its TCC identity:
  - loopback via OBS Virtual Camera's sink (proves the CMIO sink transport on this Mac)
  - loopback via AI Virtual Camera (requires the signed extension installed; skipped otherwise)
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from vcstudio.camera import camera_manager as cm
from vcstudio.camera.output import CameraOutputError, VirtualCameraOutput, list_cmio_devices
from vcstudio.native import metal

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "build" / "AI Virtual Camera Studio Dev.app"
needs_native = pytest.mark.skipif(not metal.available(), reason="libVCSNative not built")
live = pytest.mark.skipif(os.environ.get("VCS_LIVE") != "1", reason="set VCS_LIVE=1")


def test_status_is_honest_when_not_installed(monkeypatch):
    monkeypatch.setattr(cm, "camera_extensions", lambda: [])
    monkeypatch.setattr(cm, "host_app", lambda: None)
    s = cm.status()
    assert s.extension_state == "not_installed" and not s.ready
    assert "scripts/build_camera.sh" in s.message


def test_status_waiting_for_approval(monkeypatch):
    monkeypatch.setattr(cm, "camera_extensions", lambda: [{"enabled": False, "active": False, "team_id": "T",
                                                           "bundle_id": cm.EXTENSION_ID, "name": "AI Virtual Camera",
                                                           "state": "activated waiting for user"}])
    s = cm.status()
    assert not s.ready and "Camera Extensions" in s.message


def test_activate_without_host_app(monkeypatch):
    monkeypatch.setattr(cm, "host_app", lambda: None)
    rc, msg = cm.activate()
    assert rc == 1 and "build_camera.sh" in msg


@needs_native
def test_open_missing_camera_reports_clear_error():
    out = VirtualCameraOutput("No Such Camera", "No Such Sink")
    with pytest.raises(CameraOutputError) as e:
        out.open()
    assert e.value.code == -1 and "not installed" in str(e.value)
    assert isinstance(list_cmio_devices(), list)


def _loopback(device, sink, tmp_path):
    if not APP.exists():
        subprocess.run([str(ROOT / "scripts" / "build_dev_app.sh")], check=True)
    out = tmp_path / "loop.json"
    subprocess.run(["open", "-W", "-n", str(APP), "--args", "--camera-loopback-test", str(out), "--seconds", "5",
                    "--device", device, "--sink", sink], check=True, timeout=180)
    r = json.loads(out.read_text())
    print({k: r.get(k) for k in ("sent", "distinct_received", "latency_ms_median", "latency_ms_p95", "order_ok", "error")})
    return r


@live
@needs_native
def test_cmio_sink_transport_via_obs_extension(tmp_path):
    if "OBS Virtual Camera" not in list_cmio_devices():
        pytest.skip("OBS Virtual Camera not installed")
    r = _loopback("OBS Virtual Camera", "OBS Camera Extension Stream Sink", tmp_path)
    assert r["ok"], r.get("error")
    assert r["distinct_received"] >= 0.9 * r["sent"] and r["order_ok"]
    assert r["latency_ms_median"] < 33


@live
@needs_native
def test_ai_virtual_camera_end_to_end(tmp_path):
    if not cm.status().device_visible:
        pytest.skip("AI Virtual Camera extension not installed/approved yet")
    r = _loopback("AI Virtual Camera", "AI Virtual Camera Input", tmp_path)
    assert r["ok"], r.get("error")
    assert r["capture_resolution"] == "1920x1080"
    assert r["distinct_received"] >= 0.9 * r["sent"] and r["order_ok"]
