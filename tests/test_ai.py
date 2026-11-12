"""AI detection tests (Core ML via Vision). Skipped if models are not converted yet."""
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from vcstudio.ai.detection import Detection, DetectionConfig, filter_detections
from vcstudio.ai.model_manager import ModelError, ModelManager, REPO_MODELS
from vcstudio.ai.testimages import load_bgra
from vcstudio.core.capture_supervisor import CaptureSupervisor
from vcstudio.core.frame import Frame, host_time_s
from vcstudio.core.video_capture import TestPatternSource

HAVE_MODEL = (REPO_MODELS / "yolo11n.mlpackage").exists()
needs_model = pytest.mark.skipif(not HAVE_MODEL, reason="run scripts/convert_models.py first")

# Ultralytics PyTorch yolo11n reference on bus.jpg (conf >= 0.35), xyxy pixels
TORCH_REF = [("bus", 0.94, [4, 229, 796, 728]), ("person", 0.89, [671, 395, 810, 879]),
             ("person", 0.88, [47, 400, 239, 904]), ("person", 0.86, [223, 409, 344, 860])]


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


@pytest.fixture(scope="module")
def bus():
    a = load_bgra("tests/data/bus.jpg")
    return Frame(1, 0, 0, a.shape[1], a.shape[0], array=a)


@pytest.fixture
def mm(tmp_path):
    return ModelManager(tmp_path / "models")


# ------------------------------------------------------------------ pure logic
def test_filter_detections():
    d = [Detection("person", 0.9, 0, 0, 1, 1), Detection("car", 0.5, 0, 0, 1, 1), Detection("apple", 0.2, 0, 0, 1, 1)]
    assert [x.label for x in filter_detections(d, DetectionConfig(confidence=0.3))] == ["person", "car"]
    assert [x.label for x in filter_detections(d, DetectionConfig(confidence=0.1, include=["apple"]))] == ["apple"]
    assert [x.label for x in filter_detections(d, DetectionConfig(confidence=0.1, exclude=["car"]))] == ["person", "apple"]


def test_config_from_profile():
    from vcstudio.core.profiles import default_profile_body
    b = default_profile_body()
    b["detection"].update(confidence=0.6, include_classes=["car"], frequency_mode="fixed", fixed_fps=7)
    b["ai"]["face_detection"] = "vision"
    c = DetectionConfig.from_profile(b)
    assert (c.confidence, c.include, c.frequency_mode, c.fixed_fps, c.face_detection) == (0.6, ["car"], "fixed", 7.0, True)


# ------------------------------------------------------------------ model manager
@needs_model
def test_model_manager_resolve(mm):
    assert mm.resolve("auto").name == "yolo11n"
    if (REPO_MODELS / "yolo11s.mlpackage").exists():
        assert mm.resolve("auto", prefer_large=True).name == mm.ranked("detect")[0].name
    with pytest.raises(ModelError):
        mm.resolve("nope")
    info = mm.available()["yolo11n"]
    assert len(info.classes) == 80 and "apple" in info.classes and "banana" in info.classes


@needs_model
def test_compile_cache_reused(mm):
    info = mm.resolve("yolo11n")
    p1 = mm._compiled_path(info)
    p2 = mm._compiled_path(info)
    assert p1 == p2 and p1.exists()


# ------------------------------------------------------------------ inference accuracy
@needs_model
@pytest.mark.parametrize("cu", ["all", "cpu_and_ne", "cpu_and_gpu", "cpu_only"])
def test_matches_pytorch_reference(mm, bus, cu):
    from vcstudio.ai.coreml_engine import CoreMLDetector
    det = CoreMLDetector(mm.load(mm.resolve("yolo11n"), cu))
    dets, ms = det.detect(bus, 0.35, 0.5)
    for label, conf, box in TORCH_REF:
        best = max((d for d in dets if d.label == label), key=lambda d: iou(box, [d.x0, d.y0, d.x1, d.y1]))
        assert iou(box, [best.x0, best.y0, best.x1, best.y1]) > 0.9, (label, box, best)
        assert abs(best.confidence - conf) < 0.08
    assert all(0 <= d.x0 < d.x1 <= bus.width and 0 <= d.y0 < d.y1 <= bus.height for d in dets)


@needs_model
def test_thresholds_applied_in_model(mm, bus):
    from vcstudio.ai.coreml_engine import CoreMLDetector
    det = CoreMLDetector(mm.load(mm.resolve("yolo11n")))
    lo, _ = det.detect(bus, 0.10, 0.5)
    hi, _ = det.detect(bus, 0.85, 0.5)
    assert len(hi) < len(lo) and all(d.confidence >= 0.85 for d in hi)


def test_face_detection_finds_faces(bus):
    from vcstudio.ai.coreml_engine import VisionFaceDetector
    faces, _ = VisionFaceDetector().detect(bus)
    assert len(faces) >= 1 and all(f.label == "face" for f in faces)
    # faces lie inside a person box region (upper half of image)
    assert all(380 < f.center[1] < 520 for f in faces)


# ------------------------------------------------------------------ engine
class ImageSource(TestPatternSource):
    """Delivers a fixed image (with a changing corner pixel so it is not 'frozen')."""
    def __init__(self, img, fps=60):
        super().__init__(img.shape[1], img.shape[0], fps)
        self.img = img

    def _run(self, on_frame):
        period, seq = 1 / self.fps, 0
        while not self._stop.is_set():
            seq += 1
            a = self.img.copy(); a[0, 0, 0] = seq % 255
            now = host_time_s()
            on_frame(Frame(seq, now, now, self.width, self.height, array=a, source="image"))
            self._stop.wait(period)


@pytest.fixture
def pipeline(tmp_path):
    from vcstudio.ai.engine import AIEngine
    cap = CaptureSupervisor(missing_timeout_ms=1000)
    eng = AIEngine(cap, ModelManager(tmp_path / "m"))
    yield cap, eng
    eng.stop(); cap.stop()


def wait_for(pred, t=10):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@needs_model
def test_engine_publishes_detections(pipeline):
    cap, eng = pipeline
    img = load_bgra("tests/data/bus.jpg")
    cap.start(lambda: ImageSource(img, 30))
    eng.configure(DetectionConfig(confidence=0.35, frequency_mode="fixed", fixed_fps=20), "balanced")
    eng.start()
    assert wait_for(lambda: eng.results.peek()[0] is not None)
    res = eng.results.peek()[0]
    labels = sorted(d.label for d in res.detections)
    assert labels.count("person") >= 3 and "bus" in labels
    assert (res.width, res.height) == (810, 1080)


@needs_model
def test_rate_limit_and_capture_unaffected(pipeline):
    cap, eng = pipeline
    cap.start(lambda: TestPatternSource(1920, 1080, 60))
    eng.configure(DetectionConfig(frequency_mode="fixed", fixed_fps=10), "balanced")
    eng.start()
    assert wait_for(lambda: eng.stats().inferences > 5)
    time.sleep(2.0)
    s = eng.stats()
    assert 8 <= s.achieved_fps <= 12, s.achieved_fps
    # Real-time assertion: the Python test-pattern source itself costs CPU, and the suite shares the
    # machine; 50 fps (of 60) still proves AI does not starve capture (unthrottled starvation drops far lower).
    assert cap.stats().measured_fps >= 50, cap.stats().measured_fps


@needs_model
def test_max_quality_uses_large_model_unthrottled(pipeline):
    if not (REPO_MODELS / "yolo11s.mlpackage").exists():
        pytest.skip("yolo11s not converted")
    cap, eng = pipeline
    cap.start(lambda: TestPatternSource(1280, 720, 60))
    eng.configure(DetectionConfig(), "max_quality")
    eng.start()
    assert wait_for(lambda: eng.stats().inferences > 20)
    s = eng.stats()
    largest = eng.models.ranked("detect")[0].name          # adaptive: largest model sustaining the budget
    assert s.model in (largest, eng.models.ranked("detect")[1].name) and s.target_fps is None


@needs_model
def test_failure_isolation_and_recovery(pipeline):
    cap, eng = pipeline
    cap.start(lambda: TestPatternSource(640, 360, 60))
    eng.configure(DetectionConfig(frequency_mode="fixed", fixed_fps=30), "balanced")
    eng.MAX_CONSECUTIVE_ERRORS = 3
    eng.start()
    assert wait_for(lambda: eng.stats().state == "running")
    det = eng._detector
    real = det.detect
    fails = {"n": 0}

    def broken(*a, **k):
        fails["n"] += 1
        raise RuntimeError("simulated ANE failure")
    det.detect = broken
    assert wait_for(lambda: eng.stats().state == "degraded", 5)
    assert cap.stats().state == "running" and cap.stats().measured_fps > 50   # capture unaffected
    assert wait_for(lambda: eng.stats().state == "running", 10)               # model reloaded
    assert eng._detector is not det and fails["n"] >= 3
    det.detect = real


@needs_model
def test_disabled_publishes_nothing(pipeline):
    cap, eng = pipeline
    cap.start(lambda: TestPatternSource(640, 360, 60))
    eng.configure(DetectionConfig(enabled=False), "balanced")
    eng.start()
    assert wait_for(lambda: eng.stats().state == "disabled")
    time.sleep(0.3)
    assert eng.results.peek()[0] is None and eng.stats().inferences == 0


@needs_model
def test_models_unloaded_on_stop(pipeline):
    cap, eng = pipeline
    cap.start(lambda: TestPatternSource(640, 360, 60))
    eng.start()
    assert wait_for(lambda: eng.stats().inferences > 0)
    assert eng.models.loaded()
    eng.stop()
    assert eng.models.loaded() == []


@needs_model
def test_engine_tracks_with_stable_ids(pipeline):
    cap, eng = pipeline
    img = load_bgra("tests/data/bus.jpg")
    cap.start(lambda: ImageSource(img, 30))
    eng.configure(DetectionConfig(confidence=0.35, frequency_mode="fixed", fixed_fps=20), "balanced")
    eng.start()
    assert wait_for(lambda: eng.stats().inferences > 25)
    res = eng.results.peek()[0]
    ids = {o.track_id for o in res.tracked}
    assert len([o for o in res.tracked if o.label == "person"]) >= 3
    time.sleep(1.0)
    res2 = eng.results.peek()[0]
    assert {o.track_id for o in res2.tracked} == ids          # no ID churn on a static scene
    assert eng.stats().track_ids_created == len(ids)
    out = eng.tracked_at(host_time_s())                        # query at an output time
    assert {o.track_id for o in out} == ids


HAVE_SEG = (REPO_MODELS / "yolo11n-seg.mlpackage").exists()


@pytest.mark.skipif(not HAVE_SEG, reason="segmentation model not converted")
def test_segmentation_masks_follow_tracks_and_drive_object_replacement(pipeline, tmp_path):
    from vcstudio.replacement.assets import AssetLibrary
    from vcstudio.replacement.compositor import Compositor
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI
    from vcstudio.roi.resolver import resolve
    cap, eng = pipeline
    img = load_bgra("tests/data/bus.jpg")
    cap.start(lambda: ImageSource(img, 30))
    eng.configure(DetectionConfig(confidence=0.35, need_masks=True, frequency_mode="fixed", fixed_fps=20), "balanced")
    eng.start()
    assert wait_for(lambda: eng.stats().inferences > 10)
    assert eng.stats().model.endswith("-seg")       # auto picks the largest seg model that keeps up
    objs = eng.tracked_at(host_time_s())
    people = [o for o in objs if o.label == "person"]
    assert len(people) >= 3 and all(o.mask is not None for o in people)
    lib = AssetLibrary(tmp_path / "a")
    a = lib.import_file(Path("tests/data/logo_alpha.png"))
    roi = ROI(name="People", mode="auto", classes=["person"], padding=0.0)
    regs = resolve([roi], objs, 810, 1080)
    out = Compositor(lib).render(img, regs, {roi.id: ReplacementSpec(enabled=True, mode="object", asset_id=a.id,
                                                                   background=[255, 0, 255, 255])}, {a.id: a})
    o = max(people, key=lambda o: o.confidence)
    x0, y0, x1, y1 = (int(v) for v in (o.x0, o.y0, o.x1, o.y1))
    changed = (np.abs(out[y0:y1, x0:x1].astype(int) - img[y0:y1, x0:x1].astype(int)).sum(-1) > 30).mean()
    assert 0.3 < changed < 0.9          # silhouette, not the whole box


def test_face_detection_on_by_default():
    # COCO has no "face" class: without Vision faces a close-up shows only a frame-wide "person" box.
    from vcstudio.core.profiles import default_profile_body
    assert DetectionConfig.from_profile(default_profile_body()).face_detection is True
