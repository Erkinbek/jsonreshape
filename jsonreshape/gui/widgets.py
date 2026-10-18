"""Shared widgets and helpers of the viewer (docs/ARCHITECTURE.md §11.1, §11.7).

* :class:`ViewBase` -- base class of the tree/table/text/raw/schema views (default no-op
  ``activate/zoom/quick_find/find_next``; ``copy_value``/``copy_path``/``copy_key`` act on
  :meth:`ViewBase.current_ref`, by default ``session.current``);
* :func:`copy_node_value` / :func:`copy_node_path` / :func:`copy_node_key` and
  :func:`run_value_task` -- the copy machinery shared with :mod:`jsonreshape.gui.actions`
  (values are loaded and formatted in a silent background task, then put on the
  clipboard in the GUI thread);
* :class:`InfoBar` -- a themed message strip (info / warning / error / success) with
  buttons and a close button;
* :class:`ErrorDialog` / :func:`show_error` -- the error window (message, hint, JSON error
  location with context, collapsible details, "open log", copy); at most
  :data:`MAX_ERROR_DIALOGS` windows per :data:`ERROR_DIALOG_PERIOD_S` seconds, further
  errors are only logged and counted in one :class:`ErrorNotice` ("N more errors -- see the
  log");
* :class:`FullValueDialog` -- the "full value" window of a node (lazy, capped at 16 MiB;
  lines longer than :data:`FULL_LINE_CHARS` characters are shown shortened, copy copies the
  full text);
* small helpers: :func:`set_clipboard_text`, :func:`format_count`, :func:`format_size`,
  :class:`ElidedLabel`, :func:`confirm` (modal, for explicit user actions), :func:`ask`
  (the same question without a nested event loop: safe inside task callbacks),
  :func:`busy_cursor`, :func:`mono_font`.

The GUI thread never reads node values here: everything that touches the data file runs
in ``session.app.tasks`` (silent tasks owned by the view / dialog, so closing it detaches
them).
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import shiboken6
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec
from jsonreshape.errors import Cancelled, JsonReshapeError, JsonSyntaxError, TooLargeError, UserInputError
from jsonreshape.gui import copyfmt
from jsonreshape.i18n import get_language, t
from jsonreshape.tasks import CancelToken, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "ERROR_DIALOG_PERIOD_S",
    "FULL_LINE_CHARS",
    "FULL_TEXT_CAP",
    "MAX_ERROR_DIALOGS",
    "ElidedLabel",
    "ErrorDialog",
    "ErrorNotice",
    "FullText",
    "FullValueDialog",
    "InfoBar",
    "ViewBase",
    "ask",
    "busy_cursor",
    "confirm",
    "copy_node_key",
    "copy_node_path",
    "copy_node_value",
    "format_count",
    "format_size",
    "keep_alive",
    "load_full_text",
    "mono_font",
    "open_local_path",
    "open_log_folder",
    "count_hidden_error",
    "report_task_error",
    "reset_error_limit",
    "run_value_task",
    "set_clipboard_text",
    "show_error",
]

#: Text shown by :class:`FullValueDialog` at most (characters of a string / bytes of JSON).
FULL_TEXT_CAP = 16 * 1024 * 1024
#: :class:`FullValueDialog` shows longer lines shortened (laying out a multi-MB line with
#: wrapping freezes Qt for seconds; the text view uses the same limit).
FULL_LINE_CHARS = 10_000
#: :func:`show_error` opens at most this many error windows per ERROR_DIALOG_PERIOD_S seconds.
MAX_ERROR_DIALOGS = 3
ERROR_DIALOG_PERIOD_S = 10.0
_READ_CHUNK = 1 << 20   # the Reformatter is fed at most 1 MiB at a time


# ==================================================================================
# small helpers
# ==================================================================================
def set_clipboard_text(text: str) -> str:
    """Put ``text`` on the system clipboard (lone surrogates escaped: Qt would drop them);
    returns the text actually set."""
    text = copyfmt.escape_surrogates(str(text))
    QGuiApplication.clipboard().setText(text)
    return text


def format_count(n: int) -> str:
    """A count with grouped digits: ``12 345`` (Uzbek), ``12,345`` (English)."""
    text = f"{int(n):,}"
    return text.replace(",", " ") if get_language() == "uz" else text


def format_size(n: float) -> str:
    """Human-readable size (:func:`jsonreshape.tasks.format_bytes`)."""
    return format_bytes(n)


def mono_font(session: Any = None) -> QFont:
    """The monospace data font: the user's choice from the GUI state (when ``session`` is
    given) through :func:`jsonreshape.gui.theme.mono_font`, else the system fixed font."""
    family = None
    size = None
    try:
        state = session.app.state if session is not None else None
        if state is not None:
            family = state.get("mono_font") or None
            size = state.get("mono_size") or None
    except Exception:  # a fake / partial session: defaults
        family = size = None
    try:
        from jsonreshape.gui import theme

        return theme.mono_font(family, size)
    except Exception:
        log.debug("theme.mono_font unavailable", exc_info=True)
        return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


#: Tests (or an automation) may set this to ``fn(parent, text, title) -> bool`` to answer
#: every :func:`confirm` without showing a message box.
confirm_handler: Callable[[QWidget | None, str, str], bool] | None = None


def confirm(parent: QWidget | None, text: str, title: str | None = None, *, default_yes: bool = False) -> bool:
    """Ask a yes/no question (localized buttons); True for "yes".

    When the module attribute :data:`confirm_handler` is set it answers instead (tests)."""
    title = title or t("gui.common.confirm_title")
    if confirm_handler is not None:
        return bool(confirm_handler(parent, text, title))
    box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.NoButton, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    yes = box.addButton(t("gui.common.yes"), QMessageBox.ButtonRole.YesRole)
    no = box.addButton(t("gui.common.no"), QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(yes if default_yes else no)
    box.setEscapeButton(no)
    box.exec()
    return box.clickedButton() is yes


def ask(parent: QWidget | None, text: str, callback: Callable[[bool], Any], *, title: str | None = None,
        default_yes: bool = False) -> QMessageBox | None:
    """Ask a yes/no question without blocking: the message box opens with ``open()``
    (window-modal, no nested event loop -- safe inside task callbacks, unlike
    :func:`confirm`) and ``callback(answer)`` is called once when it is answered (closing it
    is "no"). When :data:`confirm_handler` is set (tests) it answers instead and the callback
    runs from a zero-delay timer (still after ``ask`` returned); ``None`` is returned then.
    Returns the message box otherwise."""
    title = title or t("gui.common.confirm_title")
    handler = confirm_handler
    if handler is not None:
        answer = bool(handler(parent, text, title))
        QTimer.singleShot(0, lambda: callback(answer))
        return None
    box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.NoButton, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    yes = box.addButton(t("gui.common.yes"), QMessageBox.ButtonRole.YesRole)
    no = box.addButton(t("gui.common.no"), QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(yes if default_yes else no)
    box.setEscapeButton(no)
    keep_alive(box)
    answered: list[bool] = []

    def finished(_result: int) -> None:
        if answered:
            return
        answered.append(box.clickedButton() is yes)
        callback(answered[0])

    box.finished.connect(finished)
    box.open()
    return box


@contextlib.contextmanager
def busy_cursor() -> Iterator[None]:
    """Show the wait cursor for a short GUI-thread operation (never around I/O)."""
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


_kept_alive: set[QWidget] = set()


def keep_alive(window: QWidget) -> QWidget:
    """Hold a Python reference to a non-modal window until it is destroyed: a window
    without a Qt parent would otherwise be deleted as soon as its last Python reference
    goes away (e.g. :func:`show_error` called from a callback). Sets
    ``WA_DeleteOnClose``, so closing the window releases it."""
    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    _kept_alive.add(window)
    window.destroyed.connect(lambda *_args, w=window: _kept_alive.discard(w))
    return window


def open_local_path(path: str | Path) -> bool:
    """Open a local file or folder with the system handler (``file:`` URLs only)."""
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))


def open_log_folder() -> bool:
    """Open the folder of the log files (:func:`jsonreshape.log.log_path`)."""
    from jsonreshape.log import log_path

    folder = log_path().parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.debug("could not create %s", folder, exc_info=True)
    return open_local_path(folder)


class ElidedLabel(QLabel):
    """A one-line label that elides its text to the available width (``…``); the full
    text is the tooltip while elided."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle) -> None:
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt name)
        self._full = copyfmt.escape_surrogates(str(text or ""))
        self._refresh()

    def full_text(self) -> str:
        return self._full

    def set_elide_mode(self, mode: Qt.TextElideMode) -> None:
        self._mode = mode
        self._refresh()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        fm = self.fontMetrics()
        margins = self.contentsMargins()
        return QSize(fm.horizontalAdvance(self._full) + margins.left() + margins.right() + 2,
                     super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(self.fontMetrics().horizontalAdvance("…") * 3, super().minimumSizeHint().height())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        width = max(0, self.contentsRect().width())
        shown = self.fontMetrics().elidedText(self._full, self._mode, width) if width else self._full
        QLabel.setText(self, shown)
        self.setToolTip(self._full if shown != self._full else "")


# ==================================================================================
# copy machinery (shared with actions.py)
# ==================================================================================
def report_task_error(session: Any, exc: BaseException, parent: QWidget | None = None, *,
                      purpose: str = "copy") -> None:
    """Report a failed value task: ``Cancelled`` is silent, ``TooLargeError`` becomes a
    localized message for ``purpose`` (``copy`` | ``filter`` | ``view``), anything else
    goes to ``session.app.report_error``."""
    if isinstance(exc, Cancelled):
        return
    if isinstance(exc, TooLargeError):
        size = exc.params.get("size", "?")
        if purpose == "filter":
            exc = UserInputError("gui.actions.filter_too_large", size=size)
        elif purpose == "copy":
            exc = UserInputError("gui.copy.too_large", size=size)
    try:
        session.app.report_error(exc, parent)
    except Exception:  # pragma: no cover - reporting must never raise into Qt
        log.exception("could not report %r", exc)


def run_value_task(session: Any, ref: Any, compute: Callable[[Any, CancelToken], Any],
                   on_result: Callable[[Any], Any], *, owner: Any = None, parent: QWidget | None = None,
                   title: str | None = None, purpose: str = "copy",
                   check: Callable[[Any], None] | None = None) -> Any:
    """Load the value of ``ref`` in a silent task (``session.node(ref).value()``), run
    ``compute(value, cancel)`` there too, then call ``on_result(result)`` in the GUI
    thread. ``check(node)`` (worker thread) may raise before the value is read. Errors
    are reported with :func:`report_task_error`. Returns the task."""

    def work(ctx: Any) -> Any:
        node = session.node(ref, ctx.cancel)
        if check is not None:
            check(node)
        value = node.value()
        ctx.cancel.check()
        return compute(value, ctx.cancel)

    def failed(exc: BaseException) -> None:
        report_task_error(session, exc, parent, purpose=purpose)

    return session.app.tasks.run(work, title=title or t("gui.widgets.task.load_value"), kind="value",
                                 owner=owner if owner is not None else session, on_done=on_result,
                                 on_error=failed, silent=True)


def copy_node_value(session: Any, ref: Any, fmt: str = "json", *, owner: Any = None,
                    parent: QWidget | None = None, indent: int | str | None = 2) -> Any:
    """Copy the value of ``ref`` in ``fmt`` (:data:`copyfmt.COPY_FORMATS`); the text is
    built in the background task. Returns the task."""
    if fmt not in copyfmt.COPY_FORMATS:
        raise ValueError(f"unknown value format: {fmt!r}")

    def compute(value: Any, cancel: CancelToken) -> str:
        return copyfmt.value_text(value, fmt, indent=indent)

    return run_value_task(session, ref, compute, set_clipboard_text, owner=owner, parent=parent,
                          title=t("gui.copy.task", format=copyfmt.format_label(fmt)), purpose="copy")


def copy_node_path(session: Any, ref: Any, style: str = "dotted") -> str:
    """Copy the absolute path of ``ref`` in ``style`` (:data:`copyfmt.PATH_STYLES`);
    returns the text."""
    return set_clipboard_text(copyfmt.path_text(session.absolute_path(ref), style))


def copy_node_key(session: Any, ref: Any) -> str | None:
    """Copy the key (or array index) of ``ref``; ``None`` (nothing copied) for the root."""
    text = copyfmt.key_text(session.absolute_path(ref))
    if text is None:
        return None
    return set_clipboard_text(text)


# ==================================================================================
# ViewBase
# ==================================================================================
class ViewBase(QWidget):
    """Base of the document views (docs/ARCHITECTURE.md §11.7).

    Subclasses set :attr:`view_id`, follow ``session.current_changed`` (ignoring their own
    ``origin``) and select nodes with ``session.select(ref, origin=self)``. Background
    tasks started here are owned by the view; :meth:`shutdown` detaches them (subclasses
    overriding it call ``super().shutdown()``).
    """

    view_id: str = ""

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session

    # -- hooks (no-op defaults) ---------------------------------------------------------
    def activate(self) -> None:
        """The view became visible: load lazily (hidden views do no I/O)."""

    def zoom(self, steps: int) -> None:
        """Ctrl +/- (0 = reset)."""

    def quick_find(self) -> None:
        """Ctrl+F (no-op where meaningless)."""

    def find_next(self, backwards: bool = False) -> None:
        """F3 / Shift+F3."""

    def current_ref(self) -> Any:
        """The node the copy commands act on (default: ``session.current``)."""
        return getattr(self.session, "current", None)

    # -- copy ------------------------------------------------------------------------------
    def copy_value(self, fmt: str = "json") -> Any:
        """Copy the current node's value (json | json_compact | python | csv |
        csv_header); loads in a silent task. Returns the task (``None``: no node)."""
        ref = self.current_ref()
        if ref is None:
            return None
        return copy_node_value(self.session, ref, fmt, owner=self, parent=self)

    def copy_path(self, style: str = "dotted") -> str | None:
        """Copy the current node's absolute path (dotted | jsonpath | pointer | python |
        js). Returns the copied text (``None``: no node)."""
        ref = self.current_ref()
        if ref is None:
            return None
        return copy_node_path(self.session, ref, style)

    def copy_key(self) -> str | None:
        """Copy the current node's key / index (``None``: no node or the root)."""
        ref = self.current_ref()
        if ref is None:
            return None
        return copy_node_key(self.session, ref)

    def show_node_menu(self, ref: Any, global_pos: Any, *, extra: Any = None, node: Any = None) -> Any:
        """Pop up the shared node context menu (:func:`jsonreshape.gui.actions.popup_node_menu`)."""
        from jsonreshape.gui import actions

        return actions.popup_node_menu(self, self.session, ref, global_pos, view=self, extra=extra, node=node)

    def shutdown(self) -> None:
        """Tab closing: detach the view's background tasks (subclasses stop loaders too)."""
        try:
            self.session.app.tasks.detach(self)
        except Exception:
            log.debug("detach failed", exc_info=True)


# ==================================================================================
# InfoBar
# ==================================================================================
_KIND_ROLE = {"info": "accent", "warning": "warn", "error": "danger", "success": "ok"}
_KIND_FALLBACK = {"info": "#2a57c4", "warning": "#9a5a08", "error": "#b23434", "success": "#2b7649"}
_KIND_ICON = {
    "info": QStyle.StandardPixmap.SP_MessageBoxInformation,
    "warning": QStyle.StandardPixmap.SP_MessageBoxWarning,
    "error": QStyle.StandardPixmap.SP_MessageBoxCritical,
    "success": QStyle.StandardPixmap.SP_DialogApplyButton,
}


def _kind_color(kind: str) -> QColor:
    try:
        from jsonreshape.gui import theme

        return QColor(theme.color(_KIND_ROLE[kind]))
    except Exception:
        return QColor(_KIND_FALLBACK[kind])


def _blend(a: QColor, b: QColor, f: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * f), round(a.green() + (b.green() - a.green()) * f),
                  round(a.blue() + (b.blue() - a.blue()) * f))


class InfoBar(QFrame):
    """A message strip above a document's views.

    ``kind``: ``info`` | ``warning`` | ``error`` | ``success``; the message (plain text,
    selectable) and an optional smaller detail line; ``buttons``: ``[(text, callback)]``;
    the close button hides the bar and emits :attr:`closed`.
    """

    closed = Signal()
    KINDS: tuple[str, ...] = ("info", "warning", "error", "success")

    def __init__(self, kind: str = "info", message: str = "", *, detail: str | None = None,
                 buttons: Iterable[tuple[str, Callable[[], Any]]] = (), closable: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("InfoBar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._kind = "info"
        self._styling = False
        self._buttons: list[QPushButton] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(8)
        self._icon = QLabel(self)
        self._icon.setFixedSize(16, 16)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)

        texts = QVBoxLayout()
        texts.setSpacing(1)
        self.message_label = QLabel(self)
        self.detail_label = QLabel(self)
        for label in (self.message_label, self.detail_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            texts.addWidget(label)
        small = QFont(self.detail_label.font())
        if small.pointSizeF() > 0:
            small.setPointSizeF(max(6.0, small.pointSizeF() - 1))
        self.detail_label.setFont(small)
        layout.addLayout(texts, 1)

        self._button_box = QHBoxLayout()
        self._button_box.setSpacing(4)
        layout.addLayout(self._button_box)

        self.close_button = QToolButton(self)
        self.close_button.setText("✕")
        self.close_button.setAutoRaise(True)
        self.close_button.setToolTip(t("gui.common.close"))
        self.close_button.setAccessibleName(t("gui.common.close"))
        self.close_button.clicked.connect(self.close_bar)
        self.close_button.setVisible(closable)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

        self.set_message(message)
        self.set_detail(detail)
        for text, callback in buttons:
            self.add_button(text, callback)
        self.set_kind(kind)
        try:
            from jsonreshape.gui import theme

            theme.notifier().theme_changed.connect(self._apply_style)
        except Exception:
            log.debug("theme notifier unavailable", exc_info=True)

    # -- content -----------------------------------------------------------------------
    def kind(self) -> str:
        return self._kind

    def set_kind(self, kind: str) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"unknown info bar kind: {kind!r}")
        self._kind = kind
        icon = self.style().standardIcon(_KIND_ICON[kind])
        self._icon.setPixmap(icon.pixmap(16, 16))
        self._apply_style()

    def message(self) -> str:
        return self.message_label.text()

    def set_message(self, text: str) -> None:
        self.message_label.setText(copyfmt.escape_surrogates(text or ""))

    def detail(self) -> str | None:
        return self.detail_label.text() if self.detail_label.isVisibleTo(self) else None

    def set_detail(self, text: str | None) -> None:
        self.detail_label.setText(copyfmt.escape_surrogates(text or ""))
        self.detail_label.setVisible(bool(text))

    def add_button(self, text: str, callback: Callable[[], Any]) -> QPushButton:
        button = QPushButton(text, self)
        button.setAutoDefault(False)
        button.clicked.connect(lambda _checked=False, cb=callback: cb())
        self._button_box.addWidget(button)
        self._buttons.append(button)
        return button

    def buttons(self) -> list[QPushButton]:
        return list(self._buttons)

    def clear_buttons(self) -> None:
        for button in self._buttons:
            self._button_box.removeWidget(button)
            button.deleteLater()
        self._buttons.clear()

    def set_closable(self, on: bool) -> None:
        self.close_button.setVisible(on)

    def close_bar(self) -> None:
        """Hide the bar and emit :attr:`closed`."""
        self.hide()
        self.closed.emit()

    # -- colors --------------------------------------------------------------------------
    def accent_color(self) -> QColor:
        return _kind_color(self._kind)

    def background_color(self) -> QColor:
        base = QGuiApplication.palette().color(QPalette.ColorRole.Window)
        return _blend(base, self.accent_color(), 0.14)

    def _apply_style(self) -> None:
        if self._styling:
            return
        self._styling = True
        try:
            base = QGuiApplication.palette().color(QPalette.ColorRole.Window)
            accent = self.accent_color()
            bg = _blend(base, accent, 0.14)
            border = _blend(base, accent, 0.5)
            self.setStyleSheet(
                f"QFrame#InfoBar {{ background-color: {bg.name()}; border: 1px solid {border.name()};"
                f" border-radius: 4px; }}")
        finally:
            self._styling = False

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._apply_style()


# ==================================================================================
# ErrorDialog
# ==================================================================================
class ErrorDialog(QDialog):
    """Error window: message, hint, JSON error location + context, collapsible details
    (traceback), buttons "open log", "copy", "close". Use :func:`show_error`."""

    def __init__(self, exc: BaseException, parent: QWidget | None = None, *, title: str | None = None) -> None:
        super().__init__(parent)
        self.exc = exc
        self.expected = isinstance(exc, JsonReshapeError)
        self.setWindowTitle(title or t("gui.widgets.error.title"))
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        icon = QLabel(self)
        pixmap = QStyle.StandardPixmap.SP_MessageBoxWarning if self.expected \
            else QStyle.StandardPixmap.SP_MessageBoxCritical
        icon.setPixmap(self.style().standardIcon(pixmap).pixmap(32, 32))
        top.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        self.message_label = self._label(self.message_text())
        bold = QFont(self.message_label.font())
        bold.setBold(True)
        self.message_label.setFont(bold)
        texts.addWidget(self.message_label)

        self.exception_label: QLabel | None = None
        if not self.expected:
            self.exception_label = self._label(copyfmt.escape_surrogates(self.exception_text()))
            texts.addWidget(self.exception_label)

        hint = self.hint_text()
        self.hint_label: QLabel | None = None
        if hint:
            self.hint_label = self._label(hint)
            texts.addWidget(self.hint_label)
        top.addLayout(texts, 1)
        layout.addLayout(top)

        self.location_label: QLabel | None = None
        self.source_label: QLabel | None = None
        self.context_view: QPlainTextEdit | None = None
        if isinstance(exc, JsonSyntaxError):
            self.location_label = self._label(t("gui.widgets.error.location", line=exc.line, column=exc.column,
                                                offset=exc.offset))
            layout.addWidget(self.location_label)
            if exc.source:
                self.source_label = self._label(t("gui.widgets.error.source", path=exc.source))
                layout.addWidget(self.source_label)
            layout.addWidget(self._label(t("gui.widgets.error.context")))
            self.context_view = self._text_box(copyfmt.escape_surrogates(exc.context()), lines=2)
            layout.addWidget(self.context_view)

        self.details_button = QToolButton(self)
        self.details_button.setText(t("gui.widgets.error.details"))
        self.details_button.setCheckable(True)
        self.details_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_button.setArrowType(Qt.ArrowType.RightArrow)
        self.details_button.setAutoRaise(True)
        self.details_button.toggled.connect(self.set_details_visible)
        layout.addWidget(self.details_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.details_view = self._text_box(copyfmt.escape_surrogates(self.details_text()), lines=10)
        self.details_view.setVisible(False)
        layout.addWidget(self.details_view, 1)

        buttons = QHBoxLayout()
        self.open_log_button = QPushButton(t("gui.widgets.error.open_log"), self)
        self.open_log_button.setToolTip(t("gui.widgets.error.open_log.tip"))
        self.open_log_button.setAutoDefault(False)
        self.open_log_button.clicked.connect(self._open_log)
        self.copy_button = QPushButton(t("gui.common.copy"), self)
        self.copy_button.setToolTip(t("gui.widgets.error.copy.tip"))
        self.copy_button.setAutoDefault(False)
        self.copy_button.clicked.connect(self.copy_details)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.open_log_button)
        buttons.addStretch(1)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.resize(560, self.sizeHint().height())

    # -- texts ---------------------------------------------------------------------------
    def message_text(self) -> str:
        if self.expected:
            return copyfmt.escape_surrogates(self.exc.message)  # type: ignore[union-attr]
        return t("gui.widgets.error.unexpected")

    def hint_text(self) -> str | None:
        if self.expected:
            hint = self.exc.hint  # type: ignore[union-attr]
            return copyfmt.escape_surrogates(hint) if hint else None
        from jsonreshape.log import log_path

        return t("gui.widgets.error.unexpected.hint", log=str(log_path()))

    def exception_text(self) -> str:
        text = str(self.exc)
        name = type(self.exc).__name__
        return f"{name}: {text}" if text else name

    def details_text(self) -> str:
        """Everything the dialog knows, as plain text (copied by the copy button)."""
        exc = self.exc
        parts = [self.message_text()]
        if not self.expected:
            parts.append(self.exception_text())
        hint = self.hint_text()
        if hint:
            parts.append(hint)
        if isinstance(exc, JsonSyntaxError):
            parts.append(t("gui.widgets.error.location", line=exc.line, column=exc.column, offset=exc.offset))
            if exc.source:
                parts.append(t("gui.widgets.error.source", path=exc.source))
            parts.append(exc.context())
        if isinstance(exc, JsonReshapeError):
            parts.append(repr(exc))
        if exc.__traceback__ is not None:
            parts.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        elif not self.expected:
            parts.append(self.exception_text())
        return "\n\n".join(p for p in parts if p)

    # -- actions --------------------------------------------------------------------------
    def set_details_visible(self, on: bool) -> None:
        self.details_view.setVisible(on)
        self.details_button.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        if self.details_button.isChecked() != on:
            self.details_button.setChecked(on)

    def copy_details(self) -> str:
        return set_clipboard_text(self.details_text())

    def _open_log(self) -> None:
        try:
            open_log_folder()
        except Exception:
            log.exception("could not open the log folder")

    # -- building blocks -------------------------------------------------------------------
    def _label(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        return label

    def _text_box(self, text: str, lines: int) -> QPlainTextEdit:
        box = QPlainTextEdit(self)
        box.setReadOnly(True)
        box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        box.setFont(mono_font())
        box.setPlainText(text)
        height = box.fontMetrics().lineSpacing() * lines + 2 * box.frameWidth() + 8
        box.setMinimumHeight(height)
        if lines <= 3:
            box.setMaximumHeight(height)
        return box


class ErrorNotice(QDialog):
    """The one window that counts the errors :func:`show_error` did not show (rate
    limit): "N more errors were not shown -- see the log", "open log", "close"."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.count = 0
        self.setWindowTitle(t("gui.widgets.error.more.title"))
        layout = QVBoxLayout(self)
        self.label = QLabel(self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        buttons = QHBoxLayout()
        self.open_log_button = QPushButton(t("gui.widgets.error.open_log"), self)
        self.open_log_button.setToolTip(t("gui.widgets.error.open_log.tip"))
        self.open_log_button.setAutoDefault(False)
        self.open_log_button.clicked.connect(self._open_log)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.open_log_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.add(0)

    def add(self, n: int = 1) -> None:
        """Count ``n`` more hidden errors."""
        self.count += int(n)
        self.label.setText(t("gui.widgets.error.more", count=format_count(self.count)))

    def _open_log(self) -> None:
        try:
            open_log_folder()
        except Exception:
            log.exception("could not open the log folder")


class _ErrorLimit:
    """Rate limit of the error windows (GUI thread)."""

    def __init__(self) -> None:
        self.shown: deque[float] = deque()
        self.notice: ErrorNotice | None = None

    def allow(self) -> bool:
        now = time.monotonic()
        shown = self.shown
        while shown and now - shown[0] >= ERROR_DIALOG_PERIOD_S:
            shown.popleft()
        if len(shown) >= MAX_ERROR_DIALOGS:
            return False
        shown.append(now)
        return True

    def hidden(self, parent: QWidget | None, exc: BaseException) -> ErrorNotice:
        log.info("error window not shown (more than %d in %.0f s): %s: %s", MAX_ERROR_DIALOGS,
                 ERROR_DIALOG_PERIOD_S, type(exc).__name__, exc)
        notice = self.notice
        # (a closed notice may not be deleted yet)
        if notice is None or not shiboken6.isValid(notice) or not notice.isVisible():
            notice = ErrorNotice(parent)
            keep_alive(notice)
            notice.destroyed.connect(lambda *_args, n=notice: self._notice_gone(n))
            self.notice = notice
            notice.open()
        notice.add(1)
        return notice

    def _notice_gone(self, notice: ErrorNotice) -> None:
        if self.notice is notice:      # (an older notice may die after a newer one opened)
            self.notice = None


_error_limit = _ErrorLimit()


def count_hidden_error(parent: QWidget | None, exc: BaseException) -> None:
    """Count an error that was not shown in a window (e.g. by the rate limit of the
    uncaught-exception hook) in the single :class:`ErrorNotice`."""
    _error_limit.hidden(parent, exc)


def reset_error_limit() -> None:
    """Forget the error windows shown so far (tests)."""
    global _error_limit
    _error_limit = _ErrorLimit()


def show_error(parent: QWidget | None, exc: BaseException) -> ErrorDialog | None:
    """Show :class:`ErrorDialog` for ``exc`` without blocking (``open()``; deleted when
    closed). ``Cancelled`` shows nothing (``None``). After :data:`MAX_ERROR_DIALOGS` windows
    within :data:`ERROR_DIALOG_PERIOD_S` seconds no further window opens (``None``): the
    error is only logged (the caller -- ``AppContext.report_error`` -- logs it fully) and
    counted in the single :class:`ErrorNotice`."""
    if isinstance(exc, Cancelled):
        return None
    limit = _error_limit
    if not limit.allow():
        limit.hidden(parent, exc)
        return None
    dialog = ErrorDialog(exc, parent)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    keep_alive(dialog)
    dialog.open()
    return dialog


# ==================================================================================
# FullValueDialog
# ==================================================================================
@dataclass
class FullText:
    """What :func:`load_full_text` produced."""

    text: str
    kind: str            # node kind: object | array | string | number | bool | null
    truncated: bool      # only the first ``cap`` characters / bytes are in ``text``
    size: int            # bytes of the node in the file
    display: str | None = None   # ``text`` with long lines shortened (None: same as ``text``)
    cut_lines: int = 0           # lines shortened in ``display``


#: Paragraph (block) separators of QTextDocument: a line between them is laid out as a whole.
_BLOCK_SPLIT_RE = re.compile("([\n\r\u2029\ufdd0\ufdd1])")   # + CR and the frame markers Qt also splits on


def shorten_long_lines(text: str, max_chars: int = FULL_LINE_CHARS) -> tuple[str, int]:
    """``text`` with every line (text between LF / U+2029, the block separators of Qt's text
    documents) longer than ``max_chars`` characters cut to ``max_chars`` and ``…``;
    ``(text, cut lines)``. Linear time; runs in a worker thread (up to 16 MiB)."""
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text, 0
    parts = _BLOCK_SPLIT_RE.split(text)      # text, separator, text, ...
    if max(map(len, parts)) <= limit:
        return text, 0
    cut = 0
    for i in range(0, len(parts), 2):
        if len(parts[i]) > limit:
            parts[i] = parts[i][:limit] + copyfmt.ELLIPSIS
            cut += 1
    return "".join(parts), cut


def _string_prefix(node: Any, cap: int, cancel: CancelToken | None) -> tuple[str, bool]:
    """The first ``cap`` characters of a string node too large to parse in memory."""
    from jsonreshape.stream import stream_text_prefix

    found = stream_text_prefix(node.doc, (node.start, node.end), "", limit=cap, cancel=cancel)
    if found is None or found[0] != "string":  # pragma: no cover - a string node is a string
        raise TooLargeError(size=format_bytes(node.size))
    return found[1], bool(found[2])


def _pretty_prefix(node: Any, indent: int | str | None, cap: int, cancel: CancelToken | None) -> tuple[bytes, bool]:
    """Pretty JSON of the node's raw bytes (``core.Reformatter``: tokens unchanged), read
    in 1 MiB pieces and stopped once ``cap`` bytes of output exist. ``(text, complete)``."""
    from jsonreshape.core import Reformatter

    fmt = Reformatter(indent=indent)
    out = bytearray()
    pos, end = node.start, node.end
    with node.doc.open_data() as f:
        f.seek(pos)
        while pos < end and len(out) <= cap:
            if cancel is not None:
                cancel.check()
            chunk = f.read(min(_READ_CHUNK, end - pos))
            if not chunk:
                break
            pos += len(chunk)
            out += fmt.feed(chunk)
    complete = pos >= end
    if complete:
        out += fmt.finish()
    if len(out) > cap:
        return bytes(out[:cap]), False
    return bytes(out), complete


def load_full_text(session: Any, ref: Any, cancel: CancelToken | None = None, *,
                   indent: int | str | None = 2, cap: int = FULL_TEXT_CAP,
                   max_line: int = FULL_LINE_CHARS) -> FullText:
    """The text of the "full value" window (worker thread): a string node's decoded text,
    any other node as pretty JSON of its exact source tokens; at most ``cap`` characters
    (strings) / bytes (JSON). ``display`` has the lines longer than ``max_line``
    characters shortened (see :func:`shorten_long_lines`)."""
    node = session.node(ref, cancel)
    kind = node.kind
    size = node.size
    if kind == "string":
        try:
            text = node.value()
            complete = True
        except TooLargeError:
            text, complete = _string_prefix(node, cap, cancel)
        truncated = not complete or len(text) > cap
        shown = copyfmt.escape_surrogates(text[:cap])
    else:
        if getattr(node, "is_virtual", False):
            # the synthetic root of jsonl/stream documents has no bytes of its own
            data = codec.dumps(node.value(), indent=indent)
            complete = len(data) <= cap
        else:
            data, complete = _pretty_prefix(node, indent, cap, cancel)
        shown = data[:cap].decode("utf-8", errors="ignore")
        truncated = not complete
    if cancel is not None:
        cancel.check()
    display, cut = shorten_long_lines(shown, max_line)
    return FullText(shown, kind, truncated, size, display=None if cut == 0 else display, cut_lines=cut)


class FullValueDialog(QDialog):
    """The "to'liq ko'rish" window of a node (non-modal). Loads lazily in a silent task
    owned by the dialog; :attr:`loaded` fires when the text (or an error) is shown."""

    loaded = Signal()

    def __init__(self, session: Any, ref: Any, parent: QWidget | None = None, *,
                 indent: int | str | None = 2, cap: int = FULL_TEXT_CAP, max_line: int = FULL_LINE_CHARS) -> None:
        super().__init__(parent)
        self.session = session
        self.ref = ref
        self.cap = cap
        self.max_line = max_line
        self.indent = indent
        self.result: FullText | None = None
        self.error: BaseException | None = None
        self._task: Any = None
        try:
            path = copyfmt.path_text(session.absolute_path(ref), "dotted")
        except Exception:
            log.debug("absolute_path failed", exc_info=True)
            path = "?"
        self.path_text = path
        self.setWindowTitle(t("gui.widgets.full.title", path=copyfmt.escape_surrogates(path)))
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        self.info_label = ElidedLabel(copyfmt.escape_surrogates(path), self)
        layout.addWidget(self.info_label)
        self.notice_label = QLabel(self)
        self.notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        layout.addWidget(self.notice_label)

        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(True)
        self.editor.setFont(mono_font(session))
        self.editor.setPlaceholderText(t("gui.common.loading"))
        self.editor.setUndoRedoEnabled(False)
        layout.addWidget(self.editor, 1)

        bottom = QHBoxLayout()
        self.wrap_check = QCheckBox(t("gui.widgets.full.wrap"), self)
        self.wrap_check.toggled.connect(self.set_wrap)
        bottom.addWidget(self.wrap_check)
        bottom.addStretch(1)
        self.copy_button = QPushButton(t("gui.common.copy"), self)
        self.copy_button.setToolTip(t("gui.widgets.full.copy.tip"))
        self.copy_button.setAutoDefault(False)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_text)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.clicked.connect(self.accept)
        bottom.addWidget(self.copy_button)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)
        self.set_wrap(False)
        self.resize(760, 520)
        self._start()

    # -- loading -----------------------------------------------------------------------
    def _start(self) -> None:
        session, ref, indent, cap, max_line = self.session, self.ref, self.indent, self.cap, self.max_line

        def work(ctx: Any) -> FullText:
            return load_full_text(session, ref, ctx.cancel, indent=indent, cap=cap, max_line=max_line)

        self._task = session.app.tasks.run(work, title=t("gui.widgets.task.load_value"), kind="value", owner=self,
                                           on_done=self._on_loaded, on_error=self._on_failed, silent=True)

    def _on_loaded(self, result: FullText) -> None:
        self.result = result
        self.set_wrap(result.kind == "string")
        self.editor.setPlainText(result.display if result.display is not None else result.text)
        self.info_label.setText(t("gui.widgets.full.info", path=copyfmt.escape_surrogates(self.path_text),
                                  size=format_size(result.size)))
        notices = []
        if result.truncated:
            shown = len(result.text.encode("utf-8", errors="surrogatepass"))
            notices.append(t("gui.widgets.full.truncated", shown=format_size(shown), total=format_size(result.size)))
        if result.cut_lines:
            notices.append(t("gui.widgets.full.long_lines", count=format_count(result.cut_lines),
                             chars=format_count(self.max_line)))
        if notices:
            self._notice("\n".join(notices))
        self.copy_button.setEnabled(True)
        self.loaded.emit()

    def _on_failed(self, exc: BaseException) -> None:
        self.error = exc
        if isinstance(exc, JsonReshapeError):
            self._notice(exc.message)
        else:
            self._notice(t("gui.widgets.error.unexpected"))
            report_task_error(self.session, exc, self, purpose="view")
        self.editor.setPlaceholderText("")
        self.loaded.emit()

    def _notice(self, text: str) -> None:
        self.notice_label.setText(copyfmt.escape_surrogates(text))
        self.notice_label.setVisible(True)

    # -- user actions ------------------------------------------------------------------
    def text(self) -> str:
        """The text shown (empty until loaded)."""
        return self.result.text if self.result is not None else ""

    def is_loaded(self) -> bool:
        return self.result is not None or self.error is not None

    def set_wrap(self, on: bool) -> None:
        mode = QPlainTextEdit.LineWrapMode.WidgetWidth if on else QPlainTextEdit.LineWrapMode.NoWrap
        self.editor.setLineWrapMode(mode)
        if self.wrap_check.isChecked() != on:
            self.wrap_check.setChecked(on)

    def copy_text(self) -> str:
        """Copy the loaded text -- long lines in full, even though they are shown
        shortened (only the loaded part of a value larger than ``cap``)."""
        return set_clipboard_text(self.text())

    def done(self, result: int) -> None:  # noqa: D401 (Qt name)
        try:
            self.session.app.tasks.detach(self)
        except Exception:
            log.debug("detach failed", exc_info=True)
        super().done(result)
