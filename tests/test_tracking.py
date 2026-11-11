"""Tracker tests against synthetic ground truth."""
import time

import numpy as np
import pytest

from vcstudio.ai.detection import Detection
from vcstudio.ai.tracking import ByteTracker, KalmanXYAH, TrackerConfig, iou_matrix, xyah_to_xyxy, xyxy_to_xyah

FPS_AI = 20


def det(label, x, y, w=200, h=400, conf=0.9, noise=0.0, rng=None):
    n = (lambda: rng.normal(0, noise)) if rng is not None and noise else (lambda: 0.0)
    return Detection(label, conf, x + n(), y + n(), x + w + n(), y + h + n())


def run(scene, seconds, fps=FPS_AI, cfg=None):
    """scene(t) -> list[(truth_id, Detection | None)]; returns tracker and per-step {truth_id: track_id}."""
    tr = ByteTracker(cfg or TrackerConfig())
    history = []
    for k in range(int(seconds * fps)):
        t = k / fps
        items = scene(t)
        tr.update([d for _, d in items if d is not None], t, (1920, 1080))
        snap = tr.snapshot(t)
        mapping = {}
        for gid, d in items:
            if d is None:
                continue
            same = [o for o in snap if o.label == d.label]
            best = max(same, key=lambda o: iou_matrix(np.array([[o.x0, o.y0, o.x1, o.y1]]),
                                                       np.array([[d.x0, d.y0, d.x1, d.y1]]))[0, 0], default=None)
            if best is not None:
                mapping[gid] = best.track_id
        history.append((t, mapping, snap))
    return tr, history


def id_switches(history) -> int:
    last, sw = {}, 0
    for _, m, _ in history:
        for gid, tid in m.items():
            if gid in last and last[gid] != tid:
                sw += 1
            last[gid] = tid
    return sw


# ------------------------------------------------------------------ geometry / filter
def test_xyah_roundtrip():
    b = (10.0, 20.0, 110.0, 220.0)
    assert np.allclose(xyah_to_xyxy(xyxy_to_xyah(b)), b)


def test_iou_matrix():
    m = iou_matrix(np.array([[0, 0, 10, 10]]), np.array([[0, 0, 10, 10], [5, 0, 15, 10], [20, 20, 30, 30]]))
    assert np.allclose(m, [[1.0, 1 / 3, 0.0]])


def test_kalman_estimates_velocity_in_px_per_second():
    kf = KalmanXYAH(0.3)
    m, P = kf.initiate(xyxy_to_xyah((0, 0, 100, 200)))
    for k in range(1, 40):
        t = k / 20
        m, P = kf.predict(m, P, 1 / 20)
        m, P = kf.update(m, P, xyxy_to_xyah((300 * t, 0, 300 * t + 100, 200)))
    assert abs(m[4] - 300) < 15, m[4]


# ------------------------------------------------------------------ association behaviour
def test_crossing_same_class_no_id_switch():
    rng = np.random.default_rng(1)
    scene = lambda t: [("A", det("person", 100 + 400 * t, 300, noise=3, rng=rng)),
                       ("B", det("person", 1600 - 400 * t, 320, noise=3, rng=rng))]
    tr, h = run(scene, 4.0)
    assert id_switches(h) == 0
    assert tr.stats.ids_created == 2


def test_occlusion_within_buffer_recovers_same_id():
    cfg = TrackerConfig(track_buffer_s=1.5, coast_s=0.3)
    scene = lambda t: [("A", None if 1.0 <= t < 1.8 else det("person", 200 + 150 * t, 300))]
    tr, h = run(scene, 3.0, cfg=cfg)
    ids = {tid for _, m, _ in h for tid in m.values()}
    assert ids == {1} and tr.stats.recovered == 1
    # coasting visible briefly after occlusion starts, hidden later, back after re-detection
    vis = {round(t, 2): [o.state for o in s] for t, _, s in h}
    assert vis[1.1] == ["coasting"]
    assert vis[1.5] == []
    assert vis[2.0] == ["tracked"]


def test_occlusion_beyond_buffer_gets_new_id():
    cfg = TrackerConfig(track_buffer_s=0.5)
    scene = lambda t: [("A", None if 1.0 <= t < 2.0 else det("person", 300, 300))]
    tr, h = run(scene, 3.0, cfg=cfg)
    assert tr.stats.ids_created == 2 and tr.stats.recovered == 0


def test_low_score_detections_keep_track():
    """Second ByteTrack stage: score dips below high threshold (e.g. motion blur) -> same track."""
    scene = lambda t: [("A", det("car", 100 + 200 * t, 400, conf=0.3 if 1.0 <= t < 2.0 else 0.9))]
    tr, h = run(scene, 3.0)
    assert tr.stats.ids_created == 1 and id_switches(h) == 0
    assert all(s and s[0].state == "tracked" for t, _, s in h if 1.0 <= t < 2.0)


def test_single_frame_false_positive_not_confirmed():
    scene = lambda t: [("A", det("person", 300, 300))] + ([("FP", det("dog", 1200, 600, 100, 100, conf=0.95))]
                                                          if abs(t - 1.0) < 1e-6 else [])
    tr, h = run(scene, 2.0)
    assert all(all(o.label != "dog" for o in s) for _, _, s in h)
    assert tr.stats.active == 1


def test_class_aware_matching():
    """A different-class detection on top of a lost person must not inherit the person's ID."""
    def scene(t):
        if t < 1.0:
            return [("P", det("person", 500, 300))]
        return [("C", det("car", 505, 305))]
    tr, h = run(scene, 2.0)
    ids_p = {m["P"] for _, m, _ in h if "P" in m}
    ids_c = {m["C"] for _, m, _ in h if "C" in m}
    assert ids_p and ids_c and not (ids_p & ids_c)


def test_many_objects_same_class_stable_ids():
    rng = np.random.default_rng(7)
    starts = [(100 + 350 * (i % 5), 100 + 450 * (i // 5)) for i in range(10)]
    vel = rng.uniform(-60, 60, size=(10, 2))
    scene = lambda t: [(i, det("person", x + vx * t, y + vy * t, 120, 300, noise=2, rng=rng))
                       for i, ((x, y), (vx, vy)) in enumerate(zip(starts, vel))]
    tr, h = run(scene, 5.0)
    assert id_switches(h) == 0 and tr.stats.ids_created == 10
    assert len(h[-1][2]) == 10


# ------------------------------------------------------------------ temporal behaviour
def test_interpolation_between_inference_frames():
    """AI at 20 fps, queried at 60 fps output timestamps: boxes follow true motion."""
    tr = ByteTracker(TrackerConfig(smoothing=0.3))
    errs = []
    for k in range(60):
        t = k / 20
        x = 100 + 450 * t
        tr.update([det("person", x, 300)], t, (1920, 1080))
        if k > 10:
            for j in (1, 2):
                tt = t + j / 60
                o = tr.snapshot(tt)[0]
                errs.append(abs(o.x0 - (100 + 450 * tt)))
    assert np.mean(errs) < 4.0 and max(errs) < 10.0, (np.mean(errs), max(errs))


def test_smoothing_reduces_jitter():
    rng = np.random.default_rng(3)
    for s, max_ratio in ((0.5, 0.75), (0.9, 0.6)):
        tr = ByteTracker(TrackerConfig(smoothing=s))
        raw, out = [], []
        for k in range(200):
            t = k / 30
            d = det("person", 500, 300, noise=6, rng=rng)
            tr.update([d], t, (1920, 1080))
            if k > 20:
                raw.append(d.x0)
                out.append(tr.snapshot(t)[0].x0)
        assert np.std(out) < max_ratio * np.std(raw), (s, np.std(out), np.std(raw))


def test_stale_when_ai_stops():
    tr = ByteTracker(TrackerConfig(coast_s=0.3, max_extrapolate_s=0.3))
    for k in range(10):
        tr.update([det("person", 300 + 10 * k, 300)], k / 20, (1920, 1080))
    t_last = 9 / 20
    assert tr.snapshot(t_last + 0.2)
    assert tr.snapshot(t_last + 2.0) == []
    # extrapolation is capped
    a = tr.snapshot(t_last + 0.3)[0].x0
    b = tr.snapshot(t_last + 0.8)[0].x0
    assert abs(a - b) < 1e-6


def test_time_going_backwards_is_safe():
    tr = ByteTracker()
    tr.update([det("person", 100, 100)], 1.0, (1920, 1080))
    tr.update([det("person", 102, 100)], 0.9, (1920, 1080))
    assert len(tr.snapshot(1.0)) == 1


def test_boxes_clipped_to_frame():
    tr = ByteTracker()
    tr.update([Detection("person", 0.9, 1800, 900, 1920, 1080)], 0.0, (1920, 1080))
    o = tr.snapshot(0.0)[0]
    assert 0 <= o.x0 < o.x1 <= 1920 and 0 <= o.y0 < o.y1 <= 1080


def test_update_cost_small():
    rng = np.random.default_rng(0)
    tr = ByteTracker()
    boxes = [(rng.uniform(0, 1700), rng.uniform(0, 800)) for _ in range(50)]
    for k in range(30):
        tr.update([det("person", x + k, y, 100, 200, conf=0.8) for x, y in boxes], k / 30, (1920, 1080))
    t0 = time.perf_counter()
    for k in range(30, 60):
        tr.update([det("person", x + k, y, 100, 200, conf=0.8) for x, y in boxes], k / 30, (1920, 1080))
    assert (time.perf_counter() - t0) / 30 * 1000 < 5.0
