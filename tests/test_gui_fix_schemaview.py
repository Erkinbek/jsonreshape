"""Second fix round for the schema view (jsonreshape/gui/schemaview.py).

* R2-8: the rows of a schema (up to 20 000 fields) are built in timed batches, the old
  rows are deleted in timed batches, the natural order needs no sort and a sort compares
  without asking the tree for its column -- no GUI-thread step of the view comes near
  100 ms; the selection, the sort column and the search survive a new schema; the old
  rows are freed;
* R2-18: the type badge keeps its look on a selected row (an opaque chip of the base
  color under the tint) in both themes -- the dark theme's selection color is the blue of
  numbers / objects and hid the badge.
"""

from __future__ import annotations

import gc
import json
import time
import weakref
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape.gui import fields, schemaview, theme  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.schemaview import COL_PRESENCE, SchemaView, badge_colors  # noqa: E402
from jsonreshape.gui.session import DocSession  # noqa: E402
from jsonreshape.schema import SchemaBuilder  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000
#: A GUI-thread step of the view must stay far below this (its budget is FILL_BUDGET_S).
STEP_LIMIT_S = 0.1


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _collect_on_the_gui_thread():
    """Cyclic garbage of these tests (closed sessions, views and their wrappers) is
    collected here, on the GUI thread, not later by a worker thread that happens to
    trigger a collection (Qt objects must be deleted on their own thread)."""
    yield
    QApplication.processEvents()          # deleteLater() of the closed widgets
    gc.collect()


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def session(qtbot, app, tmp_path: Path):
    path = tmp_path / "small.json"
    path.write_text(json.dumps([{"a": 1}, {"a": 2}]), encoding="utf-8")
    s = DocSession(app, path, use_cache=False)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    yield s
    s.close()
    assert app.tasks.wait_idle(30)


def big_schema(value: int = 0):
    """20 000 fields: 1000 top-level objects with 19 members each."""
    b = SchemaBuilder()
    b.add({f"a{i}": {f"b{j}": j + value for j in range(19)} for i in range(1000)})
    b.add({f"a{i}": {f"b{j}": j for j in range(19 - i % 7)} for i in range(1000)})   # presences differ
    sch = b.build()
    assert len(sch.root.children) == 1000
    return sch


@pytest.fixture
def step_times(monkeypatch):
    """Durations of every GUI-thread step of the view (fill, trash; the swap runs in the
    last fill step)."""
    times: list[float] = []

    def timed(name):
        orig = getattr(SchemaView, name)

        def run(self, *a):
            t0 = time.perf_counter()
            try:
                return orig(self, *a)
            finally:
                times.append(time.perf_counter() - t0)

        monkeypatch.setattr(SchemaView, name, run)

    timed("_fill_step")
    timed("_trash_step")
    gc.collect()
    gc.disable()          # the steps' own time, not collector pauses for garbage of earlier tests
    try:
        yield times
    finally:
        gc.enable()


def make_view(qtbot, session) -> SchemaView:
    view = SchemaView(session)
    qtbot.addWidget(view)
    view.resize(1000, 600)
    view.show()
    qtbot.waitExposed(view)
    return view


# ----------------------------------------------------------------------------------
# R2-8: timed batches
# ----------------------------------------------------------------------------------
def test_twenty_thousand_fields_are_shown_in_timed_steps(qtbot, app, session, monkeypatch, step_times,
                                                          lang_uz) -> None:
    schema = big_schema()
    monkeypatch.setattr(fields, "get_schema", lambda *a, **k: schema)
    view = make_view(qtbot, session)
    t0 = time.perf_counter()
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        view.activate()
        call = time.perf_counter() - t0
        assert view.filling and view.items() == [] and call < STEP_LIMIT_S
        assert view.stack.currentIndex() == 0 and "ko'rsatilmoqda" in view.message.text()
        assert "20 000 ta maydon" in view.header_label.text()
    assert not view.filling and view.schema is schema
    assert len(view.items()) == 20000 and view.tree.topLevelItemCount() == 1000
    assert view.stack.currentIndex() == 1
    assert len(step_times) > 5 and max(step_times) < STEP_LIMIT_S, sorted(step_times)[-3:]
    assert view.item_for("a999.b18") is not None


def test_a_new_schema_keeps_selection_sort_and_search_and_frees_the_old_rows(qtbot, app, session, monkeypatch,
                                                                             step_times) -> None:
    first, second = big_schema(), big_schema(1)
    current = {"schema": first}
    monkeypatch.setattr(fields, "get_schema", lambda *a, **k: current["schema"])
    view = make_view(qtbot, session)
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        view.activate()
    view.tree.sortByColumn(COL_PRESENCE, Qt.SortOrder.AscendingOrder)
    view.select_field("a7.b2")
    view.search_edit.setText("a7.")
    old = [weakref.ref(item) for item in view.items()]
    step_times.clear()
    current["schema"] = second
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        view.full_scan()                         # a cached "full" schema: shown at once
    assert view.schema is second and len(view.items()) == 20000
    assert view.current_field().text == "a7.b2" and view.current_field() is second.find("a7.b2")
    assert view.tree.sortColumn() == COL_PRESENCE
    tops = [view.tree.topLevelItem(i).fi.presence for i in range(view.tree.topLevelItemCount())]
    assert tops == sorted(tops)
    shown = [it.fi.text for it in view.items() if not it.isHidden()]
    assert shown[0] == "a7" and all(t.startswith("a7") for t in shown)
    qtbot.waitUntil(lambda: not view._trash, timeout=TIMEOUT)
    assert max(step_times) < STEP_LIMIT_S, sorted(step_times)[-3:]
    gc.collect()
    assert sum(r() is not None for r in old) == 0          # every old row deleted


def test_the_natural_order_needs_no_sort_and_sorts_compare_cheaply(qtbot, app, session, monkeypatch) -> None:
    calls = [0]

    class Counting(schemaview._FieldItem):
        def __lt__(self, other):
            calls[0] += 1
            return super().__lt__(other)

    monkeypatch.setattr(schemaview, "_FieldItem", Counting)
    schema = big_schema()
    monkeypatch.setattr(fields, "get_schema", lambda *a, **k: schema)
    view = make_view(qtbot, session)
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        view.activate()
    assert calls[0] == 0                                     # built in schema order: no sort
    texts = [view.tree.topLevelItem(i).fi.text for i in range(5)]
    assert texts == ["a0", "a1", "a2", "a3", "a4"]
    view.tree.sortByColumn(COL_PRESENCE, Qt.SortOrder.DescendingOrder)     # a header click
    assert calls[0] > 0
    tops = [view.tree.topLevelItem(i).fi.presence for i in range(view.tree.topLevelItemCount())]
    assert tops == sorted(tops, reverse=True)
    kids = view.item_for("a3")
    got = [kids.child(i).fi.presence for i in range(kids.childCount())]
    assert got == sorted(got, reverse=True)
    view.restore_order()
    assert [view.tree.topLevelItem(i).fi.text for i in range(5)] == texts


# ----------------------------------------------------------------------------------
# R2-18: badges on selected rows
# ----------------------------------------------------------------------------------
def _over(top: QColor, bottom: QColor) -> QColor:
    a = top.alphaF()
    return QColor.fromRgbF(top.redF() * a + bottom.redF() * (1 - a), top.greenF() * a + bottom.greenF() * (1 - a),
                           top.blueF() * a + bottom.blueF() * (1 - a))


def _contrast(a: QColor, b: QColor) -> float:
    def lum(c: QColor) -> float:
        def ch(v: float) -> float:
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_type_badges_stay_readable_on_a_selected_row(qapp, qtbot, session, scheme) -> None:
    previous = theme.mode()
    theme.apply_theme(qapp, scheme)
    try:
        pal = qapp.palette()
        base = pal.color(QPalette.ColorRole.Base)
        highlight = pal.color(QPalette.ColorRole.Highlight)
        old = []
        for kind in theme.TYPES:
            chip, tint, ink = badge_colors(kind, True, base)
            assert chip is not None and chip.alpha() == 255
            assert _contrast(ink, _over(tint, chip)) >= 3.0, kind
            plain_chip, plain_tint, plain_ink = badge_colors(kind, False, base)
            assert plain_chip is None and plain_tint == tint and plain_ink == ink
            old.append(_contrast(ink, _over(tint, highlight)))       # the badge drawn on the selection
        assert min(old) < 1.5                                          # what made it unreadable
        # on screen: the selected row's badge sits on the chip, not on the selection color
        view = make_view(qtbot, session)
        with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
            view.activate()
        view.tree.setFocus()
        view.select_field("a")
        item = view.item_for("a")
        kind = item.fi.main_type
        rect = view.tree.visualItemRect(item)
        img = view.tree.viewport().grab().toImage()
        pixel = img.pixelColor(rect.left() + 4 + 3, rect.center().y())
        chip, tint, _ink = badge_colors(kind, True, base)
        want = _over(tint, chip)

        def dist(a: QColor, b: QColor) -> int:
            return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())

        assert dist(pixel, want) < 30, (pixel.name(), want.name())
        assert dist(pixel, highlight) > 60
        view.shutdown()
    finally:
        theme.apply_theme(qapp, previous)
