"""VirtualGrid row drag & drop and programmatic selection (jsonreshape/gui/vgrid.py;
docs/ARCHITECTURE.md §12.9 "Selection & drag").

``drop_zone`` boundaries; ``set_selected_ranges`` / ``selected_row_ranges`` in every mode; the
internal drag through real mouse events on the viewport (QTest press / move / release): start
after ``startDragDistance``, a press on a selected row keeping the selection until the
release, zones and targets, the forbidden cursor, the painted indicator, auto-scroll near the
edges, Esc / reset / structure changes / hiding cancel, a refused start falling back to drag
selection, a handler that raises, and selections of 10**9 rows passed as ranges.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape.gui.vgrid import DROP_AFTER, DROP_BEFORE, Cell, Column, VirtualGrid, drop_zone  # noqa: E402

pytestmark = pytest.mark.gui

K = Qt.Key
M = Qt.KeyboardModifier
LEFT = Qt.MouseButton.LeftButton
NOMOD = M.NoModifier


class Source:
    def __init__(self, rows: int, depth=None) -> None:
        self.rows = rows
        self.depth = depth or (lambda row: 0)

    def row_count(self) -> int:
        return self.rows

    def cell(self, row: int, column: Column) -> Cell:
        return Cell(text=f"{column.id}:{row}", depth=self.depth(row) if column.id == "c0" else 0)

    def rows_shown(self, first: int, last: int) -> None:
        pass

    def row_label(self, row: int) -> str | None:
        return None


class Handler:
    """Records the calls; refuses targets in ``forbidden``; ``allow`` answers can_start."""

    def __init__(self, *, allow: bool = True, forbidden=(), boom: str | None = None) -> None:
        self.allow = allow
        self.forbidden = set(forbidden)
        self.boom = boom
        self.started: list = []
        self.asked: list = []
        self.dropped: list = []

    def can_start(self, rows):
        if self.boom == "can_start":
            raise RuntimeError("boom")
        self.started.append(rows)
        return self.allow

    def target(self, row, frac):
        if self.boom == "target":
            raise RuntimeError("boom")
        self.asked.append((row, frac))
        if row in self.forbidden:
            return None
        return row, drop_zone(frac)

    def drop(self, rows, row, zone):
        if self.boom == "drop":
            raise RuntimeError("boom")
        self.dropped.append((rows, row, zone))


@pytest.fixture
def make_grid(qtbot):
    def make(rows: int = 200, *, mode: str = "rows", handler=None, height: int = 400, tree: bool = False,
             depth=None):
        grid = VirtualGrid()
        qtbot.addWidget(grid)
        grid.resize(500, height)
        grid.set_columns([Column(f"c{i}", f"C{i}", 120) for i in range(3)])
        if tree:
            grid.set_tree_column(0)
        grid.set_selection_mode(mode)
        grid.set_source(Source(rows, depth))
        if handler is not None:
            grid.set_row_drag(handler)
        grid.show()
        qtbot.waitExposed(grid)
        QApplication.processEvents()
        return grid

    return make


def at(grid: VirtualGrid, row: int, frac: float = 0.5, x: int = 60) -> QPoint:
    rect = grid.row_rect(row)
    assert rect.isValid()
    return QPoint(x, rect.top() + int(frac * grid.row_height()))


def press(grid, pos, mods=NOMOD):
    QTest.mousePress(grid.viewport(), LEFT, mods, pos)


def move(grid, pos):
    QTest.mouseMove(grid.viewport(), pos)


def release(grid, pos, mods=NOMOD):
    QTest.mouseRelease(grid.viewport(), LEFT, mods, pos)


def drag_start(grid, row: int) -> None:
    """Press on ``row`` and move past the drag distance (still on that row)."""
    p = at(grid, row, 0.5)
    press(grid, p)
    move(grid, p + QPoint(QApplication.startDragDistance() + 2, 0))


# ----------------------------------------------------------------------------- pure
def test_drop_zone_boundaries() -> None:
    assert (DROP_BEFORE, DROP_AFTER) == (0.28, 0.72)
    assert [drop_zone(f) for f in (0.0, 0.1, 0.279)] == ["before"] * 3
    assert [drop_zone(f) for f in (0.28, 0.5, 0.72)] == ["inside"] * 3
    assert [drop_zone(f) for f in (0.721, 0.9, 1.0)] == ["after"] * 3


# ----------------------------------------------------------------------------- selection API
def test_set_selected_ranges_rows_mode(make_grid) -> None:
    grid = make_grid(100)
    events = []
    grid.selection_changed.connect(lambda: events.append(1))
    grid.set_current(5)
    events.clear()
    grid.set_selected_ranges([(10, 12), (3, 1), (98, 150), (200, 300), (7, 7, 2, 2)])
    assert grid.selected_ranges() == [(10, 12, 0, 2), (1, 3, 0, 2), (98, 99, 0, 2), (7, 7, 0, 2)]
    assert grid.selected_row_ranges() == [(1, 3), (7, 7), (10, 12), (98, 99)]
    assert grid.selected_row_count() == 3 + 1 + 3 + 2
    assert grid.current() == (5, 0) and events == [1]
    grid.set_selected_ranges([(10, 12), (3, 1), (98, 150), (200, 300), (7, 7, 2, 2)])
    assert events == [1]                                  # unchanged: no signal
    grid.set_selected_ranges([])
    assert grid.selected_ranges() == [] and events == [1, 1]
    # merging of touching / overlapping ranges
    grid.set_selected_ranges([(5, 9), (10, 11), (8, 20), (30, 31)])
    assert grid.selected_row_ranges() == [(5, 20), (30, 31)]


def test_set_selected_ranges_cells_and_single(make_grid) -> None:
    grid = make_grid(50, mode="cells")
    grid.set_selected_ranges([(1, 2, 2, 0), (4, 4), (6, 6, 5, 9)])
    assert grid.selected_ranges() == [(1, 2, 0, 2), (4, 4, 0, 2)]
    single = make_grid(50, mode="single")
    single.set_current(3)
    single.set_selected_ranges([(10, 20)])
    assert single.selected_ranges() == [(3, 3, 0, 2)] and single.selected_row_ranges() == [(3, 3)]


def test_selection_kept_when_rows_stay(make_grid) -> None:
    grid = make_grid(100)
    grid.set_current(4)
    grid.set_selected_ranges([(2, 6), (90, 99)])
    grid.source().rows = 95                       # shrink: the tail range is clamped
    grid.rows_changed(keep_current=True)
    assert grid.selected_row_ranges() == [(2, 6), (90, 94)] and grid.current()[0] == 4
    grid.rows_changed(keep_current=False)
    assert grid.selected_row_ranges() == [] and grid.current() == (-1, -1)


# ----------------------------------------------------------------------------- drag basics
def test_drag_starts_after_the_drag_distance_and_drops(make_grid) -> None:
    h = Handler()
    grid = make_grid(handler=h)
    p = at(grid, 2)
    press(grid, p)
    assert grid.current()[0] == 2 and grid.selected_row_ranges() == [(2, 2)]
    move(grid, p + QPoint(1, 0))                         # below the drag distance: nothing yet
    assert not grid.row_drag_active() and h.started == []
    move(grid, at(grid, 5, 0.5))
    assert grid.row_drag_active() and h.started == [[(2, 2)]]
    assert grid.drop_target() == (5, "inside")
    assert grid.viewport().cursor().shape() == Qt.CursorShape.DragMoveCursor
    move(grid, at(grid, 6, 0.1))
    assert grid.drop_target() == (6, "before")
    move(grid, at(grid, 6, 0.9))
    assert grid.drop_target() == (6, "after")
    release(grid, at(grid, 6, 0.9))
    assert h.dropped == [([(2, 2)], 6, "after")]
    assert not grid.row_drag_active() and grid.drop_target() is None
    assert grid.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    assert grid.selected_row_ranges() == [(2, 2)]        # the drop does not change the selection


def test_press_on_a_selected_row_keeps_the_selection_for_the_drag(make_grid) -> None:
    h = Handler()
    grid = make_grid(handler=h)
    QTest.mouseClick(grid.viewport(), LEFT, NOMOD, at(grid, 1))
    QTest.mouseClick(grid.viewport(), LEFT, M.ShiftModifier, at(grid, 3))
    QTest.mouseClick(grid.viewport(), LEFT, M.ControlModifier, at(grid, 7))
    assert grid.selected_row_ranges() == [(1, 3), (7, 7)]
    drag_start(grid, 2)                                  # pressed on a selected row
    assert grid.selected_row_ranges() == [(1, 3), (7, 7)]
    assert h.started == [[(1, 3), (7, 7)]]
    move(grid, at(grid, 9, 0.95))
    release(grid, at(grid, 9, 0.95))
    assert h.dropped == [([(1, 3), (7, 7)], 9, "after")]
    assert grid.selected_row_ranges() == [(1, 3), (7, 7)]


def test_click_on_a_selected_row_selects_it_on_release(make_grid) -> None:
    h = Handler()
    grid = make_grid(handler=h)
    grid.set_current(1)
    grid.set_selected_ranges([(1, 5)])
    press(grid, at(grid, 3))
    assert grid.selected_row_ranges() == [(1, 5)]        # not yet
    release(grid, at(grid, 3))
    assert grid.selected_row_ranges() == [(3, 3)] and grid.current()[0] == 3
    assert h.started == [] and h.dropped == []


def test_forbidden_target_shows_the_forbidden_cursor_and_does_not_drop(make_grid) -> None:
    h = Handler(forbidden={4})
    grid = make_grid(handler=h)
    drag_start(grid, 1)
    move(grid, at(grid, 4, 0.5))
    assert grid.row_drag_active() and grid.drop_target() is None
    assert grid.viewport().cursor().shape() == Qt.CursorShape.ForbiddenCursor
    assert not grid.drop_indicator_rect().isValid()
    move(grid, at(grid, 5, 0.5))
    assert grid.viewport().cursor().shape() == Qt.CursorShape.DragMoveCursor
    move(grid, at(grid, 4, 0.5))
    release(grid, at(grid, 4, 0.5))
    assert h.dropped == [] and not grid.row_drag_active()


def test_escape_cancels_the_drag(make_grid) -> None:
    h = Handler()
    grid = make_grid(handler=h)
    drag_start(grid, 1)
    move(grid, at(grid, 6))
    assert grid.row_drag_active()
    QTest.keyClick(grid, K.Key_Down)                     # keys do nothing while dragging
    assert grid.current()[0] == 1
    QTest.keyClick(grid, K.Key_Escape)
    assert not grid.row_drag_active()
    assert grid.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    move(grid, at(grid, 8))
    release(grid, at(grid, 8))
    assert h.dropped == []
    assert grid.selected_row_ranges() == [(1, 1)]        # no drag selection afterwards


def test_refused_start_falls_back_to_drag_selection(make_grid) -> None:
    h = Handler(allow=False)
    grid = make_grid(handler=h)
    drag_start(grid, 1)
    move(grid, at(grid, 4))
    assert not grid.row_drag_active() and h.started == [[(1, 1)]]
    assert grid.selected_row_ranges() == [(1, 4)]
    release(grid, at(grid, 4))
    # a refused drag from a selected row first selects that row
    grid.set_selected_ranges([(10, 12)])
    grid.set_current(10, select=False)
    press(grid, at(grid, 11))
    move(grid, at(grid, 13))
    assert grid.selected_row_ranges() == [(11, 13)]
    release(grid, at(grid, 13))


def test_without_a_handler_presses_select_at_once(make_grid) -> None:
    grid = make_grid()
    grid.set_current(1)
    grid.set_selected_ranges([(1, 5)])
    press(grid, at(grid, 3))
    assert grid.selected_row_ranges() == [(3, 3)]
    move(grid, at(grid, 6))
    assert grid.selected_row_ranges() == [(3, 6)]       # drag selection as before
    release(grid, at(grid, 6))
    grid.set_row_drag(Handler())
    grid.set_row_drag(None)
    assert grid.row_drag() is None


def test_single_mode_drags_the_current_row(make_grid) -> None:
    h = Handler()
    grid = make_grid(mode="single", handler=h)
    drag_start(grid, 3)
    assert h.started == [[(3, 3)]]
    move(grid, at(grid, 0, 0.0))
    release(grid, at(grid, 0, 0.0))
    assert h.dropped == [([(3, 3)], 0, "before")]


def test_cells_mode_never_drags(make_grid) -> None:
    h = Handler()
    grid = make_grid(mode="cells", handler=h)
    press(grid, at(grid, 1))
    move(grid, at(grid, 4))
    release(grid, at(grid, 4))
    assert h.started == [] and grid.selected_ranges() == [(1, 4, 0, 0)]


def test_modifier_presses_keep_selecting(make_grid) -> None:
    h = Handler()
    grid = make_grid(handler=h)
    QTest.mouseClick(grid.viewport(), LEFT, NOMOD, at(grid, 2))
    press(grid, at(grid, 5), M.ShiftModifier)
    move(grid, at(grid, 7))
    release(grid, at(grid, 7), M.ShiftModifier)
    assert h.started == [] and grid.selected_row_ranges() == [(2, 7)]


# ----------------------------------------------------------------------------- outside rows, scrolling
def test_targets_outside_the_rows(make_grid) -> None:
    h = Handler()
    grid = make_grid(5, handler=h)
    drag_start(grid, 0)
    move(grid, QPoint(60, grid.viewport().height() - 3))       # below the last row
    assert grid.drop_target() == (4, "after") and h.asked[-1] == (4, 1.0)
    release(grid, QPoint(60, grid.viewport().height() - 3))
    assert h.dropped == [([(0, 0)], 4, "after")]


def test_auto_scroll_near_the_edges(make_grid) -> None:
    h = Handler()
    grid = make_grid(1000, handler=h)
    drag_start(grid, 2)
    vp = grid.viewport()
    bottom = QPoint(60, vp.height() - 2)
    move(grid, bottom)
    assert grid._auto.isActive()
    top_before = grid.top_row()
    for _ in range(5):
        grid._auto_scroll_tick()                       # what the timer does
    assert grid.top_row() > top_before
    last = grid.visible_rows()[1]
    assert grid.drop_target()[0] >= last - 1          # the target follows the scrolling
    move(grid, QPoint(60, vp.height() // 2))           # back in the middle: scrolling stops
    assert not grid._auto.isActive()
    move(grid, QPoint(60, 1))
    assert grid._auto.isActive()
    now = grid.top_row()
    grid._auto_scroll_tick()
    assert grid.top_row() == now - 1
    release(grid, QPoint(60, 1))
    assert not grid._auto.isActive() and len(h.dropped) == 1


# ----------------------------------------------------------------------------- cancellation
def test_structure_changes_and_hiding_cancel_the_drag(make_grid) -> None:
    h = Handler()
    grid = make_grid(handler=h)
    drag_start(grid, 1)
    assert grid.row_drag_active()
    grid.rows_changed(keep_current=True)               # growth keeps the drag
    assert grid.row_drag_active()
    grid.rows_changed(keep_current=False)              # rearranged rows: cancelled
    assert not grid.row_drag_active()
    release(grid, at(grid, 3))
    drag_start(grid, 1)
    grid.reset()
    assert not grid.row_drag_active()
    release(grid, at(grid, 3))
    drag_start(grid, 1)
    grid.hide()
    assert not grid.row_drag_active() and h.dropped == []


def test_failing_handler_is_contained(make_grid, caplog) -> None:
    for boom in ("can_start", "target", "drop"):
        h = Handler(boom=boom)
        grid = make_grid(handler=h)
        drag_start(grid, 1)
        move(grid, at(grid, 4))
        if boom == "target":
            assert grid.row_drag_active() and grid.drop_target() is None
        release(grid, at(grid, 4))
        assert not grid.row_drag_active()
    assert "row drag" in caplog.text


# ----------------------------------------------------------------------------- painting
def test_indicator_geometry_and_painting(make_grid) -> None:
    h = Handler()
    grid = make_grid(handler=h, tree=True, depth=lambda row: 2 if row == 5 else 0)
    drag_start(grid, 1)
    rh = grid.row_height()
    move(grid, at(grid, 5, 0.05))
    rect = grid.drop_indicator_rect()
    assert rect.height() == 2 and rect.top() == grid.row_rect(5).top() - 1
    assert rect.left() == grid.cell_rect(5, 0).left() + grid._pad_x + 2 * grid.indent()   # tree indentation
    move(grid, at(grid, 3, 0.95))
    rect = grid.drop_indicator_rect()
    assert rect.top() == grid.row_rect(3).top() + rh - 1
    move(grid, at(grid, 3, 0.5))
    rect = grid.drop_indicator_rect()
    assert rect.height() == rh and rect.top() == grid.row_rect(3).top()
    # the indicator is painted in the accent color
    move(grid, at(grid, 6, 0.95))
    QApplication.processEvents()
    image = grid.viewport().grab().toImage()
    rect = grid.drop_indicator_rect()
    dpr = image.devicePixelRatio()
    probe = QColor(image.pixel(int((rect.left() + 20) * dpr), int((rect.top() + 1) * dpr)))
    accent = grid.palette().color(grid.palette().ColorRole.Highlight)
    assert abs(probe.red() - accent.red()) + abs(probe.green() - accent.green()) + abs(probe.blue() - accent.blue()) < 40
    release(grid, at(grid, 6, 0.95))
    assert not grid.drop_indicator_rect().isValid()


# ----------------------------------------------------------------------------- huge selections
def test_huge_selection_is_passed_as_ranges(make_grid) -> None:
    h = Handler()
    grid = make_grid(10**9, handler=h)
    grid.set_current(0)
    grid.select_all()
    drag_start(grid, 2)
    assert h.started == [[(0, 10**9 - 1)]]
    QTest.keyClick(grid, K.Key_Escape)
    release(grid, at(grid, 2))
    assert grid.selected_row_ranges() == [(0, 10**9 - 1)]
