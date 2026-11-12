"""Everything a user sets survives a restart: settings, profiles, regions, assets, cards, game layouts, models."""
import shutil

import cv2
import numpy as np

from vcstudio.ai import model_import as mi
from vcstudio.ai.model_manager import REPO_MODELS
from vcstudio.game import cardframe as cf
from vcstudio.roi.model import QUICK_FACE_ID, new_rect, set_quick_face


def test_everything_survives_restart(paths, tmp_path):
    from vcstudio.app.application import AppContext
    from vcstudio.replacement.spec import ReplacementSpec

    ctx = AppContext(paths, configure_logging=False)
    try:
        s = ctx.settings
        s.set("ui.language", "ru")
        s.set("input.method", "obs_ipc")
        hk = dict(s.get("hotkeys")); hk["card_next"] = "Ctrl+Alt+K"; s.set("hotkeys", hk)

        img = tmp_path / "card.png"; cv2.imwrite(str(img), np.full((90, 60, 3), (0, 0, 255), np.uint8))
        asset = ctx.assets.import_file(img).to_dict()
        ctx.profiles.create("Игра")
        ctx.profiles.switch("Игра")
        logo = new_rect("Логотип", 0.1, 0.1, 0.3, 0.3)
        logo.replacement = ReplacementSpec(enabled=True, asset_id=asset["id"], fit="fit").to_dict()

        def mut(b):
            b["assets"].append(asset)
            b["rois"].append(logo.to_dict())
            set_quick_face(b, True)
            cf.set_game_card(b, slot=1, rect=(0.4, 0.5, 0.2, 0.3, 0.0), asset_id=asset["id"], flip=True)
            cf.save_layout(b, "Моя игра")
            b["detection"]["confidence"] = 0.6
            b["performance_mode"] = "performance"          # what the Performance page stores with the setting
        ctx.update_active_profile(mut)
        s.set("performance.mode", "performance")

        pkg = tmp_path / "my_model.mlpackage"; shutil.copytree(REPO_MODELS / "yolo11n.mlpackage", pkg)
        mi.install(mi.inspect_model(pkg, runs=3), ctx.models.user_dir, "my_model", {"person": "player"}, 0.4)
        (paths.root / "reports").mkdir(exist_ok=True)
        (paths.root / "reports" / "e2e-obs-test.json").write_text('{"ok": true}')
    finally:
        ctx.shutdown()

    ctx = AppContext(paths, configure_logging=False)            # restart on the same data folder
    try:
        s = ctx.settings
        assert s.get("ui.language") == "ru" and s.get("performance.mode") == "performance"
        assert s.get("input.method") == "obs_ipc" and s.get("hotkeys")["card_next"] == "Ctrl+Alt+K"
        assert ctx.profiles.active_name == "Игра" and "Игра" in ctx.profiles.names()
        b = ctx.profiles.active()["body"]
        assert b["detection"]["confidence"] == 0.6
        ids = {r["id"] for r in b["rois"]}
        assert QUICK_FACE_ID in ids and cf.GAME_CARD_ID in ids and any(r["name"] == "Логотип" for r in b["rois"])
        card = cf.game_card_roi(b)
        assert card["replacement"]["flip_h"] is True and card["cx"] == 0.4
        assert "Моя игра" in cf.game_state(b)["layouts"]
        a = b["assets"][0]
        assert (ctx.assets.dir / a["file"]).exists()                 # the image file itself, not only its record
        assert {r.id for r in ctx.rois.rois()} >= {QUICK_FACE_ID, cf.GAME_CARD_ID}   # loaded into the live services
        assert ctx.replacement.active()
        info = ctx.models.available()["my_model"]
        assert info.origin == "user" and info.classes[0] == "player" and info.confidence == 0.4
        assert (paths.root / "reports" / "e2e-obs-test.json").exists()
    finally:
        ctx.shutdown()
