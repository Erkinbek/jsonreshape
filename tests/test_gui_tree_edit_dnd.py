"""Drag & drop in the tree (jsonreshape/gui/treeview.py + vgrid.py; docs/ARCHITECTURE.md §12.9 "Selection &
drag").

Real mouse gestures on the tree's grid (QTest press / move / release on the viewport) with a real edit controller:
before / inside / after, a multi-selection moved as a block, "after an open container" = before its first
child, moves between records and to / from the record level, and every refusal (into the own subtree, inside a
scalar, the wrapper's record container, record rows outside the base view, children of a sorted parent, no
editing) with the forbidden cursor and no change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape.gui.session import FilterSpec  # noqa: E402
from jsonreshape.gui.treemodel import WRAPPER  # noqa: E402
from test_gui_tree_edit import (  # noqa: E402,F401
    TIMEOUT,
    _no_modal_confirm,
    app,
    changed,
    expand,
    opened,
    select,
    values,
    views,
    wait_idle,
    wait_text,
    write,
)

pytestmark = pytest.mark.gui

LEFT = Qt.MouseButton.LeftButton
NOMOD = Qt.KeyboardModifier.NoModifier


def point(view, kp: tuple, frac: float = 0.5) -> QPoint:
    row = view.row_of(kp)
    assert row is not None, kp
    view.grid.scroll_to(row, "ensure")
    rect = view.grid.cell_rect(row, 1)                    # the value column: never the expander
    return QPoint(rect.center().x(), rect.top() + int(frac * view.grid.row_height()))


def start_drag(qtbot, view, kp: tuple) -> QPoint:
    """Press on ``kp``'s row and move past the drag distance."""
    wait_text(qtbot, view, view.row_of(kp))
    p = point(view, kp)
    vp = view.grid.viewport()
    QTest.mousePress(vp, LEFT, NOMOD, p)
    QTest.mouseMove(vp, p + QPoint(QApplication.startDragDistance() + 3, 0))
    return p


def hover(view, kp: tuple, frac: float):
    p = point(view, kp, frac)
    QTest.mouseMove(view.grid.viewport(), p)
    return view.grid.drop_target(), p


def drop(qtbot, s, view, src: tuple, dst: tuple, frac: float):
    """Drag ``src`` (or the selection holding it) onto ``dst`` at ``frac``; returns the drop target shown and
    the ChangeSet."""
    start_drag(qtbot, view, src)
    target, p = hover(view, dst, frac)
    assert target is not None, (src, dst, frac)
    cs = changed(qtbot, s, lambda: QTest.mouseRelease(view.grid.viewport(), LEFT, NOMOD, p))
    wait_idle(qtbot, view)
    return target, cs


def refused(qtbot, s, view, src: tuple, dst: tuple, frac: float) -> None:
    steps = s.edit.steps
    start_drag(qtbot, view, src)
    assert view.grid.row_drag_active()
    target, p = hover(view, dst, frac)
    assert target is None
    assert view.grid.viewport().cursor().shape() == Qt.CursorShape.ForbiddenCursor
    QTest.mouseRelease(view.grid.viewport(), LEFT, NOMOD, p)
    s.app.tasks.wait_idle(2)
    assert s.edit.steps == steps and not view.grid.row_drag_active()


def doc(n: int = 4) -> list:
    return [{"id": i, "name": f"n{i}", "list": [10 * i, 10 * i + 1, 10 * i + 2], "o": {"a": i, "b": "Toshkent"}}
            for i in range(n)]


def test_drop_before_after_inside(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "d.json", doc()))
    view = views(s)
    expand(qtbot, view, (0,))
    expand(qtbot, view, (0, 2))
    expand(qtbot, view, (0, 3))
    # "name" before "o" (same object): a move, the open containers stay open
    row_o = view.row_of((0, 3))
    target, cs = drop(qtbot, s, view, (0, 1), (0, 3), 0.1)
    assert target == (row_o, "before")
    assert list(values(s)[0]) == ["id", "list", "name", "o"]
    assert view.flat.is_expanded((0, 1)) and view.flat.is_expanded((0, 3))
    assert view.current_keypath() == (0, 2)
    # "id" inside the object "o": a member of it now
    target, cs = drop(qtbot, s, view, (0, 0), (0, 3), 0.5)
    assert target[1] == "inside"
    assert values(s)[0] == {"list": [0, 1, 2], "name": "n0", "o": {"a": 0, "b": "Toshkent", "id": 0}}
    # an element after another element of the same array
    target, cs = drop(qtbot, s, view, (0, 0, 0), (0, 0, 2), 0.9)
    assert target[1] == "after" and values(s)[0]["list"] == [1, 2, 0]


def test_after_an_open_container_is_before_its_first_child(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "d.json", doc()))
    view = views(s)
    expand(qtbot, view, (0,))
    expand(qtbot, view, (0, 2))
    first_child = view.row_of((0, 2, 0))
    target, cs = drop(qtbot, s, view, (0, 1), (0, 2), 0.95)      # below the open "list" row
    assert target == (first_child, "before")
    assert values(s)[0]["list"] == ["n0", 0, 1, 2] and "name" not in values(s)[0]


def test_records_move_in_the_base_view_and_nodes_between_records(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "d.json", doc(5)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 5, timeout=TIMEOUT)
    expand(qtbot, view, (3,))
    # record 3 (expanded) before record 0
    target, cs = drop(qtbot, s, view, (3,), (0,), 0.05)
    assert target[1] == "before" and cs.remap is not None
    assert [r["id"] for r in values(s)] == [3, 0, 1, 2, 4]
    assert view.flat.is_expanded((0,)) and not view.flat.is_expanded((3,))
    # a node of record 0 (old 3) into record 2's object
    expand(qtbot, view, (2,))
    target, cs = drop(qtbot, s, view, (0, 1), (2, 3), 0.5)
    assert values(s)[0] == {"id": 3, "list": [30, 31, 32], "o": {"a": 3, "b": "Toshkent"}}
    assert values(s)[2]["o"]["name"] == "n3"
    # a multi-selection of records after the last record
    view.select_keypaths([(1,), (3,)])
    target, cs = drop(qtbot, s, view, (1,), (4,), 0.95)
    assert target[1] == "after"
    assert [r["id"] for r in values(s)] == [3, 1, 4, 0, 2]


def test_refusals(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "d.json", doc()))
    view = views(s)
    expand(qtbot, view, (0,))
    expand(qtbot, view, (0, 3))
    refused(qtbot, s, view, (0, 3), (0, 3, 0), 0.5)          # into its own subtree
    refused(qtbot, s, view, (0, 3), (0, 3), 0.5)             # onto itself
    refused(qtbot, s, view, (0, 1), (0, 0), 0.5)             # inside a scalar
    # children of a sorted parent: no sibling drops (positions are not file positions)
    view.toggle_sort_keys(True)
    qtbot.waitUntil(lambda: view.flat.has_order((0,)), timeout=TIMEOUT)
    wait_idle(qtbot, view)
    refused(qtbot, s, view, (1,), (0, 1), 0.1)
    view.toggle_sort_keys(False)
    qtbot.waitUntil(lambda: not view.flat.has_order((0,)), timeout=TIMEOUT)
    # record rows outside the base view: no drag of records, no drop between records
    s.set_filter(FilterSpec("id >= 1"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 3, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.row_count() == 3, timeout=TIMEOUT)
    wait_idle(qtbot, view)
    start = view.row_of((1,))
    wait_text(qtbot, view, start)
    select(qtbot, view, (1,))
    assert view._drag_can_start([(start, start)]) is False
    expand(qtbot, view, (2,))
    refused(qtbot, s, view, (2, 1), (3,), 0.05)              # a node before a record row of a filtered view
    # ... but into a record (a node-level move) is fine; the key is taken there: a free one
    target, cs = drop(qtbot, s, view, (2, 1), (3,), 0.5)
    assert target[1] == "inside" and values(s)[3]["name_2"] == "n2" and "name" not in values(s)[2]


def test_wrapper_record_container_refuses_drops(qtbot, opened, views, tmp_path: Path) -> None:
    data = {"meta": {"v": 1, "w": 2}, "data": doc(2)}
    s = opened(write(tmp_path / "w.json", data), format="wrapped", record_path="data")
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 3, timeout=TIMEOUT)
    expand(qtbot, view, (WRAPPER,))
    expand(qtbot, view, (WRAPPER, 0))
    refused(qtbot, s, view, (WRAPPER, 0, 0), (WRAPPER, 1), 0.5)      # inside the record container
    refused(qtbot, s, view, (WRAPPER, 0, 0), (WRAPPER,), 0.05)       # before the wrapper row
    # the wrapper row itself is never dragged
    row = view.row_of((WRAPPER,))
    assert view._drag_can_start([(row, row)]) is False
    # a member of meta moved into a record: allowed
    expand(qtbot, view, (0,))
    target, cs = drop(qtbot, s, view, (WRAPPER, 0, 1), (0, 3), 0.5)
    assert values(s)[0]["o"]["w"] == 2
    assert s.doc.pinned().root().value()["meta"] == {"v": 1}


def test_escape_cancels_a_tree_drag(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "d.json", doc()))
    view = views(s)
    expand(qtbot, view, (0,))
    steps = s.edit.steps
    start_drag(qtbot, view, (0, 1))
    target, p = hover(view, (0, 3), 0.1)
    assert target is not None
    QTest.keyClick(view.grid, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.grid.viewport(), LEFT, NOMOD, p)
    s.app.tasks.wait_idle(2)
    assert s.edit.steps == steps and list(values(s)[0]) == ["id", "name", "list", "o"]
    assert json.loads(json.dumps(values(s)[0]))["name"] == "n0"
