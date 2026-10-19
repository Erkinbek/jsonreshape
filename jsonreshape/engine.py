"""Mapping evaluation: the exact semantics of the ``json-tuzilma.html`` engine (docs/ARCHITECTURE.md §13.2-§13.3).

Three layers:

* :class:`PlainEvaluator` / :func:`transform_plain` -- a line-by-line port of the reference evaluation
  (``resolve`` with the deepest bound ``[]`` prefix, gather, broadcast, ``evArray`` with element-node
  concatenation / fallback / ``LIMIT``, ``evElem``, ``buildObj``, iterative ``preorder``, ``_depth`` lookups;
  scratchpad ``s4/engine_sketch.py``, verified against the real engine). It is the oracle of the tests.
* :class:`CompiledMapping` -- the same semantics compiled once per tree into per-node closures: the binding
  chain becomes a list of integer slots (one per compiled ``[]`` prefix that some node really reads, and per
  ``#d`` depth key), which ``[]`` prefix is bound at a node is decided at compile time (it only depends on the
  tree), nodes whose source lies inside the value being built read it directly ("relative" resolution, no
  binding at all), value nodes are plain dictionary reads and natural arrays are list comprehensions. Both
  evaluators give identical results on every test (the test-suite runs both).
* :func:`apply` -- the streaming application to a whole document (every kind) with progress, cancellation,
  huge records, the unmapped-fields count, exact numbers, worker processes and an atomic output file;
  :func:`preview` for the designer.

Values are JSON values as :func:`jsonreshape.codec.loads` returns them (``dict``, ``list``, ``str``, ``int``,
``float``, ``bool``, ``None`` and the codec's exact-number subclasses). Bindings are keyed by the source
PREFIX (a tuple ending with :data:`WILD`), never by tree node; the depth binding of an element node with
source ``S`` is ``(S, "#d")`` and a computed ``_depth`` node reads ``(ctx, "#d")``. A ``MISSING`` result
(:data:`MISSING`, never ``None``: JSON ``null`` is a value) means "the field is not written".

Streaming (§13.3). ``apply`` evaluates the tree once on a root value in which the record array is a
:class:`_RecordStream` placeholder -- the root value itself for record documents (array, jsonl, stream,
brace_list), the value at the record path of the parsed wrapper for wrapped documents. The compiler knows
statically where the stream can be iterated (an element node whose source array is the record array, an
array step at the record path) and compiles those places into closures whose result is a
:class:`_LazyList`: an array produced while it is written, one pass over the records per element node that
reads them (``ApplyReport.passes``). This is the §13.3 decomposition (one pass per root element node,
element-node-major output; wrapper members in tree order; wrapper-level gathers as their own passes;
records see the wrapper as the root, so wrapper fields broadcast into them) and it is exact for every tree:
whether a lazy array exists at all (the reference's fallback to the first non-array value, or MISSING) is
decided by a probe that reads records only until one reaches an array. Inside a record everything is the
plain compiled evaluation. Keyed documents map every member value (the member keys are written as their
raw tokens, MISSING members are dropped); single documents are transformed whole (up to
``Settings.large_node_bytes``).

The output has exactly the layout of one ``core.Reformatter(indent)`` over the compact text (every JSON
export of the program): plain values come from ``codec.dumps(value, indent)`` (orjson's own 2-space layout)
moved to their nesting level, raw record text goes through a Reformatter. Worker processes
(``ProcessPoolExecutor``, spawn; inputs of at least 64 MiB, ``min(cpu_count - 1, 8)`` workers capped so that
they fit in half of ``Settings.memory_limit_bytes``) evaluate blocks of records of "simple" stream parts --
those whose records need nothing but the record (and, for wrapped documents, wrapper parts outside the
record array) -- and return the finished text; the parent reads the blocks, keeps a bounded number in
flight and writes the results in order, so the bytes are identical to one thread. Workers are started only
when the main module cannot run the caller's program again (:func:`_spawn_safe`); if they fail, the
mapping is applied again in one process.

Decisions and deviations (README §15; the only deviations allowed by §13.0, plus what the spec leaves open):

* numbers are the codec's exact values and are written with their exact text (``1.10``, ``1e400``,
  ``12345678901234567890``, ``-0.0`` stay as they are; the reference turns them into JS doubles);
* object keys keep their insertion order (JS puts integer-like keys such as ``"10"`` first): the output key
  order is the tree order, a raw pass-through value keeps its own order;
* the reference's ``transform`` returns ``null`` for a MISSING root; :meth:`CompiledMapping.transform_value`
  and :func:`apply` do the same (``engine.warn.not_array`` when the export is not an array);
* the binding keys are tuples, so the reference's pathological cases (a data key that is literally
  ``"\\u0002"``, keys containing ``"\\u0001"``) do not exist here;
* huge records (> ``Settings.large_node_bytes``) stop the run (``engine.error.huge_record``) or, with
  ``skip_huge``, are left out (as if absent) with a counted warning;
* a wrapped document whose records are members of an object is refused (``engine.error.member_container``):
  the tree models the record container as an array, so mapping it would drop the member keys;
* JSON Lines output of a wrapped document holds the items of the record array (the array node whose element
  node reads the record array; else the first streamed array) with ``engine.warn.jsonl_wrapper``;
* the unknown-fields count marks a record when an object node built from one of its objects finds a key
  that neither the original nor the current tree knows below that source path (``mapping.known_keys``);
  values copied whole lose nothing and are not counted;
* a record that cannot be decoded (nested deeper than the decoder allows) stops the run
  (``engine.error.unreadable``).

Evaluation recurses along the TREE (never along the data: ``preorder`` is iterative; no recursion goes
through C callbacks such as ``map``, so the C stack is not used per level); for a tree deeper than the
interpreter's recursion limit allows (data nested hundreds of levels deep gives such trees) the limit is
raised while the tree is compiled and evaluated and restored afterwards (:class:`_RecursionRoom`: scoped,
thread-safe -- concurrent evaluations never lower it under one another, the last one restores it; the
process-wide limit never stays raised after :func:`apply`, :func:`transform_plain` or a
:class:`CompiledMapping` call). The plain port is quadratic in the data depth per node (the reference's
binding-chain lookups); the compiled evaluator is linear. A :class:`CompiledMapping` keeps its binding slots
in the instance: use one instance per thread.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
from array import array
from dataclasses import dataclass, field, replace
from operator import itemgetter
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import codec, core
from .errors import EditError, JsonDecodeError, UserInputError
from .fileio import atomic_write, long_path, normalize_path
from .i18n import has, t
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "MISSING",
    "PREVIEW_LIMIT",
    "RECORD_KINDS",
    "WILD",
    "ApplyOptions",
    "ApplyReport",
    "CompiledMapping",
    "PlainEvaluator",
    "apply",
    "auto_workers",
    "check_kind",
    "compile_path",
    "preorder",
    "preview",
    "transform_plain",
    "tree_root_kind",
]

WILD = None
"""The array-step segment of a source path (``jsonreshape.mapping.WILD``)."""


class _Missing:
    """The "no value" marker of the reference (``Symbol('missing')``): the field is not written."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __reduce__(self) -> str:
        return "MISSING"     # pickles as this module's global (process pool workers)


MISSING: Any = _Missing()
"""Sentinel for "no value" (never ``None``, which is JSON ``null``)."""

_M = MISSING
_DEPTH = "#d"


def _dkey(prefix: tuple) -> tuple:
    """The binding key of the ``_depth`` value of the element node whose source is ``prefix``."""
    return (prefix, _DEPTH)


def compile_path(path: Iterable[Any]) -> tuple[list[tuple | None], tuple]:
    """The reference ``compile(path)``: ``keys[i]`` = the prefix ``path[:i+1]`` when ``path[i]`` is
    :data:`WILD` (the binding key of that array step), else ``None``; ``full`` = the whole path."""
    segs = tuple(path)
    keys = [segs[:i + 1] if seg is WILD else None for i, seg in enumerate(segs)]
    return keys, segs


def preorder(arr: list, key: str) -> Iterator[tuple[Any, int]]:
    """``(element, depth)`` of ``arr`` and, below every object element, of the array under ``key`` --
    pre-order (an element, then its ``key`` subtree, then the next sibling), depth 1 for ``arr``'s own
    elements. Iterative (any data depth)."""
    stack = [(iter(arr), 1)]
    while stack:
        it, d = stack[-1]
        for el in it:
            yield el, d
            sub = el.get(key) if type(el) is dict else None
            if type(sub) is list:
                stack.append((iter(sub), d + 1))
                break
        else:
            stack.pop()


def _src_of(node: Any) -> tuple:
    src = node.src
    if src is None:
        raise ValueError(f"mapping node {node.key!r} (id {node.id}) has no source path")
    return tuple(src)


def _tree_root(tree: Any) -> Any:
    """The root node of a ``MappingTree`` (or the node itself)."""
    return getattr(tree, "root", tree)


def _tree_depth(root: Any) -> int:
    """Number of nodes on the longest root-to-leaf path (iterative)."""
    best = 0
    stack = [(root, 1)]
    while stack:
        n, d = stack.pop()
        if d > best:
            best = d
        for c in n.children:
            stack.append((c, d + 1))
    return best


_RECURSION_LOCK = threading.Lock()
_FRAMES_PER_LEVEL = 4        # evaluation frames per tree level (build -> child -> array -> element)
_RECURSION_MARGIN = 400      # frames left for the caller and for codec/orjson calls
_RECURSION_CAP = 200_000
# guarded by _RECURSION_LOCK: the open _RecursionRoom blocks that raised the limit, the limit before the
# first of them, and the limit they set last (restored only while it is still in force)
_room_users = 0
_room_base = 0
_room_set = 0


class _RecursionRoom:
    """``with _RecursionRoom(depth):`` -- the interpreter's recursion limit lets a tree ``depth`` levels deep
    be compiled and evaluated inside the block, and is restored when the block ends (it never stays raised:
    the limit is process-wide, and a permanently raised one changed what other code -- the JSON Schema
    validator's "too deep", the GUI -- does after a mapping ran). Python-to-Python calls do not use the C
    stack (3.11+), so a higher limit only costs heap memory for the frames really used.

    Thread-safe and re-entrant: the first block that needs more than the current limit remembers it and
    raises it; blocks that start while it is raised only raise it further (never lower it under one
    another); the last one to end restores the remembered limit -- unless someone else changed the limit
    meanwhile (theirs is kept). A block that needs no more than the limit outside every block does nothing.
    """

    __slots__ = ("need", "held")

    def __init__(self, depth: int) -> None:
        self.need = min(_RECURSION_CAP, depth * _FRAMES_PER_LEVEL + _RECURSION_MARGIN)
        self.held = False

    def __enter__(self) -> _RecursionRoom:
        global _room_users, _room_base, _room_set
        need = self.need
        with _RECURSION_LOCK:
            current = sys.getrecursionlimit()
            if _room_users:
                if need <= _room_base:
                    return self                  # the limit stays >= the base while other blocks are open
                if need > current:
                    sys.setrecursionlimit(need)
                    _room_set = need
            elif need <= current:
                return self
            else:
                _room_base = current
                sys.setrecursionlimit(need)
                _room_set = need
                log.debug("mapping tree: recursion limit raised from %d to %d", current, need)
            _room_users += 1
            self.held = True
        return self

    def __exit__(self, *exc: Any) -> None:
        global _room_users
        if not self.held:
            return
        self.held = False
        with _RECURSION_LOCK:
            _room_users -= 1
            if _room_users or sys.getrecursionlimit() != _room_set:
                return
            try:
                sys.setrecursionlimit(_room_base)
            except RecursionError:               # pragma: no cover - this thread is deeper than the old limit
                log.info("recursion limit %d kept (the thread is deeper than %d)", _room_set, _room_base)


# ======================================================================================
# the reference evaluation, line by line (engine_sketch.py, engine_map.md §5-§6)
# ======================================================================================
def _look(b: tuple | None, k: Any) -> tuple | None:
    """The innermost binding ``(key, value, parent)`` of ``k`` in the chain ``b``."""
    while b is not None:
        if b[0] == k:
            return b
        b = b[2]
    return None


class PlainEvaluator:
    """The reference evaluation of a mapping tree on ``root`` (the reference's ``ROOT``), unoptimized.

    ``limit`` is the reference ``LIMIT`` (``0``/``None`` = no limit): the most items any array node or
    gathered list produces (the preview uses 21). ``depth``: the tree's depth in nodes;
    :func:`transform_plain` evaluates inside ``_RecursionRoom(depth)`` (a direct :meth:`ev` call runs within
    the current recursion limit)."""

    def __init__(self, tree: Any, root: Any, limit: int | None = None) -> None:
        self.node = _tree_root(tree)
        self.root = root
        self.limit: float = limit if limit else math.inf
        self.cp: dict[int, tuple] = {}
        self.pcp: dict[int, tuple] = {}
        stack = [self.node]
        while stack:                                    # compileAll()
            n = stack.pop()
            if not n.synthetic and not n.computed:
                segs = _src_of(n)
                self.cp[id(n)] = (segs, *compile_path(segs))
                if n.is_elem:
                    p = segs[:-1]
                    self.pcp[id(n)] = (p, *compile_path(p))
            stack.extend(n.children)
        self.depth = _tree_depth(self.node)

    def resolve(self, cp: tuple, b: tuple | None) -> tuple[list, bool]:
        segs, keys, _ = cp
        start, val = 0, self.root
        for i in range(len(segs) - 1, -1, -1):          # the deepest bound [] wins
            k = keys[i]
            if k is not None:
                h = _look(b, k)
                if h is not None:
                    start, val = i + 1, h[1]
                    break
        br: list = [(val, b)]
        gathered = False
        i = start
        n = len(segs)
        while i < n and br:
            s = segs[i]
            nx: list = []
            if s is WILD:
                gathered = True
                k = keys[i]
                for v, xb in br:
                    if isinstance(v, list):
                        nx.extend((el, (k, el, xb)) for el in v)
            else:
                for v, xb in br:
                    if isinstance(v, dict) and s in v:
                        nx.append((v[s], xb))
            br = nx
            i += 1
        return br, gathered

    def build_obj(self, n: Any, b: tuple | None) -> dict:
        o: dict = {}
        for c in n.children:
            r = self.ev(c, b)
            if r is not _M:
                o[c.key] = r                            # duplicate keys: first position, last value (as JS)
        return o

    def ev_elem(self, e: Any, b: tuple | None, el: Any) -> Any:
        if e.kind == "object":
            return self.build_obj(e, b) if isinstance(el, dict) else el
        if e.kind == "array":
            return self.ev_array(e, b) if isinstance(el, list) else el
        return el

    def ev_array(self, n: Any, b: tuple | None) -> Any:
        out: list = []
        saw = False
        fb: Any = _M
        for e in n.children:
            if not e.is_elem:
                continue
            br, _ = self.resolve(self.pcp[id(e)], b)
            ek = self.cp[id(e)][2]
            dk = _dkey(ek)
            for v, xb in br:
                if not isinstance(v, list):
                    if fb is _M:
                        fb = v
                    continue
                saw = True
                items = preorder(v, e.rec_key) if e.rec_key else ((el, 1) for el in v)
                for el, d in items:
                    if len(out) >= self.limit:
                        return out                      # JS: break out of both loops; saw is true
                    r = self.ev_elem(e, (dk, d, (ek, el, xb)), el)
                    if r is not _M:
                        out.append(r)
        return out if saw else fb

    def ev(self, n: Any, b: tuple | None) -> Any:
        if n.computed == "depth":
            h = _look(b, _dkey(tuple(n.ctx) if n.ctx is not None else None))
            return h[1] if h is not None else _M
        if n.computed:
            return _M
        if n.synthetic:
            return self.build_obj(n, b)
        if n.kind == "array":
            return self.ev_array(n, b)
        br, gathered = self.resolve(self.cp[id(n)], b)
        obj = n.kind == "object"
        if gathered:
            out = []
            for v, xb in br:
                if len(out) >= self.limit:
                    break
                out.append(self.build_obj(n, xb) if obj and isinstance(v, dict) else v)
            return out
        if not br:
            return _M
        v, xb = br[0]
        return self.build_obj(n, xb) if obj and isinstance(v, dict) else v


def transform_plain(tree: Any, value: Any, *, limit: int | None = None) -> Any:
    """The reference ``transform(value, limit)`` with the plain evaluator (a MISSING root gives ``None``)."""
    ev = PlainEvaluator(tree, value, limit)
    with _RecursionRoom(ev.depth):
        r = ev.ev(ev.node, None)
    return None if r is _M else r


# ======================================================================================
# compiled evaluation
# ======================================================================================
_BASE, _SLOT, _ROOT = 0, 1, 2       # where a path resolution starts (see _make_plan)


class _Plan:
    """How one source path is resolved at one place of the tree (static, see :func:`_make_plan`).

    ``mode``: :data:`_BASE` -- walk ``steps`` from the value being built (the "base": the object of the
    enclosing object node / element, or the root value); :data:`_SLOT` -- from the binding of the prefix
    ``key`` (the deepest ``[]`` of the path bound here); :data:`_ROOT` -- from the root value.
    ``steps`` = the segments walked, ``off`` = the index of ``steps[0]`` in the source path ``src``,
    ``fan`` = the binding keys of the ``[]`` steps walked (bound in every branch they produce)."""

    __slots__ = ("mode", "key", "steps", "off", "fan", "src")

    def __init__(self, mode: int, key: tuple | None, steps: tuple, off: int, fan: tuple, src: tuple) -> None:
        self.mode = mode
        self.key = key
        self.steps = steps
        self.off = off
        self.fan = fan
        self.src = src

    @property
    def first_wild(self) -> int:
        """Index in ``steps`` of the first ``[]`` (``-1``: none -- the path is never a gather)."""
        for i, s in enumerate(self.steps):
            if s is WILD:
                return i
        return -1


def _make_plan(src: tuple, bound: frozenset, base_src: tuple | None) -> _Plan:
    """The static resolution of ``src`` where the binding keys ``bound`` are bound and the base value is the
    value at ``base_src`` (``None``: no usable base).

    The reference ``resolve`` starts at the deepest ``[]`` of ``src`` that is bound (``d``), else at the
    root. Which keys are bound only depends on the tree (every element evaluation binds its source, every
    executed ``[]`` step binds its prefix), so ``d`` is static. When ``src`` extends ``base_src`` and no
    ``[]`` beyond ``base_src`` is bound, walking ``src[d+1:len(base_src)]`` from that binding gives exactly
    the base (the base was reached that way, with the same bindings), so the walk can start at the base:
    every ``[]`` of ``base_src`` after ``d`` was walked -- and is bound -- by the node that built the base."""
    d = -1
    for i in range(len(src) - 1, -1, -1):
        if src[i] is WILD and src[:i + 1] in bound:
            d = i
            break
    if base_src is not None and d < len(base_src) and src[:len(base_src)] == base_src:
        mode, key, off = _BASE, None, len(base_src)
    elif d >= 0:
        mode, key, off = _SLOT, src[:d + 1], d + 1
    else:
        mode, key, off = _ROOT, None, 0
    fan = tuple(src[:j + 1] for j in range(off, len(src)) if src[j] is WILD)
    return _Plan(mode, key, src[off:], off, fan, src)


class _Ctx:
    """Static evaluation context of a place in the tree: the bound binding keys, the source path of the
    base value (``None``: none) and whether the base is known to be an object."""

    __slots__ = ("bound", "base_src", "base_dict")

    def __init__(self, bound: frozenset, base_src: tuple | None, base_dict: bool) -> None:
        self.bound = bound
        self.base_src = base_src
        self.base_dict = base_dict


def _identity(v: Any) -> Any:
    return v


_SHAPE_SAME, _SHAPE_COPY, _SHAPE_SEGS = 0, 1, 2     # how an object is built for one source shape
_SHAPES_MAX = 1024                                  # shapes remembered per object node


class _RecordStream:
    """The record array of the document being mapped, standing in the root value during :func:`apply`:
    iterating it (:meth:`blocks`) is one streaming pass over the records; as a raw value it is written from
    the raw record bytes. The evaluator treats it as an array where the record array is (record documents:
    the root value itself; wrapped documents: the value at the record path of the wrapper)."""

    __slots__ = ("_src",)

    def __init__(self, src: Any) -> None:
        self._src = src

    def blocks(self) -> Iterator[tuple[array, list]]:
        """One pass: ``(record positions, values)`` block by block."""
        return self._src.blocks()

    def mark(self, pos: int) -> None:
        """Record ``pos`` contained fields the mapping does not know (``engine.warn.unmapped``)."""
        self._src.mark(pos)

    def pooled(self, part_index: int, fmt: tuple) -> Iterator[Any] | None:
        """One pass of a simple stream part evaluated by worker processes (batches of pre-formatted text,
        see :class:`_Prebuilt`), or ``None`` when the run does not use worker processes."""
        return self._src.pooled(part_index, fmt)

    def __repr__(self) -> str:
        return "<records>"


class _Prebuilt:
    """A batch of array items already written as text by a worker process (``fmt`` of the request)."""

    __slots__ = ("text", "count")

    def __init__(self, text: bytes, count: int) -> None:
        self.text = text
        self.count = count


class _LazyList:
    """An array produced while it is written: its items come from passes over the records
    (:meth:`batches` yields lists of items, or :class:`_Prebuilt` text). It keeps the binding slots of the
    place where it was made and restores the slots of its consumer when it is done."""

    __slots__ = ("_comp", "_factory", "_snap", "records")

    def __init__(self, comp: _Compiler, factory: Callable[[tuple], Iterator[Any]], records: bool = False) -> None:
        self._comp = comp
        self._factory = factory
        self._snap = comp.env[:]
        self.records = records        # the record array itself (an element node iterates the stream)

    def batches(self, fmt: tuple = (None, 0, False)) -> Iterator[Any]:
        """Batches of items; ``fmt`` = ``(indent, level, jsonl)`` of the place the array is written to (the
        layout of the text that worker processes produce)."""
        env, flag = self._comp.env, self._comp.flag
        saved, saved_flag = env[:], flag[0]
        env[:] = self._snap
        try:
            yield from self._factory(fmt)
        finally:
            env[:] = saved
            flag[0] = saved_flag

    def __repr__(self) -> str:
        return "<lazy array>"


_BATCH = 512          # items per batch of a lazy array fed by single branches


class _Part:
    """One element node of an array node, compiled: where its source array comes from (``plan``), how one
    element is evaluated (``ef``), the binding slots its evaluation sets and its recursive key. Stream-aware
    parts (their source array may be the record stream, or they walk an array step over it) have ``probe``
    and ``gen`` instead of ``fn``."""

    __slots__ = ("node", "plan", "ef", "ef_missing", "s_ek", "s_d", "rk", "fn", "stream", "probe", "gen",
                 "rec_items", "index", "pool_ok")

    def __init__(self, node: Any, plan: _Plan, ef: Callable[[Any], Any], ef_missing: bool, s_ek: int | None,
                 s_d: int | None, rk: str | None) -> None:
        self.node = node
        self.plan = plan
        self.ef = ef                  # el -> evaluated element (MISSING possible when ef_missing)
        self.ef_missing = ef_missing
        self.s_ek = s_ek              # slot of the element's source prefix (None: nobody reads it)
        self.s_d = s_d                # slot of its depth
        self.rk = rk                  # recursive key (pre-order) or None
        self.fn: Callable[[Any, list, list], bool] | None = None   # (base, out, state) -> stop
        self.stream = False
        self.probe: Callable[[Any, list], None] | None = None     # (base, state): an array branch exists?
        self.gen: Callable[[Any, tuple], Iterator[Any]] | None = None   # (base, fmt) -> batches
        self.rec_items: Callable[[Any, list], Any] | None = None  # simple stream parts: one record's items
        self.index = -1               # position in _Compiler.stream_parts (the worker processes' handle)
        self.pool_ok = False


class _Compiler:
    """Turns a mapping tree into closures. Pass 1 computes the static plans and the binding keys some node
    really reads (only those get a slot; every binding site of such a key sets and restores its slot);
    pass 2 builds the closures. Shared state of the closures: ``env`` (binding slots), ``rc`` (``rc[0]`` =
    the root value) and ``flag`` (``flag[0]`` is set when an object with keys the mapping does not know was
    built from the source; ``known`` maps a source path to the keys known below it, ``None`` = no check).

    ``stream``: the source pattern of the record array (``()`` for record documents, the record path with
    :data:`WILD` for index segments for wrapped documents) -- every place where the record stream can be
    iterated gets a stream-aware closure (``_LazyList`` results), all others the plain ones."""

    def __init__(self, root: Any, *, limit: int | None = None, known: dict | None = None,
                 stream: tuple | None = None) -> None:
        self.root = root
        self.limit: int | None = limit if limit else None
        self.known = known
        self.stream = stream
        self.plans: dict[int, _Plan] = {}          # value / object nodes
        self.pplans: dict[int, _Plan] = {}         # element nodes: their array source (src minus the last [])
        self.dkeys: dict[int, tuple | None] = {}   # computed nodes: their depth key when bound there
        self.read: set = set()                     # binding keys some node reads
        self.outer: dict[int, frozenset] = {}      # element nodes: the keys bound around their array node
        self.stream_parts: list[_Part] = []
        self.slots: dict[Any, int] = {}
        self.env: list = []
        self.rc: list = [None]
        self.flag: list = [False]
        self.depth = _tree_depth(root)    # callers evaluate root_fn inside _RecursionRoom(depth)
        self._scan(root, _Ctx(frozenset(), (), False))
        for k in sorted(self.read, key=repr):
            self.slots[k] = len(self.env)
            self.env.append(None)
        self.root_parts: list[_Part] | None = None
        with _RecursionRoom(self.depth):                 # pass 2 recurses along the tree
            if root.kind == "array" and not root.synthetic and not root.computed:
                parts = self._parts(root.children, False)
                self.root_parts = parts
                self.root_fn = self._array_of(parts)
            else:
                self.root_fn = self._node_fn(root, False)

    # -- pass 1 ---------------------------------------------------------------------------
    def _scan(self, node: Any, ctx: _Ctx) -> None:
        stack = [(node, ctx)]
        while stack:
            n, ctx = stack.pop()
            if n.computed:
                k = _dkey(tuple(n.ctx)) if (n.computed == "depth" and n.ctx is not None) else None
                if k is not None and k in ctx.bound:
                    self.read.add(k)
                    self.dkeys[id(n)] = k
                else:
                    self.dkeys[id(n)] = None
                continue
            if n.synthetic:
                stack.extend((c, ctx) for c in reversed(n.children))
                continue
            if n.kind == "array":
                stack.extend(self._scan_array(n.children, ctx))
                continue
            p = _make_plan(_src_of(n), ctx.bound, ctx.base_src)
            self.plans[id(n)] = p
            if p.mode == _SLOT:
                self.read.add(p.key)
            if n.kind == "object":
                sub = _Ctx(ctx.bound | frozenset(p.fan), p.src, True)
                stack.extend((c, sub) for c in reversed(n.children))

    @staticmethod
    def _elem_ctx(e: Any, p: _Plan, ctx: _Ctx, base_dict: bool) -> _Ctx:
        """Context inside the evaluation of element node ``e`` (its source array resolved by ``p`` in
        ``ctx``): its source and depth are bound, so are the ``[]`` steps of ``p``; the base is the element
        (when the source ends with ``[]``, as in every valid tree)."""
        es = _src_of(e)
        bound = ctx.bound | frozenset(p.fan) | {es, _dkey(es)}
        base_src = es if es and es[-1] is WILD else None
        return _Ctx(bound, base_src, base_dict)

    def _scan_array(self, children: list, ctx: _Ctx) -> list:
        """Plans of the element nodes of an array node evaluated in ``ctx``; returns the work items of their
        subtrees (reversed, for the stack)."""
        todo: list = []
        for e in children:
            if not e.is_elem:
                continue
            p = _make_plan(_src_of(e)[:-1], ctx.bound, ctx.base_src)
            self.pplans[id(e)] = p
            self.outer[id(e)] = ctx.bound
            if p.mode == _SLOT:
                self.read.add(p.key)
            if e.kind == "object":
                sub = self._elem_ctx(e, p, ctx, True)
                todo.extend((c, sub) for c in e.children)
            elif e.kind == "array":
                todo.extend(reversed(self._scan_array(e.children, self._elem_ctx(e, p, ctx, False))))
        return todo[::-1]

    # -- the record stream ----------------------------------------------------------------
    def _stream_steps(self, p: _Plan) -> frozenset:
        """Indices (in ``p.steps``) of the ``[]`` steps of ``p`` that may iterate the record stream."""
        rp = self.stream
        if rp is None:
            return frozenset()
        n = len(rp)
        return frozenset(j - p.off for j in range(p.off, len(p.src))
                         if p.src[j] is WILD and j == n and p.src[:n] == rp)

    def _stream_final(self, p: _Plan) -> bool:
        """The value ``p`` resolves to may be the record stream itself."""
        return self.stream is not None and p.src == self.stream

    # -- pass 2: paths --------------------------------------------------------------------
    def _getter(self, steps: tuple, first_dict: bool) -> Callable[[Any], Any] | None:
        """``v -> the value at the key steps below v`` (``MISSING`` when a step finds no object or no such
        key); ``None`` for no steps (the identity). ``first_dict``: ``v`` is known to be an object."""
        n = len(steps)
        if n == 0:
            return None
        if n == 1:
            k = steps[0]
            if first_dict:
                def g1d(v: Any) -> Any:
                    return v.get(k, _M)
                return g1d

            def g1(v: Any) -> Any:
                return v.get(k, _M) if type(v) is dict else _M
            return g1
        if n == 2:
            k0, k1 = steps

            def g2(v: Any) -> Any:
                if type(v) is not dict:
                    return _M
                v = v.get(k0, _M)
                if type(v) is not dict:
                    return _M
                return v.get(k1, _M)
            return g2

        def gn(v: Any) -> Any:
            for k in steps:
                if type(v) is not dict:
                    return _M
                v = v.get(k, _M)
                if v is _M:
                    return _M
            return v
        return gn

    def _start(self, p: _Plan) -> Callable[[Any], Any] | None:
        """``base -> the value the walk of p starts from`` (``None``: the base itself)."""
        if p.mode == _BASE:
            return None
        if p.mode == _SLOT:
            s = self.slots[p.key]
            env = self.env

            def from_slot(base: Any) -> Any:
                return env[s]
            return from_slot
        rc = self.rc

        def from_root(base: Any) -> Any:
            return rc[0]
        return from_root

    def _path_fn(self, p: _Plan, steps: tuple, first_dict: bool) -> Callable[[Any], Any]:
        """``base -> the value at the start of p followed by the key steps`` (``MISSING``: none)."""
        start = self._start(p)
        g = self._getter(steps, first_dict and start is None)
        if start is None:
            return g if g is not None else _identity
        if g is None:
            return start

        def path(base: Any) -> Any:
            return g(start(base))
        return path

    def _walker(self, p: _Plan, j0: int, leaf: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
        """``(v, acc) -> stop``: the depth-first walk of ``p.steps[j0:]`` (``p.steps[j0]`` is a ``[]``)
        from ``v``, calling ``leaf(value, acc)`` for every branch in the reference order with the slots of
        the ``[]`` steps set; a true ``stop`` (a ``limit`` was reached) ends the walk."""
        nxt = leaf
        for j in range(len(p.steps) - 1, j0 - 1, -1):
            nxt = self._walk_step(p.steps[j], p.src[:p.off + j + 1], nxt)
        return nxt

    def _walk_step(self, seg: Any, prefix: tuple, nxt: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
        env = self.env
        if seg is WILD:
            s = self.slots.get(prefix)
            if s is None:
                def fan(v: Any, acc: Any) -> bool:
                    if type(v) is list:
                        for el in v:
                            if nxt(el, acc):
                                return True
                    return False
                return fan

            def fan_bound(v: Any, acc: Any) -> bool:
                if type(v) is list:
                    old = env[s]
                    for el in v:
                        env[s] = el
                        if nxt(el, acc):
                            env[s] = old
                            return True
                    env[s] = old
                return False
            return fan_bound

        def key_step(v: Any, acc: Any) -> bool:
            if type(v) is dict:
                x = v.get(seg, _M)
                if x is not _M:
                    return nxt(x, acc)
            return False
        return key_step

    def _gen_walker(self, p: _Plan, j0: int) -> Callable[[Any], Iterator[Any]]:
        """Stream-aware :meth:`_walker` as a generator: ``v -> branch values`` of ``p.steps[j0:]``; the
        slots of the ``[]`` steps are set while a value is yielded; a ``[]`` step that meets the record
        stream makes one pass over it (one record at a time; the unknown-keys flag is per record)."""
        sset = self._stream_steps(p)

        def leaf(v: Any) -> Iterator[Any]:
            yield v
        nxt = leaf
        for j in range(len(p.steps) - 1, j0 - 1, -1):
            nxt = self._gen_step(p.steps[j], p.src[:p.off + j + 1], j in sset, nxt)
        return nxt

    def _gen_step(self, seg: Any, prefix: tuple, over_stream: bool,
                  nxt: Callable[[Any], Iterator[Any]]) -> Callable[[Any], Iterator[Any]]:
        env, flag = self.env, self.flag
        if seg is not WILD:
            def gkey(v: Any) -> Iterator[Any]:
                if type(v) is dict:
                    x = v.get(seg, _M)
                    if x is not _M:
                        yield from nxt(x)
            return gkey
        s = self.slots.get(prefix)

        def gfan(v: Any) -> Iterator[Any]:
            if type(v) is list:
                old = env[s] if s is not None else None
                try:
                    for el in v:
                        if s is not None:
                            env[s] = el
                        yield from nxt(el)
                finally:
                    if s is not None:
                        env[s] = old
            elif over_stream and type(v) is _RecordStream:
                old = env[s] if s is not None else None
                saved = flag[0]
                try:
                    for idx, vals in v.blocks():
                        for pos, rec in zip(idx, vals):
                            if s is not None:
                                env[s] = rec
                            flag[0] = False
                            yield from nxt(rec)
                            if flag[0]:
                                v.mark(pos)
                finally:
                    if s is not None:
                        env[s] = old
                    flag[0] = saved
        return gfan

    # -- pass 2: objects ------------------------------------------------------------------
    def _builder(self, children: list, base_dict: bool, src: tuple | None, *,
                 any_value: bool = False) -> Callable[[Any], Any]:
        """``base -> the object of these children`` (the reference ``buildObj``); ``src`` = the source of
        the object (for the unknown-keys check), ``None`` for a synthetic group. ``any_value``: the
        function also takes non-objects and returns them unchanged (a type mismatch passes through)."""
        specs = tuple(sp for sp in (self._child_spec(c, base_dict) for c in children) if sp is not None)
        known_set = self.known.get(src, frozenset()) if (self.known is not None and src is not None) else None
        flag = self.flag
        if not specs:
            if known_set is None:
                build: Callable[[Any], Any] = lambda base: {}  # noqa: E731
            else:
                issup = known_set.issuperset

                def build(base: Any) -> Any:
                    if not issup(base):
                        flag[0] = True
                    return {}
        elif base_dict and self._unique(specs):
            return self._shape_builder(specs, known_set, self._loop_builder(specs, known_set), any_value)
        else:
            build = self._loop_builder(specs, known_set)
        if not any_value:
            return build

        def build_any(base: Any) -> Any:
            return build(base) if type(base) is dict else base
        return build_any

    @staticmethod
    def _unique(specs: tuple) -> bool:
        """No two children share an output key, no two natural children a source key (duplicates exist
        only after a flatten; the loop builder handles them: first position, last value)."""
        keys = [sp[0] for sp in specs]
        srcs = [sp[2] for sp in specs if sp[1] != 2]
        return len(set(keys)) == len(keys) and len(set(srcs)) == len(srcs)

    def _loop_builder(self, specs: tuple, known_set: frozenset | None) -> Callable[[Any], dict]:
        """The plain child-by-child ``buildObj``."""
        flag = self.flag
        issup = known_set.issuperset if known_set is not None else None
        direct = any(sp[1] != 2 for sp in specs)        # modes 0/1 exist only when the base is an object

        def build(base: Any) -> dict:
            if issup is not None and not issup(base):
                flag[0] = True
            o: dict = {}
            get = base.get if direct else None
            for key, mode, a, b in specs:
                if mode == 0:
                    x = get(a, _M)
                    if x is not _M:
                        o[key] = x
                elif mode == 1:
                    x = get(a, _M)
                    if x is not _M:
                        o[key] = b(x)
                else:
                    r = b(base)
                    if r is not _M:
                        o[key] = r
            return o
        return build

    def _shape_builder(self, specs: tuple, known_set: frozenset | None, fallback: Callable[[Any], dict],
                       any_value: bool) -> Callable[[Any], Any]:
        """``buildObj`` with a plan per source shape (the tuple of the object's keys): which children are
        present, whether the object has keys the mapping does not know, and the cheapest way to build the
        result -- the source object itself (same keys in tree order, nothing to change, all known: the set
        ``same``), a copy with some values transformed, or runs of present children copied with C-level
        ``zip``. At most :data:`_SHAPES_MAX` shapes per node are remembered; other shapes use the plain
        builder. ``any_value``: non-objects pass through unchanged (a type mismatch)."""
        flag = self.flag
        cache: dict[tuple, tuple] = {}
        same: set[tuple] = set()

        def make(shape: tuple) -> tuple:
            present_keys = set(shape)
            unknown = known_set is not None and not known_set.issuperset(shape)
            segs: list[tuple] = []
            subs: list[tuple] = []
            run_out: list = []
            run_src: list = []
            present: list = []
            renamed = False

            def flush() -> None:
                if run_out:
                    if len(run_out) == 1:
                        segs.append((0, run_out[0], run_src[0]))
                    else:
                        segs.append((2, tuple(run_out), itemgetter(*run_src)))
                    run_out.clear()
                    run_src.clear()
            for key, mode, a, b in specs:
                if mode == 2:
                    flush()
                    segs.append((1, key, b))
                elif a in present_keys:
                    run_out.append(key)
                    run_src.append(a)
                    present.append(a)
                    if key != a:
                        renamed = True
                    if mode == 1:
                        subs.append((key, b))
            flush()
            if not renamed and tuple(present) == shape:
                gens = tuple((s[1], s[2]) for s in segs if s[0] == 1)
                if not gens and not subs:
                    return (unknown, _SHAPE_SAME, (), ())
                first_gen = next((i for i, s in enumerate(segs) if s[0] == 1), len(segs))
                if all(s[0] == 1 for s in segs[first_gen:]):          # every generated child after the copied ones
                    return (unknown, _SHAPE_COPY, gens, tuple(subs))
            return (unknown, _SHAPE_SEGS, tuple(segs), tuple(subs))

        def build(base: Any) -> Any:
            if any_value and type(base) is not dict:
                return base
            shape = tuple(base)
            if shape in same:
                return base
            plan = cache.get(shape)
            if plan is None:
                if len(cache) >= _SHAPES_MAX:
                    return fallback(base)
                plan = cache[shape] = make(shape)
                if plan[1] == _SHAPE_SAME and not plan[0]:
                    same.add(shape)
            unknown, how, segs, subs = plan
            if unknown:
                flag[0] = True
            if how == _SHAPE_SAME:
                return base
            if how == _SHAPE_COPY:
                o = base.copy()
                for key, f in subs:
                    o[key] = f(o[key])
                for key, fn in segs:
                    r = fn(base)
                    if r is not _M:
                        o[key] = r
                return o
            o = {}
            for seg in segs:
                k = seg[0]
                if k == 0:
                    o[seg[1]] = base[seg[2]]
                elif k == 2:
                    o.update(zip(seg[1], seg[2](base)))
                else:
                    r = seg[2](base)
                    if r is not _M:
                        o[seg[1]] = r
            for key, f in subs:
                o[key] = f(o[key])
            return o
        return build

    def _child_spec(self, c: Any, base_dict: bool) -> tuple | None:
        """``(output key, mode, a, b)`` of one child of an object being built (``None``: never written):
        mode 0 = ``base[a]`` as it is; mode 1 = ``b(base[a])`` (``b`` never gives MISSING); mode 2 =
        ``b(base)`` (MISSING possible)."""
        key = c.key
        if c.computed:
            k = self.dkeys.get(id(c))
            if k is None:
                return None                              # never bound here: always MISSING
            s = self.slots[k]
            env = self.env
            return (key, 2, None, lambda base: env[s])
        if c.synthetic:
            return (key, 2, None, self._builder(c.children, base_dict, None))
        if c.kind == "array":
            parts = self._parts(c.children, base_dict)
            natural = self._natural_array(parts, base_dict)
            if natural is not None:
                if natural[1] is _identity:
                    return (key, 0, natural[0], None)    # value elements: the source value as it is
                return (key, 1, natural[0], natural[1])
            return (key, 2, None, self._array_of(parts))
        p = self.plans[id(c)]
        if base_dict and p.mode == _BASE and len(p.steps) == 1 and p.steps[0] is not WILD:
            if c.kind == "value":
                return (key, 0, p.steps[0], None)
            return (key, 1, p.steps[0], self._builder(c.children, True, p.src, any_value=True))
        return (key, 2, None, self._node_fn(c, base_dict))

    def _node_fn(self, n: Any, base_dict: bool) -> Callable[[Any], Any]:
        """``base -> the value of node n`` (the reference ``ev``; MISSING possible)."""
        if n.computed:
            k = self.dkeys.get(id(n))
            if k is None:
                return lambda base: _M
            s = self.slots[k]
            env = self.env
            return lambda base: env[s]
        if n.synthetic:
            return self._builder(n.children, base_dict, None)
        if n.kind == "array":
            return self._array_of(self._parts(n.children, base_dict))
        p = self.plans[id(n)]
        build = self._builder(n.children, True, p.src) if n.kind == "object" else None
        j0 = p.first_wild
        if j0 < 0:                                       # one value at most
            path = self._path_fn(p, p.steps, base_dict)
            if build is None:
                return path

            def one_obj(base: Any) -> Any:
                v = path(base)
                if type(v) is dict:
                    return build(v)
                return v
            return one_obj
        pre = self._path_fn(p, p.steps[:j0], base_dict)    # a gather once its first [] is reached
        if self._stream_steps(p):
            return self._stream_gather(p, j0, pre, build)
        lim = self.limit
        if build is None:
            if lim is None:
                def leaf(v: Any, out: list) -> bool:
                    out.append(v)
                    return False
            else:
                def leaf(v: Any, out: list) -> bool:
                    out.append(v)
                    return len(out) >= lim
        elif lim is None:
            def leaf(v: Any, out: list) -> bool:
                out.append(build(v) if type(v) is dict else v)
                return False
        else:
            def leaf(v: Any, out: list) -> bool:
                out.append(build(v) if type(v) is dict else v)
                return len(out) >= lim
        walk = self._walker(p, j0, leaf)

        def gather(base: Any) -> Any:
            v = pre(base)
            if v is _M:
                return _M                                # the path died before its first []
            out: list = []
            walk(v, out)
            return out
        return gather

    def _stream_gather(self, p: _Plan, j0: int, pre: Callable[[Any], Any],
                       build: Callable[[Any], dict] | None) -> Callable[[Any], Any]:
        """A gather whose walk may go over the record stream: a lazy array (one pass when written)."""
        walk = self._gen_walker(p, j0)

        def gather_stream(base: Any) -> Any:
            v = pre(base)
            if v is _M:
                return _M

            def factory(fmt: tuple) -> Iterator[list]:
                batch: list = []
                for x in walk(v):
                    batch.append(build(x) if build is not None and type(x) is dict else x)
                    if len(batch) >= _BATCH:
                        yield batch
                        batch = []
                if batch:
                    yield batch
            return _LazyList(self, factory)
        return gather_stream

    # -- pass 2: arrays -------------------------------------------------------------------
    def _elem_fn(self, e: Any) -> tuple[Callable[[Any], Any], bool]:
        """``el -> the evaluated element`` (the reference ``evElem``) and whether it can be MISSING."""
        es = _src_of(e)
        if e.kind == "object":
            return self._builder(e.children, True, es, any_value=True), False
        if e.kind == "array":
            parts = self._parts(e.children, False)
            arr = self._array_of(parts)
            never_missing = any(pt.plan.mode == _BASE and not pt.plan.steps for pt in parts)

            def elem_arr(el: Any) -> Any:
                return arr(el) if type(el) is list else el
            return elem_arr, not never_missing
        return _identity, False

    def _parts(self, children: list, base_dict: bool) -> list[_Part]:
        parts = []
        for e in children:
            if not e.is_elem:
                continue
            p = self.pplans[id(e)]
            es = _src_of(e)
            ef, ef_missing = self._elem_fn(e)
            part = _Part(e, p, ef, ef_missing, self.slots.get(es), self.slots.get(_dkey(es)), e.rec_key or None)
            if self._stream_final(p) or self._stream_steps(p):
                part.stream = True
                self._stream_part(part, base_dict, self.outer.get(id(e), frozenset()))
            else:
                part.fn = self._part_fn(part, base_dict)
            parts.append(part)
        return parts

    def _items_fn(self, part: _Part) -> Callable[[list, list], bool]:
        """``(arr, out) -> stop``: evaluate every element of one source array (pre-order with a recursive
        key) and append the results (the inner loop of the reference ``evArray``)."""
        ef, s_ek, s_d, rk, lim = part.ef, part.s_ek, part.s_d, part.rk, self.limit
        env = self.env
        if rk is None and s_ek is None and s_d is None and lim is None:
            if ef is _identity:
                def items_raw(arr: list, out: list) -> bool:
                    out.extend(arr)
                    return False
                return items_raw
            if part.ef_missing:
                def items_m(arr: list, out: list) -> bool:
                    for el in arr:
                        r = ef(el)
                        if r is not _M:
                            out.append(r)
                    return False
                return items_m

            def items_plain(arr: list, out: list) -> bool:
                out.extend([ef(el) for el in arr])      # no map(): C-level callbacks use the C stack
                return False
            return items_plain

        def items(arr: list, out: list) -> bool:
            old_ek = env[s_ek] if s_ek is not None else None
            old_d = env[s_d] if s_d is not None else None
            stop = False
            seq = preorder(arr, rk) if rk is not None else ((el, 1) for el in arr)
            for el, d in seq:
                if lim is not None and len(out) >= lim:
                    stop = True
                    break
                if s_ek is not None:
                    env[s_ek] = el
                if s_d is not None:
                    env[s_d] = d
                r = ef(el)
                if r is not _M:
                    out.append(r)
            if s_ek is not None:
                env[s_ek] = old_ek
            if s_d is not None:
                env[s_d] = old_d
            return stop
        return items

    def _part_fn(self, part: _Part, base_dict: bool) -> Callable[[Any, list, list], bool]:
        """``(base, out, state) -> stop`` for one element node: resolve its source array(s), evaluate the
        elements of every array branch into ``out``, keep the first non-array branch in ``state[1]`` and
        whether an array was seen in ``state[0]``."""
        p = part.plan
        items = self._items_fn(part)
        j0 = p.first_wild
        if j0 < 0:
            path = self._path_fn(p, p.steps, base_dict)

            def part_one(base: Any, out: list, st: list) -> bool:
                v = path(base)
                if type(v) is list:
                    st[0] = True
                    return items(v, out)
                if v is not _M and st[1] is _M:
                    st[1] = v
                return False
            return part_one
        pre = self._path_fn(p, p.steps[:j0], base_dict)

        def branch(v: Any, acc: tuple) -> bool:
            if type(v) is list:
                acc[1][0] = True
                return items(v, acc[0])
            if acc[1][1] is _M:
                acc[1][1] = v
            return False
        walk = self._walker(p, j0, branch)

        def part_gather(base: Any, out: list, st: list) -> bool:
            v = pre(base)
            if v is _M:
                return False
            return walk(v, (out, st))
        return part_gather

    def _stream_part(self, part: _Part, base_dict: bool, outer: frozenset) -> None:
        """Compile an element node whose source may be (or go over) the record stream: ``part.probe(base,
        state)`` looks for an array branch (it stops at the first one; it reads records only when the
        stream must be walked to find one); ``part.gen(base, fmt)`` yields the evaluated elements in
        batches (one per block of records).

        "Simple" parts reach the stream by key steps only -- its source array IS the record array, or its
        first ``[]`` step iterates it (an array merged into the root array): what one record contributes is
        ``part.rec_items(record, out)``, and when nothing outside the part is read (``outer``: the keys bound
        around the array node; the root value only for wrapped documents, outside the record array) the
        records can be evaluated in worker processes (``part.pool_ok``)."""
        p = part.plan
        j0 = p.first_wild
        if j0 < 0:
            path = self._path_fn(p, p.steps, base_dict)

            def branches(base: Any) -> Iterator[Any]:
                v = path(base)
                if v is not _M:
                    yield v
        else:
            pre = self._path_fn(p, p.steps[:j0], base_dict)
            walk = self._gen_walker(p, j0)

            def branches(base: Any) -> Iterator[Any]:
                v = pre(base)
                if v is not _M:
                    yield from walk(v)

        def probe(base: Any, st: list) -> None:
            it = branches(base)
            try:
                for v in it:
                    if type(v) is list or type(v) is _RecordStream:
                        st[0] = True
                        return
                    if st[1] is _M:
                        st[1] = v
            finally:
                it.close()

        items = self._items_fn(part)
        env, flag = self.env, self.flag
        at: Callable[[Any], Any] | None = None
        fan_slot: int | None = None
        if j0 < 0 and self._stream_final(p):
            at = path                                    # the source array is the record array
            rec_items = self._rec_items(part, None)
        elif j0 >= 0 and j0 in self._stream_steps(p):
            at = pre                                     # the first [] iterates the record array
            fan_slot = self.slots.get(p.src[:p.off + j0 + 1])
            rec_items = self._rec_items(part, j0)
        else:
            rec_items = None
        part.rec_items = rec_items
        part.index = len(self.stream_parts)
        self.stream_parts.append(part)
        part.pool_ok = at is not None and self._pool_safe(part, outer)
        saved_slots = tuple(s for s in (part.s_ek, part.s_d, fan_slot) if s is not None)

        def local(stream: _RecordStream) -> Iterator[list]:
            old = [env[s] for s in saved_slots]
            saved = flag[0]
            try:
                for idx, vals in stream.blocks():
                    out: list = []
                    for pos, rec in zip(idx, vals):
                        flag[0] = False
                        rec_items(rec, out)
                        if flag[0]:
                            stream.mark(pos)
                    if out:
                        yield out
            finally:
                for s, v in zip(saved_slots, old):
                    env[s] = v
                flag[0] = saved

        def gen(base: Any, fmt: tuple) -> Iterator[Any]:
            if at is not None:
                v = at(base)
                if type(v) is _RecordStream:
                    pooled = v.pooled(part.index, fmt) if part.pool_ok else None
                    yield from (pooled if pooled is not None else local(v))
                    return
            for v in branches(base):
                if type(v) is list:
                    out: list = []
                    items(v, out)
                    if out:
                        yield out
                elif type(v) is _RecordStream:            # reached through other [] steps
                    yield from self._stream_items(part, v)
        part.probe, part.gen = probe, gen

    def _stream_items(self, part: _Part, stream: _RecordStream) -> Iterator[list]:
        """The elements of a part whose branch IS the record stream, reached through other ``[]`` steps
        (a record path through an array: ``a[0].items``)."""
        flag = self.flag
        items = self._items_fn(part)
        saved = flag[0]
        try:
            for idx, vals in stream.blocks():
                out: list = []
                for pos, rec in zip(idx, vals):
                    flag[0] = False
                    items([rec], out)
                    if flag[0]:
                        stream.mark(pos)
                if out:
                    yield out
        finally:
            flag[0] = saved

    def _rec_items(self, part: _Part, j0: int | None) -> Callable[[Any, list], Any]:
        """``(record, out)``: append what one record contributes to a simple stream part -- the record as
        the element (``j0`` None) or the array(s) below the record reached by the rest of the source path
        (``j0`` = the ``[]`` step that iterates the records). Binding slots are set, not restored (the
        caller restores them once)."""
        env = self.env
        if j0 is None:
            ef, s_ek, s_d, rk = part.ef, part.s_ek, part.s_d, part.rk
            if rk is None and s_ek is None and s_d is None:
                if ef is _identity:
                    def rec_id(rec: Any, out: list) -> None:
                        out.append(rec)
                    return rec_id
                if part.ef_missing:
                    def rec_m(rec: Any, out: list) -> None:
                        r = ef(rec)
                        if r is not _M:
                            out.append(r)
                    return rec_m

                def rec_one(rec: Any, out: list) -> None:
                    out.append(ef(rec))
                return rec_one
            items = self._items_fn(part)

            def rec_el(rec: Any, out: list) -> None:
                items([rec], out)
            return rec_el
        p = part.plan
        s = self.slots.get(p.src[:p.off + j0 + 1])
        items = self._items_fn(part)

        def leaf(v: Any, out: list) -> bool:
            if type(v) is list:
                items(v, out)
            return False
        walk = self._walker(p, j0 + 1, leaf)
        if s is None:
            def rec_fan(rec: Any, out: list) -> None:
                walk(rec, out)
            return rec_fan

        def rec_fan_bound(rec: Any, out: list) -> None:
            env[s] = rec
            walk(rec, out)
        return rec_fan_bound

    def _pool_safe(self, part: _Part, outer: frozenset) -> bool:
        """The records of a simple stream part can be evaluated without the parent: nothing bound around the
        array node, no root reads except (wrapped documents) of wrapper parts outside the record array,
        no record stream reachable below."""
        if outer:
            return False
        rp = self.stream
        assert rp is not None
        plans = [part.plan]
        stack = list(part.node.children)
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            p = self.plans.get(id(n)) or self.pplans.get(id(n))
            if p is not None:
                plans.append(p)
        for p in plans[1:]:
            # a root read that reaches the record array or a container holding it (one path is a prefix
            # of the other) sees the stream here but only a placeholder in a worker
            if p.mode == _ROOT and p.src[:len(rp)] == rp[:len(p.src)]:
                return False
            if self._stream_final(p) or self._stream_steps(p):
                return False
        return True

    def _natural_array(self, parts: list[_Part], base_dict: bool) -> tuple[str, Callable[[Any], Any]] | None:
        """``(key, sub)`` when an array node is "natural" -- one element node whose source array is
        ``base[key]``, no binding read, no recursive key, no limit, not the record stream -- with
        ``sub(base[key])`` = the array value (``base[key]`` present)."""
        if len(parts) != 1 or not base_dict or self.limit is not None:
            return None
        part = parts[0]
        p = part.plan
        if (part.stream or p.mode != _BASE or len(p.steps) != 1 or p.steps[0] is WILD or part.rk is not None
                or part.s_ek is not None or part.s_d is not None):
            return None
        ef = part.ef
        if ef is _identity:
            return p.steps[0], _identity                # a list is copied as it is, so is anything else
        if part.ef_missing:
            def sub_m(x: Any) -> Any:
                if type(x) is list:
                    return [r for r in [ef(el) for el in x] if r is not _M]
                return x
            return p.steps[0], sub_m

        def sub(x: Any) -> Any:
            if type(x) is list:
                return [ef(el) for el in x]             # a comprehension: deep data needs no C stack
            return x
        return p.steps[0], sub

    def _array_of(self, parts: list[_Part]) -> Callable[[Any], Any]:
        """``base -> the value of an array node`` (the reference ``evArray``) over its compiled parts."""
        if any(pt.stream for pt in parts):
            return self._stream_array(parts)
        fns = tuple(pt.fn for pt in parts)
        if len(fns) == 1:
            f0 = fns[0]

            def array1(base: Any) -> Any:
                out: list = []
                st = [False, _M]
                f0(base, out, st)
                return out if st[0] else st[1]
            return array1

        def array(base: Any) -> Any:
            out: list = []
            st = [False, _M]
            for f in fns:
                if f(base, out, st):
                    return out                           # LIMIT reached (an array was seen)
            return out if st[0] else st[1]
        return array

    def _stream_array(self, parts: list[_Part]) -> Callable[[Any], Any]:
        """An array node with a part that may read the record stream: the parts that do not are evaluated
        at once, the probe decides between an array (a :class:`_LazyList` whose stream parts make their
        passes when it is written) and the reference fallback (the first non-array branch, or MISSING)."""
        records = any(pt.stream and self._stream_final(pt.plan) for pt in parts)

        def array_stream(base: Any) -> Any:
            st = [False, _M]
            segs: list = []
            for pt in parts:
                if pt.stream:
                    if not st[0]:
                        pt.probe(base, st)
                    segs.append(pt)
                else:
                    out: list = []
                    pt.fn(base, out, st)
                    segs.append(out)
            if not st[0]:
                return st[1]

            def factory(fmt: tuple) -> Iterator[Any]:
                for sg in segs:
                    if type(sg) is list:
                        if sg:
                            yield sg
                    else:
                        yield from sg.gen(base, fmt)
            return _LazyList(self, factory, records)
        return array_stream


def _mapping_module() -> Any:
    """``jsonreshape.mapping`` (imported lazily: mapping may import this module one day)."""
    from . import mapping
    return mapping


def _known_map(tree: Any) -> dict[tuple, frozenset]:
    """``source path -> keys known below it`` (``mapping.known_keys``; a bare root node: its own tree)."""
    if hasattr(tree, "root") and hasattr(tree, "iter_nodes"):
        return _mapping_module().known_keys(tree)
    out: dict[tuple, set] = {}
    stack = [_tree_root(tree)]
    while stack:
        n = stack.pop()
        if n.src is not None and n.src and n.src[-1] is not WILD:
            s = tuple(n.src)
            out.setdefault(s[:-1], set()).add(s[-1])
        stack.extend(n.children)
    return {k: frozenset(v) for k, v in out.items()}


class CompiledMapping:
    """A mapping tree compiled for fast evaluation (docs/ARCHITECTURE.md §13.2); results are identical to
    :class:`PlainEvaluator`. Not thread-safe (the binding slots live in the instance): one per thread.

    * ``limit``: the reference ``LIMIT`` (the preview uses 21; ``None``/``0`` = none);
    * ``check_unknown``: count objects built from source objects with keys the mapping does not know
      (``mapping.known_keys``: the original inferred tree and the current one) -- :attr:`unknown_seen`;
    * :attr:`root_elems`: element nodes of the root array (0 when the root is not an array node).
    """

    def __init__(self, tree: Any, *, limit: int | None = None, check_unknown: bool = False) -> None:
        self.tree = tree
        self.node = _tree_root(tree)
        self.limit = limit if limit else None
        known = _known_map(tree) if check_unknown else None
        self._c = _Compiler(self.node, limit=self.limit, known=known)
        self._fn = self._c.root_fn
        parts = self._c.root_parts
        self.root_elems = len(parts) if parts is not None else 0

    @property
    def unknown_seen(self) -> bool:
        """An object with keys unknown to the mapping was built since the last evaluation started."""
        return bool(self._c.flag[0])

    def evaluate(self, value: Any) -> Any:
        """The value of the tree's root on ``value`` (the reference ``ev(TREE, null)`` with
        ``ROOT = value``); :data:`MISSING` stays."""
        c = self._c
        c.rc[0] = value
        c.flag[0] = False
        with _RecursionRoom(c.depth):
            return self._fn(value)

    def transform_value(self, value: Any) -> Any:
        """The reference ``transform(value)``: the whole document value (record documents: the list of
        records); a MISSING result gives ``None``."""
        r = self.evaluate(value)
        return None if r is _M else r

    def transform_member(self, value: Any) -> Any:
        """A keyed document's member value through a keyed tree (whose root is the member value):
        :data:`MISSING` means the member is not written (:func:`apply` drops it)."""
        return self.evaluate(value)

    def record_items(self, record: Any, *, elem: int = 0) -> tuple[list, bool, Any]:
        """``items(E_elem, record)`` of §13.3 -- what root element node ``elem`` yields from ``record``
        alone (the reference with ``ROOT = [record]``) -- plus whether a source array was reached and the
        first non-array value reached (``MISSING``: none)."""
        parts = self._c.root_parts
        if not parts:
            raise ValueError("the mapping's root is not an array node with element nodes")
        part = parts[elem]
        c = self._c
        root = [record]
        c.rc[0] = root
        c.flag[0] = False
        out: list = []
        st = [False, _M]
        assert part.fn is not None
        with _RecursionRoom(c.depth):
            part.fn(root, out, st)
        return out, st[0], st[1]

    def transform_record(self, record: Any, *, elem: int = 0) -> list:
        """``items(E_elem, record)``: the output items one record contributes through root element node
        ``elem`` (record documents; §13.3)."""
        return self.record_items(record, elem=elem)[0]

    def transform_wrapper(self, wrapper: Any, records: Iterable[Any], record_path: Iterable[Any]) -> Any:
        """A wrapped document: ``wrapper`` (the parsed wrapper value) with ``records`` placed at
        ``record_path`` (``str`` keys, ``int`` indices), transformed as a whole (the preview and the
        tests; :func:`apply` streams the records instead)."""
        return self.transform_value(_with_records(wrapper, tuple(record_path), list(records)))


def _with_records(wrapper: Any, record_path: tuple, value: Any) -> Any:
    """A copy of ``wrapper`` with ``value`` at ``record_path`` (the containers along the path are copied,
    the caller's value is unchanged). ``ValueError`` when the path does not exist."""
    if not record_path:
        return value
    if type(wrapper) is dict:
        root: Any = dict(wrapper)
    elif type(wrapper) is list:
        root = list(wrapper)
    else:
        raise ValueError("the wrapper is not an object or an array")
    cur = root
    for i, seg in enumerate(record_path):
        if type(cur) is dict and isinstance(seg, str) and seg in cur:
            pass
        elif type(cur) is list and isinstance(seg, int) and not isinstance(seg, bool) and 0 <= seg < len(cur):
            pass
        else:
            raise ValueError(f"record path {list(record_path)!r} not found in the wrapper")
        if i == len(record_path) - 1:
            cur[seg] = value
        else:
            nxt = cur[seg]
            if type(nxt) is dict:
                nxt = dict(nxt)
            elif type(nxt) is list:
                nxt = list(nxt)
            else:
                raise ValueError(f"record path {list(record_path)!r} not found in the wrapper")
            cur[seg] = nxt
            cur = nxt
    return root


PREVIEW_LIMIT = 21


def preview(tree: Any, record_value: Any, *, limit: int | None = PREVIEW_LIMIT, records: bool | None = None) -> Any:
    """The designer's live preview (the prototype's ``renderPreview``): for a tree whose root is the
    record array (``records``; default: ``tree.root_kind == "records"``, or a root array node when the
    tree has no root kind) ``transform([record_value], limit)``, a one-element array result unwrapped;
    otherwise ``transform(record_value, limit)`` (keyed trees: a member value; single and wrapped
    documents: the value). ``limit`` caps every produced array (21: the first 20 items are shown and the
    21st says that more follow)."""
    root = _tree_root(tree)
    if records is None:
        kind = getattr(tree, "root_kind", None)
        records = kind == "records" if kind is not None else root.kind == "array"
    cm = CompiledMapping(tree, limit=limit)
    if not records:
        return cm.transform_value(record_value)
    out = cm.transform_value([record_value])
    if type(out) is list and len(out) == 1:
        return out[0]
    return out


# ======================================================================================
# apply: a mapping over a whole document, streamed
# ======================================================================================
RECORD_KINDS = ("array", "jsonl", "stream", "brace_list")
_COMMA_KINDS = frozenset({"array", "brace_list", "wrapped"})   # contiguous blocks: records + "," gaps
KiB = 1 << 10
MiB = 1 << 20
STREAM_BLOCK_BYTES = 2 * MiB      # records parsed at a time (parsed objects take ~10x their text)
FEED_BYTES = 1 * MiB              # Reformatter feed size
CHUNK_BYTES = 1 * MiB             # raw huge records are copied in pieces this big
WRITE_BUFFER = 1 * MiB
CHECK_EVERY = 1024                # records evaluated between cancellation checks
POOL_MIN_BYTES = 64 * MiB         # inputs smaller than this never use worker processes
POOL_MAX_WORKERS = 8
WORKER_MEMORY = 80 * MiB          # one worker process (~62-66 MB measured) and its share of the parent's blocks


@dataclass
class ApplyOptions:
    """How :func:`apply` writes its output.

    * ``jsonl``: JSON Lines (one compact item per line) instead of one JSON document;
    * ``indent``: ``2`` (default), ``4``, any int 0..16, ``"\\t"`` (``"tab"``) or ``None`` (compact) --
      JSON only (JSON Lines are always compact);
    * ``skip_huge``: records larger than ``Settings.large_node_bytes`` are skipped with a counted warning
      instead of stopping with ``engine.error.huge_record``;
    * ``workers``: worker processes for big inputs (``None``: automatic -- ``min(cpu_count - 1, 8)`` for
      inputs of at least 64 MiB whose mapping allows it, else none; ``1``: never).
    """

    jsonl: bool = False
    indent: int | str | None = 2
    skip_huge: bool = False
    workers: int | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        ind = self.indent
        if isinstance(ind, str):
            if ind.lower() == "tab":
                ind = "\t"
            if ind != "\t":
                raise UserInputError("engine.error.indent", value=self.indent)
        elif ind is not None:
            if isinstance(ind, bool) or not isinstance(ind, int) or not 0 <= ind <= 16:
                raise UserInputError("engine.error.indent", value=self.indent)
        self.indent = ind
        self.jsonl = bool(self.jsonl)
        self.skip_huge = bool(self.skip_huge)
        if self.workers is not None:
            if isinstance(self.workers, bool) or not isinstance(self.workers, int) or self.workers < 1:
                raise UserInputError("engine.error.workers", value=self.workers)


@dataclass
class ApplyReport:
    """What :func:`apply` did.

    ``records_in``: records of the input selection; ``items_out``: values written at the top level (the
    items of the output array / the members of a keyed output / 1 for a single value) plus the items of
    every streamed array of a wrapped document; ``passes``: passes made over the records (one per element
    node of the root array that reads records, plus extra ones for wrapper-level gathers and the probes
    of the reference's "no array reached" rule); ``unmapped_records``: records that contained fields the
    mapping does not know (``engine.warn.unmapped``); ``warnings``: localized texts (``warning_keys``:
    their keys)."""

    records_in: int = 0
    items_out: int = 0
    bytes_written: int = 0
    files: list[Path] = field(default_factory=list)
    elapsed: float = 0.0
    skipped_huge: int = 0
    unmapped_records: int = 0
    warnings: list[str] = field(default_factory=list)
    passes: int = 0
    warning_keys: list[str] = field(default_factory=list)
    workers: int = 1                 # processes that evaluated records (1: this one only)
    bytes_read: int = 0              # record bytes read over all passes

    @property
    def mb_per_s(self) -> float:
        """Input MB (10^6 bytes) per second over all passes (0 when nothing was read)."""
        return self.bytes_read / 1e6 / self.elapsed if self.elapsed > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_in": self.records_in, "items_out": self.items_out, "bytes_written": self.bytes_written,
            "files": [str(p) for p in self.files], "elapsed": self.elapsed, "skipped_huge": self.skipped_huge,
            "unmapped_records": self.unmapped_records, "warnings": list(self.warnings), "passes": self.passes,
            "warning_keys": list(self.warning_keys), "workers": self.workers, "bytes_read": self.bytes_read,
        }


def _tn(key: str, /, **params: Any) -> str:
    """:func:`t` with the singular variant ``<key>.one`` when ``count`` is 1."""
    count = params.get("count")
    if type(count) is int and count == 1 and has(key + ".one"):
        return t(key + ".one", **params)
    return t(key, **params)


def _pairs(data: bytes) -> array:
    a = array("Q")
    a.frombytes(data)
    return a


def _consecutive(indices: array) -> bool:
    return len(indices) == 0 or indices[-1] - indices[0] == len(indices) - 1


def _record_pattern(record_path: Iterable[Any]) -> tuple:
    """A record path (``str`` keys, ``int`` indices) as a source pattern (indices become :data:`WILD`)."""
    return tuple(WILD if isinstance(s, int) and not isinstance(s, bool) else s for s in record_path)


def _key_token(member: bytes, value_offset: int) -> bytes:
    """The raw key token (``"..."``, escapes as in the source) of a member ``"key": value``."""
    head = member[:value_offset].rstrip(b" \t\r\n")
    if head.endswith(b":"):
        head = head[:-1].rstrip(b" \t\r\n")
    return head


# -- the output layout ---------------------------------------------------------------------
# The output is laid out exactly like one core.Reformatter(indent) over the compact text (the layout of
# every JSON export): every element and member on its own line, indented by its nesting level, ": " after
# keys, empty containers as {} / []. Plain values come from codec.dumps(value, indent) (orjson's own 2-space
# layout, or the Reformatter for other indents) moved to their level: a JSON text never holds a raw newline
# inside a string, so prefixing every line is exact. Raw record text goes through a Reformatter.
def _unit(indent: int | str | None) -> bytes:
    if indent is None:
        return b""
    return b"\t" if indent == "\t" else b" " * int(indent)


def _shift(text: bytes, pad: bytes) -> bytes:
    """``text`` (a value laid out at level 0) moved to the level ``pad`` stands for."""
    return text.replace(b"\n", b"\n" + pad) if pad and b"\n" in text else text


def _format_items(items: list, indent: int | str | None, level: int, jsonl: bool) -> bytes:
    """The text of ``items`` as elements of an array whose bracket is at nesting ``level``: compact
    ``a,b,c``; indented ``<pad>a,\\n<pad>b`` (``pad`` = the indent of level + 1; without the line break
    before the first item); JSON Lines ``a\\nb\\n``. ``TypeError`` when an item holds lazy parts."""
    if not items:
        return b""
    if jsonl:
        # each orjson result is dropped right after its concatenation (a compact piece): ``join`` would keep
        # every result -- 4 KiB or more each -- until it ends, and a single document's array has no bound
        dumps = codec.dumps_transient
        return b"".join([dumps(x) + b"\n" for x in items])
    if indent is None:
        return codec.dumps_transient(items)[1:-1]
    text = codec.dumps_transient(items, indent=indent)[2:-2]   # "[\n" ... "\n]" -> the lines at level 1
    if level:
        pad = _unit(indent) * level
        text = pad + text.replace(b"\n", b"\n" + pad)
    return text


class _Unreadable(Exception):
    """Record ``k`` of a block cannot be decoded (deeper than the decoder allows)."""

    def __init__(self, k: int, detail: str) -> None:
        super().__init__(detail)
        self.k = k
        self.detail = detail


def _parse_block(data: bytes, spans: bytes, n: int, contiguous: bool) -> list:
    """The values of the ``n`` records of a block (``contiguous``: ``data`` is the records with their ``,``
    separators); one ``codec.loads`` call per block, record by record only to find an unreadable one."""
    if contiguous:
        blob = b"[" + data + b"]"
    else:
        sp = _pairs(spans)
        blob = b"[" + b",".join([data[sp[2 * k]:sp[2 * k + 1]] for k in range(n)]) + b"]"
    try:
        vals = codec.loads(blob)
        if type(vals) is list and len(vals) == n:
            return vals
    except (JsonDecodeError, ValueError, RecursionError):
        pass
    sp = _pairs(spans)
    out = []
    for k in range(n):
        try:
            out.append(codec.loads(data[sp[2 * k]:sp[2 * k + 1]]))
        except (JsonDecodeError, ValueError, RecursionError) as exc:
            raise _Unreadable(k, str(exc)) from None
    return out


class _Printer:
    """Writes a mapped value -- plain JSON values and the lazy parts of :func:`apply` (``_LazyList``,
    ``_RecordStream``) -- to the output file in the layout of ``indent`` (JSON Lines: compact). A container
    holding lazy parts makes ``codec.dumps`` fail with ``TypeError`` and is written member by member."""

    def __init__(self, write: Callable[[bytes], Any], run: _Run) -> None:
        self.w = write
        self.run = run
        self.jsonl = run.opts.jsonl
        self.indent = None if self.jsonl else run.opts.indent
        self.unit = _unit(self.indent)
        self.colon = b":" if self.indent is None else b": "
        self.items = 0               # items of lazy arrays written

    def nl(self, level: int) -> bytes:
        return b"" if self.indent is None else b"\n" + self.unit * level

    def value(self, v: Any, level: int = 0) -> None:
        try:
            text = codec.dumps_transient(v, indent=self.indent)     # written at once
        except TypeError:
            self._struct(v, level)
            return
        self.w(_shift(text, self.unit * level) if level else text)

    def _struct(self, v: Any, level: int) -> None:
        tv = type(v)
        w = self.w
        if tv is _LazyList:
            self.lazy(v, level)
        elif tv is _RecordStream:
            self.run.write_raw_records(self, level)
        elif tv is dict:
            sep = b"{"
            for k, x in v.items():
                w(sep + self.nl(level + 1) + codec.dumps_transient(k) + self.colon)
                sep = b","
                self.value(x, level + 1)
            w(b"{}" if sep == b"{" else self.nl(level) + b"}")
        elif tv is list:
            sep = b"["
            for x in v:
                w(sep + self.nl(level + 1))
                sep = b","
                self.value(x, level + 1)
            w(b"[]" if sep == b"[" else self.nl(level) + b"]")
        else:                                            # pragma: no cover - not produced by the evaluator
            raise TypeError(f"cannot write {tv.__name__}")

    def lazy(self, lz: _LazyList, level: int) -> None:
        w = self.w
        indent = self.indent
        first = True
        brk = b"\n" if indent is not None else b""
        for batch in lz.batches((indent, level, False)):
            if type(batch) is _Prebuilt:
                text, n = batch.text, batch.count
            else:
                n = len(batch)
                try:
                    text = _format_items(batch, indent, level, False)
                except TypeError:
                    for x in batch:
                        w((b"[" if first else b",") + self.nl(level + 1))
                        first = False
                        self.value(x, level + 1)
                    self.items += n
                    continue
            if n:
                w((b"[" if first else b",") + brk)
                w(text)
                first = False
                self.items += n
        w(b"[]" if first else self.nl(level) + b"]")

    def lines(self, v: Any) -> int:
        """JSON Lines: the items of an array value (lazy or not) one per line, any other value on one line.
        Returns the number of lines."""
        w = self.w
        if type(v) is _LazyList:
            n = 0
            for batch in v.batches((None, 0, True)):
                if type(batch) is _Prebuilt:
                    w(batch.text)
                    n += batch.count
                    continue
                self._lines(batch)
                n += len(batch)
            self.items += n
            return n
        if type(v) is list:
            self._lines(v)
            return len(v)
        self.value(v)
        w(b"\n")
        return 1

    def _lines(self, items: list) -> None:
        try:
            self.w(_format_items(items, None, 0, True))
        except TypeError:
            for x in items:
                self.value(x)
                self.w(b"\n")


def _pass_bytes(doc: Any, src: Any, count: int) -> int:
    """Bytes of record data one pass reads (the progress unit; exact for all records and step-1 ranges,
    proportional otherwise)."""
    if getattr(doc, "edited", False):
        full = max(0, int(doc.effective_bytes()))
        if src is None:
            return full
        total = doc.record_count
        return int(full * min(1.0, count / total)) if total > 0 and count > 0 else 0
    if src is None:
        return max(0, doc.data_size - doc.info.data_start)
    if count <= 0 or doc.record_count <= 0:
        return 0
    if isinstance(src, range) and src.step == 1:
        return doc.record_span(src.stop - 1)[1] - doc.record_span(src.start)[0]
    first = doc.record_span(0)[0]
    last = doc.record_span(doc.record_count - 1)[1]
    return int((last - first) * count / doc.record_count)


def _normalize_source(doc: Any, source: Any) -> tuple[Any, int]:
    """``(selection for iter_blocks, number of records)``; lists, tuples, arrays and ranges are checked."""
    total = doc.record_count
    if source is None:
        return None, total
    if isinstance(source, range):
        if len(source) and (min(source) < 0 or max(source) >= total):
            bad = min(source) if min(source) < 0 else max(source)
            raise UserInputError("writer.error.bad_record", index=bad, count=total)
        return source, len(source)
    if hasattr(source, "__len__") and hasattr(source, "__getitem__") \
            and not isinstance(source, (list, tuple, array, str, bytes, bytearray, dict)):
        return source, len(source)                       # a RecordSet
    arr = array("Q")
    for i in source:
        if isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= total:
            raise UserInputError("writer.error.bad_record", index=i, count=total)
        arr.append(i)
    return arr, len(arr)


# -- worker processes (docs/ARCHITECTURE.md §13.2) -----------------------------------------------
_WORKER: dict[str, Any] = {}


def _pool_init(snapshot: bytes, stream: bytes, root_text: bytes | None) -> None:
    """Worker process start: compile the mapping exactly as the parent did (same tree, same record
    pattern), the root value for wrapper reads. Automatic garbage collection is off (the values built per
    block are acyclic and freed by reference counting)."""
    import gc
    gc.disable()
    mapping = _mapping_module()
    tree = mapping.MappingTree.restore(snapshot)
    comp = _Compiler(tree.root, known=_known_map(tree), stream=tuple(codec.loads(stream)))
    comp.rc[0] = codec.loads(root_text) if root_text is not None else None
    _WORKER["comp"] = comp


def _pool_block(part_index: int, data: bytes, spans: bytes, indices: bytes, contiguous: bool,
                fmt: tuple) -> tuple:
    """One block of records through simple stream part ``part_index``: ``("ok", text, items, marked
    positions)`` (``text`` laid out by ``fmt``) or ``("unreadable", position, detail)``."""
    comp: _Compiler = _WORKER["comp"]
    part = comp.stream_parts[part_index]
    idx = array("Q")
    idx.frombytes(indices)
    try:
        vals = _parse_block(data, spans, len(idx), contiguous)
    except _Unreadable as exc:
        return ("unreadable", int(idx[exc.k]), exc.detail)
    flag = comp.flag
    rec_items = part.rec_items
    assert rec_items is not None
    out: list = []
    marks = array("Q")
    with _RecursionRoom(comp.depth):
        for pos, rec in zip(idx, vals):
            flag[0] = False
            rec_items(rec, out)
            if flag[0]:
                marks.append(pos)
    return ("ok", _format_items(out, *fmt), len(out), marks.tobytes())


def auto_workers(input_bytes: int, settings: Any) -> int:
    """Worker processes :func:`apply` uses by itself: none below :data:`POOL_MIN_BYTES` of input, else
    ``min(cpu_count - 1, 8)``, and no more than half of ``Settings.memory_limit_bytes`` allows at
    :data:`WORKER_MEMORY` per worker (measured peak ~62-66 MB each: the interpreter, the modules and one parsed
    block; the parent's blocks in flight grow with the workers) -- 3 with the default 512 MiB budget, which keeps
    the whole export (parent included) under 300 MB: on the 2 GB benchmark `apply` 264 MB and `flatten` 299 MB at
    97-129 MB/s, while 4 workers summed to 321-355 MB for at most 20 % more speed (README §14)."""
    if input_bytes < POOL_MIN_BYTES:
        return 1
    by_cpu = min((os.cpu_count() or 1) - 1, POOL_MAX_WORKERS)
    by_memory = (int(settings.memory_limit_bytes) // 2) // WORKER_MEMORY
    return max(1, min(by_cpu, by_memory))


def _spawn_safe() -> bool:
    """Spawned worker processes import the parent's main module again (as ``__mp_main__``): automatic
    worker processes are used only when that cannot run the caller's program a second time -- a package
    ``__main__`` (``python -m jsonreshape``, ``python -m pytest``; multiprocessing skips those), the
    console-script launchers (``__main__`` of a zip), jsonreshape's own modules (no top-level actions) and
    an interactive interpreter (no main file). A plain script must protect its ``main()`` with
    ``if __name__ == "__main__":`` and ask for workers explicitly (``ApplyOptions.workers``). A frozen
    executable (PyInstaller) is safe: its entry scripts call ``multiprocessing.freeze_support()`` first, and
    a spawned worker is the same executable started in its worker mode."""
    if getattr(sys, "frozen", False):
        return True
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    name = getattr(spec, "name", None)
    if name is not None:
        return name == "__main__" or name.endswith(".__main__") or name.startswith("jsonreshape.")
    return getattr(main, "__file__", None) is None


class _Pool:
    """``ProcessPoolExecutor`` (spawn) of :func:`_pool_block` workers; the parent reads the blocks, keeps
    at most ``inflight`` of them in the workers and writes the results in order."""

    def __init__(self, workers: int, payload: tuple, inflight: int) -> None:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor

        self.workers = workers
        self.inflight = inflight
        self.ex = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                      initializer=_pool_init, initargs=payload)

    def close(self, abort: bool = False) -> None:
        self.ex.shutdown(wait=not abort, cancel_futures=True)


class _Run:
    """State of one :func:`apply` call; also the record source of the :class:`_RecordStream` (every
    :meth:`blocks` call is one pass over the selected records)."""

    def __init__(self, doc: Any, tree: Any, opts: ApplyOptions, source: Any, progress: Progress | None,
                 cancel: CancelToken | None, out: Path) -> None:
        self.doc = doc
        self.tree = tree
        self.root = _tree_root(tree)
        self.opts = opts
        self.progress = progress
        self.cancel = cancel
        self.out = out
        self.settings = doc.settings
        self.large = self.settings.large_node_bytes
        self.src, self.count = _normalize_source(doc, source)
        self.unit = max(1, _pass_bytes(doc, self.src, self.count))
        self.passes = 0
        self.bytes_read = 0
        self.skipped: set[int] = set()
        self.marks: bytearray | None = None
        self.warnings: dict[str, dict[str, Any]] = {}
        self.known = _known_map(tree)
        self.streamed = 0
        self.comma = doc.kind in _COMMA_KINDS and not doc.member_records
        self.stream_pattern: tuple = ()
        self.root_text: bytes | None = None   # wrapped: the wrapper text with the records placeholder
        workers = opts.workers
        if workers is None:
            workers = auto_workers(self.unit, self.settings) if _spawn_safe() else 1
        self.pool_workers = max(1, workers)
        self.workers = 1                     # worker processes really used
        self._pool: _Pool | None = None
        self._pool_failed = False

    # -- warnings / progress --------------------------------------------------------------
    def warn(self, key: str, **params: Any) -> None:
        self.warnings.setdefault(key, {}).update(params)

    def _begin_pass(self) -> None:
        self.passes += 1
        p = self.progress
        if p is not None:
            if p.total is None or p.total < self.unit * self.passes:
                p.set_total(self.unit * self.passes)
            if self.passes > 1:
                p.set_message(t("engine.progress.pass", name=self.out.name, n=self.passes))

    def _advance(self, nbytes: int) -> None:
        self.bytes_read += nbytes
        p = self.progress
        if p is not None:
            p.update(min(self.bytes_read, self.unit * self.passes))

    # -- the record source ------------------------------------------------------------------
    def _huge(self, blk: Any) -> int:
        s, e = blk.span(0)
        pos = int(blk.indices[0])
        if not self.opts.skip_huge:
            raise _mapping_module().MappingError("engine.error.huge_record", record=pos, size=format_bytes(e - s),
                                                 limit=format_bytes(self.large))
        self.skipped.add(pos)
        return e - s

    def _unreadable(self, pos: int, detail: str) -> Exception:
        self.doc.ensure_unchanged()           # bytes that cannot be decoded: a changed file first
        return _mapping_module().MappingError("engine.error.unreadable", record=pos, detail=detail)

    def iter_blocks(self) -> Iterator[Any]:
        return self.doc.iter_blocks(self.src, block_bytes=STREAM_BLOCK_BYTES, cancel=self.cancel)

    def contiguous(self, blk: Any) -> bool:
        return self.comma and blk.contiguous and _consecutive(blk.indices)

    def blocks(self) -> Iterator[tuple[array, list]]:
        """One pass over the selected records: ``(positions, values)`` in slices of :data:`CHECK_EVERY`
        records (a cancellation check before each); huge records are skipped (``skip_huge``) or stop the
        run."""
        self._begin_pass()
        cancel = self.cancel
        for blk in self.iter_blocks():
            check(cancel)
            if blk.huge:
                self._advance(self._huge(blk))
                continue
            idx = blk.indices
            try:
                vals = _parse_block(blk.data, blk.spans, len(idx), self.contiguous(blk))
            except _Unreadable as exc:
                raise self._unreadable(int(idx[exc.k]), exc.detail) from None
            if len(idx) <= CHECK_EVERY:
                yield idx, vals
            else:
                for i in range(0, len(idx), CHECK_EVERY):
                    check(cancel)
                    yield idx[i:i + CHECK_EVERY], vals[i:i + CHECK_EVERY]
            self._advance(len(blk.data))

    def member_blocks(self) -> Iterator[tuple[array, list, list]]:
        """Member records (keyed documents): ``(positions, raw key tokens, values)`` per block."""
        self._begin_pass()
        doc = self.doc
        for blk in self.iter_blocks():
            check(self.cancel)
            if blk.huge:
                self._advance(self._huge(blk))
                continue
            sp = _pairs(blk.spans)
            data = blk.data
            heads = []
            values = []
            for k in range(len(blk.indices)):
                buf = data[sp[2 * k]:sp[2 * k + 1]]
                _, vo = doc.split_member(buf)
                heads.append(_key_token(buf, vo))
                values.append(buf[vo:])
            blob = b"[" + b",".join(values) + b"]"
            vals = None
            try:
                vals = codec.loads(blob)
            except (JsonDecodeError, ValueError, RecursionError):
                pass
            if type(vals) is not list or len(vals) != len(values):
                vals = []
                for k, raw in enumerate(values):
                    try:
                        vals.append(codec.loads(raw))
                    except (JsonDecodeError, ValueError, RecursionError) as exc:
                        raise self._unreadable(int(blk.indices[k]), str(exc)) from None
            yield blk.indices, heads, vals
            self._advance(len(data))

    def mark(self, pos: int) -> None:
        m = self.marks
        if m is None:
            m = self.marks = bytearray((self.doc.record_count + 7) >> 3)
        m[pos >> 3] |= 1 << (pos & 7)

    # -- worker processes --------------------------------------------------------------------
    def pooled(self, part_index: int, fmt: tuple) -> Iterator[Any] | None:
        """One pass of simple stream part ``part_index`` in worker processes (``None``: not used)."""
        if self.pool_workers <= 1 or self._pool_failed:
            return None
        if self._pool is None:
            try:
                tree = self.tree if hasattr(self.tree, "snapshot") else _mapping_module().MappingTree(self.root)
                payload = (tree.snapshot(), codec.dumps(list(self.stream_pattern)), self.root_text)
                limit = self.settings.memory_limit_bytes
                inflight = max(2, min(2 * self.pool_workers, limit // (16 * STREAM_BLOCK_BYTES)))
                self._pool = _Pool(self.pool_workers, payload, inflight)
            except (OSError, RuntimeError, ValueError, ImportError) as exc:   # pragma: no cover - platform
                log.warning("worker processes could not be started, one thread is used: %s", exc)
                self._pool_failed = True
                return None
            self.workers = self.pool_workers
        return self._pool_pass(self._pool, part_index, fmt)

    def _pool_pass(self, pool: _Pool, part_index: int, fmt: tuple) -> Iterator[_Prebuilt]:
        from collections import deque

        self._begin_pass()
        cancel = self.cancel
        waiting: deque = deque()
        try:
            for blk in self.iter_blocks():
                check(cancel)
                if blk.huge:
                    self._advance(self._huge(blk))
                    continue
                fut = pool.ex.submit(_pool_block, part_index, blk.data, blk.spans, blk.indices.tobytes(),
                                     self.contiguous(blk), fmt)
                waiting.append((fut, len(blk.data)))
                if len(waiting) >= pool.inflight:
                    yield self._take(*waiting.popleft())
            while waiting:
                yield self._take(*waiting.popleft())
        finally:
            for fut, _ in waiting:
                fut.cancel()

    def _take(self, fut: Any, nbytes: int) -> _Prebuilt:
        from concurrent.futures import TimeoutError as FutureTimeout

        while True:
            try:
                res = fut.result(timeout=0.05)
                break
            except FutureTimeout:
                check(self.cancel)
        if res[0] != "ok":
            raise self._unreadable(res[1], res[2])
        _, text, count, marks = res
        if marks:
            for pos in _pairs(marks):
                self.mark(pos)
        self._advance(nbytes)
        return _Prebuilt(text, count)

    def close(self, abort: bool = False) -> None:
        if self._pool is not None:
            self._pool.close(abort)
            self._pool = None

    # -- the record stream as a raw value -------------------------------------------------------
    def write_raw_records(self, pr: _Printer, level: int) -> None:
        """The record stream written as a raw value: ``[`` + every record's own text (laid out) + ``]``
        (one pass; huge records are copied in pieces)."""
        self._begin_pass()
        w = pr.w
        sep = b"["
        fh = None
        try:
            for blk in self.iter_blocks():
                check(self.cancel)
                if blk.huge:
                    s, e = blk.span(0)
                    if fh is None:
                        fh = self.doc.open_data()
                    w(sep + pr.nl(level + 1))
                    sep = b","
                    self._raw_value(pr, self._chunks(fh, s, e), level + 1)
                    self._advance(e - s)
                    continue
                sp = _pairs(blk.spans)
                data = blk.data
                for k in range(len(blk.indices)):
                    w(sep + pr.nl(level + 1))
                    sep = b","
                    self._raw_value(pr, (data[sp[2 * k]:sp[2 * k + 1]],), level + 1)
                self._advance(len(data))
        finally:
            if fh is not None:
                fh.close()
        w(b"[]" if sep == b"[" else pr.nl(level) + b"]")

    @staticmethod
    def _raw_value(pr: _Printer, chunks: Iterable[bytes], level: int) -> None:
        rf = core.Reformatter(pr.indent, base_level=level) if pr.indent is not None else core.Reformatter(None)
        for c in chunks:
            for i in range(0, len(c), FEED_BYTES):
                out = rf.feed(c[i:i + FEED_BYTES])
                if out:
                    pr.w(out)
        out = rf.finish()
        if out:
            pr.w(out)

    def _chunks(self, fh: Any, start: int, end: int) -> Iterator[bytes]:
        pos = start
        while pos < end:
            check(self.cancel)
            stop = min(end, pos + CHUNK_BYTES)
            fh.seek(pos)
            yield fh.read(stop - pos)
            pos = stop


def _popcount(m: bytearray) -> int:
    total = 0
    for i in range(0, len(m), MiB):
        total += int.from_bytes(m[i:i + MiB], "little").bit_count()
    return total


def _pin(doc: Any, source: Any) -> Any:
    """``doc.pinned()`` (§12.3: the whole run reads one overlay state; record numbers are its positions)
    and the stale-scope check of ``source``."""
    pinned = getattr(doc, "pinned", None)
    if callable(pinned):
        doc = pinned()
    gen = getattr(source, "gen", None) if source is not None else None
    if gen is not None and gen != getattr(doc, "gen", 0):
        raise EditError("edits.error.stale_scope")
    return doc


def _check_target(doc: Any, out: Path) -> None:
    """The output is never the source (nor its prepared copy), nor a folder; its folder exists."""
    target = normalize_path(out)
    for p in (doc.source_path, doc.data_path):
        if target == normalize_path(p):
            raise UserInputError("writer.error.same_file", path=str(out))
    lp = long_path(out)
    if os.path.isdir(lp):
        raise UserInputError("fileio.error.is_directory", path=str(out))
    if os.path.exists(lp):
        try:
            same = os.path.samefile(lp, long_path(doc.source_path))
        except OSError:
            same = False
        if same:
            raise UserInputError("writer.error.same_file", path=str(out))
    if not os.path.isdir(long_path(out.parent)):
        raise UserInputError("writer.error.no_dir", path=str(out.parent))


def _ensure_index(doc: Any, progress: Progress | None, cancel: CancelToken | None) -> None:
    if doc.index_complete:
        return
    sub = None
    if progress is not None:
        total = max(0, doc.data_size - doc.info.data_start)
        progress.set_message(t("engine.progress.index", name=doc.source_path.name))
        progress.set_total(total)
        sub = Progress(total, callback=lambda info: progress.update(info.done), min_interval=progress.min_interval)
    doc.build_index(progress=sub, cancel=cancel)


def tree_root_kind(tree: Any) -> str:
    """The root kind of a mapping (``MappingTree.root_kind``; a bare root node: ``records`` when it is an
    array node, else ``value``)."""
    kind = getattr(tree, "root_kind", None)
    if kind:
        return str(kind)
    return "records" if _tree_root(tree).kind == "array" else "value"


def check_kind(doc: Any, tree: Any) -> None:
    """``MappingError("engine.error.kind_mismatch")`` when the mapping was not made for this kind of
    document (a records mapping needs an array / JSON Lines / stream / brace list document, a wrapped
    mapping a wrapped document, a keyed mapping a keyed document, a value mapping a single document)."""
    mapping = _mapping_module()
    want = mapping.root_kind_for(doc.kind)
    have = tree_root_kind(tree)
    root = _tree_root(tree)
    if have != want or (want == "records" and root.kind != "array"):
        shown = t(f"engine.root_kind.{have}") if has(f"engine.root_kind.{have}") else have
        raise mapping.MappingError("engine.error.kind_mismatch", mapping=shown, input=doc.info.label)


def _write(run: _Run, value: Any) -> int:
    """Write ``value`` (JSON or JSON Lines) atomically; returns the number of top-level items."""
    with atomic_write(run.out, "wb", buffering=WRITE_BUFFER) as f:
        pr = _Printer(f.write, run)
        if run.opts.jsonl:
            n = pr.lines(value)
        else:
            pr.value(value)
            if type(value) is _LazyList:
                n = pr.items
            elif type(value) is list:
                n = len(value)
            else:
                n = 1
        check(run.cancel)
        run.doc.ensure_unchanged()
    run.streamed = pr.items
    return n


def _apply_records(run: _Run) -> int:
    """Record documents: the root array node over the record stream (§13.3): one pass per root element
    node that reads records; the reference fallback when no record reaches an array."""
    stream = _RecordStream(run)
    run.stream_pattern = ()
    comp = _Compiler(run.root, known=run.known, stream=())
    comp.rc[0] = stream
    comp.flag[0] = False
    value = comp.root_fn(stream)
    if type(value) is not _LazyList:
        run.warn("engine.warn.not_array")
        if value is _M:
            value = None
    return _write(run, value)


_RECORDS_TOKEN = "\u0000jsonreshape-records-"


def _wrapper_value(run: _Run, stream: _RecordStream, record_path: tuple) -> Any:
    """The parsed wrapper with the record stream at the record path (the record array is never read)."""
    parts = run.doc.wrapper_parts(cancel=run.cancel)
    if parts is None:
        raise _mapping_module().MappingError("engine.error.partial_wrapper")
    prefix, suffix = parts
    token = _RECORDS_TOKEN + os.urandom(8).hex()
    run.root_text = prefix + codec.dumps_transient(token) + suffix
    w = codec.loads(run.root_text)
    holder: Any = None
    cur = w
    for seg in record_path:
        holder = cur
        if type(cur) is dict and isinstance(seg, str) and seg in cur:
            cur = cur[seg]
        elif type(cur) is list and isinstance(seg, int) and not isinstance(seg, bool) and 0 <= seg < len(cur):
            cur = cur[seg]
        else:
            holder = None
            break
    if holder is not None and cur == token:
        holder[record_path[-1]] = stream
    else:                                                # a later duplicate key hides the record array
        run.warn("engine.warn.records_hidden")
    return w


def _find_lazy(v: Any, records: bool) -> _LazyList | None:
    """The first lazy array in ``v`` (depth first, in output order); ``records``: only one that is the
    record array (made by an array node whose element source is the record array)."""
    stack = [v]
    while stack:
        x = stack.pop()
        tx = type(x)
        if tx is _LazyList:
            if not records or x.records:
                return x
        elif tx is dict:
            stack.extend(reversed(list(x.values())))
        elif tx is list:
            stack.extend(reversed(x))
    return None


def _apply_wrapped(run: _Run) -> int:
    """Wrapped documents: the wrapper value with the record stream in place of the record array; the
    record array streams its items, wrapper-level gathers make their own passes, records see the wrapper
    as the root (broadcasts of wrapper fields)."""
    doc = run.doc
    mapping = _mapping_module()
    if doc.member_records:
        raise mapping.MappingError("engine.error.member_container")
    rp = tuple(doc.record_path or ())
    pattern = _record_pattern(rp)
    run.stream_pattern = pattern
    tree_rs = getattr(run.tree, "record_src", None)
    if tree_rs is not None and tuple(tree_rs) != pattern:
        run.warn("engine.warn.record_path", mapping=mapping.path_label(tuple(tree_rs), root=""),
                 input=mapping.path_label(pattern, root=""))
    stream = _RecordStream(run)
    w = _wrapper_value(run, stream, rp)
    comp = _Compiler(run.root, known=run.known, stream=pattern)
    comp.rc[0] = w
    comp.flag[0] = False
    value = comp.root_fn(w)
    if comp.flag[0]:
        run.warn("engine.warn.unmapped_wrapper")
    if value is _M:
        value = None
    if run.opts.jsonl:
        lz = _find_lazy(value, True) or _find_lazy(value, False)
        if lz is not None:
            if lz is not value:
                run.warn("engine.warn.jsonl_wrapper")
            value = lz
    return _write(run, value)


def _apply_keyed(run: _Run) -> int:
    """Keyed documents: every member value transformed by the tree, the member keys kept in order (as
    their raw tokens); members whose value is MISSING are not written."""
    comp = _Compiler(run.root, known=run.known)
    fn, rc, flag = comp.root_fn, comp.rc, comp.flag
    jsonl = run.opts.jsonl
    indent = None if jsonl else run.opts.indent
    pad = _unit(indent)
    colon = b":" if indent is None else b": "
    nl1 = b"" if indent is None else b"\n" + pad
    dumps = codec.dumps_transient       # every result is concatenated into a piece at once
    n = 0
    with atomic_write(run.out, "wb", buffering=WRITE_BUFFER) as f:
        w = f.write
        sep = b"{"
        for idx, heads, vals in run.member_blocks():
            pieces = []
            for k, (pos, head, v) in enumerate(zip(idx, heads, vals)):
                if not k & 1023:
                    check(run.cancel)
                rc[0] = v
                flag[0] = False
                r = fn(v)
                if flag[0]:
                    run.mark(pos)
                if r is _M:
                    continue
                if jsonl:
                    pieces.append(b"{" + head + b":" + dumps(r) + b"}\n")
                else:
                    pieces.append(sep + nl1 + head + colon + _shift(dumps(r, indent=indent), pad))
                    sep = b","
                n += 1
            if pieces:
                w(b"".join(pieces))
        if not jsonl:
            w(b"{}" if sep == b"{" else (b"\n}" if indent is not None else b"}"))
        check(run.cancel)
        run.doc.ensure_unchanged()
    return n


def _apply_single(run: _Run) -> int:
    """Single documents: the value (at most ``Settings.large_node_bytes``) transformed whole."""
    doc = run.doc
    comp = _Compiler(run.root, known=run.known)
    value: Any = None
    if doc.record_count:
        s, e = doc.record_span(0)
        if e - s > run.large:
            raise _mapping_module().MappingError("engine.error.too_large", size=format_bytes(e - s),
                                                 limit=format_bytes(run.large))
        run._begin_pass()
        value = doc.record(0)
        run._advance(e - s)
    comp.rc[0] = value
    comp.flag[0] = False
    r = comp.root_fn(value)
    if comp.flag[0]:
        run.mark(0)
    return _write(run, None if r is _M else r)


def apply(doc: Any, tree: Any, out_path: Any, opts: ApplyOptions | None = None, source: Any = None, *,
          progress: Progress | None = None, cancel: CancelToken | None = None) -> ApplyReport:
    """Apply the mapping ``tree`` (a ``MappingTree``) to the whole document ``doc`` and write the result to
    ``out_path`` (JSON or JSON Lines, see :class:`ApplyOptions`), streaming (§13.3).

    ``source`` selects records (a RecordSet, a ``range``, a list of positions) in that order; ``None`` =
    all. The document is pinned at entry (an edited document is mapped with its edits); the output file
    appears atomically after ``doc.ensure_unchanged()``; the source is refused as the target. Progress is
    in bytes over all passes. Errors: ``MappingError`` (``engine.error.kind_mismatch`` -- the mapping was
    made for another kind of document; ``engine.error.huge_record`` -- a record larger than
    ``Settings.large_node_bytes`` without ``skip_huge``; ``engine.error.too_large`` -- a single document
    over that size; ``engine.error.member_container`` -- a wrapped document whose records are object
    members; ``engine.error.unreadable`` -- a record nested deeper than the decoder allows),
    ``UserInputError`` for a bad target, ``Cancelled``, ``FileChangedError``."""
    if opts is None:
        opts = ApplyOptions()
    elif not isinstance(opts, ApplyOptions):
        raise TypeError("opts must be an ApplyOptions instance")
    opts.validate()
    started = time.perf_counter()
    doc = _pin(doc, source)
    out = Path(os.path.abspath(os.fspath(out_path)))
    _check_target(doc, out)
    _ensure_index(doc, progress, cancel)
    check_kind(doc, tree)
    from concurrent.futures.process import BrokenProcessPool

    try:
        return _apply_run(doc, tree, opts, source, progress, cancel, out, started)
    except BrokenProcessPool as exc:
        if opts.workers == 1:                            # pragma: no cover - no pool without workers
            raise
        log.warning("the worker processes failed (%s): the mapping is applied again in one process", exc)
        return _apply_run(doc, tree, replace(opts, workers=1), source, progress, cancel, out, started)


def _apply_run(doc: Any, tree: Any, opts: ApplyOptions, source: Any, progress: Progress | None,
               cancel: CancelToken | None, out: Path, started: float) -> ApplyReport:
    run = _Run(doc, tree, opts, source, progress, cancel, out)
    if progress is not None:
        progress.set_message(t("engine.progress.apply", name=out.name))
        progress.set_total(run.unit)
        progress.update(0)
        progress.restart_clock()
    kind = doc.kind
    if doc.error is not None:
        run.warn("engine.warn.partial", count=run.count)
    if kind == "jsonl" and doc.broken_count:
        run.warn("engine.warn.broken_lines", count=doc.broken_count)
    ok = False
    try:
        with _RecursionRoom(_tree_depth(run.root)):      # compiling, evaluating and writing (lazy arrays)
            if kind in RECORD_KINDS:
                items = _apply_records(run)
            elif kind == "wrapped":
                items = _apply_wrapped(run)
                items = run.streamed or items            # wrapped: the items of the streamed arrays
            elif kind == "keyed":
                items = _apply_keyed(run)
            else:
                items = _apply_single(run)
        ok = True
    finally:
        run.close(abort=not ok)
    if run.skipped:
        run.warn("engine.warn.skipped_huge", count=len(run.skipped), limit=format_bytes(run.large))
    unmapped = _popcount(run.marks) if run.marks is not None else 0
    if unmapped:
        run.warn("engine.warn.unmapped", count=unmapped)
    if run.passes > 1:
        run.warn("engine.warn.passes", count=run.passes)
    if progress is not None:
        if progress.total is None or progress.done > progress.total:
            progress.set_total(progress.done)
        progress.finish()
    keys = list(run.warnings)
    texts = [_tn(k, **run.warnings[k]) for k in keys]
    try:
        size = os.stat(long_path(out)).st_size
    except OSError:  # pragma: no cover - just written
        size = 0
    report = ApplyReport(records_in=run.count, items_out=items, bytes_written=size, files=[out],
                         elapsed=time.perf_counter() - started, skipped_huge=len(run.skipped),
                         unmapped_records=unmapped, warnings=texts, passes=run.passes, warning_keys=keys,
                         workers=run.workers, bytes_read=run.bytes_read)
    log.info("mapping applied to %s (%s): %d records -> %d items, %d bytes in %.2f s (%d pass(es), %d "
             "process(es))%s", doc.source_path.name, kind, report.records_in, report.items_out, size,
             report.elapsed, run.passes, run.workers, f"; warnings: {', '.join(keys)}" if keys else "")
    return report


