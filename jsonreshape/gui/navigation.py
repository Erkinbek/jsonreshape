"""Navigation dialogs (docs/ARCHITECTURE.md §11.8; AGENT_PROMPT §3, §5).

* :class:`GoToDialog` (Ctrl+G): one line accepting ``data[120].contact.address``,
  ``$.data[120]``, ``/data/120``, ``#120``, ``#120.contact``; parsed live with
  :func:`jsonreshape.paths.parse_location` (the error position is shown under the text);
  OK resolves the location in a silent task (``session.ref_for_location``) -- a record
  number out of range or a path that does not exist is shown inline and the dialog stays
  open -- then ``session.select(ref, reveal=True)``. Recent texts are kept in the GUI state
  (``goto_history``).
* :class:`BookmarksDialog`: the bookmarks of the file (name, location, created): go
  (double click / Enter), rename, delete. Adding is the main window's Ctrl+B. Locations come
  from ``session.bookmark_ref(i)`` -- while an edit layer is active the session keeps
  bookmarks by stable record ids and gives their current position (docs/ARCHITECTURE.md
  §12.9); a bookmark whose node was deleted shows "(o'chirilgan)". The list follows
  ``session.data_changed``.
* :class:`RecordPathDialog`: the record path of an object-rooted document: the detected
  candidates (items seen in the head, their kind), a free path and a format override;
  accepting reopens the session with the choice -- after asking about unsaved changes
  (non-blocking; a Cancel keeps the dialog open).
* :class:`BrokenLinesDialog`: the broken lines of a JSON Lines file (line, column, offset,
  reason, text) in a virtual grid; activating a line emits :attr:`goto_requested` (the tab
  shows it in the raw view).

The dialogs run their background tasks with a private owner token that is detached when
the dialog closes *or is destroyed* (e.g. together with its closed tab), so no task
callback ever reaches a deleted dialog. A go-to / bookmark lookup resolves positions of the
edit state shown when it started; when an edit is applied meanwhile (``session.data_gen``
changed) it is looked up again.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from jsonreshape.errors import JsonReshapeError, PathSyntaxError, QuerySyntaxError
from jsonreshape.gui import copyfmt
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.gui.widgets import format_count, mono_font
from jsonreshape.i18n import t
from jsonreshape.paths import Location, format_path, parse_location, parse_path

log = logging.getLogger(__name__)

__all__ = ["BookmarksDialog", "BrokenLinesDialog", "GoToDialog", "RecordPathDialog", "GOTO_HISTORY_KEY",
           "GOTO_HISTORY_MAX", "FORMAT_CHOICES"]

GOTO_HISTORY_KEY = "goto_history"
GOTO_HISTORY_MAX = 20
#: Formats offered by the record path dialog (``None`` = automatic).
FORMAT_CHOICES: tuple[str | None, ...] = (None, "array", "jsonl", "wrapped", "keyed", "single", "brace_list",
                                          "stream")


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, JsonReshapeError):
        return exc.message
    return t("gui.nav.unexpected", error=f"{type(exc).__name__}: {exc}")


def _syntax_caret(exc: QuerySyntaxError) -> str:
    text = copyfmt.escape_surrogates(exc.text or "")
    if not text:
        return ""
    return f"{text}\n{' ' * max(0, min(len(text), exc.pos))}^"


class _TaskOwner:
    """The owner of a dialog's background tasks (detached when the dialog goes away)."""

    __slots__ = ()


def _task_owner(dialog: QDialog, tasks: Any) -> _TaskOwner:
    """A task owner for ``dialog`` whose tasks are detached when the dialog is destroyed
    (the connected function keeps no reference to the dialog)."""
    owner = _TaskOwner()

    def detach(*_args: Any) -> None:
        try:
            tasks.detach(owner)
        except Exception:  # pragma: no cover - the manager is gone at exit
            log.debug("detach failed", exc_info=True)

    dialog.destroyed.connect(detach)
    return owner


def _ref(record: int | None, path: Any) -> Any:
    from jsonreshape.gui.session import NodeRef

    return NodeRef(record, tuple(path))


def _data_gen(session: Any) -> Any:
    """The session's edit-state generation (changes with every applied edit)."""
    gen = getattr(session, "data_gen", None)
    if gen is not None:
        return gen
    doc = getattr(session, "doc", None)
    return getattr(doc, "gen", 0) if doc is not None else 0


#: A lookup is repeated at most this many times when edits keep arriving meanwhile.
RESOLVE_RETRIES = 3


def _set_error_style(label: QLabel) -> None:
    try:
        from jsonreshape.gui import theme

        color = theme.color("danger").name()
    except Exception:
        color = "#b23434"
    label.setStyleSheet(f"color: {color};")


# ==================================================================================
# GoToDialog
# ==================================================================================
class GoToDialog(QDialog):
    """Ctrl+G (see the module docstring). :attr:`resolved` fires with the selected
    :class:`~jsonreshape.gui.session.NodeRef` right before the dialog closes."""

    resolved = Signal(object)
    finished_resolving = Signal()      # a resolve task ended (success or error)

    def __init__(self, session: Any, parent: QWidget | None = None, *, text: str = "") -> None:
        super().__init__(parent)
        self.session = session
        self._task: Any = None
        self._owner = _task_owner(self, session.app.tasks)
        self._location: Location | None = None
        self.setWindowTitle(t("gui.nav.goto.title"))
        self.setObjectName("goto_dialog")
        layout = QVBoxLayout(self)

        help_label = QLabel(t("gui.nav.goto.help"), self)
        help_label.setWordWrap(True)
        help_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(help_label)

        self.combo = QComboBox(self)
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.setMinimumContentsLength(40)
        self.combo.setFont(mono_font(session))
        self.combo.addItems(self.history())
        self.combo.setCurrentIndex(-1)
        self.edit: QLineEdit = self.combo.lineEdit()
        self.edit.setPlaceholderText(t("gui.nav.goto.placeholder"))
        self.edit.setClearButtonEnabled(True)
        layout.addWidget(self.combo)

        self.error_label = QLabel(self)
        self.error_label.setObjectName("goto_error")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        _set_error_style(self.error_label)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        self.caret_label = QLabel(self)
        self.caret_label.setTextFormat(Qt.TextFormat.PlainText)
        self.caret_label.setFont(mono_font(session))
        _set_error_style(self.caret_label)
        self.caret_label.setVisible(False)
        layout.addWidget(self.caret_label)
        self.busy_label = QLabel(t("gui.nav.goto.busy"), self)
        self.busy_label.setVisible(False)
        layout.addWidget(self.busy_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                                        self)
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(t("gui.nav.goto.go"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t("gui.common.cancel"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.edit.textChanged.connect(self._validate)
        self.edit.setText(text)
        self._validate()
        self.edit.setFocus()

    # -- history ------------------------------------------------------------------------
    def history(self) -> list[str]:
        try:
            items = self.session.app.state.get(GOTO_HISTORY_KEY, [])
        except Exception:
            return []
        return [str(x) for x in items if isinstance(x, str) and x.strip()][:GOTO_HISTORY_MAX]

    def _remember(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        items = [text] + [x for x in self.history() if x != text]
        try:
            self.session.app.state.set(GOTO_HISTORY_KEY, items[:GOTO_HISTORY_MAX])
        except Exception:
            log.debug("could not store the go-to history", exc_info=True)

    # -- text ---------------------------------------------------------------------------
    def text(self) -> str:
        return self.edit.text()

    def set_text(self, text: str) -> None:
        self.edit.setText(text)

    def location(self) -> Location | None:
        """The parsed location (``None`` while the text is empty or invalid)."""
        return self._location

    def error_text(self) -> str:
        return self.error_label.text() if self.error_label.isVisibleTo(self) else ""

    def is_busy(self) -> bool:
        return self._task is not None

    def _show_error(self, text: str, caret: str = "") -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        self.caret_label.setText(caret)
        self.caret_label.setVisible(bool(caret))

    def _validate(self, *_args: Any) -> None:
        text = self.text()
        self._location = None
        if not text.strip():
            self._show_error("")
            self.ok_button.setEnabled(False)
            return
        try:
            self._location = parse_location(text)
        except QuerySyntaxError as exc:
            self._show_error(exc.message, _syntax_caret(exc))
            self.ok_button.setEnabled(False)
            return
        except (JsonReshapeError, ValueError) as exc:
            self._show_error(_error_text(exc))
            self.ok_button.setEnabled(False)
            return
        self._show_error("")
        self.ok_button.setEnabled(self._task is None)

    # -- resolving ------------------------------------------------------------------------
    def accept(self, _retry: int = 0) -> None:  # noqa: D401 (Qt name)
        """Resolve the location in a silent task; the dialog closes when it was found."""
        if self._task is not None:
            return
        self._validate()
        loc = self._location
        if loc is None:
            return
        session = self.session
        text = self.text()
        gen = _data_gen(session)

        def work(ctx: Any) -> Any:
            return session.ref_for_location(loc, ctx.cancel)

        self.busy_label.setVisible(True)
        self.ok_button.setEnabled(False)
        self._task = session.app.tasks.run(work, title=t("gui.nav.goto.task"), kind="goto", owner=self._owner,
                                           silent=True, on_done=lambda ref: self._found(text, ref, gen, _retry),
                                           on_error=self._not_found, on_cancel=self._resolve_cancelled)

    def _found(self, text: str, ref: Any, gen: Any = None, retry: int = 0) -> None:
        self._task = None
        self.busy_label.setVisible(False)
        if gen is not None and gen != _data_gen(self.session) and retry < RESOLVE_RETRIES:
            # an edit was applied meanwhile: the record number may address another record now
            self.accept(retry + 1)
            return
        self._remember(text)
        self.session.select(ref, reveal=True)
        self.resolved.emit(ref)
        self.finished_resolving.emit()
        super().accept()

    def _not_found(self, exc: BaseException) -> None:
        self._task = None
        self.busy_label.setVisible(False)
        if not isinstance(exc, JsonReshapeError):
            log.error("go to %r failed", self.text(), exc_info=(type(exc), exc, exc.__traceback__))
        self._show_error(_error_text(exc))
        self.ok_button.setEnabled(self._location is not None)
        self.finished_resolving.emit()

    def _resolve_cancelled(self) -> None:
        self._task = None
        self.busy_label.setVisible(False)
        self.ok_button.setEnabled(self._location is not None)
        self.finished_resolving.emit()

    def done(self, result: int) -> None:  # noqa: D401 (Qt name)
        try:
            self.session.app.tasks.detach(self._owner)
        except Exception:
            log.debug("detach failed", exc_info=True)
        self._task = None
        super().done(result)


# ==================================================================================
# BookmarksDialog
# ==================================================================================
class BookmarksDialog(QDialog):
    """The bookmarks of one file (see the module docstring)."""

    resolved = Signal(object)          # the NodeRef selected by "go"

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._task: Any = None
        self._owner = _task_owner(self, session.app.tasks)
        self.setWindowTitle(t("gui.nav.bookmarks.title", name=session.name))
        self.setObjectName("bookmarks_dialog")
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget(self)
        self.tree.setObjectName("bookmarks_tree")
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([t("gui.nav.bookmarks.col.name"), t("gui.nav.bookmarks.col.location"),
                                   t("gui.nav.bookmarks.col.created")])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 200)
        self.tree.itemActivated.connect(lambda _item, _col: self.go())
        self.tree.currentItemChanged.connect(lambda *_a: self._sync_buttons())
        layout.addWidget(self.tree, 1)

        self.empty_label = QLabel(t("gui.nav.bookmarks.empty"), self)
        self.empty_label.setWordWrap(True)
        self.empty_label.setEnabled(False)
        layout.addWidget(self.empty_label)
        self.error_label = QLabel(self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        _set_error_style(self.error_label)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        row = QHBoxLayout()
        self.go_button = QPushButton(t("gui.nav.bookmarks.go"), self)
        self.go_button.setDefault(True)
        self.go_button.clicked.connect(lambda: self.go())
        self.rename_button = QPushButton(t("gui.nav.bookmarks.rename"), self)
        self.rename_button.setAutoDefault(False)
        self.rename_button.clicked.connect(lambda: self.rename())
        self.delete_button = QPushButton(t("gui.nav.bookmarks.delete"), self)
        self.delete_button.setAutoDefault(False)
        self.delete_button.clicked.connect(lambda: self.delete())
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.reject)
        for button in (self.go_button, self.rename_button, self.delete_button):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.close_button)
        layout.addLayout(row)

        session.bookmarks_changed.connect(self.refresh)
        self._data_signal = getattr(session, "data_changed", None)
        if self._data_signal is not None and hasattr(self._data_signal, "connect"):
            self._data_signal.connect(self.refresh)     # positions move with edits
        else:
            self._data_signal = None
        self.refresh()
        self.resize(640, 360)

    def _location(self, i: int, bm: Any) -> tuple[str, bool]:
        """``(location text, deleted)`` of bookmark ``i`` -- at its current position (the
        session converts the record ids it keeps while an edit layer is active)."""
        try:
            ref = self.session.bookmark_ref(i)
        except (IndexError, KeyError, LookupError, ValueError):
            ref = None
        except Exception:
            log.debug("bookmark_ref(%d) failed", i, exc_info=True)
            ref = _ref(bm.record, bm.path)
        if ref is None:
            return t("gui.nav.bookmarks.deleted"), True
        try:
            return self.session.location_text(ref), False
        except Exception:
            return "?", False

    def refresh(self, *_args: Any) -> None:
        current = self.current_index()
        self.tree.clear()
        for i, bm in enumerate(self.session.bookmarks):
            location, deleted = self._location(i, bm)
            created = time.strftime("%Y-%m-%d %H:%M", time.localtime(bm.created)) if bm.created else ""
            item = QTreeWidgetItem(self.tree, [copyfmt.escape_surrogates(bm.name),
                                               copyfmt.escape_surrogates(location), created])
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            item.setToolTip(1, t("gui.nav.bookmarks.deleted.tip") if deleted else copyfmt.escape_surrogates(location))
            if deleted:
                font = item.font(1)
                font.setItalic(True)
                item.setFont(1, font)
        count = self.tree.topLevelItemCount()
        if count:
            self.tree.setCurrentItem(self.tree.topLevelItem(min(max(current, 0), count - 1)))
        self.empty_label.setVisible(count == 0)
        self._sync_buttons()

    def count(self) -> int:
        return self.tree.topLevelItemCount()

    def current_index(self) -> int:
        item = self.tree.currentItem()
        return self.tree.indexOfTopLevelItem(item) if item is not None else -1

    def set_current(self, index: int) -> None:
        item = self.tree.topLevelItem(index)
        if item is not None:
            self.tree.setCurrentItem(item)

    def _sync_buttons(self) -> None:
        has = self.current_index() >= 0
        for button in (self.go_button, self.rename_button, self.delete_button):
            button.setEnabled(has and self._task is None)

    def _index(self, index: int | None) -> int | None:
        i = self.current_index() if index is None else index
        return i if 0 <= i < len(self.session.bookmarks) else None

    def go(self, index: int | None = None, _retry: int = 0) -> None:
        """Resolve the bookmark in a silent task, select it (``reveal``) and close."""
        i = self._index(index)
        if i is None or self._task is not None:
            return
        session = self.session
        try:
            ref = session.bookmark_ref(i)
        except Exception:
            log.debug("bookmark_ref(%d) failed", i, exc_info=True)
            ref = None
        if ref is None:                      # its node was deleted by an edit
            self.error_label.setText(t("gui.nav.bookmarks.deleted.tip"))
            self.error_label.setVisible(True)
            return
        self.error_label.setVisible(False)
        gen = _data_gen(session)

        def work(ctx: Any) -> Any:
            return session.resolve(ref, ctx.cancel)

        def found(resolved: Any) -> None:
            self._task = None
            if gen != _data_gen(session) and _retry < RESOLVE_RETRIES:
                self.go(i, _retry + 1)       # an edit moved things meanwhile: look again
                return
            session.select(resolved, reveal=True)
            self.resolved.emit(resolved)
            self.accept()

        def failed(exc: BaseException) -> None:
            self._task = None
            self.error_label.setText(_error_text(exc))
            self.error_label.setVisible(True)
            self._sync_buttons()

        def cancelled() -> None:
            self._task = None
            self._sync_buttons()

        self._task = session.app.tasks.run(work, title=t("gui.nav.goto.task"), kind="goto", owner=self._owner,
                                           silent=True, on_done=found, on_error=failed, on_cancel=cancelled)
        self._sync_buttons()

    def ask_name(self, current: str) -> str | None:
        """Ask for a new name (tests replace this method)."""
        text, ok = QInputDialog.getText(self, t("gui.nav.bookmarks.rename.title"), t("gui.nav.bookmarks.rename.label"),
                                        QLineEdit.EchoMode.Normal, current)
        return text if ok else None

    def rename(self, index: int | None = None, name: str | None = None) -> None:
        i = self._index(index)
        if i is None:
            return
        if name is None:
            name = self.ask_name(self.session.bookmarks[i].name)
        if name is None or not name.strip():
            return
        self.session.rename_bookmark(i, name)

    def delete(self, index: int | None = None) -> None:
        i = self._index(index)
        if i is None:
            return
        self.session.remove_bookmark(i)

    def done(self, result: int) -> None:  # noqa: D401 (Qt name)
        try:
            self.session.app.tasks.detach(self._owner)
            self.session.bookmarks_changed.disconnect(self.refresh)
        except (RuntimeError, TypeError):
            pass
        except Exception:
            log.debug("bookmarks dialog cleanup failed", exc_info=True)
        if self._data_signal is not None:
            try:
                self._data_signal.disconnect(self.refresh)
            except (RuntimeError, TypeError):
                pass
            self._data_signal = None
        self._task = None
        super().done(result)


# ==================================================================================
# RecordPathDialog
# ==================================================================================
def _kind_label(kind: str | None) -> str:
    labels = {
        None: t("gui.nav.rp.format.auto"),
        "array": t("reader.kind.array"),
        "jsonl": t("reader.kind.jsonl"),
        "brace_list": t("reader.kind.brace_list"),
        "wrapped": t("reader.kind.wrapped"),
        "keyed": t("reader.kind.keyed"),
        "single": t("reader.kind.single"),
        "stream": t("reader.kind.stream"),
    }
    return labels.get(kind, str(kind))


def _items_kind_label(kind: str) -> str:
    labels = {
        "objects": t("gui.nav.rp.items.objects"),
        "arrays": t("gui.nav.rp.items.arrays"),
        "mixed": t("gui.nav.rp.items.mixed"),
        "values": t("gui.nav.rp.items.values"),
        "empty": t("gui.nav.rp.items.empty"),
    }
    return labels.get(kind, kind)


def _path_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return format_path(tuple(value)) or "$"
    except Exception:
        return str(value)


class RecordPathDialog(QDialog):
    """Choose the record path / format of a document (see the module docstring).

    :meth:`chosen` returns ``(record_path, format)`` (``None`` = automatic); accepting
    calls ``session.reopen(record_path=..., format=...)`` when ``apply`` is True."""

    def __init__(self, session: Any, parent: QWidget | None = None, *, apply: bool = True) -> None:
        super().__init__(parent)
        self.session = session
        self.apply = apply
        self.setWindowTitle(t("gui.nav.rp.title", name=session.name))
        self.setObjectName("record_path_dialog")
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        intro = QLabel(t("gui.nav.rp.intro"), self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list = QTreeWidget(self)
        self.list.setObjectName("record_path_list")
        self.list.setColumnCount(3)
        self.list.setHeaderLabels([t("gui.nav.rp.col.path"), t("gui.nav.rp.col.items"), t("gui.nav.rp.col.kind")])
        self.list.setRootIsDecorated(False)
        self.list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.list.currentItemChanged.connect(self._row_chosen)
        self.list.itemClicked.connect(lambda item, _col: self._row_chosen(item))
        self.list.itemActivated.connect(lambda _item, _col: self.accept())
        layout.addWidget(self.list, 1)

        form = QFormLayout()
        self.path_edit = QLineEdit(self)
        self.path_edit.setObjectName("record_path_edit")
        self.path_edit.setPlaceholderText(t("gui.nav.rp.path.placeholder"))
        self.path_edit.setFont(mono_font(session))
        self.path_edit.setClearButtonEnabled(True)
        self.path_edit.textChanged.connect(self._validate)
        form.addRow(t("gui.nav.rp.path"), self.path_edit)
        self.format_combo = QComboBox(self)
        self.format_combo.setObjectName("record_format_combo")
        for kind in FORMAT_CHOICES:
            self.format_combo.addItem(_kind_label(kind), kind)
        self.format_combo.currentIndexChanged.connect(self._validate)
        form.addRow(t("gui.nav.rp.format"), self.format_combo)
        layout.addLayout(form)

        self.error_label = QLabel(self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        _set_error_style(self.error_label)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                                        self)
        self.ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(t("gui.nav.rp.reopen"))
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t("gui.common.cancel"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._filling = False
        self._asking = False             # the unsaved-changes question is open
        self._fill()
        self.resize(560, 380)

    def _fill(self) -> None:
        self._filling = True
        try:
            auto = QTreeWidgetItem(self.list, [t("gui.nav.rp.auto"), "", ""])
            auto.setData(0, Qt.ItemDataRole.UserRole, None)
            doc = getattr(self.session, "doc", None)
            candidates = list(getattr(getattr(doc, "info", None), "candidates", None) or [])
            for cand in candidates:
                items = format_count(cand.items_in_head)
                if not cand.closed_in_head:
                    items = t("gui.nav.rp.items.more", count=items)
                item = QTreeWidgetItem(self.list, [copyfmt.escape_surrogates(cand.text), items,
                                                   _items_kind_label(cand.items_kind)])
                item.setData(0, Qt.ItemDataRole.UserRole, cand.text)
                item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.list.resizeColumnToContents(1)
            # preselect the session's current choice
            current = _path_text(getattr(self.session, "record_path", None))
            self.path_edit.setText(current)
            fmt = getattr(self.session, "format", None)
            index = self.format_combo.findData(fmt)
            self.format_combo.setCurrentIndex(max(0, index))
            chosen: QTreeWidgetItem | None = auto
            if current:   # the row of the current path; none when it is not a candidate
                chosen = None
                for i in range(self.list.topLevelItemCount()):
                    item = self.list.topLevelItem(i)
                    if item.data(0, Qt.ItemDataRole.UserRole) == current:
                        chosen = item
                        break
            if chosen is not None:
                self.list.setCurrentItem(chosen)
        finally:
            self._filling = False
        self._validate()

    def candidate_count(self) -> int:
        """Rows besides "automatic"."""
        return self.list.topLevelItemCount() - 1

    def choose_row(self, index: int) -> None:
        """Select row ``index`` (0 = automatic, 1.. = the candidates)."""
        item = self.list.topLevelItem(index)
        if item is not None:
            self.list.setCurrentItem(item)
            self._row_chosen(item)

    def _row_chosen(self, item: QTreeWidgetItem | None, _previous: Any = None) -> None:
        if self._filling or item is None:
            return
        value = item.data(0, Qt.ItemDataRole.UserRole)
        self.path_edit.setText(value or "")
        if value:
            index = self.format_combo.findData(None)
            if self.format_combo.currentData() not in (None, "wrapped"):
                self.format_combo.setCurrentIndex(index)

    def set_format(self, fmt: str | None) -> None:
        index = self.format_combo.findData(fmt)
        if index >= 0:
            self.format_combo.setCurrentIndex(index)

    def chosen(self) -> tuple[str | None, str | None]:
        """``(record_path, format)``; ``None`` = automatic."""
        text = self.path_edit.text().strip()
        return (text or None), self.format_combo.currentData()

    def error_text(self) -> str:
        return self.error_label.text() if self.error_label.isVisibleTo(self) else ""

    def _validate(self, *_args: Any) -> bool:
        record_path, fmt = self.chosen()
        error = ""
        if record_path is not None:
            try:
                parse_path(record_path)
            except PathSyntaxError as exc:
                error = exc.message
            except (JsonReshapeError, ValueError) as exc:
                error = _error_text(exc)
            if not error and fmt not in (None, "wrapped") and record_path not in ("$",):
                error = t("gui.nav.rp.conflict", format=_kind_label(fmt))
            if not error and record_path == "$" and fmt not in (None, "wrapped", "array", "keyed"):
                error = t("gui.nav.rp.conflict", format=_kind_label(fmt))
        self.error_label.setText(error)
        self.error_label.setVisible(bool(error))
        self.ok_button.setEnabled(not error)
        return not error

    def accept(self) -> None:  # noqa: D401 (Qt name)
        """Reopen the session with the choice (``apply``) -- after asking about unsaved
        changes; the dialog closes when the answer lets the reopen go on."""
        if not self._validate():
            return
        if not self.apply:
            super().accept()
            return
        if self._asking:
            return
        record_path, fmt = self.chosen()
        from jsonreshape.gui.doctab import confirm_drop_edits, reopen_session

        def answered(ok: bool) -> None:
            if not isValid(self):
                return                   # the dialog went away meanwhile
            self._asking = False
            if not ok:
                return                   # Cancel: the dialog stays open
            try:
                reopen_session(self.session, record_path=record_path, format=fmt)
            except Exception as exc:
                log.exception("reopening %s failed", getattr(self.session, "name", "?"))
                self.session.app.report_error(exc, self)
                return
            QDialog.accept(self)

        self._asking = True
        confirm_drop_edits(self.session, self, answered, why="reopen")


# ==================================================================================
# BrokenLinesDialog
# ==================================================================================
class _BrokenSource:
    """GridSource over a list of :class:`~jsonreshape.reader.BrokenLine`."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    def row_count(self) -> int:
        return len(self.rows)

    def cell(self, row: int, column: Column) -> Cell | None:
        if not 0 <= row < len(self.rows):
            return None
        bl = self.rows[row]
        cid = column.id
        if cid == "line":
            return Cell(format_count(bl.line), role="number", align="right")
        if cid == "column":
            return Cell(format_count(bl.column) if bl.column else "", role="muted", align="right")
        if cid == "offset":
            return Cell(format_count(bl.offset), role="number", align="right")
        if cid == "reason":
            return Cell(bl.reason, role="error")
        if cid == "text":
            text = copyfmt.escape_surrogates(bl.preview or "")
            return Cell(text, tooltip=text or None)
        return Cell("")

    def rows_shown(self, first: int, last: int) -> None:
        pass

    def row_label(self, row: int) -> str | None:
        return None


class BrokenLinesDialog(QDialog):
    """The broken lines of a JSON Lines document (non-modal; see the module docstring)."""

    goto_requested = Signal(object, object)  # data-file offset, length (Python ints: any size)
    loaded = Signal()                        # the full list (beyond the in-memory part) arrived

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._owner = _task_owner(self, session.app.tasks)
        self.setWindowTitle(t("gui.nav.broken.title", name=session.name))
        self.setObjectName("broken_lines_dialog")
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        self.summary_label = QLabel(self)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.source = _BrokenSource()
        self.grid = VirtualGrid(self)
        self.grid.setObjectName("broken_lines_grid")
        self.grid.set_columns([
            Column("line", t("gui.nav.broken.col.line"), width=90, align="right"),
            Column("column", t("gui.nav.broken.col.column"), width=70, align="right"),
            Column("offset", t("gui.nav.broken.col.offset"), width=120, align="right"),
            Column("reason", t("gui.nav.broken.col.reason"), width=260),
            Column("text", t("gui.nav.broken.col.text"), width=360),
        ])
        self.grid.set_selection_mode("single")
        self.grid.set_source(self.source)
        self.grid.activated.connect(lambda row, _col: self.go(row))
        layout.addWidget(self.grid, 1)

        row = QHBoxLayout()
        self.go_button = QPushButton(t("gui.nav.broken.go"), self)
        self.go_button.setToolTip(t("gui.nav.broken.go.tip"))
        self.go_button.clicked.connect(lambda: self.go())
        row.addWidget(self.go_button)
        row.addStretch(1)
        close = QPushButton(t("gui.common.close"), self)
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)

        doc = session.doc
        self.total = int(getattr(doc, "broken_count", 0) or 0) if doc is not None else 0
        self.source.rows = list(getattr(doc, "broken_lines", None) or []) if doc is not None else []
        self.grid.reset()
        if self.source.rows:
            self.grid.set_current(0, 0)
        self._update_summary()
        self._task: Any = None
        if doc is not None and self.total > len(self.source.rows) and doc.index_complete:
            self._load_more(doc)
        self.resize(900, 420)

    def _update_summary(self) -> None:
        shown = len(self.source.rows)
        if self.total > shown:
            text = t("gui.nav.broken.summary_capped", count=format_count(self.total), shown=format_count(shown))
        else:
            text = t("gui.nav.broken.summary", count=format_count(self.total))
        self.summary_label.setText(text)

    def _load_more(self, doc: Any) -> None:
        def work(ctx: Any) -> list[Any]:
            out = []
            for i, bl in enumerate(doc.iter_broken_lines()):
                if i % 1024 == 0:
                    ctx.cancel.check()
                out.append(bl)
            return out

        def done(rows: list[Any]) -> None:
            self._task = None
            if len(rows) > len(self.source.rows):
                current = self.grid.current()[0]
                self.source.rows = rows
                self.grid.rows_changed()
                if current >= 0:
                    self.grid.set_current(current, 0)
                self._update_summary()
            self.loaded.emit()

        def failed(exc: BaseException) -> None:
            self._task = None
            log.warning("loading the broken lines failed: %r", exc)
            self.loaded.emit()

        self._task = self.session.app.tasks.run(work, title=t("gui.nav.broken.task"), kind="broken",
                                                owner=self._owner, silent=True, on_done=done, on_error=failed)

    def row_count(self) -> int:
        return len(self.source.rows)

    def line_at(self, row: int) -> Any:
        return self.source.rows[row]

    def go(self, row: int | None = None) -> None:
        """Show broken line ``row`` (default: the current one) in the raw view."""
        if row is None:
            row = self.grid.current()[0]
        if row is None or not 0 <= row < len(self.source.rows):
            return
        bl = self.source.rows[row]
        self.goto_requested.emit(int(bl.offset), max(1, int(bl.end) - int(bl.offset)))

    def done(self, result: int) -> None:  # noqa: D401 (Qt name)
        try:
            self.session.app.tasks.detach(self._owner)
        except Exception:
            log.debug("detach failed", exc_info=True)
        super().done(result)
