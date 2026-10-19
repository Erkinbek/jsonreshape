"""jsonreshape.diff: less common paths (docs/ARCHITECTURE.md §14.6).

* the Node path: array-key matching with moves, children over the value limit (fingerprints and
  tokens streamed, values omitted), each equal to / consistent with the in-memory diff;
* lone member records over the value limit, the wrapper over the value limit, position mode in
  several batches, keys of whole elements (``array_keys=("$",)``), ``numbers="text"`` tokens of
  values orjson cannot write (lone surrogates);
* ``write_diff`` / ``record_dict``: aligned paths (``a_pointer`` / ``b_pointer``), truncated
  record diffs, the wrapper scope, omitted values and long cells in CSV, rows without details.
"""

from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, diff
from jsonreshape.diff import (
    DiffOptions,
    Omitted,
    RecordDiff,
    diff_files,
    diff_nodes,
    diff_values,
    record_dict,
    write_diff,
)
from jsonreshape.errors import UserInputError
from jsonreshape.paths import MISSING
from jsonreshape.settings import Settings
from test_dupes import make_doc  # noqa: F401 - fixture used by name


def plain(vd: diff.ValueDiff) -> list[tuple]:
    out = []
    for c in vd.changes:
        vals = []
        for v in (c.a, c.b):
            if v is MISSING:
                vals.append(None)
            elif isinstance(v, Omitted):
                vals.append(("omitted", v.kind, v.size))
            else:
                vals.append(codec.dumps(v).decode())
        out.append((c.op, c.a_path, c.b_path, *vals))
    return out


def run(a: Any, b: Any, **kw: Any) -> tuple[list[RecordDiff], diff.DiffReport]:
    rds: list[RecordDiff] = []
    rep = diff_files(a, b, on_record=rds.append, **kw)
    return rds, rep


# ----------------------------------------------------------------------------------
# the Node path
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(4))
def test_node_path_key_matching_with_moves_equals_in_memory(make_doc, seed: int) -> None:
    rng = random.Random(seed)
    va = {"items": [{"id": i, "v": i, "pad": "p" * 20} for i in range(40)], "tags": ["a", "b", "c"]}
    vb = codec.loads(codec.dumps(va))
    items = vb["items"]
    for _ in range(4):                                      # moves
        k = rng.randrange(len(items))
        x = items.pop(k)
        items.insert(rng.randrange(len(items) + 1), x)
    items[rng.randrange(len(items))]["v"] = "changed"
    del items[rng.randrange(len(items))]
    items.insert(rng.randrange(len(items)), {"id": "new", "v": 0})
    items.append({"id": 3, "v": "duplicate id"})          # the i-th 3 with the i-th 3
    vb["tags"] = ["c", "a", "b"]
    opts = DiffOptions(array_keys=("items=id", "tags=$"), max_changes=None)
    want = plain(diff_values(va, vb, opts))
    assert any(w[0] == "moved" for w in want)
    for large in (100, 400, None):
        settings = Settings(large_node_bytes=large) if large else None
        a = make_doc(codec.dumps(va), format="single", settings=settings)
        b = make_doc(codec.dumps(vb), format="single", settings=settings)
        assert plain(diff_nodes(a, a.root(), b, b.root(), opts)) == want, large


def test_unordered_arrays(make_doc) -> None:
    """``array_match="unordered"``: multisets -- reordering is no change, the i-th of equal
    elements pairs with the i-th, changed elements are removed + added, no moves."""
    u = DiffOptions(array_match="unordered", max_changes=None)
    assert diff_values([1, 2, 3, {"a": [1, 2]}], [{"a": [1, 2]}, 3, 1.0, 2], u).equal
    assert plain(diff_values([1, 2, 2, "x"], [2, "y", 1], u)) == [
        ("removed", (2,), None, "2", None), ("removed", (3,), None, '"x"', None), ("added", None, (1,), None, '"y"')]
    vd = diff_values({"list": [{"id": 1, "v": 1}, {"id": 2}]}, {"list": [{"id": 2}, {"id": 1, "v": 9}]}, u)
    assert vd.counts == {"added": 1, "removed": 1, "changed": 0, "moved": 0}
    # array_keys rules still win where they apply
    keyed = DiffOptions(array_match="unordered", array_keys=("list=id",))
    vd = diff_values({"list": [{"id": 1, "v": 1}, {"id": 2}], "tags": ["a", "b"]},
                     {"list": [{"id": 2}, {"id": 1, "v": 9}], "tags": ["b", "a"]}, keyed)
    assert vd.counts == {"added": 0, "removed": 0, "changed": 1, "moved": 1}
    assert DiffOptions(array_match=" Unordered ").array_match == "unordered"
    # the Node path gives the same
    va = {"items": [{"k": i % 7, "pad": "z" * 30} for i in range(40)], "n": [3, 1, 2]}
    vb = codec.loads(codec.dumps(va))
    vb["items"].reverse()
    vb["items"][5]["pad"] = "changed"
    vb["n"] = [1, 2, 3, 3]
    want = plain(diff_values(va, vb, u))
    assert want and not any(w[0] == "moved" for w in want)
    for large in (100, None):
        settings = Settings(large_node_bytes=large) if large else None
        a = make_doc(codec.dumps(va), format="single", settings=settings)
        b = make_doc(codec.dumps(vb), format="single", settings=settings)
        assert plain(diff_nodes(a, a.root(), b, b.root(), u)) == want, large


def test_whole_element_keys(make_doc) -> None:
    """``array_keys=("$",)``: elements matched by their whole value (moves of scalars)."""
    opts = DiffOptions(array_keys=("$",))
    vd = diff_values(["x", "y", "z", 1], ["z", "x", "y", 1.0, "w"], opts)
    assert plain(vd) == [("moved", (2,), (0,), None, None), ("added", None, (4,), None, '"w"')]
    assert diff_values([{"a": 1}, {"b": 2}], [{"b": 2}, {"a": 1}], opts).counts["moved"] == 1


def test_children_over_the_value_limit(make_doc, monkeypatch) -> None:
    """Children larger than the value limit are fingerprinted and tokenized by streaming, never
    parsed whole; a changed big child is reported with Omitted values."""
    monkeypatch.setattr(diff, "VALUE_DIFF_MAX", 300)
    big = {"blob": "x" * 500, "n": 1}
    va = [{"i": 0}, big, {"i": 2}, [1, 2, 3], {"i": 4}, big, {"i": 6}]
    vb = [{"i": "front"}, {"i": 0}, big, {"i": 2}, [1, 2, 3], {"i": 4}, {**big, "n": 2}, {"i": 6}]
    a = make_doc(codec.dumps(va), format="single", settings=Settings(large_node_bytes=100))
    b = make_doc(codec.dumps(vb), format="single", settings=Settings(large_node_bytes=100))
    got = plain(diff_nodes(a, a.root(), b, b.root()))
    assert got[0] == ("added", None, (0,), None, '{"i":"front"}')
    # the big pair is compared below the value limit member by member: only "n" differs
    assert got[1:] == [("changed", (5, "n"), (6, "n"), "1", "2")]
    # a scalar child over the limit, changed: compared by raw text, shown as Omitted
    sa = make_doc(codec.dumps(["s" * 400, 1]), format="single", settings=Settings(large_node_bytes=100))
    sb = make_doc(codec.dumps(["s" * 399 + "t", 1]), format="single", settings=Settings(large_node_bytes=100))
    got = plain(diff_nodes(sa, sa.root(), sb, sb.root()))
    assert got == [("changed", (0,), (0,), ("omitted", "string", 402), ("omitted", "string", 402))]
    # arrays of big children only: the alignment works on streamed token hashes
    many_a = [{"k": i, "pad": "y" * 400} for i in range(12)]
    many_b = many_a[:3] + [{"k": "x"}] + many_a[3:]
    ma = make_doc(codec.dumps(many_a), format="single", settings=Settings(large_node_bytes=100))
    mb = make_doc(codec.dumps(many_b), format="single", settings=Settings(large_node_bytes=100))
    assert plain(diff_nodes(ma, ma.root(), mb, mb.root())) == [("added", None, (3,), None, '{"k":"x"}')]


# ----------------------------------------------------------------------------------
# records and wrappers over the value limit, batches
# ----------------------------------------------------------------------------------
def test_lone_member_records_over_the_value_limit(make_doc) -> None:
    keys = [f"r{i}" for i in range(6)]
    vals = [{"i": i, "blob": list(range(50))} for i in range(6)]
    small = Settings(large_node_bytes=100)
    a = make_doc(vals, "keyed", format="keyed", keys=keys, settings=small)
    b = make_doc(vals[:4] + [{"i": 9}], "keyed", format="keyed", keys=keys[:4] + ["new"], settings=small)
    # $key (auto): the member values; r4 and r5 removed, "new" added
    rds, rep = run(a, b)
    assert [(rd.status, rd.a_index, rd.b_index, rd.key) for rd in rds] == [
        ("removed", 4, None, "r4"), ("removed", 5, None, "r5"), ("added", None, 4, "new")]
    c = rds[0].diff.changes[0]
    assert (c.op, c.a_path) == ("removed", ()) and codec.dumps(c.a) == codec.dumps(vals[4])
    # align: {key: value} records; r4 is paired with "new" (position-wise), r5 is removed
    rds, rep = run(a, b, mode="align")
    assert [(rd.status, rd.a_index, rd.b_index) for rd in rds] == [("changed", 4, 4), ("removed", 5, None)]
    assert [(c.op, c.a_path, c.b_path) for c in rds[0].diff.changes] == [("removed", ("r4",), None),
                                                                          ("added", None, ("new",))]
    c = rds[1].diff.changes[0]
    assert (c.op, c.a_path) == ("removed", ("r5",)) and codec.dumps(c.a) == codec.dumps(vals[5])


def test_wrapper_over_the_value_limit(make_doc, monkeypatch, lang_en) -> None:
    data = [{"id": i} for i in range(5)]
    a = make_doc(data, "wrapped", record_path="data")
    raw = jsongen.build_document(data, "wrapped").replace(b'"version":1', b'"version":2')
    b = make_doc(raw, record_path="data")
    monkeypatch.setattr(diff, "VALUE_DIFF_MAX", 10)
    rds, rep = run(a, b)
    assert rds == [] and rep.wrapper is None and rep.equal == 5
    assert rep.warnings == ["diff.warn.wrapper_too_large"]
    assert "too large" in rep.warning_texts()[0]


def test_position_mode_in_batches(make_doc, monkeypatch) -> None:
    rng = random.Random(3)
    a_recs = [{"id": i, "v": rng.randrange(5)} for i in range(300)]
    b_recs = [dict(r, v=rng.randrange(5)) for r in a_recs[:280]]
    a = make_doc(a_recs, "jsonl", format="jsonl")
    b = make_doc(b_recs, "array")
    want = run(a, b, mode="position")[0]
    monkeypatch.setattr(diff, "BATCH_RECORDS", 7)
    monkeypatch.setattr(diff, "BATCH_BYTES", 300)
    got, rep = run(a, b, mode="position")
    assert [(r.status, r.a_index, r.b_index) for r in got] == [(r.status, r.a_index, r.b_index) for r in want]
    assert [plain(r.diff) for r in got] == [plain(r.diff) for r in want]
    assert rep.removed == 20 and rep.equal + rep.changed == 280


def test_numbers_text_tokens_of_lone_surrogates() -> None:
    """``numbers="text"`` tokens fall back to codec.dumps where orjson refuses (lone surrogates)."""
    a = codec.loads(b'[["\\ud800", 1.10], "x", ["\\ud800", 2]]')
    b = codec.loads(b'["front", ["\\ud800", 1.10], "x", ["\\ud800", 2.0]]')
    vd = diff_values(a, b, DiffOptions(numbers="text"))
    assert plain(vd) == [("added", None, (0,), None, '"front"'), ("changed", (2, 1), (3, 1), "2", "2.0")]
    assert diff_values(a, b).counts == {"added": 1, "removed": 0, "changed": 0, "moved": 0}


def test_decimal_values() -> None:
    """Callers may pass ``Decimal`` numbers (exact): compared by value, or by text."""
    from decimal import Decimal

    a = [Decimal("1.10"), {"n": Decimal("1e400")}, 3]
    b = [Decimal("1.1"), {"n": codec.loads(b"10E399")}, Decimal(3)]
    assert diff_values(a, b).equal
    vd = diff_values(a, b, DiffOptions(numbers="text"))
    assert [(c.op, c.a_path) for c in vd.changes] == [("changed", (0,)), ("changed", (1, "n"))]
    assert diff_values([Decimal("2"), "x"], ["y", Decimal("2.0")]).counts["changed"] == 0


@pytest.mark.parametrize("kw", [{"key": "id"}, {"mode": "position"}])
def test_counts_without_details_through_the_node_path(make_doc, tmp_path: Path, kw: dict) -> None:
    """max_details=0: every pair is still decided exactly (the walk stops at the first change),
    also for records over the value limit (Node path)."""
    a_recs = [{"id": i, "blob": list(range(40)), "v": i} for i in range(30)]
    b_recs = [dict(r) for r in a_recs]
    for i in (3, 17, 29):
        b_recs[i]["v"] = -1
    small = Settings(large_node_bytes=100)
    a = make_doc(a_recs, "array", settings=small)
    b = make_doc(b_recs, "jsonl", format="jsonl", settings=small)
    full, rep_full = run(a, b, **kw)
    none, rep = run(a, b, max_details=0, **kw)
    assert [(r.status, r.a_index, r.b_index) for r in none] == [(r.status, r.a_index, r.b_index) for r in full]
    assert all(r.diff is None for r in none) and rep.details == 0
    assert (rep.equal, rep.changed) == (rep_full.equal, rep_full.changed) == (27, 3)


def test_document_mode_without_details(make_doc) -> None:
    a = make_doc(codec.dumps({"x": list(range(100)), "y": 1}), format="single", settings=Settings(large_node_bytes=50))
    b = make_doc(codec.dumps({"x": list(range(100)), "y": 2}), format="single", settings=Settings(large_node_bytes=50))
    rds, rep = run(a, b, max_details=0)
    assert [(r.status, r.scope, r.diff) for r in rds] == [("changed", "document", None)] and rep.changed == 1
    rds, rep = run(a, a, max_details=0)
    assert rds == [] and rep.equal == 1


# ----------------------------------------------------------------------------------
# write_diff / record_dict
# ----------------------------------------------------------------------------------
def test_record_dict_fields() -> None:
    vd = diff_values([{"a": 1}, {"b": 2}], [{"z": 0}, {"a": 1}, {"b": 3}], DiffOptions(max_changes=1))
    rd = RecordDiff("changed", 4, 7, "k", vd)
    obj = record_dict(rd)
    assert obj["truncated"] is True and obj["counts"] == {"added": 1, "removed": 0, "changed": 1, "moved": 0}
    assert obj["changes"] == [{"op": "added", "path": "[0]", "pointer": "/0", "b": {"z": 0}}]
    vd = diff_values(["x", {"b": 2}], ["new", "x", {"b": 3}])
    obj = record_dict(RecordDiff("changed", None, None, MISSING, vd, scope="wrapper"))
    assert obj["scope"] == "wrapper" and obj["key"] is None and obj["a"] is None
    shifted = obj["changes"][1]
    assert shifted == {"op": "changed", "path": "[1].b", "pointer": "/1/b", "a_pointer": "/1/b", "b_pointer": "/2/b",
                       "a": 2, "b": 3}


def test_write_diff_csv_cells(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(diff, "CSV_CELL_MAX", 20)
    rds = [
        RecordDiff("changed", 0, 0, "k1", diff.ValueDiff(changes=[
            diff.Change("changed", ("s",), ("s",), "x" * 50, Omitted("array", 9 << 20, "[1, 2"))])),
        RecordDiff("removed", 5, None),                     # beyond max_details: no changes
        RecordDiff("changed", None, None, MISSING, diff.ValueDiff(changes=[diff.Change("changed", ("v",), ("v",), 1, 2)]),
                   scope="wrapper"),
    ]
    assert write_diff(rds, tmp_path / "d.csv", "csv") == 3
    rows = list(csv.reader(io.StringIO((tmp_path / "d.csv").read_text(encoding="utf-8-sig"))))
    assert rows[1][:6] == ["changed", '"k1"', "0", "0", "changed", "s"]
    assert rows[1][6] == '"' + "x" * 18 + "…" and rows[1][7].startswith("[array, ")
    assert rows[2] == ["removed", "", "5", "", "removed", "", "", ""]
    assert rows[3] == ["changed:wrapper", "", "", "", "changed", "v", "1", "2"]
    # an error while writing: the target is not created, the generator is closed
    closed = []

    def gen():
        try:
            yield rds[0]
            raise RuntimeError("boom")
        finally:
            closed.append(True)

    with pytest.raises(RuntimeError):
        write_diff(gen(), tmp_path / "e.json", "json")
    assert closed and not (tmp_path / "e.json").exists()
    with pytest.raises(UserInputError):
        write_diff([], tmp_path / "x.json", 5)    # type: ignore[arg-type]
    assert not (tmp_path / "x.json").exists()
    # an empty diff: a valid JSON document without records
    assert write_diff([], tmp_path / "empty.json") == 0
    assert json.loads((tmp_path / "empty.json").read_text(encoding="utf-8")) == {"records": []}
