"""Send processed frames to the "AI Virtual Camera" system extension (CMIO sink stream).

Thin wrapper over libVCSNative's CMIO sink client. Frames must be IOSurface-backed BGRA
CVPixelBuffers (the Metal compositor produces exactly that), so nothing is copied on the way.
"""
from __future__ import annotations

import ctypes
import logging

from vcstudio import CAMERA_NAME

log = logging.getLogger(__name__)
SINK_NAME = "AI Virtual Camera Input"

OPEN_ERRORS = {
    -1: "the camera device is not installed or not visible (install and approve the camera extension)",
    -2: "the camera device has no sink stream (wrong or outdated extension)",
    -3: "could not open the sink buffer queue",
    -4: "could not start the sink stream",
}


class CameraOutputError(RuntimeError):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def _lib():
    from vcstudio.native import metal
    lib = metal._load()
    if lib is None:
        raise CameraOutputError(-100, f"native library unavailable: {metal.load_error()}")
    if not getattr(lib, "_cam_ready", False):
        lib.vcs_cam_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int32)]
        lib.vcs_cam_open.restype = ctypes.c_void_p
        lib.vcs_cam_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_double]
        lib.vcs_cam_send.restype = ctypes.c_int32
        lib.vcs_cam_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)]
        lib.vcs_cam_close.argtypes = [ctypes.c_void_p]
        lib.vcs_cam_list_devices.argtypes = [ctypes.c_char_p, ctypes.c_int32]
        lib.vcs_cam_list_streams.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32]
        lib._cam_ready = True
    return lib


def list_cmio_devices() -> list[str]:
    buf = ctypes.create_string_buffer(8192)
    n = _lib().vcs_cam_list_devices(buf, len(buf))
    return [s for s in buf.value.decode().split("\n") if s] if n > 0 else []


def list_streams(device: str) -> list[str]:
    buf = ctypes.create_string_buffer(8192)
    n = _lib().vcs_cam_list_streams(device.encode(), buf, len(buf))
    return [s for s in buf.value.decode().split("\n") if s] if n > 0 else []


class VirtualCameraOutput:
    def __init__(self, device: str = CAMERA_NAME, sink: str = SINK_NAME):
        self.device, self.sink = device, sink
        self._h = None

    @property
    def is_open(self) -> bool:
        return self._h is not None

    def open(self) -> None:
        if self._h:
            return
        status = ctypes.c_int32(0)
        h = _lib().vcs_cam_open(self.device.encode(), self.sink.encode(), ctypes.byref(status))
        if not h:
            raise CameraOutputError(status.value, f"{self.device}: {OPEN_ERRORS.get(status.value, status.value)}")
        self._h = h
        log.info("Virtual camera output opened: %s / %s", self.device, self.sink)

    def send(self, pixel_buffer, pts_seconds: float) -> bool:
        """True if enqueued, False if dropped because the extension's queue was full."""
        if not self._h:
            raise CameraOutputError(-10, "output not open")
        rc = _lib().vcs_cam_send(self._h, pixel_buffer.__c_void_p__().value, pts_seconds)
        if rc < 0:
            raise CameraOutputError(rc, f"send failed ({rc})")
        return rc == 0

    def stats(self) -> tuple[int, int]:
        if not self._h:
            return (0, 0)
        out = (ctypes.c_int64 * 2)()
        _lib().vcs_cam_stats(self._h, out)
        return out[0], out[1]

    def close(self) -> None:
        if self._h:
            _lib().vcs_cam_close(self._h)
            self._h = None
            log.info("Virtual camera output closed")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
