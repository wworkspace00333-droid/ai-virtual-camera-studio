import pytest
from PySide6.QtCore import Qt

from vcstudio.ai.model_manager import REPO_MODELS
from vcstudio.app.application import AppContext
from vcstudio.ui.main_window import MainWindow

needs_model = pytest.mark.skipif(not (REPO_MODELS / "yolo11n.mlpackage").exists(), reason="no model")


@pytest.fixture
def win(qtbot, paths):
    ctx = AppContext(paths, configure_logging=False)
    w = MainWindow(ctx); qtbot.addWidget(w); w.show()
    yield w
    w.close(); ctx.shutdown()


def test_detection_page_writes_profile_and_engine(win, qtbot):
    page = win.pages[win.page_keys().index("detection")]
    page.conf.setValue(60)
    page.cls_only.setChecked(True)
    page._items["car"].setCheckState(0, Qt.Checked)
    qtbot.waitUntil(lambda: win.ctx.profiles.active()["body"]["detection"]["include_classes"] == ["car"], timeout=2000)
    assert win.ctx.profiles.active()["body"]["detection"]["confidence"] == 0.6
    cfg, _ = win.ctx.ai.config()
    assert cfg.include == ["car"] and cfg.confidence == 0.6


def test_profile_switch_reconfigures_ai_and_mode(win):
    ctx = win.ctx
    ctx.profiles.create("Fast")
    ctx.profiles.update_body("Fast", {**ctx.profiles.get("Fast")["body"], "performance_mode": "performance",
                                      "detection": {**ctx.profiles.get("Fast")["body"]["detection"], "confidence": 0.8}})
    ctx.profiles.switch("Fast")
    cfg, mode = ctx.ai.config()
    assert mode == "performance" and cfg.confidence == 0.8
    assert ctx.settings.get("performance.mode") == "performance"


def test_toggle_ai_action(win):
    win.on_action("toggle_ai")
    assert win.ctx.profiles.active()["body"]["ai"]["enabled"] is False
    assert win.ctx.ai.config()[0].enabled is False
    win.on_action("toggle_ai")
    assert win.ctx.ai.config()[0].enabled is True


@needs_model
def test_preview_shows_detection_boxes(win, qtbot):
    ctx = win.ctx
    ctx.settings.set("input.method", "test_pattern")
    win.goto("preview")
    ctx.update_active_profile(lambda b: b["detection"].update(confidence=0.05))
    ctx.start_capture()
    qtbot.waitUntil(lambda: ctx.ai.stats().inferences > 3, timeout=10000)
    assert ctx.ai.stats().state == "running"
    ctx.stop_capture()
    qtbot.waitUntil(lambda: ctx.ai.stats().state == "stopped", timeout=3000)


def test_tracking_settings_roundtrip(win, qtbot):
    page = win.pages[win.page_keys().index("detection")]
    page.trk_buf.setValue(3.0)
    page.trk_smooth.setValue(80)
    page.trk_class.setChecked(False)
    qtbot.waitUntil(lambda: win.ctx.profiles.active()["body"]["tracking"]["track_buffer_s"] == 3.0, timeout=2000)
    t = win.ctx.ai.tracker_config()
    assert t.track_buffer_s == 3.0 and t.smoothing == 0.8 and t.class_aware is False
    assert t.high_thresh == win.ctx.profiles.active()["body"]["detection"]["confidence"]


@needs_model
def test_preview_overlay_shows_track_ids(win, qtbot, monkeypatch):
    from tests.test_ai import ImageSource
    from vcstudio.ai.testimages import load_bgra
    ctx = win.ctx
    img = load_bgra("tests/data/bus.jpg")
    monkeypatch.setattr(ctx, "capture_factory", lambda: (lambda: ImageSource(img, 30)))
    win.goto("preview")
    preview = win.pages[win.page_keys().index("preview")]
    ctx.start_capture()
    qtbot.waitUntil(lambda: any(c.startswith("#") for *_, c in preview.original.boxes), timeout=10000)
    captions = [c for *_, c in preview.original.boxes]
    assert sum("person" in c for c in captions) >= 3 and any("bus" in c for c in captions)
    ctx.stop_capture()


def test_replacement_page_assets_and_spec(win, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from vcstudio.roi.model import new_rect
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    ctx = win.ctx
    roi = new_rect("Logo", 0.2, 0.2, 0.5, 0.5)
    ctx.update_active_profile(lambda b: b.__setitem__("rois", [roi.to_dict()]))
    page = win.pages[win.page_keys().index("replacement")]
    page.reload()
    added = page.add_assets(["tests/data/logo_alpha.png"])
    assert len(added) == 1 and ctx.profiles.active()["body"]["assets"][0]["id"] == added[0]["id"]
    page.enabled.setChecked(True)
    page.asset.setCurrentIndex(page.asset.findData(added[0]["id"]))
    page.mode.setCurrentIndex(page.mode.findData("overlay"))
    page.opacity.setValue(70)
    rep = ctx.profiles.active()["body"]["rois"][0]["replacement"]
    assert rep["enabled"] and rep["asset_id"] == added[0]["id"] and rep["mode"] == "overlay" and rep["opacity"] == 0.7
    specs, assets = ctx.replacement.state()
    assert specs[roi.id].mode == "overlay" and added[0]["id"] in assets
    # preview renders with the test pattern
    ctx.settings.set("input.method", "test_pattern")
    win.goto("replacement")
    ctx.start_capture()
    qtbot.waitUntil(lambda: page.view.image is not None, timeout=5000)
    assert ctx.replacement.compositor.stats.regions_drawn == 1
    ctx.stop_capture()
    # removing the asset clears it from the ROI and deletes the unused file
    path = ctx.assets.dir / added[0]["file"]
    page.assets.setCurrentRow(0)
    page.remove_asset()
    assert not path.exists() and ctx.profiles.active()["body"]["rois"][0]["replacement"]["asset_id"] == ""


def test_replacement_page_video_controls(win, qtbot):
    from vcstudio.roi.model import new_rect
    ctx = win.ctx
    roi = new_rect("Screen", 0.1, 0.1, 0.6, 0.6)
    ctx.update_active_profile(lambda b: b.__setitem__("rois", [roi.to_dict()]))
    page = win.pages[win.page_keys().index("replacement")]
    page.reload()
    vid = page.add_assets(["tests/data/clip_h264.mp4"])[0]
    page.enabled.setChecked(True)
    page.asset.setCurrentIndex(page.asset.findData(vid["id"]))
    win.goto("replacement")
    assert page.video_box.isVisible()
    page.v_rate.setValue(2.0)
    page.v_sync.setCurrentIndex(page.v_sync.findData("per_roi"))
    page.v_loop.setChecked(False)
    rep = ctx.profiles.active()["body"]["rois"][0]["replacement"]
    assert rep["video_rate"] == 2.0 and rep["video_sync"] == "per_roi" and rep["video_loop"] is False
    ctx.settings.set("input.method", "test_pattern")
    ctx.start_capture()
    qtbot.waitUntil(lambda: ctx.replacement.compositor.stats.regions_drawn == 1, timeout=5000)
    players = ctx.replacement.players_for_roi(roi.id)
    assert len(players) == 1 and players[0].cfg.rate == 2.0
    page._transport("toggle")
    assert players[0].stats.paused
    qtbot.waitUntil(lambda: "Paused" in page.v_status.text(), timeout=3000)
    page._transport("restart")
    ctx.stop_capture()


def test_add_assets_button_click_opens_dialog(win, monkeypatch):
    """Regression: clicked(bool) used to be passed as `paths` and crashed."""
    from PySide6.QtWidgets import QFileDialog
    calls = []
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: (calls.append(1), ([], ""))[1])
    page = win.pages[win.page_keys().index("replacement")]
    page.add_btn.click()
    assert calls == [1]


def test_effect_mode_and_backend_setting(win, qtbot):
    from vcstudio.roi.model import new_rect
    ctx = win.ctx
    roi = new_rect("Privacy", 0.2, 0.2, 0.5, 0.5)
    ctx.update_active_profile(lambda b: b.__setitem__("rois", [roi.to_dict()]))
    page = win.pages[win.page_keys().index("replacement")]
    page.reload()
    win.goto("replacement")
    page.enabled.setChecked(True)
    page.mode.setCurrentIndex(page.mode.findData("effect"))
    page.effect.setCurrentIndex(page.effect.findData("pixelate"))
    page.pix.setValue(32)
    rep = ctx.profiles.active()["body"]["rois"][0]["replacement"]
    assert rep["mode"] == "effect" and rep["effect"] == "pixelate" and rep["pixel_size"] == 32
    assert page.effect_box.isVisible() and not page.asset.isEnabled()
    ctx.settings.set("processing.compositor", "cpu")
    assert ctx.replacement.compositor.active.name == "cpu"
    ctx.settings.set("processing.compositor", "auto")
    from vcstudio.native import metal
    assert ctx.replacement.compositor.active.name == ("metal" if metal.available() else "cpu")
