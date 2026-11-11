import string

from vcstudio import i18n
from vcstudio.i18n_ru import RU


def _fields(s):
    return sorted(f for _, f, _, _ in string.Formatter().parse(s) if f)


def test_tr_falls_back_to_english_and_formats():
    i18n.set_language("ru")
    try:
        assert i18n.tr("Start capture") == "Начать захват"
        assert i18n.tr("Profile: {name}", name="Default") == "Профиль: Default"
        assert i18n.tr("Some text nobody translated") == "Some text nobody translated"
        assert i18n.tr("{n} profile(s)", n=2) == "профилей: 2"
    finally:
        i18n.set_language("en")
    assert i18n.tr("Start capture") == "Start capture"
    assert i18n.tr("Profile: {name}", name="X") == "Profile: X"


def test_russian_placeholders_match_english():
    bad = [k for k, v in RU.items() if _fields(k) != _fields(v)]
    assert not bad


def test_language_setting_resolution(monkeypatch):
    assert i18n.resolve("ru") == "ru" and i18n.resolve("en") == "en"
    monkeypatch.setattr(i18n, "system_language", lambda: "ru")
    assert i18n.resolve("auto") == "ru"
