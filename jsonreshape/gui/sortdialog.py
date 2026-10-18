"""SortDialog: multi-key sort of the records (docs/ARCHITECTURE.md §11.8; AGENT_PROMPT §6.4).

One row per :class:`jsonreshape.sortkeys.SortKey`: field (editable combo with a completer over
the schema's fields, ``$key`` for member records, ``$index``), direction, null / missing
placement, natural order (``child2 < child10``, on by default), collation (``uz`` -- the
official Uzbek alphabet, the default -- ``simple`` or ``unicode``) and case sensitivity.
Rows can be added, removed and reordered (the first row is the main key). The dialog is
prefilled from ``session.sort_keys``; **Tartiblash** validates every row (a bad path is shown
under its row) and calls ``session.set_sort(keys)``; **Tartibni olib tashlash** calls
``session.set_sort(None)``. Rows without a field are ignored. Works with ``open()``
(non-modal) and ``exec()``.

The field suggestions come from the document's schema, asked for when the dialog is shown
or a field list is opened (never for a single-value document); a dialog opened while the
file is still being indexed gets them when indexing ends.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import JsonReshapeError
from jsonreshape.gui import fields, theme
from jsonreshape.i18n import t
from jsonreshape.sortkeys import SortKey

log = logging.getLogger(__name__)

__all__ = ["SortDialog"]

_COLLATIONS = ("uz", "simple", "unicode")
(_C_NUM, _C_FIELD, _C_DIR, _C_NULLS, _C_NATURAL, _C_COLL, _C_CASE, _C_UP, _C_DOWN, _C_REMOVE) = range(10)


class _FieldCombo(QComboBox):
    """The editable field combo: opening its list asks for the schema's fields."""

    def __init__(self, dialog: SortDialog, parent: QWidget) -> None:
        super().__init__(parent)
        self._dialog = dialog

    def showPopup(self) -> None:  # noqa: N802 (Qt name)
        self._dialog._request_fields(force=True)
        super().showPopup()


class _SortRow:
    """The widgets of one sort key (laid out in the dialog's grid)."""

    def __init__(self, dialog: SortDialog) -> None:
        parent = dialog.rows_widget
        self.number = QLabel(parent)
        self.field = _FieldCombo(dialog, parent)
        self.field.setEditable(True)
        self.field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.field.setModel(dialog.field_model)
        self.field.setMaxVisibleItems(20)
        self.field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.field.setMinimumContentsLength(18)
        completer = QCompleter(dialog.field_model, self.field)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.field.setCompleter(completer)
        self.field.setCurrentIndex(-1)
        self.field.lineEdit().setPlaceholderText(t("gui.sort.field.placeholder"))
        self.field.setToolTip(t("gui.sort.field_tip"))
        self.direction = QComboBox(parent)
        self.direction.addItem(t("gui.sort.asc"), False)
        self.direction.addItem(t("gui.sort.desc"), True)
        self.nulls = QComboBox(parent)
        self.nulls.addItem(t("gui.sort.nulls.last"), "last")
        self.nulls.addItem(t("gui.sort.nulls.first"), "first")
        self.nulls.setToolTip(t("gui.sort.nulls_tip"))
        self.natural = QCheckBox(t("gui.sort.natural"), parent)
        self.natural.setChecked(True)
        self.natural.setToolTip(t("gui.sort.natural_tip"))
        self.collation = QComboBox(parent)
        for name in _COLLATIONS:
            self.collation.addItem(t("gui.sort.collation." + name), name)
        self.collation.setToolTip(t("gui.sort.collation_tip"))
        self.case = QCheckBox(t("gui.sort.case"), parent)
        self.case.setToolTip(t("gui.sort.case_tip"))
        self.up = self._tool(parent, "\N{UPWARDS ARROW}", t("gui.sort.up"), lambda: dialog.move_row(self, -1))
        self.down = self._tool(parent, "\N{DOWNWARDS ARROW}", t("gui.sort.down"), lambda: dialog.move_row(self, 1))
        self.remove = self._tool(parent, "\N{MULTIPLICATION X}", t("gui.sort.remove"), lambda: dialog.remove_row(self))
        self.error = QLabel(parent)
        self.error.setObjectName("sortError")
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.hide()
        self.field.editTextChanged.connect(lambda _text: self.set_error(None))
        self.field.lineEdit().textEdited.connect(lambda _text: dialog._request_fields(force=True))

    @staticmethod
    def _tool(parent: QWidget, text: str, tip: str, slot: Any) -> QToolButton:
        b = QToolButton(parent)
        b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(slot)
        return b

    def widgets(self) -> list[QWidget]:
        return [self.number, self.field, self.direction, self.nulls, self.natural, self.collation, self.case,
                self.up, self.down, self.remove, self.error]

    # -- values ----------------------------------------------------------------------------
    def set_key(self, key: SortKey) -> None:
        self.field.setEditText(key.path)
        self.direction.setCurrentIndex(1 if key.descending else 0)
        self.nulls.setCurrentIndex(max(0, self.nulls.findData(key.nulls)))
        self.natural.setChecked(bool(key.natural))
        self.collation.setCurrentIndex(max(0, self.collation.findData(key.collation)))
        self.case.setChecked(bool(key.case_sensitive))

    def field_text(self) -> str:
        return self.field.currentText().strip()

    def key(self) -> SortKey:
        """The SortKey of this row (``PathSyntaxError`` / ``UserInputError`` when invalid)."""
        return SortKey(self.field_text(), descending=bool(self.direction.currentData()),
                       nulls=str(self.nulls.currentData()), natural=self.natural.isChecked(),
                       collation=str(self.collation.currentData()), case_sensitive=self.case.isChecked())

    def set_error(self, text: str | None) -> None:
        if text:
            self.error.setText(text)
            self.error.show()
        else:
            self.error.clear()
            self.error.hide()

    def error_text(self) -> str | None:
        return self.error.text() if not self.error.isHidden() else None

    def dispose(self) -> None:
        for w in self.widgets():
            w.blockSignals(True)
            w.hide()
            w.deleteLater()


class SortDialog(QDialog):
    """Sort keys of a session's view (see the module docstring)."""

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._rows: list[_SortRow] = []
        self._done = False
        self.setWindowTitle(t("gui.sort.title"))
        self.field_model = QStringListModel(self)
        lay = QVBoxLayout(self)
        self.info = QLabel(t("gui.sort.info"), self)
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.notice = QLabel(t("gui.sort.single"), self)
        self.notice.setWordWrap(True)
        self.notice.hide()
        lay.addWidget(self.notice)
        self.rows_widget = QWidget(self)
        self.grid = QGridLayout(self.rows_widget)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(2)
        heads = {_C_FIELD: "gui.sort.col.field", _C_DIR: "gui.sort.col.direction", _C_NULLS: "gui.sort.col.nulls",
                 _C_NATURAL: "gui.sort.col.natural", _C_COLL: "gui.sort.col.collation", _C_CASE: "gui.sort.col.case"}
        self._heads = []
        for col, key in heads.items():
            label = QLabel(t(key), self.rows_widget)
            f = label.font()
            f.setBold(True)
            label.setFont(f)
            self.grid.addWidget(label, 0, col)
            self._heads.append(label)
        self.grid.setColumnStretch(_C_FIELD, 1)
        lay.addWidget(self.rows_widget)
        self.add_button = QPushButton(t("gui.sort.add"), self)
        self.add_button.clicked.connect(lambda: self.add_row(focus=True))
        lay.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)
        self.buttons = QDialogButtonBox(self)
        self.ok_button = self.buttons.addButton(t("gui.sort.ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok_button.setDefault(True)
        self.clear_button = self.buttons.addButton(t("gui.sort.clear"), QDialogButtonBox.ButtonRole.ResetRole)
        self.clear_button.setToolTip(t("gui.sort.clear_tip"))
        self.cancel_button = self.buttons.addButton(t("gui.common.cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.clear_button.clicked.connect(self.clear_sort)
        lay.addWidget(self.buttons)
        self._style()
        for key in list(getattr(session, "sort_keys", []) or []):
            self.add_row(key)
        if not self._rows:
            self.add_row()
        self._update_state()
        self._fields_asked = False
        session.index_finished.connect(self._on_index_finished)
        self.resize(max(760, self.sizeHint().width()), self.sizeHint().height())

    def _style(self) -> None:
        danger = QColor(theme.color("danger"))
        warn = QColor(theme.color("warn"))
        muted = QColor(theme.color("muted"))
        self.rows_widget.setStyleSheet(f"QLabel#sortError {{ color: {danger.name()}; }}")
        pal = self.notice.palette()
        pal.setColor(QPalette.ColorRole.WindowText, warn)
        self.notice.setPalette(pal)
        pal = self.info.palette()
        pal.setColor(QPalette.ColorRole.WindowText, muted)
        self.info.setPalette(pal)

    # -- rows --------------------------------------------------------------------------------
    def rows(self) -> list[_SortRow]:
        return list(self._rows)

    def add_row(self, key: SortKey | None = None, *, focus: bool = False) -> _SortRow:
        row = _SortRow(self)
        if key is not None:
            row.set_key(key)
        self._rows.append(row)
        self._relayout()
        if focus:
            row.field.setFocus(Qt.FocusReason.OtherFocusReason)
        return row

    def remove_row(self, row: _SortRow) -> None:
        if row not in self._rows:
            return
        self._rows.remove(row)
        for w in row.widgets():
            self.grid.removeWidget(w)
        row.dispose()
        if not self._rows:
            self.add_row()
        else:
            self._relayout()

    def move_row(self, row: _SortRow, delta: int) -> None:
        """Move a key up (``-1``, more important) or down (``+1``)."""
        i = self._rows.index(row)
        j = i + delta
        if 0 <= j < len(self._rows):
            self._rows[i], self._rows[j] = self._rows[j], self._rows[i]
            self._relayout()

    def _relayout(self) -> None:
        for row in self._rows:
            for w in row.widgets():
                self.grid.removeWidget(w)
        n = len(self._rows)
        for i, row in enumerate(self._rows):
            r = 1 + 2 * i
            row.number.setText(f"{i + 1}.")
            for col, w in ((_C_NUM, row.number), (_C_FIELD, row.field), (_C_DIR, row.direction),
                           (_C_NULLS, row.nulls), (_C_NATURAL, row.natural), (_C_COLL, row.collation),
                           (_C_CASE, row.case), (_C_UP, row.up), (_C_DOWN, row.down), (_C_REMOVE, row.remove)):
                self.grid.addWidget(w, r, col)
                w.show()
            self.grid.addWidget(row.error, r + 1, _C_FIELD, 1, _C_REMOVE - _C_FIELD + 1)
            row.error.setVisible(bool(row.error.text()))
            row.up.setEnabled(i > 0)
            row.down.setEnabled(i < n - 1)
        # keep the tab order in row order
        prev: QWidget | None = None
        for row in self._rows:
            for w in (row.field, row.direction, row.nulls, row.natural, row.collation, row.case):
                if prev is not None:
                    QWidget.setTabOrder(prev, w)
                prev = w

    # -- fields -----------------------------------------------------------------------------
    def _request_fields(self, *, force: bool = False) -> None:
        """Ask for the fields when they are needed: the dialog is shown (or a field list
        is opened: ``force``); not for a single-value document, not before indexing ended."""
        s = self.session
        if self._done or self._single() or (not force and not self.isVisible()):
            return
        if getattr(s, "doc", None) is None or getattr(s, "state", "") != "ready":
            return
        if self._fields_asked and self.field_names():
            return
        self._fields_asked = True
        schema = fields.get_schema(s, self._fields_ready)
        if schema is not None:
            self._fields_ready(schema)

    def _on_index_finished(self) -> None:
        """Opened while the file was being indexed: the suggestions can come now."""
        if not self._done:
            self._fields_asked = False
            self._update_state()
            self._request_fields()

    def _fields_ready(self, schema: Any) -> None:
        if self._done:
            return
        names = fields.field_paths(schema)
        if "$index" not in names:
            names.append("$index")
        texts = [r.field.currentText() for r in self._rows]
        for r in self._rows:
            r.field.blockSignals(True)
        try:
            self.field_model.setStringList(names)
            for r, text in zip(self._rows, texts):
                r.field.setCurrentIndex(-1)
                r.field.setEditText(text)
        finally:
            for r in self._rows:
                r.field.blockSignals(False)

    def field_names(self) -> list[str]:
        return list(self.field_model.stringList())

    # -- state / result ---------------------------------------------------------------------------
    def _single(self) -> bool:
        doc = getattr(self.session, "doc", None)
        return doc is not None and doc.kind == "single"

    def _update_state(self) -> None:
        single = self._single()
        self.notice.setVisible(single)
        for w in (self.ok_button, self.clear_button, self.add_button, self.rows_widget):
            w.setEnabled(not single)

    def keys(self) -> list[SortKey] | None:
        """The sort keys of the non-empty rows in order, or ``None`` when a row is invalid
        (its error is shown under it)."""
        out: list[SortKey] = []
        ok = True
        for row in self._rows:
            if not row.field_text():
                row.set_error(None)
                continue
            try:
                out.append(row.key())
            except JsonReshapeError as exc:
                row.set_error(exc.message)
                ok = False
            else:
                row.set_error(None)
        return out if ok else None

    def accept(self) -> None:
        """Validate and sort (no rows with a field: back to the file order)."""
        if self._single():
            return
        keys = self.keys()
        if keys is None:
            for row in self._rows:
                if row.error_text():
                    row.field.setFocus(Qt.FocusReason.OtherFocusReason)
                    break
            return
        try:
            self.session.set_sort(keys or None)
        except JsonReshapeError as exc:
            self.session.app.report_error(exc, self)
            return
        super().accept()

    def clear_sort(self) -> None:
        """Back to the file order and close."""
        if self._single():
            return
        try:
            self.session.set_sort(None)
        except JsonReshapeError as exc:
            self.session.app.report_error(exc, self)
            return
        super().accept()

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        self._done = False
        super().showEvent(event)
        self._request_fields()

    def done(self, result: int) -> None:  # noqa: D401 (Qt name)
        self._done = True
        try:
            self.session.index_finished.disconnect(self._on_index_finished)
        except (RuntimeError, TypeError):
            pass
        super().done(result)
