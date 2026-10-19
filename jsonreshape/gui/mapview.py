"""The structure builder view ("Tuzilma quruvchi", docs/ARCHITECTURE.md §13.5; AGENT_PROMPT §7, §11).

Layout (the prototype ``json-tuzilma.html``, A6): a toolbar (Tuzilmani ochish…, Tuzilmani saqlash…,
Qaytarish, Qayta bajarish, Asl holat, Hammasini ochish, Yig'ish, To'liq skanerlash, Ixcham,
Eksport…), then a splitter: left the structure tree (title, the stats line ``N maydon · M
o'zgarish``, the source of the tree, the hint line with the three drag rules), right the live
preview (Natija / Asl, the record navigator ``‹ #N ›``, pretty JSON in the text view's colours, a
footer). Messages of the operations ("toasts") appear for 4.2 s at the bottom of the view and in the
status bar.

The tree is a ``QTreeView`` over :class:`~jsonreshape.gui.mapmodel.MapModel` (a flat list of the
visible rows; the tree is bounded by the schema limits) with :class:`MapDelegate` painting the rows
of the prototype (A7): indent guides, caret, label (the file name for the root, "har bir yozuv" /
"har bir element" / "har bir a'zo" for element rows), type chips, meta, badges (pills in the theme
roles accent / muted / ok / array / warn / bool, with tooltips), samples, and the row actions
(group, unwrap, flatten, rename, delete -- shown on the hovered row and on the current row while
the tree has the focus, only when enabled, like the prototype); 400 children per node, then a "yana
N ta maydonni ko'rsatish" row. The expansion is the designer's ``open`` state (undo and reset
restore it, like the reference).

Mouse: a click selects a row (keyboard commands act on it), the caret or a double click opens /
closes it, the action buttons act on their row. Drag & drop is done with mouse events (no QDrag):
a press on a row that may be dragged (not the root, not element rows) and a move past the drag
distance start it; the zone under the mouse is ``Designer.zone_for(target, node, frac)`` (28 %
rule); the target row shows a line before / after it at its indent or a box for "inside"; no zone
= the forbidden cursor; the view scrolls near its top / bottom edge; Esc cancels; the release moves
the node (one undo step) -- only over a row: off the rows (the empty space below them, outside the
view) nothing is moved, like the prototype. Hiding the view cancels a drag.

A new tree (Full scan, an opened mapping file, a reopen: ``MappingState.replaced``) may reuse node
ids for other nodes: a running drag, an open context menu and a rename being typed are dropped then.
A rename being typed is committed before any other command of the view or the window acts on the
tree (a tool button does not take the focus, so the prototype's "leaving the box commits" is done
explicitly: :meth:`MapView.finish_rename`).

Keyboard (AGENT_PROMPT §11, handled by the tree -- never window shortcuts): F2 rename (inline
editor: Enter keeps, Esc cancels, leaving the field keeps), Del delete, Insert new group in the
current object (then the inline rename), Ctrl+X marks the current node for a move and Ctrl+V on a
target moves it there (inside when allowed, else after: ``Designer.paste_zone``; Esc forgets the
mark), Alt+↑ / Alt+↓ move among the siblings, Ctrl+Shift+U unwrap, ← / → close / open (or go to
the parent / first child), Enter opens / closes, the menu key (Shift+F10) opens the context menu
(every operation, disabled ones with their reason as tooltip). Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z undo
/ redo the designer: the main window routes its undo / redo to the active view when it has its own
history (:attr:`MapView.can_undo` ...), and the tree handles the keys itself when there is no window
action. Ctrl+E (the window's export, routed to :meth:`MapView.export_with_mapping`) opens the
export dialog in mapping mode. The window's expansion commands (``*``, Yig'ish, "N daraja") act on
the builder's tree (:meth:`MapView.expand_all_current`, :meth:`MapView.collapse_all`,
:meth:`MapView.expand_to_level`); Ctrl+C / Ctrl+Shift+C copy the current row's source path.

Preview (A10): Natija = the current record through the mapping (``engine.preview``, limit 21: 20
array items + "… davomi eksportda"), Asl = the record unchanged ("… yana N ta"); record numbers are
0-based; the record follows the session's current record when it changes, the navigator moves on
its own otherwise; recomputed 60 ms after the last change; the record is read and transformed in a
silent task (the GUI thread never parses it), records above
:data:`~jsonreshape.gui.mapmodel.PREVIEW_MAX_BYTES` are not previewed, errors are shown inline.
Wrapped documents show the wrapper with the one record; keyed documents ``{"key": value}``.

Decisions: a single click does not toggle a row (desktop selection; the prototype toggles on any
click), Enter toggles (F2 renames); the inline rename keeps the prototype's rule (an empty or
unchanged name changes nothing); "Ixcham" is remembered per session and preselects the compact
indent of the export dialog; rows indent 18 px per level like the prototype until the indentation
reaches 35 % of the row width, then 6 px per level up to 60 % (:meth:`MapDelegate.indent`: the view
has no horizontal scrolling, deep recursive data keeps its names on screen); the record navigator
is a 64-bit spin box (:class:`RecordSpin`: documents may hold more than 2**31 - 1 records).
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QSyntaxHighlighter,
    QTextCharFormat,
    QValidator,
)
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionSpinBox,
    QStyleOptionViewItem,
    QToolButton,
    QToolTip,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import mapping as mp
from jsonreshape.errors import JsonReshapeError
from jsonreshape.gui import theme
from jsonreshape.gui.mapmodel import (
    INFO_ROLE,
    ROW_ROLE,
    MapModel,
    MapRow,
    MappingState,
    PreviewResult,
    RowInfo,
    compute_preview,
    display_text,
    row_info,
    state_for,
    visible_rows,
)
from jsonreshape.gui.widgets import ViewBase, ask, format_count, mono_font, set_clipboard_text
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = ["MapDelegate", "MapTree", "MapView", "PreviewPane", "RecordSpin", "RowParts", "PREVIEW_DEBOUNCE_MS",
           "TOAST_MS"]

PREVIEW_DEBOUNCE_MS = 60       # the prototype's schedulePreview
TOAST_MS = 4200                # the prototype's toast time
INDENT = 18                    # px per level (the prototype's --indent)
DEEP_STEP = 6                  # px per level once the indentation passed DEEP_SOFT of the row width
DEEP_SOFT = 0.35               # (deep rows keep room for their name: at most DEEP_HARD of the width)
DEEP_HARD = 0.6
PAD_LEFT = 8
CARET_W = 20
GAP = 8                        # between the parts of a row
CHIP_GAP = 3
BADGE_GAP = 4
ACTION_W = 26
ACTION_GAP = 1
AUTO_SCROLL_MS = 40
MIN_EDITOR_W = 80
MAX_EDITOR_W = 220
FILE_FILTER = "*.json"

_ACTION_TIPS = {
    "group": "mapping.action.group",
    "unwrap": "mapping.action.unwrap",
    "flatten": "mapping.action.flatten",
    "rename": "mapping.action.rename",
    "delete": "mapping.action.delete",
}
# the prototype's icons (16x16 view box, stroked): polylines, closed when flagged
_ICONS: dict[str, list[tuple[list[tuple[float, float]], bool]]] = {
    "group": [([(2, 4.5), (6, 4.5), (7.2, 6), (14, 6), (14, 12.5), (2, 12.5)], True),
              ([(8, 7.6), (8, 10.9)], False), ([(6.35, 9.25), (9.65, 9.25)], False)],
    "unwrap": [([(6, 3), (3, 3), (3, 13), (13, 13), (13, 10)], False), ([(7.5, 8.5), (13, 3)], False),
               ([(9.5, 3), (13, 3), (13, 6.5)], False)],
    "flatten": [([(3, 3.5), (7, 3.5)], False), ([(5, 6.5), (9, 6.5)], False), ([(7, 9.5), (11, 9.5)], False),
                ([(3, 12.5), (13, 12.5)], False), ([(12, 4), (12, 9.5)], False)],
    "rename": [([(3, 13), (3.6, 10.4), (10.6, 3.4), (12.6, 5.4), (5.6, 12.4)], True), ([(9.3, 4.7), (11.3, 6.7)], False)],
    "delete": [([(4.5, 4.5), (11.5, 11.5)], False), ([(11.5, 4.5), (4.5, 11.5)], False)],
}


def _alpha(color: QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def _role(role: str) -> QColor:
    return QColor(theme.color(role))


def plain_tip(text: str) -> str:
    """``text`` (keys, values, paths of the data) as a tooltip shown as it is: Qt shows a tooltip
    that looks like HTML as rich text (``"<b>x</b>"`` would lose its tags)."""
    return "<p style='white-space:pre-wrap'>" + html.escape(display_text(text), quote=False) + "</p>"


# ==================================================================================
# row layout + delegate
# ==================================================================================
@dataclass
class RowParts:
    """Where the parts of one row are (paint and hit tests use the same layout)."""

    caret: QRect | None = None
    label: QRect = field(default_factory=QRect)
    label_text: str = ""
    label_full: str = ""
    chips: list[tuple[QRect, str, str, str]] = field(default_factory=list)
    meta: list[tuple[QRect, str, str]] = field(default_factory=list)
    badges: list[tuple[QRect, str, str, str]] = field(default_factory=list)
    sample: QRect | None = None
    sample_text: str = ""
    sample_full: str = ""
    actions: list[tuple[QRect, str]] = field(default_factory=list)
    more: QRect | None = None
    indent_x: int = 0


class MapDelegate(QStyledItemDelegate):
    """Paints the rows of the structure tree (see the module docstring) and edits a row's name
    inline (the editor commits through the view, never through the model)."""

    def __init__(self, tree: MapTree) -> None:
        super().__init__(tree)
        self._tree = tree
        self.editing: tuple[QLineEdit, int | None] | None = None   # the open rename editor and its node id
        self.update_fonts()

    # -- fonts / metrics ----------------------------------------------------------------
    def update_fonts(self) -> None:
        base = QFont(self._tree.font())
        size = base.pointSizeF() if base.pointSizeF() > 0 else 10.0
        self.f_key = QFont(base)
        self.f_key.setWeight(QFont.Weight.Medium)
        self.f_root = QFont(base)
        self.f_root.setWeight(QFont.Weight.DemiBold)
        ui = QFont(QApplication.font())
        ui_size = ui.pointSizeF() if ui.pointSizeF() > 0 else 9.0
        self.f_elem = QFont(ui)
        self.f_elem.setPointSizeF(max(6.0, size * 0.95))
        self.f_elem.setItalic(True)
        self.f_chip = QFont(base)
        self.f_chip.setPointSizeF(max(6.0, size * 0.82))
        self.f_chip.setWeight(QFont.Weight.Medium)
        self.f_small = QFont(ui)
        self.f_small.setPointSizeF(max(6.0, min(ui_size, size) * 0.9))
        self.f_sample = QFont(base)
        self.f_sample.setPointSizeF(max(6.0, size * 0.92))
        self.fm_key = QFontMetrics(self.f_key)
        self.fm_root = QFontMetrics(self.f_root)
        self.fm_elem = QFontMetrics(self.f_elem)
        self.fm_chip = QFontMetrics(self.f_chip)
        self.fm_small = QFontMetrics(self.f_small)
        self.fm_sample = QFontMetrics(self.f_sample)
        self.row_h = max(26, QFontMetrics(base).height() + 12)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802 (Qt name)
        return QSize(200, self.row_h)

    # -- layout -----------------------------------------------------------------------------
    @staticmethod
    def indent(depth: int, width: int) -> int:
        """The indentation of a row at ``depth`` in a row ``width`` px wide: :data:`INDENT` per
        level (the prototype's), then :data:`DEEP_STEP` per level once it passed
        :data:`DEEP_SOFT` of the width, never more than :data:`DEEP_HARD` of it -- the rows of a
        deep tree (40 nested ``children``) keep their names on screen (the view has no horizontal
        scrolling); the indent guides follow the same steps."""
        full = depth * INDENT
        soft = max(INDENT * 4, int(width * DEEP_SOFT))
        if full <= soft:
            return full
        k = soft // INDENT
        return min(k * INDENT + (depth - k) * DEEP_STEP, max(soft, int(width * DEEP_HARD)))

    def layout(self, row: MapRow, info: RowInfo | None, rect: QRect, *, with_actions: bool) -> RowParts:
        parts = RowParts()
        x = rect.left() + PAD_LEFT + self.indent(row.depth, rect.width())
        parts.indent_x = x
        cy = rect.center().y()
        right = rect.right() - PAD_LEFT
        if row.kind == "more" or info is None:
            text = info.more_text if info is not None else ""
            w = self.fm_small.horizontalAdvance(text) + 4
            parts.more = QRect(x + CARET_W + GAP, rect.top(), min(w, max(0, right - x - CARET_W - GAP)), rect.height())
            return parts
        if info.has_children:
            parts.caret = QRect(x, cy - CARET_W // 2, CARET_W, CARET_W)
        x += CARET_W + GAP
        # the fixed-width parts (chips, meta, badges) and the right side (actions)
        chip_h = self.fm_chip.height() + 4
        chips = []
        for glyph, tip, role in info.chips:
            w = self.fm_chip.horizontalAdvance(glyph) + 10
            chips.append((w, glyph, tip, role))
        metas = [(self.fm_small.horizontalAdvance(text), text, tip) for text, tip in info.meta]
        badge_h = self.fm_small.height() + 4
        badges = [(self.fm_small.horizontalAdvance(text) + 14, text, tip, role) for text, tip, role in info.badges]
        actions = info.actions if with_actions else []
        act_w = len(actions) * ACTION_W + max(0, len(actions) - 1) * ACTION_GAP
        if actions:
            ax = right - act_w + 1
            for name in actions:
                parts.actions.append((QRect(ax, cy - ACTION_W // 2, ACTION_W, ACTION_W), name))
                ax += ACTION_W + ACTION_GAP
            right -= act_w + GAP
        fixed = 0
        if chips:
            fixed += sum(c[0] for c in chips) + CHIP_GAP * (len(chips) - 1) + GAP
        if metas:
            fixed += sum(m[0] for m in metas) + GAP * len(metas)
        if badges:
            fixed += sum(b[0] for b in badges) + BADGE_GAP * (len(badges) - 1) + GAP
        fm = self.fm_root if info.style == "root" else self.fm_elem if info.style == "elem" else self.fm_key
        full_w = fm.horizontalAdvance(info.label)
        label_w = max(0, min(full_w, right - x - fixed))
        parts.label = QRect(x, rect.top(), label_w, rect.height())
        parts.label_full = info.label
        parts.label_text = info.label if label_w >= full_w else fm.elidedText(info.label, Qt.TextElideMode.ElideRight,
                                                                             label_w)
        x += label_w + GAP
        for w, glyph, tip, role in chips:
            parts.chips.append((QRect(x, cy - chip_h // 2, w, chip_h), glyph, tip, role))
            x += w + CHIP_GAP
        if chips:
            x += GAP - CHIP_GAP
        for w, text, tip in metas:
            parts.meta.append((QRect(x, rect.top(), w, rect.height()), text, tip))
            x += w + GAP
        for w, text, tip, role in badges:
            parts.badges.append((QRect(x, cy - badge_h // 2, w, badge_h), text, tip, role))
            x += w + BADGE_GAP
        if badges:
            x += GAP - BADGE_GAP
        if info.sample and right - x > 8:
            sw = right - x
            parts.sample = QRect(x, rect.top(), sw, rect.height())
            parts.sample_full = info.sample
            parts.sample_text = self.fm_sample.elidedText(info.sample, Qt.TextElideMode.ElideRight, sw)
        return parts

    # -- painting -----------------------------------------------------------------------------
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row: MapRow | None = index.data(ROW_ROLE)
        if row is None:
            return
        info: RowInfo | None = index.data(INFO_ROLE)
        tree = self._tree
        rect = QRect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = tree.hover_row == index.row()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setClipRect(rect)
        # background
        if selected:
            bg = _role("selection" if tree.hasFocus() else "selection_inactive")
            painter.fillRect(rect, bg)
        elif hover and not tree.drag_active():
            painter.fillRect(rect, _role("hover"))
        drop = tree.drop_target()
        if drop is not None and drop[0] == index.row() and drop[1] == "inside":
            box = QRectF(rect).adjusted(1.5, 1.5, -1.5, -1.5)
            painter.setBrush(_role("accent_soft"))
            painter.setPen(QPen(_role("accent"), 1.5))
            painter.drawRoundedRect(box, 6, 6)
        # indent guides
        guide = QPen(_role("guide"), 1)
        painter.setPen(guide)
        last = None
        for k in range(row.depth):
            gx = rect.left() + PAD_LEFT + self.indent(k, rect.width()) + CARET_W // 2
            if gx != last:
                painter.drawLine(gx, rect.top(), gx, rect.bottom())
                last = gx
        dim = row.kind == "node" and (row.node_id == tree.drag_node_id() or row.node_id == tree.cut_id())
        if dim:
            painter.setOpacity(0.4)
        parts = self.layout(row, info, rect, with_actions=tree.shows_actions(index.row()))
        if row.kind == "more":
            painter.setFont(self.f_small)
            painter.setPen(_role("accent"))
            if parts.more is not None:
                painter.drawText(parts.more, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                                 info.more_text if info is not None else "")
        elif info is not None:
            self._paint_node(painter, info, parts, tree, index.row())
        painter.setOpacity(1.0)
        if drop is not None and drop[0] == index.row() and drop[1] in ("before", "after"):
            y = rect.top() + 1 if drop[1] == "before" else rect.bottom() - 1
            painter.setPen(QPen(_role("accent"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(parts.indent_x, y, rect.right() - PAD_LEFT, y)
        painter.restore()

    def _paint_node(self, painter: QPainter, info: RowInfo, parts: RowParts, tree: MapTree, row_index: int) -> None:
        if parts.caret is not None:
            hot = tree.hover_part == (row_index, "caret", None)
            if hot:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_role("accent_soft"))
                painter.drawRoundedRect(QRectF(parts.caret), 5, 5)
            pts = [(6, 4), (10, 8), (6, 12)] if not info.open else [(4, 6), (8, 10), (12, 6)]
            self._stroke(painter, parts.caret, [(pts, False)], _role("accent" if hot else "muted"), 1.7, 14)
        # label
        if info.style == "root":
            painter.setFont(self.f_root)
            painter.setPen(_role("fg"))
        elif info.style == "elem":
            painter.setFont(self.f_elem)
            painter.setPen(_role("muted"))
        else:
            painter.setFont(self.f_key)
            painter.setPen(_role("fg"))
        painter.drawText(parts.label, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), parts.label_text)
        # type chips: tinted rounded boxes
        painter.setFont(self.f_chip)
        tint = 46 if theme.is_dark() else 31
        for r, glyph, _tip, role in parts.chips:
            c = _role(role)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_alpha(c, tint))
            painter.drawRoundedRect(QRectF(r), 4, 4)
            painter.setPen(c)
            painter.drawText(r, int(Qt.AlignmentFlag.AlignCenter), glyph)
        # meta
        painter.setFont(self.f_small)
        painter.setPen(_role("faint"))
        for r, text, _tip in parts.meta:
            painter.drawText(r, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        # badges: pills with a border at 35 % of the role colour
        for r, text, _tip, role in parts.badges:
            c = _role(role)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(_alpha(c, 89), 1))
            rr = QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.drawRoundedRect(rr, rr.height() / 2, rr.height() / 2)
            painter.setPen(c)
            painter.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)
        if parts.sample is not None:
            painter.setFont(self.f_sample)
            painter.setPen(_role("faint"))
            painter.drawText(parts.sample, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                             parts.sample_text)
        for r, name in parts.actions:
            hot = tree.hover_part == (row_index, "action", name)
            color = _role("muted")
            if hot:
                danger = name == "delete"
                color = _role("danger" if danger else "accent")
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_alpha(_role("danger"), 31) if danger else _role("accent_soft"))
                painter.drawRoundedRect(QRectF(r), 6, 6)
            self._stroke(painter, r, _ICONS[name], color, 1.5, 15)

    @staticmethod
    def _stroke(painter: QPainter, box: QRect, shapes: list[tuple[list[tuple[float, float]], bool]], color: QColor,
                width: float, size: int) -> None:
        """Stroke icon polylines given in a 16x16 box, scaled to ``size`` px centred in ``box``."""
        scale = size / 16.0
        ox = box.left() + (box.width() - size) / 2.0
        oy = box.top() + (box.height() - size) / 2.0
        pen = QPen(color, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for pts, closed in shapes:
            path = QPainterPath(QPointF(ox + pts[0][0] * scale, oy + pts[0][1] * scale))
            for px, py in pts[1:]:
                path.lineTo(ox + px * scale, oy + py * scale)
            if closed:
                path.closeSubpath()
            painter.drawPath(path)

    # -- inline rename ------------------------------------------------------------------------------
    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> QWidget:  # noqa: N802
        editor = QLineEdit(parent)
        editor.setObjectName("mapview_rename")
        editor.setAccessibleName(t("gui.mapview.rename.name"))
        editor.setFont(self.f_key)
        accent = _role("accent").name()
        editor.setStyleSheet(f"QLineEdit {{ border: 1px solid {accent}; border-radius: 5px; padding: 0 6px; }}")
        row = index.data(ROW_ROLE)
        self.editing = (editor, int(row.node_id) if row is not None and row.kind == "node" else None)
        return editor

    def destroyEditor(self, editor: QWidget, index: QModelIndex) -> None:  # noqa: N802
        if self.editing is not None and self.editing[0] is editor:
            self.editing = None
        super().destroyEditor(editor, index)

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:  # noqa: N802
        if isinstance(editor, QLineEdit):
            editor.setText(str(index.data(int(Qt.ItemDataRole.EditRole)) or ""))
            editor.selectAll()

    def setModelData(self, editor: QWidget, model: Any, index: QModelIndex) -> None:  # noqa: N802
        if self.editing is None or self.editing[0] is not editor:
            return                          # committed or dropped already (MapView.finish_rename / cancel_rename)
        row = index.data(ROW_ROLE)
        if isinstance(editor, QLineEdit) and row is not None and row.kind == "node":
            self._tree.rename_committed(int(row.node_id), editor.text())

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # noqa: N802
        row = index.data(ROW_ROLE)
        info = index.data(INFO_ROLE)
        rect = QRect(option.rect)
        if row is None:
            editor.setGeometry(rect)
            return
        parts = self.layout(row, info, rect, with_actions=False)
        w = max(MIN_EDITOR_W, min(MAX_EDITOR_W, parts.label.width() + 24))
        h = min(rect.height() - 4, max(24, self.fm_key.height() + 8))
        editor.setGeometry(QRect(parts.label.left() - 4, rect.center().y() - h // 2, w, h))


# ==================================================================================
# the tree widget (mouse: selection, caret, actions, drag & drop; keys go to the view)
# ==================================================================================
class MapTree(QTreeView):
    """The structure tree (a ``QTreeView`` over the flat :class:`MapModel`)."""

    def __init__(self, view: MapView) -> None:
        super().__init__(view)
        self._view = view
        self.setObjectName("mapview_tree")
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setExpandsOnDoubleClick(False)
        self.setIndentation(0)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setAllColumnsShowFocus(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFont(mono_font(view.session))
        self.setModel(view.model)
        self.delegate = MapDelegate(self)
        self.setItemDelegate(self.delegate)
        self.header().setStretchLastSection(True)
        self.hover_row = -1
        self.hover_part: tuple | None = None
        self._press: tuple[QPoint, int] | None = None          # (pos, node id) of a press that may start a drag
        self._press_action: tuple[int, str] | None = None      # (row, action) pressed
        self._drag_id: int | None = None
        self._drop: tuple[int, str] | None = None               # (row, zone)
        self._drop_ids: tuple[int, int, str] | None = None      # (drag id, target id, zone)
        self._mouse = QPoint()
        self._auto = QTimer(self)
        self._auto.setInterval(AUTO_SCROLL_MS)
        self._auto.timeout.connect(self._auto_scroll)

    # -- state used by the delegate ------------------------------------------------------
    def drag_active(self) -> bool:
        return self._drag_id is not None

    def drag_node_id(self) -> int | None:
        return self._drag_id

    def drop_target(self) -> tuple[int, str] | None:
        """``(row, zone)`` of the running drag (``None``: no drag or no zone)."""
        return self._drop if self._drag_id is not None else None

    def cut_id(self) -> int | None:
        return self._view.state.cut_id

    def shows_actions(self, row: int) -> bool:
        if self._drag_id is not None:
            return False
        return row == self.hover_row or (self.hasFocus() and row == self.currentIndex().row())

    def row_height(self) -> int:
        return self.delegate.row_h

    # -- hit tests ------------------------------------------------------------------------------
    def part_at(self, pos: QPoint) -> tuple[int, str, Any]:
        """``(row, part, payload)`` under ``pos`` (viewport coordinates): parts ``caret``,
        ``action`` (payload: the action), ``chip`` / ``meta`` / ``badge`` (payload: the tooltip),
        ``label`` / ``sample`` (payload: the full text), ``more``, ``row``; ``(-1, "", None)``
        outside the rows."""
        index = self.indexAt(pos)
        if not index.isValid():
            return -1, "", None
        row = index.data(ROW_ROLE)
        info = index.data(INFO_ROLE)
        rect = self.visualRect(index)
        parts = self.delegate.layout(row, info, rect, with_actions=self.shows_actions(index.row()))
        r = index.row()
        if parts.more is not None and parts.more.contains(pos):
            return r, "more", None
        if parts.caret is not None and parts.caret.contains(pos):
            return r, "caret", None
        for rect_, name in parts.actions:
            if rect_.contains(pos):
                return r, "action", name
        for rect_, _glyph, tip, _role in parts.chips:
            if rect_.contains(pos):
                return r, "chip", tip
        for rect_, _text, tip in parts.meta:
            if rect_.contains(pos):
                return r, "meta", tip
        for rect_, _text, tip, _role in parts.badges:
            if rect_.contains(pos):
                return r, "badge", tip
        if parts.label.contains(pos):
            return r, "label", parts.label_full if parts.label_text != parts.label_full else ""
        if parts.sample is not None and parts.sample.contains(pos):
            return r, "sample", parts.sample_full if parts.sample_text != parts.sample_full else ""
        return r, "row", None

    def row_rect(self, row: int) -> QRect:
        return self.visualRect(self.model().index(row, 0))

    def action_rect(self, row: int, name: str) -> QRect | None:
        """Where the action button ``name`` of ``row`` is painted while the row shows its
        actions (tests, keyboard focus)."""
        index = self.model().index(row, 0)
        if not index.isValid():
            return None
        parts = self.delegate.layout(index.data(ROW_ROLE), index.data(INFO_ROLE), self.visualRect(index),
                                     with_actions=True)
        for rect, n in parts.actions:
            if n == name:
                return rect
        return None

    def caret_rect(self, row: int) -> QRect | None:
        index = self.model().index(row, 0)
        if not index.isValid():
            return None
        parts = self.delegate.layout(index.data(ROW_ROLE), index.data(INFO_ROLE), self.visualRect(index),
                                     with_actions=False)
        return parts.caret

    def label_rect(self, row: int) -> QRect | None:
        """Where the row's name (or the "more" text) is painted."""
        index = self.model().index(row, 0)
        if not index.isValid():
            return None
        parts = self.delegate.layout(index.data(ROW_ROLE), index.data(INFO_ROLE), self.visualRect(index),
                                     with_actions=False)
        return parts.more if parts.more is not None else parts.label

    # -- mouse ------------------------------------------------------------------------------
    def _set_hover(self, row: int, part: tuple | None) -> None:
        if row != self.hover_row or part != self.hover_part:
            old = self.hover_row
            self.hover_row = row
            self.hover_part = part
            vp = self.viewport()
            for r in {old, row}:
                if r >= 0:
                    vp.update(self.row_rect(r))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if not index.isValid():
            event.accept()
            return
        self.setCurrentIndex(index)
        self._view.row_selected(index.row())
        row = index.data(ROW_ROLE)
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        self._set_hover(index.row(), None)        # a press without a move before it: its row shows the actions
        r, part, payload = self.part_at(pos)
        if part == "more":
            self._view.state.show_more(row.node_id)
        elif part == "caret":
            self._view.state.toggle(row.node_id)
        elif part == "action":
            self._press_action = (r, str(payload))
        elif row is not None and row.kind == "node" and not row.node.is_root and not row.node.is_elem:
            self._press = (QPoint(pos), int(row.node_id))
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        pos = event.position().toPoint()
        left = bool(event.buttons() & Qt.MouseButton.LeftButton)
        if self._drag_id is not None:
            if not left:
                self.cancel_drag()          # the release happened elsewhere
                self._set_hover(*self._hover_at(pos))
                event.accept()
                return
            self._mouse = QPoint(pos)
            self._update_drag(pos)
            margin = max(4, self.row_height() // 2)
            near = pos.y() < margin or pos.y() >= self.viewport().height() - margin
            if near and not self._auto.isActive():
                self._auto.start()
            elif not near:
                self._auto.stop()
            event.accept()
            return
        if self._press is not None:
            if not left:
                self._press = None
            elif (pos - self._press[0]).manhattanLength() >= QApplication.startDragDistance():
                node_id = self._press[1]
                self._press = None
                self._start_drag(node_id, pos)
            event.accept()
            return
        self._set_hover(*self._hover_at(pos))
        event.accept()

    def _hover_at(self, pos: QPoint) -> tuple[int, tuple | None]:
        r, part, payload = self.part_at(pos)
        return r, ((r, part, payload) if part in ("caret", "action") else None)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        pos = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_id is not None:
                self._finish_drag()
            elif self._press_action is not None:
                pressed = self._press_action
                self._press_action = None
                r, part, payload = self.part_at(pos)
                if part == "action" and (r, payload) == pressed:
                    self._view.row_action(r, str(payload))
            self._press = None
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        r, part, _payload = self.part_at(event.position().toPoint())
        if r >= 0 and part in ("row", "label", "chip", "meta", "badge", "sample"):
            row = self.model().row(r)
            if row is not None and row.kind == "node":
                self._view.state.toggle(row.node_id)
        event.accept()

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt name)
        if self._drag_id is None:
            self._set_hover(-1, None)
        super().leaveEvent(event)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        self.cancel_drag()                  # another view was shown in the pane: no drag survives it
        self._set_hover(-1, None)
        super().hideEvent(event)

    def drag_is_stale(self) -> bool:
        """A drag whose button is no longer down (released outside the window / view)."""
        return self._drag_id is not None and not (QApplication.mouseButtons() & Qt.MouseButton.LeftButton)

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().focusOutEvent(event)
        self.viewport().update()

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().focusInEvent(event)
        self.viewport().update()

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.ToolTip:
            pos = event.pos()
            r, part, payload = self.part_at(pos)
            text = ""
            if part == "action":
                text = t(_ACTION_TIPS[str(payload)])
            elif part == "caret":
                row = self.model().row(r)
                is_open = row is not None and row.node is not None and row.node.open
                text = t("gui.mapview.caret.close") if is_open else t("gui.mapview.caret.open")
            elif part in ("chip", "meta", "badge", "label", "sample"):
                text = str(payload or "")
            if text:
                QToolTip.showText(event.globalPos(), plain_tip(text), self.viewport())
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        if event.type() == QEvent.Type.Leave and self._drag_id is None:
            self._set_hover(-1, None)       # also when the pointer leaves over the scroll bar
        return super().viewportEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        if self._view.handle_key(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        pos = event.pos()
        if event.reason() == event.Reason.Mouse:
            index = self.indexAt(pos)
            if not index.isValid():
                event.accept()              # empty space: no menu (not the one of a row off screen)
                return
            self.setCurrentIndex(index)
            self._view.row_selected(index.row())
        self._view.popup_menu(event.globalPos())
        event.accept()

    # -- drag & drop -----------------------------------------------------------------------------
    def _start_drag(self, node_id: int, pos: QPoint) -> None:
        why = self._view.state.can_operate()
        if why is not None:
            self._view.toast(why)
            return
        d = self._view.state.designer
        if d is None or not d.can_drag(node_id):
            return
        self._drag_id = node_id
        self._mouse = QPoint(pos)
        self._set_hover(-1, None)
        self._update_drag(pos)
        self.viewport().update()

    def _drag_row_frac(self, pos: QPoint) -> tuple[int, float]:
        """The row under ``pos`` and where in it (0..1); ``(-1, 0.0)`` off the rows -- the empty
        space below them, outside the view (another pane, above / below it while it scrolls): no
        drop there, like the prototype, which drops only on a row."""
        if not self.viewport().rect().contains(pos):
            return -1, 0.0
        index = self.indexAt(pos)
        if not index.isValid():
            return -1, 0.0
        rect = self.visualRect(index)
        frac = (pos.y() - rect.top()) / max(1, rect.height())
        return index.row(), min(1.0, max(0.0, frac))

    def _update_drag(self, pos: QPoint) -> None:
        d = self._view.state.designer
        drop: tuple[int, str] | None = None
        ids: tuple[int, int, str] | None = None
        row_i, frac = self._drag_row_frac(pos)
        row = self.model().row(row_i) if row_i >= 0 else None
        if d is not None and row is not None and row.kind == "node" and self._drag_id is not None:
            zone = d.zone_for(row.node_id, self._drag_id, frac)
            if zone is not None:
                drop = (row_i, zone)
                ids = (self._drag_id, int(row.node_id), zone)
        if drop != self._drop:
            self._drop = drop
            self.viewport().update()
        self._drop_ids = ids
        shape = Qt.CursorShape.DragMoveCursor if drop is not None else Qt.CursorShape.ForbiddenCursor
        if self.viewport().cursor().shape() != shape:
            self.viewport().setCursor(shape)

    def _auto_scroll(self) -> None:
        if self._drag_id is None:
            self._auto.stop()
            return
        bar = self.verticalScrollBar()
        pos = self._mouse
        margin = max(4, self.row_height() // 2)
        step = self.row_height()
        if pos.y() < margin:
            bar.setValue(bar.value() - step)
        elif pos.y() >= self.viewport().height() - margin:
            bar.setValue(bar.value() + step)
        else:
            self._auto.stop()
        self._update_drag(pos)

    def cancel_drag(self) -> None:
        """Stop a running drag without dropping (Esc)."""
        if self._drag_id is None and self._press is None:
            return
        self._press = None
        self._drag_id = None
        self._drop = None
        self._drop_ids = None
        self._auto.stop()
        self.viewport().unsetCursor()
        self.viewport().update()

    def _finish_drag(self) -> None:
        ids = self._drop_ids
        self._drag_id = None
        self._drop = None
        self._drop_ids = None
        self._auto.stop()
        self.viewport().unsetCursor()
        self.viewport().update()
        if ids is not None:
            self._view.drop(*ids)

    # -- inline rename ------------------------------------------------------------------------------
    def rename_committed(self, node_id: int, text: str) -> None:
        """The inline editor committed (Enter / focus left): renamed after the editor closed --
        only in the tree it was typed for (a Full scan / an opened mapping may reuse the id)."""
        designer = self._view.state.designer
        QTimer.singleShot(0, self, lambda: self._view.commit_rename(node_id, text, designer=designer))


# ==================================================================================
# preview
# ==================================================================================
def _u16(text: str, i: int) -> int:
    head = text[:i]
    return len(head) + sum(1 for ch in head if ord(ch) > 0xFFFF)


class _Highlighter(QSyntaxHighlighter):
    """Colours of the text view for the preview (``textview.line_tokens``); marker lines
    ("… davomi eksportda") faint and italic."""

    def __init__(self, document: Any) -> None:
        super().__init__(document)
        self._formats: dict[str, QTextCharFormat] = {}

    def refresh(self) -> None:
        self._formats = {}
        self.rehighlight()

    def _format(self, role: str) -> QTextCharFormat:
        fmt = self._formats.get(role)
        if fmt is None:
            fmt = QTextCharFormat()
            if role == "marker":
                fmt.setForeground(_role("faint"))
                fmt.setFontItalic(True)
            else:
                fmt.setForeground(_role({"punct": "muted"}.get(role, role)))
            self._formats[role] = fmt
        return fmt

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt name)
        if not text:
            return
        astral = any(ord(ch) > 0xFFFF for ch in text)
        if text.lstrip(" ").startswith("…"):
            self.setFormat(0, _u16(text, len(text)) if astral else len(text), self._format("marker"))
            return
        from jsonreshape.gui.textview import line_tokens

        for s, e, role in line_tokens(text):
            if astral:
                us, ue = _u16(text, s), _u16(text, e)
            else:
                us, ue = s, e
            self.setFormat(us, ue - us, self._format(role))


_DIGITS = re.compile(r"[0-9]+")


class RecordSpin(QAbstractSpinBox):
    """The preview's record number ``#N`` (0-based) for any record count: ``QSpinBox`` holds
    32-bit values only, a document may have more than 2**31 - 1 records. The value is a Python
    ``int`` in ``[0, maximum]``; ``valueChanged(object)`` when a new value is committed (Enter,
    leaving the field, the arrows / keys / wheel, :meth:`setValue`). A number above the maximum
    is clamped; text that is not a number is refused while typing."""

    valueChanged = Signal(object)  # noqa: N815 (Qt name)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0
        self._max = 0
        self.setKeyboardTracking(False)
        self.setAccelerated(True)
        self.lineEdit().setText(self.textFromValue(0))
        self.editingFinished.connect(self._commit)

    # -- value ----------------------------------------------------------------------------
    def value(self) -> int:
        return self._value

    def minimum(self) -> int:
        return 0

    def maximum(self) -> int:
        return self._max

    def setRange(self, _minimum: int, maximum: int) -> None:  # noqa: N802 (Qt name)
        """The range is ``[0, maximum]`` (the minimum is always 0)."""
        self._max = max(0, int(maximum))
        if self._value > self._max:
            self.setValue(self._max)
        else:
            self._show()
        self.updateGeometry()

    def setValue(self, value: int) -> None:  # noqa: N802 (Qt name)
        value = min(max(0, int(value)), self._max)
        changed = value != self._value
        self._value = value
        self._show()
        if changed:
            self.valueChanged.emit(value)

    def _show(self) -> None:
        text = self.textFromValue(self._value)
        if self.lineEdit().text() != text:
            self.lineEdit().setText(text)

    @staticmethod
    def textFromValue(value: int) -> str:  # noqa: N802 (Qt name)
        return f"#{int(value)}"

    @staticmethod
    def valueFromText(text: str) -> int | None:  # noqa: N802 (Qt name)
        """The number of ``"#N"`` / ``"N"`` (``None``: not a number)."""
        body = text.strip()
        if body.startswith("#"):
            body = body[1:].strip()
        return int(body) if _DIGITS.fullmatch(body) else None

    def _commit(self) -> None:
        value = self.valueFromText(self.lineEdit().text())
        if value is not None:
            self.setValue(value)
        self._show()

    # -- QAbstractSpinBox -------------------------------------------------------------------
    def validate(self, text: str, pos: int) -> object:
        body = text.strip()
        if body.startswith("#"):
            body = body[1:].strip()
        if not body:
            return QValidator.State.Intermediate, text, pos
        if not _DIGITS.fullmatch(body):
            return QValidator.State.Invalid, text, pos
        state = QValidator.State.Acceptable if int(body) <= self._max else QValidator.State.Intermediate
        return state, text, pos

    def stepBy(self, steps: int) -> None:  # noqa: N802 (Qt name)
        typed = self.valueFromText(self.lineEdit().text())
        base = self._value if typed is None else min(typed, self._max)
        self.setValue(base + int(steps))
        self.selectAll()

    def stepEnabled(self) -> QAbstractSpinBox.StepEnabledFlag:  # noqa: N802 (Qt name)
        flags = QAbstractSpinBox.StepEnabledFlag.StepNone
        if self._value > 0:
            flags |= QAbstractSpinBox.StepEnabledFlag.StepDownEnabled
        if self._value < self._max:
            flags |= QAbstractSpinBox.StepEnabledFlag.StepUpEnabled
        return flags

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        hint = super().sizeHint()
        fm = self.fontMetrics()
        width = fm.horizontalAdvance(self.textFromValue(max(self._max, 9999))) + fm.horizontalAdvance("0") + 4
        opt = QStyleOptionSpinBox()
        self.initStyleOption(opt)
        size = self.style().sizeFromContents(QStyle.ContentsType.CT_SpinBox, opt, QSize(width, hint.height()), self)
        return QSize(max(40, size.width()), hint.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return self.sizeHint()


class PreviewPane(QFrame):
    """The right pane: Natija / Asl, the record navigator, the JSON, the footer."""

    mode_changed = Signal(str)          # "out" | "src"
    record_requested = Signal(object)   # 0-based record number (a Python int: any size)

    def __init__(self, view: MapView) -> None:
        super().__init__(view)
        self.setObjectName("mapview_preview")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(10, 6, 10, 6)
        head.setSpacing(8)
        self.title = QLabel(t("gui.mapview.pane.preview").upper(), self)
        head.addWidget(self.title)
        self.out_button = QToolButton(self)
        self.out_button.setObjectName("mapview_preview_out")
        self.out_button.setText(t("gui.mapview.preview.out"))
        self.out_button.setCheckable(True)
        self.out_button.setChecked(True)
        self.src_button = QToolButton(self)
        self.src_button.setObjectName("mapview_preview_src")
        self.src_button.setText(t("gui.mapview.preview.src"))
        self.src_button.setCheckable(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.out_button)
        self.mode_group.addButton(self.src_button)
        for b in (self.out_button, self.src_button):
            b.setToolTip(t("gui.mapview.preview.kind_tip"))
            b.setAutoRaise(True)
            head.addWidget(b)
        self.out_button.toggled.connect(lambda on: on and self.mode_changed.emit("out"))
        self.src_button.toggled.connect(lambda on: on and self.mode_changed.emit("src"))
        head.addStretch(1)
        self.nav = QWidget(self)
        nav = QHBoxLayout(self.nav)
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(6)
        self.prev_button = QToolButton(self.nav)
        self.prev_button.setObjectName("mapview_prev")
        self.prev_button.setText("‹")
        self.prev_button.setToolTip(t("gui.mapview.preview.prev"))
        self.prev_button.clicked.connect(lambda: self.record_requested.emit(self.record_spin.value() - 1))
        nav.addWidget(self.prev_button)
        self.record_spin = RecordSpin(self.nav)
        self.record_spin.setObjectName("mapview_record")
        self.record_spin.setToolTip(t("gui.mapview.preview.number"))
        self.record_spin.setAccessibleName(t("gui.mapview.preview.number"))
        self.record_spin.valueChanged.connect(self._spin_changed)
        nav.addWidget(self.record_spin)
        self.total_label = QLabel(self.nav)
        nav.addWidget(self.total_label)
        self.next_button = QToolButton(self.nav)
        self.next_button.setObjectName("mapview_next")
        self.next_button.setText("›")
        self.next_button.setToolTip(t("gui.mapview.preview.next"))
        self.next_button.clicked.connect(lambda: self.record_requested.emit(self.record_spin.value() + 1))
        nav.addWidget(self.next_button)
        head.addWidget(self.nav)
        lay.addLayout(head)
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        self.note_label = QLabel(self)
        self.note_label.setObjectName("mapview_preview_note")
        self.note_label.setWordWrap(True)
        self.note_label.setContentsMargins(14, 8, 14, 0)
        self.note_label.setTextFormat(Qt.TextFormat.PlainText)
        self.note_label.hide()
        lay.addWidget(self.note_label)
        self.edit = QPlainTextEdit(self)
        self.edit.setObjectName("mapview_preview_text")
        self.edit.setReadOnly(True)
        self.edit.setUndoRedoEnabled(False)
        self.edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.edit.setFrameShape(QFrame.Shape.NoFrame)
        self.edit.setFont(mono_font(view.session))
        self.highlighter = _Highlighter(self.edit.document())
        lay.addWidget(self.edit, 1)
        line2 = QFrame(self)
        line2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line2)
        self.foot = QLabel(self)
        self.foot.setObjectName("mapview_preview_foot")
        self.foot.setWordWrap(True)
        self.foot.setContentsMargins(10, 6, 10, 6)
        lay.addWidget(self.foot)
        self.apply_style()

    def apply_style(self) -> None:
        for label, role in ((self.title, "muted"), (self.foot, "faint"), (self.total_label, "muted")):
            pal = label.palette()
            pal.setColor(QPalette.ColorRole.WindowText, _role(role))
            label.setPalette(pal)
        f = self.title.font()
        f.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(f)
        self.highlighter.refresh()

    def mode(self) -> str:
        return "src" if self.src_button.isChecked() else "out"

    def set_mode(self, mode: str) -> None:
        (self.src_button if mode == "src" else self.out_button).setChecked(True)

    def _spin_changed(self, value: int) -> None:
        self.record_requested.emit(int(value))

    def set_navigator(self, index: int, count: int, *, visible: bool, members: bool) -> None:
        self.nav.setVisible(visible and count > 0)
        if count <= 0:
            return
        self.record_spin.blockSignals(True)
        try:
            self.record_spin.setRange(0, max(0, count - 1))
            self.record_spin.setValue(min(max(0, index), count - 1))
        finally:
            self.record_spin.blockSignals(False)
        key = "gui.mapview.preview.total_members" if members else "gui.mapview.preview.total"
        self.total_label.setText(t(key, count=format_count(count)))
        self.prev_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < count - 1)

    def show_text(self, text: str) -> None:
        self.note_label.hide()
        if self.edit.toPlainText() == text:
            return
        bar = self.edit.verticalScrollBar()
        keep = bar.value()
        self.edit.setPlainText(text)
        bar.setValue(min(keep, bar.maximum()))

    def show_note(self, text: str, *, error: bool = False) -> None:
        pal = self.note_label.palette()
        pal.setColor(QPalette.ColorRole.WindowText, _role("danger" if error else "muted"))
        self.note_label.setPalette(pal)
        self.note_label.setText(text)
        self.note_label.show()
        self.edit.setPlainText("")

    def text(self) -> str:
        return self.edit.toPlainText()

    def note(self) -> str:
        return self.note_label.text() if not self.note_label.isHidden() else ""


# ==================================================================================
# the view
# ==================================================================================
class MapView(ViewBase):
    """The structure builder of a session (see the module docstring)."""

    view_id = "mapping"

    history_changed = Signal()          # undo / redo availability changed (the main window's actions)
    preview_shown = Signal()            # a preview result was put on screen (tests)
    tree_shown = Signal()               # the rows were rebuilt (tests)

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(session, parent)
        self.state: MappingState = state_for(session)
        self.model = MapModel(self)
        self.model.rename_requested.connect(self.commit_rename)
        self._dead = False
        self._activated = False
        self._current_id: int | None = None
        self._rec_index = 0
        self._preview_dirty = True
        self._preview_task: Any = None
        self._preview_serial = 0
        self._value_cache: tuple | None = None       # (key, value, member key) of a small record
        self._menu: QMenu | None = None
        self._zoom = 0
        self._build_ui()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._run_preview)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(TOAST_MS)
        self._toast_timer.timeout.connect(self._hide_toast)
        st = self.state
        st.changed.connect(self._on_state_changed)
        st.status_changed.connect(self._on_status)
        st.message.connect(self._on_message)
        st.replaced.connect(self._on_replaced)
        s = session
        s.current_changed.connect(self._on_current_changed)
        s.opened.connect(self._on_opened)
        s.index_finished.connect(self._on_index_finished)
        s.state_changed.connect(self._on_session_state)
        data_changed = getattr(s, "data_changed", None)
        if data_changed is not None:
            data_changed.connect(self._on_data_changed)
        theme.notifier().theme_changed.connect(self._on_theme)
        self._rebuild_rows()
        self._on_status()

    # -- UI -----------------------------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 0)
        bar.setSpacing(2)
        self.act_open = self._action("mapview_open", "gui.mapview.act.open", "gui.mapview.act.open.tip",
                                     self.open_mapping)
        self.act_save = self._action("mapview_save", "gui.mapview.act.save", "gui.mapview.act.save.tip",
                                     self.save_mapping)
        self.act_undo = self._action("mapview_undo", "gui.mapview.act.undo", "gui.mapview.act.undo.tip", self.undo)
        self.act_redo = self._action("mapview_redo", "gui.mapview.act.redo", "gui.mapview.act.redo.tip", self.redo)
        self.act_reset = self._action("mapview_reset", "gui.mapview.act.reset", "gui.mapview.act.reset.tip",
                                      self.reset)
        self.act_expand = self._action("mapview_expand", "gui.mapview.act.expand", None, self.expand_all_current)
        self.act_collapse = self._action("mapview_collapse", "gui.mapview.act.collapse", None, self.collapse_all)
        self.act_full = self._action("mapview_full_scan", "gui.mapview.act.full_scan", "gui.mapview.act.full_scan.tip",
                                     self.full_scan)
        self.act_export = self._action("mapview_export", "gui.mapview.act.export", "gui.mapview.act.export.tip",
                                       self.export_with_mapping)
        self.buttons: dict[str, QToolButton] = {}
        for act in (self.act_open, self.act_save, None, self.act_undo, self.act_redo, self.act_reset, None,
                    self.act_expand, self.act_collapse, None, self.act_full):
            if act is None:
                bar.addSpacing(10)
                continue
            btn = QToolButton(self)
            btn.setDefaultAction(act)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            btn.setAutoRaise(True)
            btn.setObjectName(act.objectName() + "_button")
            self.buttons[act.objectName()] = btn
            bar.addWidget(btn)
        bar.addStretch(1)
        self.compact_check = QCheckBox(t("gui.mapview.act.compact"), self)
        self.compact_check.setObjectName("mapview_compact")
        self.compact_check.setToolTip(t("gui.mapview.act.compact.tip"))
        self.compact_check.setChecked(self.state.compact)
        self.compact_check.toggled.connect(self._set_compact)
        bar.addWidget(self.compact_check)
        bar.addSpacing(8)
        self.export_button = QPushButton(t("gui.mapview.act.export"), self)
        self.export_button.setObjectName("mapview_export_button")
        self.export_button.setToolTip(t("gui.mapview.act.export.tip"))
        self.export_button.setAutoDefault(False)
        self.export_button.clicked.connect(lambda: self.export_with_mapping())
        bar.addWidget(self.export_button)
        lay.addLayout(bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("mapview_splitter")
        self.splitter.setChildrenCollapsible(False)
        left = QFrame(self.splitter)
        left.setObjectName("mapview_structure")
        left.setFrameShape(QFrame.Shape.StyledPanel)
        llay = QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(10, 6, 10, 6)
        head.setSpacing(10)
        self.title_label = QLabel(t("gui.mapview.pane.tree").upper(), left)
        head.addWidget(self.title_label)
        self.stats_label = QLabel(left)
        self.stats_label.setObjectName("mapview_stats")
        head.addWidget(self.stats_label)
        self.source_label = QLabel(left)
        self.source_label.setObjectName("mapview_source")
        self.source_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self.source_label, 1)
        llay.addLayout(head)
        self.hint_label = QLabel(left)
        self.hint_label.setObjectName("mapview_hint")
        self.hint_label.setTextFormat(Qt.TextFormat.PlainText)
        self.hint_label.setWordWrap(True)
        self.hint_label.setContentsMargins(10, 4, 10, 4)
        self.hint_label.setText("    ".join((t("gui.mapview.hint.inside"), t("gui.mapview.hint.sibling"),
                                           t("gui.mapview.hint.merge"))))
        self.hint_label.setToolTip(t("gui.mapview.hint.keys"))
        self.hint_label.setAutoFillBackground(True)
        llay.addWidget(self.hint_label)
        self.stack = QStackedWidget(left)
        msg_page = QWidget(self.stack)
        mlay = QVBoxLayout(msg_page)
        mlay.addStretch(1)
        self.message_label = QLabel(msg_page)
        self.message_label.setObjectName("mapview_message")
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)
        mlay.addWidget(self.message_label)
        self.retry_button = QPushButton(t("gui.mapview.retry"), msg_page)
        self.retry_button.setObjectName("mapview_retry")
        self.retry_button.clicked.connect(self.state.retry)
        self.retry_button.hide()
        mlay.addWidget(self.retry_button, 0, Qt.AlignmentFlag.AlignHCenter)
        mlay.addStretch(1)
        self.stack.addWidget(msg_page)
        self.tree = MapTree(self)
        self.tree.selectionModel().currentChanged.connect(lambda current, _previous: self.row_selected(current.row()))
        self.stack.addWidget(self.tree)
        llay.addWidget(self.stack, 1)
        self.preview = PreviewPane(self)
        self.preview.mode_changed.connect(lambda _m: self.schedule_preview(now=True))
        self.preview.record_requested.connect(self.set_record)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 13)
        self.splitter.setStretchFactor(1, 10)
        lay.addWidget(self.splitter, 1)
        self.toast_label = QLabel(self)
        self.toast_label.setObjectName("mapview_toast")
        self.toast_label.setWordWrap(True)
        self.toast_label.setTextFormat(Qt.TextFormat.PlainText)
        self.toast_label.setContentsMargins(14, 10, 14, 10)
        self.toast_label.hide()
        self.setFocusProxy(self.tree)
        self._apply_style()

    def _action(self, name: str, key: str, tip_key: str | None, slot: Callable[[], Any]) -> QAction:
        act = QAction(t(key), self)
        act.setObjectName(name)
        tip = t(tip_key) if tip_key else t(key)
        act.setToolTip(tip)
        act.setProperty("base_tip", tip)
        act.triggered.connect(lambda _checked=False, fn=slot: fn())
        return act

    def _apply_style(self) -> None:
        for label, role in ((self.title_label, "muted"), (self.stats_label, "faint"), (self.source_label, "faint"),
                            (self.hint_label, "muted"), (self.message_label, "muted")):
            pal = label.palette()
            pal.setColor(QPalette.ColorRole.WindowText, _role(role))
            if label is self.hint_label:
                pal.setColor(QPalette.ColorRole.Window, _role("panel_alt"))
            label.setPalette(pal)
        f = self.title_label.font()
        f.setWeight(QFont.Weight.DemiBold)
        self.title_label.setFont(f)
        fg, bg = _role("fg"), _role("bg")
        self.toast_label.setStyleSheet(f"QLabel#mapview_toast {{ background: {fg.name()}; color: {bg.name()}; "
                                       "border-radius: 9px; }")
        self.export_button.setStyleSheet(
            f"QPushButton#mapview_export_button {{ background: {_role('accent').name()}; "
            f"color: {_role('accent_ink').name()}; border-radius: 6px; padding: 4px 12px; }}"
            f"QPushButton#mapview_export_button:disabled {{ background: {_role('line').name()}; "
            f"color: {_role('faint').name()}; }}")

    def _on_theme(self) -> None:
        if self._dead:
            return
        self._apply_style()
        self.preview.apply_style()
        self.tree.viewport().update()

    # -- ViewBase ---------------------------------------------------------------------------------
    def activate(self) -> None:
        """Became visible: the tree is built (sample) when there is none; the preview follows."""
        if self._dead:
            return
        self._activated = True
        self.state.ensure()
        self._on_status()
        if self._preview_dirty:
            self.schedule_preview(now=True)

    def zoom(self, steps: int) -> None:
        self._zoom = 0 if steps == 0 else max(-6, min(12, self._zoom + int(steps)))
        for widget in (self.tree, self.preview.edit):
            font = mono_font(self.session)
            size = font.pointSizeF()
            if size > 0:
                font.setPointSizeF(max(5.0, size + self._zoom))
            widget.setFont(font)
        self.tree.delegate.update_fonts()
        self.tree.doItemsLayout()
        self.tree.viewport().update()

    def current_ref(self) -> Any:
        return None    # the designer's rows are mapping nodes, not document nodes

    def _current_source(self) -> str | None:
        row = self.current_row()
        if row is None or row.kind != "node":
            return None
        n = row.node
        if n.src is None:
            return n.key
        return mp.path_label(n.src)

    def copy_value(self, fmt: str = "json") -> Any:
        """Ctrl+C: the text selected in the preview when it has the focus, else the current row's
        source path (``[].contact.address.city``)."""
        edit = self.preview.edit
        if edit.hasFocus() and edit.textCursor().hasSelection():
            edit.copy()
            return QApplication.clipboard().text()
        text = self._current_source()
        return set_clipboard_text(text) if text is not None else None

    def copy_path(self, style: str = "dotted") -> str | None:
        text = self._current_source()
        return set_clipboard_text(text) if text is not None else None

    def copy_key(self) -> str | None:
        row = self.current_row()
        if row is None or row.kind != "node" or row.node.is_root or row.node.is_elem:
            return None
        return set_clipboard_text(row.node.key)

    def expand_all_current(self) -> None:
        """"Hammasini ochish" (also the window's ``*``)."""
        self.finish_rename()
        self.state.expand_all()

    def collapse_all(self) -> None:
        """"Yig'ish": the root and its element row stay open."""
        self.finish_rename()
        self.state.collapse_all()

    def expand_to_level(self, n: int) -> None:
        """The window's "N daraja ochish" (1-9): ``n`` levels below the current row's node open,
        the deeper ones closed (no current row: below the root), like the tree view."""
        self.finish_rename()
        self.state.expand_to_level(self._current_node_id(), max(1, min(9, int(n))))

    # -- the inline rename ------------------------------------------------------------------------
    def finish_rename(self) -> None:
        """A rename being typed is committed now, before another command acts on the tree: the
        prototype commits when the box loses the focus, a toolbar button / window shortcut does
        not take it (undo then takes back the rename, not the step before it)."""
        tree = self.tree
        editing = tree.delegate.editing
        if editing is None:
            return
        editor, node_id = editing
        text = editor.text()
        designer = self.state.designer
        tree.delegate.editing = None        # a late commit of this editor is ignored
        tree.closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)
        if node_id is not None:
            self.commit_rename(node_id, text, designer=designer)

    def cancel_rename(self) -> None:
        """Close a rename being typed without renaming."""
        tree = self.tree
        editing = tree.delegate.editing
        if editing is None:
            return
        tree.delegate.editing = None
        tree.closeEditor(editing[0], QAbstractItemDelegate.EndEditHint.RevertModelCache)

    def renaming(self) -> bool:
        """The inline rename editor is open."""
        return self.tree.delegate.editing is not None

    def shutdown(self) -> None:
        self._dead = True
        self._preview_timer.stop()
        self._toast_timer.stop()
        self.tree.cancel_drag()
        if self._preview_task is not None:
            self._preview_task.cancel()
            self._preview_task = None
        st = self.state
        for signal, slot in ((st.changed, self._on_state_changed), (st.status_changed, self._on_status),
                             (st.message, self._on_message), (st.replaced, self._on_replaced),
                             (self.session.current_changed, self._on_current_changed),
                             (self.session.opened, self._on_opened),
                             (self.session.index_finished, self._on_index_finished),
                             (self.session.state_changed, self._on_session_state),
                             (theme.notifier().theme_changed, self._on_theme)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        data_changed = getattr(self.session, "data_changed", None)
        if data_changed is not None:
            try:
                data_changed.disconnect(self._on_data_changed)
            except (RuntimeError, TypeError):
                pass
        super().shutdown()

    # -- undo / redo (the main window routes Ctrl+Z / Ctrl+Y here while this view is active) --------
    @property
    def can_undo(self) -> bool:
        d = self.state.designer
        return d is not None and d.can_undo and not self.state.busy

    @property
    def can_redo(self) -> bool:
        d = self.state.designer
        return d is not None and d.can_redo and not self.state.busy

    @property
    def undo_text(self) -> str:
        return t("gui.mapview.undo_label") if self.can_undo else ""

    @property
    def redo_text(self) -> str:
        return t("gui.mapview.undo_label") if self.can_redo else ""

    def undo(self) -> bool:
        self.finish_rename()
        res = self.state.undo()
        return bool(res is not None and res.applied)

    def redo(self) -> bool:
        self.finish_rename()
        res = self.state.redo()
        return bool(res is not None and res.applied)

    def reset(self) -> bool:
        self.finish_rename()
        res = self.state.reset()
        return bool(res is not None and res.applied)

    # -- rows ---------------------------------------------------------------------------------------
    def _info_fn(self) -> Callable[[MapRow], RowInfo]:
        st = self.state
        tree, designer = st.tree, st.designer
        name = self.session.name
        count = st.record_count()

        def fn(row: MapRow) -> RowInfo:
            return row_info(tree, designer, row, file_name=name, record_count=count)

        return fn

    def _rebuild_rows(self) -> None:
        st = self.state
        tree = self.tree
        if tree.delegate.editing is not None:
            # a change from elsewhere (the other pane, an edit of the document) while renaming:
            # the typed name is committed first (a new tree drops it: _on_replaced)
            self.finish_rename()
        bar = tree.verticalScrollBar()
        keep = bar.value()
        current = self._current_id
        current_row = tree.currentIndex().row()
        rows = visible_rows(st.tree, st.limits) if st.tree is not None else []
        self.model.set_rows(rows, self._info_fn() if st.tree is not None else None)
        tree.executeDelayedItemsLayout()
        bar.setValue(min(keep, bar.maximum()))
        target = self.model.row_of(current)
        if target is None and current is not None and st.tree is not None:
            # the current node was hidden (a parent closed): its nearest visible ancestor
            node = st.tree.find(int(current))
            p = st.tree.parent(node) if node is not None else None
            while p is not None and target is None:
                target = self.model.row_of(p.id)
                p = st.tree.parent(p)
        if target is None and rows:
            target = min(max(0, current_row), len(rows) - 1) if current_row >= 0 else None
        if target is not None:
            self._select_row(target, scroll=False)
        self._update_header()
        self._update_actions()
        self.tree_shown.emit()

    def _select_row(self, row: int, *, scroll: bool = True) -> None:
        index = self.model.index(row, 0)
        if not index.isValid():
            return
        tree = self.tree
        tree.selectionModel().setCurrentIndex(index, tree.selectionModel().SelectionFlag.ClearAndSelect)
        if scroll:
            tree.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
        self.row_selected(row)

    def row_selected(self, row: int) -> None:
        r = self.model.row(row)
        if r is not None:
            self._current_id = int(r.node_id) if r.kind == "node" else None

    def current_row(self) -> MapRow | None:
        index = self.tree.currentIndex()
        return self.model.row(index.row()) if index.isValid() else None

    def select_node(self, node_id: int | None) -> bool:
        """Make the node ``node_id`` the current row (opening its ancestors when needed)."""
        st = self.state
        if node_id is None or st.tree is None:
            return False
        node = st.tree.find(int(node_id))
        if node is None:
            return False
        p = st.tree.parent(node)
        opened = False
        while p is not None:
            if not p.open:
                st.designer.set_open(p.id, True)
                opened = True
            p = st.tree.parent(p)
        self._current_id = int(node_id)
        if opened:
            st.changed.emit(False)
        row = self.model.row_of(node_id)
        if row is None:
            return False
        self._select_row(row)
        return True

    # -- state signals --------------------------------------------------------------------------------
    def _on_state_changed(self, mapping: bool) -> None:
        if self._dead:
            return
        self._rebuild_rows()
        self.history_changed.emit()
        if mapping:
            self.schedule_preview()

    def _on_status(self) -> None:
        if self._dead:
            return
        st = self.state
        s = self.session
        page = 1
        retry = False
        if st.designer is None:
            page = 0
            if st.phase == "failed" and st.error is not None:
                err = st.error
                text = err.message if isinstance(err, JsonReshapeError) else f"{type(err).__name__}: {err}"
                hint = err.hint if isinstance(err, JsonReshapeError) else None
                msg = t("gui.mapview.state.failed", error=text)
                if hint:
                    msg += "\n" + hint
                retry = True
            elif s.state == "failed":
                msg = t("gui.mapview.state.unavailable")
            elif s.doc is None or s.state in ("new", "opening"):
                msg = t("gui.mapview.state.waiting_open")
            elif s.state == "indexing":
                msg = t("gui.mapview.state.waiting_index")
            elif st.building and st.full_running:
                msg = t("gui.mapview.state.scanning")
            else:
                msg = t("gui.mapview.state.building", count=format_count(int(s.app.settings.sample_size)))
            self.message_label.setText(msg)
            self.retry_button.setVisible(retry)
        self.stack.setCurrentIndex(page)
        if st.designer is not None and self.model.rowCount() == 0:
            self._rebuild_rows()
        if self._activated and self.isVisible() and st.designer is None and st.phase == "idle":
            st.ensure()
        if self.compact_check.isChecked() != st.compact:
            self.compact_check.blockSignals(True)
            self.compact_check.setChecked(st.compact)
            self.compact_check.blockSignals(False)
        self._update_header()
        self._update_actions()
        self.history_changed.emit()

    def _on_message(self, text: str) -> None:
        if not self._dead and self.isVisible():
            self.toast(text)

    def _on_replaced(self) -> None:
        """A new tree (Full scan, an opened mapping file, a reopen): what this view holds of the
        old one -- a drag, an open context menu, a rename being typed -- is dropped (the new tree
        may reuse its node ids for other nodes)."""
        if self._dead:
            return
        self.tree.cancel_drag()
        self.cancel_rename()
        if self._menu is not None:
            menu, self._menu = self._menu, None
            menu.close()

    def _update_header(self) -> None:
        st = self.state
        self.stats_label.setText(st.stats_text())
        parts = []
        members = st.doc_kind() == "keyed"
        if st.designer is not None:
            if st.source == "file" and st.file_path:
                parts.append(t("gui.mapview.source.file", name=os.path.basename(st.file_path)))
            elif st.source == "full":
                key = "gui.mapview.source.full_members" if members else "gui.mapview.source.full"
                parts.append(t(key, scanned=format_count(st.scanned)))
            else:
                key = "gui.mapview.source.sample_members" if members else "gui.mapview.source.sample"
                parts.append(t(key, scanned=format_count(st.scanned), total=format_count(st.total)))
            if st.skipped:
                parts.append(t("gui.mapview.source.skipped", count=format_count(st.skipped)))
            if getattr(st.tree, "truncated", False):
                parts.append(t("gui.mapview.source.truncated"))
        if st.full_running:
            parts.append(t("gui.mapview.act.full_scan.running"))
        if st.flattening:
            parts.append(t("gui.mapview.source.flattening"))
        text = " \N{MIDDLE DOT} ".join(parts)
        self.source_label.setText(text)
        self.source_label.setToolTip(t("mapping.warn.truncated") if getattr(st.tree, "truncated", False) else text)

    def _update_actions(self) -> None:
        st = self.state
        d = st.designer
        ready = st.doc_ready()
        has = d is not None
        free = has and not st.busy

        def able(act: QAction, on: bool, why: str = "") -> None:
            act.setEnabled(bool(on))
            base = str(act.property("base_tip") or act.text())
            act.setToolTip(base if on or not why else f"{base}\n{why}")

        no_tree = t("gui.mapview.why.no_tree")
        busy = "" if not st.busy else t("gui.mapview.why.scanning") if st.full_running else t("gui.mapview.why.busy")
        able(self.act_open, ready, t("gui.mapview.state.waiting_index"))
        able(self.act_save, has, no_tree)
        able(self.act_undo, free and d.can_undo, busy or t("mapping.why.nothing_to_undo"))
        able(self.act_redo, free and d.can_redo, busy or t("mapping.why.nothing_to_redo"))
        able(self.act_reset, st.can_reset, busy or t("gui.mapview.why.original"))
        able(self.act_expand, has, no_tree)
        able(self.act_collapse, has, no_tree)
        able(self.act_full, ready and not st.busy,
             busy if st.busy else t("gui.mapview.state.waiting_index"))
        self.act_full.setText(t("gui.mapview.act.full_scan.running") if st.full_running
                              else t("gui.mapview.act.full_scan"))
        able(self.act_export, has, no_tree)
        self.export_button.setEnabled(has)
        self.export_button.setToolTip(self.act_export.toolTip())

    def _set_compact(self, on: bool) -> None:
        self.state.set_compact(on)          # every builder view of the session shows it

    # -- toasts ---------------------------------------------------------------------------------------
    def toast(self, text: str) -> None:
        """Show ``text`` for :data:`TOAST_MS` at the bottom of the view."""
        if not text or self._dead:
            return
        label = self.toast_label
        label.setText(display_text(text))   # node keys may hold line breaks / bidi controls
        width = min(560, max(120, self.width() - 32))
        label.setFixedWidth(width)
        label.adjustSize()
        self._place_toast()
        label.show()
        label.raise_()
        self._toast_timer.start()

    def toast_text(self) -> str:
        """The toast on screen (``""``: none)."""
        return self.toast_label.text() if not self.toast_label.isHidden() else ""

    def _place_toast(self) -> None:
        label = self.toast_label
        x = max(0, (self.width() - label.width()) // 2)
        y = max(0, self.height() - label.height() - 16)
        label.move(x, y)

    def _hide_toast(self) -> None:
        self.toast_label.hide()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        if not self.toast_label.isHidden():
            self._place_toast()

    # -- operations ---------------------------------------------------------------------------------
    def _current_node_id(self) -> int | None:
        row = self.current_row()
        return int(row.node_id) if row is not None and row.kind == "node" else None

    def _after(self, res: Any, *, select: int | None = None) -> None:
        """Select the node an operation produced (or ``select``)."""
        if res is None or not res.applied:
            return
        node = select if select is not None else (res.node.id if res.node is not None else None)
        if node is not None and self.state.tree is not None and self.state.tree.find(node) is not None:
            self.select_node(node)

    def row_action(self, row: int, action: str) -> None:
        """A row's action button."""
        r = self.model.row(row)
        if r is None or r.kind != "node":
            return
        self._select_row(row, scroll=False)
        self.run_action(action, int(r.node_id))

    def run_action(self, action: str, node_id: int | None = None) -> Any:
        """``group`` | ``unwrap`` | ``flatten`` | ``rename`` | ``delete`` on ``node_id`` (default:
        the current row)."""
        self.finish_rename()
        if node_id is None:
            node_id = self._current_node_id()
        if node_id is None:
            return None
        st = self.state
        if action == "rename":
            return self.start_rename(node_id)
        if action == "flatten":
            return st.flatten(node_id)
        if action == "group":
            res = st.run("add_group", node_id)
            if res is not None and res.applied and res.node is not None:
                self.select_node(res.node.id)
                self.start_rename(res.node.id)
            return res
        if action == "unwrap":
            # the first lifted child becomes the current row (not the parent: no jump away)
            node = st.tree.find(node_id) if st.tree is not None else None
            lifted = [c.id for c in node.children] if node is not None else []
            res = st.run("unwrap", node_id)
            self._after(res, select=lifted[0] if lifted else None)
            return res
        if action == "delete":
            row = self.model.row_of(node_id)
            res = st.run("delete", node_id)
            if res is not None and res.applied and row is not None and self.model.rowCount():
                self._current_id = None
                self._select_row(min(row, self.model.rowCount() - 1))
            return res
        return None

    def start_rename(self, node_id: int | None = None) -> bool:
        """F2 / the rename button: the inline editor on the row's name."""
        if node_id is None:
            node_id = self._current_node_id()
        st = self.state
        if node_id is None or st.tree is None:
            return False
        why = st.can_operate()
        if why is not None:
            self.toast(why)
            return False
        node = st.tree.find(node_id)
        if node is None:
            return False
        reason = st.designer.actions(node).get("rename")
        if reason is not None:
            self.toast(reason)
            return False
        if not self.select_node(node_id):
            return False
        index = self.tree.currentIndex()
        self.tree.setFocus(Qt.FocusReason.OtherFocusReason)
        self.tree.edit(index)
        return True

    def commit_rename(self, node_id: int, text: str, *, designer: Any = None) -> Any:
        """The inline editor's text: renamed when it is not empty and differs (the prototype's
        rule: an empty or unchanged name changes nothing). ``designer``: the tree the name was
        typed for -- nothing happens when another one replaced it meanwhile."""
        if self._dead:
            return None
        st = self.state
        if designer is not None and st.designer is not designer:
            return None
        node = st.tree.find(node_id) if st.tree is not None else None
        name = mp.js_trim(str(text))
        if node is None or not name or name == node.key:
            return None
        res = st.run("rename", node_id, str(text))
        self._after(res, select=node_id)
        return res

    def drop(self, node_id: int, target_id: int, zone: str) -> Any:
        """A drag & drop ended on ``target_id`` in ``zone``."""
        res = self.state.run("move", node_id, target_id, zone)
        self._after(res)
        return res

    def cut(self) -> bool:
        """Ctrl+X: remember the current node for a move with Ctrl+V."""
        st = self.state
        node_id = self._current_node_id()
        if node_id is None or st.designer is None:
            return False
        if not st.designer.can_drag(node_id):
            node = st.tree.find(node_id)
            self.toast(t("mapping.why.root") if node is not None and node.is_root else t("mapping.why.elem"))
            return False
        st.set_cut(node_id)
        node = st.tree.find(node_id)
        self.toast(t("gui.mapview.msg.cut", name=display_text(node.key if node is not None else "")))
        return True

    def paste_reason(self, target_id: int | None) -> str | None:
        """Why Ctrl+V cannot move the cut node onto ``target_id`` (``None``: it can)."""
        st = self.state
        d = st.designer
        if d is None:
            return t("gui.mapview.why.no_tree")
        cut = st.cut_id
        if cut is None or st.tree.find(cut) is None:
            return t("gui.mapview.why.no_cut")
        if target_id is None:
            return t("mapping.why.missing")
        if d.paste_zone(target_id, cut) is None:
            node, target = st.tree.find(cut), st.tree.find(target_id)
            x = target
            while x is not None:
                if x is node:
                    return t("mapping.why.into_itself")
                x = st.tree.parent(x)
            return t("mapping.why.no_zone")
        return None

    def paste(self) -> Any:
        """Ctrl+V: move the cut node onto the current row (inside when allowed, else after)."""
        st = self.state
        target = self._current_node_id()
        why = self.paste_reason(target)
        if why is not None:
            self.toast(why)
            return None
        cut = st.cut_id
        zone = st.designer.paste_zone(target, cut)
        res = st.run("move", cut, target, zone)
        self._after(res)
        return res

    def move_current(self, step: int) -> Any:
        """Alt+↑ / Alt+↓."""
        node_id = self._current_node_id()
        if node_id is None:
            return None
        res = self.state.run("move_up" if step < 0 else "move_down", node_id)
        self._after(res, select=node_id)
        return res

    def _move_reason(self, node_id: int, step: int) -> str | None:
        st = self.state
        node = st.tree.find(node_id) if st.tree is not None else None
        if node is None:
            return t("mapping.why.missing")
        if node.is_root:
            return t("mapping.why.root")
        if node.is_elem:
            return t("mapping.why.elem")
        p = st.tree.parent(node)
        i = next((k for k, c in enumerate(p.children) if c is node), 0) + step
        if not 0 <= i < len(p.children):
            return t("mapping.why.first") if step < 0 else t("mapping.why.last")
        return None

    def full_scan(self) -> None:
        """"To'liq skanerlash" (asks first when the tree has changes)."""
        self.finish_rename()
        st = self.state
        d = st.designer
        if d is not None and d.can_undo:
            ask(self, t("gui.mapview.ask.replace", count=format_count(d.changes)),
                lambda ok: ok and st.full_scan(), title=t("gui.mapview.ask.title"))
            return
        st.full_scan()

    def open_mapping(self, path: str | None = None) -> None:
        """"Tuzilmani ochish…": a mapping file replaces the tree (asks first when it has changes)."""
        self.finish_rename()
        st = self.state
        d = st.designer

        def go(ok: bool = True) -> None:
            if not ok:
                return
            p = path
            if p is None:
                folder = os.path.dirname(st.file_path) if st.file_path else os.path.dirname(self.session.path)
                p = self.ask_open_path(t("gui.mapview.open.title"), folder, self._filters())
            if p:
                st.open_mapping(p)

        if d is not None and d.can_undo:
            ask(self, t("gui.mapview.ask.replace", count=format_count(d.changes)), go,
                title=t("gui.mapview.ask.title"))
            return
        go()

    def save_mapping(self, path: str | None = None) -> Any:
        """"Tuzilmani saqlash…": the mapping file (the CLI's ``jsonreshape apply`` reads it)."""
        self.finish_rename()
        st = self.state
        if st.designer is None:
            self.toast(t("gui.mapview.why.no_tree"))
            return None
        if path is None:
            path = self.ask_save_path(t("gui.mapview.save.title"), self._default_mapping_path(), self._filters())
        if not path:
            return None
        if not os.path.splitext(path)[1]:
            path += ".json"
        return st.save_mapping(path)

    def _default_mapping_path(self) -> str:
        st = self.state
        if st.file_path:
            return st.file_path
        stem = os.path.splitext(self.session.name)[0] or "mapping"
        if stem.lower().endswith(".json"):
            stem = stem[:-5]
        return os.path.join(os.path.dirname(self.session.path), f"{stem}.mapping.json")

    @staticmethod
    def _filters() -> str:
        return t("gui.mapview.filter", patterns=FILE_FILTER)

    def ask_open_path(self, title: str, folder: str, filters: str) -> str | None:
        """An open-file dialog (tests replace this method); ``None`` when cancelled."""
        path, _selected = QFileDialog.getOpenFileName(self, title, folder, filters)
        return path or None

    def ask_save_path(self, title: str, default: str, filters: str) -> str | None:
        """A save-file dialog (tests replace this method); ``None`` when cancelled."""
        path, _selected = QFileDialog.getSaveFileName(self, title, default, filters)
        return path or None

    def export_with_mapping(self) -> Any:
        """Ctrl+E / "Eksport…": the export dialog in mapping mode (JSON / JSON Lines, indent,
        target; ``engine.apply`` through the export runner)."""
        self.finish_rename()
        st = self.state
        if st.designer is None:
            self.toast(t("gui.mapview.why.no_tree"))
            return None
        if self.session.doc is None:
            return None
        from jsonreshape.gui.exportdialog import ExportDialog, MappingSource

        source = MappingSource(tree=st.tree_copy, summary=st.stats_text(), compact=st.compact)
        try:
            dialog = ExportDialog(self.session, self.window(), scope="view", mapping=source)
        except Exception as exc:
            log.exception("opening the export dialog failed")
            self.session.app.report_error(exc, self)
            return None
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog

    # -- keys ---------------------------------------------------------------------------------------------
    def handle_key(self, event: QKeyEvent) -> bool:
        """The tree's keys (see the module docstring); True when handled."""
        K = Qt.Key
        key = event.key()
        mods = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
                                    | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        none = mods == Qt.KeyboardModifier.NoModifier
        tree = self.tree
        st = self.state
        if tree.drag_active():
            if not tree.drag_is_stale():
                if key == K.Key_Escape:
                    tree.cancel_drag()
                return True
            tree.cancel_drag()              # its button was released elsewhere: the keys work again
        if key == K.Key_Escape and none and st.cut_id is not None:
            st.set_cut(None)
            self.toast(t("gui.mapview.msg.cut_cancelled"))
            return True
        row = self.current_row()
        node_id = int(row.node_id) if row is not None and row.kind == "node" else None
        if none and key == K.Key_F2:
            self.start_rename()
            return True
        if none and key == K.Key_Delete:
            self.run_action("delete")
            return True
        if none and key == K.Key_Insert:
            if node_id is not None and st.designer is not None:
                why = st.designer.actions(node_id).get("group")
                if why is not None:
                    self.toast(why)
                    return True
            self.run_action("group")
            return True
        if ctrl and not shift and not alt and key == K.Key_X:
            self.cut()
            return True
        if ctrl and not shift and not alt and key == K.Key_V:
            self.paste()
            return True
        if alt and not ctrl and not shift and key in (K.Key_Up, K.Key_Down):
            self.move_current(-1 if key == K.Key_Up else 1)
            return True
        if ctrl and shift and not alt and key == K.Key_U:
            self.run_action("unwrap")
            return True
        if none and key in (K.Key_Return, K.Key_Enter):
            if row is not None and row.kind == "more":
                st.show_more(row.node_id)
            elif node_id is not None:
                st.toggle(node_id)
            return True
        if none and key == K.Key_Left and row is not None:
            if row.kind == "node" and row.node.open and row.node.children:
                st.set_open(node_id, False)
            elif row.parent is not None:
                self.select_node(row.parent.id)
            return True
        if none and key == K.Key_Right and row is not None and row.kind == "node":
            n = row.node
            if n.children and not n.open:
                st.set_open(node_id, True)
            elif n.children:
                self.select_node(n.children[0].id)
            return True
        if (key == K.Key_Menu and none) or (key == K.Key_F10 and shift and not ctrl and not alt):
            rect = tree.visualRect(tree.currentIndex())
            pos = rect.bottomLeft() if rect.isValid() else QPoint(0, 0)
            self.popup_menu(tree.viewport().mapToGlobal(pos + QPoint(24, 0)))
            return True
        # fallbacks when no window action took the keys (a view outside the main window)
        if ctrl and not alt and key == K.Key_Z and not shift:
            self.undo()
            return True
        if ctrl and not alt and (key == K.Key_Y and not shift or key == K.Key_Z and shift):
            self.redo()
            return True
        if ctrl and not shift and not alt and key == K.Key_E:
            self.export_with_mapping()
            return True
        if key == K.Key_Asterisk and not ctrl and not alt:
            self.expand_all_current()
            return True
        return False

    # -- context menu ------------------------------------------------------------------------------------
    def build_menu(self) -> QMenu | None:
        """Every operation on the current row; a disabled one says why (tooltip)."""
        st = self.state
        d = st.designer
        row = self.current_row()
        if d is None or row is None or row.kind != "node":
            return None
        node = row.node
        node_id = int(row.node_id)
        busy = st.can_operate() if st.busy else None
        reasons = d.actions(node)
        menu = QMenu(self)
        menu.setObjectName("mapview_menu")
        menu.setToolTipsVisible(True)

        def add(name: str, text: str, keys: str | None, why: str | None, slot: Callable[[], Any]) -> QAction:
            act = menu.addAction(f"{text}\t{keys}" if keys else text)
            act.setObjectName(f"mapview_{name}")
            reason = busy if busy is not None and name not in ("toggle", "expand_all", "collapse_all", "copy_source") \
                else why
            act.setEnabled(reason is None)
            if reason:
                act.setToolTip(reason)
            def trigger(_checked: bool = False, fn: Callable[[], Any] = slot) -> None:
                if st.designer is d and not self._dead:     # a new tree may reuse this node's id
                    fn()
            act.triggered.connect(trigger)
            return act

        add("group", t("mapping.action.group.label"), "Ins", reasons.get("group"),
            lambda: self.run_action("group", node_id))
        add("unwrap", t("mapping.action.unwrap.label"), "Ctrl+Shift+U", reasons.get("unwrap"),
            lambda: self.run_action("unwrap", node_id))
        add("flatten", t("mapping.action.flatten.label"), None, reasons.get("flatten"),
            lambda: self.run_action("flatten", node_id))
        add("rename", t("mapping.action.rename"), "F2", reasons.get("rename"),
            lambda: self.run_action("rename", node_id))
        add("delete", t("mapping.action.delete.label"), "Del", reasons.get("delete"),
            lambda: self.run_action("delete", node_id))
        menu.addSeparator()
        drag_why = None if d.can_drag(node_id) else (t("mapping.why.root") if node.is_root else t("mapping.why.elem"))
        add("cut", t("gui.mapview.menu.cut"), "Ctrl+X", drag_why, self.cut)
        add("paste", t("gui.mapview.menu.paste"), "Ctrl+V", self.paste_reason(node_id), self.paste)
        add("move_up", t("gui.mapview.menu.move_up"), "Alt+↑", self._move_reason(node_id, -1),
            lambda: self.move_current(-1))
        add("move_down", t("gui.mapview.menu.move_down"), "Alt+↓", self._move_reason(node_id, 1),
            lambda: self.move_current(1))
        menu.addSeparator()
        toggle_text = t("gui.mapview.menu.close") if node.open else t("gui.mapview.menu.open")
        add("toggle", toggle_text, "Enter", None if node.children else t("gui.mapview.why.no_children"),
            lambda: st.toggle(node_id))
        add("expand_all", t("gui.mapview.act.expand"), "*", None, self.expand_all_current)
        add("collapse_all", t("gui.mapview.act.collapse"), None, None, self.collapse_all)
        menu.addSeparator()
        add("copy_source", t("gui.mapview.menu.copy_source"), "Ctrl+C", None, lambda: self.copy_value())
        return menu

    def popup_menu(self, global_pos: QPoint) -> QMenu | None:
        """The context menu of the current row at ``global_pos`` (deleted when it closes; closed
        when the tree is replaced)."""
        self.finish_rename()
        menu = self.build_menu()
        if menu is None:
            return None
        if self._menu is not None:
            self._menu.close()
        self._menu = menu

        def hidden() -> None:
            if self._menu is menu:
                self._menu = None
            menu.deleteLater()
        menu.aboutToHide.connect(hidden)
        menu.popup(global_pos)
        return menu

    # -- preview ---------------------------------------------------------------------------------------------
    def set_record(self, index: int) -> None:
        """Show record ``index`` (0-based) in the preview (the navigator)."""
        count = self.state.record_count()
        index = min(max(0, int(index)), max(0, count - 1))
        if index != self._rec_index:
            self._rec_index = index
        self.schedule_preview(now=True)

    def record_index(self) -> int:
        return self._rec_index

    def schedule_preview(self, *, now: bool = False) -> None:
        """Compute the preview :data:`PREVIEW_DEBOUNCE_MS` after the last change (a hidden view
        only marks it)."""
        self._preview_dirty = True
        if self._dead or not self._activated or not self.isVisible():
            return
        if now:
            self._preview_timer.stop()
            self._run_preview()
        else:
            self._preview_timer.start()

    def _kind(self) -> str | None:
        return self.state.doc_kind()

    def _run_preview(self) -> None:
        if self._dead:
            return
        s = self.session
        st = self.state
        doc = s.doc
        mode = self.preview.mode()
        kind = self._kind()
        count = st.record_count()
        members = kind == "keyed"
        self.preview.set_navigator(self._rec_index, count, visible=kind not in (None, "single"), members=members)
        self._update_foot(None)
        if doc is None or s.state != "ready" or not getattr(doc, "index_complete", False):
            return
        snap = st.snapshot() if mode == "out" else None
        if mode == "out" and snap is None:
            if st.phase == "failed":
                self.preview.show_note(t("gui.mapview.msg.not_ready"))
            return
        self._preview_dirty = False
        if self._preview_task is not None:
            self._preview_task.cancel()
        self._preview_serial += 1
        serial = self._preview_serial
        pinned = doc.pinned()
        rp = st.record_path()
        index = self._rec_index
        vcache, wcache = self._value_cache, st.wrapper_cache

        def work(ctx: Any) -> PreviewResult:
            return compute_preview(pinned, kind=kind, record_path=rp, index=index, mode=mode, snapshot=snap,
                                   value_cache=vcache, wrapper_cache=wcache, cancel=ctx.cancel)

        def done(result: PreviewResult) -> None:
            if self._dead or serial != self._preview_serial:
                return
            self._preview_task = None
            self._show_preview(result, mode)

        def failed(exc: BaseException) -> None:
            if self._dead or serial != self._preview_serial:
                return
            self._preview_task = None
            text = exc.message if isinstance(exc, JsonReshapeError) else f"{type(exc).__name__}: {exc}"
            self.preview.show_note(t("gui.mapview.preview.error", error=text), error=True)
            self._update_foot(None)
            self.preview_shown.emit()

        def cancelled() -> None:
            if serial == self._preview_serial:
                self._preview_task = None

        self._preview_task = s.app.tasks.run(work, title=t("gui.mapview.task.preview", name=s.name), kind="mapping",
                                             owner=s, on_done=done, on_error=failed, on_cancel=cancelled, silent=True)

    def _show_preview(self, result: PreviewResult, mode: str) -> None:
        # the record value is kept only when small (compute_preview drops big ones); one parsed
        # wrapper per document state, shared by the session's builder views
        self._value_cache = (result.value_key, result.value, result.key) if result.value_key is not None else None
        if result.wrapper_key is not None:
            self.state.wrapper_cache = (result.wrapper_key, result.wrapper)
        if result.count:
            self._rec_index = result.index
        kind = self._kind()
        self.preview.set_navigator(self._rec_index, result.count, visible=kind not in (None, "single"),
                                   members=kind == "keyed")
        if result.message:
            self.preview.show_note(result.message)
        else:
            self.preview.show_text(result.text)
        self._update_foot(result)
        self.preview_shown.emit()

    def _update_foot(self, result: PreviewResult | None) -> None:
        kind = self._kind()
        mode = self.preview.mode()
        records = kind not in (None, "single")
        members = kind == "keyed"
        n = f"{self._rec_index}"
        if mode == "out":
            text = ""
            if records:
                text = t("gui.mapview.preview.foot.out_member" if members else "gui.mapview.preview.foot.out",
                         record=n) + " "
            text += t("gui.mapview.preview.foot.limit")
        elif records:
            text = t("gui.mapview.preview.foot.src_member" if members else "gui.mapview.preview.foot.src", record=n)
        else:
            text = t("gui.mapview.preview.foot.src_value")
        if result is not None and result.edited:
            text += " " + t("gui.mapview.preview.foot.edited")
        self.preview.foot.setText(text)

    def preview_text(self) -> str:
        """The preview on screen (its text, or the note shown instead)."""
        return self.preview.note() or self.preview.text()

    # -- session events --------------------------------------------------------------------------------------
    def _on_current_changed(self, ref: Any, origin: Any) -> None:
        if self._dead or origin is self or ref is None:
            return
        record = getattr(ref, "record", None)
        if record is None or self._kind() in (None, "single"):
            return
        if int(record) != self._rec_index:
            self._rec_index = int(record)
            self._drop_preview()            # a preview still running reads the record before
            self.schedule_preview()

    def _drop_preview(self) -> None:
        """The preview running reads what is no longer wanted (another record, an older state of
        the document): its result is not shown."""
        self._preview_serial += 1
        if self._preview_task is not None:
            self._preview_task.cancel()
            self._preview_task = None

    def _on_data_changed(self, _cs: Any = None) -> None:
        if self._dead:
            return
        self._value_cache = None
        self._drop_preview()
        self.schedule_preview()

    def _on_opened(self) -> None:
        if self._dead:
            return
        self._value_cache = None
        self._on_status()
        self.schedule_preview()

    def _on_index_finished(self) -> None:
        if not self._dead:
            self._on_status()
            self.schedule_preview()

    def _on_session_state(self, state: str) -> None:
        if self._dead:
            return
        if state in ("opening", "closed"):
            self.tree.cancel_drag()
            self._drop_preview()            # the session detached it (it read the old document)
        self._on_status()

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        if self._activated and self._preview_dirty:
            self.schedule_preview()
