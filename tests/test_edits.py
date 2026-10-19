"""jsonreshape.edits: EditSession rules beyond the per-kind scenario (§12.5; the scenario of every command on every
kind is ``tests/test_edits_kinds.py``).

Generations (``expect_gen`` -> ``edits.error.stale_refs``), ``auto_apply``, closed sessions, labels/history/dirty,
record-key rules and free keys, every wrapper protection rule (also a record path through an ancestor array), huge
records / units / wrappers, the item limit, big ``delete_records`` / ``duplicate_records`` (array layers, "pieces"
remaps, progress, cancellation without leftovers), discard (and the reader dropping child caches of the previous
layer), close / restore / ``restore=False`` / ``upto`` after a damaged layer, locks (in-process, given locks),
open preconditions, the bulk / patch / save delegation (fakes: the preview runs without the command lock), threads,
history trimming, duplicate member keys addressed by ipath, CRLF / BOM / UTF-16 / gzip sources. Commands run through
``edits_helpers.Harness`` (model + ChangeSet + exact-byte undo/redo checks).
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from edits_helpers import Harness, Model, N, effective, open_doc, parse, ref
from jsonreshape import editops, edits, editstore, reader
from jsonreshape.edits import EditSession, Ref
from jsonreshape.editstore import LayerLock, find_pending
from jsonreshape.errors import Cancelled, EditError
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)


@pytest.fixture
def closing():
    """Objects (sessions, documents) closed at the end of the test, newest first."""
    objs: list[Any] = []
    yield objs
    for obj in reversed(objs):
        try:
            obj.close()
        except Exception:
            pass


def make(tmp_path: Path, closing: list, kind: str, *, n: int = 6, seed: int = 0, style: str = "compact",
         settings: Settings | None = None, name: str = "src.json", **kw: Any) -> tuple[Document, EditSession]:
    doc = open_doc(tmp_path / name, kind, n=n, seed=seed, style=style, settings=settings)
    closing.append(doc)
    s = EditSession.open(doc, **kw)
    closing.append(s)
    return doc, s


# ----------------------------------------------------------------------------------
# generations, closing, no-ops
# ----------------------------------------------------------------------------------
def test_expect_gen_and_stale_refs(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "array")
    g0 = s.state.gen
    assert doc.gen == g0
    cs = s.set_value(Ref(0, ("id",)), "10", expect_gen=g0)
    assert cs.gen == s.state.gen != g0
    before = effective(doc)
    for call in (lambda: s.set_value(Ref(0, ("id",)), "11", expect_gen=g0),
                 lambda: s.delete([Ref(0, ())], expect_gen=g0), lambda: s.insert(None, 0, "1", expect_gen=g0),
                 lambda: s.move_by([Ref(1, ())], 1, expect_gen=g0), lambda: s.delete_records([0], expect_gen=g0),
                 lambda: s.change_type([Ref(0, ("id",))], "string", expect_gen=g0)):
        with pytest.raises(EditError) as exc:
            call()
        assert exc.value.key == "edits.error.stale_refs"
    assert effective(doc) == before
    u = s.undo()
    assert u.gen != cs.gen and doc.gen == u.gen
    s.set_value(Ref(0, ("id",)), "12", expect_gen=u.gen)


def test_auto_apply_off_leaves_the_document_alone(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "jsonl", auto_apply=False)
    assert doc.overlay is None and s.auto_apply is False
    before = effective(doc)
    cs = s.set_value(Ref(0, ("name",)), '"GUI"')
    assert effective(doc) == before and doc.overlay is None
    doc.set_overlay(cs.state)
    assert Model.of(doc).records[0].pairs[1][1] == "GUI"


def test_closed_session_refuses_commands(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "array")
    s.set_value(Ref(0, ("id",)), "5")
    s.close()
    s.close()
    assert s.closed and not s.can_undo and not s.can_redo and s.undo_label == "" and s.history() == []
    for call in (lambda: s.set_value(Ref(0, ("id",)), "6"), lambda: s.undo(), lambda: s.redo(), lambda: s.discard(),
                 lambda: s.delete_records([0])):
        with pytest.raises(EditError) as exc:
            call()
        assert exc.value.key == "edits.error.closed"


def test_labels_history_and_dirty(tmp_path, closing, lang_en) -> None:
    doc, s = make(tmp_path, closing, "array")
    assert not s.dirty and s.undo_label == "" and s.redo_label == "" and s.history() == []
    s.set_value(Ref(0, ("id",)), "5")
    s.delete([Ref(1, ()), Ref(2, ())])
    assert s.dirty and s.undo_label == "Records deleted: 2"
    s.undo()
    assert s.undo_label == "Value changed" and s.redo_label == "Records deleted: 2"
    assert [i.label for i in s.history()] == ["Value changed", "Records deleted: 2"]
    s.undo()
    assert not s.dirty and s.redo_label == "Value changed"
    s.insert(None, 0, "1")           # truncates the redo steps
    assert not s.can_redo and [i.label for i in s.history()] == ["Record inserted"]


# ----------------------------------------------------------------------------------
# key rules
# ----------------------------------------------------------------------------------
def test_member_keys_and_free_keys(tmp_path, closing) -> None:
    p = tmp_path / "k.json"
    p.write_bytes(b'{"a": {"x": 1, "x_2": 2, "y": [10, 20]}, "b": {"x": 3}, "a": {"z": 0}}')
    doc = Document.open(p, format="keyed")
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    h = Harness(doc, s)
    m = lambda: h.model   # noqa: E731
    # duplicates already in the file are left alone; new keys are free
    h.run(lambda s: s.duplicate([ref(m(), 0, "x")]), lambda md: md.duplicate([ref(md, 0, "x")]))
    assert m().records[0][1].keys() == ["x", "x_3", "x_2", "y"]
    h.run(lambda s: s.duplicate([ref(m(), 0)]), lambda md: md.duplicate([ref(md, 0)]))
    assert [k for k, _v in m().records] == ["a", "a_2", "b", "a"]
    h.run(lambda s: s.move([ref(m(), 2, "x")], ref(m(), 0), 0), lambda md: md.move([ref(md, 2, "x")], ref(md, 0), 0))
    assert m().records[0][1].keys()[:2] == ["x_4", "x"]
    h.run(lambda s: s.move([ref(m(), 0, "y", 1)], ref(m(), 0), None),
          lambda md: md.move([ref(md, 0, "y", 1)], ref(md, 0), None))
    assert m().records[0][1].keys()[-1] == "1"
    h.run(lambda s: s.move([ref(m(), 0, "x_4")], None, 0), lambda md: md.move([ref(md, 0, "x_4")], None, 0))
    assert m().records[0][0] == "x_4"
    h.run(lambda s: s.move([ref(m(), 2, "x")], None, None), lambda md: md.move([ref(md, 2, "x")], None, None))
    assert m().records[-1][0] == "x"
    h.run(lambda s: s.paste(None, "0"), lambda md: md.insert_records(None, [(N("0"), None)]))
    h.run(lambda s: s.paste(None, "1"), lambda md: md.insert_records(None, [(N("1"), None)]))
    assert [k for k, _v in m().records[-2:]] == ["pasted", "pasted_2"]
    h.fails("edits.error.key_exists", lambda s: s.rename_key(ref(m(), 1), "b"))
    assert [k for k, _v in m().records] == ["x_4", "a", "a_2", "b", "a", "x", "pasted", "pasted_2"]
    six = ref(m(), 6)
    h.run(lambda s: s.rename_key(six, "c"), lambda md: md.rename(six, "c"), targets=(six,))
    assert s.key_index.find("c") == 6 and s.key_index.find("a") == 4 and s.key_index.find("pasted") is None
    assert doc.node(("a",)).start == doc.record_node(4).start
    h.finish()


# ----------------------------------------------------------------------------------
# the wrapper (§12.5 protection rules)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("member", [False, True])
def test_wrapper_container_is_locked(tmp_path, closing, member) -> None:
    doc, s = make(tmp_path, closing, "wrapped_obj" if member else "wrapped")
    h = Harness(doc, s)
    locked = "edits.error.container_locked"
    rp = ("data",)
    h.fails(locked, lambda s: s.set_value(Ref(None, ()), "{}"))
    h.fails(locked, lambda s: s.set_value(Ref(None, rp), "[]"))
    h.fails(locked, lambda s: s.change_type([Ref(None, rp)], "string"))
    h.fails(locked, lambda s: s.rename_key(Ref(None, rp), "d"))
    h.fails(locked, lambda s: s.delete([Ref(None, rp)]))
    h.fails(locked, lambda s: s.delete([Ref(None, ())]))
    h.fails(locked, lambda s: s.duplicate([Ref(None, rp)]))
    h.fails(locked, lambda s: s.move([Ref(None, rp)], Ref(None, ("meta",)), 0))
    h.fails(locked, lambda s: s.move([Ref(None, ())], None, 0))
    h.fails(locked, lambda s: s.insert(Ref(None, rp), 0, "1", key="k"))
    h.fails(locked, lambda s: s.paste(Ref(None, rp), "1"))
    h.fails(locked, lambda s: s.set_value(Ref(None, rp + (0,)), "1"))
    h.fails(locked, lambda s: s.set_path(Ref(None, rp + ("x", "y")), "1"))
    h.fails(locked, lambda s: s.move([Ref(0, ("id",))], Ref(None, rp), 0))
    h.fails("edits.error.key_exists", lambda s: s.insert(Ref(None, ()), 0, "1", key="data"))
    h.fails("edits.error.key_exists", lambda s: s.rename_key(Ref(None, ("meta",)), "data"))
    # allowed: members of the wrapper around the container
    h.run(lambda s: s.set_path(Ref(None, ("meta", "new", "deep")), "[1]"),
          lambda md: md.set_path(None, ("meta", "new", "deep"), [N("1")]))
    vref = ref(h.model, None, "meta", h.model.wrapper.pairs[0][1].keys()[0])
    h.run(lambda s: s.move([vref], None, 0), lambda md: md.move([vref], None, 0))
    tail = ref(h.model, None, "tail")
    rec0 = ref(h.model, 0)
    h.run(lambda s: s.move([rec0], tail, 0), lambda md: md.move([rec0], tail, 0))
    h.finish()


def test_wrapper_record_path_through_an_array(tmp_path, closing) -> None:
    p = tmp_path / "w.json"
    p.write_bytes(b'{"meta": 1, "a": [[{"id": 0}, {"id": 1}], {"keep": true}, 7], "z": "end"}')
    doc = Document.open(p, format="wrapped", record_path=("a", 0))
    doc.build_index()
    closing.append(doc)
    assert doc.record_count == 2 and doc.record_path == ("a", 0)
    s = EditSession.open(doc)
    closing.append(s)
    h = Harness(doc, s)
    locked = "edits.error.container_locked"
    h.fails(locked, lambda s: s.insert(Ref(None, ("a",)), 0, "1"))           # would shift the container
    h.fails(locked, lambda s: s.move([Ref(None, ("a", 2))], Ref(None, ("a",)), 0))
    h.fails(locked, lambda s: s.move_by([Ref(None, ("a", 1))], -1))
    h.fails(locked, lambda s: s.set_value(Ref(None, ("a",)), "[]"))
    h.run(lambda s: s.insert(Ref(None, ("a",)), 1, '"ok"'), lambda md: md.insert(ref(md, None, "a"), 1, "ok", None))
    h.run(lambda s: s.delete([Ref(None, ("a", 3))]), lambda md: md.delete([ref(md, None, "a", 3)]))
    h.run(lambda s: s.move_by([Ref(None, ("a", 1))], 1),
          lambda md: md.move([ref(md, None, "a", 1)], ref(md, None, "a"), 3))
    h.run(lambda s: s.set_value(Ref(None, ("a", 1, "keep")), "false"),
          lambda md: md.set_value(ref(md, None, "a", 1, "keep"), False))
    h.run(lambda s: s.delete([Ref(0, ())]), lambda md: md.delete([ref(md, 0)]))
    h.finish()


# ----------------------------------------------------------------------------------
# limits: huge records, too many items, single documents
# ----------------------------------------------------------------------------------
def test_huge_records_and_units(tmp_path, closing) -> None:
    p = tmp_path / "h.json"
    big = '"' + "x" * 3000 + '"'
    p.write_text(f'[{{"a": 1}}, {{"big": {big}}}, {{"c": 3}}]', encoding="utf-8")
    doc = Document.open(p, settings=Settings(large_node_bytes=2000))
    doc.build_index()
    closing.append(doc)
    size = doc.record_span(1)[1] - doc.record_span(1)[0]
    s = EditSession.open(doc)
    closing.append(s)
    with pytest.raises(EditError) as exc:
        s.set_value(Ref(1, ("big",)), "1")
    assert exc.value.key == "edits.error.huge_record" and exc.value.params["limit"] == "2.0 KB"
    with pytest.raises(EditError) as exc:
        s.set_value(Ref(0, ("a",)), '"' + "y" * 2500 + '"')       # the unit would be huge
    assert exc.value.key == "edits.error.huge_record"
    # record-level structure works on huge records (their bytes are not read)
    s.delete([Ref(1, ())])
    s.undo()
    s.move([Ref(1, ())], None, 0)
    s.duplicate_records([0])
    assert [doc.record_span(i)[1] - doc.record_span(i)[0] for i in range(2)] == [size, size]
    assert doc.is_huge(0) and doc.is_huge(1) and doc.record_count == 4
    with pytest.raises(EditError) as exc:
        s.move([Ref(0, ())], Ref(2, ()), 0)                # a whole huge record into a node: its bytes are needed
    assert exc.value.key == "edits.error.huge_record"


def test_huge_single_root(tmp_path, closing) -> None:
    p = tmp_path / "s.json"
    p.write_text('{"a": "' + "x" * 3000 + '"}', encoding="utf-8")
    doc = Document.open(p, format="single", settings=Settings(large_node_bytes=1000))
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    for call in (lambda: s.set_value(Ref(None, ("a",)), "1"), lambda: s.insert(Ref(0, ()), 0, "1", key="b")):
        with pytest.raises(EditError) as exc:
            call()
        assert exc.value.key == "edits.error.huge_record"


def test_too_many_items(tmp_path, closing, monkeypatch) -> None:
    doc, s = make(tmp_path, closing, "array", n=8)
    monkeypatch.setattr(edits, "MAX_ITEMS", 3)
    with pytest.raises(EditError) as exc:
        s.delete([Ref(i, ()) for i in range(4)])
    assert exc.value.key == "edits.error.too_many" and exc.value.params == {"count": 4, "limit": 3}
    with pytest.raises(EditError) as exc:
        s.paste(None, "[1, 2, 3, 4]", as_records=True)
    assert exc.value.key == "edits.error.too_many"
    s.delete([Ref(i, ()) for i in range(3)])
    assert doc.record_count == 5
    s.delete_records(range(4))       # the task path has no limit
    assert doc.record_count == 1


# ----------------------------------------------------------------------------------
# big record-level commands (array layers, "pieces" remaps)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl", "wrapped_obj"])
def test_big_delete_and_duplicate_records(tmp_path, closing, monkeypatch, backend, kind) -> None:
    doc, s = make(tmp_path, closing, kind, n=40, seed=5)
    monkeypatch.setattr(edits, "MAX_ITEMS", 6)
    monkeypatch.setattr(edits, "DELS_RUNS_MAX", 3)
    h = Harness(doc, s)
    h.run(lambda s: s.set_value(Ref(7, ("name",)), '"edited first"'),
          lambda md: md.set_value(ref(md, 7, "name"), "edited first"), targets=(ref(h.model, 7, "name"),))
    every3 = list(range(0, 40, 3))
    h.run(lambda s: s.duplicate_records(list(reversed(every3))),            # unsorted input, array layer
          lambda md: md.duplicate([ref(md, p) for p in every3]))
    assert s.state.layers[-2].count == len(every3)
    st = s.store.step(s.store.head)
    assert st.push_layer is not None and st.struct[0][0] == "inss"
    rs = RecordSet.from_indices(range(1, 50, 2), gen=s.state.gen)
    h.run(lambda s: s.delete_records(rs), lambda md: md.delete([ref(md, p) for p in range(1, 50, 2)]))
    st = s.store.step(s.store.head)
    assert st.struct[0][0] == "pieces"
    if s.doc.member_records:
        assert st.keys is not None and len(st.keys) == 25
    h.run(lambda s: s.delete_records(range(3, 9)), lambda md: md.delete([ref(md, p) for p in range(3, 9)]))
    assert s.store.step(s.store.head).struct[0][0] == "del"
    h.run(lambda s: s.delete_records(RecordSet.base(doc)),
          lambda md: md.delete([ref(md, p) for p in range(len(md.records))]))
    assert doc.record_count == 0
    h.finish()


def test_progress_and_cancel_of_record_tasks(tmp_path, closing, monkeypatch) -> None:
    doc, s = make(tmp_path, closing, "keyed", n=30)
    monkeypatch.setattr(edits, "MAX_ITEMS", 4)
    seen: list[int] = []
    prog = Progress(callback=lambda info: seen.append(info.done), min_interval=0)
    s.duplicate_records(range(30), progress=prog)
    assert seen and doc.record_count == 60 and s.store.head == 1
    files = sorted(p.name for p in s.store.dir.iterdir())
    tok = CancelToken()
    before = effective(doc)
    with pytest.raises(Cancelled):
        s.duplicate_records(range(60), progress=Progress(callback=lambda info: tok.cancel(), min_interval=0),
                            cancel=tok)
    assert effective(doc) == before and s.store.head == 1
    assert sorted(p.name for p in s.store.dir.iterdir()) == files      # nothing left of the cancelled command


# ----------------------------------------------------------------------------------
# discard, close, restore, locks
# ----------------------------------------------------------------------------------
def test_discard_resets_and_a_new_layer_follows(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "array")
    original = effective(doc)
    s.set_value(Ref(0, ()), '{"edited": [1, 2, 3]}')
    node = doc.node((0, "edited"))
    assert node.is_edited and node.child_count() == 3            # cached children of an edit offset
    layer = s.store.dir
    cs = s.discard()
    assert cs.kind == "discard" and cs.reset and cs.records is None and cs.remap is None
    assert effective(doc) == original and not s.dirty and s.store is None and not layer.exists()
    # a new layer in the same folder never reuses the old layer's segment names / offsets (§12.0; a reader of an
    # old pinned state must never read the new bytes -- review CL-1), and the document serves the new child list
    s.set_value(Ref(0, ()), '{"edited": [9]}')
    node2 = doc.node((0, "edited"))
    assert node2.start != node.start and node2.child_count() == 1 and node2.child(0).value() == 9
    assert s.store.dir == layer and s.store.point_segment != 0
    assert s.store.dir.exists() and s.can_undo


def test_set_overlay_drops_child_caches_of_another_layer(tmp_path, closing) -> None:
    from overlay_helpers import OverlayBuilder

    doc = open_doc(tmp_path / "a.json", "array")
    closing.append(doc)
    a = OverlayBuilder(doc, tmp_path / "layer-a")
    a.set(1, b'{"k": [1, 2, 3, 4]}')
    doc.set_overlay(a.state())
    node = doc.node((1, "k"))
    assert node.child_count() == 4
    b = OverlayBuilder(doc, tmp_path / "layer-b")             # the same segment offsets, other bytes
    b.set(1, b'{"k": [5, 6]}')
    doc.set_overlay(b.state())
    node2 = doc.node((1, "k"))
    assert node2.start == node.start and node2.child_count() == 2 and [c.value() for c in node2.children()] == [5, 6]
    doc.set_overlay(b.state())                                # the same reader: caches stay
    assert doc.node((1, "k")).child_count() == 2


def test_close_keeps_the_layer_and_open_restores_it(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "keyed")
    s.set_value(Ref(0, ("name",)), '"restored"')
    s.insert(None, 0, '{"new": true}', key="yangi")
    s.undo()
    edited = Model.of(doc)
    s.close()
    pend = find_pending(doc)
    assert len(pend) == 1 and pend[0].steps == 1 and pend[0].matches
    with pytest.raises(EditError) as exc:
        EditSession.open(doc, restore=False)
    assert exc.value.key == "edits.error.pending"
    s2 = EditSession.open(doc)
    closing.append(s2)
    assert s2.dirty and s2.state.version == 1 and Model.of(doc) == edited
    assert s2.can_redo and s2.redo_label and s2.undo_label
    s2.redo()
    assert Model.of(doc).records[0][0] == "yangi"
    s2.undo()
    s2.undo()
    assert not s2.dirty
    s2.close()
    assert find_pending(doc) == []        # a head-0 layer is not offered


def test_second_session_is_locked(tmp_path, closing, monkeypatch) -> None:
    monkeypatch.setattr(editstore, "LOCK_RETRY", 0.2)
    doc, s = make(tmp_path, closing, "array")
    with pytest.raises(EditError) as exc:
        EditSession.open(doc)
    assert exc.value.key == "edits.error.locked"
    other = Document.open(doc.source_path)
    other.build_index()
    closing.append(other)
    with pytest.raises(EditError) as exc:
        EditSession.open(other)
    assert exc.value.key == "edits.error.locked"
    s.close()
    s2 = EditSession.open(other)
    closing.append(s2)
    s2.close()


def test_given_lock_is_owned_by_the_session(tmp_path, closing) -> None:
    doc = open_doc(tmp_path / "a.json", "array")
    closing.append(doc)
    lock = LayerLock.for_doc(doc)
    lock.acquire()
    s = EditSession.open(doc, lock=lock)
    assert lock.held
    s.set_value(Ref(0, ("id",)), "9")
    s.close()
    assert not lock.held
    other = LayerLock.for_doc(doc)
    with pytest.raises(ValueError):
        EditSession.open(doc, lock=other)            # not held
    lock2 = LayerLock.for_doc(doc)
    lock2.acquire()
    with pytest.raises(EditError):
        EditSession.open(doc, lock=lock2, restore=False)   # a pending layer: the given lock stays the caller's
    assert lock2.held
    lock2.release()


def test_open_preconditions(tmp_path, closing) -> None:
    p = tmp_path / "bad.json"
    p.write_bytes(b'[{"a": 1}, {"b": ]')
    doc = Document.open(p)
    closing.append(doc)
    with pytest.raises(EditError) as exc:
        EditSession.open(doc)
    assert exc.value.key == "editstore.error.not_indexed"
    doc.build_index()
    with pytest.raises(EditError) as exc:
        EditSession.open(doc)
    assert exc.value.key == "edits.error.partial"
    doc.close()
    with pytest.raises(EditError) as exc:
        EditSession.open(doc)
    assert exc.value.key == "edits.error.closed"


def test_restore_up_to_the_last_good_step(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "array")
    s.set_value(Ref(0, ("id",)), "100")
    good = effective(doc)
    s.set_value(Ref(1, ("id",)), "101")
    s.set_value(Ref(2, ("id",)), "102")
    layer = s.store.dir
    s.close()
    seg = layer / "seg-00000.dat"
    data = seg.read_bytes()
    seg.write_bytes(data[:-5])                           # the unit of step 3 is cut
    with pytest.raises(editstore.LayerDamagedError) as exc:
        EditSession.open(doc)
    assert exc.value.last_good == 2
    s2 = EditSession.open(doc, upto=1)
    closing.append(s2)
    assert s2.state.version == 1 and s2.store.head == 1 and s2.can_redo and effective(doc) == good
    s2.set_value(Ref(3, ("id",)), "103")                 # truncates the damaged redo steps
    assert not s2.can_redo and s2.store.head == 2
    assert s2.undo() is not None and effective(doc) == good


# ----------------------------------------------------------------------------------
# delegations: bulk, patch, save
# ----------------------------------------------------------------------------------
class _FakePreview:
    def __init__(self, gen: int, step: Any) -> None:
        self.gen = gen
        self.step = step


def _install(monkeypatch, name: str, module: types.ModuleType) -> None:
    import jsonreshape

    monkeypatch.setitem(sys.modules, f"jsonreshape.{name}", module)
    monkeypatch.setattr(jsonreshape, name, module, raising=False)


def test_bulk_preview_does_not_hold_the_command_lock(tmp_path, closing, monkeypatch) -> None:
    doc, s = make(tmp_path, closing, "array")
    started = threading.Event()
    release = threading.Event()
    fake = types.ModuleType("jsonreshape.bulk")

    def preview(pinned, state, store, spec, scope, *, progress=None, cancel=None):
        assert pinned.overlay is state and store is s.store and spec == "spec"
        started.set()
        release.wait(10)
        span = store.append(b'{"bulk": 1}')
        step = editstore.Step("bulk", "edits.step.bulk.replace", {"count": 1}, content=[(0, state.top.get(0), span)],
                              records=[0])
        return _FakePreview(state.gen, step)

    fake.preview = preview
    fake.make_step = lambda pv: pv.step
    _install(monkeypatch, "bulk", fake)
    out: list[Any] = []
    th = threading.Thread(target=lambda: out.append(s.bulk_preview("spec")))
    th.start()
    assert started.wait(10)
    t0 = time.monotonic()
    s.set_value(Ref(1, ("id",)), "77")              # not blocked by the running preview
    assert time.monotonic() - t0 < 5
    release.set()
    th.join(10)
    with pytest.raises(EditError) as exc:
        s.bulk_apply(out[0])                         # computed on an older state
    assert exc.value.key == "edits.error.stale_refs"
    cs = s.bulk_apply(s.bulk_preview("spec"))
    assert cs.kind == "bulk" and cs.records == frozenset({0}) and doc.record_bytes(0) == b'{"bulk": 1}'
    assert s.undo().kind == "undo" and doc.record_bytes(0) != b'{"bulk": 1}'


def test_patch_and_save_delegation(tmp_path, closing, monkeypatch) -> None:
    doc, s = make(tmp_path, closing, "array")
    calls: list[Any] = []
    fake = types.ModuleType("jsonreshape.jsonpatch")

    def apply_patch(session, source, *, progress=None, cancel=None):
        calls.append(("apply", source))
        assert session is s and session.lock._is_owned()
        store = session.ensure_store()
        span = store.append(b'{"patched": true}')
        st = session.state
        step = editstore.Step("patch", "edits.step.patch", {"count": 1}, content=[(0, st.top.get(0), span)],
                              records=[0])
        return session.commit_step(step)

    def export_patch(session, path, *, diff=True, progress=None, cancel=None):
        calls.append(("export", path, diff))
        return 3

    fake.apply_patch = apply_patch
    fake.export_patch = export_patch
    _install(monkeypatch, "jsonpatch", fake)
    cs = s.import_patch("p.json")
    assert cs.kind == "patch" and doc.record_bytes(0) == b'{"patched": true}' and s.undo_label
    assert s.export_patch(tmp_path / "out.json", diff=False) == 3
    assert calls == [("apply", "p.json"), ("export", tmp_path / "out.json", False)]
    saved: list[Any] = []
    fake_saver = types.ModuleType("jsonreshape.saver")

    def save(d, target=None, *, backup=True, drop_broken=False, settings=None, progress=None, cancel=None, store=None):
        saved.append((d.overlay is s.state, target, backup, drop_broken, store is s.store))
        return "report"

    fake_saver.save = save
    _install(monkeypatch, "saver", fake_saver)
    layer = s.store.dir
    assert s.save(tmp_path / "copy.json", drop_broken=True) == "report"
    assert saved == [(True, tmp_path / "copy.json", True, True, True)]
    # the layer is gone for recovery at once; its segment files only once nothing can read them any more (the
    # document still shows the saved state here -- review CL-2): then they are deleted too
    assert s.closed and not (layer / editstore.DB_NAME).exists() and find_pending(doc) == []
    assert doc.record_bytes(0) == b'{"patched": true}'
    with pytest.raises(EditError):
        s.set_value(Ref(0, ()), "1")
    doc.close()                                    # (a caller reopens the saved file) its readers close
    editstore._sweep_retained()
    assert not layer.exists()


# ----------------------------------------------------------------------------------
# threads, trimming
# ----------------------------------------------------------------------------------
def test_commands_from_several_threads_are_serialized(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "jsonl", n=12)
    errors: list[BaseException] = []

    def work(k: int) -> None:
        try:
            for j in range(8):
                s.set_value(Ref(k, ("id",)), str(1000 * k + j + 1))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert not errors
    assert s.store.head == 32
    assert [Model.of(doc).records[k].pairs[0][1] for k in range(4)] == [N(str(1000 * k + 8)) for k in range(4)]
    while s.can_undo:
        s.undo()
    assert [Model.of(doc).records[k].pairs[0][1] for k in range(4)] == [N(str(k)) for k in range(4)]


def test_trim_keeps_undo_working(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "array")
    s.settings.undo_limit = 3            # below the documented minimum, to trim quickly (validation bypassed)
    for j in range(75):
        s.set_value(Ref(j % 6, ("id",)), str(j + 100))
    assert s.store.checkpoint_seq == 64 and s.store.head == 75
    n = 0
    while s.can_undo:
        s.undo()
        n += 1
    assert n == 11
    want = [max(j for j in range(64) if j % 6 == k) for k in range(6)]
    assert [Model.of(doc).records[k].pairs[0][1] for k in range(6)] == [N(str(j + 100)) for j in want]


# ----------------------------------------------------------------------------------
# misc
# ----------------------------------------------------------------------------------
def test_paths_with_canonical_index_strings_and_stale_ipaths(tmp_path, closing) -> None:
    doc, s = make(tmp_path, closing, "array")
    cs = s.set_value(Ref(1, ("tags", "0")), '"via text index"')
    assert cs.select == (1, ("tags", 0), (2, 0))
    with pytest.raises(EditError) as exc:
        s.set_value(Ref(1, ("tags", 0), (1, 0)), "1")         # the ipath names "name": a stale ref
    assert exc.value.key == "edits.error.path_not_found"
    with pytest.raises(EditError) as exc:
        s.set_value(Ref(99, ()), "1")
    assert exc.value.key == "edits.error.path_not_found"
    with pytest.raises(EditError) as exc:
        s.set_value(Ref(None, ()), "1")                        # no wrapper in an array document
    assert exc.value.key == "edits.error.path_not_found"


def test_flags_of_edited_units(tmp_path, closing) -> None:
    from jsonreshape.core import FLAG_ESCAPES, FLAG_NON_ASCII

    p = tmp_path / "plain.json"
    p.write_bytes(b'[{"a": "x"}, {"a": "y"}]')
    doc = Document.open(p)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    assert doc.flags == 0
    s.set_value(Ref(0, ("a",)), '"\\u0041 é"')
    assert doc.flags & FLAG_ESCAPES and doc.flags & FLAG_NON_ASCII
    assert s.store.step(1).flags == (0, s.state.flags)
    s.undo()
    assert doc.flags == 0


def test_no_record_documents(tmp_path, closing) -> None:
    p = tmp_path / "e.json"
    p.write_bytes(b"[]")
    doc = Document.open(p)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    s.insert(None, None, '{"first": 1}')
    s.paste(None, "[2, 3]", as_records=True)
    assert [doc.record(i) for i in range(3)] == [{"first": 1}, 2, 3]
    s.delete_records(range(3))
    assert doc.record_count == 0
    s.undo()
    assert doc.record_count == 3


# ----------------------------------------------------------------------------------
# duplicate keys, encodings and newlines, a huge wrapper
# ----------------------------------------------------------------------------------
def test_duplicate_member_keys_are_addressed_by_ipath(tmp_path, closing) -> None:
    p = tmp_path / "d.json"
    p.write_bytes(b'[{"a": 1, "b": {"x": 1}, "a": 2}, {"c": [1, 2]}]')
    doc = Document.open(p)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    h = Harness(doc, s)
    first = Ref(0, ("a",), (0,))
    h.run(lambda s: s.set_value(first, '"birinchi"'), lambda md: md.set_value(first, "birinchi"), targets=(first,))
    assert doc.record(0) == {"a": 2, "b": {"x": 1}}                     # dict semantics: the last "a" wins
    assert doc.record_bytes(0) == b'{"a": "birinchi", "b": {"x": 1}, "a": 2}'
    last = Ref(0, ("a",))
    cs = h.run(lambda s: s.set_value(last, "3"), lambda md: md.set_value(Ref(0, ("a",), (2,)), N("3")),
               targets=(Ref(0, ("a",), (2,)),))
    assert cs.select == (0, ("a",), (2,))
    h.run(lambda s: s.rename_key(first, "z"), lambda md: md.rename(first, "z"), targets=(first,))
    h.fails("edits.error.path_not_found", lambda s: s.delete([Ref(0, ("a",), (0,))]))     # it is "z" now
    h.run(lambda s: s.move_by([Ref(0, ("a",), (2,))], -2), lambda md: md.move([Ref(0, ("a",), (2,))], Ref(0, (), ()), 0))
    assert doc.record_bytes(0) == b'{"a": 3, "z": "birinchi", "b": {"x": 1}}'
    h.finish()


@pytest.mark.parametrize("variant", ["crlf", "bom", "utf16", "gzip", "utf16be_gzip"])
def test_encodings_and_newlines(tmp_path, closing, variant) -> None:
    import gzip
    import json as _json

    recs = [{"id": i, "ism": f"O‘zbek {i}", "list": [i, {"k": "v"}], "empty": {}} for i in range(4)]
    text = _json.dumps(recs, indent=2, ensure_ascii=False)
    if variant == "crlf":
        raw = text.replace("\n", "\r\n").encode("utf-8")
    elif variant == "bom":
        raw = b"\xef\xbb\xbf" + text.encode("utf-8")
    elif variant == "utf16":
        raw = "﻿".encode("utf-16-le") + text.encode("utf-16-le")
    elif variant == "gzip":
        raw = gzip.compress(text.encode("utf-8"))
    else:
        raw = gzip.compress("﻿".encode("utf-16-be") + text.encode("utf-16-be"))
    p = tmp_path / ("src.json.gz" if "gzip" in variant else "src.json")
    p.write_bytes(raw)
    doc = Document.open(p)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    h = Harness(doc, s)
    m = lambda: h.model   # noqa: E731
    h.run(lambda s: s.set_value(ref(m(), 1, "list"), '[{"deep": [1, 2]}, "ikki"]'),
          lambda md: md.set_value(ref(md, 1, "list"), parse('[{"deep": [1, 2]}, "ikki"]')), targets=(ref(m(), 1, "list"),))
    h.run(lambda s: s.insert(ref(m(), 2, "empty"), 0, '{"a": [1]}', key="yangi"),
          lambda md: md.insert(ref(md, 2, "empty"), 0, parse('{"a": [1]}'), "yangi"))
    h.run(lambda s: s.insert(None, 1, '{"new": {"x": [1, 2]}}'), lambda md: md.insert_records(1, [(parse('{"new": {"x": [1, 2]}}'), None)]))
    h.run(lambda s: s.move([ref(m(), 0, "list")], ref(m(), 3), 0), lambda md: md.move([ref(md, 0, "list")], ref(md, 3), 0))
    if variant == "crlf":
        for i in range(doc.record_count):
            data = doc.record_bytes(i)
            assert b"\n" not in data.replace(b"\r\n", b""), data          # no bare LF in a CRLF document
    else:
        assert all(b"\r" not in doc.record_bytes(i) for i in range(doc.record_count))
    assert s.style.unit == "  "
    h.finish()


def test_huge_wrapper_is_read_only_but_records_are_not(tmp_path, closing) -> None:
    p = tmp_path / "w.json"
    p.write_text('{"meta": "' + "m" * 3000 + '", "data": [{"a": 1}, {"a": 2}]}', encoding="utf-8")
    doc = Document.open(p, format="wrapped", record_path="data", settings=Settings(large_node_bytes=2000))
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    with pytest.raises(EditError) as exc:
        s.set_value(Ref(None, ("meta",)), '"short"')
    assert exc.value.key == "edits.error.huge_record"
    s.set_value(Ref(1, ("a",)), "20")
    s.insert(None, 0, '{"a": 0}')
    assert [doc.record(i)["a"] for i in range(3)] == [0, 1, 20]


@pytest.mark.slow
def test_big_record_tasks_performance(tmp_path, closing) -> None:
    import time as _time

    n = 300_000
    p = tmp_path / "big.json"
    p.write_bytes(b"[" + b",\n".join(b'{"id": %d, "v": "x%d"}' % (i, i) for i in range(n)) + b"]")
    doc = Document.open(p)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    t0 = _time.perf_counter()
    s.delete_records(range(0, n, 2))                     # 150 000 scattered records -> a "pieces" step
    t_del = _time.perf_counter() - t0
    assert doc.record_count == n // 2 and doc.record(0)["id"] == 1
    t0 = _time.perf_counter()
    s.duplicate_records(range(0, n // 2, 3))             # 50 000 copies -> an array layer of spans
    t_dup = _time.perf_counter() - t0
    assert doc.record_count == n // 2 + 50_000 and doc.record(1) == doc.record(0)
    t0 = _time.perf_counter()
    s.set_value(Ref(5, ("v",)), '"edited"')
    s.undo()
    s.undo()
    s.undo()
    t_undo = _time.perf_counter() - t0
    assert doc.record_count == n and doc.record(0)["id"] == 0
    print(f"\ndelete 150k: {t_del:.2f}s, duplicate 50k: {t_dup:.2f}s, edit + 3 undos: {t_undo:.2f}s")
    assert t_del < 30 and t_dup < 30
