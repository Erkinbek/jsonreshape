"""Stage-3 performance work outside the bulk operations (docs/ARCHITECTURE.md §12.3, §12.8, §12.1): every faster
path gives exactly the old result.

* JSON Patch export: the text comparison of the diff (``_same_text``) is ``_same_scalar`` on exact values and
  the fast ``_diff`` emits the operations of the full walk (an independent copy of the old walk); a patch
  exported in tiny chunks with tiny coalesced reads is byte for byte the default one (value and member
  documents, bulk + point edits + deletes + moves + copies + inserts).
* JSON Patch import: ``_load_checked`` is the exact decoder (values, repeated keys at any depth, escapes,
  exact numbers, deep values); the pointer fast path is ``parse_pointer``; the compact span log behaves like a
  dict; a big patch through a tiny record cache, a tiny span log and the array-layer path equals the
  reference applier, and undo restores the file.
* Reading edited documents: the window spans (:class:`reader._EffWindow`) over contiguous / evenly spaced /
  irregular overrides, two overlapping layers (the newest wins), inserted, deleted and moved records, tiny
  windows and tiny contiguous runs -- every record and ``effective_bytes`` as computed naively; a new record
  without content is ``layer_corrupt``; ``_scatter`` directly.
* Array layers: the range reads go through the file (not the mapping), equal the mapped arrays and refuse a
  closed layer.
"""

from __future__ import annotations

import json
import random
import shutil
from array import array
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonpatch_helpers import canon, doc_value, load_patch, original_value, ref_apply
from jsonreshape import codec, editops, jsonpatch, overlay, reader
from jsonreshape.bulk import BulkSpec
from jsonreshape.edits import EditSession, Ref
from jsonreshape.errors import EditError
from jsonreshape.overlay import ArrayLayer, write_array_layer
from jsonreshape.paths import parse_pointer
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from overlay_helpers import OverlayBuilder


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


EXACT = ["0.1", "1e400", "12345678901234567890", "-0.0", "0.0", "1.10", "1.1", "0", "-0", "1", "1.0", "true",
         "false", "null", '""', '"0"', '"1"', '"oʻzbek"', "\"o'zbek\"", '"Тошкент"', '"a\\nb"', '"\\u00e9"',
         '"é"', "2.5e-3", "-12345678901234567890", "18446744073709551616", "[]", "{}", "[1]", '{"a":1}']


# ----------------------------------------------------------------------------------
# JSON Patch export
# ----------------------------------------------------------------------------------
def test_same_text_is_same_scalar_on_exact_values() -> None:
    vals = [codec.loads(t) for t in EXACT]
    for a in vals:
        for b in vals:
            if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
                assert jsonpatch._same_text(a, b) == (codec.dumps(a) == codec.dumps(b))
            else:
                assert jsonpatch._same_text(a, b) == jsonpatch._same_scalar(a, b), (a, b)


class _Ops:
    """A writer that records ``(op, pointer, value text)``."""

    def __init__(self) -> None:
        self.ops: list[tuple] = []

    def op(self, op: str, path: Any, *, value: Any = jsonpatch.NO_VALUE, raw: Any = None, from_: Any = None) -> None:
        self.op_at(op, jsonpatch._ptr(path), value=value, raw=raw,
                   from_=None if from_ is None else jsonpatch._ptr(from_))

    def op_at(self, op: str, ptr: str, *, value: Any = jsonpatch.NO_VALUE, raw: Any = None,
              from_: Any = None) -> None:
        text = None if value is jsonpatch.NO_VALUE else codec.dumps(value)
        self.ops.append((op, ptr, text, raw, from_))


def old_diff(w: _Ops, old: Any, new: Any, prefix: tuple) -> None:
    """The full walk of the original export (every scalar compared by its exact text)."""
    stack: list[tuple[Any, Any, tuple]] = [(old, new, prefix)]
    while stack:
        a, b, p = stack.pop()
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a:
                if k not in b:
                    w.op("remove", p + (k,))
            for k, v in b.items():
                if k not in a:
                    w.op("add", p + (k,), value=v)
            stack.extend(reversed([(a[k], b[k], p + (k,)) for k in a if k in b]))
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            stack.extend(reversed([(a[i], b[i], p + (str(i),)) for i in range(len(a))]))
        elif not jsonpatch._same_scalar(a, b):
            w.op("replace", p, value=b)


def mutate(rng: random.Random, v: Any, depth: int = 0) -> Any:
    """A copy of ``v`` with random changes (members added / removed / renamed / reordered, scalars replaced by
    exact tokens of other types or texts, arrays grown or shrunk)."""
    if isinstance(v, dict):
        items = list(v.items())
        out = {}
        for k, x in items:
            r = rng.random()
            if r < 0.1:
                continue
            if r < 0.15:
                k = k + "~/x"
            out[k] = mutate(rng, x, depth + 1) if rng.random() < 0.5 else x
        if rng.random() < 0.3:
            out["new/~" + str(rng.randint(0, 9))] = codec.loads(rng.choice(EXACT))
        if rng.random() < 0.1:
            out = dict(reversed(list(out.items())))
        return out
    if isinstance(v, list):
        out = [mutate(rng, x, depth + 1) if rng.random() < 0.5 else x for x in v]
        if rng.random() < 0.2:
            out.append(codec.loads(rng.choice(EXACT)))
        return out
    if rng.random() < 0.5:
        return codec.loads(rng.choice(EXACT))
    return v


def test_fast_diff_emits_the_operations_of_the_full_walk() -> None:
    rng = random.Random(7)
    total = 0
    for trial in range(1500):
        a = jsongen.rand_value(rng, max_depth=4)
        if rng.random() < 0.5:
            a = {"r": a, "n": codec.loads(rng.choice(EXACT)), "s": "Oʻzbekiston"}
        a = codec.loads(codec.dumps(a))                 # the codec's exact types
        b = codec.loads(codec.dumps(mutate(rng, a)))
        for pair in ((a, b), (b, a), (a, codec.loads(codec.dumps(a)))):
            want, got = _Ops(), _Ops()
            old_diff(want, *pair, ("7",))
            jsonpatch._diff(got, *pair, ("7",))
            assert got.ops == want.ops, trial
            total += len(want.ops)
    assert total > 3000                                 # (the pairs really differ)


def edited_session(tmp_path: Path, closing: list, kind: str) -> tuple[Path, Document, EditSession, dict]:
    path = tmp_path / ("src.json" if kind != "jsonl" else "src.jsonl")
    recs = [{"id": i, "name": f"ism {i}", "n": [0.1, 1.5, i], "o": {"k": "Тошкент", "z": -i}} for i in range(120)]
    kw: dict = {}
    if kind == "keyed":
        path.write_bytes(codec.dumps({f"k{i}": r for i, r in enumerate(recs)}, indent=2))
        kw = {"format": "keyed"}
    elif kind == "jsonl":
        path.write_bytes(b"".join(codec.dumps(r) + b"\n" for r in recs))
        kw = {"format": "jsonl"}
    else:
        path.write_bytes(codec.dumps(recs))
        kw = {"format": "array"}
    doc = Document.open(path, **kw)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    pv = s.bulk_preview(BulkSpec("set_field", {"path": "o.extra", "value": "12345678901234567890"}),
                        range(0, 120, 3))
    s.bulk_apply(pv)
    s.set_value(Ref(5, ("name",)), '"Ўзгарди"')
    s.delete([Ref(7), Ref(8)])
    if kind != "keyed":
        s.move([Ref(10), Ref(11)], None, 2)
        s.duplicate([Ref(20)])
        s.insert(None, 3, '{"new": -0.0}')
    else:
        s.rename_key(Ref(4, ()), "renamed4")
        s.insert(None, None, '{"new": -0.0}', key="yangi")
    return path, doc, s, kw


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed"])
def test_export_in_tiny_chunks_is_byte_identical(tmp_path: Path, closing: list, monkeypatch, kind: str) -> None:
    _path, doc, s, _kw = edited_session(tmp_path, closing, kind)
    a = tmp_path / "a.json"
    s.export_patch(a)
    monkeypatch.setattr(jsonpatch, "EXPORT_CHUNK", 3)
    monkeypatch.setattr(jsonpatch, "COALESCE_GAP", 0)
    monkeypatch.setattr(jsonpatch, "COALESCE_MAX", 1)
    b = tmp_path / "b.json"
    s.export_patch(b)
    assert a.read_bytes() == b.read_bytes()
    final = doc_value(doc)
    assert canon(ref_apply(original_value(doc), load_patch(a))) == canon(final)


# ----------------------------------------------------------------------------------
# JSON Patch import
# ----------------------------------------------------------------------------------
def _outcome(fn: Any, data: bytes) -> tuple:
    try:
        return ("ok", codec.dumps(fn(data)))
    except jsonpatch._DupKey as exc:
        return ("dup", exc.key)
    except RecursionError:
        return ("deep",)
    except ValueError as exc:
        return ("bad", type(exc).__name__)


def test_load_checked_is_the_exact_decoder() -> None:
    rng = random.Random(3)
    texts: list[bytes] = [t.encode() for t in EXACT]
    texts += [b'{"a":1,"a":2}', b'{"a":{"b":1,"b":1}}', b'[{"x":[{"y":1,"y":2}]}]', b'{"a":1, "a":1}',
              b'{"a": "\\u0061", "\\u0061": 2}', b'{"\\u0061":1,"a":1}', b'{ "op" : "add" , "path":"/a"}',
              b'{"k": [1, 2, {"z": null}], "e": "\\"q\\""}', b"[1, 2] ", b"[1 2]", b'{"a":1}x', b"{}", b'{"a":-0}',
              b"[" * 999 + b"1" + b"]" * 999, b"[" * 1001 + b"1" + b"]" * 1001,
              b'{"a":' * 1100 + b"1" + b"}" * 1100, b"[" + b",".join(b"{}" for _ in range(1500)) + b"]",
              b'[' + b','.join(b'{"d":1,"d":2}' if i == 1400 else b"{}" for i in range(1500)) + b']']
    for _ in range(400):
        v = jsongen.rand_value(rng, max_depth=4)
        style = rng.choice(["compact", "pretty2", "escaped", "messy"])
        texts.append(jsongen.encode(v, style, rng.randint(0, 99)))
    for t in texts:
        assert _outcome(jsonpatch._load_checked, t) == _outcome(jsonpatch._decode_exact, t), t[:80]


def test_pointer_fast_path_is_parse_pointer() -> None:
    rng = random.Random(5)
    alphabet = ["a", "b", "0", "12", "", " ", "ж", "oʻ", "'", '"', ".", "[0]", "-"]
    pointers = ["", "/", "//", "/a/", "/-", "/0/1", "/ a"]
    for _ in range(500):
        pointers.append("".join("/" + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 3)))
                                for _ in range(rng.randint(1, 5))))
    for p in pointers:
        assert jsonpatch._pointer_segments(p, 0, "add", "path") == parse_pointer(p), p


@pytest.mark.parametrize("spill", [False, True])
def test_span_log_behaves_like_a_dict(tmp_path: Path, monkeypatch, spill: bool) -> None:
    monkeypatch.setattr(jsonpatch, "SPAN_FREEZE", 4)
    monkeypatch.setattr(jsonpatch, "SPAN_SPILL", 10 if spill else 1 << 30)
    monkeypatch.setattr(jsonpatch, "SPAN_SPARSE", 3)
    rng = random.Random(11)
    log = jsonpatch._SpanLog(lambda: tmp_path)
    ref: dict[int, tuple[int, int]] = {}
    writes: list[tuple[int, int, int]] = []
    for step in range(3000):
        r = rng.random()
        rid = rng.randrange(400)
        if r < 0.6:
            span = (step * 10, step * 10 + rng.randint(1, 9))
            log.put(rid, *span)
            ref[rid] = span
            writes.append((rid, *span))
        elif r < 0.8:
            log.pop(rid)
            ref.pop(rid, None)
        elif r < 0.995:
            assert log.get(rid) == ref.get(rid)
        else:
            log.clear()
            ref.clear()
            writes.clear()
        if step % 500 == 499:
            last: dict[int, tuple[int, int]] = {}
            for rids, starts, ends in log.blocks():
                for k, a, b in zip(rids, starts, ends):
                    last[k] = (a, b)
            assert last == ref
            assert list(log.live_rids()) == sorted(ref)
    assert (log.path is not None) == spill
    log.close()
    assert not list(tmp_path.iterdir())                  # the spill file is gone


@pytest.mark.parametrize("kind", ["array", "keyed"])
def test_big_import_through_tiny_caches_equals_the_reference(tmp_path: Path, closing: list, monkeypatch,
                                                             backend: str, kind: str) -> None:
    monkeypatch.setattr(jsonpatch, "LRU_MIN", 0)
    monkeypatch.setattr(jsonpatch, "SPAN_FREEZE", 7)
    monkeypatch.setattr(jsonpatch, "SPAN_SPILL", 20)
    monkeypatch.setattr(jsonpatch, "SPAN_SPARSE", 4)
    monkeypatch.setattr(jsonpatch, "RECORDS_MAX", 20)
    monkeypatch.setattr(jsonpatch, "APPEND_BATCH", 500)
    settings = Settings(memory_limit_bytes=40_000)
    path = tmp_path / "src.json"
    recs = [{"id": i, "s": "v" * 50, "n": 0.1} for i in range(300)]
    if kind == "keyed":
        path.write_bytes(codec.dumps({f"k{i}": r for i, r in enumerate(recs)}))
        pre = lambda i: f"/k{i}"  # noqa: E731
    else:
        path.write_bytes(codec.dumps(recs))
        pre = lambda i: f"/{i}"  # noqa: E731
    doc = Document.open(path, settings=settings, format=kind)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc, settings=settings)
    closing.append(s)
    ops: list[dict] = []
    for rnd in range(3):                                  # the same records again and again (cache misses)
        ops += [{"op": "add", "path": f"{pre(i)}/r{rnd}", "value": [rnd, i, -0.0]}
                for i in range(0, 300, 2)]
    ops += [{"op": "remove", "path": pre(4)}, {"op": "replace", "path": f"{pre(6)}/n", "value": 1.10}]
    if kind == "array":
        ops += [{"op": "copy", "from": "/1", "path": "/-"}, {"op": "move", "from": "/0", "path": "/7"}]
    patch = tmp_path / "p.json"
    patch.write_text(json.dumps(ops, ensure_ascii=False), encoding="utf-8")
    before = original_value(doc)
    cs = s.import_patch(patch)
    assert cs.records is None and isinstance(s.state.layers[-2], ArrayLayer)
    assert not list(s.store.dir.glob("*.spans.tmp"))     # the spilled spans were removed
    assert canon(doc_value(doc)) == canon(ref_apply(before, load_patch(patch)))
    s.undo()
    assert canon(doc_value(doc)) == canon(before)


@pytest.mark.parametrize("kind", ["array", "keyed"])
@pytest.mark.parametrize("shape", ["same", "added_and_removed", "removed"])
@pytest.mark.parametrize("records_max", [5, 10_000])
def test_import_step_holds_exactly_the_records_of_the_final_table(tmp_path: Path, closing: list, monkeypatch,
                                                                  kind: str, shape: str, records_max: int) -> None:
    """The finish of an import: with the state's own record table (nothing added, deleted or moved -- also a
    record added and removed again) every changed original record and no new one is in the step; with a
    changed table the table decides. Small steps (content) and big ones (an array layer)."""
    monkeypatch.setattr(jsonpatch, "RECORDS_MAX", records_max)
    path = tmp_path / "src.json"
    recs = [{"id": i, "s": "v" * 20} for i in range(40)]
    if kind == "keyed":
        path.write_bytes(codec.dumps({f"k{i}": r for i, r in enumerate(recs)}))
        pre = lambda i: f"/k{i}"  # noqa: E731
        new = "/yangi"
    else:
        path.write_bytes(codec.dumps(recs))
        pre = lambda i: f"/{i}"  # noqa: E731
        new = "/40"
    doc = Document.open(path, format=kind)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    ops: list[dict] = [{"op": "replace", "path": f"{pre(i)}/s", "value": f"o'zgardi {i}"} for i in range(0, 40, 3)]
    changed = len(ops)
    if shape == "added_and_removed":
        ops += [{"op": "add", "path": new if kind == "keyed" else "/-", "value": {"id": -1}},
                {"op": "add", "path": f"{new}/x", "value": 1.10}, {"op": "remove", "path": new}]
    elif shape == "removed":
        ops += [{"op": "remove", "path": pre(3)}]           # a changed record deleted again
        changed -= 1
    patch = tmp_path / "p.json"
    patch.write_text(json.dumps(ops, ensure_ascii=False), encoding="utf-8")
    before = original_value(doc)
    layers = len(s.state.layers)
    cs = s.import_patch(patch)
    assert canon(doc_value(doc)) == canon(ref_apply(before, load_patch(patch)))
    st = s.state
    if changed > records_max:
        layer = st.layers[-2] if len(st.layers) > layers + 1 else st.layers[-1]
        assert isinstance(layer, ArrayLayer) and len(layer) == changed
    else:
        assert cs.records is not None and len(cs.records) == changed
    s.undo()
    assert canon(doc_value(doc)) == canon(before)


# ----------------------------------------------------------------------------------
# reading edited documents
# ----------------------------------------------------------------------------------
def naive_effective_bytes(doc: Document, window: int) -> int:
    """Pure runs (consecutive base ids, untouched; the runs of one window of ``window`` positions) by their
    extent, everything else by its size."""
    v = doc.pinned()
    ov = v.overlay
    base = doc.pinned(None)
    total = 0
    run: list[int] = []

    def close_run() -> int:
        if not run:
            return 0
        return base.record_span(run[-1])[1] - base.record_span(run[0])[0]

    for pos in range(v.record_count):
        rid = ov.rid_at(pos)
        pure = rid < ov.base_count and ov.content(rid) is None
        if pure and run and rid == run[-1] + 1 and pos % window:
            run.append(rid)
            continue
        total += close_run()
        run = [rid] if pure else []
        if not pure:
            s, e = v.record_span(pos)
            total += e - s
    return total + close_run()


@pytest.mark.parametrize("window,contiguous", [(7, 64), (64, 1 << 18), (65536, 300)])
def test_window_spans_over_every_pattern(tmp_path: Path, monkeypatch, window: int, contiguous: int) -> None:
    monkeypatch.setattr(reader, "OV_WINDOW", window)
    monkeypatch.setattr(reader, "PURE_CONTIGUOUS_MIN", contiguous)
    src = tmp_path / "a.json"
    jsongen.write_file(src, [{"i": i, "s": "x" * (i % 7)} for i in range(400)], "array")
    doc = Document.open(src, format="array")
    doc.build_index()
    ob = OverlayBuilder(doc, tmp_path / "layer")
    try:
        ob.bulk_every(3, lambda p, b: b'{"third":%d}' % p)                     # evenly spaced
        ob.bulk({p: b'{"run":%d}' % p for p in range(100, 160)})                 # contiguous, over the 3rds
        ob.bulk({p: b'{"irr":%d}' % p for p in (5, 6, 9, 40, 41, 43, 200, 399)})  # irregular
        ob.set(7, b'{"point":7}')
        ob.insert(50, b'{"new":1}').insert(0, b'{"new":0}').insert(len(ob), b'{"new":2}')
        ob.delete(*range(250, 270))
        ob.move([300, 301, 302], 10)
        ob.duplicate(12)
        state = ob.state()
        doc.set_overlay(state)
        v = doc.pinned()
        for block_bytes in (100, 4096, 1 << 20):
            got = []
            for blk in v.iter_blocks(None, block_bytes=block_bytes):
                if blk.contiguous:
                    for k in range(len(blk.indices)):
                        assert not state.content(state.rid_at(int(blk.indices[k])))
                got += [(int(blk.indices[k]), blk.record_bytes(k)) for k in range(len(blk.indices))]
            assert [p for p, _b in got] == list(range(len(ob)))
            assert [b for _p, b in got] == ob.records
        assert v.effective_bytes() == naive_effective_bytes(doc, window)
        for src_range in (range(3, 333), range(0)):
            got = [(int(i), b) for blk in v.iter_blocks(src_range) for i, b in
                   zip(blk.indices, (blk.record_bytes(k) for k in range(len(blk.indices))))]
            assert got == [(p, ob.records[p]) for p in src_range]
    finally:
        doc.set_overlay(None)
        ob.close()
        doc.close()


def test_new_record_without_content_is_layer_corrupt(tmp_path: Path) -> None:
    src = tmp_path / "a.json"
    jsongen.write_file(src, [{"i": i} for i in range(20)], "array")
    doc = Document.open(src, format="array")
    doc.build_index()
    ob = OverlayBuilder(doc, tmp_path / "layer")
    try:
        ob.insert(3, b'{"new":1}')
        good = ob.state()
        ob.top.clear()                                    # the new record's content is lost
        bad = ob.state()
        doc.set_overlay(good)
        assert len(list(doc.iter_blocks())) >= 1
        doc.set_overlay(bad)
        with pytest.raises(EditError) as ei:
            list(doc.iter_blocks())
        assert ei.value.key == "edits.error.layer_corrupt"
        with pytest.raises(EditError):
            doc.pinned().effective_bytes()
    finally:
        doc.set_overlay(None)
        ob.close()
        doc.close()


def test_scatter_contiguous_spaced_and_irregular() -> None:
    for keys in ([5], [5, 6, 7, 8], [3, 6, 9, 12], [2, 3, 9, 10, 17], [2, 50, 51, 52], [2, 61]):
        n = 60
        spans = array("Q", bytes(16 * n))
        marks = bytearray(n)
        ka = array("Q", keys)
        pairs = array("Q", [x for k in keys for x in (k * 100, k * 100 + 7)])
        reader._scatter(spans, marks, ka, pairs, 0, len(keys), -2)
        for i in range(n):
            k = i + 2
            if k in keys:
                assert marks[i] == 1 and (spans[2 * i], spans[2 * i + 1]) == (k * 100, k * 100 + 7)
            else:
                assert marks[i] == 0 and spans[2 * i] == spans[2 * i + 1] == 0


# ----------------------------------------------------------------------------------
# array layers
# ----------------------------------------------------------------------------------
class _NoSlices:
    def __getitem__(self, _i: Any) -> Any:
        raise AssertionError("the mapping was sliced")

    def release(self) -> None:
        pass


def test_array_layer_range_reads_use_the_file(tmp_path: Path) -> None:
    n = 50_000
    entries = [(3 * i, 10 * i, 10 * i + 5) for i in range(n)]
    path = tmp_path / "layer.bin"
    write_array_layer(path, entries, flags=0, segments=(1,), work_dir=tmp_path)
    layer = ArrayLayer.open(path)
    raw = layer._raw
    try:
        layer._raw = _NoSlices()                          # range reads must not slice the mapping
        keys = layer.keys_in(30, 3 * 40_000)
        assert list(keys) == [3 * i for i in range(10, 40_000)]
        rids, pairs = layer.spans_in(31, 3 * n)
        assert list(rids) == [3 * i for i in range(11, n)]
        assert list(pairs) == [x for i in range(11, n) for x in (10 * i, 10 * i + 5)]
        assert list(layer.items()) == [(r, (s, e)) for r, s, e in entries]
        assert layer.get(3 * 777) == (7770, 7775) and layer.get(5) is None
    finally:
        layer._raw = raw
        layer.close()
    with pytest.raises(ValueError):
        layer.keys_in(0, 10)
    with pytest.raises(ValueError):
        layer.spans_in(0, 10)
    copy_path = tmp_path / "copy.bin"
    shutil.copy(path, copy_path)
    layer2 = ArrayLayer.open(copy_path)
    layer2.close()
    copy_path.unlink()                                    # nothing keeps the file open after close()
    assert overlay.EDIT_BASE > 0
