"""Stage 11: memory-leak regression, GPU fast paths vs CPU, mask caching, adaptive Maximum Quality."""
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from vcstudio.ai.model_manager import REPO_MODELS, ModelManager
from vcstudio.core.frame import Frame
from vcstudio.core.performance import phys_footprint
from vcstudio.core.pixelbuffer import pixel_buffer_for
from vcstudio.native import metal

DATA = Path(__file__).parent / "data"
needs_metal = pytest.mark.skipif(not metal.available(), reason="libVCSNative not built")
needs_seg = pytest.mark.skipif(not (REPO_MODELS / "yolo11n-seg.mlpackage").exists(), reason="no seg model")


@pytest.fixture(scope="module")
def scene_pb():
    from vcstudio.core.bench_source import bus_scene
    s = bus_scene()
    return s, Frame(1, 0, 0, s.shape[1], s.shape[0], pixel_buffer=pixel_buffer_for(Frame(0, 0, 0, s.shape[1], s.shape[0], array=s)))


def test_phys_footprint_available():
    fp = phys_footprint()
    assert isinstance(fp, int) and fp > 1_000_000


@needs_seg
@needs_metal
def test_no_autorelease_leak_in_worker_threads(scene_pb, tmp_path):
    """Regression: Core ML + Metal work on plain Python threads leaked ~3 MB/inference (MLMultiArray)."""
    import objc
    from vcstudio.ai.segmentation import SegmentationDetector
    _, frame = scene_pb
    mm = ModelManager(tmp_path)
    det = SegmentationDetector(mm.load(mm.resolve("yolo11n-seg", "segment")))
    for _ in range(5):
        det.detect(frame, 0.25, 0.5)
    growth = {}

    def work():
        before = phys_footprint()
        for _ in range(120):
            with objc.autorelease_pool():          # what the engine loop does per iteration
                det.detect(frame, 0.25, 0.5)
        growth["mb"] = (phys_footprint() - before) / 2**20
    t = threading.Thread(target=work); t.start(); t.join()
    assert growth["mb"] < 60, growth                 # without pools: ~400 MB for 120 inferences


@needs_seg
@needs_metal
def test_gpu_letterbox_matches_cpu(scene_pb, tmp_path):
    from vcstudio.ai.segmentation import SegmentationDetector
    arr, frame = scene_pb
    mm = ModelManager(tmp_path)
    det = SegmentationDetector(mm.load(mm.resolve("yolo11n-seg", "segment")))
    gpu, _ = det.detect(frame, 0.35, 0.5)                                   # pixel buffer -> GPU letterbox
    cpu, _ = det.detect(Frame(1, 0, 0, arr.shape[1], arr.shape[0], array=arr), 0.35, 0.5)   # array -> CPU path
    # the GPU letterbox is bit-identical to the CPU one, so detections must match exactly
    key = lambda L: sorted((d.label, round(d.confidence, 3), round(d.x0), round(d.y0), round(d.x1), round(d.y1)) for d in L)
    assert key(gpu) == key(cpu)
    lb_gpu = det._letterbox_gpu(frame)[0]
    with Frame(0, 0, 0, 640, 640, pixel_buffer=lb_gpu).bgra() as a:
        g = np.array(a)
    c, *_ = det._letterbox(arr)
    assert np.array_equal(g[..., :3], c[..., :3])          # regression: MPS clipRect + translate double offset


@needs_metal
def test_gpu_scale_places_rect_exactly():
    import Quartz as Q
    from vcstudio.core.pixelbuffer import PixelBufferPool
    from vcstudio.native.metal import scaler_context
    src = np.zeros((100, 200, 4), np.uint8); src[..., 2] = 255; src[..., 3] = 255
    sp = pixel_buffer_for(Frame(0, 0, 0, 200, 100, array=src))
    err, dst = Q.CVPixelBufferPoolCreatePixelBuffer(None, PixelBufferPool(400, 300)._pool, None)
    scaler_context().scale(sp, dst, 50, 70, 100, 50, 0.0)
    with Frame(0, 0, 0, 400, 300, pixel_buffer=dst).bgra() as a:
        ys, xs = np.where(a[..., 2] > 128)
        assert (xs.min(), xs.max(), ys.min(), ys.max()) == (50, 149, 70, 119)


@needs_metal
def test_gpu_thumbnail_matches_cpu_decimation(scene_pb):
    from vcstudio.ui.video_view import _gpu_thumbnail
    arr, frame = scene_pb
    g = _gpu_thumbnail(frame, 640)
    c = Frame(0, 0, 0, arr.shape[1], arr.shape[0], array=arr).downscaled_copy(640)
    assert g.shape == c.shape
    assert np.abs(g.astype(int) - c.astype(int))[..., :3].mean() < 12     # bilinear vs decimation (both previews)


@needs_metal
def test_object_mask_refined_once_per_ai_update(tmp_path):
    import cv2
    from vcstudio.ai.tracking import TrackedObject
    from vcstudio.replacement.assets import AssetLibrary
    from vcstudio.replacement.compositor import Compositor
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI
    from vcstudio.roi.resolver import resolve
    lib = AssetLibrary(tmp_path / "a")
    a = lib.import_file(DATA / "logo_alpha.png")
    comp = Compositor(lib, "metal")
    roi = ROI(name="O", mode="auto", padding=0)
    mask = np.zeros((200, 100), np.uint8); cv2.ellipse(mask, (50, 100), (40, 90), 0, 0, 360, 255, -1)
    spec = {roi.id: ReplacementSpec(enabled=True, mode="object", asset_id=a.id, feather_px=3)}
    frame = np.zeros((720, 1280, 4), np.uint8); frame[..., 3] = 255
    uploads = []
    real = comp.metal.ctx.upload
    comp.metal.ctx.upload = lambda s, arr: (uploads.append(arr.shape), real(s, arr))[1]
    for k in range(10):                      # same AI mask, box predicted to move every frame
        obj = TrackedObject(1, "person", 0.9, 400 + 3 * k, 200, 500 + 3 * k, 400, "tracked", 1, 0, mask=mask)
        comp.render(frame, resolve([roi], [obj], 1280, 720), spec, {a.id: a})
    mask_uploads = [s for s in uploads if len(s) == 2]
    assert len(mask_uploads) == 1, uploads   # refined + uploaded once, GPU resamples into each new box
    comp.close()


def test_model_listing_is_cached(tmp_path, monkeypatch):
    mm = ModelManager(tmp_path)
    mm.available()
    calls = []
    import vcstudio.ai.model_manager as m
    real = m.ModelInfo.from_package
    monkeypatch.setattr(m.ModelInfo, "from_package", classmethod(lambda cls, p: (calls.append(p), real(p))[1]))
    for _ in range(20):
        mm.available()
    assert calls == []


@pytest.mark.skipif(not (REPO_MODELS / "yolo11m.mlpackage").exists(), reason="yolo11m not converted")
def test_max_quality_steps_down_when_too_slow():
    from vcstudio.ai.detection import DetectionConfig
    from vcstudio.ai.engine import AIEngine, MQ_SAMPLES
    from vcstudio.core.capture_supervisor import CaptureSupervisor
    from vcstudio.core.video_capture import TestPatternSource
    cap = CaptureSupervisor()
    eng = AIEngine(cap, ModelManager(Path(__import__("tempfile").mkdtemp())))
    eng.configure(DetectionConfig(), "max_quality")
    cap.start(lambda: TestPatternSource(640, 360, 60))
    eng.start()
    try:
        end = time.monotonic() + 15
        while time.monotonic() < end and eng.stats().model != "yolo11m":
            time.sleep(0.05)
        assert eng.stats().model == "yolo11m"                       # largest first
        det = eng._detector
        real = det.detect
        def slow(*a, **k):                                           # simulate a model too slow for this Mac
            dets, ms = real(*a, **k)
            return dets, ms + 30.0
        det.detect = slow
        end = time.monotonic() + 15
        while time.monotonic() < end and eng.stats().model == "yolo11m":
            time.sleep(0.05)
        assert eng.stats().model == "yolo11s"                        # stepped down one size
        assert "yolo11m" in eng.mq_rejected["detect"] and "budget" in eng.stats().quality_note
    finally:
        eng.stop(); cap.stop()


@needs_metal
def test_release_resources_frees_gpu_slots(tmp_path):
    from vcstudio.replacement.assets import AssetLibrary
    from vcstudio.replacement.compositor import Compositor
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import new_rect
    from vcstudio.roi.resolver import resolve
    lib = AssetLibrary(tmp_path / "a")
    a = lib.import_file(DATA / "logo_alpha.png")
    comp = Compositor(lib, "metal")
    roi = new_rect("R", 0.1, 0.1, 0.5, 0.5)
    comp.render(np.zeros((360, 640, 4), np.uint8), resolve([roi], [], 640, 360),
                {roi.id: ReplacementSpec(enabled=True, asset_id=a.id)}, {a.id: a})
    assert comp.metal._slots
    comp.release_resources()
    assert not comp.metal._slots and not comp.metal._pools
    comp.close()
