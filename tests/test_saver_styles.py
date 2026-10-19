"""saver.save keeps the formatting of the file (docs/ARCHITECTURE.md §12.7): every document kind x style
(compact, pretty 2 / 4, tab, messy whitespace, CRLF line ends) x edit pattern (point edits, inserts /
deletes / moves / duplicates, an array layer over every 3rd record, an edited wrapper): the saved bytes are
exactly those of the record-by-record oracle of ``tests/saver_helpers.py`` (untouched records and their
gaps byte-identical) and reopen to the expected records; CRLF files get no bare LF.
"""

from __future__ import annotations

import pytest

from jsonreshape import saver
from jsonreshape.reader import Document
from overlay_helpers import KINDS
from saver_helpers import STYLES, check_saved, edited


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("kind", KINDS)
def test_styles_are_kept(tmp_path, docs, kind, style) -> None:
    for pattern in ("point", "structure", "every3", "wrapper"):
        sub = tmp_path / pattern
        doc, ob = edited(sub, docs, kind, pattern, style=style, n=18, seed=3)
        out = sub / "saved.json"
        report = saver.save(doc, out)
        text = check_saved(doc, ob, out, report)
        if style == "crlf" and doc.kind not in ("jsonl",):
            # no bare LF was introduced: every line end of the output is CRLF (the units are compact
            # one-liners or formatted with the file's newline)
            assert b"\n" not in text.replace(b"\r\n", b"")
