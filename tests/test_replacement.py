"""Image replacement: assets, geometry, compositing modes, masks, isolation."""
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from vcstudio.ai.tracking import TrackedObject
from vcstudio.replacement.assets import AssetError, AssetLibrary, decode_image
from vcstudio.replacement.compositor import Compositor, asset_affine, region_frame
from vcstudio.replacement.spec import ReplacementSpec
from vcstudio.roi.model import ROI, new_rect
from vcstudio.roi.resolver import resolve

DATA = Path(__file__).parent / "data"
W, H = 1280, 720
GREEN = (40, 160, 40, 255)


@pytest.fixture
def lib(tmp_path):
    return AssetLibrary(tmp_path / "assets")


def solid_png(tmp_path, bgra, w=100, h=50, name="solid.png"):
    a = np.zeros((h, w, 4), np.uint8); a[:] = bgra
    p = tmp_path / name
    cv2.imwrite(str(p), a)
    return p


def frame():
    f = np.zeros((H, W, 4), np.uint8); f[:] = GREEN
    return f


def obj(tid, x0, y0, x1, y1, mask=None, label="person"):
    return TrackedObject(tid, label, 0.9, x0, y0, x1, y1, "tracked", 1.0, 0.0, mask=mask)


# ------------------------------------------------------------------ spec / assets
def test_spec_sanitises():
    s = ReplacementSpec.from_dict({"mode": "nope", "fit": "x", "opacity": 3, "crop": [0.5, 0.5, 0.5, 0.9],
                                   "background": [1, 2], "scale": -1, "junk": 1})
    assert (s.mode, s.fit, s.opacity, s.crop, s.background, s.scale) == ("rect", "fill", 1.0, [0, 0, 1, 1], [1, 2, 255, 255], 0.01)


def test_import_and_decode_alpha(lib):
    a = lib.import_file(DATA / "logo_alpha.png")
    assert (a.kind, a.width, a.height) == ("image", 400, 300)
    img = lib.image(a)
    assert tuple(img[0, 0]) == (0, 0, 0, 0)                 # transparent corner, premultiplied
    assert tuple(img[150, 30]) == (0, 0, 255, 255)          # opaque red
    b = lib.import_file(DATA / "bus.jpg")
    assert lib.image(b)[..., 3].min() == 255
    assert lib.path(a).exists() and lib.path(a).parent == lib.dir


def test_import_rejects_bad_files(lib, tmp_path):
    with pytest.raises(AssetError):
        lib.import_file(tmp_path / "x.txt") if (tmp_path / "x.txt").write_text("hi") else None
    bad = tmp_path / "broken.png"; bad.write_bytes(b"not an image")
    with pytest.raises(AssetError):
        lib.import_file(bad)
    assert not any(lib.dir.iterdir())                         # nothing left behind


def test_missing_asset_file(lib):
    a = lib.import_file(DATA / "logo_alpha.png")
    lib.path(a).unlink()
    with pytest.raises(AssetError):
        lib.image(a)


# ------------------------------------------------------------------ geometry
def region_for(roi):
    return resolve([roi], [], W, H)[0]


@pytest.mark.parametrize("fit,expect", [("fit", (400, 200)), ("fill", (600, 300)), ("stretch", (400, 300)), ("original", (100, 50))])
def test_fit_modes(fit, expect):
    roi = new_rect("R", 0.25, 0.25, 0.25 + 400 / W, 0.25 + 300 / H)
    M = asset_affine(100, 50, region_for(roi), ReplacementSpec(fit=fit))
    corners = np.array([[0, 0, 1], [100, 50, 1]]) @ M.T
    size = corners[1] - corners[0]
    assert np.allclose(size, expect, atol=1e-6)
    cx, cy, *_ = region_frame(region_for(roi))
    assert np.allclose(corners.mean(axis=0), (cx, cy), atol=1e-6)    # centred


def test_offset_scale_rotation_and_crop():
    roi = new_rect("R", 0.25, 0.25, 0.75, 0.75); roi.rotation = 90
    reg = region_for(roi)
    s = ReplacementSpec(fit="stretch", offset_x=0.25, crop=[0.0, 0.0, 0.5, 1.0])
    M = asset_affine(100, 50, reg, s)
    cx, cy, rw, rh, rot = region_frame(reg)
    assert abs(rot - 90) < 1e-9
    centre = np.array([25, 25, 1]) @ M.T          # centre of the cropped half
    # offset along the region's rotated x axis (pointing down for +90°)
    assert np.allclose(centre, (cx, cy + 0.25 * rw), atol=1e-6)


# ------------------------------------------------------------------ compositing
def test_rect_replacement_fills_background_and_keeps_outside(lib, tmp_path):
    a = lib.import_file(DATA / "logo_alpha.png")
    roi = new_rect("R", 0.25, 0.25, 0.75, 0.75)
    spec = ReplacementSpec(enabled=True, mode="rect", asset_id=a.id, fit="fit", background=[255, 0, 0, 255])
    src = frame()
    out = Compositor(lib).render(src, resolve([roi], [], W, H), {roi.id: spec}, {a.id: a})
    assert tuple(src[10, 10]) == GREEN and tuple(src[360, 640]) == GREEN       # input untouched
    assert tuple(out[10, 10]) == GREEN                                           # outside region
    assert tuple(out[185, 330]) == (255, 0, 0, 255)                              # region corner: transparent asset -> bg
    assert tuple(out[360, 500]) == (0, 0, 255, 255)                              # opaque asset area (red disc)


def test_overlay_keeps_original_under_transparency_and_opacity(lib, tmp_path):
    p = solid_png(tmp_path, (0, 0, 255, 255))
    a = lib.import_file(p)
    roi = new_rect("R", 0.25, 0.25, 0.75, 0.75)
    reg = resolve([roi], [], W, H)
    c = Compositor(lib)
    alpha = lib.import_file(DATA / "logo_alpha.png")
    out = c.render(frame(), reg, {roi.id: ReplacementSpec(enabled=True, mode="overlay", asset_id=alpha.id, fit="fit")}, {alpha.id: alpha})
    assert tuple(out[185, 330]) == GREEN                                         # transparent -> original
    out = c.render(frame(), reg, {roi.id: ReplacementSpec(enabled=True, mode="overlay", asset_id=a.id, opacity=0.5)}, {a.id: a})
    assert np.allclose(out[360, 640][:3], (20, 80, 148), atol=1)                 # 50/50 blend


def test_feather_softens_edge(lib, tmp_path):
    a = lib.import_file(solid_png(tmp_path, (255, 255, 255, 255)))
    roi = new_rect("R", 0.25, 0.25, 0.75, 0.75)
    reg = resolve([roi], [], W, H)
    hard = Compositor(lib).render(frame(), reg, {roi.id: ReplacementSpec(enabled=True, asset_id=a.id)}, {a.id: a})
    soft = Compositor(lib).render(frame(), reg, {roi.id: ReplacementSpec(enabled=True, asset_id=a.id, feather_px=12)}, {a.id: a})
    row_h, row_s = hard[360, 300:340, 0].astype(int), soft[360, 300:340, 0].astype(int)
    assert len(np.unique(row_h)) <= 3 and len(np.unique(row_s)) > 8


def test_priority_order_later_on_top(lib, tmp_path):
    red = lib.import_file(solid_png(tmp_path, (0, 0, 255, 255), name="r.png"))
    blue = lib.import_file(solid_png(tmp_path, (255, 0, 0, 255), name="b.png"))
    lo = new_rect("lo", 0.2, 0.2, 0.6, 0.6); lo.priority = 1
    hi = new_rect("hi", 0.4, 0.4, 0.8, 0.8); hi.priority = 2
    specs = {lo.id: ReplacementSpec(enabled=True, asset_id=red.id), hi.id: ReplacementSpec(enabled=True, asset_id=blue.id)}
    out = Compositor(lib).render(frame(), resolve([hi, lo], [], W, H), specs, {red.id: red, blue.id: blue})
    assert tuple(out[360, 640]) == (255, 0, 0, 255)                              # overlap -> higher priority
    assert tuple(out[200, 300]) == (0, 0, 255, 255)


def test_object_mode_uses_silhouette_not_box(lib, tmp_path):
    a = lib.import_file(solid_png(tmp_path, (255, 255, 255, 255)))
    roi = ROI(name="Auto", mode="auto", padding=0.0,
              replacement=ReplacementSpec(enabled=True, mode="object", asset_id=a.id).to_dict())
    mask = np.zeros((200, 100), np.uint8)
    cv2.ellipse(mask, (50, 100), (40, 90), 0, 0, 360, 255, -1)                   # ellipse silhouette
    regs = resolve([roi], [obj(1, 400, 200, 500, 400, mask)], W, H)
    c = Compositor(lib)
    out = c.render(frame(), regs, {roi.id: ReplacementSpec.from_dict(roi.replacement)}, {a.id: a})
    box = out[200:400, 400:500, 0]
    changed = (box > 200).mean()
    expected = (mask > 127).mean()                                                # ≈ 0.565
    assert abs(changed - expected) < 0.03, (changed, expected)                    # silhouette, not the box
    assert tuple(out[205, 405]) == GREEN                                          # box corner untouched


def test_object_mode_without_mask_is_skipped_unless_box_fallback(lib, tmp_path):
    a = lib.import_file(solid_png(tmp_path, (255, 255, 255, 255)))
    roi = ROI(name="Auto", mode="auto", padding=0.0)
    regs = resolve([roi], [obj(1, 400, 200, 500, 400, None)], W, H)
    c = Compositor(lib)
    out = c.render(frame(), regs, {roi.id: ReplacementSpec(enabled=True, mode="object", asset_id=a.id)}, {a.id: a})
    assert np.array_equal(out, frame()) and c.stats.skipped_no_mask == 1
    out = c.render(frame(), regs, {roi.id: ReplacementSpec(enabled=True, mode="object", asset_id=a.id, fallback_no_mask="box")}, {a.id: a})
    assert tuple(out[300, 450]) == (255, 255, 255, 255)


def test_failure_isolated_per_roi(lib, tmp_path):
    a = lib.import_file(solid_png(tmp_path, (255, 255, 255, 255)))
    bad = new_rect("Manual-object", 0.0, 0.0, 0.2, 0.2)                    # object mode on a manual ROI -> error
    good = new_rect("Good", 0.5, 0.5, 0.7, 0.7)
    specs = {bad.id: ReplacementSpec(enabled=True, mode="object", asset_id=a.id),
             good.id: ReplacementSpec(enabled=True, mode="rect", asset_id=a.id)}
    c = Compositor(lib)
    out = c.render(frame(), resolve([bad, good], [], W, H), specs, {a.id: a})
    assert bad.id in c.stats.failed_rois and good.id not in c.stats.failed_rois
    assert tuple(out[430, 770]) == (255, 255, 255, 255)
    c.render(frame(), resolve([bad, good], [], W, H), specs, {a.id: a})       # stays disabled, no re-raise
    c.reset_failures()
    assert c.stats.failed_rois == {}


def test_render_cost_1080p_region(lib):
    a = lib.import_file(DATA / "logo_alpha.png")
    roi = new_rect("R", 0.1, 0.1, 0.6, 0.6); roi.rotation = 15
    big = np.zeros((1080, 1920, 4), np.uint8)
    regs = resolve([roi], [], 1920, 1080)
    c = Compositor(lib)
    spec = {roi.id: ReplacementSpec(enabled=True, asset_id=a.id, feather_px=4)}
    c.render(big, regs, spec, {a.id: a})
    ts = []
    for _ in range(10):
        t = time.perf_counter(); c.render(big, regs, spec, {a.id: a}); ts.append((time.perf_counter() - t) * 1000)
    print("1080p rotated region render median ms", np.median(ts))
    assert np.median(ts) < 60
