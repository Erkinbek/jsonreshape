"""Visible-row flattening of a lazily expanded tree (docs/ARCHITECTURE.md §11.4).

Pure Python (no Qt): the tree view maps its rows to tree nodes with :class:`FlatTree`.

Model
-----
The top level has ``top_count`` items (the current record view: up to 10**12 rows, maybe
filtered and sorted). Expanded nodes have known child counts. Nothing is stored per row:
only per *recorded* node (expanded nodes and the ancestors of remembered expanded nodes).

* **Identity** of a node is its key path ``(top_key, c1, c2, ...)``. ``top_key`` identifies a
  top item independently of its display position (record number, ``"wrapper"``, ...);
  ``ci`` are child indices in **file order**, also when the parent shows its children in a
  different display order (``order``). :attr:`RowInfo.pos` is the *display* position among
  the siblings.
* ``top_key(pos)`` maps a display position of the top level to its key, ``top_pos(key)``
  maps back (``None``: not in the current view). Defaults are the identity
  (``top_key = pos``). ``top_pos`` may be expensive (filtered/sorted views): it is called once
  per *recorded* top key on :meth:`FlatTree.set_top` (the result is cached until the next
  ``set_top``), once when a new top key is expanded and by :meth:`FlatTree.row_of` for a top
  key that is not recorded. ``top_key`` is only called for rows that are located.
* **Expansion state survives**: :meth:`~FlatTree.collapse` hides a subtree but remembers the
  expanded descendants (re-expanding restores them); :meth:`~FlatTree.set_top` replaces the
  top level but keeps every expansion, also of top items that are not in the new view (they
  contribute no rows until a later view contains them again). Only
  :meth:`~FlatTree.collapse_all`, child-count shrinking and re-expanding with fewer children
  forget expansions.
* **Expanding below a collapsed ancestor is allowed**: the expansion is recorded and shows
  as soon as every ancestor is expanded (and the top item is in the view); it adds no rows
  meanwhile. Child indices are validated against the child count of every *expanded*
  ancestor; below collapsed ancestors (whose child count is not known) the check happens
  when the ancestor is expanded: remembered children whose index is ``>= child_count`` are
  then dropped.

Data structure and complexity
-----------------------------
Every expanded node keeps, for its recorded children (expanded or carrying remembered
descendants), the sorted display positions, the child records and ``start[i]`` = row offset
of child record ``i`` below the node (= its display position + the visible subtree sizes of
the recorded children before it). ``start`` is maintained lazily: a size change only lowers
the node's ``dirty`` mark, and the next lookup recomputes ``start[dirty:]`` with C-speed
``itertools.accumulate``. Hence

* :meth:`~FlatTree.locate` / :meth:`~FlatTree.row_of`: ``O(depth * log k)`` (plus a pending
  ``O(k)`` C-level refresh after updates), ``k`` = recorded children of a node;
* expand / collapse / child-count growth: ``O(depth * log k)`` plus a list insert/delete
  (``memmove``); re-ordering a node is ``O(k log k)`` with a :class:`DisplayOrder` (plus
  ``O(n)`` for a plain sequence), shrinking it ``O(n + k log k)`` for ``n`` children;
* :meth:`~FlatTree.iter_rows`: one locate, then ``O(1)`` per row.

Display orders
--------------
A display order given as a plain sequence is copied, validated and inverted by
:meth:`~FlatTree.expand` / :meth:`~FlatTree.set_order` (``O(n)`` Python work: about 60 ms for a
million children). Build a :class:`DisplayOrder` instead -- in a worker thread -- and those
calls take it in ``O(1)`` (only its length is checked). A FlatTree never modifies the arrays
of a :class:`DisplayOrder` (the caller may keep and reuse it, e.g. in a cache).

A FlatTree is not thread-safe; it belongs to the GUI thread. A :class:`DisplayOrder` is
immutable once built and may be passed between threads.

Structure changes (editing, docs/ARCHITECTURE.md §12.9)
-------------------------------------------------------
Identity is positional (record number, child index), so an edit that inserts, deletes or
moves records or children shifts the key paths of everything after the change. Two
operations re-key the recorded nodes with a ``Remap``-like object (``map(old) -> new |
None``, ``delta()``, ``identity``; :class:`jsonreshape.overlay.Remap`):

* :meth:`FlatTree.remap_tops` -- record-level changes: every recorded *integer* top key
  ``k`` becomes ``remap.map(k)`` (``None``: forgotten with its recorded subtree); other top
  keys (``"wrapper"``, ``"root"``) stay. In identity mode the top count changes by
  ``remap.delta()``; with custom top functions the positions are asked again from the
  current ``top_pos`` (the owner normally calls :meth:`~FlatTree.set_top` right after).
* :meth:`FlatTree.remap_children` -- the children of one container changed: its child
  records are re-keyed (``by_idx``), the key paths of every recorded descendant are
  rewritten, children mapped to ``None`` are dropped with their subtrees, the child count
  becomes ``count`` and a display order falls back to file order (the owner computes a new
  one). Unrecorded containers need nothing.

Both cost ``O(recorded nodes below the re-keyed ones)`` plus the usual rebuild of the
touched node. :meth:`FlatTree.top_runs` lists the top-level rows inside a row range in
runs (``O(log k + expanded tops in the range)``) so a selection of millions of record rows
never has to be walked row by row.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Hashable, Iterable, Iterator
from itertools import accumulate
from operator import add, attrgetter, index, itemgetter
from typing import Any, NamedTuple

__all__ = ["DisplayOrder", "FlatTree", "RowInfo"]

TopKeyFn = Callable[[int], Hashable]
TopPosFn = Callable[[Any], "int | None"]


class RowInfo(NamedTuple):
    """One visible row."""

    row: int
    depth: int  # 0 = top level
    keypath: tuple  # (top_key, c1, c2, ...): child indices in file order
    pos: int  # display position among the siblings


_SUB = attrgetter("sub")
_FIRST = itemgetter(0)


class _Node:
    """A recorded node: expanded (``open``) or an ancestor of remembered expanded nodes.

    ``sub`` = number of rows below the node when it is expanded: ``count`` plus the ``sub`` of
    its child records (0 while collapsed); it does not depend on the ancestors' state.
    ``pos`` / ``kids`` / ``start`` list the child records sorted by display position and are
    only maintained while the node is expanded; ``start[:dirty]`` is up to date.
    """

    __slots__ = ("key", "keypath", "parent", "open", "count", "order", "inv", "sub",
                 "by_idx", "pos", "kids", "start", "dirty", "dpos")

    def __init__(self, key: Any, keypath: tuple, parent: _Node | None) -> None:
        self.key = key  # top key, or child index in file order
        self.keypath = keypath
        self.parent = parent
        self.open = False
        self.count = 0  # child count (meaningful while open)
        self.order: array | None = None  # display position -> child index (None: file order)
        self.inv: array | None = None  # child index -> display position
        self.sub = 0
        self.by_idx: dict[Any, _Node] = {}  # child records by child index (root: by top key)
        self.pos: list[int] = []
        self.kids: list[_Node] = []
        self.start: list[int] = []
        self.dirty = 0
        self.dpos: int | None = None  # display position in the parent (top: None = not in view)


def _refresh(node: _Node) -> None:
    """Recompute ``node.start[node.dirty:]`` (C-level loops)."""
    pos = node.pos
    d = node.dirty
    k = len(pos)
    if d >= k:
        return
    acc = 0 if d == 0 else node.start[d - 1] - pos[d - 1] + node.kids[d - 1].sub
    node.start[d:] = map(add, pos[d:], accumulate(map(_SUB, node.kids[d:]), initial=acc))
    node.dirty = k


def _child_row(node: _Node, base: int, p: int) -> int:
    """Row of the child at display position ``p`` of the expanded ``node`` shown at row ``base``."""
    _refresh(node)
    pos = node.pos
    i = bisect_left(pos, p)
    if i < len(pos) and pos[i] == p:
        return base + 1 + node.start[i]
    if i == 0:
        return base + 1 + p
    j = i - 1
    return base + 1 + p + node.start[j] - pos[j] + node.kids[j].sub


def _inverse(order: array) -> array:
    """Inverse permutation; ``ValueError`` when ``order`` (values already in range) has duplicates."""
    n = len(order)
    inv = array("q", [-1]) * n
    for p, c in enumerate(order):
        inv[c] = p
    if n and -1 in inv:
        raise ValueError("order is not a permutation of the child indices")
    return inv


class DisplayOrder:
    """A validated display permutation (display position -> child index in file order) with
    its inverse, ready for :meth:`FlatTree.expand` / :meth:`FlatTree.set_order` in ``O(1)``.

    Building one copies, validates and inverts the permutation: ``O(n)`` Python work that
    belongs in a worker thread for large nodes. ``ValueError`` when ``order`` is not a
    permutation of ``range(len(order))`` (or its length differs from ``n`` when given).
    Immutable: :attr:`order` and :attr:`inv` must not be modified. Behaves as a read-only
    sequence of the child indices (``len``, indexing, iteration)."""

    __slots__ = ("order", "inv")

    def __init__(self, order: Iterable[int], n: int | None = None) -> None:
        if isinstance(order, DisplayOrder):
            arr, inv = order.order, order.inv
        else:
            arr = array("q", order)
            size = len(arr)
            if size and (min(arr) < 0 or max(arr) >= size):
                raise ValueError("order contains a child index out of range")
            inv = _inverse(arr)
        if n is not None and len(arr) != index(n):
            raise ValueError(f"order has {len(arr)} items, expected {n}")
        self.order: array = arr
        self.inv: array = inv

    def __len__(self) -> int:
        return len(self.order)

    def __getitem__(self, pos: int) -> int:
        return self.order[pos]

    def __iter__(self) -> Iterator[int]:
        return iter(self.order)

    def __repr__(self) -> str:
        return f"DisplayOrder(n={len(self.order)})"


def _make_order(order: Iterable[int] | None, n: int) -> tuple[array | None, array | None]:
    """Display permutation and its inverse (``(None, None)`` = file order): a
    :class:`DisplayOrder` is taken as is (``O(1)``: only its length is checked), anything
    else is copied, validated and inverted (``O(n)``)."""
    if order is None:
        return None, None
    if isinstance(order, DisplayOrder):
        if len(order.order) != n:
            raise ValueError(f"order has {len(order.order)} items, expected {n}")
        return order.order, order.inv
    arr = array("q", order)
    if len(arr) != n:
        raise ValueError(f"order has {len(arr)} items, expected {n}")
    if n and (min(arr) < 0 or max(arr) >= n):
        raise ValueError("order contains a child index out of range")
    return arr, _inverse(arr)


def _norm_keypath(keypath: Iterable[Any]) -> tuple:
    """Key path for a write operation: non-empty, hashable top key, child indices ``>= 0``."""
    kp = tuple(keypath)
    if not kp:
        raise ValueError("empty key path")
    hash(kp[0])
    if len(kp) == 1:
        return kp
    rest = tuple(map(index, kp[1:]))
    if min(rest) < 0:
        raise IndexError(f"negative child index in key path {kp!r}")
    return (kp[0],) + rest


def _identity_key(pos: int) -> int:
    return pos


def _identity_pos_fn(n: int) -> TopPosFn:
    def top_pos(key: Any) -> int | None:
        if isinstance(key, int) and not isinstance(key, bool) and 0 <= key < n:
            return key
        return None

    return top_pos


class FlatTree:
    """Row <-> node mapping of a lazily expanded tree (see the module docstring)."""

    def __init__(self, top_count: int = 0, *, top_key: TopKeyFn | None = None,
                 top_pos: TopPosFn | None = None) -> None:
        root = _Node(None, (), None)
        root.open = True
        self._root = root
        self._top_key: TopKeyFn = _identity_key
        self._top_pos: TopPosFn = _identity_pos_fn(0)
        self.set_top(top_count, top_key=top_key, top_pos=top_pos)

    # ------------------------------------------------------------------ top level
    @property
    def top_count(self) -> int:
        return self._root.count

    def set_top(self, top_count: int, *, top_key: TopKeyFn | None = None,
                top_pos: TopPosFn | None = None) -> None:
        """Replace the top level (view changed or grew); every expansion is kept.

        ``top_key`` and ``top_pos`` must be given together (or neither: identity).
        ``top_pos`` is called once per recorded top key; positions are cached until the next
        ``set_top``. ``ValueError`` (nothing changed) for an invalid count, a position outside
        ``[0, top_count)`` or two recorded keys at the same position.
        """
        n = index(top_count)
        if n < 0:
            raise ValueError("top_count must be >= 0")
        if (top_key is None) != (top_pos is None):
            raise ValueError("top_key and top_pos must be given together")
        if top_key is None:
            key_fn: TopKeyFn = _identity_key
            pos_fn: TopPosFn = _identity_pos_fn(n)
        else:
            key_fn, pos_fn = top_key, top_pos  # type: ignore[assignment]
        root = self._root
        new_pos: dict[Any, int | None] = {}
        taken: dict[int, Any] = {}
        for key in root.by_idx:
            p = self._checked_pos(pos_fn(key), n, key)
            if p is not None:
                if p in taken:
                    raise ValueError(f"top keys {taken[p]!r} and {key!r} map to position {p}")
                taken[p] = key
            new_pos[key] = p
        self._top_key, self._top_pos = key_fn, pos_fn
        root.count = n
        for key, rec in root.by_idx.items():
            rec.dpos = new_pos[key]
        self._rebuild(root)

    @staticmethod
    def _checked_pos(p: Any, n: int, key: Any) -> int | None:
        if p is None:
            return None
        p = index(p)
        if not 0 <= p < n:
            raise ValueError(f"top_pos({key!r}) = {p} is outside [0, {n})")
        return p

    # ------------------------------------------------------------------ queries
    def row_count(self) -> int:
        return self._root.sub

    def locate(self, row: int) -> RowInfo:
        """The node shown at ``row`` (``IndexError`` outside ``[0, row_count())``)."""
        row = index(row)
        node, p, kid, _base = self._find(row)
        if kid is not None:
            kp = kid.keypath
        elif node is self._root:
            kp = (self._top_key(p),)
        else:
            order = node.order
            kp = node.keypath + ((p if order is None else order[p]),)
        return RowInfo(row, len(node.keypath), kp, p)

    def row_of(self, keypath: Iterable[Any]) -> int | None:
        """Row of a node; ``None`` when it is not shown (an ancestor collapsed, top item not in
        the view, invalid key path). Calls ``top_pos`` when the top key is not recorded."""
        return self._row_rec(keypath)[0]

    def is_expanded(self, keypath: Iterable[Any]) -> bool:
        """The node itself is expanded (it may still be hidden by a collapsed ancestor)."""
        rec = self._lookup(keypath)
        return rec is not None and rec.open

    def child_count(self, keypath: Iterable[Any]) -> int | None:
        """The child count an expanded node was given (``None``: not expanded; the top level
        for the empty key path is :attr:`top_count`)."""
        rec = self._lookup(keypath)
        return rec.count if rec is not None and rec.open else None

    def has_order(self, keypath: Iterable[Any]) -> bool:
        """The expanded node shows its children in a display order (not in file order)."""
        rec = self._lookup(keypath)
        return rec is not None and rec.open and rec.order is not None

    def expanded_keypaths(self) -> list[tuple]:
        """Key paths of all expanded nodes (also hidden ones); parents before descendants."""
        out: list[tuple] = []
        stack = list(reversed(self._root.by_idx.values()))
        while stack:
            rec = stack.pop()
            if rec.open:
                out.append(rec.keypath)
            if rec.by_idx:
                stack.extend(rec.by_idx[c] for c in sorted(rec.by_idx, reverse=True))
        return out

    def parent_row(self, row: int) -> int | None:
        """Row of the parent of ``row`` (``None`` for top-level rows)."""
        node, _p, _kid, base = self._find(index(row))
        return None if node is self._root else base

    def visible_count(self, keypath: Iterable[Any]) -> int:
        """Rows of the node's visible subtree including itself (0 when it is not shown)."""
        row, rec = self._row_rec(keypath)
        if row is None:
            return 0
        return 1 + (rec.sub if rec is not None else 0)

    def last_descendant_row(self, row: int) -> int:
        """Last row of the visible subtree of ``row`` (``row`` itself when not expanded)."""
        row = index(row)
        _node, _p, kid, _base = self._find(row)
        return row + (kid.sub if kid is not None else 0)

    def next_sibling_row(self, row: int) -> int | None:
        """Row of the next sibling of ``row`` (``None`` for the last child)."""
        row = index(row)
        node, p, kid, _base = self._find(row)
        if p + 1 >= node.count:
            return None
        return row + 1 + (kid.sub if kid is not None else 0)

    def iter_rows(self, start: int, stop: int) -> Iterator[RowInfo]:
        """``RowInfo`` of the rows ``start <= row < stop`` (clamped to ``[0, row_count())``).

        One ``O(depth * log k)`` seek, then ``O(1)`` per row (``top_key`` is called for plain
        top rows). The tree must not change while the iterator is used.
        """
        start = max(0, index(start))
        stop = min(index(stop), self._root.sub)
        if start >= stop:
            return
        root = self._root
        # frames: [node, p, i]: p = display position of the current child of node,
        # i = index of the first child record with display position >= p (after the current
        # child once its subtree is being walked)
        frames: list[list[Any]] = []
        node, r = root, start
        while True:
            _refresh(node)
            st = node.start
            i = bisect_right(st, r) - 1
            if i < 0:
                frames.append([node, r, 0])
                break
            d = r - st[i]
            ppos = node.pos[i]
            if d == 0:
                frames.append([node, ppos, i])
                break
            kid = node.kids[i]
            s = kid.sub
            if d <= s:
                frames.append([node, ppos, i + 1])
                node, r = kid, d - 1
                continue
            frames.append([node, ppos + d - s, i + 1])
            break

        top_key = self._top_key
        row = start
        frame = frames[-1]
        node, p, i = frame
        while True:
            pos = node.pos
            if i < len(pos) and pos[i] == p:
                kid = node.kids[i]
                kp = kid.keypath
                i += 1
            else:
                kid = None
                if node is root:
                    kp = (top_key(p),)
                else:
                    order = node.order
                    kp = node.keypath + ((p if order is None else order[p]),)
            yield RowInfo(row, len(node.keypath), kp, p)
            row += 1
            if row >= stop:
                return
            if kid is not None and kid.sub:
                frame[1], frame[2] = p, i
                frame = [kid, 0, 0]
                frames.append(frame)
                node, p, i = kid, 0, 0
                continue
            p += 1
            while p >= node.count:  # subtree finished: resume the parent after its child
                frames.pop()
                frame = frames[-1]  # never empty while row < stop
                node, p, i = frame[0], frame[1] + 1, frame[2]
            frame[1], frame[2] = p, i

    # ------------------------------------------------------------------ updates
    def expand(self, keypath: Iterable[Any], child_count: int, order: Iterable[int] | None = None) -> int:
        """Expand a node with ``child_count`` children shown in ``order`` (display position ->
        child index; ``None`` = file order; a :class:`DisplayOrder` is taken in ``O(1)``).
        Returns the number of rows added (the change of :meth:`row_count`, 0 when the node is
        hidden).

        Expanding an already expanded node replaces its child count and order (like
        :meth:`set_child_count` + :meth:`set_order`; the result may be negative). Remembered
        expanded children with an index ``>= child_count`` are forgotten. ``IndexError``: a
        child index is out of range for an expanded ancestor; ``ValueError``: invalid
        ``child_count`` / ``order`` / top position. Nothing changes on error.
        """
        kp = _norm_keypath(keypath)
        n = index(child_count)
        if n < 0:
            raise ValueError("child_count must be >= 0")
        order_arr, inv = _make_order(order, n)
        self._check_path(kp)
        root = self._root
        before = root.sub
        rec = self._ensure(kp)
        old_sub = rec.sub
        rec.open = True
        rec.count = n
        rec.order, rec.inv = order_arr, inv
        self._drop_kids(rec, n)
        self._rebuild(rec)
        self._propagate(rec, rec.sub - old_sub)
        return root.sub - before

    def collapse(self, keypath: Iterable[Any]) -> int:
        """Collapse a node; its expanded descendants are remembered. Returns the rows removed."""
        rec = self._lookup(keypath)
        if rec is None or not rec.open:
            return 0
        root = self._root
        before = root.sub
        old_sub = rec.sub
        rec.open = False
        rec.sub = 0
        rec.order = rec.inv = None
        rec.pos, rec.kids, rec.start, rec.dirty = [], [], [], 0
        self._propagate(rec, -old_sub)
        self._detach_if_empty(rec)
        return before - root.sub

    def collapse_all(self) -> None:
        """Collapse every node and forget all remembered expansions."""
        root = self._root
        root.by_idx = {}
        self._rebuild(root)

    def set_order(self, keypath: Iterable[Any], order: Iterable[int] | None) -> None:
        """Display order of an expanded node's children (``KeyError`` when not expanded; a
        :class:`DisplayOrder` is taken in ``O(1)``, the rebuild is ``O(k log k)``)."""
        rec = self._lookup(keypath)
        if rec is None or not rec.open:
            raise KeyError(tuple(keypath))
        rec.order, rec.inv = _make_order(order, rec.count)
        self._rebuild(rec)  # the row count does not change

    def set_child_count(self, keypath: Iterable[Any], n: int) -> None:
        """New child count of a node (growing record). Remembered expanded children with an
        index ``>= n`` are forgotten. With a display order, removed children leave the order
        and new children are appended at the end in file order (call :meth:`set_order` to
        re-sort). No-op for nodes that are not recorded.
        """
        kp = _norm_keypath(keypath)
        n = index(n)
        if n < 0:
            raise ValueError("child count must be >= 0")
        rec = self._lookup(kp)
        if rec is None:
            return
        if not rec.open:
            rec.count = n
            self._drop_kids(rec, n)
            self._detach_if_empty(rec)
            return
        old_n = rec.count
        if n == old_n:
            return
        old_sub = rec.sub
        rec.count = n
        if n > old_n:
            if rec.order is not None:  # new children at the end of the display order
                # new arrays: the old ones may belong to a DisplayOrder the caller keeps
                tail = array("q", range(old_n, n))
                rec.order = rec.order + tail
                rec.inv = rec.inv + tail  # type: ignore[operator]
            rec.sub += n - old_n  # existing child records keep their display positions
        else:
            if rec.order is not None:
                rec.order = array("q", [c for c in rec.order if c < n])
                rec.inv = _inverse(rec.order)
            self._drop_kids(rec, n)
            self._rebuild(rec)
        self._propagate(rec, rec.sub - old_sub)

    # ------------------------------------------------------------------ structure changes
    def remap_tops(self, remap: Any) -> None:
        """Re-key the recorded top items after a record-level change (see the module docstring,
        "Structure changes"). ``remap``: ``map(pos) -> int | None`` (and ``delta()``,
        ``identity``); ``None`` or an identity remap is a no-op. Integer top keys are mapped
        (a key whose mapping fails or is ``None`` is forgotten with its recorded subtree);
        other keys stay. Identity mode: the top count changes by ``remap.delta()``. Custom
        top functions: positions are asked again from the current ``top_pos``; a key it maps
        outside the top level or onto a position taken by another key is hidden until the
        next :meth:`set_top`. ``ValueError`` (nothing changed) when two keys map to one key.
        """
        if remap is None or getattr(remap, "identity", False):
            return
        root = self._root
        keep: dict[Any, _Node] = {}
        moved: list[tuple[_Node, Any]] = []
        for key, rec in root.by_idx.items():
            if isinstance(key, int) and not isinstance(key, bool):
                try:
                    new = remap.map(key)
                except (IndexError, ValueError, TypeError):
                    new = None
                if new is None:
                    continue
                moved.append((rec, index(new)))
            else:
                keep[key] = rec
        for rec, new in moved:
            if new in keep:
                raise ValueError(f"two recorded top keys map to {new!r}")
            keep[new] = rec
        identity = self._top_key is _identity_key
        n = root.count
        if identity:
            try:
                n = max(0, n + int(remap.delta()))
            except (AttributeError, TypeError, ValueError):
                n = root.count
        # -- nothing raised: change the tree
        for rec, new in moved:
            if rec.key != new:
                rec.key = new
                self._rekey_subtree(rec, (new,))
        root.by_idx = keep
        if identity:
            root.count = n
            self._top_pos = _identity_pos_fn(n)
        taken: set[int] = set()
        for key, rec in keep.items():
            try:
                p = self._checked_pos(self._top_pos(key), n, key)
            except (ValueError, TypeError):
                p = None
            if p is not None and p in taken:
                p = None
            if p is not None:
                taken.add(p)
            rec.dpos = p
        self._rebuild(root)

    def remap_children(self, keypath: Iterable[Any], remap: Any, count: int) -> None:
        """The children of the container ``keypath`` changed structure (see the module
        docstring, "Structure changes"): child records are re-keyed with ``remap.map(old)``
        (``None`` / out of ``range(count)``: dropped with their subtrees), key paths of the
        recorded descendants follow, the child count becomes ``count`` and a display order
        falls back to file order. No-op for a node that is not recorded. ``ValueError``
        (nothing changed) for a negative count or two children mapped to one index."""
        kp = _norm_keypath(keypath)
        n = index(count)
        if n < 0:
            raise ValueError("child count must be >= 0")
        rec = self._lookup(kp)
        if rec is None:
            return
        identity = remap is None or getattr(remap, "identity", False)
        if identity and rec.open and n == rec.count:
            return
        new_by: dict[Any, _Node] = {}
        moved: list[tuple[_Node, int]] = []
        for c, child in rec.by_idx.items():
            if identity:
                new: int | None = c
            else:
                try:
                    new = remap.map(c)
                except (IndexError, ValueError, TypeError):
                    new = None
            if new is None:
                continue
            new = index(new)
            if not 0 <= new < n:
                continue
            if new in new_by:
                raise ValueError(f"two children of {kp!r} map to index {new}")
            new_by[new] = child
            moved.append((child, new))
        # -- nothing raised: change the tree
        for child, new in moved:
            if child.key != new:
                child.key = new
                self._rekey_subtree(child, rec.keypath + (new,))
        rec.by_idx = new_by
        if rec.open:
            old_sub = rec.sub
            rec.count = n
            rec.order = rec.inv = None
            self._rebuild(rec)
            self._propagate(rec, rec.sub - old_sub)
        else:
            rec.count = n
            rec.order = rec.inv = None
            self._detach_if_empty(rec)

    def top_runs(self, row0: int, row1: int) -> Iterator[tuple[int, int, int]]:
        """The top-level rows inside the inclusive row range ``[row0, row1]`` as maximal runs
        ``(row, pos, n)``: rows ``row .. row+n-1`` show the top items at display positions
        ``pos .. pos+n-1`` (rows of expanded subtrees between them are left out).
        ``O(log k + expanded top items in the range)``."""
        root = self._root
        _refresh(root)
        lo = max(0, index(row0))
        hi = min(index(row1), root.sub - 1)
        if lo > hi:
            return
        starts, poss, kids = root.start, root.pos, root.kids
        k = len(poss)
        cur: list[int] | None = None   # [row, pos, n] of the run being built

        def pieces() -> Iterator[tuple[int, int, int]]:
            """Top rows in order as ``(row, pos, n)`` pieces from the one holding ``lo``."""
            i = bisect_right(starts, lo) - 1
            if i < 0:
                end = starts[0] if k else root.sub
                yield 0, 0, end
                i = 0
            for j in range(i, k):
                r = starts[j]
                if r > hi:
                    return
                yield r, poss[j], 1
                a = r + kids[j].sub + 1
                b = starts[j + 1] if j + 1 < k else root.sub
                yield a, poss[j] + 1, b - a

        for r, p, m in pieces():
            if m <= 0:
                continue
            a, b = max(r, lo), min(r + m - 1, hi)
            if a > b:
                if r > hi:
                    break
                continue
            pa = p + (a - r)
            if cur is not None and cur[0] + cur[2] == a and cur[1] + cur[2] == pa:
                cur[2] += b - a + 1
            else:
                if cur is not None:
                    yield cur[0], cur[1], cur[2]
                cur = [a, pa, b - a + 1]
        if cur is not None:
            yield cur[0], cur[1], cur[2]

    @staticmethod
    def _rekey_subtree(rec: _Node, keypath: tuple) -> None:
        """``rec`` now lives at ``keypath``: rewrite the key paths of its recorded subtree."""
        stack = [(rec, keypath)]
        while stack:
            node, kp = stack.pop()
            node.keypath = kp
            for c, child in node.by_idx.items():
                stack.append((child, kp + (c,)))

    # ------------------------------------------------------------------ internals
    def _lookup(self, keypath: Iterable[Any]) -> _Node | None:
        """Recorded node of a key path (lenient: ``None`` for anything invalid)."""
        try:
            kp = tuple(keypath)
            if not kp:
                return None
            rec = self._root.by_idx.get(kp[0])
            for c in kp[1:]:
                if rec is None:
                    return None
                rec = rec.by_idx.get(c)
        except TypeError:  # unhashable key
            return None
        return rec

    def _row_rec(self, keypath: Iterable[Any]) -> tuple[int | None, _Node | None]:
        try:
            kp = tuple(keypath)
        except TypeError:
            return None, None
        if not kp:
            return None, None
        root = self._root
        key = kp[0]
        try:
            rec = root.by_idx.get(key)
        except TypeError:
            return None, None
        if rec is not None:
            p = rec.dpos
        else:
            p = self._checked_pos(self._top_pos(key), root.count, key)
        if p is None:
            return None, None
        row = _child_row(root, -1, p)
        for c in kp[1:]:
            if rec is None or not rec.open:
                return None, None
            try:
                c = index(c)
            except TypeError:
                return None, None
            if not 0 <= c < rec.count:
                return None, None
            inv = rec.inv
            row = _child_row(rec, row, c if inv is None else inv[c])
            rec = rec.by_idx.get(c)
        return row, rec

    def _find(self, row: int) -> tuple[_Node, int, _Node | None, int]:
        """``(parent node, display position, record of the row or None, row of the parent)``;
        the root's row is -1."""
        node = self._root
        if not 0 <= row < node.sub:
            raise IndexError(f"row {row} out of range (0..{node.sub - 1})")
        r = row
        base = -1
        while True:
            _refresh(node)
            st = node.start
            i = bisect_right(st, r) - 1
            if i < 0:
                return node, r, None, base
            d = r - st[i]
            kid = node.kids[i]
            if d == 0:
                return node, node.pos[i], kid, base
            s = kid.sub
            if d <= s:
                base = base + 1 + st[i]
                node, r = kid, d - 1
                continue
            return node, node.pos[i] + d - s, None, base

    def _check_path(self, kp: tuple) -> None:
        """``IndexError`` when a child index is out of range of an expanded ancestor."""
        rec = self._root.by_idx.get(kp[0])
        for c in kp[1:]:
            if rec is None:
                return
            if rec.open and c >= rec.count:
                raise IndexError(f"child index {c} out of range in key path {kp!r}")
            rec = rec.by_idx.get(c)

    def _ensure(self, kp: tuple) -> _Node:
        """The record of ``kp``, creating (collapsed) records along the path."""
        root = self._root
        key = kp[0]
        rec = root.by_idx.get(key)
        if rec is None:
            p = self._checked_pos(self._top_pos(key), root.count, key)
            if p is not None:
                i = bisect_left(root.pos, p)
                if i < len(root.pos) and root.pos[i] == p:
                    raise ValueError(f"top keys {root.kids[i].key!r} and {key!r} map to position {p}")
            rec = _Node(key, kp[:1], root)
            rec.dpos = p
            root.by_idx[key] = rec
            if p is not None:
                self._attach(root, rec)
        for depth in range(2, len(kp) + 1):
            parent = rec
            c = kp[depth - 1]
            rec = parent.by_idx.get(c)
            if rec is None:
                rec = _Node(c, kp[:depth], parent)
                parent.by_idx[c] = rec
                if parent.open:
                    inv = parent.inv
                    rec.dpos = c if inv is None else inv[c]
                    self._attach(parent, rec)
        return rec

    @staticmethod
    def _attach(parent: _Node, rec: _Node) -> None:
        """Insert a new collapsed (``sub == 0``) child record into the lists of ``parent``."""
        p = rec.dpos
        pos = parent.pos
        i = bisect_left(pos, p)  # type: ignore[arg-type]
        d = parent.dirty
        pos.insert(i, p)  # type: ignore[arg-type]
        parent.kids.insert(i, rec)
        if d > i:
            # start[:i] is valid: compute start[i]; the shifted entries stay valid (sub == 0)
            st = parent.start
            s = p if i == 0 else p + st[i - 1] - pos[i - 1] + parent.kids[i - 1].sub
            st.insert(i, s)
            parent.dirty = d + 1
        else:
            parent.start.insert(i, 0)

    def _detach_if_empty(self, rec: _Node) -> None:
        """Forget collapsed records without child records (upwards)."""
        root = self._root
        while rec is not root and not rec.open and not rec.by_idx:
            parent = rec.parent
            assert parent is not None
            del parent.by_idx[rec.key]
            if parent.open and rec.dpos is not None:
                i = bisect_left(parent.pos, rec.dpos)
                del parent.pos[i], parent.kids[i], parent.start[i]  # sub == 0: starts stay valid
                if parent.dirty > i:
                    parent.dirty -= 1
            rec = parent

    @staticmethod
    def _drop_kids(rec: _Node, n: int) -> None:
        by_idx = rec.by_idx
        if by_idx:
            for c in [c for c in by_idx if c >= n]:
                del by_idx[c]

    def _rebuild(self, node: _Node) -> None:
        """Rebuild the sorted child-record lists of an expanded node and its ``sub``."""
        if node is self._root:
            items = [(r.dpos, r) for r in node.by_idx.values() if r.dpos is not None]
        else:
            inv = node.inv
            for c, r in node.by_idx.items():
                r.dpos = c if inv is None else inv[c]
            items = [(r.dpos, r) for r in node.by_idx.values()]
        items.sort(key=_FIRST)
        node.pos = [p for p, _r in items]
        node.kids = [r for _p, r in items]
        node.start = [0] * len(items)
        node.dirty = 0
        node.sub = node.count + sum(map(_SUB, node.kids))

    def _propagate(self, rec: _Node, delta: int) -> None:
        """``rec.sub`` changed by ``delta``: update the expanded ancestors."""
        while delta:
            parent = rec.parent
            if parent is None or not parent.open or rec.dpos is None:
                return
            i = bisect_left(parent.pos, rec.dpos)
            if parent.dirty > i + 1:
                parent.dirty = i + 1
            parent.sub += delta
            rec = parent

    def __repr__(self) -> str:
        return (f"FlatTree(top_count={self._root.count}, rows={self._root.sub}, "
                f"expanded={len(self.expanded_keypaths())})")
