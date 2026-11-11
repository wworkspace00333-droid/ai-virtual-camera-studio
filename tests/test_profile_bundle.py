"""Portable profile bundles: round trip with assets, dedupe, and hostile-archive rejection."""
import json
import zipfile
from pathlib import Path

import pytest

from vcstudio.app.application import AppContext
from vcstudio.core.paths import AppPaths
from vcstudio.core.profile_bundle import export_bundle, import_bundle
from vcstudio.core.profiles import ProfileError
from vcstudio.replacement.spec import ReplacementSpec
from vcstudio.roi.model import new_rect

DATA = Path(__file__).parent / "data"


def make_ctx(root):
    return AppContext(AppPaths(root).ensure(), configure_logging=False)


@pytest.fixture
def two(tmp_path):
    a, b = make_ctx(tmp_path / "A"), make_ctx(tmp_path / "B")
    yield a, b
    a.shutdown(); b.shutdown()


def seed(ctx):
    logo = ctx.assets.import_file(DATA / "logo_alpha.png")
    clip = ctx.assets.import_file(DATA / "clip_h264.mp4")
    r = new_rect("Logo", 0.1, 0.1, 0.4, 0.4)
    r.replacement = ReplacementSpec(enabled=True, asset_id=logo.id).to_dict()
    v = new_rect("Video", 0.5, 0.5, 0.9, 0.9)
    v.replacement = ReplacementSpec(enabled=True, asset_id=clip.id).to_dict()
    ctx.profiles.create("Show", {"assets": [logo.to_dict(), clip.to_dict()], "rois": [r.to_dict(), v.to_dict()]})
    return logo, clip


def test_round_trip_to_another_machine(two, tmp_path):
    a, b = two
    logo, clip = seed(a)
    path = export_bundle(a, "Show", tmp_path / "show")
    assert path.suffix == ".vcsprofile"
    doc = import_bundle(b, path)
    assert doc["name"] == "Show"
    body = b.profiles.get("Show")["body"]
    assert [x["file"] for x in body["assets"]] == [logo.file, clip.file]
    for f in (logo.file, clip.file):
        assert (b.assets.dir / f).read_bytes() == (a.assets.dir / f).read_bytes()
    # the imported profile renders: assets resolve in machine B
    b.replacement.load_profile(body)
    specs, assets = b.replacement.state()
    assert all(b.assets.path(x).exists() for x in assets.values())


def test_reimport_reuses_identical_assets(two, tmp_path):
    a, b = two
    seed(a)
    path = export_bundle(a, "Show", tmp_path / "s.vcsprofile")
    import_bundle(b, path)
    n = len(list(b.assets.dir.iterdir()))
    doc2 = import_bundle(b, path)
    assert doc2["name"] == "Show (2)" and len(list(b.assets.dir.iterdir())) == n


@pytest.mark.parametrize("entry", ["../evil.txt", "/etc/passwd", "assets/../../x", "assets/sub/x.png", "other.txt"])
def test_rejects_hostile_entries(two, tmp_path, entry):
    a, b = two
    bad = tmp_path / "bad.vcsprofile"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("profile.json", json.dumps({"kind": "aivcstudio.profile", "name": "X", "body": {}}))
        z.writestr(entry, b"x")
    with pytest.raises(ProfileError):
        import_bundle(b, bad)
    assert "X" not in b.profiles.names()
    assert not any(p.name.startswith(".import") for p in b.assets.dir.iterdir())


def test_missing_asset_rolls_back(two, tmp_path):
    a, b = two
    logo, clip = seed(a)
    path = tmp_path / "partial.vcsprofile"
    with zipfile.ZipFile(export_bundle(a, "Show", tmp_path / "full")) as src, zipfile.ZipFile(path, "w") as dst:
        for i in src.infolist():
            if not i.filename.endswith(clip.file):
                dst.writestr(i, src.read(i))
    with pytest.raises(ProfileError, match="missing"):
        import_bundle(b, path)
    assert "Show" not in b.profiles.names() and not (b.assets.dir / logo.file).exists()


def test_not_a_zip(two, tmp_path):
    p = tmp_path / "x.vcsprofile"; p.write_text("hello")
    with pytest.raises(ProfileError):
        import_bundle(two[1], p)


def test_profiles_page_export_import_bundle(qtbot, tmp_path):
    from vcstudio.ui.main_window import MainWindow
    ctx = make_ctx(tmp_path / "UI")
    seed(ctx)
    w = MainWindow(ctx); qtbot.addWidget(w)
    page = w.pages[w.page_keys().index("profiles")]
    page.reload()
    for i in range(page.list.count()):
        if page.list.item(i).data(256) == "Show":
            page.list.setCurrentRow(i)
    out = page.export(str(tmp_path / "show.vcsprofile"))
    assert Path(out).exists()
    ctx.profiles.delete("Show")
    doc = page.import_(str(out))
    assert doc["name"] == "Show" and "Show" in ctx.profiles.names()
    w.close(); ctx.shutdown()
