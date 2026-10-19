"""End-to-end GUI acceptance scenarios of stage 2 (AGENT_PROMPT §14 test 26: "Faylni ochish,
daraxtni ochish/yopish, qidiruv natijasiga o'tish, jadvalda tartiblash, ... eksport tugmasi";
editing / undo come in stage 3).

Every scenario starts the real application (``jsonreshape.gui.app.main(argv, run=False)``:
the real MainWindow with the default view registry and panel factories, GC policy,
excepthooks, logging) on files generated in ``tmp_path`` and drives it like a user: key
presses on the focused widget (window shortcuts such as Ctrl+Shift+F, Ctrl+2, Ctrl+G,
Alt+Left, Ctrl+B, Ctrl+E, Ctrl+W), mouse clicks on tree rows, table headers, buttons and
check boxes, typing into the search / filter / go-to / export fields, a drag & drop of a
file URL. Results are checked through public state (session view and current node, tree
rows and key paths, table rows, status bar texts, exported files) against references
computed here in plain Python.

Every scenario ends by closing the window; the fixture then asserts that no error was
reported (``AppContext.error_reported``, the uncaught-exception hooks included), nothing
was logged at ERROR level, and no background task is left.
"""

from __future__ import annotations

import csv
import gc
import hashlib
import io
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont, QKeyEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from jsonreshape import i18n
from jsonreshape.gui import app as app_mod
from jsonreshape.gui import theme
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.gui.session import NodeRef, same_node
from jsonreshape.gui.widgets import format_count, format_size
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui

TIMEOUT = 30000      # ms
NO = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier
LEFT = Qt.MouseButton.LeftButton
K = Qt.Key

# ----------------------------------------------------------------------------------------
# generated data
# ----------------------------------------------------------------------------------------
N = 240
NAMES = ["Abdulla", "Bobur", "Gulnora", "Nodira", "Olim", "Sardor", "Zafar", "Kamola"]
CITIES = ["Toshkent", "Тошкент", "Samarqand", "Buxoro", "Xiva", "Nukus"]
COUNTRIES = ["O'zbekiston", "Oʻzbekiston", "Ўзбекистон", "O’zbekiston", "Tojikiston"]
RECORD_KEYS = ("id", "name", "age", "grp", "contact", "tags")      # file order of every record
CONTACT_KEYS = ("city", "country", "zip")
EXACT_RECORD = 3                                                    # also has "nums" (exact numbers)
EXACT_NUMS = '{"big":1e400,"dec":1.10,"negz":-0.0,"huge":12345678901234567890}'


def record(i: int) -> dict:
    return {"id": i, "name": NAMES[i % len(NAMES)], "age": (i * 37) % 61 + 18, "grp": i % 4,
            "contact": {"city": CITIES[i % len(CITIES)], "country": COUNTRIES[i % len(COUNTRIES)],
                        "zip": 100000 + i},
            "tags": [f"t{i % 3}", "x"]}


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def raw_record(i: int) -> str:
    """The record's exact text in the file (compact; record 3 carries exact numbers)."""
    text = compact(record(i))
    if i == EXACT_RECORD:
        text = text[:-1] + ',"nums":' + EXACT_NUMS + "}"
    return text


def people_file(path: Path, n: int = N) -> Path:
    path.write_bytes(("[\n" + ",\n".join(raw_record(i) for i in range(n)) + "\n]\n").encode("utf-8"))
    return path


def wrapped_file(path: Path, n: int = 40) -> Path:
    body = ",\n".join(raw_record(i) for i in range(n))
    path.write_bytes(('{"meta":{"source":"e2e","version":1},"data":[\n' + body + '\n],"total":' + str(n)
                      + "}\n").encode("utf-8"))
    return path


def keyed_lines_file(path: Path, n: int = 25) -> Path:
    """JSON Lines of one-member objects ``{"k<i>": {...}}`` (the JSONL form of a keyed object)."""
    path.write_bytes("".join(compact({f"k{i}": record(i)}) + "\n" for i in range(n)).encode("utf-8"))
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pretty_nums() -> str:
    """Independent oracle of the text pane for the ``nums`` node: json.dumps(indent=2) with
    the number tokens put back exactly as written in the file."""
    tokens = {"big": "1e400", "dec": "1.10", "negz": "-0.0", "huge": "12345678901234567890"}
    text = json.dumps({k: f"@@{k}@@" for k in tokens}, indent=2)
    for k, tok in tokens.items():
        text = text.replace(f'"@@{k}@@"', tok)
    return text


def closed(widget: Any) -> bool:
    """A dialog was closed (hidden, or already deleted: ``WA_DeleteOnClose``)."""
    return not isValid(widget) or not widget.isVisible()


def stable_order(indices: list[int], key: Callable[[int], Any], reverse: bool = False) -> list[int]:
    return sorted(indices, key=key, reverse=reverse)


# ----------------------------------------------------------------------------------------
# the driver
# ----------------------------------------------------------------------------------------
class _ErrorLog(logging.Handler):
    """Collects ERROR / CRITICAL log records of the program (the uncaught-exception hooks log
    there too)."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("jsonreshape"):
            self.records.append(record)


class Gui:
    """The started application and user-level helpers."""

    def __init__(self, qtbot: Any, win: MainWindow) -> None:
        self.qtbot = qtbot
        self.win = win
        self.app = win.app
        self.app.headless = True                  # errors are collected, not shown in dialogs
        self.app.tasks.set_poll_interval(20)
        self.errors: list[BaseException] = []
        self.app.error_reported.connect(self.errors.append)
        win.resize(1440, 900)

    # -- waiting -----------------------------------------------------------------------------
    def wait(self, pred: Callable[[], Any], timeout: int = TIMEOUT) -> None:
        self.qtbot.waitUntil(lambda: bool(pred()), timeout=timeout)

    def ready(self, tab: Any = None) -> Any:
        tab = tab or self.win.current_tab()
        self.wait(lambda: tab.session.state in ("ready", "failed"))
        assert tab.session.state == "ready", tab.session.open_error
        return tab

    def idle_view(self, session: Any) -> None:
        self.wait(lambda: not session.view_busy)

    # -- input -------------------------------------------------------------------------------
    def activate(self) -> None:
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()
        self.wait(lambda: QApplication.activeWindow() is self.win)

    def focus(self, widget: Any) -> Any:
        window = widget.window()
        if QApplication.activeWindow() is not window:      # e.g. a closed dialog had it
            window.activateWindow()
            self.wait(lambda: QApplication.activeWindow() is window)
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        self.wait(widget.hasFocus)
        return widget

    def press(self, key: Qt.Key, mods: Any = NO, widget: Any = None) -> None:
        """A key press on ``widget`` (default: the focused widget): window shortcuts are
        matched first, like a real key press."""
        target = widget or QApplication.focusWidget() or self.win
        self.qtbot.keyClick(target, key, mods)

    def type_text(self, widget: Any, text: str) -> None:
        """Type ``text`` into ``widget`` key by key. Characters outside ASCII are sent as key
        events carrying the text (``QTest.keyClicks`` turns every character into a Latin-1
        key code and aborts the process on others, e.g. ``ʻ`` or Cyrillic)."""
        for ch in text:
            if ch.isascii():
                self.qtbot.keyClicks(widget, ch)
                continue
            for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                QApplication.sendEvent(widget, QKeyEvent(kind, Qt.Key.Key_unknown, NO, ch))

    def type_into(self, line_edit: Any, text: str) -> None:
        """Select everything in a line edit and type ``text`` over it."""
        self.focus(line_edit)
        line_edit.selectAll()
        self.type_text(line_edit, text)

    def click(self, widget: Any, mods: Any = NO, pos: QPoint | None = None) -> None:
        self.qtbot.mouseClick(widget, LEFT, mods, pos if pos is not None else widget.rect().center())

    def click_grid_row(self, grid: Any, row: int, col: int = 0, *, double: bool = False) -> None:
        grid.scroll_to(row, "center")
        rect = grid.cell_rect(row, col)
        assert rect.isValid() and rect.height() > 0, (row, col)
        pos = QPoint(rect.x() + min(rect.width() - 4, max(40, rect.width() // 2)), rect.center().y())
        if double:
            self.qtbot.mouseDClick(grid.viewport(), LEFT, NO, pos)
        else:
            self.qtbot.mouseClick(grid.viewport(), LEFT, NO, pos)

    def click_header(self, grid: Any, column_id: str, mods: Any = NO) -> None:
        index = next(i for i, c in enumerate(grid.columns()) if c.id == column_id)
        grid.scroll_to_column(index)
        rect = grid.header().section_rect(index)
        assert rect.isValid() and rect.width() > 20, column_id
        self.qtbot.mouseClick(grid.header(), LEFT, mods, rect.center())

    # -- views ---------------------------------------------------------------------------------
    @property
    def tab(self) -> Any:
        return self.win.current_tab()

    @property
    def session(self) -> Any:
        return self.tab.session

    def view(self, kind: str, tab: Any = None) -> Any:
        tab = tab or self.tab
        pane = tab.showing(kind)
        assert pane is not None, f"no visible pane shows the {kind} view"
        return pane.current_view()

    def tree(self, tab: Any = None) -> Any:
        return self.view("tree", tab)

    def tree_row_loaded(self, tree: Any, row: int) -> None:
        self.wait(lambda: tree.cell_text(row, "key") is not None)

    def view_records(self, session: Any = None) -> list[int]:
        s = session or self.session
        return [s.record_at(p) for p in range(s.view_len())]

    def close(self) -> None:
        if not self.win._closing:
            self.win.close()
        assert self.app.tasks.wait_idle(30), [t_.title for t_ in self.app.tasks.tasks(include_silent=True)]


@pytest.fixture
def gui(qtbot, qapp, monkeypatch, lang_uz):
    """``start(paths) -> Gui``: the real application on ``paths`` (``main(argv, run=False)``).
    Afterwards: every window closed, no error reported or logged, no task left; the
    process-wide hooks of ``main()`` are undone."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    old_font = QFont(QApplication.font())
    old_switch = sys.getswitchinterval()
    gc_was_enabled = gc.isenabled()
    errlog = _ErrorLog()
    logging.getLogger().addHandler(errlog)
    started: list[Gui] = []

    def start(paths: list[Any]) -> Gui:
        win = app_mod.main([str(p) for p in paths], run=False)
        qtbot.addWidget(win)
        g = Gui(qtbot, win)
        started.append(g)
        return g

    try:
        yield start
        for g in started:
            g.close()
            assert not g.app.tasks.tasks(include_silent=True)
            assert g.errors == [], [repr(e) for e in g.errors]
        assert errlog.records == [], [r.getMessage() for r in errlog.records]
    finally:
        for g in started:
            if not g.win._closing:
                g.win.close()
        logging.getLogger().removeHandler(errlog)
        sys.setswitchinterval(old_switch)
        if gc_was_enabled:
            gc.enable()
        QApplication.setFont(old_font)
        from jsonreshape.log import shutdown_logging

        shutdown_logging()
        i18n.set_language("uz")
        theme.apply_theme(qapp, "system")
        theme._reset_for_tests()


@pytest.fixture
def people(tmp_path) -> Path:
    return people_file(tmp_path / "people.json")


# ----------------------------------------------------------------------------------------
# 1. open, status bar, tree keys, breadcrumb, exact numbers in the text pane
# ----------------------------------------------------------------------------------------
def test_open_from_the_command_line_tree_keys_breadcrumb_and_exact_text(gui, people) -> None:
    g = gui([people])
    tab = g.ready()
    s = tab.session
    assert [x.session.name for x in g.win.tab_widgets()] == ["people.json"]
    # status bar: size, format, encoding, record count
    status = g.win.status
    g.wait(lambda: status.count_label.text() == t("gui.status.records", count=format_count(N)))
    assert status.size_label.text() == format_size(people.stat().st_size)
    assert status.format_label.text() == s.doc.info.label and s.doc.kind == "array"
    assert status.encoding_label.text() == "UTF-8"
    # the first records are rows of the tree (0-based record labels)
    tree = g.tree()
    assert tree is tab.panes[0].current_view() and tree.row_count() == N
    for row in range(5):
        g.tree_row_loaded(tree, row)
        assert tree.cell_text(row, "key") == f"#{row}"
        assert tree.cell_text(row, "children") == str(len(RECORD_KEYS) + (row == EXACT_RECORD))
        assert tree.row_keypath(row) == (row,)

    g.activate()
    g.focus(tree.grid)
    g.click_grid_row(tree.grid, 0)
    g.wait(lambda: same_node(s.current, NodeRef(0, ())))
    # Right expands the record, Right again goes to its first child
    g.press(K.Key_Right)
    g.wait(lambda: tree.flat.is_expanded((0,)))
    assert tree.row_count() == N + len(RECORD_KEYS)
    g.press(K.Key_Right)
    assert tree.current_keypath() == (0, 0)
    for _ in range(RECORD_KEYS.index("contact")):
        g.press(K.Key_Down)
    contact = (0, RECORD_KEYS.index("contact"))
    assert tree.current_keypath() == contact
    g.press(K.Key_Right)                                       # nested container
    g.wait(lambda: tree.flat.is_expanded(contact))
    assert tree.row_count() == N + len(RECORD_KEYS) + len(CONTACT_KEYS)
    g.press(K.Key_Right)
    city = contact + (0,)
    assert tree.current_keypath() == city
    g.wait(lambda: same_node(s.current, NodeRef(0, ("contact", "city"))))
    g.wait(lambda: tree.breadcrumb.segments() == ["$", "#0", "contact", "city"])
    assert g.win.status.path_label.text() == "#0.contact.city"
    # the text pane follows: the selected string value
    text = g.view("text")
    g.wait(lambda: not text.is_loading() and text.text().strip() == '"Toshkent"')
    # Left: to the parent, collapse it, to the record, collapse the record
    g.press(K.Key_Left)
    assert tree.current_keypath() == contact
    g.press(K.Key_Left)
    assert not tree.flat.is_expanded(contact) and tree.row_count() == N + len(RECORD_KEYS)
    g.press(K.Key_Left)
    assert tree.current_keypath() == (0,)
    g.press(K.Key_Left)
    assert not tree.flat.is_expanded((0,)) and tree.row_count() == N
    # "*" expands the whole subtree of the current record (contact and tags included)
    g.press(K.Key_Asterisk)
    tags = (0, RECORD_KEYS.index("tags"))
    g.wait(lambda: all(tree.flat.is_expanded(kp) for kp in ((0,), contact, tags)))
    assert tree.row_count() == N + len(RECORD_KEYS) + len(CONTACT_KEYS) + 2
    assert tree.flat.is_expanded(contact) and not tree.flat.is_expanded((1,))

    # exact numbers: the record with 1e400 / 1.10 / -0.0 / 12345678901234567890
    row3 = tree.row_of((EXACT_RECORD,))
    g.click_grid_row(tree.grid, row3)
    g.wait(lambda: same_node(s.current, NodeRef(EXACT_RECORD, ())))
    g.wait(lambda: not text.is_loading() and '"nums"' in text.text())
    whole = text.text()
    for line in ('"big": 1e400', '"dec": 1.10', '"negz": -0.0', '"huge": 12345678901234567890'):
        assert line in whole, (line, whole)
    g.press(K.Key_Right)
    nums = (EXACT_RECORD, len(RECORD_KEYS))
    g.wait(lambda: tree.row_of(nums) is not None)
    g.click_grid_row(tree.grid, tree.row_of(nums))
    g.wait(lambda: same_node(s.current, NodeRef(EXACT_RECORD, ("nums",))))
    g.wait(lambda: tree.breadcrumb.segments() == ["$", f"#{EXACT_RECORD}", "nums"])
    g.wait(lambda: not text.is_loading() and text.text().rstrip("\n") == pretty_nums())


def test_drag_and_drop_of_a_file_url_opens_it_once(gui, people, tmp_path) -> None:
    g = gui([])
    assert g.win.current_tab() is None
    other = wrapped_file(tmp_path / "wrapped.json")

    def drop(path: Path) -> bool:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        pos = g.win.rect().center()
        enter = QDragEnterEvent(pos, Qt.DropAction.CopyAction, mime, LEFT, NO)
        QApplication.sendEvent(g.win, enter)
        if not enter.isAccepted():
            return False
        QApplication.sendEvent(g.win, QDropEvent(QPointF(pos), Qt.DropAction.CopyAction, mime, LEFT, NO))
        return True

    assert drop(people)
    tab = g.ready()
    assert tab.session.name == "people.json" and tab.session.doc.record_count == N
    tree = g.tree()
    g.tree_row_loaded(tree, 0)
    assert tree.cell_text(0, "key") == "#0"
    assert drop(other)
    wrapped = g.ready()
    assert [x.session.name for x in g.win.tab_widgets()] == ["people.json", "wrapped.json"]
    assert wrapped.session.doc.kind == "wrapped" and wrapped.session.doc.record_count == 40
    # dropping a file that is already open focuses its tab (no second tab)
    assert drop(people)
    assert g.win.current_tab() is tab and g.win.tabs.count() == 2
    # a drop without a local file is refused
    mime = QMimeData()
    mime.setText("not a file")
    enter = QDragEnterEvent(g.win.rect().center(), Qt.DropAction.CopyAction, mime, LEFT, NO)
    QApplication.sendEvent(g.win, enter)
    assert not enter.isAccepted()


# ----------------------------------------------------------------------------------------
# 2. full search (apostrophe variants, Latin <-> Cyrillic) and going to a hit
# ----------------------------------------------------------------------------------------
def _expected_hits(values: dict[int, str], wanted: set[str], path: tuple) -> set[tuple[int, tuple]]:
    return {(i, path) for i, v in values.items() if v in wanted}


def test_full_search_apostrophe_and_translit_then_activate_a_hit(gui, people) -> None:
    g = gui([people])
    tab = g.ready()
    s = tab.session
    tree = g.tree()
    g.activate()
    g.focus(tree.grid)
    g.press(K.Key_F, CTRL | SHIFT)                      # Ctrl+Shift+F
    panel = g.win.panels["search"]
    assert g.win.docks["search"].isVisible()
    edit = panel.query_combo.lineEdit()
    g.wait(edit.hasFocus)
    assert not panel.translit_check.isChecked()

    # an apostrophe variant: every apostrophe form matches, the Cyrillic spelling does not
    g.type_into(edit, "Oʻzbekiston")
    with g.qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT) as finished:
        g.press(K.Key_Return, widget=edit)
    assert finished.args[1] == "done"
    countries = {i: record(i)["contact"]["country"] for i in range(N)}
    want = _expected_hits(countries, {"O'zbekiston", "Oʻzbekiston", "O’zbekiston"}, ("contact", "country"))
    assert panel.hit_count_shown() == len(want) > 0
    got = {(h.record, tuple(h.path)) for h in (panel.hit_at(r) for r in range(panel.hit_count_shown()))}
    assert got == want

    # Cyrillic query, Latin <-> Cyrillic on: both spellings of the city
    g.click(panel.translit_check)
    assert panel.translit_check.isChecked()
    g.type_into(edit, "Тошкент")
    with g.qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT) as finished:
        g.press(K.Key_Return, widget=edit)
    assert finished.args[1] == "done"
    cities = {i: record(i)["contact"]["city"] for i in range(N)}
    want = _expected_hits(cities, {"Toshkent", "Тошкент"}, ("contact", "city"))
    hits = [panel.hit_at(r) for r in range(panel.hit_count_shown())]
    assert {(h.record, tuple(h.path)) for h in hits} == want and len(hits) == len(want)
    assert {h.text for h in hits} == {"Toshkent", "Тошкент"}
    assert [h.record for h in hits] == sorted(h.record for h in hits)      # file order

    # activate (double click) a hit far down: the tree reveals and selects that node
    row = len(hits) - 2
    hit = hits[row]
    g.click_grid_row(panel.grid, row, 0, double=True)
    want_ref = NodeRef(hit.record, ("contact", "city"))
    want_kp = (hit.record, RECORD_KEYS.index("contact"), CONTACT_KEYS.index("city"))
    g.wait(lambda: same_node(s.current, want_ref))
    g.wait(lambda: tree.current_keypath() == want_kp)
    assert tree.grid.current()[0] == tree.row_of(want_kp)
    assert tree.flat.is_expanded((hit.record,)) and tree.flat.is_expanded(want_kp[:2])
    first, last = tree.grid.visible_rows()
    assert first <= tree.row_of(want_kp) <= last                          # scrolled into view
    assert tree.cell_text(tree.row_of(want_kp), "value") == f'"{hit.text}"'
    assert g.win.status.path_label.text() == f"#{hit.record}.contact.city"


# ----------------------------------------------------------------------------------------
# 3. table: header click sorting, two keys, following the tree
# ----------------------------------------------------------------------------------------
def test_table_header_sorting_and_following_the_tree(gui, people) -> None:
    g = gui([people])
    tab = g.ready()
    s = tab.session
    g.activate()
    text = g.view("text")
    g.click(text.editor.viewport())                      # the second pane becomes the active one
    g.wait(lambda: tab.active_pane_index() == 1)
    g.press(K.Key_2, CTRL)                               # Ctrl+2: the table in this pane
    assert tab.panes[1].current_kind() == "table" and tab.panes[0].current_kind() == "tree"
    assert g.win.action("view_table").isChecked()
    table = tab.panes[1].current_view()
    g.wait(lambda: {"age", "grp", "name", "contact.city"} <= set(table.visible_column_paths()))
    g.wait(lambda: table.cell_text(0, "age") is not None)
    assert table.grid.row_count() == N and table.record_at_row(0) == 0
    ages = {i: record(i)["age"] for i in range(N)}
    grps = {i: record(i)["grp"] for i in range(N)}

    def sorted_by(keys: list[tuple[str, bool]]) -> None:
        g.wait(lambda: [(k.path, k.descending) for k in s.sort_keys] == keys and not s.view_busy)

    # click: ascending
    g.click_header(table.grid, "age")
    sorted_by([("age", False)])
    assert g.view_records() == stable_order(list(range(N)), ages.get)
    g.wait(lambda: table.cell_text(0, "age") == str(min(ages.values())))
    assert table.record_at_row(0) == g.view_records()[0]
    marks = {c.id: (c.sort_order, c.sort_priority) for c in table.grid.columns() if c.sort_order}
    assert marks == {"age": (1, marks["age"][1])}
    # click again: descending
    g.click_header(table.grid, "age")
    sorted_by([("age", True)])
    assert g.view_records() == stable_order(list(range(N)), ages.get, reverse=True)
    g.wait(lambda: table.cell_text(0, "age") == str(max(ages.values())))
    # Shift+click another column: a second key
    g.click_header(table.grid, "grp", SHIFT)
    sorted_by([("age", True), ("grp", False)])
    assert g.view_records() == sorted(range(N), key=lambda i: (-ages[i], grps[i], i))
    marks = {c.id: (c.sort_order, c.sort_priority) for c in table.grid.columns() if c.sort_order}
    assert marks == {"age": (-1, 1), "grp": (1, 2)}
    # the tree shows the same order; a node selected there is followed by the table
    tree = tab.panes[0].current_view()
    order = g.view_records()
    g.tree_row_loaded(tree, 0)
    assert [tree.row_keypath(r)[0] for r in range(5)] == order[:5]
    pick = order[57]
    g.focus(tree.grid)
    g.click_grid_row(tree.grid, tree.row_of((pick,)))
    g.wait(lambda: same_node(s.current, NodeRef(pick, ())))
    g.wait(lambda: table.record_at_row(table.grid.current()[0]) == pick)
    assert table.grid.current()[0] == 57
    # a nested node: the table goes to its row and the column of that field
    g.press(K.Key_Right)
    g.wait(lambda: tree.flat.is_expanded((pick,)))
    g.press(K.Key_Right)
    for _ in range(RECORD_KEYS.index("grp")):
        g.press(K.Key_Down)
    g.wait(lambda: same_node(s.current, NodeRef(pick, ("grp",))))
    cols = [c.id for c in table.grid.columns()]
    g.wait(lambda: table.grid.current() == (57, cols.index("grp")))


# ----------------------------------------------------------------------------------------
# 4. filter dock: expression, builder, clear
# ----------------------------------------------------------------------------------------
def test_filter_expression_and_builder_give_the_reference_count(gui, people) -> None:
    g = gui([people])
    tab = g.ready()
    s = tab.session
    tree = g.tree()
    g.activate()
    g.focus(tree.grid)
    g.press(K.Key_L, CTRL | SHIFT)                       # Ctrl+Shift+L
    panel = g.win.panels["filter"]
    assert g.win.docks["filter"].isVisible() and panel.usable()
    ed = panel.current_editor()
    g.wait(ed.expr.hasFocus)
    want = [i for i in range(N) if record(i)["age"] > 40 and record(i)["contact"]["city"] == "Toshkent"]
    assert 0 < len(want) < N

    g.type_text(ed.expr, 'age > 40 and contact.city == "Toshkent"')
    g.click(panel.apply_button)
    g.wait(lambda: s.filter is not None)
    g.idle_view(s)
    assert s.view_len() == len(want) and g.view_records() == want
    g.wait(lambda: tree.row_count() == len(want))
    g.wait(lambda: g.win.status.count_label.text() == t("gui.status.records_view", shown=format_count(len(want)),
                                                          total=format_count(N)))
    assert tab.view_banner.isVisible()

    # clear: all records again
    g.click(panel.clear_button)
    g.wait(lambda: s.view_is_base and s.filter is None)
    assert s.view_len() == N and ed.text() == "" and tree.row_count() == N

    # the same filter through the visual builder
    for field, op, value in (("age", "gt", "40"), ("contact.city", "eq", "Toshkent")):
        g.click(ed.root.add_condition_button)
        row = ed.root.rows()[-1]
        g.type_into(row.field.lineEdit(), field)
        popup = row.field.completer().popup()
        if popup.isVisible():
            popup.hide()
        row.op.setCurrentIndex(row.op.findData(op))
        g.type_into(row.v1.edit, value)
    g.wait(lambda: ed.text() == 'age > 40 and contact.city == "Toshkent"')
    assert ed.builder_available()
    g.click(panel.apply_button)
    g.wait(lambda: s.filter is not None and s.filter.builder is not None)
    g.idle_view(s)
    assert s.view_len() == len(want) and g.view_records() == want
    g.click(panel.clear_button)
    g.wait(lambda: s.view_is_base)
    assert ed.root.rows() == [] and s.view_len() == N


# ----------------------------------------------------------------------------------------
# 5. go to, history, bookmarks (wrapped file)
# ----------------------------------------------------------------------------------------
def test_goto_history_and_bookmarks_on_a_wrapped_file(gui, tmp_path) -> None:
    path = wrapped_file(tmp_path / "wrapped.json", 40)
    g = gui([path])
    tab = g.ready()
    s = tab.session
    assert s.doc.kind == "wrapped" and s.doc.record_count == 40
    tree = g.tree()
    g.tree_row_loaded(tree, 0)
    assert tree.row_count() == 41 and tree.row_keypath(0) == ("wrapper",)
    g.activate()
    g.focus(tree.grid)

    def goto(text: str, ref: NodeRef, kp: tuple) -> None:
        g.press(K.Key_G, CTRL)                                    # Ctrl+G
        dialog = g.win.dialogs["goto"]
        g.wait(dialog.isVisible)
        g.type_into(dialog.edit, text)
        assert dialog.location() is not None, dialog.error_text()
        g.click(dialog.ok_button)
        g.wait(lambda: same_node(s.current, ref))
        g.wait(lambda: tree.current_keypath() == kp)
        g.wait(lambda: closed(dialog))
        assert tree.grid.current()[0] == tree.row_of(kp)
        g.focus(tree.grid)

    contact_city = (5, RECORD_KEYS.index("contact"), CONTACT_KEYS.index("city"))
    goto("#5.contact.city", NodeRef(5, ("contact", "city")), contact_city)
    assert tree.breadcrumb.segments() == ["$", "data", "#5", "contact", "city"]
    goto("/data/7", NodeRef(7, ()), (7,))
    goto("$.data[12].tags[1]", NodeRef(12, ("tags", 1)), (12, RECORD_KEYS.index("tags"), 1))
    # Alt+Left / Alt+Right walk the history
    g.press(K.Key_Left, ALT)
    g.wait(lambda: same_node(s.current, NodeRef(7, ())))
    g.wait(lambda: tree.current_keypath() == (7,))
    g.press(K.Key_Left, ALT)
    g.wait(lambda: same_node(s.current, NodeRef(5, ("contact", "city"))))
    g.wait(lambda: tree.current_keypath() == contact_city)
    assert [x.path for x in s.history()] == [("contact", "city"), (), ("tags", 1)]
    assert not s.can_back and not g.win.action("back").isEnabled() and g.win.action("forward").isEnabled()
    g.press(K.Key_Right, ALT)
    g.wait(lambda: same_node(s.current, NodeRef(7, ())))
    g.wait(lambda: tree.current_keypath() == (7,))

    # Ctrl+B bookmarks the current node (the name is asked); stage 3 moved it from Ctrl+D,
    # which duplicates nodes now (ARCHITECTURE §12.9, AGENT_PROMPT §8.1)
    asked: list[str] = []

    def ask_text(title: str, label: str, default: str = "") -> str:
        asked.append(default)
        return "yettinchi"

    g.win._ask_text = ask_text
    g.press(K.Key_B, CTRL)
    assert asked == ["#7"]
    assert [(b.name, b.record, list(b.path)) for b in s.bookmarks] == [("yettinchi", 7, [])]
    goto("#20.name", NodeRef(20, ("name",)), (20, RECORD_KEYS.index("name")))
    # go to the bookmark from the bookmarks window (Ctrl+Shift+D)
    g.press(K.Key_D, CTRL | SHIFT)
    dialog = g.win.dialogs["bookmarks"]
    g.wait(dialog.isVisible)
    assert dialog.count() == 1
    dialog.set_current(0)
    g.click(dialog.go_button)
    g.wait(lambda: same_node(s.current, NodeRef(7, ())))
    g.wait(lambda: tree.current_keypath() == (7,))
    g.wait(lambda: closed(dialog))
    # the bookmark is remembered for the file in the GUI state
    assert [b.name for b in g.app.state.bookmarks(s.path)] == ["yettinchi"]


# ----------------------------------------------------------------------------------------
# 6. export button (Ctrl+E): the filtered + sorted view as JSON Lines and CSV
# ----------------------------------------------------------------------------------------
def _run_export(g: Gui, out: Path, *, as_keyed: bool = False, toolbar: bool = False) -> Any:
    """Ctrl+E (or the toolbar's export button), type the output path, (keyed), click
    Export; returns the dialog after the report window appeared."""
    if toolbar:
        button = g.win.toolbar.widgetForAction(g.win.action("export"))
        assert button is not None and button.isEnabled()
        g.click(button)
    else:
        g.press(K.Key_E, CTRL)
    dialog = g.win.dialogs["export"]
    g.wait(dialog.isVisible)
    assert dialog.scope() == "view"
    g.type_into(dialog.path_edit, str(out))
    if as_keyed:
        g.click(dialog.as_keyed_check)
        assert dialog.as_keyed_check.isChecked()
    assert dialog.error_text() == "" and dialog.export_button.isEnabled(), dialog.error_text()
    g.click(dialog.export_button)
    g.wait(lambda: dialog.outcome is not None or dialog.error is not None, timeout=60000)
    assert dialog.error is None, dialog.error
    report = dialog.report_dialog
    assert report is not None and report.isVisible()
    assert report.outcome.report.files and Path(report.outcome.report.files[0]) == out
    return dialog


def _close_dialogs(g: Gui, dialog: Any) -> None:
    g.click(dialog.report_dialog.close_button)
    g.wait(lambda: closed(dialog.report_dialog))
    g.click(dialog.close_button)
    g.wait(lambda: closed(dialog))


def test_export_button_writes_the_filtered_sorted_view(gui, people, tmp_path) -> None:
    before = sha256(people)
    g = gui([people])
    tab = g.ready()
    s = tab.session
    g.activate()
    # filter grp == 1 in the filter dock, sort by age descending with the table header
    g.focus(g.tree().grid)
    g.press(K.Key_L, CTRL | SHIFT)
    panel = g.win.panels["filter"]
    ed = panel.current_editor()
    g.wait(ed.expr.hasFocus)
    g.type_text(ed.expr, "grp == 1")
    g.press(K.Key_Return, widget=ed.expr)                # Enter applies
    g.wait(lambda: s.filter is not None)
    g.idle_view(s)
    g.click(g.view("text").editor.viewport())
    g.wait(lambda: tab.active_pane_index() == 1)
    g.press(K.Key_2, CTRL)
    table = tab.panes[1].current_view()
    g.wait(lambda: {"age", "name"} <= set(table.visible_column_paths()))
    g.click_header(table.grid, "age")
    g.click_header(table.grid, "age")
    g.wait(lambda: [(k.path, k.descending) for k in s.sort_keys] == [("age", True)] and not s.view_busy)
    want = stable_order([i for i in range(N) if record(i)["grp"] == 1], lambda i: record(i)["age"], reverse=True)
    assert g.view_records() == want
    # hide a column: the CSV gets exactly the visible columns
    assert table.hide_column("tags")
    columns = table.visible_column_paths()
    assert "tags" not in columns and columns[:4] == ["id", "name", "age", "grp"]

    # JSON Lines of the view: the records' exact bytes in view order
    out = tmp_path / "view.jsonl"
    dialog = _run_export(g, out, toolbar=True)
    assert dialog.format() == "jsonl"
    rep = dialog.outcome.report
    assert rep.records == len(want)
    assert out.read_bytes().decode("utf-8") == "".join(raw_record(i) + "\n" for i in want)
    _close_dialogs(g, dialog)

    # CSV with the table's visible columns
    out_csv = tmp_path / "view.csv"
    g.focus(table.grid)
    dialog = _run_export(g, out_csv)
    assert dialog.format() == "csv" and dialog.column_list() == columns
    rows = list(csv.reader(io.StringIO(out_csv.read_bytes().decode("utf-8-sig"), newline="")))
    assert rows[0] == columns
    assert len(rows) == len(want) + 1
    col = {name: k for k, name in enumerate(columns)}
    for line, i in zip(rows[1:], want):
        rec = record(i)
        assert line[col["id"]] == str(i)
        assert line[col["name"]] == rec["name"]
        assert line[col["age"]] == str(rec["age"])
        assert line[col["contact.city"]] == rec["contact"]["city"]
        assert line[col["contact.country"]] == rec["contact"]["country"]
    _close_dialogs(g, dialog)
    # the source file is never touched
    assert sha256(people) == before
    assert s.view_len() == len(want)            # the view is unchanged


def test_export_dialog_keyed_round_trip(gui, tmp_path) -> None:
    """JSONL of ``{"k": v}`` lines -> JSON with "rebuild keyed object" -> open it -> JSONL:
    the original lines come back byte for byte."""
    lines = keyed_lines_file(tmp_path / "keyed.jsonl", 25)
    before = sha256(lines)
    g = gui([lines])
    tab = g.ready()
    assert tab.session.doc.kind == "jsonl" and tab.session.doc.record_count == 25
    g.activate()
    g.focus(g.tree().grid)
    keyed = tmp_path / "keyed.json"
    dialog = _run_export(g, keyed, as_keyed=True)
    assert dialog.format() == "json"
    obj = json.loads(keyed.read_bytes().decode("utf-8"))
    assert list(obj) == [f"k{i}" for i in range(25)]
    assert obj == {f"k{i}": record(i) for i in range(25)}
    _close_dialogs(g, dialog)
    assert sha256(lines) == before

    # open the keyed object: one record per member, then JSON Lines again
    g.win.open_path(keyed)
    tab2 = g.ready()
    assert tab2 is not tab and tab2.session.doc.kind == "keyed" and tab2.session.doc.record_count == 25
    tree = g.tree(tab2)
    g.tree_row_loaded(tree, 0)
    assert tree.cell_text(0, "key") == "k0"
    g.focus(tree.grid)
    back = tmp_path / "back.jsonl"
    dialog = _run_export(g, back)
    assert dialog.format() == "jsonl"
    assert back.read_bytes() == lines.read_bytes()
    _close_dialogs(g, dialog)


# ----------------------------------------------------------------------------------------
# 6b. the Tools menu (stage 5): statistics, duplicates, JSON Schema, repair, split / merge, compare
# ----------------------------------------------------------------------------------------
def test_tools_menu_pass_through_every_stage5_tool(gui, people, tmp_path) -> None:
    before = sha256(people)
    g = gui([people])
    tab = g.ready()
    s = tab.session
    g.activate()
    win = g.win
    tools = win.menus["tools"]
    keys = ("compare_files", "stats", "dupes", "schema_generate", "schema_validate", "repair", "split_file",
            "merge_files")
    assert all(win.action(k).isEnabled() and win.action(k) in tools.actions() + win.menus["json_schema"].actions()
               for k in keys)
    # statistics dock: type a field, Enter computes it
    win.action("stats").trigger()
    panel = win.panels["stats"]
    g.wait(lambda: win.docks["stats"].isVisible())
    g.type_into(panel.field_combo.lineEdit(), "contact.city")
    g.press(K.Key_Return, widget=panel.field_combo.lineEdit())
    g.wait(lambda: panel.status() == "done", timeout=60000)
    assert panel.result().distinct == len(CITIES) and panel.result().present == N
    # duplicates by name: one group per name
    win.action("dupes").trigger()
    dupes = win.dialogs["dupes"]
    g.wait(dupes.isVisible)
    dupes.set_key("name")
    with g.qtbot.waitSignal(dupes.report_ready, timeout=60000):
        g.click(dupes.run_button)
    assert dupes.report.duplicates == N - len(NAMES) and dupes.group_count() == len(NAMES)
    g.click(dupes.close_button)
    g.wait(lambda: closed(dupes))
    # JSON Schema: generate (the window's save asker), then validate the file against it
    out = tmp_path / "people.schema.json"
    win._ask_save_path = lambda title, start, filters: str(out)
    win.action("schema_generate").trigger()
    gen = win.dialogs["schema_generate"]
    g.wait(gen.isVisible)
    g.click(gen.save_button)
    g.wait(lambda: out.exists() and not gen.running, timeout=60000)
    schema = json.loads(out.read_text(encoding="utf-8"))
    assert schema["type"] == "array"
    gen.close()
    win.action("schema_validate").trigger()
    val = win.dialogs["schema_validate"]
    g.wait(val.isVisible)
    val.panel.set_schema_path(out)
    g.click(val.panel.run_button)
    g.wait(lambda: not val.panel.is_running(s) and val.panel.summary_text(), timeout=60000)
    assert val.panel.issue_count_shown() == 0
    val.close()
    # repair: the tab's file is the source; a check finds nothing to fix
    win.action("repair").trigger()
    rep = win.dialogs["repair"]
    g.wait(rep.isVisible)
    assert rep.source_path() == str(people)
    with g.qtbot.waitSignal(rep.run_finished, timeout=60000):
        g.click(rep.check_button)
    assert rep.report is not None and rep.report.total == 0
    g.click(rep.close_button)
    g.wait(lambda: closed(rep))
    # split / merge / compare windows open (non-modal) and close again
    for key in ("split_file", "merge_files", "compare_files"):
        win.action(key).trigger()
        name = {"split_file": "split", "merge_files": "merge", "compare_files": "compare"}[key]
        dialog = win.dialogs[name]
        g.wait(dialog.isVisible)
        assert not dialog.isModal()
        dialog.close()
        g.wait(lambda d=dialog: closed(d))
    assert sha256(people) == before                     # no tool touched the source


# ----------------------------------------------------------------------------------------
# 7. closing tabs and the window
# ----------------------------------------------------------------------------------------
def test_close_tabs_and_window_leave_nothing_running(gui, people, tmp_path) -> None:
    wrapped = wrapped_file(tmp_path / "wrapped.json")
    g = gui([people, wrapped])
    tabs = g.win.tab_widgets()
    assert [x.session.name for x in tabs] == ["people.json", "wrapped.json"]
    for tab in tabs:
        g.ready(tab)
    sessions = [tab.session for tab in tabs]
    g.activate()
    # some work in both tabs: a table, a filter, a search
    g.win.tabs.setCurrentWidget(tabs[0])
    g.focus(g.tree(tabs[0]).grid)
    g.press(K.Key_F, CTRL | SHIFT)
    panel = g.win.panels["search"]
    g.type_into(panel.query_combo.lineEdit(), "Samarqand")
    with g.qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        g.press(K.Key_Return, widget=panel.query_combo.lineEdit())
    assert panel.hit_count_shown() == sum(1 for i in range(N) if record(i)["contact"]["city"] == "Samarqand")
    g.win.tabs.setCurrentWidget(tabs[1])
    g.focus(g.tree(tabs[1]).grid)
    g.press(K.Key_3, CTRL)                               # Ctrl+3: the text view in the tree's pane
    assert tabs[1].panes[0].current_kind() == "text"
    # Ctrl+W closes the current tab: its session is closed, its tasks are gone
    g.focus(tabs[1].panes[0].current_view().editor)
    g.press(K.Key_W, CTRL)
    assert g.win.tabs.count() == 1 and tabs[1].closed
    g.wait(lambda: sessions[1].state == "closed")
    g.wait(lambda: not g.app.tasks.tasks(owner=sessions[1], include_silent=True))
    assert g.win.current_tab() is tabs[0]
    g.win.close_tab()
    assert g.win.tabs.count() == 0 and sessions[0].state == "closed"
    assert g.win.central.currentWidget() is g.win.welcome
    assert not g.win.action("export").isEnabled() and not g.win.action("goto").isEnabled()
    # the window: closing it saves the state and stops everything
    g.press(K.Key_Q, CTRL, widget=g.win)
    g.wait(lambda: g.win._closing)
    assert g.app.tasks.wait_idle(30)
    assert not g.app.tasks.tasks(include_silent=True)
    assert not getattr(sys.excepthook, "_jsonreshape_qt_hook", False)       # the hooks are undone
