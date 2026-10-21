"""The Russian UI texts (``jsonreshape/i18n_ru.py``, stage 7): every key exists in ``i18n.MESSAGES`` with the same
placeholders and line breaks as the English text, the coverage is complete (new keys need a Russian text — add it
to ``i18n_ru.py``; a key may be listed in ``ALLOW_MISSING_RU`` while its translation is pending), ``t()`` serves
Russian and falls back to English, and the language is offered everywhere (``SUPPORTED_LANGUAGES``, the CLI
``--lang``, the Settings page)."""

from __future__ import annotations

import re

import pytest

from jsonreshape import i18n
from jsonreshape.i18n import MESSAGES, SUPPORTED_LANGUAGES, t
from jsonreshape.i18n_ru import MESSAGES_RU

ALLOW_MISSING_RU: set[str] = set()
ALLOWED_EMPTY = {"json.expected.none"}
PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*(?:![rs])?(?::[^{}]*)?\}|%\([a-zA-Z_][a-zA-Z0-9_]*\)[sdr]")


def _placeholders(text: str) -> list[str]:
    cleaned = re.sub(r"\{\{|\}\}", "", text)
    return sorted(re.sub(r"(:[^{}]*)?\}$", "}", p) for p in PLACEHOLDER.findall(cleaned))


@pytest.fixture
def lang_ru():
    before = i18n.get_language()
    i18n.set_language("ru")
    try:
        yield
    finally:
        i18n.set_language(before)


def test_every_russian_key_exists_and_matches_the_english_placeholders() -> None:
    problems = []
    for key, text in MESSAGES_RU.items():
        entry = MESSAGES.get(key)
        if entry is None:
            problems.append(f"{key}: not in MESSAGES")
            continue
        if not isinstance(text, str) or (not text.strip() and key not in ALLOWED_EMPTY):
            problems.append(f"{key}: empty")
            continue
        if _placeholders(text) != _placeholders(entry["en"]):
            problems.append(f"{key}: placeholders {_placeholders(text)} != {_placeholders(entry['en'])}")
        if text.count("\n") != entry["en"].count("\n"):
            problems.append(f"{key}: line breaks differ from the English text")
    assert not problems, "\n".join(problems[:40])


def test_russian_coverage_is_complete() -> None:
    missing = sorted(k for k in MESSAGES if k not in MESSAGES_RU and k not in ALLOW_MISSING_RU)
    assert not missing, f"{len(missing)} keys without a Russian text, e.g. {missing[:10]}"
    stale = sorted(ALLOW_MISSING_RU & set(MESSAGES_RU))
    assert not stale, f"translated now, remove from ALLOW_MISSING_RU: {stale}"


def test_t_serves_russian_and_falls_back_to_english(lang_ru, monkeypatch) -> None:
    assert t("gui.common.cancel") == MESSAGES_RU["gui.common.cancel"] != MESSAGES["gui.common.cancel"]["en"]
    assert t("gui.main.about.text", version="1.0", backend="x").startswith("JSON Reshape 1.0")
    monkeypatch.setitem(MESSAGES, "test.ru.missing", {"uz": "uz", "en": "english only"})
    assert t("test.ru.missing") == "english only"
    monkeypatch.setitem(MESSAGES, "test.ru.fmt", {"uz": "{n}", "en": "{n}"})
    monkeypatch.setitem(MESSAGES_RU, "test.ru.fmt", "число {n}")
    assert t("test.ru.fmt", n=3) == "число 3"
    assert t("test.ru.fmt") == "число {n}"           # a missing parameter leaves the placeholder, as in uz / en


def test_russian_is_offered_everywhere() -> None:
    assert SUPPORTED_LANGUAGES == ("uz", "en", "ru")
    from jsonreshape.cli import build_parser

    args = build_parser().parse_args(["--lang", "ru", "inspect", "x.json"])
    assert args.lang == "ru"
    from jsonreshape.gui import settingsdialog

    assert ("ru", "Русский") in settingsdialog._LANGUAGES
    from jsonreshape.settings import Settings

    assert Settings(language="ru").language == "ru"


def test_russian_numbers_are_space_grouped(lang_ru) -> None:
    from jsonreshape.gui.widgets import format_count

    assert i18n.space_grouped() and format_count(1234567) == "1 234 567"
    i18n.set_language("en")
    assert not i18n.space_grouped() and format_count(1234567) == "1,234,567"


def test_language_texts_mention_russian() -> None:
    for key in ("cli.help.lang", "settings.expected.language"):
        for lang in ("uz", "en"):
            assert "ru" in MESSAGES[key][lang], (key, lang)
        assert "ru" in MESSAGES_RU[key]
