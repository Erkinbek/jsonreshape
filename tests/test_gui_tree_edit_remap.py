"""The tree after edits (jsonreshape/gui/treeview.py ``_apply_change``; docs/ARCHITECTURE.md §12.9 "Tree").

Expanded nodes, the selection, the current node and the scroll position follow inserts / deletes / moves of
records and children, undo and redo -- also in filtered and sorted views, with node sorting and for every
document kind; edited nodes say "tahrirlangan" instead of a byte offset; changes made elsewhere (the controller
called directly, as the table does) reach the tree; a reset (discard) rebuilds it; the GUI thread never calls
Node methods nor reads more than 64 KiB while edits are applied.

The model check: every object of the documents carries a unique ``"u"`` member, so a node's identity is known
from the data. After each change the rows the tree shows are compared with the effective document (key /
index and kind of every visible row) and the expanded nodes must be exactly the expanded nodes from before
that still exist (by identity), wherever they moved.
"""

from __future__ import annotations

import dataclasses
import json
import random
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jsonreshape.gui import treeview  # noqa: E402
from jsonreshape.gui.session import FilterSpec, NodeRef  # noqa: E402
from jsonreshape.gui.treemodel import ROOT, WRAPPER, NodeInfo  # noqa: E402
from jsonreshape.gui.treeview import TreeView  # noqa: E402
from jsonreshape.reader import Document, Node  # noqa: E402
from test_gui_tree_edit import (  # noqa: E402,F401
    TIMEOUT,
    K,
    M,
    _no_modal_confirm,
    app,
    changed,
    expand,
    key,
    opened,
    select,
    values,
    views,
    wait_idle,
    wait_text,
    write,
)

pytestmark = pytest.mark.gui


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def doc_value(s, top) -> object:
    """The effective value of a top row: a record, or the root (wrapper / single)."""
    pinned = s.doc.pinned()
    if top in (WRAPPER, ROOT):
        return pinned.root().value()
    return pinned.record(top)


def child_of(value, index):
    if isinstance(value, dict):
        key = list(value)[index]
        return key, value[key]
    return index, value[index]


def node_at(s, kp):
    """``(key, value)`` of the node at a key path in the effective document."""
    value = doc_value(s, kp[0])
    key = None
    for c in kp[1:]:
        key, value = child_of(value, c)
    return key, value


def identity(s, kp) -> tuple:
    """A data identity of the node: its own ``u``, else the nearest object ancestor's ``u`` + the keys below."""
    value = doc_value(s, kp[0])
    trail: list = []
    best: tuple = (repr(kp[0]),) if kp[0] in (WRAPPER, ROOT) else ()
    if isinstance(value, dict) and "u" in value:
        best, trail = (value["u"],), []
    for c in kp[1:]:
        k, value = child_of(value, c)
        trail.append(k)
        if isinstance(value, dict) and "u" in value:
            best, trail = (value["u"],), []
    return best + tuple(trail)


def kind_of(value) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    return "number"


def settle(qtbot, view: TreeView) -> None:
    """Wait until the tree is idle (also a reveal of the node a step selected) and every row on screen is
    loaded."""
    qtbot.waitUntil(lambda: view._reveal_ref is None, timeout=TIMEOUT)
    wait_idle(qtbot, view)

    def loaded() -> bool:
        first, last = view.grid.visible_rows()
        return all(view.cell_text(r, "key") is not None for r in range(first, min(last + 1, view.row_count())))

    qtbot.waitUntil(loaded, timeout=TIMEOUT)
    wait_idle(qtbot, view)


def check_rows(qtbot, s, view: TreeView, *, member_records: bool = False) -> None:
    """Every loaded visible row agrees with the effective document."""
    settle(qtbot, view)
    first, last = view.grid.visible_rows()
    for row in range(first, min(last + 1, view.row_count())):
        kp = view.row_keypath(row)
        info = view.info_at(row)
        assert isinstance(info, NodeInfo), (row, kp)
        key, value = node_at(s, kp)
        assert info.keypath == kp
        assert info.kind == kind_of(value), (kp, info.kind, value)
        if len(kp) > 1:
            if isinstance(key, str):
                assert info.key == key, (kp, info.key, key)
            else:
                assert info.key == kp[-1] or info.key is None
        ref = view.ref_at(row)
        if kp[0] not in (WRAPPER, ROOT):
            assert ref.record == kp[0] and tuple(ref.ipath) == tuple(kp[1:])
            node = s.node(ref) if not isinstance(kp[0], str) else None
            if node is not None:
                assert node.kind == info.kind


def expanded_ids(s, view: TreeView) -> set:
    return {identity(s, kp) for kp in view.flat.expanded_keypaths()}


def container_ids(s) -> set:
    """Identities of every container of the effective document (records first level and below)."""
    out: set = set()
    pinned = s.doc.pinned()

    def walk(value, ident):
        if isinstance(value, dict) and "u" in value:
            ident = (value["u"],)
        if isinstance(value, (dict, list)):
            out.add(ident)
            items = value.items() if isinstance(value, dict) else enumerate(value)
            for k, v in items:
                walk(v, ident + (k,) if not (isinstance(v, dict) and "u" in v) else ())

    for i in range(pinned.record_count):
        walk(pinned.record(i), ())
    return out


def revealed_ids(s, view: TreeView) -> set:
    """The current node and its ancestors (a step's ``select`` is revealed: its ancestors get expanded)."""
    kp = view.current_keypath()
    if kp is None:
        return set()
    return {identity(s, kp[:d]) for d in range(1, len(kp) + 1)}


def command(qtbot, fn):
    """Run a controller command ``fn(on_done)`` and wait until it finished (a no-op step brings no
    ChangeSet); returns the ChangeSet or ``None``."""
    got: list = []
    assert fn(got.append) is not False
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


def no_select(monkeypatch) -> None:
    """Steps without ``select`` (the session applies them without selecting a node): the tree's own
    state is all that moves."""
    from jsonreshape.gui.session import DocSession

    real = DocSession._apply_change

    def apply(self, cs, remapped=None):
        return real(self, dataclasses.replace(cs, select=None), remapped)

    monkeypatch.setattr(DocSession, "_apply_change", apply)


def tree_doc(n: int = 12) -> list:
    out = []
    for i in range(n):
        out.append({"u": f"r{i}", "id": i,
                    "items": [{"u": f"r{i}i{j}", "v": j, "sub": {"u": f"r{i}i{j}s", "x": [j, j + 1]}}
                              for j in range(4)],
                    "o": {"u": f"r{i}o", "p": {"u": f"r{i}op", "q": "O'zbekiston"}, "z": [1, 2]}})
    return out


# ----------------------------------------------------------------------------------
# records and children move: expansion, selection, current node, undo / redo
# ----------------------------------------------------------------------------------
def test_expansion_follows_record_and_child_changes_with_undo_redo(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "t.json", tree_doc(12)))
    view = views(s)
    for kp in ((5,), (5, 2), (5, 2, 2), (8,), (8, 3), (8, 2), (8, 2, 0), (8, 2, 0, 2)):
        expand(qtbot, view, kp)
    before = expanded_ids(s, view)
    select(qtbot, view, (8, 2, 0, 1))
    cur_id = identity(s, (8, 2, 0, 1))
    check_rows(qtbot, s, view)
    # delete record 3: everything after moves up one place
    cs = changed(qtbot, s, lambda: s.edit.delete([NodeRef(3, (), ())]))
    assert cs.remap is not None
    assert expanded_ids(s, view) == before
    assert view.flat.is_expanded((4, 2, 2)) and view.flat.is_expanded((7, 2, 0, 2))
    # the step selects the deleted record's neighbour (cs.select)
    assert view.current_keypath() == (3,) and s.current == NodeRef(3, (), ())
    assert cur_id == identity(s, (7, 2, 0, 1))
    check_rows(qtbot, s, view)
    # delete the first element of items in (old) record 8: its expanded children shift down
    cs = changed(qtbot, s, lambda: s.edit.delete([NodeRef(7, ("items", 0), (2, 0))]))
    assert cs.nodes
    after = expanded_ids(s, view)
    assert after == {i for i in before if not i[0].startswith("r8i0")}
    check_rows(qtbot, s, view)
    # undo twice: back where it was
    changed(qtbot, s, s.edit.undo)
    assert expanded_ids(s, view) == after                      # the deleted node's expansion is gone for good
    changed(qtbot, s, s.edit.undo)
    assert len(values(s)) == 12
    assert {i for i in before if not i[0].startswith("r8i0")} <= expanded_ids(s, view)
    assert view.flat.is_expanded((5, 2, 2)) and view.flat.is_expanded((8,))
    check_rows(qtbot, s, view)
    changed(qtbot, s, s.edit.redo)
    assert view.flat.is_expanded((4, 2, 2)) and view.flat.is_expanded((7,))
    check_rows(qtbot, s, view)


def test_random_changes_keep_the_tree_consistent(qtbot, opened, views, tmp_path: Path) -> None:
    rng = random.Random(7)
    s = opened(write(tmp_path / "t.json", tree_doc(10)))
    view = views(s)
    view.resize(1000, 2000)                             # many rows on screen: more rows checked
    for kp in ((1,), (1, 2), (1, 2, 1), (4,), (4, 3), (6,), (6, 2), (6, 2, 3), (6, 2, 3, 2)):
        expand(qtbot, view, kp)
    edit = s.edit
    for step in range(40):
        settle(qtbot, view)
        before = expanded_ids(s, view)
        n = len(values(s))
        # (no duplicates here: copies would share the "u" identities)
        ops = ["del_child", "ins_child", "move_child", "move_records", "del_record", "undo", "redo"]
        op = rng.choice(ops)
        rec = rng.randrange(n)
        items = values(s)[rec]["items"]
        if op == "del_child" and items:
            j = rng.randrange(len(items))
            fn = lambda done: edit.delete([NodeRef(rec, ("items", j), (2, j))], on_done=done)  # noqa: E731
        elif op == "ins_child":
            j = rng.randint(0, len(items))
            uid = f"new{step}"
            fn = lambda done: edit.insert(NodeRef(rec, ("items",), (2,)), j, json.dumps({"u": uid}), on_done=done)  # noqa: E731
        elif op == "move_child" and len(items) >= 2:
            j = rng.randrange(len(items))
            to = rng.randint(0, len(items))
            fn = lambda done: edit.move([NodeRef(rec, ("items", j), (2, j))], NodeRef(rec, ("items",), (2,)), to,  # noqa: E731
                                        on_done=done)
        elif op == "move_records" and n >= 2:
            delta = rng.choice((-1, 1))
            fn = lambda done: edit.move_by([NodeRef(rec, (), ())], delta, on_done=done)  # noqa: E731
        elif op == "del_record" and n > 3:
            fn = lambda done: edit.delete([NodeRef(rec, (), ())], on_done=done)  # noqa: E731
        elif op == "undo" and edit.can_undo:
            fn = lambda done: edit.undo(on_done=done)  # noqa: E731
        elif op == "redo" and edit.can_redo:
            fn = lambda done: edit.redo(on_done=done)  # noqa: E731
        else:
            continue
        command(qtbot, fn)
        settle(qtbot, view)
        after = expanded_ids(s, view)
        # every expanded node that still exists is still expanded, wherever it moved ...
        assert before & container_ids(s) <= after, (step, op)
        # ... and only the ancestors of the node the step selected (revealed) were expanded in addition
        assert after <= before | revealed_ids(s, view), (step, op)
        check_rows(qtbot, s, view)


def test_scroll_position_is_anchored_on_the_top_node(qtbot, opened, views, tmp_path: Path, monkeypatch) -> None:
    s = opened(write(tmp_path / "t.json", [{"u": f"r{i}", "v": i} for i in range(300)]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 300, timeout=TIMEOUT)
    view.grid.scroll_to(150, "top")
    settle(qtbot, view)
    assert view.grid.top_row() == 150
    select(qtbot, view, (160,))
    # a step selecting a node on screen: no scrolling, the rows on screen stay where they were
    changed(qtbot, s, lambda: s.edit.set_value(NodeRef(155, ("v",), (1,)), "0"))
    settle(qtbot, view)
    assert view.grid.top_row() == 150 and view.current_keypath() == (155, 1)
    view.collapse_node((155,))
    # steps that do not select a node on screen (here: none at all) keep the top node in place
    no_select(monkeypatch)
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(0, (), ()), NodeRef(1, (), ())]))
    assert view.grid.top_row() == 148 and view.row_keypath(148) == (148,)
    assert doc_value(s, 148)["u"] == "r150"
    changed(qtbot, s, lambda: s.edit.insert(None, 0, '{"u": "n"}'))
    assert view.grid.top_row() == 149 and doc_value(s, 149)["u"] == "r150"
    # the anchor node itself deleted: the next one stays where it was on screen (one row below the top)
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(149, (), ())]))
    assert view.grid.top_row() == 148 and doc_value(s, 149)["u"] == "r151"
    check_rows(qtbot, s, view)


def test_selection_follows_and_is_dropped_with_deleted_nodes(qtbot, opened, views, tmp_path: Path,
                                                             monkeypatch) -> None:
    s = opened(write(tmp_path / "t.json", tree_doc(6)))
    view = views(s)
    expand(qtbot, view, (2,))
    expand(qtbot, view, (2, 2))
    view.select_keypaths([(2, 2, 1), (2, 2, 3), (4,)])
    ids = {identity(s, kp) for kp in view.selected_keypaths()}
    # a ChangeSet that selects nothing (e.g. a step made in another view of a hidden tree): the selection
    # follows its nodes
    no_select(monkeypatch)
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(0, (), ())]))
    assert {identity(s, kp) for kp in view.selected_keypaths()} == ids
    assert view.selected_keypaths() == [(1, 2, 1), (1, 2, 3), (3,)]
    rows = [view.row_of(kp) for kp in view.selected_keypaths()]
    assert all(view.grid.is_selected(r) for r in rows) and view.selection_count() == 3
    # a selected node deleted: it leaves the selection, the others stay
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(1, ("items", 1), (2, 1))]))
    assert view.selected_keypaths() == [(1, 2, 2), (3,)]
    assert view.selection_count() == 2


def test_selection_across_expand_and_collapse(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "t.json", tree_doc(4)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 4, timeout=TIMEOUT)
    settle(qtbot, view)
    view.select_keypaths([(1,), (3,)])
    expand(qtbot, view, (0,))                         # rows move: the same nodes stay selected
    assert view.selected_keypaths() == [(1,), (3,)]
    assert [r for r in range(view.row_count()) if view.grid.is_selected(r)] == [view.row_of((1,)), view.row_of((3,))]
    expand(qtbot, view, (1,))
    view.select_keypaths([(1,), (1, 2), (3,)])
    assert view.selected_keypaths() == [(1,), (3,)]  # (1, 2) is inside a selected node
    assert view.selection_count() == 3
    view.collapse_node((1,))                          # a hidden node leaves the selection
    assert view.selection_count() == 2
    # Ctrl+A stays "everything" while rows appear; a lone selected row does not
    view.grid.setFocus()
    QTest.keyClick(view.grid, Qt.Key.Key_A, M.ControlModifier)
    n = view.row_count()
    assert view.selection_count() == n
    expand(qtbot, view, (2,))
    assert view.row_count() > n and view.selection_count() == view.row_count()
    view.collapse_all()
    select(qtbot, view, (0,))
    s2 = opened(write(tmp_path / "one.json", tree_doc(1)))
    v2 = views(s2)
    qtbot.waitUntil(lambda: v2.row_count() == 1, timeout=TIMEOUT)
    select(qtbot, v2, (0,))                           # the only row: selected, but not "everything"
    expand(qtbot, v2, (0,))
    assert v2.selection_count() == 1


# ----------------------------------------------------------------------------------
# views, sorting, kinds
# ----------------------------------------------------------------------------------
def test_filtered_view_keeps_expansion_after_record_changes(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "t.json", tree_doc(12)))
    view = views(s)
    s.set_filter(FilterSpec("id >= 6"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 6, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.row_count() == 6, timeout=TIMEOUT)
    settle(qtbot, view)
    expand(qtbot, view, (8,))
    expand(qtbot, view, (8, 2))
    ident = identity(s, (8, 2))
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(1, (), ())]))          # outside the view
    qtbot.waitUntil(lambda: view.flat.is_expanded((7, 2)) and view.row_of((7, 2)) is not None, timeout=TIMEOUT)
    assert identity(s, (7, 2)) == ident and not view.flat.is_expanded((8,))
    assert [view.row_keypath(r)[0] for r in range(view.row_count()) if len(view.row_keypath(r)) == 1] == \
        [5, 6, 7, 8, 9, 10]
    check_rows(qtbot, s, view)
    # a node inside a record of the view: the record stays where it is
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(7, ("items", 0), (2, 0))]))
    assert view.flat.is_expanded((7, 2))
    check_rows(qtbot, s, view)


def test_node_sorting_is_computed_again_after_changes(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "t.json", [{"u": "a", "b": 1, "c": 2, "d": {"u": "d", "z": 1, "y": 2}}]))
    view = views(s)
    expand(qtbot, view, (0,))
    expand(qtbot, view, (0, 3))
    view.toggle_sort_keys(True)
    qtbot.waitUntil(lambda: view.flat.has_order((0,)) and view.flat.has_order((0, 3)), timeout=TIMEOUT)

    def shown(kp):
        n = view.flat.child_count(kp)
        row = view.row_of(kp)
        out = []
        r = row + 1
        while len(out) < n:
            wait_text(qtbot, view, r)
            if view.row_keypath(r)[:-1] == kp:
                out.append(view.cell_text(r, "key"))
            r += 1
        return out

    assert shown((0,)) == ["b", "c", "d", "u"]
    changed(qtbot, s, lambda: s.edit.rename_key(NodeRef(0, ("b",), (1,)), "zz"))   # no NodeChange: re-sorted
    qtbot.waitUntil(lambda: shown((0,)) == ["c", "d", "u", "zz"], timeout=TIMEOUT)
    changed(qtbot, s, lambda: s.edit.insert(NodeRef(0, (), ()), 0, "1", key="a"))   # a NodeChange
    qtbot.waitUntil(lambda: view.flat.has_order((0,)), timeout=TIMEOUT)
    assert shown((0,)) == ["a", "c", "d", "u", "zz"]
    assert view.flat.is_expanded((0, 4)) and shown((0, 4)) == ["u", "y", "z"]     # "d" moved one place
    check_rows(qtbot, s, view)


@pytest.mark.parametrize("kind", ["jsonl", "keyed", "wrapped", "single", "brace_list", "stream"])
def test_every_kind(qtbot, opened, views, tmp_path: Path, kind: str) -> None:
    recs = [{"u": f"r{i}", "id": i, "a": [{"u": f"r{i}a{j}", "v": j} for j in range(3)]} for i in range(4)]
    kw: dict = {}
    if kind == "jsonl":
        path = tmp_path / "d.jsonl"
        path.write_bytes(("\n".join(json.dumps(r) for r in recs) + "\n").encode())
    elif kind == "keyed":
        path = write(tmp_path / "d.json", {f"k{i}": r for i, r in enumerate(recs)})
        kw["format"] = "keyed"
    elif kind == "wrapped":
        path = write(tmp_path / "d.json", {"meta": {"u": "m", "v": 1}, "data": recs})
        kw.update(format="wrapped", record_path="data")
    elif kind == "single":
        path = write(tmp_path / "d.json", {"u": "root", "a": recs})
        kw["format"] = "single"
    elif kind == "brace_list":
        path = tmp_path / "d.json"
        path.write_bytes(("{" + ", ".join(json.dumps(r) for r in recs) + "}").encode())
    else:
        path = tmp_path / "d.json"
        path.write_bytes(("\n\n".join(json.dumps(r, indent=1) for r in recs)).encode())
        kw["format"] = "stream"
    s = opened(path, **kw)
    assert s.doc.kind == kind
    view = views(s)
    if kind == "single":
        top = ROOT
        qtbot.waitUntil(lambda: view.flat.is_expanded((ROOT,)), timeout=TIMEOUT)
        expand(qtbot, view, (ROOT, 1))
        base = (ROOT, 1, 2)                                    # recs[2] inside the root's array
        ref_rec, prefix, iprefix = None, ("a", 2), (1, 2)
    else:
        top = 2
        base = (2,)
        ref_rec, prefix, iprefix = 2, (), ()
        if kind == "wrapped":
            expand(qtbot, view, (WRAPPER,))
    expand(qtbot, view, base)
    expand(qtbot, view, base + (2,))
    expand(qtbot, view, base + (2, 2))
    check_rows(qtbot, s, view)
    # an inline edit through the keyboard
    select(qtbot, view, base + (1,))
    key(qtbot, view, K.Key_Return)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    ed.setText("99")
    changed(qtbot, s, lambda: QTest.keyClick(ed, K.Key_Return))
    wait_text(qtbot, view, view.row_of(base + (1,)), "value", "99")
    assert view.cell_text(view.row_of(base + (1,)), "offset") == treeview.t("gui.tree.edited")
    # delete an element before an expanded one: the expansion follows
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(ref_rec, prefix + ("a", 0), iprefix + (2, 0))]))
    assert view.flat.is_expanded(base + (2, 1)) and not view.flat.is_expanded(base + (2, 2))
    check_rows(qtbot, s, view)
    changed(qtbot, s, s.edit.undo)
    assert view.flat.is_expanded(base + (2, 2))
    check_rows(qtbot, s, view)
    if kind != "single":
        # a record deleted before the expanded one
        changed(qtbot, s, lambda: s.edit.delete([NodeRef(0, (), ())]))
        assert view.flat.is_expanded((1, 2, 2)) and not view.flat.is_expanded((2,))
        check_rows(qtbot, s, view)
        assert top == 2


@pytest.mark.parametrize("encoding", ["utf-8-bom-crlf", "utf-16-le", "gzip"])
def test_encodings(qtbot, opened, views, tmp_path: Path, encoding: str) -> None:
    import gzip

    recs = [{"u": f"r{i}", "name": f"Ўзбек {i}", "a": [i, i + 1]} for i in range(3)]
    text = json.dumps(recs, ensure_ascii=False, indent=2).replace("\n", "\r\n")
    path = tmp_path / ("d.json.gz" if encoding == "gzip" else "d.json")
    if encoding == "utf-8-bom-crlf":
        path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    elif encoding == "utf-16-le":
        path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    else:
        path.write_bytes(gzip.compress(text.encode("utf-8")))
    s = opened(path)
    view = views(s)
    expand(qtbot, view, (1,))
    expand(qtbot, view, (1, 2))
    select(qtbot, view, (1, 1))
    key(qtbot, view, K.Key_Return)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    assert ed.text() == "Ўзбек 1"
    ed.setText("O'zbek 1")
    changed(qtbot, s, lambda: QTest.keyClick(ed, K.Key_Return))
    assert values(s)[1]["name"] == "O'zbek 1"
    assert b'\r\n    "name": "O\'zbek 1",\r\n' in s.doc.pinned().record_bytes(1)   # CRLF style kept
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(0, (), ())]))
    assert view.flat.is_expanded((0, 2))
    check_rows(qtbot, s, view)


# ----------------------------------------------------------------------------------
# misc
# ----------------------------------------------------------------------------------
def test_edited_nodes_show_tahrirlangan(qtbot, opened, views, tmp_path: Path, lang_uz) -> None:
    s = opened(write(tmp_path / "t.json", tree_doc(3)))
    view = views(s)
    view.grid.set_column_visible(4, True)                     # the offset column
    expand(qtbot, view, (0,))
    expand(qtbot, view, (1,))
    row = view.row_of((1, 1))
    wait_text(qtbot, view, row, "offset")
    assert view.cell_text(row, "offset").isdigit()
    changed(qtbot, s, lambda: s.edit.set_value(NodeRef(0, ("id",), (1,)), "5"))
    for kp in ((0,), (0, 1), (0, 2)):
        r = view.row_of(kp)
        wait_text(qtbot, view, r, "offset", "tahrirlangan")
        tip = view.cell_at(r, "size").tooltip
        assert "Tahrirlangan" in tip
    assert view.cell_text(view.row_of((1, 1)), "offset").isdigit()   # another record: still in the file


def test_changes_made_elsewhere_reach_the_tree(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "t.json", tree_doc(4)))
    view = views(s)
    expand(qtbot, view, (1,))
    row = view.row_of((1, 1))
    wait_text(qtbot, view, row, "value", "1")
    changed(qtbot, s, lambda: s.edit.set_value(NodeRef(1, ("id",), (1,)), '"bir"'))
    wait_text(qtbot, view, view.row_of((1, 1)), "value", '"bir"')
    assert view.flat.is_expanded((1,))


def test_discard_resets_the_tree(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "t.json", tree_doc(4)))
    view = views(s)
    expand(qtbot, view, (1,))
    changed(qtbot, s, lambda: s.edit.delete([NodeRef(0, (), ())]))
    assert view.flat.is_expanded((0,))
    cs = changed(qtbot, s, lambda: s.edit.discard(view))
    assert cs.reset
    qtbot.waitUntil(lambda: view.row_count() == 4, timeout=TIMEOUT)
    assert not view.flat.expanded_keypaths()
    check_rows(qtbot, s, view)


def test_quick_find_is_run_again_after_an_edit(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "t.json", tree_doc(3)))
    view = views(s)
    expand(qtbot, view, (1,))
    expand(qtbot, view, (1, 3))
    view.find_bar.open_bar("Toshkent")
    qtbot.waitUntil(lambda: view._qf_task is None, timeout=TIMEOUT)
    assert not view.quick_find_matches()
    changed(qtbot, s, lambda: s.edit.set_value(NodeRef(1, ("o", "p", "q"), (3, 1, 1)), '"Toshkent"'))
    qtbot.waitUntil(lambda: (1, 3, 1) in view.quick_find_matches() or any(
        kp[:2] == (1, 3) for kp in view.quick_find_matches()), timeout=TIMEOUT)


def test_no_node_calls_or_big_reads_on_the_gui_thread(qtbot, app, opened, views, tmp_path: Path,
                                                       monkeypatch) -> None:
    big = [{"u": f"r{i}", "id": i, "blob": "x" * 70_000, "a": list(range(50))} for i in range(4)]
    s = opened(write(tmp_path / "t.json", big))
    main = threading.main_thread()
    on_gui: list[str] = []

    def guard(name, fn):
        def wrapped(*args, **kwargs):
            if threading.current_thread() is main:
                on_gui.append(name)
            return fn(*args, **kwargs)
        return wrapped

    for name in ("child_count", "children", "child", "find_child", "find_child_index", "value", "scalar_text",
                 "raw"):
        monkeypatch.setattr(Node, name, guard(name, getattr(Node, name)))
    for name in ("record_node", "root", "node", "records_node", "node_chunks"):
        monkeypatch.setattr(Document, name, guard(name, getattr(Document, name)))
    real_read = Document._read

    def _read(self, start, end, *a, **kw):
        if threading.current_thread() is main and end - start > 64 * 1024:
            on_gui.append(f"_read {end - start}")
        return real_read(self, start, end, *a, **kw)

    monkeypatch.setattr(Document, "_read", _read)
    view = views(s)
    expand(qtbot, view, (1,))
    expand(qtbot, view, (1, 3))
    select(qtbot, view, (1, 1))
    key(qtbot, view, K.Key_Return)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    ed.setText("7")
    changed(qtbot, s, lambda: QTest.keyClick(ed, K.Key_Return))
    view.select_keypaths([(1, 3, 0), (1, 3, 5)])
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Delete))
    select(qtbot, view, (2,))
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Up, M.AltModifier))
    changed(qtbot, s, s.edit.undo)
    settle(qtbot, view)
    assert on_gui == []
