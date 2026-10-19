"""Consumers of an edited document (docs/ARCHITECTURE.md §12.3).

The oracle again: on an edited document (``tests/overlay_helpers.py``) every consumer must give
exactly what it gives on a plain document of the expected content -- ``filter_records`` (extract and
parse), ``sort_records``, ``search`` (byte mode with both confirm paths, parse mode, translit, number
and regex queries) and ``iter_hits`` and ``reference_search``, ``infer_schema``, ``writer.export``
(json / jsonl / csv / xlsx / sqlite) and ``discover_columns``, ``dupes.find_duplicates``, and the
stream functions on edited huge records -- for all 7 kinds (+ a wrapped object container), both
core backends of the reader. Plus: results carry the pinned state's ``gen``; a ``source`` of another
state is refused (``edits.error.stale_scope``); entry points pin the document (a state swapped while
they run is not seen); an edit that introduces ``\\u`` escapes switches the search to parse mode.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import editops, reader
from jsonreshape import search as search_mod
from jsonreshape.core import FLAG_U_ESCAPES
from jsonreshape.dupes import find_duplicates
from jsonreshape.errors import EditError
from jsonreshape.recordset import RecordSet, RecordSetBuilder, filter_records, sort_records
from jsonreshape.schema import infer_schema
from jsonreshape.search import SearchOptions, iter_hits, reference_search, search
from jsonreshape.settings import Settings
from jsonreshape.tasks import Progress
from jsonreshape.writer import ExportOptions, discover_columns, export
from overlay_helpers import KINDS, OverlayBuilder, make_edited, make_source

CASES = [(kind, scenario) for kind in KINDS for scenario in ("structure", "mixed", "keys", "wrapper")
         if not (scenario == "wrapper" and kind not in ("wrapped", "wrapped_obj"))
         and not (scenario == "keys" and kind not in ("keyed", "wrapped_obj"))]

# ("mixed" holds the structural edits of "structure" as well: enough for the (slower) search matrix)
SEARCH_CASES = [c for c in CASES if c[1] != "structure" or c[0] in ("array", "single")]

FILTERS = [
    "id > 1010",
    'contact.address.city == "Toshkent"',
    "exists(esc)",
    "$index < 5 or $index >= 20",
    "name contains \"o'zbek\"",
    "orders[*].oid > 50",
    "score >= 0",
    "nums contains 1e400",
    "len(tags) >= 2",
]
MEMBER_FILTERS = ['$key startswith "new"', '$key contains "renamed"', '$key == "k3"']
SORTS = [["name"], ["-score", "id"], ["contact.address.city", "-id"], ["$"], ["$index"], ["-nums"]]
QUERIES: list[tuple[str, dict[str, Any]]] = [
    ("toshkent", {}),
    ("o'zbek", {}),
    ("ўзбек", {}),
    ("Ўзбек", {"case_sensitive": True}),
    ("tashkent", {"translit": True}),
    ("1e400", {}),
    ("1.10", {"mode": "number"}),
    ("^[A-Z]", {"mode": "regex"}),
    ("new", {"scope": "keys"}),
    ("renamed", {}),
    ("2", {"whole_word": True, "scope": "values"}),
    (" ", {}),
]


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)


@pytest.fixture
def pair(tmp_path, backend):
    opened: list[Any] = []

    def make(kind: str, scenario: str, **kw: Any) -> tuple[Any, OverlayBuilder, Any]:
        doc, ob, exp = make_edited(tmp_path / f"{kind}-{scenario}-{len(opened)}", kind, scenario, **kw)
        opened.extend((doc, exp))
        return doc, ob, exp

    yield make
    for d in opened:
        d.close()


def seed_of(kind: str, scenario: str) -> int:
    return KINDS.index(kind) * 7 + len(scenario)


def hit_tuple(h: Any) -> tuple:
    return (h.record, h.path, h.where, h.text, tuple(h.spans), h.key)


# ----------------------------------------------------------------------------------
# filter / sort
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["extract", "parse"])
@pytest.mark.parametrize("kind,scenario", CASES)
def test_filter_equals_plain_document(pair, kind, scenario, strategy) -> None:
    doc, ob, exp = pair(kind, scenario, seed=seed_of(kind, scenario))
    exprs = FILTERS + (MEMBER_FILTERS if doc.member_records else [])
    for text in exprs:
        got = filter_records(doc, text, strategy=strategy)
        want = filter_records(exp, text, strategy=strategy)
        assert list(got) == list(want), text
        assert got.gen == doc.gen != 0 and want.gen == 0
        assert got.report.matched == want.report.matched and got.report.scanned == want.report.scanned
    n = doc.record_count
    sub = RecordSet.from_indices(list(range(n - 1, -1, -2)))
    assert sub.gen is None
    for text in exprs[:4]:
        assert list(filter_records(doc, text, sub, strategy=strategy)) == list(
            filter_records(exp, text, sub, strategy=strategy))
    assert list(filter_records(doc, "id > 0", RecordSet.base(doc))) == list(filter_records(exp, "id > 0"))


@pytest.mark.parametrize("kind,scenario", CASES)
def test_sort_equals_plain_document(pair, kind, scenario) -> None:
    doc, ob, exp = pair(kind, scenario, seed=seed_of(kind, scenario))
    sorts = SORTS + ([["$key"], ["-$key", "id"]] if doc.member_records else [])
    for keys in sorts:
        got = sort_records(doc, keys)
        want = sort_records(exp, keys)
        assert list(got) == list(want), keys
        assert got.gen == doc.gen and got.report.records == doc.record_count
    n = doc.record_count
    sub = [i for i in range(n) if i % 3 != 1]
    assert list(sort_records(doc, ["name"], sub)) == list(sort_records(exp, ["name"], sub))


# ----------------------------------------------------------------------------------
# search
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("locate", [True, False])
@pytest.mark.parametrize("kind,scenario", SEARCH_CASES)
def test_search_equals_plain_document(pair, monkeypatch, kind, scenario, locate) -> None:
    monkeypatch.setattr(search_mod, "LOCATE", locate)
    doc, ob, exp = pair(kind, scenario, seed=seed_of(kind, scenario))
    modes = set()
    for text, kw in QUERIES:
        opts = SearchOptions.build(text, **kw)
        got = search(doc, opts, max_hits=None)
        want = search(exp, opts, max_hits=None)
        assert [hit_tuple(h) for h in got.hits] == [hit_tuple(h) for h in want.hits], text
        assert [hit_tuple(h) for h in got.hits] == [hit_tuple(h) for h in reference_search(doc, opts)]
        assert [hit_tuple(h) for h in iter_hits(doc, opts)] == [hit_tuple(h) for h in got.hits]
        assert list(got.records) == list(want.records) and got.records.gen == doc.gen
        modes.add(got.mode_used)
        got.close()
        want.close()
    if not doc.flags & FLAG_U_ESCAPES:
        assert "bytes" in modes
    n = doc.record_count
    sub = RecordSet.from_indices([i for i in range(n) if i % 2])
    opts = SearchOptions.build("toshkent")
    assert [hit_tuple(h) for h in search(doc, opts, sub).hits] == [hit_tuple(h) for h in search(exp, opts, sub).hits]


def test_u_escapes_in_an_edit_switch_the_search_to_parse_mode(tmp_path, backend) -> None:
    p = tmp_path / "plain.json"
    p.write_bytes(b'[{"a": "summer"}, {"a": "x"}, {"a": "yoz"}]')
    doc = reader.Document.open(p)
    try:
        doc.build_index()
        opts = SearchOptions.build("été")
        assert search(doc, opts).mode_used == "bytes" and not search(doc, opts).hits
        ob = OverlayBuilder(doc, tmp_path / "layer")
        ob.set(1, b'{"a": "\\u00e9t\\u00e9"}')
        doc.set_overlay(ob.state())
        assert doc.flags & FLAG_U_ESCAPES
        res = search(doc, opts)
        assert res.mode_used == "parse" and [h.record for h in res.hits] == [1]
        assert [h.record for h in reference_search(doc, opts)] == [1]
    finally:
        doc.close()


# ----------------------------------------------------------------------------------
# schema, export, columns, duplicates
# ----------------------------------------------------------------------------------
def schema_dict(s: Any) -> dict:
    d = s.to_dict()
    d.pop("elapsed")
    return d


@pytest.mark.parametrize("kind,scenario", CASES)
def test_schema_equals_plain_document(pair, kind, scenario) -> None:
    doc, ob, exp = pair(kind, scenario, seed=seed_of(kind, scenario))
    assert schema_dict(infer_schema(doc, sample=None)) == schema_dict(infer_schema(exp, sample=None))
    assert schema_dict(infer_schema(doc, sample=7)) == schema_dict(infer_schema(exp, sample=7))
    sub = RecordSet.from_indices(list(range(0, doc.record_count, 2)))
    assert schema_dict(infer_schema(doc, sub, sample=None)) == schema_dict(infer_schema(exp, sub, sample=None))


def read_xlsx(path: Path) -> list[list[Any]]:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        return [[list(row) for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    finally:
        wb.close()


def read_sqlite(path: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(path)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: list(con.execute('SELECT * FROM "%s"' % n.replace('"', '""'))) for n in names}
    finally:
        con.close()


@pytest.mark.slow
@pytest.mark.parametrize("kind,scenario", CASES)
def test_export_equals_plain_document(pair, tmp_path, kind, scenario) -> None:
    doc, ob, exp = pair(kind, scenario, seed=seed_of(kind, scenario))
    out = tmp_path / "out"
    out.mkdir()
    n = doc.record_count
    for opts in (ExportOptions("json"), ExportOptions("json", indent=None), ExportOptions("json", keep_wrapper=False),
                 ExportOptions("json", sort_keys=True), ExportOptions("jsonl"), ExportOptions("csv"),
                 ExportOptions("csv", columns=["id", "name", "tags"]), ExportOptions("csv", array_mode="rows",
                                                                                     explode="orders")):
        a = export(doc, out / f"a.{opts.fmt}", opts)
        b = export(exp, out / f"b.{opts.fmt}", opts)
        assert (out / f"a.{opts.fmt}").read_bytes() == (out / f"b.{opts.fmt}").read_bytes(), opts
        assert a.records == b.records and a.warning_keys == b.warning_keys
    sub = [i for i in range(n) if i % 3 == 0][::-1]
    export(doc, out / "s.json", ExportOptions("json"), sub)
    export(exp, out / "t.json", ExportOptions("json"), sub)
    assert (out / "s.json").read_bytes() == (out / "t.json").read_bytes()
    export(doc, out / "a.xlsx", ExportOptions("xlsx"))
    export(exp, out / "b.xlsx", ExportOptions("xlsx"))
    assert read_xlsx(out / "a.xlsx") == read_xlsx(out / "b.xlsx")
    export(doc, out / "a.sqlite", ExportOptions("sqlite"))
    export(exp, out / "b.sqlite", ExportOptions("sqlite"))
    assert read_sqlite(out / "a.sqlite") == read_sqlite(out / "b.sqlite")
    assert discover_columns(doc) == discover_columns(exp)
    assert discover_columns(doc, sample=5) == discover_columns(exp, sample=5)
    assert discover_columns(doc, sub, array_mode="rows") == discover_columns(exp, sub, array_mode="rows")


@pytest.mark.parametrize("kind,scenario", CASES)
def test_duplicates_equal_plain_document(pair, kind, scenario) -> None:
    doc, ob, exp = pair(kind, scenario, seed=seed_of(kind, scenario))
    keys = ["name", "id", "contact.address.city", "$"] + (["$key"] if doc.member_records else [])
    for key in keys:
        for keep in ("first", "last"):
            got = find_duplicates(doc, key, keep=keep)
            want = find_duplicates(exp, key, keep=keep)
            assert (got.groups, got.duplicates, got.records_with_key, got.missing) == (
                want.groups, want.duplicates, want.records_with_key, want.missing), key
            assert list(got.records) == list(want.records) and got.examples == want.examples
            assert got.records.gen == doc.gen and got.gen == doc.gen


# ----------------------------------------------------------------------------------
# edited huge records (streamed)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped_obj", "stream"])
def test_consumers_on_edited_huge_records(pair, tmp_path, kind) -> None:
    doc, ob, exp = pair(kind, "mixed", seed=11, settings=Settings(large_node_bytes=230))
    large = doc.settings.large_node_bytes
    assert any(len(r) > large and doc.record_span(i)[0] >= reader.EDIT_BASE for i, r in enumerate(ob.records))
    for text in FILTERS + (MEMBER_FILTERS if doc.member_records else []):
        got, want = filter_records(doc, text), filter_records(exp, text)
        assert list(got) == list(want), text
        assert got.report.skipped_huge == want.report.skipped_huge
    for keys in SORTS:
        assert list(sort_records(doc, keys)) == list(sort_records(exp, keys)), keys
    for text, kw in QUERIES:
        opts = SearchOptions.build(text, **kw)
        assert [hit_tuple(h) for h in search(doc, opts).hits] == [hit_tuple(h) for h in search(exp, opts).hits]
        assert [hit_tuple(h) for h in reference_search(doc, opts)] == [hit_tuple(h) for h in reference_search(exp, opts)]
    assert schema_dict(infer_schema(doc, sample=None)) == schema_dict(infer_schema(exp, sample=None))
    out = tmp_path / "huge"
    out.mkdir()
    for opts in (ExportOptions("json"), ExportOptions("jsonl"), ExportOptions("csv", columns=["id", "name", "orders"])):
        export(doc, out / f"a.{opts.fmt}", opts)
        export(exp, out / f"b.{opts.fmt}", opts)
        assert (out / f"a.{opts.fmt}").read_bytes() == (out / f"b.{opts.fmt}").read_bytes()
    for key in ("name", "id", "$"):
        assert list(find_duplicates(doc, key).records) == list(find_duplicates(exp, key).records)


def test_edited_huge_wrapper_and_single_root_are_streamed(pair, tmp_path) -> None:
    out = tmp_path / "w"
    out.mkdir()
    doc, ob, exp = pair("wrapped", "wrapper", seed=2, settings=Settings(large_node_bytes=100))
    export(doc, out / "a.json", ExportOptions("json"))
    export(exp, out / "b.json", ExportOptions("json"))
    assert (out / "a.json").read_bytes() == (out / "b.json").read_bytes()
    doc, ob, exp = pair("single", "point", seed=3, settings=Settings(large_node_bytes=50))
    assert doc.is_huge(0) and doc.record_span(0)[0] >= reader.EDIT_BASE
    export(doc, out / "c.json", ExportOptions("json"))
    export(exp, out / "d.json", ExportOptions("json"))
    assert (out / "c.json").read_bytes() == (out / "d.json").read_bytes()
    assert [hit_tuple(h) for h in search(doc, SearchOptions.build("o'zbek")).hits] == [
        hit_tuple(h) for h in search(exp, SearchOptions.build("o'zbek")).hits]


# ----------------------------------------------------------------------------------
# gen, stale scopes, pinning
# ----------------------------------------------------------------------------------
def test_record_set_gen_and_base_sets(tmp_path, backend) -> None:
    doc = make_source(tmp_path / "a.json", "array", n=10)
    try:
        assert RecordSet.from_indices([1, 2]).gen is None
        assert RecordSet.from_indices([1, 2], gen=5).gen == 5
        b = RecordSetBuilder("manual", gen=7)
        b.extend([3, 1])
        rs = b.finish()
        assert rs.gen == 7 and "gen=7" in repr(rs)
        rs.gen = 8
        assert rs.gen == 8
        base = RecordSet.base(doc)
        assert base.gen is None and len(base) == 10
        ob = OverlayBuilder(doc, tmp_path / "layer")
        ob.delete(0, 1)
        doc.set_overlay(ob.state())
        view = doc.pinned()
        vbase = RecordSet.base(view)
        assert len(base) == 8 and len(vbase) == 8
        ob.delete(0)
        doc.set_overlay(ob.state())
        assert len(base) == 7 and len(vbase) == 8          # a view's base set is frozen
        assert base._is_base_of(view) and vbase._is_base_of(doc) and vbase._is_base_of(doc.pinned(None))
        other = make_source(tmp_path / "b.json", "array", n=10)
        try:
            assert not base._is_base_of(other)
        finally:
            other.close()
        # the base set of the live document means "all records" of a pinned state
        assert list(filter_records(view, "id >= 0", base)) == list(range(8))
    finally:
        doc.close()


def test_stale_sources_are_refused(tmp_path, backend) -> None:
    doc = make_source(tmp_path / "a.json", "keyed", n=12)
    try:
        ob = OverlayBuilder(doc, tmp_path / "layer")
        ob.set(1, b'"k1": {"id": 1}')
        doc.set_overlay(ob.state())
        rs = filter_records(doc, "exists(id)")
        assert rs.gen == doc.gen
        ob.delete(0)
        doc.set_overlay(ob.state())
        opts = SearchOptions.build("a")
        out = tmp_path / "o.json"
        calls = [
            lambda s: filter_records(doc, "id > 0", s),
            lambda s: sort_records(doc, ["id"], s),
            lambda s: search(doc, opts, s),
            lambda s: iter_hits(doc, opts, s),
            lambda s: reference_search(doc, opts, s),
            lambda s: infer_schema(doc, s),
            lambda s: export(doc, out, ExportOptions("json"), s),
            lambda s: discover_columns(doc, s),
            lambda s: find_duplicates(doc, "id", s),
        ]
        for call in calls:
            with pytest.raises(EditError) as ei:
                call(rs)
            assert ei.value.key == "edits.error.stale_scope" and ei.value.hint
        fresh = filter_records(doc, "exists(id)")
        assert fresh.gen == doc.gen
        anystate = RecordSet.from_indices([0, 2, 4])   # gen None: "valid for any state"
        for source in (fresh, anystate, RecordSet.base(doc), [0, 1], range(3)):
            for call in calls:
                result = call(source)
                if hasattr(result, "__next__"):
                    list(result)
    finally:
        doc.close()


def test_entry_points_pin_the_document(tmp_path, backend) -> None:
    doc = make_source(tmp_path / "a.json", "array", n=400)
    try:
        ob = OverlayBuilder(doc, tmp_path / "layer")
        ob.bulk_every(2, lambda p, old: b'{"id": %d, "name": "toshkent"}' % (10_000 + p))
        s1 = ob.state()
        ob.delete(*range(0, 200))
        s2 = ob.state()
        doc.set_overlay(s1)
        expected_filter = list(filter_records(doc.pinned(s1), "id >= 10000"))
        swaps = []

        def swap(*_a: Any) -> None:
            if not swaps:
                swaps.append(1)
                doc.set_overlay(s2)

        rs = filter_records(doc, "id >= 10000", on_batch=swap, block_bytes=2048, batch_interval=0.0)
        assert swaps and list(rs) == expected_filter and rs.gen == s1.gen
        doc.set_overlay(s1)
        swaps.clear()
        want = list(sort_records(doc.pinned(s1), ["-id"]))
        got = sort_records(doc, ["-id"], progress=Progress(callback=swap, min_interval=0), block_bytes=2048)
        assert swaps and list(got) == want
        doc.set_overlay(s1)
        swaps.clear()
        opts = SearchOptions.build("toshkent")
        want_hits = [hit_tuple(h) for h in search(doc.pinned(s1), opts, max_hits=None).hits]
        res = search(doc, opts, max_hits=None, on_hits=swap, block_bytes=2048)
        assert swaps and [hit_tuple(h) for h in res.hits] == want_hits
        doc.set_overlay(s1)
        swaps.clear()
        it = iter_hits(doc, opts)          # pinned when called
        doc.set_overlay(s2)
        assert [hit_tuple(h) for h in it] == want_hits
        doc.set_overlay(s1)
        swaps.clear()
        export(doc.pinned(s1), tmp_path / "want.jsonl", ExportOptions("jsonl"))
        export(doc, tmp_path / "got.jsonl", ExportOptions("jsonl"), progress=Progress(callback=swap, min_interval=0))
        assert swaps and (tmp_path / "got.jsonl").read_bytes() == (tmp_path / "want.jsonl").read_bytes()
        doc.set_overlay(s1)
        swaps.clear()
        want_schema = schema_dict(infer_schema(doc.pinned(s1), sample=None))
        got_schema = infer_schema(doc, sample=None, progress=Progress(callback=swap, min_interval=0))
        assert swaps and schema_dict(got_schema) == want_schema
        doc.set_overlay(s1)
        rep = find_duplicates(doc, "name", progress=Progress(callback=lambda *_: doc.set_overlay(s2), min_interval=0))
        assert rep.gen == s1.gen and rep.records_with_key == 400
    finally:
        doc.close()
