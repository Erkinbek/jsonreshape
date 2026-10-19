"""The table view of the records (docs/ARCHITECTURE.md §11.7 "Table"; AGENT_PROMPT §4.2).

Rows are the positions of the session's view (``session.view_len()`` /
``session.record_at(pos)``; the gutter shows ``#<record>``, 0-based), columns are field
paths (:mod:`jsonreshape.paths` patterns such as ``contact.address.city`` or
``orders[*].id``, plus ``$key`` -- the member key of keyed records --, ``$index`` -- the
record number -- and ``$`` -- the whole record). The grid is a
:class:`~jsonreshape.gui.vgrid.VirtualGrid` in ``cells`` selection mode, so 100 M rows
scroll smoothly.

Loading
-------
Cells are loaded per *record* by a :class:`~jsonreshape.gui.loader.RowLoader` (never on
the GUI thread): the worker reads ``doc.record_bytes(i)``, parses it with
:func:`jsonreshape.codec.loads` (member records: the member value) and computes the cell
texts of the current columns. A record larger than :data:`STREAM_RECORD_BYTES` (or
``Settings.large_node_bytes`` when that is smaller) is never parsed whole: its cells come
from :func:`jsonreshape.stream.stream_probe` (scalars and the child counts of containers,
nothing is built in memory, the GIL is released between small chunks; a duplicate key on a
matched path falls back to :func:`~jsonreshape.stream.stream_values` within the capture
budget). The cache is bounded by :data:`LOADER_CAPACITY` rows and :data:`RECORD_CACHE_BYTES`
bytes of *estimated Python memory* (:func:`row_cost`: the objects of the cells and of a kept
value, not the raw bytes -- a parsed record takes 5-15 times its JSON size). Only the
parsed value of a row of at most :data:`KEEP_VALUE_BYTES` is kept, so a column added later
is filled at once; bigger rows keep only their cells and are fetched again for new columns.

Cell texts: no value -> empty; one value -> :func:`jsonreshape.gui.copyfmt.scalar_cell_text`
(strings raw, numbers with their exact source text, ``true``/``false``/``null`` colored,
containers ``[3]`` / ``{5}``); several values (wildcard columns) joined with ``"; "``.
Copying several cells (the window's Ctrl+C -> :meth:`TableView.copy_value`, TSV for Excel)
uses the full texts with containers as compact JSON (records larger than
``Settings.large_node_bytes``: ``[n]`` / ``{n}``); one cell copies the value of its node in
the requested format -- a wildcard cell with several values copies those values (a JSON
array / Python list), a cell holding ``""`` copies ``""``.

Linking (AGENT_PROMPT §4.6): moving the current cell selects ``NodeRef(record, path of the
cell)`` in the session (debounced); activating a container cell (double click / Enter)
reveals it in the tree; a node selected in another view selects its record's row (and the
column whose path leads to it).

Sorting: header click -> ``session.set_sort`` (click again: descending, third click:
removed); Shift+click adds / toggles a further key. Quick filter row under the header (the
grid's :meth:`~jsonreshape.gui.vgrid.VirtualGrid.set_header_extra` strip): one line edit
per column, parsed by :func:`quick_filter_expr` (pure) into a filter expression ->
``session.set_quick_filter`` (debounced 400 ms; invalid input gets a red border and a
tooltip and changes nothing); Alt+Down in the grid focuses the current column's editor.

Quick find (Ctrl+F, F3 / Shift+F3): a :class:`~jsonreshape.gui.quickfind.QuickFindBar`
under the grid searches the painted texts of the visible columns in the loaded rows (at most
:data:`QUICK_FIND_ROWS` rows, in a silent task) and highlights the hits (``Cell.spans``).

Layout (column paths, widths, order, hidden columns, frozen first column, quick filter
row) is remembered per file *and* per kind of records in ``GuiState.set_table_layout``: one
entry per file holds a layout per (document kind, record path) -- the last saved one at the
top level, the others under ``"others"`` (:data:`LAYOUT_VARIANTS_MAX` per file) -- so the
columns of one record path never come back for another (all cells empty). An entry saved
before layouts were kept per record path (no ``"kind"``) is used only when the file is
opened with the automatic record path and format.

Editing (docs/ARCHITECTURE.md §12.9 "Table"; commands go through ``session.edit``)
--------------------------------------------------------------------------------
* **Editable cells**: a concrete column path, or a wildcard column with exactly one match
  (its concrete path); ``$index`` is read-only; ``$key`` renames the record key of
  member-record documents (keyed, wrapped object); ``$`` is the record value. Enter and a
  double click reveal a container cell in the tree (as before) and edit any other cell. F2
  renames (AGENT_PROMPT §11 "F2 nomlash"; review3 L5): the record key on a ``$key`` cell, else
  the key of the cell's member in its record (:meth:`TableView.rename_key`); the view's menus
  show no move keys (``edit_keys``: Alt+Down opens the quick filter here). Key editors refuse
  keys that exist already in place (``taken``: the loaded rows / the kept parsed record;
  ``key_check``: a lookup in a silent task when the key is committed; review3 L1). The exact
  value is loaded first in a silent task
  (:func:`~jsonreshape.gui.editors.load_edit_value`, one pinned state); the editor opens when
  it arrives and the cell is still current: an :class:`~jsonreshape.gui.editors.InlineEditor`
  over the cell for numbers and for one-line strings of at most
  :data:`~jsonreshape.gui.editors.INLINE_MAX_CHARS` characters (exact number tokens, decoded
  strings), a :class:`~jsonreshape.gui.editors.StringDialog` for other strings (its own loader
  above 64 KiB; up to 4 MiB), a toggle for booleans, a
  :class:`~jsonreshape.gui.editors.JsonEditDialog` for ``null`` and containers (Shift+Enter:
  for any cell). A missing field is created with ``set_path`` (missing intermediate objects
  too); its editor follows the column's type in the schema (strings / numbers inline,
  anything else as JSON text). An unchanged value commits nothing.
* **Del** deletes the fields of the selected cells with concrete paths (``$`` cells: the
  records); the nodes are looked up in a silent task (rows not loaded too) and deleted in ONE
  step; wildcard cells are left alone with a status message pointing to Bulk operations.
  Insert adds a record after the current row (:class:`~jsonreshape.gui.editors.InsertDialog`,
  the key too in member-record documents), Ctrl+D duplicates the records of the selected
  rows, Ctrl+V pastes the clipboard's JSON as a record after the current row; the cell menu
  also deletes the records of the selected rows. Up to :data:`EDIT_SELECTION_MAX` records go
  through ``delete`` / ``duplicate`` (node commands), more through ``delete_records`` /
  ``duplicate_records`` (tasks of the edit worker; asked first). These keys are handled in
  :meth:`TableView._grid_key` -- never as window shortcuts.
* The view edit protocol of :mod:`jsonreshape.gui.actions` (node menu, Edit menu):
  ``edit_value``, ``edit_as_json``, ``rename_key`` (``$key``: the record key, else the cell's
  member key in its record), ``insert_node`` (``before`` / ``after``: a record next to the
  current row's record; ``child``: into the cell's container), ``duplicate_selection`` and
  ``delete_selection`` (as Ctrl+D / Del), ``move_selection`` (records, base view only),
  ``change_type`` (the selected cells' nodes), ``paste_json`` and ``edit_command_state``.
* **Data changes** (``session.data_changed``): with a record remap (insert / delete /
  move), a reset or ``records is None`` the loader forgets every row, else only the rows of
  the changed records; the window cache of view positions is cleared on a remap; quick-find
  hits are searched again; the current record (and the node followed from another view) is
  remapped with :func:`~jsonreshape.gui.widgets.remap_node_ref` and kept current (a deleted
  one: the ChangeSet's selection, which the session then selects -- the table follows it like
  any selection), the first shown record stays on top; without a remap the selection stays as
  it is. An open inline editor whose record changed or moved is cancelled; an open dialog
  keeps its node (remapped; refused on OK when it was deleted). The fetch closure pins
  ``doc.pinned()`` once per batch, so a batch never mixes two states. The silent lookups of
  the key editors and of Del pin the state when they are asked for (review CL-9).

Stage 5 (docs/ARCHITECTURE.md §14.7, §14.9)
-------------------------------------------
* **Smart values**: a cell holding ONE scalar shows its smart value
  (:func:`~jsonreshape.gui.treemodel.smart_value`, computed with the cells in the loader worker
  -- or, for a column added later, from the kept parsed record; never an extra read): unix
  times and ISO dates get a tooltip in Tashkent time (Uzbek weekday, the pre-1992 note),
  ``http(s)`` / ``mailto`` links are drawn underlined in the theme's link colour and a
  Ctrl+click asks "Brauzerda ochilsinmi?" before :func:`~jsonreshape.gui.widgets.open_url`
  opens them, colours get a swatch, base64 images a size tooltip; a string of a streamed huge
  record that was cut counts only as an image. ``$key`` cells too.
* The header menu's **Statistika** (``table_stats``) asks the window for the statistics of the
  column's field (duck-typed ``window().show_stats(field)``; disabled for ``$index``).
* The cell menu's **Tanlangan ikkitasini taqqoslash** (``compare_selected``): enabled for exactly
  two selected cells of loaded rows (:meth:`TableView.compare_refs`) ->
  :func:`jsonreshape.gui.comparewindow.compare_selected`.
"""

from __future__ import annotations

import bisect
import dataclasses
import logging
import re
import sys
import threading
import weakref
from array import array
from decimal import Decimal
from itertools import chain
from typing import Any, Iterable, NamedTuple

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec, core
from jsonreshape.errors import Cancelled, JsonReshapeError, PathSyntaxError, QuerySyntaxError, UserInputError
from jsonreshape.gui import copyfmt, fields, quickfind, theme
from jsonreshape.gui.loader import LoadFailed, RowLoader
from jsonreshape.gui.session import NodeRef
from jsonreshape.gui.treemodel import smart_cell, smart_value
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.gui.widgets import (
    ViewBase,
    ask,
    confirm,
    edit_controller,
    edit_unavailable_text,
    format_count,
    mono_font,
    open_url,
    remap_node_ref,
    report_task_error,
    resolve_in,
    run_value_task,
    set_clipboard_text,
    task_doc,
    task_node,
)
from jsonreshape.i18n import t
from jsonreshape.paths import PathPattern, Wild, parse_pattern
from jsonreshape.query import compile_filter, render_field
from jsonreshape.sortkeys import SortKey
from jsonreshape.stream import CaptureLimitExceeded, DuplicateKeys, capture_budget, stream_probe, stream_values
from jsonreshape.tasks import CancelToken

log = logging.getLogger(__name__)

__all__ = [
    "EDIT_SELECTION_MAX",
    "ColumnChooserDialog",
    "ColumnSpec",
    "TableView",
    "cell_data",
    "column_spec",
    "copy_text",
    "quick_filter_expr",
    "row_cost",
    "validate_column_path",
    "values_text",
]

KEY_COLUMN = "$key"
INDEX_COLUMN = "$index"
ROOT_COLUMN = "$"
JOIN_SEP = "; "

#: Rows (records) the loader keeps / their estimated Python memory (:func:`row_cost`;
#: ARCHITECTURE §11.0: record cache <= 64 MiB).
LOADER_CAPACITY = 2048
RECORD_CACHE_BYTES = 64 * 1024 * 1024
#: The parsed value of a row up to this many JSON bytes is kept (new columns are computed
#: from it at once); bigger rows keep only their cells.
KEEP_VALUE_BYTES = 16 * 1024
#: Records larger than this are streamed (:func:`huge_cells`) instead of parsed whole:
#: bounded memory and short GIL holds (``codec.loads`` of 45 MB holds the GIL ~0.9 s).
STREAM_RECORD_BYTES = 1024 * 1024
#: A wildcard cell shows at most this many values ("; …" after them).
MAX_JOIN_VALUES = 1000
#: Characters painted per cell / shown in a cell tooltip / kept of one string of a huge record.
MAX_DISPLAY_CHARS = 1000
MAX_TIP_CHARS = 4000
MAX_STREAM_CHARS = 65536
#: Copying more rows than this asks first; more than COPY_MAX_ROWS is refused.
COPY_CONFIRM_ROWS = 100_000
COPY_MAX_ROWS = 1_000_000
#: Selections up to this many cells whose rows are cached are copied without a task.
SYNC_COPY_CELLS = 20_000

#: Quick find looks at most at this many loaded rows / reports at most this many hits.
QUICK_FIND_ROWS = 5000
QUICK_FIND_MAX_MATCHES = 10_000
QUICK_FIND_RESCAN_MS = 300
FIND_QUEUE_MAX = 64            # F3 presses kept while a quick-find search runs

#: Editing (ARCHITECTURE §12.9): node commands take at most this many nodes / records (bigger
#: record selections go to delete_records / duplicate_records; bigger field deletions to Bulk
#: operations). The editors' own limits are in :mod:`jsonreshape.gui.editors`.
EDIT_SELECTION_MAX = 10_000
#: Deleting more records than this asks first.
DELETE_CONFIRM_RECORDS = EDIT_SELECTION_MAX

SELECT_DEBOUNCE_MS = 120
QF_DEBOUNCE_MS = 400
LAYOUT_SAVE_MS = 500
LAYOUT_VERSION = 2             # 2: "kind" / "record_path" say which records the layout is for
LAYOUT_VARIANTS_MAX = 8        # layouts kept per file (record paths / formats)
STATE_COPY_HEADERS = "table_copy_headers"
_ELLIPSIS = "…"


# ==================================================================================
# columns (pure)
# ==================================================================================
@dataclasses.dataclass(frozen=True)
class ColumnSpec:
    """A table column: ``text`` is its id (a :mod:`jsonreshape.paths` pattern, ``$key``,
    ``$index`` or ``$``); ``kind`` ``value`` | ``key`` | ``index``; ``pattern`` the parsed
    path (``value`` columns); ``error`` the localized reason when the text is not a valid
    path (its cells stay empty)."""

    text: str
    kind: str
    pattern: PathPattern | None = None
    error: str | None = None

    def prefix(self) -> tuple:
        """The concrete leading segments of the pattern (the node a wildcard column lies in)."""
        if self.pattern is None:
            return ()
        out = []
        for seg in self.pattern.segments:
            if isinstance(seg, Wild):
                break
            out.append(seg)
        return tuple(out)

    def sort_block(self) -> str | None:
        """Why the column cannot be sorted (localized) or ``None``."""
        if self.error is not None:
            return self.error
        if self.pattern is not None and Wild.DESCENDANTS in self.pattern.segments:
            return t("gui.table.sort.no_descendants")
        return None

    def sort_path(self) -> str:
        """The :class:`~jsonreshape.sortkeys.SortKey` path of the column."""
        return self.text

    def matches_sort_key(self, key: SortKey) -> bool:
        if self.kind == "key":
            return key.kind == "key"
        if self.kind == "index":
            return key.kind == "index"
        return key.kind == "value" and key.pattern is not None and self.pattern is not None \
            and key.pattern.segments == self.pattern.segments


def validate_column_path(text: str) -> str:
    """The normalized column text of ``text`` (``$key``, ``$index``, ``$`` or a path
    pattern); :class:`~jsonreshape.errors.PathSyntaxError` when it is not a valid path,
    :class:`~jsonreshape.errors.UserInputError` when it is empty."""
    body = (text or "").strip()
    if not body:
        raise UserInputError("gui.table.chooser.empty_path")
    if body in (KEY_COLUMN, INDEX_COLUMN, ROOT_COLUMN):
        return body
    parse_pattern(body)
    return body


def column_spec(text: str) -> ColumnSpec:
    """The :class:`ColumnSpec` of a column text (never raises: invalid paths give a spec
    with ``error``)."""
    body = (text or "").strip()
    if body == KEY_COLUMN:
        return ColumnSpec(body, "key")
    if body == INDEX_COLUMN:
        return ColumnSpec(body, "index")
    try:
        return ColumnSpec(body, "value", parse_pattern(body))
    except PathSyntaxError as exc:
        return ColumnSpec(body, "value", None, exc.message)


# ==================================================================================
# cell texts (pure)
# ==================================================================================
class _Box:
    """A container met in the stream of a huge record (only its child count is known)."""

    __slots__ = ("is_map", "count")

    def __init__(self, is_map: bool) -> None:
        self.is_map = is_map
        self.count = 0

    def __repr__(self) -> str:
        return ("{%d}" if self.is_map else "[%d]") % self.count


class _CellData:
    """What a row knows about one of its cells: the painted :class:`Cell`, the full text,
    the path of the node the cell stands for (relative to the record; several values: their
    common prefix), whether that node is a container, whether any shown value is one,
    ``count`` -- how many values the cell has (0: none; a ``""`` value counts) -- and
    ``exact`` -- ``full`` is the complete clipboard text (no container, nothing cut or left
    out), so copying needs no record read."""

    __slots__ = ("cell", "full", "path", "container", "has_containers", "count", "exact")

    def __init__(self, cell: Cell, full: str, path: tuple, container: bool, has_containers: bool,
                 count: int = 1, exact: bool | None = None) -> None:
        self.cell = cell
        self.full = full
        self.path = path
        self.container = container
        self.has_containers = has_containers
        self.count = count
        self.exact = (not has_containers) if exact is None else exact


def _item_text(v: Any) -> str:
    if isinstance(v, _Box):
        return repr(v)
    return copyfmt.scalar_cell_text(v)


def _item_role(v: Any) -> str | None:
    if isinstance(v, dict) or (isinstance(v, _Box) and v.is_map):
        return "object"
    if isinstance(v, (list, tuple, _Box)):
        return "array"
    if v is None:
        return "null"
    if v is True or v is False:
        return "bool"
    if isinstance(v, str):
        return None
    return "number"


def _item_copy(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return codec.dumps(v).decode("utf-8")
    return _item_text(v)


def values_text(values: list, more: int = 0) -> tuple[str, str | None]:
    """``(full text, color role)`` of the values a cell shows: ``""`` for none, the
    :func:`~jsonreshape.gui.copyfmt.scalar_cell_text` of one, several joined with
    ``"; "`` (``"; …"`` when ``more`` values were left out). The role is the values'
    common type role (``number``, ``bool``, ``null``, ``array``, ``object``; strings and
    mixed types: ``None``)."""
    if not values:
        return "", None
    text = JOIN_SEP.join(_item_text(v) for v in values)
    if more > 0:
        text += JOIN_SEP + _ELLIPSIS
    roles = {_item_role(v) for v in values}
    return text, (roles.pop() if len(roles) == 1 else None)


def _make_cell(full: str, role: str | None, *, italic: bool = False) -> Cell:
    shown = copyfmt.scalar_cell_text(full, MAX_DISPLAY_CHARS, single_line=True)
    tip = None
    if shown != full:
        tip = copyfmt.escape_surrogates(full if len(full) <= MAX_TIP_CHARS else full[:MAX_TIP_CHARS] + _ELLIPSIS)
    return Cell(text=shown, role=role, italic=italic, tooltip=tip)


_EMPTY = _CellData(Cell(), "", (), False, False, count=0)


def _common_prefix(paths: list[tuple]) -> tuple:
    if not paths:
        return ()
    first = paths[0]
    n = len(first)
    for p in paths[1:]:
        n = min(n, len(p))
        for i in range(n):
            if p[i] != first[i] or type(p[i]) is not type(first[i]):
                n = i
                break
    return tuple(first[:n])


def _matches(value: Any, spec: ColumnSpec) -> list[tuple[tuple, Any]]:
    """``(typed path, value)`` of every match (an index segment on an array becomes int)."""
    pattern = spec.pattern
    assert pattern is not None
    return pattern.resolve(value)


def _smart_decorate(cell: Cell, value: Any, *, cut: bool = False) -> Cell:
    """Show the smart value (:func:`jsonreshape.gui.treemodel.smart_value`) of a cell holding ONE
    scalar ``value``: its tooltip, a link (underlined, Ctrl+click), a colour swatch. ``cut``: a
    streamed string shortened with ``…`` (then only an image counts). No file is read."""
    if value is None or isinstance(value, (bool, _Box, dict, list)):
        return cell
    if isinstance(value, str):
        text = value[:-1] if cut and value.endswith(_ELLIPSIS) else value
        smart = smart_value(text, "string", prefix=cut)
    elif isinstance(value, (int, float, Decimal)):
        smart = smart_value(value, "number")
    else:
        return cell
    if smart is not None:
        decor = smart_cell(smart, cell.text)
        cell.role = decor.pop("role", cell.role)
        for name, item in decor.items():
            setattr(cell, name, item)
    return cell


def _key_cell(key: str | None) -> _CellData:
    if key is None:
        return _EMPTY
    cell = _smart_decorate(_make_cell(copyfmt.escape_surrogates(key), "key"), key)
    return _CellData(cell, key, (), False, False)


def _index_cell(record: int) -> _CellData:
    text = str(record)
    return _CellData(Cell(text=text, role="number"), text, (), False, False)


def _cell_from_items(items: list, more: int, path: tuple, container: bool, *, cut: bool = False) -> _CellData:
    """The cell of the values ``items`` (``more`` further values left out; ``cut``: a
    streamed string was shortened)."""
    if not items:
        return _EMPTY
    full, role = values_text(items, more)
    has_containers = any(isinstance(v, (dict, list, _Box)) for v in items)
    cell = _make_cell(full, role)
    if len(items) == 1 and more == 0 and not has_containers:
        _smart_decorate(cell, items[0], cut=cut)     # one scalar: a time / date / link / colour / image
    return _CellData(cell, full, path, container, has_containers, count=len(items) + more,
                     exact=not has_containers and more == 0 and not cut)


def cell_data(value: Any, spec: ColumnSpec, record: int, key: str | None = None) -> _CellData:
    """The cell of column ``spec`` for a parsed record ``value`` (record number
    ``record``, member key ``key``)."""
    if spec.kind == "key":
        return _key_cell(key)
    if spec.kind == "index":
        return _index_cell(record)
    if spec.pattern is None:
        return _EMPTY
    found = _matches(value, spec)
    if not found:
        return _EMPTY
    n = len(found)
    items = [v for _, v in found[:MAX_JOIN_VALUES]]
    path = found[0][0] if n == 1 else _common_prefix([p for p, _ in found])
    container = n == 1 and isinstance(items[0], (dict, list))
    return _cell_from_items(items, n - len(items), tuple(path), container)


def match_values(value: Any, spec: ColumnSpec) -> list:
    """Every value column ``spec`` matches in a parsed record (document order)."""
    if spec.pattern is None:
        return []
    return [v for _, v in _matches(value, spec)]


def copy_text(value: Any, spec: ColumnSpec, record: int, key: str | None = None) -> str:
    """The clipboard text of a cell: like the cell text, but complete (no ``…``, line
    breaks kept) and containers as compact JSON."""
    if spec.kind == "key":
        return key if key is not None else ""
    if spec.kind == "index":
        return str(record)
    if spec.pattern is None:
        return ""
    return JOIN_SEP.join(_item_copy(v) for _, v in _matches(value, spec))


# ==================================================================================
# quick filter syntax (pure)
# ==================================================================================
_QF_KEYWORDS = {
    "null": "{f} is null",
    "!null": "exists({f}) and {f} is not null",
    "exists": "exists({f})",
    "!exists": "missing({f})",
    "missing": "missing({f})",
    "!missing": "exists({f})",
    "empty": "{f} is empty",
    "!empty": "exists({f}) and {f} is not empty",
}
# longest first: "==" before "=", ">=" before ">", "!=" / "!~" before the generic "!"
_QF_OPS = ("==", "!=", ">=", "<=", "!~", "=", ">", "<", "~")
_NOT_JSON = object()


def _json_value(text: str) -> Any:
    try:
        return codec.loads(text)
    except (JsonReshapeError, ValueError, TypeError, RecursionError):
        return _NOT_JSON


def _json_text(value: Any) -> str:
    return codec.dumps(value).decode("utf-8")


def _has_object(value: Any) -> bool:
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            return True
        if isinstance(v, list):
            stack.extend(v)
    return False


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _qf_field(path: str) -> str:
    body = (path or "").strip()
    if not body:
        raise UserInputError("gui.table.qf.bad_column", path=path or "")
    try:
        return render_field(body)
    except PathSyntaxError:
        raise UserInputError("gui.table.qf.bad_column", path=body) from None


def _qf_equal_literal(rest: str) -> str:
    value = _json_value(rest)
    if value is _NOT_JSON:
        return _json_text(rest)          # unquoted text: a string
    if _has_object(value):
        raise UserInputError("gui.table.qf.no_objects")
    return _json_text(value)


def _qf_order_literal(op: str, rest: str) -> str:
    value = _json_value(rest)
    if value is _NOT_JSON or not (_is_number(value) or isinstance(value, str)):
        raise UserInputError("gui.table.qf.need_number", op=op, value=rest)
    return _json_text(value)


def _qf_operator(field: str, op: str, rest: str) -> str:
    if not rest:
        raise UserInputError("gui.table.qf.need_value", op=op)
    if op in ("=", "=="):
        return f"{field} == {_qf_equal_literal(rest)}"
    if op == "!=":
        return f"{field} != {_qf_equal_literal(rest)}"
    if op in ("~", "!~"):
        expr = f"{field} matches {_json_text(rest)}"
        return expr if op == "~" else f"not ({expr})"
    return f"{field} {op} {_qf_order_literal(op, rest)}"


def _range_bound(text: str) -> Any:
    value = _json_value(text)
    if value is _NOT_JSON or not (_is_number(value) or isinstance(value, str)):
        return _NOT_JSON
    return value


def _qf_range(field: str, raw: str) -> str | None:
    """``lo..hi`` / ``lo..`` / ``..hi`` (numbers or quoted strings); ``None`` when ``raw``
    is not a range (then it is plain text)."""
    start = 0
    while True:
        k = raw.find("..", start)
        if k < 0:
            return None
        start = k + 1
        lo_t, hi_t = raw[:k].strip(), raw[k + 2:].strip()
        if not lo_t and not hi_t:
            continue
        lo = _range_bound(lo_t) if lo_t else None
        hi = _range_bound(hi_t) if hi_t else None
        if lo is _NOT_JSON or hi is _NOT_JSON:
            continue
        if lo is not None and hi is not None:
            if _is_number(lo) != _is_number(hi):
                raise UserInputError("gui.table.qf.range_types")
            return f"{field} between {_json_text(lo)} and {_json_text(hi)}"
        if lo is not None:
            return f"{field} >= {_json_text(lo)}"
        return f"{field} <= {_json_text(hi)}"


def _qf_contains(field: str, text: str) -> str:
    needle = text
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        value = _json_value(text)
        if isinstance(value, str):
            needle = value
            if not needle:
                return f'{field} == ""'
    return f"lower(string({field})) contains {_json_text(needle.lower())}"


def quick_filter_expr(path: str, text: str) -> str | None:
    """The filter expression (§9 syntax) of the quick filter ``text`` typed under the
    column ``path`` (``$key``, ``$index``, ``$`` or a path pattern); ``None`` for an
    empty text. Pure (no Qt).

    ============================  =========================================================
    ``text``                      matches records where the column ...
    ============================  =========================================================
    ``abc`` / ``"a b"``           contains the text, case-insensitively (numbers by their
                                  exact text, containers by their compact JSON); ``""``:
                                  equals the empty string
    ``!abc``                      does not contain the text
    ``=x`` / ``==x``              equals ``x``: a JSON literal when ``x`` is one (``=12``,
                                  ``="12"``, ``=true``, ``=null``, ``=[1,2]``), else the
                                  text ``x`` (``=Toshkent``)
    ``!=x``                       does not equal ``x`` (also when the field is missing)
    ``>5`` ``>=5`` ``<5`` ``<=5`` compares with a number, or with a quoted string
                                  (``>="2020-01-01"``)
    ``~regex`` / ``!~regex``      matches / does not match the regular expression
    ``null`` / ``!null``          is null / is present and not null
    ``exists`` / ``missing``      is present / is missing (``!exists``, ``!missing``)
    ``empty`` / ``!empty``        is ``""``/``[]``/``{}`` / is present and not empty
    ``5..10`` ``5..`` ``..10``    lies in the range (inclusive; quoted strings too)
    ============================  =========================================================

    Keywords are case-insensitive; quote them to search the text (``"null"``). Raises a
    localized :class:`~jsonreshape.errors.UserInputError` for invalid input (a missing
    value, ``>abc``, an invalid regular expression, an object literal, ...).
    """
    field = _qf_field(path)
    raw = (text or "").strip()
    if not raw:
        return None
    template = _QF_KEYWORDS.get(raw.lower())
    if template is not None:
        expr = template.format(f=field)
    else:
        expr = None
        for op in _QF_OPS:
            if raw.startswith(op):
                expr = _qf_operator(field, op, raw[len(op):].strip())
                break
        if expr is None and raw.startswith("!"):
            rest = raw[1:].strip()
            if not rest:
                raise UserInputError("gui.table.qf.need_value", op="!")
            expr = f"not ({_qf_contains(field, rest)})"
        if expr is None:
            expr = _qf_range(field, raw)
        if expr is None:
            expr = _qf_contains(field, raw)
    try:
        compile_filter(expr)
    except QuerySyntaxError as exc:   # e.g. an invalid regular expression
        detail = exc.params.get("detail") or exc.message
        raise UserInputError("gui.table.qf.invalid", detail=detail) from None
    return expr


# ==================================================================================
# loading (worker thread)
# ==================================================================================
_NOVALUE: Any = object()   # a row whose parsed value is not kept


class _Row:
    """A loaded record: ``cells`` per column text (computed for the columns of generation
    ``gen``), the parsed ``value`` (or :data:`_NOVALUE`), the member ``key``, ``size`` in
    bytes, ``streamed`` (cells from the stream: containers known by their child counts,
    long strings cut), ``huge`` (larger than ``Settings.large_node_bytes``: copying cannot
    load its values either), ``error`` (localized text of a failed load)."""

    __slots__ = ("record", "key", "value", "huge", "cells", "size", "gen", "error", "streamed")

    def __init__(self, record: int, key: str | None, value: Any, huge: bool, cells: dict, size: int, gen: int,
                 error: str | None = None, *, streamed: bool | None = None) -> None:
        self.record = record
        self.key = key
        self.value = value
        self.huge = huge
        self.cells = cells
        self.size = size
        self.gen = gen
        self.error = error
        self.streamed = huge if streamed is None else streamed

    def __repr__(self) -> str:
        kind = "huge " if self.huge else "streamed " if self.streamed else ""
        return f"<_Row #{self.record} {kind}{len(self.cells)} cells>"


_ROW_OVERHEAD = sys.getsizeof(_Row(0, None, None, False, {}, 0, 0)) + sys.getsizeof({}) + 64


def value_cost(value: Any) -> int:
    """Estimated bytes of the Python objects of a parsed JSON value (``sys.getsizeof`` of
    every container, key and scalar; shared objects such as small ints and cached keys are
    counted each time, so the estimate errs on the high side)."""
    total = 0
    stack = [value]
    sizeof = sys.getsizeof
    while stack:
        v = stack.pop()
        total += sizeof(v)
        if isinstance(v, dict):
            for k, item in v.items():
                total += sizeof(k)
                if isinstance(item, (dict, list)):
                    stack.append(item)
                else:
                    total += sizeof(item)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, (dict, list)):
                    stack.append(item)
                else:
                    total += sizeof(item)
    return total


def _cell_cost(cd: _CellData) -> int:
    if cd is _EMPTY:
        return 16
    cell = cd.cell
    sizeof = sys.getsizeof
    n = sizeof(cd) + sizeof(cell) + sizeof(cd.full) + sizeof(cd.path)
    if cell.text is not cd.full:
        n += sizeof(cell.text)
    if cell.tooltip is not None:
        n += sizeof(cell.tooltip)
    if cell.link is not None and cell.link is not cd.full:
        n += sizeof(cell.link)
    return n


def row_cost(row: Any) -> int:
    """Estimated Python memory of a cached row (the loader's ``sizeof``): the row and its
    cells, plus the parsed value when it is kept (:func:`value_cost`)."""
    if not isinstance(row, _Row):
        return 256
    n = _ROW_OVERHEAD
    if row.value is not _NOVALUE:
        n += value_cost(row.value)
    for cd in list(row.cells.values()):
        n += _cell_cost(cd)
    if row.error is not None:
        n += sys.getsizeof(row.error)
    return n


class _ChildTracker:
    """:class:`jsonreshape.stream.StreamProbe` of the container a column's wildcard runs
    over: it knows which child of that container is being streamed (the member key or the
    element index), so the column's probe can tell the concrete path of each match."""

    __slots__ = ("stack",)

    def __init__(self) -> None:
        self.stack: list[list[Any]] = []    # [is_map, element index, member key]

    def value(self, v: Any) -> None:
        pass

    def open(self, is_map: bool) -> bool:
        self.stack.append([is_map, -1, None])
        return True

    def child(self, kind: str, v: Any) -> None:
        if not self.stack:
            return
        top = self.stack[-1]
        if kind == "key":
            top[2] = v
        else:
            top[1] += 1

    def close(self) -> None:
        if self.stack:
            self.stack.pop()

    def current(self) -> Any:
        """The member key / element index of the child being streamed (``None``: none)."""
        if not self.stack:
            return None
        top = self.stack[-1]
        return top[2] if top[0] else (top[1] if top[1] >= 0 else None)


class _CellProbe:
    """:class:`jsonreshape.stream.StreamProbe` collecting what one column shows of a huge
    record: scalars (long strings cut) and containers with their child counts; with
    ``trackers`` (``(segment index, _ChildTracker)`` of each wildcard) also the common
    prefix of the concrete paths of the matches (``path``)."""

    __slots__ = ("items", "more", "stack", "cut", "segments", "trackers", "path", "lost")

    def __init__(self, segments: tuple = (), trackers: list[tuple[int, _ChildTracker]] | None = None) -> None:
        self.items: list[Any] = []
        self.more = 0
        self.stack: list[_Box] = []
        self.cut = False
        self.segments = tuple(segments)
        self.trackers = trackers or []
        self.path: tuple | None = None
        self.lost = False                   # a match whose path could not be told

    def _match_path(self) -> None:
        if not self.trackers or self.lost:
            return
        segs = list(self.segments)
        for i, tracker in self.trackers:
            seg = tracker.current()
            if seg is None:
                self.lost = True
                return
            segs[i] = seg
        path = tuple(segs)
        self.path = path if self.path is None else _common_prefix([self.path, path])

    def _add(self, item: Any) -> None:
        if not self.stack:
            self._match_path()
        if len(self.items) < MAX_JOIN_VALUES:
            self.items.append(item)
        else:
            self.more += 1

    def value(self, v: Any) -> None:
        if isinstance(v, str) and len(v) > MAX_STREAM_CHARS:
            v = v[:MAX_STREAM_CHARS] + _ELLIPSIS
            self.cut = True
        self._add(v)

    def open(self, is_map: bool) -> bool:
        box = _Box(is_map)
        self._add(box)
        self.stack.append(box)
        return True

    def child(self, kind: str, v: Any) -> None:
        if self.stack:
            self.stack[-1].count += 1

    def close(self) -> None:
        if self.stack:
            self.stack.pop()


def _error_cell(text: str) -> _CellData:
    return _CellData(Cell(text=text, role="error", italic=True, tooltip=text), "", (), False, False, count=0)


def huge_cells(doc: Any, record: int, span: tuple[int, int], member: bool, key: str | None,
               specs: Iterable[ColumnSpec], budget: int | None, cancel: CancelToken | None) -> dict[str, _CellData]:
    """Cells of a record too large to load, streamed (see the module doc)."""
    cells: dict[str, _CellData] = {}
    vspecs: list[ColumnSpec] = []
    for spec in specs:
        if spec.kind == "key":
            cells[spec.text] = _key_cell(key)
        elif spec.kind == "index":
            cells[spec.text] = _index_cell(record)
        elif spec.pattern is None:
            cells[spec.text] = _EMPTY
        else:
            vspecs.append(spec)
    if not vspecs:
        return cells
    pats: list[PathPattern] = [spec.pattern for spec in vspecs]

    def where(spec: ColumnSpec, found: bool) -> tuple:
        if not found:
            return ()
        return spec.prefix()

    # the concrete path of a wildcard column's matches: a tracker on each container its
    # wildcards run over says which child is being streamed when the column matches
    probes: list[_CellProbe] = []
    probe_map: dict[int, list[Any]] = {}
    for k, spec in enumerate(vspecs):
        segs = spec.pattern.segments
        trackers: list[tuple[int, _ChildTracker]] = []
        if not spec.pattern.is_concrete and Wild.DESCENDANTS not in segs:
            for i, seg in enumerate(segs):
                if isinstance(seg, Wild):
                    tracker = _ChildTracker()
                    probe_map[len(pats)] = [tracker]
                    pats.append(PathPattern(segs[:i]))
                    trackers.append((i, tracker))
        probe = _CellProbe(segs, trackers)
        probes.append(probe)
        probe_map[k] = [probe]
    try:
        stream_probe(doc, span, pats, probe_map, member=member, cancel=cancel)
    except DuplicateKeys:
        try:
            got = stream_values(doc, span, pats, member, max_bytes=budget, cancel=cancel)
        except CaptureLimitExceeded as exc:
            for spec in vspecs:
                cells[spec.text] = _error_cell(exc.message)
            return cells
        for k, spec in enumerate(vspecs):
            vals = got.get(k, [])
            items = vals[:MAX_JOIN_VALUES]
            container = len(vals) == 1 and isinstance(vals[0], (dict, list))
            cells[spec.text] = _cell_from_items(items, len(vals) - len(items), where(spec, bool(vals)), container)
        return cells
    for spec, probe in zip(vspecs, probes):
        items = probe.items
        container = len(items) == 1 and probe.more == 0 and isinstance(items[0], _Box)
        path = probe.path if (items and probe.path is not None and not probe.lost) else where(spec, bool(items))
        cells[spec.text] = _cell_from_items(items, probe.more, path, container, cut=probe.cut)
    return cells


_COUNT_CHUNK = 4 << 20      # bytes fed to the core scanner at once when counting children


_KEY_TOKEN = re.compile(rb'"(?:[^"\\]|\\.)*"', re.DOTALL)


def _distinct_keys(buf: bytes, start: int, spans: bytes, keys: set) -> None:
    """Add the member keys of the scanner output ``spans`` (members of the object at
    ``buf[start:]``) to ``keys``."""
    offsets = array("Q", spans)
    if sys.byteorder != "little":
        offsets.byteswap()
    match = _KEY_TOKEN.match
    for k in range(0, len(offsets), 2):
        m = match(buf, start + offsets[k])
        if m is None:
            raise ValueError(f"no member key at {start + offsets[k]}")
        token = m.group()
        keys.add(codec.loads(token) if b"\\" in token else token[1:-1].decode("utf-8", "surrogatepass"))


def _child_count(buf: bytes, start: int, end: int, is_map: bool, cancel: CancelToken | None) -> int:
    """Children of the container ``buf[start:end]``: the Rust core scanner indexes it in
    chunks with the GIL released. An object counts its distinct keys (dict semantics, like
    the parsed value: a repeated key is one member)."""
    scanner = core.Scanner(core.MODE_CONTAINER, [], 0)
    keys: set | None = set() if is_map else None
    view = memoryview(buf)
    pos = start
    while pos < end and scanner.error is None:
        if cancel is not None:
            cancel.check()
        stop = min(end, pos + _COUNT_CHUNK)
        out = scanner.feed(view[pos:stop])
        if keys is not None and out:
            _distinct_keys(buf, start, out, keys)
        pos = stop
    if scanner.error is None:
        out = scanner.finish()
        if keys is not None and out:
            _distinct_keys(buf, start, out, keys)
    if scanner.error is not None:
        raise ValueError(f"invalid JSON at {scanner.error[0]}: {scanner.error[1]}")
    return len(keys) if keys is not None else int(scanner.record_count)


def _token_value(buf: bytes, start: int, end: int) -> tuple[Any, bool]:
    """``(item, cut)``: a :class:`_Box` with the child count for a container token, else the
    decoded scalar (long strings cut like :class:`_CellProbe` does)."""
    first = buf[start:start + 1]
    if first in (b"{", b"["):
        return _Box(first == b"{"), False      # count filled in by the caller
    value = codec.loads(buf[start:end])
    if isinstance(value, str) and len(value) > MAX_STREAM_CHARS:
        return value[:MAX_STREAM_CHARS] + _ELLIPSIS, True
    return value, False


_JSON_WS = b" \t\r\n"


class _SpanPaths:
    """Concrete paths of value spans inside one record's bytes (worker threads): from the
    record value down, each container on the way is indexed once by the core scanner (its
    direct children's spans, cached for the row) and the child holding the span is found
    by binary search -- its member key or element index is the next path segment."""

    def __init__(self, buf: bytes, root_start: int, cancel: CancelToken | None) -> None:
        self.buf = buf
        end = len(buf)
        start = root_start
        while start < end and buf[start] in _JSON_WS:
            start += 1
        while end > start and buf[end - 1] in _JSON_WS:
            end -= 1
        self.root = (start, end)
        self.cancel = cancel
        self._children: dict[int, tuple[array, array]] = {}

    def _child_spans(self, start: int, end: int) -> tuple[array, array]:
        """Starts and ends of the direct children of the container ``[start, end)``,
        relative to ``start`` (members: from the key)."""
        got = self._children.get(start)
        if got is not None:
            return got
        scanner = core.Scanner(core.MODE_CONTAINER, [], 0)
        view = memoryview(self.buf)
        out = array("Q")
        pos = start
        while pos < end and scanner.error is None:
            if self.cancel is not None:
                self.cancel.check()
            stop = min(end, pos + _COUNT_CHUNK)
            out.frombytes(scanner.feed(view[pos:stop]))
            pos = stop
        if scanner.error is None:
            out.frombytes(scanner.finish())
        if scanner.error is not None:
            raise ValueError(f"invalid JSON at {scanner.error[0]}: {scanner.error[1]}")
        if sys.byteorder != "little":
            out.byteswap()
        starts, ends = out[0::2], out[1::2]
        self._children[start] = (starts, ends)
        return starts, ends

    def path(self, target: int) -> tuple | None:
        """The path (relative to the record value) of the value starting at byte
        ``target``; ``None`` when no value starts there."""
        buf = self.buf
        cur_start, cur_end = self.root
        path: list[Any] = []
        while cur_start != target:
            first = buf[cur_start:cur_start + 1]
            if first not in (b"{", b"["):
                return None
            starts, ends = self._child_spans(cur_start, cur_end)
            rel = target - cur_start
            k = bisect.bisect_right(starts, rel) - 1
            if k < 0 or rel >= ends[k]:
                return None
            child_start, child_end = cur_start + starts[k], cur_start + ends[k]
            if first == b"{":
                m = _KEY_TOKEN.match(buf, child_start)
                if m is None:
                    return None
                token = m.group()
                key = codec.loads(token) if b"\\" in token else token[1:-1].decode("utf-8", "surrogatepass")
                pos = m.end()
                while pos < child_end and buf[pos] in _JSON_WS:
                    pos += 1
                if buf[pos:pos + 1] != b":":
                    return None
                pos += 1
                while pos < child_end and buf[pos] in _JSON_WS:
                    pos += 1
                path.append(key)
                cur_start, cur_end = pos, child_end
            else:
                path.append(k)
                cur_start, cur_end = child_start, child_end
        return tuple(path)


def _spans_path(paths: _SpanPaths, spec: ColumnSpec, spans: list[tuple[int, int]]) -> tuple:
    """The node a wildcard column's cell stands for: the concrete path of its one match,
    else the common prefix of its matches' paths (at least the column's concrete prefix);
    :meth:`ColumnSpec.prefix` when a path cannot be told."""
    floor = spec.prefix()
    common: tuple | None = None
    for start, _end in spans:
        got = paths.path(start)
        if got is None:
            return floor
        common = got if common is None else _common_prefix([common, got])
        if len(common) <= len(floor):
            break
    return common if common is not None and len(common) >= len(floor) else floor


def _extractable(spec: ColumnSpec) -> bool:
    """A value column the Rust extractor can find (no ``**``)."""
    return spec.kind == "value" and spec.pattern is not None and Wild.DESCENDANTS not in spec.pattern.segments


def _extract_spans(doc: Any, record: int, specs: list[ColumnSpec],
                   member: bool) -> tuple[bytes, list[list[tuple[int, int]]]]:
    """``(record bytes, spans of the values each column matches)`` -- spans in ``buf``
    coordinates, document order (dict semantics for duplicate keys): ``core.PathExtractor``
    with the GIL released."""
    buf = doc.record_bytes(record)
    extractor = core.PathExtractor([spec.pattern.to_core() for spec in specs], member_spans=member)
    quads = array("Q", extractor.extract(buf, array("Q", (0, len(buf))).tobytes()))
    if sys.byteorder != "little":
        quads.byteswap()
    found: list[list[tuple[int, int]]] = [[] for _ in specs]
    for i in range(0, len(quads), 4):
        found[quads[i + 1]].append((quads[i + 2], quads[i + 3]))
    return buf, found


def extracted_cells(doc: Any, record: int, member: bool, key: str | None, specs: Iterable[ColumnSpec],
                    budget: int | None, cancel: CancelToken | None) -> dict[str, _CellData]:
    """Cells of a record too large to parse but small enough to read (at most
    ``Settings.large_node_bytes``), like :func:`huge_cells` but much faster: the Rust
    ``core.PathExtractor`` finds the matched value spans (dict semantics for duplicate
    keys), scalars are decoded one by one, containers counted by the core scanner -- the
    record is never parsed whole and the GIL is free during the scans. Columns with ``**``
    (not supported by the extractor) are streamed."""
    cells: dict[str, _CellData] = {}
    fast: list[ColumnSpec] = []
    slow: list[ColumnSpec] = []
    for spec in specs:
        if spec.kind == "key":
            cells[spec.text] = _key_cell(key)
        elif spec.kind == "index":
            cells[spec.text] = _index_cell(record)
        elif spec.pattern is None:
            cells[spec.text] = _EMPTY
        elif Wild.DESCENDANTS in spec.pattern.segments:
            slow.append(spec)
        else:
            fast.append(spec)
    if fast:
        buf, found = _extract_spans(doc, record, fast, member)
        span_paths: _SpanPaths | None = None
        for spec, spans in zip(fast, found):
            items = []
            cut = False
            for start, end in spans[:MAX_JOIN_VALUES]:
                item, was_cut = _token_value(buf, start, end)
                if isinstance(item, _Box):
                    item.count = _child_count(buf, start, end, item.is_map, cancel)
                items.append(item)
                cut = cut or was_cut
            more = len(spans) - len(items)
            container = len(spans) == 1 and isinstance(items[0], _Box)
            path: tuple = ()
            if spans:
                path = spec.prefix()
                if not spec.pattern.is_concrete:
                    # the concrete node (selecting / double-clicking the cell reveals it)
                    if span_paths is None:
                        root = doc.split_member(buf)[1] if member else 0
                        span_paths = _SpanPaths(buf, root, cancel)
                    try:
                        path = _spans_path(span_paths, spec, spans)
                    except ValueError:      # pragma: no cover - the extractor validated the bytes
                        log.debug("table: no concrete path in record %s", record, exc_info=True)
            cells[spec.text] = _cell_from_items(items, more, path, container, cut=cut)
        span_paths = None
        del buf
    if slow:
        start, end = doc.record_span(record)
        cells.update(huge_cells(doc, record, (start, end), member, key, slow, budget, cancel))
    return cells


def stream_limit(settings: Any) -> int:
    """Records larger than this many bytes are streamed, not parsed (:data:`STREAM_RECORD_BYTES`
    or ``Settings.large_node_bytes`` when smaller)."""
    return min(int(settings.large_node_bytes), STREAM_RECORD_BYTES)


def _parse_record(doc: Any, record: int) -> tuple[str | None, Any]:
    """``(member key, parsed value)`` of a record small enough to parse (worker threads)."""
    buf = doc.record_bytes(record)
    if doc.member_records:
        key, offset = doc.split_member(buf)
        return key, codec.loads(buf[offset:])
    return None, codec.loads(buf)


def load_row(doc: Any, record: int, specs: tuple[ColumnSpec, ...], gen: int, *, budget: int | None = None,
             cancel: CancelToken | None = None) -> _Row:
    """Load record ``record`` of ``doc`` with the cells of ``specs`` (worker threads):
    parsed when it has at most :func:`stream_limit` bytes (the value is kept up to
    :data:`KEEP_VALUE_BYTES`); a bigger record is never parsed whole: up to
    ``Settings.large_node_bytes`` its cells come from :func:`extracted_cells` (Rust core),
    beyond from :func:`huge_cells` (stream)."""
    start, end = doc.record_span(record)
    size = end - start
    member = bool(doc.member_records)
    settings = doc.settings
    if size > stream_limit(settings):
        key = doc.record_key(record) if member else None
        if budget is None:
            budget = capture_budget(settings)
        huge = size > settings.large_node_bytes
        if not huge and core.BACKEND == "rust":
            cells = extracted_cells(doc, record, member, key, specs, budget, cancel)
        else:
            cells = huge_cells(doc, record, (start, end), member, key, specs, budget, cancel)
        return _Row(record, key, _NOVALUE, huge, cells, size, gen, streamed=True)
    key, value = _parse_record(doc, record)
    cells = {spec.text: cell_data(value, spec, record, key) for spec in specs}
    return _Row(record, key, value if size <= KEEP_VALUE_BYTES else _NOVALUE, False, cells, size, gen)


class _FetchHolder:
    """The columns the loader worker computes (replaced by the GUI thread)."""

    __slots__ = ("lock", "specs", "gen")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.specs: tuple[ColumnSpec, ...] = ()
        self.gen = 0

    def set(self, specs: tuple[ColumnSpec, ...], gen: int) -> None:
        with self.lock:
            self.specs = specs
            self.gen = gen

    def snapshot(self) -> tuple[tuple[ColumnSpec, ...], int]:
        with self.lock:
            return self.specs, self.gen


def _pin(doc: Any) -> Any:
    """A view of ``doc`` bound to the overlay state shown now (``doc.pinned()``): a batch of
    reads never mixes two states of an edited document (the live document when it cannot be
    pinned)."""
    pin = getattr(doc, "pinned", None)
    if pin is None:
        return doc
    try:
        return pin()
    except (ValueError, TypeError):
        return doc


def _not_indexed_yet(doc: Any, record: int) -> bool:
    """``record`` lies beyond the records a growing index holds so far: not loaded yet (never an
    error -- the loader would keep the error after the index got there)."""
    try:
        return not doc.index_complete and record >= int(doc.record_count)
    except Exception:  # noqa: BLE001 - a closed document: read (and fail) as before
        return False


def _fetch_rows(holder: _FetchHolder, doc: Any, keys: list, cancel: CancelToken) -> dict[int, _Row]:
    specs, gen = holder.snapshot()
    budget = capture_budget(doc.settings)
    out: dict[int, _Row] = {}
    for record in keys:
        cancel.check()
        if _not_indexed_yet(doc, record):
            continue
        try:
            out[record] = load_row(doc, record, specs, gen, budget=budget, cancel=cancel)
        except Cancelled:
            raise
        except JsonReshapeError as exc:
            out[record] = _Row(record, None, _NOVALUE, False, {}, 0, gen, error=str(exc))
        except (OSError, ValueError, IndexError) as exc:
            log.debug("table: record %s could not be loaded", record, exc_info=True)
            out[record] = _Row(record, None, _NOVALUE, False, {}, 0, gen,
                               error=t("gui.table.load_failed", error=type(exc).__name__))
    return out


def _copy_text_cached(row: _Row, spec: ColumnSpec) -> str | None:
    """Clipboard text of a cell from a cached row without I/O; ``None`` when the record
    must be read again (a container of a row whose value is not kept)."""
    if row.error is not None:
        return ""
    if spec.kind in ("key", "index"):
        return copy_text(None, spec, row.record, row.key)
    if row.value is not _NOVALUE:
        return copy_text(row.value, spec, row.record, row.key)
    cd = row.cells.get(spec.text)
    if cd is None:
        return None
    if cd.exact or row.huge:
        return cd.full       # (a huge record: [n] / {n}, cut strings -- nothing better can be loaded)
    return None


def _row_copy_texts(doc: Any, record: int, specs: list[ColumnSpec], cached: Any,
                    cancel: CancelToken | None) -> list[str]:
    """Clipboard texts of one record's cells (worker thread; reads the record when the
    cache cannot answer)."""
    if isinstance(cached, _Row):
        texts = [_copy_text_cached(cached, spec) for spec in specs]
        if all(x is not None for x in texts):
            return texts  # type: ignore[return-value]
    start, end = doc.record_span(record)
    settings = doc.settings
    size = end - start
    if size > settings.large_node_bytes:
        row = load_row(doc, record, tuple(specs), 0, cancel=cancel)
        return [row.cells[spec.text].full if spec.text in row.cells else "" for spec in specs]
    if size > stream_limit(settings):
        return _large_copy_texts(doc, record, (start, end), specs, capture_budget(settings), cancel)
    key, value = _parse_record(doc, record)
    return [copy_text(value, spec, record, key) for spec in specs]


_WHOLE = PathPattern(())


def _span_value(doc: Any, buf: bytes, base: int, start: int, end: int, budget: int,
                cancel: CancelToken | None) -> Any:
    """The value at ``buf[start:end]`` (``base``: the file offset of ``buf``): parsed when
    small; a container above :data:`STREAM_RECORD_BYTES` is built by :func:`stream_values`
    (bounded GIL holds) -- or, beyond the capture ``budget``, stands as its size
    (a :class:`_Box`: ``[n]`` / ``{n}``, like the cells of a huge record)."""
    first = buf[start:start + 1]
    if end - start > STREAM_RECORD_BYTES and first in (b"{", b"["):
        try:
            got = stream_values(doc, (base + start, base + end), [_WHOLE], False, max_bytes=budget, cancel=cancel)
        except CaptureLimitExceeded:
            box = _Box(first == b"{")
            box.count = _child_count(buf, start, end, box.is_map, cancel)
            return box
        return got[0][0]
    return codec.loads(buf[start:end])


def _large_copy_texts(doc: Any, record: int, span: tuple[int, int], specs: list[ColumnSpec], budget: int,
                      cancel: CancelToken | None) -> list[str]:
    """Clipboard texts of a record too large to parse whole (complete values, containers as
    compact JSON): the Rust extractor finds the matched values, each is decoded on its own
    (:func:`_span_value`); ``**`` columns (or the pure-Python core) use
    :func:`stream_values`. A value beyond the capture ``budget`` gives its size text."""
    member = bool(doc.member_records)
    key = doc.record_key(record) if member else None
    fast = [spec for spec in specs if _extractable(spec)] if core.BACKEND == "rust" else []
    slow = [spec for spec in specs if spec.kind == "value" and spec.pattern is not None and spec not in fast]
    texts: dict[int, str] = {}
    if fast:
        buf, found = _extract_spans(doc, record, fast, member)
        for spec, spans in zip(fast, found):
            parts = []
            for start, end in spans:
                if cancel is not None:
                    cancel.check()
                parts.append(_item_copy(_span_value(doc, buf, span[0], start, end, budget, cancel)))
            texts[id(spec)] = JOIN_SEP.join(parts)
        del buf
    if slow:
        try:
            got = stream_values(doc, span, [spec.pattern for spec in slow], member, max_bytes=budget, cancel=cancel)
        except CaptureLimitExceeded:
            got = None
        if got is not None:
            for k, spec in enumerate(slow):
                texts[id(spec)] = JOIN_SEP.join(_item_copy(v) for v in got.get(k, []))
        else:
            # capture each column on its own: only the columns too big for the budget copy as
            # the cells of a huge record show them, the others still completely
            over = []
            for spec in slow:
                try:
                    one = stream_values(doc, span, [spec.pattern], member, max_bytes=budget, cancel=cancel)
                except CaptureLimitExceeded:
                    over.append(spec)
                else:
                    texts[id(spec)] = JOIN_SEP.join(_item_copy(v) for v in one.get(0, []))
            if over:
                cells = huge_cells(doc, record, span, member, key, over, budget, cancel)
                for spec in over:
                    texts[id(spec)] = cells[spec.text].full
    return [copy_text(None, spec, record, key) if spec.kind in ("key", "index") else texts.get(id(spec), "")
            for spec in specs]


def _matches_text(values: list, fmt: str) -> str | None:
    """Clipboard text of the values of a wildcard cell: ``None`` for none, one value as
    itself, several as one array (:func:`jsonreshape.gui.copyfmt.value_text`)."""
    if not values:
        return None
    return copyfmt.value_text(values[0] if len(values) == 1 else list(values), fmt)


# ==================================================================================
# editing (GUI thread; values are loaded by jsonreshape.gui.editors.load_edit_value)
# ==================================================================================
class _EditTarget(NamedTuple):
    """The node a cell edit / deletion acts on: grid ``row`` / ``col`` (column id ``col_id``),
    ``record`` (position), ``kind`` -- ``value`` (an existing node), ``missing`` (a field to
    create with ``set_path``) or ``key`` (the record key of a member record) --, ``path``
    relative to the record value, ``container`` (the node is an object / array) and the
    record ``key`` (``$key`` cells)."""

    row: int
    col: int
    record: int
    col_id: str
    kind: str
    path: tuple
    container: bool = False
    key: str | None = None


class _PendingEdit:
    """An open editor dialog and the node it edits (remapped while the dialog is open; ``None``
    once that node was deleted)."""

    __slots__ = ("dialog", "target")

    def __init__(self, target: _EditTarget | None) -> None:
        self.dialog: Any = None
        self.target = target


#: a key editor's target: the record keys of a member-record document (review3 L1)
_RECORD_KEYS: Any = object()
#: "change type" menus look at the kinds of at most this many selected cells (more: all types offered)
KINDS_CELLS_MAX = 2000
#: keys of loaded rows compared at once by the record key editors (the rest: their key check)
WINDOW_KEYS_MAX = 4096


class _WindowKeys:
    """``key in taken`` for the record key editors: a loaded row of the table has that record key
    (known without I/O; the records not loaded are left to the editors' key check)."""

    __slots__ = ("view",)

    def __init__(self, view: TableView) -> None:
        self.view = view

    def __contains__(self, key: object) -> bool:
        view = self.view
        loader = view._loader
        if loader is None or view._dead or not isinstance(key, str):
            return False
        for rec in list(view._win.values())[:WINDOW_KEYS_MAX]:
            row = loader.peek(rec)
            if isinstance(row, _Row) and row.key == key:
                return True
        return False


class _RowMemberKeys:
    """``key in taken`` for the member key editors: the object ``target()`` names -- ``(record,
    path)`` -- has a member with that key in the record's parsed value the table keeps (small
    records; bigger ones and rows not loaded are left to the editors' key check)."""

    __slots__ = ("target", "view")

    def __init__(self, view: TableView, target: Any) -> None:
        self.view = view
        self.target = target

    def __contains__(self, key: object) -> bool:
        where = self.target()
        if where is None or not isinstance(key, str) or self.view._dead:
            return False
        record, path = where
        row = self.view._row(record)
        if row is None or row.error is not None or row.value is _NOVALUE:
            return False
        value = row.value
        for seg in path:
            if isinstance(value, dict) and isinstance(seg, str) and seg in value:
                value = value[seg]
            elif isinstance(value, list) and isinstance(seg, int) and not isinstance(seg, bool) \
                    and 0 <= seg < len(value):
                value = value[seg]
            else:
                return False
        return isinstance(value, dict) and key in value


# remembered quick filter texts: session -> {column path: (typed text, expression)}
_typed_filters: "weakref.WeakKeyDictionary[Any, dict[str, tuple[str, str]]]" = weakref.WeakKeyDictionary()


def _typed(session: Any) -> dict[str, tuple[str, str]]:
    try:
        found = _typed_filters.get(session)
        if found is None:
            found = {}
            _typed_filters[session] = found
        return found
    except TypeError:   # not weak-referenceable (a fake session)
        return {}


# ==================================================================================
# the quick filter row (the grid's strip under the header: VirtualGrid.set_header_extra)
# ==================================================================================
class _FilterEdit(QLineEdit):
    """The quick filter editor of one column."""

    def __init__(self, strip: _FilterStrip, path: str) -> None:
        super().__init__(strip.clip)
        self.strip = strip
        self.path = path
        self.error: str | None = None
        self.setObjectName("tableQuickFilter")
        self.setPlaceholderText(t("gui.table.qf.placeholder"))
        self.setToolTip(t("gui.table.qf.help"))
        self.setAccessibleName(t("gui.table.qf.accessible", column=path))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(QF_DEBOUNCE_MS)
        self.timer.timeout.connect(self.commit)
        self.textEdited.connect(lambda _text: self.timer.start())

    def commit(self) -> None:
        self.timer.stop()
        self.strip.view._commit_filter(self)

    def flush(self) -> None:
        """Apply a pending edit now."""
        if self.timer.isActive():
            self.commit()

    def pending(self) -> bool:
        return self.timer.isActive()

    def set_error(self, message: str | None) -> None:
        self.error = message
        if message:
            color = theme.color("danger").name()
            self.setStyleSheet(f"QLineEdit {{ border: 1px solid {color}; }}")
            self.setToolTip(message + "\n\n" + t("gui.table.qf.help"))
        else:
            self.setStyleSheet("")
            self.setToolTip(t("gui.table.qf.help"))
        self.setProperty("invalid", bool(message))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self.text():
                self.setText("")
                self.commit()
            else:
                self.strip.grid.setFocus(Qt.FocusReason.ShortcutFocusReason)
            event.accept()
            return
        if key == Qt.Key.Key_Down:
            self.flush()
            self.strip.grid.setFocus(Qt.FocusReason.ShortcutFocusReason)
            event.accept()
            return
        super().keyPressEvent(event)
        # every key stays here: the line edit lives inside the grid, an unused key would bubble up
        # to it and run a table command (F2 / Ins / Ctrl+D ...; review3 M1)
        event.accept()


class _FilterStrip(QWidget):
    """The quick filter row: one :class:`_FilterEdit` per column at the column's place
    (scrolled with the columns, the frozen column's editor stays), a "clear all" button
    over the row gutter. Lives in the grid's strip under the header
    (:meth:`~jsonreshape.gui.vgrid.VirtualGrid.set_header_extra`) and follows
    ``header_geometry_changed``."""

    def __init__(self, grid: VirtualGrid, view: TableView) -> None:
        super().__init__(grid)
        self.grid = grid
        self.view = view
        self.setObjectName("tableFilterStrip")
        self.setAutoFillBackground(True)
        self.clip = QWidget(self)
        self.corner = QToolButton(self)
        self.corner.setObjectName("tableClearQuickFilters")
        self.corner.setText("✕")
        self.corner.setAutoRaise(True)
        self.corner.setToolTip(t("gui.table.qf.clear_all.tip"))
        self.corner.clicked.connect(view.clear_quick_filters)
        self.editors: dict[str, _FilterEdit] = {}
        self._height = self._measure()
        grid.header_geometry_changed.connect(self.relayout)

    def _measure(self) -> int:
        probe = QLineEdit()             # no parent: never shown, freed with the reference
        probe.setFont(self.font())
        return probe.sizeHint().height() + 2

    def preferred_height(self) -> int:
        return self._height

    def refresh_height(self) -> bool:
        """Measure again (the UI font changed); True when the height changed."""
        height = self._measure()
        if height == self._height:
            return False
        self._height = height
        return True

    def editor(self, path: str) -> _FilterEdit | None:
        return self.editors.get(path)

    def sync_columns(self) -> None:
        """Create / drop editors to match the grid's columns."""
        ids = {col.id for col in self.grid.columns()}
        for path in list(self.editors):
            if path not in ids:
                ed = self.editors.pop(path)
                ed.timer.stop()
                ed.hide()
                ed.deleteLater()
        for path in ids:
            if path not in self.editors:
                self.editors[path] = _FilterEdit(self, path)
        self.view._sync_filter_editors()
        self.relayout()

    def relayout(self) -> None:
        g = self.grid
        if not g.header_extra_visible():
            return
        header = g.header()
        w, h = self.width(), self.height()
        cols = g.columns()
        gw = g.gutter_width()
        frozen = -1
        if g.frozen_columns():
            frozen = next((i for i, c in enumerate(cols) if c.visible), -1)
        left = gw + (cols[frozen].width if frozen >= 0 else 0)
        self.corner.setGeometry(0, 1, max(0, gw - 1), max(0, h - 2))
        self.corner.setVisible(gw >= 14)
        self.clip.setGeometry(left, 0, max(0, w - left), h)
        for i, col in enumerate(cols):
            ed = self.editors.get(col.id)
            if ed is None:
                continue
            if not col.visible:
                ed.hide()
                continue
            rect = header.section_rect(i)
            parent = self if i == frozen else self.clip
            if ed.parentWidget() is not parent:
                ed.setParent(parent)
            x = rect.x() if i == frozen else rect.x() - left
            ed.setGeometry(x, 1, max(0, rect.width() - 1), max(0, h - 2))
            ed.show()
            if i == frozen:
                ed.raise_()


# ==================================================================================
# column chooser
# ==================================================================================
_PATH_ROLE = Qt.ItemDataRole.UserRole


def _field_hint(schema: Any, path: str) -> str:
    """``string, number · 97%``: the main types and the presence of a field (schema)."""
    if path == KEY_COLUMN:
        return t("gui.table.col.key")
    if path == INDEX_COLUMN:
        return t("gui.table.col.index")
    if schema is None:
        return ""
    try:
        fi = schema.find(path)
    except JsonReshapeError:
        return ""
    if fi is None:
        return t("gui.table.chooser.not_in_schema")
    types = ", ".join(list(fi.types)[:2])
    pct = fi.presence * 100.0
    pct_text = f"{pct:.0f}%" if pct >= 1 or pct == 0 else "<1%"
    return f"{types} · {pct_text}" if types else pct_text


class ColumnChooserDialog(QDialog):
    """Choose, order and add table columns: a search box, a checkable list (the current
    columns first, then the schema's fields with type / presence hints), a custom path
    entry, move up / down and "Standart" (the default columns)."""

    def __init__(self, columns: list[str], schema: Any = None, parent: QWidget | None = None, *,
                 defaults: list[str] | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tableColumnChooser")
        self.setWindowTitle(t("gui.table.chooser.title"))
        self.setSizeGripEnabled(True)
        self._schema = schema
        self._defaults = list(defaults) if defaults is not None else None
        lay = QVBoxLayout(self)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(t("gui.table.chooser.search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_search)
        lay.addWidget(self.search)

        mid = QHBoxLayout()
        self.list = QListWidget(self)
        self.list.setObjectName("tableChooserList")
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        mid.addWidget(self.list, 1)
        side = QVBoxLayout()
        self.up_button = QPushButton(t("gui.table.chooser.up"), self)
        self.up_button.clicked.connect(lambda: self.move_current(-1))
        self.down_button = QPushButton(t("gui.table.chooser.down"), self)
        self.down_button.clicked.connect(lambda: self.move_current(1))
        self.all_button = QPushButton(t("gui.table.chooser.check_all"), self)
        self.all_button.clicked.connect(lambda: self.set_all_checked(True))
        self.none_button = QPushButton(t("gui.table.chooser.check_none"), self)
        self.none_button.clicked.connect(lambda: self.set_all_checked(False))
        self.reset_button = QPushButton(t("gui.table.chooser.reset"), self)
        self.reset_button.setToolTip(t("gui.table.chooser.reset.tip"))
        self.reset_button.clicked.connect(self.reset_defaults)
        for b in (self.up_button, self.down_button, self.all_button, self.none_button, self.reset_button):
            b.setAutoDefault(False)
            side.addWidget(b)
        side.addStretch(1)
        mid.addLayout(side)
        lay.addLayout(mid, 1)

        custom = QHBoxLayout()
        self.custom_edit = QLineEdit(self)
        self.custom_edit.setPlaceholderText(t("gui.table.chooser.custom.placeholder"))
        self.custom_edit.setToolTip(t("gui.table.chooser.custom.tip"))
        self.custom_edit.returnPressed.connect(self._add_custom_clicked)
        self.custom_button = QPushButton(t("gui.table.chooser.custom.add"), self)
        self.custom_button.setAutoDefault(False)
        self.custom_button.clicked.connect(self._add_custom_clicked)
        custom.addWidget(self.custom_edit, 1)
        custom.addWidget(self.custom_button)
        lay.addLayout(custom)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setStyleSheet(f"color: {theme.color('danger').name()};")
        self.error_label.hide()
        lay.addWidget(self.error_label)
        self.note_label = QLabel(self)
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet(f"color: {theme.color('muted').name()};")
        lay.addWidget(self.note_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t("gui.common.ok"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t("gui.common.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        for path in columns:
            self._add_item(path, True)
        self.set_schema(schema)
        self.resize(520, 560)

    # -- items -----------------------------------------------------------------------------
    def _item_text(self, path: str) -> str:
        hint = _field_hint(self._schema, path)
        shown = copyfmt.escape_surrogates(path)
        return f"{shown}    ·  {hint}" if hint else shown

    def _add_item(self, path: str, checked: bool) -> QListWidgetItem:
        item = QListWidgetItem(self._item_text(path))
        item.setData(_PATH_ROLE, path)
        item.setFlags((item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
                      & ~Qt.ItemFlag.ItemIsDropEnabled)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        item.setToolTip(path)
        self.list.addItem(item)
        return item

    def items(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count())]

    def paths(self) -> list[str]:
        """Every listed path in list order."""
        return [item.data(_PATH_ROLE) for item in self.items()]

    def find(self, path: str) -> QListWidgetItem | None:
        for item in self.items():
            if item.data(_PATH_ROLE) == path:
                return item
        return None

    def set_checked(self, path: str, on: bool) -> bool:
        item = self.find(path)
        if item is None:
            return False
        item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        return True

    def set_all_checked(self, on: bool) -> None:
        for item in self.items():
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def columns(self) -> list[str]:
        """The checked paths in list order (the new table columns)."""
        return [item.data(_PATH_ROLE) for item in self.items() if item.checkState() == Qt.CheckState.Checked]

    def set_schema(self, schema: Any) -> None:
        """Add the schema's fields (unchecked) and refresh the hints."""
        self._schema = schema
        if schema is not None:
            have = set(self.paths())
            for path in fields.field_paths(schema):
                if path not in have:
                    self._add_item(path, False)
                    have.add(path)
            if self._defaults is None:
                self._defaults = fields.default_columns(schema)
            self.note_label.setText(t("gui.table.chooser.note", count=format_count(len(fields.field_paths(schema))),
                                      records=format_count(schema.scanned)))
        else:
            self.note_label.setText(t("gui.table.chooser.loading"))
        for item in self.items():
            item.setText(self._item_text(item.data(_PATH_ROLE)))
        self._apply_search(self.search.text())

    # -- actions ---------------------------------------------------------------------------
    def move_current(self, delta: int) -> bool:
        row = self.list.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.list.count():
            return False
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)
        return True

    def add_custom(self, text: str) -> bool:
        """Add (checked) a typed path; an invalid path shows the error and returns False."""
        try:
            path = validate_column_path(text)
        except JsonReshapeError as exc:
            self.error_label.setText(exc.message)
            self.error_label.show()
            return False
        self.error_label.hide()
        item = self.find(path)
        if item is None:
            item = self._add_item(path, True)
        else:
            item.setCheckState(Qt.CheckState.Checked)
            item.setHidden(False)
        self.list.setCurrentItem(item)
        self.list.scrollToItem(item)
        return True

    def _add_custom_clicked(self) -> None:
        if self.add_custom(self.custom_edit.text()):
            self.custom_edit.clear()

    def reset_defaults(self) -> None:
        """Check exactly the default columns, in their order, at the top."""
        defaults = list(self._defaults or [ROOT_COLUMN])
        for path in defaults:
            if self.find(path) is None:
                self._add_item(path, False)
        items = [self.list.takeItem(0) for _ in range(self.list.count())]
        by_path = {item.data(_PATH_ROLE): item for item in items}
        ordered = [by_path[p] for p in defaults if p in by_path]
        rest = [item for item in items if item.data(_PATH_ROLE) not in set(defaults)]
        for item in ordered + rest:
            self.list.addItem(item)
            on = item.data(_PATH_ROLE) in defaults
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _apply_search(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for item in self.items():
            path = str(item.data(_PATH_ROLE))
            item.setHidden(bool(needle) and needle not in path.lower())


# ==================================================================================
# the view
# ==================================================================================
def _column_title(spec: ColumnSpec) -> str:
    if spec.kind == "key":
        return t("gui.table.col.key")
    if spec.kind == "index":
        return t("gui.table.col.index")
    if spec.text == ROOT_COLUMN:
        return t("gui.table.col.record")
    return copyfmt.escape_surrogates(spec.text)


def _default_width(spec: ColumnSpec, schema: Any) -> int:
    if spec.kind == "index":
        return 80
    if spec.kind == "key":
        return 160
    main = None
    if schema is not None and spec.error is None:
        try:
            fi = schema.find(spec.text)
        except JsonReshapeError:
            fi = None
        main = fi.main_type if fi is not None else None
    if main in ("number", "bool", "null"):
        return 110
    if main in ("object", "array"):
        return 90
    return 170


def _layout_variants(entry: Any) -> list[dict]:
    """The layouts of a file's ``GuiState`` entry: the top-level one first, then
    ``"others"`` (each without ``"others"``)."""
    if not isinstance(entry, dict):
        return []
    top = {k: v for k, v in entry.items() if k != "others"}
    out = [top] if top.get("columns") is not None or "kind" in top else []
    others = entry.get("others")
    if isinstance(others, list):
        out += [dict(v) for v in others if isinstance(v, dict)]
    return out


def _same_records(layout: dict, kind: str, record_path: list | None) -> bool:
    """``layout`` was made for records of this kind and record path."""
    return "kind" in layout and layout.get("kind") == kind and layout.get("record_path") == record_path


def _pack_layouts(variants: list[dict]) -> dict | None:
    """One ``GuiState`` entry for ``variants`` (most recent first; ``None``: none left)."""
    if not variants:
        return None
    entry = dict(variants[0])
    rest = variants[1:LAYOUT_VARIANTS_MAX]
    if rest:
        entry["others"] = rest
    return entry


class TableView(ViewBase):
    """Records as rows, fields as columns (see the module doc)."""

    view_id = "table"
    #: the keys the menus show for edit commands where they differ from ``actions.EDIT_COMMANDS``
    #: (``actions.command_keys``): records move without keys here (Alt+Down opens the quick filter)
    edit_keys = {"move_up": None, "move_down": None}

    selection_changed = Signal()      # the grid's cell selection changed (status bar)
    columns_changed = Signal()        # the columns (paths / order / visibility) changed
    cells_loaded = Signal()           # some rows finished loading (tests / status)

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(session, parent)
        self._dead = False
        self._activated = False
        self._doc: Any = None
        self._loader: RowLoader | None = None
        self._holder = _FetchHolder()
        self._specs: dict[str, ColumnSpec] = {}
        self._colgen = 0
        self._columns_source = "none"        # none | placeholder | default | layout | user
        self._layout_dirty = False
        self._schema: Any = None
        self._schema_wanted = False
        self._win: dict[int, int] = {}       # view position -> record (current view)
        self._refetch: set[int] = set()
        self._cur_record: int | None = None
        self._following = 0
        self._resetting = False
        self._select_waiting = False
        self._follow_serial = 0
        self._last_menu: QMenu | None = None
        self._chooser: ColumnChooserDialog | None = None
        self._options: tuple | None = None      # (record path, format) of the document of self._doc
        self._schema_failed = False
        self._followed: tuple[int, tuple] | None = None   # (record, path) taken from another view
        # quick find (Ctrl+F)
        self._find_matches: dict[tuple[int, str], tuple] = {}   # (row, column id) -> spans
        self._find_order: list[tuple[int, int, str]] = []        # (row, column index, column id), sorted
        self._find_current: tuple[int, str] | None = None
        self._find_capped = False
        self._find_gen = 0
        self._find_task: Any = None
        self._find_queue: list[bool] = []    # F3 presses (backwards?) waiting for a running search
        self._settings_signal: Any = None
        # editing (§12.9)
        self._data_gen = int(getattr(session, "data_gen", 0) or 0)   # the data generation handled last
        self._absorb_view_change = False     # the view_changed that follows a structural data change
        self._edit_serial = 0                # the newest value load / reference collection
        self._edit_task: Any = None
        self._inline: Any = None             # the open InlineEditor
        self._inline_target: _EditTarget | None = None
        self._pending: _PendingEdit | None = None    # the open editor dialog and its node
        self._anchor_serial = 0
        try:
            self._copy_headers = bool(session.app.state.get(STATE_COPY_HEADERS, False))
        except Exception:
            self._copy_headers = False

        self._select_timer = QTimer(self)
        self._select_timer.setSingleShot(True)
        self._select_timer.setInterval(SELECT_DEBOUNCE_MS)
        self._select_timer.timeout.connect(self._emit_current)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(LAYOUT_SAVE_MS)
        self._layout_timer.timeout.connect(self.save_layout)
        self._refetch_timer = QTimer(self)
        self._refetch_timer.setSingleShot(True)
        self._refetch_timer.setInterval(0)
        self._refetch_timer.timeout.connect(self._do_refetch)
        self._find_timer = QTimer(self)
        self._find_timer.setSingleShot(True)
        self._find_timer.setInterval(QUICK_FIND_RESCAN_MS)
        self._find_timer.timeout.connect(self._find_rescan)

        self._build_ui()
        signal = getattr(getattr(session, "app", None), "settings_changed", None)
        if signal is not None:
            signal.connect(self._on_settings_changed)
            self._settings_signal = signal
        session.opened.connect(self._on_opened)
        session.view_changed.connect(self._on_view_changed)
        session.count_changed.connect(self._on_count_changed)
        session.view_busy_changed.connect(self._on_busy)
        session.current_changed.connect(self._on_session_current)
        session.state_changed.connect(self._on_state)
        data_changed = getattr(session, "data_changed", None)
        if data_changed is not None:
            data_changed.connect(self._on_data_changed)
        index_finished = getattr(session, "index_finished", None)
        if index_finished is not None:
            index_finished.connect(self._on_index_finished)
        if getattr(session, "doc", None) is not None:
            self._on_opened()
        else:
            self._apply_columns([(ROOT_COLUMN, None, True)], source="none")
            self._update_placeholder()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 2, 6, 2)
        bar.setSpacing(4)
        self.columns_button = QToolButton(self)
        self.columns_button.setObjectName("tableColumnsButton")
        self.columns_button.setText(t("gui.table.columns"))
        self.columns_button.setToolTip(t("gui.table.columns.tip"))
        self.columns_button.setAutoRaise(True)
        self.columns_button.clicked.connect(self.open_column_chooser)
        self.filter_button = QToolButton(self)
        self.filter_button.setObjectName("tableFilterRowButton")
        self.filter_button.setText(t("gui.table.qf.row"))
        self.filter_button.setToolTip(t("gui.table.qf.row.tip"))
        self.filter_button.setCheckable(True)
        self.filter_button.setAutoRaise(True)
        self.filter_button.toggled.connect(self._filter_button_toggled)
        self.clear_button = QToolButton(self)
        self.clear_button.setObjectName("tableClearFiltersButton")
        self.clear_button.setText(t("gui.table.qf.clear_all"))
        self.clear_button.setToolTip(t("gui.table.qf.clear_all.tip"))
        self.clear_button.setAutoRaise(True)
        self.clear_button.clicked.connect(self.clear_quick_filters)
        self.busy_label = QLabel(self)
        self.busy_label.setObjectName("tableBusy")
        self.busy_label.setStyleSheet(f"color: {theme.color('muted').name()};")
        self.busy_label.hide()
        for w in (self.columns_button, self.filter_button, self.clear_button):
            bar.addWidget(w)
        bar.addSpacing(8)
        bar.addWidget(self.busy_label)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.grid = VirtualGrid(self)
        self.grid.setObjectName("tableGrid")
        self.grid.set_selection_mode("cells")
        self.grid.set_grid_lines(True)
        self.grid.set_row_labels(True)
        self.grid.set_font(mono_font(self.session))
        self.grid.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.strip = _FilterStrip(self.grid, self)
        self.grid.set_header_extra(self.strip, self.strip.preferred_height())
        self.grid.set_key_handler(self._grid_key)
        self.grid.current_changed.connect(self._on_current_changed)
        self.grid.activated.connect(self._on_activated)
        self.grid.context_menu_requested.connect(self._on_context_menu)
        self.grid.header_clicked.connect(self._on_header_clicked)
        self.grid.header_context_menu.connect(self._on_header_menu)
        self.grid.columns_changed.connect(self._on_grid_columns_changed)
        self.grid.selection_changed.connect(self.selection_changed)
        self.grid.viewport_changed.connect(self._on_viewport_changed)
        self.grid.link_activated.connect(self._on_link)
        lay.addWidget(self.grid, 1)
        self.find_bar = quickfind.QuickFindBar(self, hint=t("gui.table.find.hint"))
        self.find_bar.setObjectName("tableFindBar")
        self.find_bar.search_changed.connect(self._find_search)
        self.find_bar.next_requested.connect(lambda: self.find_next(False))
        self.find_bar.prev_requested.connect(lambda: self.find_next(True))
        self.find_bar.closed.connect(self._find_closed)
        lay.addWidget(self.find_bar)
        self.setFocusProxy(self.grid)
        self.filter_button.blockSignals(True)
        self.filter_button.setChecked(True)
        self.filter_button.blockSignals(False)
        self.grid.set_source(self)
        self._update_clear_button()

    # ------------------------------------------------------------------ GridSource
    def row_count(self) -> int:
        if self._doc is None or getattr(self.session, "doc", None) is None:
            return 0
        return self.session.view_len()

    def rows_shown(self, first: int, last: int) -> None:
        loader = self._loader
        if loader is None or self._dead:
            return
        loader.request(self._records(first, last))

    def row_label(self, row: int) -> str | None:
        rec = self._record_of(row)
        return None if rec is None else f"#{rec}"

    def cell(self, row: int, column: Column) -> Cell | None:
        rec = self._record_of(row)
        loader = self._loader
        if rec is None or loader is None:
            return None
        data = loader.get(rec)
        if data is None:
            return None
        if isinstance(data, LoadFailed):
            return Cell(text=data.text, role="error", italic=True, tooltip=data.text)
        if data.error is not None:
            return Cell(text=data.error, role="error", italic=True, tooltip=data.error)
        cd = self._cell_data(data, column.id)
        if cd is None:
            return None
        if self._find_matches:
            hit = self._find_matches.get((row, column.id))
            if hit is not None:
                if self._find_current == (row, column.id):
                    hit = tuple((a, b, True) for a, b in hit)
                return dataclasses.replace(cd.cell, spans=hit)
        return cd.cell

    # ------------------------------------------------------------------ positions / rows
    def _records(self, first: int, last: int) -> list[int]:
        """Record numbers of view positions ``first..last`` (cached for painting)."""
        if last < first:
            return []
        rs = self.session.view_recordset()
        if rs is None:
            return []
        try:
            arr = rs[first:last + 1]
        except Exception:   # a set closed meanwhile: the view_changed reset follows
            log.debug("table: reading view positions failed", exc_info=True)
            return []
        if len(self._win) > 20000:
            self._win.clear()
        out = []
        for k, rec in enumerate(arr):
            rec = int(rec)
            self._win[first + k] = rec
            out.append(rec)
        return out

    def _record_of(self, pos: int) -> int | None:
        if pos is None or pos < 0:
            return None
        rec = self._win.get(pos)
        if rec is not None:
            return rec
        try:
            rec = int(self.session.record_at(pos))
        except Exception:
            return None
        if len(self._win) > 20000:
            self._win.clear()
        self._win[pos] = rec
        return rec

    def record_at_row(self, row: int) -> int | None:
        """The record number shown in grid row ``row`` (``None`` when out of range)."""
        return self._record_of(row)

    def _row(self, record: int) -> _Row | None:
        loader = self._loader
        if loader is None:
            return None
        data = loader.peek(record)
        return data if isinstance(data, _Row) else None

    def _cell_data(self, row: _Row, col_id: str) -> _CellData | None:
        cd = row.cells.get(col_id)
        if cd is not None:
            return cd
        spec = self._specs.get(col_id)
        if spec is None:
            return None
        if spec.kind in ("key", "index") or spec.pattern is None:
            cd = cell_data(None, spec, row.record, row.key)
        elif row.value is not _NOVALUE:
            cd = cell_data(row.value, spec, row.record, row.key)    # a small parsed record: cheap
        else:
            if row.gen != self._colgen and row.record not in self._refetch:
                self._refetch.add(row.record)       # loaded before this column existed
                self._refetch_timer.start()
            return None
        row.cells[col_id] = cd
        if self._loader is not None and cd is not _EMPTY:
            self._loader.account(row.record, _cell_cost(cd))    # the cache stays within its budget
        return cd

    def _do_refetch(self) -> None:
        keys, self._refetch = list(self._refetch), set()
        if keys and self._loader is not None:
            self._loader.invalidate(keys)

    def _make_ref(self, record: int, path: tuple) -> NodeRef:
        doc = self.session.doc
        if doc is not None and doc.kind == "single":
            return NodeRef(None, tuple(path))
        return NodeRef(record, tuple(path))

    def _ref_at(self, row: int, col: int, *, require_loaded: bool) -> NodeRef | None:
        rec = self._record_of(row)
        if rec is None:
            return None
        cols = self.grid.columns()
        spec = self._specs.get(cols[col].id) if 0 <= col < len(cols) else None
        raw = self._loader.peek(rec) if self._loader is not None else None
        if isinstance(raw, LoadFailed):
            return self._make_ref(rec, ())
        if isinstance(raw, _Row):
            if raw.error is not None or spec is None:
                return self._make_ref(rec, ())
            cd = self._cell_data(raw, spec.text)
            if cd is not None:
                return self._make_ref(rec, cd.path)
        if require_loaded:
            return None
        if spec is not None and spec.pattern is not None and spec.pattern.is_concrete:
            return self._make_ref(rec, spec.prefix())
        return self._make_ref(rec, ())

    # ------------------------------------------------------------------ session events
    def _on_opened(self) -> None:
        if self._dead:
            return
        doc = self.session.doc
        if doc is None:
            return
        options = self._doc_options()
        if doc is not self._doc:
            self._schema = None           # another document (or record path): another schema
            self._schema_wanted = False
            self._schema_failed = False
            if self._doc is not None and self._layout_dirty:
                self.save_layout()        # remembered for the records it was made for
            if self._doc is not None and options != self._options:
                # reopened with another record path / format: the session dropped its quick
                # filters -- so do the editors (typed, pending or invalid texts alike)
                self._reset_filter_editors()
            if self._doc is not None and self._layout_key(doc) != self._layout_key(self._doc) \
                    and self._columns_source in ("default", "layout"):
                # the automatic / remembered columns described the old records: the layout
                # remembered for these records, else their defaults (the user's own columns stay)
                self._columns_source = "placeholder"
        self._doc = doc
        self._options = options
        self._data_gen = int(getattr(self.session, "data_gen", 0) or 0)
        self._absorb_view_change = False
        self._cancel_edits()
        old = self._loader
        if old is not None:
            old.close()
            old.deleteLater()
        holder = self._holder

        def fetch(keys: list, cancel: CancelToken) -> dict:
            return _fetch_rows(holder, _pin(doc), keys, cancel)     # one state per batch

        self._loader = RowLoader(fetch, capacity=LOADER_CAPACITY, batch=32, max_bytes=RECORD_CACHE_BYTES,
                                 sizeof=row_cost, name=f"table:{self.session.name}", parent=self)
        self._loader.loaded.connect(self._on_loaded)
        self._win.clear()
        self._refetch.clear()
        self._find_reset()
        if self._columns_source in ("none", "placeholder"):
            if not self._restore_layout():
                self._apply_columns([(ROOT_COLUMN, None, True)], source="placeholder")
        self._refresh_specs()
        self._resetting = True
        try:
            self.grid.reset()
        finally:
            self._resetting = False
        self._update_sort_indicators()
        self._sync_filter_editors()
        self._update_clear_button()
        self._update_placeholder()
        if self._activated:
            self._ensure_columns()

    def _doc_options(self) -> tuple:
        s = self.session
        return (getattr(s, "record_path", None), getattr(s, "format", None))

    def _on_state(self, state: str) -> None:
        if state == "closed":
            if self._loader is not None:
                self._loader.close()
        self._update_placeholder()

    def _on_count_changed(self) -> None:
        if self._dead:
            return
        self.grid.rows_changed()
        self._update_placeholder()

    def _on_index_finished(self) -> None:
        """The index is final: rows that failed while it was growing are read again."""
        if self._dead or self._loader is None or self._doc is not getattr(self.session, "doc", None):
            return
        self._loader.invalidate_failed()

    def _on_view_changed(self) -> None:
        if self._dead:
            return
        if self._absorb_view_change:
            # a view installed by a data change (DocSession._apply_change emits view_changed right
            # after data_changed): _on_data_changed has moved the rows already
            self._absorb_view_change = False
            self._update_sort_indicators()
            self._sync_filter_editors()
            self._update_clear_button()
            self._update_placeholder()
            return
        remember = self._cur_record
        col = self.grid.current()[1]
        self._win.clear()
        self._find_reset()
        self._follow_serial += 1
        self._resetting = True
        try:
            self.grid.reset()
        finally:
            self._resetting = False
        self._update_sort_indicators()
        self._sync_filter_editors()
        self._update_clear_button()
        self._update_placeholder()
        if remember is not None and self.session.doc is not None:
            self._cur_record = remember
            self._goto_record(remember, col if col >= 0 else None, scroll="center")

    def _on_busy(self, busy: bool) -> None:
        if self._dead:
            return
        self.busy_label.setText(t("gui.table.busy") if busy else "")
        self.busy_label.setVisible(bool(busy))
        if not busy:
            self._update_sort_indicators()
            self._sync_filter_editors()
            self._update_clear_button()
        self._update_placeholder()

    def _on_loaded(self) -> None:
        if self._dead:
            return
        self.grid.refresh()
        if self._select_waiting and not self._select_timer.isActive():
            self._emit_current()
        if self._find_active():
            self._find_timer.start()        # newly loaded rows join the quick find
        self.cells_loaded.emit()

    def _update_placeholder(self) -> None:
        s = self.session
        state = getattr(s, "state", "")
        if s.doc is None:
            key = "gui.table.placeholder.failed" if state == "failed" else "gui.table.placeholder.opening"
        elif getattr(s, "view_busy", False):
            key = "gui.table.placeholder.busy"
        elif state == "indexing":
            key = "gui.table.placeholder.indexing"
        elif s.filter is not None or s.quick_filters:
            key = "gui.table.placeholder.no_match"
        else:
            key = "gui.table.placeholder.empty"
        self.grid.set_placeholder_text(t(key))

    # ------------------------------------------------------------------ current / linking
    def _on_current_changed(self, row: int, col: int) -> None:
        if self._resetting or self._dead:
            return
        if row is None or row < 0:
            self._cur_record = None
            return
        before = self._cur_record
        self._cur_record = self._record_of(row)
        if self._following:
            return
        if self._cur_record != before:
            # the user moved to another row: a follow of another view's selection still looking up
            # its row (_goto_record) is older -- it must not move the cell back (a value load
            # waiting for this cell would be dropped). Column changes (also the programmatic ones
            # of new columns) leave it alone.
            self._follow_serial += 1
        self._followed = None
        self._select_waiting = False
        self._select_timer.start()

    def _emit_current(self) -> None:
        if self._dead:
            return
        row, col = self.grid.current()
        if row < 0:
            self._select_waiting = False
            return
        ref = self._ref_at(row, col, require_loaded=True)
        if ref is None:
            self._select_waiting = True       # sent once the row is loaded
            return
        self._select_waiting = False
        self.session.select(ref, origin=self)

    def _on_activated(self, row: int, col: int) -> None:
        """Enter / double click: a container cell is revealed in the tree, any other cell is
        edited (:meth:`edit_cell`)."""
        if row is None or row < 0 or col < 0 or self._dead:
            return
        rec = self._record_of(row)
        data = self._row(rec) if rec is not None else None
        cols = self.grid.columns()
        if data is None or data.error is not None or not 0 <= col < len(cols):
            return
        cd = self._cell_data(data, cols[col].id)
        if cd is None:
            return
        if not cd.container:
            self.edit_cell(row, col)
            return
        self._select_timer.stop()
        self._select_waiting = False
        self.session.select(self._make_ref(rec, cd.path), origin=self, reveal=True)

    def _column_for_path(self, path: tuple) -> int | None:
        best, best_len = None, -1
        for i, col in enumerate(self.grid.columns()):
            if not col.visible:
                continue
            spec = self._specs.get(col.id)
            if spec is None or spec.kind != "value" or spec.pattern is None:
                continue
            n = len(spec.pattern.segments)
            if n > best_len and spec.pattern.matches_prefix_of(tuple(path)):
                best, best_len = i, n
        return best

    def _on_session_current(self, ref: Any, origin: Any) -> None:
        if origin is self or self._dead or ref is None:
            return
        doc = self.session.doc
        if doc is None:
            return
        record = ref.record
        if record is None:
            if doc.kind != "single":
                return
            record = 0
        self._followed = (int(record), tuple(ref.path))
        row = self.grid.current()[0]
        # a node of the current row (e.g. the node an edit of this row selects): no scrolling
        scroll = "none" if row >= 0 and self._record_of(row) == int(record) else "center"
        self._goto_record(int(record), self._column_for_path(tuple(ref.path)), scroll=scroll)

    def _retarget_followed(self) -> None:
        """The columns changed (the schema's defaults replaced ``$``): put the current cell
        of a row taken from another view on the column of the followed node again."""
        if self._followed is None:
            return
        record, path = self._followed
        row, col = self.grid.current()
        if row < 0 or self._record_of(row) != record:
            return
        target = self._column_for_path(path)
        if target is None or target == col:
            return
        self._following += 1
        try:
            self.grid.set_current(row, target, scroll="none")
        finally:
            self._following -= 1

    def _goto_record(self, record: int, col: int | None, *, scroll: str = "center", emit: bool = False) -> None:
        """Make the row of ``record`` current (async: its view position is looked up);
        ``emit``: the session follows (a record this view created or moved to itself)."""
        self._follow_serial += 1
        serial = self._follow_serial

        def found(pos: int | None) -> None:
            if self._dead or serial != self._follow_serial or pos is None:
                return
            if pos >= self.grid.row_count():
                self.grid.rows_changed()
            self._following += 1
            try:
                self._select_timer.stop()
                self.grid.set_current(pos, col, scroll=scroll)
                self._cur_record = record
            finally:
                self._following -= 1
            if emit:
                self._followed = None
                self._select_waiting = False
                self._select_timer.start()

        self.session.position_of(record, found)

    def select_record(self, record: int, path: str | None = None) -> None:
        """Make the row of ``record`` current (and the column ``path`` when shown); async."""
        col = None
        if path is not None:
            col = next((i for i, c in enumerate(self.grid.columns()) if c.id == path), None)
        self._goto_record(record, col)

    def current_ref(self) -> Any:
        row, col = self.grid.current()
        if row < 0:
            return getattr(self.session, "current", None)
        return self._ref_at(row, col, require_loaded=False)

    # ------------------------------------------------------------------ data changes (§12.9)
    def _on_data_changed(self, cs: Any) -> None:
        """A ChangeSet was applied (edit / undo / redo / bulk / patch / restore / discard)."""
        if self._dead:
            return
        self._data_gen = int(getattr(self.session, "data_gen", 0) or getattr(cs, "gen", 0) or 0)
        reset = bool(getattr(cs, "reset", False))
        remap = getattr(cs, "remap", None)
        records = getattr(cs, "records", None)
        structural = reset or remap is not None
        self._remap_edits(cs, structural, records)
        loader = self._loader
        if loader is not None:
            if structural or records is None:
                loader.invalidate()
            elif records:
                loader.invalidate(sorted(int(r) for r in records))
        if not structural:
            # the rows stay where they are (the selection too): the changed ones load again
            if records is None or records:
                self._find_reset()
            self.grid.refresh(request=True)
            self._update_placeholder()
            return
        self._absorb_view_change = True
        QTimer.singleShot(0, self, self._end_absorb)
        self._rows_moved(cs, remap if not reset else None, reset)

    def _end_absorb(self) -> None:
        self._absorb_view_change = False

    def _rows_moved(self, cs: Any, remap: Any, reset: bool) -> None:
        """Records were inserted / deleted / moved (or the layer was reset): the rows follow --
        the top shown record stays on top, the current record stays current."""
        col = self.grid.current()[1]
        old_cur = self._cur_record
        top = self.grid.top_row()
        first, last = self.grid.visible_rows()
        window = [self._win.get(p) for p in range(max(0, top), max(top, last) + 1)] if top >= 0 else []
        new_top = None
        new_cur = None
        if not reset:
            mapper = remap.map if remap is not None else (lambda p: p)
            for rec in window:
                if rec is not None:
                    got = mapper(int(rec))
                    if got is not None:
                        new_top = got          # the first record of the screen that is still there
                        break
            if old_cur is not None:
                new_cur = mapper(int(old_cur))
            sel = getattr(cs, "select", None)
            sel_rec = sel[0] if sel is not None and len(sel) and sel[0] is not None else None
            if new_cur is None and old_cur is not None:
                if sel_rec is not None:
                    new_cur = int(sel_rec)
                else:
                    count = self.session.view_len()
                    new_cur = min(int(old_cur), count - 1) if count > 0 else None
            if self._followed is not None:
                rec, path = self._followed
                moved = remap_node_ref(self.session, NodeRef(rec, path), cs)
                self._followed = (int(moved.record), tuple(moved.path)) if moved is not None and \
                    moved.record is not None else None
        else:
            self._followed = None
        self._win.clear()
        self._refetch.clear()
        self._find_reset()
        self._follow_serial += 1
        self._resetting = True
        try:
            if reset:
                self.grid.reset()          # positions of the old state mean nothing
            else:
                self.grid.rows_changed(keep_current=False)
        finally:
            self._resetting = False
        self._cur_record = new_cur
        self._update_sort_indicators()
        self._sync_filter_editors()
        self._update_clear_button()
        self._update_placeholder()
        if new_top is not None:
            self._anchor_top(new_top)
        if new_cur is not None:
            self._goto_record(new_cur, col if col >= 0 else None, scroll="none")

    def _anchor_top(self, record: int) -> None:
        """Scroll so that ``record`` is the first shown row (its view position is looked up)."""
        self._anchor_serial += 1
        serial = self._anchor_serial

        def found(pos: int | None) -> None:
            if self._dead or serial != self._anchor_serial or pos is None:
                return
            if pos >= self.grid.row_count():
                self.grid.rows_changed()
            self.grid.scroll_to(pos, "top")

        self.session.position_of(record, found)

    def _remap_edits(self, cs: Any, structural: bool, records: Any) -> None:
        """Editors and pending edit loads after a data change: an inline editor whose row
        moved or whose record changed is cancelled (it would write over the change or into
        another row), a pending value load is dropped, a dialog keeps its node (remapped; a
        deleted node is refused when the dialog is accepted)."""
        def touched(rec: int) -> bool:
            return structural or records is None or rec in records

        tgt = self._inline_target
        if self._inline is not None and tgt is not None and touched(tgt.record):
            self._close_inline()
        if self._edit_task is not None:
            self._edit_serial += 1
            self._edit_task.cancel()
            self._edit_task = None
        pe = self._pending
        tgt = pe.target if pe is not None else None
        if tgt is not None and (structural or getattr(cs, "nodes", ())):
            moved = remap_node_ref(self.session, NodeRef(None if self._is_single() else tgt.record, tgt.path), cs)
            if moved is None:
                pe.target = None
            else:
                rec = tgt.record if moved.record is None else int(moved.record)
                pe.target = tgt._replace(record=rec, path=tuple(moved.path))
        # the key checks of the open editors answered for the old data: ask again (review3 L1)
        for editor in (self._inline, pe.dialog if pe is not None else None):
            forget = getattr(editor, "forget_key_checks", None)
            if callable(forget):
                try:
                    forget()
                except RuntimeError:       # the widget is being destroyed
                    pass

    # ------------------------------------------------------------------ editing (§12.9)
    def _member_records(self) -> bool:
        doc = self.session.doc
        return bool(doc is not None and getattr(doc, "member_records", False))

    def _is_single(self) -> bool:
        doc = self.session.doc
        return doc is not None and getattr(doc, "kind", None) == "single"

    def edit_target(self, row: int | None = None, col: int | None = None) -> tuple[_EditTarget | None, str | None]:
        """The node a cell edit acts on (default: the current cell), or ``(None, why)``
        (localized; ``why`` None: no cell)."""
        if row is None or col is None:
            row, col = self.grid.current()
        cols = self.grid.columns()
        if row is None or row < 0 or not 0 <= col < len(cols):
            return None, None
        spec = self._specs.get(cols[col].id)
        rec = self._record_of(row)
        if spec is None or rec is None:
            return None, None
        if spec.kind == "index":
            return None, t("gui.table.edit.index")
        data = self._row(rec)
        if data is None:
            return None, t("gui.table.edit.loading")
        if data.error is not None:
            return None, data.error
        if spec.kind == "key":
            if not self._member_records():
                return None, t("gui.table.edit.no_key")
            return _EditTarget(row, col, rec, spec.text, "key", (), key=data.key), None
        if spec.pattern is None:
            return None, spec.error or t("gui.table.edit.invalid_column")
        cd = self._cell_data(data, spec.text)
        if cd is None:
            return None, t("gui.table.edit.loading")
        if data.huge:
            return None, t("gui.table.edit.huge")
        if spec.pattern.is_concrete:
            if cd.count == 0:
                return _EditTarget(row, col, rec, spec.text, "missing", spec.prefix()), None
            return _EditTarget(row, col, rec, spec.text, "value", tuple(cd.path), cd.container), None
        if cd.count == 1 and spec.pattern.matches(tuple(cd.path)):
            return _EditTarget(row, col, rec, spec.text, "value", tuple(cd.path), cd.container), None
        if cd.count == 0:
            return None, t("gui.table.edit.wildcard_none")
        return None, t("gui.table.edit.wildcard_many")

    def edit_block(self, row: int | None = None, col: int | None = None) -> str | None:
        """Why the cell (default: the current one) cannot be edited now (localized), or ``None``."""
        tgt, why = self.edit_target(row, col)
        if tgt is None:
            return why or t("gui.table.edit.no_cell")
        _edit, why = edit_controller(self.session)
        return why

    def edit_cell(self, row: int | None = None, col: int | None = None, *, as_json: bool = False) -> Any:
        """Enter / F2 / double click (``as_json``: Shift+Enter): edit the cell's node -- see the
        module docstring. Returns the editor / dialog / loading task (``None``: refused, the
        reason is shown in the status bar)."""
        if row is None or col is None:
            row, col = self.grid.current()
        tgt, why = self.edit_target(row, col)
        if tgt is None:
            self._status(why or t("gui.table.edit.no_cell"))
            return None
        _edit, why = edit_controller(self.session)
        if why is not None:
            self._status(why)
            return None
        self._close_inline()
        if tgt.kind == "key":
            if as_json:
                self._status(t("gui.table.edit.key_no_json"))
                return None
            return self._open_inline(tgt, "key", tgt.key or "")
        if tgt.kind == "missing":
            return self._edit_missing(tgt, as_json)
        if as_json or tgt.container:
            return self._json_dialog_for(tgt)
        return self._load_for_edit(tgt)

    def _load_for_edit(self, tgt: _EditTarget) -> Any:
        """Read the exact value (silent task, :func:`~jsonreshape.gui.editors.load_edit_value`)
        and open the editor when it arrives -- if the cell is still current and the data did
        not change meanwhile."""
        from jsonreshape.gui import editors

        self._edit_serial += 1
        serial = self._edit_serial
        if self._edit_task is not None:
            self._edit_task.cancel()
        gen = self._data_gen

        def done(value: Any) -> None:
            if self._dead or serial != self._edit_serial:
                return
            self._edit_task = None
            if gen != self._data_gen or self.grid.current() != (tgt.row, tgt.col):
                return                   # the data changed or the user moved on meanwhile
            self._open_editor(tgt, value)

        def failed(exc: BaseException) -> None:
            if serial == self._edit_serial:
                self._edit_task = None
            if self._dead or isinstance(exc, Cancelled):
                return
            if isinstance(exc, JsonReshapeError):
                self._status(exc.message)
            else:
                report_task_error(self.session, exc, self, purpose="view")

        self._edit_task = editors.load_edit_value(self.session, self._make_ref(tgt.record, tgt.path), done,
                                                  owner=self, on_error=failed)
        return self._edit_task

    def _open_editor(self, tgt: _EditTarget, value: Any) -> Any:
        """The editor for a loaded value: a toggle (bool), the inline editor (numbers, short
        one-line strings), a string dialog (other strings; read by its own loader above the
        inline limit), a JSON dialog (null, containers, numbers too long to keep)."""
        from jsonreshape.gui import editors

        kind = value.kind
        if kind == "bool" and value.token in ("true", "false"):
            edit, why = edit_controller(self.session)
            if edit is None or why is not None:
                self._status(why or edit_unavailable_text(edit))
                return None
            edit.set_value(self._make_ref(tgt.record, tgt.path), "false" if value.token == "true" else "true")
            return None
        if kind == "number" and value.token is not None:
            return self._open_inline(tgt, "number", value.token.strip())
        if kind == "string":
            if value.decoded is not None and editors.inline_editable(value.decoded):
                return self._open_inline(tgt, "string", value.decoded)
            return self._string_dialog_for(tgt, value.decoded)
        return self._json_dialog_for(tgt)

    def _edit_missing(self, tgt: _EditTarget, as_json: bool) -> Any:
        """A missing field: an editor following the column's type in the schema (strings and
        numbers in the cell, anything else as JSON text); the value is created with
        ``set_path``."""
        kind = None if as_json else self._column_type(tgt.col_id)
        if kind in ("string", "number"):
            return self._open_inline(tgt, kind, "")
        return self._open_pending(tgt, lambda pe: self._make_json_dialog(pe, "", tgt))

    def _column_type(self, col_id: str) -> str | None:
        """The most frequent type of a column's field: from the schema when it is known, else
        from the loaded cells of the column (``None``: unknown)."""
        schema = self._schema or fields.cached_schema(self.session)
        if schema is not None:
            try:
                fi = schema.find(col_id)
            except (JsonReshapeError, ValueError):
                fi = None
            if fi is not None and fi.main_type is not None:
                return fi.main_type
        loader = self._loader
        if loader is None:
            return None
        votes: dict[str, int] = {}
        for rec in list(self._win.values())[:2000]:
            row = loader.peek(rec)
            if not isinstance(row, _Row) or row.error is not None:
                continue
            cd = row.cells.get(col_id)
            if cd is None or cd.count != 1:
                continue
            kind = {None: "string", "key": "string"}.get(cd.cell.role, cd.cell.role)
            if kind != "null":           # a null says nothing about the column's type
                votes[kind] = votes.get(kind, 0) + 1
        return max(votes, key=votes.__getitem__) if votes else None

    def _open_inline(self, tgt: _EditTarget, kind: str, text: str) -> Any:
        """An :class:`~jsonreshape.gui.editors.InlineEditor` over the cell (``kind``: string |
        number | key | json); an unchanged value commits nothing (the editor cancels)."""
        from jsonreshape.gui import editors

        self._close_inline()
        self.grid.scroll_to(tgt.row, "ensure")
        self.grid.scroll_to_column(tgt.col)
        holder: list[Any] = []

        def commit(value: str) -> None:
            ed = holder[0] if holder else None
            if ed is not None and self._inline is ed:
                self._inline = None
                target, self._inline_target = self._inline_target, None
                self._commit(target or tgt, value)

        def cancel() -> None:
            ed = holder[0] if holder else None
            if ed is not None and self._inline is ed:
                self._inline = None
                self._inline_target = None

        keys: dict[str, Any] = {}
        if kind == "key":                  # a record key: unique among the records (review3 L1)
            keys = {"taken": _WindowKeys(self), "key_check": self._key_check(lambda: _RECORD_KEYS)}
        ed = editors.InlineEditor(self.grid, tgt.row, tgt.col, kind, text, commit, cancel, **keys)
        holder.append(ed)
        self._inline = ed
        self._inline_target = tgt
        ed.setFocus(Qt.FocusReason.OtherFocusReason)
        return ed

    def _key_check(self, target: Any) -> Any:
        """The editors' ``key_check`` (review3 L1): is ``key`` a key of the object ``target()`` names
        -- ``(record, path)``, or :data:`_RECORD_KEYS` for the record keys of a member-record
        document; ``None``: nothing to compare -- in the state shown now? Looked up in a silent task
        on one pinned state (``Node.find_child_index``; record keys through the key index the edit
        session keeps when it has one)."""

        def check(key: str, reply: Any) -> None:
            where = target()
            session = self.session
            if where is None or self._dead or getattr(session, "doc", None) is None:
                reply(False)
                return
            ref = None if where is _RECORD_KEYS else self._make_ref(int(where[0]), tuple(where[1]))
            doc = task_doc(session)          # the state ``ref`` belongs to (review CL-9)

            def work(ctx: Any) -> bool:
                if doc is None:
                    return False
                node = doc.records_node() if ref is None else task_node(session, ref, ctx.cancel, doc=doc)
                if node is None or node.kind != "object":
                    return False
                return node.find_child_index(key, cancel=ctx.cancel) is not None

            session.app.tasks.run(work, title=t("gui.table.task.key_check"), kind="value", owner=self, silent=True,
                                  on_done=lambda taken: reply(bool(taken)), on_error=lambda _exc: reply(False))

        return check

    def _close_inline(self) -> None:
        """Cancel the open inline editor (nothing is committed)."""
        ed = self._inline
        self._inline = None
        self._inline_target = None
        if ed is not None:
            try:
                ed.cancel()
            except RuntimeError:         # the widget is gone already
                pass

    def _open_pending(self, tgt: _EditTarget | None, make: Any) -> Any:
        """Open one editor dialog (``make(pending) -> dialog``) for ``tgt``; a dialog still
        open is closed first."""
        self._close_dialog()
        pe = _PendingEdit(tgt)
        dlg = make(pe)
        pe.dialog = dlg
        self._pending = pe

        def finished(_result: int) -> None:
            if self._pending is pe:
                self._pending = None

        dlg.finished.connect(finished)
        return dlg

    def _close_dialog(self) -> None:
        pe = self._pending
        self._pending = None
        if pe is not None and pe.dialog is not None:
            discard = getattr(pe.dialog, "discard", None)
            try:
                if callable(discard):
                    discard()
                else:
                    pe.dialog.reject()
            except RuntimeError:
                pass

    def _dialog_title(self, tgt: _EditTarget) -> str:
        try:
            path = self.session.location_text(self._make_ref(tgt.record, tgt.path))
        except Exception:  # noqa: BLE001 - a title only
            path = f"#{tgt.record}"
        return t("gui.table.edit.title", path=path)

    def _accepted(self, pe: _PendingEdit, text: str) -> None:
        """A dialog's changed value: sent to the node the dialog edits (remapped meanwhile)."""
        if self._dead:
            return
        if pe.target is None:
            self._status(t("gui.table.edit.gone"))
            return
        self._commit(pe.target, text)

    def _make_json_dialog(self, pe: _PendingEdit, text: str, tgt: _EditTarget) -> Any:
        from jsonreshape.gui import editors

        dlg = editors.JsonEditDialog(self, text, title=self._dialog_title(tgt), indent=2,
                                     tasks=self.session.app.tasks, on_accept=lambda value: self._accepted(pe, value))
        dlg.open()
        return dlg

    def _json_dialog_for(self, tgt: _EditTarget) -> Any:
        """:class:`~jsonreshape.gui.editors.JsonEditDialog` with the node's effective text
        (its own loader: a pinned document, refused above 4 MiB)."""
        from jsonreshape.gui import editors

        ref = self._make_ref(tgt.record, tgt.path)

        def make(pe: _PendingEdit) -> Any:
            return editors.JsonEditDialog.for_node(self.session, ref, parent=self, title=self._dialog_title(tgt),
                                                   indent=2, on_accept=lambda value: self._accepted(pe, value))

        return self._open_pending(tgt, make)

    def _string_dialog_for(self, tgt: _EditTarget, decoded: str | None) -> Any:
        """:class:`~jsonreshape.gui.editors.StringDialog` with the decoded string (read by the
        dialog's own loader when it was too long to load with the cell)."""
        from jsonreshape.gui import editors

        ref = self._make_ref(tgt.record, tgt.path)

        def make(pe: _PendingEdit) -> Any:
            def accept(token: str) -> None:
                self._accepted(pe, token)

            if decoded is None:
                return editors.StringDialog.for_node(self.session, ref, parent=self, title=self._dialog_title(tgt),
                                                     on_accept=accept)
            dlg = editors.StringDialog(self, decoded, title=self._dialog_title(tgt), on_accept=accept)
            dlg.open()
            return dlg

        return self._open_pending(tgt, make)

    def _commit(self, tgt: _EditTarget, value: str) -> None:
        """Send an edited value: a record key (``$key``) or JSON text (``set_path`` for a
        missing field, else ``set_value``)."""
        edit, why = edit_controller(self.session)
        if edit is None or why is not None:
            self._status(why or edit_unavailable_text(edit))
            return
        if tgt.kind == "key":
            if isinstance(value, str) and value != tgt.key:
                edit.rename_key(self._make_ref(tgt.record, ()), value)
            return
        ref = self._make_ref(tgt.record, tgt.path)
        if tgt.kind == "missing":
            edit.set_path(ref, value)
        else:
            edit.set_value(ref, value)

    def inline_editor(self) -> Any:
        """The open inline editor (tests)."""
        return self._inline

    def edit_dialog(self) -> Any:
        """The open string / JSON / key / insert dialog (tests)."""
        return self._pending.dialog if self._pending is not None else None

    def _cancel_edits(self) -> None:
        self._edit_serial += 1
        if self._edit_task is not None:
            self._edit_task.cancel()
            self._edit_task = None
        self._close_inline()
        self._close_dialog()

    # -- the view edit protocol (jsonreshape.gui.actions: node menu / Edit menu) ---------
    def edit_value(self) -> Any:
        """"Edit value": the current cell (a container cell as JSON text)."""
        return self.edit_cell()

    def edit_as_json(self) -> Any:
        """"Edit as JSON": the current cell's node as JSON text."""
        return self.edit_cell(as_json=True)

    def rename_key(self) -> Any:
        """"Rename": the record key on a ``$key`` cell, else the key of the cell's member in
        this record (:class:`~jsonreshape.gui.editors.KeyDialog`)."""
        from jsonreshape.gui import editors

        tgt, why = self.edit_target()
        if tgt is None or tgt.kind == "missing" or (tgt.kind == "value" and not self._member_path(tgt.path)):
            self._status(why or t("gui.table.edit.no_rename"))
            return None
        _edit, why = edit_controller(self.session)
        if why is not None:
            self._status(why)
            return None
        if tgt.kind == "key":
            return self.edit_cell()
        key = str(tgt.path[-1])

        def make(pe: _PendingEdit) -> Any:
            def accept(new: str) -> None:
                if self._dead:
                    return
                if pe.target is None:
                    self._status(t("gui.table.edit.gone"))
                    return
                edit, why = edit_controller(self.session)
                if edit is None or why is not None:
                    self._status(why or edit_unavailable_text(edit))
                    return
                edit.rename_key(self._make_ref(pe.target.record, pe.target.path), new)

            def parent() -> tuple[int, tuple] | None:      # the object the key must be unique in (review3 L1)
                return None if pe.target is None else (pe.target.record, tuple(pe.target.path)[:-1])

            dlg = editors.KeyDialog(self, key, title=self._dialog_title(tgt), on_accept=accept,
                                    taken=_RowMemberKeys(self, parent), key_check=self._key_check(parent))
            dlg.open()
            return dlg

        return self._open_pending(tgt, make)

    @staticmethod
    def _member_path(path: tuple) -> bool:
        return bool(path) and isinstance(path[-1], str)

    def insert_node(self, where: str = "after") -> Any:
        """"Insert": ``before`` / ``after`` -- a record next to the current row's record;
        ``child`` -- a child of the cell's object / array."""
        if where == "child":
            return self._insert_child()
        return self.insert_record(before=where == "before")

    def duplicate_selection(self) -> Any:
        """"Duplicate" (Ctrl+D): the records of the selected rows."""
        return self.duplicate_records()

    def delete_selection(self) -> Any:
        """"Delete" (Del): the fields of the selected cells."""
        return self.delete_cells()

    def move_selection(self, delta: int) -> Any:
        """"Move up / down": the records of the selected rows as one block (base view only)."""
        why = self._move_block()
        if why is not None:
            self._status(why)
            return None
        spans = self._selected_row_spans()

        def ready(positions: Any, _count: int) -> None:
            edit, why = edit_controller(self.session)
            if edit is None or why is not None:
                self._status(why or edit_unavailable_text(edit))
                return
            edit.move_by([self._make_ref(p, ()) for p in positions], int(delta))

        return self._record_positions(spans, ready)

    def _move_block(self) -> str | None:
        why = self.record_block()
        if why is not None:
            return why
        if not bool(getattr(self.session, "view_is_base", False)):
            return t("gui.table.records.move_base_only")
        n = sum(r1 - r0 + 1 for r0, r1 in self._selected_row_spans())
        if n > EDIT_SELECTION_MAX:
            return t("gui.table.records.too_many", count=format_count(n), limit=format_count(EDIT_SELECTION_MAX))
        return None

    def change_type(self, kind: str) -> Any:
        """"Change type": the nodes of the selected cells (concrete columns, existing values)."""
        cells, why = self._selected_value_cells()
        if why is not None:
            self._status(why)
            return None
        _edit, why = edit_controller(self.session)
        if why is not None:
            self._status(why)
            return None
        return self._collect_and_run(cells, ("change_type", kind))

    def change_type_kinds(self) -> tuple[str, ...] | None:
        """The kinds of the nodes :meth:`change_type` would convert (the selected cells of concrete
        columns that hold a value) for the menus' rules; ``None`` when one of them is not loaded
        yet (or more than :data:`KINDS_CELLS_MAX` cells are selected)."""
        spans, col_idx, ranges = self._selection_cells()
        cols = self.grid.columns()
        kinds: dict[str, None] = {}
        seen = 0
        for r0, r1 in spans:
            for row in range(r0, r1 + 1):
                for ci in col_idx:
                    spec = self._specs.get(cols[ci].id)
                    if spec is None or spec.kind != "value" or spec.pattern is None or not spec.pattern.is_concrete:
                        continue
                    if not any(a <= row <= b and c0 <= ci <= c1 for a, b, c0, c1 in ranges):
                        continue
                    seen += 1
                    if seen > KINDS_CELLS_MAX:
                        return None
                    rec = self._record_of(row)
                    data = self._row(rec) if rec is not None else None
                    if data is None or data.error is not None:
                        return None
                    cd = self._cell_data(data, spec.text)
                    if cd is None:
                        return None
                    if cd.count:
                        kinds[cd.cell.role or "string"] = None
        return tuple(kinds) or None

    def paste_json(self) -> Any:
        """"Paste" (Ctrl+V): the clipboard's JSON as a new record after the current row (a
        member record gets a free key)."""
        why = self.record_block()
        if why is not None:
            self._status(why)
            return None
        from PySide6.QtGui import QGuiApplication

        text = QGuiApplication.clipboard().text()
        if not text.strip():
            self._status(t("gui.table.records.paste_empty"))
            return None
        row = self.grid.current()[0]
        rec = self._record_of(row) if row >= 0 else None
        index = int(rec) + 1 if rec is not None else None
        edit, why = edit_controller(self.session)
        if edit is None or why is not None:
            self._status(why or edit_unavailable_text(edit))
            return None
        edit.paste(None, text, index=index, on_done=self._new_record_shown)
        return True

    def edit_command_state(self, name: str) -> tuple[bool, str] | None:
        """``(enabled, reason)`` of an edit command of :mod:`jsonreshape.gui.actions` for the
        current cell / selection."""
        if name in ("edit_value", "edit_json"):
            why = self.edit_block()
            if why is None and name == "edit_json":
                tgt, _w = self.edit_target()
                if tgt is not None and tgt.kind == "key":
                    why = t("gui.table.edit.key_no_json")
            return (why is None, why or "")
        if name == "rename":
            tgt, why = self.edit_target()
            if tgt is not None and (tgt.kind == "key" or (tgt.kind == "value" and self._member_path(tgt.path))):
                return True, ""
            return False, why or t("gui.table.edit.no_rename")
        if name == "insert_child":
            tgt, why = self.edit_target()
            if tgt is not None and tgt.kind == "value" and tgt.container:
                return True, ""
            return False, why or t("gui.table.edit.no_container")
        if name in ("insert_before", "insert_after", "duplicate", "paste"):
            why = self.record_block()
            return (why is None, why or "")
        if name == "delete":
            why = self._delete_block(self.grid.current()[1], selection=True)
            return (why is None, why or "")
        if name in ("move_up", "move_down"):
            why = self._move_block()
            return (why is None, why or "")
        if name == "change_type":
            _cells, why = self._selected_value_cells()
            return (why is None, why or "")
        return None

    def _insert_child(self) -> Any:
        from jsonreshape.gui import editors

        tgt, why = self.edit_target()
        if tgt is None or tgt.kind != "value" or not tgt.container:
            self._status(why or t("gui.table.edit.no_container"))
            return None
        _edit, why = edit_controller(self.session)
        if why is not None:
            self._status(why)
            return None
        data = self._row(tgt.record)
        cd = self._cell_data(data, tgt.col_id) if data is not None else None
        is_object = cd is not None and cd.cell.role == "object"

        def make(pe: _PendingEdit) -> Any:
            def accept(result: Any) -> None:
                if self._dead:
                    return
                if pe.target is None:
                    self._status(t("gui.table.edit.gone"))
                    return
                edit, why = edit_controller(self.session)
                if edit is None or why is not None:
                    self._status(why or edit_unavailable_text(edit))
                    return
                edit.insert(self._make_ref(pe.target.record, pe.target.path), None, result.value, key=result.key)

            def parent() -> tuple[int, tuple] | None:      # the object the new member goes into (review3 L1)
                return None if pe.target is None else (pe.target.record, tuple(pe.target.path))

            dlg = editors.InsertDialog(self, title=self._dialog_title(tgt), positions=("child",), position="child",
                                       key_needed=is_object, on_accept=accept, taken=_RowMemberKeys(self, parent),
                                       key_check=self._key_check(parent))
            dlg.open()
            return dlg

        return self._open_pending(tgt, make)

    def _selected_value_cells(self) -> tuple[list[tuple[int, list[tuple]]], str | None]:
        """``(view row, [relative paths])`` of the selected cells in concrete columns (``$``
        too; the nodes are looked up later), or ``([], why)``."""
        spans, col_idx, ranges = self._selection_cells()
        if not spans or not col_idx:
            return [], t("gui.table.edit.no_cell")
        cols = self.grid.columns()
        targets = []
        wild = False
        for ci in col_idx:
            spec = self._specs.get(cols[ci].id) or column_spec(cols[ci].id)
            if spec.kind != "value" or spec.pattern is None:
                continue
            if not spec.pattern.is_concrete:
                wild = True
                continue
            targets.append((ci, spec))
        if not targets:
            return [], t("gui.table.delete.wildcard") if wild else t("gui.table.delete.nothing")
        n_rows = sum(r1 - r0 + 1 for r0, r1 in spans)
        if n_rows * len(targets) > EDIT_SELECTION_MAX:
            return [], t("gui.table.delete.too_many", count=format_count(n_rows * len(targets)),
                         limit=format_count(EDIT_SELECTION_MAX))
        full_rect = all(any(r0 <= rr0 and rr1 <= r1 and c0 <= min(col_idx) and max(col_idx) <= c1
                            for r0, r1, c0, c1 in ranges) for rr0, rr1 in spans)
        cells: list[tuple[int, list[tuple]]] = []
        for r0, r1 in spans:
            for row in range(r0, r1 + 1):
                paths = [spec.prefix() for ci, spec in targets
                         if full_rect or any(a <= row <= b and c0 <= ci <= c1 for a, b, c0, c1 in ranges)]
                if paths:
                    cells.append((row, paths))
        return cells, None

    # -- deleting fields -----------------------------------------------------------------
    def delete_cells(self) -> Any:
        """Del: delete the fields of the selected cells (concrete column paths; ``$`` cells:
        the records). The nodes are looked up in a silent task (rows not loaded too) and
        deleted in ONE step. Returns the task (``None``: nothing to delete / refused)."""
        _edit, why = edit_controller(self.session)
        if why is not None:
            self._status(why)
            return None
        cells, why = self._selected_value_cells()
        if why is not None:
            self._status(why)
            return None
        if self._is_single():
            cells = [(row, [p for p in paths if p]) for row, paths in cells]
            cells = [(row, paths) for row, paths in cells if paths]
            if not cells:
                self._status(t("gui.table.records.single"))
                return None
        return self._collect_and_run(cells, ("delete",), wild=self._selection_has_wildcards())

    def _selection_has_wildcards(self) -> bool:
        _spans, col_idx, _ranges = self._selection_cells()
        cols = self.grid.columns()
        for ci in col_idx:
            spec = self._specs.get(cols[ci].id) or column_spec(cols[ci].id)
            if spec.kind == "value" and spec.pattern is not None and not spec.pattern.is_concrete:
                return True
        return False

    def _delete_block(self, col: int, *, selection: bool = False) -> str | None:
        """Why Del does nothing in column ``col`` (``selection``: in the selected columns), or
        ``None``."""
        _edit, why = edit_controller(self.session)
        if why is not None:
            return why
        if selection:
            _cells, why = self._selected_value_cells()
            return why
        cols = self.grid.columns()
        if not 0 <= col < len(cols):
            return t("gui.table.edit.no_cell")
        spec = self._specs.get(cols[col].id) or column_spec(cols[col].id)
        if spec.kind != "value" or spec.pattern is None:
            return t("gui.table.delete.nothing")
        if not spec.pattern.is_concrete:
            return t("gui.table.delete.wildcard")
        return None

    def _collect_and_run(self, cells: list[tuple[int, list[tuple]]], command: tuple, *, wild: bool = False) -> Any:
        """Look up the nodes ``cells`` name (view row -> relative paths) in one pinned state
        (silent task; missing ones are skipped), then run ``command`` (``(name, *args)`` of the
        edit controller) on them -- when the data did not change meanwhile."""
        rs = self.session.view_recordset()
        if rs is None:
            return None
        session = self.session
        single = self._is_single()
        gen = self._data_gen
        self._edit_serial += 1
        serial = self._edit_serial
        doc = task_doc(session)              # the state of the view positions in ``rs`` (review CL-9)

        def work(ctx: Any) -> list:
            if doc is None:
                raise UserInputError("gui.table.edit.not_ready")
            refs: list[Any] = []
            seen: set = set()
            for k, (row, paths) in enumerate(cells):
                if k & 63 == 0:
                    ctx.cancel.check()
                rec = int(rs[row])
                for path in paths:
                    found = resolve_in(doc, NodeRef(None if single else rec, tuple(path)), ctx.cancel)
                    if found is None:
                        continue
                    key = (found.record, found.ipath)
                    if key not in seen:
                        seen.add(key)
                        refs.append(found)
            # a whole record (a "$" cell) covers the nodes of the same record selected beside it
            whole = {r.record for r in refs if not r.path and r.record is not None}
            return [r for r in refs if not r.path or r.record not in whole]

        def done(refs: list) -> None:
            if self._dead or serial != self._edit_serial:
                return
            if gen != self._data_gen:
                self._status(t("gui.table.edit.changed"))
                return
            if not refs:
                self._status(t("gui.table.delete.wildcard") if wild else t("gui.table.delete.missing"))
                return
            edit, why = edit_controller(self.session)
            if edit is None or why is not None:
                self._status(why or edit_unavailable_text(edit))
                return
            getattr(edit, command[0])(refs, *command[1:])
            if wild:
                self._status(t("gui.table.delete.wildcard"))

        def failed(exc: BaseException) -> None:
            if self._dead or isinstance(exc, Cancelled):
                return
            if isinstance(exc, JsonReshapeError):
                self._status(exc.message)
            else:
                report_task_error(self.session, exc, self, purpose="view")

        return session.app.tasks.run(work, title=t("gui.table.edit.collect"), kind="value", owner=self,
                                     silent=True, on_done=done, on_error=failed)

    # -- records -------------------------------------------------------------------------
    def _selected_row_spans(self) -> list[tuple[int, int]]:
        """Merged row ranges of the selection (the current row when nothing is selected)."""
        spans, _cols, _ranges = self._selection_cells()
        if not spans:
            row = self.grid.current()[0]
            return [(row, row)] if row >= 0 else []
        return spans

    def _new_record_shown(self, cs: Any) -> None:
        """After this view inserted / duplicated / pasted records: a filtered or sorted view does
        not get the new records (they have no place in it until it is computed again) -- say so."""
        if self._dead or cs is None or bool(getattr(self.session, "view_is_base", True)):
            return
        sel = getattr(cs, "select", None)
        rec = sel[0] if sel is not None and len(sel) else None
        if rec is None:
            return

        def found(pos: int | None) -> None:
            if pos is None and not self._dead:
                self._status(t("gui.table.records.not_in_view"), 8000)

        self.session.position_of(int(rec), found)

    def bulk_selection(self) -> Any:
        """The "selected records" scope of the bulk dialog: the records of the selected rows (the
        current row without a selection), resolved lazily in the preview task
        (:class:`~jsonreshape.gui.bulkdialog.SelectionScope`); ``None`` without rows."""
        from jsonreshape.gui.bulkdialog import SelectionScope

        spans = self._selected_row_spans()
        return SelectionScope.for_view_rows(self.session, spans) if spans else None

    def record_block(self) -> str | None:
        """Why record commands (insert / duplicate / delete / move records) do not apply now,
        or ``None``."""
        if self.session.doc is None:
            return t("gui.table.edit.no_cell")
        if self._is_single():
            return t("gui.table.records.single")
        _edit, why = edit_controller(self.session)
        return why

    def _record_positions(self, spans: list[tuple[int, int]], on_ready: Any) -> Any:
        """The record positions of the view rows ``spans`` -> ``on_ready(positions, count)``: a
        sorted list for up to :data:`EDIT_SELECTION_MAX` rows, a range / lazy iterable of the
        rows in the base view, else a :class:`~jsonreshape.recordset.RecordSet` collected in a
        task (``gen`` = the controller's ``applied_gen``)."""
        rs = self.session.view_recordset()
        if rs is None or not spans:
            return None
        n = sum(r1 - r0 + 1 for r0, r1 in spans)
        base = bool(getattr(self.session, "view_is_base", False))
        if n <= EDIT_SELECTION_MAX:
            recs: set[int] = set()
            for r0, r1 in spans:
                recs.update(range(r0, r1 + 1) if base else map(int, rs[r0:r1 + 1]))
            return on_ready(sorted(recs), len(recs))
        if base:
            # the base view: rows are record positions (the edit worker reads them in chunks)
            positions: Any = range(spans[0][0], spans[0][1] + 1) if len(spans) == 1 else \
                chain.from_iterable(range(a, b + 1) for a, b in spans)
            return on_ready(positions, n)
        gen = self._data_gen
        self._edit_serial += 1
        serial = self._edit_serial
        edit = getattr(self.session, "edit", None)
        set_gen = getattr(edit, "applied_gen", None)

        def work(ctx: Any) -> Any:
            from jsonreshape.recordset import RecordSetBuilder

            builder = RecordSetBuilder("manual", gen=set_gen)
            try:
                done_rows = 0
                ctx.progress.set_total(n)
                for r0, r1 in spans:
                    pos = r0
                    while pos <= r1:
                        ctx.cancel.check()
                        stop = min(r1 + 1, pos + 65536)
                        builder.extend(rs[pos:stop])
                        done_rows += stop - pos
                        ctx.progress.update(done_rows)
                        pos = stop
                return builder.finish()
            except BaseException:
                builder.abort()
                raise

        def done(result: Any) -> None:
            if self._dead or serial != self._edit_serial or gen != self._data_gen:
                result.close()
                if not self._dead and serial == self._edit_serial:
                    self._status(t("gui.table.edit.changed"))
                return
            on_ready(result, len(result))

        def failed(exc: BaseException) -> None:
            if not self._dead and not isinstance(exc, Cancelled):
                report_task_error(self.session, exc, self, purpose="view")

        return self.session.app.tasks.run(work, title=t("gui.table.records.collect", count=format_count(n)),
                                          kind="value", owner=self, on_done=done, on_error=failed,
                                          silent=n <= 100_000, unit="rows")

    def delete_records(self) -> Any:
        """Delete the records of the selected rows (asks first above
        :data:`DELETE_CONFIRM_RECORDS`)."""
        why = self.record_block()
        if why is not None:
            self._status(why)
            return None
        spans = self._selected_row_spans()
        if not spans:
            return None

        def ready(positions: Any, count: int) -> None:
            def go(ok: bool = True) -> None:
                if not ok or self._dead:
                    return
                edit, why = edit_controller(self.session)
                if edit is None or why is not None:
                    self._status(why or edit_unavailable_text(edit))
                    return
                if isinstance(positions, list):
                    edit.delete([self._make_ref(p, ()) for p in positions])
                else:
                    edit.delete_records(positions)

            if count > DELETE_CONFIRM_RECORDS:
                ask(self, t("gui.table.records.delete_confirm", count=format_count(count)), go)
            else:
                go()

        return self._record_positions(spans, ready)

    def duplicate_records(self) -> Any:
        """Ctrl+D: a copy right after each record of the selected rows."""
        why = self.record_block()
        if why is not None:
            self._status(why)
            return None
        spans = self._selected_row_spans()
        if not spans:
            return None

        def ready(positions: Any, count: int) -> None:
            edit, why = edit_controller(self.session)
            if edit is None or why is not None:
                self._status(why or edit_unavailable_text(edit))
                return
            if isinstance(positions, list):
                edit.duplicate([self._make_ref(p, ()) for p in positions], on_done=self._new_record_shown)
            else:
                edit.duplicate_records(positions, on_done=self._new_record_shown)

        return self._record_positions(spans, ready)

    def insert_record(self, *, before: bool = False) -> Any:
        """Insert: a new record after (``before``: before) the current row's record -- at the
        start of an empty document; :class:`~jsonreshape.gui.editors.InsertDialog` asks its
        type and value (and the key in member-record documents)."""
        from jsonreshape.gui import editors

        why = self.record_block()
        if why is not None:
            self._status(why)
            return None
        row = self.grid.current()[0]
        rec = self._record_of(row) if row >= 0 else None
        if rec is None and self.session.view_len() > 0:
            self._status(t("gui.table.edit.no_cell"))
            return None
        # the record the new one goes next to: remapped while the dialog is open (an edit applied
        # meanwhile moves it), the index is taken when OK is pressed
        anchor = None if rec is None else _EditTarget(row, max(0, self.grid.current()[1]), int(rec), "", "value", ())
        kind = self._column_type(ROOT_COLUMN) or "object"
        if kind not in editors.INSERT_TYPES:
            kind = "object"

        def make(pe: _PendingEdit) -> Any:
            def accept(result: Any) -> None:
                if self._dead:
                    return
                if anchor is not None and pe.target is None:
                    self._status(t("gui.table.edit.gone"))
                    return
                edit, why = edit_controller(self.session)
                if edit is None or why is not None:
                    self._status(why or edit_unavailable_text(edit))
                    return
                index = 0 if pe.target is None else int(pe.target.record) + (0 if before else 1)
                edit.insert(None, index, result.value, key=result.key, on_done=self._new_record_shown)

            position = "before" if before else "after"
            keys: dict[str, Any] = {}
            if self._member_records():     # the new record's key: unique among the records (review3 L1)
                keys = {"taken": _WindowKeys(self), "key_check": self._key_check(lambda: _RECORD_KEYS)}
            dlg = editors.InsertDialog(self, title=t("gui.table.records.insert_title"), positions=(position,),
                                       position=position, key_needed=self._member_records(), value_type=kind,
                                       on_accept=accept, **keys)
            dlg.open()
            return dlg

        return self._open_pending(anchor, make)

    # ------------------------------------------------------------------ columns
    def _refresh_specs(self) -> None:
        self._colgen += 1
        self._holder.set(tuple(self._specs.values()), self._colgen)

    def _apply_columns(self, entries: list[tuple[str, int | None, bool]], *, source: str) -> None:
        self._columns_source = source
        old = {c.id: c for c in self.grid.columns()}
        cols: list[Column] = []
        specs: dict[str, ColumnSpec] = {}
        for text, width, visible in entries:
            text = (text or "").strip()
            if not text or text in specs:
                continue
            spec = self._specs.get(text) or column_spec(text)
            specs[text] = spec
            prev = old.get(text)
            w = int(width) if width else (prev.width if prev is not None else _default_width(spec, self._schema))
            cols.append(Column(id=text, title=_column_title(spec), width=w, visible=bool(visible),
                               sortable=spec.sort_block() is None, tooltip=self._column_tooltip(spec)))
        if not cols:
            spec = column_spec(ROOT_COLUMN)
            specs[ROOT_COLUMN] = spec
            cols.append(Column(id=ROOT_COLUMN, title=_column_title(spec), width=_default_width(spec, None),
                               sortable=True, tooltip=self._column_tooltip(spec)))
        self._specs = specs
        self._refresh_specs()
        self.grid.set_columns(cols)
        self._columns_source = source
        self._update_sort_indicators()
        self.strip.sync_columns()
        if source == "user":
            self._mark_layout_dirty()
        self.grid.refresh(request=True)
        self._find_columns_changed()
        self.columns_changed.emit()

    def _column_tooltip(self, spec: ColumnSpec) -> str:
        parts = [copyfmt.escape_surrogates(spec.text)]
        hint = _field_hint(self._schema, spec.text) if spec.kind == "value" else ""
        if hint and spec.text != ROOT_COLUMN:
            parts.append(hint)
        if spec.error is not None:
            parts.append(spec.error)
        elif self._columns_source == "placeholder" and spec.text == ROOT_COLUMN and not self._schema_failed:
            parts.append(t("gui.table.columns.loading"))
        block = self._sort_block(spec)
        parts.append(block if block else t("gui.table.sort.tip"))
        return "\n".join(parts)

    def _sort_block(self, spec: ColumnSpec) -> str | None:
        doc = getattr(self.session, "doc", None)
        if doc is not None and doc.kind == "single":
            return t("gui.table.single_no_view")
        return spec.sort_block()

    def visible_column_paths(self) -> list[str]:
        """The paths of the visible columns in display order (export columns)."""
        return [c.id for c in self.grid.columns() if c.visible]

    def column_paths(self) -> list[str]:
        """Every column path (hidden ones included) in display order."""
        return [c.id for c in self.grid.columns()]

    def set_columns(self, paths: Iterable[str]) -> None:
        """Show exactly these columns (in this order; widths of kept columns stay)."""
        self._apply_columns([(p, None, True) for p in paths], source="user")

    def add_column(self, path: str) -> None:
        """Add ``path`` as a visible column (the schema view's "add as column"); an
        existing column is shown and scrolled to."""
        path = validate_column_path(path)
        cols = self.grid.columns()
        for i, col in enumerate(cols):
            if col.id == path:
                if not col.visible:
                    self.grid.set_column_visible(i, True)
                self.grid.scroll_to_column(i)
                return
        entries = [(c.id, c.width, c.visible) for c in cols]
        if self._columns_source == "placeholder" and [e[0] for e in entries] == [ROOT_COLUMN]:
            entries = []
        entries.append((path, None, True))
        self._apply_columns(entries, source="user")
        idx = next((i for i, c in enumerate(self.grid.columns()) if c.id == path), None)
        if idx is not None:
            self.grid.scroll_to_column(idx)

    def remove_column(self, path: str) -> bool:
        entries = [(c.id, c.width, c.visible) for c in self.grid.columns() if c.id != path]
        if len(entries) == len(self.grid.columns()):
            return False
        self._apply_columns(entries, source="user")
        return True

    def hide_column(self, path: str) -> bool:
        cols = self.grid.columns()
        visible = [c for c in cols if c.visible]
        for i, col in enumerate(cols):
            if col.id == path and col.visible and len(visible) > 1:
                self.grid.set_column_visible(i, False)     # emits columns_changed
                return True
        return False

    def show_column(self, path: str) -> bool:
        for i, col in enumerate(self.grid.columns()):
            if col.id == path and not col.visible:
                self.grid.set_column_visible(i, True)
                return True
        return False

    def default_column_paths(self) -> list[str]:
        """The default columns of the document (from the schema; ``["$"]`` without one)."""
        return fields.default_columns(self._schema) if self._schema is not None else [ROOT_COLUMN]

    def reset_columns(self) -> None:
        """Back to the default columns (the remembered layout is forgotten)."""
        self._apply_columns([(p, None, True) for p in self.default_column_paths()],
                            source="default" if self._schema is not None else "placeholder")
        self.grid.set_frozen_columns(0)
        self.strip.relayout()
        try:
            self._forget_layout()
        except Exception:
            log.debug("table: could not forget the layout", exc_info=True)
        self._layout_dirty = False
        if self._schema is None:
            self._ensure_columns()

    def set_frozen(self, on: bool) -> None:
        """Freeze (or release) the first visible column."""
        self.grid.set_frozen_columns(1 if on else 0)
        self.strip.relayout()
        self._mark_layout_dirty()

    def is_frozen(self) -> bool:
        return self.grid.frozen_columns() > 0

    def _on_grid_columns_changed(self) -> None:
        self._update_sort_indicators()
        self.strip.relayout()
        self._mark_layout_dirty()
        self._find_columns_changed()
        self.columns_changed.emit()

    def _ensure_columns(self) -> None:
        """Columns for an opened document: the remembered layout, else the schema's
        defaults (requested in the background; ``$`` meanwhile)."""
        if self.session.doc is None:
            return
        if self._columns_source in ("none", "placeholder") and self._restore_layout():
            return
        if self._schema is None:
            cached = fields.cached_schema(self.session)
            if cached is not None:
                self._on_schema(cached)
                return
        if self._columns_source in ("none", "placeholder") and not self._schema_wanted:
            self._schema_wanted = True
            self._request_schema()

    def _request_schema(self) -> None:
        """Ask :mod:`fields` for the schema; a failed / cancelled computation leaves the
        ``$`` column (no "loading" hint any more) and the next activation asks again."""
        doc = self.session.doc
        schema = fields.get_schema(self.session, self._on_schema, on_failed=lambda _exc: self._on_schema_failed(doc))
        if schema is not None:
            self._on_schema(schema)

    def _on_schema_failed(self, doc: Any) -> None:
        if self._dead or doc is not self.session.doc:
            return
        self._schema_wanted = False
        self._schema_failed = True
        for col in self.grid.columns():
            spec = self._specs.get(col.id)
            if spec is not None:
                col.tooltip = self._column_tooltip(spec)
        self.grid.refresh_columns()
        if self._chooser is not None:
            try:
                self._chooser.note_label.setText(t("gui.table.chooser.no_schema"))
            except RuntimeError:   # the dialog is gone
                self._chooser = None

    def _on_schema(self, schema: Any) -> None:
        self._schema_wanted = False
        if self._dead or schema is None:
            return
        self._schema = schema
        self._schema_failed = False
        if self._columns_source in ("none", "placeholder"):
            self._apply_columns([(p, None, True) for p in fields.default_columns(schema)], source="default")
            self._retarget_followed()
        else:
            for col in self.grid.columns():
                spec = self._specs.get(col.id)
                if spec is not None:
                    col.tooltip = self._column_tooltip(spec)
            self.grid.refresh_columns()
        if self._chooser is not None:
            try:
                self._chooser.set_schema(schema)
            except RuntimeError:   # the dialog is gone
                self._chooser = None

    def _on_settings_changed(self) -> None:
        """The settings changed: take a new data font (the zoom steps stay)."""
        if self._dead:
            return
        font = mono_font(self.session)
        if font == self.grid.base_font():
            return
        self.grid.set_font(font, keep_zoom=True)
        self.strip.relayout()

    # ------------------------------------------------------------------ layout
    @staticmethod
    def _layout_key(doc: Any) -> tuple[str, list | None] | None:
        """``(document kind, record path)``: which records a layout is for."""
        if doc is None:
            return None
        try:
            rp = doc.record_path
            return str(doc.kind), (None if rp is None else list(rp))
        except Exception:   # pragma: no cover - a document without info
            return None

    def _automatic_options(self) -> bool:
        """The document was opened with the automatic record path and format."""
        return getattr(self.session, "record_path", None) is None and getattr(self.session, "format", None) is None

    def layout_state(self) -> dict:
        """The table layout as a JSON object (``GuiState.set_table_layout``), with the
        records it is for (``kind``, ``record_path``)."""
        key = self._layout_key(self._doc if self._doc is not None else getattr(self.session, "doc", None))
        kind, rp = key if key is not None else (None, None)
        return {
            "version": LAYOUT_VERSION,
            "kind": kind,
            "record_path": rp,
            "columns": [{"path": c.id, "width": int(c.width), "hidden": not c.visible} for c in self.grid.columns()],
            "frozen": int(self.grid.frozen_columns()),
            "filter_row": bool(self.grid.header_extra_visible()),
        }

    def _saved_layout(self) -> dict | None:
        """The remembered layout for the records shown now: the one saved for this kind
        and record path, else (automatic options only) one saved before layouts were kept
        per record path."""
        key = self._layout_key(self._doc)
        if key is None:
            return None
        variants = _layout_variants(self.session.app.state.table_layout(self.session.path))
        for layout in variants:
            if _same_records(layout, *key):
                return layout
        if self._automatic_options():
            for layout in variants:
                if "kind" not in layout:
                    return layout
        return None

    def _forget_layout(self) -> None:
        """Forget the remembered layout of the records shown now (the layouts of the
        other record paths stay)."""
        key = self._layout_key(self._doc)
        if key is None:
            return
        state = self.session.app.state
        variants = _layout_variants(state.table_layout(self.session.path))
        auto = self._automatic_options()
        left = [v for v in variants if not (_same_records(v, *key) or ("kind" not in v and auto))]
        if len(left) != len(variants):
            state.set_table_layout(self.session.path, _pack_layouts(left))

    def _restore_layout(self) -> bool:
        try:
            data = self._saved_layout()
        except Exception:
            log.debug("table: no layout", exc_info=True)
            return False
        if not isinstance(data, dict):
            return False
        entries: list[tuple[str, int | None, bool]] = []
        for item in data.get("columns") or []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            width = item.get("width")
            width = int(width) if isinstance(width, (int, float)) and not isinstance(width, bool) and width > 0 \
                else None
            entries.append((item["path"], width, not bool(item.get("hidden", False))))
        if not entries:
            return False
        self._apply_columns(entries, source="layout")
        self.grid.set_frozen_columns(1 if data.get("frozen") else 0)
        self.set_filter_row_visible(bool(data.get("filter_row", True)), remember=False)
        self._layout_dirty = False
        return True

    def _mark_layout_dirty(self) -> None:
        if self._dead or getattr(self.session, "doc", None) is None:
            return
        self._layout_dirty = True
        self._layout_timer.start()

    def save_layout(self) -> None:
        """Remember the layout for this file now (when the user changed it)."""
        self._layout_timer.stop()
        if not self._layout_dirty:
            return
        self._layout_dirty = False
        key = self._layout_key(self._doc)
        if key is None:
            return
        try:
            state = self.session.app.state
            current = self.layout_state()
            others = [v for v in _layout_variants(state.table_layout(self.session.path)) if not _same_records(v, *key)]
            state.set_table_layout(self.session.path, _pack_layouts([current] + others))
        except Exception:
            log.exception("table: could not remember the layout")

    # ------------------------------------------------------------------ sorting
    def _update_sort_indicators(self) -> None:
        keys = list(getattr(self.session, "sort_keys", []) or [])
        cols = self.grid.columns()
        marks: list[tuple[int, int]] = []
        for key in keys:
            for i, col in enumerate(cols):
                spec = self._specs.get(col.id)
                if spec is not None and spec.matches_sort_key(key):
                    marks.append((i, -1 if key.descending else 1))
                    break
        self.grid.set_sort_indicators(marks)

    def _on_header_clicked(self, index: int, modifiers: Any) -> None:
        cols = self.grid.columns()
        if not 0 <= index < len(cols):
            return
        spec = self._specs.get(cols[index].id)
        if spec is None:
            return
        try:
            shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        except TypeError:
            shift = False
        self.sort_by_column(spec.text, add=shift)

    def sort_by_column(self, path: str, *, add: bool = False) -> bool:
        """Header click on column ``path``: ascending -> descending -> unsorted; with
        ``add`` (Shift) the key is appended / toggled / removed among the other keys."""
        spec = self._specs.get(path) or column_spec(path)
        block = self._sort_block(spec)
        if block is not None:
            self._status(block)
            return False
        keys = list(self.session.sort_keys or [])
        pos = next((k for k, key in enumerate(keys) if spec.matches_sort_key(key)), None)
        try:
            if add:
                if pos is None:
                    keys.append(SortKey(spec.sort_path()))
                elif not keys[pos].descending:
                    keys[pos] = dataclasses.replace(keys[pos], descending=True)
                else:
                    del keys[pos]
            else:
                if pos is None:
                    keys = [SortKey(spec.sort_path())]
                elif not keys[pos].descending:
                    keys = [dataclasses.replace(keys[pos], descending=True)]
                else:
                    keys = []
            self.session.set_sort(keys or None)
        except JsonReshapeError as exc:
            self.session.app.report_error(exc, self)
            return False
        self._update_sort_indicators()
        return True

    def _set_column_sort(self, path: str, descending: bool | None) -> None:
        spec = self._specs.get(path) or column_spec(path)
        if self._sort_block(spec) is not None:
            return
        keys = [] if descending is None else [SortKey(spec.sort_path(), descending=descending)]
        try:
            self.session.set_sort(keys or None)
        except JsonReshapeError as exc:
            self.session.app.report_error(exc, self)
        self._update_sort_indicators()

    # ------------------------------------------------------------------ quick filters
    def set_filter_row_visible(self, on: bool, *, remember: bool = True) -> None:
        """Show / hide the quick filter row."""
        self.grid.set_header_extra_visible(on)
        if self.filter_button.isChecked() != bool(on):
            self.filter_button.blockSignals(True)
            self.filter_button.setChecked(bool(on))
            self.filter_button.blockSignals(False)
        self.strip.relayout()
        if remember:
            self._mark_layout_dirty()

    def filter_row_visible(self) -> bool:
        return self.grid.header_extra_visible()

    def _filter_button_toggled(self, on: bool) -> None:
        self.set_filter_row_visible(on)

    def filter_editor(self, path: str) -> QLineEdit | None:
        """The quick filter editor of column ``path`` (tests / focus)."""
        return self.strip.editor(path)

    def _commit_filter(self, editor: _FilterEdit) -> None:
        self.apply_quick_filter(editor.path, editor.text())

    def apply_quick_filter(self, path: str, text: str) -> bool:
        """Parse ``text`` (:func:`quick_filter_expr`) and set it as the quick filter of
        column ``path``; invalid text marks the editor and changes nothing."""
        if self._dead:
            return False
        editor = self.strip.editor(path)
        session = self.session
        current = session.quick_filters.get(path)
        if current is not None and (text or "").strip() == current.strip():
            if editor is not None:
                editor.set_error(None)
            return True        # the shown text is the expression itself (set elsewhere)
        try:
            expr = quick_filter_expr(path, text)
            doc = session.doc
            if expr is not None and doc is not None and doc.kind == "single":
                raise UserInputError("gui.table.single_no_view")
        except JsonReshapeError as exc:
            if editor is not None:
                editor.set_error(exc.message)
            return False
        if editor is not None:
            editor.set_error(None)
        typed = _typed(session)
        if expr is None:
            typed.pop(path, None)
        else:
            typed[path] = ((text or "").strip(), expr)
        if expr == current:
            return True
        try:
            session.set_quick_filter(path, expr)
        except JsonReshapeError as exc:
            if editor is not None:
                editor.set_error(exc.message)
            return False
        self._update_clear_button()
        self._update_placeholder()
        return True

    def clear_quick_filters(self) -> None:
        """Remove every quick filter (also those of columns not shown)."""
        for ed in self.strip.editors.values():
            ed.timer.stop()
            ed.set_error(None)
            if ed.text():
                ed.setText("")
        paths = list(self.session.quick_filters)
        typed = _typed(self.session)
        for path in paths:
            typed.pop(path, None)
        if paths:
            try:
                self.session.set_quick_filters({})      # one pipeline run for all columns
            except JsonReshapeError as exc:
                self.session.app.report_error(exc, self)
        self._update_clear_button()
        self._update_placeholder()

    def _sync_filter_editors(self) -> None:
        """Show the session's quick filters in the editors (typed texts when known)."""
        quick = getattr(self.session, "quick_filters", {}) or {}
        typed = _typed(self.session)
        single = getattr(self.session.doc, "kind", None) == "single"
        for path, ed in self.strip.editors.items():
            ed.setEnabled(not single)
            if ed.pending() or ed.error:
                continue      # the user's (unapplied / invalid) text stays until fixed or cleared
            expr = quick.get(path)
            if expr is None:
                text = ""
            else:
                remembered = typed.get(path)
                text = remembered[0] if remembered is not None and remembered[1] == expr else expr
            if ed.text() != text:
                ed.setText(text)
            if ed.error and expr is None and not text:
                ed.set_error(None)

    def _reset_filter_editors(self) -> None:
        """Empty every quick filter editor (no pending edit, no error) and forget the typed
        texts the session no longer has."""
        for ed in self.strip.editors.values():
            ed.timer.stop()
            ed.set_error(None)
            if ed.text():
                ed.setText("")
        quick = getattr(self.session, "quick_filters", {}) or {}
        typed = _typed(self.session)
        for path in [p for p in typed if p not in quick]:
            typed.pop(path, None)

    def _update_clear_button(self) -> None:
        n = len(getattr(self.session, "quick_filters", {}) or {})
        self.clear_button.setEnabled(n > 0)
        self.clear_button.setText(t("gui.table.qf.clear_all_n", count=n) if n else t("gui.table.qf.clear_all"))
        self.strip.corner.setEnabled(n > 0)

    # ------------------------------------------------------------------ copy
    def _selection_cells(self) -> tuple[list[tuple[int, int]], list[int], list]:
        """(row spans, visible column indices, ranges) of the selection (the current
        cell when nothing is selected)."""
        cols = self.grid.columns()
        ranges = list(self.grid.selected_ranges())
        if not ranges:
            row, col = self.grid.current()
            if row < 0 or col < 0:
                return [], [], []
            ranges = [(row, row, col, col)]
        col_idx = sorted({c for _r0, _r1, c0, c1 in ranges for c in range(max(0, c0), min(c1, len(cols) - 1) + 1)
                          if cols[c].visible})
        spans: list[tuple[int, int]] = []
        for r0, r1 in sorted((r0, r1) for r0, r1, _c0, _c1 in ranges):
            if spans and r0 <= spans[-1][1] + 1:
                spans[-1] = (spans[-1][0], max(spans[-1][1], r1))
            else:
                spans.append((r0, r1))
        return spans, col_idx, ranges

    def selection_count(self) -> int:
        """Selected rows (when more than one), else selected cells (status bar)."""
        spans, col_idx, ranges = self._selection_cells()
        rows = sum(r1 - r0 + 1 for r0, r1 in spans)
        if rows > 1:
            return rows
        return len(col_idx) if rows == 1 else 0

    def set_copy_headers(self, on: bool) -> None:
        """Copy the column titles as the first TSV line (remembered)."""
        self._copy_headers = bool(on)
        try:
            self.session.app.state.set(STATE_COPY_HEADERS, bool(on))
        except Exception:
            log.debug("table: could not remember the copy option", exc_info=True)

    def copy_headers(self) -> bool:
        return self._copy_headers

    def copy_value(self, fmt: str = "json") -> Any:
        """The window's Ctrl+C / "copy value": several selected cells -> TSV
        (:meth:`copy_selection`); one cell -> the value of the cell's node in ``fmt``
        (``ViewBase.copy_value``; ``$key`` / ``$index`` cells: their text; a cell without
        a value copies nothing; a wildcard cell with several values copies those values as
        one array -- :meth:`copy_matches`)."""
        spans, col_idx, _ranges = self._selection_cells()
        n_cells = sum(r1 - r0 + 1 for r0, r1 in spans) * len(col_idx)
        if n_cells > 1:
            return self.copy_selection()
        row, col = self.grid.current()
        cols = self.grid.columns()
        if row < 0 or not 0 <= col < len(cols):
            return super().copy_value(fmt)
        spec = self._specs.get(cols[col].id)
        rec = self._record_of(row)
        data = self._row(rec) if rec is not None else None
        if spec is not None and spec.kind in ("key", "index") and data is not None:
            return set_clipboard_text(copy_text(None, spec, data.record, data.key))
        if rec is None or spec is None or spec.kind != "value" or spec.pattern is None:
            return super().copy_value(fmt)
        cd = self._cell_data(data, spec.text) if data is not None and data.error is None else None
        if cd is not None:
            if cd.count == 0 and spec.text != ROOT_COLUMN:
                self._status(t("gui.table.copy.no_value"))
                return None
            if cd.count > 1 or (data.streamed and not spec.pattern.is_concrete):
                # several values -- or a streamed row, whose cells know only where the
                # wildcards start: the matches are resolved from that node
                return self.copy_matches(rec, spec, fmt, data=data, prefix_path=cd.path)
            return super().copy_value(fmt)      # one value (also ""): the node at cd.path
        if not spec.pattern.is_concrete:
            return self.copy_matches(rec, spec, fmt)      # not loaded: the matches decide
        return super().copy_value(fmt)

    def copy_matches(self, record: int, spec: ColumnSpec, fmt: str = "json", *, data: _Row | None = None,
                     prefix_path: tuple | None = None) -> Any:
        """Copy the values column ``spec`` matches in ``record`` in ``fmt``: several values
        as one array (JSON array / Python list / one CSV line), one value as itself, none:
        nothing (a status message). A kept parsed record answers at once (returns the
        text); else the values are read in a silent task (returns it) -- only the node
        where the pattern's wildcards start is loaded."""
        if data is not None and data.value is not _NOVALUE:
            return self._finish_copy_matches(_matches_text(match_values(data.value, spec), fmt))
        pattern = spec.pattern
        assert pattern is not None
        depth = len(spec.prefix())
        base = tuple(prefix_path[:depth]) if prefix_path is not None and len(prefix_path) >= depth \
            else spec.prefix()
        rest = PathPattern(pattern.segments[depth:])

        def compute(value: Any, cancel: CancelToken) -> str | None:
            return _matches_text([v for _, v in rest.resolve(value)], fmt)

        return run_value_task(self.session, self._make_ref(record, base), compute, self._finish_copy_matches,
                              owner=self, parent=self, title=t("gui.copy.task", format=copyfmt.format_label(fmt)),
                              purpose="copy")

    def _finish_copy_matches(self, text: str | None) -> str | None:
        if self._dead:
            return None
        if text is None:
            self._status(t("gui.table.copy.no_value"))
            return None
        return set_clipboard_text(text)

    def selected_text(self) -> str | None:
        """The current cell's text (prefills the search box); ``None`` when not loaded."""
        row, col = self.grid.current()
        cols = self.grid.columns()
        rec = self._record_of(row) if row >= 0 else None
        data = self._row(rec) if rec is not None else None
        if data is None or data.error is not None or not 0 <= col < len(cols):
            return None
        cd = self._cell_data(data, cols[col].id)
        if cd is None or not cd.full:
            return None
        text = cd.full
        return text if len(text) <= MAX_DISPLAY_CHARS else None

    def copy_cell(self) -> Any:
        """Copy the current cell's full text."""
        row, col = self.grid.current()
        if row < 0 or col < 0:
            return None
        return self._copy([(row, row)], [col], [(row, row, col, col)], headers=False)

    def copy_selection(self, *, headers: bool | None = None) -> Any:
        """Copy the selection: one cell -> its full text; several -> TSV (Excel) with the
        column titles first when ``headers`` (default: the remembered option). Rows not
        loaded yet are read in a task first. Returns the copied text (synchronous copy) or
        the task."""
        spans, col_idx, ranges = self._selection_cells()
        if not spans or not col_idx:
            return None
        n_rows = sum(r1 - r0 + 1 for r0, r1 in spans)
        single = n_rows == 1 and len(col_idx) == 1
        if headers is None:
            headers = self._copy_headers
        return self._copy(spans, col_idx, ranges, headers=bool(headers) and not single)

    def _copy(self, spans: list[tuple[int, int]], col_idx: list[int], ranges: list, *, headers: bool) -> Any:
        n_rows = sum(r1 - r0 + 1 for r0, r1 in spans)
        if n_rows > COPY_MAX_ROWS:
            self.session.app.report_error(UserInputError("gui.table.copy.too_many", count=format_count(n_rows),
                                                         limit=format_count(COPY_MAX_ROWS)), self)
            return None
        if n_rows > COPY_CONFIRM_ROWS and not confirm(self, t("gui.table.copy.confirm", count=format_count(n_rows))):
            return None
        cols = self.grid.columns()
        specs = [self._specs.get(cols[c].id) or column_spec(cols[c].id) for c in col_idx]
        single = n_rows == 1 and len(col_idx) == 1
        full_rect = all(any(r0 <= rr0 and rr1 <= r1 and c0 <= min(col_idx) and max(col_idx) <= c1
                            for r0, r1, c0, c1 in ranges) for rr0, rr1 in spans)

        def selected(row: int, ci: int) -> bool:
            if full_rect:
                return True
            return any(r0 <= row <= r1 and c0 <= ci <= c1 for r0, r1, c0, c1 in ranges)

        title_line = [cols[c].title for c in col_idx] if headers else None
        # synchronous: small selections whose rows are all cached
        if n_rows * len(col_idx) <= SYNC_COPY_CELLS:
            lines: list[list[str]] = []
            ok = True
            for r0, r1 in spans:
                for row in range(r0, r1 + 1):
                    rec = self._record_of(row)
                    data = self._row(rec) if rec is not None else None
                    if data is None:
                        ok = False
                        break
                    line = []
                    for ci, spec in zip(col_idx, specs):
                        text = _copy_text_cached(data, spec) if selected(row, ci) else ""
                        if text is None:
                            ok = False
                            break
                        line.append(text)
                    if not ok:
                        break
                    lines.append(line)
                if not ok:
                    break
            if ok:
                return self._finish_copy(lines, title_line, single, n_rows)
        return self._copy_task(spans, col_idx, specs, selected, title_line, single, n_rows)

    def _finish_copy(self, lines: list[list[str]], title_line: list[str] | None, single: bool, n_rows: int) -> str:
        if single and lines and lines[0]:
            text = lines[0][0]
        else:
            rows = ([title_line] if title_line is not None else []) + lines
            text = copyfmt.tsv_text(rows)
        copied = set_clipboard_text(text)
        cells = sum(len(line) for line in lines)
        self._status(t("gui.table.copy.done", rows=format_count(n_rows), cells=format_count(cells)))
        return copied

    def _copy_task(self, spans: list[tuple[int, int]], col_idx: list[int], specs: list[ColumnSpec], selected: Any,
                   title_line: list[str] | None, single: bool, n_rows: int) -> Any:
        rs = self.session.view_recordset()
        doc = self.session.doc
        loader = self._loader
        if rs is None or doc is None:
            return None
        doc = _pin(doc)      # the state whose positions the view holds now

        def work(ctx: Any) -> list[list[str]]:
            ctx.progress.set_total(n_rows)
            # the record numbers first (a quick read): the view may be replaced meanwhile
            records = [(r0, rs[r0:r1 + 1]) for r0, r1 in spans]
            done = 0
            out: list[list[str]] = []
            for r0, recs in records:
                for k, rec in enumerate(recs):
                    if k & 255 == 0:
                        ctx.cancel.check()
                        ctx.progress.update(done + k)
                    row = r0 + k
                    rec = int(rec)
                    cached = loader.peek(rec) if loader is not None else None
                    want = [s for ci, s in zip(col_idx, specs) if selected(row, ci)]
                    texts = iter(_row_copy_texts(doc, rec, want, cached, ctx.cancel)) if want else iter(())
                    out.append([next(texts) if selected(row, ci) else "" for ci in col_idx])
                done += len(recs)
            ctx.progress.update(done)
            return out

        def finished(lines: list[list[str]]) -> None:
            if not self._dead:
                self._finish_copy(lines, title_line, single, n_rows)

        def failed(exc: BaseException) -> None:
            if not isinstance(exc, Cancelled):
                self.session.app.report_error(exc, self)

        return self.session.app.tasks.run(work, title=t("gui.table.copy.task", count=format_count(n_rows)),
                                          kind="copy", owner=self, on_done=finished, on_error=failed,
                                          silent=n_rows <= 1000, unit="rows")

    # ------------------------------------------------------------------ menus
    def cell_menu(self, row: int, col: int) -> QMenu:
        """The context menu of a cell: copy cell / copy selection (TSV) / headers option,
        then the shared node menu of the cell's node (``actions.node_menu``, with its edit
        group for this view and, as the view's own item, "Tanlangan ikkitasini taqqoslash")
        and, after the edit group, "delete the records" of the selected rows."""
        from jsonreshape.gui import actions

        ref = self._ref_at(row, col, require_loaded=False)
        if ref is None:
            return self.table_menu()
        compare = QAction(t("gui.compare.menu.selected"), self)
        compare.setObjectName("compare_selected")
        compare.triggered.connect(lambda _checked=False: self.compare_selected())
        if self.compare_refs() is None:
            compare.setEnabled(False)
            compare.setToolTip(t("gui.compare.menu.selected.need_two"))
        menu = actions.node_menu(self, self.session, ref, view=self, extra=[compare])
        compare.setParent(menu)          # deleted with the menu
        first = menu.actions()[0] if menu.actions() else None
        mine: list[QAction] = []
        a = QAction(t("gui.table.menu.copy_cell"), menu)
        a.setObjectName("table_copy_cell")
        a.triggered.connect(lambda _checked=False: self.copy_cell())
        mine.append(a)
        a = QAction(t("gui.table.menu.copy_tsv"), menu)
        a.setObjectName("table_copy_tsv")
        a.triggered.connect(lambda _checked=False: self.copy_selection())
        mine.append(a)
        mine.append(self._copy_headers_action(menu))
        for action in mine:
            menu.insertAction(first, action)
        sep = QAction(menu)
        sep.setSeparator(True)
        menu.insertAction(first, sep)
        records = self._records_action(menu)
        acts = menu.actions()
        anchor = next((k for k, x in enumerate(acts) if x.objectName() == "edit_paste"), None)
        if anchor is not None and anchor + 1 < len(acts):
            menu.insertAction(acts[anchor + 1], records)
        else:
            menu.addAction(records)
        menu.setToolTipsVisible(True)
        return menu

    def _records_action(self, parent: QObject) -> QAction:
        """"Delete the record(s)" of the selected rows (disabled with the reason)."""
        a = QAction(t("gui.table.menu.delete_records"), parent)
        a.setObjectName("table_delete_records")
        n = sum(r1 - r0 + 1 for r0, r1 in self._selected_row_spans())
        if n > 1:
            a.setText(t("gui.table.menu.delete_records_n", count=format_count(n)))
        why = self.record_block()
        if why is not None:
            a.setEnabled(False)
            a.setToolTip(why)
        a.triggered.connect(lambda _checked=False: self.delete_records())
        return a

    def _copy_headers_action(self, parent: QObject) -> QAction:
        a = QAction(t("gui.table.menu.copy_headers"), parent)
        a.setObjectName("table_copy_headers")
        a.setCheckable(True)
        a.setChecked(self._copy_headers)
        a.toggled.connect(self.set_copy_headers)
        return a

    def table_menu(self) -> QMenu:
        """Menu of the empty area: copy option, columns, quick filter row."""
        menu = QMenu(self)
        menu.addAction(self._copy_headers_action(menu))
        self._add_table_items(menu)
        return menu

    def _add_table_items(self, menu: QMenu) -> None:
        a = menu.addAction(t("gui.table.menu.freeze"))
        a.setObjectName("table_freeze")
        a.setCheckable(True)
        a.setChecked(self.is_frozen())
        a.toggled.connect(self.set_frozen)
        a = menu.addAction(t("gui.table.menu.filter_row"))
        a.setObjectName("table_filter_row")
        a.setCheckable(True)
        a.setChecked(self.filter_row_visible())
        a.toggled.connect(lambda on: self.set_filter_row_visible(on))
        menu.addSeparator()
        a = menu.addAction(t("gui.table.columns"))
        a.setObjectName("table_columns")
        a.triggered.connect(lambda _checked=False: self.open_column_chooser())
        a = menu.addAction(t("gui.table.menu.reset_columns"))
        a.setObjectName("table_reset_columns")
        a.triggered.connect(lambda _checked=False: self.reset_columns())

    def header_menu(self, index: int) -> QMenu:
        """Context menu of header section ``index`` (-1: the empty header area)."""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        cols = self.grid.columns()
        if 0 <= index < len(cols):
            col = cols[index]
            spec = self._specs.get(col.id) or column_spec(col.id)
            block = self._sort_block(spec)
            for name, key, desc in (("table_sort_asc", "gui.table.menu.sort_asc", False),
                                    ("table_sort_desc", "gui.table.menu.sort_desc", True)):
                a = menu.addAction(t(key))
                a.setObjectName(name)
                a.triggered.connect(lambda _checked=False, p=col.id, d=desc: self._set_column_sort(p, d))
                if block:
                    a.setEnabled(False)
                    a.setToolTip(block)
            a = menu.addAction(t("gui.table.menu.sort_clear"))
            a.setObjectName("table_sort_clear")
            a.triggered.connect(lambda _checked=False: self._set_column_sort(col.id, None))
            a.setEnabled(bool(self.session.sort_keys) and block is None)
            a = menu.addAction(t("gui.statspanel.action.node"))
            a.setObjectName("table_stats")
            a.triggered.connect(lambda _checked=False, p=col.id: self.show_column_stats(p))
            why = self._stats_block(spec)
            a.setEnabled(why is None)
            a.setToolTip(why or t("gui.statspanel.action.node.tip"))
            menu.addSeparator()
            a = menu.addAction(t("gui.table.menu.hide"))
            a.setObjectName("table_hide_column")
            a.triggered.connect(lambda _checked=False, p=col.id: self.hide_column(p))
            a.setEnabled(sum(1 for c in cols if c.visible) > 1)
            a = menu.addAction(t("gui.table.menu.remove"))
            a.setObjectName("table_remove_column")
            a.triggered.connect(lambda _checked=False, p=col.id: self.remove_column(p))
            a.setEnabled(len(cols) > 1)
            a = menu.addAction(t("gui.table.menu.fit"))
            a.setObjectName("table_fit_column")
            a.triggered.connect(lambda _checked=False, i=index: self.grid.fit_column(i))
            menu.addSeparator()
        hidden = [c for c in cols if not c.visible]
        sub = menu.addMenu(t("gui.table.menu.show_hidden"))
        sub.setObjectName("table_show_hidden")
        for c in hidden:
            a = sub.addAction(copyfmt.escape_surrogates(c.title).replace("&", "&&"))
            a.triggered.connect(lambda _checked=False, p=c.id: self.show_column(p))
        sub.setEnabled(bool(hidden))
        self._add_table_items(menu)
        return menu

    @staticmethod
    def _stats_block(spec: ColumnSpec) -> str | None:
        """Why "Statistika" does not apply to a column (localized), ``None`` when it does."""
        if spec.kind == "index":
            return t("gui.table.menu.stats.index")
        if spec.error is not None:
            return spec.error
        return None

    def show_column_stats(self, path: str) -> bool:
        """The header menu's "Statistika": the window's statistics dock for the column's field
        (duck-typed ``self.window().show_stats(field)``; ``$index`` and invalid paths: nothing).
        True when it was asked for."""
        spec = self._specs.get(path) or column_spec(path)
        if self._stats_block(spec) is not None:
            return False
        fn = getattr(self.window(), "show_stats", None)
        if fn is None:
            return False
        from jsonreshape.gui.statspanel import field_for_path

        fn(field_for_path(spec.text))
        return True

    def _on_link(self, _row: Any, _col: int, url: str) -> None:
        """Ctrl+click on the link of a smart value: asked first, opened only after yes."""
        if not self._dead:
            open_url(self, url)

    def _selected_cells(self, limit: int = 2) -> list[tuple[int, int]] | None:
        """The distinct selected cells ``(row, column index)`` of visible columns (the current
        cell when nothing is selected); ``None`` as soon as there are more than ``limit``."""
        cols = self.grid.columns()
        ranges = list(self.grid.selected_ranges())
        if not ranges:
            row, col = self.grid.current()
            ranges = [(row, row, col, col)] if row >= 0 and col >= 0 else []
        seen: list[tuple[int, int]] = []
        for r0, r1, c0, c1 in ranges:
            if c1 < 0:
                c0, c1 = 0, len(cols) - 1
            visible = [c for c in range(max(0, c0), min(c1, len(cols) - 1) + 1) if cols[c].visible]
            if not visible:
                continue
            for row in range(r0, r1 + 1):
                for col in visible:
                    if (row, col) not in seen:
                        seen.append((row, col))
                        if len(seen) > limit:
                            return None
        return seen

    def compare_refs(self) -> list[NodeRef] | None:
        """The NodeRefs of "Tanlangan ikkitasini taqqoslash": the nodes of exactly two selected
        cells (:meth:`_ref_at` of loaded rows), else ``None``."""
        if self._dead or getattr(self.session, "doc", None) is None:
            return None
        cells = self._selected_cells(2)
        if cells is None or len(cells) != 2:
            return None
        refs: list[NodeRef] = []
        for row, col in cells:
            ref = self._ref_at(row, col, require_loaded=True)
            if ref is None:
                return None
            refs.append(ref)
        return refs

    def compare_selected(self) -> Any:
        """Compare the nodes of the two selected cells in a compare window
        (:func:`jsonreshape.gui.comparewindow.compare_selected`); a status message for another
        count. Returns the window (``None``: not started)."""
        refs = self.compare_refs()
        if refs is None:
            self._status(t("gui.compare.menu.selected.need_two"))
            return None
        from jsonreshape.gui import comparewindow

        window = self.window()
        return comparewindow.compare_selected(window, self.session, refs, reveal=getattr(window, "show_node", None))

    def _popup(self, menu: QMenu, gpos: QPoint) -> None:
        self._last_menu = menu
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(gpos)

    def _on_context_menu(self, row: int, col: int, gpos: QPoint) -> None:
        if row is None or row < 0 or col < 0:
            self._popup(self.table_menu(), gpos)
            return
        self._popup(self.cell_menu(row, col), gpos)

    def _on_header_menu(self, index: int, gpos: QPoint) -> None:
        self._popup(self.header_menu(index), gpos)

    # ------------------------------------------------------------------ chooser
    def open_column_chooser(self) -> ColumnChooserDialog:
        """Open the column chooser (non-blocking); its OK sets the columns."""
        schema = self._schema or fields.cached_schema(self.session)
        if schema is not None:
            self._schema = schema
        defaults = fields.default_columns(schema) if schema is not None else None
        dlg = ColumnChooserDialog(self.column_paths(), schema, self, defaults=defaults)
        for c in self.grid.columns():
            if not c.visible:
                dlg.set_checked(c.id, False)
        self._chooser = dlg
        dlg.accepted.connect(lambda: self._chooser_accepted(dlg))
        dlg.finished.connect(lambda _r: self._chooser_closed(dlg))
        if schema is None and self.session.doc is not None:
            self._schema_wanted = True
            self._request_schema()
        dlg.open()
        return dlg

    def _chooser_accepted(self, dlg: ColumnChooserDialog) -> None:
        paths = dlg.columns()
        old = {c.id: c for c in self.grid.columns()}
        self._apply_columns([(p, old[p].width if p in old else None, True) for p in paths], source="user")

    def _chooser_closed(self, dlg: ColumnChooserDialog) -> None:
        if self._chooser is dlg:
            self._chooser = None
        dlg.deleteLater()

    # ------------------------------------------------------------------ keys
    def _grid_key(self, event: QKeyEvent) -> bool:
        # Alt+Down: the current column's quick filter (like a spreadsheet's filter button)
        mods = event.modifiers()
        if event.key() == Qt.Key.Key_Down and mods == Qt.KeyboardModifier.AltModifier:
            self.focus_quick_filter()
            return True
        # editing keys (§12.9: handled here, never as window shortcuts). Enter without modifiers
        # is left to the grid: its ``activated`` edits a scalar and reveals a container.
        key = event.key()
        plain = mods & ~Qt.KeyboardModifier.KeypadModifier
        no_mods = plain == Qt.KeyboardModifier.NoModifier
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and plain == Qt.KeyboardModifier.ShiftModifier:
            self.edit_cell(as_json=True)
            return True
        if key == Qt.Key.Key_F2 and no_mods:
            self.rename_key()              # F2 renames (AGENT_PROMPT §11); Enter edits the value
            return True
        if key == Qt.Key.Key_Delete and no_mods:
            self.delete_cells()
            return True
        if key == Qt.Key.Key_Insert and no_mods:
            self.insert_record()
            return True
        if key == Qt.Key.Key_D and plain == Qt.KeyboardModifier.ControlModifier:
            self.duplicate_records()
            return True
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste_json()
            return True
        # the rest is reached only without the main window (its window actions take these keys first)
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_value("json")
            return True
        if event.matches(QKeySequence.StandardKey.Find):
            self.quick_find()
            return True
        if event.key() == Qt.Key.Key_F3:
            self.find_next(bool(mods & Qt.KeyboardModifier.ShiftModifier))
            return True
        return False

    # ------------------------------------------------------------------ ViewBase
    def activate(self) -> None:
        """Became visible: columns (layout or schema defaults; a schema computation that
        failed or was cancelled is asked for again), the visible rows and -- the first time
        -- the session's current node (its row and column)."""
        first = not self._activated
        self._activated = True
        if self._schema is None:
            self._schema_wanted = False
        if self.session.doc is not None:
            self._ensure_columns()
        self.grid.refresh(request=True)
        if first and self.grid.current()[0] < 0:
            ref = getattr(self.session, "current", None)
            if ref is not None and self.session.doc is not None:
                self._on_session_current(ref, None)

    def zoom(self, steps: int) -> None:
        self.grid.zoom(steps)

    def focus_quick_filter(self) -> bool:
        """Alt+Down: show the quick filter row and focus the current column's editor."""
        self.set_filter_row_visible(True)
        cols = self.grid.columns()
        col = self.grid.current()[1]
        if not (0 <= col < len(cols) and cols[col].visible):
            col = next((i for i, c in enumerate(cols) if c.visible), -1)
        if col < 0:
            return False
        ed = self.strip.editor(cols[col].id)
        if ed is None or not ed.isEnabled():
            return False
        self.grid.scroll_to_column(col)
        self.strip.relayout()
        ed.setFocus(Qt.FocusReason.ShortcutFocusReason)
        ed.selectAll()
        return True

    # ------------------------------------------------------------------ quick find
    def quick_find(self) -> None:
        """Ctrl+F: the find bar under the grid (searches the loaded rows' cell texts)."""
        self.find_bar.open_bar()

    def find_next(self, backwards: bool = False) -> None:
        """F3 / Shift+F3: the next / previous hit after the current cell (rows, then
        columns; wrapping); opens the bar when there is no search yet. Pressed while a
        search runs (also the silent rescan after rows were loaded or moved), the step is
        made as soon as that search is done -- it is never dropped."""
        if not self.find_bar.isVisible() or not self.find_bar.text():
            self.find_bar.open_bar()
            return
        if self._find_task is None and not self._find_order and self._find_timer.isActive():
            self._find_timer.stop()          # the hits were dropped (rows moved), the rescan is due: now
            self._find_rescan()
        if self._find_task is not None:
            if len(self._find_queue) < FIND_QUEUE_MAX:
                self._find_queue.append(bool(backwards))
            return
        self._find_step(backwards)

    def _find_step(self, backwards: bool) -> None:
        """One F3 / Shift+F3 step over the current hits."""
        order = self._find_order
        if not order:
            self._find_status(None)
            return
        cur = self.grid.current()
        if backwards:
            before = [e for e in order if (e[0], e[1]) < cur]
            target = before[-1] if before else order[-1]
        else:
            after = [e for e in order if (e[0], e[1]) > cur]
            target = after[0] if after else order[0]
        self._find_goto(target)

    def quick_find_matches(self) -> dict[tuple[int, str], tuple]:
        """``{(row, column path): spans}`` of the current quick find (tests)."""
        return dict(self._find_matches)

    def _find_active(self) -> bool:
        return self.find_bar.isVisible() and bool(self.find_bar.text())

    def _on_viewport_changed(self) -> None:
        if self._find_active():
            self._find_timer.start()        # rows scrolled into view (perhaps cached already) join the search

    def _find_entries(self) -> tuple[list[tuple[int, int, str, str]], bool]:
        """``(row, column index, column id, painted text)`` of the visible columns of the
        loaded rows of the current view (at most :data:`QUICK_FIND_ROWS` rows); the flag
        tells whether rows of the view were left out (not loaded / over the limit)."""
        loader = self._loader
        if loader is None:
            return [], False
        cols = [(i, c.id) for i, c in enumerate(self.grid.columns()) if c.visible]
        nrows = self.grid.row_count()
        out: list[tuple[int, int, str, str]] = []
        scanned = 0
        for pos, rec in sorted(self._win.items()):
            if pos >= nrows:
                continue
            data = loader.peek(rec)
            if not isinstance(data, _Row) or data.error is not None:
                continue
            if scanned >= QUICK_FIND_ROWS:
                return out, True
            scanned += 1
            for i, cid in cols:
                cd = data.cells.get(cid)
                if cd is None and data.value is not _NOVALUE:
                    cd = self._cell_data(data, cid)
                if cd is not None and cd.cell.text:
                    out.append((pos, i, cid, cd.cell.text))
        return out, scanned < nrows

    def _find_search(self, text: str, case_sensitive: bool, jump: bool = True) -> None:
        self._find_gen += 1
        gen = self._find_gen
        if self._find_task is not None:
            self._find_task.cancel()
            self._find_task = None
        matcher = quickfind.make_matcher(text, case_sensitive)
        if matcher is None or self._loader is None or self._dead:
            self._find_matches, self._find_order, self._find_current = {}, [], None
            self._find_capped = False
            self._find_status(None, 0)
            self.grid.refresh()
            return
        entries, capped = self._find_entries()

        def work(ctx: Any) -> tuple[dict, list]:
            matches: dict[tuple[int, str], tuple] = {}
            order: list[tuple[int, int, str]] = []
            for k, (row, ci, cid, cell_text) in enumerate(entries):
                if k & 1023 == 0:
                    ctx.cancel.check()
                found = matcher(cell_text)
                if found:
                    matches[(row, cid)] = tuple((a, b) for a, b in found)
                    order.append((row, ci, cid))
                    if len(order) >= QUICK_FIND_MAX_MATCHES:
                        break
            order.sort()
            return matches, order

        def done(result: tuple[dict, list]) -> None:
            if self._dead or gen != self._find_gen:
                return
            self._find_task = None
            queued, self._find_queue = self._find_queue, []
            self._find_matches, self._find_order = result
            self._find_capped = capped or len(self._find_order) >= QUICK_FIND_MAX_MATCHES
            order = self._find_order
            if not order:
                self._find_current = None
                self._find_status(None, 0)
                self.grid.refresh()
                return
            if jump:
                cur = self.grid.current()
                after = [e for e in order if (e[0], e[1]) >= cur]
                self._find_goto(after[0] if after else order[0])
                queued = queued[1:]          # the first F3 pressed meanwhile: this jump
            else:
                if self._find_current not in self._find_matches:
                    self._find_current = None
                index = next((i for i, e in enumerate(order) if (e[0], e[2]) == self._find_current), None)
                self._find_status(index, len(order))
                self.grid.refresh()
            for backwards in queued:         # F3 / Shift+F3 pressed while it ran
                self._find_step(backwards)

        def ended(exc: BaseException | None = None) -> None:
            if not self._dead and gen == self._find_gen:
                self._find_task = None
                self._find_queue = []
                if exc is not None and not isinstance(exc, Cancelled):
                    self.session.app.report_error(exc, self)

        self._find_task = self.session.app.tasks.run(work, title=t("gui.table.find.task"), kind="find", owner=self,
                                                     silent=True, on_done=done, on_error=ended, on_cancel=ended)

    def _find_goto(self, entry: tuple[int, int, str]) -> None:
        row, ci, cid = entry
        self._find_current = (row, cid)
        self.grid.set_current(row, ci, scroll="center")
        index = next((i for i, e in enumerate(self._find_order) if e == entry), None)
        self._find_status(index, len(self._find_order))
        self.grid.refresh()

    def _find_status(self, index: int | None, total: int | None = None) -> None:
        if not self.find_bar.text():
            self.find_bar.set_status("")
            return
        total = len(self._find_order) if total is None else total
        self.find_bar.set_status(quickfind.status_text(index, total, capped=self._find_capped),
                                 "found" if total else "notfound")

    def _find_rescan(self) -> None:
        if self._find_active() and not self._dead:
            self._find_search(self.find_bar.text(), self.find_bar.case_sensitive(), jump=False)

    def _find_reset(self) -> None:
        """Rows moved (another view / document): the hits are stale; search again soon."""
        self._find_gen += 1
        if self._find_task is not None:
            self._find_task.cancel()
            self._find_task = None
        self._find_matches, self._find_order, self._find_current = {}, [], None
        if self._find_active():
            self._find_timer.start()

    def _find_columns_changed(self) -> None:
        if self._find_active():
            self._find_reset()

    def _find_closed(self) -> None:
        self._find_timer.stop()
        self._find_queue = []
        self._find_reset()
        self._find_capped = False
        self.grid.refresh()
        self.grid.setFocus(Qt.FocusReason.OtherFocusReason)

    def _status(self, text: str, timeout: int = 4000) -> None:
        fn = getattr(self.session.app, "show_status", None)
        if callable(fn):
            try:
                fn(text, timeout)
            except Exception:
                log.debug("show_status failed", exc_info=True)

    def cell_text(self, row: int, path: str) -> str | None:
        """The painted text of the cell (``None`` while not loaded; tests)."""
        col = next((c for c in self.grid.columns() if c.id == path), None)
        if col is None:
            return None
        cell = self.cell(row, col)
        return None if cell is None else cell.text

    def cell_at(self, row: int, path: str) -> Cell | None:
        col = next((c for c in self.grid.columns() if c.id == path), None)
        return None if col is None else self.cell(row, col)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FontChange and hasattr(self, "strip"):
            if self.strip.refresh_height():
                self.grid.set_header_extra(self.strip, self.strip.preferred_height())
            self.strip.relayout()
        return super().event(event)

    def shutdown(self) -> None:
        """Tab closing: remember the layout, stop the loader, detach the tasks."""
        if self._dead:
            return
        self.save_layout()
        self._dead = True
        for timer in (self._select_timer, self._layout_timer, self._refetch_timer, self._find_timer):
            timer.stop()
        for ed in self.strip.editors.values():
            ed.timer.stop()
        if self._find_task is not None:
            self._find_task.cancel()
            self._find_task = None
        if self._settings_signal is not None:
            try:
                self._settings_signal.disconnect(self._on_settings_changed)
            except (RuntimeError, TypeError):
                pass
            self._settings_signal = None
        for name, slot in (("data_changed", self._on_data_changed), ("index_finished", self._on_index_finished)):
            signal = getattr(self.session, name, None)
            if signal is not None:
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._cancel_edits()
        if self._loader is not None:
            self._loader.close()
        super().shutdown()
