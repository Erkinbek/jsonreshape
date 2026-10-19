"""jsonreshape.jsonpatch: exporting the net effect of the edits (docs/ARCHITECTURE.md §12.8, export side).

The exact operations for the basic shapes (removes descending, the fewest moves from the maximum-weight
increasing subsequence of runs, copies of duplicated originals, a deleted original replaced by its copy, adds
ascending, structural diffs at the final positions with exact number tokens, the wrapper), keyed renames
(chains, cycles through a temporary key), ``diff=False`` (no byte of the data file read; a source changed on
disk), :func:`export_layer` for pending, stale and damaged layers, the warnings of the known gaps, atomic
writing, progress and cancellation. Every exported patch is applied again to a copy of the ORIGINAL file (the
import side and the reference applier of ``jsonpatch_helpers``) and must give the edited document.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from edits_helpers import open_doc, write_unique_doc
from jsonpatch_helpers import canon, doc_value, load_patch, op_texts, original_value, ref_apply
from jsonreshape import codec, edits, editstore, jsonpatch
from jsonreshape.edits import EditSession, Ref
from jsonreshape.editstore import EditStore, find_pending
from jsonreshape.errors import Cancelled, EditError, FileChangedError
from jsonreshape.jsonpatch import export_layer
from jsonreshape.reader import Document
from jsonreshape.tasks import CancelToken, Progress


@pytest.fixture
def closing():
    objs: list[Any] = []
    yield objs
    for obj in reversed(objs):
        try:
            obj.close()
        except Exception:
            pass


def open_values(tmp_path: Path, closing: list, values: list, *, fmt: str = "array", name: str = "src.json",
                text: bytes | None = None, **kw: Any) -> tuple[Document, EditSession]:
    path = tmp_path / name
    if text is not None:
        path.write_bytes(text)
    elif fmt == "jsonl":
        path.write_bytes(b"".join(codec.dumps(v) + b"\n" for v in values))
    elif fmt == "keyed":
        path.write_bytes(codec.dumps(dict(values), indent=2))
    else:
        path.write_bytes(codec.dumps(values, indent=2))
    doc = Document.open(path, format=fmt, **kw)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    return doc, s


def check_roundtrip(tmp_path: Path, closing: list, doc: Document, patch: Path, *, kw: dict | None = None,
                    original: Path | None = None, exact: bool = True) -> None:
    """The patch turns a copy of the original file into the edited document (import + reference applier)."""
    final = doc_value(doc)
    src = original if original is not None else doc.source_path
    ops = load_patch(patch)
    if exact:
        assert canon(ref_apply(original_value_of(src, kw or {}, closing), ops)) == canon(final)
    copy = tmp_path / f"copy-{len(closing)}{doc.source_path.suffix}"
    shutil.copy(src, copy)
    d2 = Document.open(copy, **(kw or {"format": doc.kind, "record_path": doc.record_path}))
    d2.build_index()
    closing.append(d2)
    s2 = EditSession.open(d2)
    closing.append(s2)
    s2.import_patch(patch)
    if exact:
        assert canon(doc_value(d2)) == canon(final)
    else:
        assert canon(doc_value(d2)) == canon(ref_apply(original_value(d2), ops))


def original_value_of(path: Path, kw: dict, closing: list) -> Any:
    d = Document.open(path, **kw) if kw else Document.open(path)
    d.build_index()
    closing.append(d)
    return original_value(d)


# ----------------------------------------------------------------------------------
# value documents: the operations of the basic shapes
# ----------------------------------------------------------------------------------
def test_nothing_changed_writes_an_empty_patch(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, [1, 2])
    assert s.export_patch(tmp_path / "p.json") == 0
    assert (tmp_path / "p.json").read_bytes() == b"[]\n"
    s.set_value(Ref(0, ()), "5")
    s.undo()
    assert s.export_patch(tmp_path / "p.json") == 0


def test_removes_moves_copies_adds_and_diffs(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, [{"id": i, "v": [i, i]} for i in range(6)])
    s.move([Ref(5, ())], None, 0)                           # 5 0 1 2 3 4
    s.delete_records([3])                                    # 5 0 1 3 4      (original 2 deleted)
    s.duplicate([Ref(2, ())])                                # 5 0 1 1' 3 4   (a copy of original 1)
    s.insert(None, 6, '{"new": 1.10}')                       # ... 4 new
    s.set_value(Ref(1, ("v", 1)), "1e400")                   # original 0 changed
    s.set_value(Ref(4, ("v",)), '[3]')                       # original 3: array length changed
    s.insert(Ref(5, ()), 2, "-0", key="z")                   # original 4: a member added
    p = tmp_path / "p.json"
    assert s.export_patch(p) == 7
    assert op_texts(p) == [
        '{"op": "remove", "path": "/2"}',                    # (1) the deleted original, descending
        '{"op": "move", "from": "/4", "path": "/0"}',        # (2) the one record outside the kept runs
        '{"op": "copy", "from": "/2", "path": "/3"}',        # (3) the duplicate of an unchanged original
        '{"op": "add", "path": "/6", "value": {"new":1.10}}',   # (4) new records, exact text
        '{"op": "replace", "path": "/1/v/1", "value": 1e400}',  # (5) diffs at the final positions
        '{"op": "replace", "path": "/4/v", "value": [3]}',
        '{"op": "add", "path": "/5/z", "value": -0}',
    ]
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "array"})


def test_fewest_moves(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, list(range(10)), fmt="jsonl")
    for p, to in ((9, 0), (4, 8), (2, 5), (7, 1)):
        s.move([Ref(p, ())], None, to)
    final = doc_value(doc)
    assert final != list(range(10))
    p = tmp_path / "p.json"
    n = s.export_patch(p)
    ops = load_patch(p)
    assert all(op["op"] == "move" for op in ops)
    # n = records outside the longest increasing run sequence of the final order
    import bisect
    tails: list[int] = []
    for v in final:
        i = bisect.bisect_left(tails, v)
        tails[i:i + 1] = [v]
    assert n == len(final) - len(tails)
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "jsonl"})
    s.undo()
    s.undo()
    doc2_ops = s.export_patch(tmp_path / "p2.json")
    assert doc2_ops == 2


def test_reversed_and_rotated_orders(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, list(range(7)))
    s.move([Ref(i, ()) for i in range(7)], None, 7)      # no change
    assert s.export_patch(tmp_path / "p.json") == 0
    for k in range(6):                                    # reverse: each record to the front
        s.move([Ref(k + 1, ())], None, 0)
    assert doc_value(doc) == list(range(6, -1, -1))
    assert s.export_patch(tmp_path / "p.json") == 6
    check_roundtrip(tmp_path, closing, doc, tmp_path / "p.json", kw={"format": "array"})


def test_deleted_original_replaced_by_its_copy(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, [{"id": i} for i in range(4)])
    s.duplicate_records([1, 2])                          # 0 1 1' 2 2' 3
    s.delete_records([1, 3])                             # 0 1' 2' 3: the originals 1 and 2 are gone, copies remain
    p = tmp_path / "p.json"
    assert s.export_patch(p) == 0                        # the copies take the originals' places: no operation
    s.move([Ref(1, ())], None, 4)                        # 0 2' 3 1'
    s.duplicate([Ref(0, ())])                            # 0 0' 2' 3 1'
    n = s.export_patch(p)
    assert op_texts(p) == ['{"op": "move", "from": "/1", "path": "/3"}', '{"op": "copy", "from": "/0", "path": "/1"}']
    assert n == 2
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "array"})


def test_diff_details_exact_tokens_and_types(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, [{"a": 1.0, "b": "x", "c": [1, 2], "d": {"e": None}, "f": True,
                                              "g": codec.loads("-0")}])
    s.set_value(Ref(0, ("a",)), "1.00")                  # same value, other token -> replace
    s.set_value(Ref(0, ("b",)), "1")                     # type change
    s.set_value(Ref(0, ("c",)), "[1, 2.0]")              # equal length: per index
    s.delete([Ref(0, ("d", "e"))])
    s.insert(Ref(0, ("d",)), 0, '"\\u00e9\\ud800 O\'zbek"', key="new/k~")
    s.set_value(Ref(0, ("f",)), "1")
    s.set_value(Ref(0, ("g",)), "0")
    p = tmp_path / "p.json"
    s.export_patch(p)
    assert op_texts(p) == [
        '{"op": "replace", "path": "/0/a", "value": 1.00}',
        '{"op": "replace", "path": "/0/b", "value": 1}',
        '{"op": "replace", "path": "/0/c/1", "value": 2.0}',
        '{"op": "remove", "path": "/0/d/e"}',
        '{"op": "add", "path": "/0/d/new~1k~0", "value": "é\\ud800 O\'zbek"}',
        '{"op": "replace", "path": "/0/f", "value": 1}',
        '{"op": "replace", "path": "/0/g", "value": 0}',
    ]
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "array"})


def test_new_records_keep_their_exact_text(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, [1])
    s.insert(None, 1, '{"n": [0.1, 1e400, 12345678901234567890, -0.0, 1.10, -0], "s": "\\u00e9\\n\\ud83d\\ude00"}')
    p = tmp_path / "p.json"
    s.export_patch(p)
    assert op_texts(p) == ['{"op": "add", "path": "/1", "value": {"n":[0.1,1e400,12345678901234567890,-0.0,1.10,-0],'
                           '"s":"\\u00e9\\n\\ud83d\\ude00"}}']


# ----------------------------------------------------------------------------------
# member records, single documents, the wrapper
# ----------------------------------------------------------------------------------
def test_keyed_renames_chains_and_cycles(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, [(k, {"v": k}) for k in "abcdef"], fmt="keyed")
    s.rename_key(Ref(0, ()), "tmp")
    s.rename_key(Ref(1, ()), "a")                        # b -> a
    s.rename_key(Ref(0, ()), "b")                        # a -> b: a cycle with b -> a
    s.rename_key(Ref(3, ()), "c2")                       # d -> c2 (c2 is new; c -> d below would chain)
    s.rename_key(Ref(2, ()), "d")                        # c -> d: d is renamed away first
    s.delete([Ref(4, ())])                               # e deleted
    s.insert(None, 0, '{"v": "e2"}', key="e")            # a new record with the deleted key
    s.set_value(Ref(5, ("v",)), '"F"')                   # f changed (e2 is at 0, e was deleted)
    p = tmp_path / "p.json"
    s.export_patch(p)
    ops = load_patch(p)
    kinds = [op["op"] for op in ops]
    assert kinds[0] == "remove" and ops[0]["path"] == "/e" and kinds.count("move") == 5
    assert any(op["op"] == "move" and op["path"].startswith("/jsonreshape-tmp-") for op in ops)
    assert {"op": "add", "path": "/e", "value": {"v": "e2"}} in ops and {"op": "replace", "path": "/f/v",
                                                                         "value": "F"} in ops
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "keyed"})
    p2 = tmp_path / "p2.json"
    s.export_patch(p2, diff=False)
    check_roundtrip(tmp_path, closing, doc, p2, kw={"format": "keyed"})


def test_rename_planner_on_random_renamings() -> None:
    """Every planned ``move`` targets a free key and the result is the renaming (paths and cycles)."""
    import random

    class Writer:
        def __init__(self) -> None:
            self.ops: list[tuple[str, str]] = []

        def op(self, op: str, path: Any, *, value: Any = None, raw: Any = None, from_: Any = None) -> None:
            assert op == "move"
            self.ops.append((from_[0], path[0]))

    ex = jsonpatch._Exporter.__new__(jsonpatch._Exporter)
    ex.layout = jsonpatch._Layout("keyed", None, True)
    for trial in range(500):
        rng = random.Random(trial)
        keys = [f"k{i}" for i in range(rng.randint(1, 10))]
        final = rng.sample(keys + [f"n{i}" for i in range(rng.randint(0, 3))], len(keys))
        w = Writer()
        ex._renames(w, [(o, f) for o, f in zip(keys, final) if o != f])   # type: ignore[arg-type]
        state = {k: k for k in keys}
        for a, b in w.ops:
            assert a in state and b not in state, (trial, w.ops)
            state[b] = state.pop(a)
        assert state == {f: o for o, f in zip(keys, final)}
        cycles = sum(1 for a, b in w.ops if b.startswith("jsonreshape-tmp-"))
        assert len(w.ops) == sum(o != f for o, f in zip(keys, final)) + cycles


@pytest.mark.parametrize("kind", ["wrapped", "wrapped_obj", "single", "keyed", "brace_list", "stream"])
def test_kinds_round_trip(tmp_path: Path, closing: list, kind: str) -> None:
    doc = open_doc(tmp_path / "src.json", kind, n=5, seed=3, style="pretty2")
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    if kind.startswith("wrapped"):
        s.set_value(Ref(None, ("meta", "v" if kind == "wrapped_obj" else "version")), '{"x": [1, 2.50]}')
        s.insert(Ref(None, ()), 0, "true", key="head")
        s.delete([Ref(None, ("tail",))])
    first = (0,) if kind == "single" else ()           # single documents: the records are the root's elements
    s.set_value(Ref(0, first + ("name",)), '"Ўзбек"')
    s.delete([Ref(0, first + ("tags", 0))])
    if kind != "single":
        s.duplicate([Ref(1, ())])
        s.delete([Ref(3, ())])
        s.move([Ref(4, ())], None, 0)
    kw = write_unique_doc(tmp_path / "unused.json", kind, n=5, seed=3, style="pretty2")
    p = tmp_path / "p.json"
    s.export_patch(p)
    check_roundtrip(tmp_path, closing, doc, p, kw=kw)
    p2 = tmp_path / "p2.json"
    warnings: list[str] = []
    jsonpatch.export_patch(s, p2, diff=False, warnings=warnings)
    if kind.startswith("wrapped"):
        assert warnings and "o'chirilgan" in warnings[0]
        check_roundtrip(tmp_path, closing, doc, p2, kw=kw, exact=False)   # the deleted "tail" is not represented
    else:
        assert warnings == []
        check_roundtrip(tmp_path, closing, doc, p2, kw=kw)


def test_wrapper_record_path_through_an_array(tmp_path: Path, closing: list) -> None:
    text = b'{"list": [{"a": 0}, {"data": [{"id": 1}, {"id": 2}]}, {"z": 9}, 4], "k": 1}'
    doc, s = open_values(tmp_path, closing, [], text=text, fmt="wrapped", record_path="/list/1/data")
    s.set_value(Ref(None, ("list", 0, "a")), "5")
    s.delete([Ref(None, ("list", 3))])
    s.insert(Ref(None, ("list",)), 3, '"x"')
    s.insert(Ref(None, ("list",)), 4, '"y"')
    s.insert(Ref(None, ("list", 1)), 1, "1", key="other")
    s.set_value(Ref(0, ("id",)), "10")
    p = tmp_path / "p.json"
    s.export_patch(p)
    assert load_patch(p) == [{"op": "replace", "path": "/list/1/data/0/id", "value": 10},   # records first
                             {"op": "replace", "path": "/list/0/a", "value": 5},          # before the container
                             {"op": "replace", "path": "/list/3", "value": "x"},          # the tail after it
                             {"op": "add", "path": "/list/4", "value": "y"},
                             {"op": "add", "path": "/list/1/other", "value": 1}]          # beside the container
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "wrapped", "record_path": "/list/1/data"})


# ----------------------------------------------------------------------------------
# diff=False and a source changed on disk
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl"])
def test_source_changed_on_disk(tmp_path: Path, closing: list, kind: str) -> None:
    doc = open_doc(tmp_path / "src.json", kind, n=6, seed=5)
    closing.append(doc)
    pristine = tmp_path / "pristine.json"
    shutil.copy(doc.source_path, pristine)
    s = EditSession.open(doc)
    closing.append(s)
    s.set_value(Ref(1, ("name",)), '"changed"')
    s.delete_records([2, 4])
    s.duplicate_records([0])
    s.insert(None, 0, '{"new": 1}', key="nk" if kind == "keyed" else None)
    if kind == "keyed":
        s.rename_key(Ref(3, ()), "renamed")
    final = doc_value(doc)
    with open(doc.source_path, "r+b") as f:              # another program rewrites the file
        f.seek(0)
        data = f.read()
        f.seek(0)
        f.write(data.replace(b'"id"', b'"ID"', 1))
    with pytest.raises(FileChangedError):
        s.export_patch(tmp_path / "p.json")
    p = tmp_path / "p.json"
    s.export_patch(p, diff=False)
    kw = write_unique_doc(tmp_path / "unused.json", kind, n=6, seed=5)
    d2 = Document.open(pristine, **kw)
    d2.build_index()
    closing.append(d2)
    s2 = EditSession.open(d2)
    closing.append(s2)
    s2.import_patch(p)
    assert canon(doc_value(d2)) == canon(final)


def test_keys_lost_by_history_trimming_and_big_deletes(tmp_path: Path, closing: list, monkeypatch) -> None:
    doc, s = open_values(tmp_path, closing, [(f"k{i}", i) for i in range(30)], fmt="keyed")
    s.rename_key(Ref(0, ()), "renamed")
    s.set_value(Ref(0, ()), "100")
    monkeypatch.setattr(editstore, "TRIM_BATCH", 1)
    s.store.maybe_trim(0)                                 # the rename's step is folded into the checkpoint
    assert s.store.checkpoint_seq >= 1
    monkeypatch.setattr(edits, "KEYS_MAX", 3)
    s.delete_records(range(10, 20))                       # more deleted records than KEYS_MAX: no keys stored
    assert s.store.step(s.store.head).keys is None
    warnings: list[str] = []
    jsonpatch.export_patch(s, tmp_path / "p.json", diff=False, warnings=warnings)
    assert len(warnings) == 2 and "10" in warnings[0]
    warnings.clear()
    jsonpatch.export_patch(s, tmp_path / "p2.json", warnings=warnings)   # diff=True reads the original keys: complete
    assert warnings == []
    check_roundtrip(tmp_path, closing, doc, tmp_path / "p2.json", kw={"format": "keyed"})


# ----------------------------------------------------------------------------------
# pending, stale and damaged layers
# ----------------------------------------------------------------------------------
def edit_and_leave(tmp_path: Path, closing: list, kind: str = "array") -> tuple[Document, Any, Path, dict]:
    doc = open_doc(tmp_path / "src.json", kind, n=6, seed=8)
    pristine = tmp_path / "pristine.json"
    shutil.copy(doc.source_path, pristine)
    s = EditSession.open(doc)
    s.set_value(Ref(0, ("name",)), '"pending"')
    s.duplicate_records([1, 2])
    s.delete_records([5])
    s.insert(None, 1, '{"inserted": [1.10]}', key="ins" if doc.member_records else None)
    final = doc_value(doc)
    s.close()                                             # the layer stays (recovery)
    doc.close()
    return final, None, pristine, write_unique_doc(tmp_path / "unused.json", kind, n=6, seed=8)


def apply_to(tmp_path: Path, closing: list, original: Path, patch: Path, kw: dict, name: str) -> Any:
    copy = tmp_path / name
    shutil.copy(original, copy)
    d = Document.open(copy, **kw)
    d.build_index()
    closing.append(d)
    s = EditSession.open(d)
    closing.append(s)
    s.import_patch(patch)
    return doc_value(d)


@pytest.mark.parametrize("kind", ["array", "keyed"])
def test_export_pending_layer(tmp_path: Path, closing: list, kind: str) -> None:
    final, _x, pristine, kw = edit_and_leave(tmp_path, closing, kind)
    doc = Document.open(tmp_path / "src.json", **kw)
    doc.build_index()
    closing.append(doc)
    (info,) = find_pending(doc)
    assert info.matches and info.steps == 4
    store = EditStore.open(doc, dir=info.dir, read_only=True, allow_stale=True)
    try:
        p = tmp_path / "p.json"
        n = export_layer(store, p, base_doc=doc)
        assert n > 0 and any(op["op"] == "copy" for op in load_patch(p)) == (kind == "array")
        assert canon(apply_to(tmp_path, closing, pristine, p, kw, "a.json")) == canon(final)
        n2 = export_layer(store, tmp_path / "p2.json")    # no document: diff=False semantics, copies unknown
        assert n2 > 0
    finally:
        store.close(close_reader=True)
    assert find_pending(doc)                              # exporting never deletes the layer


def test_export_stale_layer(tmp_path: Path, closing: list) -> None:
    final, _x, pristine, kw = edit_and_leave(tmp_path, closing, "array")
    src = tmp_path / "src.json"
    src.write_bytes(src.read_bytes()[:-1] + b',{"appended": true}]')     # the file changed after the crash
    doc = Document.open(src, **kw)
    doc.build_index()
    closing.append(doc)
    (info,) = find_pending(doc)
    assert not info.matches
    store = EditStore.open(doc, dir=info.dir, read_only=True, allow_stale=True)
    try:
        assert store.stale
        warnings: list[str] = []
        p = tmp_path / "p.json"
        export_layer(store, p, base_doc=doc, warnings=warnings)
        assert len(warnings) == 1 and "2" in warnings[0]          # the two copies of original records
        got = apply_to(tmp_path, closing, pristine, p, kw, "a.json")
        # the copies (right after their sources; every record is unique) are left out, the rest is exact
        want = [r for i, r in enumerate(final) if i == 0 or canon(r) != canon(final[i - 1])]
        assert len(want) == len(final) - 2 and canon(got) == canon(want)
    finally:
        store.close(close_reader=True)


def test_export_damaged_layer(tmp_path: Path, closing: list) -> None:
    final, _x, pristine, kw = edit_and_leave(tmp_path, closing, "jsonl")
    doc = Document.open(tmp_path / "src.json", **kw)
    doc.build_index()
    closing.append(doc)
    (info,) = find_pending(doc)
    seg = info.dir / "seg-00000.dat"
    seg.write_bytes(seg.read_bytes()[:-5])               # the last unit (the inserted record) is cut
    store = EditStore.open(doc, dir=info.dir, read_only=True, allow_stale=True)
    try:
        warnings: list[str] = []
        p = tmp_path / "p.json"
        export_layer(store, p, base_doc=doc, warnings=warnings)
        assert len(warnings) == 1 and "4" in warnings[0]
        got = apply_to(tmp_path, closing, pristine, p, kw, "a.jsonl")
        assert len(got) == len(final) - 1 and {"inserted": [codec.loads("1.10")]} not in got
    finally:
        store.close(close_reader=True)


# ----------------------------------------------------------------------------------
# atomic writing, progress, cancellation
# ----------------------------------------------------------------------------------
def test_atomic_write_progress_and_cancel(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, [{"id": i, "s": "x" * 100} for i in range(200)])
    for i in range(0, 200, 10):
        s.set_value(Ref(i, ("id",)), str(-i - 1))
    p = tmp_path / "p.json"
    p.write_bytes(b"old")
    prog = Progress()
    n = s.export_patch(p, progress=prog)
    assert n == 20 and prog.finished and prog.done > 0
    text = p.read_bytes()
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        s.export_patch(p, cancel=token)
    assert p.read_bytes() == text and not [q for q in tmp_path.iterdir() if q.name.endswith(".tmp")]
    s.close()
    with pytest.raises(EditError):
        s.export_patch(p)


def test_export_after_import_is_equivalent(tmp_path: Path, closing: list) -> None:
    doc, s = open_values(tmp_path, closing, [{"id": i} for i in range(5)])
    ops = [{"op": "move", "from": "/4", "path": "/0"}, {"op": "copy", "from": "/1", "path": "/-"},
           {"op": "replace", "path": "/2/id", "value": codec.loads("2.50")}, {"op": "remove", "path": "/3"}]
    s.import_patch(codec.dumps(ops))
    p = tmp_path / "p.json"
    s.export_patch(p)
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "array"})
    assert jsonpatch.export_patch(s, tmp_path / "p2.json") == s.export_patch(p)


def test_export_after_bulk_operations_and_big_deletes(tmp_path: Path, closing: list, monkeypatch) -> None:
    from jsonreshape.bulk import BulkSpec

    doc, s = open_values(tmp_path, closing, [{"id": i, "name": f"ali {i}"} for i in range(400)], fmt="jsonl")
    pv = s.bulk_preview(BulkSpec("replace", {"find": "ali", "replacement": "vali"}))
    s.bulk_apply(pv)                                          # an array layer over every record
    monkeypatch.setattr(edits, "DELS_RUNS_MAX", 2)
    s.delete_records(range(1, 400, 3))                        # scattered: a "pieces" remap
    s.duplicate_records(range(0, 50))
    s.insert(None, 3, '{"new": true}')
    p = tmp_path / "p.json"
    s.export_patch(p)
    ops = load_patch(p)
    assert sum(op["op"] == "remove" for op in ops) == 133 and sum(op["op"] == "copy" for op in ops) == 0
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "jsonl"})
    s.export_patch(p, diff=False)
    check_roundtrip(tmp_path, closing, doc, p, kw={"format": "jsonl"})
