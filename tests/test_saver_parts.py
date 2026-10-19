"""saver.write_part: one part of a split through the saver's planner (docs/ARCHITECTURE.md §14.4,
s5/map_convert.md §3.1/§3.3).

* every kind x style x both core backends: a part's bytes are exactly the document's head, the
  records ``[p0, p1)`` with their original gaps and the tail (an oracle restating the rules from the
  raw file); JSON Lines / concatenated values: file head only in the first part, file tail only in
  the last, the original gap after any other part -> the parts laid end to end ARE the file;
* parts reopen as the same kind with the same records; ``wrapper=False``; brace lists become arrays;
* encodings (BOM, UTF-16 LE/BE, gzip) are kept in every part;
* edited (pinned overlay) documents: the records of the effective state; JSON Lines parts laid end to
  end equal the full save;
* broken JSON Lines lines refused / dropped; positions checked; progress meter; cancellation;
  validation failures.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jsonreshape import editops, fileio, reader, saver
from jsonreshape.errors import Cancelled, EditError, ExportError, UserInputError
from jsonreshape.reader import Document
from jsonreshape.tasks import CancelToken
from overlay_helpers import KINDS, OverlayBuilder
from saver_helpers import ENCODINGS, apply_pattern, decode_saved, edited, make_doc, oracle_text, source_traits

WS = b" \t\r\n"


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


def raw_of(doc: Document) -> bytes:
    base = doc.pinned(None)
    with base.open_data() as f:
        return f.read()[:doc.data_size]


def part_oracle(doc: Document, a: int, b: int, *, wrapper: bool = True) -> bytes:
    """The UTF-8 text of part ``[a, b)`` of an UNEDITED document, from the rules of the module docstring."""
    raw = raw_of(doc)
    ds, dz = int(doc.info.data_start), int(doc.data_size)
    n = doc.record_count
    spans = [doc.record_span(i) for i in range(n)]
    body = raw[spans[a][0]:spans[b - 1][1]]
    kind = doc.kind
    if kind in ("jsonl", "stream"):
        head = raw[ds:spans[0][0]] if a == 0 else b""
        tail = raw[spans[-1][1]:dz] if b == n else raw[spans[b - 1][1]:spans[b][0]]
        return head + body + tail
    if kind == "wrapped" and not wrapper:
        cs0, cs1 = doc.container_span
        return raw[cs0:spans[0][0]] + body + raw[spans[-1][1]:cs1]
    head, tail = raw[ds:spans[0][0]], raw[spans[-1][1]:dz]
    if kind == "brace_list":
        lead = len(head) - len(head.lstrip(WS))
        assert head[lead:lead + 1] == b"{"
        head = head[:lead] + b"[" + head[lead + 1:]
        trail = len(tail.rstrip(WS))
        assert tail[trail - 1:trail] == b"}"
        tail = tail[:trail - 1] + b"]" + tail[trail:]
    return head + body + tail


def write(doc: Document, path: Path, a: int, b: int, **kw) -> int:
    fd, tmp = fileio.create_temp(path, ".jrpart")
    try:
        size = saver.write_part(doc, fd, path, a, b, **kw)
    except BaseException:
        fileio.remove_quiet(tmp)
        raise
    os.replace(tmp, path)
    return size


def bounds_of(n: int, parts: int) -> list[tuple[int, int]]:
    cuts = sorted({0, n} | {n * k // parts for k in range(1, parts)})
    return [(lo, hi) for lo, hi in zip(cuts, cuts[1:]) if hi > lo]


def reopen(path: Path, doc: Document, *, wrapper: bool = True) -> Document:
    kind = "array" if doc.kind == "brace_list" else doc.kind
    record_path = doc.record_path
    if doc.kind == "wrapped" and not wrapper:
        kind, record_path = ("keyed" if doc.member_records else "array"), None
    d = Document.open(path, format=kind, record_path=record_path)
    d.build_index()
    assert d.error is None, d.error
    return d


# ----------------------------------------------------------------------------------
# kinds x styles x backends
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("style", ["compact", "pretty2", "crlf", "tab"])
@pytest.mark.parametrize("kind", KINDS)
def test_parts_are_exact_and_reopen(tmp_path, docs, backend, kind, style) -> None:
    doc = make_doc(tmp_path / "s", kind, style, n=23, seed=3)
    docs.append(doc)
    doc = doc.pinned()
    n = doc.record_count
    plans = [[(0, n)]] if kind == "single" else [bounds_of(n, 1), bounds_of(n, 3), bounds_of(n, n)]
    for plan in plans:
        texts = []
        records: list[bytes] = []
        for k, (a, b) in enumerate(plan):
            out = tmp_path / f"p{k}.json"
            size = write(doc, out, a, b, first=a == 0, last=b == n)
            data = out.read_bytes()
            assert size == len(data)
            assert data == part_oracle(doc, a, b), (kind, style, a, b)
            texts.append(data)
            part = reopen(out, doc)
            assert part.record_count == b - a
            records += [part.record_bytes(i) for i in range(part.record_count)]
            part.close()
        assert records == [doc.record_bytes(i) for i in range(n)]
        if kind in ("jsonl", "stream"):
            assert b"".join(texts) == raw_of(doc)[doc.info.data_start:]      # cat(parts) == the file


@pytest.mark.parametrize("kind", ["wrapped", "wrapped_obj"])
def test_parts_without_the_wrapper(tmp_path, docs, backend, kind) -> None:
    doc = make_doc(tmp_path / "s", kind, "pretty2", n=10, seed=5)
    docs.append(doc)
    n = doc.record_count
    for a, b in bounds_of(n, 3):
        out = tmp_path / "p.json"
        write(doc, out, a, b, wrapper=False)
        assert out.read_bytes() == part_oracle(doc, a, b, wrapper=False)
        part = reopen(out, doc, wrapper=False)
        assert [part.record_bytes(i) for i in range(part.record_count)] == [doc.record_bytes(i) for i in range(a, b)]
        part.close()


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("kind", ["array", "jsonl", "wrapped", "keyed"])
def test_parts_keep_the_encoding(tmp_path, docs, kind, encoding) -> None:
    doc = make_doc(tmp_path / "s", kind, "pretty2", n=12, seed=7, encoding=encoding)
    docs.append(doc)
    want_enc, want_bom, gz = source_traits(encoding)
    n = doc.record_count
    for k, (a, b) in enumerate(bounds_of(n, 2)):
        out = tmp_path / (f"p{k}.json.gz" if gz else f"p{k}.json")
        write(doc, out, a, b, first=a == 0, last=b == n)
        text, enc, bom, is_gz = decode_saved(out.read_bytes())
        assert (enc, bom, is_gz) == (want_enc if want_enc != "utf-8-bom" else "utf-8-bom", want_bom, gz)
        assert text == part_oracle(doc, a, b)
        part = reopen(out, doc)
        assert [part.record_bytes(i) for i in range(part.record_count)] == [doc.record_bytes(i) for i in range(a, b)]
        part.close()


def test_gzip_follows_the_name_unless_given(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "jsonl", "compact", n=6)
    docs.append(doc)
    write(doc, tmp_path / "a.jsonl.gz", 0, 3)
    write(doc, tmp_path / "b.jsonl", 0, 3)
    write(doc, tmp_path / "c.jsonl", 0, 3, gzip=True)
    assert (tmp_path / "a.jsonl.gz").read_bytes()[:2] == b"\x1f\x8b"
    assert (tmp_path / "b.jsonl").read_bytes()[:2] != b"\x1f\x8b"
    assert (tmp_path / "c.jsonl").read_bytes()[:2] == b"\x1f\x8b"


# ----------------------------------------------------------------------------------
# edited documents
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("pattern", ["point", "structure", "every3", "mixed", "keys", "wrapper", "insert_start",
                                     "delete", "move"])
@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped", "wrapped_obj", "stream", "brace_list"])
def test_parts_of_an_edited_document(tmp_path, docs, kind, pattern) -> None:
    doc, ob = edited(tmp_path, docs, kind, pattern, style="pretty2" if kind != "jsonl" else "compact", n=24)
    pinned = doc.pinned()
    n = pinned.record_count
    assert ob is not None and n == len(ob.records)
    texts = []
    for k, (a, b) in enumerate(bounds_of(n, 4)):
        out = tmp_path / f"e{k}.json"
        write(pinned, out, a, b, first=a == 0, last=b == n)
        texts.append(out.read_bytes())
        part = reopen(out, pinned)
        assert [part.record_bytes(i) for i in range(part.record_count)] == ob.records[a:b], (kind, pattern, a, b)
        if kind == "wrapped" and pattern == "wrapper":
            assert part.root().value()["extra"] == {"added": True}           # the edited wrapper in every part
        part.close()
    if kind == "jsonl":
        assert b"".join(texts) == oracle_text(pinned, ob)                    # == the save of the edited document


def test_an_edit_after_pinning_is_not_seen(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "array", "point")
    pinned = doc.pinned()
    want = [pinned.record_bytes(i) for i in range(pinned.record_count)]
    ob2 = OverlayBuilder(doc, tmp_path / "layer2")
    apply_pattern(ob2, doc, "delete")
    doc.set_overlay(ob2.state())                     # the live document moves on
    write(pinned, tmp_path / "p.json", 0, len(want))
    part = reopen(tmp_path / "p.json", doc)
    assert [part.record_bytes(i) for i in range(part.record_count)] == want
    part.close()


# ----------------------------------------------------------------------------------
# broken lines, refusals, meter, cancellation, validation
# ----------------------------------------------------------------------------------
def test_broken_lines_refused_then_dropped(tmp_path, docs) -> None:
    text = b'{"a": 1}\nbroken line\n{"a": 2}\n\n{"a": 3}\nnot json either\n{"a": 4}\nlast broken'
    doc = make_doc(tmp_path / "s", "jsonl", text=text, open_kw={"format": "jsonl"}, name="b.jsonl")
    docs.append(doc)
    assert doc.broken_count == 3 and doc.record_count == 4
    with pytest.raises(EditError) as ei:
        write(doc, tmp_path / "x.jsonl", 0, 2)
    assert ei.value.key == "saver.error.broken_lines"
    parts = [(0, 2), (2, 4)]
    texts = []
    for k, (a, b) in enumerate(parts):
        out = tmp_path / f"p{k}.jsonl"
        write(doc, out, a, b, first=a == 0, last=b == 4, drop_broken=True)
        texts.append(out.read_bytes())
    assert b"".join(texts) == oracle_text(doc, None, drop_broken=True)      # == the save without them
    assert texts[0] == b'{"a": 1}\n{"a": 2}\n\n'
    assert texts[1] == b'{"a": 3}\n{"a": 4}\n'


def test_refusals(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "array", n=5)
    docs.append(doc)
    for a, b in ((-1, 2), (3, 2), (0, 6)):
        fd, tmp = fileio.create_temp(tmp_path / "x.json", ".jrpart")
        with pytest.raises(ValueError):
            saver.write_part(doc, fd, tmp_path / "x.json", a, b)
        assert fileio.remove_quiet(tmp)               # the descriptor was closed: removable at once
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"a": 1}, {"a": 2}, {"a": ')
    partial = Document.open(bad)
    partial.build_index()
    docs.append(partial)
    assert partial.error is not None
    fd, tmp = fileio.create_temp(tmp_path / "y.json", ".jrpart")
    with pytest.raises(UserInputError) as ei:
        saver.write_part(partial, fd, tmp_path / "y.json", 0, 1)
    assert ei.value.key == "saver.error.partial"
    assert fileio.remove_quiet(tmp)


def test_meter_cancel_and_validation(tmp_path, docs, monkeypatch) -> None:
    doc = make_doc(tmp_path / "s", "array", "pretty2", n=40)
    docs.append(doc)
    seen: list[int] = []
    write(doc, tmp_path / "m.json", 5, 30, meter=seen.append)
    assert sum(seen) == len(part_oracle(doc, 5, 30))
    # cancellation: raised, the file handle is closed (the temp file can be removed)
    token = CancelToken()
    token.cancel()
    fd, tmp = fileio.create_temp(tmp_path / "c.json", ".jrpart")
    with pytest.raises(Cancelled):
        saver.write_part(doc, fd, tmp_path / "c.json", 0, 40, cancel=token)
    assert fileio.remove_quiet(tmp) and not tmp.exists()
    token2 = CancelToken()

    def cancel_midway(n: int) -> None:
        token2.cancel()

    fd, tmp = fileio.create_temp(tmp_path / "c.json", ".jrpart")
    with pytest.raises(Cancelled):
        saver.write_part(doc, fd, tmp_path / "c.json", 0, 40, meter=cancel_midway, cancel=token2)
    assert fileio.remove_quiet(tmp)
    # a part that does not validate (a planner bug) is refused
    real_init = saver._Validator.__init__

    def off_by_one(self, scanner_cls, mode, target, expected, cancel) -> None:  # noqa: ANN001
        real_init(self, scanner_cls, mode, target, expected + 1, cancel)

    monkeypatch.setattr(saver._Validator, "__init__", off_by_one)
    fd, tmp = fileio.create_temp(tmp_path / "v.json", ".jrpart")
    with pytest.raises(ExportError) as ei:
        saver.write_part(doc, fd, tmp_path / "v.json", 0, 10)
    assert ei.value.key == "saver.error.invalid_output"
    assert fileio.remove_quiet(tmp)


def test_source_encoding(tmp_path, docs) -> None:
    for encoding in ("utf-8", "utf-8-bom", "utf-16-le", "utf-16-be-bom"):
        doc = make_doc(tmp_path / encoding, "array", n=3, encoding=encoding)
        docs.append(doc)
        bom, utf16 = saver.source_encoding(doc)
        enc, has_bom, _gz = source_traits(encoding)
        assert utf16 == (enc if enc.startswith("utf-16") else None)
        assert bool(bom) == has_bom
