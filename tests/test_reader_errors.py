"""AGENT_PROMPT §14 test 13 and §3 "JSON xato bo'lsa": the error position is exact
(e.g. line 2, column 5; columns count CHARACTERS), the records before an error stay
usable (a truncated file opens up to its last complete record), JSON Lines broken lines
are skipped and listed, and a cached index reproduces the error.
"""

from __future__ import annotations

import gzip
import json
import random
from pathlib import Path

import pytest

import jsongen
from jsonreshape import codec, reader
from jsonreshape.errors import FileFormatError, JsonSyntaxError
from jsonreshape.i18n import set_language
from jsonreshape.reader import BrokenLine, Document


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


BOM_CHAR = chr(0xFEFF)


def open_built(path: Path, **kw) -> Document:
    doc = Document.open(path, **kw)
    doc.build_index()
    return doc


def expected_position(text: str, char_pos: int) -> tuple[int, int]:
    """1-based (line, column) of character ``char_pos`` in ``text`` (columns in characters)."""
    line = text.count("\n", 0, char_pos) + 1
    col = char_pos - (text.rfind("\n", 0, char_pos) + 1) + 1
    return line, col


# ----------------------------------------------------------------------------------
# exact error positions
# ----------------------------------------------------------------------------------
def test_error_at_line_2_column_5(tmp_path: Path, backend: str, lang_en) -> None:
    p = tmp_path / "e.json"
    p.write_bytes(b'[1,\n2,  x, 3]')
    with open_built(p) as doc:
        err = doc.error
        assert isinstance(err, JsonSyntaxError) and doc.partial and doc.index_complete
        assert (err.line, err.column) == (2, 5)
        assert err.code == "unexpected_char" and err.expected == "value"
        assert err.offset == 8
        assert err.context_before == "[1,\n2,  " and err.context_after == "x, 3]"
        assert err.source == str(p)
        assert doc.record_count == 2 and [doc.record(0), doc.record(1)] == [1, 2]
        assert "line 2, column 5" in err.message and "byte 8" in err.message
        assert err.context().splitlines() == ["[1, 2,  x, 3]", " " * 8 + "^"]
        set_language("uz")
        assert "2-qator, 5-ustun" in err.message


CASES = [
    # (document text, marker of the error character)
    ('[\n"тош", x]', "x"),                               # Cyrillic: 2-byte chars
    ('[\n  "\U0001f600\U0001f600", é]', "é"),                 # emoji (4 bytes) then a bad char
    ('{"a":\r\n\t"чай",\r\n\t"b": tru}', "}"),         # CRLF + tabs, invalid literal
    ('[1,2,\n\n\n   -]', "]"),                                          # invalid number at ']'
    ('{"kéy": "välue", "x": [1, 2,, 3]}', ",,"),               # one line, non-ASCII key
    ('[\n "a\\qb"]', "q"),                                               # invalid escape
    ('[\n "\\u12G4"]', "G"),                                             # invalid hex
    ('{"a": 1} {"b": 2}', "{\"b"),                                       # trailing data
]


@pytest.mark.parametrize("text, marker", CASES)
def test_character_columns(tmp_path: Path, backend: str, text: str, marker: str) -> None:
    p = tmp_path / "c.json"
    data = text.encode("utf-8")
    p.write_bytes(data)
    char_pos = text.index(marker) + (1 if marker == ",," else 0)
    with open_built(p, format="single" if text.startswith("{") else None) as doc:
        err = doc.error
        assert err is not None
        assert err.offset == len(text[:char_pos].encode("utf-8"))
        assert (err.line, err.column) == expected_position(text, char_pos)
        assert err.context_after.startswith(text[char_pos:char_pos + 3])
        assert text[:char_pos].endswith(err.context_before)


def test_long_line_columns_across_chunks(tmp_path: Path, backend: str) -> None:
    text = '["' + "éж" * 300_000 + '", x]'
    p = tmp_path / "long.json"
    p.write_bytes(text.encode("utf-8"))
    with open_built(p) as doc:
        err = doc.error
        assert err is not None and err.code == "unexpected_char"
        assert (err.line, err.column) == (1, text.index("x") + 1)
        assert err.offset == len(text[:text.index("x")].encode("utf-8"))
        assert len(err.context_before) <= 60 and err.context_before.endswith('", ')


def test_error_offsets_after_utf8_bom(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "bom.json"
    p.write_bytes(b"\xef\xbb\xbf[1,\n x]")
    with open_built(p) as doc:
        err = doc.error
        assert doc.info.data_start == 3
        assert err is not None and err.offset == 3 + 5 and (err.line, err.column) == (2, 2)
        assert err.context_before == "[1,\n " and doc.record(0) == 1


def test_invalid_utf8_inside_string(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "u.json"
    p.write_bytes(b'["ok", "ab\xffcd"]')
    with open_built(p) as doc:
        err = doc.error
        assert err is not None and err.code == "invalid_utf8" and err.expected == "string_end"
        assert err.offset == 10 and (err.line, err.column) == (1, 11)
        assert "�" in err.context_after
        assert doc.record_count == 1 and doc.record(0) == "ok"


def test_trailing_data_keeps_all_records(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "t.json"
    p.write_bytes(b'[{"a": 1}, {"b": 2}]\n  x')
    with open_built(p) as doc:
        assert doc.error is not None and doc.error.code == "trailing_data" and doc.error.expected == "end"
        assert (doc.error.line, doc.error.column) == (2, 3)
        assert doc.record_count == 2 and doc.record(1) == {"b": 2}


def test_depth_limit(tmp_path: Path, backend: str) -> None:
    from jsonreshape.core import DEFAULT_MAX_DEPTH

    p = tmp_path / "deep.json"
    p.write_bytes(b"[" * (DEFAULT_MAX_DEPTH + 5) + b"]" * (DEFAULT_MAX_DEPTH + 5))
    with open_built(p) as doc:
        assert doc.error is not None and doc.error.code == "depth_limit"
        assert doc.error.offset == DEFAULT_MAX_DEPTH and doc.record_count == 0


# ----------------------------------------------------------------------------------
# truncated files: the records before the cut
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, kw", [("array", {}), ("array", {"style": "pretty2"}), ("wrapped", {}),
                                     ("keyed", {}), ("brace_list", {}), ("stream", {"style": "pretty2"})])
def test_truncated_file_opens_complete_records(tmp_path: Path, backend: str, fmt: str, kw: dict) -> None:
    recs = jsongen.records(30, seed=4)
    full = tmp_path / "full.json"
    data = jsongen.write_file(full, recs, fmt, **kw)
    with open_built(full) as doc:
        assert doc.error is None
        kind = doc.info.kind
        ends = [doc.record_span(i)[1] for i in range(doc.record_count)]
        first_start = doc.record_span(0)[0]
    rng = random.Random(5)
    cuts = sorted({first_start + 1, *ends[::3], *(e + 1 for e in ends[1:-1:3]),
                   *(rng.randrange(first_start, len(data) - 1) for _ in range(12))})
    for cut in cuts:
        if cut >= len(data) - 1:
            continue
        p = tmp_path / f"cut{cut}.json"
        p.write_bytes(data[:cut])
        with open_built(p, format=kind, record_path=None) as doc:
            want = sum(1 for e in ends if e <= cut)
            assert doc.record_count == want, cut
            for i in range(want):
                assert codec.dumps(doc.record(i)) == codec.dumps(recs[i])
            if kind == "stream" and data[:cut].rstrip().endswith((b"}", b"]")):
                continue  # a stream may legitimately end after a complete value
            assert doc.error is not None and doc.partial, cut
            assert doc.error.code == "unexpected_eof" and doc.error.offset == cut
            assert doc.error.context_after == ""


@pytest.mark.parametrize("text, kw, records", [
    (b"[1, 2, 12", {}, [1, 2]),                       # "12" may be the cut "123": not kept
    (b"[1, 2, 12 ", {}, [1, 2, 12]),                  # whitespace proves the number ended
    (b"[1, 2, -0.5e1", {}, [1, 2]),
    (b'[1, "ab", true', {}, [1, "ab", True]),         # literals/strings cannot be cut and stay valid
    (b'{"a": 1, "b": 12', {"format": "keyed"}, [1]),  # keyed (member value)
    (b'{"m": 0, "data": [7, 8', {"record_path": "data"}, [7]),
    (b"{1, 2, 3", {"format": "brace_list"}, [1, 2]),
])
def test_truncated_number_record_is_not_kept(tmp_path: Path, backend: str, text: bytes, kw: dict,
                                             records: list) -> None:
    p = tmp_path / "n.json"
    p.write_bytes(text)
    with open_built(p, **kw) as doc:
        assert doc.error is not None and doc.error.code == "unexpected_eof" and doc.error.offset == len(text)
        assert [doc.record(i) for i in range(doc.record_count)] == records


def test_complete_number_documents_keep_their_last_number(tmp_path: Path, backend: str) -> None:
    for text, kw in ((b"123", {}), (b"1\n2\n34", {}), (b"1 2 34", {"format": "stream"})):
        p = tmp_path / "ok.json"
        p.write_bytes(text)
        with open_built(p, **kw) as doc:
            assert doc.error is None and doc.record(doc.record_count - 1) == int(text.split()[-1])


def test_truncated_gzip_keeps_records_before_the_cut(tmp_path: Path, backend: str) -> None:
    recs = jsongen.records(300, seed=8)
    raw = gzip.compress(jsongen.build_document(recs, "array"))
    p = tmp_path / "cut.json.gz"
    p.write_bytes(raw[: len(raw) * 2 // 3])
    with open_built(p) as doc:
        assert doc.info.compression == "gzip"
        assert "fileio.warning.gzip_truncated" in doc.info.warnings
        assert doc.error is not None and doc.error.code == "unexpected_eof"
        n = doc.record_count
        assert 10 < n < 300
        for i in range(n):
            assert codec.dumps(doc.record(i)) == codec.dumps(recs[i])


# ----------------------------------------------------------------------------------
# errors in wrappers, record paths
# ----------------------------------------------------------------------------------
def test_error_in_wrapper_prefix(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "w.json"
    text = '{"meta": {"a": [1, 2,]},\n "data": [{"r": 1}, {"r": 2}]}'
    p.write_bytes(text.encode("utf-8"))
    with open_built(p, record_path="data") as doc:
        err = doc.error
        assert err is not None and err.code == "unexpected_char" and err.expected == "value"
        assert (err.line, err.column) == expected_position(text, text.index("]}"))
        assert doc.record_count == 0
        assert doc.wrapper_spans() is None


def test_error_in_wrapper_suffix_keeps_all_records(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "w.json"
    text = '{"meta": {}, "data": [{"r": 1}, {"r": 2}],\n "tail": nul}'
    p.write_bytes(text.encode("utf-8"))
    with open_built(p) as doc:
        assert doc.info.kind == "wrapped"
        err = doc.error
        assert err is not None and err.code == "invalid_literal" and (err.line, err.column) == (2, 13)
        assert doc.record_count == 2 and doc.record(1) == {"r": 2}
        assert doc.container_span is not None  # the record array itself closed


def test_record_path_not_found_or_not_a_container(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "w.json"
    text = '{"meta": {"x": 1}, "data": [1, 2]}'
    p.write_bytes(text.encode("utf-8"))
    with open_built(p, record_path="nope") as doc:
        assert doc.error is not None and doc.error.code == "target_not_found"
        assert doc.error.offset == len(text) and doc.record_count == 0
    with open_built(p, record_path="meta.x") as doc:
        assert doc.error is not None and doc.error.code == "target_not_container"
        assert doc.error.offset == text.index("1}")


# ----------------------------------------------------------------------------------
# JSON Lines broken lines
# ----------------------------------------------------------------------------------
JSONL_LINES = [
    ('{"a":1}', None),
    ('{"b":2', ("unexpected_eof", "comma_or_brace", 7)),
    ("", None),
    ("[1, 2] x", ("trailing_data", "end", 8)),
    ('{"c": tru}', ("invalid_literal", "literal", 10)),
    ('"a\x01b"', ("control_char", "string_end", 3)),
    ('{"d": 4}\r', None),
    ('{"e": "т\\q"}', ("invalid_escape", "escape", 10)),
    ("   ", None),
    ('{"f": [1, 2, 3]}', None),
    ("01", ("trailing_data", "end", 2)),
]


def test_jsonl_broken_lines_are_skipped_and_listed(tmp_path: Path, backend: str) -> None:
    text = "\n".join(line for line, _ in JSONL_LINES) + "\n"
    p = tmp_path / "b.jsonl"
    p.write_bytes(text.encode("utf-8"))
    with open_built(p) as doc:
        assert doc.info.kind == "jsonl" and doc.error is None and not doc.partial
        assert [doc.record(i) for i in range(doc.record_count)] == [{"a": 1}, {"d": 4}, {"f": [1, 2, 3]}]
        expected = [(n + 1, info) for n, (_, info) in enumerate(JSONL_LINES) if info is not None]
        assert doc.broken_count == len(expected) == len(doc.broken_lines)
        starts = []
        pos = 0
        for line, _ in JSONL_LINES:
            starts.append(pos)
            pos += len(line.encode("utf-8")) + 1
        for bl, (line_no, (code, exp, column)) in zip(doc.broken_lines, expected):
            src = JSONL_LINES[line_no - 1][0]
            assert isinstance(bl, BrokenLine)
            assert (bl.line, bl.code, bl.expected, bl.column) == (line_no, code, exp, column)
            assert bl.start == starts[line_no - 1] and bl.end == bl.start + len(src.encode("utf-8"))
            assert bl.offset == bl.start + len(src[:column - 1].encode("utf-8"))
            assert bl.preview == src.rstrip("\r")
            assert str(line_no) in bl.message
        assert list(doc.iter_broken_lines()) == doc.broken_lines


def test_jsonl_broken_line_preview_is_truncated(tmp_path: Path, backend: str) -> None:
    long_line = '{"x": "' + "ж" * 500 + '"'  # unterminated
    p = tmp_path / "long.jsonl"
    p.write_bytes(('{"a": 1}\n' + long_line + "\n" + '{"a": 2}\n').encode("utf-8"))
    with open_built(p) as doc:
        assert doc.record_count == 2 and doc.broken_count == 1
        bl = doc.broken_lines[0]
        assert len(bl.preview) == 120 and bl.preview.endswith("…") and bl.preview.startswith('{"x": "')
        assert bl.column == len(long_line) + 1


@pytest.mark.slow
def test_many_broken_lines_are_capped_in_memory(tmp_path: Path) -> None:
    n_broken = reader.BROKEN_KEEP + 50
    lines = []
    for i in range(n_broken):
        lines.append('{"ok": %d}' % i if i % 2 else "x%d" % i)
    for i in range(n_broken):
        lines.append("bad%d" % i)
    p = tmp_path / "many.jsonl"
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    total_broken = n_broken // 2 + n_broken
    with open_built(p) as doc:
        assert doc.broken_count == total_broken
        assert len(doc.broken_lines) == reader.BROKEN_KEEP
        all_broken = list(doc.iter_broken_lines())
        assert len(all_broken) == total_broken
        assert all_broken[-1].line == len(lines) and all_broken[-1].preview == "bad%d" % (n_broken - 1)
        assert doc.record_count == n_broken // 2
    with Document.open(p) as doc:  # cached
        assert doc.index_from_cache and doc.broken_count == total_broken
        assert len(doc.broken_lines) == reader.BROKEN_KEEP
        assert [b.line for b in doc.iter_broken_lines()] == [b.line for b in all_broken]


# ----------------------------------------------------------------------------------
# empty files, compressed and UTF-16 inputs
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("content", [b"", b"   \r\n\t ", b"\xef\xbb\xbf", b"\xef\xbb\xbf  \n",
                                     BOM_CHAR.encode("utf-16-le"), " \n".encode("utf-16-be"),
                                     gzip.compress(b""), gzip.compress(b"\n\n")])
def test_empty_file(tmp_path: Path, content: bytes, lang_en) -> None:
    p = tmp_path / "empty.json"
    p.write_bytes(content)
    with pytest.raises(FileFormatError) as ei:
        Document.open(p)
    assert ei.value.key == "reader.empty_file"
    assert "empty" in str(ei.value) and str(p) in str(ei.value)


def test_errors_inside_gzip_input(tmp_path: Path, backend: str) -> None:
    text = '[{"a": 1},\n {"б": 2}, oops]'
    p = tmp_path / "e.json.gz"
    p.write_bytes(gzip.compress(text.encode("utf-8")))
    with open_built(p) as doc:
        err = doc.error
        assert err is not None and err.code == "unexpected_char"
        assert (err.line, err.column) == expected_position(text, text.index("oops"))
        assert err.context_after.startswith("oops")
        assert doc.record_count == 2 and doc.record(1) == {"б": 2}


@pytest.mark.parametrize("encoding, bom", [("utf-16-le", True), ("utf-16-be", True), ("utf-16-le", False),
                                           ("utf-16-be", False)])
def test_errors_inside_utf16_input(tmp_path: Path, backend: str, encoding: str, bom: bool) -> None:
    text = '[\n  "тошкент \U0001f600",\n  {"k": tru}\n]'
    raw = text.encode(encoding)
    if bom:
        raw = BOM_CHAR.encode(encoding) + raw
    p = tmp_path / "e16.json"
    p.write_bytes(raw)
    with open_built(p) as doc:
        assert doc.info.encoding == encoding
        err = doc.error
        assert err is not None and err.code == "invalid_literal"
        # line/column are in characters, the same in UTF-16 and in the UTF-8 data file
        assert (err.line, err.column) == expected_position(text, text.index("}\n]"))
        assert err.context_before.endswith('{"k": tru')
        assert doc.record_count == 1 and doc.record(0) == "тошкент \U0001f600"


# ----------------------------------------------------------------------------------
# cached index reproduces the error
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind_text", [
    ('[{"a": 1}, {"b": 2},\n {"c": x}]', None),
    ('{"meta": 1, "data": [1, 2, 3], "tail": [}', "data"),
    ('{"a": 1}\n{"b": \n{"c": 3}\n', None),
])
def test_cached_index_reproduces_the_error(tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch,
                                           kind_text) -> None:
    text, record_path = kind_text
    p = tmp_path / "c.json"
    p.write_bytes(text.encode("utf-8"))
    with open_built(p, record_path=record_path) as doc:
        err1 = doc.error.to_dict() if doc.error else None
        broken1 = doc.broken_lines
        recs1 = [doc.record(i) for i in range(doc.record_count)]
        assert err1 is not None or broken1

    monkeypatch.setattr(Document, "_build", lambda *a, **k: pytest.fail("index rebuilt"))
    with Document.open(p, record_path=record_path) as doc:
        assert doc.index_complete and doc.index_from_cache
        assert (doc.error.to_dict() if doc.error else None) == err1
        assert doc.partial == (err1 is not None)
        assert doc.broken_lines == broken1
        assert [doc.record(i) for i in range(doc.record_count)] == recs1


def test_error_dict_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "e.json"
    p.write_bytes(b'{"a": [1, 2}')
    with open_built(p) as doc:
        err = doc.error
        assert err is not None
        again = JsonSyntaxError.from_dict(json.loads(json.dumps(err.to_dict())))
        assert again.to_dict() == err.to_dict() and str(again) == str(err)
