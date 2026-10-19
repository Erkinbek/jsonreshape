"""Stage-3 performance work on the bulk operations (docs/ARCHITECTURE.md §12.6, AGENT_PROMPT §12): worker
processes and the faster single-thread pass of :func:`jsonreshape.bulk.preview` give EXACTLY the result of the
naive reference of ``tests/test_bulk.py`` (counts, the first examples / problems, every unit byte, test 20 after
the commit, undo).

* every rule of ``tests/test_bulk_matrix.py`` through real worker processes (spawned; tiny jobs so that many
  blocks are in flight and merged in order; tiny append batches) on value and member documents, plain and
  edited states, both core backends;
* random documents of every kind: the pooled preview equals the single-thread one (also the unit bytes read
  back from the pending layer);
* cancellation (everything deleted, the workers gone), progress, the fallback to one thread when the workers
  cannot run, errors raised in a worker, the automatic number of workers (memory bound), the in-flight bound;
* the duplicate-key fast path (the dump comparison): duplicate keys, deep records, many brackets, escapes.
"""

from __future__ import annotations

import threading
from array import array
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

import psutil
import pytest

from jsonreshape import bulk
from jsonreshape.bulk import BulkSpec
from jsonreshape.errors import Cancelled, EditError, PathSyntaxError
from jsonreshape.overlay import ArrayLayer
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from test_bulk import Rig, backend, check_rule, reference, rigs, values_doc  # noqa: F401 - fixtures
from test_bulk_matrix import KINDS, RULES

CONTENT_RULES = [(k, p) for k, p in RULES if k != "dedupe"]


@pytest.fixture
def pooled(monkeypatch):
    """Every preview uses 2 worker processes, jobs of ~300 bytes (many blocks in flight, merged in order) and
    append batches of 200 bytes."""
    monkeypatch.setattr(bulk, "_spawn_safe", lambda: True)
    monkeypatch.setattr(bulk, "auto_workers", lambda scope_bytes, settings: 2)
    monkeypatch.setattr(bulk, "POOL_BLOCK_BYTES", 300)
    monkeypatch.setattr(bulk, "APPEND_BYTES", 200)


def snapshot(rig: Rig, pv: Any) -> tuple:
    """Everything a preview reports, plus the units of its pending layer (rid -> bytes)."""
    units = None
    if pv.layer is not None:
        rig.store.sync()
        layer = ArrayLayer.open(rig.store.dir / pv.layer)
        try:
            units = [(rid, rig.store.reader.read(a, b)) for rid, (a, b) in layer.items()]
        finally:
            layer.close()
    return (pv.scanned, pv.records_changed, pv.values_changed, pv.conflicts, pv.failures, pv.skipped_huge,
            pv.bytes_new, pv.examples, pv.problems, pv.records, pv.flags, pv.warnings, units)


# ----------------------------------------------------------------------------------
# the rule matrix through worker processes
# ----------------------------------------------------------------------------------
@pytest.mark.slow
@pytest.mark.parametrize("kind,style,edited", [("array", "compact", False), ("keyed", "pretty2", True),
                                               ("wrapped_obj", "compact", True), ("jsonl", "pretty2", False)])
def test_every_rule_through_worker_processes(rigs, backend, pooled, kind, style, edited) -> None:
    rig: Rig = rigs(kind, n=30, seed=KINDS.index(kind) + 7, style=style)
    if edited:
        rig.edit_some(seed=4)
    used = set()
    for rule, params in CONTENT_RULES:
        pv = check_rule(rig, BulkSpec(rule, params))
        used.add(pv.workers)
    assert used == {2}


@pytest.mark.slow
def test_scopes_through_worker_processes(rigs, backend, pooled) -> None:
    rig: Rig = rigs("array", n=40, seed=17)
    rig.edit_some(seed=3)
    for rule, params in CONTENT_RULES[::3]:
        for scope in (range(1, rig.state.count, 3), [5, 2, 9, 2, 33]):
            pv = check_rule(rig, BulkSpec(rule, params), scope)
            assert pv.workers == 2


@pytest.mark.slow
@pytest.mark.parametrize("kind", KINDS)
def test_random_documents_pool_equals_one_thread(rigs, backend, monkeypatch, kind) -> None:
    rig: Rig = rigs(kind, n=50, seed=101 + KINDS.index(kind), style="pretty2" if kind in ("keyed", "stream")
                    else "compact")
    rig.edit_some(seed=9)
    monkeypatch.setattr(bulk, "POOL_BLOCK_BYTES", 500)
    specs = [BulkSpec("replace", {"find": "a", "replacement": "Ä"}),
             BulkSpec("set_field", {"path": "extra.n", "value": "1e400"}),
             BulkSpec("delete_field", {"path": "..name"}),
             BulkSpec("convert_type", {"path": "**.id", "to": "string"})]
    for spec in specs:
        one = rig.preview(spec, workers=1)
        a = snapshot(rig, one)
        one.discard()
        two = rig.preview(spec, workers=2)
        b = snapshot(rig, two)
        two.discard()
        assert one.workers == 1 and two.workers == 2
        assert a == b, spec


# ----------------------------------------------------------------------------------
# cancellation, progress, fallback, errors, sizing
# ----------------------------------------------------------------------------------
def big_rig(rigs, n: int = 3000) -> Rig:
    recs = [{"id": i, "name": f"ism {i}", "city": "Toshkent" if i % 3 else "Xiva", "n": [i, 0.1, -0.0]}
            for i in range(n)]
    return rigs(recs, "array")


def _layer_files(rig: Rig) -> list[str]:
    return sorted(p.name for p in rig.store.dir.iterdir()) if rig.store.dir.exists() else []


def test_cancelled_pooled_preview_leaves_nothing(rigs, backend, monkeypatch) -> None:
    rig = big_rig(rigs)
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    monkeypatch.setattr(bulk, "POOL_BLOCK_BYTES", 2000)
    tok = CancelToken()

    def cb(info: Any) -> None:
        if info.done > 0:
            tok.cancel()

    prog = Progress(callback=cb, min_interval=0.0)
    with pytest.raises(Cancelled):
        rig.preview(BulkSpec("set_field", {"path": "x", "value": "1"}), progress=prog, cancel=tok, workers=2)
    files = _layer_files(rig)
    assert not [f for f in files if f.startswith(("layer-", "bulk-")) or f.endswith(".tmp")], files
    assert [f for f in files if f.startswith("seg-")] in ([], ["seg-00000.dat"]), files
    left = {p.pid for p in psutil.Process().children(recursive=True)} - before
    assert not left                                                # the workers were shut down
    pv = rig.preview(BulkSpec("set_field", {"path": "x", "value": "1"}), workers=2)
    assert pv.records_changed == 3000 and pv.workers == 2
    pv.discard()


def test_cancel_while_waiting_for_a_worker(rigs, backend, monkeypatch) -> None:
    rig = big_rig(rigs, 600)
    tok = CancelToken()
    real = ProcessPoolExecutor.submit
    submitted = threading.Event()

    def submit(self, fn, *a, **kw):
        fut = real(self, fn, *a, **kw)
        submitted.set()
        tok.cancel()                     # cancelled while the parent waits for the first result
        return fut

    monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)
    with pytest.raises(Cancelled):
        rig.preview(BulkSpec("replace", {"find": "toshkent", "replacement": "T"}), cancel=tok, workers=2)
    assert submitted.is_set()
    assert not [f for f in _layer_files(rig) if f.startswith(("layer-", "bulk-"))]


def test_pooled_progress_reaches_the_end(rigs, backend, monkeypatch) -> None:
    rig = big_rig(rigs)
    monkeypatch.setattr(bulk, "POOL_BLOCK_BYTES", 4000)
    infos: list[Any] = []
    prog = Progress(callback=infos.append, min_interval=0.0)
    pv = rig.preview(BulkSpec("replace", {"find": "toshkent", "replacement": "Tashkent"}), progress=prog, workers=3)
    assert pv.workers == 3 and pv.records_changed == 2000
    assert prog.finished and prog.done == prog.total > 0
    dones = [i.done for i in infos]
    assert dones == sorted(dones)
    pv.discard()


def test_broken_workers_fall_back_to_one_thread(rigs, backend, monkeypatch) -> None:
    rig = big_rig(rigs, 400)
    spec = BulkSpec("set_field", {"path": "x", "value": '"Oʻzbekiston"'})
    ref = reference(rig, spec)

    def broken(self, fn, *a, **kw):
        raise BrokenProcessPool("test: the workers cannot start")

    monkeypatch.setattr(ProcessPoolExecutor, "submit", broken)
    pv = rig.preview(spec, workers=4)
    assert pv.workers == 1
    assert (pv.records_changed, pv.values_changed, pv.bytes_new) == (ref["records_changed"], ref["values_changed"],
                                                                     ref["bytes_new"])
    files = _layer_files(rig)
    assert len([f for f in files if f.startswith("seg-0000") and f != "seg-00000.dat"]) == 1   # the dropped one is gone
    pv.discard()


def test_worker_errors_reach_the_caller(monkeypatch) -> None:
    err = bulk._worker_error(("error", "EditError", "edits.error.internal", {"what": "x", "detail": "y"}))
    assert isinstance(err, EditError) and err.key == "edits.error.internal" and err.params["detail"] == "y"
    err = bulk._worker_error(("error", "PathSyntaxError", "error.path_syntax", {"pos": 2, "text": "a..", "detail": ""}))
    assert isinstance(err, PathSyntaxError) and err.pos == 2
    assert type(bulk._worker_error(("error", "NoSuchClass", "edits.error.internal", {}))) is EditError

    class Boom:
        def run(self, *a: Any) -> Any:
            raise EditError("edits.error.internal", what="unit", detail="bad")

    monkeypatch.setitem(bulk._WORKER, "runner", Boom())
    res = bulk._pool_block(b"1", array("Q", [0, 1]).tobytes(), array("Q", [0]).tobytes(), True, True)
    assert res == ("error", "EditError", "edits.error.internal", {"what": "unit", "detail": "bad"})


def test_automatic_workers(monkeypatch) -> None:
    s = Settings()
    monkeypatch.setattr(bulk.os, "cpu_count", lambda: 24)
    assert bulk.auto_workers(bulk.POOL_MIN_BYTES - 1, s) == 1
    assert bulk.auto_workers(bulk.POOL_MIN_BYTES, s) == min(23, bulk.POOL_MAX_WORKERS,
                                                            (s.memory_limit_bytes // 2) // bulk.WORKER_MEMORY)
    assert bulk.auto_workers(1 << 40, s) == 4                     # the default 512 MiB budget: 4 workers
    small = Settings(memory_limit_bytes=128 << 20)
    assert bulk.auto_workers(1 << 40, small) == 1
    monkeypatch.setattr(bulk.os, "cpu_count", lambda: 2)
    assert bulk.auto_workers(1 << 40, s) == 1


def test_small_scopes_and_unsafe_mains_stay_in_this_thread(rigs, backend, monkeypatch) -> None:
    rig = big_rig(rigs, 50)
    pv = rig.preview(BulkSpec("set_field", {"path": "x", "value": "1"}))
    assert pv.workers == 1                                         # far below POOL_MIN_BYTES
    pv.discard()
    monkeypatch.setattr(bulk, "POOL_MIN_BYTES", 0)
    monkeypatch.setattr(bulk, "_spawn_safe", lambda: False)        # a plain script as __main__
    pv = rig.preview(BulkSpec("set_field", {"path": "x", "value": "1"}))
    assert pv.workers == 1
    pv.discard()
    monkeypatch.setattr(bulk, "_spawn_safe", lambda: True)
    pv = rig.preview(BulkSpec("set_field", {"path": "x", "value": "1"}))
    assert pv.workers > 1 or (bulk.os.cpu_count() or 1) <= 2
    pv.discard()


def test_jobs_in_flight_are_bounded(rigs, backend, monkeypatch) -> None:
    rig = big_rig(rigs, 2000)
    monkeypatch.setattr(bulk, "POOL_BLOCK_BYTES", 1000)
    real = ProcessPoolExecutor.submit
    real_take = bulk._Pass._take
    count = {"submitted": 0, "taken": 0, "peak": 0}

    def submit(self, fn, *a, **kw):
        count["submitted"] += 1
        count["peak"] = max(count["peak"], count["submitted"] - count["taken"])
        return real(self, fn, *a, **kw)

    def take(self, fut):
        real_take(self, fut)
        count["taken"] += 1

    monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)
    monkeypatch.setattr(bulk._Pass, "_take", take)
    pv = rig.preview(BulkSpec("set_field", {"path": "x", "value": "1"}), workers=2)
    assert pv.records_changed == 2000 and count["submitted"] > 10 and count["taken"] == count["submitted"]
    assert count["peak"] <= 2 * 2                                  # inflight = 2 * workers
    pv.discard()


# ----------------------------------------------------------------------------------
# the duplicate-key fast path
# ----------------------------------------------------------------------------------
def test_duplicate_keys_deep_records_and_escapes_with_the_dump_comparison(rigs, backend) -> None:
    deep = "[" * 400 + "1" + "]" * 400                             # deep (the reference recurses)
    many = "[" + ",".join("{}" for _ in range(1200)) + "]"         # many brackets, shallow
    lines = [b'{"a":1,"s":"x"}', b'{"a":1,"a":2,"s":"x"}', b'{"s":"x","b":{"c":1,"c":1}}',
             ('{"s":"x","d":%s}' % deep).encode(), ('{"s":"x","m":%s}' % many).encode(),
             b'{"s":"x\\u00e9","e":"\\n"}', b'{"s":"x","n":[0.1,1e400,12345678901234567890,-0.0,1.10]}']
    for i in range(300):                                           # enough records for the adaptive decisions
        lines.append(b'{"i":%d,"s":"x"}' % i)
    rig = rigs(b"\n".join(lines) + b"\n", name="d.jsonl")
    for spec in (BulkSpec("set_field", {"path": "t", "value": "true"}),
                 BulkSpec("replace", {"find": "x", "replacement": "y"}),
                 BulkSpec("set_field", {"path": "a", "value": "1", "mode": "if_missing"})):
        pv = check_rule(rig, spec)
        assert pv.conflicts == (2 if spec.params.get("mode") != "if_missing" else 1)


def test_fast_path_switches_itself_off_when_it_does_not_pay(rigs, backend, monkeypatch) -> None:
    recs = [{"id": i, "k": "v"} for i in range(5000)]
    rig = values_doc(rigs, recs)
    seen: list[Any] = []
    real_init = bulk._Runner.__init__

    def init(self, *a: Any, **kw: Any) -> None:
        real_init(self, *a, **kw)
        seen.append(self)

    monkeypatch.setattr(bulk._Runner, "__init__", init)
    spec = BulkSpec("set_field", {"path": "k", "value": '"w"', "mode": "if_missing"})   # nothing changes
    pv = check_rule(rig, spec)
    assert pv.records_changed == 0 and not seen[-1].fast
    spec = BulkSpec("set_field", {"path": "k", "value": '"w"'})                         # everything changes
    pv = check_rule(rig, spec)
    assert pv.records_changed == 5000 and seen[-1].fast and seen[-1].hits == seen[-1].tries > 0


def test_member_documents_and_pretty_styles_in_this_thread(rigs, backend) -> None:
    for kind, style in (("keyed", "pretty2"), ("wrapped_obj", "pretty4"), ("brace_list", "compact")):
        rig: Rig = rigs(kind, n=40, seed=3, style=style)
        for rule, params in CONTENT_RULES[::4]:
            check_rule(rig, BulkSpec(rule, params))


def test_examples_and_problems_are_the_first_ones_across_blocks(rigs, backend, monkeypatch) -> None:
    recs: list[Any] = []
    for i in range(120):
        recs.append({"id": i, "v": "a" if i % 7 else 5, "o": {"x": 1} if i % 11 else [1]})
    rig = rigs(recs, "array")
    monkeypatch.setattr(bulk, "POOL_BLOCK_BYTES", 120)
    for spec in (BulkSpec("convert_type", {"path": "v", "to": "number"}),       # failures for "a"
                 BulkSpec("set_field", {"path": "o.y", "value": "2"})):         # conflicts for the arrays
        ref = reference(rig, spec)
        pv = rig.preview(spec, workers=2)
        assert pv.workers == 2
        assert [tuple(vars(e).values()) for e in pv.examples] == ref["examples"]
        assert [tuple(vars(e).values()) for e in pv.problems] == ref["problems"]
        assert (pv.conflicts, pv.failures) == (ref["conflicts"], ref["failures"])
        pv.discard()
