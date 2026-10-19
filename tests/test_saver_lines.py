"""saver: JSON Lines broken lines, documents without records and the separators at their edges
(docs/ARCHITECTURE.md §12.7 "JSONL", "zero records", "a file WITHOUT base records").

* broken lines refuse a save without ``drop_broken`` (``saver.error.broken_lines``); with it they are removed
  wherever they are -- before the first record, between records (also inside copied runs of untouched
  records), after the last one (also a cut last line without a line end) -- also beyond the 10 000 kept in
  memory and the 100 000 listed in the index, with LF and CRLF line ends, blank lines kept;
* every kind with all records deleted; files without records (``[]``, a forced keyed ``{}``, a forced
  wrapped empty container in a pretty wrapper, JSON Lines of broken lines only, a whitespace-only file
  forced to jsonl/stream) that get records; a stream whose values touch (``{}{}``) next to an edit.
Each output is checked against the record-by-record oracle of ``tests/saver_helpers.py`` and reopened with
``reopen_options``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jsonreshape import reader, saver
from jsonreshape.errors import EditError
from jsonreshape.reader import Document
from overlay_helpers import KINDS, OverlayBuilder
from saver_helpers import decode_saved, make_doc, oracle_text, reopen


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


def open_text(directory: Path, docs: list, text: bytes, fmt: str, **kw) -> Document:
    doc = make_doc(directory, fmt, text=text, open_kw={"format": fmt, **kw}, name=kw.pop("name", None))
    docs.append(doc)
    return doc


def save_and_check(doc: Document, ob: OverlayBuilder | None, out: Path, *, drop_broken: bool = False,
                   expect_records: list[bytes] | None = None) -> saver.SaveReport:
    want = oracle_text(doc, ob, drop_broken=drop_broken)
    report = saver.save(doc, out, drop_broken=drop_broken)
    text, _e, _b, _g = decode_saved(out.read_bytes())
    assert text == want
    new = reopen(out, report)
    try:
        assert new.error is None and new.broken_count == 0
        records = expect_records if expect_records is not None else (
            list(ob.records) if ob is not None else None)
        if records is not None:
            assert [new.record_bytes(i) for i in range(new.record_count)] == records
    finally:
        new.close()
    return report


# ----------------------------------------------------------------------------------
# JSON Lines broken lines
# ----------------------------------------------------------------------------------
BROKEN_TEXT = (b'not json at all\n\n{"a": 1}\n{"a": 2}\n{"a": 3, oops\n   \n{"a": 4}\n{"a": 5}\n'
               b'trailing [garbage\n{"a": 6}\n{"cut": ')


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_broken_lines_refused_then_removed(tmp_path, docs, newline, lang_en) -> None:
    text = BROKEN_TEXT.replace(b"\n", newline)
    doc = open_text(tmp_path / "s", docs, text, "jsonl")
    assert doc.broken_count == 4 and doc.record_count == 5
    with pytest.raises(EditError) as ei:
        saver.save(doc, tmp_path / "o.jsonl")
    assert ei.value.key == "saver.error.broken_lines" and "4 broken lines" in str(ei.value)
    assert ei.value.hint and not (tmp_path / "o.jsonl").exists()
    # unedited
    report = save_and_check(doc, None, tmp_path / "o.jsonl", drop_broken=True,
                            expect_records=[b'{"a": %d}' % i for i in (1, 2, 4, 5, 6)])
    assert report.warning_keys == ["saver.warn.broken_dropped"] and "4 broken lines" in report.warnings[0]
    assert report.broken_removed == 4
    out = (tmp_path / "o.jsonl").read_bytes()
    assert out == (b'\n{"a": 1}\n{"a": 2}\n   \n{"a": 4}\n{"a": 5}\n{"a": 6}\n').replace(b"\n", newline)
    # edits next to the broken lines
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(1, b'{"a": 20}')
    ob.insert(3, b'{"new": true}')
    ob.delete(len(ob) - 1)
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o2.jsonl", drop_broken=True)


def test_one_broken_line_uses_the_singular(tmp_path, docs, lang_en) -> None:
    doc = open_text(tmp_path / "s", docs, b'{"a":1}\nbad\n{"a":2}\n', "jsonl")
    report = save_and_check(doc, None, tmp_path / "o.jsonl", drop_broken=True)
    assert report.warnings == ["1 broken line was removed from the file."]


def test_broken_lines_inside_a_long_run(tmp_path, docs) -> None:
    """More than BROKEN_KEEP (10 000) broken lines between untouched records copied as runs."""
    parts = []
    for i in range(6000):
        parts.append(b'{"i": %d}\n' % i)
        if i % 3:
            parts.append(b"broken %d\n" % i)
            parts.append(b"{also broken\n")
    text = b"".join(parts)
    doc = open_text(tmp_path / "s", docs, text, "jsonl")
    assert doc.broken_count == 8000 and doc.broken_count > 0
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(2500, b'{"edited": 2500}')
    doc.set_overlay(ob.state())
    report = save_and_check(doc, ob, tmp_path / "o.jsonl", drop_broken=True)
    assert report.broken_removed == 8000
    many = b"".join(b'{"i": %d}\nbad %d\n' % (i, i) for i in range(10500))
    doc2 = open_text(tmp_path / "t", docs, many, "jsonl")
    assert doc2.broken_count == 10500 > len(doc2.broken_lines)
    save_and_check(doc2, None, tmp_path / "o2.jsonl", drop_broken=True,
                   expect_records=[b'{"i": %d}' % i for i in range(10500)])


def test_broken_lines_beyond_the_listed_ones(tmp_path, docs, monkeypatch) -> None:
    """When the index lists fewer broken lines than ``broken_count`` every gap holding a non-whitespace
    byte is removed all the same."""
    monkeypatch.setattr(reader, "BROKEN_KEEP", 5)
    monkeypatch.setattr(reader, "BROKEN_META_CAP", 20)
    text = b"".join(b'{"i": %d}\n\tjunk %d\n' % (i, i) for i in range(300)) + b"x"
    doc = open_text(tmp_path / "s", docs, text, "jsonl")
    assert doc.broken_count == 301 and len(list(doc.iter_broken_lines())) == 20
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.move([10, 11], 200)
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o.jsonl", drop_broken=True)


def test_jsonl_of_broken_lines_only_gets_records(tmp_path, docs) -> None:
    doc = open_text(tmp_path / "s", docs, b"oops\n{bad\n", "jsonl")
    assert doc.record_count == 0 and doc.broken_count == 2
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.insert(0, b'{"a":1}')
    ob.insert(1, b'"two"')
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o.jsonl", drop_broken=True)
    assert (tmp_path / "o.jsonl").read_bytes() == b'{"a":1}\n"two"\n'


@pytest.mark.parametrize("fmt", ["jsonl", "stream"])
def test_whitespace_only_file_gets_records(tmp_path, docs, fmt) -> None:
    doc = open_text(tmp_path / "s", docs, b"  \n\n", fmt)
    assert doc.record_count == 0
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.insert(0, b"[1, 2]")
    ob.insert(1, b"3")
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o.json")
    assert (tmp_path / "o.json").read_bytes() == b"[1, 2]\n3\n"


# ----------------------------------------------------------------------------------
# zero records, files without records
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("style", ["compact", "pretty2", "crlf"])
@pytest.mark.parametrize("kind", [k for k in KINDS if k != "single"])
def test_every_record_deleted(tmp_path, docs, kind, style) -> None:
    doc = make_doc(tmp_path / "s", kind, style, n=6)
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.delete(*range(len(ob)))
    doc.set_overlay(ob.state())
    out = tmp_path / "o.json"
    report = save_and_check(doc, ob, out, expect_records=[])
    text = out.read_bytes()
    if doc.kind in ("jsonl", "stream"):
        assert text == b""
    else:
        assert (b"[]" in text or b"{}" in text) and report.records == 0


def test_empty_array_gets_records(tmp_path, docs) -> None:
    doc = open_text(tmp_path / "s", docs, b" [ ] \n", "array")
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.insert(0, b'{"a":1}')
    ob.insert(1, b"[2]")
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o.json")
    assert (tmp_path / "o.json").read_bytes() == b' [{"a":1},[2]] \n'


def test_forced_keyed_empty_object_gets_members(tmp_path, docs) -> None:
    doc = open_text(tmp_path / "s", docs, b"{}", "keyed")
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.insert(0, b'"k1":{"v":1}')
    ob.insert(1, b'"k2":2')
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o.json")
    assert (tmp_path / "o.json").read_bytes() == b'{"k1":{"v":1},"k2":2}'


def test_pretty_wrapped_empty_container_gets_records(tmp_path, docs) -> None:
    text = b'{\n  "meta": {\n    "v": 1\n  },\n  "data": [],\n  "tail": true\n}\n'
    doc = open_text(tmp_path / "s", docs, text, "wrapped", record_path="data")
    assert doc.record_count == 0
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.insert(0, b'{\n    "a": 1\n  }')
    ob.insert(1, b"2")
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o.json")
    assert (tmp_path / "o.json").read_bytes() == (
        b'{\n  "meta": {\n    "v": 1\n  },\n  "data": [\n    {\n    "a": 1\n  },\n    2\n  ],\n  "tail": true\n}\n')


def test_edited_wrapper_without_records(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "wrapped", "pretty2", n=4)
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set_wrapper(b'{"meta": {"v": 3}, "data": [], "extra": "x"}')
    ob.delete(*range(len(ob)))
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o.json", expect_records=[])
    assert (tmp_path / "o.json").read_bytes().strip() == b'{"meta": {"v": 3}, "data": [], "extra": "x"}'


def test_stream_values_that_touch(tmp_path, docs) -> None:
    doc = open_text(tmp_path / "s", docs, b'{"a":1}{"b":2}{"c":3}\n', "stream")
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(1, b"7")                 # "{...}7{...}" is fine, but two numbers would merge
    ob.set(2, b"8")
    doc.set_overlay(ob.state())
    save_and_check(doc, ob, tmp_path / "o.json")
    assert (tmp_path / "o.json").read_bytes() == b'{"a":1}\n7\n8\n'
    ob2 = OverlayBuilder(doc, tmp_path / "layer2")
    ob2.set(1, b'{"b":20}')         # untouched neighbours keep their (empty) gaps
    doc.set_overlay(ob2.state())
    save_and_check(doc, ob2, tmp_path / "o2.json")
