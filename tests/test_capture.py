"""Capture engine tests (synthetic source; live AVFoundation test is in test_capture_live.py)."""
import threading
import time

import numpy as np
import pytest

from vcstudio.core.capture_supervisor import CaptureSupervisor
from vcstudio.core.frame import Frame
from vcstudio.core.frame_queue import LatestSlot
from vcstudio.core.video_capture import (CaptureError, DeviceFormat, FeedbackLoopRefused, TestPatternSource,
                                         choose_format)


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- slot
def test_latest_slot_keeps_only_newest():
    s = LatestSlot()
    for i in range(100):
        s.put(i)
    assert s.take() == 99
    assert s.overwritten == 99


def test_latest_slot_wait_newer():
    s = LatestSlot()
    threading.Timer(0.05, lambda: s.put("x")).start()
    item, ver = s.wait_newer(0, timeout=2)
    assert item == "x" and ver == 1
    item, ver2 = s.wait_newer(ver, timeout=0.05)   # times out, returns same
    assert ver2 == ver


# ---------------------------------------------------------------- formats
FMTS = [DeviceFormat(1920, 1080, "BGRA", 60, 60), DeviceFormat(1280, 720, "420v", 1, 30),
        DeviceFormat(1280, 720, "420v", 1, 60), DeviceFormat(640, 480, "420v", 1, 30)]


def test_choose_format_exact():
    assert choose_format(FMTS, 1920, 1080, 60) == FMTS[0]
    assert choose_format(FMTS, 1280, 720, 60) == FMTS[2]
    assert choose_format(FMTS, 1280, 720, 30) == FMTS[1]


def test_choose_format_fallbacks():
    assert choose_format(FMTS, 1920, 1080, 30) == FMTS[0]          # exact size, best fps
    assert choose_format(FMTS, 2560, 1440, 60) == FMTS[0]          # closest area supporting fps
    assert choose_format([], 1920, 1080, 60) is None


# ---------------------------------------------------------------- frame
def test_frame_signature_and_downscale():
    a = np.zeros((1080, 1920, 4), np.uint8)
    f = Frame(1, 0, 0, 1920, 1080, array=a)
    s1 = f.signature()
    a[500:600, 500:600] = 255
    assert f.signature() != s1
    small = f.downscaled_copy(960)
    assert small.shape == (540, 960, 4) and small.flags.c_contiguous


# ---------------------------------------------------------------- supervisor
@pytest.fixture
def sup():
    s = CaptureSupervisor(missing_timeout_ms=400, frozen_timeout_ms=500, backoff=(0.1, 0.2))
    yield s
    s.stop()


def test_runs_at_nominal_rate(sup):
    src = TestPatternSource(1280, 720, 60)
    sup.start(lambda: src)
    assert wait_for(lambda: sup.stats().state == "running")
    time.sleep(1.2)
    st = sup.stats()
    assert 50 <= st.measured_fps <= 65, st.measured_fps
    assert (st.width, st.height, st.pixel_format) == (1280, 720, "BGRA")
    f, _ = sup.latest()
    assert f is not None and f.width == 1280


def test_frozen_detected_without_reconnect(sup):
    src = TestPatternSource(640, 360, 30)
    sup.start(lambda: src)
    assert wait_for(lambda: sup.stats().state == "running")
    src.frozen.set()
    assert wait_for(lambda: sup.stats().state == "frozen", 3)
    assert sup.stats().reconnects == 0 and sup.stats().frozen_for_s >= 0.5
    src.frozen.clear()
    assert wait_for(lambda: sup.stats().state == "running", 3)


def test_missing_input_triggers_reconnect_and_recovers(sup):
    src = TestPatternSource(640, 360, 30)
    states = []
    sup.add_state_listener(states.append)
    sup.start(lambda: src)
    assert wait_for(lambda: sup.stats().state == "running")
    src.stalled.set()
    assert wait_for(lambda: sup.stats().reconnects >= 1, 3)
    src.stalled.clear()
    assert wait_for(lambda: sup.stats().state == "running", 3)
    assert "no_signal" in states and "reconnecting" in states


def test_start_failures_retry_with_backoff(sup):
    src = TestPatternSource(320, 180, 30)
    src.fail_start = 2
    sup.start(lambda: src)
    assert wait_for(lambda: sup.stats().state == "running", 5)
    assert src.fail_start == 0


def test_device_disappears_then_returns(sup):
    src = TestPatternSource(320, 180, 30)
    sup.start(lambda: src)
    assert wait_for(lambda: sup.stats().state == "running")
    src.present = False
    src.stalled.set()
    assert wait_for(lambda: sup.stats().state == "reconnecting", 3)
    time.sleep(0.5)
    assert sup.stats().state in ("reconnecting", "starting", "no_signal")
    src.present = True
    src.stalled.clear()
    assert wait_for(lambda: sup.stats().state == "running", 5)


def test_terminal_error_stops_retrying(sup):
    def factory():
        class Own(TestPatternSource):
            def start(self, *a):
                raise FeedbackLoopRefused("own output")
        return Own(320, 180, 30)
    sup.start(factory)
    assert wait_for(lambda: sup.stats().state == "error", 3)
    assert "own output" in sup.stats().last_error


def test_no_autoreconnect_reports_error():
    s = CaptureSupervisor(auto_reconnect=False)
    def bad():
        raise CaptureError("nope")
    s.start(bad)
    assert wait_for(lambda: s.stats().state == "error", 3)
    s.stop()
    assert s.stats().state == "stopped"


def test_stop_is_prompt_and_clears(sup):
    sup.start(lambda: TestPatternSource(320, 180, 60))
    assert wait_for(lambda: sup.stats().state == "running")
    t0 = time.monotonic()
    sup.stop()
    assert time.monotonic() - t0 < 1.0
    assert sup.latest()[0] is None and sup.stats().state == "stopped"


def test_memory_bounded_under_load(sup):
    """Producer at 120 fps, nobody consuming: only one frame is retained."""
    import gc
    sup.start(lambda: TestPatternSource(1920, 1080, 120))
    assert wait_for(lambda: sup.stats().state == "running")
    time.sleep(1.0)
    gc.collect()
    live = [o for o in gc.get_objects() if isinstance(o, Frame)]
    assert len(live) <= 3, len(live)
    assert sup.slot.overwritten > 50
