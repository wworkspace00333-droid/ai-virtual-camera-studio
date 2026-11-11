"""ROI model, geometry, mask and resolver tests."""
import math

import numpy as np
import pytest

from vcstudio.ai.tracking import TrackedObject
from vcstudio.roi.geometry import (clip_polygon, is_convex, overlap_fraction, point_in_polygon, points_in_polygon,
                                   polygon_area, polygon_mask, rasterize)
from vcstudio.roi.model import ROI, ROIError, new_polygon, new_rect, rect_corners, rois_from_profile
from vcstudio.roi.resolver import RoiService, resolve

W, H = 1920, 1080
STAR = [[0.5, 0.1], [0.6, 0.4], [0.9, 0.4], [0.65, 0.6], [0.75, 0.9], [0.5, 0.7], [0.25, 0.9], [0.35, 0.6],
        [0.1, 0.4], [0.4, 0.4]]


def obj(tid, label, x0, y0, x1, y1, conf=0.9, state="tracked"):
    return TrackedObject(tid, label, conf, x0, y0, x1, y1, state, 1.0, 0.0)


# ------------------------------------------------------------------ model
def test_roundtrip_and_validation():
    r = new_rect("A", 0.1, 0.2, 0.4, 0.6)
    r2 = ROI.from_dict(r.to_dict())
    assert r2 == r
    assert math.isclose(r.cx, 0.25) and math.isclose(r.h, 0.4)
    with pytest.raises(ROIError):
        ROI(mode="bogus").validate()
    with pytest.raises(ROIError):
        new_polygon("bad", [[0, 0], [1, 1]])
    with pytest.raises(ROIError):
        new_polygon("flat", [[0, 0], [0.5, 0.5], [1, 1]])   # zero area


def test_invalid_rois_skipped_from_profile():
    good = new_rect("ok", 0, 0, 0.5, 0.5).to_dict()
    rois = rois_from_profile({"rois": [good, {"kind": "triangle"}, {"mode": "manual", "w": -1}]})
    assert [r.name for r in rois] == ["ok"]


def test_rotation_is_in_pixel_space():
    r = ROI(cx=0.5, cy=0.5, w=0.25, h=0.25 * W / H, rotation=45)   # a 480x480 px square
    p = r.polygon_px(W, H)
    sides = [np.linalg.norm(p[i] - p[(i + 1) % 4]) for i in range(4)]
    assert np.allclose(sides, 480, atol=1e-6)
    assert np.isclose(np.dot(p[1] - p[0], p[2] - p[1]), 0, atol=1e-6)   # right angles survive


def test_translate_and_bbox():
    r = new_polygon("p", [[0.1, 0.1], [0.3, 0.1], [0.2, 0.3]])
    r.translate(0.1, 0.0)
    assert np.allclose(r.bbox_px(W, H), (0.2 * W, 0.1 * H, 0.4 * W, 0.3 * H))


# ------------------------------------------------------------------ geometry
def test_rasterize_rotated_rect_exact_area():
    r = ROI(cx=0.5, cy=0.5, w=0.5, h=0.5, rotation=30)
    m = rasterize(r.polygon_px(W, H), W, H)
    assert abs(int((m > 0).sum()) - 960 * 540) < 0.002 * 960 * 540


def test_rasterize_matches_point_test_on_concave_polygon():
    p = new_polygon("star", STAR).polygon_px(W, H)
    m = rasterize(p, W, H)
    ys, xs = np.mgrid[0:H:5, 0:W:5]
    pts = np.stack([xs.ravel() + 0.5, ys.ravel() + 0.5], 1)
    assert ((m[ys.ravel(), xs.ravel()] > 0) == points_in_polygon(pts, p)).mean() == 1.0


def test_mask_clipped_at_frame_edges_and_cached():
    p = np.array([[-100, -100], [500, -100], [500, 500], [-100, 500]], float)
    m = polygon_mask(p, 640, 360)
    assert m.shape == (360, 640) and (m > 0).sum() == 500 * 360
    assert polygon_mask(p, 640, 360) is m           # cached
    assert not m.flags.writeable


def test_point_in_polygon_and_overlap():
    sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
    assert point_in_polygon(5, 5, sq) and not point_in_polygon(15, 5, sq)
    assert math.isclose(overlap_fraction((5, 0, 15, 10), sq), 0.5)
    star = new_polygon("s", STAR).polygon_px(W, H)
    assert not is_convex(star)
    assert 0.0 <= overlap_fraction((900, 400, 1000, 500), star) <= 1.0


def test_clip_polygon():
    a = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
    b = np.array([[5, 5], [15, 5], [15, 15], [5, 15]], float)
    assert math.isclose(polygon_area(clip_polygon(a, b)), 25)
    assert polygon_area(clip_polygon(a, b + 100)) == 0


def test_rect_corners_order():
    c = rect_corners(10, 10, 4, 2, 0)
    assert np.allclose(c, [[8, 9], [12, 9], [12, 11], [8, 11]])


# ------------------------------------------------------------------ resolver
def test_manual_region():
    r = new_rect("M", 0.1, 0.1, 0.3, 0.3)
    regs = resolve([r], [], W, H)
    assert len(regs) == 1 and not regs[0].is_object
    assert np.allclose(regs[0].bbox, (0.1 * W, 0.1 * H, 0.3 * W, 0.3 * H))


def test_disabled_and_priority_order():
    a = new_rect("A", 0, 0, 0.1, 0.1); a.priority = 5
    b = new_rect("B", 0, 0, 0.1, 0.1); b.priority = 1
    c = new_rect("C", 0, 0, 0.1, 0.1); c.enabled = False
    assert [g.roi_name for g in resolve([a, b, c], [], W, H)] == ["B", "A"]


def test_auto_region_follows_objects_with_class_filter():
    r = ROI(name="Auto", mode="auto", classes=["person"], padding=0.0)
    objs = [obj(2, "person", 100, 100, 200, 400), obj(1, "car", 500, 500, 900, 700), obj(3, "person", 1000, 100, 1100, 400)]
    regs = resolve([r], objs, W, H)
    assert [g.track_id for g in regs] == [2, 3]                      # stable ID order, car excluded
    assert np.allclose(regs[0].bbox, (100, 100, 200, 400))
    moved = [obj(2, "person", 150, 100, 250, 400)]
    assert np.allclose(resolve([r], moved, W, H)[0].bbox, (150, 100, 250, 400))


def test_auto_padding_max_objects_min_conf_and_coasting():
    r = ROI(mode="auto", padding=0.1, max_objects=1, min_confidence=0.5, include_coasting=False)
    objs = [obj(1, "dog", 100, 100, 200, 200, conf=0.6), obj(2, "dog", 300, 300, 400, 400, conf=0.9),
            obj(3, "dog", 600, 600, 700, 700, conf=0.3), obj(4, "dog", 800, 800, 900, 900, conf=0.95, state="coasting")]
    regs = resolve([r], objs, W, H)
    assert [g.track_id for g in regs] == [2]
    assert np.allclose(regs[0].bbox, (290, 290, 410, 410))


def test_hybrid_center_and_overlap_rules():
    area = new_rect("Zone", 0.0, 0.0, 0.5, 1.0); area.mode = "hybrid"
    inside = obj(1, "person", 100, 100, 300, 500)
    straddle = obj(2, "person", 900, 100, 1100, 500)      # centre at 1000 > 960 (outside), 30% overlap
    outside = obj(3, "person", 1500, 100, 1700, 500)
    regs = resolve([area], [inside, straddle, outside], W, H)
    assert [g.track_id for g in regs] == [1]
    area.inclusion, area.overlap_threshold = "overlap", 0.25
    assert [g.track_id for g in resolve([area], [inside, straddle, outside], W, H)] == [1, 2]


def test_hybrid_polygon_area_and_clip():
    zone = new_polygon("Tri", [[0.0, 0.0], [0.5, 0.0], [0.0, 1.0]]); zone.mode = "hybrid"; zone.clip_to_area = True
    zone.padding = 0
    o = obj(1, "apple", 400, 100, 900, 400)       # centre (650, 250) inside; right part outside
    regs = resolve([zone], [o], W, H)
    assert len(regs) == 1
    poly = regs[0].polygon
    assert len(poly) >= 3
    assert polygon_area(poly) < 0.9 * (500 * 300)           # clipped
    assert all(point_in_polygon(x - 1e-6 if x > 0 else x, y, zone.polygon_px(W, H)) or x <= 1e-6
               for x, y in poly[1:2])


def test_service_reload():
    svc = RoiService()
    svc.load_profile({"rois": [new_rect("X", 0, 0, 0.2, 0.2).to_dict()]})
    assert [r.name for r in svc.rois()] == ["X"]
    assert len(svc.resolve([], W, H)) == 1
