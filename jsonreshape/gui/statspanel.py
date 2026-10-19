"""Field statistics dock (docs/ARCHITECTURE.md §14.9; design: scratchpad ``s5/map_stats.md`` §5.1-§5.3;
AGENT_PROMPT §9 "Maydon statistikasi", §5 "Daraxtda ko'rsatish ... statistika qatoridan").

:class:`StatsPanel` is the content of the statistics dock:

* **Field**: an editable combo whose items (and a "contains" completer) come from the shared schema
  cache (:func:`jsonreshape.gui.fields.get_schema` + :func:`jsonreshape.schema.field_paths`), with
  ``$key`` first for documents whose records are object members; any pattern can be typed
  (``orders[*].sum``, ``a.*.b``, ``..name``, ``$``); it is validated while typing
  (:func:`jsonreshape.paths.parse_pattern`). Enter computes.
* **Scope**: "Joriy ko'rinish (N ta yozuv): <view label>" (the default, docs §14.0) or "Barcha
  yozuvlar". Single documents have one record: the scope is fixed.
* **Hisoblash / Bekor qilish / Aniq hisoblash**: :func:`jsonreshape.stats.field_stats` runs as a
  *heavy* task (kind ``stats``, owned by the session: closing or reopening the file cancels it) on
  ``session.snapshot()`` taken when the user asks (the pinned document and the view set; the view set
  is leased with ``keep_document_for``); ``on_partial`` snapshots (about one per second, in the worker
  thread) are handed over with ``TaskContext.post`` and shown as they arrive ("oraliq natija"); the
  task's progress (bytes) drives the progress bar. "Aniq hisoblash" is enabled for an approximate final
  result (HyperLogLog / Misra-Gries) and computes again with ``exact=True``.
* **Result**: a header (field, records examined, time, the scope as it was), a stacked types bar, a
  summary table -- present / ``null`` / missing, one row per type, distinct (``"N (aniq)"`` or ``"≈ N
  (±E; HyperLogLog, ±0,8 %)"``), the number of values of multi-valued fields, numbers (count and
  integers, min, max, mean, sum: the exact texts of :class:`~jsonreshape.stats.NumberStats`, never a
  float -- ``1e400``, ``12345678901234567890``, ``-0.0``, ``1.10`` stay as written), string lengths and
  empty strings, array lengths and object key counts -- and the top-20 table (value, count, the records
  holding it for multi-valued fields, share with a bar); the warnings of
  :meth:`~jsonreshape.stats.FieldStats.warning_texts`. Bars use the theme's colors (type colors for the
  types) and follow theme changes. The mean and sum rows say what makes the sum inexact: rounded to 50
  significant digits ("≈ … (50 ta muhim raqam)"), and/or N numbers left out (exponents beyond the exact
  range, ``NumberStats.sum_skipped``: "… (N ta son qo'shilmadi)", the mean "—" when none is left; the
  tip is the warning ``stats.warn.sum_skipped``). Live snapshots: distinct "hozircha N" while exact so far,
  "hozircha ≈ N (±E; HyperLogLog)" when it is an estimate (sketches, or the spilled epochs of the exact
  mode: ``distinct_rse`` set); the top title says when the live counts are Misra-Gries counts that may be up
  to ``top_bound`` too low (and such counts read "≈ N").
* **Click -> filter** (double click / Enter on a row, or its context menu): see "Filters" below.
* **Daraxtda ko'rsatish** (context menu of a top value, min and max): the first record holding the
  value (``ValueCount.first`` / ``NumberStats.min_first`` / ``max_first``) is selected with
  ``session.select(ref, reveal=True)``: the node itself for concrete paths, the record for ``$`` /
  ``$key``; for patterns with wildcards a silent task (:func:`locate_value`) walks that record through
  the Node API (memory-bounded; ``..`` patterns parse the record when it is not large) and finds the
  first matching node whose value is canonically equal (:mod:`jsonreshape.dupes` equality); when it
  cannot (the value is longer than 1 MiB, the record changed) the record itself is shown and the status
  says so.
* **Stale marker**: after ``session.data_changed`` (an edit), a reopen, or -- for a result of the scope
  "current view" -- a view change, the result stays on screen marked "eskirgan" with a Yangilash
  button (no automatic recomputation: a full pass is heavier than the schema view's sample refresh).
  A result computed while the data or the view changed arrives already marked. Results of an older
  request are dropped (a request serial). After a data change the record numbers of the result may
  point to other records, so "Daraxtda ko'rsatish" is disabled until it is recomputed; filters stay
  available (they are values, not positions).
* **Takrorlar…** (a button next to the header): opens the duplicates dialog
  (:func:`jsonreshape.gui.dupesdialog.show_dupes_dialog`) with the field as the key when it is a concrete
  path (or ``$`` / ``$key``) and some value repeats; :attr:`StatsPanel.dupes_opener` replaces that call.

The panel keeps one state per session (field text, scope, result, task), so switching tabs shows each
tab's own statistics, and a computation of a tab in the background goes on.

Filters (map_stats §5.3)
========================
Each clickable row gives a :class:`StatsFilter`: a builder :class:`~jsonreshape.query.Condition` when a
builder operator expresses it, else an expression text keyed by the field:

==========================  =================================================  ====================
row                         filter                                             e.g.
==========================  =================================================  ====================
a top value ``v``           ``Condition(f, "eq", v)`` / ``"ne"`` (menu)        ``score == 1.10``
mavjud / null / yo'q        ``exists`` / ``is_null`` / ``not_exists``          ``missing(note)``
a type ``t``                text ``type(f) == "t"`` (no builder operator)      ``type(id) == "string"``
min / max                   ``Condition(f, "eq", <exact token value>)``        ``x == 1e400``
string/array/object length  ``Condition(f, "len_eq", n)`` (+ ``and type(f) ==     ``len(name) == 3``
                            "t"`` when the field also holds other sized types)
empty strings               ``Condition(f, "eq", "")``                         ``name == ""``
==========================  =================================================  ====================

:func:`apply_filter` applies it through the session's public API: a condition goes to
``DocSession.filter_by_condition(cond)`` (the builder group extended, else ``(<old>) and <cond>``; under a
JMESPath main filter a quick filter keyed by the field); an expression text becomes the quick filter of the field
(``session.set_quick_filter(field, text)``, map_stats §5.3 "quick filter keyed by the field"). Values come
from the statistics themselves (``ValueCount.value``: ``codec`` types keeping the spelling of the first
occurrence), so the texts are exact (``big == 12345678901234567890``, ``x == -0.0``, ``s == "O‘zbek"``).

Decisions (documented deviations / extensions of map_stats §5)
===============================================================
* Filters select whole records. For a field under an array (context ``orders[*]``) presence counts
  elements while ``missing(f)`` keeps the records in which NO element has the field; the header and
  the tooltips say so (no expression selects "records with at least one element lacking the field").
* ``len()`` of the filter language reads the FIRST value of a path, so length filters are offered for
  single-valued fields only (disabled with the reason otherwise). A length filter gets
  ``and type(f) == "t"`` when the field also holds other sized types (a string of 3 characters vs an
  array of 3 elements).
* Single documents cannot be filtered (``DocSession.set_filter`` refuses them): the filter actions are
  disabled with the reason; "Daraxtda ko'rsatish" works (record ``None``, absolute path).
* A value longer than 1 MiB (``ValueCount.value`` is ``MISSING``) cannot be filtered by; "Daraxtda
  ko'rsatish" then shows its record.
* Computing is refused (with the reason) while the document is still being indexed and, for the scope
  "current view", while the view's filter / sort is still running (its set is not final yet).
* The extra rows "Qiymatlar (jami)" (multi-valued fields), array lengths and object key counts show what
  :class:`~jsonreshape.stats.FieldStats` already has.
* Extra test / integration hooks: :attr:`StatsPanel.memory_limit` (passed to ``field_stats``; ``None`` =
  the settings' budget) and :attr:`StatsPanel.dupes_opener`.

Integration API (round B): ``StatsPanel(app, parent=None)`` (the dock factory signature of
``MainWindow``: ``factory(app, window)``), ``set_session(session)`` (``None``: no document),
``show_field(path, *, scope=None, compute=True)`` (``path``: text, or a node's path tuple -- array
indices become ``[*]``, :func:`field_for_path`), ``compute(*, exact=False)``, ``compute_exact()``,
``cancel()``, ``refresh()``, ``shutdown()``; signals ``computed(session, FieldStats)``, ``partial_received(session,
FieldStats)``, ``filter_applied(session, text)``.

Threading: the GUI thread never reads the data file: the computation, the element search of "Daraxtda
ko'rsatish" and the schema of the field list run in tasks; worker results come back through task
callbacks (``on_item`` / ``on_done``), guarded by a weak reference to the panel.
"""

from __future__ import annotations

import json
import logging
import weakref
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Iterator, Sequence

import shiboken6
from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, QRect, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QContextMenuEvent, QKeySequence, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec
from jsonreshape.dupes import canonical_bytes, canonical_token
from jsonreshape.errors import JsonDecodeError, JsonReshapeError, TooLargeError, UserInputError
from jsonreshape.gui import fields, theme
from jsonreshape.gui.searchpanel import keep_for
from jsonreshape.gui.session import NodeRef
from jsonreshape.gui.widgets import format_count, mono_font, set_clipboard_text
from jsonreshape.i18n import get_language, t
from jsonreshape.paths import MISSING, PathPattern, Wild, format_path, parse_pattern
from jsonreshape.query import Condition, render_path
from jsonreshape.schema import field_paths
from jsonreshape.stats import KEY_FIELD, FieldStats, ValueCount, field_stats
from jsonreshape.tasks import CancelToken, check

log = logging.getLogger(__name__)

__all__ = ["SCOPES", "TOP_VALUES", "StatsFilter", "StatsPanel", "apply_filter", "condition_field", "field_for_path",
           "length_filter", "locate_value", "number_filter", "presence_filter", "type_filter", "value_filter"]

SCOPES = ("view", "all")
TOP_VALUES = 20                 # most frequent values listed (AGENT_PROMPT §9)
WALK_BATCH = 512                # children read at once by locate_value
SCOPE_REFRESH_MS = 300          # live counts refresh the scope texts at most this often
MENU_EXPR_CHARS = 80            # condition texts in menus are cut to this many characters
BAR_WIDTH = 56                  # share bars in the tables (device-independent pixels)
ROLE_KEY = int(Qt.ItemDataRole.UserRole)          # row kind (summary) / top index
ROLE_SHARE = int(Qt.ItemDataRole.UserRole) + 1    # 0..1 share drawn as a bar (None: no bar)
ROLE_COLOR = int(Qt.ItemDataRole.UserRole) + 2    # a type name (its color) or None (accent)
_SIZED = ("string", "array", "object")
_DECODE_ERRORS = (JsonDecodeError, ValueError, TypeError, UnicodeDecodeError, RecursionError)


# ======================================================================================
# small helpers
# ======================================================================================
def _alive(obj: Any) -> bool:
    try:
        return obj is not None and shiboken6.isValid(obj)
    except Exception:  # pragma: no cover
        return False


def _call(ref: Any, name: str, *args: Any) -> None:
    """Call method ``name`` of the panel behind the weak reference ``ref`` when it still exists
    (task callbacks never reach a deleted panel)."""
    obj = ref()
    if obj is not None and _alive(obj) and not getattr(obj, "_dead", False):
        getattr(obj, name)(*args)


def _count(n: int) -> str:
    return format_count(int(n))


def _decimal(value: float, digits: int = 1) -> str:
    """``12.5`` (English) / ``12,5`` (Uzbek), trailing zeros removed."""
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",") if get_language() == "uz" else text


def _share_text(part: int, whole: int) -> str:
    """``part / whole`` in percent like the CLI (``<0,1%`` / ``>99,9%`` at the edges)."""
    if not whole:
        return "0%"
    x = part * 100.0 / whole
    if part and x < 0.1:
        return "<" + _decimal(0.1) + "%"
    if part != whole and x > 99.9:
        return ">" + _decimal(99.9) + "%"
    return _decimal(x) + "%"


def _fraction(part: int, whole: int) -> float:
    return max(0.0, min(1.0, part / whole)) if whole else 0.0


def _tn(key: str, n: int, **params: Any) -> str:
    """``t(key)`` with ``<key>.one`` for a count of 1 (English singular)."""
    return t(key + ".one" if int(n) == 1 else key, **params)


def _elide(text: str, limit: int = MENU_EXPR_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _menu_text(text: str) -> str:
    return text.replace("&", "&&")


def _error_text(exc: BaseException | None) -> str:
    if isinstance(exc, JsonReshapeError):
        return exc.message
    return t("gui.widgets.error.unexpected") if exc is not None else ""


def field_for_path(path: Any) -> str:
    """The statistics field of a node path inside a record (text or segments): array indices become
    ``[*]`` (``("orders", 0, "sum")`` -> ``orders[*].sum``), ``()`` -> ``$``. A text is returned as given
    (stripped)."""
    if isinstance(path, str):
        return path.strip()
    segs = tuple(Wild.ANY_INDEX if isinstance(s, int) and not isinstance(s, bool) else s for s in path)
    return format_path(segs) or "$"


def _parse_field(text: str) -> str:
    """The field to compute (validated): ``$key`` or a path pattern (``PathSyntaxError``)."""
    value = (text or "").strip()
    if not value:
        raise UserInputError("gui.statspanel.field.empty")
    if value == KEY_FIELD:
        return value
    parse_pattern(value)
    return value


# ======================================================================================
# filters (Qt-free)
# ======================================================================================
@dataclass(frozen=True)
class StatsFilter:
    """A filter one click of the panel applies: ``cond`` (a builder condition) or, when no builder
    operator expresses it, ``cond`` ``None`` and the expression ``text`` (applied as the quick filter
    keyed by ``field``). ``field``: the field in path syntax (``Condition.field``); ``text``: the
    expression text (always set)."""

    field: str
    cond: Condition | None
    text: str


def condition_field(fs: FieldStats) -> str:
    """The field of ``fs`` in path syntax (``Condition.field``): ``$key``, ``$`` or ``format_path``."""
    if fs.is_key:
        return KEY_FIELD
    return format_path(tuple(fs.path)) or "$"


def _expr_field(fs: FieldStats) -> str:
    """The field of ``fs`` in expression syntax (``type(<field>)``)."""
    if fs.is_key:
        return KEY_FIELD
    return render_path(PathPattern(tuple(fs.path)))


def _condition(fs: FieldStats, op: str, value: Any = None) -> StatsFilter:
    cond = Condition(condition_field(fs), op, value)
    return StatsFilter(cond.field, cond, cond.to_text())     # to_text validates (QuerySyntaxError)


def value_filter(fs: FieldStats, value: Any, *, negate: bool = False) -> StatsFilter | None:
    """``f == value`` (``f != value``); ``None`` for an unavailable value (``MISSING``). Raises the
    builder's errors for values it cannot express."""
    if value is MISSING:
        return None
    return _condition(fs, "ne" if negate else "eq", value)


def presence_filter(fs: FieldStats, what: str) -> StatsFilter:
    """``present`` -> ``exists(f)``, ``null`` -> ``f is null``, ``missing`` -> ``missing(f)``."""
    op = {"present": "exists", "null": "is_null", "missing": "not_exists"}[what]
    return _condition(fs, op)


def type_filter(fs: FieldStats, type_name: str) -> StatsFilter:
    """``type(f) == "<type_name>"`` (no builder operator: an expression text)."""
    text = f"type({_expr_field(fs)}) == {json.dumps(type_name)}"
    return StatsFilter(condition_field(fs), None, text)


def length_filter(fs: FieldStats, n: int, type_name: str) -> StatsFilter | None:
    """``len(f) == n`` for the type ``type_name`` (``string`` / ``array`` / ``object``); ``None`` for
    multi-valued fields (``len()`` reads the first value only). When the field also holds another sized
    type, ``and type(f) == "<type_name>"`` keeps the length exact (an expression text then)."""
    if fs.multi:
        return None
    flt = _condition(fs, "len_eq", int(n))
    others = [name for name in _SIZED if name != type_name and fs.types.get(name)]
    if not others:
        return flt
    text = f"{flt.text} and type({_expr_field(fs)}) == {json.dumps(type_name)}"
    return StatsFilter(flt.field, None, text)


def number_filter(fs: FieldStats, which: str) -> StatsFilter | None:
    """``f == min`` / ``f == max`` with the exact token value (``which``: ``min`` | ``max``)."""
    nm = fs.number
    if nm is None:
        return None
    return value_filter(fs, codec.loads(nm.min_text if which == "min" else nm.max_text))


def apply_filter(session: Any, flt: StatsFilter) -> bool:
    """Apply ``flt`` to the session's view (module docstring "Filters"): a condition through
    ``session.filter_by_condition``; an expression text as the quick filter of its field. False when the
    session cannot filter (no document, a single document, closed)."""
    if session is None or getattr(session, "closed", False):
        return False
    doc = getattr(session, "doc", None)
    if doc is None or getattr(doc, "kind", None) == "single":
        return False
    if flt.cond is None:
        session.set_quick_filter(flt.field, flt.text)
        return True
    return bool(session.filter_by_condition(flt.cond))


# ======================================================================================
# "show in tree": the first node holding a value (worker threads)
# ======================================================================================
def _node_canon(node: Any) -> bytes | None:
    """Canonical bytes of a node's value (``None`` when it is too large to read or cannot be read)."""
    try:
        raw = node.raw()
    except TooLargeError:
        return None
    try:
        return canonical_token(raw)
    except _DECODE_ERRORS:
        return None


def _walk(node: Any, segs: tuple, i: int, path: tuple, ipath: tuple,
          cancel: CancelToken | None) -> Iterator[tuple[tuple, tuple, Any]]:
    """``(path, child indices, node)`` of the nodes the fixed-depth pattern ``segs[i:]`` matches below
    ``node``, in document order (dict semantics: the last member of a duplicated key)."""
    if i == len(segs):
        yield path, ipath, node
        return
    check(cancel)
    seg = segs[i]
    if seg is Wild.ANY_INDEX or seg is Wild.ANY_KEY:
        if node.kind != ("array" if seg is Wild.ANY_INDEX else "object"):
            return
        n = node.child_count(cancel=cancel)
        last: dict[Any, int] | None = None
        if seg is Wild.ANY_KEY:
            last = {}
            for start in range(0, n, WALK_BATCH):
                check(cancel)
                for k, child in enumerate(node.children(start, min(n, start + WALK_BATCH))):
                    last[child.key] = start + k
        for start in range(0, n, WALK_BATCH):
            check(cancel)
            for k, child in enumerate(node.children(start, min(n, start + WALK_BATCH))):
                idx = start + k
                if last is not None and last.get(child.key) != idx:
                    continue
                key = child.key if child.key is not None else idx
                yield from _walk(child, segs, i + 1, path + (key,), ipath + (idx,), cancel)
        return
    idx = node.find_child_index(seg, cancel=cancel)
    if idx is None:
        return
    child = node.child(idx)
    key = child.key if child.key is not None else seg
    yield from _walk(child, segs, i + 1, path + (key,), ipath + (int(idx),), cancel)


def locate_value(doc: Any, record: int | None, segments: Sequence[Any], value: Any,
                 cancel: CancelToken | None = None) -> NodeRef | None:
    """The first node of record ``record`` (``None``: the value of a single document) that the pattern
    ``segments`` matches and whose value equals ``value`` (canonical equality of
    :mod:`jsonreshape.dupes`: numbers by exact value, objects without member order), as a NodeRef
    relative to the record (child indices filled in for fixed-depth patterns); ``None`` when there is
    none or ``value`` is ``MISSING``. Fixed-depth patterns walk the Node API (memory-bounded; values
    over ``large_node_bytes`` are skipped); ``..`` patterns parse the record (``None`` when it is too
    large). Blocking: worker threads only."""
    if value is MISSING:
        return None
    try:
        target = canonical_bytes(value)
    except (TypeError, ValueError):
        return None
    root = doc.root() if record is None else doc.record_node(int(record))
    segs = tuple(segments)
    if any(s is Wild.DESCENDANTS for s in segs):
        try:
            whole = root.value()
        except TooLargeError:
            return None
        for path, v in PathPattern(segs).resolve(whole):
            check(cancel)
            try:
                if canonical_bytes(v) == target:
                    return NodeRef(record, tuple(path))
            except (TypeError, ValueError):
                continue
        return None
    for path, ipath, node in _walk(root, segs, 0, (), (), cancel):
        if _node_canon(node) == target:
            return NodeRef(record, path, ipath)
    return None


# ======================================================================================
# rows of the result
# ======================================================================================
@dataclass
class _Row:
    """One row of the summary table."""

    kind: str                                  # "present", "null", "missing", "type:<t>", "distinct", ...
    label: str
    value: str
    share: float | None = None
    share_text: str = ""
    color: str | None = None                   # a type name (its color) or None (accent)
    filters: list[StatsFilter] = dc_field(default_factory=list)   # the first one is the row's action
    why: str = ""                              # why there is no filter
    reveal: tuple[int, Any] | None = None      # (record position, value) of "show in tree"
    copy: str | None = None                    # text of "copy"
    tip: str = ""


@dataclass
class _TopRow:
    """One row of the top-values table."""

    vc: ValueCount
    filters: list[StatsFilter] = dc_field(default_factory=list)   # [eq, ne]
    why: str = ""


@dataclass
class _Meta:
    """What a request computed (the result shown belongs to it)."""

    field: str
    scope: str
    label: str
    records: int
    data_serial: int
    view_serial: int | None
    exact: bool
    single: bool


class _State:
    """The statistics of one session."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.field = ""                     # the field combo's text while another tab is shown
        self.scope = "view"
        self.fields: list[str] = []         # items of the field combo (schema + $key)
        self.result: FieldStats | None = None
        self.final = False                  # result is the final one (not a partial snapshot)
        self.meta: _Meta | None = None      # the request the result belongs to
        self.status = "idle"                # idle | running | done | cancelled | failed
        self.error: BaseException | None = None
        self.message = ""                   # extra status text (reopened, refused, reveal note)
        self.message_kind = "info"
        self.task: Any = None
        self.req = 0                        # request serial: callbacks of older requests are dropped
        self.data_serial = 0                # bumped by data_changed and reopens
        self.stale: str | None = None       # None | "data" | "view"
        self.reveal_task: Any = None

    @property
    def running(self) -> bool:
        return self.status == "running"


def _filter_or_why(build: Callable[[], StatsFilter | None], why_none: str = "") -> tuple[list[StatsFilter], str]:
    try:
        flt = build()
    except (JsonReshapeError, ValueError, TypeError) as exc:
        return [], t("gui.statspanel.why.no_filter", error=_error_text(exc) if isinstance(exc, JsonReshapeError)
                     else str(exc))
    if flt is None:
        return [], why_none
    return [flt], ""


def summary_rows(fs: FieldStats, *, single: bool = False) -> list[_Row]:
    """The summary rows of ``fs`` (module docstring); ``single``: a single document (no filters)."""
    rows: list[_Row] = []
    ctx_tip = t("gui.statspanel.tip.context", context=fs.context) if fs.context != "$" else ""

    def add(row: _Row, build: Callable[[], StatsFilter | None] | None = None, why_none: str = "") -> None:
        if build is not None:
            if single:
                row.why = t("gui.statspanel.why.single")
            else:
                row.filters, row.why = _filter_or_why(build, why_none)
        rows.append(row)

    add(_Row("present", t("gui.statspanel.row.present"), _count(fs.present), _fraction(fs.present, fs.base),
             _share_text(fs.present, fs.base), tip=ctx_tip, copy=str(fs.present)),
        lambda: presence_filter(fs, "present"))
    add(_Row("null", t("gui.statspanel.row.null"), _count(fs.nulls), _fraction(fs.nulls, fs.values),
             _share_text(fs.nulls, fs.values), color="null", copy=str(fs.nulls)),
        lambda: presence_filter(fs, "null"))
    missing_tip = t("gui.statspanel.tip.missing_context", field=fs.field) if fs.context != "$" else ""
    add(_Row("missing", t("gui.statspanel.row.missing"), _count(fs.missing), _fraction(fs.missing, fs.base),
             _share_text(fs.missing, fs.base), tip=missing_tip, copy=str(fs.missing)),
        lambda: presence_filter(fs, "missing"))
    for name, n in fs.types.items():
        add(_Row(f"type:{name}", t("gui.statspanel.row.type", type=t("schema.type." + name)), _count(n),
                 _fraction(n, fs.values), _share_text(n, fs.values), color=name, copy=str(n)),
            lambda name=name: type_filter(fs, name))
    rse = fs.distinct_rse or 0.0
    if fs.partial and fs.distinct_rse:
        # a live estimate (sketches, or the spilled epochs of the exact mode): "≈" with its error
        distinct = t("gui.statspanel.distinct.partial_approx", count=_count(fs.distinct),
                     error=_count(round(fs.distinct * rse)), rse=_decimal(rse * 100, 1))
    elif fs.partial:
        distinct = t("gui.statspanel.distinct.partial", count=_count(fs.distinct))   # exact so far
    elif fs.distinct_exact:
        distinct = t("gui.statspanel.distinct.exact", count=_count(fs.distinct))
    else:
        distinct = t("gui.statspanel.distinct.approx", count=_count(fs.distinct),
                     error=_count(round(fs.distinct * rse)), rse=_decimal(rse * 100, 1))
    rows.append(_Row("distinct", t("gui.statspanel.row.distinct"), distinct, copy=str(fs.distinct)))
    if fs.multi:
        rows.append(_Row("values", t("gui.statspanel.row.values"), _count(fs.values), copy=str(fs.values)))
    nm = fs.number
    if nm is not None:
        rows.append(_Row("numbers", t("gui.statspanel.row.numbers"),
                         t("gui.statspanel.value.numbers", count=_count(nm.count), integers=_count(nm.integers)),
                         copy=str(nm.count)))
        for which, text, first in (("min", nm.min_text, nm.min_first), ("max", nm.max_text, nm.max_first)):
            try:
                value = codec.loads(text)
            except _DECODE_ERRORS:   # pragma: no cover - texts of number tokens
                value = MISSING
            add(_Row(which, t(f"gui.statspanel.row.{which}"), text, copy=text,
                     reveal=(int(first), value), tip=t("gui.statspanel.tip.first", record=int(first))),
                lambda which=which: number_filter(fs, which))
        mean, total, tip = _sum_texts(fs)
        rows.append(_Row("mean", t("gui.statspanel.row.mean"), mean, copy=nm.mean_text, tip=tip))
        rows.append(_Row("sum", t("gui.statspanel.row.sum"), total, copy=nm.sum_text, tip=tip))
    multi_why = t("gui.statspanel.why.multi_len")
    st = fs.string
    if st is not None:
        for kind, n in (("str_min", st.min_len), ("str_max", st.max_len)):
            add(_Row(kind, t(f"gui.statspanel.row.{kind}"), _tn("gui.statspanel.value.chars", n, count=_count(n)),
                     copy=str(n)),
                lambda n=n: length_filter(fs, n, "string"), multi_why)
        add(_Row("empty", t("gui.statspanel.row.empty"), _count(st.empty),
                 _fraction(st.empty, fs.types.get("string", 0)), _share_text(st.empty, fs.types.get("string", 0)),
                 copy=str(st.empty)),
            lambda: value_filter(fs, ""))
    for prefix, rng, type_name, unit in (("arr", fs.arr_len, "array", "gui.statspanel.value.items"),
                                         ("obj", fs.obj_keys, "object", "gui.statspanel.value.keys")):
        if rng is None:
            continue
        for kind, n in ((f"{prefix}_min", rng[0]), (f"{prefix}_max", rng[1])):
            add(_Row(kind, t(f"gui.statspanel.row.{kind}"), _tn(unit, n, count=_count(n)), copy=str(n)),
                lambda n=n, type_name=type_name: length_filter(fs, n, type_name), multi_why)
    return rows


def _sum_texts(fs: FieldStats) -> tuple[str, str, str]:
    """``(mean, sum, tip)`` texts of the number rows. ``exact_sum`` False means the sum was rounded to 50
    significant digits (warning ``stats.warn.sum_approximate``), or numbers beyond the exact range were left out
    of the sum and the mean (``NumberStats.sum_skipped``, warning ``stats.warn.sum_skipped``; the mean is "—"
    when none is left), or both: the rows say which, the tip is the warning's text."""
    nm = fs.number
    assert nm is not None
    skipped = int(nm.sum_skipped or 0)
    rounded = not nm.exact_sum and (not skipped or "stats.warn.sum_approximate" in fs.warnings)
    mean = nm.mean_text
    if not skipped:
        total = t("gui.statspanel.sum.approx", sum=nm.sum_text) if rounded else nm.sum_text
        return mean, total, ""
    count = _count(skipped)
    key = "gui.statspanel.sum.approx_skipped" if rounded else "gui.statspanel.sum.skipped"
    total = _tn(key, skipped, sum=nm.sum_text, count=count)
    if mean != "—":
        mean = _tn("gui.statspanel.mean.skipped", skipped, mean=mean, count=count)
    return mean, total, _tn("stats.warn.sum_skipped", skipped, count=count)


def top_rows(fs: FieldStats, *, single: bool = False) -> list[_TopRow]:
    """The rows of the top-values table: filters ``= v`` and ``!= v`` per value."""
    out: list[_TopRow] = []
    for vc in fs.top:
        row = _TopRow(vc)
        if single:
            row.why = t("gui.statspanel.why.single")
        elif vc.value is MISSING:
            row.why = t("gui.statspanel.why.too_long")
        else:
            try:
                row.filters = [f for f in (value_filter(fs, vc.value), value_filter(fs, vc.value, negate=True))
                               if f is not None]
            except (JsonReshapeError, ValueError, TypeError) as exc:
                row.why = t("gui.statspanel.why.no_filter", error=_error_text(exc) if isinstance(exc, JsonReshapeError)
                            else str(exc))
        out.append(row)
    return out


# ======================================================================================
# widgets
# ======================================================================================
class _ShareDelegate(QStyledItemDelegate):
    """The share column: a bar (theme colors: the type's color or the accent) and the percent text."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        share = index.data(ROLE_SHARE)
        if share is None:
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
        kind = index.data(ROLE_COLOR)
        painter.save()
        try:
            width = min(BAR_WIDTH, max(8, rect.width() // 2))
            bar = QRect(rect.left(), rect.center().y() - 3, width, 6)
            painter.setPen(Qt.PenStyle.NoPen)
            if selected:   # a frame of the base color: the bar stands out from the selection
                painter.fillRect(bar.adjusted(-1, -1, 1, 1), opt.palette.color(QPalette.ColorRole.Base))
            painter.fillRect(bar, theme.color("line"))
            w = round(max(0.0, min(1.0, float(share))) * width)
            if w > 0:
                fill = theme.badge_color(kind) if kind else theme.color("accent")
                painter.fillRect(QRect(bar.left(), bar.top(), w, bar.height()), fill)
            rect.setLeft(bar.right() + 6)
            painter.setPen(fg)
            shown = opt.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width()))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), shown)
        finally:
            painter.restore()


class _TypesBar(QWidget):
    """A stacked bar of the type distribution (theme type colors)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._parts: list[tuple[str, int]] = []
        self._total = 0
        self.setFixedHeight(10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(t("gui.statspanel.types.name"))

    def set_parts(self, parts: list[tuple[str, int]], total: int) -> None:
        self._parts = [(name, int(n)) for name, n in parts if n]
        self._total = int(total)
        text = " · ".join(f"{t('schema.type.' + name)} {_share_text(n, self._total)}" for name, n in self._parts)
        tip = t("gui.statspanel.types.tip", types=text) if text else ""
        self.setToolTip(tip)
        self.setAccessibleDescription(tip)
        self.update()

    def parts(self) -> list[tuple[str, int]]:
        return list(self._parts)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt name
        painter = QPainter(self)
        try:
            rect = self.rect().adjusted(0, 1, 0, -1)
            painter.fillRect(rect, theme.color("line"))
            total = self._total
            if total <= 0 or not self._parts:
                return
            x = rect.left()
            for i, (name, n) in enumerate(self._parts):
                if i == len(self._parts) - 1:
                    w = rect.right() + 1 - x
                else:
                    w = round(n / total * rect.width())
                if w > 0:
                    painter.fillRect(QRect(x, rect.top(), w, rect.height()), theme.badge_color(name))
                x += max(0, w)
        finally:
            painter.end()


class MenuReason(QObject):
    """Remembers whether the last context-menu event of a table came from the keyboard (the menu key /
    Shift+F10 open the menu of the CURRENT row, not of the row under the viewport's center)."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.keyboard = False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt name
        if event.type() == QEvent.Type.ContextMenu and isinstance(event, QContextMenuEvent):
            self.keyboard = event.reason() == QContextMenuEvent.Reason.Keyboard
        return False


def _item(text: str, *, key: Any = None, align_right: bool = False, tip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    if key is not None:
        item.setData(ROLE_KEY, key)
    if align_right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tip:
        item.setToolTip(tip)
    return item


def _share_item(share: float | None, text: str, color: str | None, key: Any = None) -> QTableWidgetItem:
    item = _item(text, key=key)
    item.setData(ROLE_SHARE, share)
    item.setData(ROLE_COLOR, color)
    return item


def _table(parent: QWidget, columns: list[str], name: str) -> QTableWidget:
    table = QTableWidget(0, len(columns), parent)
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    table.setTabKeyNavigation(False)
    table.setWordWrap(False)
    table.setAccessibleName(name)
    table.horizontalHeader().setHighlightSections(False)
    return table


# ======================================================================================
# the panel
# ======================================================================================
class StatsPanel(QWidget):
    """The statistics dock's content (see the module docstring).

    Signals: ``computed(session, FieldStats)`` (a final result), ``partial_received(session,
    FieldStats)`` (a live snapshot), ``filter_applied(session, text)``.
    """

    computed = Signal(object, object)
    partial_received = Signal(object, object)
    filter_applied = Signal(object, str)

    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatsPanel")
        self.app = app
        #: bytes for the exact counting of ``field_stats`` (``None``: the settings' budget)
        self.memory_limit: int | None = None
        #: ``fn(session, key)`` opening the duplicates dialog (``None``: :func:`show_dupes_dialog`)
        self.dupes_opener: Callable[[Any, str], Any] | None = None
        self._session: Any = None
        self._states: dict[int, _State] = {}
        self._rows: list[_Row] = []
        self._top: list[_TopRow] = []
        self.last_menu: QMenu | None = None   # the context menu shown last (tests, automation)
        self._dead = False
        self._syncing = False
        self._scope_timer = QTimer(self)
        self._scope_timer.setSingleShot(True)
        self._scope_timer.setInterval(SCOPE_REFRESH_MS)
        self._scope_timer.timeout.connect(self._update_scope_texts)
        self._build()
        app.tasks.task_updated.connect(self._on_task_updated)
        settings_changed = getattr(app, "settings_changed", None)
        if settings_changed is not None:
            settings_changed.connect(self._apply_fonts)
        theme.notifier().theme_changed.connect(self._on_theme)
        self._apply_fonts()
        self._on_theme()
        self._refresh()

    # ================================================================================== build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(6)
        field_label = QLabel(t("gui.statspanel.field.label"), self)
        row.addWidget(field_label)
        self.field_combo = QComboBox(self)
        self.field_combo.setObjectName("stats_field")
        self.field_combo.setEditable(True)
        self.field_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.field_combo.setMaxVisibleItems(20)
        self.field_combo.setMinimumContentsLength(8)
        self.field_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.field_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.field_combo.setToolTip(t("gui.statspanel.field.tip"))
        self.field_combo.setAccessibleName(t("gui.statspanel.field.name"))
        field_label.setBuddy(self.field_combo)
        edit = self.field_combo.lineEdit()
        edit.setPlaceholderText(t("gui.statspanel.field.placeholder"))
        edit.setClearButtonEnabled(True)
        edit.textChanged.connect(self._on_field_changed)
        edit.returnPressed.connect(self._on_return)
        self.field_model = QStringListModel([], self)
        self.completer = QCompleter(self.field_model, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setMaxVisibleItems(15)
        self.field_combo.setCompleter(self.completer)
        row.addWidget(self.field_combo, 1)
        # one slot: the compute button, replaced by the cancel button while a computation runs
        self.compute_button = QPushButton(t("gui.statspanel.compute"), self)
        self.compute_button.setObjectName("stats_compute")
        self.compute_button.setToolTip(t("gui.statspanel.compute.tip"))
        self.compute_button.setAutoDefault(False)
        self.compute_button.clicked.connect(lambda _checked=False: self.compute())
        row.addWidget(self.compute_button)
        self.cancel_button = QPushButton(t("gui.statspanel.cancel"), self)
        self.cancel_button.setObjectName("stats_cancel")
        self.cancel_button.setToolTip(t("gui.statspanel.cancel.tip"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(lambda _checked=False: self.cancel())
        self.cancel_button.setVisible(False)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

        self.field_error = QLabel(self)
        self.field_error.setTextFormat(Qt.TextFormat.PlainText)
        self.field_error.setWordWrap(True)
        self.field_error.setVisible(False)
        layout.addWidget(self.field_error)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        scope_label = QLabel(t("gui.statspanel.scope.label"), self)
        row2.addWidget(scope_label)
        self.scope_combo = QComboBox(self)
        self.scope_combo.setObjectName("stats_scope")
        self.scope_combo.setToolTip(t("gui.statspanel.scope.tip"))
        self.scope_combo.setAccessibleName(t("gui.statspanel.scope.name"))
        self.scope_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.scope_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.scope_combo.setMinimumContentsLength(10)
        for scope in SCOPES:
            self.scope_combo.addItem("", scope)
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        scope_label.setBuddy(self.scope_combo)
        row2.addWidget(self.scope_combo, 1)
        self.exact_button = QPushButton(t("gui.statspanel.exact"), self)
        self.exact_button.setObjectName("stats_exact")
        self.exact_button.setAutoDefault(False)
        self.exact_button.clicked.connect(lambda _checked=False: self.compute_exact())
        row2.addWidget(self.exact_button)
        layout.addLayout(row2)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel(self)
        self.status_label.setObjectName("stats_status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)

        self.stale_frame = QWidget(self)
        stale_row = QHBoxLayout(self.stale_frame)
        stale_row.setContentsMargins(0, 0, 0, 0)
        stale_row.setSpacing(6)
        self.stale_label = QLabel(self.stale_frame)
        self.stale_label.setObjectName("stats_stale")
        self.stale_label.setTextFormat(Qt.TextFormat.PlainText)
        self.stale_label.setWordWrap(True)
        stale_row.addWidget(self.stale_label, 1)
        self.refresh_button = QPushButton(t("gui.statspanel.refresh"), self.stale_frame)
        self.refresh_button.setObjectName("stats_refresh")
        self.refresh_button.setToolTip(t("gui.statspanel.refresh.tip"))
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.clicked.connect(lambda _checked=False: self.refresh())
        stale_row.addWidget(self.refresh_button)
        self.stale_frame.setVisible(False)
        layout.addWidget(self.stale_frame)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.header_label = QLabel(self)
        self.header_label.setObjectName("stats_header")
        self.header_label.setTextFormat(Qt.TextFormat.PlainText)
        self.header_label.setWordWrap(True)
        self.header_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(self.header_label, 1)
        self.dupes_button = QPushButton(t("gui.statspanel.dupes"), self)
        self.dupes_button.setObjectName("stats_dupes")
        self.dupes_button.setAutoDefault(False)
        self.dupes_button.clicked.connect(lambda _checked=False: self.open_dupes())
        head.addWidget(self.dupes_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        self.types_bar = _TypesBar(self)
        layout.addWidget(self.types_bar)

        split = QSplitter(Qt.Orientation.Vertical, self)
        split.setChildrenCollapsible(False)
        self.summary = _table(split, [t("gui.statspanel.col.what"), t("gui.statspanel.col.value"),
                                      t("gui.statspanel.col.share")], t("gui.statspanel.summary.name"))
        self.summary.setObjectName("stats_summary")
        header = self.summary.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.summary.setColumnWidth(2, BAR_WIDTH + 70)
        self._share_delegate = _ShareDelegate(self)
        self.summary.setItemDelegateForColumn(2, self._share_delegate)
        self.summary.activated.connect(self._on_summary_activated)
        self.summary.customContextMenuRequested.connect(self._on_summary_menu)
        split.addWidget(self.summary)
        top_box = QWidget(split)
        top_layout = QVBoxLayout(top_box)
        top_layout.setContentsMargins(0, 4, 0, 0)
        top_layout.setSpacing(2)
        self.top_title = QLabel(top_box)
        self.top_title.setObjectName("stats_top_title")
        self.top_title.setTextFormat(Qt.TextFormat.PlainText)
        self.top_title.setWordWrap(True)
        top_layout.addWidget(self.top_title)
        self.top_table = _table(top_box, [t("gui.statspanel.col.rank"), t("gui.statspanel.col.value"),
                                          t("gui.statspanel.col.count"), t("gui.statspanel.col.records"),
                                          t("gui.statspanel.col.share")], t("gui.statspanel.top.name"))
        self.top_table.setObjectName("stats_top")
        theader = self.top_table.horizontalHeader()
        theader.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        theader.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        theader.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        theader.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        theader.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.top_table.setColumnWidth(4, BAR_WIDTH + 70)
        self.top_table.setItemDelegateForColumn(4, self._share_delegate)
        self.top_table.activated.connect(self._on_top_activated)
        self.top_table.customContextMenuRequested.connect(self._on_top_menu)
        top_layout.addWidget(self.top_table, 1)
        split.addWidget(top_box)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.splitter = split

        self.warnings_label = QLabel(self)
        self.warnings_label.setObjectName("stats_warnings")
        self.warnings_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warnings_label.setVisible(False)
        layout.addWidget(self.warnings_label)

        self._menu_reason = MenuReason(self)
        for table, slot in ((self.summary, self._copy_summary), (self.top_table, self._copy_top)):
            table.installEventFilter(self._menu_reason)
            table.viewport().installEventFilter(self._menu_reason)
            action = QAction(table)
            action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(lambda _checked=False, fn=slot: fn())
            table.addAction(action)

    def _apply_fonts(self) -> None:
        try:
            font = mono_font(self._session)
            self.top_table.setFont(font)
        except Exception:  # pragma: no cover - font problems never break the panel
            log.debug("could not set the value font", exc_info=True)

    def _on_theme(self) -> None:
        if self._dead or not _alive(self):
            return
        self.field_error.setStyleSheet(f"color: {theme.color('danger').name()};")
        self.stale_label.setStyleSheet(f"color: {theme.color('warn').name()};")
        self.warnings_label.setStyleSheet(f"color: {theme.color('warn').name()};")
        self.header_label.setStyleSheet("font-weight: bold;")
        self._style_status()
        for table in (self.summary, self.top_table):
            table.viewport().update()
        self.types_bar.update()

    def _style_status(self) -> None:
        st = self._current()
        kind = st.message_kind if st is not None and st.message else (
            "error" if st is not None and st.status == "failed" else "info")
        role = {"error": "danger", "warning": "warn"}.get(kind)
        self.status_label.setStyleSheet(f"color: {theme.color(role).name()};" if role
                                        else f"color: {theme.color('muted').name()};")

    # ================================================================================== sessions
    def session(self) -> Any:
        return self._session

    def set_session(self, session: Any) -> None:
        """Show the statistics of ``session`` (``None``: no document). The field text and the scope are
        kept per session."""
        if session is not None and getattr(session, "closed", False):
            session = None
        old = self._session
        if old is session:
            self._refresh()
            return
        old_st = self._state_for(old, create=False) if old is not None else None
        if old_st is not None:
            old_st.field = self.field_text()
            old_st.scope = self.scope()
        self._session = session
        st = self._state_for(session) if session is not None else None
        self._syncing = True
        try:
            self._set_field_items(st.fields if st is not None else [])
            self.field_combo.setEditText(st.field if st is not None else "")
            self._select_scope(st.scope if st is not None else "view")
        finally:
            self._syncing = False
        self._apply_fonts()
        if st is not None:
            self._load_fields(st)
        self._validate_field()
        self._refresh()

    def _state_for(self, session: Any, create: bool = True) -> _State | None:
        if session is None:
            return None
        st = self._states.get(id(session))
        if st is not None and st.session is not session:   # a recycled id
            self._drop_state(st)
            st = None
        if st is None and create:
            st = _State(session)
            self._states[id(session)] = st
            session.state_changed.connect(self._on_session_state)
            session.view_changed.connect(self._on_session_view)
            session.count_changed.connect(self._on_session_count)
            for name, slot in (("index_finished", self._on_session_index),
                               ("view_busy_changed", self._on_session_busy),
                               ("data_changed", self._on_session_data)):
                signal = getattr(session, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(slot)
        return st

    def _drop_state(self, st: _State) -> None:
        st.req += 1
        for task in (st.task, st.reveal_task):
            if task is not None and getattr(task, "state", "running") == "running":
                task.cancel()
        st.task = st.reveal_task = None
        self._states.pop(id(st.session), None)

    def _current(self) -> _State | None:
        return self._state_for(self._session, create=False) if self._session is not None else None

    def _sender_state(self) -> _State | None:
        session = self.sender()
        if session is None:
            return None
        st = self._states.get(id(session))
        return st if st is not None and st.session is session else None

    def _on_session_state(self, value: str) -> None:
        st = self._sender_state()
        if st is None:
            return
        if value == "closed":
            self._drop_state(st)
            if st.session is self._session:
                self.set_session(None)
            return
        if value == "opening":
            # a reopen detached the session's tasks: their callbacks never come; positions are void
            st.req += 1
            st.data_serial += 1
            st.reveal_task = None
            if st.running:
                st.task = None
                st.status = "idle"
                self._set_message(st, t("gui.statspanel.status.reopened"), "warning")
            if st.result is not None:
                st.stale = "data"
            st.fields = []
            if st.session is self._session:
                self._set_field_items([])
        if st.session is self._session:
            if value == "ready":
                self._load_fields(st)
            self._refresh()

    def _on_session_index(self) -> None:
        st = self._sender_state()
        if st is not None and st.session is self._session:
            self._load_fields(st)
            self._refresh()

    def _on_session_view(self) -> None:
        st = self._sender_state()
        if st is None:
            return
        meta = st.meta
        if st.result is not None and meta is not None and meta.scope == "view" and st.stale is None:
            st.stale = "view"
        if st.session is self._session:
            self._update_scope_texts()
            self._refresh()

    def _on_session_count(self) -> None:
        st = self._sender_state()
        if st is not None and st.session is self._session and not self._scope_timer.isActive():
            self._scope_timer.start()

    def _on_session_busy(self, _busy: bool) -> None:
        st = self._sender_state()
        if st is not None and st.session is self._session:
            self._refresh_buttons()

    def _on_session_data(self, _cs: Any) -> None:
        st = self._sender_state()
        if st is None:
            return
        st.data_serial += 1
        if st.reveal_task is not None:
            st.reveal_task.cancel()
            st.reveal_task = None
        if st.result is not None:
            st.stale = "data"
        if st.session is self._session:
            self._refresh()

    # ================================================================================== fields
    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt name
        super().showEvent(event)
        st = self._current()
        if st is not None and not st.fields:
            self._load_fields(st)

    def _load_fields(self, st: _State) -> None:
        """Fill the field list from the shared schema cache (a sample task when nothing is cached) -- only
        while the panel is visible: a hidden dock asks nothing (its ``showEvent`` asks later)."""
        if not self.isVisible():
            return
        session = st.session
        doc = getattr(session, "doc", None)
        if doc is None or not getattr(doc, "index_complete", False):
            return
        ref = weakref.ref(self)
        try:
            schema = fields.get_schema(session, lambda sch, s=session: _call(ref, "_fields_ready", s, sch))
        except Exception:  # pragma: no cover - the field list is a convenience
            log.debug("the schema for the field list is not available", exc_info=True)
            return
        if schema is not None:
            self._fields_ready(session, schema)

    def _fields_ready(self, session: Any, schema: Any) -> None:
        st = self._state_for(session, create=False)
        if st is None or st.session is not session:
            return
        doc = getattr(session, "doc", None)
        items: list[str] = []
        if doc is not None and getattr(doc, "member_records", False) and getattr(doc, "kind", None) != "single":
            items.append(KEY_FIELD)
        for text in field_paths(schema):
            if text not in items:
                items.append(text)
        st.fields = items
        if session is self._session:
            self._set_field_items(items)

    def _set_field_items(self, items: list[str]) -> None:
        text = self.field_combo.currentText()
        was = self._syncing
        self._syncing = True
        try:
            self.field_combo.blockSignals(True)
            self.field_combo.clear()
            self.field_combo.addItems(items)
            self.field_combo.setEditText(text)
        finally:
            self.field_combo.blockSignals(False)
            self._syncing = was
        self.field_model.setStringList(items)

    def field_items(self) -> list[str]:
        """The fields offered by the combo (and its completer)."""
        return [self.field_combo.itemText(i) for i in range(self.field_combo.count())]

    def field_text(self) -> str:
        return self.field_combo.currentText().strip()

    def set_field_text(self, text: str) -> None:
        self.field_combo.setEditText(str(text))

    def _on_field_changed(self, _text: str = "") -> None:
        if not self._syncing:
            self._validate_field()
            self._refresh_buttons()

    def _validate_field(self) -> str | None:
        """The validated field, or ``None`` (the error is shown under the combo when the text is not
        empty)."""
        text = self.field_text()
        if not text:
            self.field_error.setVisible(False)
            return None
        try:
            value = _parse_field(text)
        except JsonReshapeError as exc:
            self.field_error.setText(exc.message)
            self.field_error.setVisible(True)
            return None
        self.field_error.setVisible(False)
        return value

    def _on_return(self) -> None:
        if self.completer.popup() is not None and self.completer.popup().isVisible():
            return
        self.compute()

    # ================================================================================== scope
    def scope(self) -> str:
        value = self.scope_combo.currentData()
        return value if value in SCOPES else "view"

    def set_scope(self, scope: str) -> None:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope!r}")
        self._select_scope(scope)

    def _select_scope(self, scope: str) -> None:
        index = self.scope_combo.findData(scope)
        if index >= 0:
            self.scope_combo.setCurrentIndex(index)

    def _on_scope_changed(self, _index: int = 0) -> None:
        if self._syncing:
            return
        st = self._current()
        if st is not None:
            st.scope = self.scope()
        self._refresh_buttons()

    def _scope_label(self, session: Any, scope: str) -> str:
        doc = getattr(session, "doc", None)
        if doc is not None and getattr(doc, "kind", None) == "single":
            return t("gui.statspanel.scope.single")
        if scope == "all":
            total = int(doc.record_count) if doc is not None else 0
            return t("gui.statspanel.scope.all", count=_count(total))
        try:
            n = int(session.view_len())
        except Exception:
            n = 0
        try:
            label = session.view_label()
        except Exception:
            label = ""
        return _tn("gui.statspanel.scope.view", n, count=_count(n), label=label)

    def _update_scope_texts(self) -> None:
        if self._dead or not _alive(self):
            return
        session = self._session
        for i in range(self.scope_combo.count()):
            scope = self.scope_combo.itemData(i)
            text = self._scope_label(session, scope) if session is not None else ""
            self.scope_combo.setItemText(i, text)
            self.scope_combo.setItemData(i, text, Qt.ItemDataRole.ToolTipRole)
        doc = getattr(session, "doc", None) if session is not None else None
        single = doc is not None and getattr(doc, "kind", None) == "single"
        self.scope_combo.setEnabled(session is not None and not single)

    # ================================================================================== compute
    def _blocked(self, st: _State | None, *, field: str | None = None, scope: str | None = None) -> str:
        """Why a computation of ``field`` over ``scope`` (default: the inputs) cannot start now (``""``: it
        can)."""
        if st is None:
            return t("gui.statspanel.status.no_document")
        doc = getattr(st.session, "doc", None)
        if doc is None or not getattr(doc, "index_complete", False):
            return t("gui.statspanel.status.not_ready")
        scope = self.scope() if scope is None else scope
        if scope == "view" and getattr(doc, "kind", None) != "single" \
                and bool(getattr(st.session, "view_busy", False)):
            return t("gui.statspanel.status.view_busy")
        text = self.field_text() if field is None else field
        if not text:
            return t("gui.statspanel.field.empty")
        try:
            _parse_field(text)
        except JsonReshapeError as exc:
            return exc.message
        return ""

    def compute(self, *, exact: bool = False) -> Any:
        """Compute the statistics of the field over the scope (a running computation of the session is
        replaced). Returns the task, or ``None`` when it cannot start (the reason is shown)."""
        st = self._current()
        reason = self._blocked(st)
        if reason or st is None:
            if st is not None:
                self._set_message(st, reason, "warning")
                self._refresh()
            else:
                self.status_label.setText(reason)
            return None
        session = st.session
        field = _parse_field(self.field_text())
        live = session.doc
        single = getattr(live, "kind", None) == "single"
        scope = "all" if single else self.scope()
        doc, view = session.snapshot()
        if doc is None:
            self._set_message(st, t("gui.statspanel.status.not_ready"), "warning")
            self._refresh()
            return None
        source = None
        if scope == "view" and not bool(getattr(session, "view_is_base", True)) and view is not None:
            source = view
        records = len(source) if source is not None else int(doc.record_count)
        self._stop(st)
        st.req += 1
        req = st.req
        st.status = "running"
        st.error = None
        st.message = ""
        st.result = None
        st.final = False
        st.stale = None
        st.meta = _Meta(field=field, scope=scope, label=self._scope_label(session, scope), records=records,
                        data_serial=st.data_serial, view_serial=getattr(session, "view_serial", None),
                        exact=bool(exact), single=single)
        st.field = field
        st.scope = self.scope()
        memory_limit = self.memory_limit
        name = getattr(session, "name", "")
        ref = weakref.ref(self)

        def work(ctx: Any) -> FieldStats:
            def partial(snaps: list[FieldStats]) -> None:
                if snaps:
                    ctx.post(snaps[0])

            return field_stats(doc, [field], source, top=TOP_VALUES, exact=bool(exact), memory_limit=memory_limit,
                               progress=ctx.progress, cancel=ctx.cancel, on_partial=partial)[0]

        title = t("gui.statspanel.task.exact" if exact else "gui.statspanel.task", field=_elide(field, 40), name=name)
        task = self.app.tasks.run(
            work, title=title, kind="stats", owner=session, heavy=True,
            on_item=lambda item, st=st, req=req: _call(ref, "_on_partial", st, req, item),
            on_done=lambda fs, st=st, req=req: _call(ref, "_on_done", st, req, fs),
            on_error=lambda exc, st=st, req=req: _call(ref, "_on_failed", st, req, exc),
            on_cancel=lambda st=st, req=req: _call(ref, "_on_cancelled", st, req))
        st.task = task
        keep_for(session, task, sets=(source,) if source is not None else (), doc=doc)
        self._refresh()
        return task

    def refresh(self) -> Any:
        """Compute the result shown again (the "Yangilash" of a stale result): its field and scope, exact
        when it was computed exactly; without a result: :meth:`compute`."""
        st = self._current()
        meta = st.meta if st is not None else None
        if meta is None:
            return self.compute()
        self._restore_inputs(meta)
        return self.compute(exact=meta.exact)

    def compute_exact(self) -> Any:
        """"Aniq hisoblash": the field and scope of the (approximate) result shown, with ``exact=True``."""
        st = self._current()
        if st is not None and st.meta is not None:
            self._restore_inputs(st.meta)
        return self.compute(exact=True)

    def _restore_inputs(self, meta: _Meta) -> None:
        self.set_field_text(meta.field)
        if not meta.single:
            self._select_scope(meta.scope)
        self._validate_field()

    def cancel(self) -> None:
        """Stop the current session's computation (the partial result so far stays)."""
        st = self._current()
        if st is not None and st.running and st.task is not None:
            st.task.cancel()

    def _stop(self, st: _State) -> None:
        task = st.task
        if task is not None and getattr(task, "state", "running") == "running":
            task.cancel()
        st.task = None
        st.req += 1

    def _on_partial(self, st: _State, req: int, item: Any) -> None:
        if req != st.req or not isinstance(item, FieldStats):
            return
        st.result = item
        st.final = False
        self.partial_received.emit(st.session, item)
        if st.session is self._session:
            self._refresh()

    def _on_done(self, st: _State, req: int, fs: FieldStats) -> None:
        if req != st.req:
            return
        st.task = None
        st.status = "done"
        st.result = fs
        st.final = True
        meta = st.meta
        st.stale = None
        if meta is not None:
            if st.data_serial != meta.data_serial:
                st.stale = "data"
            elif meta.scope == "view" and getattr(st.session, "view_serial", None) != meta.view_serial:
                st.stale = "view"
        if st.session is self._session:
            self._refresh()
        self.computed.emit(st.session, fs)

    def _on_cancelled(self, st: _State, req: int) -> None:
        if req != st.req:
            return
        st.task = None
        st.status = "cancelled"
        if st.session is self._session:
            self._refresh()

    def _on_failed(self, st: _State, req: int, exc: BaseException) -> None:
        if req != st.req:
            return
        st.task = None
        st.status = "failed"
        st.error = exc
        if st.session is self._session:
            self._refresh()
        try:
            self.app.report_error(exc, self)
        except Exception:  # pragma: no cover - reporting never raises into Qt
            log.exception("could not report %r", exc)

    def _on_task_updated(self, task: Any) -> None:
        st = self._current()
        if st is None or task is not st.task or self._dead:
            return
        self._update_progress(task)

    def _update_progress(self, task: Any) -> None:
        if getattr(task, "queued", False):
            self.progress_bar.setRange(0, 0)
            return
        info = task.info() if hasattr(task, "info") else None
        if info is None or info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))

    # ================================================================================== state
    def result(self) -> FieldStats | None:
        """The statistics shown (a final result or a live snapshot: ``FieldStats.partial``)."""
        st = self._current()
        return st.result if st is not None else None

    def status(self) -> str:
        """``idle`` | ``running`` | ``done`` | ``cancelled`` | ``failed`` of the current session."""
        st = self._current()
        return st.status if st is not None else "idle"

    def stale(self) -> str | None:
        """``None`` or why the result is out of date (``data`` | ``view``)."""
        st = self._current()
        return st.stale if st is not None else None

    @property
    def task(self) -> Any:
        """The running computation of the current session (``None`` when none)."""
        st = self._current()
        return st.task if st is not None else None

    def _set_message(self, st: _State, text: str, kind: str = "info") -> None:
        st.message = text or ""
        st.message_kind = kind

    def _status_text(self, st: _State | None) -> str:
        if st is None:
            return t("gui.statspanel.status.no_document")
        if st.message:
            return st.message
        fs = st.result
        if st.status == "running":
            if fs is not None and st.meta is not None:
                return t("gui.statspanel.status.partial", done=_count(fs.scanned), total=_count(st.meta.records))
            return t("gui.statspanel.status.running")
        if st.status == "done" and fs is not None:
            return t("gui.statspanel.status.done", seconds=_decimal(fs.elapsed, 1))
        if st.status == "cancelled":
            return t("gui.statspanel.status.cancelled" if fs is not None else "gui.statspanel.status.cancelled_empty")
        if st.status == "failed":
            return t("gui.statspanel.status.failed", error=_error_text(st.error))
        doc = getattr(st.session, "doc", None)
        if doc is None or not getattr(doc, "index_complete", False):
            return t("gui.statspanel.status.not_ready")
        return t("gui.statspanel.status.idle")

    def status_text(self) -> str:
        return self._status_text(self._current())

    # ================================================================================== refresh
    def _refresh(self) -> None:
        if self._dead or not _alive(self):
            return
        st = self._current()
        self.field_combo.setEnabled(st is not None)
        self._update_scope_texts()
        self.status_label.setText(self._status_text(st))
        self._style_status()
        running = st is not None and st.running
        self.progress_bar.setVisible(running)
        if running and st is not None and st.task is not None:
            self._update_progress(st.task)
        stale = st.stale if st is not None else None
        self.stale_frame.setVisible(bool(stale) and not running)
        if stale:
            self.stale_label.setText(t("gui.statspanel.stale.data" if stale == "data" else "gui.statspanel.stale.view"))
        self._render(st)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        if self._dead or not _alive(self):
            return
        st = self._current()
        running = st is not None and st.running
        self.compute_button.setVisible(not running)
        self.cancel_button.setVisible(running)
        reason = self._blocked(st)
        self.compute_button.setEnabled(not reason)
        self.compute_button.setToolTip(reason or t("gui.statspanel.compute.tip"))
        fs = st.result if st is not None else None
        meta = st.meta if st is not None else None
        again = self._blocked(st, field=meta.field, scope=meta.scope) if meta is not None else reason
        approximate = bool(fs is not None and st is not None and st.final and not fs.partial
                           and (not fs.distinct_exact or not fs.top_exact))
        self.exact_button.setEnabled(approximate and not running and not again)
        if approximate:
            self.exact_button.setToolTip(again or t("gui.statspanel.exact.tip"))
        else:
            self.exact_button.setToolTip(t("gui.statspanel.exact.tip_off"))
        self.refresh_button.setEnabled(not again)
        self.refresh_button.setToolTip(again or t("gui.statspanel.refresh.tip"))
        why = self._dupes_why(st)
        self.dupes_button.setVisible(fs is not None)
        self.dupes_button.setEnabled(not why)
        self.dupes_button.setToolTip(why or t("gui.statspanel.dupes.tip"))

    def _dupes_why(self, st: _State | None) -> str:
        fs = st.result if st is not None else None
        if st is None or fs is None:
            return t("gui.statspanel.status.idle")
        if st.meta is not None and st.meta.single:
            return t("gui.statspanel.why.single")
        if not fs.is_key and not PathPattern(tuple(fs.path)).is_concrete:
            return t("gui.statspanel.why.dupes_wild")
        if not fs.top or fs.top[0].count < 2:
            return t("gui.statspanel.why.dupes_none")
        return ""

    def _render(self, st: _State | None) -> None:
        fs = st.result if st is not None else None
        self._rows = []
        self._top = []
        if fs is None or st is None:
            self.header_label.setText("")
            self.types_bar.set_parts([], 0)
            self.types_bar.setVisible(False)
            self.summary.setRowCount(0)
            self.top_table.setRowCount(0)
            self.top_title.setText("")
            self.warnings_label.setVisible(False)
            return
        meta = st.meta
        single = bool(meta is not None and meta.single)
        if fs.partial and meta is not None:
            head = t("gui.statspanel.header.partial", field=fs.field, done=_count(fs.scanned),
                     total=_count(meta.records))
        else:
            head = _tn("gui.statspanel.header", fs.scanned, field=fs.field, records=_count(fs.scanned),
                       seconds=_decimal(fs.elapsed, 1))
        lines = [head]
        if meta is not None:
            lines.append(t("gui.statspanel.header.scope", label=meta.label))
        if fs.context != "$":
            lines.append(t("gui.statspanel.header.context", context=fs.context, count=_count(fs.base)))
        self.header_label.setText("\n".join(lines))
        self.types_bar.set_parts(list(fs.types.items()), fs.values)
        self.types_bar.setVisible(bool(fs.types))
        self._rows = summary_rows(fs, single=single)
        self._fill_summary()
        self._top = top_rows(fs, single=single)
        self._fill_top(fs)
        warnings = fs.warning_texts() if not fs.partial else []
        self.warnings_label.setText("\n".join(warnings))
        self.warnings_label.setVisible(bool(warnings))

    def _fill_summary(self) -> None:
        table = self.summary
        current = table.currentRow()
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(self._rows))
            for i, row in enumerate(self._rows):
                tip = row.tip
                if row.filters:
                    tip = "\n".join(x for x in (row.filters[0].text, tip) if x)
                elif row.why:
                    tip = "\n".join(x for x in (tip, row.why) if x)
                table.setItem(i, 0, _item(row.label, key=row.kind, tip=tip))
                table.setItem(i, 1, _item(row.value, key=row.kind, tip=tip or row.value))
                if row.share is None:
                    table.setItem(i, 2, _item("", key=row.kind))
                else:
                    table.setItem(i, 2, _share_item(row.share, row.share_text, row.color, key=row.kind))
            if 0 <= current < len(self._rows):
                table.setCurrentCell(current, 0)
        finally:
            table.setUpdatesEnabled(True)

    def _fill_top(self, fs: FieldStats) -> None:
        table = self.top_table
        rows = self._top
        if fs.partial and (fs.top_bound or any(vc.error for vc in fs.top)):
            # Misra-Gries counts (sketches, the spilled epochs of the exact mode): each may be too low
            title = t("gui.statspanel.top.title_partial_approx",
                      bound=_count(max([fs.top_bound] + [vc.error for vc in fs.top])))
        elif fs.partial:
            title = t("gui.statspanel.top.title_partial")
        elif not rows:
            title = t("gui.statspanel.top.empty")
        elif fs.top_exact:
            title = _tn("gui.statspanel.top.title", len(rows), count=len(rows))
        else:
            title = t("gui.statspanel.top.title_approx", count=len(rows), bound=_count(fs.top_bound))
        self.top_title.setText(title)
        current = table.currentRow()
        table.setUpdatesEnabled(False)
        try:
            table.setColumnHidden(3, not fs.multi)
            table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                vc = row.vc
                tip = t("gui.statspanel.top.value_tip", value=vc.text, record=vc.first)
                if row.why:
                    tip += "\n" + row.why
                elif row.filters:
                    tip += "\n" + row.filters[0].text
                table.setItem(i, 0, _item(str(i + 1), key=i, align_right=True))
                table.setItem(i, 1, _item(vc.text, key=i, tip=tip))
                if vc.error:
                    count_text = t("gui.statspanel.top.count_approx", count=_count(vc.count))
                    count_tip = t("gui.statspanel.top.count_tip", low=_count(vc.count),
                                  high=_count(vc.count + vc.error))
                else:
                    count_text, count_tip = _count(vc.count), ""
                table.setItem(i, 2, _item(count_text, key=i, align_right=True, tip=count_tip))
                table.setItem(i, 3, _item(_count(vc.records), key=i, align_right=True,
                                          tip=t("gui.statspanel.top.records_tip", count=_count(vc.records))))
                table.setItem(i, 4, _share_item(_fraction(vc.count, fs.values), _share_text(vc.count, fs.values),
                                                None, key=i))
            if 0 <= current < len(rows):
                table.setCurrentCell(current, 1)
        finally:
            table.setUpdatesEnabled(True)

    # ================================================================================== rows (API)
    def summary_kinds(self) -> list[str]:
        """The kinds of the summary rows shown (``present``, ``null``, ``missing``, ``type:<t>``,
        ``distinct``, ``values``, ``numbers``, ``min``, ``max``, ``mean``, ``sum``, ``str_min``,
        ``str_max``, ``empty``, ``arr_min``, ``arr_max``, ``obj_min``, ``obj_max``)."""
        return [row.kind for row in self._rows]

    def _row(self, kind: str) -> _Row:
        for row in self._rows:
            if row.kind == kind:
                return row
        raise KeyError(kind)

    def summary_text(self, kind: str) -> str:
        """The value text of a summary row."""
        return self._row(kind).value

    def summary_share(self, kind: str) -> str:
        return self._row(kind).share_text

    def summary_filters(self, kind: str) -> list[StatsFilter]:
        """The filters a summary row offers (the first one is its double-click action)."""
        return list(self._row(kind).filters)

    def top_count(self) -> int:
        return len(self._top)

    def top_value(self, row: int) -> ValueCount:
        return self._top[row].vc

    def top_filters(self, row: int) -> list[StatsFilter]:
        """``[f == v, f != v]`` of a top value (empty when it cannot be filtered by)."""
        return list(self._top[row].filters)

    def records_column_visible(self) -> bool:
        return not self.top_table.isColumnHidden(3)

    def activate_summary(self, kind: str) -> bool:
        """The double-click action of a summary row: its first filter."""
        row = self._row(kind)
        return self._apply(row.filters[0]) if row.filters else False

    def activate_top(self, row: int, *, negate: bool = False) -> bool:
        """Filter by a top value (``negate``: ``!=``)."""
        filters = self._top[row].filters
        if not filters:
            return False
        flt = filters[1] if negate and len(filters) > 1 else filters[0]
        return self._apply(flt)

    def _apply(self, flt: StatsFilter) -> bool:
        st = self._current()
        if st is None:
            return False
        try:
            ok = apply_filter(st.session, flt)
        except (JsonReshapeError, ValueError, TypeError) as exc:
            self.app.report_error(exc if isinstance(exc, JsonReshapeError) else UserInputError(
                "error.generic", detail=str(exc)), self)
            return False
        if ok:
            self.filter_applied.emit(st.session, flt.text)
            show = getattr(self.app, "show_status", None)
            if callable(show):
                show(t("gui.statspanel.filter.applied", expr=_elide(flt.text)), 4000)
        return ok

    def _on_summary_activated(self, index: QModelIndex) -> None:
        if 0 <= index.row() < len(self._rows):
            row = self._rows[index.row()]
            if row.filters:
                self._apply(row.filters[0])
            elif row.why:
                self._flash(row.why)

    def _on_top_activated(self, index: QModelIndex) -> None:
        if 0 <= index.row() < len(self._top):
            row = self._top[index.row()]
            if row.filters:
                self._apply(row.filters[0])
            elif row.why:
                self._flash(row.why)

    def _flash(self, text: str) -> None:
        show = getattr(self.app, "show_status", None)
        if callable(show):
            show(text, 6000)

    # ================================================================================== copy
    def _copy(self, text: str | None) -> None:
        if text is None:
            return
        set_clipboard_text(text)
        self._flash(t("gui.statspanel.copied"))

    def _copy_summary(self) -> None:
        r = self.summary.currentRow()
        if 0 <= r < len(self._rows):
            self._copy(self._rows[r].copy if self._rows[r].copy is not None else self._rows[r].value)

    def _copy_top(self) -> None:
        r = self.top_table.currentRow()
        if 0 <= r < len(self._top):
            self._copy(self._value_json(self._top[r].vc))

    @staticmethod
    def _value_json(vc: ValueCount) -> str:
        if vc.value is MISSING:
            return vc.text
        try:
            return codec.dumps(vc.value).decode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover - values come from codec
            return vc.text

    # ================================================================================== menus
    def _reveal_why(self, st: _State | None) -> str:
        if st is None:
            return t("gui.statspanel.status.no_document")
        if st.stale == "data":
            return t("gui.statspanel.why.stale")
        return ""

    def summary_menu(self, kind: str) -> QMenu:
        """The context menu of a summary row (the caller shows it)."""
        row = self._row(kind)
        st = self._current()
        menu = QMenu(self)
        menu.setObjectName("stats_summary_menu")
        menu.setToolTipsVisible(True)
        for flt in row.filters:
            action = menu.addAction(_menu_text(t("gui.statspanel.menu.filter", expr=_elide(flt.text))))
            action.setToolTip(flt.text)
            action.triggered.connect(lambda _checked=False, f=flt: self._apply(f))
        if not row.filters and row.why:
            action = menu.addAction(t("gui.statspanel.menu.filter_none"))
            action.setEnabled(False)
            action.setToolTip(row.why)
        if row.reveal is not None:
            action = menu.addAction(t("gui.statspanel.menu.reveal"))
            why = self._reveal_why(st)
            if why:
                action.setEnabled(False)
                action.setToolTip(why)
            position, value = row.reveal
            action.triggered.connect(lambda _checked=False, p=position, v=value: self._reveal(p, v))
        menu.addSeparator()
        copy = menu.addAction(t("gui.statspanel.menu.copy"))
        copy.triggered.connect(lambda _checked=False, r=row: self._copy(r.copy if r.copy is not None else r.value))
        return menu

    def top_menu(self, row: int) -> QMenu:
        """The context menu of a top value (the caller shows it)."""
        top = self._top[row]
        st = self._current()
        menu = QMenu(self)
        menu.setObjectName("stats_top_menu")
        menu.setToolTipsVisible(True)
        for flt in top.filters:
            action = menu.addAction(_menu_text(t("gui.statspanel.menu.filter", expr=_elide(flt.text))))
            action.setToolTip(flt.text)
            action.triggered.connect(lambda _checked=False, f=flt: self._apply(f))
        if not top.filters:
            action = menu.addAction(t("gui.statspanel.menu.filter_none"))
            action.setEnabled(False)
            action.setToolTip(top.why)
        action = menu.addAction(t("gui.statspanel.menu.reveal"))
        why = self._reveal_why(st)
        if why:
            action.setEnabled(False)
            action.setToolTip(why)
        action.triggered.connect(lambda _checked=False, i=row: self.reveal_top(i))
        menu.addSeparator()
        copy = menu.addAction(t("gui.statspanel.menu.copy"))
        copy.triggered.connect(lambda _checked=False, vc=top.vc: self._copy(self._value_json(vc)))
        return menu

    def _popup(self, menu: QMenu, table: QTableWidget, pos: QPoint) -> None:
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.last_menu = menu
        menu.popup(table.viewport().mapToGlobal(pos))

    def _menu_row(self, table: QTableWidget, pos: QPoint) -> tuple[int, QPoint]:
        keyboard = self._menu_reason.keyboard
        self._menu_reason.keyboard = False
        row = -1 if keyboard else table.rowAt(pos.y())
        if row < 0:   # the context menu key (or no row under the mouse): the current row
            row = table.currentRow()
            if row >= 0:
                item = table.item(row, 0)
                rect = table.visualItemRect(item) if item is not None else QRect()
                pos = rect.center() if rect.isValid() else QPoint(4, 4)
        return row, pos

    def _on_summary_menu(self, pos: QPoint) -> None:
        row, pos = self._menu_row(self.summary, pos)
        if 0 <= row < len(self._rows):
            self._popup(self.summary_menu(self._rows[row].kind), self.summary, pos)

    def _on_top_menu(self, pos: QPoint) -> None:
        row, pos = self._menu_row(self.top_table, pos)
        if 0 <= row < len(self._top):
            self._popup(self.top_menu(row), self.top_table, pos)

    # ================================================================================== show in tree
    def reveal_top(self, row: int) -> Any:
        """"Daraxtda ko'rsatish" of a top value (the first record holding it). Returns the NodeRef
        selected, the silent task resolving it (wildcard patterns) or ``None``."""
        vc = self._top[row].vc
        return self._reveal(int(vc.first), vc.value)

    def reveal_summary(self, kind: str) -> Any:
        """"Daraxtda ko'rsatish" of the min / max row."""
        row = self._row(kind)
        if row.reveal is None:
            return None
        return self._reveal(*row.reveal)

    def _reveal(self, position: int, value: Any) -> Any:
        st = self._current()
        why = self._reveal_why(st)
        fs = st.result if st is not None else None
        if why or st is None or fs is None:
            if why:
                self._flash(why)
            return None
        session = st.session
        single = bool(st.meta is not None and st.meta.single)
        record = None if single else int(position)
        if st.reveal_task is not None:
            st.reveal_task.cancel()
            st.reveal_task = None
        path = tuple(fs.path)
        if fs.is_key or not path:
            return self._select(st, NodeRef(record, ()))
        pattern = PathPattern(path)
        if pattern.is_concrete:
            return self._select(st, NodeRef(record, path))
        doc = session.snapshot()[0]
        if doc is None:
            return None
        serial = st.data_serial
        ref = weakref.ref(self)

        def work(ctx: Any) -> NodeRef | None:
            return locate_value(doc, record, path, value, ctx.cancel)

        def done(found: NodeRef | None, st: _State = st) -> None:
            st.reveal_task = None
            if serial != st.data_serial:
                return              # the data changed meanwhile: positions of another state
            if found is None:
                _call(ref, "_flash", t("gui.statspanel.reveal.not_found", record=position))
                found = NodeRef(record, ())
            _call(ref, "_select", st, found)

        app = self.app

        def failed(exc: BaseException, st: _State = st) -> None:
            st.reveal_task = None
            try:
                app.report_error(exc)
            except Exception:  # pragma: no cover
                log.exception("could not report %r", exc)

        def cancelled(st: _State = st) -> None:
            st.reveal_task = None

        task = self.app.tasks.run(work, title=t("gui.statspanel.task.reveal"), kind="value", owner=session,
                                  silent=True, on_done=done, on_error=failed, on_cancel=cancelled)
        st.reveal_task = task
        return task

    def _select(self, st: _State, ref: NodeRef) -> NodeRef | None:
        session = st.session
        if getattr(session, "closed", False) or getattr(session, "doc", None) is None:
            return None
        session.select(ref, reveal=True)
        return ref

    # ================================================================================== entry points
    def show_field(self, path: Any, *, scope: str | None = None, compute: bool = True) -> Any:
        """Put ``path`` in the field combo (a text, or a node path: indices become ``[*]``), optionally
        choose the scope, and compute (returns the task, ``None`` when it could not start)."""
        self.set_field_text(field_for_path(path))
        if scope is not None:
            self.set_scope(scope)
        self._validate_field()
        self._refresh_buttons()
        if compute:
            return self.compute()
        return None

    def open_dupes(self) -> Any:
        """Open the duplicates dialog with the field as the key (module docstring)."""
        st = self._current()
        why = self._dupes_why(st)
        if why or st is None or st.result is None:
            if why:
                self._flash(why)
            return None
        key = condition_field(st.result)
        scope = st.meta.scope if st.meta is not None else "view"
        if self.dupes_opener is not None:
            return self.dupes_opener(st.session, key)
        from jsonreshape.gui.dupesdialog import show_dupes_dialog

        return show_dupes_dialog(self.window(), st.session, key=key, scope=scope)

    # ================================================================================== lifetime
    def shutdown(self) -> None:
        """Cancel every computation and forget the results (the main window is closing)."""
        for st in list(self._states.values()):
            self._drop_state(st)
        self._states.clear()
        self._session = None
        self._scope_timer.stop()
        self._dead = True
