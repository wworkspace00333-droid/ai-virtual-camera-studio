"""Metal (GPU) compositor: parity with the CPU backend, effects, zero-copy path, performance, fallback."""
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from vcstudio.ai.testimages import load_bgra
from vcstudio.ai.tracking import TrackedObject
from vcstudio.core.frame import Frame
from vcstudio.core.pixelbuffer import pixel_buffer_for
from vcstudio.native import metal
from vcstudio.replacement.assets import AssetLibrary
from vcstudio.replacement.compositor import Compositor
from vcstudio.replacement.spec import ReplacementSpec
from vcstudio.roi.model import ROI, new_polygon, new_rect
from vcstudio.roi.resolver import resolve

pytestmark = pytest.mark.skipif(not metal.available(), reason="libVCSNative not built (scripts/build_native.sh)")
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def scene():
    bus = load_bgra(DATA / "bus.jpg")
    frame = np.ascontiguousarray(cv2.resize(bus, (1280, 1080 * 1280 // 810))[:720])
    return frame


@pytest.fixture
def comps(tmp_path):
    lib = AssetLibrary(tmp_path / "a")
    cpu, gpu = Compositor(lib, "cpu"), Compositor(lib, "metal")
    assert gpu.active.name == "metal", gpu.backend_error
    yield lib, cpu, gpu
    cpu.close(); gpu.close()


def diff(a, b):
    d = np.abs(a.astype(int) - b.astype(int))[..., :3]
    return d.max(), d.mean(), (d > 3).mean()


CASES = [
    dict(mode="rect", fit="fill"),
    dict(mode="rect", fit="fit", background=[255, 0, 255, 255], feather_px=6),
    dict(mode="rect", fit="stretch", rotation=33, opacity=0.7),
    dict(mode="rect", fit="fit", background=[0, 0, 0, 0]),                    # transparent fill => original shows
    dict(mode="overlay", fit="fit", scale=1.4, offset_x=0.2),
    dict(mode="overlay", fit="original", crop=[0.1, 0.2, 0.8, 0.9], opacity=0.5),
]


@pytest.mark.parametrize("kw", CASES, ids=[f"{c['mode']}-{i}" for i, c in enumerate(CASES)])
@pytest.mark.parametrize("rot", [0, 17])
def test_composite_parity(comps, scene, kw, rot):
    lib, cpu, gpu = comps
    a = lib.import_file(DATA / "logo_alpha.png")
    roi = new_rect("R", 0.2, 0.15, 0.7, 0.75); roi.rotation = rot
    regs = resolve([roi], [], 1280, 720)
    spec = {roi.id: ReplacementSpec(enabled=True, asset_id=a.id, **kw)}
    oc = cpu.render(scene, regs, spec, {a.id: a})
    og = gpu.render(scene, regs, spec, {a.id: a})
    mx, mean, frac = diff(oc, og)
    assert mean < 0.3 and frac < 0.005, (mx, mean, frac)
    assert np.array_equal(og[..., 3], scene[..., 3])                 # frame alpha preserved
    assert not np.array_equal(og, scene)


def test_polygon_and_object_mask_parity(comps, scene):
    lib, cpu, gpu = comps
    a = lib.import_file(DATA / "logo_alpha.png")
    poly = new_polygon("P", [[0.1, 0.1], [0.5, 0.05], [0.6, 0.6], [0.3, 0.8], [0.05, 0.5]])
    mask = np.zeros((300, 150), np.uint8); cv2.ellipse(mask, (75, 150), (60, 140), 0, 0, 360, 255, -1)
    auto = ROI(name="O", mode="auto", padding=0)
    obj = TrackedObject(1, "person", 0.9, 800, 200, 950, 500, "tracked", 1, 0, mask=mask)
    regs = resolve([poly, auto], [obj], 1280, 720)
    specs = {poly.id: ReplacementSpec(enabled=True, asset_id=a.id, feather_px=4),
             auto.id: ReplacementSpec(enabled=True, mode="object", asset_id=a.id, feather_px=2,
                                      background=[0, 255, 0, 255])}
    mx, mean, frac = diff(cpu.render(scene, regs, specs, {a.id: a}), gpu.render(scene, regs, specs, {a.id: a}))
    assert mean < 0.3 and frac < 0.005, (mx, mean, frac)


@pytest.mark.parametrize("effect,params,tol", [
    ("pixelate", dict(pixel_size=24), 0.0),
    ("color", dict(brightness=0.1, contrast=1.4, saturation=0.2), 0.01),
    ("blur", dict(blur_sigma=8), 0.08),          # MPS and OpenCV Gaussian kernels differ slightly
])
def test_effect_parity(comps, scene, effect, params, tol):
    lib, cpu, gpu = comps
    roi = new_rect("E", 0.3, 0.2, 0.8, 0.9); roi.rotation = 12
    regs = resolve([roi], [], 1280, 720)
    spec = {roi.id: ReplacementSpec(enabled=True, mode="effect", effect=effect, **params)}
    oc, og = cpu.render(scene, regs, spec, {}), gpu.render(scene, regs, spec, {})
    mx, mean, frac = diff(oc, og)
    assert frac <= tol + 1e-9 and mean < 1.0, (effect, mx, mean, frac)
    inside = (slice(300, 400), slice(600, 800))
    assert np.abs(og[inside].astype(int) - scene[inside].astype(int)).mean() > 2   # effect visible
    assert np.array_equal(og[:50, :50], scene[:50, :50])                          # outside untouched


def test_layer_order_and_multiple_layers(comps, scene):
    lib, cpu, gpu = comps
    a = lib.import_file(DATA / "logo_alpha.png")
    rois = []
    for i in range(8):
        r = new_rect(f"L{i}", 0.05 + 0.1 * i, 0.1, 0.25 + 0.1 * i, 0.6); r.priority = i
        r.replacement = {}
        rois.append(r)
    specs = {r.id: ReplacementSpec(enabled=True, asset_id=a.id, background=[(37 * i) % 255, 90, 200, 255])
             for i, r in enumerate(rois)}
    specs[rois[3].id] = ReplacementSpec(enabled=True, mode="effect", effect="pixelate", pixel_size=16)
    regs = resolve(rois, [], 1280, 720)
    mx, mean, frac = diff(cpu.render(scene, regs, specs, {a.id: a}), gpu.render(scene, regs, specs, {a.id: a}))
    assert mean < 0.3 and frac < 0.005, (mx, mean, frac)


def test_zero_copy_pixel_buffer_path(comps, scene):
    lib, cpu, gpu = comps
    a = lib.import_file(DATA / "logo_alpha.png")
    roi = new_rect("R", 0.1, 0.1, 0.5, 0.5)
    regs = resolve([roi], [], 1280, 720)
    spec = {roi.id: ReplacementSpec(enabled=True, asset_id=a.id)}
    src = Frame(1, 0, 0, 1280, 720, pixel_buffer=pixel_buffer_for(Frame(0, 0, 0, 1280, 720, array=scene)))
    out_pb = gpu.render_pb(src, regs, spec, {a.id: a})
    import Quartz as Q
    assert Q.CVPixelBufferGetIOSurface(out_pb) is not None                     # IOSurface: shareable with the camera
    with Frame(0, 0, 0, 1280, 720, pixel_buffer=out_pb).bgra() as o:
        ref = cpu.render(scene, regs, spec, {a.id: a})
        assert diff(np.array(o), ref)[1] < 0.3
    with src.bgra() as s:
        assert np.array_equal(s, scene)                                         # source untouched


def test_video_frames_on_gpu(comps):
    lib, cpu, gpu = comps
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from make_test_clips import decode_index
    v = lib.import_file(DATA / "clip_h264.mp4")
    roi = new_rect("V", 0, 0, 1, 1)
    regs = resolve([roi], [], 640, 360)
    spec = {roi.id: ReplacementSpec(enabled=True, asset_id=v.id, fit="stretch")}
    black = np.zeros((360, 640, 4), np.uint8); black[..., 3] = 255
    seen = set()
    for t in (10.0, 10.5, 11.0):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            out = gpu.render(black, regs, spec, {v.id: v}, t)
            if gpu.stats.regions_drawn:
                seen.add(decode_index(out))
                if decode_index(out) == round((t - 10.0) * 30):
                    break
            time.sleep(0.01)
    assert {0, 15, 30} <= seen


def test_metal_performance_1080p(comps):
    lib, cpu, gpu = comps
    a = lib.import_file(DATA / "logo_alpha.png")
    v = lib.import_file(DATA / "clip_h264.mp4")
    frame = np.zeros((1080, 1920, 4), np.uint8); frame[..., 1] = 120; frame[..., 3] = 255
    src = Frame(1, 0, 0, 1920, 1080, pixel_buffer=pixel_buffer_for(Frame(0, 0, 0, 1920, 1080, array=frame)))
    rois, specs = [], {}
    for i, (mode, extra) in enumerate([("rect", dict(feather_px=6)), ("overlay", {}), ("rect", dict(asset="v")),
                                        ("effect", dict(effect="blur", blur_sigma=10)),
                                        ("effect", dict(effect="pixelate", pixel_size=20))]):
        r = new_rect(f"R{i}", 0.05 + 0.18 * i, 0.1, 0.25 + 0.18 * i, 0.8); r.rotation = 10 * i; r.priority = i
        rois.append(r)
        aid = v.id if extra.pop("asset", None) == "v" else a.id
        specs[r.id] = ReplacementSpec(enabled=True, mode=mode, asset_id=aid, **extra)
    regs = resolve(rois, [], 1920, 1080)
    assets = {a.id: a, v.id: v}
    for _ in range(10):
        gpu.render_pb(src, regs, specs, assets, time.monotonic())
    wall, gpu_ms = [], []
    for _ in range(60):
        t = time.perf_counter()
        gpu.render_pb(src, regs, specs, assets, time.monotonic())
        wall.append((time.perf_counter() - t) * 1000); gpu_ms.append(gpu.stats.gpu_ms)
    cpu_t = []
    with src.bgra() as arr:
        arr = np.array(arr)
    for _ in range(5):
        t = time.perf_counter(); cpu.render(arr, regs, specs, assets, time.monotonic()); cpu_t.append((time.perf_counter() - t) * 1000)
    print(f"\n1080p, 5 layers (2 images, 1 video, blur, pixelate): Metal wall median {np.median(wall):.2f} ms "
          f"(p95 {np.percentile(wall, 95):.2f}), GPU {np.median(gpu_ms):.2f} ms; CPU backend {np.median(cpu_t):.1f} ms")
    assert np.median(wall) < 16.7          # fits a 60 fps frame budget


def test_fallback_to_cpu_when_metal_fails(comps, scene, monkeypatch):
    lib, cpu, gpu = comps
    a = lib.import_file(DATA / "logo_alpha.png")
    roi = new_rect("R", 0.1, 0.1, 0.5, 0.5)
    regs = resolve([roi], [], 1280, 720)
    spec = {roi.id: ReplacementSpec(enabled=True, asset_id=a.id)}

    def boom(*a, **k):
        raise RuntimeError("simulated GPU fault")
    monkeypatch.setattr(gpu.metal.ctx, "render", boom)
    out = gpu.render(scene, regs, spec, {a.id: a})
    assert gpu.active.name == "cpu" and "simulated GPU fault" in gpu.backend_error
    assert diff(out, cpu.render(scene, regs, spec, {a.id: a}))[0] == 0


def test_concurrent_renders_are_serialised(comps, scene):
    import threading
    lib, cpu, gpu = comps
    a = lib.import_file(DATA / "logo_alpha.png")
    roi = new_rect("R", 0.1, 0.1, 0.6, 0.6)
    regs = resolve([roi], [], 1280, 720)
    spec = {roi.id: ReplacementSpec(enabled=True, asset_id=a.id)}
    ref = gpu.render(scene, regs, spec, {a.id: a})
    errors, outs = [], []

    def worker():
        try:
            for _ in range(20):
                outs.append(gpu.render(scene, regs, spec, {a.id: a}))
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors and len(outs) == 60 and all(np.array_equal(o, ref) for o in outs)
    assert gpu.active.name == "metal"
