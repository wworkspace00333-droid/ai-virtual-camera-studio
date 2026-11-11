"""Video capture sources.

- AVFoundationSource: real capture from any AVFoundation device (OBS Virtual Camera, webcams).
  Frames stay in their IOSurface-backed CVPixelBuffers (no copy on the capture path).
- TestPatternSource: a synthetic moving pattern for diagnostics and automated tests. It shows up in
  the UI as "Test pattern", never as a camera.

Sources push frames into a callback; the CaptureSupervisor (capture_supervisor.py) owns lifecycle,
health monitoring and reconnects.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from vcstudio.core.devices import is_own_output
from vcstudio.core.frame import Frame, host_time_s

log = logging.getLogger(__name__)

FrameCallback = Callable[[Frame], None]
EventCallback = Callable[[str, str], None]   # (kind, message): "error" | "dropped" | "disconnected"


class CaptureError(RuntimeError):
    pass


class PermissionDenied(CaptureError):
    pass


class FeedbackLoopRefused(CaptureError):
    pass


@dataclass(frozen=True)
class DeviceFormat:
    width: int
    height: int
    subtype: str
    min_fps: float
    max_fps: float

    @property
    def label(self) -> str:
        fps = f"{self.max_fps:g}" if self.min_fps == self.max_fps else f"{self.min_fps:g}–{self.max_fps:g}"
        return f"{self.width}×{self.height} {self.subtype} @ {fps} fps"


def choose_format(formats: list[DeviceFormat], width: int, height: int, fps: float) -> DeviceFormat | None:
    """Best match: exact size supporting fps > exact size (highest fps) > closest area supporting fps."""
    if not formats:
        return None

    def supports(f: DeviceFormat) -> bool:
        return f.min_fps - 0.01 <= fps <= f.max_fps + 0.01

    exact = [f for f in formats if f.width == width and f.height == height]
    for f in exact:
        if supports(f):
            return f
    if exact:
        return max(exact, key=lambda f: f.max_fps)
    target = width * height
    ok = [f for f in formats if supports(f)] or formats
    return min(ok, key=lambda f: (abs(f.width * f.height - target), -f.max_fps))


class CaptureSource:
    """Base interface."""
    kind = "base"
    name = ""

    def start(self, on_frame: FrameCallback, on_event: EventCallback) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def is_present(self) -> bool:
        return True

    @property
    def nominal(self) -> tuple[int, int, float]:
        return (0, 0, 0.0)


# ----------------------------------------------------------------------------------------------
# Camera permission
# ----------------------------------------------------------------------------------------------

def request_camera_access(timeout: float = 60.0) -> bool:
    """Ask macOS for camera access (shows the system prompt once). Blocks up to `timeout`."""
    import AVFoundation as AV  # type: ignore
    status = int(AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeVideo))
    if status == 3:
        return True
    if status in (1, 2):
        return False
    done = threading.Event()
    result = {"granted": False}

    def handler(granted):
        result["granted"] = bool(granted)
        done.set()

    AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeVideo, handler)
    if threading.current_thread() is threading.main_thread():
        # Keep the main run loop alive so the prompt can be shown.
        from Foundation import NSDate, NSRunLoop  # type: ignore
        end = time.monotonic() + timeout
        while not done.is_set() and time.monotonic() < end:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
    else:
        done.wait(timeout)
    return result["granted"]


# ----------------------------------------------------------------------------------------------
# AVFoundation
# ----------------------------------------------------------------------------------------------

def _fourcc(code: int) -> str:
    try:
        s = int(code).to_bytes(4, "big").decode("ascii")
        return s if s.isprintable() else str(code)
    except (ValueError, OverflowError, UnicodeDecodeError):
        return str(code)


def find_av_device(unique_id: str | None, name_hint: str | None):
    import AVFoundation as AV  # type: ignore
    devices = list(AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo) or [])
    if unique_id:
        for d in devices:
            if str(d.uniqueID()) == unique_id:
                return d
    if name_hint:
        for d in devices:
            if name_hint.casefold() in str(d.localizedName()).casefold():
                return d
    return None


def device_formats(device) -> list[DeviceFormat]:
    import CoreMedia as CM  # type: ignore
    out = []
    for f in device.formats():
        desc = f.formatDescription()
        dims = CM.CMVideoFormatDescriptionGetDimensions(desc)
        sub = _fourcc(CM.CMFormatDescriptionGetMediaSubType(desc))
        ranges = list(f.videoSupportedFrameRateRanges())
        if not ranges:
            continue
        out.append(DeviceFormat(int(dims.width), int(dims.height), sub,
                                min(float(r.minFrameRate()) for r in ranges),
                                max(float(r.maxFrameRate()) for r in ranges)))
    return out


def formats_for_device_id(unique_id: str) -> list[DeviceFormat]:
    d = find_av_device(unique_id, None)
    return device_formats(d) if d is not None else []


def _make_delegate_class():
    """Create the Objective-C delegate class once (PyObjC classes are global)."""
    import objc  # type: ignore
    from Foundation import NSObject  # type: ignore

    try:
        return objc.lookUpClass("VCSCaptureDelegate")
    except objc.nosuchclass_error:
        pass

    class VCSCaptureDelegate(NSObject):
        def initWithOwner_(self, owner):
            self = objc.super(VCSCaptureDelegate, self).init()
            if self is None:
                return None
            self.owner = owner
            return self

        def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sbuf, conn):
            owner = self.owner
            if owner is not None:
                owner._on_sample(sbuf)

        def captureOutput_didDropSampleBuffer_fromConnection_(self, output, sbuf, conn):
            owner = self.owner
            if owner is not None:
                owner._on_drop()

    return VCSCaptureDelegate


class AVFoundationSource(CaptureSource):
    kind = "avfoundation"

    def __init__(self, unique_id: str | None, name_hint: str | None = None, width: int = 1920,
                 height: int = 1080, fps: float = 60.0, allow_own_output: bool = False):
        self.unique_id, self.name_hint = unique_id, name_hint
        self.allow_own_output = allow_own_output   # only for self-tests that verify the camera output
        self.want = (width, height, fps)
        self.name = name_hint or unique_id or "?"
        self._session = None
        self._output = None
        self._delegate = None
        self._queue = None
        self._observers: list = []
        self._on_frame: FrameCallback | None = None
        self._on_event: EventCallback | None = None
        self._seq = 0
        self._nominal = (0, 0, 0.0)
        self.selected_format: DeviceFormat | None = None
        self._lock = threading.Lock()

    @property
    def nominal(self) -> tuple[int, int, float]:
        return self._nominal

    def is_present(self) -> bool:
        return find_av_device(self.unique_id, None if self.unique_id else self.name_hint) is not None

    def start(self, on_frame: FrameCallback, on_event: EventCallback) -> None:
        import AVFoundation as AV  # type: ignore
        import libdispatch  # type: ignore
        import Quartz as Q  # type: ignore
        from Foundation import NSNotificationCenter  # type: ignore

        if not request_camera_access(timeout=60):
            raise PermissionDenied("Camera access is not granted. Enable it in System Settings → "
                                   "Privacy & Security → Camera.")
        dev = find_av_device(self.unique_id, self.name_hint)
        if dev is None:
            raise CaptureError(f"Video device not found: {self.name_hint or self.unique_id}")
        self.name = str(dev.localizedName())
        self.unique_id = str(dev.uniqueID())
        if is_own_output(self.name, str(dev.modelID())) and not self.allow_own_output:
            raise FeedbackLoopRefused(f"Refusing to capture from '{self.name}': it is this app's own output.")

        session = AV.AVCaptureSession.alloc().init()
        session.beginConfiguration()
        inp, err = AV.AVCaptureDeviceInput.deviceInputWithDevice_error_(dev, None)
        if inp is None or not session.canAddInput_(inp):
            raise CaptureError(f"Cannot open {self.name}: {err.localizedDescription() if err else 'canAddInput failed'}")
        session.addInput_(inp)

        w, h, fps = self.want
        fmts = device_formats(dev)
        chosen = choose_format(fmts, w, h, fps)
        if chosen is not None:
            idx = fmts.index(chosen)
            ok, err = dev.lockForConfiguration_(None)
            if ok:
                try:
                    dev.setActiveFormat_(dev.formats()[idx])
                    rate = min(max(fps, chosen.min_fps), chosen.max_fps)
                    import CoreMedia as CM  # type: ignore
                    dur = CM.CMTimeMake(1000, int(round(rate * 1000)))
                    try:
                        dev.setActiveVideoMinFrameDuration_(dur)
                        dev.setActiveVideoMaxFrameDuration_(dur)
                    except Exception as e:  # some virtual cameras reject explicit durations
                        log.info("Frame duration not settable on %s: %s", self.name, e)
                finally:
                    dev.unlockForConfiguration()
            else:
                log.warning("lockForConfiguration failed on %s: %s", self.name, err)
            self.selected_format = chosen
            self._nominal = (chosen.width, chosen.height, min(max(fps, chosen.min_fps), chosen.max_fps))

        out = AV.AVCaptureVideoDataOutput.alloc().init()
        out.setAlwaysDiscardsLateVideoFrames_(True)   # never queue stale frames
        out.setVideoSettings_({
            Q.kCVPixelBufferPixelFormatTypeKey: Q.kCVPixelFormatType_32BGRA,
            Q.kCVPixelBufferMetalCompatibilityKey: True,
            Q.kCVPixelBufferIOSurfacePropertiesKey: {},
        })
        delegate = _make_delegate_class().alloc().initWithOwner_(self)
        queue = libdispatch.dispatch_queue_create(b"com.aivcstudio.capture", None)
        out.setSampleBufferDelegate_queue_(delegate, queue)
        if not session.canAddOutput_(out):
            raise CaptureError("Cannot add video data output")
        session.addOutput_(out)
        session.commitConfiguration()

        nc = NSNotificationCenter.defaultCenter()

        def on_runtime_error(note):
            e = note.userInfo().get(AV.AVCaptureSessionErrorKey) if note.userInfo() else None
            self._emit("error", f"AVCaptureSession runtime error: {e.localizedDescription() if e else note.name()}")

        def on_disconnect(note):
            d = note.object()
            if d is not None and str(d.uniqueID()) == self.unique_id:
                self._emit("disconnected", f"{self.name} was disconnected")

        def on_interrupt(note):
            self._emit("error", "Capture session was interrupted")

        self._observers = [
            nc.addObserverForName_object_queue_usingBlock_(AV.AVCaptureSessionRuntimeErrorNotification, session, None, on_runtime_error),
            nc.addObserverForName_object_queue_usingBlock_(AV.AVCaptureDeviceWasDisconnectedNotification, None, None, on_disconnect),
            nc.addObserverForName_object_queue_usingBlock_(AV.AVCaptureSessionWasInterruptedNotification, session, None, on_interrupt),
        ]
        with self._lock:
            self._on_frame, self._on_event = on_frame, on_event
            self._session, self._output, self._delegate, self._queue = session, out, delegate, queue
        session.startRunning()
        if not session.isRunning():
            self.stop()
            raise CaptureError(f"Capture session for {self.name} did not start")
        log.info("AVFoundation capture started: %s (%s)", self.name,
                 self.selected_format.label if self.selected_format else "device default format")

    def _emit(self, kind: str, msg: str) -> None:
        cb = self._on_event
        if cb:
            cb(kind, msg)

    def _on_drop(self) -> None:
        self._emit("dropped", "")

    def _on_sample(self, sbuf) -> None:
        import objc  # type: ignore
        # Defence in depth: drain Objective-C temporaries per frame. Measured on the M5 with
        # OBS Virtual Camera at 1080p60, capture memory is flat with or without this (GCD drains
        # its own pool), and the per-frame pool costs nothing measurable.
        with objc.autorelease_pool():
            self._on_sample_impl(sbuf)

    def _on_sample_impl(self, sbuf) -> None:
        import CoreMedia as CM  # type: ignore
        cb = self._on_frame
        if cb is None:
            return
        pb = CM.CMSampleBufferGetImageBuffer(sbuf)
        if pb is None:
            return
        import Quartz as Q  # type: ignore
        pts = CM.CMSampleBufferGetPresentationTimeStamp(sbuf)
        pts_s = float(CM.CMTimeGetSeconds(pts)) if (pts.flags & 1) else host_time_s()
        self._seq += 1
        frame = Frame(seq=self._seq, pts=pts_s, arrival=host_time_s(),
                      width=int(Q.CVPixelBufferGetWidth(pb)), height=int(Q.CVPixelBufferGetHeight(pb)),
                      pixel_format=_fourcc(Q.CVPixelBufferGetPixelFormatType(pb)),
                      pixel_buffer=pb, source=self.name)
        try:
            cb(frame)
        except Exception:
            log.exception("frame callback failed")

    def stop(self) -> None:
        from Foundation import NSNotificationCenter  # type: ignore
        with self._lock:
            session, out, obs = self._session, self._output, self._observers
            self._session = self._output = None
            self._on_frame = None
            self._observers = []
        nc = NSNotificationCenter.defaultCenter()
        for o in obs:
            nc.removeObserver_(o)
        if out is not None:
            out.setSampleBufferDelegate_queue_(None, None)
        if session is not None:
            session.stopRunning()
            log.info("AVFoundation capture stopped: %s", self.name)
        if self._delegate is not None:
            self._delegate.owner = None
        self._delegate = None
        self._on_event = None


# ----------------------------------------------------------------------------------------------
# Test pattern
# ----------------------------------------------------------------------------------------------

class TestPatternSource(CaptureSource):
    """Synthetic BGRA source at a fixed rate. Controls let tests simulate stalls and freezes."""
    kind = "test_pattern"
    __test__ = False  # not a pytest class

    def __init__(self, width: int = 1920, height: int = 1080, fps: float = 60.0):
        self.name = "Test pattern"
        self.width, self.height, self.fps = width, height, fps
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.stalled = threading.Event()   # set => no frames delivered (simulates missing input)
        self.frozen = threading.Event()    # set => identical frames delivered
        self.present = True
        self.fail_start = 0                # number of upcoming start() calls that should fail
        bars = np.array([[255, 255, 255], [0, 255, 255], [255, 255, 0], [0, 255, 0],
                         [255, 0, 255], [0, 0, 255], [255, 0, 0], [16, 16, 16]], np.uint8)  # BGR
        cols = (np.arange(width) * len(bars) // width)
        base = np.empty((height, width, 4), np.uint8)
        base[..., :3] = bars[cols][None, :, :]
        base[..., 3] = 255
        self._base = base

    @property
    def nominal(self) -> tuple[int, int, float]:
        return (self.width, self.height, self.fps)

    def is_present(self) -> bool:
        return self.present

    def start(self, on_frame: FrameCallback, on_event: EventCallback) -> None:
        if self.fail_start > 0:
            self.fail_start -= 1
            raise CaptureError("simulated start failure")
        if not self.present:
            raise CaptureError("test pattern device not present")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_frame,), name="test-pattern", daemon=True)
        self._thread.start()

    def _run(self, on_frame: FrameCallback) -> None:
        period = 1.0 / self.fps
        nxt = time.perf_counter()
        seq = 0
        last: np.ndarray | None = None
        bar_h = max(8, self.height // 12)
        while not self._stop.is_set():
            nxt += period
            if not self.stalled.is_set():
                if self.frozen.is_set() and last is not None:
                    img = last
                else:
                    seq += 1
                    img = self._base.copy()
                    x = (seq * 8) % self.width
                    img[-bar_h:, :, :3] = 40
                    img[-bar_h:, x:x + 64, :3] = 255
                    last = img
                now = host_time_s()
                on_frame(Frame(seq=seq, pts=now, arrival=now, width=self.width, height=self.height,
                               array=img, source=self.name))
            delay = nxt - time.perf_counter()
            if delay > 0:
                self._stop.wait(delay)
            else:
                nxt = time.perf_counter()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
