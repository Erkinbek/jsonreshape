"""jsonreshape.gui.vgrid: VirtualGrid + GridHeader (docs/ARCHITECTURE.md §11.5).

Huge row counts (10**9, 10**12: scaled scrollbar, exact scrolling), visible window and
``rows_shown``, keyboard / mouse navigation and the three selection modes, header gestures
(resize, fit, reorder, click, context menu, sort indicators), frozen column, hidden
columns, tree column (expander, guides, badges), placeholders, live growth, zoom,
tooltips, theme repaint, painting smoke and timing.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QContextMenuEvent, QFontMetrics, QHelpEvent, QPalette, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QAbstractSlider, QApplication  # noqa: E402

from jsonreshape.gui import theme  # noqa: E402
from jsonreshape.gui.vgrid import MIN_COLUMN_WIDTH, Cell, Column, VirtualGrid  # noqa: E402

pytestmark = pytest.mark.gui

K = Qt.Key
M = Qt.KeyboardModifier
LEFT = Qt.MouseButton.LeftButton
RIGHT = Qt.MouseButton.RightButton
NOMOD = M.NoModifier


class FakeSource:
    """GridSource recording what the grid asks for."""

    def __init__(self, rows: int, make=None, labels: bool = True) -> None:
        self.rows = rows
        self.make = make
        self.shown: list[tuple[int, int]] = []
        self.asked: set[tuple[int, str]] = set()
        self.calls = 0
        if not labels:
            self.row_label = None  # type: ignore[assignment]

    def row_count(self) -> int:
        return self.rows

    def cell(self, row: int, column: Column):
        self.calls += 1
        self.asked.add((row, column.id))
        if self.make is not None:
            return self.make(row, column)
        return Cell(text=f"{column.id}:{row}")

    def rows_shown(self, first: int, last: int) -> None:
        self.shown.append((first, last))

    def row_label(self, row: int) -> str | None:
        return f"#{row}"


def columns(n: int, width: int = 120) -> list[Column]:
    return [Column(f"c{i}", f"Column {i}", width) for i in range(n)]


@pytest.fixture
def make_grid(qtbot):
    def make(rows: int = 1000, ncols: int = 4, *, width: int = 600, height: int = 400, source=None,
             mode: str = "rows", col_width: int = 120):
        grid = VirtualGrid()
        qtbot.addWidget(grid)
        grid.resize(width, height)
        grid.set_columns(columns(ncols, col_width))
        grid.set_selection_mode(mode)
        src = source if source is not None else FakeSource(rows)
        grid.set_source(src)
        grid.show()
        qtbot.waitExposed(grid)
        QApplication.processEvents()
        return grid, src

    return make


def page(grid: VirtualGrid) -> int:
    return max(1, grid.viewport().height() // grid.row_height())


def window(grid: VirtualGrid) -> int:
    return -(-grid.viewport().height() // grid.row_height())


def center(grid: VirtualGrid, row: int, col: int) -> QPoint:
    rect = grid.cell_rect(row, col)
    assert rect.isValid()
    return rect.center()


def click(grid: VirtualGrid, pos: QPoint, mods=NOMOD, button=LEFT) -> None:
    QTest.mouseClick(grid.viewport(), button, mods, pos)


def key(grid: VirtualGrid, k, mods=NOMOD) -> None:
    QTest.keyClick(grid, k, mods)


def wheel(grid: VirtualGrid, dy: int = 0, dx: int = 0, mods=NOMOD) -> None:
    pos = QPointF(50, 50)
    ev = QWheelEvent(pos, QPointF(grid.viewport().mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(dx, dy),
                     Qt.MouseButton.NoButton, mods, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(grid.viewport(), ev)


# ----------------------------------------------------------------------------- huge counts
def test_visible_window_and_rows_shown(make_grid) -> None:
    grid, src = make_grid(10**9)
    first, last = grid.visible_rows()
    assert first == 0 and last == window(grid) - 1
    assert src.shown[-1] == (0, last + page(grid))
    # only visible rows were asked
    assert {r for r, _ in src.asked} <= set(range(first, last + 1))
    n_calls = len(src.shown)
    grid.refresh()
    grid.scroll_to(0, "top")
    assert len(src.shown) == n_calls          # deduplicated
    grid.scroll_to(500_000_000, "top")
    assert src.shown[-1] == (500_000_000, 500_000_000 + window(grid) - 1 + page(grid))
    grid.refresh(request=True)
    assert len(src.shown) == n_calls + 2      # forced re-request
    # the prefetch never runs past the last row
    grid.scroll_to(10**9 - 1)
    assert src.shown[-1][1] == 10**9 - 1


def test_scaled_scrollbar_round_trips(make_grid) -> None:
    rows = 10**12
    grid, _ = make_grid(rows)
    bar = grid.verticalScrollBar()
    max_top = rows - page(grid)
    assert bar.minimum() == 0 and bar.maximum() == 2**30
    for value in (0, 1, 12345, 2**29, 2**30 - 1, 2**30):
        top = grid._value_to_top(value)
        assert grid._top_to_value(top) == value
    for top in (0, 1, 10**6, 5 * 10**11, max_top - 1, max_top):
        back = grid._value_to_top(grid._top_to_value(top))
        assert abs(back - top) <= max_top // 2**30 + 1
    bar.setValue(2**29)                       # dragging the thumb maps proportionally
    assert abs(grid.top_row() - max_top // 2) <= max_top // 2**30 + 1
    bar.setValue(2**30)
    assert grid.top_row() == max_top
    assert grid.visible_rows()[1] == rows - 1


def test_scaled_scrollbar_steps_move_exact_rows(make_grid) -> None:
    grid, _ = make_grid(10**12)
    bar = grid.verticalScrollBar()
    grid.scroll_to(4 * 10**11, "top")
    top = grid.top_row()
    assert top == 4 * 10**11
    bar.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
    assert grid.top_row() == top + 1
    bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepAdd)
    assert grid.top_row() == top + 1 + page(grid)
    bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
    bar.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepSub)
    assert grid.top_row() == top
    bar.triggerAction(QAbstractSlider.SliderAction.SliderToMaximum)
    assert grid.top_row() == 10**12 - page(grid)


def test_small_row_count_uses_plain_range(make_grid) -> None:
    grid, _ = make_grid(1000)
    bar = grid.verticalScrollBar()
    assert bar.maximum() == 1000 - page(grid) and bar.pageStep() == page(grid)
    bar.setValue(123)
    assert grid.top_row() == 123
    grid.scroll_to(900, "top")
    assert bar.value() == 900


def test_scroll_to_end_center_ensure(make_grid) -> None:
    rows = 10**12
    grid, _ = make_grid(rows)
    p = page(grid)
    grid.scroll_to(rows - 1)
    assert grid.visible_rows()[1] == rows - 1 and grid.top_row() == rows - p
    assert grid.verticalScrollBar().value() == grid.verticalScrollBar().maximum()
    mid = 5 * 10**11 + 7
    grid.scroll_to(mid, "center")
    first, last = grid.visible_rows()
    assert grid.top_row() == mid - (p - 1) // 2
    assert first < mid < last
    grid.scroll_to(mid, "top")
    assert grid.top_row() == mid
    grid.scroll_to(mid + 2, "ensure")         # already visible: no move
    assert grid.top_row() == mid
    grid.scroll_to(mid + p + 10, "ensure")    # minimal move: becomes the last full row
    assert grid.top_row() == mid + 11
    grid.scroll_to(mid - 3, "ensure")
    assert grid.top_row() == mid - 3
    grid.scroll_to(-5)
    assert grid.top_row() == 0


def test_set_current_last_row_of_huge_grid(make_grid) -> None:
    rows = 10**12
    grid, src = make_grid(rows)
    events = []
    grid.current_changed.connect(lambda r, c: events.append((r, c)))
    grid.set_current(rows - 1, 2)
    assert grid.current() == (rows - 1, 2)
    assert events == [(rows - 1, 2)]
    rect = grid.cell_rect(rows - 1, 2)
    assert rect.isValid() and 0 <= rect.top() and rect.bottom() < grid.viewport().height()
    assert grid.selected_ranges() == [(rows - 1, rows - 1, 0, 3)]
    grid.set_current(rows - 1, 2)             # unchanged -> no signal
    assert len(events) == 1
    grid.viewport().repaint()
    assert {r for r, _ in src.asked if r > 10**11} <= set(range(rows - window(grid) - 1, rows))
    # far rows give clamped (but valid) rects; invalid rows a null rect
    assert grid.cell_rect(0, 0).isValid()
    assert grid.cell_rect(rows, 0).isNull() and grid.cell_rect(-1, 0).isNull()
    assert grid.row_at(grid.cell_rect(rows - 1, 0).center().y()) == rows - 1
    if grid.viewport().height() % grid.row_height():      # the partial area below the last row
        assert grid.row_at(grid.viewport().height() - 1) == -1
    assert grid.row_at(-1) == -1


def test_select_all_is_constant_time(make_grid) -> None:
    grid, _ = make_grid(10**12)
    start = time.perf_counter()
    with_signal = []
    grid.selection_changed.connect(lambda: with_signal.append(1))
    key(grid, K.Key_A, M.ControlModifier)
    assert time.perf_counter() - start < 0.5
    assert grid.selected_ranges() == [(0, 10**12 - 1, 0, 3)]
    assert grid.selected_row_count() == 10**12
    assert with_signal == [1]
    assert grid.is_selected(987654321012)
    grid.clear_selection()
    assert grid.selected_ranges() == []


# ----------------------------------------------------------------------------- scrolling input
def test_wheel_scrolls_rows_and_columns(make_grid) -> None:
    grid, _ = make_grid(1000, ncols=8)
    viewports = []
    grid.viewport_changed.connect(lambda: viewports.append(grid.visible_rows()))
    wheel(grid, dy=-120)
    assert grid.top_row() == 3
    wheel(grid, dy=-60)                       # half notches (touchpads) accumulate fractions
    assert grid.top_row() == 4
    wheel(grid, dy=-60)
    assert grid.top_row() == 6
    wheel(grid, dy=240)
    assert grid.top_row() == 0
    wheel(grid, dy=120)                       # at the top: nothing changes
    assert grid.top_row() == 0
    assert len(viewports) == 4
    wheel(grid, dy=-120, mods=M.ShiftModifier)
    assert grid.horizontal_offset() > 0 and grid.top_row() == 0
    off = grid.horizontal_offset()
    wheel(grid, dx=120)
    assert grid.horizontal_offset() < off


def test_ctrl_wheel_and_keys_zoom(make_grid, qtbot) -> None:
    grid, _ = make_grid(1000)
    h0 = grid.row_height()
    size0 = grid.grid_font().pointSizeF()
    with qtbot.waitSignal(grid.font_changed, timeout=1000):
        wheel(grid, dy=120, mods=M.ControlModifier)
    assert grid.grid_font().pointSizeF() == pytest.approx(size0 + 1)
    assert grid.top_row() == 0
    key(grid, K.Key_Plus, M.ControlModifier)
    key(grid, K.Key_Plus, M.ControlModifier)
    assert grid.zoom_level() == pytest.approx(3)
    assert grid.row_height() > h0
    key(grid, K.Key_Minus, M.ControlModifier)
    assert grid.zoom_level() == pytest.approx(2)
    key(grid, K.Key_0, M.ControlModifier)
    assert grid.zoom_level() == 0 and grid.row_height() == h0
    grid.zoom(-100)                            # clamped
    assert grid.grid_font().pointSizeF() >= 5
    grid.zoom(0)
    assert grid.row_height() == h0


def test_zoom_keeps_top_row_and_updates_window(make_grid) -> None:
    grid, src = make_grid(10**9)
    grid.scroll_to(777_777, "top")
    grid.zoom(4)
    assert grid.top_row() == 777_777
    assert grid.visible_rows() == (777_777, 777_777 + window(grid) - 1)
    assert src.shown[-1][0] == 777_777
    font = grid.grid_font()
    font.setPointSizeF(20)
    grid.set_font(font)
    assert grid.zoom_level() == 0 and grid.grid_font().pointSizeF() == 20
    grid.zoom(1)
    grid.zoom(0)
    assert grid.grid_font().pointSizeF() == 20


# ----------------------------------------------------------------------------- keyboard
def test_keyboard_navigation_rows_mode(make_grid, qtbot) -> None:
    grid, _ = make_grid(1000)
    p = page(grid)
    key(grid, K.Key_Down)                      # no current yet: the top row becomes current
    assert grid.current() == (0, 0)
    key(grid, K.Key_Down)
    key(grid, K.Key_Down)
    assert grid.current() == (2, 0)
    assert grid.selected_ranges() == [(2, 2, 0, 3)]
    key(grid, K.Key_Down, M.ShiftModifier)
    key(grid, K.Key_Down, M.ShiftModifier)
    assert grid.current() == (4, 0)
    assert grid.selected_ranges() == [(2, 4, 0, 3)]
    key(grid, K.Key_Up, M.ShiftModifier)
    assert grid.selected_ranges() == [(2, 3, 0, 3)]
    key(grid, K.Key_PageDown)
    assert grid.current()[0] == 3 + p
    assert grid.visible_rows()[0] <= 3 + p <= grid.top_row() + p - 1
    key(grid, K.Key_End, M.ControlModifier)
    assert grid.current()[0] == 999 and grid.visible_rows()[1] == 999
    key(grid, K.Key_PageDown)                  # clamped
    assert grid.current()[0] == 999
    key(grid, K.Key_Home, M.ControlModifier)
    assert grid.current() == (0, 0) and grid.top_row() == 0
    key(grid, K.Key_End)                       # rows mode: End = last row
    assert grid.current()[0] == 999
    key(grid, K.Key_Home)
    assert grid.current()[0] == 0
    key(grid, K.Key_Up)
    assert grid.current()[0] == 0
    with qtbot.waitSignal(grid.activated, timeout=1000) as blocker:
        key(grid, K.Key_Return)
    assert blocker.args == [0, 0]
    with qtbot.waitSignal(grid.activated, timeout=1000):
        key(grid, K.Key_Enter)


def test_ctrl_arrows_move_current_without_selecting(make_grid) -> None:
    grid, _ = make_grid(100)
    grid.set_current(5)
    key(grid, K.Key_Down, M.ControlModifier)
    key(grid, K.Key_Down, M.ControlModifier)
    assert grid.current()[0] == 7
    assert grid.selected_ranges() == [(5, 5, 0, 3)]
    key(grid, K.Key_Space, M.ControlModifier)  # toggle the current row
    assert sorted(grid.selected_ranges()) == [(5, 5, 0, 3), (7, 7, 0, 3)]
    assert grid.selected_row_count() == 2


def test_left_right_scroll_horizontally_in_rows_mode(make_grid) -> None:
    grid, _ = make_grid(100, ncols=10)
    grid.set_current(0, 0)
    key(grid, K.Key_Right)
    assert grid.horizontal_offset() > 0 and grid.current() == (0, 0)
    key(grid, K.Key_Left)
    assert grid.horizontal_offset() == 0


def test_keyboard_cells_mode(make_grid) -> None:
    grid, _ = make_grid(100, ncols=8, mode="cells")
    grid.set_column_visible(2, False)
    grid.set_current(3, 0)
    key(grid, K.Key_Right)
    assert grid.current() == (3, 1)
    key(grid, K.Key_Right)                    # skips the hidden column 2
    assert grid.current() == (3, 3)
    key(grid, K.Key_Right, M.ShiftModifier)
    key(grid, K.Key_Down, M.ShiftModifier)
    assert grid.current() == (4, 4)
    assert grid.selected_ranges() == [(3, 4, 3, 4)]
    key(grid, K.Key_End)                      # cells mode: End = last column
    assert grid.current() == (4, 7)
    assert grid.selected_ranges() == [(4, 4, 7, 7)]
    rect = grid.cell_rect(4, 7)               # scrolled into view
    assert rect.left() >= 0 and rect.right() < grid.viewport().width()
    key(grid, K.Key_Home)
    assert grid.current() == (4, 0) and grid.horizontal_offset() == 0
    key(grid, K.Key_Left)
    assert grid.current() == (4, 0)
    key(grid, K.Key_End, M.ControlModifier)
    assert grid.current() == (99, 0)
    key(grid, K.Key_A, M.ControlModifier)
    assert grid.selected_ranges() == [(0, 99, 0, 7)]


def test_key_handler_runs_first(make_grid) -> None:
    grid, _ = make_grid(100)
    seen = []

    def handler(event) -> bool:
        seen.append(event.key())
        return event.key() == K.Key_Down

    grid.set_key_handler(handler)
    grid.set_current(5)
    key(grid, K.Key_Down)
    assert grid.current()[0] == 5             # consumed by the handler
    key(grid, K.Key_Up)
    assert grid.current()[0] == 4
    assert seen == [K.Key_Down, K.Key_Up]
    grid.set_key_handler(None)
    key(grid, K.Key_Down)
    assert grid.current()[0] == 5


def test_single_mode(make_grid) -> None:
    grid, _ = make_grid(100, mode="single")
    click(grid, center(grid, 4, 1))
    assert grid.current() == (4, 1)
    assert grid.selected_ranges() == [(4, 4, 0, 3)]
    click(grid, center(grid, 6, 0), M.ShiftModifier)
    assert grid.selected_ranges() == [(6, 6, 0, 3)]
    key(grid, K.Key_Down, M.ShiftModifier)
    assert grid.selected_ranges() == [(7, 7, 0, 3)]
    key(grid, K.Key_A, M.ControlModifier)
    assert grid.selected_ranges() == [(7, 7, 0, 3)]
    assert grid.is_selected(7) and not grid.is_selected(6)


# ----------------------------------------------------------------------------- mouse
def test_mouse_selection_rows_mode(make_grid, qtbot) -> None:
    grid, _ = make_grid(1000)
    sel_events = []
    grid.selection_changed.connect(lambda: sel_events.append(1))
    click(grid, center(grid, 2, 1))
    assert grid.current() == (2, 1)
    assert grid.selected_ranges() == [(2, 2, 0, 3)]
    click(grid, center(grid, 5, 2), M.ShiftModifier)
    assert grid.current() == (5, 2)
    assert grid.selected_ranges() == [(2, 5, 0, 3)]
    click(grid, center(grid, 8, 0), M.ControlModifier)
    assert sorted(grid.selected_ranges()) == [(2, 5, 0, 3), (8, 8, 0, 3)]
    click(grid, center(grid, 3, 0), M.ControlModifier)   # toggles a row out of a range
    assert sorted(grid.selected_ranges()) == [(2, 2, 0, 3), (4, 5, 0, 3), (8, 8, 0, 3)]
    assert grid.selected_row_count() == 4
    click(grid, center(grid, 10, 0), M.ControlModifier | M.ShiftModifier)  # extend, keep the rest
    assert sorted(grid.selected_ranges()) == [(2, 2, 0, 3), (3, 10, 0, 3), (4, 5, 0, 3), (8, 8, 0, 3)]
    assert grid.selected_row_count() == 9
    assert sel_events
    # selection survives scrolling
    before = grid.selected_ranges()
    grid.scroll_to(900, "top")
    grid.scroll_to(0, "top")
    assert grid.selected_ranges() == before
    # double click activates
    with qtbot.waitSignal(grid.activated, timeout=1000) as blocker:
        QTest.mouseDClick(grid.viewport(), LEFT, NOMOD, center(grid, 7, 3))
    assert blocker.args == [7, 3]


def test_mouse_drag_selects_and_autoscrolls(make_grid) -> None:
    grid, _ = make_grid(1000)
    vp = grid.viewport()
    QTest.mousePress(vp, LEFT, NOMOD, center(grid, 1, 0))
    QTest.mouseMove(vp, center(grid, 4, 2))
    assert grid.selected_ranges() == [(1, 4, 0, 3)]
    assert grid.current() == (4, 2)
    below = QPoint(30, vp.height() + 3 * grid.row_height())
    QTest.mouseMove(vp, below)
    grid._auto_scroll_tick()                  # what the auto-scroll timer does
    assert grid.top_row() > 0
    last = grid.visible_rows()[1]
    assert grid.selected_ranges()[0][0] == 1 and grid.selected_ranges()[0][1] >= last - 1
    QTest.mouseRelease(vp, LEFT, NOMOD, below)
    top = grid.top_row()
    grid._auto_scroll_tick()                  # released: no more scrolling
    assert grid.top_row() == top


def test_mouse_cells_mode_rectangles(make_grid) -> None:
    grid, _ = make_grid(100, ncols=4, mode="cells")
    click(grid, center(grid, 1, 0))
    click(grid, center(grid, 3, 2), M.ShiftModifier)
    assert grid.selected_ranges() == [(1, 3, 0, 2)]
    click(grid, center(grid, 2, 1), M.ControlModifier)    # punch a hole
    cells = {(r, c) for r0, r1, c0, c1 in grid.selected_ranges()
             for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}
    assert cells == {(r, c) for r in (1, 2, 3) for c in (0, 1, 2)} - {(2, 1)}
    assert not grid.is_selected(2, 1) and grid.is_selected(2, 2)
    click(grid, center(grid, 2, 1), M.ControlModifier)    # and fill it again
    assert grid.is_selected(2, 1)


def test_clicks_outside_rows_and_in_gutter(make_grid) -> None:
    grid, _ = make_grid(3)
    grid.set_row_labels(True)
    QApplication.processEvents()
    click(grid, QPoint(200, grid.viewport().height() - 2))  # below the last row
    assert grid.current() == (-1, -1)
    assert grid.column_at(2) == -1 and grid.gutter_width() > 0
    click(grid, QPoint(2, grid.row_height() + 2))           # gutter: selects the row
    assert grid.current() == (1, 0)


def test_context_menu(make_grid, qtbot) -> None:
    grid, _ = make_grid(100)
    grid.set_current(2)
    click(grid, center(grid, 3, 0), M.ShiftModifier)        # rows 2..3 selected
    # right press on a selected row keeps the selection
    QTest.mousePress(grid.viewport(), RIGHT, NOMOD, center(grid, 2, 1))
    assert grid.selected_ranges() == [(2, 3, 0, 3)]
    assert grid.current() == (2, 1)
    # right press on an unselected row moves the current row
    QTest.mousePress(grid.viewport(), RIGHT, NOMOD, center(grid, 6, 2))
    assert grid.current() == (6, 2) and grid.selected_ranges() == [(6, 6, 0, 3)]
    pos = center(grid, 6, 2)
    gpos = grid.viewport().mapToGlobal(pos)
    with qtbot.waitSignal(grid.context_menu_requested, timeout=1000) as blocker:
        QApplication.sendEvent(grid.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, gpos))
    assert blocker.args == [6, 2, gpos]
    with qtbot.waitSignal(grid.context_menu_requested, timeout=1000) as blocker:   # keyboard (Menu key)
        QApplication.sendEvent(grid.viewport(), QContextMenuEvent(
            QContextMenuEvent.Reason.Keyboard, QPoint(0, 0), QPoint(0, 0)))
    assert blocker.args[:2] == [6, 2]
    assert blocker.args[2] == grid.viewport().mapToGlobal(grid.cell_rect(6, 2).center())


def test_context_menu_on_empty_area(make_grid, qtbot) -> None:
    grid, _ = make_grid(3)
    pos = QPoint(200, grid.viewport().height() - 2)
    gpos = grid.viewport().mapToGlobal(pos)
    with qtbot.waitSignal(grid.context_menu_requested, timeout=1000) as blocker:
        QApplication.sendEvent(grid.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, gpos))
    assert blocker.args == [-1, -1, gpos]
    pos = QPoint(grid.viewport().width() - 3, grid.cell_rect(1, 0).center().y())   # right of the columns
    with qtbot.waitSignal(grid.context_menu_requested, timeout=1000) as blocker:
        QApplication.sendEvent(grid.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, gpos))
    assert blocker.args[:2] == [1, -1]


def test_hover_row(make_grid) -> None:
    grid, _ = make_grid(100)
    QTest.mouseMove(grid.viewport(), center(grid, 4, 1))
    assert grid.hover_row() == 4
    QApplication.sendEvent(grid.viewport(), QEvent(QEvent.Type.Leave))
    assert grid.hover_row() == -1


# ----------------------------------------------------------------------------- reset / growth
def test_reset_and_rows_changed(make_grid, qtbot) -> None:
    src = FakeSource(50)
    grid, _ = make_grid(source=src)
    grid.set_current(10, 1)
    click(grid, center(grid, 12, 1), M.ShiftModifier)
    assert grid.selected_ranges() == [(10, 12, 0, 3)]
    # live growth keeps current, top row and selection
    src.rows = 10**6
    grid.scroll_to(5, "top")
    grid.rows_changed()
    assert grid.row_count() == 10**6 and grid.current() == (12, 1) and grid.top_row() == 5
    assert grid.selected_ranges() == [(10, 12, 0, 3)]
    assert grid.verticalScrollBar().maximum() == 10**6 - page(grid)
    # shrinking clamps the selection; the current row stays while valid
    src.rows = 12
    events = []
    grid.current_changed.connect(lambda r, c: events.append((r, c)))
    grid.rows_changed()
    assert grid.current() == (-1, -1) and events == [(-1, -1)]
    assert grid.selected_ranges() == [(10, 11, 0, 3)]
    assert grid.top_row() == max(0, 12 - page(grid))
    src.rows = 40
    grid.rows_changed()
    grid.set_current(20)
    grid.rows_changed(keep_current=False)
    assert grid.current() == (-1, -1) and grid.selected_ranges() == []
    grid.set_current(30)
    grid.scroll_to(30, "top")
    grid.reset()
    assert grid.current() == (-1, -1) and grid.top_row() == 0 and grid.selected_ranges() == []


def test_rows_changed_from_empty_requests_rows(make_grid) -> None:
    src = FakeSource(0)
    grid, _ = make_grid(source=src)
    assert src.shown == []
    assert grid.visible_rows() == (0, -1)
    src.rows = 5
    grid.rows_changed()
    assert src.shown == [(0, 4)]
    src.rows = 500
    grid.rows_changed()
    assert src.shown[-1] == (0, window(grid) - 1 + page(grid))


def test_hidden_grid_does_not_request_rows(qtbot) -> None:
    grid = VirtualGrid()
    qtbot.addWidget(grid)
    grid.set_columns(columns(2))
    src = FakeSource(100)
    grid.set_source(src)
    assert src.shown == []                     # not visible yet: no I/O
    grid.resize(400, 300)
    grid.show()
    qtbot.waitExposed(grid)
    assert src.shown and src.shown[-1][0] == 0


# ----------------------------------------------------------------------------- columns / header
def header_y(grid: VirtualGrid) -> int:
    return grid.header().height() // 2


def test_header_resize_by_drag_and_min_width(make_grid) -> None:
    grid, _ = make_grid(100)
    header = grid.header()
    changed = []
    grid.columns_changed.connect(lambda: changed.append(1))
    edge = QPoint(120, header_y(grid))
    assert header.edge_at(118) == 0 and header.edge_at(60) == -1
    QTest.mousePress(header, LEFT, NOMOD, edge)
    QTest.mouseMove(header, QPoint(160, edge.y()))
    QTest.mouseRelease(header, LEFT, NOMOD, QPoint(160, edge.y()))
    assert grid.columns()[0].width == 160
    assert changed == [1]
    assert grid.cell_rect(0, 1).x() == 160 + grid.gutter_width()
    QTest.mousePress(header, LEFT, NOMOD, QPoint(160, edge.y()))
    QTest.mouseMove(header, QPoint(-500, edge.y()))
    QTest.mouseRelease(header, LEFT, NOMOD, QPoint(-500, edge.y()))
    assert grid.columns()[0].width == MIN_COLUMN_WIDTH
    assert changed == [1, 1]


def test_header_double_click_edge_fits(make_grid) -> None:
    def make(row, col):
        return Cell(text=("x" * (row % 7 + 1)) + f"-{col.id}")

    grid, src = make_grid(100, source=FakeSource(100, make))
    changed = []
    grid.columns_changed.connect(lambda: changed.append(1))
    QTest.mouseDClick(grid.header(), LEFT, NOMOD, QPoint(120, header_y(grid)))
    fm = QFontMetrics(grid.grid_font())
    first, last = grid.visible_rows()
    widest = max(fm.horizontalAdvance(make(r, grid.columns()[0]).text) for r in range(first, last + 1))
    expected = max(grid.header().title_width(0), widest + 2 * grid._pad_x + 1)
    assert grid.columns()[0].width == max(MIN_COLUMN_WIDTH, expected)
    assert changed == [1]


def test_header_reorder_by_drag(make_grid) -> None:
    grid, _ = make_grid(100, ncols=4)
    grid.set_tree_column(0)
    grid.set_current(5, 0)
    header = grid.header()
    changed = []
    grid.columns_changed.connect(lambda: changed.append(1))
    y = header_y(grid)
    QTest.mousePress(header, LEFT, NOMOD, QPoint(60, y))
    QTest.mouseMove(header, QPoint(200, y))
    QTest.mouseMove(header, QPoint(310, y))
    assert header.drop_position() == 3
    header.grab()                               # paints the drag ghost + drop indicator
    QTest.mouseRelease(header, LEFT, NOMOD, QPoint(310, y))
    assert [c.id for c in grid.columns()] == ["c1", "c2", "c0", "c3"]
    assert changed == [1]
    assert grid.current() == (5, 2)            # the current cell follows its column
    assert grid.tree_column() == 2
    # API move back
    grid.move_column(2, 0)
    assert [c.id for c in grid.columns()] == ["c0", "c1", "c2", "c3"]
    assert grid.current() == (5, 0) and grid.tree_column() == 0
    assert changed == [1, 1]


def test_header_click_and_context_menu(make_grid, qtbot) -> None:
    grid, _ = make_grid(100)
    header = grid.header()
    changed = []
    grid.columns_changed.connect(lambda: changed.append(1))
    with qtbot.waitSignal(grid.header_clicked, timeout=1000) as blocker:
        QTest.mouseClick(header, LEFT, M.ShiftModifier, QPoint(180, header_y(grid)))
    col, mods = blocker.args
    assert col == 1 and bool(mods & M.ShiftModifier)
    assert changed == []                       # sorting is the owner's job
    gpos = header.mapToGlobal(QPoint(300, 5))
    with qtbot.waitSignal(grid.header_context_menu, timeout=1000) as blocker:
        QApplication.sendEvent(header, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(300, 5), gpos))
    assert blocker.args == [2, gpos]


def test_sort_indicators(make_grid, lang_en) -> None:
    grid, _ = make_grid(100, width=1000, col_width=220)
    header = grid.header()
    grid.set_sort_indicators([(2, -1), (0, 1)])
    cols = grid.columns()
    assert (cols[2].sort_order, cols[2].sort_priority) == (-1, 1)
    assert (cols[0].sort_order, cols[0].sort_priority) == (1, 2)
    assert cols[1].sort_order == 0 and cols[1].sort_priority == 0
    tip = header.tooltip_at(550)
    assert "Sorted descending" in tip and "key 1" in tip
    assert header.title_width(2) > header.title_width(1)   # room for arrow + priority
    header.grab()
    grid.set_sort_indicators([(1, 1)])
    assert header.tooltip_at(550) is None
    assert "key" not in header.tooltip_at(330)              # single key: no priority number
    grid.set_sort_indicators([])
    assert all(c.sort_order == 0 for c in grid.columns())
    grid.columns()[3].tooltip = "contact.address.city"
    assert header.tooltip_at(770) == "contact.address.city"
    grid.set_column_width(1, 40)                             # elided title -> the title is the tip
    assert header.tooltip_at(240) == "Column 1"


def test_header_visibility(make_grid) -> None:
    grid, _ = make_grid(100)
    h = grid.viewport().height()
    grid.set_header_visible(False)
    QApplication.processEvents()
    assert not grid.header().isVisible() and grid.viewport().height() > h
    grid.set_header_visible(True)
    QApplication.processEvents()
    assert grid.header().isVisible() and grid.viewport().height() == h
    assert grid.header().geometry().bottom() + 1 == grid.viewport().geometry().top()


def test_hidden_columns(make_grid) -> None:
    grid, src = make_grid(100, ncols=4)
    changed = []
    grid.columns_changed.connect(lambda: changed.append(1))
    grid.set_column_visible(1, False)
    assert changed == [1]
    assert [c.visible for c in grid.columns()] == [True, False, True, True]
    assert grid.cell_rect(0, 1).isNull()
    assert grid.cell_rect(0, 2).x() == 120
    assert grid.column_at(130) == 2 and grid.column_at(60) == 0
    src.asked.clear()
    grid.viewport().repaint()
    assert src.asked and all(cid != "c1" for _, cid in src.asked)
    grid.set_current(3, 1)                    # hidden -> falls back to a visible column
    assert grid.current()[1] != 1
    grid.set_column_visible(1, True)
    assert grid.cell_rect(0, 1).x() == 120 and changed == [1, 1]
    grid.set_column_width(1, 50)
    assert grid.cell_rect(0, 2).x() == 170 and changed == [1, 1, 1]


def test_frozen_column_stays_while_scrolling(make_grid) -> None:
    grid, _ = make_grid(100, ncols=8, col_width=150)
    grid.set_frozen_columns(1)
    QApplication.processEvents()
    hbar = grid.horizontalScrollBar()
    avail = grid.viewport().width() - grid.gutter_width()
    assert hbar.maximum() == 8 * 150 - avail
    grid.set_horizontal_offset(200)
    assert grid.horizontal_offset() == 200 and hbar.value() == 200
    assert grid.cell_rect(0, 0).x() == 0
    assert grid.cell_rect(0, 2).x() == 300 - 200
    assert grid.column_at(5) == 0 and grid.column_at(149) == 0
    assert grid.column_at(150) == 2            # column 1 is hidden under the frozen one
    assert grid.header().section_rect(0).x() == 0
    grid.viewport().repaint()
    grid.header().grab()
    # the scrolled-away part of column 1 cannot be resized through the frozen column
    assert grid.header().edge_at(100) == -1
    # cells mode: navigating to a column under the frozen one scrolls it into view
    grid.set_selection_mode("cells")
    grid.set_current(0, 1)
    assert grid.cell_rect(0, 1).x() >= 150
    grid.set_frozen_columns(0)
    grid.set_horizontal_offset(200)
    assert grid.cell_rect(0, 0).x() == -200
    # with a gutter the frozen column starts after it
    grid.set_frozen_columns(1)
    grid.set_row_labels(True)
    grid.set_horizontal_offset(100)
    assert grid.cell_rect(0, 0).x() == grid.gutter_width() > 0


def test_set_columns_keeps_rows_and_clamps(make_grid) -> None:
    grid, _ = make_grid(100, ncols=4)
    grid.set_current(7, 3)
    grid.set_columns(columns(2))
    assert grid.current() == (7, 0)
    assert grid.selected_ranges() == [(7, 7, 0, 1)]
    grid.set_columns([Column("tiny", "T", 3)])
    assert grid.columns()[0].width == MIN_COLUMN_WIDTH
    grid.columns()[0].title = "Renamed"
    grid.columns()[0].width = 300
    grid.refresh_columns()
    assert grid.cell_rect(7, 0).width() == 300


# ----------------------------------------------------------------------------- tree column
TYPES = ["object", "array", "string", "number", "bool", "null"]


def tree_cell(row: int, column: Column):
    if column.id == "c0":
        kind = TYPES[row % 6]
        return Cell(text=f"key_{row}", role="key", badge=kind, depth=row % 4,
                    expander=(1 if kind in ("object", "array") else 0), spans=((0, 3),) if row == 2 else ())
    return Cell(text=f"value {row}", role="number")


def test_tree_expander_click(make_grid, qtbot) -> None:
    grid, _ = make_grid(100, source=FakeSource(100, tree_cell))
    grid.set_tree_column(0)
    grid.set_current(10)
    ind, pad = grid.indent(), grid._pad_x
    for row in (0, 1, 6, 7):                   # rows with expanders at depth 0, 1, 2, 3
        rect = grid.expander_rect(row)
        cell = grid.cell_rect(row, 0)
        assert rect.isValid()
        assert rect.x() == cell.x() + pad + (row % 4) * ind and rect.width() == ind
        assert grid.row_at(rect.center().y()) == row and grid.column_at(rect.center().x()) == 0
        with qtbot.waitSignal(grid.expander_clicked, timeout=1000) as blocker:
            click(grid, rect.center())
        assert blocker.args == [row]
        assert grid.current() == (10, 0)       # no selection change
    assert grid.expander_rect(2).isNull()      # a string: no expander
    with qtbot.assertNotEmitted(grid.expander_clicked):
        click(grid, QPoint(grid.expander_rect(1).right() + 30, grid.cell_rect(1, 0).center().y()))
    assert grid.current() == (1, 0)
    with qtbot.waitSignal(grid.expander_clicked, timeout=1000):
        QTest.mouseDClick(grid.viewport(), LEFT, NOMOD, grid.expander_rect(6).center())


def test_geometry_is_consistent(make_grid) -> None:
    grid, _ = make_grid(10**9, ncols=5, source=FakeSource(10**9, tree_cell))
    grid.set_tree_column(0)
    grid.set_row_labels(True)
    grid.scroll_to(123_456_789, "top")
    grid.set_horizontal_offset(70)
    QApplication.processEvents()
    first, last = grid.visible_rows()
    for row in range(first, last + 1):
        for col in range(5):
            rect = grid.cell_rect(row, col)
            if rect.right() < grid.gutter_width() or rect.left() >= grid.viewport().width():
                continue
            x = max(rect.center().x(), grid.gutter_width())
            if rect.bottom() < grid.viewport().height():
                assert grid.row_at(rect.center().y()) == row
            if grid.column_at(x) != -1:
                assert grid.column_at(x) == col, (row, col)
    assert grid.cell_rect(first, 0).y() == 0
    assert grid.cell_rect(first + 3, 0).y() == 3 * grid.row_height()


def test_tree_indent_scales_with_font(make_grid) -> None:
    grid, _ = make_grid(10)
    small = grid.indent()
    grid.zoom(8)
    assert grid.indent() > small


# ----------------------------------------------------------------------------- painting
def test_placeholder_cells_and_empty_text(make_grid, qtbot, lang_en) -> None:
    grid, src = make_grid(source=FakeSource(100, lambda r, c: None))
    grid.viewport().repaint()                  # None cells paint the placeholder, no error
    assert src.calls > 0
    assert grid.tooltip_at(center(grid, 1, 1)) is None
    empty, _ = make_grid(source=FakeSource(0))
    assert empty.visible_rows() == (0, -1)
    empty.set_placeholder_text("Nothing here")
    image = empty.viewport().grab().toImage()
    base = empty.viewport().palette().color(QPalette.ColorRole.Base)
    assert any(image.pixelColor(x, y) != base
               for x in range(0, image.width(), 3) for y in range(image.height() // 2 - 10, image.height() // 2 + 10))


def test_broken_source_does_not_break_painting(make_grid, caplog) -> None:
    def boom(row, col):
        raise RuntimeError("bad cell")

    grid, _ = make_grid(source=FakeSource(10, boom))
    grid.viewport().repaint()
    grid.viewport().repaint()
    assert sum("cell() failed" in r.message for r in caplog.records) == 1


def test_tooltips(make_grid, qtbot) -> None:
    long_text = "lorem ipsum dolor " * 40

    def make(row, col):
        if row == 0:
            return Cell(text=long_text)
        if row == 1:
            return Cell(text="short", tooltip="explicit tip")
        if row == 2:
            return Cell(text="abcdefghijkl", bold=True)
        return Cell(text="ok")

    grid, _ = make_grid(source=FakeSource(5, make))
    tip = grid.tooltip_at(center(grid, 0, 0))
    assert tip.replace("\n", "") == long_text          # wrapped, nothing lost
    assert grid.tooltip_at(center(grid, 1, 0)) == "explicit tip"
    assert grid.tooltip_at(center(grid, 4, 0)) is None  # fits: no tooltip
    below = grid.cell_rect(4, 0).bottom() + 5
    assert grid.tooltip_at(QPoint(20, below)) is None   # no row there
    grid.set_column_width(0, 60)                         # elided -> the full text
    assert grid.tooltip_at(center(grid, 2, 0)) == "abcdefghijkl"
    grid.set_column_width(0, 400)
    assert grid.tooltip_at(center(grid, 2, 0)) is None
    for pos in (center(grid, 1, 1), QPoint(20, below)):  # real help events: shown / hidden
        QApplication.sendEvent(grid.viewport(), QHelpEvent(QEvent.Type.ToolTip, pos, grid.viewport().mapToGlobal(pos)))


def test_huge_tooltip_is_capped(make_grid) -> None:
    grid, _ = make_grid(source=FakeSource(3, lambda r, c: Cell(text="x" * 100_000)))
    tip = grid.tooltip_at(center(grid, 0, 0))
    assert 4000 <= len(tip.replace("\n", "")) <= 4001
    assert max(len(line) for line in tip.split("\n")) <= 120


def _find_color(image, rect, color: QColor, tolerance: int = 6) -> bool:
    dpr = image.devicePixelRatio()
    for y in range(int(rect.top() * dpr), int(rect.bottom() * dpr), 2):
        for x in range(int(rect.left() * dpr), int(rect.right() * dpr)):
            c = image.pixelColor(x, y)
            if abs(c.red() - color.red()) <= tolerance and abs(c.green() - color.green()) <= tolerance \
                    and abs(c.blue() - color.blue()) <= tolerance:
                return True
    return False


@pytest.fixture
def themed(qapp):
    palette = QPalette(qapp.palette())
    style = qapp.style().name()
    yield qapp
    theme._reset_for_tests()
    qapp.styleHints().unsetColorScheme()
    qapp.setStyle(style)
    qapp.setPalette(palette)
    theme.invalidate()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_paint_smoke(make_grid, themed, scheme) -> None:
    theme.apply_theme(themed, scheme)
    grid, src = make_grid(10**12, ncols=5, width=900, col_width=320, source=FakeSource(10**12, tree_cell))
    grid.set_tree_column(0)
    grid.set_row_labels(True)
    grid.set_frozen_columns(1)
    grid.set_grid_lines(True)
    grid.columns()[1].align = "right"
    grid.set_sort_indicators([(1, 1), (2, -1)])
    grid.set_current(2, 0)
    grid.setFocus()
    grid.set_horizontal_offset(50)
    QApplication.processEvents()
    image = grid.viewport().grab().toImage()
    assert not image.isNull()
    # the span of row 2 ("key" of key_2) is highlighted with the theme's match color
    assert _find_color(image, grid.cell_rect(2, 0), theme.color("match"))
    # a badge is drawn in its type color (row 3 = number)
    assert _find_color(image, grid.cell_rect(3, 0), theme.color("number"), tolerance=40)
    # base background
    base = grid.viewport().palette().color(QPalette.ColorRole.Base)
    assert base.name() == theme.colors(scheme)["panel"].name()
    grid.header().grab()
    grid.set_selection_mode("cells")
    grid.select_all()
    grid.viewport().grab()


def test_theme_change_repaints(make_grid, themed) -> None:
    theme.apply_theme(themed, "light")
    grid, _ = make_grid(2)                      # two rows: empty area below
    y = grid.viewport().height() - 2
    assert grid.viewport().grab().toImage().pixelColor(300, y).name() == "#ffffff"
    theme.apply_theme(themed, "dark")
    QApplication.processEvents()
    assert grid._colors is None or grid._colors.base.name() == "#151a22"
    assert grid.viewport().grab().toImage().pixelColor(300, y).name() == "#151a22"


def test_gutter_adapts_to_labels(make_grid) -> None:
    grid, _ = make_grid(10**12)
    grid.set_row_labels(True)
    narrow = grid.gutter_width()
    assert narrow > 0
    grid.scroll_to(10**12 - 1)
    wide = grid.gutter_width()
    assert wide > narrow
    assert grid.cell_rect(10**12 - 1, 0).x() == wide
    assert grid.header().section_rect(0).x() == wide
    grid.set_row_labels(False)
    assert grid.gutter_width() == 0


def test_alternating_and_selection_colors(make_grid, themed) -> None:
    theme.apply_theme(themed, "light")
    grid, _ = make_grid(source=FakeSource(20, lambda r, c: Cell(text="")))
    grid.set_alternating(True)
    image = grid.viewport().grab().toImage()
    pal = grid.viewport().palette()
    x = grid.cell_rect(0, 0).center().x()
    assert image.pixelColor(x, grid.cell_rect(0, 0).center().y()) == pal.color(QPalette.ColorRole.Base)
    assert image.pixelColor(x, grid.cell_rect(1, 0).center().y()) == pal.color(QPalette.ColorRole.AlternateBase)
    grid.set_current(4)
    image = grid.viewport().grab().toImage()
    selected = image.pixelColor(x, grid.cell_rect(4, 0).center().y())
    assert selected not in (pal.color(QPalette.ColorRole.Base), pal.color(QPalette.ColorRole.AlternateBase))


def test_painting_a_page_is_fast(make_grid) -> None:
    def make(row, col):
        if col.id == "c0":
            return Cell(text=f"field_{row}", role="key", badge=TYPES[row % 6], depth=row % 5, expander=row % 3,
                        spans=((1, 4),) if row % 7 == 0 else ())
        return Cell(text=f"value {row} " * (1 + row % 9), role=TYPES[row % 6], align="right" if col.id == "c3" else "left")

    grid, src = make_grid(10**9, ncols=6, width=900, source=FakeSource(10**9, make))
    grid.set_tree_column(0)
    grid.set_row_labels(True)
    grid.resize(900, 60 * grid.row_height() + grid.header().height() + 40)
    QApplication.processEvents()
    assert window(grid) >= 60
    grid.viewport().repaint()                  # warm the caches
    runs = 10
    start = time.perf_counter()
    for i in range(runs):
        grid.scroll_to(1000 * i, "top")
        grid.viewport().repaint()
    per_page = (time.perf_counter() - start) / runs
    assert per_page < 0.25, f"painting a page took {per_page * 1000:.1f} ms"


def test_loading_expander_tooltip(make_grid, lang_en) -> None:
    grid, _ = make_grid(source=FakeSource(3, lambda r, c: Cell(text="x", expander=3)))
    grid.set_tree_column(0)
    assert grid.tooltip_at(grid.expander_rect(0).center()) == "Loading…"


def test_alignment_and_options(make_grid) -> None:
    def make(row, col):
        return Cell(text="12", align="right" if col.id == "c1" else "left")

    grid, _ = make_grid(source=FakeSource(5, make))
    grid.columns()[2].align = "center"
    grid.viewport().repaint()
    with pytest.raises(ValueError):
        grid.set_selection_mode("columns")
    grid.set_tree_column(9)                     # out of range -> no tree column
    assert grid.tree_column() is None
    grid.set_tree_column(1)
    assert grid.tree_column() == 1
    grid.set_tree_column(None)
    assert grid.tree_column() is None
    grid.set_frozen_columns(5)
    assert grid.frozen_columns() == 1
    grid.set_alternating(False)
    grid.set_grid_lines(True)
    grid.viewport().repaint()


def test_selected_row_count_merges_overlaps(make_grid) -> None:
    grid, _ = make_grid(100)
    grid.set_current(10)
    click(grid, center(grid, 14, 0), M.ShiftModifier)          # 10..14
    click(grid, center(grid, 12, 0), M.ControlModifier)        # toggle 12 off
    click(grid, center(grid, 16, 0), M.ControlModifier)        # + 16
    click(grid, center(grid, 11, 0), M.ControlModifier | M.ShiftModifier)  # 16..11 on top
    rows = {r for r0, r1, *_ in grid.selected_ranges() for r in range(r0, r1 + 1)}
    assert rows == set(range(10, 17))
    assert grid.selected_row_count() == 7


def test_selection_mode_switch_keeps_current(make_grid) -> None:
    grid, _ = make_grid(100)
    grid.set_current(5, 2)
    grid.set_selection_mode("cells")
    assert grid.selected_ranges() == [(5, 5, 2, 2)]
    grid.set_selection_mode("single")
    assert grid.selected_ranges() == [(5, 5, 0, 3)]
    grid.set_selection_mode("rows")
    assert grid.selected_ranges() == [(5, 5, 0, 3)]


def test_deleted_grid_ignores_theme_changes(qtbot) -> None:
    import shiboken6

    grid = VirtualGrid()
    grid.set_columns(columns(2))
    shiboken6.delete(grid)
    theme.notifier().theme_changed.emit()      # must not touch the deleted widget
    theme.invalidate()


def test_header_drag_with_frozen_column_and_autoscroll(make_grid) -> None:
    grid, _ = make_grid(100, ncols=8, col_width=150)
    grid.set_frozen_columns(1)
    grid.set_horizontal_offset(150)              # column 1 is now completely under the frozen one
    header = grid.header()
    y = header_y(grid)
    # drag column 3 onto the left half of the first scrollable column on screen
    start = grid.cell_rect(0, 3).center().x()
    QTest.mousePress(header, LEFT, NOMOD, QPoint(start, y))
    QTest.mouseMove(header, QPoint(start - 40, y))
    QTest.mouseMove(header, QPoint(160, y))
    assert header.drop_position() == 2
    QTest.mouseMove(header, QPoint(header.width() - 5, y))    # near the right edge: auto-scroll
    off = grid.horizontal_offset()
    header._auto_tick()
    assert grid.horizontal_offset() > off
    QTest.mouseRelease(header, LEFT, NOMOD, QPoint(header.width() - 5, y))
    assert header.drop_position() == -1
    assert [c.id for c in grid.columns()][0] == "c0"          # the frozen column stays first


# ----------------------------------------------------------------------------- fix round (FX3)
def test_header_extra_strip_api(make_grid) -> None:
    """K1: a public strip under the header (the table's quick filter row uses it instead of
    overriding the grid's private layout methods)."""
    from PySide6.QtWidgets import QWidget

    grid, _ = make_grid(100, ncols=4, col_width=200)
    header = grid.header()
    plain_top = grid.viewport().geometry().top()
    moved: list[int] = []
    grid.header_geometry_changed.connect(lambda: moved.append(1))
    strip = QWidget()
    grid.set_header_extra(strip, 30)
    QApplication.processEvents()
    assert grid.header_extra() is strip and strip.parentWidget() is grid and strip.isVisible()
    assert grid.header_extra_visible() and grid.header_extra_height() == 30
    g = strip.geometry()
    assert g.top() == header.geometry().bottom() + 1 and g.height() == 30
    assert (g.x(), g.width()) == (header.geometry().x(), header.geometry().width())
    assert grid.viewport().geometry().top() == g.bottom() + 1 == plain_top + 30
    assert moved                                            # placed -> signal
    for action in (lambda: grid.set_column_width(1, 260), lambda: grid.set_horizontal_offset(120),
                   lambda: grid.move_column(0, 2), lambda: grid.resize(700, 420)):
        moved.clear()
        action()
        QApplication.processEvents()
        assert moved, "header_geometry_changed must follow every section move"
    grid.set_header_extra_visible(False)
    QApplication.processEvents()
    assert strip.isHidden() and grid.header_extra_height() == 0 and not grid.header_extra_visible()
    assert grid.viewport().geometry().top() == header.geometry().bottom() + 1
    grid.set_header_extra(strip, 44)                        # same widget: only the height changes
    QApplication.processEvents()
    assert strip.isHidden() and grid.header_extra_height() == 0
    grid.set_header_extra_visible(True)
    QApplication.processEvents()
    assert strip.geometry().height() == 44 and grid.viewport().geometry().top() == strip.geometry().bottom() + 1
    grid.set_header_visible(False)                          # the strip stays without a header
    QApplication.processEvents()
    assert strip.geometry().top() == grid.viewport().geometry().top() - 44
    grid.set_header_visible(True)
    grid.set_header_extra(None)
    QApplication.processEvents()
    assert grid.header_extra() is None and strip.isHidden()
    assert grid.viewport().geometry().top() == plain_top


def _distance(a: QColor, b: QColor) -> int:
    return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("mode", ["rows", "cells"])
def test_selection_stays_visible_without_focus(make_grid, qtbot, themed, scheme, mode) -> None:
    """C2: the selected row / cell of a grid without focus is clearly tinted and the
    current one keeps a (softer) accent outline."""
    from PySide6.QtWidgets import QLineEdit

    theme.apply_theme(themed, scheme)
    grid, _ = make_grid(source=FakeSource(20, lambda r, c: Cell(text="")), mode=mode)
    other = QLineEdit()
    other.show()
    grid.set_current(4, 1)
    other.setFocus()
    QApplication.processEvents()
    assert not grid.hasFocus()
    image = grid.viewport().grab().toImage()
    pal = grid.viewport().palette()
    base, alt = pal.color(QPalette.ColorRole.Base), pal.color(QPalette.ColorRole.AlternateBase)
    rect = grid.cell_rect(4, 1)
    inside = image.pixelColor(rect.center().x(), rect.center().y())
    assert _distance(inside, base) >= 40 and _distance(inside, alt) >= 30, (inside.name(), base.name(), alt.name())
    hover = grid._palette_colors().hover
    assert _distance(inside, hover) >= 25
    edge = image.pixelColor(rect.center().x(), rect.top())             # the outline of the current cell / row
    colors = grid._palette_colors()
    assert _distance(edge, colors.focus_inactive) <= 6, (edge.name(), colors.focus_inactive.name())
    assert _distance(colors.focus_inactive, colors.focus) >= 30           # softer than the focused outline
    other.close()
    grid.activateWindow()
    grid.setFocus()
    qtbot.waitUntil(grid.hasFocus, timeout=3000)
    focused = grid.viewport().grab().toImage()
    assert _distance(focused.pixelColor(rect.center().x(), rect.top()), colors.focus) <= 6


def test_set_font_can_keep_the_zoom(make_grid) -> None:
    grid, _ = make_grid(100)
    font = grid.grid_font()
    font.setPointSizeF(10)
    grid.set_font(font)
    grid.zoom(3)
    assert grid.grid_font().pointSizeF() == 13
    new = grid.grid_font()
    new.setFamily("Courier New")
    new.setPointSizeF(12)
    grid.set_font(new, keep_zoom=True)
    assert grid.zoom_level() == 3 and grid.grid_font().pointSizeF() == 15
    assert grid.base_font().pointSizeF() == 12
    grid.zoom(0)
    assert grid.grid_font().pointSizeF() == 12
    grid.zoom(2)
    grid.set_font(new)                                   # default: the zoom is reset
    assert grid.zoom_level() == 0 and grid.grid_font().pointSizeF() == 12
