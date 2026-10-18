"""jsonreshape.gui.widgets: ViewBase copy commands, InfoBar, ErrorDialog / show_error,
FullValueDialog, small helpers. A real Document + AppContext (headless) behind a small
fake session (the DocSession API of docs/ARCHITECTURE.md §11.6 that the widgets use)."""

from __future__ import annotations

import gc
import json
from pathlib import Path
from typing import Any, NamedTuple

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPlainTextEdit  # noqa: E402

from jsonreshape import codec  # noqa: E402
from jsonreshape.core import Reformatter  # noqa: E402
from jsonreshape.errors import (  # noqa: E402
    Cancelled,
    JsonSyntaxError,
    PathSyntaxError,
    TooLargeError,
    UserInputError,
)
from jsonreshape.gui import widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.widgets import (  # noqa: E402
    ElidedLabel,
    ErrorDialog,
    FullValueDialog,
    InfoBar,
    ViewBase,
    busy_cursor,
    confirm,
    format_count,
    format_size,
    load_full_text,
    mono_font,
    set_clipboard_text,
    show_error,
)
from jsonreshape.reader import Document  # noqa: E402
from jsonreshape.settings import Settings  # noqa: E402

pytestmark = pytest.mark.gui

LONG = "uzun matn " * 50          # 500 characters
RECORDS_JSON = (
    '[\n'
    ' {"id": 1, "name": "Ali", "n": 1.10, "big": 1e400, "huge": 12345678901234567890,\n'
    '  "tags": ["a", "b"], "addr": {"city": "Toshkent", "zip": null},\n'
    '  "nested": {"k": [1, 2, {"z": "Тошкент 😀"}]}, "long": "' + LONG + '", "a&b": true},\n'
    ' {"id": 2, "name": "Vali", "n": -0.0, "tags": [], "addr": {"city": "Samarqand"}}\n'
    ']\n'
)


class Ref(NamedTuple):
    record: int | None
    path: tuple = ()
    ipath: tuple | None = None


class FakeSession:
    """The part of DocSession the widgets/actions use, over a real array Document."""

    def __init__(self, app: AppContext, doc: Document, path: Path) -> None:
        self.app = app
        self.doc = doc
        self.path = path
        self.record_path = None
        self.format = "array"
        self.current: Ref | None = None
        self.calls: list[tuple] = []
        self.fail_with: BaseException | None = None

    def absolute_path(self, ref: Ref) -> tuple:
        return ((ref.record,) if ref.record is not None else ()) + tuple(ref.path)

    def node(self, ref: Ref, cancel: Any = None) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        return self.doc.node(self.absolute_path(ref), cancel=cancel)

    def filter_by_value(self, ref: Ref, value: Any) -> bool:
        self.calls.append(("filter", ref, value))
        return True

    def sort_by_field(self, path: Any, descending: bool = False) -> None:
        self.calls.append(("sort", tuple(path), descending))

    def select(self, ref: Ref, *, origin: Any = None, history: bool = True, reveal: bool = False) -> None:
        self.calls.append(("select", ref, origin, reveal))


def open_doc(tmp_path: Path, text: str = RECORDS_JSON, *, large: int | None = None) -> Document:
    path = tmp_path / "data.json"
    path.write_text(text, encoding="utf-8")
    settings = Settings(large_node_bytes=large) if large is not None else None
    doc = Document.open(path, format="array", settings=settings, use_cache=False)
    doc.build_index()
    return doc


@pytest.fixture
def app(qapp, tmp_path):
    ctx = AppContext(state=GuiState(tmp_path / "gui_state.json"), headless=True)
    yield ctx
    ctx.tasks.shutdown(5)


@pytest.fixture(autouse=True)
def _fresh_error_limit():
    """Every test starts with the full budget of error windows (show_error rate limit)."""
    widgets.reset_error_limit()
    yield
    widgets.reset_error_limit()


@pytest.fixture
def reported(app):
    errors: list[BaseException] = []
    app.error_reported.connect(errors.append)
    return errors


@pytest.fixture
def session(app, tmp_path):
    doc = open_doc(tmp_path)
    yield FakeSession(app, doc, doc.source_path)
    doc.close()


def clipboard() -> str:
    return QGuiApplication.clipboard().text()


def wait_tasks(app: AppContext) -> None:
    assert app.tasks.wait_idle(10)


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def test_format_count_by_language(lang_uz) -> None:
    assert format_count(12345) == "12 345"
    assert format_count(1_234_567_890) == "1 234 567 890"
    assert format_count(7) == "7"


def test_format_count_english(lang_en) -> None:
    assert format_count(12345) == "12,345"
    assert format_size(1500) == "1.5 KB"
    assert format_size(999) == "999 B"


def test_set_clipboard_text_escapes_surrogates(qapp) -> None:
    assert set_clipboard_text("Тошкент 😀") == "Тошкент 😀"
    assert clipboard() == "Тошкент 😀"
    assert set_clipboard_text("a\ud800b") == "a\\ud800b"
    assert clipboard() == "a\\ud800b"


def test_confirm_handler_and_message_box(qapp, monkeypatch, lang_uz) -> None:
    seen = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: seen.append((text, title)) or True)
    assert confirm(None, "Davom etilsinmi?") is True
    assert seen == [("Davom etilsinmi?", "Tasdiqlash")]
    monkeypatch.setattr(widgets, "confirm_handler", None)

    def answer(text: str) -> None:
        box = QApplication.activeModalWidget()
        assert isinstance(box, QMessageBox)
        assert sorted(b.text() for b in box.buttons()) == ["Ha", "Yo'q"]
        for b in box.buttons():
            if b.text() == text:
                b.click()

    QTimer.singleShot(0, lambda: answer("Ha"))
    assert confirm(None, "Rostdanmi?") is True
    QTimer.singleShot(0, lambda: answer("Yo'q"))
    assert confirm(None, "Rostdanmi?", "Sarlavha") is False


def test_ask_is_non_modal_and_answers_once(qtbot, monkeypatch, lang_uz) -> None:
    """ask(): the same question as confirm() without a nested event loop -- it returns at
    once and the callback gets the answer later (safe inside task callbacks)."""
    answers: list[bool] = []
    seen = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: seen.append((text, title)) or True)
    assert widgets.ask(None, "Davom etilsinmi?", answers.append) is None
    assert answers == []                       # asynchronous even with the test handler
    qtbot.waitUntil(lambda: answers == [True], timeout=2000)
    assert seen == [("Davom etilsinmi?", "Tasdiqlash")]
    monkeypatch.setattr(widgets, "confirm_handler", None)
    box = widgets.ask(None, "Rostdanmi?", answers.append, title="Sarlavha", default_yes=True)
    assert isinstance(box, QMessageBox) and box.isVisible() and box.isModal()   # window-modal via open()
    assert box.windowTitle() == "Sarlavha" and box.defaultButton().text() == "Ha"
    assert answers == [True]                   # nothing yet: no nested loop ran
    [b for b in box.buttons() if b.text() == "Ha"][0].click()
    qtbot.waitUntil(lambda: answers == [True, True], timeout=2000)
    box2 = widgets.ask(None, "Yana?", answers.append)
    assert box2.defaultButton().text() == "Yo'q"
    box2.reject()                              # closing = "no", exactly once
    qtbot.waitUntil(lambda: answers == [True, True, False], timeout=2000)
    qtbot.wait(20)
    assert answers == [True, True, False]


def test_busy_cursor(qapp) -> None:
    assert QApplication.overrideCursor() is None
    with busy_cursor():
        assert QApplication.overrideCursor().shape() == Qt.CursorShape.WaitCursor
    assert QApplication.overrideCursor() is None


def test_mono_font_is_fixed_pitch(qapp, session) -> None:
    session.app.state.set("mono_size", 13)
    font = mono_font(session)
    assert font.pointSizeF() == 13
    assert mono_font().fixedPitch()


def test_elided_label(qtbot) -> None:
    label = ElidedLabel("a" * 300 + "END")
    qtbot.addWidget(label)
    label.resize(120, 30)
    label.show()
    assert label.full_text() == "a" * 300 + "END"
    assert "\u2026" in label.text() and len(label.text()) < 300
    assert label.toolTip() == label.full_text()
    label.setText("short")
    assert label.text() == "short" and label.toolTip() == ""
    label.setText("x\ud800")
    assert label.full_text() == "x\\ud800"


# ----------------------------------------------------------------------------------
# ViewBase
# ----------------------------------------------------------------------------------
class DemoView(ViewBase):
    view_id = "table"


def test_viewbase_defaults_are_noops(qtbot, session) -> None:
    view = DemoView(session)
    qtbot.addWidget(view)
    view.activate()
    view.zoom(2)
    view.quick_find()
    view.find_next(backwards=True)
    assert view.copy_value() is None and view.copy_path() is None and view.copy_key() is None
    view.shutdown()


@pytest.mark.parametrize("fmt, expected", [
    ("json", '{\n  "city": "Toshkent",\n  "zip": null\n}'),
    ("json_compact", '{"city":"Toshkent","zip":null}'),
    ("python", "{\n  'city': 'Toshkent',\n  'zip': None\n}"),
    ("csv", "Toshkent,"),
    ("csv_header", "city,zip\nToshkent,"),
])
def test_viewbase_copy_value(qtbot, session, fmt: str, expected: str) -> None:
    view = DemoView(session)
    qtbot.addWidget(view)
    set_clipboard_text("before")
    session.current = Ref(0, ("addr",))
    task = view.copy_value(fmt)
    assert task is not None and task.silent and task.owner is view
    wait_tasks(session.app)
    assert clipboard() == expected


def test_viewbase_copy_value_exact_numbers_and_unicode(qtbot, session) -> None:
    view = DemoView(session)
    qtbot.addWidget(view)
    for path, fmt, expected in [
        (("n",), "json", "1.10"), (("big",), "python", 'float("1e400")'), (("huge",), "csv", "12345678901234567890"),
        (("nested",), "json_compact", '{"k":[1,2,{"z":"Тошкент 😀"}]}'),
    ]:
        session.current = Ref(0, path)
        view.copy_value(fmt)
        wait_tasks(session.app)
        assert clipboard() == expected
    session.current = Ref(1, ("n",))
    view.copy_value("json")
    wait_tasks(session.app)
    assert clipboard() == "-0.0"


@pytest.mark.parametrize("style, expected", [
    ("dotted", '[0].addr.city'), ("jsonpath", "$[0].addr.city"), ("pointer", "/0/addr/city"),
    ("python", '[0]["addr"]["city"]'), ("js", '[0].addr.city'),
])
def test_viewbase_copy_path(qtbot, session, style: str, expected: str) -> None:
    view = DemoView(session)
    qtbot.addWidget(view)
    session.current = Ref(0, ("addr", "city"))
    assert view.copy_path(style) == expected
    assert clipboard() == expected


def test_viewbase_copy_key(qtbot, session) -> None:
    view = DemoView(session)
    qtbot.addWidget(view)
    session.current = Ref(0, ("tags", 1))
    assert view.copy_key() == "1" and clipboard() == "1"
    session.current = Ref(0, ("a&b",))
    assert view.copy_key() == "a&b"
    set_clipboard_text("kept")
    session.current = Ref(None, ())
    assert view.copy_key() is None
    assert clipboard() == "kept"


def test_viewbase_current_ref_override(qtbot, session) -> None:
    class CellView(ViewBase):
        def current_ref(self):
            return Ref(1, ("name",))

    view = CellView(session)
    qtbot.addWidget(view)
    view.copy_value("json")
    wait_tasks(session.app)
    assert clipboard() == '"Vali"'


def test_copy_value_too_large_is_reported(qtbot, app, reported, tmp_path, lang_uz) -> None:
    doc = open_doc(tmp_path, large=64)
    try:
        session = FakeSession(app, doc, doc.source_path)
        view = DemoView(session)
        qtbot.addWidget(view)
        set_clipboard_text("unchanged")
        session.current = Ref(0, ())
        view.copy_value("json")
        wait_tasks(app)
        assert clipboard() == "unchanged"
        assert len(reported) == 1
        exc = reported[0]
        assert isinstance(exc, UserInputError) and exc.key == "gui.copy.too_large"
        assert "juda katta" in exc.message and exc.hint
    finally:
        doc.close()


def test_copy_value_unknown_format(qtbot, session) -> None:
    view = DemoView(session)
    qtbot.addWidget(view)
    session.current = Ref(0, ())
    with pytest.raises(ValueError):
        view.copy_value("yaml")


def test_copy_value_failure_is_reported(qtbot, session, reported) -> None:
    view = DemoView(session)
    qtbot.addWidget(view)
    session.current = Ref(0, ())
    session.fail_with = RuntimeError("disk on fire")
    view.copy_value("json")
    wait_tasks(session.app)
    assert [type(e) for e in reported] == [RuntimeError]
    session.fail_with = Cancelled()
    view.copy_value("json")
    wait_tasks(session.app)
    assert len(reported) == 1   # a cancellation is not an error


def test_shutdown_detaches_view_tasks(qtbot, session) -> None:
    view = DemoView(session)
    qtbot.addWidget(view)
    session.current = Ref(0, ())
    set_clipboard_text("before")
    view.copy_value("json")
    view.shutdown()
    wait_tasks(session.app)
    assert clipboard() == "before"


# ----------------------------------------------------------------------------------
# InfoBar
# ----------------------------------------------------------------------------------
def test_infobar_content_buttons_and_close(qtbot, lang_uz) -> None:
    clicked = []
    bar = InfoBar("warning", "Fayl o'zgardi", detail="data.json", buttons=[("Qayta ochish", lambda: clicked.append(1))])
    qtbot.addWidget(bar)
    bar.show()
    assert bar.kind() == "warning"
    assert bar.message() == "Fayl o'zgardi"
    assert bar.detail() == "data.json"
    assert bar.message_label.textFormat() == Qt.TextFormat.PlainText
    assert bar.message_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    assert [b.text() for b in bar.buttons()] == ["Qayta ochish"]
    qtbot.mouseClick(bar.buttons()[0], Qt.MouseButton.LeftButton)
    assert clicked == [1]
    extra = bar.add_button("Yana", lambda: clicked.append(2))
    extra.click()
    assert clicked == [1, 2]
    assert bar.close_button.toolTip() == "Yopish"
    with qtbot.waitSignal(bar.closed, timeout=1000):
        qtbot.mouseClick(bar.close_button, Qt.MouseButton.LeftButton)
    assert not bar.isVisible()


def test_infobar_kinds_and_colors(qtbot) -> None:
    bar = InfoBar("info", "<b>not rich</b>")
    qtbot.addWidget(bar)
    assert bar.message_label.text() == "<b>not rich</b>"
    assert bar.detail() is None
    colors = {}
    for kind in InfoBar.KINDS:
        bar.set_kind(kind)
        colors[kind] = bar.background_color().name()
        assert colors[kind] in bar.styleSheet()
    assert len(set(colors.values())) == 4
    base = QGuiApplication.palette().color(QPalette.ColorRole.Window)
    assert all(c != base.name() for c in colors.values())
    with pytest.raises(ValueError):
        bar.set_kind("fatal")
    bar.set_detail("tafsilot")
    assert bar.detail() == "tafsilot"
    bar.set_detail(None)
    assert bar.detail() is None
    bar.clear_buttons()
    assert bar.buttons() == []
    bar.set_closable(False)
    assert not bar.close_button.isVisibleTo(bar)


def test_infobar_without_close_button(qtbot) -> None:
    bar = InfoBar("error", "x", closable=False)
    qtbot.addWidget(bar)
    assert not bar.close_button.isVisibleTo(bar)


# ----------------------------------------------------------------------------------
# ErrorDialog
# ----------------------------------------------------------------------------------
def _labels(dialog) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [w.text() for w in dialog.findChildren(QLabel) if w.text()]


def test_error_dialog_user_error_with_hint(qtbot, lang_uz) -> None:
    exc = PathSyntaxError(pos=3, text="a..[", detail="kalit kutilgan")
    dialog = ErrorDialog(exc)
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "Xato"
    assert dialog.message_label.text() == exc.message
    assert dialog.hint_label is not None and dialog.hint_label.text() == exc.hint
    assert dialog.exception_label is None and dialog.location_label is None
    assert (dialog.open_log_button.text(), dialog.copy_button.text(), dialog.close_button.text()) == \
        ("Logni ochish", "Nusxalash", "Yopish")
    assert not dialog.details_view.isVisibleTo(dialog)


def test_error_dialog_json_syntax_error(qtbot, lang_en) -> None:
    exc = JsonSyntaxError(code="unexpected_char", offset=1234, line=12, column=7, expected="value",
                          context_before='{"a": 1,\n "b": ', context_after="tru}", source="C:/data/x.json")
    dialog = ErrorDialog(exc)
    qtbot.addWidget(dialog)
    assert dialog.message_label.text() == exc.message
    assert dialog.location_label.text() == "Location: line 12, column 7, byte 1234"
    assert dialog.source_label.text() == "File: C:/data/x.json"
    context = dialog.context_view.toPlainText()
    assert context == exc.context()
    first, caret = context.split("\n")
    assert caret.index("^") == len('{"a": 1,  "b": ')
    assert "1234" in dialog.details_text()


def test_error_dialog_unexpected_error_with_traceback(qtbot, lang_uz, monkeypatch) -> None:
    try:
        raise ValueError("boom")
    except ValueError as e:
        exc = e
    dialog = ErrorDialog(exc)
    qtbot.addWidget(dialog)
    assert dialog.message_label.text() == "Kutilmagan xato yuz berdi."
    assert dialog.exception_label.text() == "ValueError: boom"
    assert "log" in dialog.hint_label.text()
    details = dialog.details_view.toPlainText()
    assert "Traceback" in details and "ValueError: boom" in details
    assert "test_error_dialog_unexpected_error_with_traceback" in details
    # collapsible details
    assert not dialog.details_view.isVisibleTo(dialog)
    dialog.details_button.click()
    assert dialog.details_view.isVisibleTo(dialog)
    dialog.set_details_visible(False)
    assert not dialog.details_view.isVisibleTo(dialog) and not dialog.details_button.isChecked()
    # copy
    dialog.copy_button.click()
    assert clipboard() == dialog.details_text()
    # open log -> the logs folder through a file: URL
    opened = []
    monkeypatch.setattr(widgets, "open_local_path", lambda path: opened.append(Path(path)) or True)
    dialog.open_log_button.click()
    from jsonreshape.log import log_path

    assert opened == [log_path().parent]
    assert log_path().parent.is_dir()


def test_open_local_path_uses_file_urls(qapp, monkeypatch, tmp_path) -> None:
    from PySide6.QtGui import QDesktopServices

    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: urls.append(url) or True)
    assert widgets.open_local_path(tmp_path)
    assert urls[0].isLocalFile() and Path(urls[0].toLocalFile()) == tmp_path


def test_error_dialog_exception_without_traceback(qtbot) -> None:
    dialog = ErrorDialog(KeyError("x"))
    qtbot.addWidget(dialog)
    assert dialog.exception_label.text() == "KeyError: 'x'"
    assert "KeyError: 'x'" in dialog.details_text()


def test_show_error(qtbot, lang_uz) -> None:
    assert show_error(None, Cancelled()) is None
    before = set(widgets._kept_alive)
    dialog = show_error(None, TooLargeError(size="60.0 MB"))
    assert dialog is not None
    assert dialog.isVisible()
    assert "60.0 MB" in dialog.message_label.text()
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert dialog in widgets._kept_alive
    gone = []
    dialog.destroyed.connect(lambda *_args: gone.append(1))
    dialog.close_button.click()
    qtbot.waitUntil(lambda: bool(gone), timeout=2000)
    assert widgets._kept_alive == before


def test_report_error_shows_the_dialog(qtbot, tmp_path, lang_uz) -> None:
    ctx = AppContext(state=GuiState(tmp_path / "s.json"), headless=False)
    try:
        ctx.report_error(UserInputError("gui.copy.too_large", size="1 GB"))
        gc.collect()   # the parentless dialog must survive without a Python reference
        dialogs = [w for w in QApplication.topLevelWidgets() if isinstance(w, ErrorDialog) and w.isVisible()]
        assert dialogs and "1 GB" in dialogs[-1].message_label.text()
        for d in dialogs:
            d.close()
    finally:
        ctx.tasks.shutdown(5)


# ----------------------------------------------------------------------------------
# FullValueDialog
# ----------------------------------------------------------------------------------
def _open_full(qtbot, session, ref, **kw) -> FullValueDialog:
    dialog = FullValueDialog(session, ref, **kw)
    qtbot.addWidget(dialog)
    with qtbot.waitSignal(dialog.loaded, timeout=10000):
        dialog.show()
    return dialog


def test_full_value_string(qtbot, session, lang_uz) -> None:
    dialog = _open_full(qtbot, session, Ref(0, ("long",)))
    assert dialog.windowTitle() == "[0].long — to'liq qiymat"
    assert dialog.text() == LONG
    assert dialog.editor.toPlainText() == LONG
    assert dialog.editor.isReadOnly()
    assert dialog.wrap_check.isChecked()
    assert dialog.editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth
    assert not dialog.notice_label.isVisibleTo(dialog)
    dialog.wrap_check.setChecked(False)
    assert dialog.editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap
    dialog.copy_button.click()
    assert clipboard() == LONG
    assert dialog.editor.font().fixedPitch()


def test_full_value_container_keeps_exact_tokens(qtbot, session) -> None:
    dialog = _open_full(qtbot, session, Ref(0, ()))
    raw = session.doc.node((0,)).raw()
    fmt = Reformatter(indent=2)
    expected = (fmt.feed(raw) + fmt.finish()).decode("utf-8")
    assert dialog.text() == expected
    assert '"n": 1.10' in expected and '"big": 1e400' in expected and "Тошкент 😀" in expected
    assert not dialog.wrap_check.isChecked()
    assert dialog.result.kind == "object" and not dialog.result.truncated


def test_full_value_truncated(qtbot, session, lang_uz) -> None:
    dialog = _open_full(qtbot, session, Ref(0, ("long",)), cap=100)
    assert dialog.text() == LONG[:100]
    assert dialog.result.truncated
    assert dialog.notice_label.isVisibleTo(dialog)
    assert "faqat boshidagi" in dialog.notice_label.text()
    dialog2 = _open_full(qtbot, session, Ref(0, ()), cap=50)
    assert len(dialog2.text().encode()) <= 50 and dialog2.result.truncated
    assert dialog2.text() == load_full_text(session, Ref(0, ())).text.encode()[:50].decode("utf-8", "ignore")


def test_full_value_huge_container_reads_only_a_prefix(app, tmp_path) -> None:
    big = {"items": [{"i": i, "s": "x" * 50} for i in range(40_000)]}
    doc = open_doc(tmp_path, "[" + json.dumps(big) + "]", large=1024)
    try:
        session = FakeSession(app, doc, doc.source_path)
        full = load_full_text(session, Ref(0, ()), cap=10_000)
        assert full.truncated and full.kind == "object"
        assert len(full.text.encode()) <= 10_000
        assert full.text.startswith('{\n  "items": [\n    {\n      "i": 0,')
        assert full.size > 2_000_000
    finally:
        doc.close()


def test_full_value_string_larger_than_large_node_bytes(app, tmp_path) -> None:
    text = "Тошкент 😀 " * 400      # ~ 6 KB of UTF-8
    doc = open_doc(tmp_path, json.dumps([{"s": text}], ensure_ascii=False), large=256)
    try:
        session = FakeSession(app, doc, doc.source_path)
        with pytest.raises(TooLargeError):
            doc.node((0, "s")).value()
        full = load_full_text(session, Ref(0, ("s",)))
        assert full.text == text and not full.truncated
        part = load_full_text(session, Ref(0, ("s",)), cap=30)
        assert part.text == text[:30] and part.truncated
    finally:
        doc.close()


def test_full_value_scalar_and_escapes(qtbot, app, tmp_path) -> None:
    doc = open_doc(tmp_path, r'[{"x": "a\ud800b", "n": 1E+2}]')
    try:
        session = FakeSession(app, doc, doc.source_path)
        assert load_full_text(session, Ref(0, ("x",))).text == "a\\ud800b"
        assert load_full_text(session, Ref(0, ("n",))).text == "1E+2"
    finally:
        doc.close()


def test_full_value_error_is_shown(qtbot, session, reported, lang_uz) -> None:
    session.fail_with = UserInputError("reader.path_not_found", path="#9")
    dialog = _open_full(qtbot, session, Ref(9, ()))
    assert dialog.error is not None and dialog.text() == ""
    assert dialog.notice_label.isVisibleTo(dialog)
    assert dialog.notice_label.text() == session.fail_with.message
    assert reported == []
    session.fail_with = RuntimeError("boom")
    dialog = _open_full(qtbot, session, Ref(0, ()))
    assert dialog.notice_label.text() == "Kutilmagan xato yuz berdi."
    assert [type(e) for e in reported] == [RuntimeError]


def test_full_value_close_detaches(qtbot, session) -> None:
    dialog = FullValueDialog(session, Ref(0, ()))
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.close()
    wait_tasks(session.app)
    assert dialog.result is None   # the callback was dropped


def test_load_full_text_virtual_root(app) -> None:
    class VirtualNode:
        kind = "array"
        size = 0
        is_virtual = True

        def value(self):
            return [codec.loads("1.10"), {"a": "b"}]

    class S:
        def node(self, ref, cancel=None):
            return VirtualNode()

    full = load_full_text(S(), Ref(None, ()))
    assert full.text == '[\n  1.10,\n  {\n    "a": "b"\n  }\n]'
    assert not load_full_text(S(), Ref(None, ()), cap=5).text.endswith("]")


# ----------------------------------------------------------------------------------
# fix round: error window rate limit (M6), long lines in the full value window (K3)
# ----------------------------------------------------------------------------------
def _error_windows() -> tuple[list[ErrorDialog], list[widgets.ErrorNotice]]:
    tops = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
    return ([w for w in tops if isinstance(w, ErrorDialog)], [w for w in tops if isinstance(w, widgets.ErrorNotice)])


def _close_error_windows(qtbot) -> None:
    dialogs, notices = _error_windows()
    for w in dialogs + notices:
        w.close()
    qtbot.waitUntil(lambda: _error_windows() == ([], []), timeout=5000)


def test_show_error_rate_limit_and_notice(qtbot, monkeypatch, lang_en) -> None:
    clock = [1000.0]
    monkeypatch.setattr(widgets.time, "monotonic", lambda: clock[0])
    widgets.reset_error_limit()
    try:
        shown = [show_error(None, RuntimeError(f"e{i}")) for i in range(10)]
        assert sum(d is not None for d in shown) == widgets.MAX_ERROR_DIALOGS == 3
        dialogs, notices = _error_windows()
        assert len(dialogs) == 3 and len(notices) == 1
        assert notices[0].count == 7 and "7 more errors" in notices[0].label.text()
        assert not notices[0].label.text().startswith("{")
        # still inside the period: counted in the same notice
        clock[0] += 5
        assert show_error(None, ValueError("x")) is None
        assert _error_windows()[1][0].count == 8
        # the notice was closed: the next hidden error opens a new one
        _close_error_windows(qtbot)
        assert show_error(None, ValueError("y")) is None
        assert _error_windows()[1][0].count == 1
        # after the period, windows open again
        clock[0] += widgets.ERROR_DIALOG_PERIOD_S
        assert show_error(None, ValueError("z")) is not None
        assert show_error(None, Cancelled()) is None
    finally:
        _close_error_windows(qtbot)
        widgets.reset_error_limit()


def test_flood_of_failing_callbacks_opens_three_windows(qtbot, tmp_path, lang_en) -> None:
    """The reviewer's repro (M6): 200 failing on_item callbacks + 50 failing tasks used to
    open 250 error windows."""
    widgets.reset_error_limit()
    ctx = AppContext(state=GuiState(tmp_path / "s.json"), headless=False)
    reported: list = []
    ctx.error_reported.connect(reported.append)
    try:
        def fn(c):
            for i in range(200):
                c.post(i)

        def bad_item(i):
            raise RuntimeError(f"bug in on_item #{i}")

        ctx.tasks.run(fn, title="flood", on_item=bad_item)

        def failing(c):
            raise ValueError("task failed")

        for i in range(50):
            ctx.tasks.run(failing, title=f"f{i}")
        assert ctx.tasks.wait_idle(20)
        qtbot.wait(50)
        dialogs, notices = _error_windows()
        assert len(reported) == 250          # every error is still reported and logged
        assert len(dialogs) == 3 and len(notices) == 1 and notices[0].count == 247
    finally:
        _close_error_windows(qtbot)
        ctx.tasks.shutdown(5)
        widgets.reset_error_limit()


def test_shorten_long_lines() -> None:
    assert widgets.shorten_long_lines("abc", 3) == ("abc", 0)
    text = "ab\ncdefg hi jklmn\r\nxy"
    shown, cut = widgets.shorten_long_lines(text, 3)
    assert shown == "ab\ncde… hi …\r\nxy" and cut == 2  # CR is a Qt block separator too: kept
    many = ("y" * 9_999 + "\n") * 50
    assert widgets.shorten_long_lines(many, 10_000) == (many, 0)


def test_full_value_multi_megabyte_single_line_string(qtbot, app, tmp_path, lang_en) -> None:
    """A 3 MB one-line string with wrapping on used to freeze the window for seconds (K3):
    the line is shown shortened with a notice, copy copies the whole text."""
    import time as _time

    text = ("0123456789abcdef" * 200_000)[:3_000_000]
    doc = open_doc(tmp_path, json.dumps([{"s": text, "t": "line one\n" + "z" * 20_000}]))
    try:
        session = FakeSession(app, doc, doc.source_path)
        started = _time.perf_counter()
        dialog = _open_full(qtbot, session, Ref(0, ("s",)))
        qtbot.waitExposed(dialog)
        QApplication.processEvents()
        took = _time.perf_counter() - started
        assert dialog.wrap_check.isChecked()
        assert dialog.text() == text and dialog.result.cut_lines == 1
        shown = dialog.editor.toPlainText()
        assert len(shown) == widgets.FULL_LINE_CHARS + 1 and shown.endswith("…")
        assert dialog.notice_label.isVisibleTo(dialog) and "shortened" in dialog.notice_label.text()
        assert took < 10.0, took
        dialog.copy_button.click()
        assert len(clipboard()) == len(text)
        dialog2 = _open_full(qtbot, session, Ref(0, ("t",)))
        assert dialog2.editor.toPlainText() == "line one\n" + "z" * widgets.FULL_LINE_CHARS + "…"
        assert dialog2.text() == "line one\n" + "z" * 20_000
        # short values keep the editor text identical to the value
        dialog3 = _open_full(qtbot, session, Ref(0, ()))
        assert dialog3.result.display is not None or dialog3.editor.toPlainText() == dialog3.text()
    finally:
        doc.close()



def test_shorten_long_lines_splits_on_every_qt_block_separator() -> None:
    from jsonreshape.gui.widgets import shorten_long_lines

    text = "a" * 20 + "\r" + "b" * 20 + "﷐" + "c" * 5 + " " + "d" * 30
    shown, cut = shorten_long_lines(text, 10)
    assert cut == 3
    assert shown.split("\r")[0] == "a" * 10 + "…"
    assert "﷐" + "c" * 5 + " " in shown
