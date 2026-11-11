"""User-interface translation.

English source strings are the keys. `tr("Start capture")` returns the translation for the active
language, or the English text itself when there is none, so a missing entry never breaks the UI.
Placeholders use str.format: tr("{n} regions", n=3).

The language is chosen once at startup (settings `ui.language`: auto | en | ru; auto follows the
macOS preferred language). Log messages, file formats and settings keys stay English.
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

LANGUAGES = {"en": "English", "ru": "Русский"}
_lang = "en"
_tables: dict[str, dict[str, str]] = {}


def _table(lang: str) -> dict[str, str]:
    if lang not in _tables:
        if lang == "ru":
            from vcstudio.i18n_ru import RU
            _tables[lang] = RU
        else:
            _tables[lang] = {}
    return _tables[lang]


def system_language() -> str:
    """First macOS preferred language we support, else "en"."""
    try:
        out = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLanguages"], capture_output=True,
                             text=True, timeout=2).stdout
        for line in out.splitlines():
            code = line.strip().strip('",()').lower()
            if code:
                base = code.split("-")[0].split("_")[0]
                return base if base in LANGUAGES else "en"
    except Exception:
        pass
    return "en"


def resolve(setting: str | None) -> str:
    return setting if setting in LANGUAGES else system_language()


def set_language(lang: str) -> None:
    global _lang
    _lang = lang if lang in LANGUAGES else "en"
    log.info("UI language: %s", _lang)


def language() -> str:
    return _lang


def tr(text: str, **kw) -> str:
    out = _table(_lang).get(text, text) if _lang != "en" else text
    return out.format(**kw) if kw else out
