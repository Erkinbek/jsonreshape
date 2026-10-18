"""Regression tests of the tree view fix round (FX2): reveal state, placeholder, quick find
over what the rows show, node sorting off the GUI thread (progress, cancel, O(1) display
orders), throttled position lookups, the non-modal "expand many rows?" question, the
breadcrumb layout / colors and the data font following the settings.

Findings: A-F5, A-F6, C4, C7/M7, C8, C11, P-F5, P-F10, P-F11, T7 (see the review notes).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from jsonreshape.gui import flattree, theme, widgets  # noqa: E402
from jsonreshape.gui import session as session_mod  # noqa: E402
from jsonreshape.gui import treemodel  # noqa: E402
from jsonreshape.gui import treeview as tv  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef  # noqa: E402
from jsonreshape.gui.treeview import Breadcrumb, TreeView  # noqa: E402
from jsonreshape.reader import Node  # noqa: E402
from jsonreshape.tasks import check  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000


# ----------------------------------------------------------------------------------
# fixtures / helpers (as in test_gui_treeview.py)
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_modal_confirm(monkeypatch):
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.statuses = []
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def opened(qtbot, app):
    made: list = []

    def open_file(path: Path, *, wait: bool = True, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        if wait:
            with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
                s.start()
        else:
            with qtbot.waitSignal(s.opened, timeout=TIMEOUT):
                s.start()
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def views(qtbot):
    made: list[TreeView] = []

    def make(session: DocSession, *, activate: bool = True) -> TreeView:
        view = TreeView(session)
        qtbot.addWidget(view)
        view.resize(1000, 600)
        view.show()
        made.append(view)
        if activate:
            view.activate()
        return view

    yield make
    for v in made:
        v.shutdown()


def write(path: Path, value) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def people(n: int) -> list[dict]:
    return [{"id": i, "name": f"user{i}", "x": {"y": i}} for i in range(n)]


def wait_text(qtbot, view: TreeView, row: int, col: str) -> str:
    qtbot.waitUntil(lambda: row < view.row_count() and view.cell_text(row, col) is not None, timeout=TIMEOUT)
    return view.cell_text(row, col)


def wait_idle(qtbot, view: TreeView) -> None:
    qtbot.waitUntil(lambda: not view.busy(), timeout=TIMEOUT)


def wait_view(qtbot, s: DocSession) -> None:
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)


def expand(qtbot, view: TreeView, kp: tuple) -> None:
    view.expand_node(kp)
    qtbot.waitUntil(lambda: view.flat.is_expanded(kp) and not view.is_loading(kp), timeout=TIMEOUT)


def count_tasks(app, monkeypatch, kind: str) -> list:
    """Record every task of ``kind`` started through the app's task manager."""
    started: list = []
    orig = app.tasks.run

    def run(fn, **kw):
        task = orig(fn, **kw)
        if kw.get("kind") == kind:
            started.append(task)
        return task

    monkeypatch.setattr(app.tasks, "run", run)
    return started


# ----------------------------------------------------------------------------------
# A-F5: a reveal that ends without its node does not block the next one
# ----------------------------------------------------------------------------------
def test_reveal_not_in_view_then_again_after_reset(qtbot, app, opened, views, tmp_path: Path, lang_uz) -> None:
    s = opened(write(tmp_path / "a.json", people(400)))
    view = views(s)
    ref = NodeRef(300, ("x",))
    s.select(ref, reveal=True)
    qtbot.waitUntil(lambda: view.current_keypath() == (300, 2), timeout=TIMEOUT)
    wait_idle(qtbot, view)
    s.set_filter(FilterSpec("id < 5"))
    wait_view(qtbot, s)
    app.statuses.clear()
    s.select(ref, reveal=True)                     # not in the filtered view
    qtbot.waitUntil(lambda: "Bu yozuv joriy ko'rinishda yo'q" in app.statuses, timeout=TIMEOUT)
    assert view._reveal_ref is None                # the reveal ended: nothing "on its way"
    s.reset_view()
    wait_view(qtbot, s)
    qtbot.waitUntil(lambda: view.row_count() > 300, timeout=TIMEOUT)
    wait_idle(qtbot, view)
    view.grid.scroll_to(0, "top")
    assert view.grid.visible_rows()[1] < 300
    s.select(ref, reveal=True)                     # the same node again: shown and centered
    row = view.row_of((300, 2))

    def shown() -> bool:
        first, last = view.grid.visible_rows()
        return row is not None and first <= row <= last

    qtbot.waitUntil(shown, timeout=TIMEOUT)
    assert view.grid.current()[0] == row and view._reveal_ref is None


def test_reveal_chain_ends_when_children_task_is_cancelled(qtbot, app, opened, views, tmp_path: Path,
                                                           monkeypatch) -> None:
    app.settings.large_node_bytes = 1000
    s = opened(write(tmp_path / "c.json", [{"big": list(range(3000))}, {"z": 1}]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    wait_text(qtbot, view, 0, "key")
    orig = Node.child_count
    release = threading.Event()

    def slow_count(self, progress=None, cancel=None):
        while cancel is not None and not release.is_set():
            check(cancel)
            time.sleep(0.005)
        return orig(self, progress=progress, cancel=cancel)

    monkeypatch.setattr(Node, "child_count", slow_count)
    ref = NodeRef(0, ("big", 5), (0, 5))          # with its ipath: no resolving (it would count too)
    s.select(ref, reveal=True)
    qtbot.waitUntil(lambda: any(t.kind == "children" for t in app.tasks.tasks(owner=view)), timeout=TIMEOUT)
    assert view._reveal_ref is not None
    next(t for t in app.tasks.tasks(owner=view) if t.kind == "children").cancel()
    qtbot.waitUntil(lambda: view._reveal_ref is None, timeout=TIMEOUT)   # ended with the cancelled task
    release.set()
    s.select(ref, reveal=True)                     # not ignored as "already on its way"
    qtbot.waitUntil(lambda: view.current_keypath() == (0, 0, 5), timeout=TIMEOUT)
    assert not app.errors


def test_expand_node_reports_failure_paths(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "e.json", [{"a": 1, "b": []}]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 1, timeout=TIMEOUT)
    expand(qtbot, view, (0,))
    wait_text(qtbot, view, 1, "key")
    wait_text(qtbot, view, 2, "key")
    calls: list[str] = []
    assert not view.expand_node((0, 0), then=lambda: calls.append("then"), otherwise=lambda: calls.append("no"))
    assert not view.expand_node((0, 1), then=lambda: calls.append("then"), otherwise=lambda: calls.append("no"))
    assert view.expand_node((0,), then=lambda: calls.append("then"), otherwise=lambda: calls.append("no"))
    assert calls == ["no", "no", "then"]           # a scalar, an empty array, an expanded node


# ----------------------------------------------------------------------------------
# A-F6: the placeholder of a tree made after the open failed
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("content", ["", "   \n"])
def test_placeholder_when_open_failed_before_the_view(qtbot, app, views, tmp_path: Path, lang_uz,
                                                      content: str) -> None:
    p = tmp_path / "e.json"
    p.write_text(content, encoding="utf-8")
    s = DocSession(app, p, use_cache=False)
    s.start()
    qtbot.waitUntil(lambda: s.state in ("ready", "failed"), timeout=TIMEOUT)
    assert s.state == "failed"
    view = views(s, activate=False)
    assert view.placeholder_text() == "Faylni ochib bo'lmadi"
    view.activate()
    assert view.placeholder_text() == "Faylni ochib bo'lmadi" and view.row_count() == 0
    s.close()


# ----------------------------------------------------------------------------------
# C4: quick find finds what the rows show
# ----------------------------------------------------------------------------------
def test_quick_find_searches_visible_previews_even_when_capped(qtbot, app, opened, views, tmp_path: Path,
                                                               monkeypatch) -> None:
    s = opened(write(tmp_path / "p.json", people(300)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 300, timeout=TIMEOUT)
    expand(qtbot, view, (0,))                      # 3 rows below #0 ...
    monkeypatch.setattr(tv, "QUICK_SCAN_ROWS", 2)   # ... more than the scan budget: capped
    view.grid.scroll_to(view.row_count() - 1, "bottom")
    last = view.row_count() - 1
    wait_text(qtbot, view, last, "value")
    view.find_bar.open_bar("user295")
    qtbot.waitUntil(lambda: (295,) in view.quick_find_matches(), timeout=TIMEOUT)
    row = view.row_of((295,))
    cell = view.cell_at(row, "value")
    at = cell.text.index("user295")
    assert cell.spans and cell.spans[0][:2] == (at, at + 7)
    assert "+" in view.find_bar.status_text()      # the expanded part was capped


def test_quick_find_in_filtered_view_needs_no_position_lookup(qtbot, app, opened, views, tmp_path: Path,
                                                              monkeypatch) -> None:
    s = opened(write(tmp_path / "p.json", people(300)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 300, timeout=TIMEOUT)
    s.set_filter(FilterSpec("id >= 100"))
    wait_view(qtbot, s)
    qtbot.waitUntil(lambda: view.row_count() == 200, timeout=TIMEOUT)
    assert not s.view_is_base
    wait_text(qtbot, view, 5, "value")
    wait_idle(qtbot, view)
    locates = count_tasks(app, monkeypatch, "locate")
    view.find_bar.open_bar("user105")
    qtbot.waitUntil(lambda: set(view.quick_find_matches()) == {(105,)}, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.grid.current()[0] == 5, timeout=TIMEOUT)   # #105 is row 5 of the view
    assert view.find_bar.status_text() == "1 / 1"
    assert locates == []                           # rows on screen: positions already known


# ----------------------------------------------------------------------------------
# C8 / P-F5 / P-F11: element sort with progress and cancel; orders built off the GUI thread
# ----------------------------------------------------------------------------------
def big_array_doc(tmp_path: Path, n: int) -> Path:
    items = ",".join('{"v":%d}' % ((k * 7919) % 1000) for k in range(n))
    path = tmp_path / "arr.json"
    path.write_text('[{"items":[' + items + ']},{"z":1}]', encoding="utf-8")
    return path


def test_element_sort_progress_cancel_and_orders_off_the_gui_thread(qtbot, app, opened, views,
                                                                     tmp_path: Path, monkeypatch) -> None:
    n = tv.BIG_TASK_CHILDREN + 5_000               # a visible task
    s = opened(big_array_doc(tmp_path, n))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    expand(qtbot, view, (0,))
    kp = (0, 0)
    expand(qtbot, view, kp)
    inverse_on_gui: list[int] = []
    orig_inverse = flattree._inverse

    def spy_inverse(order):
        if threading.current_thread() is threading.main_thread():
            inverse_on_gui.append(len(order))
        return orig_inverse(order)

    monkeypatch.setattr(flattree, "_inverse", spy_inverse)
    added: list = []
    app.tasks.task_added.connect(added.append)
    assert view.sort_elements(kp, "v")
    qtbot.waitUntil(lambda: view.element_sort(kp) == ("v", False) and not view.is_loading(kp)
                    and kp in view._orders, timeout=TIMEOUT)
    task = next(t for t in added if t.kind == "children")
    info = task.info()
    assert info is not None and info.unit == "items" and info.total == n and info.done == n
    assert task.title.startswith("Elementlar tartiblanmoqda") or task.title.startswith("Sorting elements")

    def shown_values(k: int = 6) -> list[int]:
        return [int(view.cell_text(r, "value").split(": ")[1]) for r in range(3, 3 + k)]

    qtbot.waitUntil(lambda: all(view.cell_text(r, "value") for r in range(3, 9)), timeout=TIMEOUT)
    assert shown_values() == sorted(shown_values()) and shown_values()[0] == 0
    # collapse / expand / original order / sort keys: no O(n) order work on the GUI thread
    view.collapse_node(kp)
    t0 = time.perf_counter()
    assert view.expand_node(kp) is True
    assert time.perf_counter() - t0 < 0.5
    assert inverse_on_gui == []
    # cancel a running sort: the previous sort stays (order, state, menu)
    release = threading.Event()
    real_argsort = treemodel.stable_argsort

    def blocked(keys, cancel=None, progress=None, **kw):
        while not release.is_set():
            check(cancel)
            time.sleep(0.005)
        return real_argsort(keys, cancel, progress, **kw)

    monkeypatch.setattr(treemodel, "stable_argsort", blocked)
    added.clear()
    assert view.sort_elements(kp, "v", descending=True)
    qtbot.waitUntil(lambda: any(t.kind == "children" for t in added), timeout=TIMEOUT)
    assert view.element_sort(kp) == ("v", True) and view.is_loading(kp)
    next(t for t in added if t.kind == "children").cancel()
    qtbot.waitUntil(lambda: not view.is_loading(kp), timeout=TIMEOUT)
    release.set()
    assert view.element_sort(kp) == ("v", False)   # back to the sort still shown
    assert shown_values() == sorted(shown_values())
    assert kp in view._orders and not app.errors
    assert inverse_on_gui == []


# ----------------------------------------------------------------------------------
# P-F10: position lookups while a filter grows
# ----------------------------------------------------------------------------------
def test_locate_tasks_are_throttled_while_a_filter_grows(qtbot, app, opened, views, tmp_path: Path,
                                                         monkeypatch) -> None:
    orig = session_mod.filter_records

    def slow(doc, flt, source=None, *, progress=None, cancel=None, on_batch=None, **kw):
        def batch(rs):
            on_batch(rs)
            time.sleep(0.03)

        return orig(doc, flt, source, progress=progress, cancel=cancel, on_batch=batch if on_batch else None,
                    batch_interval=0.0, block_bytes=32 * 1024)

    monkeypatch.setattr(session_mod, "filter_records", slow)
    monkeypatch.setattr(DocSession, "COUNT_POLL_MS", 25)
    path = tmp_path / "g.json"
    path.write_bytes(b"[" + b",".join(b'{"i":%d,"g":%d}' % (i, i % 2) for i in range(60_000)) + b"]")
    s = opened(path)
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 60_000, timeout=TIMEOUT)
    for r in (0, 1, 2):
        expand(qtbot, view, (r,))
    wait_idle(qtbot, view)
    locates = count_tasks(app, monkeypatch, "locate")
    t0 = time.perf_counter()
    s.set_filter(FilterSpec("i >= 5"))             # the expanded records are never in it
    qtbot.waitUntil(lambda: s.view_busy, timeout=TIMEOUT)
    wait_view(qtbot, s)
    elapsed = time.perf_counter() - t0
    wait_idle(qtbot, view)
    assert elapsed > 0.5                           # it really grew for a while
    bound = elapsed / (tv.POS_RETRY_MS / 1000) + 4
    assert 1 <= len(locates) <= bound, (len(locates), elapsed)
    assert view.row_count() == 60_000 - 5
    s.reset_view()                                 # the expansions come back
    qtbot.waitUntil(lambda: view.row_count() == 60_000 + 3 * 2, timeout=TIMEOUT)


# ----------------------------------------------------------------------------------
# T7: the "expand many rows?" question is not a modal loop inside a task callback
# ----------------------------------------------------------------------------------
def nested(n_items: int) -> dict:
    return {"items": [{"a": i, "b": [i, i + 1]} for i in range(n_items)], "meta": {"x": {"y": 1}}}


def test_expand_question_is_non_modal_and_applies_after_the_answer(qtbot, app, opened, views,
                                                                   tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(widgets, "confirm_handler", None)
    s = opened(write(tmp_path / "n.json", [nested(400), {"z": 1}]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    wait_text(qtbot, view, 0, "key")
    expected = 2 + 400 + 400 * 2 + 400 * 2 + 1 + 1

    def button(box: QMessageBox, role: QMessageBox.ButtonRole):
        return next(b for b in box.buttons() if box.buttonRole(b) == role)

    view.expand_all((0,))
    qtbot.waitUntil(lambda: view.plan_question() is not None, timeout=TIMEOUT)
    box = view.plan_question()
    assert box.isVisible() and view.row_count() == 2 and view.busy()
    # other tasks are delivered while the question is open (no nested loop in a callback)
    got: list = []
    app.tasks.run(lambda ctx: 42, title="x", kind="other", silent=True, on_done=got.append)
    qtbot.waitUntil(lambda: got == [42], timeout=TIMEOUT)
    button(box, QMessageBox.ButtonRole.NoRole).click()
    qtbot.waitUntil(lambda: view.plan_question() is None, timeout=TIMEOUT)
    wait_idle(qtbot, view)
    assert view.row_count() == 2                   # "no": nothing expanded
    view.expand_all((0,))
    qtbot.waitUntil(lambda: view.plan_question() is not None, timeout=TIMEOUT)
    first = view.plan_question()
    view.expand_to_level(2, (0,))                  # a newer plan voids the open question
    qtbot.waitUntil(lambda: view.plan_question() is not first, timeout=TIMEOUT)
    wait_idle(qtbot, view)
    assert view.row_count() == 2 + 2 + 400 + 1     # level 2 (403 rows) needed no question... below 1000
    view.collapse_all()
    view.expand_all((0,))
    qtbot.waitUntil(lambda: view.plan_question() is not None, timeout=TIMEOUT)
    box = view.plan_question()
    button(box, QMessageBox.ButtonRole.YesRole).click()
    qtbot.waitUntil(lambda: view.row_count() == 2 + expected, timeout=TIMEOUT)
    assert view.plan_question() is None and not app.errors


# ----------------------------------------------------------------------------------
# C11: breadcrumb layout and colors; C7/M7: the data font follows the settings
# ----------------------------------------------------------------------------------
def test_breadcrumb_copy_button_stays_right_and_separators_follow_the_theme(qtbot, monkeypatch) -> None:
    crumb = Breadcrumb()
    qtbot.addWidget(crumb)
    crumb.resize(600, crumb.height())
    crumb.show()
    qtbot.waitExposed(crumb)

    def at_right() -> bool:
        return crumb.copy_button.geometry().right() >= crumb.width() - 8

    crumb.set_segments([])
    qtbot.waitUntil(at_right, timeout=TIMEOUT)
    crumb.set_segments([("$", True), ("data", True), ("#3", False)])
    qtbot.waitUntil(at_right, timeout=TIMEOUT)
    crumb.set_segments([])
    qtbot.waitUntil(at_right, timeout=TIMEOUT)
    crumb.set_segments([("$", True), ("a", False)])
    assert crumb.separator_color() == theme.color("muted")
    orig = theme.color
    monkeypatch.setattr(theme, "color", lambda role: QColor("#123456") if role == "muted" else orig(role))
    crumb.update_colors()
    assert crumb.separator_color() == QColor("#123456")


def test_view_recolors_breadcrumb_on_theme_change(qtbot, opened, views, tmp_path: Path, monkeypatch) -> None:
    s = opened(write(tmp_path / "p.json", people(3)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 3, timeout=TIMEOUT)
    s.select(NodeRef(1, ("x",)), reveal=True)
    qtbot.waitUntil(lambda: len(view.breadcrumb.segments()) == 3, timeout=TIMEOUT)
    orig = theme.color
    monkeypatch.setattr(theme, "color", lambda role: QColor("#654321") if role == "muted" else orig(role))
    theme.notifier().theme_changed.emit()
    assert view.breadcrumb.separator_color() == QColor("#654321")


def test_data_font_follows_settings_and_keeps_the_zoom(qtbot, app, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(3)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 3, timeout=TIMEOUT)
    grid = view.grid
    view.zoom(2)
    assert grid.zoom_level() == 2
    app.state.set("mono_size", 15)
    app.settings_changed.emit()
    expected = widgets.mono_font(s)
    assert expected.pointSizeF() == 15
    assert grid.grid_font().family() == expected.family()
    assert grid.grid_font().pointSizeF() == 17 and grid.zoom_level() == 2
    rows_before = grid.row_height()
    app.settings_changed.emit()                    # nothing changed: the grid is left alone
    assert grid.row_height() == rows_before and grid.zoom_level() == 2
