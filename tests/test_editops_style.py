"""jsonreshape.editops.detect_style (docs/ARCHITECTURE.md §12.4): the formatting style of a
document — for all 7 kinds, compact / 2 spaces / 4 spaces / tab / messy text, LF and CRLF,
UTF-8 BOM, UTF-16 and gzip inputs (the data file is the prepared UTF-8 copy) — read with at
most 64 KiB, cached per document, from base spans only.
"""

from __future__ import annotations

import gzip
import json
import random

import pytest

import jsongen
from jsonreshape import codec, editops
from jsonreshape.editops import DocStyle
from jsonreshape.reader import Document

BOM = b"\xef\xbb\xbf"
RECS = [{"id": i, "name": "Toshkent ўзбек O'zbekiston", "tags": ["a", "b"], "m": {"x": codec.loads("1.10")},
         "n": None} for i in range(3)]
UNITS = {None: None, "  ": 2, "    ": 4, "\t": "\t"}
KINDS = ("array", "jsonl", "brace_list", "wrapped", "keyed", "single", "stream")


def dumps(value, unit) -> bytes:
    return codec.dumps(value, indent=UNITS[unit])


def build(kind: str, unit: str | None) -> bytes:
    """A document laid out like json.dumps(indent=...) (exact numbers)."""
    if kind in ("array", "single"):
        return dumps(RECS, unit)
    if kind == "brace_list":
        text = dumps(RECS, unit)
        return b"{" + text[1:-1] + b"}"
    if kind == "keyed":
        return dumps({f"k{i}": r for i, r in enumerate(RECS)}, unit)
    if kind == "wrapped":
        return dumps({"meta": {"v": 1, "list": [1, 2]}, "data": RECS, "tail": {"ok": True}}, unit)
    if kind == "jsonl":
        return b"".join(codec.dumps(r) + b"\n" for r in RECS)
    if kind == "stream":
        return b"\n".join(dumps(r, unit) for r in RECS) + b"\n"
    raise ValueError(kind)


def encode_file(data: bytes, encoding: str) -> bytes:
    if encoding == "utf-8":
        return data
    if encoding == "utf-8-bom":
        return BOM + data
    if encoding in ("utf-16-le", "utf-16-be"):
        return "﻿".encode(encoding) + data.decode("utf-8").encode(encoding)
    if encoding == "gzip":
        return gzip.compress(data)
    if encoding == "gzip-bom":
        return gzip.compress(BOM + data)
    raise ValueError(encoding)


def open_doc(tmp_path, data: bytes, kind: str, *, encoding: str = "utf-8", name: str = "doc.json",
             index: bool = True) -> Document:
    path = tmp_path / name
    path.write_bytes(encode_file(data, encoding))
    options = {"format": kind}
    if kind == "wrapped":
        options["record_path"] = "data"
    doc = Document.open(path, **options)
    if index:
        doc.build_index()
        assert doc.error is None and doc.kind == kind
    return doc


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for doc in opened:
        doc.close()


def expected(kind: str, unit: str | None, nl: bytes) -> DocStyle:
    level = {"array": 1, "brace_list": 1, "keyed": 1, "wrapped": 2}.get(kind, 0)
    empty = {"array": b"[]", "brace_list": b"[]", "keyed": b"{}", "wrapped": b"[]"}.get(kind, b"")
    if kind == "jsonl":
        return DocStyle(newline=nl, record_sep=nl, record_level=0, unit=None, colon=b":", empty=b"", comma=b",")
    if unit is None:  # compact text: only a stream has newlines (between its records)
        newline = nl if kind == "stream" else b"\n"
        sep = newline if kind in ("stream", "single") else b","
        return DocStyle(newline=newline, record_sep=sep, record_level=level, unit=None, colon=b":", empty=empty,
                        comma=b",")
    sep = nl if kind in ("stream", "single") else b"," + nl + (unit * level).encode()
    return DocStyle(newline=nl, record_sep=sep, record_level=level, unit=unit, colon=b": ", empty=empty,
                    comma=b", ")


@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
@pytest.mark.parametrize("unit", list(UNITS), ids=["compact", "pretty2", "pretty4", "tab"])
@pytest.mark.parametrize("kind", KINDS)
def test_detect_style_every_kind_and_layout(tmp_path, docs, kind, unit, crlf) -> None:
    data = build(kind, unit)
    nl = b"\r\n" if crlf else b"\n"
    if crlf:
        data = data.replace(b"\n", b"\r\n")
    doc = open_doc(tmp_path, data, kind)
    docs.append(doc)
    assert editops.detect_style(doc) == expected(kind, unit, nl)


@pytest.mark.parametrize("encoding", ["utf-8-bom", "utf-16-le", "utf-16-be", "gzip", "gzip-bom"])
@pytest.mark.parametrize("kind, unit, crlf", [("array", "  ", True), ("jsonl", None, True), ("wrapped", "\t", False),
                                              ("keyed", None, False), ("stream", "    ", True)])
def test_detect_style_encodings(tmp_path, docs, encoding, kind, unit, crlf) -> None:
    data = build(kind, unit)
    nl = b"\r\n" if crlf else b"\n"
    if crlf:
        data = data.replace(b"\n", b"\r\n")
    name = "doc.json.gz" if encoding.startswith("gzip") else "doc.json"
    doc = open_doc(tmp_path, data, kind, encoding=encoding, name=name)
    docs.append(doc)
    assert editops.detect_style(doc) == expected(kind, unit, nl)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("style", ["messy", "escaped", "messy_escaped", "compact", "pretty2", "tab"])
def test_detect_style_jsongen_documents_give_usable_styles(tmp_path, docs, kind, style) -> None:
    """Whatever the layout, the detected style produces valid units (JSON Lines: one line)."""
    rng = random.Random(7)
    recs = jsongen.records(4, seed=3)
    path = tmp_path / "gen.json"
    jsongen.write_file(path, recs, kind, style=style, newline="\r\n" if style == "tab" else "\n")
    options = {"format": kind}
    if kind == "wrapped":
        options["record_path"] = "data"
    doc = Document.open(path, **options)
    docs.append(doc)
    doc.build_index()
    st = editops.detect_style(doc)
    assert st.newline in (b"\n", b"\r\n") and st.colon in (b":", b": ") and st.comma in (b",", b", ")
    assert st.unit is None or st.unit == "\t" or (st.unit.strip(" ") == "" and 1 <= len(st.unit) <= 16)
    if kind == "jsonl":
        assert st.unit is None and st.record_sep == st.newline
    for _ in range(5):
        value = codec.dumps(jsongen.rand_value(rng, 0, 3))
        unit = editops.format_record(value, st, key="k" if doc.member_records else None)
        editops.check_unit(unit, member=doc.member_records)
        if kind == "jsonl":
            assert b"\n" not in unit
    if kind in ("array", "brace_list", "keyed", "wrapped"):
        assert st.record_sep.strip(b" \t\r\n") == b","  # a comma plus whitespace
    else:
        assert not st.record_sep.strip(b" \t\r\n")


@pytest.mark.parametrize("data, want", [
    # records give no evidence (scalars / empty containers): the text's own lines decide
    (b"[\n  1,\n  2\n]", DocStyle(b"\n", b",\n  ", 1, "  ", b": ", b"[]", b", ")),
    (b"[\n    {},\n    []\n]", DocStyle(b"\n", b",\n    ", 1, "    ", b": ", b"[]", b", ")),
    (b"[1,2,3]", DocStyle(b"\n", b",", 1, None, b":", b"[]", b",")),
    (b"[]", DocStyle(b"\n", b",", 1, None, b":", b"[]", b",")),
    # one pretty record: the separator follows its line
    (b'[\r\n  {\r\n    "a": 1\r\n  }\r\n]', DocStyle(b"\r\n", b",\r\n  ", 1, "  ", b": ", b"[]", b", ")),
    # one single-line record per line (Python json.dumps per record): compact records
    (b'[\n  {"a": 1, "b": [1, 2]},\n  {"c": 3}\n]', DocStyle(b"\n", b",\n  ", 1, None, b": ", b"[]", b", ")),
    (b'[{"a": 1, "b": [1, 2]}]', DocStyle(b"\n", b",", 1, None, b": ", b"[]", b", ")),
    # records starting on the bracket line ("[{" layouts): level 0
    (b'[{\n  "a": 1\n}, {\n  "b": 2\n}]', DocStyle(b"\n", b", ", 0, "  ", b": ", b"[]", b", ")),
])
def test_detect_style_arrays(tmp_path, docs, data, want) -> None:
    doc = open_doc(tmp_path, data, "array")
    docs.append(doc)
    assert editops.detect_style(doc) == want


def test_detect_style_python_jsonl_and_empty_wrapped(tmp_path, docs) -> None:
    lines = "".join(json.dumps({"id": i, "tags": ["x", "y"], "ism": "Ғулом"}, ensure_ascii=False) + "\n"
                    for i in range(3)).encode()
    doc = open_doc(tmp_path, lines, "jsonl", name="py.jsonl")
    docs.append(doc)
    assert editops.detect_style(doc) == DocStyle(b"\n", b"\n", 0, None, b": ", b"", b", ")
    wrapped = b'{\n  "meta": {\n    "v": 1\n  },\n  "data": []\n}'
    doc = open_doc(tmp_path, wrapped, "wrapped", name="empty.json")
    docs.append(doc)
    assert doc.record_count == 0
    assert editops.detect_style(doc) == DocStyle(b"\n", b",\n    ", 2, "  ", b": ", b"[]", b", ")
    keyed = b'{\n\t"a": {\n\t\t"x": 1\n\t},\n\t"b": {}\n}'
    doc = open_doc(tmp_path, keyed, "keyed", name="keyed.json")
    docs.append(doc)
    assert editops.detect_style(doc) == DocStyle(b"\n", b",\n\t", 1, "\t", b": ", b"{}", b", ")
    obj = b'{"meta": 1, "data": {\n    "a": {\n      "x": 1\n    }\n  }}'
    doc = open_doc(tmp_path, obj, "wrapped", name="wobj.json")
    docs.append(doc)
    assert doc.member_records
    st = editops.detect_style(doc)
    assert (st.unit, st.record_level, st.empty, st.record_sep) == ("  ", 2, b"{}", b",\n    ")


class _Counting:
    """A data handle that counts the bytes read through it."""

    def __init__(self, f, counter: list[int]) -> None:
        self._f = f
        self._counter = counter

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()

    def seek(self, *args):
        return self._f.seek(*args)

    def read(self, n=-1):
        data = self._f.read(n)
        self._counter.append(len(data))
        return data

    def close(self):
        self._f.close()


def counting(doc: Document) -> list[int]:
    reads: list[int] = []
    real = doc.open_data
    doc.open_data = lambda *a, **k: _Counting(real(*a, **k), reads)  # type: ignore[method-assign]
    return reads


def test_detect_style_reads_at_most_64_kib(tmp_path, docs) -> None:
    big = {"meta": {"blob": "x" * 300_000}, "data": RECS * 50, "tail": {"blob": "y" * 300_000}}
    doc = open_doc(tmp_path, codec.dumps(big, indent=2), "wrapped", name="bigprefix.json")
    docs.append(doc)
    reads = counting(doc)
    st = editops.detect_style(doc)
    assert sum(reads) <= editops.STYLE_SAMPLE_BYTES
    assert st == DocStyle(b"\n", b",\n    ", 2, "  ", b": ", b"[]", b", ")
    # record 0 larger than the window: the gap is read on its own
    huge = [{"blob": "z" * 150_000, "x": {"y": [1]}}, {"id": 2}]
    doc = open_doc(tmp_path, codec.dumps(huge, indent=4).replace(b"\n", b"\r\n"), "array", name="bigrec.json")
    docs.append(doc)
    reads = counting(doc)
    st = editops.detect_style(doc)
    assert sum(reads) <= editops.STYLE_SAMPLE_BYTES and len(reads) == 2
    assert st == DocStyle(b"\r\n", b",\r\n    ", 1, "    ", b": ", b"[]", b", ")


def test_detect_style_cache(tmp_path, docs) -> None:
    doc = open_doc(tmp_path, build("array", "  "), "array")
    docs.append(doc)
    first = editops.detect_style(doc)
    reads = counting(doc)
    assert editops.detect_style(doc) is first and reads == []

    class View:  # a pinned view: shares its origin's entry
        origin = doc

    assert editops.detect_style(View()) is first
    pinned = getattr(doc, "pinned", None)
    if callable(pinned):
        assert editops.detect_style(pinned()) is first


def test_detect_style_is_not_cached_before_the_index_is_complete(tmp_path, docs) -> None:
    data = b'[\n  {"a": 1},\n  {"b": 2}\n]'
    doc = open_doc(tmp_path, data, "array", index=False)
    docs.append(doc)
    early = editops.detect_style(doc)  # no records yet: the lines decide
    assert early.unit == "  "
    doc.build_index()
    late = editops.detect_style(doc)
    assert late.unit is None and late.record_sep == b",\n  "
    assert editops.detect_style(doc) is late


class _EditedView:
    """A document whose records 0/1 live in the edit layer (virtual spans)."""

    def __init__(self, doc: Document, base: Document | None) -> None:
        self._doc = doc
        self._base = base

    @property
    def origin(self):  # a live document of its own (its own cache entry)
        return self

    def __getattr__(self, name):
        return getattr(self._doc, name)

    def record_span(self, i):
        if i < 2:
            return (1 << 48) + 10 * i, (1 << 48) + 10 * i + 5
        return self._doc.record_span(i)

    def pinned(self, state):
        assert state is None
        if self._base is None:
            raise AttributeError("no base view")
        return self._base


def test_detect_style_samples_base_spans_only(tmp_path, docs) -> None:
    doc = open_doc(tmp_path, build("array", "    "), "array")
    docs.append(doc)
    want = editops.detect_style(doc)
    assert editops.detect_style(_EditedView(doc, doc)) == want
    # without a base view the edited spans are skipped; the text's lines still decide
    fallback = editops.detect_style(_EditedView(doc, None))
    assert fallback.unit == "    " and fallback.newline == b"\n" and fallback.empty == b"[]"
