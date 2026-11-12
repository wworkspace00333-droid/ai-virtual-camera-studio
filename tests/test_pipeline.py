"""Stage 10: pipeline modes, error isolation, camera reconnect, watchdog, hotkeys, feedback-loop guard."""
import json
import os
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from vcstudio.app.application import AppContext
from vcstudio.core.frame import Frame
from vcstudio.core.video_capture import TestPatternSource
from vcstudio.core.video_pipeline import AUTO_PASSTHROUGH_ERRORS, CameraOutputController
from vcstudio.replacement.spec import ReplacementSpec
from vcstudio.roi.model import new_rect

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data"


def wait_for(pred, t=8.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class FakeOutput:
    def __init__(self):
        self.sent, self.fail_next = [], 0

    def send(self, pb, pts):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("camera went away")
        self.sent.append(pts)
        return True

    def stats(self):
        return len(self.sent), 0

    def close(self):
        pass


@pytest.fixture
def ctx(paths):
    c = AppContext(paths, configure_logging=False)
    c.settings.set("input.method", "test_pattern")
    c.settings.set("input.preferred_width", 1280)
    c.settings.set("input.preferred_height", 720)
    c.settings.set("output.camera_enabled", False)
    yield c
    c.stop_capture()
    c.shutdown()


def add_rect_replacement(ctx, **kw):
    a = ctx.assets.import_file(DATA / "logo_alpha.png")
    roi = new_rect("R", 0.25, 0.25, 0.75, 0.75)
    roi.replacement = ReplacementSpec(enabled=True, asset_id=a.id, background=[255, 0, 255, 255], **kw).to_dict()
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [a.to_dict()]), b.__setitem__("rois", [roi.to_dict()])))
    return roi


def latest_pair(ctx):
    pair, _ = ctx.pipeline.processed.peek()
    return pair


def px(frame, y, x):
    with frame.bgra() as a:
        return tuple(int(v) for v in a[y, x])


def test_passthrough_is_zero_copy_without_replacements(ctx):
    ctx.start_capture()
    assert wait_for(lambda: ctx.pipeline.stats().frames_out > 75)      # steady state (> 1 s)
    src, out = latest_pair(ctx)
    assert out.source == "passthrough"
    from vcstudio.core.pixelbuffer import pixel_buffer_for
    assert out.pixel_buffer is not None
    s = ctx.pipeline.stats()
    assert s.state == "running" and 50 <= s.output_fps <= 65 and s.latency_ms < 30


def test_replacement_applied_and_rate_follows_input(ctx):
    add_rect_replacement(ctx)
    ctx.start_capture()
    assert wait_for(lambda: ctx.pipeline.stats().frames_out > 75)
    src, out = latest_pair(ctx)
    assert out.source == "processed" and src.seq == out.seq
    assert px(out, 200, 330)[:3] == (255, 0, 255)            # inside region: magenta fill
    assert px(out, 20, 20) == px(src, 20, 20)                # outside: untouched
    s = ctx.pipeline.stats()
    assert 50 <= s.output_fps <= 65 and s.render_errors == 0 and s.regions == 1


def test_pause_freezes_output_and_resumes(ctx):
    ctx.start_capture()
    assert wait_for(lambda: ctx.pipeline.stats().frames_out > 5)
    ctx.pipeline.set_paused(True)
    time.sleep(0.1)
    seq1 = latest_pair(ctx)[1].seq
    time.sleep(0.3)
    p2 = latest_pair(ctx)
    assert p2[1].seq == seq1 and p2[0].seq > seq1             # capture continues, output frozen
    assert ctx.pipeline.stats().state == "paused"
    ctx.pipeline.set_paused(False)
    assert wait_for(lambda: latest_pair(ctx)[1].seq > seq1)


def test_emergency_passthrough_and_replacement_toggle(ctx):
    add_rect_replacement(ctx)
    ctx.start_capture()
    assert wait_for(lambda: latest_pair(ctx) is not None and latest_pair(ctx)[1].source == "processed")
    ctx.pipeline.set_passthrough(True, "test")
    assert wait_for(lambda: latest_pair(ctx)[1].source == "passthrough")
    assert ctx.pipeline.stats().state == "passthrough" and ctx.pipeline.stats().passthrough_reason == "test"
    ctx.pipeline.set_passthrough(False)
    assert wait_for(lambda: latest_pair(ctx)[1].source == "processed")
    ctx.pipeline.set_replacement_enabled(False)
    assert wait_for(lambda: latest_pair(ctx)[1].source == "passthrough")


def test_render_failures_pass_frames_through_then_auto_passthrough(ctx, monkeypatch):
    add_rect_replacement(ctx)
    ctx.start_capture()
    assert wait_for(lambda: ctx.pipeline.stats().frames_out > 5)

    def boom(*a, **k):
        raise RuntimeError("GPU exploded")
    monkeypatch.setattr(ctx.replacement, "render_pb", boom)
    assert wait_for(lambda: ctx.pipeline.passthrough)
    s = ctx.pipeline.stats()
    assert s.render_errors >= AUTO_PASSTHROUGH_ERRORS and "automatic" in s.passthrough_reason
    frames = s.frames_out
    assert wait_for(lambda: ctx.pipeline.stats().frames_out > frames + 10)    # output never stopped
    assert latest_pair(ctx)[1].source == "passthrough"


def test_camera_controller_retry_and_reconnect(monkeypatch):
    import vcstudio.camera.output as out_mod
    c = CameraOutputController("No Such Camera", "No Sink", True)
    assert c.send(object(), 0.0) is None and c.state == "not_installed"
    assert c.send(object(), 0.0) is None                                    # backoff: no hammering
    fake = FakeOutput()
    monkeypatch.setattr(out_mod.VirtualCameraOutput, "open", lambda self: None)
    monkeypatch.setattr(out_mod.VirtualCameraOutput, "send", lambda self, pb, pts: fake.send(pb, pts))
    c._next_try = 0
    assert c.send(object(), 1.0) is True and c.state == "connected"
    fake.fail_next = 1
    assert c.send(object(), 2.0) is None and c.state == "error"           # error -> closed, will reconnect
    c._next_try = 0
    assert c.send(object(), 3.0) is True and c.state == "connected"
    c.configure("X", "Y", False)
    assert c.send(object(), 4.0) is None and c.state == "disabled"


def test_feedback_loop_guard(ctx):
    ctx.settings.set("input.method", "avfoundation")
    ctx.settings.set("input.device_name_hint", "AI Virtual Camera")
    ctx.settings.set("output.camera_enabled", True)
    with pytest.raises(RuntimeError, match="feed back"):
        ctx.start_capture()
    assert ctx.capture.stats().state == "stopped"


def test_watchdog_restarts_stalled_pipeline(ctx, monkeypatch):
    add_rect_replacement(ctx)
    ctx.watchdog.interval = 0.1
    ctx.start_capture()
    assert wait_for(lambda: ctx.pipeline.stats().frames_out > 5)
    real = ctx.replacement.render_pb
    hang = {"on": True}

    def stuck(*a, **k):
        while hang["on"]:
            time.sleep(0.05)       # simulates a deadlocked render call
        return real(*a, **k)
    monkeypatch.setattr(ctx.replacement, "render_pb", stuck)
    import vcstudio.core.watchdog as wd
    monkeypatch.setattr(wd, "PIPELINE_STALL_S", 0.5)
    assert wait_for(lambda: ctx.watchdog.stats.pipeline_restarts >= 1, 5)
    hang["on"] = False
    monkeypatch.setattr(ctx.replacement, "render_pb", real)
    n = ctx.pipeline.stats().frames_out
    assert wait_for(lambda: ctx.pipeline.stats().frames_out > n + 10)


def test_watchdog_restarts_stalled_ai(ctx, monkeypatch):
    from vcstudio.ai.model_manager import REPO_MODELS
    if not (REPO_MODELS / "yolo11n.mlpackage").exists():
        pytest.skip("no model")
    import vcstudio.core.watchdog as wd
    monkeypatch.setattr(wd, "AI_STALL_S", 0.8)
    ctx.watchdog.interval = 0.1
    ctx.start_capture()
    assert wait_for(lambda: ctx.ai.stats().inferences > 3)
    det = ctx.ai._detector
    stop = {"on": True}

    def hang(*a, **k):
        while stop["on"]:
            time.sleep(0.05)
        raise RuntimeError("released")
    det.detect = hang
    assert wait_for(lambda: ctx.watchdog.stats.ai_restarts >= 1, 6)
    stop["on"] = False
    assert wait_for(lambda: ctx.ai.stats().state == "running" and ctx.ai._detector is not det, 8)
    assert ctx.pipeline.stats().frames_out > 0 and ctx.capture.stats().state == "running"


def test_watchdog_memory_load_shedding(ctx):
    ctx.settings.set("performance.mode", "performance")
    w = ctx.watchdog
    w.warn_mb, w.critical_mb = 100, 200
    w.rss_fn = lambda: 150
    w.check()
    assert w.stats.memory_state == "warning" and not w.stats.load_shed
    w.rss_fn = lambda: 250
    w.check()
    assert w.stats.load_shed and ctx.settings.get("performance.mode") == "low_resource"
    w.rss_fn = lambda: 50
    w.check()
    assert not w.stats.load_shed and ctx.settings.get("performance.mode") == "performance"
    events = [e["message"] for e in ctx.db.recent_events(20)]
    assert any("critical memory" in m for m in events)


def test_memory_threshold_settings_apply_live(ctx):
    from vcstudio.core.performance import auto_memory_warning_mb
    auto = auto_memory_warning_mb()
    assert (ctx.watchdog.warn_mb, ctx.watchdog.critical_mb) == (auto, 2 * auto)
    ctx.settings.set("performance.memory_warning_mb", 1000)
    assert ctx.watchdog.warn_mb == auto                       # ignored while automatic
    ctx.settings.set("performance.memory_limits", "manual")
    assert (ctx.watchdog.warn_mb, ctx.watchdog.critical_mb) == (1000, 2000)
    assert ctx.monitor.memory_warning_bytes == 1000 * 2**20
    ctx.settings.set("performance.memory_limits", "auto")
    assert ctx.watchdog.warn_mb == auto


def test_hotkey_parsing_and_native_registration():
    from vcstudio.native import metal
    from vcstudio.native.hotkeys import CMD, CTRL, OPT, SHIFT, GlobalHotkeys, parse
    assert parse("Ctrl+Alt+P") == (35, CMD | OPT)
    assert parse("Meta+Shift+F5") == (96, CTRL | SHIFT)
    assert parse("Ctrl+Alt+8") == (28, CMD | OPT) and parse("Ctrl+Alt+0") == (29, CMD | OPT)
    assert parse("P") is None and parse("") is None and parse("Hyper+P") is None
    if not metal.available():
        pytest.skip("native lib missing")
    g = GlobalHotkeys()
    errs = g.apply({"a": "Ctrl+Alt+Shift+F15", "b": "Q"}, lambda a: None)   # obscure combo, released at once
    assert g.count == 1 and "b" in errs
    g.clear()
    assert g.count == 0


@pytest.mark.skipif(os.environ.get("VCS_LIVE") != "1", reason="set VCS_LIVE=1")
def test_live_end_to_end_through_cmio_camera(tmp_path):
    """Input -> AI -> tracking -> object replacement -> Metal -> CMIO camera -> client capture (dev app bundle)."""
    from vcstudio.camera import camera_manager as cm
    from vcstudio.camera.output import list_cmio_devices
    if cm.status().device_visible:
        dev, sink = "AI Virtual Camera", "AI Virtual Camera Input"
    elif "OBS Virtual Camera" in list_cmio_devices():
        dev, sink = "OBS Virtual Camera", "OBS Camera Extension Stream Sink"
    else:
        pytest.skip("no CMIO camera with a sink stream installed")
    app = ROOT / "build" / "AI Virtual Camera Studio Dev.app"
    out = tmp_path / "e2e.json"
    subprocess.run(["open", "-W", "-n", str(app), "--args", "--pipeline-e2e-test", str(out), "--seconds", "8",
                    "--device", dev, "--sink", sink], check=True, timeout=240)
    r = json.loads(out.read_text())
    print({k: r.get(k) for k in ("out_device", "pipeline_fps", "pipeline_latency_ms", "client_distinct_fps",
                                 "frames_with_replacement", "ai_model", "ai_fps", "error")})
    assert r["ok"], r.get("error")
    assert r["client_distinct_fps"] >= 50 and r["client_resolution"] == "1920x1080"


def test_camera_output_pixel_format_reaches_the_sender(monkeypatch):
    """Settings > Pixel format used to be shown but ignored; it now selects what the camera receives."""
    import vcstudio.camera.output as out
    from vcstudio.core.video_pipeline import CameraOutputController
    made = []

    class Fake:
        def __init__(self, device, sink, pixel_format="BGRA"):
            made.append(pixel_format)

        def open(self):
            pass

        def send(self, pb, pts):
            return True

        def close(self):
            pass
    monkeypatch.setattr(out, "VirtualCameraOutput", Fake)
    c = CameraOutputController("OBS Virtual Camera", "OBS Camera Extension Stream Sink", True, "NV12")
    assert c.send(object(), 0.0) is True and made == ["NV12"]
    c.configure("OBS Virtual Camera", "OBS Camera Extension Stream Sink", True, "BGRA")   # change -> reopen
    c.send(object(), 0.0)
    assert made == ["NV12", "BGRA"]
    assert out.PIXEL_FORMATS["NV12"] == int.from_bytes(b"420v", "big") and out.PIXEL_FORMATS["BGRA"] == 0
