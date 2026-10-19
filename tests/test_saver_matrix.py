"""saver.save over a seeded sample of the whole product kind x style x encoding x edit pattern x
(in place | Save As) x core backend (docs/ARCHITECTURE.md §12.7, AGENT_PROMPT §14 test 19).

The other saver modules cover each dimension against the others in pairs; this one draws 72 random
combinations (fixed seed) so that interactions -- a CRLF gzip UTF-16 keyed file with moved records saved in
place, ... -- are exercised too. Each saved file is compared byte for byte with the oracle of
``tests/saver_helpers.py`` and, independently of it, reopened (``reopen_options``) and compared with the
plain document of the expected content written by ``tests/overlay_helpers.py`` (records and the whole
root value).
"""

from __future__ import annotations

import random

import pytest

from jsonreshape import editops, reader, saver
from jsonreshape.reader import Document
from overlay_helpers import KINDS, OverlayBuilder, open_expected
from saver_helpers import (
    ENCODINGS,
    PATTERNS,
    STYLES,
    apply_pattern,
    decode_saved,
    make_doc,
    oracle_text,
    reopen,
    source_traits,
    temp_leftovers,
)


def _cases(count: int, seed: int) -> list[tuple]:
    rng = random.Random(seed)
    out = []
    for i in range(count):
        out.append((rng.choice(KINDS), rng.choice(STYLES), rng.choice(ENCODINGS), rng.choice(PATTERNS),
                    rng.random() < 0.5, rng.random() < 0.25, i))
    return out


CASES = _cases(72, 20261018)


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


@pytest.mark.parametrize("kind, style, encoding, pattern, in_place, python_core, seed", CASES)
def test_sampled_combination(tmp_path, docs, kind, style, encoding, pattern, in_place, python_core, seed) -> None:
    old = reader.set_scanner_backend("python" if python_core else None)
    old_ops = editops.set_core_backend("python" if python_core else None)
    try:
        name = "src.json.gz" if encoding.startswith("gzip") else "src.json"
        doc = make_doc(tmp_path / "src", kind, style, n=14, seed=seed, encoding=encoding, name=name)
        docs.append(doc)
        ob = OverlayBuilder(doc, tmp_path / "layer")
        apply_pattern(ob, doc, pattern, seed)
        doc.set_overlay(ob.state())
        want = oracle_text(doc, ob)
        expected = open_expected(doc, ob.records, tmp_path / "expected.json", wrapper=ob.wrapper_text)
        docs.append(expected)
        src = doc.source_path
        before = src.read_bytes()
        info_enc, bom, gz = source_traits(encoding)
        if in_place:
            report = saver.save(doc)
            out = src
            assert report.backup is not None and report.backup.read_bytes() == before
        else:
            out = tmp_path / ("out.json.gz" if gz else "out.json")
            report = saver.save(doc, out)
            assert src.read_bytes() == before
        text, enc, has_bom, is_gz = decode_saved(out.read_bytes())
        assert text == want
        assert has_bom == bom and is_gz == gz and report.encoding == info_enc
        new = reopen(out, report)
        docs.append(new)
        assert new.error is None and new.record_count == expected.record_count
        assert [new.record_bytes(i) for i in range(new.record_count)] == \
            [expected.record_bytes(i) for i in range(expected.record_count)]
        if new.kind in ("array", "keyed", "wrapped", "single"):
            assert new.root().value() == expected.root().value()
        assert temp_leftovers(src.parent) == []
    finally:
        reader.set_scanner_backend(old)
        editops.set_core_backend(old_ops)
