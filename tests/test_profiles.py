import json

import pytest

from vcstudio.core.profiles import DEFAULT_PROFILE_NAME, ProfileError, ProfileManager


@pytest.fixture
def pm(paths):
    m = ProfileManager(paths)
    m.load_all()
    m.switch(DEFAULT_PROFILE_NAME)
    return m


def test_default_created(pm, paths):
    assert pm.names() == [DEFAULT_PROFILE_NAME]
    assert len(list(paths.profiles_dir.glob("*.json"))) == 1


def test_create_rename_duplicate_delete(pm, paths):
    pm.create("Stream A")
    pm.rename("Stream A", "Stream B")
    assert "Stream B" in pm.names() and "Stream A" not in pm.names()
    d = pm.duplicate("Stream B")
    assert d["name"] == "Stream B copy"
    pm.delete("Stream B")
    assert pm.names() == sorted([DEFAULT_PROFILE_NAME, "Stream B copy"], key=str.casefold)
    assert len(list(paths.profiles_dir.glob("*.json"))) == 2


def test_persistence_roundtrip(pm, paths):
    pm.create("Persist")
    body = pm.get("Persist")["body"]
    body["rois"] = [{"id": "r1", "kind": "rect", "enabled": True}]
    body["detection"]["confidence"] = 0.6
    pm.update_body("Persist", body)
    m2 = ProfileManager(paths); m2.load_all()
    b2 = m2.get("Persist")["body"]
    assert b2["rois"][0]["id"] == "r1" and b2["detection"]["confidence"] == 0.6


def test_duplicate_names_and_invalid_names(pm):
    with pytest.raises(ProfileError):
        pm.create(DEFAULT_PROFILE_NAME)
    for bad in ("", "a/b", "x" * 65, "bad\nname"):
        with pytest.raises(ProfileError):
            pm.create(bad)


def test_cannot_delete_last(pm):
    with pytest.raises(ProfileError):
        pm.delete(DEFAULT_PROFILE_NAME)


def test_delete_active_switches(pm):
    pm.create("Other")
    pm.switch("Other")
    pm.delete("Other")
    assert pm.active_name == DEFAULT_PROFILE_NAME


def test_rename_active_tracks(pm):
    pm.rename(DEFAULT_PROFILE_NAME, "Main")
    assert pm.active_name == "Main"


def test_export_import_with_conflict(pm, tmp_path):
    pm.create("Exp")
    out = pm.export("Exp", tmp_path / "exp.json")
    imported = pm.import_file(out)
    assert imported["name"] == "Exp (2)"
    assert imported["id"] != pm.get("Exp")["id"]


def test_import_rejects_garbage(pm, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"kind": "something.else", "name": "x", "body": {}}))
    with pytest.raises(ProfileError):
        pm.import_file(p)
    p.write_text(json.dumps([1, 2]))
    with pytest.raises(ProfileError):
        pm.import_file(p)


def test_invalid_file_on_disk_skipped(pm, paths):
    (paths.profiles_dir / "junk.json").write_text("{oops")
    m2 = ProfileManager(paths); m2.load_all()
    assert m2.names() == [DEFAULT_PROFILE_NAME]


def test_next_name_cycles(pm):
    pm.create("B"); pm.create("C")
    seen = []
    for _ in range(3):
        pm.switch(pm.next_name()); seen.append(pm.active_name)
    assert sorted(seen) == ["B", "C", DEFAULT_PROFILE_NAME]


def test_listener_events(pm):
    ev = []
    pm.add_listener(lambda e, n: ev.append(e))
    pm.create("L"); pm.switch("L"); pm.rename("L", "M"); pm.delete("M")
    assert ev[:4] == ["created", "switched", "renamed", "deleted"]
