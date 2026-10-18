"""Data side of the tree view (docs/ARCHITECTURE.md §11.7 "Tree"; Qt-free).

:mod:`jsonreshape.gui.treeview` shows a lazily expanded tree of
:class:`~jsonreshape.reader.Node` objects through a :class:`~jsonreshape.gui.flattree.FlatTree`
whose node identity is the **key path** ``(top_key, c1, c2, ...)``:

* ``top_key`` is a record number (records of the current view), :data:`WRAPPER` (the wrapper
  object of a wrapped document, shown as the first row) or :data:`ROOT` (the value of a single
  document);
* ``ci`` are child indices in file order (``Node.children(i, i + 1)``).

Everything here runs in worker threads (the tree's ``RowLoader`` and its tasks) except the
pure text helpers, which the GUI thread uses too:

* :class:`NodeInfo` -- what a row shows (kind, key, absolute path, span, child count, value
  text) -- built by :meth:`NodeSource.make_info`;
* :class:`NodeSource` -- an LRU of ``Node`` objects by key path over one document: record
  nodes via ``doc.record_node(i)``, children fetched in contiguous ranges per parent
  (:meth:`NodeSource.fetch` is the loader's fetch function), child counts, the display
  orders of node sorting (:meth:`~NodeSource.key_order`, :meth:`~NodeSource.element_order`),
  the expand-all / expand-to-level planner (:meth:`~NodeSource.plan`) and the ancestor chain
  of a reveal (:meth:`~NodeSource.chain`);
* text helpers: :func:`display_key`, :func:`string_display`, :func:`scalar_display`,
  :func:`summarize_prefix`, :func:`container_text`, :func:`key_label`, :func:`find_matches`;
* :func:`stable_argsort` -- the display orders' sort: stable, but in small C-level steps
  (sorted runs + a k-way block merge) so a worker never holds the GIL for long (one
  ``sorted()`` of a million keys holds it for 0.2-0.6 s and freezes the GUI thread).

Child counts are computed only where that is cheap: when the children are already indexed
(``Node.children_indexed``) or the node is small (:data:`SMALL_NODE_BYTES` and not
``is_large``). Larger nodes get their count from the "children" task of the view.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from jsonreshape import codec, core
from jsonreshape.errors import Cancelled, JsonReshapeError
from jsonreshape.i18n import t
from jsonreshape.paths import MISSING, format_path
from jsonreshape.sortkeys import SortKey, collation_key
from jsonreshape.tasks import CancelToken, Progress, check

log = logging.getLogger(__name__)

__all__ = [
    "CHILD_BATCH",
    "ELLIPSIS",
    "ExpandPlan",
    "NodeInfo",
    "NodeSource",
    "ROOT",
    "SMALL_NODE_BYTES",
    "WRAPPER",
    "container_text",
    "display_key",
    "find_matches",
    "key_label",
    "scalar_display",
    "search_texts",
    "stable_argsort",
    "string_display",
    "summarize_prefix",
]

WRAPPER = "wrapper"          # top key of the wrapper object row (wrapped documents)
ROOT = "root"                # top key of the root row (single documents)
ELLIPSIS = "…"
SMALL_NODE_BYTES = 1 << 20   # child counts of nodes up to this size are computed by the loader
VALUE_CHARS = 300            # characters of a scalar token read for the value column
SUMMARY_SOURCE_CHARS = 240   # characters of a container's JSON prefix read for its summary
SUMMARY_ITEMS = 6            # members / elements shown in a container summary
SUMMARY_ITEM_CHARS = 40      # one summary item is cut after this many characters
NODE_CACHE = 16384           # Node objects kept by key path
CHILD_BATCH = 4096           # children read per call when walking a node
MAX_RUN_GAP = 16             # loader: sibling indices closer than this are read as one range
READ_WINDOW = 4 << 20        # element sort: bytes read at once
RECORDS_ROLE = "records"     # Node.role of the node holding the records (wrapped documents)
SORT_RUN = 16384             # stable_argsort: keys sorted per C-level call (a few ms of GIL)
SORT_BLOCK = 256             # stable_argsort: minimal block taken from a run per merge step
WALK_SHARE = 0.8             # node sorting progress: reading the keys, then sorting

_CTRL_RE = re.compile("[\x00-\x1f\x7f  ]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_HEX = frozenset("0123456789abcdefABCDEF")
_WS = " \t\r\n"


# ==================================================================================
# row data
# ==================================================================================
@dataclass(slots=True, eq=False)
class NodeInfo:
    """What one tree row shows (built in a worker thread, read in the GUI thread).

    ``count`` is ``None`` while the child count is unknown (a large node whose children are
    not indexed yet); the view sets it once its children task finished. ``records``: the
    member of a wrapped document's wrapper that holds the records (shown with the record
    count, never expanded inline: the records are the rows below the wrapper)."""

    keypath: tuple
    kind: str                    # object | array | string | number | bool | null
    key: str | int | None        # member key / array index / record key (Node.key)
    path: tuple                  # absolute path (Node.path)
    start: int
    end: int
    count: int | None = None
    text: str = ""               # value column text
    large: bool = False          # unknown children need the visible "children" task
    records: bool = False

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def is_container(self) -> bool:
        return self.kind in ("object", "array")

    @property
    def expandable(self) -> bool:
        """Has (or may have) children shown inline."""
        return self.is_container and not self.records and self.count != 0


@dataclass
class ExpandPlan:
    """Result of :meth:`NodeSource.plan`: the nodes to expand (parents first) with their
    child counts and display orders, the rows that adds, and whether the cap was hit."""

    items: list[tuple[tuple, int, Any, str]] = field(default_factory=list)   # (keypath, count, order, kind)
    rows: int = 0
    overflow: bool = False
    positions: dict[Any, int] = field(default_factory=dict)   # top key -> view position (top-level plans)


# ==================================================================================
# text helpers (pure; GUI thread and workers)
# ==================================================================================
def _escape_surrogates(text: str) -> str:
    return _SURROGATE_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _escape_display(text: str) -> str:
    """JSON-style escaping for one-line display (quotes, backslashes, control characters)."""
    return _escape_surrogates(json.dumps(text, ensure_ascii=False)[1:-1].replace(" ", "\\u2028")
                              .replace(" ", "\\u2029"))


def display_key(key: str) -> str:
    """A member key as shown in the "Kalit" column: as is, or JSON-quoted when it is empty,
    has surrounding spaces or contains control characters."""
    key = str(key)
    if not key or key != key.strip(_WS + " ") or _CTRL_RE.search(key):
        return _escape_surrogates(json.dumps(key, ensure_ascii=False).replace(" ", "\\u2028")
                                  .replace(" ", "\\u2029"))
    return _escape_surrogates(key)


def _unescape_partial(body: str) -> str:
    """Decode the JSON escapes of a (possibly cut) string token body; an incomplete escape
    at the end is dropped; surrogate pairs are combined."""
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        j = body.find("\\", i)
        if j < 0:
            out.append(body[i:])
            break
        out.append(body[i:j])
        if j + 1 >= n:
            break
        c = body[j + 1]
        if c == "u":
            hexs = body[j + 2:j + 6]
            if len(hexs) < 4 or not set(hexs) <= _HEX:
                break
            out.append(chr(int(hexs, 16)))
            i = j + 6
        else:
            out.append(_SIMPLE_ESCAPES.get(c, c))
            i = j + 2
    text = "".join(out)
    if _SURROGATE_RE.search(text):
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return text


def string_display(raw: str) -> str:
    """Display text of a string token (``raw`` as ``Node.scalar_text`` gives it: the token,
    cut with ``…`` when long): escapes decoded (``\\u0442`` -> ``т``), quotes kept, control
    characters, quotes and backslashes shown JSON-style, ``…`` for a cut value."""
    raw = raw.strip(_WS)
    if len(raw) >= 2 and raw[0] == '"':
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if isinstance(value, str):
            return '"' + _escape_display(value) + '"'
    body = raw[1:] if raw.startswith('"') else raw
    cut = body.endswith(ELLIPSIS)
    if cut:
        body = body[:-1]
    elif body.endswith('"'):
        body = body[:-1]
    return '"' + _escape_display(_unescape_partial(body)) + (ELLIPSIS if cut else '"')


def scalar_display(kind: str, raw: str) -> str:
    """Value column text of a scalar token (strings decoded, other tokens as written)."""
    if kind == "string":
        return string_display(raw)
    return _escape_surrogates(raw.strip(_WS))


def _cut(text: str, limit: int = SUMMARY_ITEM_CHARS) -> str:
    return text if len(text) <= limit else text[:limit - 1] + ELLIPSIS


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in _WS:
        i += 1
    return i


def _string_end(s: str, i: int) -> int:
    """Index after the string token starting at ``s[i] == '"'`` (-1: not closed)."""
    n = len(s)
    j = i + 1
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        j += 1
    return -1


def _container_end(s: str, i: int) -> int:
    """Index after the container starting at ``s[i]`` (-1: not closed in ``s``)."""
    depth = 0
    n = len(s)
    j = i
    while j < n:
        c = s[j]
        if c == '"':
            e = _string_end(s, j)
            if e < 0:
                return -1
            j = e
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def _scalar_end(s: str, i: int) -> int:
    n = len(s)
    j = i
    while j < n and s[j] not in ",]}" and s[j] not in _WS:
        j += 1
    return j


def summarize_prefix(kind: str, prefix: str, *, max_items: int = SUMMARY_ITEMS) -> str:
    """Short preview of a container from the beginning of its JSON text (``prefix``, maybe
    cut with ``…``): ``id: 1, name: "Ali", …`` for objects, ``1, 2, 3`` for arrays; nested
    containers are shown as ``{…}`` / ``[…]``; ``…`` marks members not shown."""
    cut = prefix.endswith(ELLIPSIS)
    s = prefix[:-1] if cut else prefix
    n = len(s)
    i = _skip_ws(s, 0)
    if i >= n or s[i] not in "[{":
        return ELLIPSIS
    obj = s[i] == "{"
    i += 1
    items: list[str] = []
    complete = False
    while True:
        i = _skip_ws(s, i)
        if i >= n:
            break
        c = s[i]
        if c in "]}":
            complete = True
            break
        if c == ",":
            i += 1
            continue
        if len(items) >= max_items:
            break
        key_text = ""
        if obj:
            if c != '"':
                break
            e = _string_end(s, i)
            if e < 0:
                break
            try:
                key_text = display_key(json.loads(s[i:e]))
            except ValueError:
                key_text = s[i + 1:e - 1]
            i = _skip_ws(s, e)
            if i >= n or s[i] != ":":
                break
            i = _skip_ws(s, i + 1)
            if i >= n:
                break
            c = s[i]
        if c == '"':
            e = _string_end(s, i)
            if e < 0:
                value = string_display(s[i:] + ELLIPSIS)
                items.append(_cut(f"{key_text}: {value}" if obj else value))
                break
            value = string_display(s[i:e])
            i = e
        elif c in "[{":
            e = _container_end(s, i)
            value = "{…}" if c == "{" else "[…]"
            if e < 0:
                items.append(f"{key_text}: {value}" if obj else value)
                break
            if e - i <= 2:   # {} / []
                value = s[i:e].replace(" ", "")
            i = e
        else:
            e = _scalar_end(s, i)
            if e >= n and cut:   # the token may continue
                break
            value = s[i:e]
            i = e
        items.append(_cut(f"{key_text}: {value}" if obj else value))
    text = ", ".join(items)
    if not complete:
        text = f"{text}, {ELLIPSIS}" if items else ELLIPSIS
    return _escape_surrogates(text)


def count_badge(kind: str, count: int | None) -> str:
    """``{5}`` / ``[3]`` / ``{?}``."""
    inner = "?" if count is None else str(count)
    return "{" + inner + "}" if kind == "object" else "[" + inner + "]"


def container_text(kind: str, count: int | None, prefix: str) -> str:
    """Value column text of a container: ``{5} id: 1, name: "Ali", …`` (``{}`` / ``[]``
    when empty)."""
    if count == 0:
        return "{}" if kind == "object" else "[]"
    summary = summarize_prefix(kind, prefix)
    return f"{count_badge(kind, count)} {summary}"


def key_label(keypath: tuple, info: NodeInfo | None) -> str | None:
    """The "Kalit" column text: ``#12`` for records of array-like documents, the key of
    keyed records / object members (:func:`display_key`), ``[3]`` for array elements,
    ``(o'rama)`` for the wrapper, ``$`` for a single document's root. ``None`` when it
    cannot be known without the row's data."""
    top = keypath[0]
    if len(keypath) == 1:
        if top == WRAPPER:
            return t("gui.tree.wrapper")
        if top == ROOT:
            return "$"
        if info is not None and isinstance(info.key, str):
            return display_key(info.key)
        if info is None:
            return None
        return f"#{top}"
    if info is None:
        return None
    if isinstance(info.key, str):
        return display_key(info.key)
    return f"[{keypath[-1]}]"


def search_texts(keypath: tuple, info: NodeInfo) -> tuple[str | None, str | None]:
    """(key text, value text) quick find looks at -- the texts of the row as painted:
    member / record keys (not the labels ``#12`` / ``[3]`` / the wrapper), the value column
    of scalars and the preview of containers (``{5} id: 1, name: "Ali", …``: what a
    collapsed record shows must be findable). The record array of a wrapper shows a
    label, not data: no value text."""
    key = display_key(info.key) if isinstance(info.key, str) else None
    value = None if info.records else (info.text or None)
    return key, value


def find_matches(entries: Iterable[tuple[tuple, str | None, str | None]],
                 matcher: Callable[[str], list[tuple[int, int]]], *, limit: int = 100_000,
                 cancel: CancelToken | None = None) -> dict[tuple, tuple[tuple, tuple]]:
    """``{keypath: (key spans, value spans)}`` of the entries whose key or value text
    matches (at most ``limit`` matches)."""
    out: dict[tuple, tuple[tuple, tuple]] = {}
    for k, (kp, key, value) in enumerate(entries):
        if cancel is not None and not k & 1023:
            check(cancel)
        ks = tuple(matcher(key)) if key else ()
        vs = tuple(matcher(value)) if value else ()
        if ks or vs:
            out[kp] = (ks, vs)
            if len(out) >= limit:
                break
    return out


# ==================================================================================
# stable sort in small steps (worker threads)
# ==================================================================================
def stable_argsort(keys: list, cancel: CancelToken | None = None, progress: Progress | None = None, *,
                   run: int = SORT_RUN, block: int = SORT_BLOCK) -> array:
    """``array('q')`` of the indices of ``keys`` ordered by key, equal keys in index order
    -- exactly ``sorted(range(len(keys)), key=keys.__getitem__)`` -- computed so that no
    single C-level call sorts more than ``2 * run`` items.

    One ``sorted()`` of a million keys keeps the GIL for 0.2-0.6 s (the GUI thread stalls
    that long). Here runs of ``run`` indices are sorted one by one and then merged k ways in
    small steps (``block``: the preferred number of items a step takes from each run):

    * *gallop*: when the next items of one run are below the heads of all other runs they
      are moved out as they are (presorted, descending and all-equal keys need no merging);
    * *merge step*: ``F`` = the smallest key among the last items of the runs' next blocks
      (``best`` = the first run whose block ends with it); every block item that may precede
      the rest goes out in one small ``sorted()`` -- runs before ``best``: items ``<= F``,
      ``best``: its block, later runs: items ``< F`` (parts concatenated in run order, so
      equal keys keep the index order).

    ``progress`` (absolute: up to ``len(keys)``) is updated and ``cancel`` checked between
    the steps."""
    n = len(keys)
    get = keys.__getitem__
    out = array("q")
    run = max(2, int(run))
    if n <= run:
        out.extend(sorted(range(n), key=get))
        if progress is not None:
            progress.update(n)
        return out
    runs: list[list[int]] = []
    for a in range(0, n, run):
        check(cancel)
        runs.append(sorted(range(a, min(n, a + run)), key=get))
        if progress is not None:
            progress.update(min(n, a + run) // 2)
    k = len(runs)
    # k * step <= 2 * run bounds a merge step; at least run // k so that steps stay few
    step = max(1, run // k, min(int(block), (2 * run) // k))
    pos = [0] * k
    live = list(range(k))
    while live:
        check(cancel)
        if len(live) == 1:
            r = live[0]
            seq = runs[r]
            for a in range(pos[r], len(seq), run):
                out.extend(seq[a:a + run])
            break
        best = -1
        top: Any = None           # F
        low: Any = None           # the smallest head of the other runs (found below)
        low_run = -1
        for r in live:
            seq = runs[r]
            last = get(seq[min(len(seq), pos[r] + step) - 1])
            if best < 0 or last < top:
                top, best = last, r
        for r in live:
            if r != best:
                head = get(runs[r][pos[r]])
                if low_run < 0 or head < low:
                    low, low_run = head, r
        seq = runs[best]
        p = pos[best]
        if top < low or (top == low and low_run > best):
            # gallop: best's items below every other head (equal ones too when the runs
            # holding that key come after best) go out unchanged
            j = (bisect_right if low_run > best else bisect_left)(seq, low, p, len(seq), key=get)
            for a in range(p, j, run):
                out.extend(seq[a:min(j, a + run)])
            pos[best] = j
            if j >= len(seq):
                live.remove(best)
        else:
            merged: list[int] = []
            still: list[int] = []
            for r in live:
                seq = runs[r]
                p = pos[r]
                e = min(len(seq), p + step)
                if r < best:      # its block ends above F: every item <= F is in the block
                    j = bisect_right(seq, top, p, e, key=get)
                elif r == best:   # the block ends with F
                    j = e
                else:             # its items equal to F wait for the runs before it
                    j = bisect_left(seq, top, p, e, key=get)
                if j > p:
                    merged += seq[p:j]
                pos[r] = j
                if j < len(seq):
                    still.append(r)
            live = still
            out.extend(sorted(merged, key=get))
        if progress is not None:
            progress.update(n // 2 + len(out) // 2)
    if progress is not None:
        progress.update(n)
    return out


def _done(order: array, progress: Progress | None) -> array:
    """``order``; a progress with a total is complete now (its parts report throttled)."""
    if progress is not None and progress.total:
        progress.update(progress.total)
    return order


def _split_progress(progress: Progress | None, count: int) -> tuple[Progress | None, Progress | None]:
    """(reading the keys, sorting) parts of a node sorting progress with a total; without a
    total the reading reports to ``progress`` itself and the sorting reports nothing."""
    if progress is None or not progress.total:
        return progress, None
    return (progress.child(0.0, WALK_SHARE, total=max(1, count)),
            progress.child(WALK_SHARE, 1.0, total=max(1, count)))


# ==================================================================================
# node source (worker threads)
# ==================================================================================
class NodeSource:
    """Nodes of one document by key path (thread-safe LRU) and the worker-side operations
    of the tree view. Every method may block on I/O: worker threads only."""

    def __init__(self, doc: Any, *, capacity: int = NODE_CACHE) -> None:
        self.doc = doc
        self.kind: str = doc.kind
        base = tuple(doc.info.record_path or ()) if doc.kind == "wrapped" else ()
        self.record_base: tuple = base
        self.record_depth = len(base) + 1     # len(Node.path) of a record node
        self.capacity = max(16, int(capacity))
        self._lock = threading.Lock()
        self._nodes: OrderedDict[tuple, Any] = OrderedDict()
        self.recent_tops: deque[tuple] = deque(maxlen=8192)   # top key paths fetched lately (quick find)

    # -- cache ---------------------------------------------------------------------
    def _get(self, kp: tuple) -> Any | None:
        with self._lock:
            node = self._nodes.get(kp)
            if node is not None:
                self._nodes.move_to_end(kp)
            return node

    def _put(self, kp: tuple, node: Any) -> None:
        with self._lock:
            self._nodes[kp] = node
            self._nodes.move_to_end(kp)
            while len(self._nodes) > self.capacity:
                self._nodes.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._nodes.clear()

    # -- nodes ---------------------------------------------------------------------
    def top_node(self, top: Any) -> Any:
        if top == WRAPPER or top == ROOT:
            return self.doc.root()
        if isinstance(top, bool) or not isinstance(top, int):
            raise KeyError(top)
        return self.doc.record_node(top)

    def node(self, kp: tuple, cancel: CancelToken | None = None) -> Any:
        """The Node of a key path (``IndexError`` for a child index out of range)."""
        kp = tuple(kp)
        node = self._get(kp)
        if node is not None:
            return node
        if len(kp) == 1:
            node = self.top_node(kp[0])
        else:
            check(cancel)
            parent = self.node(kp[:-1], cancel)
            kids = parent.children(kp[-1], kp[-1] + 1)
            if not kids:
                raise IndexError(f"child {kp[-1]} of {parent!r} does not exist")
            node = kids[0]
        self._put(kp, node)
        return node

    def is_records(self, node: Any) -> bool:
        return getattr(node, "role", None) == RECORDS_ROLE

    # -- row data ------------------------------------------------------------------
    def make_info(self, kp: tuple, node: Any, cancel: CancelToken | None = None) -> NodeInfo:
        """The :class:`NodeInfo` of a node (small reads; counts only where cheap)."""
        kind = node.kind
        large = node.size > SMALL_NODE_BYTES or bool(node.is_large)
        info = NodeInfo(keypath=tuple(kp), kind=kind, key=node.key, path=tuple(node.path), start=node.start,
                        end=node.end, large=large)
        if kind in ("object", "array"):
            if self.is_records(node):
                info.records = True
                info.count = int(self.doc.record_count)
                info.text = count_badge(kind, info.count)
                return info
            if not large or node.children_indexed:
                check(cancel)
                info.count = int(node.child_count(cancel=cancel))
            info.text = container_text(kind, info.count, node.scalar_text(SUMMARY_SOURCE_CHARS))
        else:
            info.text = scalar_display(kind, node.scalar_text(VALUE_CHARS))
        return info

    def fetch(self, keys: list, cancel: CancelToken) -> dict[tuple, Any]:
        """The loader's fetch: ``{keypath: NodeInfo | exception}``. Siblings are read in
        contiguous ranges (``parent.children(a, b)``). ``Cancelled`` propagates."""
        out: dict[tuple, Any] = {}
        groups: dict[tuple, list[int]] = {}
        for kp in keys:
            kp = tuple(kp)
            if len(kp) == 1:
                try:
                    check(cancel)
                    node = self.node(kp, cancel)
                    out[kp] = self.make_info(kp, node, cancel)
                    self.recent_tops.append(kp)
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - shown in the row
                    out[kp] = exc
            else:
                groups.setdefault(kp[:-1], []).append(kp[-1])
        for parent_kp, idxs in groups.items():
            check(cancel)
            idxs.sort()
            try:
                parent = self.node(parent_kp, cancel)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                for i in idxs:
                    out[parent_kp + (i,)] = exc
                continue
            for a, b in _runs(idxs, MAX_RUN_GAP):
                wanted = set(idxs[a:b])
                lo, hi = idxs[a], idxs[b - 1] + 1
                try:
                    check(cancel)
                    kids = parent.children(lo, hi)
                    for k, child in enumerate(kids):
                        ckp = parent_kp + (lo + k,)
                        self._put(ckp, child)
                        if lo + k in wanted:
                            out[ckp] = self.make_info(ckp, child, cancel)
                    for i in wanted:
                        out.setdefault(parent_kp + (i,), IndexError(f"child {i} does not exist"))
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    for i in wanted:
                        out.setdefault(parent_kp + (i,), exc)
        return out

    def child_count(self, kp: tuple, progress: Progress | None = None, cancel: CancelToken | None = None) -> int:
        node = self.node(kp, cancel)
        return int(node.child_count(progress=progress, cancel=cancel))

    # -- node sorting --------------------------------------------------------------
    def _walk_children(self, node: Any, count: int, cancel: CancelToken | None,
                       progress: Progress | None) -> Iterator[tuple[int, Any]]:
        for a in range(0, count, CHILD_BATCH):
            check(cancel)
            b = min(count, a + CHILD_BATCH)
            for k, child in enumerate(node.children(a, b)):
                yield a + k, child
            if progress is not None:
                progress.update(b)

    def key_order(self, node: Any, count: int, cancel: CancelToken | None = None,
                  progress: Progress | None = None) -> array:
        """Display order of an object's members by key (``sortkeys.collation_key``, Uzbek
        collation with natural digit runs); equal keys keep the file order. ``progress``
        with a total (e.g. ``count``) gets the reading of the keys as its first
        :data:`WALK_SHARE` and the sorting (:func:`stable_argsort`) as the rest; without a
        total it gets the absolute number of members read."""
        walk, sort = _split_progress(progress, count)
        keys: list[bytes] = [b""] * count
        for i, child in self._walk_children(node, count, cancel, walk):
            keys[i] = collation_key(str(child.key))
        return _done(stable_argsort(keys, cancel, sort), progress)

    def element_order(self, node: Any, count: int, spec: SortKey, cancel: CancelToken | None = None,
                      progress: Progress | None = None) -> array:
        """Display order of an array's elements by the value of ``spec`` in each element
        (``sortkeys`` keys: typed, nulls/missing placed per ``spec``, stable). Field values
        come from ``core.PathExtractor`` on the element bytes (the same keys as
        ``encode_value`` of the parsed values); elements above ``large_node_bytes`` are
        streamed. ``progress``: as for :meth:`key_order`."""
        enc_tok = spec.encode_token
        enc_val = spec.encode_value
        missing = enc_tok(None)
        walk, sort = _split_progress(progress, count)
        keys: list[bytes] = [missing] * count
        if spec.kind != "value":   # "$index": the element index; "$key": missing everywhere
            if spec.kind == "index":
                keys = [enc_val(i) for i in range(count)]
            return _done(stable_argsort(keys, cancel, sort), progress)
        extractor = core.PathExtractor([spec.core_path()])
        doc = self.doc
        large = int(doc.settings.large_node_bytes)
        with doc.open_data() as f:
            window: list[tuple[int, Any]] = []
            win_bytes = 0

            def flush() -> None:
                nonlocal window, win_bytes
                if window:
                    self._element_keys(f, window, extractor, enc_tok, enc_val, spec, keys, cancel)
                window, win_bytes = [], 0

            for i, child in self._walk_children(node, count, cancel, walk):
                if child.size > large:
                    flush()
                    keys[i] = self._huge_key(child, spec, missing, cancel)
                    continue
                if window and (child.start < window[-1][1].end or win_bytes + child.size > READ_WINDOW):
                    flush()
                window.append((i, child))
                win_bytes = child.end - window[0][1].start
            flush()
        return _done(stable_argsort(keys, cancel, sort), progress)

    @staticmethod
    def _element_keys(f: Any, window: list[tuple[int, Any]], extractor: Any, enc_tok: Callable[[Any], bytes],
                      enc_val: Callable[[Any], bytes], spec: SortKey, keys: list[bytes],
                      cancel: CancelToken | None) -> None:
        check(cancel)
        lo = window[0][1].start
        hi = window[-1][1].end
        f.seek(lo)
        buf = f.read(hi - lo)
        spans = array("Q")
        for _i, child in window:
            spans.append(child.start - lo)
            spans.append(child.end - lo)
        try:
            quads = array("Q")
            quads.frombytes(extractor.extract(buf, spans.tobytes()))
            seen: set[int] = set()
            for j in range(0, len(quads), 4):
                k = int(quads[j])
                if k in seen:
                    continue
                seen.add(k)
                keys[window[k][0]] = enc_tok(buf[quads[j + 2]:quads[j + 3]])
        except ValueError:
            for k, (i, child) in enumerate(window):   # one element at a time
                raw = buf[child.start - lo:child.end - lo]
                try:
                    keys[i] = enc_val(spec.extract(codec.loads(raw)))
                except (ValueError, TypeError, JsonReshapeError):
                    keys[i] = enc_tok(None)

    def _huge_key(self, child: Any, spec: SortKey, missing: bytes, cancel: CancelToken | None) -> bytes:
        from jsonreshape.stream import capture_budget, stream_values

        try:
            got = stream_values(self.doc, (child.start, child.end), [spec.pattern], first_only=True,
                                max_bytes=capture_budget(self.doc.settings), cancel=cancel)
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001 - too large / unreadable: sorted as missing
            log.debug("element sort: no key for %r", child, exc_info=True)
            return missing
        values = got.get(0) or []
        return spec.encode_value(values[0] if values else MISSING)

    def field_candidates(self, kp: tuple, cancel: CancelToken | None = None, *, sample: int = 50,
                         limit: int = 300) -> list[str]:
        """Field paths for "sort elements by field": the member keys of the first
        ``sample`` elements (objects up to :data:`SMALL_NODE_BYTES`), ``$`` first when
        there are scalar elements (or nothing else)."""
        node = self.node(kp, cancel)
        n = int(node.child_count(cancel=cancel))
        out: list[str] = []
        seen: set[str] = set()
        scalars = False
        for child in node.children(0, min(n, sample)) if n else []:
            check(cancel)
            if child.kind == "object" and child.size <= SMALL_NODE_BYTES:
                m = int(child.child_count(cancel=cancel))
                for g in child.children(0, min(m, limit)) if m else []:
                    text = format_path((str(g.key),))
                    if text not in seen:
                        seen.add(text)
                        out.append(text)
                        if len(out) >= limit:
                            break
            elif child.kind not in ("object", "array"):
                scalars = True
        if scalars or not out:
            out.insert(0, "$")
        return out

    # -- expand planning -----------------------------------------------------------
    def plan(self, roots: Iterable[tuple], *, levels: int | None, cap: int,
             order_for: Callable[[tuple, Any, int, CancelToken | None], Any] | None = None,
             cancel: CancelToken | None = None, progress: Progress | None = None,
             positions: Callable[[], dict] | None = None) -> ExpandPlan:
        """Nodes to expand below each root key path (breadth-first per root, roots in the
        given order): every container down to ``levels`` levels (``None``: all), with child
        counts (indexing children where needed) and ``order_for(kp, node, count, cancel)``.
        Stops with ``overflow`` once more than ``cap`` rows would be added."""
        result = ExpandPlan()
        for root in roots:
            check(cancel)
            queue: deque[tuple[tuple, int]] = deque([(tuple(root), 0)])
            while queue:
                kp, depth = queue.popleft()
                if levels is not None and depth >= levels:
                    continue
                check(cancel)
                node = self.node(kp, cancel)
                if node.kind not in ("object", "array") or self.is_records(node):
                    continue
                count = int(node.child_count(progress=progress, cancel=cancel))
                if count == 0:
                    continue
                result.rows += count
                if result.rows > cap:
                    result.overflow = True
                    return result
                order = order_for(kp, node, count, cancel) if order_for is not None else None
                result.items.append((kp, count, order, node.kind))
                if levels is not None and depth + 1 >= levels:
                    continue
                for i, child in self._walk_children(node, count, cancel, None):
                    if child.kind in ("object", "array") and not self.is_records(child):
                        ckp = kp + (i,)
                        self._put(ckp, child)
                        queue.append((ckp, depth + 1))
        return result

    def chain(self, kp: tuple, cancel: CancelToken | None = None) -> list[NodeInfo]:
        """Infos of the ancestors of ``kp`` (top first, ``kp`` itself excluded)."""
        out = []
        for d in range(1, len(kp)):
            anc = tuple(kp[:d])
            out.append(self.make_info(anc, self.node(anc, cancel), cancel))
        return out


def _runs(idxs: list[int], gap: int) -> Iterator[tuple[int, int]]:
    """``(a, b)`` slices of the sorted ``idxs`` whose neighbours are at most ``gap`` apart."""
    n = len(idxs)
    a = 0
    for k in range(1, n + 1):
        if k == n or idxs[k] - idxs[k - 1] > gap:
            yield a, k
            a = k
