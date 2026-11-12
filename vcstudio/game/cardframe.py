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


def find_frames(screenshot_bgr: np.ndarray, cal: Calibration, tol: int = 28,
                min_frac: float = 0.02, max_frac: float = 0.9) -> list[np.ndarray]:
    """Screen rectangles (4x2, TL TR BR BL) of the insides of all the game's frames, in reading order
    (top to bottom, then left to right). `screenshot_bgr` is taken while the camera shows grey_frame()."""
    h, w = screenshot_bgr.shape[:2]
    area = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(area, np.round(cal.camera_area()).astype(np.int32), 1)
    if area.sum() < 100:
        return []
    # The game may tint or darken the picture around its frames, so the camera grey can look like any
    # of the area's main colours: try the 3 most common ones.
    inside = area.astype(bool)
    px = screenshot_bgr[inside].astype(np.int16)
    q = (px // 16).astype(np.int32)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    refs = [px[keys == k].mean(axis=0) for k in vals[np.argsort(-counts)[:3]]]
    shown = int(inside.sum())
    border = area - cv2.erode(area, np.ones((5, 5), np.uint8))
    cands = []
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
            cands.append((int(a), x, y, bw, bh))
    # the same frame found with two reference colours, or a frame inside another: keep the larger
    kept = []
    for a, x, y, bw, bh in sorted(cands, reverse=True):
        box = np.array([[x, y, x + bw, y + bh]], float)
        if kept and _overlap(box, np.array([[k[1], k[2], k[1] + k[3], k[2] + k[4]] for k in kept], float)).max() > 0.5:
            continue
        kept.append((a, x, y, bw, bh))
    row_h = max(1.0, float(np.median([k[4] for k in kept])) / 2) if kept else 1.0
    kept.sort(key=lambda k: (round((k[2] + k[4] / 2) / row_h), k[1]))
    return [np.array([[x, y], [x + bw, y], [x + bw, y + bh], [x, y + bh]], float) for _, x, y, bw, bh in kept]


def _overlap(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Intersection over the smaller box, (1,4) vs (M,4) xyxy."""
    ix = np.clip(np.minimum(a[:, None, 2], b[None, :, 2]) - np.maximum(a[:, None, 0], b[None, :, 0]), 0, None)
    iy = np.clip(np.minimum(a[:, None, 3], b[None, :, 3]) - np.maximum(a[:, None, 1], b[None, :, 1]), 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return (ix * iy) / np.maximum(np.minimum(area_a[:, None], area_b[None, :]), 1e-9)


def find_frame(screenshot_bgr: np.ndarray, cal: Calibration, **kw) -> np.ndarray | None:
    """The largest of the game's frames, or None."""
    frames = find_frames(screenshot_bgr, cal, **kw)
    if not frames:
        return None
    return max(frames, key=lambda q: (q[2, 0] - q[0, 0]) * (q[2, 1] - q[0, 1]))


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
# A profile can hold several card frames ("slots", e.g. hand + table). Slot 1 is the region
# "gamecard", slot n the region "gamecard-n". body["game"] keeps the card list (asset ids in the order
# the user sees them), the active slot (what hotkeys act on) and named layouts, one per game.
GAME_CARD_ID = "gamecard"
SLOT_KEYS = ("cx", "cy", "w", "h", "rotation", "enabled")
REP_KEYS = ("asset_id", "fit", "flip_h")


def slot_id(n: int) -> str:
    return GAME_CARD_ID if n == 1 else f"{GAME_CARD_ID}-{n}"


def slot_number(roi_id: str) -> int | None:
    if roi_id == GAME_CARD_ID:
        return 1
    if roi_id.startswith(GAME_CARD_ID + "-") and roi_id[len(GAME_CARD_ID) + 1:].isdigit():
        return int(roi_id[len(GAME_CARD_ID) + 1:])
    return None


def game_state(body: dict) -> dict:
    g = body.setdefault("game", {})
    g.setdefault("cards", [])
    g.setdefault("layouts", {})
    g.setdefault("active_slot", 1)
    return g


def game_slots(body: dict) -> dict[int, dict]:
    """slot number -> region dict, in slot order."""
    out = {}
    for r in body.get("rois", []):
        n = slot_number(r.get("id", ""))
        if n is not None:
            out[n] = r
    return dict(sorted(out.items()))


def game_card_roi(body: dict, slot: int = 1) -> dict | None:
    return game_slots(body).get(slot)


def set_game_card(body: dict, *, slot: int = 1, rect=None, asset_id: str | None = None, fit: str | None = None,
                  flip: bool | None = None, enabled: bool | None = None) -> dict:
    """Create/update a card frame; only the given fields change.

    rect = (cx, cy, w, h, rotation) normalised to the camera picture (see frame_to_rect)."""
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI
    d = game_card_roi(body, slot)
    if d is None:
        r = ROI(id=slot_id(slot), name=f"Game card {slot}", kind="rect", mode="manual", priority=200 + slot,
                cx=0.5, cy=0.5, w=0.2, h=0.36)
        r.replacement = ReplacementSpec(enabled=True, mode="rect", fit="fit", background=[0, 0, 0, 0]).to_dict()
        d = r.to_dict()
        body.setdefault("rois", []).append(d)
    if rect is not None:
        d["cx"], d["cy"], d["w"], d["h"], d["rotation"] = (float(v) for v in rect)
    rep = d.setdefault("replacement", {})
    if asset_id is not None:
        rep["asset_id"] = asset_id
        cards = game_state(body)["cards"]
        if asset_id and asset_id not in cards:
            cards.append(asset_id)
    if fit is not None:
        rep["fit"] = fit
    if flip is not None:
        rep["flip_h"] = bool(flip)
    if enabled is not None:
        d["enabled"] = bool(enabled)
    return d


def remove_slot(body: dict, slot: int) -> None:
    body["rois"] = [r for r in body.get("rois", []) if slot_number(r.get("id", "")) != slot]
    g = game_state(body)
    if g["active_slot"] == slot:
        g["active_slot"] = min(game_slots(body), default=1)


def next_free_slot(body: dict) -> int:
    used = game_slots(body)
    return next(n for n in range(1, len(used) + 2) if n not in used)


def add_cards(body: dict, asset_ids: list[str]) -> None:
    cards = game_state(body)["cards"]
    cards.extend(a for a in asset_ids if a not in cards)


def remove_card(body: dict, asset_id: str) -> None:
    g = game_state(body)
    g["cards"] = [a for a in g["cards"] if a != asset_id]
    for d in game_slots(body).values():
        if d.get("replacement", {}).get("asset_id") == asset_id:
            d["replacement"]["asset_id"] = ""


def cycle_card(body: dict, step: int, slot: int | None = None) -> str | None:
    """Show the next (step=1) / previous (step=-1) card of the card list in a slot (default: active)."""
    g = game_state(body)
    slot = slot or g["active_slot"]
    d = game_card_roi(body, slot)
    cards = [a for a in g["cards"] if any(x.get("id") == a for x in body.get("assets", []))]
    if d is None or not cards:
        return None
    cur = d.get("replacement", {}).get("asset_id")
    i = cards.index(cur) if cur in cards else (-1 if step > 0 else 0)
    new = cards[(i + step) % len(cards)]
    set_game_card(body, slot=slot, asset_id=new, enabled=True)
    return new


def toggle_cards(body: dict) -> bool:
    """Hide all card frames if any is shown, else show them all. Returns the new state."""
    slots = game_slots(body)
    on = not any(d.get("enabled", True) for d in slots.values())
    for d in slots.values():
        d["enabled"] = on
    return on


def save_layout(body: dict, name: str) -> None:
    """Remember the current frames (geometry, card, options) under a game's name."""
    name = name.strip()
    if not name:
        raise ValueError("layout name is empty")
    slots = []
    for n, d in game_slots(body).items():
        rep = d.get("replacement", {})
        slots.append({"slot": n, **{k: d.get(k) for k in SLOT_KEYS}, **{k: rep.get(k) for k in REP_KEYS}})
    game_state(body)["layouts"][name] = {"slots": slots}


def apply_layout(body: dict, name: str) -> None:
    """Replace the current card frames with a saved layout."""
    lay = game_state(body)["layouts"][name]
    body["rois"] = [r for r in body.get("rois", []) if slot_number(r.get("id", "")) is None]
    for s in lay["slots"]:
        set_game_card(body, slot=int(s["slot"]), rect=(s["cx"], s["cy"], s["w"], s["h"], s.get("rotation", 0.0)),
                      asset_id=s.get("asset_id") or "", fit=s.get("fit") or "fit", flip=bool(s.get("flip_h")),
                      enabled=s.get("enabled", True))
    g = game_state(body)
    g["active_slot"] = min(game_slots(body), default=1)
    g["last_layout"] = name


def delete_layout(body: dict, name: str) -> None:
    game_state(body)["layouts"].pop(name, None)
