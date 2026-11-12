"""Multi-object tracking: ByteTrack association on a continuous-time Kalman filter.

Why continuous time: detections arrive at the AI rate (10-30 fps) with frame timestamps, while
the compositor needs object boxes for every output frame (60 fps). The filter state carries
velocity in units/second, so `snapshot(t)` can predict every track to any output-frame
timestamp without mutating state (detection metadata interpolation/extrapolation).

ByteTrack (Zhang et al., 2022) association per update:
  1. predict all tracks to the detection frame time
  2. high-score detections  <-> tracked + lost tracks       (IoU, Hungarian)
  3. low-score detections   <-> still-unmatched tracked     (recovers occluded/blurred objects)
  4. unmatched tracked      -> lost (kept for track_buffer_s, velocity damped, can be re-matched)
  5. tentative tracks       <-> remaining high detections   (confirmed after `min_hits` consecutive hits)
  6. remaining high dets above new_track_thresh -> new tentative tracks
Matching is class-aware by default (a "car" detection never continues a "person" track).
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import numpy as np

try:
    from scipy.optimize import linear_sum_assignment
except ImportError:  # pragma: no cover - scipy is a declared dependency
    linear_sum_assignment = None

from vcstudio.ai.detection import Detection

REF_FPS = 30.0   # ByteTrack noise parameters are tuned per frame at ~30 fps


@dataclass
class TrackerConfig:
    enabled: bool = True
    high_thresh: float = 0.5        # first-association detection score
    low_thresh: float = 0.1         # detections below this are ignored entirely
    new_track_thresh: float = 0.6   # min score to start a new track
    match_iou: float = 0.2          # first association: min IoU
    low_match_iou: float = 0.5      # second association (low-score dets): min IoU
    tentative_match_iou: float = 0.3
    track_buffer_s: float = 1.5     # how long a lost track can be recovered with the same ID
    coast_s: float = 0.4            # how long a lost track stays visible (predicted) in output
    max_extrapolate_s: float = 0.3  # cap on forward prediction past the last update
    smoothing: float = 0.5          # 0 = follow detections tightly, 1 = very smooth
    class_aware: bool = True
    min_hits: int = 3               # consecutive detections before a new object is shown (filters flicker)

    @classmethod
    def from_profile(cls, body: dict) -> "TrackerConfig":
        t = body.get("tracking", {})
        c = cls()
        for k in c.__dataclass_fields__:
            if k in t:
                setattr(c, k, type(getattr(c, k))(t[k]))
        return c


# ---------------------------------------------------------------------------------------------
# Kalman filter in (cx, cy, aspect, h) with constant velocity, continuous time
# ---------------------------------------------------------------------------------------------

class KalmanXYAH:
    w_pos = 1.0 / 20
    w_vel = 1.0 / 160

    def __init__(self, smoothing: float = 0.5):
        self.r_scale = 0.5 + 4.0 * float(np.clip(smoothing, 0.0, 1.0)) ** 1.5
        self._H = np.eye(4, 8)

    def initiate(self, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mean = np.r_[z, np.zeros(4)]
        h = z[3]
        std = [2 * self.w_pos * h, 2 * self.w_pos * h, 1e-2, 2 * self.w_pos * h,
               10 * self.w_vel * h * REF_FPS, 10 * self.w_vel * h * REF_FPS, 1e-5 * REF_FPS, 10 * self.w_vel * h * REF_FPS]
        return mean, np.diag(np.square(std))

    def predict(self, mean, cov, dt: float, vel_decay_tau: float | None = None):
        if dt <= 0:
            return mean.copy(), cov.copy()
        F = np.eye(8)
        F[:4, 4:] = np.eye(4) * dt
        h = max(mean[3], 1.0)
        k = dt * REF_FPS   # number of reference frames elapsed
        q_pos = [self.w_pos * h, self.w_pos * h, 1e-2, self.w_pos * h]
        q_vel = [self.w_vel * h * REF_FPS, self.w_vel * h * REF_FPS, 1e-5 * REF_FPS, self.w_vel * h * REF_FPS]
        Q = np.diag(np.square(np.r_[q_pos, q_vel])) * k
        m = F @ mean
        P = F @ cov @ F.T + Q
        if vel_decay_tau:
            m[4:] *= math.exp(-dt / vel_decay_tau)
        m[3] = max(m[3], 1.0)
        m[2] = max(m[2], 1e-3)
        return m, P

    def update(self, mean, cov, z):
        h = max(mean[3], 1.0)
        R = np.diag(np.square([self.w_pos * h, self.w_pos * h, 1e-1, self.w_pos * h])) * self.r_scale
        S = self._H @ cov @ self._H.T + R
        K = np.linalg.solve(S, (cov @ self._H.T).T).T
        y = z - self._H @ mean
        return mean + K @ y, cov - K @ S @ K.T


def xyxy_to_xyah(b) -> np.ndarray:
    x0, y0, x1, y1 = b
    w, h = max(x1 - x0, 1e-3), max(y1 - y0, 1e-3)
    return np.array([x0 + w / 2, y0 + h / 2, w / h, h], dtype=float)


def xyah_to_xyxy(m) -> tuple[float, float, float, float]:
    cx, cy, a, h = m[:4]
    w = a * h
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a: (N,4), b: (M,4) xyxy -> (N,M) IoU."""
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    ix0 = np.maximum(a[:, None, 0], b[None, :, 0]); iy0 = np.maximum(a[:, None, 1], b[None, :, 1])
    ix1 = np.minimum(a[:, None, 2], b[None, :, 2]); iy1 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(ix1 - ix0, 0, None) * np.clip(iy1 - iy0, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-9)


def _assign(cost: np.ndarray, max_cost: float) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    if cost.size == 0:
        return [], list(range(cost.shape[0])), list(range(cost.shape[1]))
    c = np.where(cost > max_cost, max_cost + 1e3, cost)
    if linear_sum_assignment is not None:
        rows, cols = linear_sum_assignment(c)
    else:  # greedy fallback
        rows, cols, used_r, used_c = [], [], set(), set()
        for idx in np.argsort(c, axis=None):
            r, k = divmod(int(idx), c.shape[1])
            if r not in used_r and k not in used_c:
                rows.append(r); cols.append(k); used_r.add(r); used_c.add(k)
    matches = [(int(r), int(k)) for r, k in zip(rows, cols) if cost[r, k] <= max_cost]
    mr = {r for r, _ in matches}
    mc = {k for _, k in matches}
    return matches, [r for r in range(cost.shape[0]) if r not in mr], [k for k in range(cost.shape[1]) if k not in mc]


# ---------------------------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------------------------

TENTATIVE, TRACKED, LOST, REMOVED = "tentative", "tracked", "lost", "removed"


@dataclass
class Track:
    track_id: int
    label: str
    mean: np.ndarray
    cov: np.ndarray
    t: float                      # time of `mean`
    last_update: float
    first_seen: float
    score: float
    state: str = TENTATIVE
    hits: int = 1
    source: str = "yolo"
    lost_since: float | None = None
    mask: object = None

    def box(self) -> tuple[float, float, float, float]:
        return xyah_to_xyxy(self.mean)


@dataclass(frozen=True, eq=False)
class TrackedObject:
    """What consumers (overlays, ROI, replacement) see for a given output time."""
    track_id: int
    label: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float
    state: str            # tracked | coasting
    age_s: float
    since_update_s: float
    vx: float = 0.0       # px/s
    vy: float = 0.0
    source: str = "yolo"
    mask: object = None   # uint8 box-local silhouette from the latest segmentation, re-projected to the box

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def normalized(self, width: int, height: int) -> tuple[float, float, float, float]:
        return (self.x0 / width, self.y0 / height, self.x1 / width, self.y1 / height)


@dataclass
class TrackerStats:
    active: int = 0
    lost: int = 0
    tentative: int = 0
    ids_created: int = 0
    recovered: int = 0          # lost tracks re-matched with the same ID
    updates: int = 0
    update_ms: float = 0.0


class ByteTracker:
    def __init__(self, cfg: TrackerConfig | None = None):
        self.cfg = cfg or TrackerConfig()
        self.kf = KalmanXYAH(self.cfg.smoothing)
        self._tracks: list[Track] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self.stats = TrackerStats()
        self._last_t: float | None = None
        self.frame_size: tuple[int, int] = (0, 0)

    def configure(self, cfg: TrackerConfig) -> None:
        with self._lock:
            self.cfg = cfg
            self.kf = KalmanXYAH(cfg.smoothing)

    def reset(self) -> None:
        with self._lock:
            self._tracks = []
            self._last_t = None
            self.stats = TrackerStats(ids_created=0)

    # -------------------------------------------------------------- update
    def update(self, dets: list[Detection], t: float, frame_size: tuple[int, int] | None = None) -> None:
        import time as _time
        t0 = _time.perf_counter()
        with self._lock:
            if frame_size:
                self.frame_size = frame_size
            cfg = self.cfg
            if self._last_t is not None and t < self._last_t:
                t = self._last_t   # never go backwards in time
            for tr in self._tracks:
                tau = 0.5 if tr.state == LOST else None
                tr.mean, tr.cov = self.kf.predict(tr.mean, tr.cov, t - tr.t, tau)
                tr.t = t

            dets = [d for d in dets if d.confidence >= cfg.low_thresh]
            high = [d for d in dets if d.confidence >= cfg.high_thresh]
            low = [d for d in dets if d.confidence < cfg.high_thresh]

            confirmed = [tr for tr in self._tracks if tr.state in (TRACKED, LOST)]
            tentative = [tr for tr in self._tracks if tr.state == TENTATIVE]

            # 1st association: high dets <-> tracked + lost
            matches, u_tr, u_det = _assign(self._cost(confirmed, high), 1 - cfg.match_iou)
            for i, j in matches:
                self._apply(confirmed[i], high[j], t)
            rem_tracks = [confirmed[i] for i in u_tr]
            rem_high = [high[j] for j in u_det]

            # 2nd association: low dets <-> remaining *tracked* (not lost) tracks
            tracked_rem = [tr for tr in rem_tracks if tr.state == TRACKED]
            matches, u_tr2, _ = _assign(self._cost(tracked_rem, low), 1 - cfg.low_match_iou)
            for i, j in matches:
                self._apply(tracked_rem[i], low[j], t)
            for i in u_tr2:
                tr = tracked_rem[i]
                tr.state, tr.lost_since = LOST, t

            # tentative tracks <-> remaining high dets
            matches, u_tent, u_det3 = _assign(self._cost(tentative, rem_high), 1 - cfg.tentative_match_iou)
            for i, j in matches:
                self._apply(tentative[i], rem_high[j], t)
            for i in u_tent:
                tentative[i].state = REMOVED
            rem_high = [rem_high[j] for j in u_det3]

            # new tracks
            first_frame = self._last_t is None
            for d in rem_high:
                if d.confidence < cfg.new_track_thresh:
                    continue
                mean, cov = self.kf.initiate(xyxy_to_xyah((d.x0, d.y0, d.x1, d.y1)))
                tr = Track(self._next_id, d.label, mean, cov, t, t, t, d.confidence,
                           TRACKED if first_frame else TENTATIVE, 1, d.source, mask=d.mask)
                self._next_id += 1
                self.stats.ids_created += 1
                self._tracks.append(tr)

            # expire lost tracks, drop removed, de-duplicate
            for tr in self._tracks:
                if tr.state == LOST and t - (tr.lost_since or t) > cfg.track_buffer_s:
                    tr.state = REMOVED
            self._tracks = [tr for tr in self._tracks if tr.state != REMOVED]
            self._dedupe()
            self._last_t = t
            st = self.stats
            st.active = sum(tr.state == TRACKED for tr in self._tracks)
            st.lost = sum(tr.state == LOST for tr in self._tracks)
            st.tentative = sum(tr.state == TENTATIVE for tr in self._tracks)
            st.updates += 1
            st.update_ms = (_time.perf_counter() - t0) * 1000

    def _cost(self, tracks: list[Track], dets: list[Detection]) -> np.ndarray:
        if not tracks or not dets:
            return np.zeros((len(tracks), len(dets)))
        tb = np.array([tr.box() for tr in tracks])
        db = np.array([(d.x0, d.y0, d.x1, d.y1) for d in dets])
        cost = 1.0 - iou_matrix(tb, db)
        if self.cfg.class_aware:
            tl = np.array([tr.label for tr in tracks])[:, None]
            dl = np.array([d.label for d in dets])[None, :]
            cost = np.where(tl == dl, cost, 1.0)
        return cost

    def _apply(self, tr: Track, d: Detection, t: float) -> None:
        tr.mean, tr.cov = self.kf.update(tr.mean, tr.cov, xyxy_to_xyah((d.x0, d.y0, d.x1, d.y1)))
        tr.t = t
        tr.last_update = t
        tr.hits += 1
        tr.score = 0.7 * tr.score + 0.3 * d.confidence
        if d.mask is not None:
            tr.mask = d.mask
        if tr.state == LOST:
            self.stats.recovered += 1
        if tr.state == LOST or (tr.state == TENTATIVE and tr.hits >= self.cfg.min_hits):
            tr.state = TRACKED
        tr.lost_since = None

    def _dedupe(self) -> None:
        tracked = [tr for tr in self._tracks if tr.state == TRACKED]
        lost = [tr for tr in self._tracks if tr.state == LOST]
        if not tracked or not lost:
            return
        iou = iou_matrix(np.array([a.box() for a in tracked]), np.array([b.box() for b in lost]))
        drop = set()
        for i, j in zip(*np.where(iou > 0.85)):
            a, b = tracked[i], lost[j]
            if a.label != b.label:
                continue
            older, newer = (a, b) if a.first_seen <= b.first_seen else (b, a)
            drop.add(id(newer))
        self._tracks = [tr for tr in self._tracks if id(tr) not in drop]

    # -------------------------------------------------------------- query
    def snapshot(self, t: float | None = None) -> list[TrackedObject]:
        """Confirmed tracks predicted to time t (defaults to the last update time)."""
        with self._lock:
            cfg = self.cfg
            if t is None:
                t = self._last_t or 0.0
            out = []
            for tr in self._tracks:
                if tr.state == TENTATIVE:
                    continue
                since = t - tr.last_update
                if tr.state == LOST and (t - (tr.lost_since or t)) > cfg.coast_s:
                    continue
                if since > max(cfg.coast_s, cfg.max_extrapolate_s) + 0.6:
                    continue   # AI stalled: do not draw stale boxes forever
                dt = min(max(t - tr.t, -cfg.max_extrapolate_s), cfg.max_extrapolate_s)
                if dt > 0:
                    m, _ = self.kf.predict(tr.mean, tr.cov, dt, 0.5 if tr.state == LOST else None)
                elif dt < 0:   # output frame older than state: step back along velocity
                    m = tr.mean.copy()
                    m[:4] += tr.mean[4:] * dt
                else:
                    m = tr.mean
                x0, y0, x1, y1 = xyah_to_xyxy(m)
                fw, fh = self.frame_size
                if fw and fh:
                    x0, x1 = max(0.0, x0), min(float(fw), x1)
                    y0, y1 = max(0.0, y0), min(float(fh), y1)
                    if x1 <= x0 or y1 <= y0:
                        continue
                out.append(TrackedObject(tr.track_id, tr.label, tr.score, x0, y0, x1, y1,
                                         "tracked" if tr.state == TRACKED else "coasting",
                                         t - tr.first_seen, max(0.0, since), float(m[4]), float(m[5]), tr.source,
                                         tr.mask))
            return out

    def tracks(self) -> list[Track]:
        with self._lock:
            return list(self._tracks)
