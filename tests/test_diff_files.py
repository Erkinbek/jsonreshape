"""jsonreshape.diff: file diffs (docs/ARCHITECTURE.md §14.6, s5/map_repair_diff.md §4.4, §4.8).

* the same data in every kind, style and encoding is identical in every mode (key / align /
  position); the round trip JSON -> JSONL -> JSON of acceptance test 22 changes nothing;
* align mode: inserted / deleted / changed records give the minimal edit list;
* key mode: any order, changed + removed in A order then the added, key values shown; a tiny
  memory limit (spilled runs, fingerprints in a file) gives exactly the in-memory result; both
  core backends and strategies; duplicate keys paired i-th with i-th, missing and unreadable keys
  counted with warnings; ``$key`` for member documents (auto), member records as
  ``{key: value}`` in the other modes;
* position mode; document mode (single documents, the Node path with a tiny
  ``large_node_bytes`` equals the in-memory result); wrappers; huge records;
* edited (pinned) documents compare by positions;
* limits and errors (too big without a key, bad arguments), ``max_details``, the result
  table, progress, cancellation and clean-up, a file changed meanwhile,
  :func:`iter_record_diffs` returning the report, :func:`write_diff` in every format.
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
from pathlib import Path
from typing import Any

import pytest

import jsongen
from diff_helpers import assert_same_data, diff_paths, shape
from jsonreshape import codec, diff, dupes, reader
from jsonreshape.appdirs import work_dir
from jsonreshape.codec import JsonFloat
from jsonreshape.diff import (
    RESULT_ENTRY,
    DiffOptions,
    DiffReport,
    Omitted,
    RecordDiff,
    diff_files,
    iter_record_diffs,
    write_diff,
)
from jsonreshape.errors import Cancelled, FileChangedError, PathSyntaxError, UserInputError
from jsonreshape.paths import MISSING
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export
from overlay_helpers import OverlayBuilder, open_expected
from test_dupes import backend, make_doc  # noqa: F401 - fixtures used by name


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def run(a: Document, b: Document, **kw: Any) -> tuple[list[RecordDiff], DiffReport]:
    rds: list[RecordDiff] = []
    rep = diff_files(a, b, on_record=rds.append, **kw)
    return rds, rep


def brief(rds: list[RecordDiff]) -> list[tuple]:
    return [(rd.status, rd.a_index, rd.b_index) for rd in rds]


def changes(rd: RecordDiff) -> list[tuple]:
    assert rd.diff is not None
    return [(c.op, c.a_path, c.b_path, None if c.a is MISSING else codec.dumps(c.a).decode(),
             None if c.b is MISSING else codec.dumps(c.b).decode()) for c in rd.diff.changes]


def no_temp_dirs() -> bool:
    return not any(name.startswith("sort-") for name in os.listdir(work_dir()))


def recs(n: int, seed: int) -> list[dict[str, Any]]:
    out = jsongen.records(n, seed=seed)
    for i, r in enumerate(out):
        r["id"] = i
    return out


# ----------------------------------------------------------------------------------
# the same data is identical
# ----------------------------------------------------------------------------------
SAME = [
    ("array", {"style": "pretty2", "newline": "\r\n", "bom": True}, {}),
    ("array", {"style": "messy_escaped"}, {}),
    ("array", {"encoding": "utf-16-le", "bom": True, "style": "tab"}, {}),
    ("array", {"gz": True, "style": "pretty4"}, {}),
    ("stream", {"style": "pretty4"}, {"format": "stream"}),
    ("brace_list", {"style": "messy"}, {"format": "brace_list"}),
    ("jsonl", {"style": "escaped", "newline": "\r\n"}, {"format": "jsonl"}),
]


@pytest.mark.parametrize("fmt, write_kw, open_kw", SAME, ids=[f"{c[0]}-{i}" for i, c in enumerate(SAME)])
def test_the_same_data_is_identical_in_every_mode(make_doc, fmt, write_kw, open_kw) -> None:
    data = recs(150, seed=3)
    a = make_doc(data, "jsonl", format="jsonl")
    b = make_doc(data, fmt, **write_kw, **open_kw)
    assert b.record_count == 150
    for kw in ({}, {"mode": "position"}, {"key": "id"}, {"key": "$"}, {"mode": "document"}):
        rds, rep = run(a, b, **kw)
        assert rds == [] and rep.identical, kw
        assert (rep.equal, rep.a_records, rep.b_records) == ((1, 150, 150) if kw.get("mode") == "document"
                                                              else (150, 150, 150)), kw
        assert not rep.warnings, kw


def test_round_trip_json_jsonl_json_changes_nothing(tmp_path: Path) -> None:
    """Acceptance test 22 through the diff: JSON -> JSONL -> JSON keeps every value exactly."""
    rng = random.Random(22)
    data = recs(300, seed=22)
    for i, r in enumerate(data):
        r["exact"] = codec.loads(rng.choice(jsongen.EDGE_NUMBER_TOKENS))
        r["text"] = rng.choice(jsongen.EDGE_STRINGS)
    src = tmp_path / "src.json"
    jsongen.write_file(src, data, "array", style="pretty2")
    mid = tmp_path / "mid.jsonl"
    out = tmp_path / "out.json"
    with Document.open(src) as d:
        export(d, mid, ExportOptions("jsonl"))
    with Document.open(mid) as d:
        export(d, out, ExportOptions("json", indent=4))
    for p in (mid, out):
        rep = assert_same_data(src, p)
        assert rep.equal == 300
        assert_same_data(src, p, opts=DiffOptions(numbers="text"))    # the number tokens are unchanged too
        assert_same_data(src, p, key="id", mode="key")
    # and a real change is found
    lines = mid.read_bytes().split(b"\n")
    old = codec.loads(lines[5])["exact"]
    lines[5] = codec.dumps({**codec.loads(lines[5]), "exact": JsonFloat("1.100")})
    mid.write_bytes(b"\n".join(lines))
    rds, rep = diff_paths(src, mid, opts=DiffOptions(numbers="text"))
    assert brief(rds) == [("changed", 5, 5)]
    assert changes(rds[0]) == [("changed", ("exact",), ("exact",), codec.dumps(old).decode(), "1.100")]


def test_numbers_text_option(make_doc) -> None:
    a = make_doc(b'[{"n": 1}, {"n": 1.10}, {"n": -0}, {"n": 1e400}]')
    b = make_doc(b'[{"n": 1.0}, {"n": 1.1}, {"n": 0}, {"n": 1E+400}]')
    assert run(a, b)[1].identical
    rds, rep = run(a, b, opts=DiffOptions(numbers="text"))
    assert brief(rds) == [("changed", i, i) for i in range(4)] and rep.equal == 0


# ----------------------------------------------------------------------------------
# align mode
# ----------------------------------------------------------------------------------
def test_align_mode_minimal_edits(make_doc) -> None:
    a_recs = recs(200, seed=1)
    b_recs = [dict(r) for r in a_recs]
    b_recs[10]["name"] = "Vali"                  # changed
    del b_recs[50]                               # removed (A #50)
    b_recs.insert(0, {"id": "new-front"})        # added (B #0)
    b_recs.insert(120, {"id": "new-middle"})     # added
    b_recs[150]["tags"] = ["yangi"]              # changed (A #150)
    b_recs.append({"id": "new-end"})
    a = make_doc(a_recs, "array", style="pretty2")
    b = make_doc(b_recs, "jsonl", format="jsonl")
    rds, rep = run(a, b)
    assert rep.mode == "align" and rep.key is None
    # B: [front] + A[0:50] + A[51:120] + [middle] + A[120:] + [end]; B #150 is A #149
    assert brief(rds) == [("added", None, 0), ("changed", 10, 11), ("removed", 50, None), ("added", None, 120),
                          ("changed", 149, 150), ("added", None, 201)]
    assert changes(rds[1]) == [("changed", ("name",), ("name",), codec.dumps(a_recs[10]["name"]).decode(), '"Vali"')]
    assert changes(rds[2]) == [("removed", (), None, codec.dumps(a_recs[50]).decode(), None)]
    assert changes(rds[0]) == [("added", None, (), None, '{"id":"new-front"}')]
    assert (rep.equal, rep.changed, rep.added, rep.removed) == (197, 2, 3, 1)
    assert rep.differences == 6 and not rep.identical and rep.details == 6


def test_align_mode_through_patience(make_doc, monkeypatch) -> None:
    a_recs = recs(400, seed=8)
    b_recs = [dict(r) for r in a_recs]
    for i in (5, 100, 333):
        b_recs[i]["score"] = "changed"
    del b_recs[200:203]
    b_recs[250:250] = [{"id": "x1"}, {"id": "x2"}]
    a = make_doc(a_recs, "jsonl", format="jsonl")
    b = make_doc(b_recs, "jsonl", format="jsonl")
    want = shape(run(a, b)[0])
    monkeypatch.setattr(diff, "SM_PRODUCT_MAX", 16)
    assert shape(run(a, b)[0]) == want
    assert brief(run(a, b)[0]) == [("changed", 5, 5), ("changed", 100, 100), ("removed", 200, None),
                                   ("removed", 201, None), ("removed", 202, None), ("added", None, 250),
                                   ("added", None, 251), ("changed", 333, 332)]
    monkeypatch.setattr(diff, "PATIENCE_MAX", 50)       # over the limit: a content-defined sample anchors
    assert shape(run(a, b)[0]) == want
    monkeypatch.setattr(diff, "PATIENCE_MAX", 2)        # a tiny sample: a valid (if longer) edit list
    rds, rep = run(a, b)
    assert not rep.warnings and rep.changed + rep.added + rep.removed >= 8
    assert rep.equal + rep.changed + rep.removed == rep.a_records == 400
    assert rep.equal + rep.changed + rep.added == rep.b_records == 399


def edited_records(rng: random.Random, n: int) -> tuple[list[dict], list[dict]]:
    a_recs = recs(n, seed=rng.randrange(1000))
    b_recs = [dict(r) for r in a_recs]
    for _ in range(rng.randrange(0, 25)):
        r = rng.random()
        if r < 0.25 and b_recs:
            del b_recs[rng.randrange(len(b_recs))]
        elif r < 0.5:
            b_recs.insert(rng.randrange(len(b_recs) + 1), {"id": f"new{rng.randrange(10**6)}"})
        elif r < 0.8 and b_recs:
            k = rng.randrange(len(b_recs))
            b_recs[k] = {**b_recs[k], "name": f"changed {rng.randrange(100)}"}
        elif b_recs:                                     # a block moved
            k = rng.randrange(len(b_recs))
            block = b_recs[k:k + rng.randrange(1, 8)]
            del b_recs[k:k + len(block)]
            p = rng.randrange(len(b_recs) + 1)
            b_recs[p:p] = block
        if rng.random() < 0.1:
            b_recs.insert(rng.randrange(len(b_recs) + 1), dict(b_recs[0]) if b_recs else {"id": 0})   # a duplicate
    return a_recs, b_recs


@pytest.mark.parametrize("seed", range(8))
def test_align_mode_equals_the_in_memory_array_diff(make_doc, monkeypatch, seed: int) -> None:
    """The records of two files aligned by their fingerprints give the same pairs and changes as
    diff_values of the two record arrays (the same alignment over equivalent identities) -- with
    SequenceMatcher and with the patience alignment. With sampled anchors (the file path samples
    fingerprint bits, the value path token-hash bits: other samples) the result is a valid,
    monotone alignment whose pairs carry their exact changes."""
    rng = random.Random(seed)
    a_recs, b_recs = edited_records(rng, rng.choice([30, 150, 400]))
    a = make_doc(a_recs, "array", style="pretty2")
    b = make_doc(b_recs, "jsonl", format="jsonl")
    opts = DiffOptions(max_changes=None)
    monkeypatch.setattr(diff, "SM_PRODUCT_MAX", 16)
    monkeypatch.setattr(diff, "PATIENCE_MAX", 60)
    rds, rep = run(a, b, mode="align")
    seen_a = [rd.a_index for rd in rds if rd.a_index is not None]
    seen_b = [rd.b_index for rd in rds if rd.b_index is not None]
    assert seen_a == sorted(set(seen_a)) and len(set(seen_b)) == len(seen_b)
    pairs = [(rd.a_index, rd.b_index) for rd in rds if rd.status == "changed"]
    assert [j for _, j in pairs] == sorted(j for _, j in pairs)                  # monotone
    for rd in rds:
        if rd.status == "changed":
            want_vd = diff.diff_values(a_recs[rd.a_index], b_recs[rd.b_index], opts)
            assert [c[:3] for c in changes(rd)] == [(c.op, c.a_path, c.b_path) for c in want_vd.changes]
            assert not want_vd.equal
    assert rep.equal + rep.changed + rep.removed == len(a_recs)
    assert rep.equal + rep.changed + rep.added == len(b_recs)
    for sm, pmax in ((4_000_000, 1_000_000), (16, 1_000_000)):
        monkeypatch.setattr(diff, "SM_PRODUCT_MAX", sm)
        monkeypatch.setattr(diff, "PATIENCE_MAX", pmax)
        rds, rep = run(a, b, mode="align")
        # the reference: the walker's pairing of the two record arrays, each pair diffed in memory
        opts = DiffOptions(max_changes=None)
        walker = diff._Walker(opts, diff._Sink(None))
        want: dict[tuple, list] = {}                     # (status, A index, B index) -> changes inside the record
        for item in walker._array(codec.loads(codec.dumps(a_recs)), codec.loads(codec.dumps(b_recs)), None, None):
            if isinstance(item, diff._Emit):
                if item.op == "added":
                    want[("added", None, item.cb[1])] = [("added", None, (), None, codec.dumps(item.b).decode())]
                else:
                    want[("removed", item.ca[1], None)] = [("removed", (), None, codec.dumps(item.a).decode(), None)]
                continue
            ca, cb, x, y = item
            vd = diff.diff_values(x, y, opts)
            if not vd.equal:
                want[("changed", ca[1], cb[1])] = [
                    (c.op, c.a_path, c.b_path, None if c.a is MISSING else codec.dumps(c.a).decode(),
                     None if c.b is MISSING else codec.dumps(c.b).decode()) for c in vd.changes]
        got = {(rd.status, rd.a_index, rd.b_index): changes(rd) for rd in rds}
        assert got == want, (sm, pmax)
        assert rep.equal + rep.changed + rep.removed == len(a_recs)
        assert rep.equal + rep.changed + rep.added == len(b_recs)


def test_align_limit_and_errors(make_doc, lang_en) -> None:
    small = Settings(diff_align_max_bytes=100)
    a = make_doc(recs(20, seed=2), "array", settings=small)
    b = make_doc(recs(20, seed=2), "jsonl", format="jsonl", settings=small)
    for mode in ("auto", "align"):
        with pytest.raises(UserInputError) as ei:
            run(a, b, mode=mode)
        assert ei.value.key == "diff.error.too_big_no_key"
        assert "--key" in ei.value.hint and "--by-position" in ei.value.hint
    assert run(a, b, mode="position")[1].identical
    assert run(a, b, key="id")[1].identical
    assert no_temp_dirs()


def test_argument_errors(make_doc) -> None:
    a = make_doc(b"[1]")
    cases = [({"mode": "fast"}, "diff.error.mode"), ({"mode": "position", "key": "id"}, "diff.error.key_mode"),
             ({"mode": "key"}, "diff.error.key_required"), ({"max_details": -1}, "diff.error.max_details"),
             ({"memory_limit": 0}, "diff.error.memory_limit"), ({"memory_limit": True}, "diff.error.memory_limit"),
             ({"key": "a[*]"}, "dupes.error.wildcard"), ({"key": " "}, "dupes.error.key")]
    for kw, key in cases:
        with pytest.raises(UserInputError) as ei:
            iter_record_diffs(a, a, **kw)
        assert ei.value.key == key, kw
    with pytest.raises(PathSyntaxError):
        iter_record_diffs(a, a, key="a[")
    with pytest.raises(TypeError):
        diff_files(a, a, opts={"numbers": "text"})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        diff_files(a, a, strategy="fast")


# ----------------------------------------------------------------------------------
# key mode
# ----------------------------------------------------------------------------------
def key_case(n: int = 300, seed: int = 4) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    a_recs = recs(n, seed=seed)
    b_recs = [dict(r) for r in a_recs]
    for i in rng.sample(range(n), 25):
        b_recs[i]["name"] = f"changed {i}"
    gone = set(rng.sample(range(n), 10))
    b_recs = [r for i, r in enumerate(b_recs) if i not in gone]
    b_recs += [{"id": 1000 + k, "name": "new"} for k in range(7)]
    rng.shuffle(b_recs)
    return a_recs, b_recs


def key_reference(a_recs: list[dict], b_recs: list[dict]) -> list[tuple]:
    b_pos = {r["id"]: j for j, r in enumerate(b_recs)}
    out = []
    for i, r in enumerate(a_recs):
        j = b_pos.get(r["id"])
        if j is None:
            out.append(("removed", i, None))
        elif codec.dumps(r, sort_keys=True) != codec.dumps(b_recs[j], sort_keys=True):
            out.append(("changed", i, j))
    a_ids = {r["id"] for r in a_recs}
    out += [("added", None, j) for j, r in enumerate(b_recs) if r["id"] not in a_ids]
    return out


@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_key_mode_matches_records_in_any_order(make_doc, backend, strategy) -> None:
    a_recs, b_recs = key_case()
    a = make_doc(a_recs, "array", style="pretty2")
    b = make_doc(b_recs, "jsonl", format="jsonl")
    rds, rep = run(a, b, key="id", strategy=strategy)
    assert rep.mode == "key" and rep.key == "id"
    want = key_reference(a_recs, b_recs)
    assert brief(rds) == want
    n_changed = sum(1 for w in want if w[0] == "changed")
    assert 20 <= n_changed <= 25
    assert (rep.changed, rep.removed, rep.added, rep.equal) == (n_changed, 10, 7, 290 - n_changed)
    for rd in rds:
        want = a_recs[rd.a_index]["id"] if rd.a_index is not None else b_recs[rd.b_index]["id"]
        assert rd.key == want
        if rd.status == "changed":
            assert [c[1] for c in changes(rd)] == [("name",)]
    assert rep.duplicate_keys == (0, 0) and rep.missing_keys == (0, 0) and not rep.warnings


@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_key_mode_spilled_equals_in_memory(make_doc, backend, strategy, monkeypatch) -> None:
    a_recs, b_recs = key_case(500, seed=9)
    a = make_doc(a_recs, "jsonl", format="jsonl")
    b = make_doc(b_recs, "array", style="messy")
    want = shape(run(a, b, key="id", strategy=strategy)[0])
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 3)
    monkeypatch.setattr(dupes, "MAX_FANIN", 3)
    monkeypatch.setattr(dupes, "CHECK_RECORDS", 7)
    monkeypatch.setattr(diff, "FP_MEMORY_ENTRIES", 10)          # fingerprints in a temporary file
    monkeypatch.setattr(diff, "BATCH_RECORDS", 5)
    spills: list[int] = []
    mapped: list[bool] = []
    real_spill, real_finish = dupes.HashRuns.spill, diff._FpStore.finish

    def spill(self: Any, entries: list[int]) -> None:
        spills.append(len(entries))
        real_spill(self, entries)

    def finish(self: Any) -> Any:
        out = real_finish(self)
        mapped.append(self._view is not None)
        return out

    monkeypatch.setattr(dupes.HashRuns, "spill", spill)
    monkeypatch.setattr(diff._FpStore, "finish", finish)
    rds, rep = run(a, b, key="id", strategy=strategy, memory_limit=dupes.ENTRY_BYTES * 30, block_bytes=2000)
    assert shape(rds) == want
    # key entries of both sides and the changed pairs went through sorted runs; fingerprints through files
    assert len(spills) > 40 and max(spills) <= 15 + 7 and mapped == [True, True]    # (checked every 7 records)
    assert no_temp_dirs()


def test_duplicate_missing_and_skipped_keys(make_doc, lang_en) -> None:
    a = make_doc(b'[{"id": 1, "v": "a1"}, {"id": 1, "v": "a2"}, {"id": 2, "v": 1}, {"v": "no id"}, {"id": 3}, '
                 b'{"id": 1, "v": "a3"}]', name="a.json")
    b = make_doc(b'{"id": 1, "v": "a1"}\n{"id": 2, "v": 1.0}\n{"id": 1, "v": "b2"}\n{"x": 0}\n{"x": 1}\n'
                 b'{"id": 4}\n', format="jsonl", name="b.jsonl")
    rds, rep = run(a, b, key="id")
    # the 1st "1" of A with the 1st of B, the 2nd with the 2nd, the 3rd is removed
    assert brief(rds) == [("changed", 1, 2), ("removed", 4, None), ("removed", 5, None), ("added", None, 5)]
    assert changes(rds[0]) == [("changed", ("v",), ("v",), '"a2"', '"b2"')]
    assert (rep.equal, rep.duplicate_keys, rep.missing_keys) == (2, (1, 1), (1, 2))
    texts = rep.warning_texts()
    assert texts == [
        "a.json: 1 record has no field id: it was not compared.",
        "b.jsonl: 2 records have no field id: they were not compared.",
        "a.json: 1 value of id occurs in more than one record; these records were paired in order (the first "
        "with the first, the second with the second).",
        "b.jsonl: 1 value of id occurs in more than one record; these records were paired in order (the first "
        "with the first, the second with the second).",
    ]
    # a key value too large to capture (a streamed huge record) is skipped with a warning
    long = b'"' + b"x" * 1_200_000 + b'"'
    small = Settings(large_node_bytes=1000)
    c = make_doc(b'{"id": 1}\n{"id": ' + long + b'}\n', format="jsonl", settings=small, name="c.jsonl")
    rds, rep = run(c, c, key="id")
    assert rds == [] and rep.skipped_keys == (1, 1) and rep.equal == 1
    assert "the key value of 1 record (#1) is too large" in rep.warning_texts()[0]


def test_keyed_documents_compare_by_member_key(make_doc, lang_en) -> None:
    keys = [f"k{i}" for i in range(30)]
    values = [{"n": i, "tags": [i, i + 1]} for i in range(30)]
    a = make_doc(values, "keyed", format="keyed", keys=keys)
    b_keys = list(reversed(keys[2:])) + ["new"]
    b_vals = [dict(values[keys.index(k)]) if k != "new" else {"n": -1} for k in b_keys]
    b_vals[0]["n"] = 999                                        # k29 changed
    b = make_doc(b_vals, "keyed", format="keyed", keys=b_keys, style="pretty2")
    rds, rep = run(a, b)
    assert rep.mode == "key" and rep.key == "$key"
    assert brief(rds) == [("removed", 0, None), ("removed", 1, None), ("changed", 29, 0), ("added", None, 28)]
    assert [rd.key for rd in rds] == ["k0", "k1", "k29", "new"]
    assert changes(rds[2]) == [("changed", ("n",), ("n",), "29", "999")]          # paths inside the value
    assert changes(rds[0]) == [("removed", (), None, '{"n":0,"tags":[0,1]}', None)]
    # forced align: the member is the record ({key: value}); a renamed key is removed + added
    b2 = make_doc(b'{"k0": {"n": 0, "tags": [0, 1]}, "K1": {"n": 1, "tags": [1, 2]}}', format="keyed")
    a2 = make_doc(b'{"k0": {"n": 0, "tags": [0, 1]}, "k1": {"n": 1, "tags": [1, 2]}}', format="keyed")
    rds, rep = run(a2, b2, mode="align")
    assert brief(rds) == [("changed", 1, 1)]
    assert changes(rds[0]) == [("removed", ("k1",), None, '{"n":1,"tags":[1,2]}', None),
                               ("added", None, ("K1",), None, '{"n":1,"tags":[1,2]}')]
    # $key against a document without member keys: nothing to match, a warning
    plain = make_doc(b'[{"n": 0}]', name="plain.json")
    rds, rep = run(a2, plain, key="$key")
    assert brief(rds) == [("removed", 0, None), ("removed", 1, None)] and rep.missing_keys == (0, 1)
    assert rep.warning_texts() == ["plain.json: the records of this file have no keys ($key works only in keyed "
                                   "documents): 1 record was not compared."]


def test_member_records_by_a_path_key(make_doc) -> None:
    a = make_doc(b'{"x": {"id": 1, "v": 1}, "y": {"id": 2, "v": 2}}', format="keyed")
    b = make_doc(b'{"z": {"id": 2, "v": 2}, "x": {"id": 1, "v": 5}}', format="keyed")
    rds, rep = run(a, b, key="id")
    assert brief(rds) == [("changed", 0, 1), ("changed", 1, 0)]
    assert changes(rds[0]) == [("changed", ("x", "v"), ("x", "v"), "1", "5")]
    assert changes(rds[1])[0][0] == "removed" and changes(rds[1])[1][:3] == ("added", None, ("z",))
    assert [rd.key for rd in rds] == [1, 2]


# ----------------------------------------------------------------------------------
# position and document modes, wrappers
# ----------------------------------------------------------------------------------
def test_position_mode(make_doc) -> None:
    a_recs = recs(50, seed=5)
    b_recs = [dict(r) for r in a_recs[:45]]
    b_recs[3]["name"] = "x"
    b_recs.insert(0, {"id": "front"})
    a = make_doc(a_recs, "jsonl", format="jsonl")
    b = make_doc(b_recs, "array")
    rds, rep = run(a, b, mode="position")
    assert rep.mode == "position"
    # everything shifted: pairs by position, then the tail of A removed
    assert [x for x in brief(rds) if x[0] == "changed"] == [("changed", i, i) for i in range(46)]
    assert brief(rds)[46:] == [("removed", i, None) for i in range(46, 50)]
    rds2, rep2 = run(b, a, mode="position", max_details=0)
    assert brief(rds2)[46:] == [("added", None, i) for i in range(46, 50)]
    assert all(rd.diff is None for rd in rds2) and rep2.details == 0


@pytest.mark.parametrize("seed", range(3))
def test_document_mode_node_path_equals_in_memory(make_doc, tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    va = {"meta": {"v": seed, "list": list(range(12))}, "items": recs(12, seed=seed),
          "nested": jsongen.rand_value(rng, 0, 4, 5)}
    vb = codec.loads(codec.dumps(va))
    vb["meta"]["v"] = "changed"
    vb["items"].insert(3, {"id": "new"})
    del vb["items"][8]
    vb["items"][10]["name"] = "renamed"
    vb["meta"]["list"].reverse()
    vb["nested"] = jsongen.rand_value(rng, 0, 4, 5) if seed % 2 else vb["nested"]
    pa, pb = tmp_path / f"a{seed}.json", tmp_path / f"b{seed}.json"
    pa.write_bytes(jsongen.encode(va, "pretty2"))
    pb.write_bytes(jsongen.encode(vb, "messy"))
    options = (DiffOptions(), DiffOptions(array_match="index"), DiffOptions(array_keys=("items=id",)),
               DiffOptions(numbers="text", max_changes=None))
    wants = [diff.diff_values(codec.loads(pa.read_bytes()), codec.loads(pb.read_bytes()), o) for o in options]
    for large in (None, 150, 1000):        # small values: every bigger node goes through the Node API
        settings = Settings(large_node_bytes=large) if large else None
        a = make_doc(pa.read_bytes(), format="single", settings=settings, name=f"x{large}a{seed}.json")
        b = make_doc(pb.read_bytes(), format="single", settings=settings, name=f"x{large}b{seed}.json")
        for opts, want in zip(options, wants):
            rds, rep = run(a, b, opts=opts)
            assert rep.mode == "document"
            assert len(rds) == 1 and rds[0].scope == "document" and rds[0].a_index is None
            got = rds[0].diff
            assert got is not None
            assert [(c.op, c.a_path, c.b_path) for c in got.changes] == \
                   [(c.op, c.a_path, c.b_path) for c in want.changes], (opts, large)
            assert [codec.dumps(c.a) if c.a is not MISSING else None for c in got.changes] == \
                   [codec.dumps(c.a) if c.a is not MISSING else None for c in want.changes]
            assert got.counts == want.counts


def test_wrappers_are_compared(make_doc, lang_en) -> None:
    data = recs(20, seed=6)
    a = make_doc(data, "wrapped", record_path="data")
    raw = jsongen.build_document(data, "wrapped").replace(b'"version":1', b'"version":2')
    b = make_doc(raw, record_path="data")
    rds, rep = run(a, b)
    assert len(rds) == 1 and rds[0].scope == "wrapper" and rds[0].status == "changed"
    assert changes(rds[0]) == [("changed", ("meta", "version"), ("meta", "version"), "1", "2")]
    assert rep.wrapper is not None and rep.equal == 20 and rep.differences == 1 and not rep.identical
    c = make_doc(data, "jsonl", format="jsonl", name="c.jsonl")
    rds, rep = run(a, c)
    assert rds == [] and rep.identical and rep.equal == 20
    assert rep.warning_texts() == [f"{a.source_path.name}: the part around the records (the wrapper) was not "
                                   "compared: the other file has none."]


def test_partial_documents_and_broken_lines_are_reported(make_doc, lang_en) -> None:
    a = make_doc(b'[{"id": 1}, {"id": 2}, {"id": oops', name="part.json")
    b = make_doc(b'{"id": 1}\n{"id": \n{"id": 2}\n', format="jsonl", name="lines.jsonl")
    rds, rep = run(a, b)
    assert rds == [] and rep.equal == 2
    assert rep.warning_texts() == ["part.json: the file has a JSON error; only the 2 records before it were compared.",
                                   "lines.jsonl: 1 broken line was not compared."]


# ----------------------------------------------------------------------------------
# huge records, edited documents
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [{}, {"key": "id"}, {"mode": "position"}])
def test_huge_records_go_through_the_node_path(make_doc, tmp_path: Path, kw) -> None:
    a_recs, b_recs = key_case(60, seed=12)
    if not kw:
        b_recs.sort(key=lambda r: str(r["id"]))
    for r in a_recs[:5] + b_recs[:5]:
        r["blob"] = {"list": list(range(200)), "text": "y" * 300}
    pa, pb = tmp_path / "ha.json", tmp_path / "hb.jsonl"
    jsongen.write_file(pa, a_recs, "array", style="pretty2")
    jsongen.write_file(pb, b_recs, "jsonl")
    want = shape(diff_paths(pa, pb, **kw)[0])
    small = Settings(large_node_bytes=400)
    got_rds, rep = diff_paths(pa, pb, open_a={"settings": small}, open_b={"settings": small}, **kw)
    with Document.open(pa, settings=small) as d:
        d.build_index()
        assert any(d.is_huge(i) for i in range(d.record_count))
    assert shape(got_rds) == want


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped"])
def test_edited_documents_compare_by_positions(tmp_path: Path, kind: str) -> None:
    from overlay_helpers import make_source

    doc = make_source(tmp_path / f"src.{kind}", kind, n=30, seed=3)
    try:
        ob = OverlayBuilder(doc, tmp_path / "layer")
        member = doc.member_records
        new = b'"zz": {"id": "new"}' if member else b'{"id": "new"}'
        ob.set(4, ob.records[4].replace(b'"id":4', b'"id":44') if not member else ob.records[4])
        ob.delete(10).insert(0, new).move([20, 21], 2)
        doc.set_overlay(ob.state())
        plain = open_expected(doc, ob.records, tmp_path / f"expected.{kind}")
        try:
            rds, rep = run(doc, plain, mode="position")
            assert rds == [] and rep.equal == len(ob.records) and rep.a_gen == doc.gen != 0
            rds, rep = run(doc, plain)
            assert rds == [] and rep.identical
            # the file as it is against the edited state: the edits
            rds, rep = run(doc.pinned(None), doc, mode="align")
            assert rep.differences > 0 and rep.a_records == 30 and rep.b_records == len(ob.records)
            assert ("added", None, 0) in brief(rds)
        finally:
            plain.close()
    finally:
        doc.close()


# ----------------------------------------------------------------------------------
# details, the result table, the generator, writing
# ----------------------------------------------------------------------------------
def test_max_details_and_the_result_table(make_doc) -> None:
    a_recs, b_recs = key_case(200, seed=7)
    a = make_doc(a_recs, "jsonl", format="jsonl")
    b = make_doc(b_recs, "jsonl", format="jsonl")
    full, rep_full = run(a, b, key="id", results=True)
    try:
        assert rep_full.results_count == len(full) and rep_full.results_file is not None
        assert rep_full.read_results() == [(rd.status, rd.a_index, rd.b_index) for rd in full]
        assert rep_full.read_results(3, 2) == [(rd.status, rd.a_index, rd.b_index) for rd in full[3:5]]
        assert rep_full.results_file.stat().st_size == RESULT_ENTRY.size * len(full)
        path = rep_full.results_file
    finally:
        rep_full.close()
    assert not path.exists() and rep_full.read_results() == [] and no_temp_dirs()
    rds, rep = run(a, b, key="id", max_details=5)
    assert brief(rds) == brief(full) and rep.details == 5
    assert [rd.diff is not None for rd in rds] == [True] * 5 + [False] * (len(rds) - 5)
    assert all(rd.key is MISSING for rd in rds[5:])
    assert (rep.changed, rep.added, rep.removed) == (rep_full.changed, rep_full.added, rep_full.removed)
    assert run(a, b, key="id")[1].results_file is None


@pytest.mark.parametrize("case", ["key", "align", "position", "keyed", "huge"])
def test_record_diff_reproduces_the_rows_of_a_run(make_doc, case: str) -> None:
    """The GUI's per-row details: record_diff of a result row equals the record diff of the run."""
    from jsonreshape.diff import record_diff

    a_recs, b_recs = key_case(80, seed=13)
    settings = Settings(large_node_bytes=300) if case == "huge" else None
    if case == "huge":
        for r in a_recs[:6] + b_recs[:6]:
            r["blob"] = {"list": list(range(60)), "s": "z" * 120}
    if case == "keyed":
        a = make_doc(a_recs, "keyed", format="keyed", keys=[f"k{r['id']}" for r in a_recs])
        b = make_doc(b_recs, "keyed", format="keyed", keys=[f"k{r['id']}" for r in b_recs], style="pretty2")
        kw: dict[str, Any] = {}
    else:
        a = make_doc(a_recs, "array", settings=settings)
        b = make_doc(b_recs, "jsonl", format="jsonl", settings=settings)
        kw = {"key": "id"} if case in ("key", "huge") else {"mode": case}
    opts = DiffOptions(array_keys=("orders=oid",), max_changes=None)
    rds, rep = run(a, b, opts=opts, results=True, **kw)
    try:
        rows = rep.read_results()
        assert rows == brief(rds) and rows
        for rd, (status, ai, bi) in zip(rds, rows):
            one = record_diff(a, ai, b, bi, rep.options, key=rep.key)
            assert shape([one]) == shape([rd]), (status, ai, bi)
    finally:
        rep.close()
    if case == "key":
        b_pos = {r["id"]: j for j, r in enumerate(b_recs)}
        i = next(i for i, r in enumerate(a_recs) if r["id"] in b_pos and
                 codec.dumps(r, sort_keys=True) == codec.dumps(b_recs[b_pos[r["id"]]], sort_keys=True))
        same = record_diff(a, i, b, b_pos[a_recs[i]["id"]], key="id")
        assert (same.status, same.a_index, same.key) == ("changed", i, MISSING) and same.diff is not None
        assert same.diff.equal and same.diff.changes == []
        with pytest.raises(IndexError):
            record_diff(a, 10**6, b, 0)
        with pytest.raises(ValueError):
            record_diff(a, None, b, None)
    assert no_temp_dirs()


def test_iter_record_diffs_generator_and_early_close(make_doc) -> None:
    a_recs, b_recs = key_case(120, seed=8)
    a = make_doc(a_recs, "array")
    b = make_doc(b_recs, "array")
    gen = iter_record_diffs(a, b, key="id", results=True)
    first = next(gen)
    assert isinstance(first, RecordDiff)
    gen.close()                                 # stopped early: every temporary file is gone
    assert no_temp_dirs()

    def collect() -> Any:
        report = yield from iter_record_diffs(a, b, key="id")
        return report

    items = []
    g = collect()
    while True:
        try:
            items.append(next(g))
        except StopIteration as stop:
            report = stop.value
            break
    assert isinstance(report, DiffReport) and report.changed + report.added + report.removed == len(items)


def test_write_diff_formats(make_doc, tmp_path: Path) -> None:
    a = make_doc(b'[{"id": 1, "n": 12345678901234567890, "s": "Toshkent"}, {"id": 2, "n": 0.1}, {"id": 3}]')
    b = make_doc(b'[{"id": 1, "n": 1e400, "s": "\\u0422\\u043e\\u0448\\u043a\\u0435\\u043d\\u0442"}, {"id": 4}]')
    rds, rep = run(a, b, key="id")
    n = write_diff(rds, tmp_path / "d.json", "json", report=rep)
    doc = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert n == 4 and len(doc["records"]) == 4 and doc["summary"]["changed"] == 1
    first = doc["records"][0]
    assert (first["status"], first["key"], first["a"], first["b"]) == ("changed", 1, 0, 0)
    raw = (tmp_path / "d.json").read_bytes()
    assert b'"a":12345678901234567890' in raw and b'"b":1e400' in raw and "Тошкент".encode() in raw
    assert first["changes"][0] == {"op": "changed", "path": "n", "pointer": "/n", "a": 12345678901234567890,
                                   "b": float("inf")} or b'"path":"n"' in raw
    # JSON Lines straight from the generator (the summary is its return value)
    n = write_diff(iter_record_diffs(a, b, key="id"), tmp_path / "d.jsonl", "jsonl")
    lines = [json.loads(x) for x in (tmp_path / "d.jsonl").read_text(encoding="utf-8").splitlines()]
    assert n == 4 and len(lines) == 5 and lines[-1]["summary"]["removed"] == 2 and lines[-1]["summary"]["added"] == 1
    # the public object builders are the file's lines (the CLI prints them as JSON Lines)
    from jsonreshape.diff import record_dict, summary_dict

    raw_lines = (tmp_path / "d.jsonl").read_bytes().split(b"\n")
    assert raw_lines[:4] == [codec.dumps(record_dict(rd)) for rd in rds]
    want_summary = summary_dict(rep)
    assert {k: v for k, v in json.loads(raw_lines[4])["summary"].items() if k != "elapsed"} == \
        {k: v for k, v in json.loads(codec.dumps(want_summary)).items() if k != "elapsed"}
    # CSV: one row per change
    n = write_diff(rds, tmp_path / "d.csv", ".CSV")
    text = (tmp_path / "d.csv").read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["status", "key", "a_index", "b_index", "op", "path", "a_value", "b_value"]
    assert rows[1] == ["changed", "1", "0", "0", "changed", "n", "12345678901234567890", "1e400"]
    assert ["removed", "2", "1", "", "removed", "", '{"id":2,"n":0.1}', ""] in rows
    with pytest.raises(UserInputError) as ei:
        write_diff(rds, tmp_path / "d.xml", "xml")
    assert ei.value.key == "diff.error.format" and not (tmp_path / "d.xml").exists()
    # an Omitted value
    rd = RecordDiff("changed", 0, 0, MISSING, diff.ValueDiff(changes=[diff.Change("changed", ("big",), ("big",),
                                                                                    Omitted("string", 9 << 20, "xx"), 1)]))
    write_diff([rd], tmp_path / "o.json")
    obj = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["records"][0]["changes"][0]
    assert obj["a_omitted"] == {"kind": "string", "size": 9 << 20, "preview": "xx"} and obj["b"] == 1


# ----------------------------------------------------------------------------------
# progress, cancellation, changes on disk
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [{"key": "id"}, {}, {"mode": "position"}])
def test_progress_in_bytes(make_doc, kw) -> None:
    a_recs, b_recs = key_case(300, seed=10)
    a = make_doc(a_recs, "array", build=False)
    b = make_doc(b_recs, "jsonl", format="jsonl", build=False)
    infos: list[Any] = []
    progress = Progress(callback=infos.append, min_interval=0)
    rds, rep = run(a, b, progress=progress, block_bytes=3000, **kw)
    done = [i.done for i in infos]
    assert done == sorted(done) and progress.finished and done[-1] == progress.total
    assert len(set(done)) > 5 and rep.bytes_read > 0
    assert a.index_complete and b.index_complete


@pytest.mark.parametrize("kw", [{"key": "id"}, {}, {"mode": "position"}, {"mode": "document"}])
def test_cancel_removes_temporary_files(make_doc, monkeypatch, kw) -> None:
    a_recs, b_recs = key_case(400, seed=11)
    settings = Settings(large_node_bytes=2000)              # the document mode walks nodes
    a = make_doc(a_recs, "jsonl", format="jsonl", settings=settings)
    b = make_doc(b_recs, "jsonl", format="jsonl", settings=settings)
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 2)
    monkeypatch.setattr(diff, "FP_MEMORY_ENTRIES", 5)
    monkeypatch.setattr(diff, "CHECK_EVERY", 7)
    args = {"results": True, "memory_limit": 200, "block_bytes": 1500, **kw}
    calls = [0]

    def count(info: Any) -> None:
        calls[0] += 1

    diff_files(a, b, progress=Progress(callback=count, min_interval=0), **args).close()
    total = calls[0]
    assert total >= 5 and no_temp_dirs()
    for stop_at in sorted({1, 2, total // 3, total // 2, total - 2}):
        cancel = CancelToken()
        seen = [0]

        def on_progress(info: Any, stop_at: int = stop_at, cancel: CancelToken = cancel) -> None:
            seen[0] += 1
            if seen[0] >= stop_at:
                cancel.cancel()

        with pytest.raises(Cancelled):
            diff_files(a, b, progress=Progress(callback=on_progress, min_interval=0), cancel=cancel, **args)
        assert no_temp_dirs(), stop_at


def test_file_changed_meanwhile(make_doc) -> None:
    a = make_doc(recs(30, seed=1), "array")
    b = make_doc(recs(30, seed=1), "array")
    st = b.source_path.stat()
    os.utime(b.source_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    for kw in ({}, {"key": "id"}):
        with pytest.raises(FileChangedError):
            diff_files(a, b, results=True, **kw)
    assert no_temp_dirs()


def test_a_document_against_itself(make_doc) -> None:
    a = make_doc(recs(80, seed=2), "keyed", format="keyed")
    for kw in ({}, {"mode": "align"}, {"mode": "position"}, {"key": "id"}, {"mode": "document"}):
        rds, rep = run(a, a, **kw)
        assert rds == [] and rep.identical, kw
