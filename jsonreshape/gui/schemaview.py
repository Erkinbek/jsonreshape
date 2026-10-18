"""SchemaView: the structure of the records (docs/ARCHITECTURE.md §11.7 "Schema"; AGENT_PROMPT §4.4).

The view shows :func:`jsonreshape.schema.infer_schema` of the document through the shared
cache of :mod:`jsonreshape.gui.fields`: a sample of ``Settings.sample_size`` records when the
view is first activated, every record after **To'liq skanerlash** (a background task with
progress, cancellable from the tasks panel). The schema always describes all records of the
document (the base set), not the current filter or sort; the header says so.

A tree of fields (a ``QTreeWidget``: the number of fields is bounded by
``schema.MAX_FIELDS``; the rows are built in timed batches and old rows deleted in timed
batches, so even 20 000 fields never hold the GUI thread for long) with the columns *field* (key, type badge), *types* (distribution text
plus a stacked bar), *presence* (% with a bar), *count* (occurrences / records), *examples*
and *details* (string length, number range, array length, object keys). ``$key`` comes
first for keyed documents. Columns sort on a header click (the header menu restores the
schema order); a search box filters the fields.

Actions on the selected field (toolbar and context menu; disabled with a tooltip saying why
when they do not apply): add as a table column (:attr:`SchemaView.add_column_requested`),
quick filter "exists" / "missing" (``session.set_quick_filter``), sort ascending /
descending (``session.sort_by_field``; a field under ``[*]`` sorts by its first value, the
rule of :class:`jsonreshape.sortkeys.SortKey`), copy the path, add to the filter builder
(:attr:`SchemaView.filter_field_requested`).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import JsonReshapeError
from jsonreshape.gui import copyfmt, fields, theme
from jsonreshape.gui.widgets import ViewBase, format_count, set_clipboard_text
from jsonreshape.i18n import t
from jsonreshape.paths import Wild
from jsonreshape.query import render_path
from jsonreshape.schema import FieldInfo, Schema

log = logging.getLogger(__name__)

__all__ = ["SchemaView", "badge_colors", "field_label", "types_text", "details_text", "percent_text"]

COL_FIELD, COL_TYPES, COL_PRESENCE, COL_COUNT, COL_EXAMPLES, COL_DETAILS, COL_ORDER = range(7)
ROLE_KIND = int(Qt.ItemDataRole.UserRole) + 1        # main type of the field (badge)
ROLE_DIST = int(Qt.ItemDataRole.UserRole) + 2        # [(type, fraction)] (types bar)
ROLE_PRESENCE = int(Qt.ItemDataRole.UserRole) + 3    # 0..1 (presence bar)
BAR_WIDTH = 56
EXPAND_ALL_LIMIT = 3000      # larger schemas start with the first level expanded only
#: Seconds of field-row building per event-loop turn: a schema has up to 20 000 fields and
#: building all their rows at once kept the GUI thread busy for ~0.6 s.
FILL_BUDGET_S = 0.02
FILL_BATCH = 32              # rows built between two clock reads
_DISPLAY_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff\ud800-\udfff]")


# ----------------------------------------------------------------------------------
# texts
# ----------------------------------------------------------------------------------
def _display(text: str) -> str:
    """Control / bidi characters and lone surrogates as ``\\uXXXX`` (one line, safe)."""
    return _DISPLAY_RE.sub(lambda m: "\\u%04x" % ord(m.group()), text)


def percent_text(x: float) -> str:
    """``98%``, ``<0.1%``, ``>99.9%``, ``100%`` (one decimal when needed)."""
    if x >= 1.0:
        return "100%"
    if x <= 0.0:
        return "0%"
    v = x * 100.0
    if v < 0.1:
        return "<0.1%"
    if v > 99.9:
        return ">99.9%"
    text = f"{v:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text + "%"


def is_pooled(fi: FieldInfo) -> bool:
    """The field collects the keys of a map-like object (``scores.*``)."""
    return any(seg is Wild.ANY_KEY for seg in fi.path)


def under_array(fi: FieldInfo) -> bool:
    return any(seg is Wild.ANY_INDEX for seg in fi.path)


def field_label(fi: FieldInfo) -> str:
    """The name shown in the tree: the last path segment (``[*]`` for array elements,
    ``*`` for pooled map keys, ``$key`` / ``$`` for the member key / the record)."""
    if fi.is_key:
        return "$key"
    if not fi.path:
        return "$"
    seg = fi.path[-1]
    if seg is Wild.ANY_INDEX:
        return "[*]"
    if seg is Wild.ANY_KEY:
        return "*"
    if isinstance(seg, Wild):  # pragma: no cover - the schema has no descendants
        return "**"
    return _display(str(seg)) if str(seg) else '""'


def _type_name(kind: str) -> str:
    return t("schema.type." + kind)


def types_text(fi: FieldInfo) -> str:
    """``satr 98% · null 2%`` (most frequent first)."""
    if not fi.count:
        return ""
    return " \N{MIDDLE DOT} ".join(f"{_type_name(k)} {percent_text(n / fi.count)}" for k, n in fi.types.items())


def _span(lo: Any, hi: Any) -> str:
    return f"{lo}" if lo == hi else f"{lo}\N{EN DASH}{hi}"


def details_text(fi: FieldInfo) -> str:
    """String length, number range, array length, object keys (those that apply)."""
    parts = []
    if fi.str_len is not None:
        parts.append(t("gui.schema.detail.str_len", range=_span(*fi.str_len)))
    if fi.num_range is not None:
        lo, hi = fi.num_range
        parts.append(t("gui.schema.detail.num_range", range=lo if lo == hi else f"{lo} \N{HORIZONTAL ELLIPSIS} {hi}"))
    if fi.arr_len is not None:
        parts.append(t("gui.schema.detail.arr_len", range=_span(*fi.arr_len)))
    if fi.obj_keys is not None:
        parts.append(t("gui.schema.detail.obj_keys", range=_span(*fi.obj_keys)))
    return " \N{MIDDLE DOT} ".join(parts)


def _count_text(fi: FieldInfo) -> str:
    if fi.count == fi.records:
        return format_count(fi.count)
    return f"{format_count(fi.count)} / {format_count(fi.records)}"


def expression_field(fi: FieldInfo) -> str:
    """The field in filter-expression syntax (``$key``, ``$``, ``orders[*].id``)."""
    if fi.is_key:
        return "$key"
    return render_path(fi.path)


# ----------------------------------------------------------------------------------
# items and painting
# ----------------------------------------------------------------------------------
class _FieldItem(QTreeWidgetItem):
    """One field; sorts by typed keys per column (numbers as numbers). ``sort_column`` is
    the view's one-item list holding the column being sorted (kept by the view from the
    header's sort indicator): a sort calls :meth:`__lt__` ~10 times per row, asking the
    tree for its column each time made sorting 20 000 rows take ~80 ms."""

    def __init__(self, fi: FieldInfo, order: int, sort_column: list[int] | None = None,
                 parent_row: int = -1) -> None:
        super().__init__()
        self.fi = fi
        self._sort_column = sort_column if sort_column is not None else [COL_ORDER]
        self.parent_row = parent_row         # the parent's index in the view's rows (-1: top level)
        self.hidden_by_search = False
        self.search_text = fi.text.casefold()
        label = field_label(fi)
        types = types_text(fi)
        examples = ", ".join(_display(e) for e in fi.examples)
        details = details_text(fi)
        main = fi.main_type or ""
        self.keys: tuple = (
            label.casefold(),
            (main, -fi.type_fraction(main) if main else 0.0),
            fi.presence,
            fi.count,
            examples.casefold(),
            details.casefold(),
            order,
        )
        self.setText(COL_FIELD, label)
        self.setText(COL_TYPES, types)
        self.setText(COL_PRESENCE, percent_text(fi.presence))
        self.setText(COL_COUNT, _count_text(fi))
        self.setText(COL_EXAMPLES, examples)
        self.setText(COL_DETAILS, details)
        self.setText(COL_ORDER, str(order))
        self.setTextAlignment(COL_COUNT, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setData(COL_FIELD, ROLE_KIND, main)
        dist = [(k, n / fi.count) for k, n in fi.types.items()] if fi.count else []
        self.setData(COL_TYPES, ROLE_DIST, dist)
        self.setData(COL_PRESENCE, ROLE_PRESENCE, float(fi.presence))
        # tooltips
        tip = _display(fi.text)
        if is_pooled(fi):
            tip += "\n" + t("gui.schema.tip.pooled")
        if fi.is_key:
            tip += "\n" + t("gui.schema.tip.key")
        self.setToolTip(COL_FIELD, tip)
        if fi.count:
            self.setToolTip(COL_TYPES, "\n".join(
                t("gui.schema.tip.type", type=_type_name(k), count=format_count(n), pct=percent_text(n / fi.count))
                for k, n in fi.types.items()))
        context = t("gui.schema.tip.context_elements") if under_array(fi) else t("gui.schema.tip.context_records")
        self.setToolTip(COL_PRESENCE, t("gui.schema.tip.presence", present=format_count(fi.present),
                                        base=format_count(fi.base), context=context))
        self.setToolTip(COL_COUNT, t("gui.schema.tip.count", count=format_count(fi.count),
                                     records=format_count(fi.records)))
        if fi.examples:
            self.setToolTip(COL_EXAMPLES, "\n".join(_display(e) for e in fi.examples))
        if details:
            self.setToolTip(COL_DETAILS, details.replace(" \N{MIDDLE DOT} ", "\n"))

    def __lt__(self, other: QTreeWidgetItem) -> bool:  # noqa: D401 (Qt sort hook)
        col = self._sort_column[0]
        if isinstance(other, _FieldItem) and 0 <= col < len(self.keys):
            return self.keys[col] < other.keys[col]
        return super().__lt__(other)


def presence_bar_colors(selected: bool, selected_text: QColor) -> tuple[QColor, QColor]:
    """``(track, fill)`` of the presence bar. On a selected row the accent color may be the
    selection color itself (dark theme): the bar is drawn in the selected text's color
    over a faint track of it, so it stays visible."""
    if not selected:
        return QColor(theme.color("line")), QColor(theme.color("accent"))
    track = QColor(selected_text)
    track.setAlpha(70)
    return track, QColor(selected_text)


def badge_colors(kind: str, selected: bool, base: QColor) -> tuple[QColor | None, QColor, QColor]:
    """``(chip, tint, text)`` of the type badge ``kind``. The badge is the type color at
    low opacity with the glyph in the type color; on a selected row the selection color
    can be that hue itself (the dark theme's highlight is the blue of numbers and
    objects), so the badge first gets an opaque ``chip`` of the view's base color and
    looks as it does on the other rows."""
    chip = QColor(base) if selected else None
    if chip is not None:
        chip.setAlpha(255)
    return chip, QColor(theme.badge_background(kind)), QColor(theme.badge_color(kind))


class _SchemaDelegate(QStyledItemDelegate):
    """Type badge in the field column, stacked type bar and presence bar."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        col = index.column()
        if col not in (COL_FIELD, COL_TYPES, COL_PRESENCE):
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        rect = opt.rect.adjusted(4, 0, -4, 0)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        fg = opt.palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        fm = opt.fontMetrics
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if col == COL_FIELD:
                kind = index.data(ROLE_KIND)
                if kind:
                    glyph = theme.badge_glyph(kind)
                    font = painter.font()
                    small = opt.font
                    small.setPointSizeF(max(6.0, opt.font.pointSizeF() * 0.8))
                    painter.setFont(small)
                    sfm = painter.fontMetrics()
                    w = sfm.horizontalAdvance(glyph) + 8
                    h = min(rect.height() - 2, sfm.height() + 2)
                    badge = QRect(rect.left(), rect.center().y() - h // 2, w, h)
                    chip, tint, ink = badge_colors(kind, selected, opt.palette.color(QPalette.ColorRole.Base))
                    painter.setPen(Qt.PenStyle.NoPen)
                    if chip is not None:
                        painter.setBrush(chip)
                        painter.drawRoundedRect(badge, 3, 3)
                    painter.setBrush(tint)
                    painter.drawRoundedRect(badge, 3, 3)
                    painter.setPen(ink)
                    painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, glyph)
                    painter.setFont(font)
                    rect.setLeft(badge.right() + 6)
            elif col == COL_TYPES:
                dist = index.data(ROLE_DIST) or []
                bar = QRect(rect.left(), rect.center().y() - 4, BAR_WIDTH, 8)
                painter.setPen(Qt.PenStyle.NoPen)
                if selected:     # a frame of the base color: the type colors stand out from the selection
                    painter.fillRect(bar.adjusted(-1, -1, 1, 1), opt.palette.color(QPalette.ColorRole.Base))
                painter.setBrush(theme.color("line"))
                painter.drawRect(bar)
                x = bar.left()
                for i, (kind, frac) in enumerate(dist):
                    w = bar.right() + 1 - x if i == len(dist) - 1 else round(frac * BAR_WIDTH)
                    if w > 0:
                        painter.fillRect(QRect(x, bar.top(), w, bar.height()), theme.badge_color(kind))
                    x += max(0, w)
                rect.setLeft(bar.right() + 8)
            else:
                frac = float(index.data(ROLE_PRESENCE) or 0.0)
                bar = QRect(rect.left(), rect.center().y() - 3, BAR_WIDTH, 6)
                track, fill = presence_bar_colors(selected, fg)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(track)
                painter.drawRoundedRect(bar, 2, 2)
                w = round(max(0.0, min(1.0, frac)) * BAR_WIDTH)
                if w > 0:
                    painter.setBrush(fill)
                    painter.drawRoundedRect(QRect(bar.left(), bar.top(), w, bar.height()), 2, 2)
                rect.setLeft(bar.right() + 8)
            painter.setPen(fg)
            shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width()))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), shown)
        finally:
            painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:  # noqa: N802 (Qt name)
        size = super().sizeHint(option, index)
        col = index.column()
        if col in (COL_TYPES, COL_PRESENCE):
            size.setWidth(size.width() + BAR_WIDTH + 12)
        elif col == COL_FIELD and index.data(ROLE_KIND):
            size.setWidth(size.width() + option.fontMetrics.horizontalAdvance("null") + 16)
        return size


class _Fill:
    """A schema being put on screen in timed steps: ``stage`` "build" (rows made off the
    tree), "attach" (rows put on the tree, sorted once at most), "search" (the search box
    applied, in slices), "finish" (shown)."""

    __slots__ = ("schema", "flat", "items", "tops", "pos", "keep", "stage", "visible", "expand", "spos")

    def __init__(self, schema: Schema, flat: list[tuple[FieldInfo, int]], keep: str | None) -> None:
        self.schema = schema
        self.keep = keep                     # the field selected before (selected again when shown)
        self.flat = flat                     # (field, index of its parent row; -1: top level) in schema order
        self.items: list[_FieldItem] = []
        self.tops: list[_FieldItem] = []
        self.pos = 0
        self.stage = "build"
        self.visible: list[bool] | None = None   # per row: shown by the search box
        self.expand = False
        self.spos = 0


def _schema_rows(schema: Schema) -> list[tuple[FieldInfo, int]]:
    """The rows of ``schema`` in pre-order: ``(field, parent row index or -1)``; the record
    itself first when the records are not all objects."""
    flat: list[tuple[FieldInfo, int]] = []
    root = schema.root
    if root.count and set(root.types) != {"object"}:
        flat.append((root, -1))
    stack: list[tuple[FieldInfo, int]] = [(fi, -1) for fi in reversed(root.children.values())]
    while stack:
        fi, parent = stack.pop()
        flat.append((fi, parent))
        me = len(flat) - 1
        stack.extend((child, me) for child in reversed(fi.children.values()))
    return flat


# ----------------------------------------------------------------------------------
# the view
# ----------------------------------------------------------------------------------
class SchemaView(ViewBase):
    """Types, presence, examples and ranges of the record fields (see the module doc)."""

    view_id = "schema"

    add_column_requested = Signal(str)     # field text (paths syntax, "$key", "$")
    filter_field_requested = Signal(str)   # field text for the filter builder
    schema_shown = Signal()                # a schema was put on screen (tests, status)

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(session, parent)
        self._schema: Schema | None = None
        self._doc_id: int | None = None
        self._loading = False
        self._full_running = False
        self._activated = False
        self._stale = False
        self._dead = False
        self._items: list[_FieldItem] = []
        self._fill: _Fill | None = None      # rows of a new schema being built
        self._fill_timer = QTimer(self)
        self._fill_timer.setInterval(0)
        self._fill_timer.timeout.connect(self._fill_step)
        self._trash: list[_FieldItem] = []   # replaced rows, deleted in timed batches
        self._trash_timer = QTimer(self)
        self._trash_timer.setInterval(0)
        self._trash_timer.timeout.connect(self._trash_step)
        self._sort_column = [COL_ORDER]      # the column being sorted (see _FieldItem)
        self._zoom = 0
        self._build_ui()
        session.opened.connect(self._on_opened)
        session.index_finished.connect(self._on_index_finished)
        session.state_changed.connect(self._on_state_changed)
        session.app.tasks.task_finished.connect(self._on_task_finished)
        theme.notifier().theme_changed.connect(self._on_theme_changed)
        self._update_actions()
        self._show_message(t("gui.schema.not_loaded"))

    # -- UI -------------------------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        top = QHBoxLayout()
        top.setContentsMargins(6, 4, 6, 0)
        self.header_label = QLabel(self)
        self.header_label.setObjectName("schemaHeader")
        self.header_label.setTextFormat(Qt.TextFormat.PlainText)
        self.header_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.header_label.setToolTip(t("gui.schema.base_note"))
        top.addWidget(self.header_label, 1)
        self.full_button = QPushButton(t("gui.schema.full_scan"), self)
        self.full_button.setObjectName("schemaFullScan")
        self.full_button.setToolTip(t("gui.schema.full_scan.tip"))
        self.full_button.clicked.connect(self.full_scan)
        top.addWidget(self.full_button)
        lay.addLayout(top)
        self.note_label = QLabel(t("gui.schema.base_note"), self)
        self.note_label.setWordWrap(True)
        self.note_label.setContentsMargins(6, 0, 6, 0)
        self._style_muted(self.note_label)
        lay.addWidget(self.note_label)

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 0, 6, 0)
        bar.setSpacing(2)
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(t("gui.schema.search.placeholder"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_search)
        self.search_edit.setMaximumWidth(260)
        bar.addWidget(self.search_edit)
        bar.addSpacing(8)
        self.act_add_column = self._action("schema_add_column", "gui.schema.act.add_column", self._add_column)
        self.act_filter_exists = self._action("schema_filter_exists", "gui.schema.act.filter_exists",
                                              lambda: self._quick_filter(exists=True))
        self.act_filter_missing = self._action("schema_filter_missing", "gui.schema.act.filter_missing",
                                               lambda: self._quick_filter(exists=False))
        self.act_sort_asc = self._action("schema_sort_asc", "gui.schema.act.sort_asc", lambda: self._sort(False))
        self.act_sort_desc = self._action("schema_sort_desc", "gui.schema.act.sort_desc", lambda: self._sort(True))
        self.act_copy_path = self._action("schema_copy_path", "gui.schema.act.copy_path", self._copy_field_path)
        self.act_filter_builder = self._action("schema_filter_builder", "gui.schema.act.filter_builder",
                                               self._to_filter_builder)
        for act in self.actions_list():
            btn = QToolButton(self)
            btn.setDefaultAction(act)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            btn.setAutoRaise(True)
            bar.addWidget(btn)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.stack = QStackedWidget(self)
        self.message = QLabel(self.stack)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self._style_muted(self.message)
        msg_page = QWidget(self.stack)
        mlay = QVBoxLayout(msg_page)
        mlay.addStretch(1)
        mlay.addWidget(self.message)
        self.retry_button = QPushButton(t("gui.schema.retry"), msg_page)
        self.retry_button.clicked.connect(lambda: self._load(force=True))
        self.retry_button.hide()
        mlay.addWidget(self.retry_button, 0, Qt.AlignmentFlag.AlignHCenter)
        mlay.addStretch(1)
        self.stack.addWidget(msg_page)

        self.tree = QTreeWidget(self.stack)
        self.tree.setObjectName("schemaTree")
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels([t("gui.schema.col.field"), t("gui.schema.col.types"),
                                   t("gui.schema.col.presence"), t("gui.schema.col.count"),
                                   t("gui.schema.col.examples"), t("gui.schema.col.details"), "#"])
        self.tree.setColumnHidden(COL_ORDER, True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setItemDelegate(_SchemaDelegate(self.tree))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.currentItemChanged.connect(lambda *_: self._update_actions())
        self.tree.itemSelectionChanged.connect(self._update_actions)
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSortIndicatorShown(True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        # connected before the tree's own sorting slot (setSortingEnabled): runs first
        header.sortIndicatorChanged.connect(self._on_sort_indicator)
        widths = (220, 210, 120, 90, 260)
        for col, w in enumerate(widths):
            self.tree.setColumnWidth(col, w)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(COL_ORDER, Qt.SortOrder.AscendingOrder)
        self.stack.addWidget(self.tree)
        lay.addWidget(self.stack, 1)
        self._base_font = self.tree.font()

    def _action(self, name: str, key: str, slot: Any) -> QAction:
        act = QAction(t(key), self)
        act.setObjectName(name)
        act.triggered.connect(slot)
        return act

    def actions_list(self) -> list[QAction]:
        """The field actions in menu order."""
        return [self.act_add_column, self.act_filter_exists, self.act_filter_missing, self.act_sort_asc,
                self.act_sort_desc, self.act_copy_path, self.act_filter_builder]

    @staticmethod
    def _style_muted(label: QLabel) -> None:
        pal = label.palette()
        pal.setColor(QPalette.ColorRole.WindowText, QColor(theme.color("muted")))
        label.setPalette(pal)

    def _show_message(self, text: str, *, retry: bool = False) -> None:
        self.message.setText(text)
        self.retry_button.setVisible(retry)
        self.stack.setCurrentIndex(0)

    # -- ViewBase hooks ------------------------------------------------------------------
    def activate(self) -> None:
        """Became visible: load the sample schema (or the cached one) once."""
        self._activated = True
        if (self._schema is None and self._fill is None) or self._stale or self._doc_id != id(self.session.doc):
            self._load()

    def zoom(self, steps: int) -> None:
        self._zoom = 0 if steps == 0 else max(-6, min(12, self._zoom + int(steps)))
        font = self.tree.font()
        base = self._base_font.pointSizeF()
        if base > 0:
            font.setPointSizeF(max(5.0, base + self._zoom))
            self.tree.setFont(font)

    def quick_find(self) -> None:
        """Ctrl+F: the field search box."""
        self.search_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search_edit.selectAll()

    def current_ref(self) -> Any:
        return None   # the schema has no document node

    def copy_value(self, fmt: str = "json") -> Any:
        """Ctrl+C: the selected field's path (the schema has no values to copy)."""
        return self._copy_field_path()

    def copy_path(self, style: str = "dotted") -> str | None:
        fi = self.current_field()
        if fi is None:
            return None
        if fi.is_key:
            return set_clipboard_text("$key")
        try:
            text = copyfmt.path_text(fi.path, style)
        except ValueError:   # wildcards in a JSON Pointer
            text = fi.text
        return set_clipboard_text(text)

    def copy_key(self) -> str | None:
        fi = self.current_field()
        if fi is None or not fi.path:
            return None
        return set_clipboard_text(field_label(fi))

    def shutdown(self) -> None:
        self._dead = True
        self._stop_fill()
        for signal, slot in ((self.session.opened, self._on_opened),
                             (self.session.index_finished, self._on_index_finished),
                             (self.session.state_changed, self._on_state_changed),
                             (self.session.app.tasks.task_finished, self._on_task_finished),
                             (theme.notifier().theme_changed, self._on_theme_changed)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().shutdown()

    # -- loading -------------------------------------------------------------------------
    @property
    def schema(self) -> Schema | None:
        """The schema on screen (``None`` while loading)."""
        return self._schema

    @property
    def filling(self) -> bool:
        """The rows of a schema are being built (it is shown when they are done)."""
        return self._fill is not None

    @property
    def loading(self) -> bool:
        return self._loading

    @property
    def full_scan_running(self) -> bool:
        return self._full_running

    def _ready(self) -> bool:
        s = self.session
        return s.doc is not None and s.state == "ready"

    def _load(self, *, force: bool = False) -> None:
        if self._dead:
            return
        s = self.session
        self._stale = False
        if s.doc is None or s.state in ("new", "opening"):
            self._show_message(t("gui.schema.waiting_open"))
            return
        if s.state == "failed":
            self._show_message(t("gui.schema.unavailable"))
            return
        if s.state == "indexing":
            self._show_message(t("gui.schema.waiting_index"))
            return
        if self._doc_id != id(s.doc):
            self._schema = None
            self._stop_fill()
            self._full_running = False
        schema = fields.get_schema(s, self._schema_ready, refresh=force)
        if schema is not None:
            self._loading = False
            self._show(schema)
            return
        self._loading = True
        self._show_message(t("gui.schema.loading", count=format_count(int(s.app.settings.sample_size))))
        self._update_actions()

    def full_scan(self) -> None:
        """Analyse every record (background task, cancellable from the tasks panel)."""
        if self._dead or not self._ready() or self._full_running:
            return
        s = self.session
        if self._doc_id != id(s.doc):
            self._schema = None
        schema = fields.get_schema(s, self._schema_ready, full=True)
        if schema is not None:
            self._show(schema)
            return
        self._full_running = True
        self._update_header()

    def _schema_ready(self, schema: Schema) -> None:
        """A schema computed by :mod:`fields` (sample or full) for the current document."""
        if self._dead or self.session.doc is None:
            return
        self._loading = False
        if schema.complete:
            self._full_running = False
        current = self._fill.schema if self._fill is not None else self._schema
        if current is not None and current.complete and not schema.complete and self._doc_id == id(self.session.doc):
            self._update_header()   # never replace a full scan by a sample
            return
        self._show(schema)

    def _on_task_finished(self, task: Any) -> None:
        """A schema task of this session ended: when it brought nothing (cancelled from
        the tasks panel, failed), leave the waiting state."""
        if self._dead or getattr(task, "owner", None) is not self.session or getattr(task, "kind", "") != "schema":
            return
        running = [x for x in self.session.app.tasks.tasks(owner=self.session) if x.kind == "schema"]
        if running:
            return
        if self._full_running and fields.cached_schema(self.session, full=True) is None:
            self._full_running = False
            self._update_header()
        if self._loading and fields.cached_schema(self.session) is None:
            self._loading = False
            text = t("gui.schema.cancelled") if task.state == "cancelled" else t("gui.schema.failed")
            self._show_message(text, retry=True)
            self._update_actions()

    def _on_opened(self) -> None:
        """The document (re)opened: the old schema belongs to another Document."""
        if self._dead:
            return
        if self._schema is not None or self._doc_id is not None:   # a reopen: another Document
            self._schema = None
            self._stop_fill()
            self._full_running = False
            self._discard_rows()
            self._stale = True
        if self._activated and self.isVisible():
            self._load()
        else:
            self._stale = True

    def _on_index_finished(self) -> None:
        if self._dead:
            return
        if self._activated and self._schema is None and not self._loading:
            if self.isVisible():
                self._load()
            else:
                self._stale = True

    def _on_state_changed(self, state: str) -> None:
        if self._dead:
            return
        if state == "failed" and self._schema is None:
            self._show_message(t("gui.schema.unavailable"))
        self._update_actions()

    def _on_theme_changed(self) -> None:
        if not self._dead:
            self._style_muted(self.note_label)
            self._style_muted(self.message)
            self.tree.viewport().update()

    # -- showing -------------------------------------------------------------------------
    def _show(self, schema: Schema) -> None:
        """Put ``schema`` on screen: its rows are built in timed batches (the old rows are
        deleted meanwhile, also in batches; a progress message is shown when it takes more
        than one step), then put on the tree; ``schema_shown`` follows."""
        if self._fill is not None:
            keep = self._fill.keep
        else:
            selected = self.current_field()
            keep = selected.text if selected is not None else None
        self._stop_fill()
        self._doc_id = id(self.session.doc)
        self._fill = _Fill(schema, _schema_rows(schema), keep)
        if self._items:
            self.tree.setSortingEnabled(False)
            self._discard_rows()
        self._update_header()
        self._fill_step()

    def _stop_fill(self) -> None:
        self._fill_timer.stop()
        self._fill = None

    def _fill_step(self) -> None:
        """One turn (:data:`FILL_BUDGET_S`) of putting a schema on screen; small schemas
        are done at once."""
        f = self._fill
        if f is None or self._dead:
            self._fill_timer.stop()
            return
        t0 = time.perf_counter()
        while True:
            if f.stage == "build":
                if not self._build_rows(f, t0):
                    break
                f.stage = "attach"
                if len(f.items) > EXPAND_ALL_LIMIT:
                    break                         # attaching (and sorting) many rows: a turn of its own
            elif f.stage == "attach":
                self._attach_rows(f)
                f.stage = "search"
            elif f.stage == "search":
                if not self._search_rows(f, t0):
                    break
                f.stage = "finish"
            else:
                self._fill_timer.stop()
                self._fill = None
                self._finish_fill(f)
                return
            if time.perf_counter() - t0 >= FILL_BUDGET_S:
                break
        n = max(1, len(f.flat))
        self._show_message(t("gui.schema.filling", percent=min(99, int(f.pos * 100 / n))))
        self._update_actions()
        self._fill_timer.start()

    def _build_rows(self, f: _Fill, t0: float) -> bool:
        """Make rows until the budget is spent; True when every row exists."""
        flat, items, tops = f.flat, f.items, f.tops
        n = len(flat)
        while f.pos < n:
            end = min(n, f.pos + FILL_BATCH)
            for i in range(f.pos, end):
                fi, parent = flat[i]
                item = _FieldItem(fi, i, self._sort_column, parent)
                items.append(item)
                if parent < 0:
                    tops.append(item)
                else:
                    items[parent].addChild(item)
            f.pos = end
            if time.perf_counter() - t0 >= FILL_BUDGET_S:
                break
        return f.pos >= n

    def _attach_rows(self, f: _Fill) -> None:
        """Put the built rows on the tree (still behind the progress message), keeping the
        sort column: rows in their natural order are not sorted at all."""
        tree = self.tree
        header = tree.header()
        sort_col = tree.sortColumn()
        sort_col = sort_col if 0 <= sort_col < 7 else COL_ORDER
        order_flag = header.sortIndicatorOrder()
        tree.setSortingEnabled(False)
        self._discard_rows()
        tree.addTopLevelItems(f.tops)
        self._items = f.items
        if len(f.items) <= EXPAND_ALL_LIMIT:
            tree.expandAll()
        else:
            tree.expandToDepth(0)
        if sort_col == COL_ORDER and order_flag == Qt.SortOrder.AscendingOrder:
            # the rows were built in this order: enabling sorting without a column sorts
            # nothing (the order column is hidden, so no indicator is missing)
            header.setSortIndicator(-1, order_flag)
        else:
            header.setSortIndicator(sort_col, order_flag)
        tree.setSortingEnabled(True)          # sorts once, by the indicator

    def _search_rows(self, f: _Fill, t0: float) -> bool:
        """Apply the search box to the new rows in slices; True when done."""
        if f.visible is None:
            needle = self.search_edit.text().strip().casefold()
            f.visible = self._visible_rows(f.items, needle)
            f.expand = bool(needle)
            f.spos = 0
        n = len(f.items)
        while f.spos < n:
            end = min(n, f.spos + 8 * FILL_BATCH)
            self._set_visible(f.items, f.visible, f.expand, f.spos, end)
            f.spos = end
            if time.perf_counter() - t0 >= FILL_BUDGET_S:
                break
        return f.spos >= n

    def _finish_fill(self, f: _Fill) -> None:
        self._schema = f.schema
        self._update_header()
        if not self._items:
            self._show_message(t("schema.no_records") if not f.schema.scanned else t("gui.schema.no_fields"))
        else:
            self.stack.setCurrentIndex(1)
            if f.keep is not None:
                self.select_field(f.keep)
        self._update_actions()
        self.schema_shown.emit()

    def _discard_rows(self) -> None:
        """Take the rows off the tree and delete them in timed batches later: deleting
        20 000 rows in one go took ~50 ms. Only the top-level rows (at most
        ``schema.MAX_KEYS`` + 1) leave the tree here, collapsed first (removing a row
        makes the view lay out its visible rows again)."""
        old = self._items
        self._items = []
        tree = self.tree
        if not old:
            tree.clear()
            return
        root = tree.invisibleRootItem()
        if root.childCount():
            tree.collapseAll()
            for i in reversed(range(root.childCount())):
                root.removeChild(root.child(i))   # Python owns the row now: deleted from the trash
        self._trash.extend(old)           # schema (pre-)order: from its end, children before parents
        if not self._trash_timer.isActive():
            self._trash_timer.start()

    def _trash_step(self) -> None:
        t0 = time.perf_counter()
        trash = self._trash
        while trash:
            for _ in range(min(FILL_BATCH, len(trash))):
                item = trash.pop()
                parent = item.parent()
                if parent is not None:
                    parent.removeChild(item)   # Python owns it now: deleted with the last reference
                del item
            if time.perf_counter() - t0 >= FILL_BUDGET_S:
                break
        if not trash:
            self._trash_timer.stop()

    def _on_sort_indicator(self, column: int, _order: Any) -> None:
        self._sort_column[0] = int(column)

    def _update_header(self) -> None:
        f = self._fill
        schema = f.schema if f is not None else self._schema
        if schema is None:
            self.header_label.setText("")
        else:
            nfields = format_count(len(f.flat) if f is not None else len(self._items))
            seconds = f"{schema.elapsed:.2f}"
            if schema.complete:
                text = t("gui.schema.header.full", scanned=format_count(schema.scanned), fields=nfields,
                         seconds=seconds)
            else:
                text = t("gui.schema.header.sample", scanned=format_count(schema.scanned),
                         total=format_count(schema.total_records), fields=nfields, seconds=seconds)
            if schema.skipped:
                text += " \N{MIDDLE DOT} " + t("gui.schema.header.skipped", count=format_count(schema.skipped))
            if schema.truncated:
                text += " \N{MIDDLE DOT} " + t("gui.schema.header.truncated")
            if self._full_running:
                text += " \N{MIDDLE DOT} " + t("gui.schema.header.scanning")
            self.header_label.setText(text)
        complete = schema is not None and schema.complete
        self.full_button.setEnabled(self._ready() and not self._full_running and not complete)
        if self._full_running:
            self.full_button.setText(t("gui.schema.full_scan.running"))
        else:
            self.full_button.setText(t("gui.schema.full_scan"))
        self.full_button.setToolTip(t("gui.schema.full_scan.done_tip") if complete else t("gui.schema.full_scan.tip"))

    # -- fields / selection --------------------------------------------------------------
    def items(self) -> list[_FieldItem]:
        """The field items in schema order."""
        return list(self._items)

    def item_for(self, text: str) -> _FieldItem | None:
        for item in self._items:
            if item.fi.text == text:
                return item
        return None

    def current_field(self) -> FieldInfo | None:
        item = self.tree.currentItem()
        if isinstance(item, _FieldItem) and item.isSelected():
            return item.fi
        selected = self.tree.selectedItems()
        if selected and isinstance(selected[0], _FieldItem):
            return selected[0].fi
        return None

    def select_field(self, text: str) -> bool:
        """Select the field whose text (``FieldInfo.text``) is ``text``."""
        item = self.item_for(text)
        if item is None:
            return False
        self.tree.setCurrentItem(item)
        item.setSelected(True)
        self.tree.scrollToItem(item)
        self._update_actions()
        return True

    def _apply_search(self, text: str) -> None:
        """Show the fields whose path or name holds ``text`` (ignoring case) and their
        ancestors; all fields for an empty text."""
        f = self._fill
        if f is not None:
            if f.stage in ("search", "finish"):   # the rows are on the tree: apply the new text
                f.stage = "search"
                f.visible = None
            return                                # (the fill applies the search box)
        items = self._items
        if not items:
            return
        needle = (text or "").strip().casefold()
        self._set_visible(items, self._visible_rows(items, needle), bool(needle), 0, len(items))

    @staticmethod
    def _visible_rows(items: list[_FieldItem], needle: str) -> list[bool]:
        """Per row (schema order): shown by the search ``needle`` (casefolded)."""
        if not needle:
            return [True] * len(items)
        shown = [False] * len(items)
        for i, item in enumerate(items):
            if needle in item.search_text or needle in item.keys[0]:
                j = i
                while j >= 0 and not shown[j]:
                    shown[j] = True
                    j = items[j].parent_row
        return shown

    @staticmethod
    def _set_visible(items: list[_FieldItem], shown: list[bool], expand: bool, start: int, stop: int) -> None:
        """Rows ``start..stop-1`` shown / hidden (only the changed ones are touched);
        ``expand``: shown rows with children are expanded (a search result)."""
        for i in range(start, stop):
            item = items[i]
            visible = shown[i]
            if item.hidden_by_search == visible:
                item.setHidden(not visible)
                item.hidden_by_search = not visible
            if expand and visible and item.childCount():
                item.setExpanded(True)

    # -- actions ---------------------------------------------------------------------------
    def _single(self) -> bool:
        doc = self.session.doc
        return doc is not None and doc.kind == "single"

    def _set(self, act: QAction, enabled: bool, why: str = "") -> None:
        act.setEnabled(enabled)
        base = act.text()
        act.setToolTip(base if enabled or not why else f"{base}\n{why}")

    def _update_actions(self) -> None:
        if self._dead:
            return
        fi = self.current_field()
        none = t("gui.schema.why.no_field")
        if fi is None:
            for act in self.actions_list():
                self._set(act, False, none)
            return
        single = self._single()
        single_why = t("gui.session.single_no_view")
        pooled = is_pooled(fi)
        root = not fi.path and not fi.is_key
        self._set(self.act_add_column, not pooled, t("gui.schema.why.pooled_column"))
        filter_ok = not single and not root and not fi.is_key
        filter_why = single_why if single else t("gui.schema.why.always_present")
        self._set(self.act_filter_exists, filter_ok, filter_why)
        self._set(self.act_filter_missing, filter_ok, filter_why)
        sort_ok = not single and not pooled
        sort_why = single_why if single else t("gui.schema.why.pooled_sort")
        for act in (self.act_sort_asc, self.act_sort_desc):
            self._set(act, sort_ok, sort_why)
            if sort_ok and under_array(fi):
                act.setToolTip(f"{act.text()}\n{t('gui.schema.tip.sort_first')}")
        self._set(self.act_copy_path, True)
        self._set(self.act_filter_builder, not single, single_why)

    def _context_menu(self, pos: Any) -> QMenu | None:
        item = self.tree.itemAt(pos)
        if isinstance(item, _FieldItem):
            self.tree.setCurrentItem(item)
            item.setSelected(True)
        self._update_actions()
        menu = self.build_menu()
        menu.popup(self.tree.viewport().mapToGlobal(pos))
        return menu

    def build_menu(self) -> QMenu:
        """The field context menu (the toolbar's actions)."""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addAction(self.act_add_column)
        menu.addSeparator()
        menu.addAction(self.act_filter_exists)
        menu.addAction(self.act_filter_missing)
        menu.addAction(self.act_filter_builder)
        menu.addSeparator()
        menu.addAction(self.act_sort_asc)
        menu.addAction(self.act_sort_desc)
        menu.addSeparator()
        menu.addAction(self.act_copy_path)
        return menu

    def _header_menu(self, pos: Any) -> None:
        menu = QMenu(self)
        act = menu.addAction(t("gui.schema.original_order"))
        act.setObjectName("schema_original_order")
        act.triggered.connect(self.restore_order)
        menu.popup(self.tree.header().mapToGlobal(pos))

    def restore_order(self) -> None:
        """Back to the schema's own order (first appearance)."""
        self.tree.sortByColumn(COL_ORDER, Qt.SortOrder.AscendingOrder)

    def _add_column(self) -> None:
        fi = self.current_field()
        if fi is not None and not is_pooled(fi):
            self.add_column_requested.emit(fi.text)

    def _quick_filter(self, *, exists: bool) -> None:
        fi = self.current_field()
        if fi is None or not self.act_filter_exists.isEnabled():
            return
        expr = f"{'exists' if exists else 'missing'}({expression_field(fi)})"
        try:
            self.session.set_quick_filter(fi.text, expr)
        except JsonReshapeError as exc:
            self.session.app.report_error(exc, self)

    def _sort(self, descending: bool) -> None:
        fi = self.current_field()
        if fi is None or not self.act_sort_asc.isEnabled():
            return
        try:
            self.session.sort_by_field("$key" if fi.is_key else fi.path, descending=descending)
        except JsonReshapeError as exc:
            self.session.app.report_error(exc, self)

    def _copy_field_path(self) -> str | None:
        fi = self.current_field()
        if fi is None:
            return None
        return set_clipboard_text(fi.text)

    def _to_filter_builder(self) -> None:
        fi = self.current_field()
        if fi is not None and not self._single():
            self.filter_field_requested.emit(fi.text)
