"""One-click face hiding: the quick face region and effect-only replacement."""
from vcstudio.core.profiles import default_profile_body
from vcstudio.replacement.assets import AssetLibrary
from vcstudio.replacement.service import ReplacementService
from vcstudio.roi.model import QUICK_FACE_ID, rois_from_profile, set_quick_face
from vcstudio.roi.resolver import resolve
from vcstudio.ai.tracking import TrackedObject


def test_quick_face_toggle_roundtrip():
    b = default_profile_body()
    b["ai"]["face_detection"] = None
    set_quick_face(b, True)
    set_quick_face(b, True)            # idempotent: one region
    rois = rois_from_profile(b)
    assert [r.id for r in rois] == [QUICK_FACE_ID]
    assert rois[0].mode == "auto" and rois[0].classes == ["face"]
    assert b["ai"]["face_detection"] == "vision"
    set_quick_face(b, False)
    assert rois_from_profile(b) == []


def test_effect_region_without_assets_is_active(paths):
    # Regression: effect regions were ignored unless some image/video asset existed.
    svc = ReplacementService(AssetLibrary(paths.assets_dir), "cpu")
    b = default_profile_body()
    svc.load_profile(b)
    assert not svc.active()
    set_quick_face(b, True)
    svc.load_profile(b)
    assert svc.active()


def test_quick_face_region_follows_face_only():
    b = default_profile_body(); set_quick_face(b, True)
    face = TrackedObject(2, "face", 0.9, 400, 300, 600, 550, "tracked", 1.0, 0.0)
    person = TrackedObject(1, "person", 0.95, 100, 200, 1000, 1080, "tracked", 1.0, 0.0)
    regs = resolve(rois_from_profile(b), [person, face], 1920, 1080)
    assert len(regs) == 1 and regs[0].label == "face"
    x0, y0, x1, y1 = regs[0].bbox
    assert x0 < 400 and y0 < 300 and x1 > 600 and y1 > 550     # padded around the face
