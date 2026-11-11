"""Replacement asset library.

Imported files are copied into <app data>/assets/<id><ext> so profiles keep working if the
original file moves. Profiles reference assets by id (profile body "assets" list).
Images decode through Apple ImageIO (PNG, JPEG, WEBP, HEIC, TIFF...) into premultiplied BGRA.
"""
from __future__ import annotations

import functools
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".tif", ".tiff", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
MAX_IMAGE_SIDE = 4096          # larger images are downscaled on load (memory budget)


class AssetError(ValueError):
    pass


@dataclass
class Asset:
    id: str
    name: str
    file: str          # file name inside the assets dir
    kind: str          # image | video
    width: int = 0
    height: int = 0
    duration: float = 0.0       # video only
    fps: float = 0.0
    codec: str = ""
    decoder: str = ""           # avfoundation | ffmpeg
    hardware: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Asset":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def decode_image(path: Path) -> np.ndarray:
    """Premultiplied BGRA uint8 (H, W, 4)."""
    import Foundation  # type: ignore
    import Quartz as Q  # type: ignore
    src = Q.CGImageSourceCreateWithURL(Foundation.NSURL.fileURLWithPath_(str(path)), None)
    if src is None or Q.CGImageSourceGetCount(src) < 1:
        raise AssetError(f"Cannot decode image: {path.name}")
    img = Q.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        raise AssetError(f"Cannot decode image: {path.name}")
    w, h = int(Q.CGImageGetWidth(img)), int(Q.CGImageGetHeight(img))
    scale = min(1.0, MAX_IMAGE_SIDE / max(w, h))
    tw, th = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    buf = np.zeros((th, tw, 4), np.uint8)
    cs = Q.CGColorSpaceCreateWithName(Q.kCGColorSpaceSRGB)
    ctx = Q.CGBitmapContextCreate(buf, tw, th, 8, tw * 4, cs,
                                  Q.kCGImageAlphaPremultipliedFirst | Q.kCGBitmapByteOrder32Little)
    Q.CGContextSetInterpolationQuality(ctx, Q.kCGInterpolationHigh)
    Q.CGContextDrawImage(ctx, Q.CGRectMake(0, 0, tw, th), img)
    return buf


class AssetLibrary:
    def __init__(self, assets_dir: Path):
        self.dir = assets_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, asset: Asset) -> Path:
        cache = self.__dict__.setdefault("_path_cache", {})
        p = cache.get(asset.file)
        if p is None:
            p = cache[asset.file] = self.dir / asset.file      # Path joins showed up in per-frame profiles
        return p

    def import_file(self, src: Path) -> Asset:
        src = Path(src)
        ext = src.suffix.lower()
        if ext in IMAGE_EXTS:
            kind = "image"
        elif ext in VIDEO_EXTS:
            kind = "video"
        else:
            raise AssetError(f"Unsupported file type: {ext}")
        aid = uuid.uuid4().hex[:12]
        dest = self.dir / f"{aid}{ext}"
        shutil.copy2(src, dest)
        a = Asset(aid, src.stem, dest.name, kind)
        try:
            if kind == "image":
                img = decode_image(dest)
                a.height, a.width = img.shape[:2]
            else:
                from vcstudio.replacement.video import VideoError, probe
                try:
                    info = probe(dest)
                except VideoError as e:
                    raise AssetError(str(e))
                a.width, a.height, a.duration, a.fps = info.width, info.height, info.duration, info.fps
                a.codec, a.decoder, a.hardware = info.codec, info.decoder, info.hardware
                if info.duration <= 0:
                    raise AssetError(f"{src.name}: video has no duration")
        except AssetError:
            dest.unlink(missing_ok=True)
            raise
        return a

    def remove_file(self, asset: Asset) -> None:
        self.path(asset).unlink(missing_ok=True)
        self.__dict__.get("_stat_cache", {}).pop(self.path(asset), None)
        image_cache.cache_clear()

    def _mtime(self, p: Path) -> int:
        """File mtime, re-checked at most every 2 s (stat() showed up in per-frame profiles)."""
        import time as _t
        now = _t.monotonic()
        cache = self.__dict__.setdefault("_stat_cache", {})
        hit = cache.get(p)
        if hit and now - hit[0] < 2.0:
            return hit[1]
        if not p.exists():
            cache.pop(p, None)
            raise AssetError(f"Asset file missing: {p.name}")
        m = p.stat().st_mtime_ns
        cache[p] = (now, m)
        return m

    def image(self, asset: Asset) -> np.ndarray:
        p = self.path(asset)
        sp = self.__dict__.setdefault("_str_cache", {}).get(p)
        if sp is None:
            sp = self._str_cache[p] = str(p)
        return image_cache(sp, self._mtime(p))

    def video_info(self, asset: Asset):
        from vcstudio.replacement.video import probe
        p = self.path(asset)
        return video_info_cache(str(p), self._mtime(p))

    def thumbnail(self, asset: Asset, size: int = 96) -> np.ndarray:
        if asset.kind == "video":
            from vcstudio.replacement.video import thumbnail
            img = thumbnail(self.path(asset), max(size, 160))
            if img is None:
                raise AssetError(f"Cannot decode video: {asset.file}")
        else:
            img = self.image(asset)
        h, w = img.shape[:2]
        s = size / max(w, h)
        return cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)


@functools.lru_cache(maxsize=16)
def image_cache(path: str, _mtime: int) -> np.ndarray:
    a = decode_image(Path(path))
    a.flags.writeable = False
    return a


@functools.lru_cache(maxsize=32)
def video_info_cache(path: str, _mtime: int):
    from vcstudio.replacement.video import probe
    return probe(Path(path))


def assets_from_profile(body: dict) -> dict[str, Asset]:
    out = {}
    for d in body.get("assets", []):
        try:
            a = Asset.from_dict(d)
            out[a.id] = a
        except TypeError:
            continue
    return out
