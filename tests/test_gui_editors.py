"""gui/editors.py: the editing widgets and dialogs (docs/ARCHITECTURE.md §12.9).

* text helpers: the JSON number grammar, the views' line-editor rule, the escaped form (round trip of every
  edge string incl. unpaired surrogates and separators; error positions), UTF-16 positions and the error
  span with leading whitespace, Cyrillic, emoji, line ends and the end of the text;
* :class:`InlineEditor` over a real VirtualGrid: placement (incl. the tree column's indentation), exact
  number tokens, invalid numbers kept open, strings shown DECODED and committed as JSON tokens (Uzbek
  Latin / Cyrillic, apostrophes, quotes, backslashes), the escaped mode for texts a line edit cannot hold,
  keys (empty / taken), JSON values, Enter / keypad Enter / Esc, unchanged = cancel, focus-out rules (popup
  and window switches do not count), keys never reaching the grid, following scrolling, cancelling when the
  row disappears, long texts (no 32767 cut);
* :class:`StringDialog`: exact round trips (CRLF / LF / CR line endings kept, NBSP, U+2028, NUL, tab), the
  forced escaped mode (mixed line endings, U+2029, U+FDD0, unpaired surrogates), toggling, counts, wrap,
  the 4 MiB refusal, the "discard changes?" question, the node loader on a real session;
* :class:`JsonEditDialog`: validation status, OK only when valid, the red wavy underline at the right UTF-16
  position, the debounce, big texts validated / formatted in a task with stale results dropped, Format with
  exact numbers (undoable), sanitized separators, Ctrl+Enter, the 4 MiB refusal, the node loader (plain and
  composite nodes, exact tokens);
* :class:`KeyDialog`, :class:`InsertDialog`, :func:`type_menu`, :func:`load_edit_value`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

import jsongen
from jsonreshape import codec, editops
from jsonreshape.errors import EditError
from jsonreshape.gui import editors, widgets
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.editors import (
    InlineEditor,
    InsertDialog,
    InsertResult,
    JsonEditDialog,
    KeyDialog,
    StringDialog,
    error_span,
    escape_text,
    inline_editable,
    is_json_number,
    load_edit_value,
    type_menu,
    unescape_text,
    utf16_offset,
)
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession, NodeRef
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui

TIMEOUT = 30000

EDGE = [s for s in jsongen.EDGE_STRINGS] + ["a\ud800b", "\udfff", "fd\U0000fdd0x\U0000fdd1", "nbsp\xa0",
                                            "bom\U0000feff", "mixed\r\nand\nlf", "only\rcr", "del\x7f", "c1\x85"]


@pytest.fixture(autouse=True)
def _answer_yes(monkeypatch: pytest.MonkeyPatch):
    """"Discard changes?" questions are answered "yes" (tests that need "no" set their own handler)."""
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)


def flush_deletes() -> None:
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ======================================================================================
# text helpers
# ======================================================================================
@pytest.mark.parametrize("text", ["0", "-0", "-0.0", "0.1", "1.10", "1e400", "1E-7", "-2.5e+3", "12345678901234567890",
                                  " 7 ", "\t\n42\r\n"])
def test_json_numbers(text: str) -> None:
    assert is_json_number(text)


@pytest.mark.parametrize("text", ["", " ", "+1", "01", "-01", ".5", "1.", "1e", "1e+", "NaN", "Infinity", "-", "0x10",
                                  "1 2", "1,5", "--1", "١٢", "１２", "1_000", "true"])
def test_not_json_numbers(text: str) -> None:
    assert not is_json_number(text)


def test_inline_rule() -> None:
    assert inline_editable("O'zbekiston Ўзбекистон")
    assert inline_editable("a" * editors.INLINE_MAX_CHARS)
    assert not inline_editable("a" * (editors.INLINE_MAX_CHARS + 1))
    for brk in ("\n", "\r", "\U00002028", "\U00002029", "\x85", "\x0b", "\x0c"):
        assert not inline_editable(f"a{brk}b"), repr(brk)
    assert inline_editable("tab\there")


@pytest.mark.parametrize("value", EDGE)
def test_escaped_form_round_trips(value: str) -> None:
    text = escape_text(value)
    assert unescape_text(text) == value
    # nothing a Qt text widget would drop or turn into a line break is left raw
    assert not any(ch in text for ch in "\n\r\U00002028\U00002029\U0000fdd0\U0000fdd1\x85\x7f")
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def test_escaped_form_errors_point_into_the_text() -> None:
    with pytest.raises(EditError) as err:
        unescape_text('say "hi"')
    assert err.value.key == "gui.editors.error.escaped" and err.value.char_offset == 4
    assert err.value.params["column"] == 5
    with pytest.raises(EditError) as err:
        unescape_text("bad \\x escape")
    assert err.value.char_offset == 4 or err.value.char_offset == 5
    with pytest.raises(EditError) as err:
        unescape_text("raw\ttab")
    assert err.value.char_offset == 3
    with pytest.raises(EditError):
        unescape_text("ends with \\")
    assert unescape_text("") == ""
    assert unescape_text("\\u0416\\n\\\\") == "Ж\n\\"


def test_utf16_positions() -> None:
    text = "a😀bЎ😀c"
    assert [utf16_offset(text, i) for i in range(len(text) + 1)] == [0, 1, 3, 4, 5, 7, 8]
    assert utf16_offset(text, 99) == 8 and utf16_offset(text, -3) == 0


@pytest.mark.parametrize("text, offset, expected", [
    ("  [1, 2,, 3]", 8, (8, 9)),                          # leading whitespace
    ('{"Ўзбек": x}', 10, (10, 11)),                       # Cyrillic: one unit each
    ('["😀", x]', 6, (7, 8)),                              # an emoji before the error: two units
    ('["😀😀", 😀]', 7, (9, 11)),                           # the error character itself is an emoji
    ("[1, 2\n", 6, (4, 5)),                                # the end after a line end: the last character
    ("[1, 2", 5, (4, 5)),                                  # the end of the text
    ('"ab\ncd"', 3, (2, 3)),                               # at a line break: the character before it
    ("", 0, (0, 0)),
    ("\n\n", 2, (0, 0)),
])
def test_error_span(text: str, offset: int, expected: tuple[int, int]) -> None:
    assert error_span(text, offset) == expected


# ======================================================================================
# InlineEditor
# ======================================================================================
class _Rows:
    def __init__(self, n: int) -> None:
        self.n = n

    def row_count(self) -> int:
        return self.n

    def cell(self, row: int, column: Column) -> Cell:
        if column.id == "key":
            return Cell(text=f"key{row}", depth=2, badge="string", expander=0)
        return Cell(text=f"value {row}")


@pytest.fixture
def grid(qtbot):
    g = VirtualGrid()
    g.rows_source = _Rows(300)
    g.set_source(g.rows_source)
    g.set_columns([Column("key", "Key", 160), Column("value", "Value", 220)])
    g.resize(520, 320)
    qtbot.addWidget(g)
    g.show()
    qtbot.waitExposed(g)
    return g


def open_editor(grid, kind: str, text: str, row: int = 2, col: int = 1, **kw):
    out: dict = {"commit": [], "cancel": 0}

    def cancelled() -> None:
        out["cancel"] += 1

    ed = InlineEditor(grid, row, col, kind, text, out["commit"].append, cancelled, **kw)
    return ed, out


def test_inline_editor_sits_over_the_cell(grid) -> None:
    ed, _out = open_editor(grid, "number", "1.10")
    rect = grid.cell_rect(2, 1)
    assert ed.parent() is grid.viewport()
    assert ed.geometry().topLeft() == rect.topLeft()
    assert ed.height() == rect.height() and ed.width() >= rect.width()
    assert ed.isVisible() and ed.text() == "1.10" and ed.selectedText() == "1.10"
    assert ed.is_valid() and ed.error_text() == ""


def test_inline_editor_skips_the_tree_indentation(grid) -> None:
    grid.set_tree_column(0)
    ed, _out = open_editor(grid, "key", "key2", col=0)
    rect = grid.cell_rect(2, 0)
    assert ed.geometry().x() > rect.x() + grid.indent()          # depth 2 + badge are not covered
    ed2, _ = open_editor(grid, "key", "key3", row=3, col=0, inset=0)
    assert ed2.geometry().x() == grid.cell_rect(3, 0).x()


@pytest.mark.parametrize("token", ["0", "-0", "-0.0", "0.1", "1.10", "1e400", "-1E-7", "12345678901234567890",
                                   "2.5e+3"])
def test_number_tokens_are_committed_exactly(grid, token: str) -> None:
    ed, out = open_editor(grid, "number", "7")
    ed.setText(f"  {token} ")
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert out == {"commit": [token], "cancel": 0}
    flush_deletes()
    assert not shiboken6.isValid(ed)


@pytest.mark.parametrize("text", ["", "+1", "01", ".5", "1.", "NaN", "1 2", "1,5", "0x1F"])
def test_invalid_numbers_keep_the_editor_open(grid, text: str) -> None:
    ed, out = open_editor(grid, "number", "7")
    seen: list[bool] = []
    ed.validity_changed.connect(seen.append)
    ed.setText(text)
    assert not ed.is_valid() and ed.error_text() == t("gui.editors.error.number")
    assert ed.toolTip() == t("gui.editors.error.number")
    assert seen == [False]
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert out == {"commit": [], "cancel": 0} and not ed.done and ed.isVisible()
    with pytest.raises(EditError):
        ed.value_text()
    ed.setText("8")
    assert ed.is_valid() and seen == [False, True]
    QTest.keyClick(ed, Qt.Key.Key_Enter)                           # keypad Enter
    assert out == {"commit": ["8"], "cancel": 0}


def test_escape_cancels_and_unchanged_is_a_cancel(grid) -> None:
    ed, out = open_editor(grid, "number", "1.10")
    ed.setText("2")
    QTest.keyClick(ed, Qt.Key.Key_Escape)
    assert out == {"commit": [], "cancel": 1} and ed.done
    ed2, out2 = open_editor(grid, "number", "1.10")
    ed2.setText(" 1.10 ")                                          # the same token
    QTest.keyClick(ed2, Qt.Key.Key_Return)
    assert out2 == {"commit": [], "cancel": 1}
    ed3, out3 = open_editor(grid, "string", "abc")
    assert ed3.commit() is True
    assert out3 == {"commit": [], "cancel": 1}
    assert ed3.commit() is False                                    # already done
    ed3.cancel()
    assert out3["cancel"] == 1


def test_editor_keys_never_reach_the_grid(grid, qtbot) -> None:
    activated: list = []
    grid.activated.connect(lambda row, col: activated.append(row))
    grid.set_current(5, 1)
    ed, out = open_editor(grid, "number", "1", row=5)
    for key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
        QTest.keyClick(ed, key)
    assert grid.current() == (5, 1)
    ed.setText("2")
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert out["commit"] == ["2"] and activated == []
    ed2, out2 = open_editor(grid, "number", "1", row=6)
    QTest.keyClick(ed2, Qt.Key.Key_Escape)
    assert out2["cancel"] == 1 and activated == []


@pytest.mark.parametrize("value", ["Toshkent", "Тошкент", "O'zbekiston", "Oʻzbekiston", "O‘zbekiston", "G'ulom",
                                   'quote"inside', "back\\slash", "tab\there", "emoji 😀 test", "nul\u0000char", "",
                                   "null", "123", "nbsp\xa0", "Straße"])
def test_strings_are_shown_decoded_and_committed_as_tokens(grid, value: str) -> None:
    ed, out = open_editor(grid, "string", value)
    assert ed.text() == value and not ed.escaped
    new = value + " Ғ'"
    ed.setText(new)
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert out["commit"] == [editops.encode_string(new).decode("utf-8")]
    assert codec.loads(out["commit"][0]) == new


@pytest.mark.parametrize("value", ["a\ud800b", "\udfff", "new\nline", "cr\rlf", "line sep \U00002028 para \U00002029",
                                   "a\r\nb"])
def test_texts_a_line_edit_cannot_hold_are_escaped(grid, value: str) -> None:
    ed, out = open_editor(grid, "string", value)
    assert ed.escaped and ed.text() == escape_text(value)
    assert ed.toolTip() == t("gui.editors.escaped.tip")
    QTest.keyClick(ed, Qt.Key.Key_Return)                          # unchanged
    assert out == {"commit": [], "cancel": 1}
    ed2, out2 = open_editor(grid, "string", value)
    ed2.setText(ed2.text() + "Ж")
    QTest.keyClick(ed2, Qt.Key.Key_Return)
    assert codec.loads(out2["commit"][0]) == value + "Ж"
    ed3, out3 = open_editor(grid, "string", value)
    ed3.setText(ed3.text() + '"')                                  # an unescaped quote
    assert not ed3.is_valid()
    QTest.keyClick(ed3, Qt.Key.Key_Return)
    assert out3 == {"commit": [], "cancel": 0}


def test_key_editor(grid) -> None:
    ed, out = open_editor(grid, "key", "name", taken={"name", "age", "Ёш"})
    ed.setText("")
    assert not ed.is_valid() and ed.error_text() == t("gui.editors.error.key_empty")
    ed.setText("age")
    assert ed.error_text() == t("gui.editors.error.key_exists", key="age")
    ed.setText("Ёш")
    assert not ed.is_valid()
    ed.setText("name")                                             # its own key is fine
    assert ed.is_valid()
    ed.setText("  ")                                               # any characters, only non-empty
    assert ed.is_valid()
    ed.setText('to\'liq "ism"')
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert out["commit"] == ['to\'liq "ism"']                      # the key itself, not JSON
    ed2, out2 = open_editor(grid, "key", "a\ud800")
    assert ed2.escaped
    ed2.setText("b\\ud800")
    QTest.keyClick(ed2, Qt.Key.Key_Return)
    assert out2["commit"] == ["b\ud800"]


def test_json_editor_kind(grid) -> None:
    ed, out = open_editor(grid, "json", "null")
    ed.setText(' {"a": [1.10, 1e400]} ')
    assert ed.is_valid()
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert out["commit"] == ['{"a": [1.10, 1e400]}']
    ed2, out2 = open_editor(grid, "json", "null")
    ed2.setText('{"a":')
    assert not ed2.is_valid() and "1-qator" in ed2.error_text()
    assert ed2.commit() is False and out2["commit"] == []


def test_focus_out_rules(grid) -> None:
    def focus_out(ed, reason) -> None:
        QApplication.sendEvent(ed, QFocusEvent(QEvent.Type.FocusOut, reason))

    ed, out = open_editor(grid, "number", "1")
    ed.setText("2")
    focus_out(ed, Qt.FocusReason.PopupFocusReason)                 # its context menu: still editing
    focus_out(ed, Qt.FocusReason.ActiveWindowFocusReason)          # another window: still editing
    assert out == {"commit": [], "cancel": 0} and not ed.done
    focus_out(ed, Qt.FocusReason.MouseFocusReason)
    assert out == {"commit": ["2"], "cancel": 0}
    ed2, out2 = open_editor(grid, "number", "1", commit_on_focus_out=False)
    ed2.setText("3")
    focus_out(ed2, Qt.FocusReason.TabFocusReason)
    assert out2 == {"commit": [], "cancel": 1}
    ed3, out3 = open_editor(grid, "number", "1")
    ed3.setText("x")                                               # invalid: discarded, never committed
    focus_out(ed3, Qt.FocusReason.MouseFocusReason)
    assert out3 == {"commit": [], "cancel": 1}


def test_real_focus_changes(qtbot) -> None:
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

    window = QWidget()
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    g = VirtualGrid()
    g.set_source(_Rows(20))
    g.set_columns([Column("value", "Value", 200)])
    other = QLineEdit()
    layout.addWidget(g)
    layout.addWidget(other)
    window.resize(400, 400)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow, timeout=TIMEOUT)
    ed, out = open_editor(g, "number", "1", row=1, col=0)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is ed, timeout=TIMEOUT)
    ed.setText("5")
    other.setFocus()                                               # the user clicked elsewhere
    qtbot.waitUntil(lambda: bool(out["commit"]), timeout=TIMEOUT)
    assert out == {"commit": ["5"], "cancel": 0} and QApplication.focusWidget() is other
    ed2, out2 = open_editor(g, "number", "1", row=2, col=0)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is ed2, timeout=TIMEOUT)
    QTest.keyClick(ed2, Qt.Key.Key_Escape)
    assert out2["cancel"] == 1 and QApplication.focusWidget() is g   # Enter / Esc give the focus back


def test_editor_follows_scrolling_and_cancels_when_its_row_goes(grid, qtbot) -> None:
    ed, out = open_editor(grid, "number", "1", row=4)
    grid.scroll_to(100, "top")
    assert ed.geometry().y() == grid.cell_rect(4, 1).y() < 0
    grid.scroll_to(0, "top")
    assert ed.geometry().y() == grid.cell_rect(4, 1).y() >= 0
    grid.rows_source.n = 3
    grid.rows_changed()
    qtbot.waitUntil(lambda: ed.done, timeout=TIMEOUT)
    assert out == {"commit": [], "cancel": 1}


def test_an_editor_for_a_missing_cell_cancels_after_the_constructor(grid, qtbot) -> None:
    ed, out = open_editor(grid, "number", "1", row=999)              # no such row
    assert out == {"commit": [], "cancel": 0} and not ed.isVisible()  # the caller gets the editor first
    qtbot.waitUntil(lambda: out["cancel"] == 1, timeout=TIMEOUT)
    assert out["commit"] == []
    grid.set_column_visible(1, False)
    ed2, out2 = open_editor(grid, "number", "1", row=1, col=1)       # a hidden column
    qtbot.waitUntil(lambda: out2["cancel"] == 1, timeout=TIMEOUT)


def test_long_texts_are_not_cut(grid) -> None:
    value = "Ў" * 50_000
    ed, out = open_editor(grid, "string", value)
    assert ed.text() == value
    ed.setText(value + "x")
    assert ed.commit()
    assert codec.loads(out["commit"][0]) == value + "x"


def test_unknown_kind(grid) -> None:
    with pytest.raises(ValueError):
        InlineEditor(grid, 0, 0, "date", "x", print)


# ======================================================================================
# StringDialog
# ======================================================================================
@pytest.fixture
def dialogs(qtbot):
    made: list = []

    def keep(dlg):
        qtbot.addWidget(dlg)
        made.append(dlg)
        return dlg

    return keep


def type_at_end(edit: QPlainTextEdit, text: str) -> None:
    edit.moveCursor(edit.textCursor().MoveOperation.End)
    edit.insertPlainText(text)


@pytest.mark.parametrize("value, newline", [("a\r\nb\r\nc", "\r\n"), ("a\nb", "\n"), ("a\rb", "\r"), ("one line", "\n"),
                                            ("nbsp\xa0 ls\U00002028x nul\x00 tab\t Ўзбек 😀", "\n"), ("", "\n")])
def test_string_dialog_keeps_line_endings(dialogs, value: str, newline: str) -> None:
    dlg = dialogs(StringDialog(None, value))
    assert not dlg.escaped and not dlg.note.isVisible() and dlg.value() == value
    assert dlg.ok_button.isEnabled() and not dlg.is_modified()
    got: list[str] = []
    dlg.value_accepted.connect(got.append)
    dlg.accept()                                                    # unchanged: no value
    assert got == [] and dlg.result() == 1
    dlg2 = dialogs(StringDialog(None, value))
    type_at_end(dlg2.editor, "\nyangi qator")
    expected = value + newline + "yangi qator"
    assert dlg2.value() == expected and dlg2.is_modified()
    calls: list[str] = []
    dlg2._on_accept = calls.append
    dlg2.value_accepted.connect(got.append)
    dlg2.accept()
    token = editops.encode_string(expected).decode("utf-8")
    assert got == [token] and calls == [token] and codec.loads(token) == expected


@pytest.mark.parametrize("value, what", [
    ("mixed\r\nand\nlf", "gui.editors.special.mixed_newlines"),
    ("para \U00002029 sep", "gui.editors.special.separators"),
    ("frame \U0000fdd0", "gui.editors.special.separators"),
    ("lone \ud800 surrogate", "gui.editors.special.surrogates"),
])
def test_string_dialog_escapes_what_plain_text_cannot_keep(dialogs, value: str, what: str) -> None:
    dlg = dialogs(StringDialog(None, value))
    assert dlg.escaped and dlg.escape_check.isChecked()
    assert t(what) in dlg.note.text() and not dlg.note.isHidden()
    assert dlg.value() == value and not dlg.is_modified()
    assert dlg.set_escaped(False) is False                          # plain text would change it
    assert dlg.escaped and t(what) in dlg.error_label.text()
    type_at_end(dlg.editor, "\\tЖ")
    got: list[str] = []
    dlg.value_accepted.connect(got.append)
    dlg.accept()
    assert codec.loads(got[0]) == value + "\tЖ"


def test_string_dialog_toggle_and_invalid_escapes(dialogs) -> None:
    value = 'Toshkent "markaz"\n\tko\'cha'
    dlg = dialogs(StringDialog(None, value))
    assert not dlg.escaped
    assert dlg.set_escaped(True) and dlg.escaped
    assert dlg.editor.toPlainText() == escape_text(value)
    assert dlg.value() == value
    type_at_end(dlg.editor, "\\q")                                  # not a JSON escape
    dlg._update()
    assert not dlg.ok_button.isEnabled() and dlg.error_label.text()
    assert dlg.count_label.text() == t("gui.editors.string.count_invalid")
    assert dlg.set_escaped(False) is False and dlg.escaped
    dlg.editor.setPlainText(escape_text(value + "!"))
    assert dlg.set_escaped(False) and not dlg.escaped
    assert dlg.value() == value + "!" and dlg.editor.toPlainText() == value + "!"
    dlg._update()
    assert dlg.ok_button.isEnabled()


def test_string_dialog_counts_and_wrap(dialogs) -> None:
    dlg = dialogs(StringDialog(None, "ab\r\ncd😀"))
    assert dlg.count_label.text() == t("gui.editors.string.count", count="7", lines="2")
    assert dlg.editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth
    dlg.wrap_check.setChecked(False)
    assert dlg.editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap


def test_string_dialog_refuses_huge_texts(dialogs, monkeypatch) -> None:
    monkeypatch.setattr(editors, "STRING_DIALOG_MAX", 16)
    dlg = dialogs(StringDialog(None, "Ў" * 9))                      # 18 bytes of UTF-8
    assert dlg.refused and not dlg.note.isHidden()
    assert t("gui.editors.string.too_large.hint") in dlg.note.text()
    assert dlg.editor.isHidden() and dlg.ok_button.isHidden()
    assert dlg.cancel_button.text() == t("gui.common.close")
    dlg.accept()
    assert dlg.result() == 0 and dlg.isVisible() is False
    ok = dialogs(StringDialog(None, "Ў" * 8))
    assert not ok.refused
    type_at_end(ok.editor, "abc")                                   # typed past the limit
    ok._update()
    assert not ok.ok_button.isEnabled() and ok.error_label.text()


def test_string_dialog_asks_before_dropping_changes(dialogs, monkeypatch, qtbot) -> None:
    answers: list[str] = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: answers.append(text) or False)
    dlg = dialogs(StringDialog(None, "abc"))
    dlg.show()
    dlg.reject()                                                    # unchanged: closes at once
    assert answers == [] and not dlg.isVisible()
    dlg2 = dialogs(StringDialog(None, "abc"))
    dlg2.show()
    type_at_end(dlg2.editor, "d")
    dlg2.reject()
    qtbot.waitUntil(lambda: bool(answers), timeout=TIMEOUT)
    qtbot.wait(20)
    assert answers == [t("gui.editors.discard.question")] and dlg2.isVisible()
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)
    dlg2.reject()
    qtbot.waitUntil(lambda: not dlg2.isVisible(), timeout=TIMEOUT)
    dlg3 = dialogs(StringDialog(None, "abc"))
    dlg3.show()
    type_at_end(dlg3.editor, "d")
    dlg3.discard()                                                  # no question
    assert not dlg3.isVisible()


# ======================================================================================
# JsonEditDialog
# ======================================================================================
def test_json_dialog_status_and_ok(dialogs) -> None:
    dlg = dialogs(JsonEditDialog(None, '{"a": [1.10, 1e400], "s": "Ўзбек"}'))
    assert dlg.is_valid() and dlg.ok_button.isEnabled() and dlg.format_button.isEnabled()
    assert dlg.status_label.text() == t("gui.editors.json.valid", type=t("gui.editors.type.object"))
    assert dlg.validation_path == "gui" and dlg.error_selection() is None and dlg.value_kind == "object"
    bad = dialogs(JsonEditDialog(None, "[1, 2,, 3]"))
    assert not bad.is_valid() and not bad.ok_button.isEnabled() and not bad.format_button.isEnabled()
    assert bad.status_label.text().startswith("✗") and "1-qator, 7-ustun" in bad.status_label.text()
    got: list[str] = []
    bad.value_accepted.connect(got.append)
    bad.accept()
    assert got == [] and bad.result() == 0


@pytest.mark.parametrize("text, underlined", [
    ("  [1, 2,, 3]", ","),
    ("\n  {\"Ўзбек\": 😀}", "😀"),
    ('["😀", "Ғ", x]', "x"),
    ('{"a": "b"\n "c": 1}', '"'),
    ("[1, 2\n", "2"),
    ('{"k": "line\nbreak"}', "e"),
])
def test_json_dialog_underlines_the_error(dialogs, text: str, underlined: str) -> None:
    dlg = dialogs(JsonEditDialog(None, text))
    assert not dlg.is_valid()
    start, end = dlg.error_selection()
    sel = dlg.editor.extraSelections()[0]
    assert sel.cursor.selectedText() == underlined
    assert sel.format.underlineStyle() == sel.format.UnderlineStyle.WaveUnderline
    err = dlg.error
    assert err is not None and err.char_offset is not None
    assert (start, end) == error_span(dlg.text(), err.char_offset)


def test_json_dialog_validation_is_debounced(dialogs, qtbot) -> None:
    dlg = dialogs(JsonEditDialog(None, "[1]"))
    assert dlg.is_valid()
    dlg.editor.setPlainText("[1, 2]")
    assert not dlg.is_valid() and not dlg.ok_button.isEnabled()
    assert dlg.status_label.text() == t("gui.editors.json.checking")
    qtbot.waitUntil(dlg.is_valid, timeout=TIMEOUT)
    assert dlg.ok_button.isEnabled()
    dlg.editor.setPlainText("[1, 2")                                # Ctrl+Enter before the debounce fired
    QTest.keyClick(dlg.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert dlg.result() == 0 and not dlg.is_valid() and dlg.error is not None


def test_json_dialog_accept_and_ctrl_enter(dialogs) -> None:
    dlg = dialogs(JsonEditDialog(None, "null"))
    got: list[str] = []
    calls: list[str] = []
    dlg.value_accepted.connect(got.append)
    dlg._on_accept = calls.append
    dlg.show()
    dlg.editor.setPlainText('{"yangi": "qiymat", "n": 1.10}')
    QTest.keyClick(dlg.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert got == ['{"yangi": "qiymat", "n": 1.10}'] == calls and dlg.result() == 1
    same = dialogs(JsonEditDialog(None, "[1]"))
    same.value_accepted.connect(got.append)
    same.accept()                                                    # unchanged: no value
    assert same.result() == 1 and len(got) == 1


def test_json_dialog_format_keeps_exact_tokens(dialogs) -> None:
    text = '{"a":1.10,"b":[1e400,-0.0,12345678901234567890],"s":"\\u0416 Ўзбек","e":{},"f":[]}'
    dlg = dialogs(JsonEditDialog(None, text, indent=4))
    assert dlg.indent() == 4
    dlg.format_text()
    assert dlg.text() == ('{\n    "a": 1.10,\n    "b": [\n        1e400,\n        -0.0,\n        12345678901234567890\n'
                          '    ],\n    "s": "\\u0416 Ўзбек",\n    "e": {},\n    "f": []\n}')
    assert dlg.is_valid()
    dlg.set_indent("\t")
    dlg.format_text()
    assert dlg.text().startswith('{\n\t"a": 1.10,\n\t"b": [\n\t\t1e400')
    dlg.editor.undo()
    assert dlg.text().startswith('{\n    "a": 1.10')                 # Format is one undo step
    dlg.editor.undo()
    assert dlg.text() == text


def test_json_dialog_sanitizes_separators(dialogs) -> None:
    value = "line sep \U00002028 para \U00002029 frame \U0000fdd0"
    text = '{"s": "' + value + '"}'
    dlg = dialogs(JsonEditDialog(None, text))
    assert dlg.is_valid()
    assert "\U00002029" not in dlg.text() and "\U00002028" not in dlg.text()
    assert codec.loads(dlg.text()) == {"s": value}


def test_json_dialog_big_texts_use_a_task(dialogs, qtbot) -> None:
    big = "[" + ", ".join(["1.10"] * 60_000) + "]"
    assert len(big.encode()) > editors.JSON_SYNC_MAX
    dlg = dialogs(JsonEditDialog(None, big))
    assert dlg.validation_path == "task"
    qtbot.waitUntil(dlg.is_valid, timeout=TIMEOUT)
    assert dlg.ok_button.isEnabled()
    dlg.editor.setPlainText(big[:-1] + ", x]")
    dlg._validate_now()
    qtbot.waitUntil(lambda: dlg.error is not None, timeout=TIMEOUT)
    assert dlg.validation_path == "task" and not dlg.ok_button.isEnabled()
    assert dlg.editor.extraSelections()[0].cursor.selectedText() == "x"
    dlg.editor.setPlainText(big)
    dlg._validate_now()
    qtbot.waitUntil(dlg.is_valid, timeout=TIMEOUT)
    dlg.format_text()                                                # big: in a task, then applied
    qtbot.waitUntil(lambda: dlg.text().startswith("[\n  1.10,\n  1.10"), timeout=TIMEOUT)
    qtbot.waitUntil(dlg.is_valid, timeout=TIMEOUT)


def test_json_dialog_drops_stale_background_results(dialogs, qtbot, monkeypatch) -> None:
    gate = threading.Event()
    calls: list[int] = []
    real = editors._check_json_text

    def slow(text: str, data: bytes):
        calls.append(len(data))
        if len(calls) == 1:
            gate.wait(20)
        return real(text, data)

    monkeypatch.setattr(editors, "_check_json_text", slow)
    big = "[" + ", ".join(["7"] * 100_000) + "]"
    dlg = dialogs(JsonEditDialog(None, big))                         # the first check waits on the gate
    results: list[bool] = []
    dlg.validated.connect(results.append)
    assert dlg.validation_path == "task"
    dlg.editor.setPlainText(big[:-1])                                # invalid: "]" missing
    dlg._validate_now()                                              # one check at a time: queued behind it
    dlg.editor.setPlainText(big[:-2] + "x]")                         # changed again (still invalid)
    dlg._validate_now()
    assert len(calls) == 1 and not dlg.is_valid() and not dlg.ok_button.isEnabled()
    gate.set()                                                       # the stale (valid) result arrives now
    qtbot.waitUntil(lambda: dlg.error is not None, timeout=TIMEOUT)
    assert dlg._own_tasks.wait_idle(20)
    assert len(calls) == 2                                           # the newest text only, not every change
    assert results == [False] and not dlg.is_valid() and not dlg.ok_button.isEnabled()
    assert dlg.editor.extraSelections()[0].cursor.selectedText() == "x"


@pytest.fixture
def backend(core_backend, monkeypatch):
    """check_json (editops) and the Reformatter of the editors on one core backend."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = editops.set_core_backend(name)
    monkeypatch.setattr(editors, "core", core_backend)
    yield name
    editops.set_core_backend(old)


def test_validation_and_format_on_every_backend(backend, dialogs) -> None:
    cases = {"  [1, 2,, 3]": ",", '["😀", "Ғ", x]': "x", "\n  {\"Ўзбек\": 😀}": "😀", "[1, 2\n": "2"}
    for text, underlined in cases.items():
        dlg = dialogs(JsonEditDialog(None, text))
        assert dlg.editor.extraSelections()[0].cursor.selectedText() == underlined, (backend, text)
    dlg = dialogs(JsonEditDialog(None, '{"a":[1.10,1e400,-0.0],"s":"O\\u2018zbek Ўзбек"}', indent="\t"))
    dlg.format_text()
    assert dlg.text() == '{\n\t"a": [\n\t\t1.10,\n\t\t1e400,\n\t\t-0.0\n\t],\n\t"s": "O\\u2018zbek Ўзбек"\n}'
    assert unescape_text(escape_text("a\ud800\n\U00002029b")) == "a\ud800\n\U00002029b"


def test_large_strings_round_trip(dialogs) -> None:
    value = ("Ўзбекистон O'zbekiston 😀\r\n" * 60_000)[:-2]               # ~1.9 MB, CRLF lines
    dlg = dialogs(StringDialog(None, value))
    assert not dlg.refused and not dlg.escaped and dlg.value() == value and not dlg.is_modified()
    type_at_end(dlg.editor, "\n!")
    assert dlg.value() == value + "\r\n!"


def test_json_dialog_refuses_huge_texts(dialogs, monkeypatch) -> None:
    monkeypatch.setattr(editors, "JSON_DIALOG_MAX", 10)
    dlg = dialogs(JsonEditDialog(None, '{"a": "12345"}'))
    assert dlg.refused and t("gui.editors.json.too_large.hint") in dlg.note.text()
    assert dlg.ok_button.isHidden() and dlg.editor.isHidden()
    dlg.accept()
    assert dlg.result() == 0


# ======================================================================================
# KeyDialog, InsertDialog, type_menu
# ======================================================================================
def test_key_dialog(dialogs) -> None:
    dlg = dialogs(KeyDialog(None, "ism", taken={"ism", "yosh"}))
    assert dlg.ok_button.isEnabled() and not dlg.escaped
    dlg.edit.setText("")
    assert not dlg.ok_button.isEnabled() and dlg.error_label.text() == t("gui.editors.error.key_empty")
    dlg.edit.setText("yosh")
    assert not dlg.ok_button.isEnabled()
    got: list[str] = []
    dlg.key_accepted.connect(got.append)
    dlg.edit.setText("to'liq ism")
    dlg.accept()
    assert got == ["to'liq ism"] and dlg.result() == 1
    same = dialogs(KeyDialog(None, "ism"))
    same.key_accepted.connect(got.append)
    same.accept()
    assert len(got) == 1 and same.result() == 1
    esc = dialogs(KeyDialog(None, "line\nkey"))
    assert esc.escaped and esc.edit.text() == "line\\nkey"
    esc.edit.setText("line\\r\\nkey")
    esc.key_accepted.connect(got.append)
    esc.accept()
    assert got[-1] == "line\r\nkey"


def test_insert_dialog_positions_and_keys(dialogs) -> None:
    dlg = dialogs(InsertDialog(None, key_needed={"child": True}, taken={"child": {"a", "b"}}))
    assert dlg.position() == "after" and not dlg.key_needed() and dlg.key_edit.isHidden()
    assert dlg.ok_button.isEnabled()
    assert dlg.result() == InsertResult("after", None, '""', "string")
    dlg.set_position("child")
    assert dlg.key_needed() and not dlg.key_edit.isHidden()
    assert not dlg.ok_button.isEnabled() and dlg.error_label.text() == t("gui.editors.error.key_required")
    dlg.key_edit.setText("a")
    assert not dlg.ok_button.isEnabled()
    dlg.key_edit.setText("c")
    assert dlg.ok_button.isEnabled()
    dlg.string_edit.setText('Ўзбек "matn"')
    got: list[InsertResult] = []
    dlg.insert_accepted.connect(got.append)
    dlg.accept()
    assert got == [InsertResult("child", "c", '"Ўзбек \\"matn\\""', "string")]
    record_level = dialogs(InsertDialog(None, positions=("before", "after"), position="before", key_needed=True,
                                        taken={"id1"}, key="id2"))
    assert list(record_level.position_buttons) == ["before", "after"] and record_level.position() == "before"
    assert record_level.result().key == "id2"
    with pytest.raises(ValueError):
        InsertDialog(None, positions=())
    with pytest.raises(ValueError):
        record_level.set_position("child")


@pytest.mark.parametrize("kind, setup, value", [
    ("number", lambda d: d.number_edit.setText(" 1.10 "), "1.10"),
    ("number", lambda d: d.number_edit.setText("1e400"), "1e400"),
    ("bool", lambda d: d.bool_combo.setCurrentIndex(1), "false"),
    ("bool", lambda d: None, "true"),
    ("null", lambda d: None, "null"),
    ("object", lambda d: None, "{}"),
    ("array", lambda d: None, "[]"),
    ("json", lambda d: d.json_edit.setPlainText(' {"a": [12345678901234567890]} '), '{"a": [12345678901234567890]}'),
])
def test_insert_dialog_values(dialogs, kind: str, setup, value: str) -> None:
    dlg = dialogs(InsertDialog(None))
    dlg.set_value_type(kind)
    setup(dlg)
    if kind == "json":
        dlg._check_json_now()
    assert dlg.ok_button.isEnabled()
    assert dlg.result() == InsertResult("after", None, value, kind)


def test_insert_dialog_invalid_values(dialogs, qtbot) -> None:
    dlg = dialogs(InsertDialog(None, value_type="number", value="+1"))
    assert not dlg.ok_button.isEnabled() and dlg.error_label.text() == t("gui.editors.error.number")
    with pytest.raises(EditError):
        dlg.result()
    dlg.set_value_type("json")
    dlg.json_edit.setPlainText("[1,")
    assert not dlg.ok_button.isEnabled()                              # waiting for the debounced check
    qtbot.waitUntil(lambda: bool(dlg.error_label.text()), timeout=TIMEOUT)
    assert "1-qator" in dlg.error_label.text()
    dlg.json_edit.setPlainText("[1]")
    qtbot.waitUntil(dlg.ok_button.isEnabled, timeout=TIMEOUT)


def test_type_menu(qtbot) -> None:
    picked: list[str] = []
    menu = type_menu(None, "object", picked.append)
    acts = {a.objectName(): a for a in menu.actions()}
    assert list(acts) == [f"type_{k}" for k in editors.MENU_TYPES]
    assert acts["type_object"].isChecked() and not acts["type_object"].isEnabled()
    assert not acts["type_number"].isEnabled() and not acts["type_bool"].isEnabled()
    assert acts["type_number"].toolTip() == t("gui.editors.type.impossible", source=t("edits.type.object"),
                                              target=t("edits.type.number"))
    assert acts["type_string"].isEnabled() and acts["type_array"].isEnabled() and acts["type_null"].isEnabled()
    acts["type_array"].trigger()
    assert picked == ["array"]
    mixed_menu = type_menu(menu, ["string", "array", "boolean"], picked.append)      # parented: kept alive
    mixed = {a.objectName(): a for a in mixed_menu.actions()}
    assert not mixed["type_object"].isEnabled()                        # array -> object is impossible
    assert not mixed["type_number"].isEnabled()                        # array -> number too
    assert mixed["type_string"].isEnabled() and not mixed["type_string"].isChecked()
    free_menu = type_menu(menu, None, picked.append, title="Tur")
    assert all(a.isEnabled() for a in free_menu.actions()) and free_menu.title() == "Tur"
    assert menu.title() == t("gui.editors.type.menu")


# ======================================================================================
# with a real session: the loaders
# ======================================================================================
@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def open_session(app, qtbot, tmp_path: Path):
    made: list[DocSession] = []

    def factory(content: bytes, name: str = "doc.json") -> DocSession:
        path = tmp_path / name
        path.write_bytes(content)
        s = DocSession(app, str(path), use_cache=False)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


DOC = ('[{"s": "line1\\r\\nline2 \\u00a0 \\u0416 O\\u2018zbek \\ud83d\\ude00", "n": 1.10, '
       '"big": 12345678901234567890, "inf": 1e400, "z": -0.0, "o": {"k": [true, null]}},\n'
       ' {"s": "ikkinchi", "n": 2}]').encode("utf-8")


def test_string_dialog_loader(open_session, dialogs, qtbot, monkeypatch) -> None:
    s = open_session(DOC)
    dlg = dialogs(StringDialog.for_node(s, NodeRef(0, ("s",)), show=False))
    assert dlg.loading and not dlg.ok_button.isEnabled()
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    assert dlg.value() == "line1\r\nline2 \xa0 Ж O‘zbek 😀" and not dlg.escaped and not dlg.refused
    assert dlg.loaded_gen is not None
    other = dialogs(StringDialog.for_node(s, NodeRef(0, ("n",)), show=False))
    qtbot.waitUntil(lambda: other.refused, timeout=TIMEOUT)
    assert t("gui.editors.not_string") in other.note.text()
    monkeypatch.setattr(editors, "STRING_DIALOG_MAX", 5)
    big = dialogs(StringDialog.for_node(s, NodeRef(1, ("s",)), show=False))
    qtbot.waitUntil(lambda: big.refused, timeout=TIMEOUT)
    assert t("gui.editors.string.too_large.hint") in big.note.text()
    assert s.app.errors == []


def test_json_dialog_loader(open_session, dialogs, qtbot, monkeypatch) -> None:
    s = open_session(DOC)
    dlg = dialogs(JsonEditDialog.for_node(s, NodeRef(0, ("o",)), indent=2, show=False))
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    assert dlg.text() == '{\n  "k": [\n    true,\n    null\n  ]\n}' and dlg.is_valid()
    rec = dialogs(JsonEditDialog.for_node(s, NodeRef(0, ()), indent="\t", show=False))
    qtbot.waitUntil(lambda: not rec.loading, timeout=TIMEOUT)
    assert '\t"n": 1.10,\n\t"big": 12345678901234567890,\n\t"inf": 1e400,\n\t"z": -0.0' in rec.text()
    assert '"line1\\r\\nline2 \\u00a0 \\u0416 O\\u2018zbek \\ud83d\\ude00"' in rec.text()   # tokens byte-identical
    assert rec.indent() == "\t"
    root = dialogs(JsonEditDialog.for_node(s, NodeRef(None, ()), show=False))     # the records container
    qtbot.waitUntil(lambda: not root.loading, timeout=TIMEOUT)
    assert codec.loads(root.text())[1] == {"s": "ikkinchi", "n": 2} and root.is_valid()
    monkeypatch.setattr(editors, "JSON_DIALOG_MAX", 30)
    big = dialogs(JsonEditDialog.for_node(s, NodeRef(0, ()), show=False))
    qtbot.waitUntil(lambda: big.refused, timeout=TIMEOUT)
    assert t("gui.editors.json.too_large.hint") in big.note.text()
    assert s.app.errors == []


def test_load_edit_value(open_session, qtbot) -> None:
    s = open_session(DOC)
    got: list = []
    for path in (("n",), ("big",), ("inf",), ("z",), ("s",), ("o",), ("o", "k", 0), ("o", "k", 1)):
        load_edit_value(s, NodeRef(0, path), got.append)
    qtbot.waitUntil(lambda: len(got) == 8, timeout=TIMEOUT)
    by_token = [(v.kind, v.token) for v in got]
    assert by_token[:4] == [("number", "1.10"), ("number", "12345678901234567890"), ("number", "1e400"),
                            ("number", "-0.0")]
    assert got[4].kind == "string" and got[4].decoded == "line1\r\nline2 \xa0 Ж O‘zbek 😀"
    assert got[4].token.startswith('"line1\\r\\n')
    assert got[5].kind == "object" and got[5].token is None and got[5].decoded is None and got[5].size > 0
    assert (got[6].kind, got[6].token) == ("bool", "true") and (got[7].kind, got[7].token) == ("null", "null")
    small: list = []
    load_edit_value(s, NodeRef(0, ("s",)), small.append, max_bytes=4)
    qtbot.waitUntil(lambda: bool(small), timeout=TIMEOUT)
    assert small[0].token is None and small[0].decoded is None and small[0].kind == "string"
    assert s.app.errors == []


# ======================================================================================
# key checks (review3 L1): keys looked up by the view when they are committed
# ======================================================================================
class FakeKeyCheck:
    """A view's ``key_check`` answered by the test: ``pending`` holds the open lookups ``(key, reply)``;
    ``sync`` answers at once."""

    def __init__(self, taken=(), *, sync: bool = False) -> None:
        self.taken = set(taken)
        self.sync = sync
        self.asked: list[str] = []
        self.pending: list = []

    def __call__(self, key: str, reply) -> None:
        self.asked.append(key)
        if self.sync:
            reply(key in self.taken)
        else:
            self.pending.append((key, reply))

    def answer(self) -> None:
        pending, self.pending = self.pending, []
        for key, reply in pending:
            reply(key in self.taken)


def test_inline_key_editor_checks_new_keys_before_committing(grid) -> None:
    check = FakeKeyCheck({"age"})
    ed, out = open_editor(grid, "key", "name", key_check=check)
    ed.setText("age")
    assert ed.is_valid()                                           # not known before the commit
    assert ed.commit() is False and ed.checking and check.asked == ["age"]
    assert ed.commit() is False and check.asked == ["age"]         # one lookup at a time
    check.answer()
    assert not ed.checking and not ed.is_valid() and ed.error_text() == t("gui.editors.error.key_exists", key="age")
    assert out == {"commit": [], "cancel": 0} and not ed.done      # stays open: the error in place
    ed.setText("yosh")
    QTest.keyClick(ed, Qt.Key.Key_Return)
    assert out["commit"] == [] and ed.checking
    check.answer()
    assert out["commit"] == ["yosh"] and ed.done                   # free: committed once answered
    # a synchronous answer commits at once; an unchanged key is never looked up
    sync = FakeKeyCheck(sync=True)
    ed2, out2 = open_editor(grid, "key", "name", key_check=sync)
    ed2.setText("other")
    assert ed2.commit() is True and out2["commit"] == ["other"]
    ed3, out3 = open_editor(grid, "key", "name", key_check=sync)
    QTest.keyClick(ed3, Qt.Key.Key_Return)
    assert out3 == {"commit": [], "cancel": 1} and sync.asked == ["other"]
    # the data changed: the answers are dropped, a lookup on its way is asked again (its old answer ignored)
    check2 = FakeKeyCheck({"x"})
    ed4, out4 = open_editor(grid, "key", "name", key_check=check2)
    ed4.setText("x")
    assert ed4.commit() is False
    ed4.forget_key_checks()
    assert check2.asked == ["x", "x"] and len(check2.pending) == 2
    check2.taken.clear()                                           # "x" was freed meanwhile
    check2.answer()
    assert out4["commit"] == ["x"] and ed4.done


def test_key_dialog_checks_the_key_on_ok(dialogs) -> None:
    check = FakeKeyCheck({"yosh"})
    dlg = dialogs(KeyDialog(None, "ism", key_check=check))
    got: list[str] = []
    dlg.key_accepted.connect(got.append)
    dlg.edit.setText("yosh")
    assert dlg.ok_button.isEnabled()
    dlg.accept()
    assert dlg.checking and not dlg.ok_button.isEnabled() and dlg.note.text() == t("gui.editors.key.checking")
    assert dlg.result() == 0 and got == []
    check.answer()
    assert not dlg.checking and not dlg.ok_button.isEnabled() and dlg.note.text() == ""
    assert dlg.error_label.text() == t("gui.editors.error.key_exists", key="yosh") and got == []
    dlg.edit.setText("familiya")
    assert dlg.ok_button.isEnabled()
    dlg.accept()
    check.answer()
    assert got == ["familiya"] and dlg.result() == 1


def test_insert_dialog_checks_keys_per_position(dialogs) -> None:
    child = FakeKeyCheck({"a"})
    dlg = dialogs(InsertDialog(None, positions=("after", "child"), position="child",
                               key_needed={"after": True, "child": True}, key_check={"child": child}))
    got: list[InsertResult] = []
    dlg.insert_accepted.connect(got.append)
    dlg.key_edit.setText("a")
    dlg.accept()
    assert dlg.checking and not dlg.ok_button.isEnabled() and got == []
    child.answer()
    assert t("gui.editors.error.key_exists", key="a") in dlg.error_label.text() and got == []
    dlg.set_position("after")                                     # no check for this position: accepted
    assert dlg.ok_button.isEnabled()
    dlg.accept()
    assert [(r.position, r.key) for r in got] == [("after", "a")] and child.asked == ["a"]
    ok = dialogs(InsertDialog(None, positions=("child",), key_needed=True, key_check=FakeKeyCheck(sync=True)))
    done: list[InsertResult] = []
    ok.insert_accepted.connect(done.append)
    ok.key_edit.setText("b")
    ok.accept()
    assert [r.key for r in done] == ["b"]                          # a synchronous "free": accepted at once
