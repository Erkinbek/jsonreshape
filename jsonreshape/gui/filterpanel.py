"""FilterPanel: the filter dock (docs/ARCHITECTURE.md §11.8; AGENT_PROMPT §6.3, §14 test 18).

* **Visual builder** -- nested groups (VA / YOKI, EMAS, add condition / group, remove) of
  condition rows: field (editable combo with a completer over the schema's fields, ``$key``
  for member records, ``$index``), operator (every :data:`jsonreshape.query.BUILDER_OPS`
  with :func:`~jsonreshape.query.op_label`) and 0, 1 or 2 value editors
  (:func:`~jsonreshape.query.op_arity`). A value is JSON when it parses as JSON (``12``,
  ``"12"``, ``true``, ``null``, ``[1,2]``), otherwise a string; a badge shows the type it
  was read as, a toggle forces a string (:func:`interpret_value`). ``in`` takes a JSON
  list or comma separated items; ``regex`` always takes a string. Rows without a field or
  without a value are incomplete and ignored. A builder error highlights its row
  (``QuerySyntaxError.params["item"]``).
* **Expression line** -- language (expression / JMESPath) and the text with a live,
  debounced syntax check: the character at ``QuerySyntaxError.pos`` is underlined and the
  message shown under it. Editing the builder rewrites the text (``Group.to_text()``);
  editing the text rebuilds the builder when :func:`jsonreshape.query.builder_from_text`
  can express it, otherwise the builder is read-only with a notice. The text is always the
  filter that is applied.
* **Qo'llash** (Enter / Ctrl+Enter) -> ``session.set_filter(FilterSpec(text, lang,
  builder))``, **Tozalash** -> ``set_filter(None)``; a status line (running / result count
  / the filter report's warnings); **presets** (load / save / delete,
  :class:`jsonreshape.query.FilterPresets`) -- the presets file is read and written (under
  its inter-process lock) in silent background tasks; the preset buttons wait meanwhile.

The field list of the builder comes from the document's schema, asked for only when it is
needed: while the panel is visible or when a field list / completer is opened -- never for
single-value documents (a hidden panel computes nothing).

One editor (builder + expression) per session is kept while the panel is bound to other
tabs (:meth:`FilterPanel.set_session`), so a tab switch never loses a draft. The panel is
disabled with a hint for single-value documents and when no file is open.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPalette, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec
from jsonreshape.errors import JsonReshapeError, QuerySyntaxError, UserInputError
from jsonreshape.gui import fields, theme
from jsonreshape.gui.session import FilterSpec
from jsonreshape.gui.widgets import confirm, format_count, mono_font
from jsonreshape.i18n import t
from jsonreshape.query import (
    BUILDER_MAX_DEPTH,
    BUILDER_OPS,
    Condition,
    Filter,
    FilterPresets,
    Group,
    builder_from_text,
    compile_filter,
    op_arity,
    op_label,
)

log = logging.getLogger(__name__)

__all__ = ["FilterPanel", "display_value", "interpret_value", "TEXT_DEBOUNCE_MS"]

#: The expression is checked (and the builder rebuilt) this long after the last keystroke.
TEXT_DEBOUNCE_MS = 250
_NO = object()   # "no value" marker


# ==================================================================================
# value texts
# ==================================================================================
def _json(text: str) -> Any:
    try:
        return codec.loads(text)
    except (JsonReshapeError, ValueError, TypeError, RecursionError):
        return _NO


def interpret_value(raw: str, op: str, force_string: bool = False) -> tuple[bool, Any]:
    """The JSON value a builder value editor stands for: ``(complete, value)``.

    * ``regex``: the text as a string (a JSON string literal is decoded);
    * ``in``: a JSON list / object / string as written; another JSON scalar becomes a
      one-item list; any other text is split at commas, each item JSON when it parses as a
      scalar, else a string (``force_string``: every item a string);
    * every other operator: JSON when the text parses as JSON, else the text itself;
      ``force_string``: always the text.

    An empty text is incomplete, except a forced string (the empty string ``""``).
    """
    if op == "in":
        text = raw.strip()
        if not text:
            return False, None
        if not force_string:
            parsed = _json(text)
            if parsed is not _NO:
                return True, parsed if isinstance(parsed, (list, dict, str)) else [parsed]
        items = [p.strip() for p in text.split(",")]
        items = [p for p in items if p]
        if force_string:
            return True, items
        out = []
        for item in items:
            v = _json(item)
            out.append(item if v is _NO or isinstance(v, (list, dict)) else v)
        return True, out
    if force_string:
        return True, raw
    if not raw.strip():
        return False, None
    parsed = _json(raw)
    if op == "regex":
        return True, parsed if isinstance(parsed, str) else raw
    return True, raw if parsed is _NO else parsed


def display_value(value: Any, op: str) -> str:
    """The editor text for ``value``: a plain string when :func:`interpret_value` reads it
    back as the same string, else its JSON text."""
    if isinstance(value, str):
        ok, back = interpret_value(value, op)
        if ok and isinstance(back, str) and back == value:
            return value
    try:
        return codec.dumps(value).decode("utf-8")
    except (TypeError, ValueError):
        return str(value)


def _json_type(value: Any) -> str:
    try:
        return codec.json_type(value)
    except TypeError:
        return "string"


def _spec_key(spec: FilterSpec | None) -> tuple[str, str]:
    if spec is None or spec.empty:
        return ("", "expr")
    try:
        text = spec.expr_text()
    except JsonReshapeError:
        text = spec.text
    return ((text or "").strip(), spec.lang)


def _has_conditions(group: Group) -> bool:
    return any(isinstance(x, Condition) or _has_conditions(x) for x in group.items)


def _rgba(c: QColor) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


# ==================================================================================
# builder widgets
# ==================================================================================
class _ValueEdit(QWidget):
    """One value: text, type badge, "always a string" toggle."""

    changed = Signal()
    submitted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._op = "eq"
        self._exact: Any = _NO      # the exact value given by set_value(), until edited
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(t("gui.filter.value.placeholder"))
        self.edit.setMinimumWidth(80)
        self.edit.textEdited.connect(self._edited)
        self.edit.returnPressed.connect(self.submitted.emit)
        lay.addWidget(self.edit, 1)
        self.badge = QLabel(self)
        self.badge.setObjectName("filterValueBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setMinimumWidth(30)
        lay.addWidget(self.badge)
        self.force = QToolButton(self)
        self.force.setText('""')
        self.force.setCheckable(True)
        self.force.setAutoRaise(True)
        self.force.setToolTip(t("gui.filter.value.force_string"))
        self.force.toggled.connect(self._edited)
        lay.addWidget(self.force)
        self._refresh()

    def set_op(self, op: str) -> None:
        """Another operator: the shown text is read again for it (an exact value given by
        :meth:`set_value` belonged to the old operator: ``5`` is ``[5]`` for ``in``)."""
        if op != self._op:
            self._exact = _NO
        self._op = op
        self.force.setVisible(op != "regex")
        self._refresh()

    def set_value(self, value: Any) -> None:
        """Show ``value`` exactly (kept as it is until the user edits the text)."""
        self._exact = value
        self.edit.setText(display_value(value, self._op))
        self.force.blockSignals(True)
        self.force.setChecked(False)
        self.force.blockSignals(False)
        self._refresh()

    def set_raw(self, raw: str, force_string: bool = False) -> None:
        """Set the text as if the user typed it."""
        self.edit.setText(raw)
        self.force.blockSignals(True)
        self.force.setChecked(bool(force_string))
        self.force.blockSignals(False)
        self._edited()

    def raw(self) -> str:
        return self.edit.text()

    def value(self) -> tuple[bool, Any]:
        """``(complete, value)``."""
        if self._exact is not _NO:
            return True, self._exact
        return interpret_value(self.edit.text(), self._op, self.force.isChecked() and self._op != "regex")

    def type_name(self) -> str | None:
        """The JSON type the text was read as (``None`` while incomplete)."""
        ok, value = self.value()
        return _json_type(value) if ok else None

    def _edited(self, *_args: Any) -> None:
        self._exact = _NO
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        ok, value = self.value()
        if not ok:
            self.badge.setText("")
            self.badge.setToolTip("")
            self.badge.setStyleSheet("")
            return
        kind = _json_type(value)
        self.badge.setText(theme.badge_glyph(kind))
        try:
            shown = codec.dumps(value).decode("utf-8")
        except (TypeError, ValueError):
            shown = str(value)
        if len(shown) > 200:
            shown = shown[:199] + "\N{HORIZONTAL ELLIPSIS}"
        self.badge.setToolTip(t("gui.filter.value.read_as", type=t("schema.type." + kind), value=shown))
        self.badge.setStyleSheet(f"QLabel#filterValueBadge {{ color: {_rgba(theme.badge_color(kind))};"
                                 f" background: {_rgba(theme.badge_background(kind))}; border-radius: 3px;"
                                 f" padding: 0px 3px; }}")


class _FieldCombo(QComboBox):
    """The editable field combo: opening its list asks for the schema's fields."""

    def __init__(self, editor: _Editor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editor = editor

    def showPopup(self) -> None:  # noqa: N802 (Qt name)
        self._editor.fields_wanted(force=True)
        super().showPopup()


class _ConditionRow(QFrame):
    """field | operator | value [va value2] | remove."""

    changed = Signal()
    remove_requested = Signal(object)
    submitted = Signal()

    def __init__(self, editor: _Editor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self.setObjectName("filterCondition")
        self.setProperty("error", False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)
        self.field = _FieldCombo(editor, self)
        self.field.setEditable(True)
        self.field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.field.setModel(editor.field_model)
        self.field.setMaxVisibleItems(20)
        self.field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.field.setMinimumContentsLength(14)
        completer = QCompleter(editor.field_model, self.field)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.field.setCompleter(completer)
        self.field.setCurrentIndex(-1)
        self.field.lineEdit().setPlaceholderText(t("gui.filter.field.placeholder"))
        self.field.setToolTip(t("gui.filter.field.tip"))
        self.field.editTextChanged.connect(lambda _text: self.changed.emit())
        # typing uses the completer: it needs the field list
        self.field.lineEdit().textEdited.connect(lambda _text: editor.fields_wanted(force=True))
        lay.addWidget(self.field, 3)
        self.op = QComboBox(self)
        for op in BUILDER_OPS:
            self.op.addItem(op_label(op), op)
        self.op.currentIndexChanged.connect(self._op_changed)
        lay.addWidget(self.op, 0)
        self.v1 = _ValueEdit(self)
        self.and_label = QLabel(t("gui.filter.between_and"), self)
        self.v2 = _ValueEdit(self)
        for v in (self.v1, self.v2):
            v.changed.connect(self.changed.emit)
            v.submitted.connect(self.submitted.emit)
        lay.addWidget(self.v1, 3)
        lay.addWidget(self.and_label)
        lay.addWidget(self.v2, 3)
        self.remove_button = QToolButton(self)
        self.remove_button.setText("\N{MULTIPLICATION X}")
        self.remove_button.setAutoRaise(True)
        self.remove_button.setToolTip(t("gui.filter.remove_condition"))
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        lay.addWidget(self.remove_button)
        self._op_changed()

    # -- state ---------------------------------------------------------------------------
    def set_condition(self, field: str, op: str = "eq", value: Any = _NO, value2: Any = _NO) -> None:
        self.blockSignals(True)
        try:
            self.field.setEditText(str(field))
            self.set_op(op)
            if value is not _NO and op_arity(op) >= 1:
                self.v1.set_value(value)
            if value2 is not _NO and op_arity(op) == 2:
                self.v2.set_value(value2)
        finally:
            self.blockSignals(False)

    def set_op(self, op: str) -> None:
        idx = self.op.findData(op)
        if idx < 0:
            raise ValueError(f"unknown builder operator: {op!r}")
        self.op.setCurrentIndex(idx)

    def op_name(self) -> str:
        return str(self.op.currentData())

    def field_text(self) -> str:
        return self.field.currentText().strip()

    def condition(self) -> Condition | None:
        """The condition, or ``None`` while the row is incomplete."""
        field = self.field_text()
        if not field:
            return None
        op = self.op_name()
        n = op_arity(op)
        if n == 0:
            return Condition(field, op)
        ok1, v1 = self.v1.value()
        if not ok1:
            return None
        if n == 1:
            return Condition(field, op, v1)
        ok2, v2 = self.v2.value()
        if not ok2:
            return None
        return Condition(field, op, v1, v2)

    def set_error(self, on: bool) -> None:
        if bool(self.property("error")) == on:
            return
        self.setProperty("error", on)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def has_error(self) -> bool:
        return bool(self.property("error"))

    def focus_input(self) -> None:
        """Focus the first thing to fill: the field when empty, else the value."""
        if not self.field_text():
            self.field.setFocus(Qt.FocusReason.OtherFocusReason)
        elif op_arity(self.op_name()) >= 1:
            self.v1.edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _op_changed(self, *_args: Any) -> None:
        op = self.op_name()
        n = op_arity(op)
        self.v1.set_op(op)
        self.v2.set_op(op)
        self.v1.setVisible(n >= 1)
        self.and_label.setVisible(n == 2)
        self.v2.setVisible(n == 2)
        self.changed.emit()


class _GroupWidget(QFrame):
    """An AND/OR group (optionally negated) of condition rows and sub-groups."""

    changed = Signal()
    remove_requested = Signal(object)
    submitted = Signal()

    def __init__(self, editor: _Editor, depth: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self.depth = depth
        self.setObjectName("filterGroup")
        self.children_: list[QWidget] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(4)
        self.op = QComboBox(self)
        self.op.addItem(t("gui.filter.group.and"), "and")
        self.op.addItem(t("gui.filter.group.or"), "or")
        self.op.setToolTip(t("gui.filter.group.op_tip"))
        self.op.currentIndexChanged.connect(lambda _i: self.changed.emit())
        head.addWidget(self.op)
        self.negate = QCheckBox(t("gui.filter.group.not"), self)
        self.negate.setToolTip(t("gui.filter.group.not_tip"))
        self.negate.toggled.connect(lambda _on: self.changed.emit())
        head.addWidget(self.negate)
        self.add_condition_button = QPushButton(t("gui.filter.add_condition"), self)
        self.add_condition_button.setFlat(True)
        self.add_condition_button.clicked.connect(lambda: self.add_condition(focus=True))
        head.addWidget(self.add_condition_button)
        self.add_group_button = QPushButton(t("gui.filter.add_group"), self)
        self.add_group_button.setFlat(True)
        self.add_group_button.setEnabled(depth + 1 < BUILDER_MAX_DEPTH)
        self.add_group_button.clicked.connect(lambda: self.add_group())
        head.addWidget(self.add_group_button)
        head.addStretch(1)
        self.remove_button = QToolButton(self)
        self.remove_button.setText("\N{MULTIPLICATION X}")
        self.remove_button.setAutoRaise(True)
        self.remove_button.setToolTip(t("gui.filter.remove_group"))
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        self.remove_button.setVisible(depth > 0)
        head.addWidget(self.remove_button)
        lay.addLayout(head)
        self.body_frame = QFrame(self)
        self.body_frame.setObjectName("filterGroupBody")
        self.body = QVBoxLayout(self.body_frame)
        self.body.setContentsMargins(12, 0, 0, 0)
        self.body.setSpacing(2)
        self.empty_hint = QLabel(t("gui.filter.group.empty"), self.body_frame)
        self.empty_hint.setObjectName("filterHint")
        self.body.addWidget(self.empty_hint)
        lay.addWidget(self.body_frame)

    # -- items -----------------------------------------------------------------------------
    def items(self) -> list[QWidget]:
        return list(self.children_)

    def _add(self, w: QWidget) -> None:
        self.children_.append(w)
        self.body.addWidget(w)
        self.empty_hint.setVisible(False)
        w.changed.connect(self.changed.emit)
        w.remove_requested.connect(self.remove_item)
        w.submitted.connect(self.submitted.emit)

    def add_condition(self, field: str = "", op: str = "eq", value: Any = _NO, value2: Any = _NO, *,
                      focus: bool = False) -> _ConditionRow:
        row = _ConditionRow(self.editor, self.body_frame)
        row.set_condition(field, op, value, value2)
        self._add(row)
        self.editor.fields_wanted()
        self.changed.emit()
        if focus:
            row.focus_input()
        return row

    def add_group(self, op: str = "and", negate: bool = False) -> _GroupWidget:
        if self.depth + 1 >= BUILDER_MAX_DEPTH:
            raise ValueError("builder groups nested too deeply")
        grp = _GroupWidget(self.editor, self.depth + 1, self.body_frame)
        grp.set_op(op)
        grp.negate.setChecked(bool(negate))
        self._add(grp)
        self.changed.emit()
        return grp

    @staticmethod
    def _discard(w: QWidget) -> None:
        # silenced first: until deleteLater runs, its combo still shares the field model
        w.blockSignals(True)
        w.hide()
        w.deleteLater()

    def remove_item(self, w: QWidget) -> None:
        if w not in self.children_:
            return
        self.children_.remove(w)
        self.body.removeWidget(w)
        self._discard(w)
        self.empty_hint.setVisible(not self.children_)
        self.changed.emit()

    def clear(self) -> None:
        for w in list(self.children_):
            self.body.removeWidget(w)
            self._discard(w)
        self.children_.clear()
        self.empty_hint.setVisible(True)

    def set_op(self, op: str) -> None:
        idx = self.op.findData(str(op).lower())
        self.op.setCurrentIndex(max(0, idx))

    def load(self, group: Group) -> None:
        """Show ``group`` (signals blocked: the caller syncs)."""
        self.blockSignals(True)
        try:
            self.set_op(group.op)
            self.negate.setChecked(bool(group.negate))
            self.clear()
            for item in group.items:
                if isinstance(item, Group):
                    if self.depth + 1 >= BUILDER_MAX_DEPTH:
                        continue
                    sub = _GroupWidget(self.editor, self.depth + 1, self.body_frame)
                    sub.load(item)
                    self._add(sub)
                else:
                    row = _ConditionRow(self.editor, self.body_frame)
                    row.set_condition(item.field, item.op, item.value, item.value2)
                    self._add(row)
        finally:
            self.blockSignals(False)

    def model(self, path: tuple = ()) -> tuple[Group, dict[tuple, QWidget]]:
        """The builder tree (incomplete rows left out) and index path -> widget."""
        items: list[Condition | Group] = []
        mapping: dict[tuple, QWidget] = {path: self}
        for w in self.children_:
            if isinstance(w, _ConditionRow):
                cond = w.condition()
                if cond is None:
                    continue
                mapping[path + (len(items),)] = w
                items.append(cond)
            elif isinstance(w, _GroupWidget):
                sub, sub_map = w.model(path + (len(items),))
                items.append(sub)
                mapping.update(sub_map)
        return Group(str(self.op.currentData()), items, self.negate.isChecked()), mapping

    def rows(self) -> list[_ConditionRow]:
        """Every condition row below this group (pre-order)."""
        out: list[_ConditionRow] = []
        for w in self.children_:
            if isinstance(w, _ConditionRow):
                out.append(w)
            elif isinstance(w, _GroupWidget):
                out.extend(w.rows())
        return out

    def groups(self) -> list[_GroupWidget]:
        out: list[_GroupWidget] = []
        for w in self.children_:
            if isinstance(w, _GroupWidget):
                out.append(w)
                out.extend(w.groups())
        return out


class _ExprEdit(QPlainTextEdit):
    """The expression text; Enter / Ctrl+Enter apply (Shift+Enter: new line); the error
    character is underlined."""

    submitted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._error_pos: int | None = None

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        fm = self.fontMetrics()
        return QSize(super().sizeHint().width(), fm.lineSpacing() * 3 + 12)

    def error_pos(self) -> int | None:
        return self._error_pos

    def set_error_pos(self, pos: int | None) -> None:
        self._error_pos = pos
        if pos is None:
            self.setExtraSelections([])
            return
        doc_len = len(self.toPlainText())
        sel = QTextEdit.ExtraSelection()
        fmt = QTextCharFormat()
        fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
        fmt.setUnderlineColor(theme.color("danger"))
        bg = QColor(theme.color("danger"))
        bg.setAlpha(40)
        fmt.setBackground(bg)
        sel.format = fmt
        cursor = QTextCursor(self.document())
        start = max(0, min(pos, doc_len))
        if start >= doc_len and doc_len > 0:
            start = doc_len - 1   # the end of the text: mark the last character
        cursor.setPosition(start)
        if doc_len:
            cursor.setPosition(min(doc_len, start + 1), QTextCursor.MoveMode.KeepAnchor)
        sel.cursor = cursor
        self.setExtraSelections([sel])


# ==================================================================================
# one editor per session
# ==================================================================================
class _Editor(QWidget):
    """Builder + expression of one session (a page of the panel's stack)."""

    changed = Signal()          # anything edited / synced (status refresh)
    submitted = Signal()        # Enter in the expression or a value
    session_closed = Signal(object)

    def __init__(self, panel: FilterPanel, session: Any) -> None:
        super().__init__(panel)
        self.panel = panel
        self.session = session
        self.field_model = QStringListModel(self)
        self.fields_doc: int | None = None    # id(doc) the field list belongs to
        self.dirty = False
        self.applied_key: tuple[str, str] | None = None   # content applied from this draft last
        self.state = "sync"          # sync | unrepresentable | invalid | jmespath
        self._syncing = False
        self._builder_text = ""
        self._error: QuerySyntaxError | None = None
        self._builder_error: str | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TEXT_DEBOUNCE_MS)
        self._timer.timeout.connect(self._sync_from_text)
        self._build_ui()
        self._apply_style()
        session.state_changed.connect(self._session_state)

    # -- UI --------------------------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        title = QLabel(t("gui.filter.builder"), self)
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)
        self.notice = QLabel(self)
        self.notice.setObjectName("filterNotice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        lay.addWidget(self.notice)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.StyledPanel)
        holder = QWidget(self.scroll)
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(4, 4, 4, 4)
        self.root = _GroupWidget(self, 0, holder)
        hl.addWidget(self.root)
        hl.addStretch(1)
        self.scroll.setWidget(holder)
        lay.addWidget(self.scroll, 3)
        self.builder_error = QLabel(self)
        self.builder_error.setObjectName("filterError")
        self.builder_error.setWordWrap(True)
        self.builder_error.hide()
        lay.addWidget(self.builder_error)

        row = QHBoxLayout()
        label = QLabel(t("gui.filter.expression"), self)
        f = label.font()
        f.setBold(True)
        label.setFont(f)
        row.addWidget(label)
        row.addStretch(1)
        self.lang = QComboBox(self)
        self.lang.addItem(t("gui.filter.lang.expr"), "expr")
        self.lang.addItem(t("gui.filter.lang.jmespath"), "jmespath")
        self.lang.setToolTip(t("gui.filter.lang.tip"))
        self.lang.currentIndexChanged.connect(self._lang_changed)
        row.addWidget(self.lang)
        lay.addLayout(row)
        self.expr = _ExprEdit(self)
        self.expr.setFont(mono_font(self.session))
        self.expr.textChanged.connect(self._text_changed)
        self.expr.submitted.connect(self.submitted.emit)
        lay.addWidget(self.expr, 1)
        self.error_label = QLabel(self)
        self.error_label.setObjectName("filterError")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.hide()
        lay.addWidget(self.error_label)
        self.hint_label = QLabel(self)
        self.hint_label.setObjectName("filterWarn")
        self.hint_label.setWordWrap(True)
        self.hint_label.setTextFormat(Qt.TextFormat.PlainText)
        self.hint_label.hide()
        lay.addWidget(self.hint_label)
        self.root.changed.connect(self._builder_changed)
        self.root.submitted.connect(self.submitted.emit)
        self._update_placeholder()

    def _apply_style(self) -> None:
        danger, warn, line, muted = (_rgba(theme.color(r)) for r in ("danger", "warn", "line", "muted"))
        self.setStyleSheet(
            f"QFrame#filterCondition[error=\"true\"] {{ border: 1px solid {danger}; border-radius: 3px; }}"
            f" QFrame#filterGroupBody {{ border-left: 2px solid {line}; }}"
            f" QLabel#filterError {{ color: {danger}; }}"
            f" QLabel#filterWarn, QLabel#filterNotice {{ color: {warn}; }}"
            f" QLabel#filterHint {{ color: {muted}; }}")

    def theme_changed(self) -> None:
        self._apply_style()
        self.expr.set_error_pos(self.expr.error_pos())

    def _update_placeholder(self) -> None:
        key = "gui.filter.placeholder.jmespath" if self.lang_name() == "jmespath" else "gui.filter.placeholder.expr"
        self.expr.setPlaceholderText(t(key))

    # -- accessors -------------------------------------------------------------------------
    def text(self) -> str:
        return self.expr.toPlainText()

    def lang_name(self) -> str:
        return str(self.lang.currentData() or "expr")

    def content_key(self) -> tuple[str, str]:
        text = self.text().strip()
        return ("", "expr") if not text else (text, self.lang_name())

    def builder_available(self) -> bool:
        return self.state == "sync"

    def builder_group(self) -> Group | None:
        """The builder's tree (``None`` while it is read-only)."""
        if self.state != "sync":
            return None
        return self.root.model()[0]

    def builder_dict(self) -> dict | None:
        """``Group.to_dict()`` for :class:`FilterSpec` when the builder shows the filter."""
        if self.state != "sync" or self.lang_name() != "expr" or self._builder_error:
            return None
        group = self.root.model()[0]
        return group.to_dict() if _has_conditions(group) else None

    def error(self) -> QuerySyntaxError | None:
        return self._error

    def builder_error_text(self) -> str | None:
        return self._builder_error

    def error_rows(self) -> list[_ConditionRow]:
        return [r for r in self.root.rows() if r.has_error()]

    # -- field list ------------------------------------------------------------------------
    def set_fields(self, names: list[str]) -> None:
        rows = self.root.rows()
        texts = [(r, r.field.currentText()) for r in rows]
        for r, _ in texts:
            r.blockSignals(True)
            r.field.blockSignals(True)
        self._syncing = True
        try:
            self.field_model.setStringList(list(names))
            for r, text in texts:
                r.field.setCurrentIndex(-1)
                r.field.setEditText(text)
        finally:
            self._syncing = False
            for r, _ in texts:
                r.field.blockSignals(False)
                r.blockSignals(False)

    def field_names(self) -> list[str]:
        return list(self.field_model.stringList())

    def fields_wanted(self, force: bool = False) -> None:
        """The field list is needed (``force``: a field list / completer is being opened,
        even while the panel is hidden)."""
        self.panel._request_fields(self, force=force)

    # -- programmatic changes ----------------------------------------------------------------
    def set_text(self, text: str, lang: str = "expr") -> None:
        """Replace the expression as if the user typed it (the builder is rebuilt from it
        at once; the draft counts as edited)."""
        self._syncing = True
        try:
            idx = self.lang.findData(lang)
            self.lang.setCurrentIndex(max(0, idx))
            self.expr.setPlainText(text)
        finally:
            self._syncing = False
        self._update_placeholder()
        self._timer.stop()
        self._sync_from_text()
        self.dirty = True
        self.changed.emit()

    def load_spec(self, spec: FilterSpec | None) -> None:
        """Show the session's filter (not dirty afterwards)."""
        group: Group | None = None
        if spec is None or spec.empty:
            text, lang = "", "expr"
        else:
            lang = spec.lang if spec.lang in ("expr", "jmespath") else "expr"
            try:
                text = spec.expr_text()
            except JsonReshapeError:
                text = spec.text
            if lang == "expr" and spec.builder:
                try:
                    group = Group.from_dict(spec.builder)
                    group.to_text()
                except Exception:   # a stale or foreign builder: rebuild from the text
                    group = None
        if group is not None:
            self._syncing = True
            try:
                self.lang.setCurrentIndex(max(0, self.lang.findData(lang)))
                self.expr.setPlainText(text)
                self.root.load(group)
            finally:
                self._syncing = False
            self._update_placeholder()
            self._timer.stop()
            self._clear_builder_error()
            self._builder_text = group.to_text()
            self.state = "sync"
            self._validate(text, lang)
            self._update_notice()
            self.changed.emit()
        else:
            self.set_text(text, lang)
        self.dirty = False

    def reset(self) -> None:
        self.set_text("", "expr")
        self.dirty = False

    def flush(self) -> None:
        """Run a pending (debounced) text check now."""
        if self._timer.isActive():
            self._timer.stop()
            self._sync_from_text()

    def sync_with_session(self) -> None:
        """Follow a filter changed elsewhere (filter by value, reopen, show records) unless
        the user has unapplied edits."""
        s = self.session
        if getattr(s, "view_busy", False) or getattr(s, "closed", False):
            return
        key = _spec_key(s.filter)
        if key == self.content_key():
            self.dirty = False
            return
        if not self.dirty:
            self.load_spec(s.filter)

    # -- builder -> text -----------------------------------------------------------------------
    def _builder_changed(self) -> None:
        if self._syncing:
            return
        self.dirty = True
        self._sync_from_builder()
        self.changed.emit()

    def _clear_builder_error(self) -> None:
        self._builder_error = None
        self.builder_error.hide()
        for r in self.root.rows():
            r.set_error(False)

    def _sync_from_builder(self) -> None:
        group, mapping = self.root.model()
        self._clear_builder_error()
        try:
            text = group.to_text() if _has_conditions(group) else ""
        except (JsonReshapeError, ValueError, TypeError) as exc:
            self._show_builder_error(exc, mapping)
            return
        self._builder_text = text
        self.state = "sync"
        self._syncing = True
        try:
            self.lang.setCurrentIndex(max(0, self.lang.findData("expr")))
            self.expr.setPlainText(text)
        finally:
            self._syncing = False
        self._timer.stop()
        self._update_placeholder()
        if text:
            self._validate(text, "expr")
        else:
            self._set_error(None)
            self._set_hint([])
        self._update_notice()

    def _show_builder_error(self, exc: BaseException, mapping: dict[tuple, QWidget]) -> None:
        params = getattr(exc, "params", {}) or {}
        widget = None
        item = params.get("item")
        if item is not None:
            widget = mapping.get(tuple(item))
        if widget is None:   # e.g. a bad regex: find the row whose condition fails alone
            for w in mapping.values():
                if isinstance(w, _ConditionRow):
                    cond = w.condition()
                    try:
                        if cond is not None:
                            Group(items=[cond]).to_text()
                    except (JsonReshapeError, ValueError, TypeError):
                        widget = w
                        break
        if isinstance(widget, _ConditionRow):
            widget.set_error(True)
        detail = params.get("detail") or (exc.message if isinstance(exc, JsonReshapeError) else str(exc))
        self._builder_error = t("gui.filter.builder_error", detail=detail)
        self.builder_error.setText(self._builder_error)
        self.builder_error.show()

    # -- text -> builder -----------------------------------------------------------------------
    def _text_changed(self) -> None:
        if self._syncing:
            return
        self.dirty = True
        self._timer.start()
        self.changed.emit()

    def _lang_changed(self, *_args: Any) -> None:
        self._update_placeholder()
        if self._syncing:
            return
        self.dirty = True
        self._timer.stop()
        self._sync_from_text()
        self.changed.emit()

    def _load_builder(self, group: Group) -> None:
        self._syncing = True
        try:
            self.root.load(group)
        finally:
            self._syncing = False

    def _recheck_builder(self) -> None:
        """The builder error from the rows as they are (the text was not rebuilt from them)."""
        group, mapping = self.root.model()
        self._clear_builder_error()
        try:
            if _has_conditions(group):
                group.to_text()
        except (JsonReshapeError, ValueError, TypeError) as exc:
            self._show_builder_error(exc, mapping)

    def _sync_from_text(self) -> None:
        text, lang = self.text(), self.lang_name()
        if not text.strip():
            self._clear_builder_error()
            self._set_error(None)
            self._set_hint([])
            if self.state != "sync" or self.root.items():
                self._load_builder(Group())
            self._builder_text = ""
            self.state = "sync"
            self._update_notice()
            return
        flt = self._validate(text, lang)
        if flt is None:
            self._clear_builder_error()
            self.state = "invalid"
        elif lang == "jmespath":
            self._clear_builder_error()
            self.state = "jmespath"
        elif text.strip() == self._builder_text.strip():
            # the builder rows (incomplete ones too) already stand for this text: keep them
            # and their error state (a whitespace edit, or back from JMESPath / a bad text)
            self.state = "sync"
            self._recheck_builder()
        else:
            self._clear_builder_error()
            try:
                group = builder_from_text(text)
            except JsonReshapeError:   # pragma: no cover - validated above
                group = None
            if group is None:
                self.state = "unrepresentable"
            else:
                self._load_builder(group)
                self._builder_text = group.to_text()
                self.state = "sync"
        self._update_notice()

    def _validate(self, text: str, lang: str) -> Filter | None:
        try:
            flt = compile_filter(text, lang)
        except QuerySyntaxError as exc:
            self._set_error(exc)
            self._set_hint([])
            return None
        except (ValueError, TypeError) as exc:   # pragma: no cover - unknown language
            log.debug("filter check failed: %r", exc)
            return None
        self._set_error(None)
        self._set_hint(list(flt.warnings))
        return flt

    def _set_error(self, exc: QuerySyntaxError | None) -> None:
        self._error = exc
        if exc is None:
            self.expr.set_error_pos(None)
            self.error_label.hide()
            return
        self.expr.set_error_pos(exc.pos)
        detail = exc.params.get("detail") or exc.message
        self.error_label.setText(t("gui.filter.error_at", pos=exc.pos + 1, detail=detail))
        self.error_label.show()

    def _set_hint(self, warnings: list[str]) -> None:
        if warnings:
            self.hint_label.setText("\n".join(warnings))
            self.hint_label.show()
        else:
            self.hint_label.hide()

    def _update_notice(self) -> None:
        texts = {
            "unrepresentable": t("gui.filter.notice.unrepresentable"),
            "invalid": t("gui.filter.notice.invalid"),
            "jmespath": t("gui.filter.notice.jmespath"),
        }
        text = texts.get(self.state)
        if text:
            self.notice.setText(text)
            self.notice.show()
        else:
            self.notice.hide()
        self.scroll.setEnabled(self.state == "sync")

    def notice_text(self) -> str | None:
        return self.notice.text() if self.state != "sync" else None

    # -- lifetime --------------------------------------------------------------------------------
    def _session_state(self, state: str) -> None:
        if state == "closed":
            self.session_closed.emit(self)


# ==================================================================================
# the panel
# ==================================================================================
class FilterPanel(QWidget):
    """The filter dock's content (see the module docstring)."""

    filter_applied = Signal()    # set_filter() was called from the panel
    presets_changed = Signal()   # a presets file operation ended (names refreshed)

    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._session: Any = None
        self._editors: dict[int, _Editor] = {}
        self._presets = FilterPresets()
        self._message: str | None = None
        self._preset_task: Any = None
        self._preset_queue: list[tuple[Any, Any, str | None]] = []
        self._preset_owner = _TaskOwner()
        self.destroyed.connect(lambda *_a, owner=self._preset_owner, tasks=app.tasks: tasks.detach(owner))
        self._build_ui()
        self._refresh_presets()
        theme.notifier().theme_changed.connect(self._on_theme_changed)
        self._update_state()

    # -- UI --------------------------------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)
        presets = QHBoxLayout()
        presets.addWidget(QLabel(t("gui.filter.presets"), self))
        self.preset_combo = QComboBox(self)
        self.preset_combo.setMinimumWidth(140)
        self.preset_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.preset_combo.setPlaceholderText(t("gui.filter.preset.none"))
        presets.addWidget(self.preset_combo, 1)
        self.preset_load_button = QPushButton(t("gui.filter.preset.load"), self)
        self.preset_load_button.setToolTip(t("gui.filter.preset.load_tip"))
        self.preset_load_button.clicked.connect(lambda: self.load_preset())
        presets.addWidget(self.preset_load_button)
        self.preset_save_button = QPushButton(t("gui.filter.preset.save"), self)
        self.preset_save_button.setToolTip(t("gui.filter.preset.save_tip"))
        self.preset_save_button.clicked.connect(lambda: self.save_preset())
        presets.addWidget(self.preset_save_button)
        self.preset_delete_button = QPushButton(t("gui.filter.preset.delete"), self)
        self.preset_delete_button.setToolTip(t("gui.filter.preset.delete_tip"))
        self.preset_delete_button.clicked.connect(lambda: self.delete_preset())
        presets.addWidget(self.preset_delete_button)
        lay.addLayout(presets)

        self.stack = QStackedWidget(self)
        self.notice_page = QLabel(self.stack)
        self.notice_page.setWordWrap(True)
        self.notice_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.notice_page)
        lay.addWidget(self.stack, 1)

        buttons = QHBoxLayout()
        self.apply_button = QPushButton(t("gui.filter.apply"), self)
        self.apply_button.setToolTip(t("gui.filter.apply_tip"))
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.apply)
        buttons.addWidget(self.apply_button)
        self.clear_button = QPushButton(t("gui.filter.clear"), self)
        self.clear_button.setToolTip(t("gui.filter.clear_tip"))
        self.clear_button.clicked.connect(self.clear)
        buttons.addWidget(self.clear_button)
        self.load_applied_button = QPushButton(t("gui.filter.load_applied"), self)
        self.load_applied_button.setToolTip(t("gui.filter.load_applied_tip"))
        self.load_applied_button.clicked.connect(self.load_applied)
        self.load_applied_button.hide()
        buttons.addWidget(self.load_applied_button)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.status_label)
        self.warn_label = QLabel(self)
        self.warn_label.setWordWrap(True)
        self.warn_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warn_label.hide()
        lay.addWidget(self.warn_label)
        self._color_labels()
        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(self.apply)

    def _color_labels(self) -> None:
        pal = self.warn_label.palette()
        pal.setColor(QPalette.ColorRole.WindowText, QColor(theme.color("warn")))
        self.warn_label.setPalette(pal)
        pal = self.notice_page.palette()
        pal.setColor(QPalette.ColorRole.WindowText, QColor(theme.color("muted")))
        self.notice_page.setPalette(pal)

    def _on_theme_changed(self) -> None:
        self._color_labels()
        for ed in self._editors.values():
            ed.theme_changed()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(520, 480)

    # -- session binding -------------------------------------------------------------------------
    @property
    def session(self) -> Any:
        return self._session

    def current_editor(self) -> _Editor | None:
        s = self._session
        return self._editors.get(id(s)) if s is not None else None

    def set_session(self, session: Any) -> None:
        """Bind to the active tab's session (``None``: no file). Each session keeps its own
        draft (builder + expression) while other tabs are active."""
        if session is self._session:
            return
        old = self._session
        if old is not None:
            self._disconnect(old)
        self._session = session
        self._message = None
        if session is not None:
            ed = self._editors.get(id(session))
            if ed is None:
                ed = _Editor(self, session)
                ed.changed.connect(self._on_editor_changed)
                ed.submitted.connect(self.apply)
                ed.session_closed.connect(self._drop_editor)
                self._editors[id(session)] = ed
                self.stack.addWidget(ed)
                ed.load_spec(session.filter)
            else:
                ed.sync_with_session()
            session.view_busy_changed.connect(self._on_busy)
            session.view_changed.connect(self._on_view_changed)
            session.count_changed.connect(self._update_status)
            session.warnings_changed.connect(self._update_status)
            session.opened.connect(self._on_opened)
            session.index_finished.connect(self._on_index_finished)
            self._request_fields(ed)
        self._update_state()

    def _disconnect(self, s: Any) -> None:
        for signal, slot in ((s.view_busy_changed, self._on_busy), (s.view_changed, self._on_view_changed),
                             (s.count_changed, self._update_status), (s.warnings_changed, self._update_status),
                             (s.opened, self._on_opened), (s.index_finished, self._on_index_finished)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _drop_editor(self, ed: _Editor) -> None:
        for key, value in list(self._editors.items()):
            if value is ed:
                del self._editors[key]
        if ed.session is self._session:
            self._disconnect(ed.session)
            self._session = None
        self.stack.removeWidget(ed)
        ed.deleteLater()
        self._update_state()

    def _single(self) -> bool:
        s = self._session
        doc = getattr(s, "doc", None) if s is not None else None
        return doc is not None and doc.kind == "single"

    def usable(self) -> bool:
        """A file with records is bound (the panel is enabled)."""
        return self._session is not None and not self._single() and not getattr(self._session, "closed", False)

    def _update_state(self) -> None:
        ed = self.current_editor()
        usable = self.usable() and ed is not None
        if self._session is None:
            self.notice_page.setText(t("gui.filter.no_file"))
            self.stack.setCurrentWidget(self.notice_page)
        elif self._single():
            self.notice_page.setText(t("gui.filter.single"))
            self.stack.setCurrentWidget(self.notice_page)
        elif ed is not None:
            self.stack.setCurrentWidget(ed)
        for w in (self.apply_button, self.clear_button):
            w.setEnabled(usable)
        self._update_preset_buttons()
        self._update_status()

    def _update_preset_buttons(self) -> None:
        enabled = self.usable() and self.current_editor() is not None and not self.presets_busy()
        for w in (self.preset_combo, self.preset_load_button, self.preset_save_button, self.preset_delete_button):
            w.setEnabled(enabled)

    def _on_busy(self, busy: bool) -> None:
        if not busy:
            ed = self.current_editor()
            if ed is not None:
                ed.sync_with_session()
        self._update_status()

    def _on_view_changed(self) -> None:
        s = self._session
        if s is not None and not s.view_busy:
            ed = self.current_editor()
            if ed is not None:
                ed.sync_with_session()
        self._update_status()

    def _on_opened(self) -> None:
        """The document (re)opened: a reopen may have cleared the filter -- shown unless the
        draft has unapplied edits."""
        s = self._session
        ed = self.current_editor()
        if s is not None and ed is not None and not getattr(s, "view_busy", False):
            if ed.dirty and ed.content_key() == ed.applied_key and _spec_key(s.filter) != ed.applied_key:
                ed.dirty = False     # the draft was applied as it is; the reopen dropped that filter
            ed.sync_with_session()
        self._update_state()

    def _on_index_finished(self) -> None:
        self._request_fields(self.current_editor())
        self._update_state()

    # -- fields for the completer --------------------------------------------------------------------
    def _request_fields(self, ed: _Editor | None, *, force: bool = False) -> None:
        """Ask for the schema's fields when they are needed: the panel is visible (or a
        field list is opened: ``force``); never for a single-value document."""
        if ed is None or ed.session is not self._session or self._single():
            return
        if not force and not self.isVisible():
            return
        s = ed.session
        doc = getattr(s, "doc", None)
        if doc is None or s.state != "ready" or ed.fields_doc == id(doc):
            return
        schema = fields.get_schema(s, lambda sc, ed=ed: self._fields_ready(ed, sc))
        if schema is not None:
            self._fields_ready(ed, schema)

    def _fields_ready(self, ed: _Editor, schema: Any) -> None:
        if ed not in self._editors.values():
            return
        doc = getattr(ed.session, "doc", None)
        if doc is None:
            return
        ed.fields_doc = id(doc)
        names = fields.field_paths(schema)
        if "$index" not in names:
            names.append("$index")
        ed.set_fields(names)

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        self._request_fields(self.current_editor())

    # -- status ----------------------------------------------------------------------------------------
    def status_text(self) -> str:
        return self.status_label.text()

    def warning_text(self) -> str:
        return self.warn_label.text() if not self.warn_label.isHidden() else ""

    def _update_status(self, *_args: Any) -> None:
        s = self._session
        ed = self.current_editor()
        if s is None or ed is None or not self.usable():
            self.status_label.setText("")
            self.warn_label.hide()
            self.load_applied_button.hide()
            return
        doc = s.doc
        total = int(doc.record_count) if doc is not None else 0
        n = s.view_len()
        shown = getattr(s, "shown_records", None)     # search results shown: the filter works on them
        if shown is not None:
            try:
                total = len(shown)
            except Exception:   # a set closed meanwhile (the session replaces it)
                total = n
            label = getattr(s, "shown_records_label", None) or t("gui.filter.status.shown_records")
        if s.view_busy:
            text = t("gui.filter.status.running", count=format_count(n))
        elif s.filter is not None or s.quick_filters:
            if shown is not None:
                text = t("gui.filter.status.result_shown", count=format_count(n), total=format_count(total),
                         source=label)
            else:
                text = t("gui.filter.status.result", count=format_count(n), total=format_count(total))
        elif shown is not None:
            text = t("gui.filter.status.none_shown", total=format_count(total), source=label)
        else:
            text = t("gui.filter.status.none", total=format_count(total))
        differs = _spec_key(s.filter) != ed.content_key()
        if differs:
            text += " \N{MIDDLE DOT} " + t("gui.filter.status.pending")
        if self._message:
            text = self._message + "\n" + text
        self.status_label.setText(text)
        self.load_applied_button.setVisible(differs and s.filter is not None and not s.view_busy)
        warnings = list(getattr(s, "view_warnings", []) or [])
        if warnings:
            self.warn_label.setText("\n".join(warnings))
            self.warn_label.show()
        else:
            self.warn_label.hide()

    def _say(self, text: str | None) -> None:
        self._message = text
        self._update_status()

    def _on_editor_changed(self) -> None:
        self._message = None   # a message about the previous content is stale now
        self._update_status()

    # -- actions -----------------------------------------------------------------------------------------
    def apply(self) -> bool:
        """Apply the expression (Qo'llash); False when nothing was applied."""
        s = self._session
        ed = self.current_editor()
        if s is None or ed is None or not self.usable():
            return False
        ed.flush()
        self._message = None
        text, lang = ed.text(), ed.lang_name()
        if not text.strip():
            return self._set_filter(None)
        if ed.builder_error_text() and ed.state == "sync":
            self._say(t("gui.filter.status.builder_error"))
            return False
        try:
            compile_filter(text, lang)
        except QuerySyntaxError as exc:
            ed._set_error(exc)
            self._say(t("gui.filter.status.invalid"))
            ed.expr.setFocus(Qt.FocusReason.OtherFocusReason)
            return False
        return self._set_filter(FilterSpec(text=text, lang=lang, builder=ed.builder_dict()))

    def _set_filter(self, spec: FilterSpec | None) -> bool:
        s = self._session
        try:
            s.set_filter(spec)
        except JsonReshapeError as exc:
            self.app.report_error(exc, self)
            return False
        ed = self.current_editor()
        if ed is not None:
            ed.applied_key = ed.content_key()      # what this draft applied (see _on_opened)
        self._update_status()
        self.filter_applied.emit()
        return True

    def clear(self) -> None:
        """Tozalash: empty builder and expression, no main filter."""
        ed = self.current_editor()
        if ed is None or not self.usable():
            return
        ed.reset()
        self._message = None
        if self._session.filter is not None:
            self._set_filter(None)
        self._update_status()

    def load_applied(self) -> None:
        """Replace the draft by the filter the session applies."""
        ed = self.current_editor()
        if ed is not None and self._session is not None:
            ed.load_spec(self._session.filter)
            self._update_status()

    def add_condition(self, field: str, op: str = "eq", value: Any = None) -> bool:
        """Add a condition row (``value=None``: an empty value editor to fill; JSON null is
        ``op="is_null"``). When the builder is read-only, a complete condition is AND-ed to
        the expression text instead. Returns whether something was added."""
        op_arity(op)
        ed = self.current_editor()
        if ed is None or not self.usable():
            return False
        ed.flush()
        if ed.builder_available():
            row = ed.root.add_condition(field, op, _NO if value is None else value)
            row.focus_input()
            return True
        complete = op_arity(op) == 0 or value is not None
        if ed.lang_name() == "expr" and ed.state == "unrepresentable" and complete:
            try:
                cond = Condition(field, op, value).to_text()
            except JsonReshapeError as exc:
                self.app.report_error(exc, self)
                return False
            ed.set_text(f"({ed.text().strip()}) and {cond}", "expr")
            ed.dirty = True
            return True
        self._say(t("gui.filter.cannot_add"))
        return False

    def focus_expression(self) -> None:
        ed = self.current_editor()
        if ed is not None:
            ed.expr.setFocus(Qt.FocusReason.ShortcutFocusReason)
            ed.expr.selectAll()

    # -- presets --------------------------------------------------------------------------------------------
    # The presets file is read and written under an inter-process lock (up to seconds when
    # another process holds it): every operation runs in a silent task; the preset buttons
    # are disabled until it ends.
    def preset_names(self) -> list[str]:
        return [self.preset_combo.itemText(i) for i in range(self.preset_combo.count())]

    def presets_busy(self) -> bool:
        """A presets file operation runs or waits."""
        return self._preset_task is not None or bool(self._preset_queue)

    def _run_preset(self, work: Any, done: Any, busy_text: str | None = None) -> bool:
        """Run ``work()`` (worker thread) then ``done(result)`` (GUI thread), one operation
        at a time (later ones wait in order); errors are reported and the names read again."""
        self._preset_queue.append((work, done, busy_text))
        if self._preset_task is None:
            self._next_preset()
        return True

    def _next_preset(self) -> None:
        if self._preset_task is not None or not self._preset_queue:
            self._update_preset_buttons()
            return
        work, done, busy_text = self._preset_queue.pop(0)

        def finished() -> None:
            self._preset_task = None

        def ok(result: Any) -> None:
            finished()
            done(result)
            self.presets_changed.emit()
            self._next_preset()

        def failed(exc: BaseException) -> None:
            finished()
            self._say(None)
            self.app.report_error(exc, self)
            if not self._preset_queue:
                self._refresh_presets()
            self._next_preset()

        def cancelled() -> None:
            finished()
            self._preset_queue.clear()
            self._update_preset_buttons()

        self._preset_task = self.app.tasks.run(lambda _ctx: work(), title=t("gui.filter.task.presets"),
                                               kind="presets", owner=self._preset_owner, silent=True, on_done=ok,
                                               on_error=failed, on_cancel=cancelled)
        self._update_preset_buttons()
        if busy_text:
            self._say(busy_text)

    def _fill_presets(self, names: list[str], select: str | None = None) -> None:
        current = select if select is not None else self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(names)
        folded = [n.casefold() for n in names]
        idx = folded.index(current.casefold()) if current and current.casefold() in folded else -1
        self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.blockSignals(False)

    def _refresh_presets(self, select: str | None = None) -> bool:
        """Read the preset names again (background)."""
        presets = self._presets
        return self._run_preset(presets.names, lambda names: self._fill_presets(names, select))

    def load_preset(self, name: str | None = None) -> bool:
        """Put the preset's expression (and language) into the editor (read in the
        background). False when nothing was started."""
        ed = self.current_editor()
        if ed is None or not self.usable():
            return False
        name = name if name is not None else self.preset_combo.currentText()
        if not name:
            return False
        presets = self._presets

        def work() -> tuple[dict, list[str]]:
            return presets.get(name), presets.names()

        def done(result: tuple[dict, list[str]]) -> None:
            entry, names = result
            self._fill_presets(names, entry["name"])
            if ed in self._editors.values():
                ed.set_text(entry["text"], entry["lang"])
                ed.dirty = True
            self._say(t("gui.filter.preset.loaded", name=entry["name"]))

        return self._run_preset(work, done, t("gui.filter.preset.working"))

    def save_preset(self, name: str | None = None) -> bool:
        """Save the expression under ``name`` (asked when ``None``); an existing name is
        overwritten after a confirmation. The file is written in the background
        (:attr:`presets_changed` when done). False when nothing was started."""
        ed = self.current_editor()
        if ed is None or not self.usable():
            return False
        ed.flush()
        if name is None:
            name, ok = QInputDialog.getText(self, t("gui.filter.preset.save_title"), t("gui.filter.preset.name"),
                                            QLineEdit.EchoMode.Normal, self.preset_combo.currentText())
            if not ok:
                return False
        name = (name or "").strip()
        if not name:
            return False
        text, lang = ed.text(), ed.lang_name()
        if not text.strip():
            self.app.report_error(UserInputError("query.preset.empty", name=name), self)
            return False
        existing = {n.casefold(): n for n in self.preset_names()}
        if name.casefold() in existing and not confirm(
                self, t("gui.filter.preset.overwrite", name=existing[name.casefold()])):
            return False
        presets = self._presets

        def work() -> list[str]:
            presets.save(name, text, lang)
            return presets.names()

        def done(names: list[str]) -> None:
            self._fill_presets(names, name)
            self._say(t("gui.filter.preset.saved", name=name))

        return self._run_preset(work, done, t("gui.filter.preset.working"))

    def delete_preset(self, name: str | None = None) -> bool:
        """Delete the preset (after a confirmation; in the background). False when nothing
        was started."""
        if not self.usable():
            return False
        name = name if name is not None else self.preset_combo.currentText()
        if not name:
            return False
        if not confirm(self, t("gui.filter.preset.delete_confirm", name=name)):
            return False
        presets = self._presets

        def work() -> tuple[bool, list[str]]:
            return presets.delete(name), presets.names()

        def done(result: tuple[bool, list[str]]) -> None:
            removed, names = result
            self._fill_presets(names, "")
            self._say(t("gui.filter.preset.deleted", name=name) if removed else None)

        return self._run_preset(work, done, t("gui.filter.preset.working"))


class _TaskOwner:
    """Owner token of the panel's tasks (detached when the panel is destroyed)."""
