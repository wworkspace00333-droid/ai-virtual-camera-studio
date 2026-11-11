"""Frame model shared by all pipeline stages.

A frame either wraps a CVPixelBuffer (zero-copy from AVFoundation/IOSurface) or a NumPy array
(test pattern, decoded assets). Pixel access goes through `bgra()`, which locks the buffer for
the duration of the `with` block. The returned view MUST NOT escape the block.
"""
from __future__ import annotations

import contextlib
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np


def host_time_s() -> float:
    """Seconds on the host clock used by CoreMedia/AVFoundation (mach_absolute_time based)."""
    try:
        import CoreMedia as CM  # type: ignore
        return float(CM.CMTimeGetSeconds(CM.CMClockGetTime(CM.CMClockGetHostTimeClock())))
    except Exception:  # pragma: no cover
        return time.monotonic()


@dataclass
class Frame:
    seq: int
    pts: float                  # presentation time, host-clock seconds
    arrival: float              # host-clock seconds when the app received it
    width: int
    height: int
    pixel_format: str = "BGRA"
    pixel_buffer: Any = None    # CVPixelBufferRef (PyObjC keeps it retained)
    array: np.ndarray | None = None
    source: str = ""
    meta: dict = field(default_factory=dict)

    @contextlib.contextmanager
    def bgra(self) -> Iterator[np.ndarray]:
        """Read-only (H, W, 4) uint8 BGRA view."""
        if self.array is not None:
            yield self.array
            return
        if self.pixel_buffer is None:
            raise ValueError("frame has no pixel data")
        import Quartz as Q  # type: ignore
        pb = self.pixel_buffer
        if Q.CVPixelBufferLockBaseAddress(pb, Q.kCVPixelBufferLock_ReadOnly) != 0:
            raise RuntimeError("CVPixelBufferLockBaseAddress failed")
        try:
            bpr = Q.CVPixelBufferGetBytesPerRow(pb)
            h = Q.CVPixelBufferGetHeight(pb)
            w = Q.CVPixelBufferGetWidth(pb)
            buf = Q.CVPixelBufferGetBaseAddress(pb).as_buffer(bpr * h)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpr)[:, : w * 4].reshape(h, w, 4)
            arr.flags.writeable = False
            yield arr
        finally:
            Q.CVPixelBufferUnlockBaseAddress(pb, Q.kCVPixelBufferLock_ReadOnly)

    def signature(self, step: int = 24) -> int:
        """Cheap content fingerprint (subsampled CRC32) used for frozen-frame detection."""
        with self.bgra() as a:
            return zlib.crc32(np.ascontiguousarray(a[::step, ::step, :3]).data)

    def downscaled_copy(self, max_width: int) -> np.ndarray:
        """Contiguous BGRA copy with integer-step decimation so width <= max_width (preview use)."""
        with self.bgra() as a:
            step = max(1, -(-a.shape[1] // max(1, max_width)))
            return np.ascontiguousarray(a[::step, ::step])

    @property
    def age_ms(self) -> float:
        return (host_time_s() - self.arrival) * 1000.0
