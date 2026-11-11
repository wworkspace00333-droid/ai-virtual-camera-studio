"""Input method 2 (direct OBS transfer): shared-memory protocol, reader, source, recovery, plugin build."""
import ctypes
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from vcstudio.core.capture_supervisor import CaptureSupervisor
from vcstudio.core.frame import host_time_s
from vcstudio.core.obs_shm_source import ObsShmSource, obs_sharing_status, shm_lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from make_test_clips import BITS  # noqa: E402

pytestmark = pytest.mark.skipif(shm_lib() is None, reason="libVCSShm not built")
ROOT = Path(__file__).resolve().parents[1]


class Writer:
    """Plays OBS: publishes bar-coded BGRA frames with the plugin's protocol (C writer in libVCSShm)."""

    def __init__(self, name, w=1280, h=720, fps=60):
        self.lib, self.name, self.w, self.h, self.fps = shm_lib(), name, w, h, fps
        self.lib.vcs_shm_unlink(name.encode())
        st = ctypes.c_int(0)
        self.h_ = self.lib.vcs_shm_writer_open(name.encode(), fps, 1, ctypes.byref(st))
        assert self.h_, st.value
        self.stop_ = threading.Event()
        self.pause = threading.Event()
        self.n = 0
        self.sent = {}

    def frame(self, n):
        # reuse one buffer per size and only rewrite the bar-code rows (keeps the writer at 60 fps under load)
        key = (self.w, self.h)
        if getattr(self, "_key", None) != key:
            self._buf = np.zeros((self.h, self.w, 4), np.uint8); self._buf[..., 2] = 90; self._buf[..., 3] = 255
            self._key = key
        a = self._buf
        cell = self.w // BITS
        for b in range(BITS):
            a[self.h - 30:, b * cell:(b + 1) * cell, :3] = 255 if ((n % 1000) >> b) & 1 else 0
        return a

    def run(self):
        t0 = time.perf_counter()
        while not self.stop_.is_set():
            if not self.pause.is_set():
                self.n += 1
                a = self.frame(self.n)
                self.sent[self.n % 1000] = host_time_s()
                self.lib.vcs_shm_writer_publish(self.h_, a.ctypes.data, self.w, self.h, self.w * 4, 0)
            d = t0 + (self.n + 1) / self.fps - time.perf_counter()
            if d > 0:
                time.sleep(d)

    def start(self):
        self.t = threading.Thread(target=self.run, daemon=True); self.t.start(); return self

    def close(self):
        self.stop_.set(); self.t.join(2)
        self.lib.vcs_shm_writer_set_active(self.h_, 0)
        self.lib.vcs_shm_close(self.h_)
        self.lib.vcs_shm_unlink(self.name.encode())


def decode(frame):
    with frame.bgra() as a:
        cell = frame.width // BITS
        row = a[frame.height - 15, :, :3].astype(int).mean(axis=1)
        return sum(1 << b for b in range(BITS) if row[int((b + 0.5) * cell)] > 127)


@pytest.fixture
def writer():
    w = Writer("/vcs-test-frames").start()
    end = time.monotonic() + 3                     # wait for the first published frame (thread start can lag under load)
    while time.monotonic() < end and not obs_sharing_status(w.name)["available"]:
        time.sleep(0.02)
    yield w
    w.close()


def test_status_without_writer():
    s = obs_sharing_status("/vcs-no-such-segment")
    assert not s["available"] and "plugin" in s["reason"]


def test_status_and_frames_in_order_low_latency(writer):
    s = obs_sharing_status(writer.name)
    assert s["available"], s
    assert (s["width"], s["height"], s["fps"]) == (1280, 720, 60.0), s
    got = []
    src = ObsShmSource(writer.name)
    src.start(lambda f: got.append((decode(f), f.arrival, f.width)), lambda k, m: None)
    time.sleep(3)
    src.stop()
    idx = [i for i, _, _ in got]
    gaps = sum(1 for a, b in zip(idx, idx[1:]) if (b - a) % 1000 != 1)
    lat = [(t - writer.sent[i]) * 1000 for i, t, _ in got if i in writer.sent]
    print(f"frames {len(got)}, gaps {gaps}, latency median {np.median(lat):.2f} ms")
    assert len(got) >= 165 and gaps <= 2 and np.median(lat) < 5 and all(w == 1280 for *_, w in got)


def test_supervisor_reconnects_after_obs_restart(writer):
    sup = CaptureSupervisor(missing_timeout_ms=400, backoff=(0.2,))
    sup.start(lambda: ObsShmSource(writer.name))
    try:
        assert wait(lambda: sup.stats().state == "running")
        writer.lib.vcs_shm_writer_set_active(writer.h_, 0)          # OBS stops sharing / quits
        writer.pause.set()
        assert wait(lambda: sup.stats().state in ("reconnecting", "no_signal", "starting"), 3)
        writer.lib.vcs_shm_writer_set_active(writer.h_, 1)          # OBS back
        writer.pause.clear()
        assert wait(lambda: sup.stats().state == "running", 5)
        assert sup.stats().reconnects >= 1
        assert wait(lambda: sup.stats().measured_fps > 40, 3)              # steady rate after reconnect
    finally:
        sup.stop()


def test_resolution_change(writer):
    got = []
    src = ObsShmSource(writer.name)
    src.start(lambda f: got.append((f.width, f.height)), lambda k, m: None)
    time.sleep(0.5)
    writer.w, writer.h = 1920, 1080
    time.sleep(0.8)
    src.stop()
    assert (1280, 720) in got and got[-1] == (1920, 1080)


def wait(pred, t=5.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_plugin_builds_against_installed_obs():
    from vcstudio.core import obs_plugin
    if obs_plugin.obs_version() is None:
        pytest.skip("OBS not installed")
    p = obs_plugin.built_plugin()
    if p is None:
        subprocess.run([str(ROOT / "scripts" / "build_obs_plugin.sh")], check=True, timeout=600)
        p = obs_plugin.built_plugin()
    out = subprocess.run(["nm", "-gU", str(p / "Contents" / "MacOS" / "vcstudio-obs")], capture_output=True, text=True).stdout
    for sym in ("_obs_module_load", "_obs_module_unload", "_obs_module_set_pointer", "_obs_module_ver"):
        assert sym in out
    assert "bundle" in subprocess.run(["file", str(p / "Contents" / "MacOS" / "vcstudio-obs")], capture_output=True,
                                      text=True).stdout
