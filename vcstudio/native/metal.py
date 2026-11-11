"""ctypes bridge to libVCSNative (Swift + Metal GPU compositor).

The library is built by scripts/build_native.sh into build/native/ (or shipped inside the app
bundle's Frameworks folder). If it is missing or fails to initialise, `available()` is False and the
compositor uses the CPU backend; the UI states which backend is active.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

LAYER_FLOATS = 28
from vcstudio.core.resources import native_lib

CANDIDATES = [
    Path(os.environ["VCS_NATIVE_LIB"]) if os.environ.get("VCS_NATIVE_LIB") else None,
    native_lib("libVCSNative.dylib"),
]

_lib = None
_lib_error = ""
_lock = threading.Lock()


def _load():
    global _lib, _lib_error
    with _lock:
        if _lib is not None or _lib_error:
            return _lib
        for p in CANDIDATES:
            if p is not None and p.exists():
                try:
                    lib = ctypes.CDLL(str(p))
                    lib.vcs_create.restype = ctypes.c_void_p
                    lib.vcs_destroy.argtypes = [ctypes.c_void_p]
                    lib.vcs_device_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32]
                    lib.vcs_last_error.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32]
                    lib.vcs_upload.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_int32,
                                               ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
                    lib.vcs_upload.restype = ctypes.c_int32
                    lib.vcs_release_slot.argtypes = [ctypes.c_void_p, ctypes.c_int32]
                    lib.vcs_render.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p]
                    lib.vcs_render.restype = ctypes.c_int32
                    lib.vcs_scale.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
                                              ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_float]
                    lib.vcs_scale.restype = ctypes.c_int32
                    lib.vcs_flush.argtypes = [ctypes.c_void_p, ctypes.c_int32]
                    _lib = lib
                    log.info("Loaded native compositor %s", p)
                    return _lib
                except OSError as e:
                    _lib_error = f"{p}: {e}"
        if not _lib_error:
            _lib_error = "libVCSNative.dylib not found (run scripts/build_native.sh)"
        log.warning("Metal compositor unavailable: %s", _lib_error)
        return None


def available() -> bool:
    return _load() is not None


def load_error() -> str:
    _load()
    return _lib_error


def pb_ptr(pb) -> int:
    """Raw CVPixelBufferRef pointer of a PyObjC pixel buffer (borrowed; caller keeps `pb` alive)."""
    return pb.__c_void_p__().value


class MetalContext:
    """One GPU context: texture slots + render calls. Not thread-safe; use from one thread."""

    def __init__(self):
        lib = _load()
        if lib is None:
            raise RuntimeError(load_error())
        self._lib = lib
        self._h = lib.vcs_create()
        if not self._h:
            raise RuntimeError("Metal context creation failed (no GPU device or shader compile error)")
        buf = ctypes.create_string_buffer(256)
        lib.vcs_device_name(self._h, buf, 256)
        self.device_name = buf.value.decode()
        self.last_cpu_ms = 0.0
        self.last_gpu_ms = 0.0

    def error(self) -> str:
        buf = ctypes.create_string_buffer(512)
        self._lib.vcs_last_error(self._h, buf, 512)
        return buf.value.decode()

    def upload(self, slot: int, arr: np.ndarray) -> None:
        a = np.ascontiguousarray(arr)
        if a.ndim == 2:
            fmt, stride = 1, a.shape[1]
        elif a.ndim == 3 and a.shape[2] == 4:
            fmt, stride = 0, a.shape[1] * 4
        else:
            raise ValueError("upload expects (H, W) uint8 masks or (H, W, 4) BGRA")
        if a.dtype != np.uint8:
            raise ValueError("uint8 required")
        rc = self._lib.vcs_upload(self._h, slot, a.ctypes.data, a.shape[1], a.shape[0], stride, fmt)
        if rc != 0:
            raise RuntimeError(f"texture upload failed ({rc})")

    def release(self, slot: int) -> None:
        self._lib.vcs_release_slot(self._h, slot)

    def render(self, src_pb, dst_pb, layers: np.ndarray, slots: np.ndarray) -> None:
        layers = np.ascontiguousarray(layers, dtype=np.float32)
        slots = np.ascontiguousarray(slots, dtype=np.int32)
        n = len(layers)
        timing = (ctypes.c_double * 2)()
        rc = self._lib.vcs_render(self._h, pb_ptr(src_pb), pb_ptr(dst_pb),
                                  layers.ctypes.data if n else None, slots.ctypes.data if n else None, n, timing)
        if rc != 0:
            raise RuntimeError(f"Metal render failed ({rc}): {self.error()}")
        self.last_cpu_ms, self.last_gpu_ms = timing[0], timing[1]

    def scale(self, src_pb, dst_pb, x: int, y: int, w: int, h: int, fill: float = -1.0) -> None:
        """GPU bilinear scale of src into the (x, y, w, h) rect of dst; fill >= 0 clears dst to that gray first."""
        rc = self._lib.vcs_scale(self._h, pb_ptr(src_pb), pb_ptr(dst_pb), x, y, w, h, fill)
        if rc != 0:
            raise RuntimeError(f"GPU scale failed ({rc})")

    def flush(self, drop_slots: bool = False) -> None:
        self._lib.vcs_flush(self._h, 1 if drop_slots else 0)

    def close(self) -> None:
        if self._h:
            self._lib.vcs_destroy(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


_shared: "MetalContext | None" = None
_shared_lock = threading.Lock()
_tls = threading.local()


def scaler_context() -> "MetalContext | None":
    """A per-thread Metal context for scaling (the AI and UI threads must not share the compositor's)."""
    ctx = getattr(_tls, "ctx", None)
    if ctx is None and available():
        try:
            ctx = MetalContext()
        except RuntimeError:
            ctx = None
        _tls.ctx = ctx
    return ctx
