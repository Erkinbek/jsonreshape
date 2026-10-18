"""Regression tests for the writer fixes of review phase 5d (jsonreshape/writer.py docstring).

* N01: the map rule never collapses a table: keys found in at least 1 % of the records
  keep their columns (the record root included), only the rare keys of a map are pooled
  into one JSON column (``attrs.*``, ``*`` for the records; the whole object when no key
  is kept); keys first seen (or no longer walked) after a path became a map keep one
  column for their whole value; the warning names the pooled column and its keys;
* N10: SQLite applies the same rule (pooled keys are one JSON text, so array values do
  not create a table per key; the time grows linearly) and stops at SQLITE_MAX_TABLES
  child tables (further arrays: JSON text in the parent row);
* N04: the SQLite overflow warning names the real overflow column (``_extra_2``);
* N12: rows mode fills the automatic plain column at the expanded path only for records
  whose value there is not an array;
* N16 / requirements F3: huge records with explicit columns are never dropped: values
  beyond the capture budget give text cells built from the stream, cut at a text cap with
  a warning naming the record and the column (XLSX: Excel's limit);
* blackbox F18: explicit columns that find no value in any exported record are named;
* blackbox F16 / codereview CR-14: English writer warnings read correctly for a count of 1.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, i18n, writer
from jsonreshape.errors import UserInputError
from jsonreshape.paths import parse_pattern
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.writer import ExportOptions, discover_columns, export

openpyxl = pytest.importorskip("openpyxl")

BOM = b"\xef\xbb\xbf"


@pytest.fixture
def open_doc(tmp_path: Path):
    """``open_doc(data=bytes | recs=list, fmt=..., settings=..., name=...)``."""
    docs: list[Document] = []
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    def make(data: bytes | None = None, *, recs: list | None = None, fmt: str = "array", name: str | None = None,
             settings: Settings | None = None, format: str | None = None, **kw: Any) -> Document:
        p = src_dir / (name or f"src{len(docs)}.{'jsonl' if fmt == 'jsonl' else 'json'}")
        if data is None:
            jsongen.write_file(p, recs, fmt, **kw)
        else:
            p.write_bytes(data)
        doc = Document.open(p, settings=settings, format=format)
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


def read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    assert raw.startswith(BOM)
    old = csv.field_size_limit(1 << 30)
    try:
        return list(csv.reader(io.StringIO(raw[len(BOM):].decode("utf-8"), newline="")))
    finally:
        csv.field_size_limit(old)


def sheet_rows(path: Path) -> list[list[Any]]:
    ws = openpyxl.load_workbook(path).active
    return [list(r) for r in ws.iter_rows(values_only=True)]


def q(db: Path, sql: str, *args: Any) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def columns_of(db: Path, table: str) -> list[str]:
    return [r[1] for r in q(db, "PRAGMA table_info(" + '"' + table.replace('"', '""') + '")')]


def dumps(v: Any) -> str:
    return codec.dumps(v).decode("utf-8")


def sparse_records(n: int = 1000, nested: bool = False) -> list[dict]:
    import random

    rng = random.Random(7)
    attrs = ["attr_%03d" % k for k in range(150)]
    out = []
    for i in range(n):
        picked = {a: rng.randrange(100) for a in rng.sample(attrs, 5)}
        out.append({"id": i, "name": f"product {i}", **({"attrs": picked} if nested else picked)})
    return out


# ----------------------------------------------------------------------------------
# N01: the map rule never collapses a table
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("nested", [False, True])
def test_sparse_tables_keep_one_column_per_field(open_doc, out_dir: Path, nested: bool) -> None:
    recs = sparse_records(nested=nested)
    doc = open_doc(recs=recs)
    rep = export(doc, out_dir / "s.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "s.csv")
    prefix = "attrs." if nested else ""
    assert len(rows[0]) == 152 and rows[0][:2] == ["id", "name"] and rep.warnings == []
    assert sorted(rows[0][2:]) == sorted(prefix + "attr_%03d" % k for k in range(150))
    for i in (0, 517, 999):
        row = dict(zip(rows[0], rows[i + 1]))
        fields = recs[i]["attrs"] if nested else {k: v for k, v in recs[i].items() if k.startswith("attr_")}
        assert row["id"] == str(i) and row["name"] == f"product {i}"
        assert {k[len(prefix):]: int(v) for k, v in row.items() if k.startswith(prefix + "attr_") and v} == fields
    rep = export(doc, out_dir / "s.xlsx", ExportOptions("xlsx"))
    assert sheet_rows(out_dir / "s.xlsx")[0] == rows[0] and rep.warnings == []


def test_the_record_root_pools_only_its_rare_keys(open_doc, out_dir: Path, lang_en) -> None:
    recs = [{"id": i, "name": f"n{i}", f"k{i:05d}": i} for i in range(500)]
    doc = open_doc(recs=recs)
    assert discover_columns(doc) == ["id", "name", "*"]
    rep = export(doc, out_dir / "t.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "t.csv")
    assert rows[0] == ["id", "name", "*"] and len(rows) == 501
    assert rows[1] == ["0", "n0", '{"k00000":0}'] and rows[500] == ["499", "n499", '{"k00499":499}']
    assert rep.warning_keys == ["writer.warn.map_columns"]
    assert "* (keys: 500)" in rep.warnings[0] and "(objects: 1)" in rep.warnings[0]
    rep = export(doc, out_dir / "t.xlsx", ExportOptions("xlsx"))
    got = sheet_rows(out_dir / "t.xlsx")
    assert got[0] == ["id", "name", "*"] and got[1] == [0, "n0", '{"k00000":0}']


def test_the_review_case_of_unique_top_level_keys(open_doc, out_dir: Path, lang_en) -> None:
    """probe_cr07.py case 5: 3000 records {id, k<i>} kept only one column before."""
    doc = open_doc(recs=[{"id": i, f"k{i:05d}": i} for i in range(3000)])
    rep = export(doc, out_dir / "u.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "u.csv")
    assert rows[0] == ["id", "*"] and rows[3000] == ["2999", '{"k02999":2999}']
    assert "* (keys: ~" in rep.warnings[0]           # more different keys than counted exactly


def test_frequent_keys_of_a_map_keep_their_columns(open_doc, out_dir: Path, lang_uz) -> None:
    recs = [{"id": i, "m": {"total": i, "kind": "x", **{f"u{i * 3 + j}": j for j in range(3)}}} for i in range(400)]
    recs[5]["m"]["kind"] = {"nested": True}
    doc = open_doc(recs=recs)
    assert discover_columns(doc) == ["id", "m.total", "m.kind", "m.kind.nested", "m.*"]
    rep = export(doc, out_dir / "m.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "m.csv")
    assert rows[1] == ["0", "0", "x", "", '{"u0":0,"u1":1,"u2":2}']
    assert rows[6] == ["5", "5", '{"nested":true}', "true", '{"u15":0,"u16":1,"u17":2}']
    assert rep.warning_keys == ["writer.warn.map_columns"] and "m.* (~1" in rep.warnings[0]
    assert "ta kalit)" in rep.warnings[0]               # Uzbek
    # rows mode: a map inside the elements of the expanded array
    recs2 = [{"id": i, "items": [{"sku": f"s{i}", **{f"x{i}_{j}": j}} for j in range(2)]} for i in range(200)]
    doc2 = open_doc(recs=recs2)
    rep = export(doc2, out_dir / "r.csv", ExportOptions("csv", explode="items"))
    rows = read_csv(out_dir / "r.csv")
    assert rows[0] == ["id", "items[*].sku", "items[*].*"]
    assert rows[1] == ["0", "s0", '{"x0_0":0}'] and rows[2] == ["0", "s0", '{"x0_1":1}']


def test_whole_maps_still_become_one_column(open_doc, out_dir: Path, lang_en) -> None:
    """robustness F2 / blackbox F3: maps with only rare keys stay one JSON column."""
    recs = [{"id": i, "scores": {f"user{i * 20 + j}": j for j in range(20)}} for i in range(2000)]
    doc = open_doc(recs=recs)
    started = time.perf_counter()
    rep = export(doc, out_dir / "m.csv", ExportOptions("csv"))
    assert time.perf_counter() - started < 30
    rows = read_csv(out_dir / "m.csv")
    assert rows[0] == ["id", "scores"] and rows[7] == ["6", dumps(recs[6]["scores"])]
    assert rep.warning_keys == ["writer.warn.map_columns"] and "scores (keys: ~" in rep.warnings[0]


def test_keys_found_after_a_path_became_a_map_keep_their_whole_value(open_doc, out_dir: Path) -> None:
    recs: list[dict] = [{"id": i, f"k{i}": i} for i in range(150)]          # the records are maps by now
    recs += [{"id": i, "late": {"x": i, "deep": [i]}} for i in range(150, 300)]
    doc = open_doc(recs=recs)
    assert discover_columns(doc) == ["id", "late", "*"]
    export(doc, out_dir / "l.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "l.csv")
    assert rows[1] == ["0", "", '{"k0":0}'] and rows[151] == ["150", '{"x":150,"deep":[150]}', ""]


def test_keys_no_longer_walked_keep_every_value(open_doc, out_dir: Path) -> None:
    """A key walked before its path became a map but rare for a while (no longer walked)
    and frequent in the end is one column for its whole value: no value is lost."""
    recs: list[dict] = [{"id": i, "early": {"a": i, "b": "x"}, **{f"k{i}_{j}": j for j in range(3)}}
                        for i in range(50)]                        # a map from record 34 on
    recs += [{"id": i} for i in range(50, 5050)]                  # "early" is too rare by record 5001
    recs += [{"id": i, "early": {"c": i}} for i in range(5050, 5150)]
    doc = open_doc(recs=recs)
    assert discover_columns(doc) == ["id", "early", "*"]
    export(doc, out_dir / "e.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "e.csv")
    assert rows[1] == ["0", '{"a":0,"b":"x"}', '{"k0_0":0,"k0_1":1,"k0_2":2}']
    assert rows[5051] == ["5050", '{"c":5050}', ""]


def test_map_key_statistics_stay_bounded() -> None:
    stats = writer._MapStats({"id": 5}, 5)
    for r in range(6, 200_006):
        stats.add(("id", f"key{r}", f"other{r}"), r)
    assert len(stats.counts) <= 2 * writer.MAP_BUCKET + 2          # rare keys are forgotten
    assert stats.kept(200_005) == ["id"]
    n, exact = stats.pooled(1)
    assert not exact and 0.9 * 400_000 < n < 1.1 * 400_000


# ----------------------------------------------------------------------------------
# N10 / N04: SQLite
# ----------------------------------------------------------------------------------
def _map_array_records(n: int) -> list[dict]:
    return [{"id": i, "attrs": {f"k{i}_{j}": [j, j + 1] for j in range(3)}} for i in range(n)]


def test_sqlite_maps_with_array_values_are_one_json_text(open_doc, out_dir: Path, lang_en) -> None:
    recs = _map_array_records(800)
    db = out_dir / "m.sqlite"
    rep = export(open_doc(recs=recs), db, ExportOptions("sqlite"))
    assert rep.tables == ["records"] and rep.records == 800
    assert columns_of(db, "records") == ["_id", "id", "attrs"]
    rows = q(db, "select id, attrs from records order by _id")
    assert all(json.loads(a) == recs[i]["attrs"] and rid == i for i, (rid, a) in enumerate(rows))
    assert rep.warning_keys == ["writer.warn.map_columns"] and "attrs (keys: ~" in rep.warnings[0]


def test_sqlite_export_time_grows_linearly_with_maps(open_doc, out_dir: Path) -> None:
    times = []
    for n in (1000, 4000):
        doc = open_doc(recs=_map_array_records(n))
        started = time.perf_counter()
        rep = export(doc, out_dir / f"t{n}.sqlite", ExportOptions("sqlite"))
        times.append(time.perf_counter() - started)
        assert rep.tables == ["records"]
    assert times[1] < 12 * times[0] + 1.0          # 4x the records; was ~30x (a table per key)


def test_sqlite_pools_only_the_rare_keys(open_doc, out_dir: Path) -> None:
    recs = [{"id": i, "m": {"total": i, "tags": ["a", str(i)], **{f"u{i * 3 + j}": [j] for j in range(3)}}}
            for i in range(300)]
    db = out_dir / "p.sqlite"
    rep = export(open_doc(recs=recs), db, ExportOptions("sqlite"))
    assert rep.tables == ["records", "m.tags"]
    assert columns_of(db, "records") == ["_id", "id", "m.total", "m.*"]
    for i, total, rest in q(db, 'select _id, "m.total", "m.*" from records order by _id'):
        assert total == i and json.loads(rest) == {f"u{i * 3 + j}": [j] for j in range(3)}
    assert q(db, 'select value from "m.tags" where _parent = 7 order by _idx') == [("a",), ("7",)]


def test_sqlite_child_tables_are_capped(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, lang_en) -> None:
    monkeypatch.setattr(writer, "SQLITE_MAX_TABLES", 3)
    recs = [{"id": i, **{f"a{k}": [k, i] for k in range(5)}} for i in range(4)]
    db = out_dir / "c.sqlite"
    rep = export(open_doc(recs=recs), db, ExportOptions("sqlite"))
    assert rep.tables == ["records", "a0", "a1", "a2"]
    assert columns_of(db, "records") == ["_id", "id", "a3", "a4"]
    assert q(db, "select a3, a4 from records where _id = 2") == [("[3,2]", "[4,2]")]
    assert q(db, "select value from a1 where _parent = 2 order by _idx") == [(1,), (2,)]
    assert rep.warning_keys == ["writer.warn.sqlite_tables"]
    assert "a3, a4" in rep.warnings[0] and "(arrays: 8)" in rep.warnings[0]


def test_sqlite_overflow_warning_names_the_real_column(open_doc, out_dir: Path, lang_en) -> None:
    recs = [{"id": i, "_extra": f"mine{i}", "a": {f"f{k:04d}": k for k in range(800)},
             "b": {f"g{k:04d}": k for k in range(800)}} for i in range(3)]
    db = out_dir / "x.sqlite"
    rep = export(open_doc(recs=recs), db, ExportOptions("sqlite"))
    cols = columns_of(db, "records")
    assert cols[-1] == "_extra_2" and "_extra" in cols
    assert rep.warning_keys == ["writer.warn.sqlite_extra"] and '"_extra_2"' in rep.warnings[0]
    row = dict(zip(cols, q(db, "select * from records where _id = 1")[0]))
    rebuilt = {k: v for k, v in row.items() if k not in ("_id", "_extra_2") and v is not None}
    rebuilt.update(json.loads(row["_extra_2"]))
    want = {"id": 1, "_extra": "mine1", **{f"a.f{k:04d}": k for k in range(800)},
            **{f"b.g{k:04d}": k for k in range(800)}}
    assert rebuilt == want


# ----------------------------------------------------------------------------------
# N12: rows mode, the automatic plain column at the expanded path
# ----------------------------------------------------------------------------------
def test_plain_column_only_for_values_that_are_not_arrays(open_doc, out_dir: Path) -> None:
    recs = [{"id": 1, "items": [{"oid": 7}, {"oid": 8, "x": 1}]}, {"id": 2, "items": "not-an-array"},
            {"id": 3, "items": []}, {"id": 4}, {"id": 5, "items": [1, 2]}, {"id": 6, "items": None}]
    doc = open_doc(recs=recs)
    want = [["id", "items", "items[*].oid", "items[*].x", "items[*]"],
            ["1", "", "7", "", '{"oid":7}'], ["1", "", "8", "1", '{"oid":8,"x":1}'],
            ["2", "not-an-array", "", "", ""], ["3", "", "", "", ""], ["4", "", "", "", ""],
            ["5", "", "", "", "1"], ["5", "", "", "", "2"], ["6", "", "", "", ""]]
    for opts in (ExportOptions("csv", array_mode="rows"), ExportOptions("csv", explode="items")):
        export(doc, out_dir / "r.csv", opts)
        assert read_csv(out_dir / "r.csv") == want
    export(doc, out_dir / "r.xlsx", ExportOptions("xlsx", explode="items"))
    got = sheet_rows(out_dir / "r.xlsx")
    assert [r[1] for r in got[1:]] == [None, None, "not-an-array", None, None, None, None, None]
    # explicit columns keep the documented rule: the column at the expanded path is the element
    export(doc, out_dir / "x.csv", ExportOptions("csv", columns=["id", "items"], explode="items"))
    assert read_csv(out_dir / "x.csv")[1:3] == [["1", '{"oid":7}'], ["1", '{"oid":8,"x":1}']]


# ----------------------------------------------------------------------------------
# N16 / requirements F3: huge records with explicit columns are never dropped
# ----------------------------------------------------------------------------------
def test_values_beyond_the_capture_budget_still_give_full_cells(open_doc, out_dir: Path) -> None:
    n = 40_000                      # ~3 MB of captured strings: over the 1 MiB budget, a 400 kB cell
    doc_bytes = (b'{"items": [' + b",".join(b'{"id": %d, "city": "%s"}' % (i, b"Toshkent" if i == n - 1 else
                                                                              b"Samarqand") for i in range(n))
                 + b'], "status": "ok"}')
    doc = open_doc(doc_bytes, name="single.json", settings=Settings(large_node_bytes=1000), format="single")
    assert doc.record_count == 1 and doc.is_huge(0)
    rep = export(doc, out_dir / "c.csv", ExportOptions("csv", columns=["status", "items[*].city"]))
    rows = read_csv(out_dir / "c.csv")
    assert rows[1] == ["ok", ";".join(["Samarqand"] * (n - 1) + ["Toshkent"])]
    assert rep.records == 1 and rep.skipped == 0 and rep.warnings == []


def test_a_huge_value_is_cut_and_the_row_kept(open_doc, out_dir: Path, lang_en) -> None:
    big = "y" * (3 * 1024 * 1024)
    recs = [{"id": 1, "text": "small"}, {"id": 2, "text": big, "n": 5}, {"id": 3, "text": "z", "pad": "p" * 2000}]
    doc = open_doc(recs=recs, settings=Settings(large_node_bytes=1000))
    assert doc.is_huge(1)
    rep = export(doc, out_dir / "c.csv", ExportOptions("csv", columns=["id", "text", "n"]))
    rows = read_csv(out_dir / "c.csv")
    cap = max(writer.CELL_TEXT_MIN, writer.CAPTURE_MIN_BYTES // 3)
    assert rows[1] == ["1", "small", ""] and rows[3] == ["3", "z", ""]
    assert rows[2] == ["2", "y" * cap, "5"]
    assert rep.records == 3 and rep.skipped == 0 and rep.warning_keys == ["writer.warn.cell_truncated"]
    assert "record #1, column text" in rep.warnings[0] and f"{cap} characters" in rep.warnings[0]
    rep = export(doc, out_dir / "c.xlsx", ExportOptions("xlsx", columns=["id", "text", "n"]))
    got = sheet_rows(out_dir / "c.xlsx")
    assert got[2] == [2, "y" * writer.XLSX_MAX_STRING, 5] and rep.records == 3
    assert rep.warning_keys == ["writer.warn.cell_truncated"] and "32767 characters" in rep.warnings[0]


def test_huge_elements_of_an_expanded_array_are_cut_not_dropped(open_doc, out_dir: Path, lang_en) -> None:
    rec = {"id": 1, "items": [{"k": 0, "v": "a"}, {"k": 1, "v": "b" * (2 * 1024 * 1024)}, {"k": 2, "v": "c"}],
           "tail": "t"}
    doc = open_doc(recs=[rec], settings=Settings(large_node_bytes=1000))
    rep = export(doc, out_dir / "x.csv", ExportOptions("csv", columns=["id", "items[*].k", "items[*].v", "tail"],
                                                       explode="items"))
    rows = read_csv(out_dir / "x.csv")
    cap = max(writer.CELL_TEXT_MIN, writer.CAPTURE_MIN_BYTES // 2)
    assert rows[1:] == [["1", "0", "a", "t"], ["1", "1", "b" * cap, "t"], ["1", "2", "c", "t"]]
    assert rep.warning_keys == ["writer.warn.cell_truncated"] and "column items[*].v" in rep.warnings[0]


STREAM_RECS: list[Any] = [
    {"id": 1, "name": "Ali", "tags": ["x", "y"], "flag": True, "nil": None, "text": 'Oʻzbekiston "q"\n\\',
     "orders": [{"oid": 7, "sum": 0.5, "items": [{"sku": "a"}, {"sku": "b", "qty": 12345678901234567890}]},
                {"oid": 8}],
     "contact": {"address": {"city": "Toshkent", "zip": 100}}, "deep": {"b": {"b": 1, "c": [{"b": False}]}}},
    {"id": 2, "tags": [], "orders": [], "contact": {}, "mixed": [1, "two", None, False, {"k": [1, 2]}, [3], 1.10]},
    {"id": 3, "tags": "not-a-list", "orders": {"oid": 9}, "contact": None, "": {"": ""}},
    7,
    "text record",
    [1, [2, 3], {"b": {"b": 4}}, []],
]
STREAM_COLUMNS = ["id", "name", "tags", "flag", "nil", "text", "orders", "orders[*].oid", "orders[*].items[*].sku",
                  "orders[*].items", "contact", "contact.address.city", "missing", "*", "**", "**.b", "orders[*]",
                  "$", "mixed", "mixed[*]", "mixed[4].k", "deep", "deep.**", "[1]", "[*]", '[""]', '[""][""]']


@pytest.mark.parametrize("fmt", ["array", "keyed"])
def test_cells_built_from_the_stream_equal_the_normal_cells(open_doc, fmt: str) -> None:
    doc = open_doc(recs=STREAM_RECS, fmt=fmt, format=fmt)
    assert doc.kind == fmt and doc.record_count == len(STREAM_RECS)
    ctx = SimpleNamespace(doc=doc, cancel=None)
    patterns = [parse_pattern(c) for c in STREAM_COLUMNS]
    member = fmt == "keyed"
    for i, rec in enumerate(STREAM_RECS):
        span = doc.record_span(i)
        for cells in (writer._TextCells(";"), writer._XlsxCells(";")):
            built = writer._stream_cells(ctx, span, patterns, member, ";", 1 << 30)
            for column, pattern, sc in zip(STREAM_COLUMNS, patterns, built):
                want = writer._cell_from_values(cells, pattern, pattern.values(rec))
                got = sc.cell(cells)
                assert got == want and type(got) is type(want), (i, column, got, want)
                assert sc.matched == bool(pattern.values(rec)) and not sc.truncated
        # a cap cuts every cell at exactly that many characters
        text = writer._TextCells(";")
        for cap in (1, 4, 17, 60):
            built = writer._stream_cells(ctx, span, patterns, member, ";", cap)
            for column, pattern, sc in zip(STREAM_COLUMNS, patterns, built):
                want = writer._cell_from_values(text, pattern, pattern.values(rec))
                assert sc.cell(text) == want[:cap] and sc.truncated == (len(want) > cap), (i, column, cap)


# ----------------------------------------------------------------------------------
# blackbox F18: explicit columns that matched nothing
# ----------------------------------------------------------------------------------
def test_explicit_columns_without_any_value_are_named(open_doc, out_dir: Path, lang_en) -> None:
    doc = open_doc(recs=[{"name": n, "nil": None, "orders": [{"oid": 1}]} for n in ("Zafar", "Abdulla", "Olim")])
    for fmt in ("csv", "xlsx"):
        rep = export(doc, out_dir / f"t.{fmt}", ExportOptions(fmt, columns=["nmae", "name", "nil", "orders[*].oiid",
                                                                             "$index"]))
        assert rep.warning_keys == ["writer.warn.columns_not_found"]
        assert rep.warnings[0].endswith(": nmae, orders[*].oiid.")
    rep = export(doc, out_dir / "r.csv", ExportOptions("csv", columns=["name", "orders[*].oid", "orders[*].x"],
                                                       explode="orders"))
    assert rep.warning_keys == ["writer.warn.columns_not_found"] and rep.warnings[0].endswith(": orders[*].x.")
    rep = export(doc, out_dir / "ok.csv", ExportOptions("csv", columns=["name", "nil", "$"]))
    assert rep.warnings == []                       # null is a value; "$" is the record
    rep = export(doc, out_dir / "none.csv", ExportOptions("csv", columns=["nmae"]), source=[])
    assert rep.warnings == []                       # nothing exported, nothing to check
    i18n.set_language("uz")
    rep = export(doc, out_dir / "uz.csv", ExportOptions("csv", columns=["nmae"]))
    assert rep.warnings == [i18n.t("writer.warn.columns_not_found", count=1, columns="nmae")]


def test_columns_found_only_in_huge_records_are_not_reported(open_doc, out_dir: Path) -> None:
    recs = [{"id": 1}, {"id": 2, "rare": "x", "pad": "p" * 3000}]
    doc = open_doc(recs=recs, settings=Settings(large_node_bytes=1000))
    assert doc.is_huge(1)
    rep = export(doc, out_dir / "h.csv", ExportOptions("csv", columns=["id", "rare"]))
    assert rep.warnings == [] and read_csv(out_dir / "h.csv")[2] == ["2", "x"]


# ----------------------------------------------------------------------------------
# blackbox F16 / codereview CR-14: English singular forms
# ----------------------------------------------------------------------------------
def test_english_warnings_read_correctly_for_one(open_doc, out_dir: Path, lang_en) -> None:
    doc = open_doc(recs=[{"n": 12345678901234567890, "t": "y" * 40000}])
    rep = export(doc, out_dir / "a.xlsx", ExportOptions("xlsx"))
    assert rep.warnings == [
        "1 number cannot be stored exactly in Excel (more than 15 digits, or too large/small), so it was written "
        "as text.",
        "1 text was longer than the Excel limit (32767 characters) and was truncated."]
    two = open_doc(recs=[{"n": 12345678901234567890, "t": "y" * 40000}] * 2)
    rep = export(two, out_dir / "b.xlsx", ExportOptions("xlsx"))
    assert rep.warnings[0].startswith("2 numbers cannot") and rep.warnings[1].startswith("2 texts were")
    rep = export(doc, out_dir / "d.sqlite", ExportOptions("sqlite"), source=[0, 0])
    assert "1 repeated record was written to the SQLite database only once." in rep.warnings
    lines = open_doc(b'{"a":1}\n{bad\n{"a":2}\n', name="b.jsonl")
    rep = export(lines, out_dir / "l.csv", ExportOptions("csv"))
    assert rep.warnings == ["1 broken line of the source was skipped."]
    huge = open_doc(recs=[{"a": 1}, {"a": "x" * 3000}], settings=Settings(large_node_bytes=1000))
    rep = export(huge, out_dir / "h.csv", ExportOptions("csv"))
    assert rep.warnings[0].startswith("1 very large record (over ") and "export it too" in rep.warnings[0]
    rep = export(huge, out_dir / "h.sqlite", ExportOptions("sqlite"))
    assert rep.warnings[0].startswith("1 very large record (over ") and "for it." in rep.warnings[0]
    rep = export(huge, out_dir / "h.json", ExportOptions("json", sort_keys=True))
    assert rep.warnings == ["The keys of 1 very large record were written unsorted."]
    with pytest.raises(UserInputError) as ei:
        export(doc, out_dir / "x.csv", ExportOptions("csv"), source=[3])
    assert "(records in the file: 1, numbered from 0)" in ei.value.message


def test_singular_variants_exist_in_both_languages() -> None:
    for key in ("writer.warn.broken_lines", "writer.warn.huge_skipped", "writer.warn.huge_skipped_sqlite",
                "writer.warn.huge_unsorted", "writer.warn.xlsx_number_text", "writer.warn.xlsx_truncated",
                "writer.warn.duplicates"):
        one, base = i18n.MESSAGES[key + ".one"], i18n.MESSAGES[key]
        assert one["uz"] and one["en"] != base["en"] and "{count}" in one["en"] and "{count}" in one["uz"]
