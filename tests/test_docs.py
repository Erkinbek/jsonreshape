"""Documentation sanity checks (AGENT_PROMPT §16: the README is updated at the end of every stage)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_has_no_placeholders() -> None:
    assert "PLACEHOLDER" not in _readme()


def test_test_files_named_in_readme_exist() -> None:
    names = set(re.findall(r"`(test_[a-z0-9_]+\.py)`", _readme()))
    assert names, "the README lists the acceptance tests"
    missing = sorted(n for n in names if not (ROOT / "tests" / n).exists())
    assert not missing, missing


def test_readme_cli_examples_have_no_inline_comments() -> None:
    """cmd.exe has no inline comments: example command lines must not end with '# ...'."""
    text = _readme()
    for block in re.findall(r"```text\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            if line.startswith("jsonreshape "):
                assert " #" not in line, line


def test_architecture_doc_mentions_current_cli() -> None:
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for needle in ("--preset NAME", "--sort-keys [alpha|codepoint]", "ensure_unchanged", "locate_tokens"):
        assert needle in arch, needle
