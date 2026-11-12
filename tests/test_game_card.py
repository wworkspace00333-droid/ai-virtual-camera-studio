"""Card for a game: calibration camera -> screen, finding the game's frame, the profile region, the UI flow."""
import cv2
import numpy as np
import pytest

from vcstudio.game import cardframe as cf

W, H = 1920, 1080


def game_screen(cam_bgr, mirror=False, crop=False, scale=0.35, overlay=None):
    """A fake screen: a game window showing the camera scaled/cropped/mirrored, plus its overlay."""
    v = cam_bgr[..., :3]
    if crop:
        v = v[:, 420:1500]                  # square crop, as many mobile games do
    if mirror:
        v = cv2.flip(v, 1)
    v = cv2.resize(v, (int(v.shape[1] * scale), int(v.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    scr = np.full((900, 1440, 3), 40, np.uint8)
    x, y = 300, 200
    scr[y:y + v.shape[0], x:x + v.shape[1]] = v
    box = (x, y, v.shape[1], v.shape[0])
    if overlay:
        overlay(scr, box)
    return scr, box


def frame_box(box):
    x, y, vw, vh = box
    return x + vw // 4, y + vh // 5, x + vw // 4 + vw // 3, y + vh // 5 + vh // 2


def dark_overlay(scr, box):
    x, y, vw, vh = box
    fx0, fy0, fx1, fy1 = frame_box(box)
    keep = scr[fy0:fy1, fx0:fx1].copy()
    scr[y:y + vh, x:x + vw] = (scr[y:y + vh, x:x + vw] * 0.45).astype(np.uint8)
    scr[fy0:fy1, fx0:fx1] = keep
    cv2.rectangle(scr, (fx0, fy0), (fx1, fy1), (255, 255, 255), 3)
    cv2.putText(scr, "Put the card here", (x + 10, y + 30), 0, 0.8, (255, 255, 255), 2)


def outline_overlay(scr, box):
    fx0, fy0, fx1, fy1 = frame_box(box)
    cv2.rectangle(scr, (fx0, fy0), (fx1, fy1), (0, 220, 255), 4)


@pytest.mark.parametrize("mirror,crop,overlay", [(False, False, dark_overlay), (True, False, dark_overlay),
                                                 (True, True, outline_overlay), (False, True, outline_overlay)])
def test_calibrate_and_find_frame(mirror, crop, overlay):
    scr, box = game_screen(cf.calibration_frame(W, H), mirror, crop)
    cal = cf.calibrate(scr, (W, H))
    assert cal.mirrored == mirror and cal.error_px < 1.0 and cal.markers >= 6
    g, _ = game_screen(cf.grey_frame(W, H), mirror, crop, overlay=overlay)
    quad = cf.find_frame(g, cal)
    assert quad is not None
    fx0, fy0, fx1, fy1 = frame_box(box)
    truth = cal.to_camera([[fx0, fy0], [fx1, fy1]])
    cx, cy, rw, rh, rot = cf.frame_to_rect(cal, quad)
    assert abs(cx * W - truth[:, 0].mean()) < 4 and abs(cy * H - truth[:, 1].mean()) < 4
    assert abs(rw * W - abs(truth[1, 0] - truth[0, 0])) < 20 and abs(rh * H - abs(truth[1, 1] - truth[0, 1])) < 20
    assert abs(rot) < 1


def test_no_game_on_screen_is_reported():
    with pytest.raises(cf.CalibrationError):
        cf.calibrate(np.full((900, 1440, 3), 40, np.uint8), (W, H))


def test_plain_camera_without_frame_finds_nothing():
    scr, _ = game_screen(cf.calibration_frame(W, H))
    cal = cf.calibrate(scr, (W, H))
    g, _ = game_screen(cf.grey_frame(W, H))
    assert cf.find_frame(g, cal) is None


def test_game_card_profile_region():
    from vcstudio.core.profiles import default_profile_body
    from vcstudio.roi.model import rois_from_profile
    b = default_profile_body()
    cf.set_game_card(b, rect=(0.4, 0.5, 0.2, 0.3, 0.0), asset_id="abc")
    cf.set_game_card(b, flip=True)
    rois = rois_from_profile(b)
    assert [r.id for r in rois] == [cf.GAME_CARD_ID]
    r = rois[0]
    assert (r.mode, r.cx, r.w, r.replacement["asset_id"], r.replacement["flip_h"]) == ("manual", 0.4, 0.2, "abc", True)


def test_flip_mirrors_the_asset():
    from vcstudio.replacement.compositor import asset_affine
    from vcstudio.replacement.spec import ReplacementSpec
    from vcstudio.roi.model import new_rect
    from vcstudio.roi.resolver import resolve
    reg = resolve([new_rect("r", 0.25, 0.25, 0.75, 0.75)], [], 400, 400)[0]
    a = asset_affine(100, 100, reg, ReplacementSpec(fit="stretch"))
    b = asset_affine(100, 100, reg, ReplacementSpec(fit="stretch", flip_h=True))
    left = np.array([0, 50, 1.0])
    assert a @ left == pytest.approx([100, 200]) and b @ left == pytest.approx([300, 200])


def test_dialog_finds_frame_and_places_card(qtbot, paths, monkeypatch, tmp_path):
    """Whole UI flow with a fake screen that renders whatever the pipeline would send to the camera."""
    from vcstudio.app.application import AppContext
    from vcstudio.ui import game_card_dialog as gd
    ctx = AppContext(paths, configure_logging=False)
    try:
        monkeypatch.setattr(gd, "SETTLE_MS", 10)
        monkeypatch.setattr(gd.GameCardDialog, "_ensure_running", lambda self: True)
        monkeypatch.setattr(gd.GameCardDialog, "_camera_size", lambda self: (W, H))

        def fake_screenshot(path=None):
            ov = ctx.pipeline.override
            cam = ov(W, H) if ov else cf.grey_frame(W, H)
            return game_screen(cam, mirror=True, crop=True, overlay=dark_overlay)[0]
        monkeypatch.setattr(cf, "screenshot", fake_screenshot)
        card = tmp_path / "card.png"
        cv2.imwrite(str(card), np.full((300, 200, 3), (0, 0, 255), np.uint8))
        dlg = gd.GameCardDialog(ctx)
        qtbot.addWidget(dlg)
        dlg.add_cards([str(card)])
        dlg.cards.setCurrentRow(0)
        dlg.find_automatically()
        qtbot.waitUntil(lambda: dlg.auto_btn.isEnabled(), timeout=5000)
        assert ctx.pipeline.override is None
        roi = cf.game_card_roi(ctx.profiles.active()["body"])
        assert roi is not None and roi["replacement"]["asset_id"] and roi["enabled"]
        assert dlg.cal is not None and dlg.cal.mirrored
        # the region lands inside the square crop of the camera (x 420..1500)
        assert 420 < roi["cx"] * W < 1500 and 0.2 < roi["h"] < 0.8
        assert any(r.id == cf.GAME_CARD_ID for r in ctx.rois.rois())
        assert ctx.replacement.active()
    finally:
        ctx.shutdown()


def test_frame_and_caption_drawn_over_the_markers():
    """Regression from the live check: the game draws its frame and caption right over the centre of the
    picture, crossing the coarse grid's markers; the dense grid still calibrates."""
    def fake_game(cam, S=540):
        img = cam[::2, ::2, :3]
        h, w = img.shape[:2]
        v = cv2.resize(cv2.flip(img[:, (w - h) // 2:(w - h) // 2 + h], 1), (S, S), interpolation=cv2.INTER_AREA)
        fx0, fy0, fx1, fy1 = int(S * 0.3), int(S * 0.22), int(S * 0.7), int(S * 0.82)
        keep = v[fy0:fy1, fx0:fx1].copy()
        v = (v * 0.45).astype(np.uint8)
        v[fy0:fy1, fx0:fx1] = keep
        cv2.rectangle(v, (fx0, fy0), (fx1, fy1), (255, 255, 255), 3)
        cv2.putText(v, "Show your card", (fx0 - 10, fy0 - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        scr = np.full((1080, 1920, 3), 230, np.uint8)
        scr[150:150 + S, 120:120 + S] = v
        return scr, (120 + fx0, 150 + fy0, 120 + fx1, 150 + fy1)
    scr, (fx0, fy0, fx1, fy1) = fake_game(cf.calibration_frame(W, H))
    cal = cf.calibrate(scr, (W, H))
    assert cal.markers >= 12 and cal.mirrored
    quad = cf.find_frame(fake_game(cf.grey_frame(W, H))[0], cal)
    assert quad is not None
    assert np.abs(quad[[0, 2]].ravel() - [fx0, fy0, fx1, fy1]).max() <= 5


# ------------------------------------------------------------------ several frames, layouts, hotkeys, paste
def body_with_cards(n=3):
    from vcstudio.core.profiles import default_profile_body
    b = default_profile_body()
    b["assets"] = [{"id": f"a{i}", "name": f"c{i}", "file": f"c{i}.png", "kind": "image"} for i in range(n)]
    cf.add_cards(b, [f"a{i}" for i in range(n)])
    return b


def test_slots_cycle_and_toggle():
    b = body_with_cards(3)
    cf.set_game_card(b, slot=1, rect=(0.3, 0.5, 0.2, 0.3, 0), asset_id="a0")
    cf.set_game_card(b, slot=2, rect=(0.7, 0.5, 0.2, 0.3, 0), asset_id="a2")
    assert list(cf.game_slots(b)) == [1, 2] and cf.next_free_slot(b) == 3
    assert cf.cycle_card(b, 1) == "a1"                       # active slot 1: a0 -> a1
    assert cf.cycle_card(b, 1, slot=2) == "a0"               # wraps around: a2 -> a0
    assert cf.cycle_card(b, -1) == "a0"
    assert cf.toggle_cards(b) is False and not any(d["enabled"] for d in cf.game_slots(b).values())
    assert cf.toggle_cards(b) is True and all(d["enabled"] for d in cf.game_slots(b).values())
    cf.remove_card(b, "a0")
    assert cf.game_card_roi(b, 1)["replacement"]["asset_id"] == "" and "a0" not in cf.game_state(b)["cards"]
    cf.remove_slot(b, 1)
    assert list(cf.game_slots(b)) == [2] and cf.game_state(b)["active_slot"] == 2


def test_layouts_per_game():
    b = body_with_cards(2)
    cf.set_game_card(b, slot=1, rect=(0.3, 0.5, 0.2, 0.3, 0), asset_id="a0", flip=True)
    cf.save_layout(b, "Game A")
    cf.remove_slot(b, 1)
    cf.set_game_card(b, slot=1, rect=(0.5, 0.5, 0.1, 0.1, 0), asset_id="a1")
    cf.set_game_card(b, slot=2, rect=(0.8, 0.5, 0.1, 0.1, 0), asset_id="a1")
    cf.save_layout(b, "Game B")
    cf.apply_layout(b, "Game A")
    s = cf.game_slots(b)
    assert list(s) == [1] and s[1]["cx"] == 0.3 and s[1]["replacement"]["flip_h"] is True
    cf.apply_layout(b, "Game B")
    assert list(cf.game_slots(b)) == [1, 2]
    cf.delete_layout(b, "Game A")
    assert list(cf.game_state(b)["layouts"]) == ["Game B"]
    with pytest.raises(ValueError):
        cf.save_layout(b, "  ")


def two_frames_overlay(scr, box):
    x, y, vw, vh = box
    for fx0 in (x + vw // 8, x + vw // 2 + vw // 8):
        cv2.rectangle(scr, (fx0, y + vh // 4), (fx0 + vw // 4, y + 3 * vh // 4), (255, 255, 255), 3)


def test_find_two_frames_in_reading_order():
    scr, box = game_screen(cf.calibration_frame(W, H))
    cal = cf.calibrate(scr, (W, H))
    frames = cf.find_frames(game_screen(cf.grey_frame(W, H), overlay=two_frames_overlay)[0], cal)
    assert len(frames) == 2 and frames[0][0, 0] < frames[1][0, 0]


def test_card_hotkeys_in_main_window(qtbot, paths):
    from vcstudio.app.application import AppContext
    from vcstudio.ui.main_window import MainWindow
    ctx = AppContext(paths, configure_logging=False)
    try:
        w = MainWindow(ctx)
        qtbot.addWidget(w)
        b = body_with_cards(2)
        cf.set_game_card(b, slot=1, rect=(0.3, 0.5, 0.2, 0.3, 0), asset_id="a0")
        ctx.update_active_profile(lambda body: body.update({"assets": b["assets"], "rois": b["rois"], "game": b["game"]}))
        assert {"card_next", "card_prev", "card_toggle"} <= set(ctx.settings.get("hotkeys"))
        w.on_action("card_next")
        assert cf.game_card_roi(ctx.profiles.active()["body"])["replacement"]["asset_id"] == "a1"
        w.on_action("card_toggle")
        assert cf.game_card_roi(ctx.profiles.active()["body"])["enabled"] is False
        w.on_action("card_toggle")
        assert any(r.id == cf.GAME_CARD_ID and r.enabled for r in ctx.rois.rois())
    finally:
        ctx.shutdown()


def test_paste_and_drop_add_cards(qtbot, paths, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from vcstudio.app.application import AppContext
    from vcstudio.ui import game_card_dialog as gd
    ctx = AppContext(paths, configure_logging=False)
    try:
        dlg = gd.GameCardDialog(ctx)
        qtbot.addWidget(dlg)
        img = QImage(60, 90, QImage.Format_RGB32); img.fill(0xff2040c0)
        QApplication.clipboard().setImage(img)
        dlg.paste()
        f = tmp_path / "dropped.png"
        cv2.imwrite(str(f), np.zeros((90, 60, 3), np.uint8))
        md = QMimeData(); md.setUrls([QUrl.fromLocalFile(str(f))])
        dlg.add_cards(gd._paths_from_mime(md))
        dlg.add_cards([str(tmp_path / "notes.txt")])          # not an image: reported, not added
        assert dlg.cards.count() == 2
        assert len(cf.game_state(ctx.profiles.active()["body"])["cards"]) == 2
    finally:
        ctx.shutdown()
