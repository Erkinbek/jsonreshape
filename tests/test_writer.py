"""jsonreshape.writer: streaming exporters (docs/ARCHITECTURE.md §8; AGENT_PROMPT §10
"Konvertatsiya", §14 test 22).

* JSON: compact / indent 2 / 4 / tab output equals ``codec.dumps`` of the parsed document;
  JSON -> JSONL -> JSON round trips leave every record identical; wrapped documents keep or
  drop the wrapper; keyed documents stay objects; sorted keys (alphabetical and code point);
  selections keep their order; huge records are streamed.
* CSV: automatic and explicit columns (nested ``a.b.c``, arrays joined or expanded into
  rows, wildcards, ``$key``/``$index``), BOM, delimiter, quoting; values compared with a
  small reference implementation.
* XLSX: values and types read back with openpyxl; split into sheets and into files.
* SQLite: tables, columns, parent-child relations and storage classes queried with sqlite3
  (numbers a double holds exactly are REAL, only the others TEXT).
* Atomic output: cancellation and errors leave an existing target untouched and no
  temporary files.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from array import array
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec, writer
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.paths import MISSING, get, parse_path, parse_pattern
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.sortkeys import collation_key
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, ExportReport, discover_columns, export, format_for_path

openpyxl = pytest.importorskip("openpyxl")

BOM = b"\xef\xbb\xbf"


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def open_doc(tmp_path: Path):
    """``open_doc(data=bytes | recs=list, fmt=..., settings=..., **write_file kwargs)``."""
    docs: list[Document] = []
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    def make(data: bytes | None = None, *, recs: list | None = None, fmt: str = "array", name: str | None = None,
             settings: Settings | None = None, record_path: Any = None, format: str | None = None,
             **kw: Any) -> Document:
        p = src_dir / (name or f"src{len(docs)}.{'jsonl' if fmt == 'jsonl' else 'json'}")
        if data is None:
            jsongen.write_file(p, recs, fmt, **kw)
        else:
            p.write_bytes(data)
        doc = Document.open(p, settings=settings, record_path=record_path, format=format)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "out"
    d.mkdir()
    return d


def dumps(v: Any, **kw: Any) -> bytes:
    return codec.dumps(v, **kw)


def same_records(got: list, want: list) -> bool:
    """Equality of parsed records through their exact JSON text."""
    return len(got) == len(want) and all(dumps(a) == dumps(b) for a, b in zip(got, want))


def read_csv(path: Path, delimiter: str = ",", bom: bool = True) -> list[list[str]]:
    raw = path.read_bytes()
    if bom:
        assert raw.startswith(BOM)
        raw = raw[len(BOM):]
    else:
        assert not raw.startswith(BOM)
    return list(csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), delimiter=delimiter))


def ref_text(v: Any, sep: str = ";") -> str:
    """Reference CSV cell text (module docstring of jsonreshape.writer)."""
    if v is MISSING or v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return codec.number_text(v)
    if isinstance(v, list):
        return ref_join(v, sep)
    return dumps(v).decode("utf-8")


def ref_join(values: list, sep: str = ";") -> str:
    return sep.join(dumps(e).decode("utf-8") if isinstance(e, (dict, list)) else ref_text(e, sep) for e in values)


def ref_cell(rec: Any, column: str, sep: str = ";") -> str:
    pattern = parse_pattern(column)
    if pattern.is_concrete:
        return ref_text(get(rec, pattern.segments), sep)
    return ref_join(pattern.values(rec), sep)


def ref_rows(recs: list, columns: list[str], sep: str = ";", explode: str | None = None,
             keys: list[str] | None = None) -> list[list[str]]:
    """Reference rows (join mode, or rows mode expanding ``explode``)."""
    out = []
    for i, rec in enumerate(recs):
        def cells(value: Any, element: Any = MISSING) -> list[str]:
            row = []
            for c in columns:
                if c == "$key":
                    row.append(keys[i] if keys else "")
                elif c == "$index":
                    row.append(str(i))
                elif element is not MISSING and explode is not None and _inner(c, explode) is not None:
                    rest = _inner(c, explode)
                    row.append(ref_cell(element, rest, sep) if rest else ref_text(element, sep))
                else:
                    row.append(ref_cell(value, c, sep))
            return row

        arr = get(rec, parse_path(explode)) if explode is not None else MISSING
        if isinstance(arr, list) and arr:
            out.extend(cells(rec, el) for el in arr)
        else:
            out.append(cells(rec))
    return out


def _inner(column: str, explode: str) -> str | None:
    """The part of ``column`` below ``explode[*]`` ("" for the array itself), else None."""
    segs = parse_pattern(column).segments
    e = tuple(parse_path(explode))
    n = len(e)
    if segs[:n] != e:
        return None
    if len(segs) == n:
        return ""
    if segs[n] is not parse_pattern("[*]").segments[0]:
        return None
    rest = segs[n + 1:]
    return "$" + "".join(f"[{s}]" if isinstance(s, int) else f"[{codec.dumps(s).decode()}]" for s in rest) \
        if rest else ""


def files_in(d: Path) -> list[str]:
    return sorted(os.listdir(d))


def alpha_sorted(v: Any) -> Any:
    """Reference for ``sort_keys="alpha"``: the keys of every object in
    ``sortkeys.collation_key`` order (Uzbek collation, natural, case-insensitive)."""
    if isinstance(v, dict):
        return {k: alpha_sorted(v[k]) for k in sorted(v, key=collation_key)}
    if isinstance(v, list):
        return [alpha_sorted(x) for x in v]
    return v


def sql_stored(v: Any) -> tuple[Any, str]:
    """Reference ``(value, typeof)`` of a number in the SQLite export: INTEGER in the
    64-bit range, REAL when a double holds the exact decimal value, else the exact TEXT."""
    import math
    from decimal import Decimal

    if isinstance(v, int) and not isinstance(v, bool):
        if -2**63 <= v < 2**63:
            return int(v), "integer"
        return codec.number_text(v), "text"
    f = float(v)
    if math.isfinite(f) and Decimal(codec.number_text(v)) == Decimal(repr(f)):
        return f, "real"
    return codec.number_text(v), "text"


# ----------------------------------------------------------------------------------
# options
# ----------------------------------------------------------------------------------
def test_options_defaults_and_normalization() -> None:
    o = ExportOptions("JSON")
    assert (o.fmt, o.indent, o.sort_keys, o.keep_wrapper, o.columns, o.array_mode, o.explode) == \
        ("json", 2, False, True, None, "join", None)
    assert (o.join_sep, o.csv_delimiter, o.csv_bom, o.xlsx_max_rows, o.xlsx_split) == (";", ",", True, 1048576, "sheets")
    assert ExportOptions("jsonl", indent="TAB").indent == "\t"
    assert ExportOptions("json", indent="\t").indent == "\t"
    assert ExportOptions("json", indent="compact").indent is None
    assert ExportOptions("json", indent="4").indent == 4
    o = ExportOptions("csv", columns=[" id ", "contact.address.city", "$key", "$index", "orders[*].oid"],
                      explode="orders")
    assert o.columns == ["id", "contact.address.city", "$key", "$index", "orders[*].oid"]
    assert o.array_mode == "rows"  # explode implies rows
    assert ExportOptions(" XLSX ", xlsx_split="FILES", xlsx_max_rows=2).xlsx_split == "files"


@pytest.mark.parametrize("kwargs, key", [
    ({"fmt": "xml"}, "writer.invalid_option"),
    ({"fmt": 5}, "writer.invalid_option"),
    ({"fmt": "json", "indent": 17}, "writer.invalid_option"),
    ({"fmt": "json", "indent": -1}, "writer.invalid_option"),
    ({"fmt": "json", "indent": True}, "writer.invalid_option"),
    ({"fmt": "json", "indent": "wide"}, "writer.invalid_option"),
    ({"fmt": "json", "sort_keys": "yes"}, "writer.invalid_option"),
    ({"fmt": "json", "sort_keys": None}, "writer.invalid_option"),
    ({"fmt": "json", "sort_keys": 1}, "writer.invalid_option"),
    ({"fmt": "json", "keep_wrapper": 1}, "writer.invalid_option"),
    ({"fmt": "csv", "columns": "a,b"}, "writer.invalid_option"),
    ({"fmt": "csv", "columns": []}, "writer.invalid_option"),
    ({"fmt": "csv", "columns": ["a", " "]}, "writer.invalid_option"),
    ({"fmt": "csv", "columns": ["a", 3]}, "writer.invalid_option"),
    ({"fmt": "csv", "columns": ["a["]}, "writer.bad_column"),
    ({"fmt": "csv", "columns": ["$ key"]}, "writer.bad_column"),
    ({"fmt": "csv", "array_mode": "split"}, "writer.invalid_option"),
    ({"fmt": "csv", "explode": "orders[*]"}, "writer.bad_explode"),
    ({"fmt": "csv", "explode": ""}, "writer.invalid_option"),
    ({"fmt": "csv", "join_sep": ""}, "writer.invalid_option"),
    ({"fmt": "csv", "join_sep": "a\nb"}, "writer.invalid_option"),
    ({"fmt": "csv", "csv_delimiter": ",,"}, "writer.invalid_option"),
    ({"fmt": "csv", "csv_delimiter": '"'}, "writer.invalid_option"),
    ({"fmt": "csv", "csv_bom": "no"}, "writer.invalid_option"),
    ({"fmt": "xlsx", "xlsx_max_rows": 1}, "writer.invalid_option"),
    ({"fmt": "xlsx", "xlsx_max_rows": 1048577}, "writer.invalid_option"),
    ({"fmt": "xlsx", "xlsx_max_rows": 10.0}, "writer.invalid_option"),
    ({"fmt": "xlsx", "xlsx_split": "pages"}, "writer.invalid_option"),
])
def test_options_invalid(kwargs: dict, key: str, lang_en) -> None:
    with pytest.raises(UserInputError) as ei:
        ExportOptions(**kwargs)
    assert ei.value.key == key
    assert str(ei.value) and "{" not in str(ei.value)


def test_options_validate_after_assignment() -> None:
    o = ExportOptions("json")
    o.indent = 99
    with pytest.raises(UserInputError):
        o.validate()


def test_options_from_settings() -> None:
    s = Settings(export_indent="tab", csv_bom=False, csv_delimiter=";", array_join_sep="|", xlsx_max_rows=100)
    o = ExportOptions.from_settings("csv", s)
    assert (o.indent, o.csv_bom, o.csv_delimiter, o.join_sep, o.xlsx_max_rows) == ("\t", False, ";", "|", 100)
    o = ExportOptions.from_settings("json", s, indent=None, sort_keys=True)
    assert o.indent is None and o.sort_keys == "alpha"   # True means alphabetical
    assert ExportOptions("json", sort_keys=" CodePoint ").sort_keys == "codepoint"
    assert ExportOptions("json", sort_keys="alpha").sort_keys == "alpha"
    assert ExportOptions("json", sort_keys=False).sort_keys is False
    assert ExportOptions.from_settings("json").indent == 2  # user settings (defaults)


def test_format_for_path() -> None:
    assert format_for_path("a/b.JSON") == "json"
    assert format_for_path("x.ndjson") == "jsonl"
    assert format_for_path(Path("x.jsonl")) == "jsonl"
    assert format_for_path("x.csv") == "csv"
    assert format_for_path("x.xlsx") == "xlsx"
    assert format_for_path("x.db") == "sqlite" and format_for_path("x.sqlite3") == "sqlite"
    assert format_for_path("x.txt") is None and format_for_path("noext") is None


# ----------------------------------------------------------------------------------
# JSON / JSONL
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("style", ["compact", "pretty2", "tab", "messy"])
@pytest.mark.parametrize("indent", [None, 2, 4, "\t", 0])
def test_json_output_equals_codec_dumps(open_doc, out_dir: Path, style: str, indent: Any) -> None:
    recs = jsongen.records(40, seed=11)
    doc = open_doc(recs=recs, fmt="array", style=style, newline="\r\n" if style == "messy" else "\n")
    out = out_dir / "out.json"
    rep = export(doc, out, ExportOptions("json", indent=indent))
    assert out.read_bytes() == dumps(recs, indent=indent)
    assert rep.records == rep.rows == 40 and rep.files == [out] and rep.bytes_written == out.stat().st_size
    assert rep.warnings == [] and rep.fmt == "json" and rep.elapsed > 0


def test_json_random_values_all_kinds(open_doc, out_dir: Path) -> None:
    """Scalars, arrays and objects as records (edge strings and numbers included)."""
    recs = jsongen.records(120, seed=5, kind="value")
    doc = open_doc(recs=recs, fmt="array", style="pretty4")
    for indent in (None, 2, "\t"):
        export(doc, out_dir / "v.json", ExportOptions("json", indent=indent))
        assert (out_dir / "v.json").read_bytes() == dumps(recs, indent=indent)


def test_json_escaped_source_keeps_its_tokens(open_doc, out_dir: Path) -> None:
    recs = jsongen.records(30, seed=2)
    doc = open_doc(recs=recs, fmt="array", style="escaped")
    export(doc, out_dir / "e.json", ExportOptions("json", indent=2))
    data = (out_dir / "e.json").read_bytes()
    assert same_records(codec.loads(data), recs)
    assert b"\\u" in data and b"\\/" in data  # tokens are copied byte for byte (not re-escaped)
    export(doc, out_dir / "e2.json", ExportOptions("json", indent=2, sort_keys="codepoint"))
    data2 = (out_dir / "e2.json").read_bytes()
    assert data2 == dumps(recs, indent=2, sort_keys=True)  # re-encoded: escapes normalized, raw UTF-8
    export(doc, out_dir / "e3.json", ExportOptions("json", indent=2, sort_keys=True))
    assert (out_dir / "e3.json").read_bytes() == dumps(alpha_sorted(recs), indent=2)


@pytest.mark.parametrize("style", ["compact", "pretty2", "messy", "escaped"])
def test_json_jsonl_json_round_trip(open_doc, out_dir: Path, style: str) -> None:
    """§14 test 22: JSON -> JSONL -> JSON leaves every record identical."""
    recs = jsongen.records(60, seed=21) + jsongen.records(40, seed=22, kind="value")
    doc = open_doc(recs=recs, fmt="array", style=style)
    rep = export(doc, out_dir / "a.jsonl", ExportOptions("jsonl"))
    raw = (out_dir / "a.jsonl").read_bytes()
    lines = raw.split(b"\n")
    assert lines[-1] == b"" and len(lines) == len(recs) + 1 and rep.rows == len(recs)
    assert same_records([codec.loads(x) for x in lines[:-1]], recs)
    if style != "escaped":
        assert lines[:-1] == [dumps(r) for r in recs]
    doc2 = open_doc((out_dir / "a.jsonl").read_bytes(), name="back.jsonl", fmt="jsonl")
    assert doc2.kind == "jsonl" and doc2.record_count == len(recs)
    export(doc2, out_dir / "b.json", ExportOptions("json", indent=None))
    back = codec.loads((out_dir / "b.json").read_bytes())
    assert same_records(back, recs)
    export(doc2, out_dir / "c.json", ExportOptions("json", indent=4))
    assert same_records(codec.loads((out_dir / "c.json").read_bytes()), recs)


@pytest.mark.parametrize("fmt", ["array", "jsonl", "brace_list", "stream"])
def test_value_record_formats_to_json(open_doc, out_dir: Path, fmt: str) -> None:
    recs = jsongen.records(25, seed=31)
    doc = open_doc(recs=recs, fmt=fmt, style="pretty2" if fmt != "jsonl" else "compact")
    assert doc.kind == fmt
    export(doc, out_dir / "o.json", ExportOptions("json", indent=2))
    assert (out_dir / "o.json").read_bytes() == dumps(recs, indent=2)
    export(doc, out_dir / "o.jsonl", ExportOptions("jsonl"))
    assert (out_dir / "o.jsonl").read_bytes() == b"".join(dumps(r) + b"\n" for r in recs)


def test_keyed_document(open_doc, out_dir: Path) -> None:
    recs = jsongen.records(20, seed=41)
    keys = [f"k{i}" if i % 3 else f"Тошкент {i}" for i in range(20)]
    doc = open_doc(recs=recs, fmt="keyed", keys=keys, style="pretty2")
    assert doc.kind == "keyed" and doc.member_records
    whole = dict(zip(keys, recs))
    for indent in (None, 2, "\t"):
        export(doc, out_dir / "k.json", ExportOptions("json", indent=indent))
        assert (out_dir / "k.json").read_bytes() == dumps(whole, indent=indent)
    export(doc, out_dir / "k.jsonl", ExportOptions("jsonl"))
    assert (out_dir / "k.jsonl").read_bytes() == b"".join(dumps({k: r}) + b"\n" for k, r in zip(keys, recs))
    # a selection of members keeps its order (and stays an object)
    export(doc, out_dir / "sel.json", ExportOptions("json", indent=None), source=[5, 1, 7])
    assert (out_dir / "sel.json").read_bytes() == dumps({keys[5]: recs[5], keys[1]: recs[1], keys[7]: recs[7]})


@pytest.mark.parametrize("wrapper_path", [("data",), ("result", "items"), ("a", "b", "c")])
@pytest.mark.parametrize("indent", [None, 2, "\t"])
def test_wrapped_keep_and_drop(open_doc, out_dir: Path, wrapper_path: tuple, indent: Any) -> None:
    recs = jsongen.records(15, seed=51)
    doc = open_doc(recs=recs, fmt="wrapped", wrapper_path=wrapper_path, style="pretty2",
                   record_path=".".join(wrapper_path))
    assert doc.kind == "wrapped" and not doc.member_records
    source = codec.loads(doc.source_path.read_bytes())
    assert list(source) == ["meta", wrapper_path[0], "tail"]
    export(doc, out_dir / "keep.json", ExportOptions("json", indent=indent))
    assert (out_dir / "keep.json").read_bytes() == dumps(source, indent=indent)
    export(doc, out_dir / "drop.json", ExportOptions("json", indent=indent, keep_wrapper=False))
    assert (out_dir / "drop.json").read_bytes() == dumps(recs, indent=indent)
    # a selection inside the wrapper (meta and tail are kept)
    export(doc, out_dir / "sel.json", ExportOptions("json", indent=indent), source=[3, 0])
    want = codec.loads(doc.source_path.read_bytes())
    node = want
    for k in wrapper_path[:-1]:
        node = node[k]
    node[wrapper_path[-1]] = [recs[3], recs[0]]
    assert (out_dir / "sel.json").read_bytes() == dumps(want, indent=indent)


def test_wrapped_object_container(open_doc, out_dir: Path) -> None:
    data = (b'{"meta": {"v": 1, "s": "\xd0\xa2"},\n "data": {"a": {"x": 1.10, "y": [1, 2]}, "b": {"x": 1e400},'
            b' "c": 5},\n "tail": [true, null]}\n')
    doc = open_doc(data, record_path="data")
    assert doc.kind == "wrapped" and doc.member_records and doc.record_count == 3
    for indent in (None, 2):
        export(doc, out_dir / "keep.json", ExportOptions("json", indent=indent))
        assert (out_dir / "keep.json").read_bytes() == dumps(codec.loads(data), indent=indent)
        export(doc, out_dir / "drop.json", ExportOptions("json", indent=indent, keep_wrapper=False))
        assert (out_dir / "drop.json").read_bytes() == dumps(codec.loads(data)["data"], indent=indent)
    export(doc, out_dir / "o.jsonl", ExportOptions("jsonl"))
    assert (out_dir / "o.jsonl").read_bytes() == b'{"a":{"x":1.10,"y":[1,2]}}\n{"b":{"x":1e400}}\n{"c":5}\n'


def test_single_document(open_doc, out_dir: Path) -> None:
    recs = jsongen.records(5, seed=61)
    doc = open_doc(recs=recs, fmt="single", style="pretty4", format="single")
    assert doc.kind == "single" and doc.record_count == 1
    export(doc, out_dir / "s.json", ExportOptions("json", indent=2))
    assert (out_dir / "s.json").read_bytes() == dumps(recs, indent=2)  # just the value
    export(doc, out_dir / "s.jsonl", ExportOptions("jsonl"))
    assert (out_dir / "s.jsonl").read_bytes() == dumps(recs) + b"\n"
    export(doc, out_dir / "twice.json", ExportOptions("json", indent=None), source=[0, 0])
    assert (out_dir / "twice.json").read_bytes() == dumps([recs, recs])
    doc2 = open_doc('  "Oʻzbekiston 😀"  \n'.encode("utf-8"), name="scalar.json")
    assert doc2.kind == "single"
    export(doc2, out_dir / "scalar.json", ExportOptions("json"))
    assert (out_dir / "scalar.json").read_bytes() == '"Oʻzbekiston 😀"'.encode("utf-8")


@pytest.mark.parametrize("mode", ["codepoint", "alpha", True])
def test_sort_keys(open_doc, out_dir: Path, mode: Any) -> None:
    recs = jsongen.records(30, seed=71)
    doc = open_doc(recs=recs, fmt="array", style="pretty2")

    def enc(v: Any, **kw: Any) -> bytes:   # the expected encoding of a sorted value
        return dumps(v, sort_keys=True, **kw) if mode == "codepoint" else dumps(alpha_sorted(v), **kw)

    export(doc, out_dir / "s.json", ExportOptions("json", indent=2, sort_keys=mode))
    assert (out_dir / "s.json").read_bytes() == enc(recs, indent=2)
    export(doc, out_dir / "s.jsonl", ExportOptions("jsonl", sort_keys=mode))
    assert (out_dir / "s.jsonl").read_bytes() == b"".join(enc(r) + b"\n" for r in recs)
    keys = [f"z{i}" for i in range(30)][::-1]
    kdoc = open_doc(recs=recs, fmt="keyed", keys=keys, style="compact")
    export(kdoc, out_dir / "k.json", ExportOptions("json", indent=None, sort_keys=mode))
    # member order is kept, the values are sorted
    got = (out_dir / "k.json").read_bytes()
    assert got == b"{" + b",".join(dumps(k) + b":" + enc(r) for k, r in zip(keys, recs)) + b"}"
    export(kdoc, out_dir / "k.jsonl", ExportOptions("jsonl", sort_keys=mode))
    assert (out_dir / "k.jsonl").read_bytes() == b"".join(
        b"{" + dumps(k) + b":" + enc(r) + b"}\n" for k, r in zip(keys, recs))


class FakeRecordSet:
    """A RecordSet-like selection (len / [] / iter_chunks)."""

    def __init__(self, indices: list[int]) -> None:
        self._a = array("Q", indices)

    def __len__(self) -> int:
        return len(self._a)

    def __getitem__(self, i: int) -> int:
        return self._a[i]

    def iter_chunks(self, size: int = 65536):
        for i in range(0, len(self._a), 3):
            yield self._a[i:i + 3]


@pytest.mark.parametrize("make_source", [
    lambda: [7, 2, 9, 2, 0, 19],
    lambda: (7, 2, 9, 2, 0, 19),
    lambda: array("Q", [7, 2, 9, 2, 0, 19]),
    lambda: iter([7, 2, 9, 2, 0, 19]),
    lambda: FakeRecordSet([7, 2, 9, 2, 0, 19]),
])
def test_selection_order_is_kept(open_doc, out_dir: Path, make_source) -> None:
    recs = jsongen.records(20, seed=81)
    doc = open_doc(recs=recs, fmt="array", style="pretty2")
    order = [7, 2, 9, 2, 0, 19]
    want = [recs[i] for i in order]
    rep = export(doc, out_dir / "o.json", ExportOptions("json", indent=2), source=make_source())
    assert (out_dir / "o.json").read_bytes() == dumps(want, indent=2) and rep.records == 6
    export(doc, out_dir / "o.jsonl", ExportOptions("jsonl"), source=make_source())
    assert (out_dir / "o.jsonl").read_bytes() == b"".join(dumps(r) + b"\n" for r in want)
    export(doc, out_dir / "o.csv", ExportOptions("csv", columns=["$index", "id", "name"]), source=make_source())
    assert read_csv(out_dir / "o.csv")[1:] == [[str(i), str(recs[i]["id"]), recs[i]["name"]] for i in order]


def test_ranges_and_block_paths_give_identical_output(open_doc, out_dir: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Whole-block feeding (consecutive records) and record-by-record joining agree."""
    monkeypatch.setattr(writer, "BLOCK_BYTES", 700)  # many small blocks
    recs = jsongen.records(80, seed=91)
    for fmt in ("array", "keyed", "brace_list", "jsonl"):
        doc = open_doc(recs=recs, fmt=fmt, style="pretty2" if fmt != "jsonl" else "compact")
        outputs = []
        for source in (None, list(range(80)), range(80), FakeRecordSet(list(range(80)))):
            export(doc, out_dir / "x.json", ExportOptions("json", indent=2), source=source)
            outputs.append((out_dir / "x.json").read_bytes())
        assert len(set(outputs)) == 1
        sub = [0, 1, 2, 5, 6, 40, 41, 79]
        export(doc, out_dir / "y.json", ExportOptions("json", indent=None), source=sub)
        export(doc, out_dir / "z.json", ExportOptions("json", indent=None), source=range(10, 20))
        if fmt == "keyed":
            keys = [f"id{i}" for i in range(80)]
            assert (out_dir / "y.json").read_bytes() == dumps({keys[i]: recs[i] for i in sub})
            assert (out_dir / "z.json").read_bytes() == dumps({keys[i]: recs[i] for i in range(10, 20)})
        else:
            assert (out_dir / "y.json").read_bytes() == dumps([recs[i] for i in sub])
            assert (out_dir / "z.json").read_bytes() == dumps(recs[10:20])


@pytest.mark.parametrize("source", [[0, 25], [-1], range(3, 30), [1.5], ["1"]])
def test_bad_selection(open_doc, out_dir: Path, source: Any) -> None:
    doc = open_doc(recs=jsongen.records(20, seed=1), fmt="array")
    with pytest.raises(UserInputError) as ei:
        export(doc, out_dir / "o.json", ExportOptions("json"), source=source)
    assert ei.value.key == "writer.error.bad_record"
    assert files_in(out_dir) == []


def test_unicode_is_never_escaped(open_doc, out_dir: Path) -> None:
    texts = ["Oʻzbekiston", "Тошкент", "Ўзбекистон", "emoji 😀 test", "G'ulom", "line sep " + chr(0x2028)]
    recs = [{"t": s, s: [s]} for s in texts]
    doc = open_doc(recs=recs, fmt="array")
    for fmt in ("json", "jsonl"):
        for sk in (False, True):
            out = out_dir / f"u.{fmt}"
            export(doc, out, ExportOptions(fmt, sort_keys=sk))
            data = out.read_bytes()
            assert b"\\u" not in data
            for s in texts:
                assert s.encode("utf-8") in data


@pytest.mark.parametrize("fmt", ["array", "keyed", "jsonl", "wrapped"])
def test_huge_records_are_streamed(open_doc, out_dir: Path, fmt: str) -> None:
    recs = jsongen.records(30, seed=101)
    small = open_doc(recs=recs, fmt=fmt, style="pretty2" if fmt != "jsonl" else "compact", name=f"small.{fmt}")
    huge = open_doc(recs=recs, fmt=fmt, style="pretty2" if fmt != "jsonl" else "compact", name=f"huge.{fmt}",
                    settings=Settings(large_node_bytes=150))
    assert any(huge.is_huge(i) for i in range(huge.record_count))
    assert not all(huge.is_huge(i) for i in range(huge.record_count))
    for opts in (ExportOptions("json", indent=2), ExportOptions("json", indent=None), ExportOptions("jsonl"),
                 ExportOptions("json", indent="\t", keep_wrapper=False)):
        export(small, out_dir / "a.out", opts)
        rep = export(huge, out_dir / "b.out", opts, source=[4, 3, 2, 1, 0, 29])
        export(small, out_dir / "c.out", opts, source=[4, 3, 2, 1, 0, 29])
        assert (out_dir / "b.out").read_bytes() == (out_dir / "c.out").read_bytes()
        export(huge, out_dir / "d.out", opts)
        assert (out_dir / "d.out").read_bytes() == (out_dir / "a.out").read_bytes()
        # JSON Lines has no place for a wrapper: said in a notice (docs/ARCHITECTURE.md §14.4, G5)
        notice = ["writer.note.wrapper_not_written"] if fmt == "wrapped" and opts.fmt == "jsonl" else []
        assert rep.warning_keys == notice and len(rep.warnings) == len(notice)
    rep = export(huge, out_dir / "s.json", ExportOptions("json", indent=None, sort_keys=True))
    assert rep.warning_keys == ["writer.warn.huge_unsorted"]
    raw = (out_dir / "s.json").read_bytes()
    got = codec.loads(raw)
    if fmt == "keyed":
        got = list(got.values())
    elif fmt == "wrapped":
        got = got["data"]
    assert [dumps(g, sort_keys=True) for g in got] == [dumps(r, sort_keys=True) for r in recs]
    for i in range(huge.record_count):
        # small records are re-encoded with sorted keys, huge ones are streamed as they are
        assert (dumps(recs[i], sort_keys=True) if not huge.is_huge(i) else dumps(recs[i])) in raw


def test_partial_document_exports_records_before_the_error(open_doc, out_dir: Path, lang_en) -> None:
    recs = jsongen.records(10, seed=111)
    full = jsongen.build_document(recs, "array", style="pretty2")
    cut = full[: len(full) * 2 // 3]
    doc = open_doc(cut, name="cut.json")
    assert doc.partial and 0 < doc.record_count < 10
    rep = export(doc, out_dir / "p.json", ExportOptions("json"))
    got = codec.loads((out_dir / "p.json").read_bytes())
    assert same_records(got, recs[:doc.record_count])
    assert rep.warning_keys == ["writer.warn.partial"]
    assert f"({doc.record_count})" in rep.warnings[0]
    # a wrapped document cut inside its records: the wrapper cannot be kept
    wfull = jsongen.build_document(recs, "wrapped", style="compact")
    wdoc = open_doc(wfull[: len(wfull) // 2], name="wcut.json", record_path="data")
    rep = export(wdoc, out_dir / "w.json", ExportOptions("json"))
    assert set(rep.warning_keys) == {"writer.warn.partial", "writer.warn.wrapper_dropped"}
    assert same_records(codec.loads((out_dir / "w.json").read_bytes()), recs[:wdoc.record_count])


def test_jsonl_broken_lines_are_reported(open_doc, out_dir: Path, lang_en) -> None:
    data = b'{"a":1}\n{"a":\n{"a":3}\nnot json\n{"a":5}\n'
    doc = open_doc(data, name="b.jsonl")
    assert doc.broken_count == 2 and doc.record_count == 3
    rep = export(doc, out_dir / "o.json", ExportOptions("json", indent=None))
    assert (out_dir / "o.json").read_bytes() == b'[{"a":1},{"a":3},{"a":5}]'
    assert rep.warning_keys == ["writer.warn.broken_lines"] and "2" in rep.warnings[0]


# ----------------------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------------------
HAND_JSON = (
    '[{"id": 1, "name": "Ali, \\"Vali\\"", "a": {"b": {"c": 1.10}}, "tags": ["x", "y;z"], "flag": true,\n'
    '  "orders": [{"oid": 7, "sum": 0.1}, {"oid": 8, "sum": 1e400}], "nil": null},\n'
    ' {"id": 2, "name": "Тошкент\\nshahri", "a": {"b": {"c": 12345678901234567890}}, "tags": [],\n'
    '  "flag": false, "orders": [], "obj": {"k": [1, {"z": null}]}},\n'
    ' {"id": 3, "name": "", "a": {"b": "text"}, "tags": [1, [2, 3], {"q": "w"}, null, false], "orders": null},\n'
    ' {"id": 4, "name": "Oʻzbekiston 😀", "a": [1, 2], "orders": [{"oid": 9}]}]\n'
).encode("utf-8")


def hand_doc(open_doc, **kw: Any) -> Document:
    return open_doc(HAND_JSON, name="hand.json", **kw)


def test_csv_handcrafted_cells(open_doc, out_dir: Path) -> None:
    doc = hand_doc(open_doc)
    recs = [doc.record(i) for i in range(doc.record_count)]
    assert recs[0]["a"]["b"]["c"].raw == "1.10" and recs[0]["orders"][1]["sum"].raw == "1e400"
    cols = discover_columns(doc)
    assert cols == ["id", "name", "a.b.c", "tags", "flag", "orders", "nil", "obj.k", "a.b", "a"]
    rep = export(doc, out_dir / "h.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "h.csv")
    assert rows[0] == cols and rep.columns == cols and rep.rows == 4
    # objects at a column path are compact JSON ("a.b" and "a" exist because of records 3 and 4)
    assert rows[1] == ["1", 'Ali, "Vali"', "1.10", "x;y;z", "true", '{"oid":7,"sum":0.1};{"oid":8,"sum":1e400}',
                       "", "", '{"c":1.10}', '{"b":{"c":1.10}}']
    assert rows[2] == ["2", "Тошкент\nshahri", "12345678901234567890", "", "false", "", "", '1;{"z":null}',
                       '{"c":12345678901234567890}', '{"b":{"c":12345678901234567890}}']
    assert rows[3] == ["3", "", "", '1;[2,3];{"q":"w"};;false', "", "", "", "", "text", '{"b":"text"}']
    assert rows[4] == ["4", "Oʻzbekiston 😀", "", "", "", '{"oid":9}', "", "", "", "1;2"]
    assert rows[1:] == ref_rows(recs, cols)
    raw = (out_dir / "h.csv").read_bytes()
    assert b',1.10,' in raw and b',12345678901234567890,' in raw  # exact text, never quoted
    assert b'"Ali, ""Vali"""' in raw and "\"Тошкент\nshahri\"".encode() in raw
    assert raw.count(b"\r\n") == 5  # header + 4 rows; the newline inside the quoted cell stays "\n"


def test_csv_explicit_columns_wildcards_and_specials(open_doc, out_dir: Path) -> None:
    doc = hand_doc(open_doc)
    cols = ["$index", "$key", "id", "orders[*].oid", "orders[0].sum", "a.*", "**.oid", "a.b", "missing.x", "$"]
    export(doc, out_dir / "w.csv", ExportOptions("csv", columns=cols, join_sep="|", csv_bom=False))
    rows = read_csv(out_dir / "w.csv", bom=False)
    recs = [doc.record(i) for i in range(4)]
    assert rows[0] == cols
    want = ref_rows(recs, cols, sep="|")
    assert rows[1:] == want
    assert rows[1][3] == "7|8" and rows[1][1] == "" and rows[3][0] == "2"


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_delimiter_bom_and_quoting(open_doc, out_dir: Path, delimiter: str) -> None:
    recs = [{"a": f"x{delimiter}y", "b": 'say "hi"', "c": "l1\r\nl2", "d": " lead", "e": "plain", "f": 1.5}]
    doc = open_doc(recs=recs, fmt="array")
    for bom in (True, False):
        export(doc, out_dir / "q.csv", ExportOptions("csv", csv_delimiter=delimiter, csv_bom=bom))
        rows = read_csv(out_dir / "q.csv", delimiter=delimiter, bom=bom)
        assert rows == [["a", "b", "c", "d", "e", "f"], [f"x{delimiter}y", 'say "hi"', "l1\r\nl2", " lead", "plain", "1.5"]]
        raw = (out_dir / "q.csv").read_bytes().decode("utf-8-sig")
        assert raw.endswith("\r\n")
        assert f'"x{delimiter}y"' in raw and '"say ""hi"""' in raw and '"l1\r\nl2"' in raw
        assert f"{delimiter}plain{delimiter}1.5\r\n" in raw  # minimal quoting


def test_csv_random_records_match_reference(open_doc, out_dir: Path) -> None:
    recs = jsongen.records(120, seed=121)
    doc = open_doc(recs=recs, fmt="array", style="messy")
    cols = discover_columns(doc)
    export(doc, out_dir / "r.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "r.csv")
    assert rows[0] == cols
    assert rows[1:] == ref_rows(recs, cols)


def test_csv_rows_are_written_in_small_calls(open_doc, out_dir: Path, monkeypatch) -> None:
    """P-F8: csv writerows() is one C call holding the GIL; rows go in slices of at most
    CSV_ROWS_PER_CALL (a whole 8 MiB block of rows froze the GUI thread for ~0.2 s).
    Same file content."""
    recs = jsongen.records(700, seed=5)
    doc = open_doc(recs=recs, fmt="array")
    export(doc, out_dir / "a.csv", ExportOptions("csv"))
    sizes: list[int] = []
    real_writer = writer.csv.writer

    class Spy:
        def __init__(self, *args, **kwargs):
            self._w = real_writer(*args, **kwargs)

        def writerow(self, row):
            return self._w.writerow(row)

        def writerows(self, rows):
            rows = list(rows)
            sizes.append(len(rows))
            return self._w.writerows(rows)

    monkeypatch.setattr(writer, "CSV_ROWS_PER_CALL", 64)
    monkeypatch.setattr(writer.csv, "writer", Spy)
    export(doc, out_dir / "b.csv", ExportOptions("csv"))
    assert sizes and max(sizes) <= 64 and sum(sizes) == 700
    assert (out_dir / "a.csv").read_bytes() == (out_dir / "b.csv").read_bytes()


def test_csv_rows_mode(open_doc, out_dir: Path) -> None:
    doc = hand_doc(open_doc)
    recs = [doc.record(i) for i in range(4)]
    # automatic columns, the first array column (tags) is expanded
    cols = discover_columns(doc, array_mode="rows")
    assert cols == ["id", "name", "a.b.c", "tags[*]", "tags[*].q", "flag", "orders", "nil", "obj.k", "a.b", "a"]
    rep = export(doc, out_dir / "r1.csv", ExportOptions("csv", array_mode="rows"))
    rows = read_csv(out_dir / "r1.csv")
    assert rows[0] == cols
    assert rows[1:] == ref_rows(recs, cols, explode="tags")
    assert rep.rows == 2 + 1 + 5 + 1 and rep.records == 4
    assert [r[3] for r in rows[1:]] == ["x", "y;z", "", "1", "2;3", '{"q":"w"}', "", "false", ""]
    assert [r[4] for r in rows[1:]] == ["", "", "", "", "", "w", "", "", ""]
    # explicit explode: element columns orders[*].oid / orders[*].sum
    cols2 = discover_columns(doc, explode="orders")
    assert cols2 == ["id", "name", "a.b.c", "tags", "flag", "orders[*].oid", "orders[*].sum", "nil", "obj.k", "a.b",
                     "a"]
    export(doc, out_dir / "r2.csv", ExportOptions("csv", explode="orders"))
    rows = read_csv(out_dir / "r2.csv")
    assert rows[1:] == ref_rows(recs, cols2, explode="orders")
    assert [(r[0], r[5], r[6]) for r in rows[1:]] == [("1", "7", "0.1"), ("1", "8", "1e400"), ("2", "", ""),
                                                      ("3", "", ""), ("4", "9", "")]
    # explicit columns: the array before the first [*] is expanded; other columns repeat
    cols3 = ["id", "orders[*].oid", "tags", "orders"]
    export(doc, out_dir / "r3.csv", ExportOptions("csv", columns=cols3, array_mode="rows"))
    rows = read_csv(out_dir / "r3.csv")
    assert rows[1:] == ref_rows(recs, cols3, explode="orders")
    assert rows[1] == ["1", "7", "x;y;z", '{"oid":7,"sum":0.1}']
    # explicit columns without [*]: the first column holding arrays is expanded
    export(doc, out_dir / "r4.csv", ExportOptions("csv", columns=["id", "flag", "tags"], array_mode="rows"))
    rows = read_csv(out_dir / "r4.csv")
    assert rows[1:] == ref_rows(recs, ["id", "flag", "tags"], explode="tags")


def test_csv_keyed_records(open_doc, out_dir: Path) -> None:
    recs = jsongen.records(12, seed=131)
    keys = [f"key {i}" for i in range(12)]
    doc = open_doc(recs=recs, fmt="keyed", keys=keys)
    cols = discover_columns(doc)
    assert cols[0] == "$key"
    export(doc, out_dir / "k.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "k.csv")
    assert rows[0] == cols and rows[1:] == ref_rows(recs, cols, keys=keys)
    export(doc, out_dir / "k2.csv", ExportOptions("csv", columns=["id", "$key", "$index"]), source=[3, 1])
    assert read_csv(out_dir / "k2.csv")[1:] == [["3", "key 3", "3"], ["1", "key 1", "1"]]


def test_discover_columns_details(open_doc) -> None:
    recs: list[Any] = [{"a": {}, "b": {"c": {}}}, {"b": {"c": {"d": 1}}, "e": []}, {"a": {"x": None}, "f": {}}]
    doc = open_doc(recs=recs, fmt="array")
    # empty objects are columns only when nothing lies above or below them
    assert discover_columns(doc) == ["b.c.d", "e", "a.x", "f"]
    scal = open_doc(recs=[1, "x", [1, 2], {"k": 1}, {}], fmt="jsonl")
    assert discover_columns(scal) == ["$", "k"]
    # evenly spread deterministic sample: records 0, 20, 40, 60, 80 of 100
    many = [{f"f{i}": i} for i in range(100)]
    mdoc = open_doc(recs=many, fmt="array")
    assert discover_columns(mdoc, sample=5) == ["f0", "f20", "f40", "f60", "f80"]
    assert discover_columns(mdoc, sample=5) == discover_columns(mdoc, sample=5)
    assert discover_columns(mdoc, [99, 3, 50], sample=10) == ["f99", "f3", "f50"]
    assert discover_columns(mdoc, range(10, 20), sample=2) == ["f10", "f15"]
    with pytest.raises(UserInputError):
        discover_columns(mdoc, sample=0)
    with pytest.raises(UserInputError):
        discover_columns(mdoc, array_mode="x")
    # rows mode with element objects, scalars and nested arrays
    rdoc = open_doc(recs=[{"id": 1, "o": [{"p": 1, "q": [1]}, 5, [7]]}, {"id": 2, "o": [{"r": {"s": 1}}]}],
                    fmt="array")
    assert discover_columns(rdoc, array_mode="rows") == ["id", "o[*].p", "o[*].q", "o[*]", "o[*].r.s"]
    assert discover_columns(rdoc, explode="o") == ["id", "o[*].p", "o[*].q", "o[*]", "o[*].r.s"]
    assert discover_columns(rdoc, explode="o[0].q") == ["id", "o", "o[0].q[*]"]
    # keys that need quoting round-trip through the path syntax
    qdoc = open_doc(recs=[{"dot.key": 1, "": 2, "0": 3, "$key": 4, "a b": {"*": 5}}], fmt="array")
    cols = discover_columns(qdoc)
    assert cols == ['["dot.key"]', '[""]', '["0"]', '["$key"]', '["a b"]["*"]']
    assert [parse_pattern(c).segments for c in cols] == [("dot.key",), ("",), ("0",), ("$key",), ("a b", "*")]


def test_csv_huge_records(open_doc, out_dir: Path, lang_en) -> None:
    recs = jsongen.records(20, seed=141)
    doc = open_doc(recs=recs, fmt="array", style="pretty2", settings=Settings(large_node_bytes=200))
    huge = [i for i in range(20) if doc.is_huge(i)]
    assert huge and len(huge) < 20
    # (no columns with edge number tokens: ijson normalizes exponents of huge records)
    cols = ["id", "name", "contact.address.city", "orders[*].oid", "tags", "active", "$index"]
    rep = export(doc, out_dir / "e.csv", ExportOptions("csv", columns=cols))
    assert read_csv(out_dir / "e.csv")[1:] == ref_rows(recs, cols) and rep.warnings == [] and rep.records == 20
    # rows mode streams the expanded array of huge records through the node index
    rep = export(doc, out_dir / "r.csv", ExportOptions("csv", columns=cols, explode="orders"))
    assert read_csv(out_dir / "r.csv")[1:] == ref_rows(recs, cols, explode="orders")
    # automatic columns: huge records are skipped with a warning
    rep = export(doc, out_dir / "a.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "a.csv")
    assert len(rows) - 1 == 20 - len(huge) and rep.skipped == len(huge)
    assert rep.warning_keys == ["writer.warn.huge_skipped"] and str(len(huge)) in rep.warnings[0]
    assert [r[0] for r in rows[1:]] == [str(i) for i in range(20) if i not in huge]


def test_csv_huge_record_with_large_elements(open_doc, out_dir: Path) -> None:
    rec = {"id": 1, "items": [{"k": i, "v": "x" * (i * 40)} for i in range(8)], "tail": "t"}
    doc = open_doc(recs=[rec, {"id": 2, "items": []}], fmt="array", settings=Settings(large_node_bytes=150))
    assert doc.is_huge(0)
    cols = ["id", "items[*].k", "items[*].v", "tail"]
    export(doc, out_dir / "x.csv", ExportOptions("csv", columns=cols, explode="items"))
    rows = read_csv(out_dir / "x.csv")
    assert rows[1:] == ref_rows([rec, {"id": 2, "items": []}], cols, explode="items")


def test_csv_no_columns(open_doc, out_dir: Path) -> None:
    # records that are all empty objects: the record itself is the only column
    doc = open_doc(recs=[{}, {}], fmt="array")
    export(doc, out_dir / "o.csv", ExportOptions("csv"))
    assert read_csv(out_dir / "o.csv") == [["$"], ["{}"], ["{}"]]
    # every record huge: nothing can be sampled, so there are no automatic columns
    big = open_doc(recs=[{"a": "x" * 50}, {"b": "y" * 50}], fmt="array", settings=Settings(large_node_bytes=20))
    with pytest.raises(UserInputError) as ei:
        export(big, out_dir / "n.csv", ExportOptions("csv"))
    assert ei.value.key == "writer.error.no_columns" and ei.value.hint
    assert files_in(out_dir) == ["o.csv"]
    empty = open_doc(b"[]", name="empty.json")
    rep = export(empty, out_dir / "e.csv", ExportOptions("csv"))
    assert (out_dir / "e.csv").read_bytes() == BOM and rep.rows == 0


# ----------------------------------------------------------------------------------
# XLSX
# ----------------------------------------------------------------------------------
def sheet_rows(ws: Any) -> list[list[Any]]:
    return [list(r) for r in ws.iter_rows(values_only=True)]


def test_xlsx_values_and_types(open_doc, out_dir: Path) -> None:
    doc = hand_doc(open_doc)
    cols = ["id", "name", "a.b.c", "tags", "flag", "orders[*].sum", "nil", "missing", "obj"]
    rep = export(doc, out_dir / "h.xlsx", ExportOptions("xlsx", columns=cols))
    wb = openpyxl.load_workbook(out_dir / "h.xlsx")
    assert wb.sheetnames == ["Sheet1"]
    ws = wb["Sheet1"]
    rows = sheet_rows(ws)
    assert rows[0] == cols and all(ws.cell(1, c).font.b for c in range(1, len(cols) + 1))
    assert not ws.cell(2, 1).font.b
    assert rows[1] == [1, 'Ali, "Vali"', 1.1, "x;y;z", True, "0.1;1e400", None, None, None]
    assert rows[2] == [2, "Тошкент\nshahri", "12345678901234567890", None, False, None, None, None,
                       '{"k":[1,{"z":null}]}']
    assert rows[3][3] == '1;[2,3];{"q":"w"};;false' and rows[3][2] is None
    assert rows[4][1] == "Oʻzbekiston 😀" and rows[4][5] is None
    assert isinstance(rows[1][0], int) and isinstance(rows[1][2], float) and rows[1][4] is True
    # "missing" has a value in no record: it is named (a misspelled path?)
    assert rep.warning_keys == ["writer.warn.xlsx_number_text", "writer.warn.columns_not_found"]
    assert rep.warnings[0].startswith("1 ") and rep.warnings[1].endswith(": missing.")


def test_xlsx_numbers(open_doc, out_dir: Path) -> None:
    tokens = ["0", "-0", "7", "0.1", "1.10", "-0.0", "1E5", "2.5E+3", "123456789012345", "1234567890123456",
              "12345678901234567890", "1e400", "1e-400", "5e-324", "3.141592653589793", "3.14159265358979",
              "9007199254740993", "1e300", "0.00001"]
    data = b"[" + b",".join(b'{"n":' + tk.encode() + b"}" for tk in tokens) + b"]"
    doc = open_doc(data, name="nums.json")
    rep = export(doc, out_dir / "n.xlsx", ExportOptions("xlsx"))
    got = [r[0] for r in sheet_rows(openpyxl.load_workbook(out_dir / "n.xlsx").active)[1:]]
    as_text = {"1234567890123456", "12345678901234567890", "1e400", "1e-400", "5e-324", "3.141592653589793",
               "9007199254740993"}
    for tk, v in zip(tokens, got):
        if tk in as_text:
            assert v == tk, tk
        else:
            assert isinstance(v, (int, float)) and not isinstance(v, bool), tk
            assert v == float(tk), tk
    assert rep.warning_keys == ["writer.warn.xlsx_number_text"]
    assert rep.warnings[0].startswith(f"{len(as_text)} ")


def test_xlsx_strings_stay_strings(open_doc, out_dir: Path) -> None:
    recs = [{"s": s} for s in ["=1+2", "http://example.com", "123", "1e5", "TRUE", "  pad  ", "a\x07b", "_x0041_",
                               "Ўзбекистон"]]
    doc = open_doc(recs=recs, fmt="array")
    export(doc, out_dir / "s.xlsx", ExportOptions("xlsx"))
    ws = openpyxl.load_workbook(out_dir / "s.xlsx").active
    got = [r[0] for r in sheet_rows(ws)[1:]]
    assert got[:6] == ["=1+2", "http://example.com", "123", "1e5", "TRUE", "  pad  "]
    assert got[8] == "Ўзбекистон"
    assert all(ws.cell(i, 1).data_type == "s" for i in range(2, 2 + len(recs)))


@pytest.mark.parametrize("split", ["sheets", "files"])
def test_xlsx_split(open_doc, out_dir: Path, split: str, lang_en) -> None:
    recs = [{"i": i, "s": f"r{i}", "arr": list(range(i % 3))} for i in range(23)]
    doc = open_doc(recs=recs, fmt="array")
    rep = export(doc, out_dir / "big.xlsx", ExportOptions("xlsx", xlsx_max_rows=5, xlsx_split=split,
                                                          explode="arr"))
    # rows mode: one row per element (records with an empty array give one row)
    want = []
    for r in recs:
        els = r["arr"] or [None]
        want.extend([r["i"], r["s"], e] for e in els)
    assert rep.rows == len(want)
    per = 4  # 5 rows per sheet, header included
    parts = (len(want) + per - 1) // per
    got = []
    if split == "sheets":
        assert rep.files == [out_dir / "big.xlsx"] and files_in(out_dir) == ["big.xlsx"]
        wb = openpyxl.load_workbook(out_dir / "big.xlsx")
        assert wb.sheetnames == [f"Sheet{k}" for k in range(1, parts + 1)]
        sheets = [wb[n] for n in wb.sheetnames]
        assert rep.warning_keys == ["writer.warn.xlsx_sheets"] and f"{parts} sheets" in rep.warnings[0]
    else:
        names = [f"big_{k}.xlsx" for k in range(1, parts + 1)]
        assert files_in(out_dir) == sorted(names) and [p.name for p in rep.files] == names
        sheets = []
        for n in names:
            wb = openpyxl.load_workbook(out_dir / n)
            assert wb.sheetnames == ["Sheet1"]
            sheets.append(wb.active)
        assert rep.warning_keys == ["writer.warn.xlsx_files"] and f"{parts} files" in rep.warnings[0]
        assert rep.bytes_written == sum((out_dir / n).stat().st_size for n in names)
    for ws in sheets:
        rows = sheet_rows(ws)
        assert rows[0] == ["i", "s", "arr[*]"] and ws.cell(1, 1).font.b
        assert 2 <= len(rows) <= 5
        got.extend(rows[1:])
    assert got == want  # no row lost, order kept


def test_xlsx_files_single_part_keeps_name(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=[{"a": 1}, {"a": 2}], fmt="array")
    rep = export(doc, out_dir / "one.xlsx", ExportOptions("xlsx", xlsx_split="files", xlsx_max_rows=3))
    assert files_in(out_dir) == ["one.xlsx"] and rep.files == [out_dir / "one.xlsx"] and rep.warnings == []
    assert sheet_rows(openpyxl.load_workbook(out_dir / "one.xlsx").active) == [["a"], [1], [2]]


def test_xlsx_long_strings_are_truncated(open_doc, out_dir: Path, lang_en) -> None:
    long_ascii = "a" * 40000
    # an emoji (2 UTF-16 units) straddling the limit is not split
    long_emoji = "b" * 32766 + "😀" + "c" * 10
    doc = open_doc(recs=[{"s": long_ascii}, {"s": long_emoji}, {"s": "z" * 32767}], fmt="array")
    rep = export(doc, out_dir / "l.xlsx", ExportOptions("xlsx"))
    got = [r[0] for r in sheet_rows(openpyxl.load_workbook(out_dir / "l.xlsx").active)[1:]]
    assert got[0] == "a" * 32767 and got[1] == "b" * 32766 and got[2] == "z" * 32767
    assert rep.warning_keys == ["writer.warn.xlsx_truncated"] and rep.warnings[0].startswith("2 ")


def test_xlsx_too_many_columns(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=[{"a": 1}], fmt="array")
    with pytest.raises(UserInputError) as ei:
        export(doc, out_dir / "w.xlsx", ExportOptions("xlsx", columns=[f"c{i}" for i in range(16385)]))
    assert ei.value.key == "writer.error.too_many_columns"
    assert files_in(out_dir) == []


def test_xlsx_keyed_and_huge(open_doc, out_dir: Path) -> None:
    recs = jsongen.records(10, seed=151)
    keys = [f"k{i}" for i in range(10)]
    doc = open_doc(recs=recs, fmt="keyed", keys=keys, settings=Settings(large_node_bytes=180))
    cols = ["$key", "$index", "id", "orders[*].oid"]
    export(doc, out_dir / "k.xlsx", ExportOptions("xlsx", columns=cols))
    rows = sheet_rows(openpyxl.load_workbook(out_dir / "k.xlsx").active)
    assert rows[0] == cols
    for i, row in enumerate(rows[1:]):
        oids = [o["oid"] for o in recs[i].get("orders", [])]
        want_oid = None if not oids else (oids[0] if len(oids) == 1 else ";".join(map(str, oids)))
        assert row == [keys[i], i, i, want_oid]


# ----------------------------------------------------------------------------------
# SQLite
# ----------------------------------------------------------------------------------
def q(db: Path, sql: str, *args: Any) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def qi(name: str) -> str:
    """Quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def columns_of(db: Path, table: str) -> list[str]:
    return [r[1] for r in q(db, f"PRAGMA table_info({qi(table)})")]


SQL_JSON = (
    '[{"id": 1, "name": "Ali", "contact": {"phone": "+998", "address": {"city": "Toshkent", "zip": 100000}},\n'
    '  "orders": [{"oid": 7, "sum": 0.1, "items": [{"sku": "a", "qty": 2}, {"sku": "b"}]}, {"oid": 8, "sum": 1.10}],\n'
    '  "tags": ["x", "y"], "active": true, "score": 12345678901234567890, "note": null},\n'
    ' {"id": 2, "name": "Тошкент", "orders": [], "tags": [1, 2.5, null, true], "active": false, "score": 1e400,\n'
    '  "extra": {"deep": {"er": [[1, 2], [3]]}}},\n'
    ' {"id": 3, "later": "new column", "contact": {"address": {"city": null}}, "score": -0.0,\n'
    '  "orders": [{"oid": 9, "items": []}]}]\n'
).encode("utf-8")


def sql_doc(open_doc) -> Document:
    return open_doc(SQL_JSON, name="sql.json")


def test_sqlite_tables_columns_and_relations(open_doc, out_dir: Path, lang_uz) -> None:
    doc = sql_doc(open_doc)
    db = out_dir / "d.sqlite"
    rep = export(doc, db, ExportOptions("sqlite"))
    assert rep.tables == ["records", "orders", "tags", "orders.items", "extra.deep.er", "extra.deep.er[*]"]
    assert rep.records == 3 and rep.files == [db]
    # 12345678901234567890 and 1e400 cannot be SQLite numbers: their exact text is stored
    assert rep.warning_keys == ["writer.warn.sqlite_number_text"] and rep.warnings[0].startswith("2 ")
    assert columns_of(db, "records") == ["_id", "id", "name", "contact.phone", "contact.address.city",
                                         "contact.address.zip", "active", "score", "note", "later"]
    assert columns_of(db, "orders") == ["_id", "_parent", "_idx", "oid", "sum"]
    assert columns_of(db, "orders.items") == ["_id", "_parent", "_idx", "sku", "qty"]
    assert columns_of(db, "tags") == ["_id", "_parent", "_idx", "value"]
    assert columns_of(db, "extra.deep.er[*]") == ["_id", "_parent", "_idx", "value"]
    assert q(db, 'select _id, id, name, "contact.address.city", later from records order by _id') == [
        (0, 1, "Ali", "Toshkent", None), (1, 2, "Тошкент", None, None), (2, 3, None, None, "new column")]
    # storage classes: exact text only for numbers no double/int64 holds
    assert q(db, "select score, typeof(score) from records order by _id") == [
        ("12345678901234567890", "text"), ("1e400", "text"), (-0.0, "real")]
    assert q(db, "select active, typeof(active) from records order by _id") == [(1, "integer"), (0, "integer"),
                                                                               (None, "null")]
    assert q(db, 'select "sum", typeof("sum") from orders order by _id') == [(0.1, "real"), (1.1, "real"),
                                                                             (None, "null")]
    assert q(db, "select value, typeof(value) from tags order by _id") == [
        ("x", "text"), ("y", "text"), (1, "integer"), (2.5, "real"), (None, "null"), (1, "integer")]
    # relations: parent ids and element indices
    assert q(db, "select r.id, o.oid, o._idx from records r join orders o on o._parent = r._id order by o._id") == [
        (1, 7, 0), (1, 8, 1), (3, 9, 0)]
    assert q(db, 'select o.oid, i.sku, i.qty, i._idx from orders o join "orders.items" i on i._parent = o._id '
                 "order by i._id") == [(7, "a", 2, 0), (7, "b", None, 1)]
    assert q(db, 'select e._idx, x.value, x._idx from "extra.deep.er" e join "extra.deep.er[*]" x '
                 "on x._parent = e._id order by x._id") == [(0, 1, 0), (0, 2, 1), (1, 3, 0)]
    indexes = {r[0] for r in q(db, "select tbl_name from sqlite_master where type = 'index'")}
    assert indexes == {"orders", "tags", "orders.items", "extra.deep.er", "extra.deep.er[*]"}


def test_sqlite_keyed_and_selection(open_doc, out_dir: Path, lang_en) -> None:
    recs = [{"v": i, "arr": [i, i + 1]} for i in range(6)]
    doc = open_doc(recs=recs, fmt="keyed", keys=[f"key{i}" for i in range(6)])
    db = out_dir / "k.db"
    rep = export(doc, db, ExportOptions("sqlite"), source=[4, 1, 4, 0])
    # a selection in another order than the file's keeps its order in "_ord" (§14.4, S3)
    assert columns_of(db, "records") == ["_id", "_ord", "$key", "v"]
    assert q(db, 'select _id, "$key", v from records order by _id') == [(0, "key0", 0), (1, "key1", 1),
                                                                        (4, "key4", 4)]
    assert q(db, "select _id from records order by _ord") == [(4,), (1,), (0,)]
    assert q(db, "select _parent, value from arr order by _id") == [(4, 4), (4, 5), (1, 1), (1, 2), (0, 0), (0, 1)]
    assert rep.warning_keys == ["writer.warn.duplicates"] and rep.records == 3
    assert rep.warnings[0].startswith("1 ")


def test_sqlite_name_collisions_and_odd_records(open_doc, out_dir: Path) -> None:
    data = (b'{"Name": 1, "name": 2, "_id": 3, "records": [1], "sqlite_x": [2], "a\\u0000b": 4, "q\\"uote": 5}\n'
            b'7\n"text"\n[1, [2, 3], {"k": 4}]\n{"value": 9}\n')
    doc = open_doc(data, name="odd.jsonl")
    db = out_dir / "o.sqlite"
    rep = export(doc, db, ExportOptions("sqlite"))
    assert columns_of(db, "records") == ["_id", "Name", "name_2", "_id_2", '["a\\u0000b"]', '["q\\"uote"]', "value",
                                         "value_2"]
    assert rep.tables == ["records", "records_2", "_sqlite_x", "value", "value[*]"]
    assert q(db, 'select "_id", Name, name_2, "_id_2", value, value_2 from records order by _id') == [
        (0, 1, 2, 3, None, None), (1, None, None, None, 7, None), (2, None, None, None, "text", None),
        (3, None, None, None, None, None), (4, None, None, None, None, 9)]
    assert q(db, "select _parent, _idx, value, k from value order by _id") == [(3, 0, 1, None), (3, 1, None, None),
                                                                               (3, 2, None, 4)]
    assert q(db, 'select _parent, value from "value[*]" order by _id') == [(2, 2), (2, 3)]
    assert q(db, "select _parent, value from records_2") == [(0, 1)]


def test_sqlite_huge_records_skipped(open_doc, out_dir: Path, lang_en) -> None:
    recs = jsongen.records(15, seed=161)
    doc = open_doc(recs=recs, fmt="array", settings=Settings(large_node_bytes=250))
    huge = [i for i in range(15) if doc.is_huge(i)]
    assert huge
    rep = export(doc, out_dir / "h.sqlite", ExportOptions("sqlite"))
    assert rep.skipped == len(huge) and rep.warning_keys[0] == "writer.warn.huge_skipped_sqlite"
    assert set(rep.warning_keys[1:]) <= {"writer.warn.sqlite_number_text"}
    assert [r[0] for r in q(out_dir / "h.sqlite", "select _id from records order by _id")] == \
        [i for i in range(15) if i not in huge]


def test_sqlite_random_records_round_trip(open_doc, out_dir: Path) -> None:
    """Every scalar leaf of every record is in the database exactly once."""
    recs = jsongen.records(60, seed=171)
    doc = open_doc(recs=recs, fmt="array")
    db = out_dir / "r.sqlite"
    rep = export(doc, db, ExportOptions("sqlite"))
    total_rows = sum(q(db, f"select count(*) from {qi(tb)}")[0][0] for tb in rep.tables)
    assert total_rows == rep.rows
    con = sqlite3.connect(db)
    try:
        for i in (0, 7, 33, 59):
            row = con.execute("select * from records where _id = ?", (i,)).fetchone()
            names = [d[0] for d in con.execute("select * from records limit 0").description]
            got = dict(zip(names, row))
            assert got["id"] == recs[i]["id"] and got["name"] == recs[i]["name"]
            assert got["active"] == int(recs[i]["active"])
            score = recs[i]["score"]
            assert got["score"] == sql_stored(score)[0]
            n_orders = con.execute("select count(*) from orders where _parent = ?", (i,)).fetchone()[0]
            assert n_orders == len(recs[i].get("orders", []))
        # every number score is stored as the reference says (numbers stay numbers where possible)
        for i, rec in enumerate(recs):
            score = rec.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                v, kind = con.execute("select score, typeof(score) from records where _id = ?", (i,)).fetchone()
                assert (v, kind) == sql_stored(score), score
    finally:
        con.close()


# ----------------------------------------------------------------------------------
# atomic output, cancellation, errors, progress
# ----------------------------------------------------------------------------------
ALL_FORMATS = [("json", "o.json"), ("jsonl", "o.jsonl"), ("csv", "o.csv"), ("xlsx", "o.xlsx"),
               ("sqlite", "o.sqlite")]


@pytest.mark.parametrize("fmt, name", ALL_FORMATS)
@pytest.mark.parametrize("when", ["start", "middle"])
def test_cancel_keeps_old_target_and_leaves_no_temp_files(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                         fmt: str, name: str, when: str) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 2000)
    recs = jsongen.records(60, seed=181)
    doc = open_doc(recs=recs, fmt="array")
    target = out_dir / name
    target.write_bytes(b"OLD CONTENT")
    token = CancelToken()

    def cb(info: Any) -> None:
        if when == "start" and info.message.startswith(("Eksport", "Export")):
            token.cancel()
        if when == "middle" and info.done > 3000:
            token.cancel()

    progress = Progress(callback=cb, min_interval=0)
    # "middle": explicit columns, so that the cancel hits the writing pass (automatic ones scan first)
    columns = ["id", "name", "orders[*].oid"] if when == "middle" and fmt in ("csv", "xlsx") else None
    with pytest.raises(Cancelled):
        export(doc, target, ExportOptions(fmt, columns=columns), progress=progress, cancel=token)
    assert target.read_bytes() == b"OLD CONTENT"
    assert files_in(out_dir) == [name]
    tmp = Path(os.environ["JSONRESHAPE_HOME"]) / "tmp"
    assert not tmp.exists() or not [p for p in tmp.iterdir() if p.name.startswith("xlsx-")]


def test_cancel_during_xlsx_files_split(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 500)
    doc = open_doc(recs=[{"i": i, "s": "x" * 50} for i in range(100)], fmt="array")
    token = CancelToken()
    progress = Progress(callback=lambda info: token.cancel() if info.done > 4000 else None, min_interval=0)
    with pytest.raises(Cancelled):
        export(doc, out_dir / "f.xlsx", ExportOptions("xlsx", xlsx_split="files", xlsx_max_rows=10),
               progress=progress, cancel=token)
    assert files_in(out_dir) == []


@pytest.mark.parametrize("fmt, name", ALL_FORMATS)
def test_error_midway_leaves_no_temp_files(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                           fmt: str, name: str) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 1000)
    doc = open_doc(recs=jsongen.records(40, seed=191), fmt="array")
    real = writer._Ctx.advance
    calls = {"n": 0}

    def boom(self: Any, blk: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk on fire")
        real(self, blk)

    monkeypatch.setattr(writer._Ctx, "advance", boom)
    (out_dir / name).write_bytes(b"keep")
    columns = ["id", "name"] if fmt in ("csv", "xlsx") else None   # no scan pass: fail while writing
    with pytest.raises(RuntimeError):
        export(doc, out_dir / name, ExportOptions(fmt, columns=columns))
    assert files_in(out_dir) == [name] and (out_dir / name).read_bytes() == b"keep"


def test_target_checks(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=[{"a": 1}], fmt="array")
    with pytest.raises(UserInputError) as ei:
        export(doc, doc.source_path, ExportOptions("json"))
    assert ei.value.key == "writer.error.same_file"
    with pytest.raises(UserInputError) as ei:
        export(doc, str(doc.source_path).upper() if os.name == "nt" else doc.source_path, ExportOptions("jsonl"))
    assert ei.value.key == "writer.error.same_file"
    with pytest.raises(UserInputError) as ei:
        export(doc, out_dir / "missing" / "x.json", ExportOptions("json"))
    assert ei.value.key == "writer.error.no_dir"
    with pytest.raises(UserInputError) as ei:
        export(doc, out_dir, ExportOptions("json"))
    assert ei.value.key == "fileio.error.is_directory"
    with pytest.raises(TypeError):
        export(doc, out_dir / "x.json", {"fmt": "json"})  # type: ignore[arg-type]
    assert doc.source_path.read_bytes() == b'[{"a":1}]'


def test_progress_and_report(open_doc, out_dir: Path) -> None:
    recs = jsongen.records(50, seed=201)
    src = open_doc(recs=recs, fmt="array")
    path = src.source_path
    doc = Document.open(path, use_cache=False)  # not indexed yet: export builds the index
    try:
        infos: list[Any] = []
        progress = Progress(callback=infos.append, min_interval=0)
        rep = export(doc, out_dir / "p.csv", ExportOptions("csv"), progress=progress)
        assert doc.index_complete
        assert progress.finished and progress.done == progress.total
        assert any(i.message.startswith(("Indekslanmoqda", "Indexing")) for i in infos)
        assert infos[-1].message.startswith(("Eksport", "Export"))
        assert isinstance(rep, ExportReport) and rep.rows == 50 and rep.records == 50
        assert rep.to_dict()["files"] == [str(out_dir / "p.csv")] and rep.mb_per_s >= 0
        # a selection: the total is an estimate, the end is always 100 %
        progress = Progress(min_interval=0)
        export(doc, out_dir / "s.json", ExportOptions("json"), source=[3, 1], progress=progress)
        assert progress.finished and progress.done == progress.total
    finally:
        doc.close()


def test_report_warnings_are_localized(open_doc, out_dir: Path) -> None:
    from jsonreshape import i18n

    doc = open_doc(recs=[{"n": 12345678901234567890}], fmt="array")
    old = i18n.get_language()
    try:
        i18n.set_language("uz")
        rep = export(doc, out_dir / "a.xlsx", ExportOptions("xlsx"))
        assert rep.warnings == [i18n.t("writer.warn.xlsx_number_text", count=1)]
        assert "son" in rep.warnings[0]
        i18n.set_language("en")
        rep = export(doc, out_dir / "a.xlsx", ExportOptions("xlsx"))
        assert rep.warnings == ["1 number cannot be stored exactly in Excel (more than 15 digits, or too large/small),"
                                " so it was written as text."]
    finally:
        i18n.set_language(old)


def test_export_error_messages_are_localized(lang_uz) -> None:
    with pytest.raises(JsonReshapeError) as ei:
        ExportOptions("csv", csv_delimiter="ab")
    assert "Eksport sozlamasi" in str(ei.value)


# ----------------------------------------------------------------------------------
# robustness: property test, prepared sources, lone surrogates, empty documents
# ----------------------------------------------------------------------------------
_KEY_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=6)
_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.sampled_from(jsongen.EDGE_NUMBER_TOKENS).map(codec.loads),
    st.integers(min_value=-2**70, max_value=2**70),
    st.floats(allow_nan=False, allow_infinity=False),
    st.sampled_from(jsongen.EDGE_STRINGS),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=10),
)
_VALUES = st.recursive(
    _SCALARS,
    lambda children: st.one_of(st.lists(children, max_size=4), st.dictionaries(_KEY_TEXT, children, max_size=4)),
    max_leaves=12,
)


@hsettings(deadline=None, max_examples=40, derandomize=True, database=None,
           suppress_health_check=[HealthCheck.too_slow])
@given(recs=st.lists(_VALUES, max_size=8), indent=st.sampled_from([None, 2, 4, "\t", 0]),
       src_indent=st.sampled_from([None, 2, "\t"]))
def test_property_json_and_jsonl_outputs(tmp_path_factory: pytest.TempPathFactory, recs: list, indent: Any,
                                          src_indent: Any) -> None:
    d = tmp_path_factory.mktemp("prop")
    src = d / "src.json"
    src.write_bytes(codec.dumps(recs, indent=src_indent))
    with Document.open(src, format="array") as doc:
        doc.build_index()
        export(doc, d / "o.json", ExportOptions("json", indent=indent))
        assert (d / "o.json").read_bytes() == codec.dumps(recs, indent=indent)
        export(doc, d / "o.jsonl", ExportOptions("jsonl"))
        assert (d / "o.jsonl").read_bytes() == b"".join(codec.dumps(r) + b"\n" for r in recs)
        export(doc, d / "s.json", ExportOptions("json", indent=indent, sort_keys="codepoint"))
        assert (d / "s.json").read_bytes() == codec.dumps(recs, indent=indent, sort_keys=True)
        export(doc, d / "a.json", ExportOptions("json", indent=indent, sort_keys="alpha"))
        assert (d / "a.json").read_bytes() == codec.dumps(alpha_sorted(recs), indent=indent)


@pytest.mark.parametrize("variant", ["bom", "gz", "utf16", "utf16bom"])
@pytest.mark.parametrize("fmt", ["array", "wrapped"])
def test_prepared_sources(open_doc, out_dir: Path, variant: str, fmt: str) -> None:
    """UTF-8 BOM, gzip and UTF-16 sources: the data file is read, never the raw bytes."""
    recs = jsongen.records(15, seed=7)
    kw = {"bom": variant in ("bom", "utf16bom"), "gz": variant == "gz",
          "encoding": "utf-16-le" if variant.startswith("utf16") else "utf-8"}
    doc = open_doc(recs=recs, fmt=fmt, style="pretty2", **kw)
    assert doc.kind == fmt
    whole = codec.loads(jsongen.build_document(recs, fmt, style="pretty2"))
    export(doc, out_dir / "o.json", ExportOptions("json", indent=2))
    assert (out_dir / "o.json").read_bytes() == dumps(whole, indent=2)
    export(doc, out_dir / "o.jsonl", ExportOptions("jsonl"))
    assert (out_dir / "o.jsonl").read_bytes() == b"".join(dumps(r) + b"\n" for r in recs)


def test_lone_surrogate_escapes_in_every_format(open_doc, out_dir: Path) -> None:
    """Lone surrogate escapes (valid JSON, not encodable as UTF-8) never crash an export:
    JSON keeps the escape tokens, the other formats write the escape as text."""
    bs = chr(92)
    e800, edc00, ed83d = bs + "ud800", bs + "udc00", bs + "ud83d"   # the escape texts (ASCII)
    src = '[{"s": "a' + e800 + 'b", "k' + edc00 + '": 1, "arr": ["' + ed83d + '", "ok"]}]'
    doc = open_doc(src.encode("ascii"), name="sur.json")
    export(doc, out_dir / "o.json", ExportOptions("json", indent=None))
    want = '[{"s":"a' + e800 + 'b","k' + edc00 + '":1,"arr":["' + ed83d + '","ok"]}]'
    assert (out_dir / "o.json").read_bytes() == want.encode("ascii")
    export(doc, out_dir / "s.jsonl", ExportOptions("jsonl", sort_keys=True))
    want = '{"arr":["' + ed83d + '","ok"],"k' + edc00 + '":1,"s":"a' + e800 + 'b"}' + chr(10)
    assert (out_dir / "s.jsonl").read_bytes() == want.encode("ascii")
    export(doc, out_dir / "o.csv", ExportOptions("csv"))
    assert read_csv(out_dir / "o.csv") == [["s", "k" + edc00, "arr"], ["a" + e800 + "b", "1", ed83d + ";ok"]]
    export(doc, out_dir / "o.xlsx", ExportOptions("xlsx"))
    assert sheet_rows(openpyxl.load_workbook(out_dir / "o.xlsx").active) == [
        ["s", "k" + edc00, "arr"], ["a" + e800 + "b", 1, ed83d + ";ok"]]
    export(doc, out_dir / "o.sqlite", ExportOptions("sqlite"))
    db = out_dir / "o.sqlite"
    assert columns_of(db, "records") == ["_id", "s", "k" + edc00]
    assert q(db, f"select s, {qi('k' + edc00)} from records") == [("a" + e800 + "b", 1)]
    assert q(db, "select value from arr order by _id") == [(ed83d,), ("ok",)]


def test_empty_documents(open_doc, out_dir: Path) -> None:
    doc = open_doc(b"[ ]\n", name="empty.json")
    assert doc.record_count == 0
    for fmt, name, want in (("json", "e.json", b"[]"), ("jsonl", "e.jsonl", b""), ("csv", "e.csv", BOM)):
        rep = export(doc, out_dir / name, ExportOptions(fmt))
        assert (out_dir / name).read_bytes() == want and rep.records == rep.rows == 0
    rep = export(doc, out_dir / "e.xlsx", ExportOptions("xlsx"))
    assert sheet_rows(openpyxl.load_workbook(out_dir / "e.xlsx").active) in ([], [[None]]) and rep.rows == 0
    rep = export(doc, out_dir / "e.sqlite", ExportOptions("sqlite"))
    assert rep.tables == ["records"] and q(out_dir / "e.sqlite", "select count(*) from records") == [(0,)]
    keyed = open_doc(b"{}", name="ek.json", format="keyed")
    assert keyed.kind == "keyed" and keyed.record_count == 0
    export(keyed, out_dir / "k.json", ExportOptions("json"))
    assert (out_dir / "k.json").read_bytes() == b"{}"
    wrapped = open_doc(b'{"meta": 1, "data": [], "tail": 2}', name="ew.json", record_path="data")
    export(wrapped, out_dir / "w.json", ExportOptions("json", indent=None))
    assert (out_dir / "w.json").read_bytes() == b'{"meta":1,"data":[],"tail":2}'


def test_tiny_feed_pieces_split_tokens(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocks, huge records and wrapper parts fed in pieces that cut strings, escapes and
    numbers give the same output."""
    recs = jsongen.records(25, seed=221)
    for fmt in ("array", "wrapped", "keyed"):
        for settings in (None, Settings(large_node_bytes=200)):
            doc = open_doc(recs=recs, fmt=fmt, style="escaped", settings=settings)
            whole = codec.loads(doc.source_path.read_bytes())
            for indent in (None, 2, "\t"):
                for opts, source in ((ExportOptions("json", indent=indent), None),
                                     (ExportOptions("json", indent=indent), [3, 1, 2]),
                                     (ExportOptions("jsonl"), None)):
                    export(doc, out_dir / "ref.out", opts, source=source)
                    with monkeypatch.context() as m:
                        m.setattr(writer, "FEED_BYTES", 7)
                        m.setattr(writer, "CHUNK_BYTES", 5)
                        export(doc, out_dir / "tiny.out", opts, source=source)
                    assert (out_dir / "tiny.out").read_bytes() == (out_dir / "ref.out").read_bytes()
                export(doc, out_dir / "t.json", ExportOptions("json", indent=indent))
                assert dumps(codec.loads((out_dir / "t.json").read_bytes()), indent=indent) == \
                    dumps(whole, indent=indent)


@pytest.mark.parametrize("source", [range(19, -1, -3), range(0, 20, 4)])
def test_stepped_ranges(open_doc, out_dir: Path, source: range) -> None:
    recs = jsongen.records(20, seed=211)
    doc = open_doc(recs=recs, fmt="array")
    export(doc, out_dir / "r.json", ExportOptions("json", indent=None), source=source)
    assert (out_dir / "r.json").read_bytes() == dumps([recs[i] for i in source])


def test_base_set_of_the_live_document_means_all_records_of_a_pinned_state(open_doc, out_dir: Path,
                                                                           tmp_path: Path) -> None:
    """The base RecordSet of the live document follows the live record count; exported with a pinned view
    of another state it stands for "all records" of THAT state (docs/ARCHITECTURE.md §12.3), never for the
    first ``len(base set)`` of them."""
    from jsonreshape.recordset import RecordSet
    from overlay_helpers import OverlayBuilder

    recs = jsongen.records(12, seed=301)
    doc = open_doc(recs=recs, fmt="array")
    base_set = RecordSet.base(doc)
    file_view = doc.pinned(None)                       # the file as it is: 12 records
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.delete(0, 1, 2)                                 # the live document now shows 9 records
    doc.set_overlay(ob.state())
    assert len(base_set) == 9
    export(file_view, out_dir / "all.json", ExportOptions("json", indent=None), source=base_set)
    assert (out_dir / "all.json").read_bytes() == dumps(recs)
    export(doc, out_dir / "live.json", ExportOptions("json", indent=None), source=base_set)
    assert (out_dir / "live.json").read_bytes() == dumps(recs[3:])
    ob2 = OverlayBuilder(doc, tmp_path / "layer2")
    ob2.insert(12, b'{"new": 1}')                      # a longer state, pinned before the live one changes
    longer = doc.pinned(ob2.state())
    report = export(longer, out_dir / "longer.json", ExportOptions("json", indent=None), source=base_set)
    assert report.records == 13 and (out_dir / "longer.json").read_bytes() == dumps([*recs, {"new": 1}])
