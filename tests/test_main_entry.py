"""The real entry point (vcstudio.app.main) in a subprocess: what a user's first launch runs.

Regression: an UnboundLocalError in main() crashed every launch with --snapshot; unit tests build
MainWindow directly and never went through main()."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_first_launch_renders_all_pages_in_russian(tmp_path):
    home, shots = tmp_path / "home", tmp_path / "shots"
    env = dict(os.environ, VCSTUDIO_HOME=str(home), QT_QPA_PLATFORM="offscreen", VCS_DISABLE_GLOBAL_HOTKEYS="1",
               VCS_NO_AUTOSTART="1")
    r = subprocess.run([sys.executable, "-m", "vcstudio", "--lang", "ru", "--snapshot", str(shots), "--snapshot-all"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    pages = sorted(p.stem for p in shots.glob("*.png"))
    assert len(pages) == 12, pages
    assert (home / "settings.json").exists() and (home / "profiles" / "default.json").exists()
    log = (home / "logs" / "studio.log").read_text()
    assert "Unhandled exception" not in log and "Traceback" not in log
