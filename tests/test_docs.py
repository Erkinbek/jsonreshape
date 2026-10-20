"""Documentation sanity checks (AGENT_PROMPT §16: the README is updated at the end of every stage)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


READMES = ("README.md", "README.uz.md", "README.ru.md")      # English (main), Uzbek, Russian


def _readme(name: str = "README.md") -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", READMES)
def test_readme_has_no_placeholders(name: str) -> None:
    assert "PLACEHOLDER" not in _readme(name)


@pytest.mark.parametrize("name", READMES)
def test_test_files_named_in_readme_exist(name: str) -> None:
    names = set(re.findall(r"`(test_[a-z0-9_]+\.py)`", _readme(name)))
    assert names, "the README lists the acceptance tests"
    missing = sorted(n for n in names if not (ROOT / "tests" / n).exists())
    assert not missing, missing


@pytest.mark.parametrize("name", READMES)
def test_readme_cli_examples_have_no_inline_comments(name: str) -> None:
    """cmd.exe has no inline comments: example command lines must not end with '# ...'."""
    text = _readme(name)
    for block in re.findall(r"```text\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            if line.startswith("jsonreshape "):
                assert " #" not in line, line


def test_readmes_link_each_other_and_keep_the_same_sections() -> None:
    """README.md (English) is the main one; README.uz.md and README.ru.md are its Uzbek and Russian versions: each
    links the other two at the top, all have the same anchors (the links between sections), the author section and
    the copyright line."""
    en, uz, ru = _readme("README.md"), _readme("README.uz.md"), _readme("README.ru.md")
    heads = {name: text.split("\n## ", 1)[0] for name, text in (("en", en), ("uz", uz), ("ru", ru))}
    assert "[O'zbekcha](README.uz.md)" in heads["en"] and "[Русский](README.ru.md)" in heads["en"]
    assert "[English](README.md)" in heads["uz"] and "[Русский](README.ru.md)" in heads["uz"]
    assert "[English](README.md)" in heads["ru"] and "[O'zbekcha](README.uz.md)" in heads["ru"]
    anchors = re.compile(r'<a id="([a-z0-9-]+)"></a>')
    assert anchors.findall(en) == anchors.findall(uz) == anchors.findall(ru) and "muallif" in anchors.findall(en)
    for text in (en, uz, ru):
        assert "Erkin Pardayev" in text and "Claude Opus 5.5" in text and "© 2026 Erkin Pardayev" in text
        assert "[Apache License 2.0](LICENSE)" in text
        assert "https://github.com/Erkinbek" in text and "[@ErkinPardayev](https://t.me/ErkinPardayev)" in text
        for target in set(re.findall(r"\]\(#([a-z0-9-]+)\)", text)):
            assert target in anchors.findall(text), target


def test_architecture_doc_mentions_current_cli() -> None:
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for needle in ("--preset NAME", "--sort-keys [alpha|codepoint]", "ensure_unchanged", "locate_tokens"):
        assert needle in arch, needle


def test_license_files() -> None:
    """Apache-2.0 (the user's choice, 2026-10-20): the full text, the NOTICE, and the metadata agree."""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith("Apache License") and "Version 2.0, January 2004" in text
    assert "END OF TERMS AND CONDITIONS" in text
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert notice.startswith("jsonreshape\nCopyright 2026 Erkin Pardayev")
    assert 'license = { text = "Apache-2.0" }' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for name in ("THIRD-PARTY-NOTICES.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md",
                 ".github/ISSUE_TEMPLATE/bug_report.md", ".github/ISSUE_TEMPLATE/feature_request.md",
                 ".github/PULL_REQUEST_TEMPLATE.md"):
        assert (ROOT / name).is_file(), name
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in changelog and "## [0.1.0]" in changelog
