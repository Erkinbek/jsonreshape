"""Aligned rows of a structural diff for the compare views (Qt-free; docs/ARCHITECTURE.md
§14.9 "Compare"; design: scratchpad ``s5/map_repair_diff.md`` §4.7).

:class:`DiffTreeModel` shows two JSON values side by side as ONE tree (*Kalit | A | B*): every
row is a pair of aligned nodes -- the same member of two objects, two array elements the diff
matched -- or a node that exists on one side only. The rows come from the values and the
:class:`~jsonreshape.diff.ValueDiff` the core computed for them (:func:`~jsonreshape.diff.diff_values`,
:func:`~jsonreshape.diff.diff_nodes`, :func:`~jsonreshape.diff.record_diff`); the model never
decides equality itself, so what it shows is exactly the core's verdict (canonical equality:
``1`` = ``1.0``, ``-0`` = ``0``, exact ``1e400``; ``numbers="text"``; the array matching).

Two kinds of models
-------------------
* **Complete** (both values given and the change list not truncated): every node is shown.
  The children of an aligned pair of containers are rebuilt from the changes: objects pair by
  key (A's members in A order, then the members only in B); arrays pair the elements the
  changes name (``changed`` / ``moved`` changes carry both paths) and pair the remaining
  elements -- the equal ones -- in order between those anchors (every alignment of the core
  is monotonic apart from ``moved`` pairs, so this is exact), except under
  ``array_match="unordered"``, where the remaining elements pair by canonical value, the i-th
  equal one with the i-th (as the core matches them). Elements only in A / only in B
  (``removed`` / ``added``) are placed between their neighbours in unified-diff order; a
  ``moved`` pair is shown at its A position.
* **Changes only** (a value not loaded -- :func:`~jsonreshape.diff.diff_nodes` of big nodes --
  or a truncated change list): only the changed paths are shown (the path trie of the
  changes); "only differences" is forced. Values the changes carry (added / removed subtrees,
  both sides of a type change) can still be expanded.

Rows and expansion
------------------
Rows are flattened lazily by a :class:`~jsonreshape.gui.flattree.FlatTree` (top level: one row,
the compared pair; key paths = display indices of the children). Equal containers are one
collapsed row (``{…} 12 ta maydon, o'zgarishsiz``); containers holding changes are expanded on
creation (breadth first, up to :data:`INITIAL_ROWS` rows). The path trie of the changes marks
the ancestors of every change ("contains changes"); :meth:`DiffTreeModel.next_change` /
:meth:`~DiffTreeModel.prev_change` walk the difference rows in document order (pre-order),
expanding the ancestors of the one found. "Only differences"
(:meth:`~DiffTreeModel.set_only_changes`) hides the equal children: each run of them becomes
one gap row (``… 12 ta o'zgarishsiz``); expansions are remembered by node identity (the
segments from the root), so they survive the toggle.

The children of a pair are laid out when it is expanded: ``O(changes)`` for ordered arrays
(runs of equal pairs), ``O(members)`` for objects, ``O(elements)`` canonical encodings for
unordered arrays -- a big one is laid out in a worker thread (:meth:`~DiffTreeModel.layout_job`).

Threading: a model is built in a worker thread (``__init__`` lays out the initial expansion)
and then belongs to the GUI thread. :meth:`LayoutJob.run` reads only immutable data (the
values, the trie, the options) and may run in a worker while the GUI thread uses the model.
"""

from __future__ import annotations

import difflib
import json
import logging
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from jsonreshape import codec
from jsonreshape.diff import Change, DiffOptions, Omitted, ValueDiff, diff_values
from jsonreshape.dupes import canonical_bytes
from jsonreshape.gui.flattree import FlatTree
from jsonreshape.gui.treemodel import count_badge, display_key, string_display
from jsonreshape.i18n import has, t
from jsonreshape.paths import MISSING, format_path
from jsonreshape.tasks import CancelToken, check

log = logging.getLogger(__name__)

__all__ = [
    "ABSENT",
    "CHANGE_STATUSES",
    "DiffRow",
    "DiffTreeModel",
    "INITIAL_ROWS",
    "LayoutJob",
    "STATUS_ROLES",
    "UNKNOWN",
    "char_spans",
    "count_text",
    "preview_text",
    "value_kind",
]


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


#: A side without a value at this place (the node is only in the other value).
ABSENT: Any = MISSING
#: The value exists but is not loaded (a changes-only model).
UNKNOWN: Any = _Sentinel("UNKNOWN")
#: Segment of a side whose element index is not known (changes-only arrays: the partner of an
#: element with changes inside).
_UNKNOWN_SEG: Any = _Sentinel("?")
#: Segment of a gap row (only-differences mode).
_GAP_SEG: Any = _Sentinel("GAP")

EQUAL, CHANGED, ADDED, REMOVED, MOVED, CONTAINS, GAP = (
    "equal", "changed", "added", "removed", "moved", "contains", "gap")
#: Row statuses that are differences themselves (navigation stops on them).
CHANGE_STATUSES = (CHANGED, ADDED, REMOVED, MOVED)
#: Theme role of the row background per status (rows inside an added / removed subtree too).
STATUS_ROLES = {CHANGED: "diff_changed", ADDED: "diff_added", REMOVED: "diff_removed", MOVED: "diff_moved"}
_SPLIT = "split"              # layout of a type change: A's children removed, B's added

INITIAL_ROWS = 2000           # containers with changes are expanded on creation up to this many rows
EXPAND_ALL_NODES = 5000       # expand_all() expands at most this many containers
EXPAND_ALL_ROWS = 200_000     # ... and adds no container beyond this many rows
SYNC_UNORDERED_MAX = 5000     # unordered arrays with more elements (both sides) are laid out in a worker
VALUE_CHARS = 300             # characters of a scalar shown in a cell
SUMMARY_ITEMS = 6             # members / elements in a container summary
SUMMARY_ITEM_CHARS = 40       # one summary item is cut after this many characters
CHAR_DIFF_MAX = 400           # changed scalars up to this many characters get character highlights
CHAR_DIFF_MIN_RATIO = 0.25    # ... when at least this share of their characters is common
PAIR_CACHE = 50_000           # pairs kept by key path (cleared beyond)
ROW_CACHE = 8192              # rows kept (cleared beyond)
FIELD_SCAN_NODES = 4000       # array_fields(): containers looked at
FIELD_SCAN_ELEMENTS = 200     # ... elements read per array
REVEAL_SCAN_MAX = 200_000     # reveal_paths(): children searched per level
ELLIPSIS = "…"

_RUN, _ITEM, _GAP = 0, 1, 2   # layout piece tags


# ==================================================================================
# rows
# ==================================================================================
@dataclass(slots=True, eq=False)
class DiffRow:
    """What one visible row shows.

    ``status``: ``equal`` | ``changed`` | ``added`` | ``removed`` | ``moved`` | ``contains``
    (differences inside) | ``gap`` (hidden equal siblings); ``point``: the row is a difference
    itself (navigation stops on it; the rows inside an added / removed subtree are tinted like
    it but are not points); ``role``: the theme role of the row background (``None``: none).
    ``a_path`` / ``b_path``: the paths of the two nodes relative to the compared values
    (``None`` for a side without the node or whose index is not known). ``expander``: 0 none,
    1 collapsed, 2 expanded. ``changes``: differences in the row's subtree. ``a_spans`` /
    ``b_spans``: character ranges of a changed scalar that differ. ``summary``: the texts are an
    "unchanged" summary (shown muted); ``gap``: the hidden siblings of a gap row."""

    row: int
    depth: int
    keypath: tuple
    key_label: str
    a_text: str
    b_text: str
    a_kind: str | None
    b_kind: str | None
    status: str
    point: bool
    role: str | None
    a_path: tuple | None
    b_path: tuple | None
    expander: int
    changes: int = 0
    a_spans: tuple = ()
    b_spans: tuple = ()
    summary: bool = False
    gap: int = 0

    @property
    def path(self) -> tuple | None:
        """A's path, else B's."""
        return self.a_path if self.a_path is not None else self.b_path

    @property
    def path_text(self) -> str:
        """The dotted path of the row (``$`` for the compared values themselves)."""
        path = self.path
        if path is None:
            return ""
        return format_path(path) or "$"


# ==================================================================================
# text helpers (pure)
# ==================================================================================
def value_kind(v: Any) -> str | None:
    """``object`` / ``array`` / ``string`` / ``number`` / ``bool`` / ``null``; an
    :class:`~jsonreshape.diff.Omitted` value: its kind; ``None`` for ABSENT / UNKNOWN."""
    if v is ABSENT or v is UNKNOWN:
        return None
    if isinstance(v, Omitted):
        return v.kind
    if isinstance(v, dict):
        return "object"
    if isinstance(v, (list, tuple)):
        return "array"
    if isinstance(v, str):
        return "string"
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    return "number"


def _quote(s: str, limit: int) -> str:
    if len(s) > limit:
        token = json.dumps(s[:limit], ensure_ascii=False)[:-1] + ELLIPSIS
    else:
        token = json.dumps(s, ensure_ascii=False)
    return string_display(token)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + ELLIPSIS


def _scalar_text(v: Any, limit: int = VALUE_CHARS) -> str:
    if isinstance(v, str):
        return _quote(v, limit)
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    try:
        return _cut(codec.number_text(v), limit)
    except TypeError:
        return _cut(repr(v), limit)


def _summary(v: Any) -> str:
    """``{3} id: 1, name: "Ali", …`` / ``[2] 1, 2`` (``{}`` / ``[]`` when empty)."""
    obj = isinstance(v, dict)
    n = len(v)
    if n == 0:
        return "{}" if obj else "[]"
    items: list[str] = []
    seq = iter(v.items()) if obj else ((None, x) for x in v)
    for k, x in seq:
        if len(items) >= SUMMARY_ITEMS:
            break
        if isinstance(x, dict):
            text = "{}" if not x else "{…}"
        elif isinstance(x, (list, tuple)):
            text = "[]" if not x else "[…]"
        else:
            text = _scalar_text(x, SUMMARY_ITEM_CHARS)
        items.append(_cut(f"{display_key(k)}: {text}" if obj else text, SUMMARY_ITEM_CHARS))
    body = ", ".join(items)
    if n > len(items):
        body += ", " + ELLIPSIS
    return f"{count_badge('object' if obj else 'array', n)} {body}"


def preview_text(v: Any, kind: str | None = None) -> str:
    """The cell text of a value: scalars as JSON (strings quoted and escaped, numbers exactly as
    written, cut at :data:`VALUE_CHARS`), containers as a short summary, ``""`` for ABSENT,
    ``{…}`` / ``[…]`` for an UNKNOWN container of ``kind``, the localized size text of an
    :class:`~jsonreshape.diff.Omitted` value."""
    if v is ABSENT:
        return ""
    if v is UNKNOWN:
        return "{…}" if kind == "object" else "[…]" if kind == "array" else ELLIPSIS
    if isinstance(v, Omitted):
        return v.text()
    if isinstance(v, (dict, list, tuple)):
        return _summary(v)
    return _scalar_text(v)


def char_spans(a: str, b: str) -> tuple[tuple, tuple]:
    """Character ranges of ``a`` and ``b`` that differ (``difflib`` on characters): ``(spans of
    a, spans of b)`` of ``(start, end)`` pairs; empty when a text is longer than
    :data:`CHAR_DIFF_MAX` or the texts have little in common (everything would be marked)."""
    if not a or not b or len(a) > CHAR_DIFF_MAX or len(b) > CHAR_DIFF_MAX or a == b:
        return (), ()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    common = sum(block.size for block in sm.get_matching_blocks())
    if common < CHAR_DIFF_MIN_RATIO * min(len(a), len(b)):
        return (), ()
    sa: list[tuple[int, int]] = []
    sb: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            sa.append((i1, i2))
        if tag in ("replace", "insert") and j2 > j1:
            sb.append((j1, j2))
    return tuple(sa), tuple(sb)


def _is_container(v: Any) -> bool:
    return isinstance(v, (dict, list, tuple))


def _child_count(v: Any) -> int:
    return len(v) if isinstance(v, (dict, list, tuple)) else 0


def count_text(key: str, count: int, **params: Any) -> str:
    """``t(key, count=...)``, with the ``<key>.one`` text for a count of 1 when there is one
    (English singulars)."""
    if count == 1 and has(key + ".one"):
        key = key + ".one"
    return t(key, count=count, **params)


def _unchanged_text(v: Any) -> str:
    if isinstance(v, dict):
        return count_text("gui.diff.unchanged.object", len(v))
    if isinstance(v, (list, tuple)):
        return count_text("gui.diff.unchanged.array", len(v))
    return preview_text(v)


def _index(seg: Any) -> int | None:
    return seg if isinstance(seg, int) and not isinstance(seg, bool) else None


# ==================================================================================
# the path trie of the changes
# ==================================================================================
class _Trie:
    """Changes below one path. ``kids``: segment -> trie; ``change``: a ``changed`` /
    ``removed`` / ``added`` change exactly here; ``moved``: a ``moved`` change exactly here;
    ``count``: changes in this subtree; ``bseg``: the B-side segment of this level when a
    change with both paths passes (array elements aligned to another index)."""

    __slots__ = ("kids", "change", "moved", "count", "bseg")

    def __init__(self) -> None:
        self.kids: dict[Any, _Trie] | None = None
        self.change: Change | None = None
        self.moved: Change | None = None
        self.count = 0
        self.bseg: Any = MISSING

    def lone(self, op: str) -> bool:
        """A whole node only on one side (an exact ``removed`` / ``added`` change)."""
        return self.change is not None and self.change.op == op and not self.kids


def _build_tries(changes: list[Change]) -> tuple[_Trie, _Trie]:
    """``(A trie, B trie)``: the changes with an A path (changed, removed, moved) by their A
    path, the ``added`` changes (only a B path) by their B path."""
    ta = _Trie()
    tb = _Trie()
    for c in changes:
        if c.a_path is not None:
            node = ta
            node.count += 1
            bp = c.b_path
            for k, seg in enumerate(c.a_path):
                kids = node.kids
                if kids is None:
                    kids = node.kids = {}
                nxt = kids.get(seg)
                if nxt is None:
                    nxt = kids[seg] = _Trie()
                if bp is not None and nxt.bseg is MISSING and k < len(bp):
                    nxt.bseg = bp[k]
                node = nxt
                node.count += 1
            if c.op == "moved":
                node.moved = c
            elif node.change is None:
                node.change = c
        elif c.b_path is not None:
            node = tb
            node.count += 1
            for seg in c.b_path:
                kids = node.kids
                if kids is None:
                    kids = node.kids = {}
                nxt = kids.get(seg)
                if nxt is None:
                    nxt = kids[seg] = _Trie()
                node = nxt
                node.count += 1
            if node.change is None:
                node.change = c
    return ta, tb


def _kid(trie: _Trie | None, seg: Any) -> _Trie | None:
    if trie is None or seg is None or seg is _UNKNOWN_SEG or seg is _GAP_SEG:
        return None
    kids = trie.kids
    return kids.get(seg) if kids else None


def _trie_kind(trie: _Trie | None) -> str | None:
    if trie is None or not trie.kids:
        return None
    return "array" if _index(next(iter(trie.kids))) is not None else "object"


# ==================================================================================
# pairs
# ==================================================================================
class _Pair:
    """One node of the aligned tree: the values ``a`` / ``b`` (ABSENT / UNKNOWN possible), the
    tries of its changes, ``ident`` (``((aseg, bseg), ...)`` from the root) and ``inherit``
    (``added`` / ``removed``: inside a lone subtree or among the children of a type change)."""

    __slots__ = ("a", "b", "ident", "ta", "tb", "inherit", "akind", "bkind", "gap", "status", "point")

    def __init__(self, a: Any, b: Any, ident: tuple, ta: _Trie | None, tb: _Trie | None, inherit: str | None,
                 akind: str | None = None, bkind: str | None = None, gap: int = 0) -> None:
        self.a = a
        self.b = b
        self.ident = ident
        self.ta = ta
        self.tb = tb
        self.inherit = inherit
        self.akind = akind if akind is not None else value_kind(a)
        self.bkind = bkind if bkind is not None else value_kind(b)
        self.gap = gap
        self.status, self.point = self._status()

    def _status(self) -> tuple[str, bool]:
        if self.gap:
            return GAP, False
        if self.inherit is not None:
            return self.inherit, False
        if self.a is ABSENT:
            return ADDED, True
        if self.b is ABSENT:
            return REMOVED, True
        ta, tb = self.ta, self.tb
        if ta is not None and ta.change is not None and ta.change.op == "changed":
            return CHANGED, True
        if ta is not None and ta.moved is not None:
            return MOVED, True
        if (ta is not None and ta.kids) or (tb is not None and tb.kids):
            return CONTAINS, False
        return EQUAL, False

    @property
    def lone(self) -> bool:
        return self.inherit is not None or self.a is ABSENT or self.b is ABSENT

    @property
    def changes(self) -> int:
        if self.inherit is not None or self.gap:
            return 0
        n = (self.ta.count if self.ta is not None else 0) + (self.tb.count if self.tb is not None else 0)
        return max(n, 1) if self.point else n

    @property
    def nav_children(self) -> bool:
        """Differences below (some children are or hold difference rows)."""
        if self.lone or self.gap:
            return False
        return bool((self.ta is not None and self.ta.kids) or (self.tb is not None and self.tb.kids))

    def paths(self) -> tuple[tuple | None, tuple | None]:
        """``(A path, B path)``; ``None`` for a side without the node or whose index is unknown."""
        a: list[Any] | None = []
        b: list[Any] | None = []
        for aseg, bseg in self.ident:
            if a is not None:
                if aseg is None or aseg is _UNKNOWN_SEG or aseg is _GAP_SEG:
                    a = None
                else:
                    a.append(aseg)
            if b is not None:
                if bseg is None or bseg is _UNKNOWN_SEG or bseg is _GAP_SEG:
                    b = None
                else:
                    b.append(bseg)
        if self.a is ABSENT:
            a = None
        if self.b is ABSENT:
            b = None
        return (tuple(a) if a is not None else None), (tuple(b) if b is not None else None)


# ==================================================================================
# layouts: the children of a pair
# ==================================================================================
class _Layout:
    """The display children of one pair.

    ``pieces``: ``(_RUN, i0, j0, n)`` -- n children ``(i0 + k, j0 + k)`` (a side ``None``: a
    run of lone elements); ``(_ITEM, aseg, bseg)`` -- one child (a side ``None``: lone);
    ``(_GAP, n)`` -- n hidden equal children (only-differences mode). ``starts``: display index
    of each piece's first child; ``nav``: sorted display indices of the children that are or
    hold difference rows; ``inherit``: the status every child inherits (``added`` /
    ``removed`` / ``split``)."""

    __slots__ = ("pieces", "starts", "count", "nav", "inherit", "_items", "_runs", "_runs_i0")

    def __init__(self, pieces: list[tuple], nav: list[int], inherit: str | None = None) -> None:
        self.pieces = pieces
        self.nav = nav
        self.inherit = inherit
        starts: list[int] = []
        pos = 0
        for piece in pieces:
            starts.append(pos)
            tag = piece[0]
            pos += piece[3] if tag == _RUN else 1            # a gap is one row
        self.starts = starts
        self.count = pos
        self._items: dict[tuple, int] | None = None
        self._runs: list[tuple[int, int, Any, Any, int]] = []
        self._runs_i0: list[int] = []

    def spec(self, idx: int) -> tuple[Any, Any, int]:
        """``(aseg, bseg, 0)`` of child ``idx``, or ``(None, None, n)`` for a gap of n children."""
        p = bisect_right(self.starts, idx) - 1
        piece = self.pieces[p]
        tag = piece[0]
        if tag == _ITEM:
            return piece[1], piece[2], 0
        if tag == _GAP:
            return None, None, piece[1]
        off = idx - self.starts[p]
        i0, j0 = piece[1], piece[2]
        return (None if i0 is None else i0 + off), (None if j0 is None else j0 + off), 0

    def index_of(self, aseg: Any, bseg: Any) -> int | None:
        """Display index of the child ``(aseg, bseg)`` (``None``: not shown here)."""
        if self._items is None:
            items: dict[tuple, int] = {}
            runs: list[tuple[int, int, Any, Any, int]] = []
            for p, piece in enumerate(self.pieces):
                if piece[0] == _ITEM:
                    items[(piece[1], piece[2])] = self.starts[p]
                elif piece[0] == _RUN:
                    runs.append((piece[1] if piece[1] is not None else -1, self.starts[p], piece[1], piece[2],
                                 piece[3]))
            runs.sort(key=lambda r: r[0])
            self._items = items
            self._runs = runs
            self._runs_i0 = [r[0] for r in runs]
        try:
            found = self._items.get((aseg, bseg))
        except TypeError:          # an unhashable segment cannot be a child
            return None
        if found is not None:
            return found
        ai, bj = _index(aseg), _index(bseg)
        if ai is not None:
            k = bisect_right(self._runs_i0, ai) - 1
            if k >= 0:
                _key, start, i0, j0, n = self._runs[k]
                if i0 is not None and i0 <= ai < i0 + n:
                    off = ai - i0
                    if (j0 is None and bseg is None) or (j0 is not None and j0 + off == bj):
                        return start + off
            return None
        if aseg is None and bj is not None:
            for _key, start, i0, j0, n in self._runs:
                if i0 is None and j0 is not None and j0 <= bj < j0 + n:
                    return start + (bj - j0)
        return None


def _complement_runs(n: int, excluded: list[int]) -> list[tuple[int, int]]:
    """``(start, length)`` runs of ``range(n)`` without the sorted, unique ``excluded``."""
    out: list[tuple[int, int]] = []
    prev = 0
    for x in excluded:
        if x < prev or x >= n:
            continue
        if x > prev:
            out.append((prev, x - prev))
        prev = x + 1
    if prev < n:
        out.append((prev, n - prev))
    return out


def _runs_in(runs: list[tuple[int, int]], lo: int, hi: int) -> deque:
    """The parts of the sorted ``runs`` inside ``[lo, hi)``."""
    out: deque = deque()
    for s, n in runs[max(0, bisect_right(runs, (lo, 1 << 62)) - 1):]:
        e = s + n
        if e <= lo:
            continue
        if s >= hi:
            break
        a, b = max(s, lo), min(e, hi)
        if b > a:
            out.append((a, b - a))
    return out


def _zip_runs(ra: deque, rb: deque) -> tuple[list[tuple[int, int, int]], deque, deque]:
    """Pair the indices of two run queues in order: ``(pair runs (i0, j0, n), A left, B left)``."""
    pairs: list[tuple[int, int, int]] = []
    while ra and rb:
        (sa, na), (sb, nb) = ra[0], rb[0]
        n = min(na, nb)
        pairs.append((sa, sb, n))
        if na == n:
            ra.popleft()
        else:
            ra[0] = (sa + n, na - n)
        if nb == n:
            rb.popleft()
        else:
            rb[0] = (sb + n, nb - n)
    return pairs, ra, rb


def _split_runs(pairs: list[tuple[int, int, int]], touched_a: list[int], touched_b: list[int]) -> Iterator[tuple]:
    """Pair runs as pieces; the pairs touching a change (sorted index lists) become single items."""
    for i0, j0, n in pairs:
        cuts: list[int] = []
        if touched_a or touched_b:
            ka = touched_a[bisect_left(touched_a, i0):bisect_left(touched_a, i0 + n)]
            kb = touched_b[bisect_left(touched_b, j0):bisect_left(touched_b, j0 + n)]
            if ka or kb:
                cuts = sorted({i - i0 for i in ka} | {j - j0 for j in kb})
        start = 0
        for k in cuts:
            if k > start:
                yield (_RUN, i0 + start, j0 + start, k - start)
            yield (_ITEM, i0 + k, j0 + k)
            start = k + 1
        if n > start:
            yield (_RUN, i0 + start, j0 + start, n - start)


def _lone_pieces(indices: list[int], a_side: bool) -> list[tuple]:
    """Sorted lone indices as runs."""
    out: list[tuple] = []
    k = 0
    while k < len(indices):
        e = k
        while e + 1 < len(indices) and indices[e + 1] == indices[e] + 1:
            e += 1
        s, n = indices[k], e - k + 1
        out.append((_RUN, s, None, n) if a_side else (_RUN, None, s, n))
        k = e + 1
    return out


def _array_pieces_ordered(n: int, m: int, ta: _Trie | None, tb: _Trie | None) -> list[tuple]:
    """Children of two aligned arrays whose alignment is monotonic apart from ``moved`` pairs
    (``auto`` / ``index`` / key matching), in unified-diff order."""
    ta_k = (ta.kids if ta is not None else None) or {}
    tb_k = (tb.kids if tb is not None else None) or {}
    removed: list[int] = []
    moved: dict[int, int] = {}
    known: dict[int, int] = {}
    for seg, kid in ta_k.items():
        i = _index(seg)
        if i is None or i >= n:
            continue
        if kid.lone("removed"):
            removed.append(i)
        elif kid.moved is not None and _index(kid.bseg) is not None:
            moved[i] = kid.bseg
        elif _index(kid.bseg) is not None:
            known[i] = kid.bseg
    added = sorted(j for seg, kid in tb_k.items() if (j := _index(seg)) is not None and j < m and kid.lone("added"))
    removed.sort()
    # anchors: the named pairs that are monotonic (any other is placed like a move)
    anchors: list[tuple[int, int]] = []
    last_j = -1
    for i in sorted(known):
        j = known[i]
        if j > last_j and j < m:
            anchors.append((i, j))
            last_j = j
        elif j < m:
            moved[i] = j
    excl_a = sorted(set(removed) | set(moved) | {i for i, _ in anchors})
    excl_b = sorted(set(added) | set(moved.values()) | {j for _, j in anchors})
    rest_a = _complement_runs(n, excl_a)
    rest_b = _complement_runs(m, excl_b)
    touched_a = sorted(i for seg in ta_k if (i := _index(seg)) is not None)
    touched_b = sorted(j for seg in tb_k if (j := _index(seg)) is not None)
    pair_pieces: list[tuple] = []        # paired children, monotonic in both indices
    extra_a: list[int] = []
    extra_b: list[int] = []
    lo_a = lo_b = 0
    for i, j in anchors + [(n, m)]:
        pairs, left_a, left_b = _zip_runs(_runs_in(rest_a, lo_a, i), _runs_in(rest_b, lo_b, j))
        pair_pieces.extend(_split_runs(pairs, touched_a, touched_b))
        for s, cnt in left_a:
            extra_a.extend(range(s, s + cnt))
        for s, cnt in left_b:
            extra_b.extend(range(s, s + cnt))
        if i < n and j < m:
            pair_pieces.append((_ITEM, i, j))
        lo_a, lo_b = i + 1, j + 1
    if extra_a or extra_b:
        log.debug("diff tree: %d / %d elements without a partner (alignment not reproduced)", len(extra_a),
                  len(extra_b))
    a_singles = sorted(set(removed) | set(moved) | set(extra_a))
    b_singles = sorted(set(added) | set(extra_b))
    out: list[tuple] = []
    pos = [0, 0]

    def flush_a(limit: int) -> None:
        lone: list[int] = []
        k = pos[0]
        while k < len(a_singles) and a_singles[k] < limit:
            i = a_singles[k]
            if i in moved:
                out.extend(_lone_pieces(lone, True))
                lone = []
                out.append((_ITEM, i, moved[i]))
            else:
                lone.append(i)
            k += 1
        out.extend(_lone_pieces(lone, True))
        pos[0] = k

    def flush_b(limit: int) -> None:
        k = start = pos[1]
        while k < len(b_singles) and b_singles[k] < limit:
            k += 1
        out.extend(_lone_pieces(b_singles[start:k], False))
        pos[1] = k

    for piece in pair_pieces:
        flush_a(piece[1])
        flush_b(piece[2])
        out.append(piece)
    flush_a(n + 1)
    flush_b(m + 1)
    return out


def _token(v: Any) -> bytes:
    try:
        return canonical_bytes(v)
    except (TypeError, ValueError):     # pragma: no cover - parsed JSON values
        return repr(v).encode("utf-8", "surrogatepass")


def _array_pieces_unordered(a: list, b: list, ta: _Trie | None, tb: _Trie | None,
                            cancel: CancelToken | None = None) -> list[tuple]:
    """Children of two arrays compared without order: the elements pair by canonical value,
    the i-th equal one with the i-th (the pairs the changes name -- number texts -- first);
    A order, then the elements only in B."""
    n, m = len(a), len(b)
    ta_k = (ta.kids if ta is not None else None) or {}
    tb_k = (tb.kids if tb is not None else None) or {}
    removed = {i for seg, kid in ta_k.items() if (i := _index(seg)) is not None and kid.lone("removed")}
    added = {j for seg, kid in tb_k.items() if (j := _index(seg)) is not None and kid.lone("added")}
    pair_of: dict[int, int] = {}
    for seg, kid in ta_k.items():
        i = _index(seg)
        if i is not None and i not in removed and _index(kid.bseg) is not None and kid.bseg < m:
            pair_of[i] = kid.bseg
    used_b = set(pair_of.values()) | added
    groups: dict[bytes, deque] = {}
    for j in range(m):
        if j % 1024 == 0:
            check(cancel)
        if j not in used_b:
            groups.setdefault(_token(b[j]), deque()).append(j)
    for i in range(n):
        if i % 1024 == 0:
            check(cancel)
        if i in removed or i in pair_of:
            continue
        dq = groups.get(_token(a[i]))
        if dq:
            pair_of[i] = dq.popleft()
    left_b = sorted(j for dq in groups.values() for j in dq)
    if left_b:
        log.debug("diff tree: %d elements of B without a partner (unordered)", len(left_b))
    out: list[tuple] = []
    run: list[int] = []            # an open run of untouched pairs: [i0, j0, n]
    lone: list[int] = []

    def close_run() -> None:
        if run:
            out.append((_RUN, run[0], run[1], run[2]))
            run.clear()

    for i in range(n):
        j = pair_of.get(i)
        if j is None:
            close_run()
            lone.append(i)
            continue
        if lone:
            out.extend(_lone_pieces(lone, True))
            lone = []
        if i in ta_k or j in tb_k:
            close_run()
            out.append((_ITEM, i, j))
        elif run and run[0] + run[2] == i and run[1] + run[2] == j:
            run[2] += 1
        else:
            close_run()
            run.extend((i, j, 1))
    close_run()
    out.extend(_lone_pieces(lone, True))
    out.extend(_lone_pieces(sorted(set(left_b) | added), False))
    return out


def _object_pieces(a: dict, b: dict) -> list[tuple]:
    out: list[tuple] = [(_ITEM, k, k if k in b else None) for k in a]
    out.extend((_ITEM, None, k) for k in b if k not in a)
    return out


def _changes_only_pieces(ta: _Trie | None, tb: _Trie | None, kind: str | None) -> list[tuple]:
    """Children of a pair whose values are not shown in full: the changed paths only."""
    ta_k = (ta.kids if ta is not None else None) or {}
    tb_k = (tb.kids if tb is not None else None) or {}
    if kind is None:
        kind = _trie_kind(ta) or _trie_kind(tb)
    if kind != "array":
        out: list[tuple] = []
        for k, kid in ta_k.items():
            out.append((_ITEM, k, None if kid.lone("removed") and k not in tb_k else k))
        for k, kid in tb_k.items():
            if k not in ta_k:
                out.append((_ITEM, None if kid.lone("added") else k, k))
        return out
    a_items: list[tuple[int, Any]] = []
    linked_b: set = set()
    for seg, kid in ta_k.items():
        i = _index(seg)
        if i is None:
            continue
        if kid.lone("removed"):
            a_items.append((i, None))
        elif _index(kid.bseg) is not None:
            a_items.append((i, kid.bseg))
            linked_b.add(kid.bseg)
        else:
            a_items.append((i, _UNKNOWN_SEG))
    a_items.sort(key=lambda x: x[0])
    b_items: list[tuple[Any, int]] = []
    for seg, kid in tb_k.items():
        j = _index(seg)
        if j is None or j in linked_b:
            continue
        b_items.append((None if kid.lone("added") else _UNKNOWN_SEG, j))
    b_items.sort(key=lambda x: x[1])
    out = []
    pb = 0
    for i, j in a_items:
        if _index(j) is not None:
            while pb < len(b_items) and b_items[pb][1] < j:
                out.append((_ITEM, b_items[pb][0], b_items[pb][1]))
                pb += 1
        out.append((_ITEM, i, j))
    out.extend((_ITEM, aseg, j) for aseg, j in b_items[pb:])
    return out


def _touches(aseg: Any, bseg: Any, ta_k: dict, tb_k: dict) -> bool:
    if aseg is None or bseg is None or aseg is _UNKNOWN_SEG or bseg is _UNKNOWN_SEG:
        return True
    return aseg in ta_k or bseg in tb_k


def _nav_of(pieces: list[tuple], ta: _Trie | None, tb: _Trie | None) -> list[int]:
    """Display indices of the children that are or hold difference rows (lone children and the
    pairs the changes name)."""
    ta_k = (ta.kids if ta is not None else None) or {}
    tb_k = (tb.kids if tb is not None else None) or {}
    nav: list[int] = []
    pos = 0
    for piece in pieces:
        tag = piece[0]
        if tag == _ITEM:
            if _touches(piece[1], piece[2], ta_k, tb_k):
                nav.append(pos)
            pos += 1
        elif tag == _RUN:
            cnt = piece[3]
            if piece[1] is None or piece[2] is None:      # a lone run: every element is a difference
                nav.extend(range(pos, pos + cnt))
            pos += cnt
        else:
            pos += 1                                       # a gap row
    return nav


def _only_changes(pieces: list[tuple], ta: _Trie | None, tb: _Trie | None) -> list[tuple]:
    """``pieces`` with every run of equal children replaced by one gap."""
    ta_k = (ta.kids if ta is not None else None) or {}
    tb_k = (tb.kids if tb is not None else None) or {}
    out: list[tuple] = []
    hidden = 0
    for piece in pieces:
        tag = piece[0]
        if tag == _GAP:
            hidden += piece[1]
            continue
        if tag == _RUN and piece[1] is not None and piece[2] is not None:
            hidden += piece[3]
            continue
        if tag == _ITEM and not _touches(piece[1], piece[2], ta_k, tb_k):
            hidden += 1
            continue
        if hidden:
            out.append((_GAP, hidden))
            hidden = 0
        out.append(piece)
    if hidden:
        out.append((_GAP, hidden))
    return out


# ==================================================================================
# layout jobs (worker threads)
# ==================================================================================
class LayoutJob:
    """The layout of one expensive pair computed in a worker thread
    (:meth:`DiffTreeModel.layout_job`): :meth:`run` there, then :meth:`DiffTreeModel.apply_job`
    in the GUI thread."""

    __slots__ = ("ident", "epoch", "pieces", "_fn")

    def __init__(self, ident: tuple, epoch: int, fn: Callable[[CancelToken | None], list[tuple]]) -> None:
        self.ident = ident
        self.epoch = epoch
        self.pieces: list[tuple] | None = None
        self._fn: Callable[[CancelToken | None], list[tuple]] | None = fn

    def run(self, cancel: CancelToken | None = None) -> LayoutJob:
        fn, self._fn = self._fn, None
        if fn is not None:
            self.pieces = fn(cancel)
        return self


# ==================================================================================
# the model
# ==================================================================================
class DiffTreeModel:
    """Rows of the side-by-side tree of one value pair (see the module docstring).

    ``a`` / ``b``: the compared values (``codec`` values) -- :data:`UNKNOWN` when a value is not
    loaded (big nodes), :data:`ABSENT` for a side without a value (a record only in one file);
    ``vd``: their :class:`~jsonreshape.diff.ValueDiff` (``None``: computed here with
    :func:`~jsonreshape.diff.diff_values`, ``opts`` and no limit on the changes -- small values,
    tests); ``opts``: the options of that diff (the array matching decides how equal elements
    pair); ``skip``: leading path segments the array patterns of ``opts.array_keys`` do not see
    (a member record compared as ``{key: value}``); ``a_kind`` / ``b_kind``: the kinds of values
    that are not loaded; ``root_label``: the key column of the top row; ``complete=False``
    forces a changes-only model; ``initial_rows``: the expansion on creation stops there."""

    def __init__(self, a: Any = UNKNOWN, b: Any = UNKNOWN, vd: ValueDiff | None = None, *,
                 opts: DiffOptions | None = None, skip: int = 0, a_kind: str | None = None,
                 b_kind: str | None = None, root_label: str = "$", complete: bool | None = None,
                 initial_rows: int = INITIAL_ROWS) -> None:
        self.opts = opts if opts is not None else DiffOptions()
        if vd is None:
            if a is UNKNOWN or b is UNKNOWN or a is ABSENT or b is ABSENT:
                raise ValueError("a ValueDiff is needed when a value is not given")
            vd = diff_values(a, b, DiffOptions(array_match=self.opts.array_match,
                                               array_keys=tuple(self.opts.array_keys),
                                               numbers=self.opts.numbers, max_changes=None))
        self.vd = vd
        self.skip = max(0, int(skip))
        self.root_label = root_label or "$"
        values = a is not UNKNOWN and b is not UNKNOWN
        self.complete = bool(values and not vd.truncated and complete is not False)
        self._rules = self.opts.key_rules()
        self._unordered_default = self.opts.array_match == "unordered"
        self._ta, self._tb = _build_tries(vd.changes)
        self._root = _Pair(a, b, (), self._ta, self._tb, None, akind=a_kind if a is UNKNOWN else None,
                           bkind=b_kind if b is UNKNOWN else None)
        self._only = not self.complete
        self._epoch = 0
        #: increases whenever the rows change (expand, collapse, mode): views reset their rows
        self.version = 0
        self._layouts: dict[tuple, _Layout] = {}      # (ident, only) -> layout
        self._pairs: dict[tuple, _Pair] = {}          # key path -> pair (current FlatTree)
        self._rows: dict[int, DiffRow] = {}
        self._expanded: set[tuple] = set()            # idents of the expanded pairs
        self._fields_cache: list[str] | None = None
        self._flat = FlatTree(1)
        self._expand_kp((0,), self._root)
        self._expand_changes(max_rows=initial_rows, max_nodes=EXPAND_ALL_NODES, lone=False)

    # ------------------------------------------------------------------ facts
    @property
    def counts(self) -> dict[str, int]:
        """Every difference by op (complete also when the stored list was truncated)."""
        return dict(self.vd.counts)

    @property
    def total_changes(self) -> int:
        return self.vd.total

    @property
    def truncated(self) -> bool:
        return self.vd.truncated

    @property
    def equal(self) -> bool:
        return self.vd.equal

    @property
    def only_changes(self) -> bool:
        return self._only

    @property
    def can_show_all(self) -> bool:
        """Equal rows can be shown (a complete model)."""
        return self.complete

    @property
    def a_value(self) -> Any:
        return self._root.a

    @property
    def b_value(self) -> Any:
        return self._root.b

    def row_count(self) -> int:
        return self._flat.row_count()

    def describe(self) -> str:
        """Localized counts (``Farq yo'q`` when equal)."""
        c = self.vd.counts
        if self.vd.equal:
            return t("gui.diff.summary.equal")
        return t("gui.diff.summary.counts", changed=c.get("changed", 0), added=c.get("added", 0),
                 removed=c.get("removed", 0), moved=c.get("moved", 0))

    # ------------------------------------------------------------------ children of a pair
    def _is_unordered(self, a_path: tuple | None) -> bool:
        if not self._unordered_default:
            return False
        if self._rules and a_path is not None:
            rel = a_path[self.skip:]
            for pat, _fpath in self._rules:
                if pat is None or pat.matches(rel):
                    return False
        return True

    @staticmethod
    def _lone_children(v: Any, a_side: bool) -> list[tuple]:
        if isinstance(v, dict):
            return [(_ITEM, k, None) if a_side else (_ITEM, None, k) for k in v]
        n = _child_count(v)
        if n == 0:
            return []
        return [(_RUN, 0, None, n) if a_side else (_RUN, None, 0, n)]

    def _type_change(self, pair: _Pair) -> bool:
        return (pair.status == CHANGED and pair.a is not UNKNOWN and pair.b is not UNKNOWN
                and (_is_container(pair.a) or _is_container(pair.b)))

    def _pieces(self, pair: _Pair, cancel: CancelToken | None = None) -> list[tuple]:
        """All children of a container pair (before the only-differences filter)."""
        a, b = pair.a, pair.b
        if pair.lone:
            if a is not ABSENT and _is_container(a):
                return self._lone_children(a, True)
            if b is not ABSENT and _is_container(b):
                return self._lone_children(b, False)
            return []
        if self._type_change(pair):
            out: list[tuple] = []
            if _is_container(a):
                out.extend(self._lone_children(a, True))
            if _is_container(b):
                out.extend(self._lone_children(b, False))
            return out
        if a is UNKNOWN or b is UNKNOWN or not self.complete:
            kind = pair.akind if pair.akind in ("object", "array") else pair.bkind
            return _changes_only_pieces(pair.ta, pair.tb, kind)
        if isinstance(a, dict) and isinstance(b, dict):
            return _object_pieces(a, b)
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if self._is_unordered(pair.paths()[0]):
                return _array_pieces_unordered(a, b, pair.ta, pair.tb, cancel)
            return _array_pieces_ordered(len(a), len(b), pair.ta, pair.tb)
        return []

    def _can_expand(self, pair: _Pair) -> bool:
        if pair.gap:
            return False
        a, b = pair.a, pair.b
        if pair.lone:
            return _child_count(a if a is not ABSENT else b) > 0
        if self._type_change(pair):
            return _child_count(a) > 0 or _child_count(b) > 0
        if a is UNKNOWN or b is UNKNOWN or not self.complete:
            return pair.nav_children
        return _child_count(a) > 0 or _child_count(b) > 0

    def _expensive(self, pair: _Pair) -> bool:
        """Laying out the children of ``pair`` needs a worker (a big unordered array)."""
        a, b = pair.a, pair.b
        if pair.lone or not self.complete or not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
            return False
        if len(a) + len(b) <= SYNC_UNORDERED_MAX:
            return False
        return self._is_unordered(pair.paths()[0])

    def _make_layout(self, pair: _Pair, pieces: list[tuple], only: bool | None = None) -> _Layout:
        if pair.lone:
            inherit = pair.inherit or (ADDED if pair.a is ABSENT else REMOVED)
            return _Layout(pieces, [], inherit)
        if self._type_change(pair):
            return _Layout(pieces, [], _SPLIT)
        if self._only if only is None else only:
            pieces = _only_changes(pieces, pair.ta, pair.tb)
        return _Layout(pieces, _nav_of(pieces, pair.ta, pair.tb))

    def _has_layout(self, pair: _Pair) -> bool:
        return (pair.ident, self._only) in self._layouts

    def _layout(self, pair: _Pair) -> _Layout:
        key = (pair.ident, self._only)
        lay = self._layouts.get(key)
        if lay is None:
            lay = self._layouts[key] = self._make_layout(pair, self._pieces(pair))
        return lay

    @staticmethod
    def _child_value(parent: Any, seg: Any) -> Any:
        if seg is None:
            return ABSENT
        if parent is UNKNOWN or parent is ABSENT or seg is _UNKNOWN_SEG:
            return UNKNOWN
        if isinstance(parent, dict):
            return parent.get(seg, UNKNOWN) if isinstance(seg, str) else UNKNOWN
        i = _index(seg)
        if isinstance(parent, (list, tuple)) and i is not None:
            return parent[i] if 0 <= i < len(parent) else UNKNOWN
        return UNKNOWN

    def _child(self, parent: _Pair, idx: int) -> _Pair:
        lay = self._layout(parent)
        aseg, bseg, gap = lay.spec(idx)
        if gap:
            return _Pair(ABSENT, ABSENT, parent.ident + ((_GAP_SEG, idx),), None, None, None, gap=gap)
        inherit = lay.inherit
        if inherit == _SPLIT:
            inherit = REMOVED if bseg is None else ADDED
        a = self._child_value(parent.a, aseg)
        b = self._child_value(parent.b, bseg)
        if inherit is not None:
            return _Pair(a, b, parent.ident + ((aseg, bseg),), None, None, inherit)
        ta = _kid(parent.ta, aseg)
        tb = _kid(parent.tb, bseg)
        if a is UNKNOWN or b is UNKNOWN:
            ch = ta.change if ta is not None else None
            if ch is None and tb is not None:
                ch = tb.change
            if ch is not None:
                if a is UNKNOWN and ch.a is not MISSING:
                    a = ch.a
                if b is UNKNOWN and ch.b is not MISSING:
                    b = ch.b
        akind = bkind = None
        if a is UNKNOWN or b is UNKNOWN:
            kind = _trie_kind(ta) or _trie_kind(tb)
            if a is UNKNOWN:
                akind = kind or value_kind(b) if b is not ABSENT else kind
            if b is UNKNOWN:
                bkind = kind or value_kind(a) if a is not ABSENT else kind
        return _Pair(a, b, parent.ident + ((aseg, bseg),), ta, tb, None, akind=akind, bkind=bkind)

    def _pair(self, kp: tuple) -> _Pair:
        if len(kp) <= 1:
            return self._root
        pair = self._pairs.get(kp)
        if pair is not None:
            return pair
        pair = self._child(self._pair(kp[:-1]), kp[-1])
        if len(self._pairs) >= PAIR_CACHE:
            self._pairs.clear()
        self._pairs[kp] = pair
        return pair

    # ------------------------------------------------------------------ expansion
    def _changed(self) -> None:
        self._rows.clear()
        self.version += 1

    def _expand_kp(self, kp: tuple, pair: _Pair) -> int:
        if not self._can_expand(pair):
            return 0
        lay = self._layout(pair)
        self._expanded.add(pair.ident)
        if self._flat.is_expanded(kp) and self._flat.child_count(kp) == lay.count:
            return 0
        return self._flat.expand(kp, lay.count)

    def _kp(self, row: int) -> tuple:
        return self._flat.locate(row).keypath

    def keypath(self, row: int) -> tuple:
        """The key path of ``row`` (stable while the rows above it do not change)."""
        return self._kp(row)

    def row_of_keypath(self, kp: tuple) -> int | None:
        """The row of a key path (``None``: not shown)."""
        try:
            return self._flat.row_of(kp)
        except (IndexError, ValueError, TypeError):
            return None

    def ident(self, row: int) -> tuple:
        """The identity of the node at ``row`` (its ``(A segment, B segment)`` pairs from the
        top): stable across rebuilds (only-differences toggle)."""
        return self._pair(self._kp(row)).ident

    def row_of_ident(self, ident: tuple) -> int | None:
        """The row of a node identity in the current rows (``None``: hidden or collapsed away)."""
        kp = self._keypath_of(ident)
        return None if kp is None else self.row_of_keypath(kp)

    def is_expanded(self, row: int) -> bool:
        return self._flat.is_expanded(self._kp(row))

    def can_expand(self, row: int) -> bool:
        return self._can_expand(self._pair(self._kp(row)))

    def expand(self, row: int) -> int:
        """Expand the container at ``row``: the rows added (0: nothing to expand or already
        expanded; -1: its layout is expensive -- run :meth:`layout_job` in a worker)."""
        kp = self._kp(row)
        pair = self._pair(kp)
        if not self._can_expand(pair):
            return 0
        if not self._has_layout(pair) and self._expensive(pair):
            return -1
        added = self._expand_kp(kp, pair)
        self._changed()
        return added

    def collapse(self, row: int) -> int:
        """Collapse the row (its expanded descendants are remembered); the rows removed."""
        kp = self._kp(row)
        if not self._flat.is_expanded(kp):
            return 0
        removed = self._flat.collapse(kp)
        self._expanded.discard(self._pair(kp).ident)
        self._changed()
        return removed

    def toggle(self, row: int) -> int:
        """Collapse an expanded row (negative result) or expand a collapsed one (as :meth:`expand`)."""
        if self.is_expanded(row):
            return -self.collapse(row)
        return self.expand(row)

    def layout_job(self, row: int) -> LayoutJob | None:
        """A job laying out the expensive children of ``row`` in a worker (``None``: not needed)."""
        pair = self._pair(self._kp(row))
        if not self._can_expand(pair) or self._has_layout(pair) or not self._expensive(pair):
            return None
        return LayoutJob(pair.ident, self._epoch, lambda cancel, p=pair: self._pieces(p, cancel))

    def apply_job(self, job: LayoutJob) -> int | None:
        """Install the layout of a finished :class:`LayoutJob` and expand its row; the row
        (``None``: the model was rebuilt meanwhile or the node is not shown)."""
        if job.epoch != self._epoch or job.pieces is None:
            return None
        kp = self._keypath_of(job.ident)
        if kp is None:
            return None
        pair = self._pair(kp)
        for only in (False, True):
            self._layouts.setdefault((pair.ident, only), self._make_layout(pair, job.pieces, only))
        self._expand_kp(kp, pair)
        self._changed()
        return self._flat.row_of(kp)

    def _expand_changes(self, *, max_rows: int, max_nodes: int, lone: bool) -> bool:
        """Expand the containers holding differences, breadth first, while the rows stay below
        ``max_rows`` (``lone``: also added / removed / type-changed containers, one level);
        False when a limit or an expensive layout stopped it."""
        flat = self._flat
        queue: deque = deque([((0,), self._root)])
        nodes = 0
        done = True
        while queue:
            kp, pair = queue.popleft()
            if not self._can_expand(pair):
                continue
            if not (pair.nav_children or (lone and pair.point) or len(kp) == 1):
                continue
            if not self._has_layout(pair) and self._expensive(pair):
                done = False
                continue
            if len(kp) > 1 and (nodes >= max_nodes or flat.row_count() >= max_rows):
                done = False
                break
            self._expand_kp(kp, pair)
            nodes += 1
            if pair.nav_children:
                for idx in self._layout(pair).nav:
                    child_kp = kp + (idx,)
                    queue.append((child_kp, self._pair(child_kp)))
        return done

    def expand_all(self, *, max_nodes: int = EXPAND_ALL_NODES, max_rows: int = EXPAND_ALL_ROWS) -> bool:
        """Expand every container holding differences (and the added / removed / type-changed
        containers, one level); False when a limit or an expensive layout stopped it."""
        done = self._expand_changes(max_rows=max_rows, max_nodes=max_nodes, lone=True)
        self._changed()
        return done

    def collapse_all(self) -> None:
        """Collapse everything but the top row."""
        self._flat.collapse_all()
        self._expanded.clear()
        self._expand_kp((0,), self._root)
        self._changed()

    def set_only_changes(self, on: bool) -> bool:
        """Show only the rows with differences (each run of equal siblings becomes a gap row);
        a changes-only model stays on. Expansions are kept. True when the mode changed."""
        on = bool(on) or not self.complete
        if on == self._only:
            return False
        self._only = on
        self._rebuild()
        return True

    def _rebuild(self) -> None:
        """A new FlatTree for the current mode, with the remembered expansions."""
        expanded = sorted(self._expanded, key=len)
        self._epoch += 1
        self._pairs.clear()
        self._flat = FlatTree(1)
        self._expanded = set()
        self._expand_kp((0,), self._root)
        for ident in expanded:
            if not ident:
                continue
            kp = self._keypath_of(ident)
            if kp is None:
                continue
            pair = self._pair(kp)
            if self._can_expand(pair) and (self._has_layout(pair) or not self._expensive(pair)):
                self._expand_kp(kp, pair)
        self._changed()

    def _keypath_of(self, ident: tuple) -> tuple | None:
        """The key path of the pair ``ident`` in the current layouts (``None``: not shown)."""
        kp: tuple = (0,)
        pair = self._root
        for aseg, bseg in ident:
            if not self._can_expand(pair):
                return None
            if not self._has_layout(pair) and self._expensive(pair):
                return None
            idx = self._layout(pair).index_of(aseg, bseg)
            if idx is None:
                return None
            kp = kp + (idx,)
            pair = self._pair(kp)
        return kp

    # ------------------------------------------------------------------ rows
    def row(self, r: int) -> DiffRow:
        """Row ``r`` (``IndexError`` outside ``0 .. row_count() - 1``)."""
        cached = self._rows.get(r)
        if cached is not None:
            return cached
        info = self._flat.locate(r)
        row = self._make_row(r, info.depth, info.keypath, self._pair(info.keypath))
        if len(self._rows) >= ROW_CACHE:
            self._rows.clear()
        self._rows[r] = row
        return row

    def iter_rows(self, start: int = 0, stop: int | None = None) -> Iterator[DiffRow]:
        """Rows ``start .. stop - 1``."""
        stop = self.row_count() if stop is None else min(stop, self.row_count())
        for r in range(max(0, start), stop):
            yield self.row(r)

    def _key_label(self, kp: tuple, pair: _Pair) -> str:
        if len(kp) <= 1:
            return self.root_label
        if pair.gap:
            return count_text("gui.diff.gap", pair.gap)
        aseg, bseg = pair.ident[-1]
        for seg in (aseg, bseg):
            if isinstance(seg, str):
                return display_key(seg)
        ai, bj = _index(aseg), _index(bseg)
        a_txt = str(ai) if ai is not None else "?" if aseg is _UNKNOWN_SEG else None
        b_txt = str(bj) if bj is not None else "?" if bseg is _UNKNOWN_SEG else None
        if a_txt is None:
            return f"[{b_txt}]"
        if b_txt is None or a_txt == b_txt:
            return f"[{a_txt}]"
        return f"[{a_txt} → {b_txt}]"

    def _make_row(self, r: int, depth: int, kp: tuple, pair: _Pair) -> DiffRow:
        label = self._key_label(kp, pair)
        if pair.gap:
            return DiffRow(r, depth, kp, label, "", "", None, None, GAP, False, None, None, None, 0,
                           summary=True, gap=pair.gap)
        a_path, b_path = pair.paths()
        status = pair.status
        expander = (2 if self._flat.is_expanded(kp) else 1) if self._can_expand(pair) else 0
        a_text = preview_text(pair.a, pair.akind)
        b_text = preview_text(pair.b, pair.bkind)
        summary = False
        if status == EQUAL and (_is_container(pair.a) or _is_container(pair.b)):
            a_text, b_text, summary = _unchanged_text(pair.a), _unchanged_text(pair.b), True
        a_spans: tuple = ()
        b_spans: tuple = ()
        if status == CHANGED and pair.akind == pair.bkind and pair.akind in ("string", "number") \
                and not isinstance(pair.a, Omitted) and not isinstance(pair.b, Omitted):
            a_spans, b_spans = char_spans(a_text, b_text)
        return DiffRow(r, depth, kp, label, a_text, b_text, pair.akind, pair.bkind, status, pair.point,
                       STATUS_ROLES.get(status), a_path, b_path, expander, pair.changes, a_spans, b_spans,
                       summary)

    def change_at(self, row: int) -> Change | None:
        """The change exactly at ``row`` (``None`` for other rows)."""
        pair = self._pair(self._kp(row))
        if not pair.point:
            return None
        for trie in (pair.ta, pair.tb):
            if trie is not None:
                if trie.change is not None:
                    return trie.change
                if trie.moved is not None:
                    return trie.moved
        return None

    def values_at(self, row: int) -> tuple[Any, Any]:
        """The two values of ``row`` (ABSENT / UNKNOWN possible)."""
        pair = self._pair(self._kp(row))
        return pair.a, pair.b

    # ------------------------------------------------------------------ navigation
    def _nav_layout(self, kp: tuple) -> _Layout | None:
        pair = self._pair(kp)
        if not pair.nav_children or not self._can_expand(pair):
            return None
        if not self._has_layout(pair) and self._expensive(pair):
            return None
        return self._layout(pair)

    def _first_point(self, kp: tuple) -> tuple | None:
        """The first difference row at or below ``kp`` in pre-order."""
        if self._pair(kp).point:
            return kp
        lay = self._nav_layout(kp)
        if lay is None:
            return None
        for idx in lay.nav:
            found = self._first_point(kp + (idx,)) if len(kp) < 64 else self._first_point_iter(kp + (idx,))
            if found is not None:
                return found
        return None

    def _first_point_iter(self, kp: tuple) -> tuple | None:
        """:meth:`_first_point` for deep trees (no backtracking: the first branch decides)."""
        while not self._pair(kp).point:
            lay = self._nav_layout(kp)
            if lay is None or not lay.nav:
                return None
            kp = kp + (lay.nav[0],)
        return kp

    def _last_point(self, kp: tuple) -> tuple | None:
        """The last difference row at or below ``kp`` in pre-order."""
        while True:
            lay = self._nav_layout(kp)
            if lay is None or not lay.nav:
                return kp if self._pair(kp).point else None
            kp = kp + (lay.nav[-1],)

    def _reveal(self, kp: tuple | None) -> int | None:
        if kp is None:
            return None
        changed = False
        for k in range(1, len(kp)):
            prefix = kp[:k]
            if not self._flat.is_expanded(prefix):
                self._expand_kp(prefix, self._pair(prefix))
                changed = True
        if changed:
            self._changed()
        return self._flat.row_of(kp)

    def next_change(self, row: int) -> int | None:
        """The row of the next difference after ``row`` in document order (``row`` negative or
        past the end: the first one), its ancestors expanded; ``None`` when there is none."""
        if row < 0 or row >= self.row_count():
            return self._reveal(self._first_point((0,)))
        kp = self._kp(row)
        lay = self._nav_layout(kp)
        if lay is not None:
            for idx in lay.nav:
                found = self._first_point(kp + (idx,))
                if found is not None:
                    return self._reveal(found)
        while len(kp) > 1:
            parent = kp[:-1]
            lay = self._layout(self._pair(parent))
            for k in range(bisect_right(lay.nav, kp[-1]), len(lay.nav)):
                found = self._first_point(parent + (lay.nav[k],))
                if found is not None:
                    return self._reveal(found)
            kp = parent
        return None

    def prev_change(self, row: int) -> int | None:
        """The row of the previous difference before ``row`` (``row`` negative or past the end:
        the last one); ``None`` when there is none."""
        if row < 0 or row >= self.row_count():
            return self._reveal(self._last_point((0,)))
        kp = self._kp(row)
        while len(kp) > 1:
            parent = kp[:-1]
            lay = self._layout(self._pair(parent))
            for k in range(bisect_left(lay.nav, kp[-1]) - 1, -1, -1):
                found = self._last_point(parent + (lay.nav[k],))
                if found is not None:
                    return self._reveal(found)
            if self._pair(parent).point:
                return self._reveal(parent)
            kp = parent
        return None

    def first_change(self) -> int | None:
        return self.next_change(-1)

    def reveal_paths(self, a_path: tuple | None, b_path: tuple | None = None) -> int | None:
        """Expand down to the node at ``a_path`` (A side) -- or, without it, ``b_path`` (B side) --
        and return its row (``None``: not shown or not found)."""
        path = a_path if a_path is not None else b_path
        if path is None:
            return None
        use_a = a_path is not None
        kp: tuple = (0,)
        pair = self._root
        for seg in path:
            if not self._can_expand(pair):
                return None
            lay = self._layout(pair)
            found = None
            for idx in range(min(lay.count, REVEAL_SCAN_MAX)):
                aseg, bseg, gap = lay.spec(idx)
                if not gap and (aseg if use_a else bseg) == seg and type(aseg if use_a else bseg) is type(seg):
                    found = idx
                    break
            if found is None:
                return None
            kp = kp + (found,)
            pair = self._pair(kp)
        return self._reveal(kp)

    # ------------------------------------------------------------------ helpers for the view
    def array_fields(self, limit: int = 200) -> list[str]:
        """Member names of the objects inside the arrays of the two values (sorted): the field
        list of "match array elements by key". Computed once (a bounded scan; the view asks for
        it in the worker that built the model)."""
        cached = self._fields_cache
        if cached is not None:
            return list(cached)
        fields = self._scan_fields(limit)
        self._fields_cache = fields
        return list(fields)

    def _scan_fields(self, limit: int) -> list[str]:
        found: set[str] = set()
        todo: deque = deque(v for v in (self._root.a, self._root.b) if _is_container(v))
        nodes = 0
        while todo and nodes < FIELD_SCAN_NODES and len(found) < limit:
            v = todo.popleft()
            nodes += 1
            if isinstance(v, dict):
                todo.extend(x for x in v.values() if _is_container(x))
                continue
            for x in v[:FIELD_SCAN_ELEMENTS]:
                if isinstance(x, dict):
                    found.update(k for k in x if isinstance(k, str))
                    todo.extend(y for y in x.values() if _is_container(y))
                elif isinstance(x, (list, tuple)):
                    todo.append(x)
        return sorted(found)[:limit]
