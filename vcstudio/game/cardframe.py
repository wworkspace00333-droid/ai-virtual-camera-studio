"""Put a picture (a game card) into a frame that a game draws over the camera picture.

The app cannot see how a browser game or an emulator shows the camera: the picture can be scaled,
cropped (e.g. to a square) or mirrored. So the frame is found in two short steps while the game is on
screen:

1. Calibration. The camera briefly shows a grid of ArUco markers with known positions. A screenshot
   is taken; every marker found in it gives 4 point pairs (camera pixel -> screen pixel), and a
   homography H fitted with RANSAC maps camera pixels to screen pixels. A grid, not 4 corner markers,
   so a game that crops the camera still shows enough markers.
2. Frame. The camera shows a flat grey picture and a second screenshot is taken. Inside the area where
   the game shows the camera, the game's own overlay is everything that is not that grey. The frame's
   inside is the largest grey area that does not touch the edge of the camera area (works for an
   outlined frame and for a frame cut out of a darkened overlay). The user can also draw the frame by
   hand on the screenshot. Either way the screen rectangle is mapped back to camera pixels with H^-1.

Pure NumPy/OpenCV; the screen capture and the camera output are passed in, so it is testable offline.
"""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

GREY = 128
# Two grids: a dense one first (many small markers, so enough of them escape the game's frame lines and
# captions drawn over the picture), then a coarse one for games that show the camera very small.
GRIDS = {"dense": (12, 6), "coarse": (6, 3)}
DICT = cv2.aruco.DICT_4X4_100


def _dictionary():
    return cv2.aruco.getPredefinedDictionary(DICT)


def marker_layout(width: int, height: int, grid: str = "dense") -> dict[int, np.ndarray]:
    """id -> 4x2 corner positions (TL, TR, BR, BL) in camera pixels for the calibration grid."""
    COLS, ROWS = GRIDS[grid]
    cw, ch = width / COLS, height / ROWS
    side = int(min(cw, ch) * 0.62)
    out = {}
    for r in range(ROWS):
        for c in range(COLS):
            x0 = int(c * cw + (cw - side) / 2)
            y0 = int(r * ch + (ch - side) / 2)
            out[r * COLS + c] = np.array([[x0, y0], [x0 + side, y0], [x0 + side, y0 + side], [x0, y0 + side]], float)
    return out


def calibration_frame(width: int, height: int, grid: str = "dense") -> np.ndarray:
    """BGRA frame: white background with the marker grid (white quiet zones around every marker)."""
    img = np.full((height, width), 255, np.uint8)
    d = _dictionary()
    for mid, q in marker_layout(width, height, grid).items():
        x0, y0 = q[0].astype(int)
        side = int(q[1][0] - q[0][0])
        img[y0:y0 + side, x0:x0 + side] = cv2.aruco.generateImageMarker(d, mid, side)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)


def grey_frame(width: int, height: int) -> np.ndarray:
    f = np.full((height, width, 4), GREY, np.uint8)
    f[..., 3] = 255
    return f


@dataclass
class Calibration:
    H: np.ndarray                 # 3x3, camera pixel -> screen pixel
    camera_size: tuple[int, int]
    markers: int                  # markers found
    error_px: float               # median reprojection error on screen
    mirrored: bool                # the game shows the camera mirrored

    def to_screen(self, pts) -> np.ndarray:
        p = np.asarray(pts, float).reshape(-1, 1, 2)
        return cv2.perspectiveTransform(p, self.H).reshape(-1, 2)

    def to_camera(self, pts) -> np.ndarray:
        p = np.asarray(pts, float).reshape(-1, 1, 2)
        return cv2.perspectiveTransform(p, np.linalg.inv(self.H)).reshape(-1, 2)

    def camera_area(self) -> np.ndarray:
        """Screen polygon (4x2) of the whole camera picture (may extend past what the game shows)."""
        w, h = self.camera_size
        return self.to_screen([[0, 0], [w, 0], [w, h], [0, h]])


class CalibrationError(RuntimeError):
    pass


def coarse_calibration_frame(width: int, height: int) -> np.ndarray:
    return calibration_frame(width, height, "coarse")


def calibrate(screenshot_bgr: np.ndarray, camera_size: tuple[int, int], min_markers: int = 3,
              grid: str = "dense") -> Calibration:
    gray = screenshot_bgr if screenshot_bgr.ndim == 2 else cv2.cvtColor(screenshot_bgr, cv2.COLOR_BGR2GRAY)
    params = cv2.aruco.DetectorParameters()
    det = cv2.aruco.ArucoDetector(_dictionary(), params)
    layout = marker_layout(*camera_size, grid)

    def detect(img):
        corners, ids, _ = det.detectMarkers(img)
        found = {}
        if ids is not None:
            for c, i in zip(corners, ids.ravel()):
                if int(i) in layout:
                    found[int(i)] = c.reshape(4, 2)
        return found

    def fit(found):
        if len(found) < min_markers:
            return None
        src = np.vstack([layout[i] for i in found])
        dst = np.vstack([found[i] for i in found])
        H, _ = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
        if H is None:
            return None
        proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
        return H, float(np.median(np.linalg.norm(proj - dst, axis=1))), len(found)

    # A mirrored preview shows mirrored markers: ArUco does not decode those (or decodes a few as wrong
    # ids), so also look in the flipped screenshot and map the corners back (x -> w-1-x, same order).
    w = gray.shape[1]
    direct = detect(gray)
    flipped = {i: np.column_stack([w - 1 - c[:, 0], c[:, 1]]) for i, c in detect(cv2.flip(gray, 1)).items()}
    fits = [f for f in (fit(direct), fit(flipped)) if f is not None]
    if not fits:
        raise CalibrationError(f"found {max(len(direct), len(flipped))} calibration markers on screen "
                               f"(need {min_markers})")
    H, err, n = min(fits, key=lambda f: (f[1] > 3.0, -f[2], f[1]))
    mirrored = float(np.linalg.det(H[:2, :2])) < 0
    return Calibration(H, tuple(camera_size), n, err, mirrored)


def find_frame(screenshot_bgr: np.ndarray, cal: Calibration, tol: int = 28,
               min_frac: float = 0.02, max_frac: float = 0.9) -> np.ndarray | None:
    """Screen rectangle (4x2, TL TR BR BL) of the inside of the game's frame, or None.

    `screenshot_bgr` is taken while the camera shows grey_frame()."""
    h, w = screenshot_bgr.shape[:2]
    area = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(area, np.round(cal.camera_area()).astype(np.int32), 1)
    if area.sum() < 100:
        return None
    # The game may tint or darken the picture around its frame, so the camera grey can look like any
    # of the area's main colours: try the 3 most common ones.
    inside = area.astype(bool)
    px = screenshot_bgr[inside].astype(np.int16)
    q = (px // 16).astype(np.int32)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    refs = [px[keys == k].mean(axis=0) for k in vals[np.argsort(-counts)[:3]]]
    shown = int(inside.sum())
    border = area - cv2.erode(area, np.ones((5, 5), np.uint8))
    best = None
    for ref in refs:
        same = (np.abs(screenshot_bgr.astype(np.int16) - ref).max(axis=2) <= tol) & inside
        same = cv2.morphologyEx(same.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, lab, stats, _ = cv2.connectedComponentsWithStats(same, 4)
        border_ids = set(np.unique(lab[border.astype(bool)]).tolist())
        for i in range(1, n):
            x, y, bw, bh, a = stats[i]
            if i in border_ids or x == 0 or y == 0 or x + bw >= w or y + bh >= h:
                continue
            if not (min_frac * shown <= a <= max_frac * shown):
                continue
            if a < 0.6 * bw * bh:          # not roughly rectangular (text, icons)
                continue
            if best is None or a > best[4]:
                best = (x, y, bw, bh, a)
    if best is None:
        return None
    x, y, bw, bh, _ = best
    return np.array([[x, y], [x + bw, y], [x + bw, y + bh], [x, y + bh]], float)


def frame_to_rect(cal: Calibration, screen_quad) -> tuple[float, float, float, float, float]:
    """Screen quad -> camera rotated rect (cx, cy, w, h, rotation_deg), normalised to the camera size."""
    q = cal.to_camera(screen_quad).astype(np.float32)
    (cx, cy), (rw, rh), ang = cv2.minAreaRect(q)
    # keep the rectangle's orientation close to the screen's (minAreaRect may swap w/h and add 90 deg)
    if ang > 45:
        ang -= 90
        rw, rh = rh, rw
    elif ang < -45:
        ang += 90
        rw, rh = rh, rw
    W, H = cal.camera_size
    return cx / W, cy / H, rw / W, rh / H, float(ang)


def screenshot(path: Path | None = None) -> np.ndarray:
    """Main display screenshot (BGR). Needs Screen Recording permission; without it macOS returns
    the desktop without other apps' windows, which the calibration then reports as markers not found."""
    out = path or Path(tempfile.mkdtemp()) / "screen.png"
    r = subprocess.run(["/usr/sbin/screencapture", "-x", "-t", "png", str(out)], capture_output=True, timeout=15)
    img = cv2.imread(str(out))
    if r.returncode != 0 or img is None:
        raise CalibrationError("screenshot failed: allow Screen Recording for this app in System Settings > "
                               "Privacy & Security")
    return img


# ------------------------------------------------------------------------------------ profile
GAME_CARD_ID = "gamecard"


def game_card_roi(body: dict) -> dict | None:
    return next((r for r in body.get("rois", []) if r.get("id") == GAME_CARD_ID), None)


def set_game_card(body: dict, *, rect=None, asset_id: str | None = None, fit: str | None = None,
                  flip: bool | None = None, enabled: bool | None = None) -> dict:
    """Create/update the game-card region in a profile body; only the given fields change.

    rect = (cx, cy, w, h, rotation) normalised to the camera picture (see frame_to_rect)."""
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI
    d = game_card_roi(body)
    if d is None:
        r = ROI(id=GAME_CARD_ID, name="Game card", kind="rect", mode="manual", priority=200,
                cx=0.5, cy=0.5, w=0.2, h=0.36)
        r.replacement = ReplacementSpec(enabled=True, mode="rect", fit="fit", background=[0, 0, 0, 0]).to_dict()
        d = r.to_dict()
        body.setdefault("rois", []).append(d)
    if rect is not None:
        d["cx"], d["cy"], d["w"], d["h"], d["rotation"] = (float(v) for v in rect)
    rep = d.setdefault("replacement", {})
    if asset_id is not None:
        rep["asset_id"] = asset_id
    if fit is not None:
        rep["fit"] = fit
    if flip is not None:
        rep["flip_h"] = bool(flip)
    if enabled is not None:
        d["enabled"] = bool(enabled)
    return d
