"""Documentation sanity checks (AGENT_PROMPT §16: the README is updated at the end of every stage)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


READMES = ("README.md", "README.uz.md")      # English (main) and Uzbek


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
    """README.md (English) is the main one; README.uz.md is its Uzbek version: each links the other at the top, both
    have the same anchors (the links between sections), the author section and the copyright line."""
    en, uz = _readme("README.md"), _readme("README.uz.md")
    assert "[O'zbekcha](README.uz.md)" in en.split("\n## ", 1)[0]
    assert "[English](README.md)" in uz.split("\n## ", 1)[0]
    anchors = re.compile(r'<a id="([a-z0-9-]+)"></a>')
    assert anchors.findall(en) == anchors.findall(uz) and "muallif" in anchors.findall(en)
    for text in (en, uz):
        assert "Erkin Pardayev" in text and "Claude Opus 5.5" in text and "© 2026 Erkin Pardayev" in text
        assert "https://github.com/Erkinbek" in text
        for target in set(re.findall(r"\]\(#([a-z0-9-]+)\)", text)):
            assert target in anchors.findall(text), target


def test_architecture_doc_mentions_current_cli() -> None:
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for needle in ("--preset NAME", "--sort-keys [alpha|codepoint]", "ensure_unchanged", "locate_tokens"):
        assert needle in arch, needle
