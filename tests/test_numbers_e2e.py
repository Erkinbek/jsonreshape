"""§14 test 24 end to end: number precision and Unicode through every export (AGENT_PROMPT §12
"Raqamlar aniqligi yo'qolmasin ... Natija satrga aylanib qolmasin", §10 "Unicode \\u ga
aylantirilmaydi").

Records hold the §12 test values ``0.1``, ``1e400``, ``12345678901234567890``, ``-0.0``,
``1.10`` and every ``tests/jsongen.EDGE_NUMBER_TOKENS`` token, written into the source as raw
tokens, plus Uzbek Latin/Cyrillic text with apostrophe variants and emoji. Every export
(JSON compact / 2 / 4 / tab, sorted keys, JSONL, CSV, XLSX, SQLite) must keep each token
byte-identical and never turn it into a string; text stays raw UTF-8. XLSX and SQLite store
numbers as numbers whenever the cell type holds the exact value (SQLite: INTEGER in the
64-bit range, REAL when a double has the token's decimal value -- ``1.10``, ``1E5``,
``-0.0``) and the exact token text only otherwise, with a counted warning. Filter and sort
results (``jsonreshape.recordset``) exported the same way keep their records and order.
"""

from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.writer import ExportOptions, export

PROMPT_VALUES = ["0.1", "1e400", "12345678901234567890", "-0.0", "1.10"]
TOKENS = list(dict.fromkeys(PROMPT_VALUES + jsongen.EDGE_NUMBER_TOKENS))
TEXTS = ["Oʻzbekiston", "Ўзбекистон", "Тошкент", "Gʻulom va G'ulom", "O‘zbekiston O’zbekiston", "Chorsu — Чорсу",
         "emoji 😀 test 🇺🇿", "Shahrisabz / Шаҳрисабз", "qoʻngʻiroq"]

_LEX = re.compile(rb'"(?:[^"\\]|\\.)*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null'
                  rb'|[{}\[\],:]|[ \t\r\n]+')


def number_tokens(data: bytes) -> list[bytes]:
    """Number tokens outside strings, in document order (``data`` must be valid JSON)."""
    out = []
    pos = 0
    for m in _LEX.finditer(data):
        assert m.start() == pos, data[pos:pos + 40]
        pos = m.end()
        tok = m.group()
        if tok[:1] in b"-0123456789":
            out.append(tok)
    assert pos == len(data)
    return out


def record_text(k: int, tok: str, text: str, style: str) -> str:
    sp = " " if style == "pretty" else ""
    nl = "\n  " if style == "pretty" else ""
    return (f'{{{nl}"i":{sp}{k},{nl}"n":{sp}{tok},{nl}"arr":{sp}[{tok},{sp}{tok},{sp}"{text}"],'
            f'{nl}"obj":{sp}{{"x":{sp}{tok},{sp}"t":{sp}"{text}"}},{nl}"t":{sp}"{text}"\n}}')


def source_bytes(style: str = "compact") -> bytes:
    recs = [record_text(k, tok, TEXTS[k % len(TEXTS)], style) for k, tok in enumerate(TOKENS)]
    return ("[\n" + ",\n".join(recs) + "\n]\n").encode("utf-8")


@pytest.fixture
def doc(tmp_path: Path):
    p = tmp_path / "numbers.json"
    p.write_bytes(source_bytes("pretty"))
    d = Document.open(p)
    d.build_index()
    assert d.record_count == len(TOKENS)
    yield d
    d.close()


def expected_tokens() -> list[bytes]:
    out = []
    for k, tok in enumerate(TOKENS):
        out += [str(k).encode(), tok.encode(), tok.encode(), tok.encode(), tok.encode()]
    return out


def check_text(data: bytes) -> None:
    assert b"\\u" not in data
    for s in TEXTS:
        assert s.encode("utf-8") in data


def check_not_quoted(data: bytes) -> None:
    for tok in TOKENS:
        assert b'"' + tok.encode() + b'"' not in data, tok


def test_source_tokens_are_what_we_think(doc: Document) -> None:
    assert number_tokens(doc.source_path.read_bytes()) == expected_tokens()
    values = [doc.record(i)["n"] for i in range(doc.record_count)]
    assert [codec.number_text(v) for v in values] == TOKENS
    assert any(isinstance(v, JsonFloat) for v in values) and any(isinstance(v, JsonInt) for v in values)


@pytest.mark.parametrize("indent", [None, 2, 4, "\t"])
def test_json_keeps_every_token(doc: Document, tmp_path: Path, indent: Any) -> None:
    out = tmp_path / "out.json"
    export(doc, out, ExportOptions("json", indent=indent))
    data = out.read_bytes()
    assert number_tokens(data) == expected_tokens()
    assert data == codec.dumps(codec.loads(doc.source_path.read_bytes()), indent=indent)
    check_text(data)
    check_not_quoted(data)


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_sorted_keys_keep_every_token(doc: Document, tmp_path: Path, fmt: str) -> None:
    out = tmp_path / f"out.{fmt}"
    export(doc, out, ExportOptions(fmt, indent=4, sort_keys=True))
    data = out.read_bytes()
    want = []
    for k, tok in enumerate(TOKENS):  # keys sorted: arr, i, n, obj, t
        want += [tok.encode(), tok.encode(), str(k).encode(), tok.encode(), tok.encode()]
    lines = data.split(b"\n")[:-1] if fmt == "jsonl" else [data]
    assert [t for line in lines for t in number_tokens(line)] == want
    check_text(data)
    check_not_quoted(data)


def test_jsonl_keeps_every_token(doc: Document, tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    export(doc, out, ExportOptions("jsonl"))
    lines = out.read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == len(TOKENS) + 1
    for k, (line, tok) in enumerate(zip(lines, TOKENS)):
        assert number_tokens(line) == [str(k).encode()] + [tok.encode()] * 4
        assert line == codec.dumps(doc.record(k))
    check_text(out.read_bytes())
    # and back: JSONL -> JSON gives the same tokens again
    back = Document.open(out)
    try:
        back.build_index()
        export(back, tmp_path / "back.json", ExportOptions("json", indent=2))
        assert number_tokens((tmp_path / "back.json").read_bytes()) == expected_tokens()
    finally:
        back.close()


def test_huge_records_keep_every_token(tmp_path: Path) -> None:
    p = tmp_path / "huge.json"
    p.write_bytes(source_bytes("pretty"))
    with Document.open(p, settings=Settings(large_node_bytes=120)) as d:
        d.build_index()
        assert any(d.is_huge(i) for i in range(d.record_count))
        for fmt, indent in (("json", None), ("json", "\t"), ("jsonl", None)):
            out = tmp_path / f"h.{fmt}"
            export(d, out, ExportOptions(fmt, indent=indent))
            data = out.read_bytes()
            lines = data.split(b"\n")[:-1] if fmt == "jsonl" else [data]
            assert [t for line in lines for t in number_tokens(line)] == expected_tokens()
            check_text(data)


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
def test_csv_keeps_exact_number_text(doc: Document, tmp_path: Path, delimiter: str) -> None:
    out = tmp_path / "out.csv"
    sep = "|" if delimiter == ";" else ";"   # a joined cell holding the delimiter would be quoted
    rep = export(doc, out, ExportOptions("csv", csv_delimiter=delimiter, join_sep=sep))
    assert rep.columns == ["i", "n", "arr", "obj.x", "obj.t", "t"]
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw[3:].decode("utf-8")
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    lines = text.split("\r\n")
    for k, tok in enumerate(TOKENS):
        s = TEXTS[k % len(TEXTS)]
        assert rows[k + 1] == [str(k), tok, f"{tok}{sep}{tok}{sep}{s}", tok, s, s]
        # exact text, never quoted
        assert lines[k + 1].startswith(f"{k}{delimiter}{tok}{delimiter}{tok}{sep}{tok}{sep}")
    check_text(raw)


def excel_exact(tok: str) -> bool:
    """Excel keeps the value: at most 15 significant digits, exponent within +-307."""
    d = Decimal(tok)
    if d.is_zero():
        return True
    digits = list(d.as_tuple().digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
    return len(digits) <= 15 and -307 <= d.adjusted() <= 307


def test_xlsx_numbers_are_numbers_when_exact(doc: Document, tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    out = tmp_path / "out.xlsx"
    rep = export(doc, out, ExportOptions("xlsx", columns=["i", "n", "t"]))
    rows = list(openpyxl.load_workbook(out).active.iter_rows(values_only=True))
    n_text = 0
    for k, tok in enumerate(TOKENS):
        i, n, t = rows[k + 1]
        assert i == k and t == TEXTS[k % len(TEXTS)]
        if excel_exact(tok):
            assert isinstance(n, (int, float)) and not isinstance(n, bool), tok   # a real number cell
            assert float(n) == float(tok), tok
        else:
            n_text += 1
            assert n == tok, tok   # the exact text instead of a rounded number
    assert {"0.1", "1.10", "-0.0"} <= {tok for tok in TOKENS if excel_exact(tok)}
    assert {"1e400", "12345678901234567890"} <= {tok for tok in TOKENS if not excel_exact(tok)}
    assert rep.warning_keys == ["writer.warn.xlsx_number_text"]
    assert rep.warnings[0].split()[0] == str(n_text)


def sqlite_expected(tok: str) -> tuple[Any, str]:
    """``(value, typeof)`` the SQLite export must store for a number token: INTEGER in the
    64-bit range (``-0`` is 0), REAL whenever a double holds the token's exact decimal
    value (``1.10``, ``1E5``, ``-0.0``, ``3.141592653589793``), the exact TEXT only for the
    rest (``1e400``, ``12345678901234567890``, ``123456789.123456789``)."""
    v = codec.loads(tok)
    if isinstance(v, int):
        if -2**63 <= v < 2**63:
            return int(v), "integer"
        return tok, "text"
    f = float(v)
    if math.isfinite(f) and Decimal(tok) == Decimal(repr(f)):
        return f, "real"
    return tok, "text"


def test_sqlite_storage_classes(doc: Document, tmp_path: Path, lang_uz) -> None:
    """Numbers stay numbers (§12 "Natija satrga aylanib qolmasin"): text only where no
    SQLite number holds the exact value, and then with a counted warning."""
    out = tmp_path / "out.sqlite"
    rep = export(doc, out, ExportOptions("sqlite"))
    con = sqlite3.connect(out)
    try:
        got = con.execute('select _id, n, typeof(n), "obj.x", t from records order by _id').fetchall()
        arr = con.execute("select _parent, _idx, value, typeof(value) from arr order by _id").fetchall()
    finally:
        con.close()
    n_text = 0
    for k, tok in enumerate(TOKENS):
        _id, n, kind, objx, t = got[k]
        assert _id == k and t == TEXTS[k % len(TEXTS)]
        want_value, want_kind = sqlite_expected(tok)
        assert kind == want_kind and n == want_value, tok
        n_text += kind == "text"
        assert objx == n
        assert [a[:2] for a in arr[3 * k:3 * k + 3]] == [(k, 0), (k, 1), (k, 2)]
        assert arr[3 * k][2:] == (n, kind) and arr[3 * k + 2][2:] == (TEXTS[k % len(TEXTS)], "text")
    # the §12 values: 1.10 and -0.0 are numbers, 1e400 and 12345678901234567890 exact text
    assert {sqlite_expected(tok)[1] for tok in ("0.1", "1.10", "-0.0", "1E5", "2.5E+3")} == {"real"}
    assert {sqlite_expected(tok)[1] for tok in ("1e400", "12345678901234567890", "1e-400")} == {"text"}
    # every text number is counted (n, obj.x and both array elements of a record)
    assert rep.warning_keys == ["writer.warn.sqlite_number_text"]
    assert rep.warnings[0].split()[0] == str(4 * n_text)


# ----------------------------------------------------------------------------------
# filter / sort results exported (jsonreshape.recordset is written in parallel)
# ----------------------------------------------------------------------------------
def _recordset_api():
    recordset = pytest.importorskip("jsonreshape.recordset")
    flt = getattr(recordset, "filter_records", None)
    srt = getattr(recordset, "sort_records", None)
    if flt is None or srt is None:
        pytest.skip("jsonreshape.recordset has no filter_records/sort_records yet")
    return flt, srt


def _export_tokens(doc: Document, out: Path, source: Any) -> list[bytes]:
    export(doc, out, ExportOptions("json", indent=2), source=source)
    return number_tokens(out.read_bytes())


def test_filter_round_trip(doc: Document, tmp_path: Path) -> None:
    filter_records, _ = _recordset_api()
    from jsonreshape.query import compile_filter

    flt = compile_filter("n > 0.5 or n == -0.0")
    rs = filter_records(doc, flt)
    try:
        idx = [rs[k] for k in range(len(rs))]
        want = [i for i in range(doc.record_count) if flt.matches(doc.record(i), index=i)]
        assert idx == want and 0 < len(idx) < doc.record_count
        tokens = _export_tokens(doc, tmp_path / "f.json", rs)
        assert tokens == [t for i in idx for t in (str(i).encode(),) + (TOKENS[i].encode(),) * 4]
        export(doc, tmp_path / "f.csv", ExportOptions("csv", columns=["i", "n"]), source=rs)
        rows = list(csv.reader(io.StringIO((tmp_path / "f.csv").read_bytes()[3:].decode("utf-8"), newline="")))
        assert rows[1:] == [[str(i), TOKENS[i]] for i in idx]
    finally:
        close = getattr(rs, "close", None)
        if close is not None:
            close()


def test_sort_round_trip(doc: Document, tmp_path: Path) -> None:
    _, sort_records = _recordset_api()
    from jsonreshape.sortkeys import SortKey

    rs = sort_records(doc, [SortKey("n", descending=True)])
    try:
        idx = [rs[k] for k in range(len(rs))]
        assert sorted(idx) == list(range(doc.record_count))
        values = [Decimal(TOKENS[i]) for i in idx]
        assert values == sorted(values, reverse=True)   # exact decimal order, never as strings
        tokens = _export_tokens(doc, tmp_path / "s.json", rs)
        assert tokens == [t for i in idx for t in (str(i).encode(),) + (TOKENS[i].encode(),) * 4]
        export(doc, tmp_path / "s.jsonl", ExportOptions("jsonl"), source=rs)
        lines = (tmp_path / "s.jsonl").read_bytes().split(b"\n")[:-1]
        assert lines == [codec.dumps(doc.record(i)) for i in idx]
    finally:
        close = getattr(rs, "close", None)
        if close is not None:
            close()
