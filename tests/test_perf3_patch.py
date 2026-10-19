"""Stage-3 performance: the JSON Patch export diffs many changed records in worker processes
(docs/ARCHITECTURE.md §12.8; ``jsonpatch._DiffPool``) and writes EXACTLY the patch of one thread.

* random edit sequences on documents of every kind (value, member, wrapped, single), ``diff=True`` and
  ``diff=False``, both core backends: the patch made with 2 worker processes and chunks of 2 records (many
  chunks in flight, written in order) is byte for byte the one-thread patch; :func:`export_layer` too;
* bulk-edited documents (every record changed, member documents with renamed records): pooled == one thread,
  and the patch applies to the original file;
* automatic workers: only after ``POOL_MIN_BYTES`` of changed records were diffed in this thread, only when
  ``bulk._spawn_safe()``, ``bulk.auto_workers`` of them;
* cancellation (no file, the workers gone), progress (the bytes one thread counts), the fallback to one thread
  when the workers cannot run (at submit, while running), a chunk a worker cannot do (redone here: the error of
  one thread), the in-flight bound, ``_PatchWriter.write_ops`` at the start of a patch, the packed job
  arguments, the original spans read in one go (``_base_spans``).
"""

from __future__ import annotations

import logging
import random
import shutil
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import psutil
import pytest

from edits_helpers import write_unique_doc
from jsonpatch_helpers import canon, doc_value, load_patch, original_value, random_edits, ref_apply
from jsonreshape import bulk, codec, editops, jsonpatch, reader
from jsonreshape.bulk import BulkSpec
from jsonreshape.edits import EditSession, Ref
from jsonreshape.editstore import EditStore, find_pending
from jsonreshape.errors import Cancelled
from jsonreshape.reader import Document
from jsonreshape.tasks import CancelToken, Progress

KINDS = ("array", "jsonl", "stream", "brace_list", "keyed", "wrapped", "wrapped_obj", "single")


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    old_jp = jsonpatch.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)
    jsonpatch.set_core_backend(old_jp)


@pytest.fixture
def closing():
    objs: list[Any] = []
    yield objs
    for obj in reversed(objs):
        try:
            obj.close()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _quiet_export_warnings():
    logging.disable(logging.WARNING)       # the diff=False gap warnings; asserted where they matter
    yield
    logging.disable(logging.NOTSET)


def random_session(tmp_path: Path, closing: list, kind: str, seed: int, *, n: int = 8, steps: int = 20
                   ) -> tuple[Path, dict, Document, EditSession]:
    """A document of ``kind`` after random edits and a bulk change of every record."""
    rng = random.Random(seed * 31 + len(kind))
    src = tmp_path / f"src-{kind}-{seed}.json"
    kw = write_unique_doc(src, kind, n=n, seed=seed, style=rng.choice(["compact", "pretty2", "tab"]))
    doc = Document.open(src, **kw)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    random_edits(s, doc, rng, steps)
    if kind != "single":
        pv = s.bulk_preview(BulkSpec("set_field", {"path": "extra", "value": '{"n": 1.10, "s": "Oʻzbekiston"}'}),
                            None)
        s.bulk_apply(pv)
    random_edits(s, doc, rng, steps // 2)
    return src, kw, doc, s


def export(s: EditSession, path: Path, *, diff: bool = True, workers: int) -> bytes:
    jsonpatch.export_patch(s, path, diff=diff, workers=workers)
    return path.read_bytes()


class _Spawned:
    """Counts the worker pools an export starts and the chunks handed to them."""

    def __init__(self, monkeypatch) -> None:
        self.pools: list[int] = []
        self.jobs = 0
        real = jsonpatch._executor
        real_submit = ProcessPoolExecutor.submit

        def executor(workers: int) -> Any:
            self.pools.append(workers)
            return real(workers)

        def submit(ex, fn, *a, **kw):
            self.jobs += 1
            return real_submit(ex, fn, *a, **kw)

        monkeypatch.setattr(jsonpatch, "_executor", executor)
        monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)


# ----------------------------------------------------------------------------------
# pooled == one thread
# ----------------------------------------------------------------------------------
@pytest.mark.slow
@pytest.mark.parametrize("kind", KINDS)
def test_random_documents_pool_equals_one_thread(tmp_path: Path, closing: list, monkeypatch, kind: str) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 2)
    spawned = _Spawned(monkeypatch)
    for seed in range(2):
        src, kw, doc, s = random_session(tmp_path, closing, kind, seed)
        final = doc_value(doc)
        for diff in (True, False):
            one = export(s, tmp_path / f"one-{seed}-{diff}.json", diff=diff, workers=1)
            two = export(s, tmp_path / f"two-{seed}-{diff}.json", diff=diff, workers=2)
            assert one == two, (kind, seed, diff)
            if diff:
                assert canon(ref_apply(original_value(doc), load_patch(tmp_path / f"two-{seed}-{diff}.json"))) == \
                    canon(final)
    if kind != "single":
        assert spawned.pools and set(spawned.pools) == {2} and spawned.jobs > 4
    else:
        assert not spawned.pools                       # a single document has no chunks of records


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["array", "keyed", "wrapped_obj"])
def test_both_backends_pool_equals_one_thread(tmp_path: Path, closing: list, backend: str, monkeypatch,
                                              kind: str) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 3)
    _src, _kw, _doc, s = random_session(tmp_path, closing, kind, 7)
    for diff in (True, False):
        assert export(s, tmp_path / "one.json", diff=diff, workers=1) == \
            export(s, tmp_path / "two.json", diff=diff, workers=2), (backend, diff)


def bulk_session(tmp_path: Path, closing: list, kind: str, n: int = 400) -> tuple[Path, dict, Document, EditSession]:
    """Every record changed (a bulk set_field and a text replace); member documents: some records renamed."""
    recs = [{"id": i, "name": f"ism {i}", "n": [0.1, 1.5, i], "o": {"k": "Тошкент", "z": -i}} for i in range(n)]
    src = tmp_path / ("src.jsonl" if kind == "jsonl" else "src.json")
    if kind == "keyed":
        src.write_bytes(codec.dumps({f"k{i}": r for i, r in enumerate(recs)}, indent=2))
        kw = {"format": "keyed"}
    elif kind == "jsonl":
        src.write_bytes(b"".join(codec.dumps(r) + b"\n" for r in recs))
        kw = {"format": "jsonl"}
    else:
        src.write_bytes(codec.dumps(recs))
        kw = {"format": "array"}
    doc = Document.open(src, **kw)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    s.bulk_apply(s.bulk_preview(BulkSpec("set_field", {"path": "o.extra", "value": "12345678901234567890"}), None))
    s.bulk_apply(s.bulk_preview(BulkSpec("replace", {"find": "ism", "replacement": "nom"}), range(0, n, 3)))
    if kind == "keyed":
        for i in (4, 9, 33):
            s.rename_key(Ref(i, ()), f"yangi{i}")
    s.set_value(Ref(5, ("name",)), '"Ўзгарди"')
    return src, kw, doc, s


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed"])
def test_every_record_changed_pool_equals_one_thread(tmp_path: Path, closing: list, monkeypatch, kind: str) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 16)
    spawned = _Spawned(monkeypatch)
    src, kw, doc, s = bulk_session(tmp_path, closing, kind)
    one = export(s, tmp_path / "one.json", workers=1)
    two = export(s, tmp_path / "two.json", workers=3)
    assert one == two
    assert spawned.pools == [3] and spawned.jobs >= 400 // 16
    copy = tmp_path / ("copy.jsonl" if kind == "jsonl" else "copy.json")
    shutil.copy(src, copy)
    d2 = Document.open(copy, **kw)
    d2.build_index()
    closing.append(d2)
    s2 = EditSession.open(d2)
    closing.append(s2)
    s2.import_patch(tmp_path / "two.json")
    assert canon(doc_value(d2)) == canon(doc_value(doc))


@pytest.mark.parametrize("kind", ["array", "keyed"])
def test_export_layer_pool_equals_one_thread(tmp_path: Path, closing: list, monkeypatch, kind: str) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 8)
    src, kw, doc, s = bulk_session(tmp_path, closing, kind, n=120)
    s.close()                                              # the layer stays (recovery)
    doc.close()
    doc = Document.open(src, **kw)
    doc.build_index()
    closing.append(doc)
    (info,) = find_pending(doc)
    store = EditStore.open(doc, dir=info.dir, read_only=True, allow_stale=True)
    try:
        for base in (doc, None):                           # diff=True / no document: diff=False semantics
            a, b = tmp_path / "a.json", tmp_path / "b.json"
            na = jsonpatch.export_layer(store, a, base_doc=base, workers=1)
            nb = jsonpatch.export_layer(store, b, base_doc=base, workers=2)
            assert na == nb and a.read_bytes() == b.read_bytes()
    finally:
        store.close(close_reader=True)


# ----------------------------------------------------------------------------------
# automatic workers
# ----------------------------------------------------------------------------------
def test_automatic_workers_start_after_pool_min_bytes(tmp_path: Path, closing: list, monkeypatch) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 10)
    _src, _kw, _doc, s = bulk_session(tmp_path, closing, "array", n=300)
    want = export(s, tmp_path / "one.json", workers=1)
    spawned = _Spawned(monkeypatch)
    assert export(s, tmp_path / "auto.json", workers=None) == want          # far below POOL_MIN_BYTES
    assert not spawned.pools
    monkeypatch.setattr(jsonpatch, "POOL_MIN_BYTES", 3000)
    monkeypatch.setattr(bulk, "_spawn_safe", lambda: False)                 # a plain script as __main__
    assert export(s, tmp_path / "auto.json", workers=None) == want
    assert not spawned.pools
    monkeypatch.setattr(bulk, "_spawn_safe", lambda: True)
    sizes: list[int] = []
    monkeypatch.setattr(bulk, "auto_workers", lambda scope, settings: sizes.append(scope) or 2)
    assert export(s, tmp_path / "auto.json", workers=None) == want
    assert spawned.pools == [2] and sizes and sizes[0] >= 3000
    assert 0 < spawned.jobs < 30                           # the first chunks were diffed in this thread
    monkeypatch.setattr(bulk, "auto_workers", lambda scope, settings: 1)    # e.g. a small memory budget
    assert export(s, tmp_path / "auto.json", workers=None) == want
    assert spawned.pools == [2]


def test_the_log_names_the_workers(tmp_path: Path, closing: list, monkeypatch, caplog) -> None:
    logging.disable(logging.NOTSET)
    _src, _kw, _doc, s = bulk_session(tmp_path, closing, "array", n=60)
    with caplog.at_level(logging.INFO, logger="jsonreshape.jsonpatch"):
        jsonpatch.export_patch(s, tmp_path / "a.json", workers=2)
        jsonpatch.export_patch(s, tmp_path / "b.json", workers=1)
    texts = [r.getMessage() for r in caplog.records if "JSON Patch export" in r.getMessage()]
    assert "workers=2" in texts[0] and "workers=1" in texts[1]


# ----------------------------------------------------------------------------------
# cancellation, fallback, errors, bounds
# ----------------------------------------------------------------------------------
def test_cancelled_pooled_export_leaves_nothing(tmp_path: Path, closing: list, monkeypatch) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 20)
    _src, _kw, _doc, s = bulk_session(tmp_path, closing, "array", n=600)
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    tok = CancelToken()

    def cb(info: Any) -> None:
        if info.done > 3000:
            tok.cancel()

    target = tmp_path / "p.json"
    with pytest.raises(Cancelled):
        jsonpatch.export_patch(s, target, progress=Progress(callback=cb, min_interval=0.0), cancel=tok, workers=2)
    assert not target.exists() and not [q for q in tmp_path.iterdir() if q.name.endswith(".tmp")]
    assert not {p.pid for p in psutil.Process().children(recursive=True)} - before     # the workers are gone
    assert export(s, target, workers=2) == export(s, tmp_path / "one.json", workers=1)


def test_cancel_while_waiting_for_a_worker(tmp_path: Path, closing: list, monkeypatch) -> None:
    _src, _kw, _doc, s = bulk_session(tmp_path, closing, "array", n=100)
    tok = CancelToken()
    real = ProcessPoolExecutor.submit

    def submit(ex, fn, *a, **kw):
        fut = real(ex, fn, *a, **kw)
        tok.cancel()                                       # cancelled while the export waits for the result
        return fut

    monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)
    with pytest.raises(Cancelled):
        jsonpatch.export_patch(s, tmp_path / "p.json", cancel=tok, workers=2)
    assert not (tmp_path / "p.json").exists()


def test_broken_workers_fall_back_to_one_thread(tmp_path: Path, closing: list, monkeypatch) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 7)
    _src, _kw, _doc, s = bulk_session(tmp_path, closing, "keyed", n=100)
    want = export(s, tmp_path / "one.json", workers=1)

    def broken(ex, fn, *a, **kw):
        raise BrokenProcessPool("test: the workers cannot start")

    with monkeypatch.context() as m:
        m.setattr(ProcessPoolExecutor, "submit", broken)
        assert export(s, tmp_path / "a.json", workers=4) == want

    class Fake:
        """Runs the jobs here; from the fourth one on the futures fail like a pool whose workers died."""

        def __init__(self) -> None:
            self.n = 0

        def submit(self, fn, *a):
            self.n += 1
            fut: Future = Future()
            if self.n < 4:
                fut.set_result(fn(*a))
            else:
                fut.set_exception(BrokenProcessPool("test: a worker died"))
            return fut

        def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
            pass

    fake = Fake()
    monkeypatch.setattr(jsonpatch, "_executor", lambda workers: fake)
    assert export(s, tmp_path / "b.json", workers=2) == want
    assert fake.n >= 4


def test_a_chunk_a_worker_cannot_do_is_redone_here(tmp_path: Path, closing: list, monkeypatch) -> None:
    def job(kind: str, args: tuple) -> tuple:
        return jsonpatch._pool_job(kind, jsonpatch._pack_args(kind, args))

    assert job("values", ("", [0], [b'{"a": 1,}'], [b"{}"])) == ("redo",)
    ok = job("values", ("/d", [3], [b'{"a": 1}'], [b'{"a": 2, "b": []}']))
    assert ok[0] == "ok" and ok[2] == 2 and ok[1].startswith(b',\n{"op": "add", "path": "/d/3/b"')
    assert ok[3] is None
    ren = job("members", (("data",), [b'"k2": {"a": 2}'], [b'"k1": {"a": 1}'], [None]))
    assert ren[0] == "ok" and ren[3] == [("k1", "k2")]
    # every job answered "redo": the patch of one thread
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 9)
    _src, _kw, _doc, s = bulk_session(tmp_path, closing, "array", n=80)
    want = export(s, tmp_path / "one.json", workers=1)

    class Redo:
        def submit(self, fn, *a):
            fut: Future = Future()
            fut.set_result(("redo",))
            return fut

        def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
            pass

    with monkeypatch.context() as m:
        m.setattr(jsonpatch, "_executor", lambda workers: Redo())
        assert export(s, tmp_path / "redo.json", workers=2) == want
    # a record that cannot be parsed: the error of one thread, with workers too
    real_read = jsonpatch._Exporter._read_spans

    def damaged(self, starts, ends):
        out = real_read(self, starts, ends)
        if len(out) > 3:
            out[3] = b'{"broken": '
        return out

    monkeypatch.setattr(jsonpatch._Exporter, "_read_spans", damaged)
    errors = []
    for workers in (1, 2):
        with pytest.raises(Exception) as info:
            jsonpatch.export_patch(s, tmp_path / f"bad{workers}.json", workers=workers)
        errors.append((type(info.value), str(info.value)))
        assert not (tmp_path / f"bad{workers}.json").exists()
    assert errors[0] == errors[1]


def test_chunks_in_flight_are_bounded(tmp_path: Path, closing: list, monkeypatch) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 5)
    _src, _kw, _doc, s = bulk_session(tmp_path, closing, "array", n=300)
    real = ProcessPoolExecutor.submit
    real_take = jsonpatch._DiffPool._take
    count = {"submitted": 0, "taken": 0, "peak": 0}

    def submit(ex, fn, *a, **kw):
        count["submitted"] += 1
        count["peak"] = max(count["peak"], count["submitted"] - count["taken"])
        return real(ex, fn, *a, **kw)

    def take(self):
        real_take(self)
        count["taken"] += 1

    monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)
    monkeypatch.setattr(jsonpatch._DiffPool, "_take", take)
    want = export(s, tmp_path / "one.json", workers=1)
    assert export(s, tmp_path / "two.json", workers=2) == want
    assert count["submitted"] >= 300 // 5 and count["taken"] == count["submitted"]
    assert count["peak"] <= 2 * 2                                            # inflight = 2 * workers


def test_write_ops_starts_and_continues_a_patch(tmp_path: Path) -> None:
    import io

    ok = jsonpatch._pool_job("values", jsonpatch._pack_args("values", ("", [0, 1], [b"[1]", b"[1]"],
                                                                        [b"[2]", b"[3]"])))
    data, n = ok[1], ok[2]
    for first in (True, False):
        buf = io.BytesIO()
        w = jsonpatch._PatchWriter(buf, None)
        if not first:
            w.op_at("test", "/9", value=1)
        w.write_ops(b"", 0)
        w.write_ops(data, n)
        w.close()
        one = io.BytesIO()
        w1 = jsonpatch._PatchWriter(one, None)
        if not first:
            w1.op_at("test", "/9", value=1)
        w1.op_at("replace", "/0/0", value=2)
        w1.op_at("replace", "/1/0", value=3)
        w1.close()
        assert buf.getvalue() == one.getvalue() and w.count == w1.count
        assert codec.loads(buf.getvalue())


def test_pack_round_trip() -> None:
    cases = [[], [b""], [None], [None, None], [b"a", None, b"", b"\x00\xff" * 50, None, "Ўзбек".encode()],
             [bytes([i]) * i for i in range(40)]]
    for items in cases:
        packed = jsonpatch._pack(items)
        assert jsonpatch._unpack(packed) == items
        assert (packed[2] is None) == (None not in items)
    args = ("/x", [1, 2], [b"1", None], [b"2", b"3"])
    assert jsonpatch._unpack_args("values", jsonpatch._pack_args("values", args)) == args


def test_base_spans_equal_record_spans(tmp_path: Path, closing: list) -> None:
    _src, _kw, doc, s = bulk_session(tmp_path, closing, "array", n=200)
    ex = jsonpatch._Exporter(state=s.state, layout=jsonpatch._Layout(doc.kind, doc.record_path, False), doc=doc,
                             diff=True, steps=lambda: iter(()), settings=doc.settings, warnings=None, progress=None,
                             cancel=None)
    base = doc.pinned(None)
    rng = random.Random(5)
    for rids in ([7], list(range(10, 60)), list(range(0, 200, 3)), list(range(0, 200, 9)), [150, 3, 99, 4],
                 [5, 5, 6], sorted(rng.sample(range(200), 40)), [0, 199]):
        starts, ends = ex._base_spans(rids)
        assert [(int(a), int(b)) for a, b in zip(starts, ends)] == [base.record_span(r) for r in rids], rids
        assert ex._base_raws(rids) == [base.record_bytes(r) for r in rids]
    with pytest.raises(IndexError):
        ex._base_spans([198, 199, 200])


def test_pooled_progress_counts_what_one_thread_counts(tmp_path: Path, closing: list, monkeypatch) -> None:
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 25)
    _src, _kw, _doc, s = bulk_session(tmp_path, closing, "keyed", n=300)
    seen = []
    for workers in (1, 2):
        infos: list[Any] = []
        prog = Progress(callback=infos.append, min_interval=0.0)
        jsonpatch.export_patch(s, tmp_path / f"p{workers}.json", progress=prog, workers=workers)
        dones = [i.done for i in infos]
        assert prog.finished and dones == sorted(dones) and prog.done > 0
        seen.append(prog.done)
    assert seen[0] == seen[1]
    assert (tmp_path / "p1.json").read_bytes() == (tmp_path / "p2.json").read_bytes()
