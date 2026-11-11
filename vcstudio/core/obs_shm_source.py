"""Input method 2: direct frame transfer from OBS through the vcstudio-obs plugin (shared memory).

The plugin publishes OBS's program output as BGRA frames into POSIX shared memory (triple buffer +
seqlock, native/obs-plugin/vcs_frame_shm.h). libVCSShm waits for a new frame and copies it straight
into an IOSurface CVPixelBuffer with the GIL released; from there the frame follows the same zero-copy
path as AVFoundation capture. Unlike OBS Virtual Camera, this does not occupy OBS's virtual camera,
and timestamps are OBS's own render timestamps.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from pathlib import Path

from vcstudio.core.frame import Frame, host_time_s
from vcstudio.core.video_capture import CaptureError, CaptureSource

log = logging.getLogger(__name__)
SHM_NAME = "/vcs-obs-frames"
STALE_S = 1.0


class FrameInfo(ctypes.Structure):
    _fields_ = [("frame", ctypes.c_uint64), ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
                ("stride", ctypes.c_uint32), ("format", ctypes.c_uint32), ("timestamp_ns", ctypes.c_uint64),
                ("fps_num", ctypes.c_uint32), ("fps_den", ctypes.c_uint32), ("writer_pid", ctypes.c_int32),
                ("active", ctypes.c_uint32), ("heartbeat_ns", ctypes.c_uint64)]


_lib = None


def shm_lib():
    global _lib
    if _lib is None:
        from vcstudio.core.resources import native_lib
        for p in (native_lib("libVCSShm.dylib"),):
            if p.exists():
                lib = ctypes.CDLL(str(p))
                lib.vcs_shm_reader_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
                lib.vcs_shm_reader_open.restype = ctypes.c_void_p
                lib.vcs_shm_writer_open.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32,
                                                    ctypes.POINTER(ctypes.c_int)]
                lib.vcs_shm_writer_open.restype = ctypes.c_void_p
                lib.vcs_shm_close.argtypes = [ctypes.c_void_p]
                lib.vcs_shm_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(FrameInfo)]
                lib.vcs_shm_wait.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int, ctypes.POINTER(ctypes.c_uint64)]
                lib.vcs_shm_wait.restype = ctypes.c_int
                lib.vcs_shm_copy_to_pixelbuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(FrameInfo)]
                lib.vcs_shm_copy_to_pixelbuffer.restype = ctypes.c_int
                lib.vcs_shm_writer_publish.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                       ctypes.c_uint32, ctypes.c_uint64]
                lib.vcs_shm_writer_publish.restype = ctypes.c_int
                lib.vcs_shm_writer_set_active.argtypes = [ctypes.c_void_p, ctypes.c_int]
                lib.vcs_shm_unlink.argtypes = [ctypes.c_char_p]
                lib.vcs_shm_now_ns.restype = ctypes.c_uint64
                _lib = lib
                break
    return _lib


def obs_sharing_status(name: str = SHM_NAME) -> dict:
    """{'available': bool, 'reason': str, ...frame info} without starting capture."""
    lib = shm_lib()
    if lib is None:
        return {"available": False, "reason": "libVCSShm not built (scripts/build_native.sh)"}
    st = ctypes.c_int(0)
    h = lib.vcs_shm_reader_open(name.encode(), ctypes.byref(st))
    if not h:
        return {"available": False, "reason": {-1: "OBS is not sharing frames (install the vcstudio-obs plugin and "
                                                    "enable Tools > AI Virtual Camera Studio: share frames)",
                                               -2: "shared memory has an unexpected size",
                                               -3: "incompatible plugin protocol version"}.get(st.value, str(st.value))}
    info = FrameInfo()
    lib.vcs_shm_info(h, ctypes.byref(info))
    age = (lib.vcs_shm_now_ns() - info.heartbeat_ns) / 1e9 if info.heartbeat_ns else None
    lib.vcs_shm_close(h)
    live = bool(info.active) and age is not None and age < STALE_S
    return {"available": live, "reason": "" if live else ("OBS stopped sharing" if not info.active else "no recent frames"),
            "width": info.width, "height": info.height, "fps": (info.fps_num / info.fps_den) if info.fps_den else 0,
            "writer_pid": info.writer_pid, "frames": info.frame, "age_s": age}


class ObsShmSource(CaptureSource):
    kind = "obs_ipc"

    def __init__(self, name: str = SHM_NAME):
        self.shm_name = name
        self.name = "OBS (direct frame transfer)"
        self._h = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._nominal = (0, 0, 0.0)
        self._pools: dict = {}

    @property
    def nominal(self):
        return self._nominal

    def is_present(self) -> bool:
        return obs_sharing_status(self.shm_name)["available"]

    def start(self, on_frame, on_event) -> None:
        lib = shm_lib()
        if lib is None:
            raise CaptureError("libVCSShm not built (scripts/build_native.sh)")
        status = obs_sharing_status(self.shm_name)
        if not status["available"]:
            raise CaptureError(status["reason"])
        st = ctypes.c_int(0)
        self._h = lib.vcs_shm_reader_open(self.shm_name.encode(), ctypes.byref(st))
        if not self._h:
            raise CaptureError(f"cannot open OBS shared memory ({st.value})")
        self._nominal = (status["width"], status["height"], status["fps"])
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_frame, on_event), name="obs-shm-reader", daemon=True)
        self._thread.start()
        log.info("OBS direct frame transfer: %dx%d @ %.2f fps (OBS pid %s)", status["width"], status["height"],
                 status["fps"], status["writer_pid"])

    def _pixel_buffer(self, w: int, h: int):
        import Quartz as Q  # type: ignore
        from vcstudio.core.pixelbuffer import PixelBufferPool
        pool = self._pools.get((w, h))
        if pool is None:
            self._pools.clear()
            pool = self._pools[(w, h)] = PixelBufferPool(w, h)
        err, pb = Q.CVPixelBufferPoolCreatePixelBuffer(None, pool._pool, None)
        if err != 0:
            raise CaptureError(f"pixel buffer allocation failed ({err})")
        return pb

    def _run(self, on_frame, on_event) -> None:
        import objc  # type: ignore
        lib = shm_lib()
        last, seq = 0, 0
        info = FrameInfo()
        out = ctypes.c_uint64(0)
        while not self._stop.is_set():
            with objc.autorelease_pool():
                rc = lib.vcs_shm_wait(self._h, last, 200, ctypes.byref(out))
                if rc == 2:
                    on_event("disconnected", "OBS stopped sharing frames")
                    self._stop.wait(0.2)
                    continue
                if rc != 0:
                    continue                              # timeout: supervisor detects missing input
                lib.vcs_shm_info(self._h, ctypes.byref(info))
                if not info.width or not info.height:
                    last = out.value
                    continue
                pb = self._pixel_buffer(info.width, info.height)
                rc = lib.vcs_shm_copy_to_pixelbuffer(self._h, pb.__c_void_p__().value, ctypes.byref(info))
                if rc == 1:                               # resolution changed between info and copy
                    continue
                if rc != 0:
                    continue
                last = info.frame
                seq += 1
                pts = info.timestamp_ns / 1e9
                now = host_time_s()
                on_frame(Frame(seq, pts if 0 < now - pts < 1.0 else now, now, info.width, info.height,
                               pixel_buffer=pb, source=self.name))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self._thread = None
        if self._h:
            shm_lib().vcs_shm_close(self._h)
            self._h = None
