import json

import pytest

from vcstudio.core.config import DEFAULT_SETTINGS, Settings, SettingsError, validate_settings
from vcstudio.core.jsonio import deep_merge


def test_defaults_are_valid():
    assert validate_settings(DEFAULT_SETTINGS) == []


def test_first_load_creates_file(paths):
    s = Settings(paths)
    assert s.load() == []
    assert paths.settings_file.exists()
    assert s.get("output.width") == 1920


def test_set_persists_and_reloads(paths):
    s = Settings(paths); s.load()
    s.set("performance.mode", "low_resource")
    s2 = Settings(paths); s2.load()
    assert s2.get("performance.mode") == "low_resource"


def test_invalid_set_rejected(paths):
    s = Settings(paths); s.load()
    with pytest.raises(SettingsError):
        s.set("output.fps", 0)
    with pytest.raises(SettingsError):
        s.set("performance.mode", "turbo")
    assert s.get("output.fps") == 60


def test_corrupt_file_recovers_with_backup(paths):
    paths.settings_file.write_text("{not json")
    s = Settings(paths)
    problems = s.load()
    assert problems and s.get("output.fps") == 60
    assert paths.settings_file.with_suffix(".corrupt.json").exists()


def test_invalid_values_repaired_individually(paths):
    paths.settings_file.write_text(json.dumps({"output": {"fps": 9999, "width": 1280}, "performance": {"mode": "x"}}))
    s = Settings(paths)
    problems = s.load()
    assert len(problems) == 2
    assert s.get("output.fps") == 60 and s.get("output.width") == 1280
    assert s.get("performance.mode") == "balanced"


def test_unknown_keys_preserved_and_new_defaults_merged(paths):
    paths.settings_file.write_text(json.dumps({"custom": 1}))
    s = Settings(paths); s.load()
    assert s.get("custom") == 1 and s.get("input.preferred_fps") == 60


def test_listener_called(paths):
    s = Settings(paths); s.load()
    seen = []
    s.add_listener(lambda k, v: seen.append((k, v)))
    s.set("ui.show_preview", False)
    assert seen == [("ui.show_preview", False)]


def test_deep_merge_does_not_mutate():
    a = {"x": {"y": 1}}
    b = {"x": {"z": 2}}
    m = deep_merge(a, b)
    assert m == {"x": {"y": 1, "z": 2}} and a == {"x": {"y": 1}}
