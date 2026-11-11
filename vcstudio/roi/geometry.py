"""Polygon geometry: point tests, overlap, and mask rasterisation (NumPy, no OpenCV)."""
from __future__ import annotations

import functools

import numpy as np


def point_in_polygon(x: float, y: float, poly: np.ndarray) -> bool:
    """Even-odd rule."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xc = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xc:
                inside = not inside
        j = i
    return inside


def points_in_polygon(pts: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Vectorised even-odd test for (M, 2) points."""
    x, y = pts[:, 0][:, None], pts[:, 1][:, None]
    xi, yi = poly[:, 0][None, :], poly[:, 1][None, :]
    xj, yj = np.roll(poly[:, 0], 1)[None, :], np.roll(poly[:, 1], 1)[None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        cond = (yi > y) != (yj > y)
        xc = xi + (y - yi) * (xj - xi) / (yj - yi)
        crosses = cond & (x < xc)
    return (crosses.sum(axis=1) % 2) == 1


def rasterize(poly: np.ndarray, width: int, height: int) -> np.ndarray:
    """uint8 mask (H, W) with 255 inside the polygon; pixel centres sampled, even-odd rule."""
    mask = np.zeros((height, width), np.uint8)
    if len(poly) < 3:
        return mask
    y0 = max(0, int(np.floor(poly[:, 1].min())))
    y1 = min(height, int(np.ceil(poly[:, 1].max())) + 1)
    if y0 >= y1:
        return mask
    xs, ys = poly[:, 0], poly[:, 1]
    xs2, ys2 = np.roll(xs, -1), np.roll(ys, -1)
    rows = np.arange(y0, y1) + 0.5
    # edge/scanline intersections, vectorised over all rows
    yy = rows[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        cond = (ys[None, :] <= yy) != (ys2[None, :] <= yy)
        xints = xs[None, :] + (yy - ys[None, :]) * (xs2 - xs)[None, :] / (ys2 - ys)[None, :]
    xints = np.where(cond, xints, np.inf)
    xints.sort(axis=1)
    cols = np.arange(width) + 0.5
    for r, xi in enumerate(xints):
        xi = xi[np.isfinite(xi)]
        for a, b in zip(xi[0::2], xi[1::2]):
            c0 = int(np.ceil(a - 0.5))
            c1 = int(np.floor(b - 0.5)) + 1
            c0, c1 = max(0, c0), min(width, c1)
            if c1 > c0:
                mask[y0 + r, c0:c1] = 255
    del cols
    return mask


@functools.lru_cache(maxsize=64)
def _cached_mask(key: tuple, width: int, height: int) -> np.ndarray:
    m = rasterize(np.array(key, dtype=float).reshape(-1, 2), width, height)
    m.flags.writeable = False
    return m


def polygon_mask(poly: np.ndarray, width: int, height: int) -> np.ndarray:
    """Cached rasterisation (static manual ROIs are rasterised once)."""
    key = tuple(np.round(np.asarray(poly, dtype=float), 2).ravel().tolist())
    return _cached_mask(key, width, height)


def box_polygon(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float)


def clip_polygon(subject: np.ndarray, clip: np.ndarray) -> np.ndarray:
    """Sutherland–Hodgman: clip `subject` by a *convex* `clip` polygon (clockwise or ccw)."""
    def inside(p, a, b, sign):
        return sign * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) >= 0

    def intersect(p1, p2, a, b):
        d1 = p2 - p1
        d2 = b - a
        den = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(den) < 1e-12:
            return p2
        t = ((a[0] - p1[0]) * d2[1] - (a[1] - p1[1]) * d2[0]) / den
        return p1 + t * d1

    area2 = float(np.dot(clip[:, 0], np.roll(clip[:, 1], -1)) - np.dot(clip[:, 1], np.roll(clip[:, 0], -1)))
    sign = 1.0 if area2 > 0 else -1.0
    out = [np.asarray(p, float) for p in subject]
    for i in range(len(clip)):
        a, b = clip[i], clip[(i + 1) % len(clip)]
        inp, out = out, []
        if not inp:
            break
        s = inp[-1]
        for e in inp:
            if inside(e, a, b, sign):
                if not inside(s, a, b, sign):
                    out.append(intersect(s, e, a, b))
                out.append(e)
            elif inside(s, a, b, sign):
                out.append(intersect(s, e, a, b))
            s = e
    return np.array(out) if out else np.zeros((0, 2))


def polygon_area(p: np.ndarray) -> float:
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def is_convex(p: np.ndarray) -> bool:
    n = len(p)
    if n < 4:
        return True
    signs = set()
    for i in range(n):
        a, b, c = p[i], p[(i + 1) % n], p[(i + 2) % n]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-9:
            signs.add(cross > 0)
    return len(signs) <= 1


def overlap_fraction(box: tuple[float, float, float, float], area_poly: np.ndarray, samples: int = 12) -> float:
    """Fraction of `box` covered by `area_poly` (exact for convex areas, sampled otherwise)."""
    x0, y0, x1, y1 = box
    ba = max((x1 - x0) * (y1 - y0), 1e-9)
    if is_convex(area_poly):
        return polygon_area(clip_polygon(box_polygon(*box), area_poly)) / ba
    gx = np.linspace(x0, x1, samples + 2)[1:-1]
    gy = np.linspace(y0, y1, samples + 2)[1:-1]
    pts = np.stack(np.meshgrid(gx, gy), -1).reshape(-1, 2)
    return float(points_in_polygon(pts, area_poly).mean())
