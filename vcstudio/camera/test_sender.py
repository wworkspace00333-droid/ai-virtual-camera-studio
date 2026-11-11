"""Sends an animated, frame-numbered test pattern to the virtual camera (verification tool).

Lets users confirm in FaceTime / QuickTime / Zoom that "AI Virtual Camera" works before the full
processing pipeline (Stage 10) is running. Uses the same zero-copy CMIO sink path as the pipeline.
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

from vcstudio.camera.output import VirtualCameraOutput
from vcstudio.core.frame import host_time_s
from vcstudio.core.pixelbuffer import PixelBufferPool

log = logging.getLogger(__name__)


class TestPatternSender:
    __test__ = False

    def __init__(self, output: VirtualCameraOutput, width: int = 1920, height: int = 1080, fps: float = 60.0):
        self.output, self.w, self.h, self.fps = output, width, height, fps
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.sent = 0
        self.dropped = 0
        self.error = ""

    @property
    def running(self) -> bool:
        return self._t is not None and self._t.is_alive()

    def start(self) -> None:
        self.output.open()
        self._stop.clear()
        self._t = threading.Thread(target=self._run, name="camera-test-pattern", daemon=True)
        self._t.start()

    def _run(self) -> None:
        import cv2
        pool = PixelBufferPool(self.w, self.h)
        bars = np.array([[255, 255, 255], [0, 255, 255], [255, 255, 0], [0, 255, 0], [255, 0, 255],
                         [0, 0, 255], [255, 0, 0], [30, 30, 30]], np.uint8)
        base = np.empty((self.h, self.w, 4), np.uint8)
        base[..., :3] = bars[(np.arange(self.w) * 8 // self.w)][None]
        base[..., 3] = 255
        t0, n = time.perf_counter(), 0
        try:
            while not self._stop.is_set():
                img = base.copy()
                x = int((n * 12) % self.w)
                img[:, x:x + 24, :3] = 0
                cv2.putText(img, f"AI Virtual Camera  test pattern  #{n}", (60, self.h - 60),
                            cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255, 255), 5, cv2.LINE_AA)
                if self.output.send(pool.from_array(img), host_time_s()):
                    self.sent += 1
                else:
                    self.dropped += 1
                n += 1
                delay = t0 + n / self.fps - time.perf_counter()
                if delay > 0:
                    self._stop.wait(delay)
        except Exception as e:
            self.error = str(e)
            log.error("Test pattern sender stopped: %s", e)

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(2)
        self._t = None
        self.output.close()
