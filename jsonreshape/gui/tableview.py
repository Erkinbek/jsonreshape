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
from typing import Any, Iterable

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
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.gui.widgets import (
    ViewBase,
    confirm,
    format_count,
    mono_font,
    run_value_task,
    set_clipboard_text,
)
from jsonreshape.i18n import t
from jsonreshape.paths import PathPattern, Wild, parse_pattern
from jsonreshape.query import compile_filter, render_field
from jsonreshape.sortkeys import SortKey
from jsonreshape.stream import CaptureLimitExceeded, DuplicateKeys, capture_budget, stream_probe, stream_values
from jsonreshape.tasks import CancelToken

log = logging.getLogger(__name__)

__all__ = [
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


def _key_cell(key: str | None) -> _CellData:
    if key is None:
        return _EMPTY
    return _CellData(_make_cell(copyfmt.escape_surrogates(key), "key"), key, (), False, False)


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
    return _CellData(_make_cell(full, role), full, path, container, has_containers, count=len(items) + more,
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


def _fetch_rows(holder: _FetchHolder, doc: Any, keys: list, cancel: CancelToken) -> dict[int, _Row]:
    specs, gen = holder.snapshot()
    budget = capture_budget(doc.settings)
    out: dict[int, _Row] = {}
    for record in keys:
        cancel.check()
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
        old = self._loader
        if old is not None:
            old.close()
            old.deleteLater()
        holder = self._holder

        def fetch(keys: list, cancel: CancelToken) -> dict:
            return _fetch_rows(holder, doc, keys, cancel)

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

    def _on_view_changed(self) -> None:
        if self._dead:
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
        self._cur_record = self._record_of(row)
        if self._following:
            return
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
        if row is None or row < 0 or col < 0 or self._dead:
            return
        rec = self._record_of(row)
        data = self._row(rec) if rec is not None else None
        cols = self.grid.columns()
        if data is None or data.error is not None or not 0 <= col < len(cols):
            return
        cd = self._cell_data(data, cols[col].id)
        if cd is None or not cd.container:
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
        self._goto_record(int(record), self._column_for_path(tuple(ref.path)), scroll="center")

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

    def _goto_record(self, record: int, col: int | None, *, scroll: str = "center") -> None:
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
        then the shared node menu of the cell's node (``actions.node_menu``)."""
        from jsonreshape.gui import actions

        ref = self._ref_at(row, col, require_loaded=False)
        if ref is None:
            return self.table_menu()
        menu = actions.node_menu(self, self.session, ref, view=self)
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
        return menu

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
        if self._loader is not None:
            self._loader.close()
        super().shutdown()
