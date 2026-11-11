"""Live capture through the dev app bundle (needs OBS Virtual Camera + camera permission).

Skipped unless VCS_LIVE=1. Run:  VCS_LIVE=1 .venv/bin/python -m pytest tests/test_capture_live.py -s
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "build" / "AI Virtual Camera Studio Dev.app"

pytestmark = pytest.mark.skipif(os.environ.get("VCS_LIVE") != "1", reason="set VCS_LIVE=1 for live capture test")


def test_obs_virtual_camera_capture(tmp_path):
    if not APP.exists():
        subprocess.run([str(ROOT / "scripts" / "build_dev_app.sh")], check=True)
    out = tmp_path / "cap.json"
    subprocess.run(["open", "-W", "-n", str(APP), "--args", "--capture-selftest", str(out), "--seconds", "8"],
                   check=True, timeout=180)
    r = json.loads(out.read_text())
    print(json.dumps({k: r.get(k) for k in ("ok", "steady_fps_avg", "steady_fps_min", "preview_convert_ms_avg")}))
    assert r["permission_granted"], r.get("error")
    assert r["ok"], r.get("error")
    f = r["final"]
    assert (f["width"], f["height"]) == (1920, 1080)
    assert r["steady_fps_avg"] >= 0.9 * f["nominal_fps"]
    assert f["reconnects"] == 0
