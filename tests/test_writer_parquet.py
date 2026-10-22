"""jsonreshape.writer_parquet: the Parquet export (docs/ARCHITECTURE.md §8, stage 7).

* columns and rows exactly as CSV (automatic and explicit columns, wildcards, ``$key``/``$index``, join
  and rows mode, ``explode``): the values read back with pyarrow and compared cell for cell with the
  CSV export and with the source;
* column types: ``int64`` / ``double`` (exact doubles only) / ``bool`` / ``string``; mixed columns and
  numbers no type keeps exactly as their exact text (``writer.warn.parquet_number_text``); all-null
  columns; arrays and objects as text;
* the type decision from the first rows and the two-pass restart on a later conflict: the same bytes as
  the one-pass file, the counters / warnings / progress of the dropped attempt;
* worker processes give the same file as one thread (real processes on small blocks; the 70 MB file, slow);
* compression, row groups, atomic output and cancellation, huge records, ``.parquet.gz`` refused,
  ``plan_columns`` / ``export_many``, pyarrow missing;
* the CLI: ``convert in.json out.parquet``, ``--to parquet``, ``--parquet-compression``, the option
  compatibility warnings.

Every test is skipped when pyarrow is not installed.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import cli, codec, writer
from jsonreshape import writer_parquet as wp
from jsonreshape.errors import Cancelled, ExportError, UserInputError
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export, format_for_path, plan_columns
from test_writer import files_in, open_doc, out_dir, read_csv, ref_rows  # noqa: F401 - fixtures and references

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

SCALARS = ["id", "name", "score", "active", "contact.phone", "contact.address.city", "contact.address.zip"]
SPECIAL = ["$index", "orders[*].oid", "tags", "contact", "note", "nope.never", "extra.**", "orders[0].sum"]

# one record per line; every kind of value and number (exact tokens: never through float)
TYPES_JSON = b"""[
{"i": 1, "big": 12345678901234567890, "neg0": -0, "f": 0.1, "f2": 1.10, "e": 1E3, "pi": 3.141592653589793,
 "inexact": 123456789.123456789, "huge": 1e400, "b": true, "mix": 1, "mixb": true, "nul": null,
 "arr": [1, "a", {"k": 2}], "obj": {"k": [1, 2]}, "s": "x", "if": 1, "iw": 1, "nz": -0.0, "e2": 1},
{"i": -2, "big": 2, "neg0": 5, "f": 2.5, "f2": 2, "e": 1e-3, "pi": 1, "inexact": 1.5, "huge": 1, "b": false,
 "mix": "two", "mixb": 1, "nul": null, "arr": [], "obj": {}, "s": null, "if": 2.5, "iw": 9007199254740993,
 "nz": 0.25, "e2": 2},
{"i": null, "big": null, "neg0": null, "f": null, "f2": null, "e": null, "pi": null, "inexact": null, "huge": null,
 "b": null, "mix": null, "mixb": null, "nul": null, "arr": null, "obj": null, "s": "y", "if": null, "iw": 2.5,
 "nz": null, "e2": 3}
]"""


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def table(path: Path) -> Any:
    return pq.read_table(path)


def kinds(path: Path) -> dict[str, str]:
    """``{column: int64|double|bool|string}`` of a Parquet file."""
    names = {"int64": "int64", "double": "double", "bool": "bool", "string": "string", "large_string": "string"}
    schema = pq.read_schema(path)
    return {f.name: names[str(f.type)] for f in schema}


def columns(path: Path) -> dict[str, list[Any]]:
    t = table(path)
    return {name: t.column(name).to_pylist() for name in t.column_names}


def check_against_csv(pq_path: Path, csv_path: Path) -> None:
    """Every Parquet value equals the CSV cell of the same row and column: strings as they are, numbers by
    value (the CSV text is exact), booleans ``true``/``false``, null the empty cell."""
    t = table(pq_path)
    rows = read_csv(csv_path)
    header, data = rows[0], rows[1:]
    assert t.column_names == header
    assert t.num_rows == len(data)
    for j, name in enumerate(header):
        typ = t.schema.field(j).type
        for v, text in zip(t.column(j).to_pylist(), (r[j] for r in data)):
            if v is None:
                assert text == "", (name, text)
            elif pa.types.is_string(typ) or pa.types.is_large_string(typ):
                assert v == text, (name, v, text)
            elif pa.types.is_boolean(typ):
                assert text == ("true" if v else "false"), (name, v, text)
            elif pa.types.is_int64(typ):
                assert int(text) == v and "." not in text and "e" not in text.lower(), (name, v, text)
            else:
                assert pa.types.is_float64(typ) and Decimal(text) == Decimal(repr(v)), (name, v, text)


def export_both(doc: Document, out: Path, **kw: Any) -> tuple[writer.ExportReport, writer.ExportReport]:
    """The Parquet export and the CSV export with the same table options; the Parquet values are checked
    against the CSV cells."""
    rp = export(doc, out.with_suffix(".parquet"), ExportOptions("parquet", **kw), workers=1)
    rc = export(doc, out.with_suffix(".csv"), ExportOptions("csv", **kw), workers=1)
    check_against_csv(out.with_suffix(".parquet"), out.with_suffix(".csv"))
    assert (rp.records, rp.rows, rp.columns, rp.skipped) == (rc.records, rc.rows, rc.columns, rc.skipped)
    assert rp.fmt == "parquet" and rp.files == [out.with_suffix(".parquet")] and rp.bytes_written > 0
    return rp, rc


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]):
    def _run(*argv: Any) -> tuple[int, str, str]:
        code = cli.main([str(a) for a in argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


@pytest.fixture
def small_types(monkeypatch: pytest.MonkeyPatch):
    """The type decision from the first 50 rows (so a small file reaches the two-pass restart)."""
    monkeypatch.setattr(wp, "TYPE_ROWS", 50)
    monkeypatch.setattr(wp, "TYPE_MIN_ROWS", 50)
    return 50


# ----------------------------------------------------------------------------------
# registration and options
# ----------------------------------------------------------------------------------
def test_format_is_registered() -> None:
    assert "parquet" in writer.FORMATS and writer.FORMAT_NAMES["parquet"] == "Parquet"
    assert writer.TABLE_FORMATS == ("csv", "xlsx", "parquet", "feather", "arrow") and "parquet" not in writer.GZIP_FORMATS
    assert format_for_path("x.parquet") == "parquet" and format_for_path("X.PARQUET") == "parquet"
    assert format_for_path("x.parquet.gz") is None
    assert writer.PARQUET_COMPRESSIONS == wp.PARQUET_COMPRESSIONS == ("zstd", "snappy", "gzip", "none")
    assert set(cli.OUTPUT_FORMATS) == set(writer.FORMATS) and cli.PARQUET_COMPRESSIONS == wp.PARQUET_COMPRESSIONS
    assert wp.pyarrow_available()


def test_options(lang_en) -> None:
    assert ExportOptions("parquet").parquet_compression == "zstd"
    assert ExportOptions("PARQUET", parquet_compression=" Snappy ").parquet_compression == "snappy"
    for bad in ("lz4", "", None, 1):
        with pytest.raises(UserInputError) as info:
            ExportOptions("parquet", parquet_compression=bad)
        assert info.value.key == "writer.invalid_option" and "zstd, snappy, gzip or none" in info.value.message
    with pytest.raises(UserInputError) as info:
        ExportOptions("parquet", as_keyed=True)
    assert "parquet" in info.value.message
    assert ExportOptions.from_settings("parquet").parquet_compression == "zstd"
    o = ExportOptions("csv")
    o.parquet_compression = "none"
    o.validate()
    assert o.parquet_compression == "none"


def test_gzip_names_are_refused(open_doc, out_dir: Path, lang_en) -> None:
    doc = open_doc(recs=[{"a": 1}], fmt="array")
    with pytest.raises(UserInputError) as info:
        export(doc, out_dir / "o.parquet.gz", ExportOptions("parquet"))
    assert info.value.key == "writer.error.gzip_format" and "Parquet" in info.value.message
    with pytest.raises(UserInputError):
        export(doc, out_dir / "o.parquet", ExportOptions("parquet"), gzip=True)
    assert files_in(out_dir) == []


# ----------------------------------------------------------------------------------
# the same table as CSV
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    {},
    {"columns": SCALARS},
    {"columns": SCALARS + SPECIAL},
    {"array_mode": "rows"},
    {"explode": "orders"},
    {"columns": ["id", "orders[*].oid", "orders[*].sum", "name", "$index"], "array_mode": "rows"},
    {"columns": SCALARS, "join_sep": " | "},
])
def test_same_columns_and_cells_as_csv(open_doc, out_dir: Path, kw: dict) -> None:
    recs = jsongen.records(80, seed=7)
    doc = open_doc(recs=recs, fmt="array")
    rp, _rc = export_both(doc, out_dir / "o", **kw)
    assert rp.records == 80
    if kw.get("columns") == SCALARS:
        data = columns(out_dir / "o.parquet")
        assert data["id"] == [r["id"] for r in recs]
        want = ref_rows(recs, SCALARS)
        assert [str(v) for v in data["id"]] == [row[0] for row in want]


@pytest.mark.parametrize("kind", ["jsonl", "keyed", "wrapped", "brace_list", "single", "stream"])
def test_every_document_kind(open_doc, out_dir: Path, kind: str) -> None:
    recs = jsongen.records(30, seed=11)
    kw: dict[str, Any] = {}
    if kind == "keyed":
        kw["keys"] = [f"k{i}" for i in range(30)]
    if kind == "wrapped":
        kw["record_path"] = "data"
    doc = open_doc(recs=recs, fmt=kind, **kw)
    rp, _ = export_both(doc, out_dir / "o")
    if kind == "keyed":
        assert rp.columns[0] == "$key" and kinds(out_dir / "o.parquet")["$key"] == "string"
        assert columns(out_dir / "o.parquet")["$key"] == [f"k{i}" for i in range(30)]
    if kind == "wrapped":
        assert "writer.note.wrapper_not_written" in rp.warning_keys and "Parquet" in rp.warnings[0]


def test_selection_order_and_index(open_doc, out_dir: Path) -> None:
    recs = jsongen.records(40, seed=12)
    doc = open_doc(recs=recs, fmt="array")
    sel = [30, 5, 5, 17]
    rp = export(doc, out_dir / "s.parquet", ExportOptions("parquet", columns=["$index", "id"]), source=sel)
    assert rp.records == 4 and columns(out_dir / "s.parquet") == {"$index": sel, "id": [recs[i]["id"] for i in sel]}
    assert kinds(out_dir / "s.parquet") == {"$index": "int64", "id": "int64"}


# ----------------------------------------------------------------------------------
# types
# ----------------------------------------------------------------------------------
def test_types_of_every_kind_of_value(open_doc, out_dir: Path, lang_en) -> None:
    doc = open_doc(data=TYPES_JSON)
    out = out_dir / "t.parquet"
    rep = export(doc, out, ExportOptions("parquet"))
    assert kinds(out) == {
        "i": "int64", "big": "string", "neg0": "int64", "f": "double", "f2": "double", "e": "double", "pi": "double",
        "inexact": "string", "huge": "string", "b": "bool", "mix": "string", "mixb": "string", "nul": "string",
        "arr": "string", "obj.k": "string", "s": "string", "if": "double", "iw": "string", "nz": "double",
        "e2": "int64", "obj": "string",       # (``obj``: the automatic column of the ``{}`` and ``null`` values)
    }
    data = columns(out)
    assert data["i"] == [1, -2, None] and data["neg0"] == [0, 5, None] and data["e2"] == [1, 2, 3]
    assert data["big"] == ["12345678901234567890", "2", None]            # digits never lost
    assert data["f"] == [0.1, 2.5, None] and data["f2"] == [1.1, 2.0, None] and data["e"] == [1000.0, 0.001, None]
    assert data["pi"] == [3.141592653589793, 1.0, None] and data["nz"] == [-0.0, 0.25, None]
    assert str(data["nz"][0]) == "-0.0"
    assert data["inexact"] == ["123456789.123456789", "1.5", None] and data["huge"] == ["1e400", "1", None]
    assert data["b"] == [True, False, None]
    assert data["mix"] == ["1", "two", None] and data["mixb"] == ["true", "1", None]
    assert data["nul"] == [None, None, None]
    assert data["arr"] == ['1;a;{"k":2}', "", None] and data["obj"] == ['{"k":[1,2]}', "{}", None]
    assert data["obj.k"] == ["1;2", None, None]
    assert data["s"] == ["x", None, "y"]
    assert data["if"] == [1.0, 2.5, None] and data["iw"] == ["1", "9007199254740993", "2.5"]
    assert rep.warning_keys == ["writer.warn.parquet_number_text"]
    assert rep.warning_params[0]["names"] == ["big", "inexact", "huge", "iw"] and rep.warning_params[0]["count"] == 4
    assert "4 columns hold numbers" in rep.warnings[0] and "big, inexact, huge, iw" in rep.warnings[0]
    # the same cells as CSV (numbers compared by value)
    export(doc, out_dir / "t.csv", ExportOptions("csv"))
    check_against_csv(out, out_dir / "t.csv")


def test_one_number_column_as_text_and_big_ints_alone(open_doc, out_dir: Path, lang_en) -> None:
    doc = open_doc(data=b'[{"n": 1e400}, {"n": 2}]')
    rep = export(doc, out_dir / "n.parquet", ExportOptions("parquet"))
    assert columns(out_dir / "n.parquet") == {"n": ["1e400", "2"]}
    assert rep.warnings[0].startswith("1 column holds numbers") and "it was written as text: n" in rep.warnings[0]
    doc2 = open_doc(data=b'[{"n": 9223372036854775807, "m": -9223372036854775808, "u": 18446744073709551615}]')
    export(doc2, out_dir / "r.parquet", ExportOptions("parquet"))
    assert kinds(out_dir / "r.parquet") == {"n": "int64", "m": "int64", "u": "string"}
    assert columns(out_dir / "r.parquet") == {"n": [2**63 - 1], "m": [-2**63], "u": ["18446744073709551615"]}


def test_decision_rules() -> None:
    def decide(values: list) -> tuple[str, bool, bool]:
        ct = wp._ColumnTypes()
        ct.add(values)
        return ct.decide()

    JF, JI = codec.JsonFloat, codec.JsonInt
    assert decide([None, None]) == ("string", True, False)
    assert decide([1, None, JI("-0")]) == ("int64", False, False)
    assert decide([1, JI("99999999999999999999")]) == ("string", False, True)
    assert decide([0.5, 1, JF("1.10"), JF("1E3")]) == ("double", False, False)
    assert decide([0.5, 2**53 + 1]) == ("string", False, True)
    assert decide([0.5, 2**53]) == ("double", False, False)
    assert decide([JF("1e400")]) == ("string", False, True)
    assert decide([JF("123456789.123456789"), 1]) == ("string", False, True)
    assert decide([True, None]) == ("bool", False, False)
    assert decide([True, 1]) == ("string", False, False)
    assert decide(["a", 1, 2.5, True]) == ("string", False, False)
    assert decide([Decimal("1.5"), 1]) == ("double", False, False)
    assert decide([Decimal("1.0000000000000000001")]) == ("string", False, True)
    # what fits a fixed type
    assert wp._misfit([1, None], {int, type(None)}, "int64", False) == -1
    assert wp._misfit([1, 2.5], {int, float}, "int64", False) == 1
    assert wp._misfit([1, JI("99999999999999999999")], {int, JI}, "int64", False) == 1
    assert wp._misfit([1.5, 3, None], {float, int, type(None)}, "double", False) == -1
    assert wp._misfit([1.5, 2**60], {float, int}, "double", False) == 1
    assert wp._misfit([JF("1.10"), JF("1e400")], {JF}, "double", False) == 1
    assert wp._misfit([True, None, "x"], {bool, type(None), str}, "bool", False) == 2
    assert wp._misfit([None, "x", 1], {type(None), str, int}, "string", True) == 2
    assert wp._misfit([None, "x", 1], {type(None), str, int}, "string", False) == -1


# ----------------------------------------------------------------------------------
# the type decision from the first rows; the two-pass restart
# ----------------------------------------------------------------------------------
def conflict_records(n: int = 300) -> list[dict[str, Any]]:
    return [{"id": i, "price": 2.5 if i == 250 else i, "late": None if i < 150 else i,
             "flag": True if i < 120 else "x", "huge": 2**70 if i == 280 else i, "s": str(i),
             "arr": [i, i + 1] if i % 2 else []} for i in range(n)]


def test_conflict_restarts_in_two_passes(open_doc, out_dir: Path, small_types: int, lang_en) -> None:
    recs = conflict_records()
    doc = open_doc(recs=recs, fmt="array")
    seen: list[tuple[int, Any, str]] = []
    progress = Progress(callback=lambda info: seen.append((info.done, info.total, info.message)), min_interval=0)
    cols = ["id", "price", "late", "flag", "huge", "s", "arr", "nope"]
    rep = export(doc, out_dir / "c.parquet", ExportOptions("parquet", columns=cols), progress=progress, workers=1)
    assert kinds(out_dir / "c.parquet") == {"id": "int64", "price": "double", "late": "int64", "flag": "string",
                                            "huge": "string", "s": "string", "arr": "string", "nope": "string"}
    data = columns(out_dir / "c.parquet")
    assert data["price"][250] == 2.5 and data["price"][0] == 0.0 and data["late"][:2] == [None, None]
    assert data["late"][150] == 150 and data["flag"][119] == "true" and data["flag"][120] == "x"
    assert data["huge"][280] == str(2**70) and data["huge"][0] == "0" and data["arr"][:2] == ["", "1;2"]
    # counted once: the dropped attempt leaves no trace in the counters and warnings
    assert rep.records == rep.rows == 300 and rep.skipped == 0
    assert rep.warning_keys == ["writer.warn.parquet_restarted", "writer.warn.parquet_number_text",
                                "writer.warn.columns_not_found"]
    params = dict(zip(rep.warning_keys, rep.warning_params))
    assert params["writer.warn.columns_not_found"]["count"] == 1 and params["writer.warn.parquet_number_text"]["names"] == ["huge"]
    restarted = params["writer.warn.parquet_restarted"]
    # the first record that does not fit: "flag" turns to text at #120 (before "late" #150 and "price" #250)
    assert restarted["rows"] == 50 and restarted["column"] == "flag" and restarted["index"] == 120
    assert "Column flag" in rep.warnings[0] and "two passes" in rep.warnings[0] and "first 50 rows" in rep.warnings[0]
    # progress: monotone, complete, three passes (the attempt, the types, the writing)
    dones = [d for d, _t, _m in seen]
    assert dones == sorted(dones) and seen[-1][0] == seen[-1][1] > 0
    assert seen[-1][1] == 3 * seen[0][1]
    assert any(m.startswith("Deciding the column types") for _d, _t, m in seen)
    # the same bytes as the file written without a restart (the decision from all rows)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wp, "TYPE_ROWS", 65536)
        mp.setattr(wp, "TYPE_MIN_ROWS", 1024)
        rep2 = export(doc, out_dir / "all.parquet", ExportOptions("parquet", columns=cols), workers=1)
    assert rep2.warning_keys == ["writer.warn.parquet_number_text", "writer.warn.columns_not_found"]
    assert (out_dir / "all.parquet").read_bytes() == (out_dir / "c.parquet").read_bytes()
    assert files_in(out_dir) == ["all.parquet", "c.parquet"]            # no temporary file of the attempt


def test_conflicts_of_every_kind_restart(open_doc, out_dir: Path, small_types: int) -> None:
    """A conflict in every typed column kind, also with automatic columns and in rows mode."""
    cases = {
        "int_then_float": ([{"v": i} for i in range(60)] + [{"v": 0.5}], "double", [0.5], True),
        "int_then_string": ([{"v": i} for i in range(60)] + [{"v": "x"}], "string", ["x"], True),
        "bool_then_int": ([{"v": True} for _ in range(60)] + [{"v": 1}], "string", ["1"], True),
        "null_then_int": ([{"v": None} for _ in range(60)] + [{"v": 7}], "int64", [7], True),
        "null_then_string": ([{"v": None} for _ in range(60)] + [{"v": "s"}], "string", ["s"], False),
        "int_then_big": ([{"v": i} for i in range(60)] + [{"v": 2**64}], "string", [str(2**64)], True),
        "double_then_wide": ([{"v": 0.5} for _ in range(60)] + [{"v": 2**60}], "string", [str(2**60)], True),
        "double_then_inexact": ([{"v": 0.5} for _ in range(60)], "string", ["123456789.123456789"], True),
        "double_then_int": ([{"v": 0.5} for _ in range(60)] + [{"v": 3}], "double", [3.0], False),
    }
    for name, (recs, kind, tail, restarted) in cases.items():
        data = json.dumps(recs).encode()
        if name == "double_then_inexact":
            data = data[:-1] + b', {"v": 123456789.123456789}]'
        doc = open_doc(data=data, name=f"{name}.json")
        out = out_dir / f"{name}.parquet"
        rep = export(doc, out, ExportOptions("parquet"))
        assert kinds(out) == {"v": kind}, name
        assert columns(out)["v"][-len(tail):] == tail, name
        assert rep.records == 61 and ("writer.warn.parquet_restarted" in rep.warning_keys) == restarted, name
        if restarted:
            assert dict(zip(rep.warning_keys, rep.warning_params))["writer.warn.parquet_restarted"]["index"] == 60
    # rows mode: the conflict in an element's column
    recs = [{"id": i, "items": [{"k": i}, {"k": i + 1}]} for i in range(40)] + [{"id": 40, "items": [{"k": "z"}]}]
    doc = open_doc(recs=recs, fmt="array")
    rep = export(doc, out_dir / "rows.parquet", ExportOptions("parquet", explode="items"))
    assert kinds(out_dir / "rows.parquet") == {"id": "int64", "items[*].k": "string"} and rep.rows == 81
    assert columns(out_dir / "rows.parquet")["items[*].k"][-2:] == ["40", "z"]
    assert dict(zip(rep.warning_keys, rep.warning_params))["writer.warn.parquet_restarted"]["index"] == 40


def test_no_restart_when_the_prefix_decides_string_or_the_values_fit(open_doc, out_dir: Path, small_types: int) -> None:
    recs = [{"a": "s" if i == 0 else i, "b": 1.5 if i == 0 else i, "c": None if i % 2 else i} for i in range(80)]
    doc = open_doc(recs=recs, fmt="array")
    rep = export(doc, out_dir / "p.parquet", ExportOptions("parquet"))
    assert rep.warning_keys == [] and kinds(out_dir / "p.parquet") == {"a": "string", "b": "double", "c": "int64"}
    assert columns(out_dir / "p.parquet")["a"][:3] == ["s", "1", "2"]


# ----------------------------------------------------------------------------------
# worker processes
# ----------------------------------------------------------------------------------
def same_with_workers(doc: Document, opts: ExportOptions, out_dir: Path, workers: int = 2) -> writer.ExportReport:
    one = export(doc, out_dir / "one.parquet", opts, workers=1)
    many = export(doc, out_dir / "many.parquet", opts, workers=workers)
    assert (out_dir / "many.parquet").read_bytes() == (out_dir / "one.parquet").read_bytes()
    assert (many.records, many.rows, many.skipped, many.columns, many.warnings, many.warning_params) == \
        (one.records, one.rows, one.skipped, one.columns, one.warnings, one.warning_params)
    assert one.workers == 1
    return many


def test_real_workers_give_the_same_file(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1500)
    doc = open_doc(recs=jsongen.records(90, seed=21), fmt="array")
    rep = same_with_workers(doc, ExportOptions("parquet", columns=SCALARS + SPECIAL), out_dir)
    assert rep.workers == 2
    rep = same_with_workers(doc, ExportOptions("parquet", explode="orders"), out_dir)
    assert rep.workers == 2


def test_real_workers_with_a_restart(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, small_types: int) -> None:
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1200)
    doc = open_doc(recs=conflict_records(200), fmt="array")
    rep = same_with_workers(doc, ExportOptions("parquet"), out_dir)
    assert rep.workers == 2 and "writer.warn.parquet_restarted" in rep.warning_keys
    assert kinds(out_dir / "many.parquet")["late"] == "int64" and kinds(out_dir / "many.parquet")["flag"] == "string"


@pytest.mark.slow
def test_real_workers_on_a_70_mb_file(tmp_path: Path) -> None:
    """workers=1 and workers=3 write byte for byte the same file (the automatic workers would start at 64 MiB)."""
    src = tmp_path / "big.json"
    with open(src, "wb") as f:
        f.write(b"[")
        n = 0
        while f.tell() < 70 * 1024 * 1024:
            for r in jsongen.records(2000, seed=n):
                f.write((b",\n" if n else b"") + jsongen.encode(r, "compact", n))
                n += 1
        f.write(b"]")
    doc = Document.open(src, use_cache=False)
    try:
        doc.build_index()
        for opts in (ExportOptions("parquet"), ExportOptions("parquet", columns=SCALARS + ["orders[*].oid", "tags"])):
            r1 = export(doc, tmp_path / "one.parquet", opts, workers=1)
            r3 = export(doc, tmp_path / "three.parquet", opts, workers=3)
            assert (tmp_path / "three.parquet").read_bytes() == (tmp_path / "one.parquet").read_bytes()
            assert r1.workers == 1 and r3.workers == 3 and r3.rows == r1.rows == n
            assert pq.ParquetFile(tmp_path / "one.parquet").metadata.num_rows == n
    finally:
        doc.close()


# ----------------------------------------------------------------------------------
# compression, row groups, atomic output, cancellation, odd records
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("compression, stored", [("zstd", "ZSTD"), ("snappy", "SNAPPY"), ("gzip", "GZIP"),
                                                 ("none", "UNCOMPRESSED")])
def test_compression(open_doc, out_dir: Path, compression: str, stored: str) -> None:
    doc = open_doc(recs=jsongen.records(20, seed=3), fmt="array")
    out = out_dir / f"{compression}.parquet"
    export(doc, out, ExportOptions("parquet", columns=["id", "name"], parquet_compression=compression))
    md = pq.ParquetFile(out).metadata
    assert md.row_group(0).column(0).compression == stored and md.num_rows == 20
    assert columns(out)["id"] == [r["id"] for r in jsongen.records(20, seed=3)]


def test_row_groups(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wp, "ROW_GROUP_BYTES", 3000)
    monkeypatch.setattr(wp, "ROW_GROUP_MIN_ROWS", 10)
    monkeypatch.setattr(wp, "TYPE_ROWS", 25)
    monkeypatch.setattr(wp, "TYPE_MIN_ROWS", 25)
    recs = [{"id": i, "text": "x" * 100} for i in range(500)]
    doc = open_doc(recs=recs, fmt="array")
    out = out_dir / "g.parquet"
    rep = export(doc, out, ExportOptions("parquet"), workers=1)
    md = pq.ParquetFile(out).metadata
    assert md.num_rows == 500 and md.num_row_groups > 3
    sizes = {md.row_group(i).num_rows for i in range(md.num_row_groups - 1)}
    assert len(sizes) == 1 and sizes.pop() == wp._group_rows(wp._row_bytes([[0, "x" * 100, 0]] * 25), 25)
    assert columns(out)["id"] == list(range(500)) and rep.rows == 500
    # the early cut when the rows grow far beyond the estimate (the data is the same)
    monkeypatch.setattr(wp, "PENDING_MAX_BYTES", 2000)
    export(doc, out_dir / "cut.parquet", ExportOptions("parquet"), workers=1)
    assert columns(out_dir / "cut.parquet") == columns(out)
    assert pq.ParquetFile(out_dir / "cut.parquet").metadata.num_row_groups >= md.num_row_groups


def test_group_rows_rule() -> None:
    assert wp._group_rows(0, 0) == wp.ROW_GROUP_MAX_ROWS
    assert wp._group_rows(100 * 1000, 1000) == wp.ROW_GROUP_BYTES // 100
    assert wp._group_rows(10**9, 1) == wp.ROW_GROUP_MIN_ROWS
    assert wp._group_rows(1, 10**6) == wp.ROW_GROUP_MAX_ROWS
    assert wp._row_bytes([["abc", 1, None, True, 7]]) == 3 + wp.STRING_OVERHEAD + 8 + 1 + 8


def test_empty_selection_and_no_records(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=jsongen.records(5, seed=1), fmt="array")
    rep = export(doc, out_dir / "e.parquet", ExportOptions("parquet", columns=["id", "name"]), source=[])
    t = table(out_dir / "e.parquet")
    assert t.num_rows == 0 and t.column_names == ["id", "name"] and rep.records == 0
    assert kinds(out_dir / "e.parquet") == {"id": "string", "name": "string"}      # nothing but null: string
    empty = open_doc(data=b"[]")
    rep = export(empty, out_dir / "n.parquet", ExportOptions("parquet"))
    t = table(out_dir / "n.parquet")
    assert t.num_rows == 0 and t.num_columns == 0 and rep.records == 0 and rep.columns == []


def test_cancel_keeps_the_old_target(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 2000)
    doc = open_doc(recs=jsongen.records(60, seed=181), fmt="array")
    target = out_dir / "c.parquet"
    target.write_bytes(b"OLD CONTENT")
    for when in ("start", "middle", "finish"):
        token = CancelToken()

        def cb(info: Any, when: str = when, token: CancelToken = token) -> None:
            if when == "start" and info.message.startswith(("Eksport", "Export")):
                token.cancel()
            elif when == "middle" and info.done > 3000:
                token.cancel()

        progress = Progress(callback=cb, min_interval=0)
        if when == "finish":
            real = wp._Collector.finish

            def finish(self: Any) -> None:
                token.cancel()
                real(self)

            monkeypatch.setattr(wp._Collector, "finish", finish)
        with pytest.raises(Cancelled):
            export(doc, target, ExportOptions("parquet", columns=["id", "name", "orders[*].oid"]), progress=progress,
                   cancel=token, workers=1)
        assert target.read_bytes() == b"OLD CONTENT" and files_in(out_dir) == ["c.parquet"]
    monkeypatch.undo()


def test_error_midway_leaves_no_temp_files(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    with pytest.raises(RuntimeError):
        export(doc, out_dir / "x.parquet", ExportOptions("parquet", columns=["id"]), workers=1)
    assert files_in(out_dir) == []
    monkeypatch.setattr(writer._Ctx, "advance", real)
    # a pyarrow error is an ExportError
    monkeypatch.setattr(wp._Collector, "_write_group", lambda self, g: (_ for _ in ()).throw(pa.ArrowInvalid("boom")))
    with pytest.raises(ExportError) as info:
        export(doc, out_dir / "y.parquet", ExportOptions("parquet", columns=["id"]), workers=1)
    assert info.value.key == "writer.error.parquet" and "boom" in info.value.message and files_in(out_dir) == []


def test_source_file_and_same_file_are_refused(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=[{"a": 1}], fmt="array")
    with pytest.raises(UserInputError) as info:
        export(doc, doc.source_path, ExportOptions("parquet"))
    assert info.value.key == "writer.error.same_file"


def test_huge_and_deep_records(open_doc, out_dir: Path, lang_en) -> None:
    recs = jsongen.records(20, seed=141)
    doc = open_doc(recs=recs, fmt="array", style="pretty2", settings=Settings(large_node_bytes=200))
    huge = [i for i in range(20) if doc.is_huge(i)]
    assert huge and len(huge) < 20
    cols = ["id", "name", "contact.address.city", "orders[*].oid", "tags", "active", "$index"]
    rp, _ = export_both(doc, out_dir / "h", columns=cols)
    assert rp.warnings == [] and rp.records == 20
    rp, _ = export_both(doc, out_dir / "r", columns=cols, explode="orders")
    assert rp.records == 20
    rp, rc = export_both(doc, out_dir / "a")
    assert rp.skipped == len(huge) and rp.warning_keys == rc.warning_keys == ["writer.warn.huge_skipped"]
    # a record too deep to decode in memory: skipped with automatic columns, streamed with explicit ones
    deep = b"[" * 20000 + b"1" + b"]" * 20000
    doc2 = open_doc(data=b'[{"id": 1, "x": ' + deep + b'}, {"id": 2, "x": 5}]')
    rp = export(doc2, out_dir / "d.parquet", ExportOptions("parquet", columns=["id", "x"]))
    assert rp.records == 2 and columns(out_dir / "d.parquet")["id"] == [1, 2]
    assert kinds(out_dir / "d.parquet")["x"] == "string"
    rp = export(doc2, out_dir / "d2.parquet", ExportOptions("parquet"))
    assert rp.skipped == 1 and rp.warning_keys == ["writer.warn.deep_skipped"]


def test_unicode_and_odd_strings(open_doc, out_dir: Path) -> None:
    doc = open_doc(data=b'[{"s": "O\xca\xbbzbekiston \xd0\xa1\xd0\xb0\xd0\xbc\xd0\xb0\xd1\x80\xd2\x9b\xd0\xb0\xd0\xbd\xd0\xb4"},'
                        b' {"s": "lone \\ud800 and pair \\ud83d\\ude00"}, {"s": "q\\"uote, comma\\r\\n\\ttab \\u0000"}]')
    rp, _ = export_both(doc, out_dir / "u")
    data = columns(out_dir / "u.parquet")["s"]
    assert data[0] == "Oʻzbekiston Самарқанд" and data[1] == "lone \\ud800 and pair \U0001f600"
    assert data[2] == 'q"uote, comma\r\n\ttab \x00' and rp.records == 3


def test_plan_columns_and_export_many(open_doc, out_dir: Path) -> None:
    doc = open_doc(recs=jsongen.records(12, seed=5), fmt="array")
    plan = plan_columns(doc, ExportOptions("parquet"))
    rep = export(doc, out_dir / "p.parquet", ExportOptions("parquet"), source=range(0, 6), column_plan=plan)
    assert rep.columns == plan.header and table(out_dir / "p.parquet").column_names == plan.header
    with pytest.raises(UserInputError):
        writer.export_many([(doc, None)], out_dir / "m.parquet", ExportOptions("parquet"))


def test_pyarrow_missing(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, lang_en) -> None:
    doc = open_doc(recs=[{"a": 1}], fmt="array")
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    assert not wp.pyarrow_available()
    with pytest.raises(ExportError) as info:
        export(doc, out_dir / "o.parquet", ExportOptions("parquet"))
    assert info.value.key == "writer.error.parquet_missing"
    assert 'pip install "jsonreshape[parquet]"' in info.value.message and 'pip install "jsonreshape[parquet]"' in info.value.hint
    assert files_in(out_dir) == []
    # the other formats still work
    export(doc, out_dir / "o.csv", ExportOptions("csv"))
    assert files_in(out_dir) == ["o.csv"]


def test_module_docstring_and_writer_docstring_mention_parquet() -> None:
    assert "Parquet" in writer.__doc__ and "writer_parquet" in writer.__doc__
    for word in ("int64", "double", "65 536", "two passes", "combine_chunks", "zstd"):
        assert word in wp.__doc__, word


# ----------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------
@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "people.json"
    jsongen.write_file(p, jsongen.records(12, seed=9), "array")
    return p


def test_cli_convert_to_parquet(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.parquet"
    code, _out, err = run("convert", src, out, "--columns", "id,name,orders[*].oid", "--sep", "|", "--lang", "en")
    assert code == 0, err
    assert kinds(out) == {"id": "int64", "name": "string", "orders[*].oid": "string"}
    assert columns(out)["id"] == [r["id"] for r in jsongen.records(12, seed=9)]
    assert "12 / 12 records" in err and "out.parquet" in err
    # --to parquet names the format for any extension; --parquet-compression chooses the codec
    out2 = tmp_path / "out.bin"
    code, _out, err = run("convert", src, out2, "--to", "parquet", "--parquet-compression", "gzip", "--explode",
                          "orders", "--lang", "en")
    assert code == 0, err
    md = pq.ParquetFile(out2).metadata
    assert md.row_group(0).column(0).compression == "GZIP"
    assert md.num_rows > 12 and f"{md.num_rows} rows" in err
    code, _out, err = run("convert", src, tmp_path / "bad.parquet", "--parquet-compression", "lz4", "--lang", "en")
    assert code == 1 and "lz4" in err and not (tmp_path / "bad.parquet").exists()


def test_cli_option_compatibility(run, src: Path, tmp_path: Path) -> None:
    code, _out, err = run("convert", src, tmp_path / "o.parquet", "--xlsx-split", "files", "--no-bom", "--indent", "4",
                          "--columns", "id", "--lang", "en")
    assert code == 0, err
    assert "--indent, --no-bom, --xlsx-split: not used by the parquet format, ignored." in err
    code, _out, err = run("convert", src, tmp_path / "o.csv", "--parquet-compression", "snappy", "--columns", "id",
                          "--lang", "en")
    assert code == 0 and "--parquet-compression: not used by the csv format, ignored." in err
    code, _out, err = run("convert", src, tmp_path / "o.parquet.gz", "--lang", "en")
    assert code == 1 and "--to json, jsonl, csv, xlsx, sqlite, parquet, feather or arrow" in err   # (no format from that name)
    code, _out, err = run("convert", src, tmp_path / "o.parquet.gz", "--to", "parquet", "--lang", "en")
    assert code == 1 and "Parquet files cannot be gzip-compressed" in err and not (tmp_path / "o.parquet.gz").exists()
    code, _out, err = run("convert", src, tmp_path / "o.json", "--to", "parquet", "--as-keyed", "--lang", "en")
    assert code == 1
    help_code, out, _err = run("convert", "--help", "--lang", "en")
    assert help_code == 0 and "--parquet-compression" in out and "parquet" in out
