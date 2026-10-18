"""Navigation dialogs (jsonreshape/gui/navigation.py; AGENT_PROMPT §3, §5): Ctrl+G for every
location syntax (+ syntax errors with their position, records out of range, missing
paths, the history), bookmarks (list, go, rename, delete, persisted), the record path
dialog (candidates, free path, format override, conflicts, reopen) and the broken lines
dialog (in-memory part, lines beyond it loaded from the index, go to the raw view)."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt

from guishell_helpers import TIMEOUT, jsonl_with_broken, make_app, records, wait_ready, wrapped_file, write_json
from jsonreshape.errors import PathSyntaxError
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.navigation import (
    GOTO_HISTORY_KEY,
    BookmarksDialog,
    BrokenLinesDialog,
    GoToDialog,
    RecordPathDialog,
)
from jsonreshape.gui.session import DocSession, NodeRef, same_node
from jsonreshape.paths import parse_location
from jsonreshape.reader import BROKEN_KEEP

pytestmark = pytest.mark.gui


@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    yield ctx
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def open_session(qtbot, app):
    made: list[DocSession] = []

    def factory(path, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        s.start()
        wait_ready(qtbot, s)
        assert s.state == "ready", s.open_error
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def wrapped(open_session, tmp_path) -> DocSession:
    return open_session(wrapped_file(tmp_path / "w.json", 200))


def run_goto(qtbot, session, text: str) -> GoToDialog:
    dialog = GoToDialog(session)
    qtbot.addWidget(dialog)
    dialog.set_text(text)
    with qtbot.waitSignal(dialog.finished_resolving, timeout=TIMEOUT):
        dialog.accept()
    return dialog


# ----------------------------------------------------------------------------------------
# GoToDialog
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("data[120].contact.address", NodeRef(120, ("contact", "address"))),
    ("$.data[120]", NodeRef(120, ())),
    ("/data/120", NodeRef(120, ())),
    ("/data/120/contact/zip", NodeRef(120, ("contact", "zip"))),
    ("#120", NodeRef(120, ())),
    ("#120.contact", NodeRef(120, ("contact",))),
    ("#120.tags[1]", NodeRef(120, ("tags", 1))),
    ("#120/contact/address", NodeRef(120, ("contact", "address"))),
    ("meta.tags[1]", NodeRef(None, ("meta", "tags", 1))),
    ("$", NodeRef(None, ())),
])
def test_goto_every_syntax(qtbot, wrapped, text, expected) -> None:
    reveals = []
    wrapped.reveal_requested.connect(reveals.append)
    dialog = run_goto(qtbot, wrapped, text)
    assert dialog.result() == dialog.DialogCode.Accepted, dialog.error_text()
    assert same_node(wrapped.current, expected), wrapped.current
    assert wrapped.current.ipath is not None                 # resolved (tree identity)
    assert reveals and same_node(reveals[-1], expected)


def test_goto_live_syntax_error_with_position(qtbot, wrapped) -> None:
    dialog = GoToDialog(wrapped)
    qtbot.addWidget(dialog)
    assert not dialog.ok_button.isEnabled()                   # empty
    dialog.set_text("data[12")
    assert not dialog.ok_button.isEnabled()
    assert dialog.error_text()
    caret = dialog.caret_label.text().splitlines()
    with pytest.raises(PathSyntaxError) as info:
        parse_location("data[12")
    assert caret[0] == "data[12" and caret[1].index("^") == info.value.pos
    assert f"{info.value.pos + 1}" in dialog.error_text()      # shown 1-based
    dialog.set_text("#abc")
    assert dialog.error_text() and dialog.location() is None
    dialog.set_text("#12.contact")
    assert dialog.error_text() == "" and dialog.ok_button.isEnabled()
    assert dialog.location().record == 12


def test_goto_record_out_of_range_stays_open(qtbot, wrapped) -> None:
    before = wrapped.current
    dialog = run_goto(qtbot, wrapped, "#5000")
    assert dialog.result() != dialog.DialogCode.Accepted
    assert "#5000 yozuv yo'q" in dialog.error_text()
    assert "200" in dialog.error_text()
    assert wrapped.current == before
    assert dialog.ok_button.isEnabled()                       # the user can correct it


def test_goto_missing_path(qtbot, wrapped) -> None:
    dialog = run_goto(qtbot, wrapped, "data[3].nope")
    assert dialog.result() != dialog.DialogCode.Accepted
    assert "nope" in dialog.error_text()
    dialog.set_text("data[3].name")
    with qtbot.waitSignal(dialog.finished_resolving, timeout=TIMEOUT):
        dialog.accept()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert same_node(wrapped.current, NodeRef(3, ("name",)))


def test_goto_history(qtbot, app, wrapped) -> None:
    run_goto(qtbot, wrapped, "#1")
    run_goto(qtbot, wrapped, "#2.name")
    run_goto(qtbot, wrapped, "#1")
    assert app.state.get(GOTO_HISTORY_KEY) == ["#1", "#2.name"]
    run_goto(qtbot, wrapped, "#99999")                       # failures are not remembered
    assert app.state.get(GOTO_HISTORY_KEY) == ["#1", "#2.name"]
    dialog = GoToDialog(wrapped)
    qtbot.addWidget(dialog)
    assert [dialog.combo.itemText(i) for i in range(dialog.combo.count())] == ["#1", "#2.name"]
    assert dialog.text() == ""


def test_goto_single_document(qtbot, open_session, tmp_path) -> None:
    s = open_session(write_json(tmp_path / "s.json", {"a": {"b": [1, 2, 3]}}))
    dialog = run_goto(qtbot, s, "a.b[2]")
    assert same_node(s.current, NodeRef(None, ("a", "b", 2)))
    dialog = run_goto(qtbot, s, "#0.a")
    assert same_node(s.current, NodeRef(None, ("a",)))
    dialog = run_goto(qtbot, s, "#1")
    assert dialog.result() != dialog.DialogCode.Accepted and "#1" in dialog.error_text()


def test_goto_keyed_and_jsonl(qtbot, open_session, tmp_path) -> None:
    keyed = open_session(write_json(tmp_path / "k.json", {f"id{i}": {"v": i} for i in range(30)}))
    run_goto(qtbot, keyed, "id7.v")
    assert same_node(keyed.current, NodeRef(7, ("v",)))
    path = tmp_path / "l.jsonl"
    path.write_text("\n".join(json.dumps({"i": i, "x": {"y": i}}) for i in range(50)) + "\n", encoding="utf-8")
    lines = open_session(path)
    run_goto(qtbot, lines, "[42].x.y")
    assert same_node(lines.current, NodeRef(42, ("x", "y")))


def test_goto_cancel_while_resolving_detaches(qtbot, wrapped) -> None:
    dialog = GoToDialog(wrapped)
    qtbot.addWidget(dialog)
    dialog.set_text("#3")
    dialog.accept()
    assert dialog.is_busy()
    dialog.reject()
    assert not dialog.is_busy()
    qtbot.wait(200)
    assert wrapped.current is None                            # the detached result is dropped


# ----------------------------------------------------------------------------------------
# BookmarksDialog
# ----------------------------------------------------------------------------------------
def test_bookmarks_list_go_rename_delete_persisted(qtbot, app, wrapped, tmp_path) -> None:
    wrapped.add_bookmark("Birinchi", NodeRef(5, ("contact", "address")))
    wrapped.add_bookmark("", NodeRef(None, ("meta",)))       # default name = location text
    dialog = BookmarksDialog(wrapped)
    qtbot.addWidget(dialog)
    assert dialog.count() == 2
    first = dialog.tree.topLevelItem(0)
    assert first.text(0) == "Birinchi" and first.text(1) == "#5.contact.address" and first.text(2)
    assert dialog.tree.topLevelItem(1).text(0) == "meta"
    # rename (asked) + delete
    dialog.ask_name = lambda current: current + " (yangi)"
    dialog.rename(0)
    assert wrapped.bookmarks[0].name == "Birinchi (yangi)"
    assert dialog.tree.topLevelItem(0).text(0) == "Birinchi (yangi)"
    dialog.rename(1, "   ")                                   # blank: unchanged
    assert wrapped.bookmarks[1].name == "meta"
    dialog.delete(1)
    assert dialog.count() == 1
    qtbot.waitUntil(lambda: not app.state_saving, timeout=TIMEOUT)     # saved in the background (T9)
    stored = GuiState.load(tmp_path / "gui_state.json").bookmarks(wrapped.path)
    assert [b.name for b in stored] == ["Birinchi (yangi)"]
    # go: resolves, selects with reveal and closes
    reveals = []
    wrapped.reveal_requested.connect(reveals.append)
    with qtbot.waitSignal(dialog.resolved, timeout=TIMEOUT):
        dialog.go(0)
    assert same_node(wrapped.current, NodeRef(5, ("contact", "address")))
    assert reveals and dialog.result() == dialog.DialogCode.Accepted


def test_bookmark_that_no_longer_exists(qtbot, wrapped) -> None:
    wrapped.add_bookmark("yo'q", NodeRef(3, ("missing",)))
    dialog = BookmarksDialog(wrapped)
    qtbot.addWidget(dialog)
    dialog.go(0)
    qtbot.waitUntil(lambda: dialog.error_label.isVisibleTo(dialog), timeout=TIMEOUT)
    assert "missing" in dialog.error_label.text()
    assert dialog.result() != dialog.DialogCode.Accepted


def test_bookmarks_empty_and_double_click(qtbot, wrapped) -> None:
    dialog = BookmarksDialog(wrapped)
    qtbot.addWidget(dialog)
    assert dialog.count() == 0 and dialog.empty_label.isVisibleTo(dialog)
    assert not dialog.go_button.isEnabled()
    wrapped.add_bookmark("x", NodeRef(1, ()))                # live refresh
    assert dialog.count() == 1 and dialog.go_button.isEnabled()
    item = dialog.tree.topLevelItem(0)
    with qtbot.waitSignal(dialog.resolved, timeout=TIMEOUT):
        dialog.tree.itemActivated.emit(item, 0)
    assert same_node(wrapped.current, NodeRef(1, ()))


# ----------------------------------------------------------------------------------------
# RecordPathDialog
# ----------------------------------------------------------------------------------------
def test_record_path_candidates_and_reopen(qtbot, wrapped) -> None:
    assert wrapped.doc.info.record_path == ("data",)
    dialog = RecordPathDialog(wrapped)
    qtbot.addWidget(dialog)
    texts = [dialog.list.topLevelItem(i).text(0) for i in range(dialog.list.topLevelItemCount())]
    assert texts[0] == "Avtomatik aniqlash"
    assert "data" in texts and "other" in texts
    row = texts.index("other")
    assert dialog.list.topLevelItem(row).text(1) == "3"
    assert dialog.list.topLevelItem(row).text(2) == "obyektlar"
    dialog.choose_row(row)
    assert dialog.chosen() == ("other", None)
    with qtbot.waitSignal(wrapped.index_finished, timeout=TIMEOUT):
        dialog.accept()
    assert wrapped.doc.info.record_path == ("other",)
    assert wrapped.doc.record_count == 3
    # the dialog now preselects the chosen path; "automatic" goes back
    again = RecordPathDialog(wrapped)
    qtbot.addWidget(again)
    assert again.chosen() == ("other", None)
    again.choose_row(0)
    assert again.chosen() == (None, None)
    with qtbot.waitSignal(wrapped.index_finished, timeout=TIMEOUT):
        again.accept()
    assert wrapped.doc.info.record_path == ("data",) and wrapped.record_path is None


def test_record_path_free_path_format_and_conflicts(qtbot, wrapped) -> None:
    dialog = RecordPathDialog(wrapped, apply=False)
    qtbot.addWidget(dialog)
    dialog.path_edit.setText("data[")
    assert dialog.error_text() and not dialog.ok_button.isEnabled()
    dialog.path_edit.setText("meta.tags")
    assert dialog.error_text() == ""
    dialog.set_format("jsonl")
    assert "JSON Lines" in dialog.error_text() and not dialog.ok_button.isEnabled()
    dialog.set_format("wrapped")
    assert dialog.error_text() == ""
    assert dialog.chosen() == ("meta.tags", "wrapped")
    dialog.path_edit.setText("$")
    dialog.set_format("keyed")
    assert dialog.error_text() == ""
    dialog.path_edit.setText("")
    dialog.set_format("single")
    assert dialog.chosen() == (None, "single")
    dialog.accept()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert wrapped.state == "ready" and wrapped.doc.kind == "wrapped"   # apply=False: nothing reopened


def test_record_path_format_override_reopens(qtbot, wrapped) -> None:
    dialog = RecordPathDialog(wrapped)
    qtbot.addWidget(dialog)
    dialog.choose_row(0)
    dialog.set_format("single")
    with qtbot.waitSignal(wrapped.index_finished, timeout=TIMEOUT):
        dialog.accept()
    assert wrapped.doc.kind == "single" and wrapped.format == "single"


def test_record_path_dialog_without_document(qtbot, app, tmp_path) -> None:
    s = DocSession(app, tmp_path / "missing.json")
    dialog = RecordPathDialog(s, apply=False)
    qtbot.addWidget(dialog)
    assert dialog.candidate_count() == 0
    assert dialog.chosen() == (None, None)
    s.close()


# ----------------------------------------------------------------------------------------
# BrokenLinesDialog
# ----------------------------------------------------------------------------------------
def test_broken_lines_dialog(qtbot, open_session, tmp_path) -> None:
    path = jsonl_with_broken(tmp_path / "b.jsonl", 20, (3, 7, 12))
    s = open_session(path)
    assert s.doc.broken_count == 3
    dialog = BrokenLinesDialog(s)
    qtbot.addWidget(dialog)
    assert dialog.row_count() == 3
    assert [dialog.line_at(i).line for i in range(3)] == [4, 8, 13]
    assert "3 ta buzuq qator" in dialog.summary_label.text()
    cell = dialog.source.cell(0, dialog.grid.columns()[0])
    assert cell.text == "4"
    reason = dialog.source.cell(0, dialog.grid.columns()[3])
    assert reason.text and reason.role == "error"
    with qtbot.waitSignal(dialog.goto_requested, timeout=TIMEOUT) as blocker:
        dialog.grid.activated.emit(1, 0)
    offset, length = blocker.args
    bl = dialog.line_at(1)
    assert offset == bl.offset and length == bl.end - bl.offset
    data = path.read_bytes()
    assert data[bl.start:bl.end].startswith(b'{"id": 7')
    dialog.grid.set_current(2, 0)
    with qtbot.waitSignal(dialog.goto_requested, timeout=TIMEOUT) as blocker:
        dialog.go_button.click()
    assert blocker.args[0] == dialog.line_at(2).offset


def test_broken_lines_beyond_the_memory_cap(qtbot, open_session, tmp_path) -> None:
    n = BROKEN_KEEP + 1500
    path = tmp_path / "many.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write('{"ok": %d}\n{"bad": %d,}\n' % (i, i))
    s = open_session(path, use_cache=True)
    assert s.doc.broken_count == n and len(s.doc.broken_lines) == BROKEN_KEEP
    dialog = BrokenLinesDialog(s)
    qtbot.addWidget(dialog)
    if dialog.row_count() < n:
        with qtbot.waitSignal(dialog.loaded, timeout=TIMEOUT):
            pass
    assert dialog.row_count() == n
    assert dialog.line_at(n - 1).line == 2 * n
    assert "ta buzuq qator" in dialog.summary_label.text()
    dialog.grid.scroll_to(n - 1)                               # virtual grid: no per-row widgets
