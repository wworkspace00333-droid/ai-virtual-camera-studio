"""ROI editor interaction tests (simulated mouse/keyboard on the real canvas)."""
import math

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest

from vcstudio.app.application import AppContext
from vcstudio.roi.model import new_rect
from vcstudio.ui.main_window import MainWindow


@pytest.fixture
def page(qtbot, paths):
    ctx = AppContext(paths, configure_logging=False)
    w = MainWindow(ctx); qtbot.addWidget(w); w.resize(1500, 950); w.show()
    w.goto("roi")
    p = w.pages[w.page_keys().index("roi")]
    qtbot.waitExposed(p.canvas)
    yield p
    w.close(); ctx.shutdown()


def wpt(canvas, nx, ny) -> QPoint:
    return canvas.to_widget(nx, ny).toPoint()


def drag(canvas, a: QPoint, b: QPoint, mods=Qt.NoModifier):
    QTest.mousePress(canvas, Qt.LeftButton, mods, a)
    for i in range(1, 6):
        QTest.mouseMove(canvas, a + (b - a) * i / 5)
    QTest.mouseRelease(canvas, Qt.LeftButton, mods, b)


def saved(page):
    return page.ctx.profiles.active()["body"]["rois"]


def test_draw_rectangle_persists(page, qtbot):
    c = page.canvas
    page.tool_btns["rect"].setChecked(True)
    drag(c, wpt(c, 0.1, 0.1), wpt(c, 0.4, 0.5))
    assert len(c.rois) == 1 and page.tool_btns["select"].isChecked()
    r = c.rois[0]
    assert abs(r.cx - 0.25) < 0.01 and abs(r.w - 0.3) < 0.01
    qtbot.waitUntil(lambda: len(saved(page)) == 1, timeout=2000)
    assert page.ctx.rois.rois()[0].id == r.id                     # engine-side service reloaded


def test_move_resize_rotate(page):
    c = page.canvas
    c.rois.append(new_rect("R", 0.2, 0.2, 0.4, 0.4)); c.selected = c.rois[0].id
    page._commit()
    r = c.rois[0]
    drag(c, wpt(c, 0.3, 0.3), wpt(c, 0.5, 0.3))                    # move right by 0.2
    assert abs(r.cx - 0.5) < 0.01 and abs(r.cy - 0.3) < 0.01
    br = [hp for k, i, hp in c._handles(r) if k == "resize" and i == 2][0].toPoint()
    drag(c, br, br + QPoint(80, 40))                                 # resize from bottom-right corner
    assert r.w > 0.2 + 0.03 and r.h > 0.2 + 0.03
    left_edge_x = r.cx - r.w / 2
    rot = [hp for k, i, hp in c._handles(r) if k == "rotate"][0].toPoint()
    center = c.px_to_widget(r.cx * c.frame_size[0], r.cy * c.frame_size[1]).toPoint()
    target = QPoint(center.x() + 200, center.y())                    # point right of centre => +90°
    drag(c, rot, target)
    assert abs(r.rotation - 90) < 3
    assert abs((r.cx - r.w / 2) - left_edge_x) < 0.2


def test_resize_rotated_rect_keeps_opposite_corner(page):
    c = page.canvas
    r = new_rect("Rot", 0.3, 0.3, 0.5, 0.5); r.rotation = 30
    c.rois.append(r); c.selected = r.id
    fw, fh = c.frame_size
    tl_before = r.polygon_px(fw, fh)[0].copy()
    br = [hp for k, i, hp in c._handles(r) if k == "resize" and i == 2][0].toPoint()
    drag(c, br, br + QPoint(60, 30))
    tl_after = r.polygon_px(fw, fh)[0]
    assert math.dist(tl_before, tl_after) < 3.0                     # anchored (frame px)


def test_draw_polygon_and_edit_vertices(page, qtbot):
    c = page.canvas
    page.tool_btns["polygon"].setChecked(True)
    for nx, ny in ((0.6, 0.2), (0.9, 0.25), (0.85, 0.6)):
        QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, wpt(c, nx, ny))
    QTest.mouseDClick(c, Qt.LeftButton, Qt.NoModifier, wpt(c, 0.65, 0.55))
    assert len(c.rois) == 1 and c.rois[0].kind == "polygon" and len(c.rois[0].points) >= 3
    r = c.rois[0]
    v0 = [hp for k, i, hp in c._handles(r) if k == "vertex" and i == 0][0].toPoint()
    drag(c, v0, v0 + QPoint(-30, 0))
    assert r.points[0][0] < 0.6
    # manual vertex entry
    page.pts.setPlainText("100, 100\n500, 120\n300, 400\n80, 300")
    page._apply_points()
    assert len(r.points) == 4 and abs(r.points[1][0] * c.frame_size[0] - 500) < 1e-6
    qtbot.waitUntil(lambda: len(saved(page)) == 1 and len(saved(page)[0]["points"]) == 4, timeout=2000)


def test_polygon_escape_cancels(page):
    c = page.canvas
    page.tool_btns["polygon"].setChecked(True)
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, wpt(c, 0.2, 0.2))
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, wpt(c, 0.3, 0.2))
    QTest.keyClick(c, Qt.Key_Escape)
    assert c.rois == [] and c._poly_pts == []


def test_manual_coordinates_and_properties(page, qtbot):
    c = page.canvas
    c.rois.append(new_rect("R", 0.1, 0.1, 0.2, 0.2)); c.selected = c.rois[0].id
    page._commit()
    page.x.setValue(100); page.y.setValue(200); page.w.setValue(300); page.h.setValue(150); page.rot.setValue(15)
    r = c.rois[0]
    fw, fh = c.frame_size
    assert math.isclose(r.cx * fw, 250) and math.isclose(r.cy * fh, 275) and r.rotation == 15
    page.name.setText("Logo area"); page.name.editingFinished.emit()
    page.mode.setCurrentIndex(page.mode.findData("hybrid"))
    page.classes.setText("person, car"); page.classes.editingFinished.emit()
    qtbot.waitUntil(lambda: bool(saved(page)) and saved(page)[0]["classes"] == ["person", "car"], timeout=2000)
    d = saved(page)[0]
    assert d["name"] == "Logo area" and d["mode"] == "hybrid"


def test_delete_key_duplicate_priority(page):
    c = page.canvas
    for i in range(2):
        r = new_rect(f"R{i}", 0.1 * i, 0.1, 0.1 * i + 0.1, 0.2); r.priority = i
        c.rois.append(r)
    c.selected = c.rois[0].id
    page._commit()
    page._duplicate()
    assert len(c.rois) == 3
    top = page._ordered()[0]
    c.selected = page._ordered()[-1].id
    page._move(+1)
    assert page._ordered()[-1].id != c.selected
    c.setFocus()
    QTest.keyClick(c, Qt.Key_Delete)
    assert len(c.rois) == 2 and top in c.rois


def test_auto_region_and_list_toggle(page, qtbot):
    page._add_auto()
    r = page.canvas.rois[0]
    assert r.mode == "auto" and r.classes == ["person"]
    it = page.list.item(0)
    it.setCheckState(Qt.Unchecked)
    assert r.enabled is False
    qtbot.waitUntil(lambda: bool(saved(page)) and saved(page)[0]["enabled"] is False, timeout=2000)


def test_profile_switch_reloads_editor(page):
    ctx = page.ctx
    ctx.profiles.create("Other")
    body = ctx.profiles.get("Other")["body"]; body["rois"] = [new_rect("Theirs", 0, 0, 0.5, 0.5).to_dict()]
    ctx.profiles.update_body("Other", body)
    ctx.profiles.switch("Other")
    assert [r.name for r in page.canvas.rois] == ["Theirs"]
    assert [r.name for r in ctx.rois.rois()] == ["Theirs"]
