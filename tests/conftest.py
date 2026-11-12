import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["VCS_DISABLE_GLOBAL_HOTKEYS"] = "1"   # tests must never grab system-wide shortcuts
os.environ["VCS_NO_AUTOSTART"] = "1"             # tests start capture explicitly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vcstudio.core.paths import AppPaths  # noqa: E402


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("VCSTUDIO_HOME", str(tmp_path / "home"))
    return AppPaths(tmp_path / "home").ensure()


@pytest.fixture(autouse=True)
def _no_leaked_worker_threads():
    """Every test must stop the capture/AI/video threads it starts."""
    import threading
    import time as _t
    before = {t.ident for t in threading.enumerate()}
    yield
    names = ("capture-supervisor", "ai-engine", "video-", "test-pattern")
    end = _t.monotonic() + 3
    while True:
        leaked = [t.name for t in threading.enumerate()
                  if t.ident not in before and t.is_alive() and t.name.startswith(names)]
        if not leaked or _t.monotonic() > end:
            break
        _t.sleep(0.05)
    assert not leaked, f"leaked worker threads: {leaked}"
