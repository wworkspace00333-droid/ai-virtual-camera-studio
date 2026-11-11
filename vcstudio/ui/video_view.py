"""Video preview widget and a feeder that converts the newest captured frame at <= N fps."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
import colorsys
import zlib

import numpy as np

from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from vcstudio.ui import theme

log = logging.getLogger(__name__)


class VideoView(QWidget):
    """Aspect-fit image display with a status overlay."""

    def __init__(self, title: str = ""):
        super().__init__()
        self.title = title
        self.image: QImage | None = None
        self.overlay = "No signal"
        self.overlay_level = "idle"
        self.boxes: list[tuple[float, float, float, float, str]] = []   # normalized x0,y0,x1,y1, caption
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_image(self, img: QImage | None) -> None:
        self.image = img
        self.update()

    def set_boxes(self, boxes) -> None:
        self.boxes = boxes
        self.update()

    def set_overlay(self, text: str, level: str = "idle") -> None:
        if (text, level) != (self.overlay, self.overlay_level):
            self.overlay, self.overlay_level = text, level
            self.update()

    def content_size(self) -> tuple[int, int] | None:
        if self.image is not None and not self.image.isNull():
            return self.image.width(), self.image.height()
        return None

    def image_rect(self) -> QRectF:
        """Aspect-fit rectangle of the content inside the widget."""
        size = self.content_size()
        if not size:
            return QRectF(self.rect())
        iw, ih = size
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paint_background(self, p: QPainter, target: QRectF) -> None:
        pass

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        target = self.image_rect()
        self.paint_background(p, target)
        if self.image is not None and not self.image.isNull():
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(target, self.image)
            if self.boxes:
                self._draw_boxes(p, target)
        if self.title:
            p.setPen(QColor(theme.TEXT))
            p.fillRect(QRectF(8, 8, p.fontMetrics().horizontalAdvance(self.title) + 16, 22), QColor(0, 0, 0, 150))
            p.drawText(QRectF(16, 8, 400, 22), Qt.AlignVCenter | Qt.AlignLeft, self.title)
        if self.overlay:
            color = {"warn": theme.WARN, "error": theme.ERR, "ok": theme.OK}.get(self.overlay_level, theme.MUTED)
            f = QFont(p.font()); f.setPointSize(14); f.setBold(True); p.setFont(f)
            fm = p.fontMetrics()
            w = fm.horizontalAdvance(self.overlay) + 28
            box = QRectF((self.width() - w) / 2, self.height() - 52, w, 34)
            if self.image is None:
                box.moveCenter(self.rect().center())
            p.fillRect(box, QColor(0, 0, 0, 170))
            p.setPen(QColor(color))
            p.drawText(box, Qt.AlignCenter, self.overlay)
        self.paint_foreground(p, target)

    def paint_foreground(self, p: QPainter, target: QRectF) -> None:
        pass


def label_color(label: str) -> QColor:
    h = (zlib.crc32(label.encode()) % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.75, 1.0)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


def _draw_boxes(self, p: QPainter, target: QRectF) -> None:
    f = QFont(p.font()); f.setPointSize(11); f.setBold(True); p.setFont(f)
    fm = p.fontMetrics()
    for x0, y0, x1, y1, caption in self.boxes:
        words = caption.split(" ")
        c = label_color(words[1] if words[0].startswith("#") and len(words) > 1 else words[0])
        coasting = caption.endswith("⋯")
        r = QRectF(target.x() + x0 * target.width(), target.y() + y0 * target.height(),
                   (x1 - x0) * target.width(), (y1 - y0) * target.height())
        pen = QPen(c, 2)
        if coasting:
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(r)
        tw = fm.horizontalAdvance(caption) + 8
        tag = QRectF(r.x(), max(target.y(), r.y() - 18), tw, 18)
        p.fillRect(tag, c)
        p.setPen(QColor("#000000"))
        p.drawText(tag, Qt.AlignCenter, caption)


VideoView._draw_boxes = _draw_boxes


STATE_OVERLAY = {
    "stopped": ("Capture stopped", "idle"),
    "starting": ("Connecting…", "idle"),
    "running": ("", "ok"),
    "frozen": ("Frozen input: identical frames", "warn"),
    "no_signal": ("No signal", "error"),
    "reconnecting": ("Reconnecting…", "warn"),
    "error": ("Capture error (see Input page)", "error"),
}


_preview_pools: dict = {}


def _gpu_thumbnail(frame, max_width: int):
    """Downscale an IOSurface frame on the GPU and read back only the small result."""
    if frame.pixel_buffer is None or frame.width <= max_width:
        return None
    from vcstudio.native.metal import scaler_context
    ctx = scaler_context()
    if ctx is None:
        return None
    import Quartz as Q  # type: ignore
    from vcstudio.core.frame import Frame
    from vcstudio.core.pixelbuffer import PixelBufferPool
    # same output size as the CPU decimation path (integer step), so paired previews always match
    step = max(1, -(-frame.width // max(1, max_width)))
    w, h = -(-frame.width // step), -(-frame.height // step)
    pool = _preview_pools.get((w, h))
    if pool is None:
        if len(_preview_pools) > 6:
            _preview_pools.clear()
        pool = _preview_pools[(w, h)] = PixelBufferPool(w, h)
    err, pb = Q.CVPixelBufferPoolCreatePixelBuffer(None, pool._pool, None)
    if err != 0:
        return None
    try:
        ctx.scale(frame.pixel_buffer, pb, 0, 0, w, h, -1.0)
    except RuntimeError:
        return None
    with Frame(0, 0, 0, w, h, pixel_buffer=pb).bgra() as a:
        return np.ascontiguousarray(a)


def frame_to_qimage(frame, max_width: int) -> QImage:
    arr = _gpu_thumbnail(frame, max_width)
    if arr is None:
        arr = frame.downscaled_copy(max_width)
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, arr.strides[0], QImage.Format_RGB32)  # BGRA bytes == RGB32 on little-endian
    return img.copy()  # detach from the numpy buffer


class PreviewFeeder(QObject):
    """Polls the capture slot at <= max_fps and pushes QImages to registered, visible views."""
    image_ready = Signal(QImage)

    def __init__(self, capture, max_fps: int = 30, parent=None):
        super().__init__(parent)
        self.capture = capture
        self.views: list[VideoView] = []
        self._last_version = -1
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.set_max_fps(max_fps)
        self.timer.start()
        self.converted = 0
        self.ai = None                 # AIEngine (optional) for detection overlays
        self.overlay_views: list[VideoView] = []
        self.show_detections = True
        self._last_result_seq = -1

    def set_max_fps(self, fps: int) -> None:
        self.timer.setInterval(max(1, int(1000 / max(1, fps))))

    def add_view(self, v: VideoView, detections: bool = False) -> None:
        self.views.append(v)
        if detections:
            self.overlay_views.append(v)

    def _update_boxes(self, frame) -> None:
        self._update_boxes_for(self.overlay_views, frame)

    def _update_boxes_for(self, views, frame) -> None:
        """Boxes for the frame being displayed: tracks predicted to its timestamp, else raw detections."""
        boxes = []
        if self.ai is not None and self.show_detections and frame is not None:
            if self.ai.tracker_config().enabled:
                for o in self.ai.tracked_at(frame.pts):
                    tag = f"#{o.track_id} {o.label} {o.confidence:.2f}" + (" ⋯" if o.state == "coasting" else "")
                    boxes.append((*o.normalized(frame.width, frame.height), tag))
            else:
                res = self.ai.results.peek()[0]
                if res is not None:
                    boxes = [(*d.normalized(res.width, res.height), f"{d.label} {d.confidence:.2f}")
                             for d in res.detections]
        for v in views:
            if boxes or v.boxes:
                v.set_boxes(boxes)

    def tick(self) -> None:
        visible = [v for v in self.views if v.isVisible()]
        st = self.capture.stats().state
        text, level = STATE_OVERLAY.get(st, (st, "idle"))
        for v in self.views:
            v.set_overlay(text, level)
        if st in ("stopped", "error"):
            for v in self.views:
                if v.image is not None:
                    v.set_image(None)
                if v.boxes:
                    v.set_boxes([])
            self._last_version = -1
            return
        if not visible:
            return
        frame, ver = self.capture.latest()
        if frame is None or ver == self._last_version:
            return
        self._last_version = ver
        self._update_boxes(frame)
        max_w = max(min(v.width(), 1920) for v in visible)
        try:
            img = frame_to_qimage(frame, max(320, int(max_w * self.devicePixelRatio())))
        except Exception:
            log.exception("preview conversion failed")
            return
        self.converted += 1
        for v in visible:
            v.set_image(img)
        self.image_ready.emit(img)

    def devicePixelRatio(self) -> float:  # noqa: N802
        return self.views[0].devicePixelRatioF() if self.views else 1.0
