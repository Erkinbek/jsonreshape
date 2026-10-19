"""Bulk operations dialog (docs/ARCHITECTURE.md §12.9 ``bulkdialog.py``, §12.6; AGENT_PROMPT §8.3).

:class:`BulkDialog` (non-modal, one per session -- :func:`open_bulk_dialog` / :meth:`BulkDialog.show_for`
reuse the open one) runs one bulk operation of :mod:`jsonreshape.bulk` on a document of a
:class:`~jsonreshape.gui.session.DocSession`:

* operation combo (:data:`jsonreshape.bulk.KINDS`: replace, rename key, delete field, set field, clean
  strings, convert type, remove duplicates) and a parameter form per kind, validated inline with
  ``BulkSpec.validate()`` on every change (the message under the form, the offending field outlined; the
  one-line ``BulkSpec.label()`` when valid);
* scope radio: all records / the current view (``session.snapshot()``: the pinned document and the view
  set, leased for the task with ``keep_document_for``; refused while the view is still being computed) /
  the selected records of the active view (the ``selection`` callback, see below);
* Preview: ``session.edit.bulk_preview(spec, scope, on_done, on_failed=...)`` -- a heavy task with progress
  and a Stop button -- then the exact counts, a table of the first 5 examples (record, path, before -> after),
  the problems (conflicts / failures with their reason) and the preview's warnings; the huge-record
  limitation is stated before Preview when the document has records larger than ``large_node_bytes`` (found
  by a background scan of the index spans); a refused / failed / stopped preview is explained in the status
  line (the controller reports errors and refusals too);
* Apply is enabled only while ``preview.gen == session.edit.applied_gen`` (and the preview changes
  something, editing is available and no command is in flight): the dialog listens to
  ``session.data_changed`` and ``session.edit_state_changed``; a preview of an older state is stale for
  good (gens only grow): its files are discarded at once and the dialog says "preview again". Apply hands
  the preview to ``session.edit.bulk_apply(preview, on_done=...)`` (the controller owns it from then on);
  ``on_done(ChangeSet)`` -> "applied" (signal :attr:`BulkDialog.applied`), ``on_done(None)`` -> nothing was
  committed: the preview is discarded (the worker is done with it); a refusal (``bulk_apply`` returned
  False) keeps a still fresh preview for another try;
* Close (and every newer preview) discards the preview that was not applied (``BulkPreview.discard()``); a
  preview still running is cancelled and discarded when its result arrives; a preview being applied is left
  to the controller.

The ``selection`` callback
==========================
``selection()`` is called in the GUI thread whenever the dialog needs the active view's selection (the
scope label when the dialog is activated or :meth:`BulkDialog.refresh_scope` is called, and at Preview). It
returns ``None`` (no selection) or a scope ``bulk.preview`` accepts: a ``RecordSet``, ``range``,
``array('Q')`` / list of positions, or any object with ``iter_chunks()`` and ``len()`` -- typically a
:class:`SelectionScope`, which resolves the selected ROWS of a view to record positions lazily, inside the
preview task (so a Ctrl+A over millions of rows costs nothing in the GUI thread). A scope without a ``gen``
attribute is wrapped in a :class:`SelectionScope` tagged with the gen shown when it was captured, so a
selection made before an edit can never be applied to the newer state (``bulk.error.stale_scope``).
:meth:`SelectionScope.for_view_rows` builds the scope of a table selection (row ranges of the current
view, overlapping ranges merged). Without a callback the dialog asks its parent window's ``active_view()``
when that view shows the same session: the view's ``bulk_selection()`` if it has one, else (public view
APIs) the table's ``grid.selected_ranges()`` or the records holding the tree's ``selected_keypaths()`` (tree
top keys are positions); otherwise "selected records" is disabled.

Public API: ``BulkDialog(session, scope_default="all", *, selection=None, parent=None, spec=None)``;
``show_for(session, *, parent=None, scope_default=None, selection=None, spec=None, preview=False)``
(classmethod; ``spec`` fills the form, ``preview`` starts the preview -- both left alone while a preview
runs) = :func:`open_bulk_dialog`; ``instance(session)``; ``kind()``, ``set_kind(kind)``, ``spec()``,
``set_spec(spec)``, ``spec_error()``; ``scope_choice()``, ``set_scope_choice(scope)``,
``set_selection_source(fn)``, ``refresh_scope()``; ``preview() -> GuiTask | None``, ``stop()``,
``apply() -> bool``, ``can_apply()``; ``preview_result`` (the BulkPreview shown), ``task`` (the running
preview); signals ``preview_finished(preview)``, ``preview_failed(exc)``, ``applied(changeset)``.
The last operation and its parameters are remembered in the GUI state (``bulk_last``).

Decisions: the "current view" scope of an unfiltered view is ``None`` (all records); a preview whose
``gen`` is newer than ``applied_gen`` (a ChangeSet not yet installed) waits instead of being declared
stale; the reason editing is unavailable is the controller's ``reason_text``; the scope's record sets
(the view, a selection's source) are leased with ``session.keep_document_for(task, sets, doc=pinned)`` in
addition to what the controller leases.
"""

from __future__ import annotations

import logging
import operator
import weakref
from array import array
from typing import Any, Callable, Iterator

import shiboken6
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.bulk import KINDS, BulkPreview, BulkSpec, kind_title
from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.gui import theme
from jsonreshape.i18n import t
from jsonreshape.recordset import RecordSet
from jsonreshape.tasks import format_bytes, format_duration

log = logging.getLogger(__name__)

__all__ = ["BulkDialog", "SCOPES", "SelectionScope", "open_bulk_dialog"]

SCOPES = ("all", "view", "selected")
PROGRESS_POLL_MS = 150          # progress / task state refresh while a preview runs
VALIDATE_DELAY_MS = 120         # inline validation after a parameter change
STATE_KEY = "bulk_last"         # GuiState: the last operation and its parameters
HUGE_SCAN_BATCH = 1 << 18       # index spans read per step of the huge-record scan
_TYPES = ("string", "number", "bool", "null", "object", "array")
_SET_MODES = ("always", "if_missing", "if_exists")

_OPEN: weakref.WeakKeyDictionary[Any, BulkDialog] = weakref.WeakKeyDictionary()


def _alive(obj: Any) -> bool:
    try:
        return obj is not None and shiboken6.isValid(obj)
    except Exception:  # pragma: no cover
        return False


def _discard(preview: Any) -> None:
    """Discard a preview that will not be applied (safe at any time, idempotent)."""
    fn = getattr(preview, "discard", None)
    if callable(fn):
        try:
            fn()
        except Exception:  # pragma: no cover - best effort
            log.warning("discarding a bulk preview failed", exc_info=True)


def _apply_done(ref: Any, preview: Any, cs: Any) -> None:
    """The controller's answer to ``bulk_apply``: the ChangeSet, or ``None`` when nothing was committed -- the
    worker is done with the preview then, so its files are discarded (also when the dialog is gone)."""
    if cs is None:
        _discard(preview)
    dlg = ref()
    if dlg is not None and _alive(dlg) and not dlg._closing:
        dlg._apply_finished(preview, cs)


def _count(n: int) -> str:
    from jsonreshape.gui.widgets import format_count

    return format_count(int(n))


# ======================================================================================
# selection scope
# ======================================================================================
class SelectionScope:
    """Selected records resolved lazily (module docstring): ``rows`` -- inclusive ``(first, last)`` row
    ranges of ``source`` (the view RecordSet mapping rows to record positions; ``None`` = the base view, row
    == position) -- and/or explicit ``positions`` (any iterable / RecordSet / array). ``gen``: the state the
    rows / positions belong to. ``iter_chunks()`` runs in the preview task."""

    def __init__(self, *, source: Any = None, rows: Any = (), positions: Any = None, gen: int | None = None) -> None:
        self.source = source
        self.rows = tuple((min(int(a), int(b)), max(int(a), int(b))) for a, b in rows)
        self.positions = positions
        self.gen = gen

    @classmethod
    def for_view_rows(cls, session: Any, ranges: Any) -> SelectionScope | None:
        """The records of the selected rows of the session's current view: ``ranges`` are inclusive
        ``(first_row, last_row)`` pairs or VirtualGrid ranges ``(r0, r1, c0, c1)``. ``None`` when empty."""
        spans = sorted((min(int(item[0]), int(item[1])), max(int(item[0]), int(item[1])))
                       for item in ranges or () if int(item[0]) >= 0 and int(item[1]) >= 0)
        rows: list[tuple[int, int]] = []
        for a, b in spans:                       # overlapping / adjacent ranges merged: each row once
            if rows and a <= rows[-1][1] + 1:
                rows[-1] = (rows[-1][0], max(rows[-1][1], b))
            else:
                rows.append((a, b))
        if not rows:
            return None
        base = bool(getattr(session, "view_is_base", True))
        source = None if base else session.view_recordset()
        gen = getattr(source, "gen", None) if source is not None else None
        if gen is None:
            gen = _shown_gen(session)
        return cls(source=source, rows=rows, gen=gen)

    def __len__(self) -> int:
        n = sum(b - a + 1 for a, b in self.rows)
        if self.positions is not None:
            try:
                n += len(self.positions)
            except TypeError:
                pass
        return n

    def __repr__(self) -> str:
        return f"<SelectionScope {len(self)} records gen={self.gen}>"

    def iter_chunks(self, size: int = 65536) -> Iterator[array]:
        size = max(1, int(size))
        for a, b in self.rows:
            lo = a
            while lo <= b:
                hi = min(b, lo + size - 1)
                if self.source is None:
                    yield array("Q", range(lo, hi + 1))
                else:
                    chunk = self.source[lo:hi + 1]
                    if not len(chunk):
                        break
                    yield array("Q", chunk)
                lo = hi + 1
        pos = self.positions
        if pos is None:
            return
        if hasattr(pos, "iter_chunks"):
            for chunk in pos.iter_chunks():
                yield array("Q", chunk)
            return
        if isinstance(pos, (array, list, tuple, range)):
            for i in range(0, len(pos), size):
                yield array("Q", [operator.index(p) for p in pos[i:i + size]])
            return
        batch = array("Q")
        for p in pos:
            batch.append(operator.index(p))
            if len(batch) >= size:
                yield batch
                batch = array("Q")
        if batch:
            yield batch


def _shown_gen(session: Any) -> int | None:
    edit = getattr(session, "edit", None)
    gen = getattr(edit, "applied_gen", None) if edit is not None else None
    if gen is None:
        gen = getattr(session, "data_gen", None)
    return None if gen is None else int(gen)


def _has_huge_records(doc: Any, limit: int, cancel: Any) -> bool:
    """Whether a record of ``doc``'s file is larger than ``limit`` (a scan of the index spans; edited
    records are never larger than the limit). Unknown -> True (the note is shown)."""
    spans = getattr(doc, "_spans_bytes", None)
    n = getattr(doc, "base_record_count", None)
    if not callable(spans) or n is None:
        return True
    n = int(n)
    i = 0
    while i < n:
        if cancel is not None:
            cancel.check()
        k = min(HUGE_SCAN_BATCH, n - i)
        a = array("Q")
        a.frombytes(spans(i, k, True))
        if not a:
            break
        if max(map(operator.sub, a[1::2], a[0::2])) > limit:
            return True
        i += k
    return False


# ======================================================================================
# the dialog
# ======================================================================================
class BulkDialog(QDialog):
    """Preview and apply one bulk operation (module docstring)."""

    preview_finished = Signal(object)    # BulkPreview
    preview_failed = Signal(object)      # the exception
    applied = Signal(object)             # ChangeSet of the applied operation

    def __init__(self, session: Any, scope_default: str = "all", *, selection: Callable[[], Any] | None = None,
                 parent: QWidget | None = None, spec: BulkSpec | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BulkDialog")
        self.session = session
        self.app = session.app
        self._selection = selection
        self._edit: Any = None
        self.preview_result: BulkPreview | None = None
        self.task: Any = None
        self._token: object | None = None          # identifies the preview being computed
        self._handed: tuple[BulkPreview, int] | None = None   # preview given to bulk_apply, gen at that time
        self._handed_label = ""
        self._stale = False
        self._closing = False
        self._spec_error: JsonReshapeError | None = None
        self._huge: bool | None = None
        self._huge_task: Any = None
        self._connections: list[tuple[Any, Any]] = []
        self.setWindowTitle(t("gui.bulk.title", name=getattr(session, "name", "")))
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setSizeGripEnabled(True)
        self._poll = QTimer(self)
        self._poll.setInterval(PROGRESS_POLL_MS)
        self._poll.timeout.connect(self._poll_task)
        self._validate_timer = QTimer(self)
        self._validate_timer.setSingleShot(True)
        self._validate_timer.setInterval(VALIDATE_DELAY_MS)
        self._validate_timer.timeout.connect(self._validate)
        self._build()

        for name, slot in (("edit_changed", self._bind_edit), ("edit_state_changed", self._refresh_state),
                           ("data_changed", self._on_data_changed), ("state_changed", self._on_session_state),
                           ("view_changed", self.refresh_scope), ("count_changed", self.refresh_scope),
                           ("index_finished", self._bind_edit)):
            signal = getattr(session, name, None)
            if signal is not None:
                try:
                    signal.connect(slot)
                    self._connections.append((signal, slot))
                except (RuntimeError, TypeError):  # pragma: no cover - a fake session
                    pass
        try:
            _OPEN[session] = self
        except TypeError:  # pragma: no cover - a session that cannot be weakly referenced
            pass
        self._bind_edit()
        if spec is not None:
            self.set_spec(spec)
        else:
            self._restore_last()
        self.set_scope_choice(scope_default if scope_default in SCOPES else "all")
        self._validate()
        self.refresh_scope()
        self._start_huge_check()
        self._refresh_state()
        self.resize(760, 640)

    # ------------------------------------------------------------------------------------ registry
    @classmethod
    def instance(cls, session: Any) -> BulkDialog | None:
        """The open dialog of ``session`` (``None`` when there is none)."""
        try:
            dlg = _OPEN.get(session)
        except TypeError:  # pragma: no cover
            return None
        return dlg if _alive(dlg) and not dlg._closing else None

    @classmethod
    def show_for(cls, session: Any, *, parent: QWidget | None = None, scope_default: str | None = None,
                 selection: Callable[[], Any] | None = None, spec: BulkSpec | None = None,
                 preview: bool = False) -> BulkDialog:
        """Show the session's dialog (created on first use, else raised; one per session). ``spec``: the
        operation to show in the form; ``preview``: start its preview at once (e.g. the duplicates dialog's
        "remove the duplicates"). While a preview of the open dialog runs, neither the form, the scope nor a
        new preview is touched (the running one is shown)."""
        dlg = cls.instance(session)
        if dlg is None:
            dlg = cls(session, scope_default or "all", selection=selection, parent=parent, spec=spec)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        else:
            if selection is not None:
                dlg.set_selection_source(selection)
            if dlg.task is None:
                if spec is not None:
                    dlg.set_spec(spec)
                if scope_default in SCOPES:
                    dlg.set_scope_choice(scope_default)
            dlg.refresh_scope()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        if preview and dlg.task is None:
            dlg.preview()
        return dlg

    # --------------------------------------------------------------------------------------- build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        top = QFormLayout()
        self.kind_combo = QComboBox(self)
        self.kind_combo.setObjectName("bulk_kind")
        for kind in KINDS:
            self.kind_combo.addItem(kind_title(kind), kind)
        top.addRow(t("gui.bulk.operation"), self.kind_combo)
        layout.addLayout(top)

        params = QGroupBox(t("gui.bulk.params"), self)
        params_layout = QVBoxLayout(params)
        self.pages = QStackedWidget(params)
        self.fields: dict[str, dict[str, QWidget]] = {}
        for kind in KINDS:
            self.pages.addWidget(self._page(kind))
        params_layout.addWidget(self.pages)
        help_label = QLabel(t("gui.bulk.param.path.help"), params)
        help_label.setWordWrap(True)
        theme.bind_style(help_label, "color: $muted;")
        params_layout.addWidget(help_label)
        self.summary_label = QLabel(params)
        self.summary_label.setObjectName("bulk_summary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        params_layout.addWidget(self.summary_label)
        self.error_label = QLabel(params)
        self.error_label.setObjectName("bulk_error")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        theme.bind_style(self.error_label, "color: $danger;")
        self.error_label.setVisible(False)
        params_layout.addWidget(self.error_label)
        layout.addWidget(params)

        scope_box = QGroupBox(t("gui.bulk.scope"), self)
        scope_layout = QVBoxLayout(scope_box)
        self.scope_group = QButtonGroup(self)
        self.scope_buttons: dict[str, QRadioButton] = {}
        for i, scope in enumerate(SCOPES):
            button = QRadioButton(scope_box)
            button.setObjectName(f"scope_{scope}")
            self.scope_group.addButton(button, i)
            self.scope_buttons[scope] = button
            scope_layout.addWidget(button)
        self.scope_buttons["selected"].setToolTip(t("gui.bulk.scope.selected.tip"))
        self.scope_group.idToggled.connect(lambda _id, on: on and self._refresh_buttons())
        layout.addWidget(scope_box)

        self.huge_label = QLabel(self)
        self.huge_label.setObjectName("bulk_huge_note")
        self.huge_label.setWordWrap(True)
        self.huge_label.setTextFormat(Qt.TextFormat.PlainText)
        theme.bind_style(self.huge_label, "color: $warn;")
        self.huge_label.setVisible(False)
        layout.addWidget(self.huge_label)
        self.unavailable_label = QLabel(self)
        self.unavailable_label.setObjectName("bulk_unavailable")
        self.unavailable_label.setWordWrap(True)
        self.unavailable_label.setTextFormat(Qt.TextFormat.PlainText)
        theme.bind_style(self.unavailable_label, "color: $warn;")
        self.unavailable_label.setVisible(False)
        layout.addWidget(self.unavailable_label)

        run = QHBoxLayout()
        self.preview_button = QPushButton(t("gui.bulk.preview"), self)
        self.preview_button.setObjectName("bulk_preview")
        self.preview_button.setToolTip(t("gui.bulk.preview.tip"))
        self.preview_button.setDefault(True)
        self.preview_button.clicked.connect(self.preview)
        run.addWidget(self.preview_button)
        self.stop_button = QPushButton(t("gui.bulk.stop"), self)
        self.stop_button.setObjectName("bulk_stop")
        self.stop_button.setAutoDefault(False)
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setVisible(False)
        run.addWidget(self.stop_button)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setVisible(False)
        run.addWidget(self.progress_bar, 1)
        layout.addLayout(run)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("bulk_status")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)

        result = QGroupBox(t("gui.bulk.result.title"), self)
        result_layout = QVBoxLayout(result)
        self.counts_label = QLabel(result)
        self.counts_label.setObjectName("bulk_counts")
        self.counts_label.setWordWrap(True)
        self.counts_label.setTextFormat(Qt.TextFormat.PlainText)
        self.counts_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        result_layout.addWidget(self.counts_label)
        result_layout.addWidget(QLabel(t("gui.bulk.examples"), result))
        self.examples_table = self._table(result, ("record", "path", "before", "after"))
        self.examples_table.setObjectName("bulk_examples")
        result_layout.addWidget(self.examples_table, 2)
        self.problems_title = QLabel(t("gui.bulk.problems"), result)
        result_layout.addWidget(self.problems_title)
        self.problems_table = self._table(result, ("record", "path", "value", "reason"))
        self.problems_table.setObjectName("bulk_problems")
        result_layout.addWidget(self.problems_table, 1)
        self.warnings_label = QLabel(result)
        self.warnings_label.setObjectName("bulk_warnings")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setTextFormat(Qt.TextFormat.PlainText)
        theme.bind_style(self.warnings_label, "color: $warn;")
        result_layout.addWidget(self.warnings_label)
        layout.addWidget(result, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.apply_button = QPushButton(t("gui.bulk.apply"), self)
        self.apply_button.setObjectName("bulk_apply")
        self.apply_button.setToolTip(t("gui.bulk.apply.tip"))
        self.apply_button.setAutoDefault(False)
        self.apply_button.clicked.connect(self.apply)
        buttons.addWidget(self.apply_button)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setObjectName("bulk_close")
        self.close_button.setToolTip(t("gui.bulk.close.tip"))
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.kind_combo.currentIndexChanged.connect(self._on_kind)
        self._clear_results()
        self._show_problems(False)

    def _table(self, parent: QWidget, columns: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(columns), parent)
        table.setHorizontalHeaderLabels([t(f"gui.bulk.col.{c}") for c in columns])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setWordWrap(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        for i in range(2, len(columns)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        table.setToolTip(t("gui.bulk.example.reveal.tip"))
        table.cellDoubleClicked.connect(lambda row, _col, tb=table: self._reveal(tb, row))
        return table

    def _line(self, kind: str, name: str, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit(self)
        edit.setObjectName(f"{kind}_{name}")
        edit.setMaxLength(1 << 30)
        if placeholder:
            edit.setPlaceholderText(placeholder)
        edit.textChanged.connect(self._changed)
        self.fields.setdefault(kind, {})[name] = edit
        return edit

    def _check(self, kind: str, name: str, text: str) -> QCheckBox:
        box = QCheckBox(text, self)
        box.setObjectName(f"{kind}_{name}")
        box.toggled.connect(self._changed)
        self.fields.setdefault(kind, {})[name] = box
        return box

    def _combo(self, kind: str, name: str, items: list[tuple[str, str]]) -> QComboBox:
        combo = QComboBox(self)
        combo.setObjectName(f"{kind}_{name}")
        for text, data in items:
            combo.addItem(text, data)
        combo.currentIndexChanged.connect(self._changed)
        self.fields.setdefault(kind, {})[name] = combo
        return combo

    def _page(self, kind: str) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        optional = t("gui.bulk.param.path_optional")
        required = t("gui.bulk.param.path_required")
        if kind == "replace":
            form.addRow(t("gui.bulk.param.find"), self._line(kind, "find"))
            form.addRow(t("gui.bulk.param.replacement"), self._line(kind, "replacement"))
            opts = QHBoxLayout()
            opts.addWidget(self._check(kind, "regex", t("gui.bulk.param.regex")))
            opts.addWidget(self._check(kind, "case_sensitive", t("gui.bulk.param.case_sensitive")))
            opts.addWidget(self._check(kind, "whole_word", t("gui.bulk.param.whole_word")))
            opts.addStretch(1)
            form.addRow("", opts)
            form.addRow(t("gui.bulk.param.path"), self._line(kind, "path", optional))
        elif kind == "rename_key":
            form.addRow(t("gui.bulk.param.old"), self._line(kind, "old"))
            form.addRow(t("gui.bulk.param.new"), self._line(kind, "new"))
            form.addRow(t("gui.bulk.param.path"), self._line(kind, "path", t("gui.bulk.param.objects_path")))
        elif kind == "delete_field":
            form.addRow(t("gui.bulk.param.path"), self._line(kind, "path", required))
        elif kind == "set_field":
            form.addRow(t("gui.bulk.param.path"), self._line(kind, "path", required))
            form.addRow(t("gui.bulk.param.value"), self._line(kind, "value", t("gui.bulk.param.value.placeholder")))
            form.addRow(t("gui.bulk.param.mode"),
                        self._combo(kind, "mode", [(t(f"bulk.label.mode.{m}"), m) for m in _SET_MODES]))
        elif kind == "clean_strings":
            trim = self._check(kind, "trim", t("gui.bulk.param.trim"))
            trim.setChecked(True)
            form.addRow("", trim)
            collapse = self._check(kind, "collapse_spaces", t("gui.bulk.param.collapse"))
            collapse.setChecked(True)
            form.addRow("", collapse)
            form.addRow(t("gui.bulk.param.path"), self._line(kind, "path", optional))
        elif kind == "convert_type":
            form.addRow(t("gui.bulk.param.path"), self._line(kind, "path", required))
            form.addRow(t("gui.bulk.param.to"),
                        self._combo(kind, "to", [(t(f"gui.editors.type.{k}"), k) for k in _TYPES]))
        elif kind == "dedupe":
            form.addRow(t("gui.bulk.param.key"), self._line(kind, "key", t("gui.bulk.param.key.placeholder")))
            form.addRow(t("gui.bulk.param.keep"),
                        self._combo(kind, "keep", [(t(f"bulk.label.keep.{k}"), k) for k in ("first", "last")]))
        return page

    # ----------------------------------------------------------------------------------- the spec
    def kind(self) -> str:
        return str(self.kind_combo.currentData())

    def set_kind(self, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown bulk operation: {kind!r}")
        self.kind_combo.setCurrentIndex(KINDS.index(kind))

    def _params(self, kind: str) -> dict[str, Any]:
        f = self.fields[kind]

        def text(name: str) -> str:
            return f[name].text()               # type: ignore[attr-defined]

        def path(name: str = "path") -> str | None:
            value = f[name].text().strip()      # type: ignore[attr-defined]
            return value or None

        def on(name: str) -> bool:
            return bool(f[name].isChecked())    # type: ignore[attr-defined]

        def data(name: str) -> str:
            return str(f[name].currentData())   # type: ignore[attr-defined]

        if kind == "replace":
            return {"find": text("find"), "replacement": text("replacement"),
                    "mode": "regex" if on("regex") else "text", "case_sensitive": on("case_sensitive"),
                    "whole_word": on("whole_word"), "path": path()}
        if kind == "rename_key":
            return {"old": text("old"), "new": text("new"), "path": path()}
        if kind == "delete_field":
            return {"path": text("path").strip()}
        if kind == "set_field":
            return {"path": text("path").strip(), "value": text("value"), "mode": data("mode")}
        if kind == "clean_strings":
            return {"trim": on("trim"), "collapse_spaces": on("collapse_spaces"), "path": path()}
        if kind == "convert_type":
            return {"path": text("path").strip(), "to": data("to")}
        return {"key": text("key").strip(), "keep": data("keep")}

    def spec(self) -> BulkSpec:
        """The operation as the form shows it (not validated)."""
        kind = self.kind()
        return BulkSpec(kind, self._params(kind))

    def set_spec(self, spec: BulkSpec) -> None:
        """Show ``spec`` in the form (unknown parameters are ignored)."""
        kind = spec.kind
        if kind not in KINDS:
            raise ValueError(f"unknown bulk operation: {kind!r}")
        self.set_kind(kind)
        f = self.fields[kind]
        params = dict(spec.params or {})
        for name, value in params.items():
            if name == "mode" and kind == "replace":
                f["regex"].setChecked(value == "regex")      # type: ignore[attr-defined]
                continue
            widget = f.get(name)
            if isinstance(widget, QLineEdit):
                widget.setText("" if value is None else str(value))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
        self._validate()

    def spec_error(self) -> JsonReshapeError | None:
        """The validation error of the form (``None`` = valid)."""
        return self._spec_error

    def _on_kind(self, _index: int = 0) -> None:
        self.pages.setCurrentIndex(max(0, self.kind_combo.currentIndex()))
        self._validate()

    def _changed(self, *_args: Any) -> None:
        self._validate_timer.start()

    def _validate(self) -> None:
        self._validate_timer.stop()
        spec = self.spec()
        error: JsonReshapeError | None = None
        try:
            spec.validate()
        except JsonReshapeError as exc:
            error = exc
        except Exception as exc:  # pragma: no cover - a validation bug must not break the dialog
            log.exception("bulk spec validation failed")
            error = UserInputError("error.generic", detail=str(exc))
        self._spec_error = error
        param = error.params.get("param") if error is not None else None
        for kind, fields in self.fields.items():
            for name, widget in fields.items():
                bad = kind == spec.kind and name == param
                theme.bind_style(widget, "border: 1px solid $danger;" if bad and isinstance(widget, QLineEdit) else "")
        if error is None:
            self.error_label.setVisible(False)
            self.summary_label.setText(spec.label())
        else:
            message = error.message
            hint = error.hint
            self.error_label.setText(f"{message}\n{hint}" if hint else message)
            self.error_label.setVisible(True)
            self.summary_label.setText("")
        self._refresh_buttons()

    # ------------------------------------------------------------------------------------- scope
    def scope_choice(self) -> str:
        for scope, button in self.scope_buttons.items():
            if button.isChecked():
                return scope
        return "all"

    def set_scope_choice(self, scope: str) -> None:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope!r}")
        self.scope_buttons[scope].setChecked(True)

    def set_selection_source(self, selection: Callable[[], Any] | None) -> None:
        """The callback giving the active view's selection (module docstring)."""
        self._selection = selection
        self.refresh_scope()

    def _source(self) -> Callable[[], Any] | None:
        """The selection callback: the one given, else the parent window's active view (when it has one)."""
        if self._selection is not None:
            return self._selection
        window = self.parentWidget()
        if window is not None and callable(getattr(window, "active_view", None)):
            return self._default_selection
        return None

    def _default_selection(self) -> Any:
        """The selection of the parent window's active view when it shows THIS session: its
        ``bulk_selection()`` when it has one, else the table's selected rows (``grid.selected_ranges()``) or
        the records holding the tree's selected nodes (``selected_keypaths()``; top keys are positions)."""
        window = self.parentWidget()
        active = getattr(window, "active_view", None)
        view = active() if callable(active) else None
        if view is None or getattr(view, "session", None) is not self.session:
            return None
        fn = getattr(view, "bulk_selection", None)
        if callable(fn):
            return fn()
        kind = getattr(view, "view_id", "")
        if kind == "table":
            grid = getattr(view, "grid", None)
            return SelectionScope.for_view_rows(self.session, grid.selected_ranges() if grid is not None else ())
        if kind == "tree" and callable(getattr(view, "selected_keypaths", None)):
            kps = view.selected_keypaths() or []
            positions = sorted({int(kp[0]) for kp in kps if kp and isinstance(kp[0], int)})
            if positions:
                return SelectionScope(positions=positions, gen=_shown_gen(self.session))
        return None

    def _selection_scope(self) -> Any:
        fn = self._source()
        if fn is None:
            return None
        try:
            scope = fn()
        except JsonReshapeError as exc:
            log.info("selection unavailable: %s", exc)
            return None
        except Exception:
            log.exception("the selection callback failed")
            return None
        if scope is None:
            return None
        if getattr(scope, "gen", None) is None and not isinstance(scope, SelectionScope):
            scope = SelectionScope(positions=scope, gen=_shown_gen(self.session))
        return scope

    def refresh_scope(self, *_args: Any) -> None:
        """Update the scope labels (counts, the selection)."""
        if self._closing or not _alive(self):
            return
        s = self.session
        doc = getattr(s, "doc", None)
        total = 0
        if doc is not None:
            try:
                total = int(doc.record_count)
            except Exception:
                total = 0
        self.scope_buttons["all"].setText(t("gui.bulk.scope.all", count=_count(total)))
        if bool(getattr(s, "view_is_base", True)):
            self.scope_buttons["view"].setText(t("gui.bulk.scope.view_base"))
        else:
            try:
                n = int(s.view_len())
            except Exception:
                n = 0
            self.scope_buttons["view"].setText(t("gui.bulk.scope.view", count=_count(n)))
        button = self.scope_buttons["selected"]
        if self._source() is None:
            button.setText(t("gui.bulk.scope.selected_none"))
            button.setEnabled(False)
        else:
            button.setEnabled(True)
            scope = self._selection_scope()
            try:
                n = len(scope) if scope is not None else 0
            except TypeError:
                n = 0
            button.setText(t("gui.bulk.scope.selected", count=_count(n)) if n
                           else t("gui.bulk.scope.selected_none"))
        if not button.isEnabled() and button.isChecked():
            self.scope_buttons["all"].setChecked(True)
        self._refresh_buttons()

    def _preview_scope(self) -> tuple[Any, tuple, Any]:
        """``(scope, record sets to lease, pinned document)`` of the chosen scope (``UserInputError`` when the
        scope cannot be used now)."""
        s = self.session
        choice = self.scope_choice()
        pinned = None
        snap = getattr(s, "snapshot", None)
        if choice == "view":
            if bool(getattr(s, "view_busy", False)):
                raise UserInputError("gui.bulk.scope.view_busy")
            if callable(snap):
                pinned, view = snap()
            else:  # pragma: no cover - sessions without snapshot()
                view = s.view_recordset()
            if view is None or bool(getattr(s, "view_is_base", False)):
                return None, (), pinned
            return view, (view,), pinned
        if callable(snap):
            pinned = snap()[0]
        if choice == "selected":
            scope = self._selection_scope()
            if scope is None or not len(scope):
                raise UserInputError("gui.bulk.scope.no_selection")
            # the record sets the scope reads in the task are leased until it ended
            sets = tuple(rs for rs in (scope, getattr(scope, "source", None), getattr(scope, "positions", None))
                         if isinstance(rs, RecordSet))
            return scope, sets, pinned
        return None, (), pinned

    # ---------------------------------------------------------------------------------- edit state
    def _bind_edit(self, *_args: Any) -> None:
        edit = getattr(self.session, "edit", None)
        if edit is self._edit:
            self._refresh_state()
            return
        old = self._edit
        if old is not None and _alive(old) and not hasattr(self.session, "edit_state_changed"):
            try:
                old.state_changed.disconnect(self._refresh_state)
            except (RuntimeError, TypeError):
                pass
        self._edit = edit
        if edit is not None and not hasattr(self.session, "edit_state_changed"):
            signal = getattr(edit, "state_changed", None)
            if signal is not None:
                try:
                    signal.connect(self._refresh_state)
                except (RuntimeError, TypeError):  # pragma: no cover
                    pass
        self._refresh_state()

    def _availability(self) -> str:
        """``""`` when bulk operations can run now, else the localized reason (the controller's
        ``reason_text``)."""
        edit = self._edit
        if edit is None:
            return t("gui.bulk.unavailable")
        if bool(getattr(edit, "available", False)):
            return ""
        text = getattr(edit, "reason_text", None)
        if callable(text):
            try:
                text = text()
            except Exception:  # pragma: no cover
                text = None
        if isinstance(text, str) and text.strip():
            return text
        return t("gui.bulk.unavailable")

    def _current_gen(self) -> int | None:
        edit = self._edit
        gen = getattr(edit, "applied_gen", None) if edit is not None else None
        return None if gen is None else int(gen)

    def _refresh_state(self, *_args: Any) -> None:
        """Editing availability, staleness of the preview shown, the buttons."""
        if self._closing or not _alive(self):
            return
        self._check_stale()
        reason = self._availability()
        self.unavailable_label.setText(reason)
        self.unavailable_label.setVisible(bool(reason))
        self._refresh_buttons()

    def _check_stale(self) -> None:
        pv = self.preview_result
        gen = self._current_gen()
        if pv is None or gen is None or self._stale:
            return
        if self._handed is not None and self._handed[0] is pv:
            return                               # being applied: the controller owns it
        if int(pv.gen) < gen:
            self._stale = True                   # gens only grow: this preview can never be applied
            _discard(pv)
            self._set_status(t("gui.bulk.status.stale"), "warning")

    def can_apply(self) -> bool:
        """Apply is possible now: a fresh, non-empty preview, editing available and idle."""
        pv = self.preview_result
        edit = self._edit
        if pv is None or edit is None or self._stale or self._handed is not None or self.task is not None:
            return False
        if bool(getattr(pv, "discarded", False)) or pv.empty:
            return False
        if not bool(getattr(edit, "available", False)) or bool(getattr(edit, "busy", False)):
            return False
        return self._current_gen() == int(pv.gen)

    def _can_preview(self) -> bool:
        edit = self._edit
        if self._spec_error is not None or edit is None or self.task is not None or self._handed is not None:
            return False
        if not bool(getattr(edit, "available", False)) or bool(getattr(edit, "busy", False)):
            return False
        if self.scope_choice() == "selected" and self._source() is None:
            return False
        return True

    def _refresh_buttons(self) -> None:
        running = self.task is not None
        self.preview_button.setEnabled(self._can_preview())
        self.stop_button.setVisible(running)
        self.progress_bar.setVisible(running)
        self.apply_button.setEnabled(self.can_apply())
        self.kind_combo.setEnabled(not running)
        self.pages.setEnabled(not running)

    def _set_status(self, text: str, kind: str = "info") -> None:
        role = {"error": "danger", "warning": "warn", "ok": "ok"}.get(kind)
        theme.bind_style(self.status_label, f"color: ${role};" if role else "")
        self.status_label.setText(text or "")

    # ---------------------------------------------------------------------------------- preview
    def preview(self) -> Any:
        """Start the preview (a heavy task of the controller); returns the task or ``None`` when it could not
        start (the reason is shown)."""
        if self.task is not None or self._closing:
            return None
        self._validate()
        if self._spec_error is not None:
            return None
        edit = self._edit
        reason = self._availability()
        if reason:
            self._set_status(reason, "warning")
            return None
        if bool(getattr(edit, "busy", False)):
            self._set_status(t("gui.bulk.status.busy"), "warning")
            return None
        try:
            scope, sets, pinned = self._preview_scope()
        except JsonReshapeError as exc:
            self._set_status(exc.message, "warning")
            return None
        spec = self.spec()
        self._drop_preview()
        self._clear_results()
        token = object()
        self._token = token
        ref = weakref.ref(self)
        starting = [True]

        def on_done(pv: Any) -> None:
            dlg = ref()
            if dlg is None or not _alive(dlg) or dlg._closing or dlg._token is not token:
                _discard(pv)                     # nobody will apply it
                return
            dlg._preview_done(pv)

        def on_failed(exc: BaseException | None) -> None:
            if starting[0]:
                return                           # refused while starting: handled below (task is None)
            dlg = ref()
            if dlg is not None and _alive(dlg) and not dlg._closing and dlg._token is token:
                dlg._preview_failed(exc)

        try:
            task = edit.bulk_preview(spec, scope, on_done, on_failed=on_failed)
        except JsonReshapeError as exc:
            self._token = None
            self._set_status(t("gui.bulk.status.failed", error=exc.message), "error")
            return None
        finally:
            starting[0] = False
        if self._token is not token:            # already delivered (a synchronous controller)
            return task
        if task is None:                         # refused (the controller tells why in the status bar)
            self._token = None
            self._set_status(self._refusal_text(scope), "warning")
            self._refresh_buttons()
            return None
        self.task = task
        keep = getattr(self.session, "keep_document_for", None)
        if callable(keep):
            try:
                keep(task, sets, doc=pinned)
            except TypeError:  # pragma: no cover - an older session
                keep(task, sets)
        self._remember(spec)
        self._set_status(t("gui.bulk.status.running"))
        self.progress_bar.setRange(0, 0)
        self._poll.start()
        self._refresh_buttons()
        return task

    def _refusal_text(self, scope: Any) -> str:
        """Why the controller did not start a preview (it said so in the status bar too)."""
        reason = self._availability()
        if reason:
            return reason
        gen = getattr(scope, "gen", None)
        if isinstance(scope, RecordSet) and gen is not None and gen != self._current_gen():
            exc = UserInputError("bulk.error.stale_scope")
            return f"{exc.message}\n{exc.hint}" if exc.hint else exc.message
        return t("gui.bulk.status.busy")

    def stop(self) -> None:
        """Cancel the running preview (its result, if any, is discarded)."""
        task = self.task
        if task is not None and getattr(task, "state", "running") == "running":
            task.cancel()

    def _preview_failed(self, exc: BaseException | None) -> None:
        """The controller's ``on_failed``: ``None`` / ``Cancelled`` = stopped, else the error (also reported by
        the controller)."""
        self._token = None
        self._poll.stop()
        self.task = None
        if exc is None or isinstance(exc, Cancelled):
            self._set_status(t("gui.bulk.status.cancelled"), "warning")
            self.preview_failed.emit(exc if exc is not None else Cancelled())
        else:
            text = exc.message if isinstance(exc, JsonReshapeError) else t("gui.widgets.error.unexpected")
            hint = exc.hint if isinstance(exc, JsonReshapeError) else None
            self._set_status(t("gui.bulk.status.failed", error=text) + (f"\n{hint}" if hint else ""), "error")
            self.preview_failed.emit(exc)
        self._refresh_buttons()

    def _poll_task(self) -> None:
        task = self.task
        if task is None:
            self._poll.stop()
            return
        state = getattr(task, "state", "running")
        if state == "running":
            if getattr(task, "queued", False):
                self.progress_bar.setRange(0, 0)
                self._set_status(t("gui.tasks.queued"))
                return
            info = task.info() if hasattr(task, "info") else None
            if info is None or info.fraction is None:
                self.progress_bar.setRange(0, 0)
            else:
                self.progress_bar.setRange(0, 1000)
                self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))
                if info.message:
                    self._set_status(info.message)
            return
        # the task ended: its callbacks ran before this poll (fallback for a controller that did not call them)
        self._poll.stop()
        if self._token is not None and state in ("failed", "cancelled"):
            self._preview_failed(getattr(task, "error", None) if state == "failed" else None)
            return
        if self._token is None:
            self.task = None
        self._refresh_buttons()

    def _preview_done(self, pv: BulkPreview) -> None:
        self._token = None
        self._poll.stop()
        self.task = None
        self._stale = False
        self.preview_result = pv
        self._show_results(pv)
        self._set_status(t("gui.bulk.status.done", label=pv.spec.label()), "ok")
        if pv.empty:
            _discard(pv)                         # nothing to apply (no files are left anyway)
        self._refresh_state()
        self.preview_finished.emit(pv)

    def _drop_preview(self) -> None:
        """Forget the preview shown; discard it unless it was handed to the controller."""
        pv = self.preview_result
        self.preview_result = None
        self._stale = False
        if pv is not None and (self._handed is None or self._handed[0] is not pv):
            _discard(pv)

    # ---------------------------------------------------------------------------------- results
    def _clear_results(self) -> None:
        self.counts_label.setText("")
        self.examples_table.setRowCount(0)
        self.problems_table.setRowCount(0)
        self.warnings_label.setText("")
        self._show_problems(False)

    def _show_problems(self, on: bool) -> None:
        self.problems_title.setVisible(on)
        self.problems_table.setVisible(on)

    def _fill(self, table: QTableWidget, rows: list[tuple[str, ...]], positions: list[int], paths: list[str]) -> None:
        from jsonreshape.gui import copyfmt

        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                item = QTableWidgetItem(copyfmt.escape_surrogates(text))
                item.setToolTip(copyfmt.escape_surrogates(text))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, positions[r])
                    item.setData(Qt.ItemDataRole.UserRole + 1, paths[r])
                table.setItem(r, c, item)

    def _show_results(self, pv: BulkPreview) -> None:
        if pv.spec.kind == "dedupe":
            counts = t("gui.bulk.result.counts_dedupe", scanned=_count(pv.scanned), records=_count(pv.records_changed))
        else:
            counts = t("gui.bulk.result.counts", scanned=_count(pv.scanned), records=_count(pv.records_changed),
                       values=_count(pv.values_changed))
        extra = []
        if pv.conflicts:
            extra.append(t("gui.bulk.result.conflicts", count=_count(pv.conflicts)))
        if pv.failures:
            extra.append(t("gui.bulk.result.failures", count=_count(pv.failures)))
        if pv.skipped_huge:
            extra.append(t("gui.bulk.result.huge", count=_count(pv.skipped_huge)))
        lines = [" · ".join([counts, *extra])]
        lines.append(t("gui.bulk.result.detail", size=format_bytes(pv.bytes_new), elapsed=format_duration(pv.elapsed)))
        if pv.empty:
            lines.append(t("gui.bulk.result.nothing"))
        self.counts_label.setText("\n".join(lines))
        self._fill(self.examples_table, [(f"#{e.position}", e.path, e.before, e.after) for e in pv.examples],
                   [e.position for e in pv.examples], [e.path for e in pv.examples])
        self._fill(self.problems_table, [(f"#{e.position}", e.path, e.before, e.after) for e in pv.problems],
                   [e.position for e in pv.problems], [e.path for e in pv.problems])
        self._show_problems(bool(pv.problems))
        self.warnings_label.setText("\n".join(pv.warnings or []))

    def _reveal(self, table: QTableWidget, row: int) -> None:
        """Double-click on an example / problem: select its record in the views (fresh previews only)."""
        pv = self.preview_result
        item = table.item(row, 0)
        if item is None or pv is None or self._stale or self._current_gen() != int(pv.gen):
            return
        position = item.data(Qt.ItemDataRole.UserRole)
        if position is None:
            return
        from jsonreshape.gui.session import NodeRef

        path: tuple = ()
        text = item.data(Qt.ItemDataRole.UserRole + 1)
        if pv.spec.kind != "dedupe" and isinstance(text, str) and text not in ("", "$"):
            try:
                from jsonreshape.paths import parse_path

                path = tuple(parse_path(text))
            except Exception:
                path = ()
        try:
            self.session.select(NodeRef(int(position), path), reveal=True)
        except Exception:
            log.debug("could not reveal #%s", position, exc_info=True)

    # ---------------------------------------------------------------------------------- apply
    def apply(self) -> bool:
        """Apply the previewed result (one undoable step); False when that is not possible now."""
        if not self.can_apply():
            self._refresh_buttons()
            return False
        pv = self.preview_result
        edit = self._edit
        assert pv is not None and edit is not None
        self._handed = (pv, int(self._current_gen() or 0))
        self._handed_label = pv.spec.label()
        self._set_status(t("gui.bulk.status.applying"))
        self._refresh_buttons()
        ref = weakref.ref(self)
        starting = [True]
        early: list[Any] = []

        def done(cs: Any) -> None:
            if starting[0]:
                early.append(cs)                 # answered while bulk_apply runs: handled below
                return
            _apply_done(ref, pv, cs)

        try:
            accepted = bool(edit.bulk_apply(pv, on_done=done))
        except JsonReshapeError as exc:
            log.warning("bulk apply refused: %s", exc)
            accepted = False
        finally:
            starting[0] = False
        if not accepted:                         # refused before anything happened (the controller said why)
            self._handed = None
            self._set_status(t("gui.bulk.status.not_applied"), "error")
            self._refresh_state()                # a preview that became stale is discarded here
            return False
        if early:                                # a controller that answered at once
            _apply_done(ref, pv, early[0])
        self._refresh_buttons()
        return True

    def _apply_finished(self, pv: BulkPreview, cs: Any) -> None:
        if self._handed is not None and self._handed[0] is pv:
            self._handed = None
        if self.preview_result is pv:
            self.preview_result = None           # applied, or gone with a failed commit
        if cs is None:
            self._set_status(t("gui.bulk.status.not_applied"), "error")
        else:
            self._set_status(t("gui.bulk.status.applied", label=self._handed_label), "ok")
            self.applied.emit(cs)
        self._refresh_state()

    def _on_data_changed(self, cs: Any) -> None:
        if self._closing or not _alive(self):
            return
        self._refresh_state()
        self.refresh_scope()

    # ---------------------------------------------------------------------------------- misc
    def _on_session_state(self, state: str) -> None:
        if state == "closed" and not self._closing and _alive(self):
            self.reject()

    def _remember(self, spec: BulkSpec) -> None:
        state = getattr(self.app, "state", None)
        if state is None:
            return
        try:
            data = state.get(STATE_KEY) or {}
            params = dict(data.get("params") or {}) if isinstance(data, dict) else {}
            params[spec.kind] = dict(spec.params)
            state.set(STATE_KEY, {"kind": spec.kind, "params": params})
        except (TypeError, ValueError, AttributeError):
            log.debug("could not remember the bulk operation", exc_info=True)
            return
        save = getattr(self.app, "save_state", None)
        if callable(save):
            save()

    def _restore_last(self) -> None:
        state = getattr(self.app, "state", None)
        try:
            data = state.get(STATE_KEY) if state is not None else None
        except Exception:  # pragma: no cover
            data = None
        if not isinstance(data, dict):
            self._on_kind()
            return
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        for kind, values in params.items():
            if kind in KINDS and isinstance(values, dict):
                try:
                    self.set_spec(BulkSpec(kind, dict(values)))
                except Exception:
                    log.debug("ignoring remembered bulk params of %s", kind, exc_info=True)
        kind = data.get("kind")
        if kind in KINDS:
            self.set_kind(kind)
        self._on_kind()

    def _start_huge_check(self) -> None:
        doc = getattr(self.session, "doc", None)
        if doc is None:
            return
        try:
            limit = int(doc.settings.large_node_bytes)
            size = int(doc.data_size)
        except Exception:
            return
        if size <= limit:
            self._huge = False
            return
        ref = weakref.ref(self)

        def done(found: bool) -> None:
            dlg = ref()
            if dlg is not None and _alive(dlg) and not dlg._closing:
                dlg._show_huge(bool(found), limit)

        def failed(exc: BaseException) -> None:
            log.debug("huge-record scan failed: %r", exc)
            done(True)

        self._huge_task = self.app.tasks.run(lambda ctx: _has_huge_records(doc, limit, ctx.cancel),
                                             title=t("gui.bulk.task.huge"), kind="scan", owner=self, silent=True,
                                             on_done=done, on_error=failed)

    def _show_huge(self, found: bool, limit: int) -> None:
        self._huge = found
        self.huge_label.setText(t("gui.bulk.huge_note", limit=format_bytes(limit)) if found else "")
        self.huge_label.setVisible(found)

    @property
    def huge_records(self) -> bool | None:
        """The document has records larger than ``large_node_bytes`` (``None``: not known yet)."""
        return self._huge

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt name
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.refresh_scope()                 # the user may have selected other rows meanwhile

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        """Close: cancel a running preview and discard the preview that was not applied."""
        if not self._closing:
            self._closing = True
            self._poll.stop()
            self._validate_timer.stop()
            task = self.task
            if task is not None and getattr(task, "state", "running") == "running":
                task.cancel()                    # its late result is discarded by on_done
            self.task = None
            self._token = None
            self._drop_preview()
            try:
                self.app.tasks.detach(self)
            except Exception:  # pragma: no cover
                log.debug("detach failed", exc_info=True)
            for signal, slot in self._connections:
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            self._connections = []
            try:
                if _OPEN.get(self.session) is self:
                    del _OPEN[self.session]
            except (TypeError, KeyError):
                pass
        super().done(result)


def open_bulk_dialog(session: Any, *, parent: QWidget | None = None, scope_default: str | None = None,
                     selection: Callable[[], Any] | None = None, spec: BulkSpec | None = None,
                     preview: bool = False) -> BulkDialog:
    """Show the bulk operations dialog of ``session`` (one per session; :meth:`BulkDialog.show_for`)."""
    return BulkDialog.show_for(session, parent=parent, scope_default=scope_default, selection=selection, spec=spec,
                               preview=preview)
