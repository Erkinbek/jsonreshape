"""writer.py stage-5 additions (docs/ARCHITECTURE.md §14.4, s5/map_convert.md §1.4-§1.7):

* ``export_node`` (moved from the export dialog, Qt-free): byte parity with the GUI version for every
  kind, node and format, on plain and edited documents; formats refused; gzip; progress;
* SQLite relation metadata: the ``_jsonreshape`` table, ``REFERENCES`` on every ``_parent``, the
  ``_wrapper`` table (also an edited wrapper; not for a huge wrapper / partial document -> notice),
  ``_ord`` for selections in another order, reserved names;
* the ``writer.note.wrapper_not_written`` notice for JSON Lines / CSV / XLSX;
* ``.json.gz`` / ``.jsonl.gz`` / ``.csv.gz`` outputs (``format_for_path``, ``gzip=``), refused for XLSX /
  SQLite;
* ``plan_columns`` + ``export(column_plan=...)``: parts share one header;
* the XLSX ``files`` parts committed through ``fileio.commit_files`` (a part this program reads is
  replaced).
"""

from __future__ import annotations

import gzip
import io
import csv
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, writer
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.fileio import open_shared
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export, export_node, format_for_path, plan_columns
from overlay_helpers import KINDS, make_edited, make_source

WINDOWS = sys.platform == "win32"


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "out"
    d.mkdir()
    return d


def open_bytes(tmp_path: Path, docs: list, data: bytes, name: str = "s.json", **kw: Any) -> Document:
    p = tmp_path / "src" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    doc = Document.open(p, **kw)
    doc.build_index()
    docs.append(doc)
    return doc


def q(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return list(con.execute(sql))
    finally:
        con.close()


# ----------------------------------------------------------------------------------
# export_node: parity with the GUI version
# ----------------------------------------------------------------------------------
class FakeSession:
    """What gui.exportdialog.export_node asks of a session (the node is the reference itself)."""

    def __init__(self, doc: Document) -> None:
        self.doc = doc
        self.path = doc.source_path
        self.name = doc.source_path.name

    def node(self, ref: Any, cancel: Any = None, doc: Any = None) -> Any:
        return ref


def interesting_nodes(doc: Document) -> list[Any]:
    """The root, the records node, records, and containers inside a record."""
    nodes = [doc.root()]
    rec = doc.records_node()
    if rec is not None and rec != nodes[0]:
        nodes.append(rec)
    n = doc.record_count
    for i in sorted({0, n // 2, n - 1}):
        node = doc.record_node(i)
        nodes.append(node)
        if node.is_container:
            for child in node.children(0, 12):
                if child.is_container:
                    nodes.append(child)
                    break
    return nodes


def check_parity(doc: Document, out: Path) -> int:
    gui = pytest.importorskip("jsonreshape.gui.exportdialog")
    session = FakeSession(doc)
    checked = 0
    for node in interesting_nodes(doc):
        for fmt, indent in (("json", 2), ("json", None), ("json", "\t"), ("json", 4), ("jsonl", None)):
            a = export_node(doc, node, out / "a.out", fmt, indent=indent)
            b = gui.export_node(session, node, out / "b.out", fmt, indent=indent, doc=doc)
            assert (out / "a.out").read_bytes() == (out / "b.out").read_bytes(), (node, fmt, indent)
            assert (a.records, a.rows, a.fmt) == (b.records, b.rows, b.fmt)
            checked += 1
    return checked


@pytest.mark.parametrize("kind", KINDS)
def test_export_node_parity_with_the_gui(tmp_path, docs, out_dir, kind) -> None:
    doc = make_source(tmp_path / "s.json", kind, n=12, seed=6, style="pretty2")
    docs.append(doc)
    assert check_parity(doc.pinned(), out_dir) > 10


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped", "wrapped_obj", "stream"])
def test_export_node_parity_on_edited_documents(tmp_path, docs, out_dir, kind) -> None:
    doc, _ob, exp = make_edited(tmp_path / "e", kind, "wrapper" if kind.startswith("wrapped") else "mixed", n=14,
                                seed=9)
    docs += [doc, exp]
    pinned = doc.pinned()
    check_parity(pinned, out_dir)
    # the effective text: the same value as the node of the plain document of the expected content
    def value(path: Path, fmt: str) -> Any:
        data = path.read_bytes()
        if fmt == "jsonl":
            data = b"[" + b",".join(data.splitlines()) + b"]"
        return codec.dumps(codec.loads(data))

    for fmt, indent in (("json", 2), ("jsonl", None)):
        export_node(pinned, pinned.root(), out_dir / "e.out", fmt, indent=indent)
        export_node(exp, exp.root(), out_dir / "x.out", fmt, indent=indent)
        assert value(out_dir / "e.out", fmt) == value(out_dir / "x.out", fmt)


def test_export_node_formats_gzip_progress(tmp_path, docs, out_dir, lang_en) -> None:
    doc = make_source(tmp_path / "s.json", "array", n=20, seed=2, style="pretty2")
    docs.append(doc)
    node = doc.root()
    for fmt in ("csv", "xlsx", "sqlite", "yaml"):
        with pytest.raises(UserInputError) as ei:
            export_node(doc, node, out_dir / "x.out", fmt)
        assert ei.value.key == "writer.error.node_format" and ei.value.hint
    rep = export_node(doc, node, out_dir / "n.jsonl.gz", "jsonl")
    plain = export_node(doc, node, out_dir / "n.jsonl", "jsonl")
    assert gzip.decompress((out_dir / "n.jsonl.gz").read_bytes()) == (out_dir / "n.jsonl").read_bytes()
    assert rep.records == plain.records == 20
    infos = []
    progress = Progress(callback=infos.append, min_interval=0)
    export_node(doc, node, out_dir / "p.json", "json", progress=progress)
    assert progress.finished and progress.done == progress.total == node.end - node.start
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        export_node(doc, node, out_dir / "c.json", "json", cancel=token)
    assert not (out_dir / "c.json").exists()
    with pytest.raises(UserInputError) as ei:
        export_node(doc, node, doc.source_path, "json")
    assert ei.value.key == "writer.error.same_file"


def test_stream_value_short_read_is_a_user_error() -> None:
    class Short(io.BytesIO):
        def read(self, n: int = -1) -> bytes:  # the file got shorter than the node's span
            return b""

    with pytest.raises(UserInputError) as ei:
        writer._stream_value(Short(b""), 0, 10, None, io.BytesIO(), None)
    assert ei.value.key == "writer.error.node_truncated"


# ----------------------------------------------------------------------------------
# SQLite: _jsonreshape, REFERENCES, _wrapper, _ord
# ----------------------------------------------------------------------------------
SQL_DOC = (b'{"meta": {"v": 1, "who": "Ali"}, "data": [\n'
           b' {"id": 1, "orders": [{"oid": 7, "items": [{"sku": "a"}, {"sku": "b"}]}, {"oid": 8}], "tags": ["x"]},\n'
           b' {"id": 2, "orders": [], "matrix": [[1, 2], [3]]},\n'
           b' {"id": 3, "_jsonreshape": [1, 2], "_wrapper": [3]}\n'
           b'], "tail": {"n": 3}}')


def test_sqlite_relation_metadata(tmp_path, docs, out_dir) -> None:
    doc = open_bytes(tmp_path, docs, SQL_DOC, format="wrapped", record_path="data")
    db = out_dir / "d.sqlite"
    rep = export(doc, db, ExportOptions("sqlite"))
    assert "_jsonreshape" not in rep.tables and "_wrapper" not in rep.tables     # data tables only
    # the description tables' names are reserved: data arrays of those names get "_2"
    assert "_jsonreshape_2" in rep.tables and "_wrapper_2" in rep.tables
    meta = q(db, "select name, path, parent, kind, rows from _jsonreshape order by rowid")   # plain column names
    assert sorted(meta, key=str) == sorted([
        ("records", "$", None, "records", 3),
        ("orders", "orders[*]", "records", "array", 2),
        ("orders.items", "orders[*].items[*]", "orders", "array", 2),
        ("tags", "tags[*]", "records", "array", 1),
        ("matrix", "matrix[*]", "records", "array", 2),
        ("matrix[*]", "matrix[*][*]", "matrix", "array", 3),
        ("_jsonreshape_2", "_jsonreshape[*]", "records", "array", 2),
        ("_wrapper_2", "_wrapper[*]", "records", "array", 1),
    ], key=str)
    assert [r[0] for r in meta] == rep.tables                # in the order the tables were made
    for name, _path, parent, _kind, rows in meta:
        assert q(db, f'select count(*) from "{name}"') == [(rows,)]
        if parent is not None:      # declared relation: PRAGMA foreign_key_list names the parent table
            fks = q(db, f"pragma foreign_key_list(\"{name}\")")
            assert [(r[2], r[3], r[4]) for r in fks] == [(parent, "_parent", "_id")]
            orphans = q(db, f'select count(*) from "{name}" c left join "{parent}" p on c._parent = p._id '
                            "where p._id is null")
            assert orphans == [(0,)]
    assert q(db, "pragma foreign_key_check") == []
    # the wrapper, with null in place of the records
    (path, text), = q(db, "select path, json from _wrapper")
    assert path == "data" and codec.loads(text.encode()) == {"meta": {"v": 1, "who": "Ali"}, "data": None,
                                                             "tail": {"n": 3}}
    # no _ord for the file order
    assert "_ord" not in [r[1] for r in q(db, "pragma table_info(records)")]


def test_sqlite_wrapper_cases(tmp_path, docs, out_dir, lang_en) -> None:
    # an edited wrapper is stored as it is shown
    doc, _ob, exp = make_edited(tmp_path / "e", "wrapped", "wrapper", n=8, seed=3)
    docs += [doc, exp]
    export(doc, out_dir / "e.sqlite", ExportOptions("sqlite"))
    (_p, text), = q(out_dir / "e.sqlite", "select path, json from _wrapper")
    assert codec.loads(text.encode())["extra"] == {"added": True}
    # a wrapper bigger than large_node_bytes is not stored: a notice says so
    big = b'{"meta": "' + b"m" * 400 + b'", "data": [{"a": 1}, {"a": 2}]}'
    huge = open_bytes(tmp_path, docs, big, name="big.json", settings=Settings(large_node_bytes=200),
                      format="wrapped", record_path="data")
    rep = export(huge, out_dir / "h.sqlite", ExportOptions("sqlite"))
    assert rep.warning_keys == ["writer.note.wrapper_not_written"] and "SQLite" in rep.warnings[0]
    assert q(out_dir / "h.sqlite", "select count(*) from sqlite_master where name = '_wrapper'") == [(0,)]
    # keep_wrapper=False: the records alone, no notice; other kinds: no _wrapper table
    rep = export(huge, out_dir / "k.sqlite", ExportOptions("sqlite", keep_wrapper=False))
    assert rep.warnings == []
    arr = open_bytes(tmp_path, docs, b'[{"a": 1}]', name="a.json")
    export(arr, out_dir / "a.sqlite", ExportOptions("sqlite"))
    assert q(out_dir / "a.sqlite", "select name from sqlite_master where type = 'table' order by name") == [
        ("_jsonreshape",), ("records",)]


def test_sqlite_order_column(tmp_path, docs, out_dir) -> None:
    doc = open_bytes(tmp_path, docs, b"[" + b",".join(b'{"i": %d}' % i for i in range(10)) + b"]")
    ascending = (None, range(2, 8), [0, 3, 9], RecordSet.from_indices([1, 2, 5]), range(9, -1, -1)[::-1])
    for k, src in enumerate(ascending):
        export(doc, out_dir / f"a{k}.sqlite", ExportOptions("sqlite"), source=src)
        assert "_ord" not in [r[1] for r in q(out_dir / f"a{k}.sqlite", "pragma table_info(records)")], src
    for k, src in enumerate(([3, 1, 2], range(9, 0, -2), RecordSet.from_indices([8, 0, 4]), [5, 5, 1])):
        db = out_dir / f"o{k}.sqlite"
        export(doc, db, ExportOptions("sqlite"), source=src)
        cols = [r[1] for r in q(db, "pragma table_info(records)")]
        assert cols[:2] == ["_id", "_ord"]
        want = list(dict.fromkeys(src))            # a record selected twice is written once
        assert [r[0] for r in q(db, "select _id from records order by _ord")] == want
        assert [r[0] for r in q(db, "select _ord from records order by _ord")] == list(range(len(want)))


# ----------------------------------------------------------------------------------
# the wrapper notice
# ----------------------------------------------------------------------------------
def test_wrapper_notice(tmp_path, docs, out_dir, lang_en) -> None:
    wrapped = make_source(tmp_path / "w.json", "wrapped", n=5, seed=1)
    keyed = make_source(tmp_path / "k.json", "keyed", n=5, seed=1)
    docs += [wrapped, keyed]
    for fmt in ("jsonl", "csv", "xlsx"):
        rep = export(wrapped, out_dir / f"w.{fmt}", ExportOptions(fmt))
        assert rep.warning_keys == ["writer.note.wrapper_not_written"], fmt
        assert "data" in rep.warnings[0] and writer.FORMAT_NAMES[fmt] in rep.warnings[0]
        assert export(wrapped, out_dir / f"n.{fmt}", ExportOptions(fmt, keep_wrapper=False)).warnings == []
        assert export(keyed, out_dir / f"k.{fmt}", ExportOptions(fmt)).warnings == []
    assert export(wrapped, out_dir / "w.json", ExportOptions("json")).warnings == []


# ----------------------------------------------------------------------------------
# gzip outputs
# ----------------------------------------------------------------------------------
def test_format_for_gzip_names() -> None:
    assert format_for_path("a.json.gz") == "json" and format_for_path("A.JSONL.GZ") == "jsonl"
    assert format_for_path("x.ndjson.gz") == "jsonl" and format_for_path("t.csv.gz") == "csv"
    assert format_for_path("b.xlsx.gz") is None and format_for_path("c.sqlite.gz") is None
    assert format_for_path("d.gz") is None and format_for_path("e.json") == "json"
    assert writer.gzip_path("x.CSV.GZ") and not writer.gzip_path("x.csv")


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv"])
def test_gzip_outputs(tmp_path, docs, out_dir, fmt) -> None:
    doc = make_source(tmp_path / "s.json", "array", n=30, seed=4, style="pretty2")
    docs.append(doc)
    plain = export(doc, out_dir / f"p.{fmt}", ExportOptions(fmt))
    rep = export(doc, out_dir / f"p.{fmt}.gz", ExportOptions(fmt))
    raw = (out_dir / f"p.{fmt}.gz").read_bytes()
    assert raw[:2] == b"\x1f\x8b" and gzip.decompress(raw) == (out_dir / f"p.{fmt}").read_bytes()
    assert rep.records == plain.records and rep.bytes_written == len(raw)
    # explicit choice wins over the name
    export(doc, out_dir / f"q.{fmt}", ExportOptions(fmt), gzip=True)
    assert (out_dir / f"q.{fmt}").read_bytes()[:2] == b"\x1f\x8b"
    export(doc, out_dir / f"r.{fmt}.gz", ExportOptions(fmt), gzip=False)
    assert (out_dir / f"r.{fmt}.gz").read_bytes() == (out_dir / f"p.{fmt}").read_bytes()
    if fmt != "csv":
        back = Document.open(out_dir / f"p.{fmt}.gz")
        back.build_index()
        assert back.record_count == 30 and back.info.compression == "gzip"
        back.close()


def test_gzip_refused_for_xlsx_and_sqlite(tmp_path, docs, out_dir, lang_en) -> None:
    doc = make_source(tmp_path / "s.json", "array", n=3)
    docs.append(doc)
    for fmt in ("xlsx", "sqlite"):
        with pytest.raises(UserInputError) as ei:
            export(doc, out_dir / f"x.{fmt}.gz", ExportOptions(fmt))
        assert ei.value.key == "writer.error.gzip_format" and ei.value.hint
        with pytest.raises(UserInputError):
            export(doc, out_dir / f"x.{fmt}", ExportOptions(fmt), gzip=True)
    assert not list(out_dir.iterdir())


# ----------------------------------------------------------------------------------
# column plans
# ----------------------------------------------------------------------------------
def read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))


def test_column_plan_gives_every_part_one_header(tmp_path, docs, out_dir) -> None:
    recs = [{"id": i, "a" if i < 5 else "b": i, "nested": {"x": i} if i % 2 else None} for i in range(10)]
    doc = open_bytes(tmp_path, docs, codec.dumps(recs))
    opts = ExportOptions("csv")
    plan = plan_columns(doc, opts)
    whole = export(doc, out_dir / "all.csv", opts)
    assert plan.header == whole.columns
    rows = []
    for k, part in enumerate((range(0, 5), range(5, 10))):
        rep = export(doc, out_dir / f"p{k}.csv", opts, source=part, column_plan=plan)
        assert rep.columns == plan.header
        got = read_csv(out_dir / f"p{k}.csv")
        assert got[0] == plan.header
        rows += got[1:]
    assert rows == read_csv(out_dir / "all.csv")[1:]
    # without the plan the parts would differ
    assert export(doc, out_dir / "x.csv", opts, source=range(0, 5)).columns != plan.header
    # map columns found by the plan's scan are reported by the plan
    wide = [{"id": i, "m": {f"k{i}_{j}": j for j in range(3)}} for i in range(200)]
    wdoc = open_bytes(tmp_path, docs, codec.dumps(wide), name="wide.json")
    wplan = plan_columns(wdoc, ExportOptions("xlsx"))
    assert wplan.warning_keys == ["writer.warn.map_columns"]
    rep = export(wdoc, out_dir / "w.xlsx", ExportOptions("xlsx"), source=range(100), column_plan=wplan)
    assert rep.warnings == [] and rep.columns == wplan.header
    with pytest.raises(UserInputError):
        plan_columns(doc, ExportOptions("json"))


# ----------------------------------------------------------------------------------
# XLSX parts through fileio.commit_files
# ----------------------------------------------------------------------------------
@pytest.mark.skipif(not WINDOWS, reason="open files block replacing only on Windows")
def test_xlsx_parts_replace_a_part_this_program_reads(tmp_path, docs, out_dir) -> None:
    doc = open_bytes(tmp_path, docs, codec.dumps([{"i": i} for i in range(30)]))
    opts = ExportOptions("xlsx", xlsx_split="files", xlsx_max_rows=11)
    export(doc, out_dir / "big.xlsx", opts)
    old = (out_dir / "big_2.xlsx").read_bytes()
    with open_shared(out_dir / "big_2.xlsx") as f:       # shared handles (our own reader) do not block
        rep = export(doc, out_dir / "big.xlsx", opts, source=list(range(29, -1, -1)))
        assert f.read() == old
    assert [p.name for p in rep.files] == ["big_1.xlsx", "big_2.xlsx", "big_3.xlsx"]
    assert (out_dir / "big_2.xlsx").read_bytes() != old
    assert sorted(p.name for p in out_dir.iterdir()) == ["big_1.xlsx", "big_2.xlsx", "big_3.xlsx"]
