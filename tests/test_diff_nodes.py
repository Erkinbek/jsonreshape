"""jsonreshape.diff: the memory-bounded Node path (``diff_nodes``, docs/ARCHITECTURE.md §14.6,
s5/map_repair_diff.md §4.4d).

* ``diff_nodes`` equals ``diff_values`` of the parsed nodes whatever the thresholds (tiny
  ``large_node_bytes`` forces the traversal through the Node API, also with on-disk child
  indexes), for objects, arrays (auto / index / key), numbers as text, member records;
* nodes over :data:`VALUE_DIFF_MAX`: big strings compared by raw text, :class:`Omitted`
  values with a preview, too many object members refused, long arrays paired by position;
* composite nodes of edited (pinned) documents and the record container of wrapped documents;
* member records (keyed) compared as ``{key: value}`` through the Node path, also against a
  JSON Lines file of one-member objects; cancellation and progress.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, diff
from jsonreshape.diff import DiffOptions, Omitted, diff_files, diff_nodes, diff_values
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.paths import MISSING
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from overlay_helpers import OverlayBuilder, make_source, open_expected
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


def mutate(v: Any, rng: random.Random) -> Any:
    """A changed copy: some members changed / removed / added, array elements inserted."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            r = rng.random()
            if r < 0.08:
                continue
            out[k] = mutate(x, rng) if r < 0.9 else jsongen.rand_scalar(rng)
        if rng.random() < 0.15:
            out["added"] = jsongen.rand_value(rng, 2, 4)
        return out
    if isinstance(v, list):
        out_l = [mutate(x, rng) if rng.random() < 0.3 else x for x in v]
        if out_l and rng.random() < 0.3:
            out_l.insert(rng.randrange(len(out_l)), jsongen.rand_value(rng, 2, 4))
        if out_l and rng.random() < 0.2:
            del out_l[rng.randrange(len(out_l))]
        return out_l
    return v if rng.random() < 0.9 else jsongen.rand_scalar(rng)


@pytest.mark.parametrize("seed", range(6))
def test_diff_nodes_equals_diff_values(make_doc, seed: int) -> None:
    rng = random.Random(seed)
    va = {"items": jsongen.records(6, seed=seed), "deep": jsongen.rand_value(rng, 0, 4, 5),
          "arr": [jsongen.rand_value(rng, 1, 3) for _ in range(12)]}
    vb = mutate(codec.loads(codec.dumps(va)), rng)
    raw_a = jsongen.encode(va, "pretty2", seed)
    raw_b = jsongen.encode(vb, "messy_escaped", seed)
    options = (DiffOptions(), DiffOptions(array_match="index"), DiffOptions(array_keys=("items=id",)),
               DiffOptions(numbers="text"), DiffOptions(max_changes=3))
    wants = [plain(diff_values(codec.loads(raw_a), codec.loads(raw_b), o)) for o in options]
    subs = [plain(diff_values(codec.loads(raw_a)["items"], codec.loads(raw_b)["items"], o)) for o in options]
    for large in (150, 1000, None):    # tiny: every bigger node goes through the Node API (disk child indexes)
        settings = Settings(large_node_bytes=large) if large else None
        a = make_doc(raw_a, format="single", settings=settings)
        b = make_doc(raw_b, format="single", settings=settings)
        for opts, want, sub in zip(options, wants, subs):
            got = diff_nodes(a, a.root(), b, b.root(), opts)
            assert plain(got) == want, (opts, large)
            # a sub-node pair: paths relative to the nodes
            assert plain(diff_nodes(a, a.node("items"), b, b.node("items"), opts)) == sub


def test_big_values_are_omitted_and_compared_raw(make_doc, monkeypatch) -> None:
    monkeypatch.setattr(diff, "VALUE_DIFF_MAX", 200)
    long_a = "a" * 1000
    long_b = "a" * 999 + "b"
    a = make_doc(codec.dumps({"s": long_a, "same": long_a, "esc": "A" * 300, "gone": list(range(100)), "k": 1}),
                 format="single", settings=Settings(large_node_bytes=100))
    b = make_doc(codec.dumps({"s": long_b, "same": long_a, "k": 2}).replace(b'"k"', b'"esc":"' + b"\\u0041" * 300
                                                                            + b'","k"'),
                 format="single", settings=Settings(large_node_bytes=100))
    vd = diff_nodes(a, a.root(), b, b.root())
    got = {c.a_path or c.b_path: c for c in vd.changes}
    assert set(got) == {("s",), ("esc",), ("gone",), ("k",)}
    s = got[("s",)]
    assert s.op == "changed" and isinstance(s.a, Omitted) and s.a.kind == "string" and s.a.size == 1002
    assert s.a.preview.startswith('"aaaa') and s.a.preview.endswith("…")
    # a big string with escapes is compared by its raw text (documented deviation)
    assert got[("esc",)].op == "changed"
    gone = got[("gone",)]
    assert gone.op == "removed" and isinstance(gone.a, Omitted) and gone.a.kind == "array"
    assert (got[("k",)].a, got[("k",)].b) == (1, 2)


def test_too_many_object_members(make_doc, monkeypatch, lang_en) -> None:
    monkeypatch.setattr(diff, "OBJECT_KEYS_MAX", 5)
    data = codec.dumps({f"k{i}": {"v": i, "pad": "x" * 50} for i in range(10)})
    a = make_doc(data, format="single", settings=Settings(large_node_bytes=100))
    with pytest.raises(UserInputError) as ei:
        diff_nodes(a, a.root(), a, a.root())
    assert ei.value.key == "diff.error.too_many_keys" and "10" in str(ei.value) and "--key" in ei.value.hint


def test_long_arrays_are_aligned_through_a_sample(make_doc, monkeypatch) -> None:
    va = [{"i": i, "pad": "x" * 20} for i in range(60)]
    vb = [{"i": "front"}] + [dict(x) for x in va]
    vb[-1]["pad"] = "changed"                          # no common prefix nor suffix
    a = make_doc(codec.dumps(va), format="single", settings=Settings(large_node_bytes=100))
    b = make_doc(codec.dumps(vb), format="single", settings=Settings(large_node_bytes=100))
    want = [("added", None, (0,), None, '{"i":"front"}'),
            ("changed", (59, "pad"), (60, "pad"), '"' + "x" * 20 + '"', '"changed"')]
    assert plain(diff_nodes(a, a.root(), b, b.root())) == want
    # over PATIENCE_MAX children: a content-defined sample of the children anchors (no cascade)
    monkeypatch.setattr(diff, "SM_PRODUCT_MAX", 10)
    monkeypatch.setattr(diff, "PATIENCE_MAX", 20)
    vd = diff_nodes(a, a.root(), b, b.root())
    assert plain(vd) == want
    # the in-memory diff samples the same elements (the same token hashes)
    assert plain(vd) == plain(diff_values(va, vb))
    # nothing in common: paired by position, in both paths
    vc = [[i] for i in range(61)]
    c = make_doc(codec.dumps(vc), format="single", settings=Settings(large_node_bytes=100))
    vd = diff_nodes(a, a.root(), c, c.root())
    assert vd.counts == {"added": 1, "removed": 0, "changed": 60, "moved": 0}
    assert plain(vd) == plain(diff_values(va, vc))


def test_edited_documents_and_composite_nodes(tmp_path: Path) -> None:
    for kind in ("array", "jsonl", "wrapped"):
        doc = make_source(tmp_path / f"e.{kind}", kind, n=20, seed=4, settings=Settings(large_node_bytes=300))
        ob = OverlayBuilder(doc, tmp_path / f"layer-{kind}")
        ob.set(2, b'{"id": 2, "name": "edited"}').delete(5).insert(0, b'{"id": -1}')
        doc.set_overlay(ob.state())
        expected = open_expected(doc, ob.records, tmp_path / f"x.{kind}")
        try:
            pinned = doc.pinned()
            root_a, root_b = pinned.root(), expected.root()
            assert diff_nodes(pinned, root_a, expected, root_b).equal, kind
            base = doc.pinned(None)
            vd = diff_nodes(base, base.root(), pinned, pinned.root())
            want = diff_values(codec.loads(b"".join(base.node_chunks(base.root()))),
                               codec.loads(b"".join(pinned.node_chunks(pinned.root()))))
            assert plain(vd) == plain(want) and not vd.equal, kind
            rds: list[Any] = []
            rep = diff_files(base, pinned, mode="document", on_record=rds.append)
            assert rep.changed == 1 and plain(rds[0].diff) == plain(want)
        finally:
            expected.close()
            doc.close()


def test_member_records_through_the_node_path(make_doc, tmp_path: Path) -> None:
    keys = [f"r{i}" for i in range(12)]
    vals = [{"i": i, "blob": list(range(40))} for i in range(12)]
    small = Settings(large_node_bytes=100)            # every record is huge: the Node path
    a = make_doc(vals, "keyed", format="keyed", keys=keys, settings=small)
    keys_b = list(keys)
    keys_b[3] = "renamed"
    vals_b = [dict(v) for v in vals]
    vals_b[7]["i"] = 77
    b = make_doc(vals_b, "keyed", format="keyed", keys=keys_b, settings=small)
    # $key: values; align / position: {key: value}
    for kw in ({}, {"mode": "align"}, {"mode": "position"}):
        rds: list[Any] = []
        rep = diff_files(a, b, on_record=rds.append, **kw)
        normal_a = make_doc(vals, "keyed", format="keyed", keys=keys)
        normal_b = make_doc(vals_b, "keyed", format="keyed", keys=keys_b)
        want: list[Any] = []
        diff_files(normal_a, normal_b, on_record=want.append, **kw)
        assert [(r.status, r.a_index, r.b_index, plain(r.diff)) for r in rds] == \
               [(r.status, r.a_index, r.b_index, plain(r.diff)) for r in want], kw
        assert rep.changed + rep.added + rep.removed == len(rds) and rds
    # a keyed document against JSON Lines of one-member objects (writer as_keyed form)
    lines = b"\n".join(codec.dumps({k: v}) for k, v in zip(keys, vals)) + b"\n"
    c = make_doc(lines, format="jsonl", settings=small)
    rds = []
    rep = diff_files(a, c, mode="position", on_record=rds.append)
    assert rds == [] and rep.equal == 12
    c2 = make_doc(lines.replace(b'"r5"', b'"r5x"'), format="jsonl", settings=small)
    rds = []
    diff_files(a, c2, mode="position", on_record=rds.append)
    assert [(r.status, r.a_index, r.b_index) for r in rds] == [("changed", 5, 5)]
    assert [(c.op, c.a_path, c.b_path) for c in rds[0].diff.changes] == [("removed", ("r5",), None),
                                                                          ("added", None, ("r5x",))]
    # the other way round (the member on the B side), and a record that is not an object at all
    rds = []
    diff_files(c2, a, mode="position", on_record=rds.append)
    assert [(c.op, c.a_path, c.b_path) for c in rds[0].diff.changes] == [("removed", ("r5x",), None),
                                                                          ("added", None, ("r5",))]
    lines3 = lines.split(b"\n")
    lines3[2] = codec.dumps([{"i": 2, "blob": list(range(40))}])
    c3 = make_doc(b"\n".join(lines3), format="jsonl", settings=small)
    for x, y in ((a, c3), (c3, a)):
        rds = []
        diff_files(x, y, mode="position", on_record=rds.append)
        assert [(r.status, r.a_index, r.b_index) for r in rds] == [("changed", 2, 2)]
        assert [(c.op, c.a_path, c.b_path) for c in rds[0].diff.changes] == [("changed", (), ())]


def test_progress_and_cancel(make_doc) -> None:
    data = codec.dumps({"list": [{"i": i, "pad": "x" * 30} for i in range(300)], "meta": {"a": 1}})
    a = make_doc(data, format="single", settings=Settings(large_node_bytes=200))
    b = make_doc(data.replace(b'"i":7,', b'"i":8,'), format="single", settings=Settings(large_node_bytes=200))
    infos: list[Any] = []
    p = Progress(callback=infos.append, min_interval=0)
    vd = diff_nodes(a, a.root(), b, b.root(), progress=p)
    assert vd.counts["changed"] == 1 and p.finished
    done = [i.done for i in infos]
    assert done == sorted(done) and len(set(done)) >= 2
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(Cancelled):
        diff_nodes(a, a.root(), b, b.root(), cancel=cancel)
