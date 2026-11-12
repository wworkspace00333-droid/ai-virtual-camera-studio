"""Only one app window per data directory (two copies fight over the output camera)."""
import os
import subprocess
import sys
import time
from pathlib import Path

from vcstudio.core import single_instance

ROOT = Path(__file__).resolve().parents[1]


def test_lock_is_exclusive_and_released(tmp_path):
    holder = subprocess.Popen([sys.executable, "-c",
                               "import sys,time; sys.path.insert(0, sys.argv[1]);"
                               "from pathlib import Path; from vcstudio.core import single_instance as s;"
                               "assert s.acquire(Path(sys.argv[2])) is None; print('held', flush=True); time.sleep(30)",
                               str(ROOT), str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert single_instance.acquire(tmp_path) == holder.pid      # the other process, by pid
    finally:
        holder.kill(); holder.wait()
    # the OS dropped the dead process's lock: a new start is not blocked by a stale file
    assert single_instance.acquire(tmp_path) is None
    single_instance.release(tmp_path)


def test_second_gui_copy_exits(tmp_path):
    env = dict(os.environ, VCSTUDIO_HOME=str(tmp_path), QT_QPA_PLATFORM="offscreen", VCS_DISABLE_GLOBAL_HOTKEYS="1",
               VCS_NO_AUTOSTART="1")
    first = subprocess.Popen([sys.executable, "-m", "vcstudio"], cwd=ROOT, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        lock = tmp_path / "app.lock"
        end = time.time() + 30
        while time.time() < end and not (lock.exists() and lock.read_text().strip() == str(first.pid)):
            time.sleep(0.2)
        assert lock.read_text().strip() == str(first.pid)
        second = subprocess.run([sys.executable, "-m", "vcstudio", "--smoke-test"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=60)
        assert second.returncode == 3 and str(first.pid) in second.stderr
    finally:
        first.terminate(); first.wait(15)
