"""Interactive ROI editing canvas drawn over the live input.

Tools
  select   click to select; drag inside to move; drag handles to resize / rotate / move vertices;
           Delete removes; arrow keys nudge (Shift = 10 px); right-click a polygon vertex to delete it;
           double-click a polygon edge to insert a vertex
  rect     drag to create a rectangle
  polygon  click to add points; double-click, Enter, or click the first point to close; Esc cancels
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF

from vcstudio.roi.model import ROI, new_polygon, new_rect
from vcstudio.ui import theme
from vcstudio.ui.video_view import VideoView

MODE_COLORS = {"manual": QColor("#4f8cff"), "auto": QColor("#3fb950"), "hybrid": QColor("#f0883e")}
HANDLE = 5.0          # half-size of handle squares, widget px
HIT = 9.0             # hit tolerance, widget px
ROT_OFFSET = 30.0     # rotation handle distance above the top edge, widget px
MIN_PX = 4.0          # minimum rect size in frame pixels
# rect handle signs (sx, sy) in local coordinates: corners then edge midpoints
RECT_HANDLES = [(-1, -1), (1, -1), (1, 1), (-1, 1), (0, -1), (1, 0), (0, 1), (-1, 0)]


class RoiCanvas(VideoView):
    rois_changed = Signal()            # geometry edited (emitted on mouse release / key edit)
    selection_changed = Signal(str)    # roi id or ""
    created = Signal(str)              # new roi id

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.overlay = ""
        self.frame_size = (1920, 1080)
        self.rois: list[ROI] = []
        self.selected: str = ""
        self.tool = "select"
        self.regions = []               # resolved ActiveRegions (live), drawn dashed
        self._drag = None               # dict describing the active drag
        self._poly_pts: list[list[float]] = []   # normalised points while drawing a polygon
        self._mouse = QPointF()
        self._counter = 0

    # ---------------------------------------------------------------- coordinates
    def content_size(self):
        if self.image is not None and not self.image.isNull():
            return super().content_size()
        return self.frame_size

    def to_norm(self, p: QPointF) -> tuple[float, float]:
        t = self.image_rect()
        return ((p.x() - t.x()) / t.width(), (p.y() - t.y()) / t.height())

    def to_widget(self, nx: float, ny: float) -> QPointF:
        t = self.image_rect()
        return QPointF(t.x() + nx * t.width(), t.y() + ny * t.height())

    def px_to_widget(self, x: float, y: float) -> QPointF:
        fw, fh = self.frame_size
        return self.to_widget(x / fw, y / fh)

    def widget_to_px(self, p: QPointF) -> tuple[float, float]:
        nx, ny = self.to_norm(p)
        return nx * self.frame_size[0], ny * self.frame_size[1]

    def roi(self, rid: str) -> ROI | None:
        return next((r for r in self.rois if r.id == rid), None)

    def set_rois(self, rois: list[ROI], selected: str | None = None) -> None:
        self.rois = rois
        if selected is not None:
            self.selected = selected
        if not self.roi(self.selected):
            self.selected = ""
        self.update()

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self._poly_pts = []
        self._drag = None
        self.setCursor(Qt.CrossCursor if tool in ("rect", "polygon") else Qt.ArrowCursor)
        self.update()

    def _next_name(self, base: str) -> str:
        names = {r.name for r in self.rois}
        i = 1
        while f"{base} {i}" in names:
            i += 1
        return f"{base} {i}"

    # ---------------------------------------------------------------- hit testing
    def _handles(self, r: ROI) -> list[tuple[str, int, QPointF]]:
        """(kind, index, widget point) for the selected ROI."""
        fw, fh = self.frame_size
        out = []
        if r.kind == "polygon":
            for i, (x, y) in enumerate(r.points):
                out.append(("vertex", i, self.to_widget(x, y)))
            return out
        a = math.radians(r.rotation)
        c, s = math.cos(a), math.sin(a)
        cx, cy, w, h = r.cx * fw, r.cy * fh, r.w * fw, r.h * fh
        for i, (sx, sy) in enumerate(RECT_HANDLES):
            lx, ly = sx * w / 2, sy * h / 2
            out.append(("resize", i, self.px_to_widget(cx + lx * c - ly * s, cy + lx * s + ly * c)))
        top = self.px_to_widget(cx + (h / 2) * s, cy - (h / 2) * c)
        cen = self.px_to_widget(cx, cy)
        d = top - cen
        n = math.hypot(d.x(), d.y()) or 1.0
        out.append(("rotate", 0, top + QPointF(d.x() / n * ROT_OFFSET, d.y() / n * ROT_OFFSET)))
        return out

    def _roi_contains(self, r: ROI, p: QPointF) -> bool:
        from vcstudio.roi.geometry import point_in_polygon
        if r.mode == "auto":
            return False
        x, y = self.widget_to_px(p)
        return point_in_polygon(x, y, r.polygon_px(*self.frame_size))

    def _hit(self, p: QPointF):
        sel = self.roi(self.selected)
        if sel is not None and sel.mode != "auto":
            for kind, idx, hp in self._handles(sel):
                if abs(hp.x() - p.x()) <= HIT and abs(hp.y() - p.y()) <= HIT:
                    return kind, sel, idx
        for r in sorted(self.rois, key=lambda r: -r.priority):   # topmost first
            if self._roi_contains(r, p):
                return "move", r, -1
        return None, None, -1

    # ---------------------------------------------------------------- mouse
    def mousePressEvent(self, e):  # noqa: N802
        p = e.position()
        self.setFocus()
        if self.tool == "rect" and e.button() == Qt.LeftButton:
            n = self.to_norm(p)
            self._drag = {"kind": "create_rect", "start": n, "cur": n}
            return
        if self.tool == "polygon" and e.button() == Qt.LeftButton:
            n = self.to_norm(p)
            if len(self._poly_pts) >= 3:
                first = self.to_widget(*self._poly_pts[0])
                if math.hypot(first.x() - p.x(), first.y() - p.y()) <= HIT:
                    self._finish_polygon()
                    return
            self._poly_pts.append([min(max(n[0], 0), 1), min(max(n[1], 0), 1)])
            self.update()
            return
        if self.tool != "select":
            return
        kind, r, idx = self._hit(p)
        if e.button() == Qt.RightButton:
            if kind == "vertex" and len(r.points) > 3:
                del r.points[idx]
                self.update()
                self.rois_changed.emit()
            return
        if r is None:
            if self.selected:
                self.selected = ""
                self.selection_changed.emit("")
            self.update()
            return
        if r.id != self.selected:
            self.selected = r.id
            self.selection_changed.emit(r.id)
        self._drag = {"kind": kind, "roi": r, "idx": idx, "start_px": self.widget_to_px(p),
                      "orig": r.to_dict(), "moved": False}
        self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        p = e.position()
        self._mouse = p
        d = self._drag
        if d is None:
            if self.tool == "select":
                kind, _, _ = self._hit(p)
                self.setCursor({"resize": Qt.SizeAllCursor, "vertex": Qt.PointingHandCursor,
                                "rotate": Qt.CrossCursor, "move": Qt.OpenHandCursor}.get(kind, Qt.ArrowCursor))
            if self.tool == "polygon":
                self.update()
            return
        if d["kind"] == "create_rect":
            d["cur"] = self.to_norm(p)
            self.update()
            return
        r: ROI = d["roi"]
        orig = ROI.from_dict(d["orig"])
        fw, fh = self.frame_size
        x, y = self.widget_to_px(p)
        x0, y0 = d["start_px"]
        d["moved"] = True
        if d["kind"] == "move":
            r.cx, r.cy, r.points = orig.cx, orig.cy, [list(q) for q in orig.points]
            r.translate((x - x0) / fw, (y - y0) / fh)
        elif d["kind"] == "vertex":
            r.points[d["idx"]] = [min(max(x / fw, 0), 1), min(max(y / fh, 0), 1)]
        elif d["kind"] == "rotate":
            ang = math.degrees(math.atan2(y - orig.cy * fh, x - orig.cx * fw)) + 90.0
            if e.modifiers() & Qt.ShiftModifier:
                ang = round(ang / 15.0) * 15.0
            r.rotation = ((ang + 180.0) % 360.0) - 180.0
        elif d["kind"] == "resize":
            self._resize_rect(r, orig, d["idx"], x, y, bool(e.modifiers() & Qt.ShiftModifier))
        self.update()

    def _resize_rect(self, r: ROI, orig: ROI, idx: int, x: float, y: float, keep_aspect: bool) -> None:
        fw, fh = self.frame_size
        sx, sy = RECT_HANDLES[idx]
        a = math.radians(orig.rotation)
        c, s = math.cos(a), math.sin(a)
        cx, cy, w, h = orig.cx * fw, orig.cy * fh, orig.w * fw, orig.h * fh
        dx, dy = x - cx, y - cy
        lx, ly = dx * c + dy * s, -dx * s + dy * c          # mouse in local (unrotated) coords
        nx0, nx1, ny0, ny1 = -w / 2, w / 2, -h / 2, h / 2
        if sx:
            if sx > 0:
                nx1 = max(lx, nx0 + MIN_PX)
            else:
                nx0 = min(lx, nx1 - MIN_PX)
        if sy:
            if sy > 0:
                ny1 = max(ly, ny0 + MIN_PX)
            else:
                ny0 = min(ly, ny1 - MIN_PX)
        nw, nh = nx1 - nx0, ny1 - ny0
        if keep_aspect and sx and sy:
            ratio = w / h
            if nw / nh > ratio:
                nw = nh * ratio
            else:
                nh = nw / ratio
            nx0, nx1 = (nx1 - nw, nx1) if sx < 0 else (nx0, nx0 + nw)
            ny0, ny1 = (ny1 - nh, ny1) if sy < 0 else (ny0, ny0 + nh)
        lcx, lcy = (nx0 + nx1) / 2, (ny0 + ny1) / 2
        r.cx = (cx + lcx * c - lcy * s) / fw
        r.cy = (cy + lcx * s + lcy * c) / fh
        r.w, r.h = nw / fw, nh / fh

    def mouseReleaseEvent(self, e):  # noqa: N802
        d, self._drag = self._drag, None
        if d is None:
            return
        if d["kind"] == "create_rect":
            (ax, ay), (bx, by) = d["start"], d["cur"]
            fw, fh = self.frame_size
            ax, bx = (min(max(v, 0), 1) for v in (ax, bx))
            ay, by = (min(max(v, 0), 1) for v in (ay, by))
            if abs(bx - ax) * fw >= MIN_PX * 2 and abs(by - ay) * fh >= MIN_PX * 2:
                r = new_rect(self._next_name("Region"), ax, ay, bx, by)
                r.priority = max((q.priority for q in self.rois), default=-1) + 1
                self.rois.append(r)
                self.selected = r.id
                self.created.emit(r.id)
                self.selection_changed.emit(r.id)
                self.rois_changed.emit()
            self.update()
            return
        if d.get("moved"):
            self.rois_changed.emit()

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if self.tool == "polygon":
            if len(self._poly_pts) >= 3:
                self._finish_polygon()
            return
        sel = self.roi(self.selected)
        if self.tool == "select" and sel is not None and sel.kind == "polygon":
            p = e.position()
            pts = [self.to_widget(*q) for q in sel.points]
            for i in range(len(pts)):
                a, b = pts[i], pts[(i + 1) % len(pts)]
                if _dist_to_segment(p, a, b) <= HIT:
                    sel.points.insert(i + 1, list(self.to_norm(p)))
                    self.rois_changed.emit()
                    self.update()
                    return

    def _finish_polygon(self) -> None:
        pts, self._poly_pts = self._poly_pts, []
        try:
            r = new_polygon(self._next_name("Polygon"), pts)
        except ValueError:
            self.update()
            return
        r.priority = max((q.priority for q in self.rois), default=-1) + 1
        self.rois.append(r)
        self.selected = r.id
        self.created.emit(r.id)
        self.selection_changed.emit(r.id)
        self.rois_changed.emit()
        self.update()

    def keyPressEvent(self, e):  # noqa: N802
        if self.tool == "polygon":
            if e.key() in (Qt.Key_Return, Qt.Key_Enter) and len(self._poly_pts) >= 3:
                self._finish_polygon()
                return
            if e.key() == Qt.Key_Escape:
                self._poly_pts = []
                self.update()
                return
            if e.key() == Qt.Key_Backspace and self._poly_pts:
                self._poly_pts.pop()
                self.update()
                return
        sel = self.roi(self.selected)
        if sel is None:
            return super().keyPressEvent(e)
        step = 10 if e.modifiers() & Qt.ShiftModifier else 1
        moves = {Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0), Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step)}
        if e.key() in moves and sel.mode != "auto":
            dx, dy = moves[e.key()]
            sel.translate(dx / self.frame_size[0], dy / self.frame_size[1])
            self.rois_changed.emit()
            self.update()
        elif e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.rois = [r for r in self.rois if r.id != sel.id]
            self.selected = ""
            self.selection_changed.emit("")
            self.rois_changed.emit()
            self.update()
        else:
            super().keyPressEvent(e)

    # ---------------------------------------------------------------- painting
    def paint_background(self, p: QPainter, target: QRectF) -> None:
        if self.image is None:
            p.fillRect(target, QColor("#0b0d11"))
            p.setPen(QPen(QColor("#1c2029"), 1))
            for i in range(1, 16):
                x = target.x() + target.width() * i / 16
                p.drawLine(QPointF(x, target.top()), QPointF(x, target.bottom()))
            for i in range(1, 9):
                y = target.y() + target.height() * i / 9
                p.drawLine(QPointF(target.left(), y), QPointF(target.right(), y))
            p.setPen(QColor(theme.MUTED))
            p.drawText(target, Qt.AlignCenter, f"{self.frame_size[0]}×{self.frame_size[1]}: start capture to see live video")

    def paint_foreground(self, p: QPainter, target: QRectF) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        p.save()
        p.setClipRect(target)
        f = QFont(p.font()); f.setPointSize(11); f.setBold(True); p.setFont(f)
        fw, fh = self.frame_size
        # resolved live regions (auto / hybrid objects)
        for reg in self.regions:
            if not reg.is_object:
                continue
            col = QColor(MODE_COLORS.get(reg.mode, QColor("white")))
            pen = QPen(col, 2, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(col.red(), col.green(), col.blue(), 40))
            p.drawPolygon(QPolygonF([self.px_to_widget(x, y) for x, y in reg.polygon]))
            tag = f"{reg.roi_name} · #{reg.track_id} {reg.label}"
            tl = self.px_to_widget(reg.bbox[0], reg.bbox[1])
            p.fillRect(QRectF(tl.x(), tl.y(), p.fontMetrics().horizontalAdvance(tag) + 8, 18), col)
            p.setPen(QColor("black"))
            p.drawText(QRectF(tl.x() + 4, tl.y(), 400, 18), Qt.AlignVCenter | Qt.AlignLeft, tag)
        # ROI geometry
        for r in sorted(self.rois, key=lambda r: r.priority):
            if r.mode == "auto":
                continue
            col = QColor(MODE_COLORS[r.mode])
            sel = r.id == self.selected
            alpha = 55 if r.enabled else 15
            poly = QPolygonF([self.px_to_widget(x, y) for x, y in r.polygon_px(fw, fh)])
            p.setBrush(QColor(col.red(), col.green(), col.blue(), alpha))
            p.setPen(QPen(col if r.enabled else QColor(theme.MUTED), 3 if sel else 2,
                          Qt.SolidLine if r.enabled else Qt.DotLine))
            p.drawPolygon(poly)
            label = r.name + ("" if r.enabled else " (off)") + (f" · {r.mode}" if r.mode != "manual" else "")
            br = poly.boundingRect()
            p.setPen(QColor("white"))
            p.drawText(QRectF(br.x() + 6, br.y() + 4, 300, 18), Qt.AlignLeft | Qt.AlignVCenter, label)
        # handles
        sel = self.roi(self.selected)
        if sel is not None and sel.mode != "auto":
            for kind, _, hp in self._handles(sel):
                if kind == "rotate":
                    p.setPen(QPen(QColor("white"), 1.5))
                    p.setBrush(QColor(theme.ACCENT))
                    p.drawEllipse(hp, HANDLE + 1, HANDLE + 1)
                else:
                    p.setPen(QPen(QColor("black"), 1))
                    p.setBrush(QColor("white"))
                    p.drawRect(QRectF(hp.x() - HANDLE, hp.y() - HANDLE, 2 * HANDLE, 2 * HANDLE))
        # in-progress shapes
        if self._drag and self._drag["kind"] == "create_rect":
            a = self.to_widget(*self._drag["start"]); b = self.to_widget(*self._drag["cur"])
            p.setPen(QPen(QColor(theme.ACCENT), 2, Qt.DashLine)); p.setBrush(QColor(79, 140, 255, 40))
            p.drawRect(QRectF(a, b).normalized())
        if self._poly_pts:
            path = QPainterPath(self.to_widget(*self._poly_pts[0]))
            for q in self._poly_pts[1:]:
                path.lineTo(self.to_widget(*q))
            path.lineTo(self._mouse)
            p.setPen(QPen(QColor(theme.ACCENT), 2, Qt.DashLine)); p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setBrush(QBrush(QColor("white")))
            for q in self._poly_pts:
                p.drawEllipse(self.to_widget(*q), 4, 4)
        p.restore()


def _dist_to_segment(p: QPointF, a: QPointF, b: QPointF) -> float:
    ax, ay, bx, by, px, py = a.x(), a.y(), b.x(), b.y(), p.x(), p.y()
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
