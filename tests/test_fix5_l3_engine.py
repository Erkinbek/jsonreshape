"""L3 item 2: the mapping engine raised the process-wide recursion limit permanently.

``engine._ensure_recursion`` raised ``sys.setrecursionlimit`` for deep mapping trees and never lowered it
(1000 -> 20 400 after test_engine.py), which made tests order-dependent (a jschema test had to pin the limit)
and changed GUI behaviour after a mapping ran. The raise is now scoped (``engine._RecursionRoom``): the limit
is raised while a tree is compiled and evaluated and restored afterwards; concurrent evaluations never
lower it under one another (a counter: the last one restores it). Deep trees keep working.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec, engine
from jsonreshape.engine import ApplyOptions, CompiledMapping, apply, preview, transform_plain
from jsonreshape.mapping import MappingTree, infer_tree
from jsonreshape.reader import Document

BASE = 1000


@pytest.fixture
def base_limit():
    """The interpreter's default limit for the test's duration (whatever ran before)."""
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(BASE)
    try:
        yield BASE
    finally:
        sys.setrecursionlimit(old)


def chain(depth: int) -> list:
    rec: Any = {"name": "leaf"}
    for i in range(depth):
        rec = {"name": f"n{i}", "children": [rec]}
    return [rec]


def records_tree(recs: list) -> MappingTree:
    return infer_tree(recs, root_kind="records", max_keys=None, max_depth=None, max_nodes=None)


def need(tree: Any) -> int:
    return engine._RecursionRoom(engine._tree_depth(engine._tree_root(tree))).need


def test_limit_is_restored_after_every_entry_point(base_limit: int, tmp_path: Path) -> None:
    recs = chain(1500)
    tree = records_tree(recs)
    assert need(tree) > 10 * base_limit                     # the evaluation really needs the raise
    want = codec.dumps(recs)

    cm = CompiledMapping(tree)                              # compiling recurses along the tree too
    assert sys.getrecursionlimit() == base_limit
    assert codec.dumps(cm.transform_value(recs)) == want
    assert sys.getrecursionlimit() == base_limit
    items, saw, fb = cm.record_items(recs[0])
    assert codec.dumps(items) == want and saw and fb is engine.MISSING
    assert sys.getrecursionlimit() == base_limit
    assert codec.dumps(cm.transform_record(recs[0])) == want
    assert codec.dumps(cm.transform_member(recs)) == want
    assert sys.getrecursionlimit() == base_limit
    assert codec.dumps(preview(tree, recs[0])) == codec.dumps(recs[0])
    assert sys.getrecursionlimit() == base_limit

    small = chain(250)                                      # the plain port is cubic in the depth
    small_tree = records_tree(small)
    assert need(small_tree) > base_limit
    assert codec.dumps(transform_plain(small_tree, small)) == codec.dumps(small)
    assert sys.getrecursionlimit() == base_limit

    # apply: compiling, evaluating and writing the lazy record array
    deep = chain(600) + chain(5)
    p = tmp_path / "deep.json"
    p.write_bytes(codec.dumps(deep))                        # (jsongen's writer recurses)
    doc = Document.open(p)
    try:
        doc.build_index()
        out = tmp_path / "out.json"
        dtree = records_tree(deep)
        assert need(dtree) > base_limit
        rep = apply(doc, dtree, out, ApplyOptions(indent=None, workers=1))
        assert sys.getrecursionlimit() == base_limit
        assert out.read_bytes() == codec.dumps(deep) and rep.records_in == 2
    finally:
        doc.close()


class Boom(Exception):
    pass


class EvilKey(str):
    """A key equal in hash to "name" whose comparison fails: the evaluation raises deep inside the tree."""

    def __hash__(self) -> int:
        return hash("name")

    def __eq__(self, other: object) -> bool:
        raise Boom()


def test_a_failing_evaluation_restores_the_limit_too(base_limit: int) -> None:
    recs = chain(800)
    cm = CompiledMapping(records_tree(recs))
    bad: Any = {EvilKey("name"): "leaf"}
    for i in range(800):
        bad = {"name": f"n{i}", "children": [bad]}
    with pytest.raises(Boom):
        cm.transform_value([bad])
    assert sys.getrecursionlimit() == base_limit
    with pytest.raises(Boom):
        with engine._RecursionRoom(5000):
            assert sys.getrecursionlimit() == engine._RecursionRoom(5000).need
            raise Boom()
    assert sys.getrecursionlimit() == base_limit
    assert codec.dumps(cm.transform_value(recs)) == codec.dumps(recs)     # and it still works
    assert sys.getrecursionlimit() == base_limit


def test_rooms_nest_and_keep_a_limit_changed_meanwhile(base_limit: int) -> None:
    Room = engine._RecursionRoom
    big, small = Room(5000), Room(100)
    assert small.need < base_limit < big.need
    with small:                                             # within the limit: nothing changes
        assert sys.getrecursionlimit() == base_limit
    with big:
        assert sys.getrecursionlimit() == big.need
        with Room(3000):                                    # nested, smaller: stays
            assert sys.getrecursionlimit() == big.need
        with Room(8000) as r:                               # nested, bigger: raised further
            assert sys.getrecursionlimit() == r.need
        assert sys.getrecursionlimit() == Room(8000).need   # never lowered while the outer block is open
    assert sys.getrecursionlimit() == base_limit
    # someone else changes the limit inside the block: theirs is kept
    with big:
        sys.setrecursionlimit(50_000)
    assert sys.getrecursionlimit() == 50_000


def _two_threads(first: Any, second: Any) -> dict[str, int]:
    """``first`` opens its room, then ``second`` opens and closes its own; ``first`` closes last."""
    seen: dict[str, int] = {}
    opened, closed = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def a() -> None:
        try:
            with first:
                opened.set()
                assert closed.wait(30)
                seen["first_after_second"] = sys.getrecursionlimit()
        except BaseException as exc:                        # pragma: no cover - reported below
            errors.append(exc)
            opened.set()

    def b() -> None:
        try:
            assert opened.wait(30)
            with second:
                seen["second_inside"] = sys.getrecursionlimit()
            seen["second_after"] = sys.getrecursionlimit()
        except BaseException as exc:                        # pragma: no cover - reported below
            errors.append(exc)
        finally:
            closed.set()

    threads = [threading.Thread(target=a), threading.Thread(target=b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert errors == [] and not any(th.is_alive() for th in threads)
    return seen


def test_concurrent_rooms_never_lower_the_limit_under_each_other(base_limit: int) -> None:
    Room = engine._RecursionRoom
    hi, lo = Room(5000).need, Room(1000).need
    # the deeper evaluation is open while a shallower one ends: the limit stays
    seen = _two_threads(Room(5000), Room(1000))
    assert seen == {"second_inside": hi, "second_after": hi, "first_after_second": hi}
    assert sys.getrecursionlimit() == base_limit
    # a deeper evaluation starts and ends while a shallower one is open: never below the shallower need
    seen = _two_threads(Room(1000), Room(5000))
    assert seen["second_inside"] == hi
    assert seen["second_after"] >= lo and seen["first_after_second"] >= lo
    assert sys.getrecursionlimit() == base_limit
    # one that needs nothing extra while another is open
    seen = _two_threads(Room(5000), Room(10))
    assert seen == {"second_inside": hi, "second_after": hi, "first_after_second": hi}
    assert sys.getrecursionlimit() == base_limit


def test_concurrent_deep_evaluations(base_limit: int) -> None:
    """Two threads evaluating deep trees over and over: no RecursionError (a naive restore in one thread
    would pull the limit from under the other) and the limit is back at the end."""
    deep, shallow = chain(1200), chain(300)
    jobs = [(records_tree(deep), deep, 12), (records_tree(shallow), shallow, 150)]
    errors: list[BaseException] = []
    start = threading.Barrier(len(jobs))

    def work(tree: Any, recs: list, n: int) -> None:
        try:
            cm = CompiledMapping(tree)
            want = codec.dumps(recs)
            start.wait(30)
            for _ in range(n):
                assert codec.dumps(cm.transform_value(recs)) == want
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=job) for job in jobs]
    for th in threads:
        th.start()
    for th in threads:
        th.join(120)
    assert errors == [] and not any(th.is_alive() for th in threads)
    assert sys.getrecursionlimit() == base_limit
