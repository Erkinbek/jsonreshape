"""Regression tests for the writer fixes of review phase 5a (jsonreshape/writer.py docstring).

* automatic CSV/XLSX columns come from a scan of EVERY selected record (no field is lost
  outside a sample), rows mode keeps a non-array value at the expanded path;
* column explosion is bounded: map-like objects become one JSON column, wide records keep
  their fields, more than MAX_AUTO_COLUMNS collapse deeper objects, row batches are sized by
  cells, the scan's own memory is bounded;
* SQLite never exceeds its column limit (``_extra``), numbers stay numbers (REAL/INTEGER;
  TEXT only for values no SQLite number holds), deep values become JSON text;
* deep nesting never raises RecursionError; records ``codec.loads`` cannot decode are
  skipped with a warning, streamed with explicit columns, written unsorted with sort_keys;
* XLSX finalization reports progress, can be cancelled, never leaves temp files (also on a
  full disk or a second Ctrl+C), works for paths beyond MAX_PATH; staging folders are named
  ``xlsx-<pid>-<hex>`` under work_dir and purged when their process is gone; split files
  are committed all or nothing and stale parts are reported;
* progress never shows done > total; ``sort_keys`` alphabetical (Uzbek collation) or by code
  point; keyed/wrapped round trips; a source changed during the export is detected before
  the commit; huge-record values beyond the capture budget give cut cells (review 5d).
"""

from __future__ import annotations

import builtins
import csv
import errno
import gc
import io
import json
import os
import re
import sqlite3
import sys
import time
from array import array
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, i18n, writer, writer_xlsx
from jsonreshape.codec import JsonFloat
from jsonreshape.errors import Cancelled, ExportError, FileChangedError, JsonReshapeError
from jsonreshape.fileio import long_path
from jsonreshape.i18n import t
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.sortkeys import collation_key
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, discover_columns, export

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


def files_in(d: Path) -> list[str]:
    return sorted(os.listdir(d))


def read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    assert raw.startswith(BOM)
    old = csv.field_size_limit(1 << 30)       # cells of deep records are large
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


def staging_dirs() -> list[str]:
    tmp = Path(os.environ["JSONRESHAPE_HOME"]) / "tmp"
    return [p.name for p in tmp.iterdir() if p.name.startswith("xlsx-")] if tmp.exists() else []


@pytest.fixture
def no_unraisable(monkeypatch: pytest.MonkeyPatch):
    """Collects exceptions Python could not raise (e.g. in ``ZipFile.__del__``)."""
    seen: list[Any] = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: seen.append((u.exc_type, u.exc_value, u.object)))
    yield seen


# ----------------------------------------------------------------------------------
# blackbox F1 / codereview CR-07: automatic columns cover every field
# ----------------------------------------------------------------------------------
def test_auto_columns_cover_fields_outside_the_old_sample(open_doc, out_dir: Path) -> None:
    recs: list[dict] = [{"id": i, "name": f"n{i}"} for i in range(3000)]
    recs[1]["email"] = "a@example.com"          # in no evenly spread sample of 10 records
    recs[2999]["late"] = {"x": JsonFloat("1.10")}
    doc = open_doc(recs=recs, fmt="jsonl", settings=Settings(sample_size=10))
    rep = export(doc, out_dir / "o.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "o.csv")
    assert rows[0] == rep.columns == ["id", "name", "email", "late.x"]
    assert rows[2] == ["1", "n1", "a@example.com", ""] and rows[3000] == ["2999", "n2999", "", "1.10"]
    assert rep.warnings == [] and rep.rows == 3000
    rep = export(doc, out_dir / "o.xlsx", ExportOptions("xlsx"))
    got = sheet_rows(out_dir / "o.xlsx")
    assert got[0] == ["id", "name", "email", "late.x"]
    assert got[2] == [1, "n1", "a@example.com", None] and got[3000] == [2999, "n2999", None, 1.1]


def test_discover_columns_scans_everything_unless_a_sample_is_asked(open_doc) -> None:
    recs: list[dict] = [{"id": i} for i in range(500)]
    recs[7]["rare"] = True
    doc = open_doc(recs=recs, fmt="array", settings=Settings(sample_size=5))
    assert discover_columns(doc) == ["id", "rare"]
    assert discover_columns(doc, sample=5) == ["id"]            # explicit quick preview
    assert discover_columns(doc, [3, 7, 9]) == ["id", "rare"]
    progress = Progress(min_interval=0)
    assert discover_columns(doc, progress=progress) == ["id", "rare"]
    assert progress.finished and progress.done == progress.total


def test_rows_mode_keeps_a_non_array_value_at_the_expanded_path(open_doc, out_dir: Path) -> None:
    recs = [{"id": 1, "items": [{"oid": 7}]}, {"id": 2, "items": "not-an-array"}, {"id": 3, "items": None},
            {"id": 4}]
    doc = open_doc(recs=recs, fmt="array")
    export(doc, out_dir / "r.csv", ExportOptions("csv", array_mode="rows"))
    # the plain column holds only values that are not arrays (review 5d, N12)
    assert read_csv(out_dir / "r.csv") == [["id", "items", "items[*].oid"], ["1", "", "7"],
                                           ["2", "not-an-array", ""], ["3", "", ""], ["4", "", ""]]
    export(doc, out_dir / "e.csv", ExportOptions("csv", explode="items"))
    assert read_csv(out_dir / "e.csv")[2] == ["2", "not-an-array", ""]
    # null (an empty cell anyway) needs no plain column
    doc2 = open_doc(recs=[{"id": 1, "items": [{"oid": 7}]}, {"id": 2, "items": None}], fmt="array")
    assert discover_columns(doc2, array_mode="rows") == ["id", "items[*].oid"]


def test_column_scan_progress_and_cancel(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 500)
    doc = open_doc(recs=[{"i": i, "s": "x" * 40} for i in range(200)], fmt="array")
    scan_msg = t("writer.progress.columns", name="o.csv")
    infos: list[Any] = []
    export(doc, out_dir / "o.csv", ExportOptions("csv"), progress=Progress(callback=infos.append, min_interval=0))
    scan = [i for i in infos if i.message == scan_msg]
    assert scan and all(i.done <= i.total // 2 for i in scan)     # the scan is the first of two passes
    token = CancelToken()

    def cb(info: Any) -> None:
        if info.message == scan_msg and info.done > 0:
            token.cancel()

    (out_dir / "o.csv").write_bytes(b"OLD")
    with pytest.raises(Cancelled):
        export(doc, out_dir / "o.csv", ExportOptions("csv"), progress=Progress(callback=cb, min_interval=0),
               cancel=token)
    assert files_in(out_dir) == ["o.csv"] and (out_dir / "o.csv").read_bytes() == b"OLD"


# ----------------------------------------------------------------------------------
# robustness F2: column explosion
# ----------------------------------------------------------------------------------
def test_map_like_objects_become_one_json_column(open_doc, out_dir: Path, lang_en) -> None:
    recs = [{"id": i, "scores": {f"user{i * 20 + j}": j for j in range(20)}} for i in range(300)]
    doc = open_doc(recs=recs, fmt="array")
    assert discover_columns(doc) == ["id", "scores"]
    rep = export(doc, out_dir / "m.csv", ExportOptions("csv"))
    rows = read_csv(out_dir / "m.csv")
    assert rows[0] == ["id", "scores"] and len(rows) == 301
    assert rows[5] == ["4", dumps(recs[4]["scores"])]
    assert rep.warning_keys == ["writer.warn.map_columns"] and "scores" in rep.warnings[0]
    rep = export(doc, out_dir / "m.xlsx", ExportOptions("xlsx"))
    got = sheet_rows(out_dir / "m.xlsx")
    assert got[0] == ["id", "scores"] and json.loads(got[1][1]) == recs[0]["scores"]


def test_wide_records_keep_one_column_per_field(open_doc, out_dir: Path) -> None:
    recs = [{"id": i, "attrs": {f"field{k:03d}": i * k for k in range(150)}} for i in range(40)]
    doc = open_doc(recs=recs, fmt="array")
    rep = export(doc, out_dir / "w.csv", ExportOptions("csv"))
    assert rep.columns == ["id"] + [f"attrs.field{k:03d}" for k in range(150)] and rep.warnings == []


def test_more_than_max_auto_columns_collapses_deeper_objects(open_doc, out_dir: Path, lang_en) -> None:
    deep = [{"id": i, "a": {f"b{o:02d}": {f"f{k:02d}": k for k in range(40)} for o in range(30)}} for i in range(3)]
    doc = open_doc(recs=deep, fmt="array")
    rep = export(doc, out_dir / "c.csv", ExportOptions("csv"))       # 1201 leaf paths
    assert rep.columns == ["id"] + [f"a.b{o:02d}" for o in range(30)]
    rows = read_csv(out_dir / "c.csv")
    assert rows[1][1] == dumps(deep[0]["a"]["b00"])
    assert rep.warning_keys == ["writer.warn.columns_capped"] and "level 2" in rep.warnings[0]
    flat = [{f"obj{o:02d}": {f"f{k:02d}": k for k in range(40)} for o in range(30)} for _ in range(2)]
    doc2 = open_doc(recs=flat, fmt="array")
    rep = export(doc2, out_dir / "d.csv", ExportOptions("csv"))
    assert rep.columns == [f"obj{o:02d}" for o in range(30)] and "level 1" in rep.warnings[0]


def test_row_batches_are_sized_by_cells(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[tuple[int, int]] = []
    real = writer._iter_row_batches

    def spy(ctx: Any, builder: Any):
        for batch in real(ctx, builder):
            sizes.append((len(batch), builder.width))
            yield batch

    monkeypatch.setattr(writer, "_iter_row_batches", spy)
    settings = Settings(memory_limit_bytes=2 * 1024 * 1024)     # cell budget: 4096 cells
    doc = open_doc(recs=[{"c0": i, "c5": "x"} for i in range(100)], fmt="array", settings=settings)
    cols = [f"c{k}" for k in range(1000)]
    rep = export(doc, out_dir / "w.csv", ExportOptions("csv", columns=cols))
    assert rep.rows == 100 and sum(n for n, _ in sizes) == 100
    assert max(n * w for n, w in sizes) <= 2 * 4096               # 4 rows (+ the check interval)
    sizes.clear()
    export(doc, out_dir / "r.csv", ExportOptions("csv", columns=cols[:3] + ["c0"], explode="c5"))
    assert max(n for n, _ in sizes) <= writer.ROW_BATCH


def test_column_scan_memory_is_bounded(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, lang_en) -> None:
    monkeypatch.setattr(writer, "SCAN_MAX_PATHS", 60)
    rec = {f"a{i}": {f"b{j}": {f"c{k}": k for k in range(5)} for j in range(5)} for i in range(5)}  # 125 leaves
    doc = open_doc(recs=[rec, rec], fmt="array")
    rep = export(doc, out_dir / "s.csv", ExportOptions("csv"))
    assert rep.columns == [f"a{i}.b{j}" for i in range(5) for j in range(5)]
    assert read_csv(out_dir / "s.csv")[1][0] == dumps(rec["a0"]["b0"])
    assert "writer.warn.deep_columns" in rep.warning_keys


# ----------------------------------------------------------------------------------
# blackbox F3 / codereview CR-02: SQLite column limit
# ----------------------------------------------------------------------------------
def test_sqlite_extra_column_instead_of_too_many_columns(open_doc, out_dir: Path, lang_en) -> None:
    # 2501 leaves of objects that are not maps (each object has all its keys)
    rec = {"id": 1, "a": {f"f{k:04d}": k for k in range(1250)}, "b": {f"f{k:04d}": k for k in range(1250)}}
    doc = open_doc(recs=[rec, {"id": 2, "b": {"f1249": "x"}}], fmt="array")
    db = out_dir / "w.sqlite"
    rep = export(doc, db, ExportOptions("sqlite"))
    cols = columns_of(db, "records")
    assert len(cols) == writer.SQLITE_MAX_COLUMNS and cols[-1] == "_extra"

    def leaves(v: dict) -> dict:
        return {f"{k}.{kk}" if isinstance(x, dict) else k: xx for k, x in v.items()
                for kk, xx in (x.items() if isinstance(x, dict) else [(None, x)])}

    for _id, want in ((0, rec), (1, {"id": 2, "b": {"f1249": "x"}})):
        row = dict(zip(cols, q(db, "select * from records where _id = ?", _id)[0]))
        extra = json.loads(row.pop("_extra") or "{}")
        got = {k: v for k, v in row.items() if k != "_id" and v is not None}
        assert not set(got) & set(extra) and {**got, **extra} == leaves(want)   # every field exactly once
    assert rep.warning_keys == ["writer.warn.sqlite_extra"] and "records" in rep.warnings[0]
    assert '"_extra"' in rep.warnings[0]
    # map-like objects (2401 leaf paths, the review's case) export instead of aborting: one JSON column
    maps = [{"id": i, "attrs": {f"k{i * 3 + j}": j for j in range(3)}} for i in range(800)]
    rep = export(open_doc(recs=maps, fmt="array"), out_dir / "m.sqlite", ExportOptions("sqlite"))
    assert rep.records == 800 and columns_of(out_dir / "m.sqlite", "records") == ["_id", "id", "attrs"]
    assert rep.warning_keys == ["writer.warn.map_columns"]


def test_sqlite_column_cap_in_child_tables(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "SQLITE_MAX_COLUMNS", 8)
    recs = [{"id": i, "items": [{f"k{j}": j * i for j in range(10)}]} for i in range(3)]
    db = out_dir / "c.sqlite"
    export(open_doc(recs=recs, fmt="array"), db, ExportOptions("sqlite"))
    cols = columns_of(db, "items")
    assert cols == ["_id", "_parent", "_idx", "k0", "k1", "k2", "k3", "_extra"]
    for row in q(db, "select * from items order by _id"):
        d = dict(zip(cols, row))
        extra = json.loads(d.pop("_extra"))
        assert {**{k: d[k] for k in ("k0", "k1", "k2", "k3")}, **extra} == recs[d["_parent"]]["items"][0]


def test_sqlite_deep_values_are_json_text(open_doc, out_dir: Path, lang_en) -> None:
    depth = 100
    deep_obj = '{"a":' * depth + '"leaf"' + "}" * depth
    deep_arr = "[" * depth + "1" + "]" * depth
    data = ('[{"id":1,"o":' + deep_obj + ',"arr":' + deep_arr + '}]').encode()
    db = out_dir / "d.sqlite"
    rep = export(open_doc(data, name="deep.json"), db, ExportOptions("sqlite"))
    assert len(rep.tables) <= writer.MAX_DEPTH + 1
    col = "o." + ".".join(["a"] * (writer.MAX_DEPTH - 1))
    assert col in columns_of(db, "records")
    text = q(db, 'select "' + col + '" from records')[0][0]
    assert json.loads(text) == json.loads('{"a":' * (depth - writer.MAX_DEPTH + 1) + '"leaf"'
                                          + "}" * (depth - writer.MAX_DEPTH + 1))
    assert "writer.warn.sqlite_deep" in rep.warning_keys


# ----------------------------------------------------------------------------------
# requirements F2: SQLite numbers stay numbers
# ----------------------------------------------------------------------------------
def test_sqlite_numbers_compare_and_sort_as_numbers(open_doc, out_dir: Path, lang_en) -> None:
    data = (b'[{"k":"a","price":5.50},{"k":"b","price":100.5},{"k":"c","price":20},{"k":"d","price":1E3},'
            b'{"k":"e","price":-0.0},{"k":"f","price":1e400},{"k":"g","price":12345678901234567890},'
            b'{"k":"h","price":0.1}]')
    db = out_dir / "p.sqlite"
    rep = export(open_doc(data, name="prices.json"), db, ExportOptions("sqlite"))
    stored = dict((k, (v, kind)) for k, v, kind in q(db, "select k, price, typeof(price) from records"))
    assert stored["a"] == (5.5, "real") and stored["d"] == (1000.0, "real") and stored["e"][1] == "real"
    assert stored["c"] == (20, "integer") and stored["h"] == (0.1, "real")
    assert stored["f"] == ("1e400", "text") and stored["g"] == ("12345678901234567890", "text")
    assert [r[0] for r in q(db, "select k from records where price > 50 and typeof(price) != 'text'")] == ["b", "d"]
    assert [r[0] for r in q(db, "select k from records order by price, _id")] == ["e", "h", "a", "c", "b", "d", "g",
                                                                                  "f"]
    assert rep.warning_keys == ["writer.warn.sqlite_number_text"] and rep.warnings[0].endswith(": 2.")


# ----------------------------------------------------------------------------------
# blackbox F4 / codereview CR-10: deep nesting
# ----------------------------------------------------------------------------------
def test_deep_objects_in_table_exports(open_doc, out_dir: Path, lang_en) -> None:
    depth = 600                     # far beyond Python's recursion limit for a recursive walk
    data = ('[{"id":1,"deep":' + '{"a":' * depth + '"needle"' + "}" * depth + '},{"id":2}]').encode()
    doc = open_doc(data, name="deep.json")
    col = "deep." + ".".join(["a"] * (writer.MAX_DEPTH - 1))
    rest = depth - writer.MAX_DEPTH + 1
    for name in ("d.csv", "d.xlsx"):
        rep = export(doc, out_dir / name, ExportOptions(name.rsplit(".", 1)[1]))
        assert rep.columns == ["id", col] and rep.warning_keys == ["writer.warn.deep_columns"]
    assert read_csv(out_dir / "d.csv")[1] == ["1", '{"a":' * rest + '"needle"' + "}" * rest]
    leaf = "deep." + ".".join(["a"] * depth)
    export(doc, out_dir / "e.csv", ExportOptions("csv", columns=["id", leaf]))
    assert read_csv(out_dir / "e.csv")[1:] == [["1", "needle"], ["2", ""]]
    export(doc, out_dir / "r.csv", ExportOptions("csv", array_mode="rows"))
    assert read_csv(out_dir / "r.csv")[0] == ["id", col]


@pytest.fixture
def too_deep(open_doc):
    """Three records; #1 is nested jsongen.TOO_DEEP levels (valid JSON the scanner accepts, but too
    deep for codec.loads)."""
    depth = jsongen.TOO_DEEP
    data = (b'[{"id":1,"x":"a"},{"id":2,"deep":' + b'{"a":' * depth + b'"needle"' + b"}" * depth
            + b'},{"id":3,"x":"c"}]')
    doc = open_doc(data, name="toodeep.json")
    raw = doc.record_bytes(1)
    with pytest.raises(JsonReshapeError):
        codec.loads(raw)
    return doc, raw


def test_records_too_deep_to_decode(too_deep, out_dir: Path, lang_en) -> None:
    doc, raw = too_deep
    rep = export(doc, out_dir / "a.csv", ExportOptions("csv"))
    assert read_csv(out_dir / "a.csv") == [["id", "x"], ["1", "a"], ["3", "c"]]
    assert rep.skipped == 1 and rep.records == 2 and rep.warning_keys == ["writer.warn.deep_skipped"]
    rep = export(doc, out_dir / "a.xlsx", ExportOptions("xlsx"))
    assert sheet_rows(out_dir / "a.xlsx") == [["id", "x"], [1, "a"], [3, "c"]] and rep.skipped == 1
    # explicit columns: the record is streamed
    rep = export(doc, out_dir / "e.csv", ExportOptions("csv", columns=["id", "deep"]))
    rows = read_csv(out_dir / "e.csv")
    assert [r[0] for r in rows[1:]] == ["1", "2", "3"] and rep.records == 3 and rep.warnings == []
    assert rows[2][1] == '{"a":' * jsongen.TOO_DEEP + '"needle"' + "}" * jsongen.TOO_DEEP      # compact JSON of the value
    rep = export(doc, out_dir / "e.xlsx", ExportOptions("xlsx", columns=["id", "x"]))
    assert sheet_rows(out_dir / "e.xlsx")[1:] == [[1, "a"], [2, None], [3, "c"]]
    rep = export(doc, out_dir / "d.sqlite", ExportOptions("sqlite"))
    assert q(out_dir / "d.sqlite", "select id from records order by _id") == [(1,), (3,)]
    assert rep.warning_keys == ["writer.warn.deep_skipped_sqlite"] and rep.skipped == 1
    for name, opts in (("s.json", ExportOptions("json", indent=None, sort_keys=True)),
                       ("s.jsonl", ExportOptions("jsonl", sort_keys="codepoint"))):
        rep = export(doc, out_dir / name, opts)
        assert rep.records == 3 and rep.warning_keys == ["writer.warn.deep_unsorted"]
        assert raw in (out_dir / name).read_bytes()          # written as it is (unsorted)


# ----------------------------------------------------------------------------------
# robustness F6: XLSX finalization
# ----------------------------------------------------------------------------------
def _xlsx_doc(open_doc) -> Document:
    import hashlib

    return open_doc(recs=[{"i": i, "s": hashlib.sha256(str(i).encode()).hexdigest() * 3} for i in range(2000)],
                    fmt="array")


def test_xlsx_finalization_reports_progress_and_can_be_cancelled(open_doc, out_dir: Path, no_unraisable: list,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    # stage 7: the sheets are deflated as the rows arrive (writer_xlsx), so there is no slow final step any
    # more: progress is the rows' bytes, monotone and complete, and a cancel while rows are written holds
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 20000)
    doc = _xlsx_doc(open_doc)
    export_msg = t("writer.progress.export", name="o.xlsx")
    infos: list[Any] = []
    export(doc, out_dir / "o.xlsx", ExportOptions("xlsx"), progress=Progress(callback=infos.append, min_interval=0))
    dones = [i.done for i in infos if i.message == export_msg and i.total]
    assert len(dones) > 3 and dones == sorted(dones)
    assert infos[-1].finished and infos[-1].done == infos[-1].total
    old = (out_dir / "o.xlsx").read_bytes()
    token = CancelToken()

    def cb(info: Any) -> None:
        if info.message == export_msg and info.total and 0 < info.done < info.total:
            token.cancel()                      # cancel while the sheet is being written

    with pytest.raises(Cancelled) as ei:
        export(doc, out_dir / "o.xlsx", ExportOptions("xlsx"), progress=Progress(callback=cb, min_interval=0),
               cancel=token)
    del ei
    gc.collect()
    assert no_unraisable == []
    assert files_in(out_dir) == ["o.xlsx"] and (out_dir / "o.xlsx").read_bytes() == old and staging_dirs() == []


@pytest.mark.parametrize("split", ["sheets", "files"])
def test_xlsx_cancel_after_finalization_is_not_committed(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                         split: str) -> None:
    doc = _xlsx_doc(open_doc)
    token = CancelToken()
    real = writer_xlsx._XlsxBook._finish_file

    def finish(self: Any) -> None:
        real(self)
        token.cancel()                          # Ctrl+C just as the zip is complete

    monkeypatch.setattr(writer_xlsx._XlsxBook, "_finish_file", finish)
    (out_dir / "o.xlsx").write_bytes(b"OLD")
    with pytest.raises(Cancelled):
        export(doc, out_dir / "o.xlsx", ExportOptions("xlsx", columns=["i", "s"], xlsx_split=split), cancel=token)
    assert files_in(out_dir) == ["o.xlsx"] and (out_dir / "o.xlsx").read_bytes() == b"OLD"
    assert staging_dirs() == []


class _FailingFile:
    """Wraps the XLSX output file: raises ``exc`` once more than ``after`` bytes were written."""

    def __init__(self, f: Any, after: int, exc: BaseException) -> None:
        self.f = f
        self.after = after
        self.exc = exc
        self.n = 0

    def write(self, data: Any) -> int:
        self.n += len(data)
        if self.n > self.after:
            raise self.exc
        return self.f.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.f, name)


@pytest.mark.parametrize("exc, expect", [
    (OSError(errno.ENOSPC, "No space left on device"), "fileio.error.disk_full"),
    (KeyboardInterrupt(), None),             # a second Ctrl+C in the middle of the zip
])
def test_xlsx_failure_during_finalization_leaves_nothing(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                         no_unraisable: list, exc: BaseException,
                                                         expect: str | None) -> None:
    doc = _xlsx_doc(open_doc)
    real_init = writer_xlsx._XlsxSink.__init__

    def init(self: Any, f: Any, cancel: Any) -> None:
        real_init(self, _FailingFile(f, 3000, exc), cancel)

    monkeypatch.setattr(writer_xlsx._XlsxSink, "__init__", init)
    (out_dir / "o.xlsx").write_bytes(b"OLD")
    with pytest.raises(BaseException) as ei:
        export(doc, out_dir / "o.xlsx", ExportOptions("xlsx"))
    if expect is None:
        assert ei.type is KeyboardInterrupt
    else:
        assert isinstance(ei.value, JsonReshapeError) and ei.value.key == expect
    del ei
    gc.collect()
    assert no_unraisable == []
    assert files_in(out_dir) == ["o.xlsx"] and (out_dir / "o.xlsx").read_bytes() == b"OLD"
    assert staging_dirs() == []


# ----------------------------------------------------------------------------------
# robustness F7: output paths beyond MAX_PATH
# ----------------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH is a Windows limit")
def test_xlsx_to_a_long_output_path(open_doc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = open_doc(recs=[{"a": i, "b": "Oʻzbekiston"} for i in range(30)], fmt="array")
    deep = tmp_path / ("d" * 120) / ("e" * 120)
    os.makedirs(long_path(deep))
    out = deep / "out.xlsx"
    assert len(str(out)) > 260
    real_open = builtins.open

    def max_path_open(file: Any, *args: Any, **kw: Any) -> Any:
        # an interpreter that is not long-path aware: unprefixed long paths are not found
        if isinstance(file, (str, os.PathLike)):
            s = os.fspath(file)
            if isinstance(s, str) and len(s) >= 260 and not s.startswith("\\\\?\\"):
                raise FileNotFoundError(errno.ENOENT, "The system cannot find the path specified", s)
        return real_open(file, *args, **kw)

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", max_path_open)
        m.setattr(io, "open", max_path_open)
        rep = export(doc, out, ExportOptions("xlsx"))
        rep2 = export(doc, out, ExportOptions("xlsx", xlsx_split="files", xlsx_max_rows=11))
    assert rep.rows == 30 and len(rep2.files) == 3
    got = [list(r) for r in openpyxl.load_workbook(long_path(out)).active.iter_rows(values_only=True)]
    assert got[0] == ["a", "b"] and got[30] == [29, "Oʻzbekiston"]
    assert sorted(os.listdir(long_path(deep))) == ["out.xlsx", "out_1.xlsx", "out_2.xlsx", "out_3.xlsx"]


# ----------------------------------------------------------------------------------
# robustness F9: XLSX staging folders
# ----------------------------------------------------------------------------------
def test_xlsx_export_stages_nothing(open_doc, out_dir: Path, tmp_path: Path) -> None:
    # stage 7: the own XLSX writer streams the sheets into the zip; no xlsx-<pid>-<hex> staging folder is made
    # (the purge of folders left by earlier versions stays: the test below)
    cache = tmp_path / "cache"
    doc = open_doc(recs=[{"a": i, "s": "x" * 100} for i in range(500)], fmt="array",
                   settings=Settings(cache_dir=str(cache)))
    export(doc, out_dir / "o.xlsx", ExportOptions("xlsx", xlsx_split="files", xlsx_max_rows=100))
    assert sorted(p.name for p in out_dir.iterdir()) == [f"o_{k}.xlsx" for k in range(1, 7)]   # 99 rows + header
    for root in (cache / "tmp", Path(os.environ["JSONRESHAPE_HOME"]) / "tmp"):
        assert not root.exists() or not [p for p in root.iterdir() if p.name.startswith("xlsx-")]
    assert staging_dirs() == []


def _dead_pid() -> int:
    import psutil

    pid = 2**30 + 7
    while psutil.pid_exists(pid):  # pragma: no cover - practically never
        pid += 4
    return pid


def test_stale_staging_folders_are_purged(open_doc, out_dir: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    doc = open_doc(recs=[{"a": 1}], fmt="array", settings=Settings(cache_dir=str(cache)))
    home_tmp = Path(os.environ["JSONRESHAPE_HOME"]) / "tmp"
    made: dict[str, Path] = {}
    for root in (home_tmp, cache / "tmp"):
        root.mkdir(parents=True, exist_ok=True)
        for key, name in (("dead", f"xlsx-{_dead_pid()}-0123abcd"), ("mine", f"xlsx-{os.getpid()}-89ab4567"),
                          ("alive", f"xlsx-{os.getppid()}-cdef0123"), ("legacy_old", "xlsx-abcd_123"),
                          ("legacy_new", "xlsx-zyxw9876"), ("other", "sort-123-abc")):
            p = root / name
            p.mkdir()
            (p / "rows.tmp").write_bytes(b"x" * 100)
            made[f"{root.name}/{key}"] = p
        old = time.time() - 3 * 86400
        os.utime(root / "xlsx-abcd_123", (old, old))
    export(doc, out_dir / "o.json", ExportOptions("json"))     # every export purges
    for label, p in made.items():
        gone = label.endswith(("/dead", "/legacy_old"))
        assert p.exists() != gone, label


# ----------------------------------------------------------------------------------
# robustness F10: --xlsx-split files
# ----------------------------------------------------------------------------------
def _parts_doc(open_doc) -> Document:
    return open_doc(recs=[{"i": i} for i in range(30)], fmt="array")


def _split(**kw: Any) -> ExportOptions:
    return ExportOptions("xlsx", xlsx_split="files", xlsx_max_rows=11, **kw)


def test_xlsx_split_files_reports_stale_parts(open_doc, out_dir: Path, lang_en) -> None:
    doc = _parts_doc(open_doc)
    rep = export(doc, out_dir / "big.xlsx", _split())
    assert [p.name for p in rep.files] == ["big_1.xlsx", "big_2.xlsx", "big_3.xlsx"]
    third = (out_dir / "big_3.xlsx").read_bytes()
    rep = export(doc, out_dir / "big.xlsx", _split(), source=range(15))
    assert [p.name for p in rep.files] == ["big_1.xlsx", "big_2.xlsx"]
    assert "writer.warn.xlsx_stale_parts" in rep.warning_keys
    assert "big_3.xlsx" in rep.warnings[rep.warning_keys.index("writer.warn.xlsx_stale_parts")]
    assert (out_dir / "big_3.xlsx").read_bytes() == third               # left unchanged
    rep = export(doc, out_dir / "big.xlsx", _split(), source=range(5))   # one file: the requested name
    assert rep.files == [out_dir / "big.xlsx"]
    text = rep.warnings[rep.warning_keys.index("writer.warn.xlsx_stale_parts")]
    assert "big_1.xlsx, big_2.xlsx, big_3.xlsx" in text


@pytest.mark.parametrize("fail_on", ["aside", "place"])
def test_xlsx_split_files_commit_is_all_or_nothing(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                   fail_on: str) -> None:
    doc = _parts_doc(open_doc)
    export(doc, out_dir / "big.xlsx", _split())
    before = {n: (out_dir / n).read_bytes() for n in files_in(out_dir)}
    real = writer._replace_retry

    def flaky(src: Path, dst: Path, timeout: float = 1.0) -> None:
        if fail_on == "aside" and Path(src).name == "big_2.xlsx":       # moving the old part aside
            raise PermissionError(errno.EACCES, "in use", str(src))
        if fail_on == "place" and Path(dst).name == "big_3.xlsx":       # placing the last new part
            raise PermissionError(errno.EACCES, "in use", str(dst))
        real(src, dst, timeout)

    monkeypatch.setattr(writer, "_replace_retry", flaky)
    with pytest.raises(ExportError) as ei:
        export(doc, out_dir / "big.xlsx", _split(), source=range(29, -1, -1))
    assert ei.value.key == "writer.error.xlsx_part_locked" and ei.value.hint
    assert {n: (out_dir / n).read_bytes() for n in files_in(out_dir)} == before   # nothing changed, no leftovers


@pytest.mark.skipif(os.name != "nt", reason="an open file blocks renaming only on Windows")
def test_xlsx_split_files_with_a_part_open_elsewhere(open_doc, out_dir: Path) -> None:
    doc = _parts_doc(open_doc)
    export(doc, out_dir / "big.xlsx", _split())
    before = {n: (out_dir / n).read_bytes() for n in files_in(out_dir)}
    with open(out_dir / "big_2.xlsx", "rb"):     # like Excel: no delete/rename sharing
        with pytest.raises(ExportError) as ei:
            export(doc, out_dir / "big.xlsx", _split(), source=range(29, -1, -1))
    assert ei.value.key == "writer.error.xlsx_part_locked"
    assert {n: (out_dir / n).read_bytes() for n in files_in(out_dir)} == before


# ----------------------------------------------------------------------------------
# blackbox F10: progress units
# ----------------------------------------------------------------------------------
class FakeRecordSet:
    def __init__(self, indices: list[int]) -> None:
        self._a = array("Q", indices)

    def __len__(self) -> int:
        return len(self._a)

    def __getitem__(self, i: int) -> int:
        return self._a[i]

    def iter_chunks(self):
        for i in range(0, len(self._a), 7):
            yield self._a[i:i + 7]


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv", "xlsx", "sqlite"])
def test_progress_never_exceeds_its_total(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                          fmt: str) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 2000)
    doc = open_doc(recs=jsongen.records(200, seed=5), fmt="array")
    for source in (None, range(10, 150), list(range(0, 200, 2)), list(range(199, -1, -3)),
                   FakeRecordSet(list(range(0, 200, 3)))):
        infos: list[Any] = []
        progress = Progress(callback=infos.append, min_interval=0)
        export(doc, out_dir / f"o.{fmt}", ExportOptions(fmt), source=source, progress=progress)
        assert progress.finished and progress.done == progress.total
        known = []
        for info in infos:
            if info.total is None:
                if known:               # XLSX finalization: bytes of the zip, total unknown
                    break
                continue
            assert info.done <= info.total, (source, info)
            known.append(info.done)
        assert known and known == sorted(known)


# ----------------------------------------------------------------------------------
# requirements F16: sort_keys
# ----------------------------------------------------------------------------------
KEYS = ["Banan", "Zebo", "apple", "item10", "item2", "olma", "oʻrik", "sabzi", "shaftoli", "анор"]
ALPHA = ["apple", "Banan", "item2", "item10", "olma", "sabzi", "Zebo", "oʻrik", "shaftoli", "анор"]


def test_sort_keys_alphabetical_with_uzbek_collation(open_doc, out_dir: Path) -> None:
    assert sorted(KEYS, key=collation_key) == ALPHA
    inner = {k: [{"b": 1, "B": 2, "a10": 3, "a9": 4}] for k in KEYS}
    rec = {**{k: i for i, k in enumerate(KEYS)}, "nested": inner}
    doc = open_doc(recs=[rec], fmt="array")
    for mode in (True, "alpha"):
        export(doc, out_dir / "a.json", ExportOptions("json", indent=None, sort_keys=mode))
        got = codec.loads((out_dir / "a.json").read_bytes())[0]
        assert list(got) == sorted(ALPHA + ["nested"], key=collation_key)
        assert list(got["nested"]) == ALPHA
        assert list(got["nested"]["olma"][0]) == ["a9", "a10", "b", "B"]      # natural, lower case first
    export(doc, out_dir / "a.jsonl", ExportOptions("jsonl", sort_keys="alpha"))
    assert list(codec.loads((out_dir / "a.jsonl").read_bytes())) == sorted(ALPHA + ["nested"], key=collation_key)
    export(doc, out_dir / "c.json", ExportOptions("json", indent=None, sort_keys="codepoint"))
    got = codec.loads((out_dir / "c.json").read_bytes())[0]
    assert list(got) == sorted(KEYS + ["nested"]) and list(got["nested"]["olma"][0]) == ["B", "a10", "a9", "b"]


# ----------------------------------------------------------------------------------
# requirements F18: keyed and wrapped round trips
# ----------------------------------------------------------------------------------
def test_keyed_and_wrapped_round_trips(open_doc, out_dir: Path) -> None:
    keyed = {"user1": {"id": 1, "n": JsonFloat("1.10")}, "user2": {"id": 2, "tags": ["x"]}, "Тошкент": {"id": 3}}
    kdoc = open_doc(codec.dumps(keyed, indent=2), name="keyed.json", format="keyed")
    assert kdoc.kind == "keyed"
    export(kdoc, out_dir / "k.json", ExportOptions("json", indent=None))
    assert (out_dir / "k.json").read_bytes() == codec.dumps(keyed)          # JSON -> JSON keeps the object
    export(kdoc, out_dir / "k.jsonl", ExportOptions("jsonl"))
    lines = (out_dir / "k.jsonl").read_bytes().split(b"\n")
    assert lines[:-1] == [codec.dumps({k: v}) for k, v in keyed.items()]   # {"key": value} lines
    back = open_doc((out_dir / "k.jsonl").read_bytes(), name="back.jsonl")
    export(back, out_dir / "kb.json", ExportOptions("json", indent=None))
    got = codec.loads((out_dir / "kb.json").read_bytes())
    assert got == [{k: v} for k, v in keyed.items()]                         # documented: one-member objects
    merged: dict = {}
    for member in got:
        merged.update(member)
    assert codec.dumps(merged) == codec.dumps(keyed)
    wrapped = {"meta": {"total": 2}, "data": [{"id": 1}, {"id": 2, "x": JsonFloat("1e400")}], "tail": True}
    wdoc = open_doc(codec.dumps(wrapped, indent=4), name="wrapped.json", record_path="data")
    assert wdoc.kind == "wrapped"
    export(wdoc, out_dir / "w.json", ExportOptions("json", indent=4))
    assert (out_dir / "w.json").read_bytes() == codec.dumps(wrapped, indent=4)   # identical
    export(wdoc, out_dir / "w.jsonl", ExportOptions("jsonl"))
    wback = open_doc((out_dir / "w.jsonl").read_bytes(), name="wback.jsonl")
    export(wback, out_dir / "wb.json", ExportOptions("json", indent=None))
    assert (out_dir / "wb.json").read_bytes() == codec.dumps(wrapped["data"])   # the wrapper is not in JSONL


# ----------------------------------------------------------------------------------
# requirements F1 (writer part): a source changed during the export is never committed
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, name, kw", [
    ("json", "o.json", {}), ("jsonl", "o.jsonl", {}), ("csv", "o.csv", {}), ("xlsx", "o.xlsx", {}),
    ("xlsx", "o.xlsx", {"xlsx_split": "files", "xlsx_max_rows": 11}), ("sqlite", "o.sqlite", {}),
])
def test_source_changed_during_the_export(open_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, fmt: str,
                                          name: str, kw: dict) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 1000)
    doc = open_doc(recs=[{"id": i, "name": f"n{i:03d}"} for i in range(40)], fmt="array")
    src = doc.source_path
    real = writer._Ctx.advance
    state = {"changed": False}

    def advance(self: Any, blk: Any) -> None:
        real(self, blk)
        if not state["changed"]:          # another program rewrites the file in place
            state["changed"] = True
            data = src.read_bytes()
            st = os.stat(src)
            src.write_bytes(data.replace(b'"n0', b'"N0'))     # same size, other content
            os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    monkeypatch.setattr(writer._Ctx, "advance", advance)
    target = out_dir / name
    target.write_bytes(b"OLD")
    with pytest.raises(FileChangedError):
        export(doc, target, ExportOptions(fmt, **kw))
    assert state["changed"] and files_in(out_dir) == [name] and target.read_bytes() == b"OLD"
    assert staging_dirs() == []


# ----------------------------------------------------------------------------------
# codereview CR-14 (e) and the huge-record capture budget
# ----------------------------------------------------------------------------------
def test_xlsx_too_large_hint_names_the_cli_option() -> None:
    for lang in ("uz", "en"):
        assert "--xlsx-split files" in i18n.MESSAGES["writer.error.xlsx_too_large.hint"][lang]


def test_huge_record_values_beyond_the_capture_budget_are_cut(open_doc, out_dir: Path, lang_en) -> None:
    big = "y" * (2 * 1024 * 1024)                 # more than the 1 MiB capture floor
    recs = [{"id": 1, "big": "small"}, {"id": 2, "big": big}, {"id": 3, "big": "z", "pad": "p" * 2000}]
    doc = open_doc(recs=recs, fmt="array", settings=Settings(large_node_bytes=1000))
    assert doc.is_huge(1) and doc.is_huge(2)
    rep = export(doc, out_dir / "c.csv", ExportOptions("csv", columns=["id", "big"]))
    # the row is kept; the cell is cut at the text cap (review 5d, N16)
    cap = max(writer.CELL_TEXT_MIN, writer.CAPTURE_MIN_BYTES // 2)
    assert read_csv(out_dir / "c.csv") == [["id", "big"], ["1", "small"], ["2", "y" * cap], ["3", "z"]]
    assert rep.skipped == 0 and rep.records == 3 and rep.warning_keys == ["writer.warn.cell_truncated"]
    assert "#1" in rep.warnings[0] and "column big" in rep.warnings[0]
