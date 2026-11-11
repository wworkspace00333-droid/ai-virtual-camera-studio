"""Load images into BGRA arrays via ImageIO (no OpenCV/Pillow dependency)."""
from __future__ import annotations

from pathlib import Path

import numpy as np


def load_bgra(path: str | Path) -> np.ndarray:
    import Foundation  # type: ignore
    import Quartz as Q  # type: ignore
    p = Path(path)
    if not p.is_absolute():
        from vcstudio.core.resources import REPO, sample
        p = sample(p.name) if p.parts[:2] == ("tests", "data") else REPO / p
    src = Q.CGImageSourceCreateWithURL(Foundation.NSURL.fileURLWithPath_(str(p)), None)
    if src is None:
        raise FileNotFoundError(p)
    img = Q.CGImageSourceCreateImageAtIndex(src, 0, None)
    w, h = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img)
    buf = np.zeros((h, w, 4), np.uint8)
    cs = Q.CGColorSpaceCreateDeviceRGB()
    ctx = Q.CGBitmapContextCreate(buf, w, h, 8, w * 4, cs, Q.kCGImageAlphaPremultipliedFirst | Q.kCGBitmapByteOrder32Little)
    Q.CGContextDrawImage(ctx, Q.CGRectMake(0, 0, w, h), img)
    return buf
