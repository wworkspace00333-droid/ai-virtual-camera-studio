"""Live check of every performance mode x every kind of processing on the real OBS video (developer tool).

Input: OBS through the vcstudio-obs plugin. Output: OBS Virtual Camera. An independent AVFoundation client
receives the camera, and each scenario is judged on what the client actually got:
  image   magenta image in a fixed rectangle      -> fraction of magenta pixels inside the rectangle
  object  person silhouette replaced (segmentation) -> fraction of magenta pixels in the frame
  video   test clip in a rectangle                 -> differs from the input there and changes over time
  face    Hide face (pixelate tracked faces)       -> differs from the input inside the face box, not outside
  card    Card for a game (green card)             -> fraction of green pixels inside the card frame
  all     all of the above at once
Run through the dev bundle (camera permission):
    open -W -n "build/AI Virtual Camera Studio Dev.app" --args --run-script live_matrix.py --out <json> [--seconds 6]
Stop the app's own window first (both would feed the same camera).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODES = ("low_resource", "performance", "balanced", "max_quality")
SCENARIOS = ("image", "object", "video", "face", "card", "all")
RECT_IMG = (0.40, 0.10, 0.60, 0.30)
RECT_VID = (0.05, 0.60, 0.35, 0.95)
RECT_CARD = (0.62, 0.20, 0.80, 0.60)


def main() -> int:
    import cv2
    import numpy as np
    a = sys.argv[1:]
    out_path = a[a.index("--out") + 1]
    secs = float(a[a.index("--seconds") + 1]) if "--seconds" in a else 6.0
    only = a[a.index("--scenarios") + 1].split(",") if "--scenarios" in a else None
    os.environ["VCSTUDIO_HOME"] = tempfile.mkdtemp(prefix="vcs-matrix-")
    os.environ.setdefault("VCS_DISABLE_GLOBAL_HOTKEYS", "1")
    from vcstudio.app.application import AppContext
    from vcstudio.core.resources import sample
    from vcstudio.core.video_capture import AVFoundationSource
    from vcstudio.game import cardframe as cf
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import ROI, new_rect, set_quick_face

    ctx = AppContext(configure_logging=False)
    s = ctx.settings
    s.set("input.method", "obs_ipc")
    s.set("output.camera_device", "OBS Virtual Camera")
    s.set("output.camera_sink", "OBS Camera Extension Stream Sink")
    tmp = Path(tempfile.mkdtemp())
    mag, grn = tmp / "magenta.png", tmp / "green.png"
    cv2.imwrite(str(mag), np.full((64, 64, 3), (255, 0, 255), np.uint8))
    cv2.imwrite(str(grn), np.full((90, 60, 3), (0, 200, 0), np.uint8))
    A = {k: ctx.assets.import_file(p).to_dict() for k, p in (("mag", mag), ("grn", grn), ("vid", sample("clip_h264.mp4")))}

    def rois_for(sc: str, b: dict) -> None:
        b["assets"] = list(A.values())
        b["rois"] = []
        b.pop("game", None)
        want = SCENARIOS[:-1] if sc == "all" else (sc,)
        if "image" in want:
            r = new_rect("Image", *RECT_IMG)
            r.replacement = ReplacementSpec(enabled=True, asset_id=A["mag"]["id"], fit="stretch").to_dict()
            b["rois"].append(r.to_dict())
        if "video" in want:
            r = new_rect("Video", *RECT_VID)
            r.replacement = ReplacementSpec(enabled=True, asset_id=A["vid"]["id"], fit="stretch").to_dict()
            b["rois"].append(r.to_dict())
        if "object" in want:
            r = ROI(name="Silhouette", mode="auto", classes=["person"], padding=0.0)
            r.replacement = ReplacementSpec(enabled=True, mode="object", asset_id=A["mag"]["id"], fit="stretch").to_dict()
            b["rois"].append(r.to_dict())
        if "face" in want:
            set_quick_face(b, True)
        if "card" in want:
            x0, y0, x1, y1 = RECT_CARD
            cf.set_game_card(b, slot=1, rect=((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0, 0.0),
                             asset_id=A["grn"]["id"], fit="stretch")
        b["ai"]["face_detection"] = "vision"

    # independent client: newest received frame + the input frame current at that moment
    lock = threading.Lock()
    rec = {"frames": [], "keys": []}

    def on_frame(f):
        img = f.downscaled_copy(480)[..., :3]
        inp, _ = ctx.capture.slot.peek()
        tracked = ctx.ai.tracked_at(f.pts) if inp is not None else []
        faces = [o.normalized(inp.width, inp.height) for o in tracked if o.label == "face"] if inp is not None else []
        with lock:
            rec["frames"].append((img, inp.downscaled_copy(480)[..., :3] if inp is not None else None, faces))
            rec["keys"].append(hash(img[::8, ::8].tobytes()))
            if len(rec["frames"]) > 40:
                rec["frames"].pop(0)
    client = AVFoundationSource(None, "OBS Virtual Camera", 1920, 1080, 60.0, allow_own_output=True)
    ctx.start_capture()
    time.sleep(2)
    client.start(on_frame, lambda *e: None)

    def crop(img, r):
        h, w = img.shape[:2]
        return img[int(r[1] * h):int(r[3] * h), int(r[0] * w):int(r[2] * w)]

    def magenta(img):
        b, g, r = img[..., 0].astype(int), img[..., 1].astype(int), img[..., 2].astype(int)
        return float(((b > 190) & (r > 190) & (g < 70)).mean()) if img.size else 0.0

    def green(img):
        b, g, r = img[..., 0].astype(int), img[..., 1].astype(int), img[..., 2].astype(int)
        return float(((g > 150) & (b < 70) & (r < 70)).mean()) if img.size else 0.0

    def judge(sc: str, frames) -> dict:
        fr = [f for f in frames if f[1] is not None]
        if not fr:
            return {"visible": False, "why": "no client frames"}
        res = {}
        if sc in ("image", "all"):
            res["image_magenta"] = round(float(np.median([magenta(crop(c, RECT_IMG)) for c, _, _ in fr])), 3)
        if sc in ("object", "all"):
            res["object_magenta_frame"] = round(float(np.median([magenta(c) for c, _, _ in fr])), 3)
        if sc in ("video", "all"):
            d = [float(np.abs(crop(c, RECT_VID).astype(int) - crop(i, RECT_VID).astype(int)).mean()) for c, i, _ in fr]
            t = [float(np.abs(crop(fr[k][0], RECT_VID).astype(int) - crop(fr[k - 1][0], RECT_VID).astype(int)).mean())
                 for k in range(1, len(fr))]
            res["video_vs_input"] = round(float(np.median(d)), 1)
            res["video_changes"] = round(float(np.mean(t)) if t else 0.0, 2)
        if sc in ("face", "all"):
            ins, outs = [], []
            for c, i, faces in fr:
                if not faces:
                    continue
                x0, y0, x1, y1 = faces[0]
                diff = np.abs(c.astype(int) - i.astype(int)).mean(axis=2)
                m = np.zeros(diff.shape, bool)
                h, w = diff.shape
                m[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = True
                # "outside" = the rest of the picture minus the other regions of this scenario, which change it too
                other = np.zeros(diff.shape, bool)
                if sc == "all":
                    for r in (RECT_IMG, RECT_VID, RECT_CARD):
                        other[int(r[1] * h):int(r[3] * h), int(r[0] * w):int(r[2] * w)] = True
                    bb, gg, rr = c[..., 0].astype(int), c[..., 1].astype(int), c[..., 2].astype(int)
                    sil = ((bb > 190) & (rr > 190) & (gg < 70)).astype(np.uint8)
                    other |= cv2.dilate(sil, np.ones((9, 9), np.uint8)).astype(bool)   # replaced silhouette + its edge
                ins.append(float(diff[m].mean())); outs.append(float(diff[~m & ~other].mean()))
            res["face_diff_inside"] = round(float(np.median(ins)), 1) if ins else None
            res["face_diff_outside"] = round(float(np.median(outs)), 1) if outs else None
        if sc in ("card", "all"):
            res["card_green"] = round(float(np.median([green(crop(c, RECT_CARD)) for c, _, _ in fr])), 3)
        ok = []
        if "image_magenta" in res: ok.append(res["image_magenta"] > 0.8)
        if "object_magenta_frame" in res: ok.append(res["object_magenta_frame"] > 0.02)
        if "video_vs_input" in res: ok.append(res["video_vs_input"] > 15 and res["video_changes"] > 0.5)
        if "face_diff_inside" in res:
            ok.append(res["face_diff_inside"] is not None and res["face_diff_inside"] > 3 * max(1.0, res["face_diff_outside"] or 0))
        if "card_green" in res: ok.append(res["card_green"] > 0.8)
        res["visible"] = all(ok) and bool(ok)
        return res

    results = []
    for mode in MODES:
        s.set("performance.mode", mode)
        for sc in [x for x in SCENARIOS if not only or x in only]:
            ctx.update_active_profile(lambda b: (rois_for(sc, b), b.__setitem__("performance_mode", mode)))
            time.sleep(2.0)                                     # model switch / warm-up
            with lock:
                rec["frames"].clear(); rec["keys"].clear()
            p0, e0 = ctx.pipeline.stats(), ctx.ai.stats()
            t0 = time.monotonic()
            time.sleep(secs)
            dt = time.monotonic() - t0
            p1, e1 = ctx.pipeline.stats(), ctx.ai.stats()
            with lock:
                frames = list(rec["frames"])
                distinct = len(set(rec["keys"]))
            row = {"mode": mode, "scenario": sc, "pipeline_fps": round(p1.output_fps, 1),
                   "client_distinct_fps": round(distinct / dt, 1), "latency_ms": round(p1.latency_ms, 2),
                   "process_ms": round(p1.process_ms, 2), "render_errors": p1.render_errors - p0.render_errors,
                   "regions": p1.regions, "ai_model": e1.model, "ai_fps": round(e1.achieved_fps, 1), "ai_ms": round(e1.inference_ms_avg, 1),
                   "ai_errors": e1.errors - e0.errors, "camera_dropped": p1.camera_dropped - p0.camera_dropped,
                   "camera_sent": p1.camera_sent - p0.camera_sent}
            row.update(judge(sc, frames))
            results.append(row)
            print(json.dumps(row), flush=True)
    client.stop()
    ctx.shutdown()
    Path(out_path).write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
