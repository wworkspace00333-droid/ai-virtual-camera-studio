"""Compositor: per-frame planning on the CPU, pixel work on Metal (GPU) or NumPy/OpenCV (fallback).

Planning (`plan`) turns active regions + replacement specs into DrawOps: the source image (asset
or current video frame, already cropped), the frame->source affine, a coverage mask over the op's
bounding box, opacity, background fill and effect parameters. Per-ROI failures are caught here:
the ROI is disabled for the session and reported; other ROIs continue.

Execution backends implement the same maths (premultiplied BGRA, frame alpha preserved):
  composite:  inner = src + bg * (1 - src.a);   out = k * inner + dst * (1 - k * inner.a)
  effect:     out = k * effect(input) + dst * (1 - k)
  (k = opacity * mask)
Overlay is a composite with a transparent background and no region mask; region replacement and
object replacement differ only in the mask (region shape vs. segmentation silhouette).

  MetalBackend  libVCSNative: CVPixelBuffer/IOSurface textures, hardware blending, MPS blur
  CpuBackend    OpenCV warpAffine / GaussianBlur + NumPy blending

Object mode never passes a bounding box off as a silhouette: without a mask the object is skipped
unless the user explicitly chose the "box" fallback.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from vcstudio.replacement.assets import Asset, AssetError, AssetLibrary
from vcstudio.replacement.spec import ReplacementSpec
from vcstudio.replacement.video import PlayerPool, VideoError

log = logging.getLogger(__name__)
LUMA_BGR = np.array([0.0722, 0.7152, 0.2126], np.float32)


@dataclass
class CompositorStats:
    render_ms: float = 0.0       # total wall time of render()
    plan_ms: float = 0.0
    gpu_ms: float = 0.0          # Metal only
    backend: str = "cpu"
    regions_drawn: int = 0
    skipped_no_mask: int = 0
    failed_rois: dict[str, str] = field(default_factory=dict)
    frames: int = 0
    video_players: int = 0
    video_waiting: int = 0


@dataclass
class DrawOp:
    roi_id: str
    kind: str                                  # composite | effect
    bbox: tuple[int, int, int, int]            # integer frame px
    opacity: float
    mask: np.ndarray | None = None             # uint8 mask; None = full coverage
    mask_rect: tuple[float, float, float, float] | None = None   # frame px the mask spans (None = bbox)
    mask_key: tuple | None = None              # stable identity for static masks (GPU upload caching)
    src: np.ndarray | None = None              # premultiplied BGRA source (cropped)
    src_key: tuple | None = None
    inv: np.ndarray | None = None              # 2x3 frame px -> src px (pixel-index convention)
    bg: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)   # premultiplied BGRA 0..1
    effect: str = ""
    params: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def region_frame(region) -> tuple[float, float, float, float, float]:
    """(cx, cy, w, h, rotation_deg) of the region's local (unrotated) frame in pixels."""
    poly = np.asarray(region.polygon, dtype=float)
    if len(poly) == 4 and abs(region.rotation) > 1e-6 and not region.is_object:
        tl, tr, br, bl = poly
        w = float(np.linalg.norm(tr - tl)); h = float(np.linalg.norm(bl - tl))
        c = poly.mean(axis=0)
        return float(c[0]), float(c[1]), w, h, float(region.rotation)
    x0, y0, x1, y1 = region.bbox
    return (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0, 0.0


def asset_affine(asset_w: int, asset_h: int, region, spec: ReplacementSpec) -> np.ndarray:
    """2x3 matrix mapping (uncropped) asset pixels to frame pixels."""
    cx, cy, rw, rh, rrot = region_frame(region)
    cx0, cy0, cx1, cy1 = spec.crop
    aw, ah = (cx1 - cx0) * asset_w, (cy1 - cy0) * asset_h
    if spec.fit == "fit":
        sx = sy = min(rw / aw, rh / ah)
    elif spec.fit == "fill":
        sx = sy = max(rw / aw, rh / ah)
    elif spec.fit == "stretch":
        sx, sy = rw / aw, rh / ah
    else:
        sx = sy = 1.0
    sx *= spec.scale
    sy *= spec.scale
    a = math.radians(rrot + spec.rotation)
    c, s = math.cos(a), math.sin(a)
    rr = math.radians(rrot)
    ox, oy = spec.offset_x * rw, spec.offset_y * rh
    tx = cx + ox * math.cos(rr) - oy * math.sin(rr)
    ty = cy + ox * math.sin(rr) + oy * math.cos(rr)
    acx, acy = cx0 * asset_w + aw / 2, cy0 * asset_h + ah / 2
    R = np.array([[c * sx, -s * sy], [s * sx, c * sy]])
    t = np.array([tx, ty]) - R @ np.array([acx, acy])
    return np.hstack([R, t[:, None]])


def _invert(M: np.ndarray) -> np.ndarray:
    return cv2.invertAffineTransform(M.astype(np.float64))


def _clip_box(x0, y0, x1, y1, W, H) -> tuple[int, int, int, int]:
    return (max(0, int(math.floor(x0))), max(0, int(math.floor(y0))),
            min(W, int(math.ceil(x1))), min(H, int(math.ceil(y1))))


# =============================================================================================
# Backends
# =============================================================================================

class CpuBackend:
    name = "cpu"

    def execute(self, frame: np.ndarray, ops: list[DrawOp]) -> np.ndarray:
        out = np.array(frame, copy=True)
        H, W = out.shape[:2]
        for op in ops:
            x0, y0, x1, y1 = op.bbox
            w, h = x1 - x0, y1 - y0
            dst = out[y0:y1, x0:x1, :3].astype(np.float32) / 255
            k = np.full((h, w, 1), op.opacity, np.float32)
            if op.mask is not None:
                m = op.mask if op.mask_rect is None else self._place(op.mask, op.mask_rect, op.bbox)
                k *= m[..., None].astype(np.float32) / 255
            if op.kind == "composite":
                M = op.inv.copy()
                M[:, 2] += M[:, :2] @ np.array([x0, y0], dtype=np.float64)
                patch = cv2.warpAffine(op.src, M, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                       borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0)).astype(np.float32) / 255
                bg = np.array(op.bg, np.float32)
                inner = patch + bg * (1 - patch[..., 3:4])
                res = inner[..., :3] * k + dst * (1 - k * inner[..., 3:4])
            else:
                eff = self._effect(frame, op, W, H)
                res = eff * k + dst * (1 - k)
            out[y0:y1, x0:x1, :3] = np.clip(res * 255 + 0.5, 0, 255).astype(np.uint8)
        return out

    @staticmethod
    def _place(mask: np.ndarray, rect, bbox) -> np.ndarray:
        """Resample a mask spanning `rect` (frame px, float) onto the integer `bbox` grid (bilinear)."""
        x0, y0, x1, y1 = bbox
        rx0, ry0, rx1, ry1 = rect
        mh, mw = mask.shape
        sx, sy = mw / max(rx1 - rx0, 1e-6), mh / max(ry1 - ry0, 1e-6)
        # dst pixel (u, v) centre -> mask coordinate (pixel-index convention), same as the GPU sampler
        M = np.array([[sx, 0, (x0 + 0.5 - rx0) * sx - 0.5], [0, sy, (y0 + 0.5 - ry0) * sy - 0.5]], np.float32)
        return cv2.warpAffine(mask, M, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    @staticmethod
    def _effect(frame: np.ndarray, op: DrawOp, W: int, H: int) -> np.ndarray:
        x0, y0, x1, y1 = op.bbox
        if op.effect == "blur":
            sigma = max(0.5, op.params[0])
            m = int(math.ceil(3 * sigma))
            px0, py0, px1, py1 = max(0, x0 - m), max(0, y0 - m), min(W, x1 + m), min(H, y1 + m)
            region = frame[py0:py1, px0:px1, :3]
            b = cv2.GaussianBlur(region, (0, 0), sigmaX=sigma, borderType=cv2.BORDER_REPLICATE)
            return b[y0 - py0:y1 - py0, x0 - px0:x1 - px0].astype(np.float32) / 255
        if op.effect == "pixelate":
            b = max(1, int(op.params[0]))
            xs = np.clip((np.arange(x0, x1) // b) * b + b // 2, 0, W - 1)
            ys = np.clip((np.arange(y0, y1) // b) * b + b // 2, 0, H - 1)
            return frame[ys][:, xs, :3].astype(np.float32) / 255
        c = frame[y0:y1, x0:x1, :3].astype(np.float32) / 255
        bright, contrast, sat = op.params[0], op.params[1], op.params[2]
        c = (c - 0.5) * contrast + 0.5 + bright
        lum = (c * LUMA_BGR).sum(-1, keepdims=True)
        return np.clip(lum + (c - lum) * sat, 0, 1)


class MetalBackend:
    """GPU execution through libVCSNative. Source/destination are IOSurface CVPixelBuffers."""
    name = "metal"
    MAX_SLOTS = 48

    def __init__(self):
        from vcstudio.native.metal import MetalContext
        self.ctx = MetalContext()
        self._slots: dict[tuple, int] = {}
        self._slot_use: dict[int, float] = {}
        self._free: list[int] = []
        self._next = 0
        self._pools: dict[tuple[int, int], object] = {}

    def _alloc_slot(self) -> int:
        if self._free:
            return self._free.pop()
        if self._next < self.MAX_SLOTS:
            self._next += 1
            return self._next - 1
        old_key = min(self._slots, key=lambda k: self._slot_use[self._slots[k]])
        return self._slots.pop(old_key)

    def _slot_for(self, key: tuple, arr: np.ndarray, persistent: bool) -> int:
        s = self._slots.get(key) if persistent else None
        if s is None:
            s = self._alloc_slot()
            self.ctx.upload(s, arr)
            if persistent:
                self._slots[key] = s
        self._slot_use[s] = time.monotonic()
        return s

    def dst_pool(self, w: int, h: int):
        from vcstudio.core.pixelbuffer import PixelBufferPool
        pool = self._pools.get((w, h))
        if pool is None:
            pool = self._pools[(w, h)] = PixelBufferPool(w, h)
        return pool

    def execute_pb(self, src_pb, w: int, h: int, ops: list[DrawOp]):
        import Quartz as Q  # type: ignore
        pool = self.dst_pool(w, h)
        err, dst = Q.CVPixelBufferPoolCreatePixelBuffer(None, pool._pool, None)
        if err != 0:
            raise RuntimeError(f"output pixel buffer allocation failed ({err})")
        layers = np.zeros((len(ops), 28), np.float32)
        slots = np.full((len(ops), 2), -1, np.int32)
        transient: list[int] = []
        frame_uploads: dict[int, int] = {}      # id(src array) -> slot: upload a shared video frame once per frame
        for i, op in enumerate(ops):
            x0, y0, x1, y1 = op.bbox
            L = layers[i]
            L[0:4] = (x0, y0, x1, y1)
            if op.kind == "composite":
                # images are persistent (uploaded once); video frames change every frame
                persistent = bool(op.src_key) and op.src_key[0] == "img"
                if persistent:
                    s = self._slot_for(("src",) + tuple(op.src_key or ()), op.src, True)
                elif id(op.src) in frame_uploads:
                    s = frame_uploads[id(op.src)]
                else:
                    s = self._slot_for(("src",) + tuple(op.src_key or ()), op.src, False)
                    frame_uploads[id(op.src)] = s
                    transient.append(s)
                slots[i, 0] = s
                inv = op.inv
                L[4:8] = (inv[0, 0], inv[0, 1], inv[1, 0], inv[1, 1])
                L[8:12] = (inv[0, 2], inv[1, 2], op.src.shape[1], op.src.shape[0])
                b, g, r, a = op.bg
                L[12:16] = (r, g, b, a)                      # texture channels are presented as RGBA
            else:
                L[16:20] = op.params
            if op.mask is not None:
                persistent = bool(op.mask_key) and op.mask_key[0] in ("static", "obj")
                s = self._slot_for(("mask",) + tuple(op.mask_key or ()), op.mask, persistent)
                slots[i, 1] = s
                if not persistent:
                    transient.append(s)
                L[20:24] = op.mask_rect if op.mask_rect is not None else (x0, y0, x1, y1)
            effect = {"blur": 1, "pixelate": 2, "color": 3}.get(op.effect, 0)
            L[24:28] = (op.opacity, 0 if op.kind == "composite" else 1, effect, 1 if op.mask is not None else 0)
        try:
            self.ctx.render(src_pb, dst, layers, slots)
        finally:
            self._free.extend(transient)       # per-frame textures: slot reused next frame
        self._renders = getattr(self, "_renders", 0) + 1
        if self._renders % 600 == 0:           # Apple: flush CVMetalTextureCache periodically
            self.ctx.flush(drop_slots=False)
        return dst

    def release(self) -> None:
        """Free GPU memory held for this session (on stop): texture slots, caches, output pools."""
        self.ctx.flush(drop_slots=True)
        self._slots.clear(); self._slot_use.clear(); self._free.clear(); self._next = 0
        self._pools.clear()

    def execute(self, frame: np.ndarray, ops: list[DrawOp]) -> np.ndarray:
        """Array in, array out (copies through a pixel buffer); used by previews and tests."""
        from vcstudio.core.frame import Frame
        from vcstudio.core.pixelbuffer import pixel_buffer_for
        h, w = frame.shape[:2]
        src = pixel_buffer_for(Frame(0, 0, 0, w, h, array=np.ascontiguousarray(frame)))
        dst = self.execute_pb(src, w, h, ops)
        with Frame(0, 0, 0, w, h, pixel_buffer=dst).bgra() as a:
            return np.array(a)

    def close(self) -> None:
        self.ctx.close()


# =============================================================================================
# Compositor (planning + backend selection)
# =============================================================================================

class Compositor:
    def __init__(self, library: AssetLibrary, backend: str = "auto"):
        self.library = library
        self.stats = CompositorStats()
        self._failed: dict[str, str] = {}
        self._mask_cache: dict[tuple, np.ndarray] = {}
        self.videos = PlayerPool()
        self.cpu = CpuBackend()
        self.metal: MetalBackend | None = None
        self.backend_error = ""
        self.active = self.cpu
        self._lock = threading.RLock()     # one GPU context / caches: serialise renders across threads
        self.set_backend(backend)

    # ------------------------------------------------------------------ backend
    def set_backend(self, which: str) -> str:
        """auto | metal | cpu. Returns the backend actually in use."""
        with self._lock:
            return self._set_backend(which)

    def _set_backend(self, which: str) -> str:
        if which in ("auto", "metal") and self.metal is None:
            try:
                self.metal = MetalBackend()
                self.backend_error = ""
            except Exception as e:
                self.backend_error = str(e)
                log.warning("Metal backend unavailable, using CPU: %s", e)
        self.active = self.metal if (which in ("auto", "metal") and self.metal is not None) else self.cpu
        self.stats.backend = self.active.name
        return self.active.name

    def reset_failures(self) -> None:
        self._failed.clear()
        self.stats.failed_rois = {}

    def release_resources(self) -> None:
        """Called when processing stops: return GPU and cache memory to the system."""
        with self._lock:
            self.videos.close_all()
            self._mask_cache.clear()
            self.__dict__.get("_objmask_cache", {}).clear()
            if self.metal is not None:
                self.metal.release()

    def close(self) -> None:
        self.videos.close_all()
        if self.metal is not None:
            self.metal.close()
            self.metal = None
        self.active = self.cpu

    # ------------------------------------------------------------------ masks
    @staticmethod
    def _poly_mask(poly: np.ndarray, ox: int, oy: int, w: int, h: int) -> np.ndarray:
        m = np.zeros((h, w), np.uint8)
        pts = np.round((np.asarray(poly, float) - [ox, oy]) * 16).astype(np.int32)
        cv2.fillPoly(m, [pts], 255, lineType=cv2.LINE_AA, shift=4)
        return m

    def _object_local_mask(self, region, spec: ReplacementSpec, feather: float):
        """Refined silhouette at the mask's own (box-local) resolution, cached per AI mask update.

        Returns (mask, key, rect) where rect is the frame-px object box the mask spans, or None.
        The GPU resamples it into the object's *predicted* box every frame, so the expensive part
        (threshold, morphology, feathering) runs at the AI rate on a <=256 px mask, not per output frame.
        """
        src = region.mask
        if src is None or region.object_box is None:
            return None
        bx0, by0, bx1, by1 = region.object_box
        if bx1 - bx0 < 2 or by1 - by0 < 2:
            return None
        params = (spec.mask_threshold, spec.refine_edges, round(feather, 2))
        cache = self.__dict__.setdefault("_objmask_cache", {})
        hit = cache.get(id(src))
        if hit is not None and hit[0] is src and hit[1] == params:
            return hit[2], ("obj", id(src)) + params, (bx0, by0, bx1, by1)
        local = np.asarray(src).astype(np.float32) / 255
        t = spec.mask_threshold
        local = np.clip((local - (t - 0.1)) / 0.2, 0, 1)
        if spec.refine_edges:
            k = np.ones((3, 3), np.uint8)
            hard = (local > 0.5).astype(np.uint8)
            hard = cv2.morphologyEx(hard, cv2.MORPH_OPEN, k)
            hard = cv2.morphologyEx(hard, cv2.MORPH_CLOSE, k)
            local = np.minimum(local, cv2.dilate(hard, k).astype(np.float32))
        m = (local * 255).astype(np.uint8)
        if feather > 0:
            scale = m.shape[1] / max(bx1 - bx0, 1)                   # frame px -> mask px
            m = cv2.GaussianBlur(m, (0, 0), sigmaX=max(0.3, feather / 2 * scale))
        m.flags.writeable = False
        if len(cache) > 128:
            cache.clear()
        cache[id(src)] = (src, params, m)
        return m, ("obj", id(src)) + params, (bx0, by0, bx1, by1)

    def _static_mask(self, region, box, feather: float) -> tuple[np.ndarray, tuple]:
        key = (box, tuple(np.round(np.asarray(region.polygon), 1).ravel()), feather)
        m = self._mask_cache.get(key)
        if m is None:
            x0, y0, x1, y1 = box
            m = self._poly_mask(region.polygon, x0, y0, x1 - x0, y1 - y0)
            if feather > 0:
                m = cv2.GaussianBlur(m, (0, 0), sigmaX=max(0.3, feather / 2))
            if len(self._mask_cache) > 64:
                self._mask_cache.clear()
            m.flags.writeable = False
            self._mask_cache[key] = m
        return m, ("static",) + key

    # ------------------------------------------------------------------ sources
    def _source(self, asset: Asset, region, spec: ReplacementSpec, t: float | None):
        """(image, key) for an asset; videos give the decoded frame for output time t."""
        if asset.kind != "video":
            return self.library.image(asset), ("img", asset.id)
        if t is None:
            t = time.monotonic()
        scope = {"shared": (), "per_roi": (region.roi_id,), "per_object": (region.roi_id, region.track_id)}[spec.video_sync]
        info = self.library.video_info(asset)
        player = self.videos.get((asset.id, spec.video_sync) + scope, self.library.path(asset), info, spec.playback())
        if player.failed:
            raise VideoError(player.stats.error)
        return player.frame_at(t), ("vid", asset.id, spec.video_sync) + scope

    # ------------------------------------------------------------------ planning
    def plan(self, W: int, H: int, regions: list, specs: dict[str, ReplacementSpec],
             assets: dict[str, Asset], t: float | None) -> list[DrawOp]:
        ops: list[DrawOp] = []
        drawn = skipped = waiting = 0
        for region in regions:
            rid = region.roi_id
            spec = specs.get(rid)
            if spec is None or not spec.enabled or rid in self._failed:
                continue
            try:
                op = self._plan_one(region, spec, assets, t, W, H)
                if op == "waiting":
                    waiting += 1
                elif op is None:
                    skipped += 1
                else:
                    ops.append(op)
                    drawn += 1
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self._failed[rid] = msg
                self.stats.failed_rois = dict(self._failed)
                log.error("Replacement for ROI '%s' failed and was disabled: %s", region.roi_name, msg)
        st = self.stats
        st.regions_drawn, st.skipped_no_mask, st.video_waiting = drawn, skipped, waiting
        return ops

    def _feathered_box(self, bbox, feather, W, H):
        box = _clip_box(*bbox, W, H)
        if feather > 0:
            f = int(math.ceil(feather))
            box = _clip_box(box[0] - f, box[1] - f, box[2] + f, box[3] + f, W, H)
        return box

    def _plan_one(self, region, spec: ReplacementSpec, assets, t, W, H):
        feather = spec.feather_px
        if spec.mode == "effect":
            box = self._feathered_box(region.bbox, feather, W, H)
            if box[2] <= box[0] or box[3] <= box[1]:
                return None
            mask, mkey, mrect = None, None, None
            if region.is_object and region.mask is not None:
                got = self._object_local_mask(region, spec, feather)
                if got is not None:
                    mask, mkey, mrect = got
            if mask is None:
                mask, mkey = self._static_mask(region, box, feather)
            params = {"blur": (spec.blur_sigma, 0, 0, 0), "pixelate": (float(spec.pixel_size), 0, 0, 0),
                      "color": (spec.brightness, spec.contrast, spec.saturation, 0)}[spec.effect]
            return DrawOp(region.roi_id, "effect", box, spec.opacity, mask=mask, mask_key=mkey, mask_rect=mrect,
                          effect=spec.effect, params=params)

        asset = assets.get(spec.asset_id)
        if asset is None:
            return None
        img, skey = self._source(asset, region, spec, t)
        if img is None:
            return "waiting"
        ah, aw = img.shape[:2]
        M = asset_affine(aw, ah, region, spec)
        cx0, cy0, cx1, cy1 = spec.crop
        ix0, iy0 = int(round(cx0 * aw)), int(round(cy0 * ah))
        ix1, iy1 = max(ix0 + 1, int(round(cx1 * aw))), max(iy0 + 1, int(round(cy1 * ah)))
        src = img
        if (ix0, iy0, ix1, iy1) != (0, 0, aw, ah):          # crop: sample only inside the crop rect
            src = np.ascontiguousarray(img[iy0:iy1, ix0:ix1])
            skey = skey + ("crop", ix0, iy0, ix1, iy1)
        inv = _invert(M)
        inv[:, 2] -= [ix0, iy0]                              # into cropped-source pixel coordinates
        if spec.mode == "overlay":
            corners = np.array([[ix0, iy0, 1], [ix1, iy0, 1], [ix1, iy1, 1], [ix0, iy1, 1]], float) @ M.T
            box = _clip_box(corners[:, 0].min(), corners[:, 1].min(), corners[:, 0].max(), corners[:, 1].max(), W, H)
        else:
            box = self._feathered_box(region.bbox, feather, W, H)
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            return None
        bg = (0.0, 0.0, 0.0, 0.0)
        mask, mkey, mrect = None, None, None
        if spec.mode in ("rect", "object"):
            b = np.array(spec.background, np.float64) / 255
            bg = (b[0] * b[3], b[1] * b[3], b[2] * b[3], b[3])
            if spec.mode == "object":
                if not region.is_object:
                    raise AssetError("Object replacement needs an Automatic or Hybrid region")
                got = self._object_local_mask(region, spec, feather)
                if got is None:
                    if spec.fallback_no_mask != "box":
                        return None
                    mask, mkey = self._static_mask(region, box, feather)
                else:
                    mask, mkey, mrect = got
            else:
                mask, mkey = self._static_mask(region, box, feather)
        return DrawOp(region.roi_id, "composite", box, spec.opacity, mask=mask, mask_key=mkey, mask_rect=mrect,
                      src=src, src_key=skey, inv=inv, bg=bg)

    # ------------------------------------------------------------------ render
    def render(self, frame: np.ndarray, regions: list, specs: dict[str, ReplacementSpec],
               assets: dict[str, Asset], t: float | None = None) -> np.ndarray:
        """BGRA array in -> new BGRA array out (input untouched)."""
        with self._lock:
            return self._render(frame, regions, specs, assets, t)

    def _render(self, frame, regions, specs, assets, t):
        t0 = time.perf_counter()
        H, W = frame.shape[:2]
        ops = self.plan(W, H, regions, specs, assets, t)
        t1 = time.perf_counter()
        out = None
        if self.active is self.metal and self.metal is not None:
            try:
                out = self.metal.execute(frame, ops)
            except Exception as e:
                self._metal_failed(e)
        if out is None:
            out = self.cpu.execute(frame, ops)
        self._finish(t0, t1)
        return out

    def render_pb(self, frame, regions: list, specs: dict[str, ReplacementSpec], assets: dict[str, Asset],
                  t: float | None = None):
        """Frame (pixel buffer or array) in -> CVPixelBuffer out (zero-copy path for the output pipeline)."""
        with self._lock:
            return self._render_pb(frame, regions, specs, assets, t)

    def _render_pb(self, frame, regions, specs, assets, t):
        from vcstudio.core.frame import Frame
        from vcstudio.core.pixelbuffer import pixel_buffer_for
        t0 = time.perf_counter()
        ops = self.plan(frame.width, frame.height, regions, specs, assets, t)
        t1 = time.perf_counter()
        if self.active is self.metal and self.metal is not None:
            try:
                out = self.metal.execute_pb(pixel_buffer_for(frame), frame.width, frame.height, ops)
                self._finish(t0, t1)
                return out
            except Exception as e:
                self._metal_failed(e)
        with frame.bgra() as a:
            arr = self.cpu.execute(a, ops)
        out = pixel_buffer_for(Frame(0, 0, 0, frame.width, frame.height, array=arr))
        self._finish(t0, t1)
        return out

    def _metal_failed(self, e: Exception) -> None:
        self.backend_error = f"Metal render failed, switched to CPU: {e}"
        log.error(self.backend_error)
        self.active = self.cpu
        self.stats.backend = "cpu"

    def _finish(self, t0: float, t1: float) -> None:
        st = self.stats
        st.plan_ms = (t1 - t0) * 1000
        st.render_ms = (time.perf_counter() - t0) * 1000
        st.gpu_ms = self.metal.ctx.last_gpu_ms if (self.active is self.metal and self.metal) else 0.0
        st.backend = self.active.name
        st.frames += 1
        if st.frames % 30 == 0:
            self.videos.sweep()
        st.video_players = len(self.videos.players())
