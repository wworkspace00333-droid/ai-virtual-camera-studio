"""Stress tests (Stage 12). Heavy: run with VCS_STRESS=1 (each takes 10-30 s).

    VCS_STRESS=1 .venv/bin/python -m pytest -s tests/test_stress.py
"""
import os
import random
import statistics
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from vcstudio.ai.model_manager import REPO_MODELS
from vcstudio.app.application import AppContext
from vcstudio.core.bench_source import BenchSource, bus_scene
from vcstudio.core.performance import phys_footprint
from vcstudio.native import metal
from vcstudio.replacement.spec import ReplacementSpec
from vcstudio.roi.model import ROI, new_rect

pytestmark = [pytest.mark.skipif(os.environ.get("VCS_STRESS") != "1", reason="set VCS_STRESS=1"),
              pytest.mark.skipif(not metal.available(), reason="Metal library not built"),
              pytest.mark.skipif(not (REPO_MODELS / "yolo11n-seg.mlpackage").exists(), reason="models missing")]
DATA = Path(__file__).parent / "data"


def crowd_scene(w=1920, h=1080, nx=3, ny=2):
    from vcstudio.ai.testimages import load_bgra
    bus = load_bgra(DATA / "bus.jpg")
    tile = cv2.resize(bus, (w // nx, h // ny))
    img = np.zeros((h, w, 4), np.uint8); img[..., 3] = 255
    for j in range(ny):
        for i in range(nx):
            img[j * (h // ny):(j + 1) * (h // ny), i * (w // nx):(i + 1) * (w // nx)] = tile
    return img


@pytest.fixture
def ctx(paths):
    c = AppContext(paths, configure_logging=False)
    c.settings.set("input.method", "test_pattern")
    c.settings.set("output.camera_enabled", False)
    yield c
    c.stop_capture()
    c.shutdown()


def run_and_measure(ctx, scene, seconds=10.0, warm=4.0):
    ctx.capture_factory = lambda: (lambda: BenchSource(scene, 60))
    ctx.start_capture()
    time.sleep(warm)
    fps, lat = [], []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(1.0)
        s = ctx.pipeline.stats()
        fps.append(s.output_fps); lat.append(s.latency_ms_p95)
    s = ctx.pipeline.stats()
    r = {"fps_min": min(fps), "fps_med": statistics.median(fps), "lat_p95_max": max(lat),
         "process_ms": s.process_ms, "render_errors": s.render_errors, "regions": s.regions,
         "footprint_mb": phys_footprint() / 2**20, "ai": ctx.ai.stats()}
    print({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if k != "ai"},
          "AI", r["ai"].model, round(r["ai"].achieved_fps, 1), "tracks", r["ai"].tracks_active)
    return r


def test_many_rois(ctx):
    logo = ctx.assets.import_file(DATA / "logo_alpha.png")
    clip = ctx.assets.import_file(DATA / "clip_h264.mp4")
    rois = []
    rnd = random.Random(1)
    for i in range(24):
        x, y = rnd.uniform(0, 0.8), rnd.uniform(0, 0.8)
        r = new_rect(f"R{i}", x, y, x + rnd.uniform(0.05, 0.2), y + rnd.uniform(0.05, 0.2))
        r.rotation, r.priority = rnd.uniform(-30, 30), i
        kind = i % 4
        spec = (ReplacementSpec(enabled=True, asset_id=logo.id, feather_px=3) if kind == 0 else
                ReplacementSpec(enabled=True, mode="overlay", asset_id=logo.id, opacity=0.7) if kind == 1 else
                ReplacementSpec(enabled=True, asset_id=clip.id) if kind == 2 else
                ReplacementSpec(enabled=True, mode="effect", effect=("blur", "pixelate", "color")[i % 3]))
        r.replacement = spec.to_dict()
        rois.append(r)
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict(), clip.to_dict()]),
                                         b.__setitem__("rois", [x.to_dict() for x in rois])))
    r = run_and_measure(ctx, bus_scene())
    assert r["render_errors"] == 0 and r["regions"] == 24
    assert r["fps_med"] >= 58 and r["fps_min"] >= 50 and r["lat_p95_max"] < 16.7


def test_crowd_segmentation(ctx):
    logo = ctx.assets.import_file(DATA / "logo_alpha.png")
    people = ROI(name="People", mode="auto", classes=["person"], padding=0.0,
                 replacement=ReplacementSpec(enabled=True, mode="object", asset_id=logo.id, feather_px=2).to_dict())
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict()]), b.__setitem__("rois", [people.to_dict()]),
                                         b["detection"].__setitem__("confidence", 0.3)))
    r = run_and_measure(ctx, crowd_scene())
    a = r["ai"]
    assert a.model.endswith("-seg") and a.tracks_active >= 12, a.tracks_active
    assert r["render_errors"] == 0 and r["fps_med"] >= 58 and r["fps_min"] >= 50
    ids_before = a.track_ids_created
    time.sleep(5)
    assert ctx.ai.stats().track_ids_created - ids_before <= 3        # static crowd: IDs stay stable


def test_six_video_players(ctx):
    clip = ctx.assets.import_file(DATA / "clip_h264.mp4")
    rois = []
    for i in range(6):
        r = new_rect(f"V{i}", 0.02 + 0.16 * i, 0.1, 0.16 + 0.16 * i, 0.6)
        r.replacement = ReplacementSpec(enabled=True, asset_id=clip.id, video_sync="per_roi",
                                        video_start_offset_s=0.3 * i).to_dict()
        rois.append(r)
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [clip.to_dict()]), b.__setitem__("rois", [x.to_dict() for x in rois])))
    r = run_and_measure(ctx, bus_scene())
    assert ctx.replacement.compositor.stats.video_players == 6
    assert r["fps_med"] >= 58 and r["render_errors"] == 0 and r["footprint_mb"] < 1500


def test_configuration_churn_while_running(ctx):
    logo = ctx.assets.import_file(DATA / "logo_alpha.png")
    ctx.profiles.create("B")
    ctx.capture_factory = lambda: (lambda: BenchSource(bus_scene(), 60))
    ctx.start_capture()
    time.sleep(2)
    rnd = random.Random(7)
    t_end = time.monotonic() + 30
    n = 0
    while time.monotonic() < t_end:
        op = rnd.randrange(5)
        if op == 0:
            ctx.profiles.switch(rnd.choice(ctx.profiles.names()))
        elif op == 1:
            r = new_rect("C", rnd.random() * 0.7, rnd.random() * 0.7, 0.9, 0.9)
            r.replacement = ReplacementSpec(enabled=True, asset_id=logo.id).to_dict()
            ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict()]), b.__setitem__("rois", [r.to_dict()])))
        elif op == 2:
            ctx.pipeline.set_passthrough(rnd.random() < 0.5, "churn")
        elif op == 3:
            ctx.settings.set("performance.mode", rnd.choice(["performance", "balanced", "low_resource", "max_quality"]))
        else:
            ctx.update_active_profile(lambda b: b["ai"].__setitem__("enabled", rnd.random() < 0.8))
        n += 1
        time.sleep(0.1)
    ctx.pipeline.set_passthrough(False)
    f0 = ctx.pipeline.stats().frames_out
    time.sleep(2)
    s = ctx.pipeline.stats()
    print("churn operations:", n, "render errors", s.render_errors, "restarts", s.restarts)
    assert n > 200 and s.frames_out > f0 + 90 and s.render_errors == 0 and s.restarts == 0
    assert ctx.capture.stats().state == "running"


def test_input_flapping(ctx):
    holder = {}

    def factory():
        holder["s"] = BenchSource(bus_scene(), 60)
        return holder["s"]
    ctx.settings.set("input.missing_input_timeout_ms", 300)
    ctx.capture_factory = lambda: factory
    ctx.start_capture()
    time.sleep(2)
    for _ in range(12):
        holder["s"].stalled.set(); time.sleep(0.7)
        holder["s"].stalled.clear(); time.sleep(1.0)
    end = time.monotonic() + 8
    while time.monotonic() < end and ctx.capture.stats().state != "running":
        time.sleep(0.1)
    c = ctx.capture.stats()
    print("reconnects", c.reconnects, "state", c.state)
    assert c.state == "running" and c.reconnects >= 6
    f0 = ctx.pipeline.stats().frames_out
    time.sleep(1.5)
    assert ctx.pipeline.stats().frames_out > f0 + 60


def test_rapid_start_stop(ctx):
    logo = ctx.assets.import_file(DATA / "logo_alpha.png")
    r = new_rect("R", 0.1, 0.1, 0.5, 0.5); r.replacement = ReplacementSpec(enabled=True, asset_id=logo.id).to_dict()
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict()]), b.__setitem__("rois", [r.to_dict()])))
    scene = bus_scene()
    ctx.capture_factory = lambda: (lambda: BenchSource(scene, 60))
    fps_after = []
    for i in range(20):
        ctx.start_capture()
        time.sleep(0.6)
        ctx.stop_capture()
        if i == 4:
            fp5 = phys_footprint() / 2**20
    fp20 = phys_footprint() / 2**20
    names = [t.name for t in threading.enumerate()]
    print(f"footprint after 5 cycles {fp5:.0f} MB, after 20 {fp20:.0f} MB; threads {len(names)}")
    assert fp20 - fp5 < 60
    assert not [n for n in names if n.startswith(("capture-supervisor", "ai-engine", "video-pipeline", "video-"))]


def test_gui_page_switching_under_load(paths, qtbot):
    from vcstudio.ui.main_window import MainWindow
    ctx = AppContext(paths, configure_logging=False)
    ctx.settings.set("input.method", "test_pattern"); ctx.settings.set("output.camera_enabled", False)
    ctx.capture_factory = lambda: (lambda: BenchSource(bus_scene(), 60))
    logo = ctx.assets.import_file(DATA / "logo_alpha.png")
    people = ROI(name="People", mode="auto", classes=["person"],
                 replacement=ReplacementSpec(enabled=True, mode="object", asset_id=logo.id).to_dict())
    ctx.update_active_profile(lambda b: (b.__setitem__("assets", [logo.to_dict()]), b.__setitem__("rois", [people.to_dict()])))
    w = MainWindow(ctx); qtbot.addWidget(w); w.resize(1500, 950); w.show()
    w.start_processing()
    keys = w.page_keys()
    t_end = time.monotonic() + 20
    i = 0
    while time.monotonic() < t_end:
        w.goto(keys[i % len(keys)])
        qtbot.wait(100)
        i += 1
    s = ctx.pipeline.stats()
    print("page switches", i, "output fps", round(s.output_fps, 1), "render errors", s.render_errors)
    assert s.render_errors == 0 and s.output_fps >= 50
    w.close(); ctx.stop_capture(); ctx.shutdown()
