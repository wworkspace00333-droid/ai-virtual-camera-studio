"""CVPixelBuffer helpers: wrap NumPy BGRA frames in IOSurface-backed pixel buffers (pooled)."""
from __future__ import annotations

import threading

import numpy as np


class PixelBufferPool:
    """Small CVPixelBufferPool for a fixed size; avoids per-frame allocations."""

    def __init__(self, width: int, height: int):
        import Quartz as Q  # type: ignore
        self.width, self.height = width, height
        attrs = {
            Q.kCVPixelBufferPixelFormatTypeKey: Q.kCVPixelFormatType_32BGRA,
            Q.kCVPixelBufferWidthKey: width,
            Q.kCVPixelBufferHeightKey: height,
            Q.kCVPixelBufferIOSurfacePropertiesKey: {},
            Q.kCVPixelBufferMetalCompatibilityKey: True,
        }
        err, self._pool = Q.CVPixelBufferPoolCreate(None, {Q.kCVPixelBufferPoolMinimumBufferCountKey: 3}, attrs, None)
        if err != 0:
            raise RuntimeError(f"CVPixelBufferPoolCreate failed: {err}")

    def from_array(self, arr: np.ndarray):
        import Quartz as Q  # type: ignore
        h, w = arr.shape[:2]
        if (w, h) != (self.width, self.height) or arr.shape[2] != 4 or arr.dtype != np.uint8:
            raise ValueError("array does not match pool format")
        err, pb = Q.CVPixelBufferPoolCreatePixelBuffer(None, self._pool, None)
        if err != 0:
            raise RuntimeError(f"CVPixelBufferPoolCreatePixelBuffer failed: {err}")
        Q.CVPixelBufferLockBaseAddress(pb, 0)
        try:
            bpr = Q.CVPixelBufferGetBytesPerRow(pb)
            buf = Q.CVPixelBufferGetBaseAddress(pb).as_buffer(bpr * h)
            dst = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpr)[:, : w * 4].reshape(h, w, 4)
            dst[...] = arr
        finally:
            Q.CVPixelBufferUnlockBaseAddress(pb, 0)
        return pb


_pools: dict[tuple[int, int], PixelBufferPool] = {}
_lock = threading.Lock()


def pixel_buffer_for(frame):
    """CVPixelBuffer for a Frame (zero-copy if it already has one; pooled copy for arrays)."""
    if frame.pixel_buffer is not None:
        return frame.pixel_buffer
    if frame.array is None:
        raise ValueError("frame has no pixel data")
    key = (frame.width, frame.height)
    with _lock:
        pool = _pools.get(key)
        if pool is None:
            if len(_pools) > 4:
                _pools.clear()
            pool = _pools[key] = PixelBufferPool(*key)
    return pool.from_array(frame.array)
