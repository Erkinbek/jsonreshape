r"""Mapping tree, designer operations, undo/redo and mapping files (docs/ARCHITECTURE.md §13.1).

The reference is the engine of ``json-tuzilma.html`` (``/*ENGINE-START*/ … /*ENGINE-END*/``,
HTML lines 219-536). This module is a node-for-node port of its tree model and of every edit
operation -- ``inferInto``/``fromAcc``/``setOpen`` (:func:`infer_tree`), ``moveNode``,
``zoneFor``, ``deleteNode``, ``unwrapNode``, ``addGroup``, ``renameNode``, ``findRec``/
``canFlatten``/``flattenRec``, ``undo``/``resetTree`` (:class:`Designer`), the badge logic
``relation``/``isMoved``/``rowHTML`` (:func:`badges`) -- with the same guards, effects, toast
texts (Uzbek through :func:`jsonreshape.i18n.t`) and node numbering. Evaluation lives in
``engine.py``. Verified against the reference with the golden files in ``tests/golden`` and
with differential tests against a line-by-line transliteration.

Node model (:class:`MapNode`, field order = the reference ``mk`` order)
=====================================================================
* ``src`` is the absolute source path from the document root: ``str`` keys and :data:`WILD`
  (``None``) for an array step; ``None`` for synthetic groups and computed nodes. It never
  changes after creation (moves and renames keep it). An element node's ``src`` is its source
  array's ``src`` + ``(WILD,)``; after a merge one array node holds element nodes of unrelated
  sources.
* ``ctx`` of a computed ``_depth`` node is the ``src`` of the flattened element node (the
  reference stores the compiled ``SEP``-joined string; a tuple here, a segment list in files).
* ``types`` uses the reference names ``object array string number boolean null`` in that
  order; ``samples`` are the reference's UI strings: ``JSON.stringify`` of up to three
  distinct non-null scalars, cut to 27 UTF-16 code units + ``…`` when longer than 28 units
  (a cut may leave a lone high surrogate, exactly like the JavaScript ``slice``).

Root kinds (:data:`ROOT_KINDS`)
===============================
* ``records`` (array, jsonl, stream, brace_list documents): the reference's root array of
  records: root ``kind "array"``, ``src ()``, one element node ``src (WILD,)`` ("har bir
  yozuv") inferred from the record values.
* ``value`` (single documents): the root describes the document value itself (any kind).
* ``wrapped``: the reference's object root: the parsed wrapper ``W`` is inferred with the
  record array (at ``record_path``; integer segments select array elements and become
  ``WILD`` in ``src``) replaced by the records, which flow into that array's element
  accumulator in their order. ``MappingTree.record_src`` is the record array's ``src``.
* ``keyed`` (keyed documents, one member per record, possibly millions): the root node is the
  member VALUE -- the values of all members are inferred into ONE root accumulator (count =
  members), ``src`` paths are relative to a member value, and the root row reads "har bir
  a'zo". The engine transforms each member value with the tree and keeps the member keys in
  order. Deviation from the reference, which builds one node per member key (A15 1c): member
  values now share one structure, so their key order is normalized to the first-seen order
  like records (the identity of keys and values is kept).

Deliberate deviations from the reference (README §15)
=====================================================
* Object keys keep their insertion order (inference walks ``dict`` order; JavaScript moves
  integer-like keys such as ``"10"`` first). Golden tests feed JavaScript-ordered input.
* Numbers keep their exact text (``samples`` show ``1.10``, ``1e400``,
  ``12345678901234567890`` as written; the reference shows JavaScript doubles).
* Undo keeps ``undo_limit`` steps (default ``Settings.undo_limit``, >= 200; the reference
  keeps 100) and there is redo: undone steps can be redone until the next applied operation.
  ``reset`` is undoable (as in the reference) and clears the redo stack.
* ``src`` uses ``None`` for the array step (``"[]"`` and ``"\u0002"`` are ordinary keys);
  compiled-path ``SEP`` collisions of the reference do not exist.
* The default name of a new group is localized (``yangi_guruh`` in Uzbek, ``new_group`` in
  English); collisions get ``_2``, ``_3`` … as in the reference.
* Bounded trees (the schema limits of ``schema.py``): an object accumulator that would get more
  than ``max_keys`` (1000) distinct child keys, a container deeper than ``max_depth`` (128)
  path segments, or more than ``max_nodes`` (20000) accumulators in one inference turns into
  an *opaque* value node (``kind "value"`` whose ``types`` contain ``object``/``array``): its
  values are copied whole, so nothing is dropped, and ``MappingTree.truncated`` is set. The
  limits only change trees beyond them; ``None`` disables a limit (the golden tests do).
* A refused operation returns ``OpResult(applied=False)`` with a localized ``reason`` (the
  reference refuses silently); the GUI shows it for disabled actions.
* ``Designer.move(..., zone=None)`` (no drop zone) is a refusal; like the reference, any zone
  other than ``inside``/``after`` behaves as ``before``.

Designer
========
Every operation is one undo step, applied only when the reference's guards pass. Snapshots
are immutable nested tuples of every node field (``open`` included) that share unchanged
subtrees between steps, so a long history of a big tree costs little memory; the whole-tree
``bytes`` form is :meth:`MappingTree.snapshot`. Node ids come from one counter that never
goes back (``MappingTree.next_id``), so ids survive undo/redo/reset like the reference
``uid``. Operations accept a :class:`MapNode` of the tree, a node id or a REF (see
:meth:`MappingTree.find`); a node object of an earlier state (before an undo) is resolved by
its id. ``flatten`` re-infers the flattened elements over ``population`` (a callable
returning an iterable of the records' values -- the sample or the full data): the work is
split into :meth:`Designer.plan_flatten` (read-only, may run in a worker thread, progress in
values, cancellable) and the mutation (:meth:`Designer.flatten` with ``plan=``), which refuses
a plan when the tree changed meanwhile.

For the GUI: :meth:`Designer.actions` (the prototype's row-button rules with the reason of a
disabled one), :meth:`Designer.can_drag`, :meth:`Designer.paste_zone` (cut/paste: inside when
allowed, else after), :meth:`Designer.move_up`/:meth:`Designer.move_down` (Alt+Up/Down),
:meth:`Designer.expand_all`/:meth:`Designer.collapse_all` (no undo step, like the prototype),
and the row texts :func:`node_label`, :func:`type_chips`, :func:`node_meta`,
:func:`sample_display`, :func:`badges`, :func:`stats_text`.

The default tree of a document (docs/ARCHITECTURE.md §13.0 "Tree source") is made by
:func:`infer_document` -- the one way the CLI's ``flatten`` and the GUI's structure builder
infer it: the records at the positions of ``schema.sample_positions`` (:func:`sample_source`)
or every record, read in blocks by :func:`document_values` (huge records left out and
counted), wrapped documents with their wrapper (:func:`document_wrapper`), keyed documents
with the member values, a single document with its value (at most ``large_node_bytes``).
The same functions give a designer's ``population`` (the records the tree was made of).

Mapping files
=============
UTF-8 JSON ``{"version": 1, "input": {...}, "root_kind": "...", "tree": {...},
"known_paths": [...], "designer": {"orig": {...}}}`` written atomically, one node per line
(indented by depth up to 40 levels; written without recursion, any depth). Nodes use ``key
kind src`` + the flags only when set (``is_root is_elem synthetic rec_key computed ctx``) + the
UI fields ``types count pcount samples open`` + ``children``; ``src``/``ctx`` are absolute
segment arrays with ``null`` for the array step; ids are not stored (assigned in pre-order on
load: the tree first, then ``designer.orig``). ``root_kind`` is optional (derived from
``input.kind``, else from the root's kind). ``known_paths`` lists the known source paths that
neither stored tree holds (the reader adds the paths of both trees: the same set, without
repeating every ``src``; absent = unknown). :func:`load_mapping` validates everything and
raises :class:`MappingError` (``mapping.error.*``) naming the JSON pointer of the bad node;
files larger than 128 MiB are refused (a data file picked by mistake is never read whole).
Trees deeper than the JSON decoders support are read with an iterative parser. Absolute
``src`` paths make the size of a tree grow with depth x nodes; the default inference limits
keep it bounded.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import codec
from .errors import UserInputError
from .i18n import get_language, has, t
from .tasks import CancelToken, Progress, check, format_bytes

try:  # the C encoder behind json.dumps(..., ensure_ascii=False)
    from json.encoder import c_encode_basestring as _encode_basestring
except ImportError:  # pragma: no cover - CPython always has it
    from json.encoder import py_encode_basestring as _encode_basestring

log = logging.getLogger(__name__)

__all__ = [
    "JS_WS",
    "MAPPING_VERSION",
    "MAX_DEPTH",
    "MAX_KEYS",
    "MAX_NODES",
    "NODE_KINDS",
    "ROOT_KINDS",
    "TYPE_GLYPHS",
    "TYPE_ORDER",
    "WILD",
    "ZONES",
    "Designer",
    "DocumentTree",
    "FlattenPlan",
    "MapNode",
    "MappingError",
    "MappingTree",
    "OpResult",
    "badges",
    "can_flatten",
    "count_fields",
    "document_values",
    "document_wrapper",
    "dumps_mapping",
    "enclosing_ctx",
    "find_rec",
    "infer_document",
    "infer_tree",
    "is_moved",
    "js_trim",
    "known_keys",
    "load_mapping",
    "make_input_info",
    "node_label",
    "node_meta",
    "node_to_dict",
    "parse_ref",
    "path_label",
    "ref_of",
    "relation",
    "root_kind_for",
    "sample_display",
    "sample_source",
    "sample_text",
    "save_mapping",
    "stats_text",
    "tree_from_dict",
    "type_chips",
    "type_of",
]

# ----------------------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------------------
WILD = None
"""The array-step segment in ``MapNode.src`` (JSON ``null`` in a mapping file; ``"[]"`` is a real key)."""

TYPE_ORDER: tuple[str, ...] = ("object", "array", "string", "number", "boolean", "null")
NODE_KINDS: tuple[str, ...] = ("object", "array", "value")
ROOT_KINDS: tuple[str, ...] = ("records", "value", "wrapped", "keyed")
ZONES: tuple[str, ...] = ("before", "inside", "after")
MAPPING_VERSION = 1

# the schema limits (schema.MAX_KEYS / MAX_DEPTH / MAX_FIELDS; a test keeps them equal)
MAX_KEYS = 1000
MAX_DEPTH = 128
MAX_NODES = 20000

#: ECMAScript ``String.prototype.trim`` set (WhiteSpace + LineTerminator). Not ``str.strip()``:
#: U+001C-U+001F, U+0085 and U+200B stay, U+FEFF goes.
JS_WS = "".join(map(chr, (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
                         0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF)))

#: type chip glyphs of the prototype (language independent)
TYPE_GLYPHS: dict[str, str] = {"object": "{ }", "array": "[ ]", "string": "abc", "number": "123",
                               "boolean": "t/f", "null": "null"}
_TYPE_ROLES = {"object": "object", "array": "array", "string": "string", "number": "number",
               "boolean": "bool", "null": "null"}

_RECORD_DOC_KINDS = ("array", "jsonl", "stream", "brace_list")
_SAMPLES = 3            # samples kept per node
_SAMPLE_UNITS = 28      # a sample longer than this (UTF-16 units) is cut ...
_SAMPLE_CUT = 27        # ... to this many units + "…"
_OPEN_SMALL = 160       # setOpen: trees with fewer nodes open 6 levels
MAX_FILE_BYTES = 128 * 1024 * 1024
_CHECK_EVERY = 0x3FFF   # inference steps between cancellation checks
_SEEN_LIMIT = 64        # string heads remembered per node while its samples fill

# type bits of the inference accumulator (TYPE_ORDER order)
_OBJ, _ARR, _STR, _NUM, _BOOL, _NULL = 1, 2, 4, 8, 16, 32
_TYPE_BITS = ((_OBJ, "object"), (_ARR, "array"), (_STR, "string"), (_NUM, "number"), (_BOOL, "boolean"),
              (_NULL, "null"))


class MappingError(UserInputError):
    """A mapping problem the user can fix (bad mapping file, mapping that does not fit the input)."""

    key = "mapping.error.invalid"


# ----------------------------------------------------------------------------------
# node model
# ----------------------------------------------------------------------------------
@dataclass(eq=False)
class MapNode:
    """One node of the mapping tree (the reference ``mk`` node; field order = snapshot order)."""

    id: int
    key: str = ""
    kind: str = "value"  # "object" | "array" | "value"
    src: tuple | None = ()  # absolute source path (str keys and WILD); None for synthetic / computed
    children: list[MapNode] = field(default_factory=list)  # array nodes: element nodes only (>= 1)
    types: tuple = ()
    count: int = 0
    pcount: int | None = None
    samples: tuple = ()
    synthetic: bool = False
    rec_key: str | None = None
    computed: str | None = None  # "depth"
    ctx: tuple | None = None  # computed nodes: src of the flattened element node
    open: bool = False
    is_elem: bool = False
    is_root: bool = False

    def __repr__(self) -> str:  # short: a node holds its whole subtree
        what = "root" if self.is_root else "elem" if self.is_elem else repr(self.key)
        src = path_label(self.src, root="") if self.src is not None else None
        return f"MapNode(id={self.id}, {what}, {self.kind}, src={src})"


@dataclass(eq=False)
class MappingTree:
    """A mapping tree: the root node plus tree-level information.

    * ``next_id``: the id the next new node gets (the reference ``uid + 1``; ``0`` = unknown,
      the :class:`Designer` computes it);
    * ``root_kind``: one of :data:`ROOT_KINDS`;
    * ``known_paths``: the ``src`` of every node of the originally inferred tree (``None`` when
      unknown) -- the engine's unmapped-fields count, see :func:`known_keys`;
    * ``record_src``: wrapped trees: the ``src`` of the record array;
    * ``orig``: the originally inferred tree when this tree was edited (mapping files keep it
      so "Asl holat" works after reopening); ``None`` = this tree is the original;
    * ``truncated``: an inference limit turned some nodes opaque.

    Node lookups use an index rebuilt lazily; code that changes the structure of the nodes
    directly (instead of through :class:`Designer`) must call :meth:`touch` afterwards.
    """

    root: MapNode
    next_id: int = 0
    root_kind: str = "records"
    known_paths: frozenset | None = None
    record_src: tuple | None = None
    orig: MappingTree | None = None
    truncated: bool = False

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if name == "root":
            object.__setattr__(self, "_idx", None)

    def touch(self) -> None:
        """Forget the node index (call after changing the structure directly)."""
        object.__setattr__(self, "_idx", None)

    def _index(self) -> tuple[dict[int, MapNode], dict[int, tuple[MapNode, MapNode]]]:
        idx = self.__dict__.get("_idx")
        if idx is None:
            by_id: dict[int, MapNode] = {}
            parents: dict[int, tuple[MapNode, MapNode]] = {}
            stack = [self.root]
            while stack:
                n = stack.pop()
                by_id.setdefault(n.id, n)
                for c in n.children:
                    parents[id(c)] = (c, n)
                stack.extend(reversed(n.children))
            idx = (by_id, parents)
            object.__setattr__(self, "_idx", idx)
        return idx

    # -- traversal ------------------------------------------------------------------
    def iter_nodes(self) -> Iterator[MapNode]:
        """Every node in pre-order (iterative)."""
        stack = [self.root]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed(n.children))

    def walk(self) -> Iterator[tuple[MapNode, MapNode | None, int]]:
        """``(node, parent, depth)`` in pre-order (iterative)."""
        stack: list[tuple[MapNode, MapNode | None, int]] = [(self.root, None, 0)]
        while stack:
            n, p, d = stack.pop()
            yield n, p, d
            stack.extend((c, n, d + 1) for c in reversed(n.children))

    def contains(self, node: MapNode) -> bool:
        """``node`` (this object) is part of the tree."""
        if node is self.root:
            return True
        entry = self._index()[1].get(id(node))
        return entry is not None and entry[0] is node

    def find(self, ref: Any) -> MapNode | None:
        """The node for ``ref``: a :class:`MapNode` (itself, or the node with its id after an
        undo), a node id, ``{"id": n}``, a REF list of steps (a ``str`` step = the first
        non-element child with that key, an ``int`` step = the child at that index) or a REF
        string (``""`` root, ``"[]"`` the root element, ``"[].contact.address.city"``,
        ``"orders[1]"`` the second element node of ``orders``; see :func:`parse_ref`).
        ``None`` when there is no such node (or the REF string is malformed)."""
        if isinstance(ref, MapNode):
            if self.contains(ref):
                return ref
            ref = ref.id
        if isinstance(ref, bool):
            return None
        if isinstance(ref, int):
            return self._index()[0].get(ref)
        if isinstance(ref, dict):
            i = ref.get("id")
            return self._index()[0].get(i) if isinstance(i, int) and not isinstance(i, bool) else None
        if isinstance(ref, str):
            try:
                steps: Sequence[Any] = parse_ref(ref)
            except ValueError:
                return None
        elif isinstance(ref, (list, tuple)):
            steps = ref
        else:
            return None
        n = self.root
        for s in steps:
            if isinstance(s, bool):
                return None
            if isinstance(s, int):
                if not 0 <= s < len(n.children):
                    return None
                n = n.children[s]
            elif isinstance(s, str):
                nxt = next((c for c in n.children if not c.is_elem and c.key == s), None)
                if nxt is None:
                    return None
                n = nxt
            else:
                return None
        return n

    def parent(self, node: Any) -> MapNode | None:
        """The parent of ``node`` (``None`` for the root or an unknown node)."""
        n = node if isinstance(node, MapNode) and self.contains(node) else self.find(node)
        if n is None or n is self.root:
            return None
        entry = self._index()[1].get(id(n))
        return entry[1] if entry is not None and entry[0] is n else None

    def node_count(self) -> int:
        return sum(1 for _ in self.iter_nodes())

    # -- snapshots ------------------------------------------------------------------
    def snapshot(self) -> bytes:
        """The whole tree (ids, ``open`` and every UI field included) as immutable bytes."""
        return codec.dumps(self._snapshot_doc())

    def _snapshot_doc(self) -> dict[str, Any]:
        # pre-order node records; ``src`` as [length shared with the parent's src, *rest] so that
        # deep trees stay linear in size (absolute paths would be quadratic)
        nodes = []
        for n, p, _d in self.walk():
            nodes.append([n.id, n.key, n.kind, _segs_rel(n.src, p.src if p is not None else None), len(n.children),
                          list(n.types), n.count, n.pcount, list(n.samples), n.synthetic, n.rec_key, n.computed,
                          _segs_out(n.ctx), n.open, n.is_elem, n.is_root])
        return {"v": 1, "next_id": self.next_id, "root_kind": self.root_kind,
                "record_src": _segs_out(self.record_src), "truncated": self.truncated,
                "known_paths": _known_out(_known_extra(self.known_paths, self.root)),
                "orig": self.orig._snapshot_doc() if self.orig is not None else None, "nodes": nodes}

    @classmethod
    def restore(cls, data: bytes | bytearray | str) -> MappingTree:
        """The tree of a :meth:`snapshot` (new node objects, same ids and fields)."""
        doc = codec.loads(data)
        if not isinstance(doc, dict) or doc.get("v") != 1:
            raise ValueError("not a mapping tree snapshot")
        return cls._from_snapshot_doc(doc)

    @classmethod
    def _from_snapshot_doc(cls, doc: dict[str, Any]) -> MappingTree:
        root: MapNode | None = None
        stack: list[list[Any]] = []
        for rec in doc["nodes"]:
            parent = stack[-1][0] if stack else None
            n = MapNode(id=rec[0], key=rec[1], kind=rec[2], src=_segs_abs(rec[3], parent.src if parent else None),
                        types=tuple(rec[5]), count=rec[6], pcount=rec[7], samples=tuple(rec[8]), synthetic=rec[9],
                        rec_key=rec[10], computed=rec[11], ctx=_segs_in(rec[12]), open=rec[13], is_elem=rec[14],
                        is_root=rec[15])
            if root is None:
                root = n
            else:
                if not stack:
                    raise ValueError("snapshot: more nodes than the child counts allow")
                top = stack[-1]
                top[0].children.append(n)
                top[1] -= 1
            if rec[4]:
                stack.append([n, rec[4]])
            while stack and stack[-1][1] == 0:
                stack.pop()
        if root is None or stack:
            raise ValueError("snapshot: incomplete node list")
        known = doc.get("known_paths")
        orig = doc.get("orig")
        return cls(root, next_id=doc.get("next_id", 0), root_kind=doc.get("root_kind", "records"),
                   known_paths=_known_in(known, root) if known is not None else None,
                   record_src=_segs_in(doc.get("record_src")), truncated=bool(doc.get("truncated")),
                   orig=cls._from_snapshot_doc(orig) if orig is not None else None)


def _segs_rel(src: Sequence | None, base: Sequence | None) -> list | None:
    """``src`` as ``[k, *src[k:]]`` with ``k`` = the length of the prefix shared with ``base``."""
    if src is None:
        return None
    k = 0
    if base:
        limit = min(len(src), len(base))
        while k < limit and src[k] == base[k] and (src[k] is None) == (base[k] is None):
            k += 1
    return [k, *src[k:]]


def _segs_abs(rel: list | None, base: Sequence | None) -> tuple | None:
    if rel is None:
        return None
    k = rel[0]
    return tuple(base[:k]) + tuple(rel[1:]) if k else tuple(rel[1:])


def _tree_paths(*roots: MapNode | None) -> set[tuple]:
    out: set[tuple] = set()
    for root in roots:
        if root is not None:
            out.update(tuple(n.src) for n in _iter_pre(root) if n.src is not None)
    return out


def _known_extra(known: frozenset | None, *roots: MapNode | None) -> frozenset | None:
    """The known paths that none of ``roots`` holds (the rest is implied by the trees)."""
    if known is None:
        return None
    return frozenset(known - _tree_paths(*roots))


def _known_in(extra: Sequence, *roots: MapNode | None) -> frozenset:
    return frozenset({_segs_in(p) for p in extra} | _tree_paths(*roots))


def _segs_out(segs: Sequence | None) -> list | None:
    return None if segs is None else list(segs)


def _segs_in(segs: Sequence | None) -> tuple | None:
    return None if segs is None else tuple(segs)


def _path_sort_key(path: tuple) -> tuple:
    return (len(path), tuple((0, "") if s is WILD else (1, s) for s in path))


def _known_out(known: frozenset | None) -> list | None:
    if known is None:
        return None
    return [list(p) for p in sorted(known, key=_path_sort_key)]


# ----------------------------------------------------------------------------------
# JavaScript helpers
# ----------------------------------------------------------------------------------
def js_trim(s: str) -> str:
    """``String.prototype.trim`` (the ECMAScript whitespace set, not ``str.strip()``)."""
    return s.strip(JS_WS)


def type_of(v: Any) -> str:
    """The reference ``typeOf``: ``object array string number boolean null``."""
    if v is None:
        return "null"
    tv = type(v)
    if tv is dict:
        return "object"
    if tv is list:
        return "array"
    if tv is str:
        return "string"
    if tv is bool:
        return "boolean"
    if tv is int or tv is float:
        return "number"
    base = _base_type(v)
    return {dict: "object", list: "array", str: "string", bool: "boolean", int: "number"}[base]


def _base_type(v: Any) -> type:
    """Map JSON-like values (subclasses included: JsonFloat, JsonInt, Decimal, tuples) to the
    base types the inference dispatches on."""
    if v is None:
        return type(None)
    if isinstance(v, bool):
        return bool
    if isinstance(v, (int, float, Decimal)):
        return int
    if isinstance(v, str):
        return str
    if isinstance(v, dict):
        return dict
    if isinstance(v, (list, tuple)):
        return list
    raise TypeError(f"not a JSON value: {type(v).__name__}")


def path_label(src: Sequence | None, *, root: str | None = None) -> str:
    """The reference ``pathLabel``: ``[].contact.address.city``, ``orders[]``; an empty path
    is ``(ildiz)`` (localized; ``root`` overrides it)."""
    s = ""
    for seg in src or ():
        if seg is WILD:
            s += "[]"
        else:
            s += ("." if s else "") + seg
    if s:
        return s
    return t("mapping.label.root_path") if root is None else root


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _json_str(s: str) -> str:
    """``JSON.stringify`` of a string (lone surrogates escaped like ES2019)."""
    text = _encode_basestring(s)
    if not text.isascii() and _SURROGATE_RE.search(text):
        text = _SURROGATE_RE.sub(lambda m: "\\u%04x" % ord(m.group()), text)
    return text


def _utf16_len(text: str) -> int:
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _utf16_cut(text: str, units: int) -> str:
    """``text.slice(0, units)`` in UTF-16 code units (an astral character cut in half leaves
    its high surrogate, like JavaScript)."""
    used = 0
    for i, ch in enumerate(text):
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > units:
            if used < units:
                return text[:i] + chr(0xD800 + ((ord(ch) - 0x10000) >> 10))
            return text[:i]
        used += w
    return text


def sample_text(v: Any) -> str:
    """The reference sample text of a non-null scalar: ``JSON.stringify(v)`` cut to 27 UTF-16
    units + ``…`` when longer than 28 units. Numbers keep their exact text."""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        if len(v) >= _SAMPLE_CUT:  # quoted: surely longer than 28 units; only the head matters
            return _utf16_cut(_json_str(v[:_SAMPLE_CUT]), _SAMPLE_CUT) + "…"
        s = _json_str(v)
        if len(s) <= _SAMPLE_UNITS // 2 or (len(s) <= _SAMPLE_UNITS and s.isascii()):
            return s
    else:
        s = codec.number_text(v)  # ASCII
        return s if len(s) <= _SAMPLE_UNITS else s[:_SAMPLE_CUT] + "…"
    if _utf16_len(s) > _SAMPLE_UNITS:
        s = _utf16_cut(s, _SAMPLE_CUT) + "…"
    return s


# ----------------------------------------------------------------------------------
# REF syntax (the harness / golden-test node addressing)
# ----------------------------------------------------------------------------------
_REF_PART = re.compile(r"([^\[\]]*)((?:\[[0-9]*\])*)")
_REF_INDEX = re.compile(r"\[([0-9]*)\]")


def parse_ref(text: str) -> list[str | int]:
    """Steps of a REF string: ``""`` -> ``[]``; ``"[].contact.city"`` -> ``[0, "contact",
    "city"]``; ``"orders[1]"`` -> ``["orders", 1]`` (``[]`` = index 0). Keys containing
    ``.``, ``[`` or ``]`` need the list form. ``ValueError`` on bad syntax."""
    steps: list[str | int] = []
    if text == "":
        return steps
    for part in text.split("."):
        m = _REF_PART.fullmatch(part)
        if m is None:
            raise ValueError(f"bad REF segment {part!r} in {text!r}")
        if m.group(1):
            steps.append(m.group(1))
        idx = _REF_INDEX.findall(m.group(2))
        steps.extend(int(d) if d else 0 for d in idx)
        if not m.group(1) and not idx:
            raise ValueError(f"empty REF segment in {text!r}")
    return steps


def ref_of(tree: MappingTree, node: MapNode) -> list[str | int]:
    """The list REF of ``node`` (the harness ``refOf``): its key when it is the first
    non-element child with that key, else its index."""
    steps: list[str | int] = []
    x = node
    p = tree.parent(x)
    while p is not None:
        i = _index_of(p.children, x)
        first = next((c for c in p.children if not c.is_elem and c.key == x.key), None)
        steps.append(x.key if (not x.is_elem and first is x) else i)
        x = p
        p = tree.parent(x)
    steps.reverse()
    return steps


def _index_of(items: list[MapNode], node: MapNode) -> int:
    for i, c in enumerate(items):
        if c is node:
            return i
    return -1


# ----------------------------------------------------------------------------------
# inference (inferInto / fromAcc / setOpen)
# ----------------------------------------------------------------------------------
class _Acc:
    """The reference ``newAcc()`` accumulator (+ the opaque flag of the limits)."""

    __slots__ = ("count", "obj_count", "types", "props", "elem", "samples", "opaque", "depth", "seen")

    def __init__(self, depth: int) -> None:
        self.count = 0
        self.obj_count = 0
        self.types = 0
        self.props: dict[str, _Acc] = {}
        self.elem: _Acc | None = None
        self.samples: list[str] = []
        self.opaque = False
        self.depth = depth
        self.seen: set[str] | None = None  # string heads already sampled (bounded; speed only)


class _Slot:
    """Placeholder for the records inside a value (the root array of a records document, the
    record array of a wrapper)."""

    __slots__ = ()


_SLOT = _Slot()
_END = object()


class _Feed:
    """A pending stream of array elements (the records) on the inference stack."""

    __slots__ = ("acc", "it")

    def __init__(self, acc: _Acc, it: Iterator[Any]) -> None:
        self.acc = acc
        self.it = it


_FAST_TYPES = frozenset({dict, list, str, int, float, bool, type(None)})


class _Inference:
    def __init__(self, *, max_keys: int | None, max_depth: int | None, max_nodes: int | None,
                 progress: Progress | None, cancel: CancelToken | None) -> None:
        # the root array and its element node of a records tree always fit: depth >= 1, nodes >= 2
        for name, v, low in (("max_keys", max_keys, 1), ("max_depth", max_depth, 1), ("max_nodes", max_nodes, 2)):
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < low):
                raise ValueError(f"{name} must be an int >= {low} or None, got {v!r}")
        self.max_keys = max_keys
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.progress = progress
        self.cancel = cancel
        self.nodes = 0
        self.values = 0
        self.truncated = False

    def new_acc(self, depth: int) -> _Acc:
        self.nodes += 1
        return _Acc(depth)

    def counted(self) -> None:
        """One more top-level value (record) consumed."""
        self.values += 1
        if not self.values & 0xFF:
            check(self.cancel)
            if self.progress is not None:
                self.progress.update(self.values)

    def walk(self, acc: _Acc, value: Any, records: Iterable[Any] | None = None) -> None:
        """``inferInto(acc, value)`` without recursion; a ``_SLOT`` inside ``value`` is an array
        whose elements are ``records`` (consumed lazily, in order)."""
        max_keys, max_depth, max_nodes = self.max_keys, self.max_depth, self.max_nodes
        cancel = self.cancel
        stack: list[Any] = [(acc, value)]
        pop, push = stack.pop, stack.append
        steps = 0
        while stack:
            steps += 1
            if not steps & _CHECK_EVERY:
                check(cancel)
            item = pop()
            if type(item) is _Feed:
                x = next(item.it, _END)
                if x is not _END:
                    push(item)
                    push((item.acc, x))
                    self.counted()
                continue
            a, x = item
            a.count += 1
            tx = type(x)
            if tx not in _FAST_TYPES:
                tx = _Slot if tx is _Slot else _base_type(x)
            if tx is dict:
                a.types |= _OBJ
                a.obj_count += 1
                if a.opaque or not x:
                    continue
                if max_depth is not None and a.depth >= max_depth:
                    a.opaque = self.truncated = True
                    continue
                props = a.props
                todo = []
                for k, v in x.items():
                    c = props.get(k)
                    if c is None:
                        if (max_keys is not None and len(props) >= max_keys) or \
                                (max_nodes is not None and self.nodes >= max_nodes):
                            a.opaque = self.truncated = True
                            todo = None
                            break
                        c = props[k] = self.new_acc(a.depth + 1)
                    todo.append((c, v))
                if todo:
                    todo.reverse()
                    stack.extend(todo)
            elif tx is list or tx is _Slot:
                a.types |= _ARR
                if a.opaque:
                    continue
                if max_depth is not None and a.depth >= max_depth:
                    a.opaque = self.truncated = True
                    continue
                e = a.elem
                if e is None:
                    if max_nodes is not None and self.nodes >= max_nodes:
                        a.opaque = self.truncated = True
                        continue
                    e = a.elem = self.new_acc(a.depth + 1)
                if tx is _Slot:
                    push(_Feed(e, iter(records if records is not None else ())))
                elif x:
                    stack.extend([(e, el) for el in reversed(x)])
            elif x is None:
                a.types |= _NULL
            else:
                if tx is str:
                    a.types |= _STR
                elif tx is bool:
                    a.types |= _BOOL
                else:
                    a.types |= _NUM
                samples = a.samples
                if len(samples) < _SAMPLES:
                    if tx is str:
                        # the sample of a string depends on its first 27 characters only: skip the
                        # heads already seen (low-cardinality fields would recompute it per value)
                        head = x if len(x) < _SAMPLE_CUT else x[:_SAMPLE_CUT]
                        seen = a.seen
                        if seen is None:
                            seen = a.seen = set()
                        if head in seen:
                            continue
                        if len(seen) < _SEEN_LIMIT:
                            seen.add(head)
                    s = sample_text(x)
                    if s not in samples:
                        samples.append(s)

    def build(self, acc: _Acc, key: str, src: tuple, pcount: int | None, new_id: Callable[[], int], *,
              is_root: bool = False, is_elem: bool = False) -> MapNode:
        """``fromAcc`` without recursion; ids in pre-order (parent before its children)."""
        root: MapNode | None = None
        stack: list[tuple[Any, ...]] = [(acc, key, src, pcount, is_root, is_elem, None)]
        while stack:
            a, k, s, pc, isr, ise, out = stack.pop()
            if a.opaque:
                kind = "value"
            elif a.types & _ARR:
                kind = "array"
            elif a.types & _OBJ:
                kind = "object"
            else:
                kind = "value"
            n = MapNode(id=new_id(), key=k, kind=kind, src=s, types=_types_of(a.types), count=a.count, pcount=pc,
                        samples=tuple(a.samples), is_elem=ise, is_root=isr)
            if out is None:
                root = n
            else:
                out.append(n)
            if kind == "object":
                kids = n.children
                for ck, ca in reversed(list(a.props.items())):
                    stack.append((ca, ck, s + (ck,), a.obj_count, False, False, kids))
            elif kind == "array":
                elem = a.elem if a.elem is not None else _Acc(len(s) + 1)
                stack.append((elem, "", s + (WILD,), None, False, True, n.children))
        assert root is not None
        return root


def _types_of(bits: int) -> tuple[str, ...]:
    return tuple(name for bit, name in _TYPE_BITS if bits & bit)


def _set_open(root: MapNode) -> None:
    """The reference ``setOpen(TREE, 0, countNodes(TREE) < 160)``."""
    count = 0
    stack = [root]
    while stack:
        n = stack.pop()
        count += 1
        stack.extend(n.children)
    small = count < _OPEN_SMALL
    stack2 = [(root, 0)]
    while stack2:
        n, d = stack2.pop()
        n.open = d <= 1 or (n.is_elem and d <= 3) or (small and d < 6)
        stack2.extend((c, d + 1) for c in n.children)


def _with_slot(wrapper: Any, record_path: Sequence[Any]) -> Any:
    """``wrapper`` with the value at ``record_path`` replaced by the records placeholder (the
    containers along the path are copied; the caller's value is not changed)."""
    if not record_path:
        return _SLOT
    if not isinstance(wrapper, (dict, list)):
        raise ValueError("the wrapper must be an object or an array")
    root: Any = dict(wrapper) if isinstance(wrapper, dict) else list(wrapper)
    cur = root
    for i, seg in enumerate(record_path):
        if isinstance(seg, bool):
            raise ValueError(f"bad record path segment {seg!r}")
        if isinstance(seg, int):
            if not isinstance(cur, list) or not 0 <= seg < len(cur):
                raise ValueError(f"record path {list(record_path)!r} not found in the wrapper")
        elif isinstance(seg, str):
            if not isinstance(cur, dict) or seg not in cur:
                raise ValueError(f"record path {list(record_path)!r} not found in the wrapper")
        else:
            raise ValueError(f"bad record path segment {seg!r}")
        if i == len(record_path) - 1:
            cur[seg] = _SLOT
        else:
            nxt = cur[seg]
            if isinstance(nxt, dict):
                nxt = dict(nxt)
            elif isinstance(nxt, list):
                nxt = list(nxt)
            else:
                raise ValueError(f"record path {list(record_path)!r} not found in the wrapper")
            cur[seg] = nxt
            cur = nxt
    return root


def _record_src(record_path: Sequence[Any]) -> tuple:
    return tuple(WILD if isinstance(s, int) and not isinstance(s, bool) else s for s in record_path)


def infer_tree(values: Iterable[Any], *, root_kind: str = "records", wrapper: Any = None,
               record_path: Sequence[Any] | None = None, max_keys: int | None = MAX_KEYS,
               max_depth: int | None = MAX_DEPTH, max_nodes: int | None = MAX_NODES,
               progress: Progress | None = None, cancel: CancelToken | None = None) -> MappingTree:
    """The default tree (the reference ``buildSchema``: ``inferInto`` + ``fromAcc`` + ``setOpen``).

    ``values`` by ``root_kind``: ``records`` -- the record values (the root array); ``value`` --
    the document value (an iterable holding it; several values are merged); ``keyed`` -- the
    member values; ``wrapped`` -- the record values, with ``wrapper`` = the parsed wrapper and
    ``record_path`` = the path of the record array in it (``str`` keys, ``int`` indices; its
    value in ``wrapper`` is ignored). Inference is iterative (any depth), keys in insertion
    order, array beats object beats value, an element node for every array (also when always
    empty). ``progress`` (optional) receives the number of values consumed; ``cancel`` is
    checked regularly. The limits are described in the module docstring (``None`` = off).
    """
    if root_kind not in ROOT_KINDS:
        raise ValueError(f"unknown root kind {root_kind!r}")
    inf = _Inference(max_keys=max_keys, max_depth=max_depth, max_nodes=max_nodes, progress=progress,
                     cancel=cancel)
    acc = inf.new_acc(0)
    record_src = None
    if root_kind == "records":
        inf.walk(acc, _SLOT, values)
    elif root_kind == "wrapped":
        if not record_path:
            raise ValueError("wrapped trees need a non-empty record_path")
        inf.walk(acc, _with_slot(wrapper, record_path), values)
        record_src = _record_src(record_path)
    else:
        for v in values:
            inf.walk(acc, v)
            inf.counted()
    check(cancel)
    counter = [0]

    def new_id() -> int:
        counter[0] += 1
        return counter[0]

    root = inf.build(acc, "", (), None, new_id, is_root=True)
    _set_open(root)
    tree = MappingTree(root, next_id=counter[0] + 1, root_kind=root_kind, record_src=record_src,
                       truncated=inf.truncated)
    tree.known_paths = frozenset(n.src for n in tree.iter_nodes() if n.src is not None)
    if progress is not None:
        progress.update(inf.values)
    if inf.truncated:
        log.info("mapping tree truncated by the limits (keys %s, depth %s, nodes %s)", max_keys, max_depth, max_nodes)
    return tree


# ----------------------------------------------------------------------------------
# tree helpers shared by the operations and the badges
# ----------------------------------------------------------------------------------
def _unique_key(p: MapNode, key: str, self_node: MapNode | None) -> str:
    """The reference ``uniqueKey``: ``key``, else ``key_2``, ``key_3`` … not used by the
    non-element children of ``p`` other than ``self_node``."""
    used = {c.key for c in p.children if c is not self_node and not c.is_elem}
    if key not in used:
        return key
    i = 2
    while f"{key}_{i}" in used:
        i += 1
    return f"{key}_{i}"


def _is_under(src: Sequence | None, base: Sequence) -> bool:
    """The reference ``isUnder``: ``src`` lies strictly below ``base``."""
    return src is not None and len(src) > len(base) and tuple(src[:len(base)]) == tuple(base)


def _can_inside(t: MapNode, n: MapNode) -> bool:
    return n.kind == "array" if t.kind == "array" else t.kind == "object"


def _can_sibling(t: MapNode) -> bool:
    return not t.is_root and not t.is_elem


def find_rec(A: MapNode, E: MapNode) -> MapNode | None:
    """The reference ``findRec``: the recursive child array of element node ``E`` of array
    ``A`` (same-named as ``A`` preferred, else the first candidate), or ``None``."""
    if not E.is_elem or E.kind != "object" or E.rec_key or E.src is None:
        return None
    base = tuple(E.src)
    cands = [C for C in E.children
             if C.kind == "array" and not C.synthetic and C.src is not None and len(C.src) == len(base) + 1
             and tuple(C.src[:-1]) == base and C.children and C.children[0].kind == "object"]
    if not cands:
        return None
    if A.src:
        want = A.src[-1]
        for C in cands:
            if C.src[-1] == want and want is not WILD:
                return C
    return cands[0]


def can_flatten(A: MapNode) -> bool:
    """The reference ``canFlatten``: ``A`` is an array node with an element that has a
    recursive child array (the root array qualifies too)."""
    return A.kind == "array" and any(find_rec(A, E) is not None for E in A.children)


# ----------------------------------------------------------------------------------
# immutable snapshots (undo history)
# ----------------------------------------------------------------------------------
def _tup(x: Any) -> Any:
    return x if x is None or type(x) is tuple else tuple(x)


def _same_node(prev: tuple, f: tuple) -> bool:
    """``prev`` and ``f`` hold the same fields and the very same child tuples (no recursion)."""
    if prev[:4] != f[:4] or prev[5:] != f[5:]:
        return False
    a, b = prev[4], f[4]
    return len(a) == len(b) and all(x is y for x, y in zip(a, b))


def _freeze(root: MapNode, memo: dict[int, tuple]) -> tuple[tuple, dict[int, tuple]]:
    """Nested tuples of every field (children as a tuple of frozen children); unchanged nodes
    reuse the tuple of ``memo`` (by id), so consecutive snapshots share their structure and
    an unchanged tree gives the very same tuple. Iterative; flat comparisons only."""
    out: dict[int, tuple] = {}
    new_memo: dict[int, tuple] = {}
    stack: list[tuple[MapNode, bool]] = [(root, False)]
    while stack:
        n, done = stack.pop()
        if not done:
            stack.append((n, True))
            stack.extend((c, False) for c in n.children)
            continue
        f = (n.id, n.key, n.kind, _tup(n.src), tuple([out[id(c)] for c in n.children]), _tup(n.types), n.count,
             n.pcount, _tup(n.samples), n.synthetic, n.rec_key, n.computed, _tup(n.ctx), n.open, n.is_elem,
             n.is_root)
        prev = memo.get(n.id)
        if prev is not None and _same_node(prev, f):
            f = prev
        out[id(n)] = f
        new_memo[n.id] = f
    return out[id(root)], new_memo


def _frozen_equal(a: tuple, b: tuple) -> bool:
    """Deep equality of two frozen trees without recursion (shared subtrees are skipped)."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if x is y:
            continue
        if x[:4] != y[:4] or x[5:] != y[5:] or len(x[4]) != len(y[4]):
            return False
        stack.extend(zip(x[4], y[4]))
    return True


def _thaw(frozen: tuple) -> MapNode:
    root: MapNode | None = None
    stack: list[tuple[tuple, list[MapNode] | None]] = [(frozen, None)]
    while stack:
        f, out = stack.pop()
        n = MapNode(id=f[0], key=f[1], kind=f[2], src=f[3], types=f[5], count=f[6], pcount=f[7], samples=f[8],
                    synthetic=f[9], rec_key=f[10], computed=f[11], ctx=f[12], open=f[13], is_elem=f[14],
                    is_root=f[15])
        if out is None:
            root = n
        else:
            out.append(n)
        stack.extend((c, n.children) for c in reversed(f[4]))
    assert root is not None
    return root


def _memo_of(frozen: tuple) -> dict[int, tuple]:
    memo: dict[int, tuple] = {}
    stack = [frozen]
    while stack:
        f = stack.pop()
        memo[f[0]] = f
        stack.extend(f[4])
    return memo


# ----------------------------------------------------------------------------------
# designer
# ----------------------------------------------------------------------------------
@dataclass
class OpResult:
    """Result of a designer operation.

    ``applied``: the operation was accepted (the reference reached ``onChange``); ``changed``:
    the tree differs from before (an accepted no-op move gives ``applied=True,
    changed=False``); ``message``: the localized toast (``""`` = none); ``node``: the node the
    operation produced or acted on (the new group, the moved/renamed node, the target array of
    a merge, the flattened array, the parent of an unwrapped object, the removed node) or
    ``None``; ``reason``: why a refused operation was refused (localized)."""

    applied: bool
    changed: bool = False
    message: str = ""
    node: MapNode | None = None
    reason: str = ""


@dataclass(frozen=True)
class FlattenPlan:
    """The read-only part of a recursive flatten (:meth:`Designer.plan_flatten`)."""

    target_id: int
    version: int
    entries: tuple  # (element node, recursive key, accumulator, item count) per flattened element
    truncated: bool = False


def _tc(msg_key: str, count: int, /, **params: Any) -> str:
    """:func:`t` with the singular variant ``<msg_key>.one`` when ``count`` is 1."""
    if count == 1 and has(msg_key + ".one"):
        return t(msg_key + ".one", **params)
    return t(msg_key, **params)


class Designer:
    """The reference edit operations on a :class:`MappingTree`, each one undo step.

    ``population``: a callable returning an iterable of values for :meth:`flatten`'s
    re-inference: the record values (``records``, ``wrapped`` -- then also ``wrapper`` and
    ``record_path`` as for :func:`infer_tree`), the member values (``keyed``) or the document
    value (``value``). ``undo_limit``: steps kept (default ``Settings.undo_limit``). The
    ``max_*`` limits apply to the re-inference like in :func:`infer_tree`.

    Not thread-safe: call it from one thread (the GUI thread); only :meth:`plan_flatten` may run
    in a worker thread while no operation changes the tree (its plan is refused otherwise).
    After :meth:`undo`, :meth:`redo` and :meth:`reset` the tree holds new node objects (same
    ids): views keep ids, not nodes.
    """

    def __init__(self, tree: MappingTree, *, population: Callable[[], Iterable[Any]] | None = None,
                 undo_limit: int | None = None, wrapper: Any = None, record_path: Sequence[Any] | None = None,
                 max_keys: int | None = MAX_KEYS, max_depth: int | None = MAX_DEPTH,
                 max_nodes: int | None = MAX_NODES) -> None:
        if undo_limit is None:
            from .settings import get_settings

            undo_limit = get_settings().undo_limit
        if not isinstance(undo_limit, int) or isinstance(undo_limit, bool) or undo_limit < 1:
            raise ValueError(f"undo_limit must be a positive int, got {undo_limit!r}")
        self.tree = tree
        self.population = population
        self.wrapper = wrapper
        self.record_path = tuple(record_path) if record_path is not None else None
        self.undo_limit = undo_limit
        self.limits = {"max_keys": max_keys, "max_depth": max_depth, "max_nodes": max_nodes}
        top = _normalize_ids(tree.root, 0)
        orig_tree = tree.orig if tree.orig is not None else tree
        if orig_tree is not tree:
            top = max(top, _normalize_ids(orig_tree.root, 0))
        self._orig, _ = _freeze(orig_tree.root, {})
        self._next = max(int(tree.next_id or 0), top + 1)
        tree.next_id = self._next
        tree.touch()
        self._memo: dict[int, tuple] = {}
        self._undo: deque[tuple] = deque(maxlen=undo_limit)
        self._redo: list[tuple] = []
        self._version = 0

    # -- state -----------------------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def steps(self) -> int:
        """Undoable steps (the reference ``UNDO.length``)."""
        return len(self._undo)

    @property
    def redo_steps(self) -> int:
        return len(self._redo)

    @property
    def changes(self) -> int:
        """The header's "N o'zgarish" (the reference shows ``UNDO.length``)."""
        return len(self._undo)

    @property
    def version(self) -> int:
        """Increases with every applied operation (undo/redo/reset included)."""
        return self._version

    @property
    def orig(self) -> MappingTree:
        """A copy of the original tree (the "Asl holat" target)."""
        root = _thaw(self._orig)
        return MappingTree(root, next_id=self.tree.next_id, root_kind=self.tree.root_kind,
                           known_paths=self.tree.known_paths, record_src=self.tree.record_src)

    # -- internals -------------------------------------------------------------------
    def _node(self, ref: Any) -> MapNode | None:
        return None if ref is None else self.tree.find(ref)

    def _new_id(self) -> int:
        i = self._next
        self._next += 1
        self.tree.next_id = self._next
        return i

    def _freeze_now(self) -> tuple:
        frozen, self._memo = _freeze(self.tree.root, self._memo)
        return frozen

    def _set_root(self, frozen: tuple) -> None:
        self.tree.root = _thaw(frozen)
        self._memo = _memo_of(frozen)

    @staticmethod
    def _refuse(key: str) -> OpResult:
        return OpResult(False, False, "", None, t(key))

    def _begin(self) -> tuple:
        """``pushUndo``: the snapshot before the change; any new step clears the redo stack."""
        before = self._freeze_now()
        self._undo.append(before)
        self._redo.clear()
        return before

    def _finish(self, before: tuple, message: str, node: MapNode | None) -> OpResult:
        self.tree.touch()
        self._version += 1
        after = self._freeze_now()
        return OpResult(True, not _frozen_equal(before, after), message, node)

    def _parent(self, n: MapNode) -> MapNode | None:
        return self.tree.parent(n)

    def _detach(self, n: MapNode) -> MapNode | None:
        p = self._parent(n)
        if p is not None:
            i = _index_of(p.children, n)
            if i >= 0:
                del p.children[i]
        return p

    def _is_within(self, t: MapNode, n: MapNode) -> bool:
        """The reference ``isWithin``: ``n`` is ``t`` or an ancestor of ``t``."""
        x: MapNode | None = t
        while x is not None:
            if x is n:
                return True
            x = self._parent(x)
        return False

    # -- drag & drop -------------------------------------------------------------------
    def zone_for(self, target: Any, node: Any, frac: float) -> str | None:
        """The reference ``zoneFor``: where ``node`` dropped at height fraction ``frac`` of the
        ``target`` row goes (``before``/``inside``/``after``) or ``None`` (no drop). Inside and
        sibling allowed: ``< 0.28`` before, ``> 0.72`` after, else inside; only inside: the
        whole row; only sibling: ``< 0.5`` before, else after."""
        n, t_ = self._node(node), self._node(target)
        if n is None or t_ is None or self._is_within(t_, n):
            return None
        ins, sib = _can_inside(t_, n), _can_sibling(t_)
        if ins and sib:
            return "before" if frac < 0.28 else "after" if frac > 0.72 else "inside"
        if ins:
            return "inside"
        if sib:
            return "before" if frac < 0.5 else "after"
        return None

    def can_drag(self, node: Any) -> bool:
        """Rows that can be dragged (not the root, not element rows)."""
        n = self._node(node)
        return n is not None and not n.is_root and not n.is_elem

    def paste_zone(self, target: Any, node: Any) -> str | None:
        """The keyboard "cut / paste" zone: ``inside`` when allowed, else ``after`` (the zone of a
        drop on the middle of the row), ``None`` when ``node`` cannot go there at all."""
        return self.zone_for(target, node, 0.5)

    def move_up(self, node: Any) -> OpResult:
        """Alt+Up: before the previous sibling (one undo step, like a drop)."""
        return self._move_by(node, -1)

    def move_down(self, node: Any) -> OpResult:
        """Alt+Down: after the next sibling."""
        return self._move_by(node, 1)

    def _move_by(self, node: Any, step: int) -> OpResult:
        n = self._node(node)
        if n is None:
            return self._refuse("mapping.why.missing")
        if n.is_root:
            return self._refuse("mapping.why.root")
        if n.is_elem:
            return self._refuse("mapping.why.elem")
        p = self._parent(n)
        assert p is not None
        i = _index_of(p.children, n) + step
        if not 0 <= i < len(p.children):
            return self._refuse("mapping.why.first" if step < 0 else "mapping.why.last")
        return self.move(n, p.children[i], "before" if step < 0 else "after")

    # -- operations --------------------------------------------------------------------
    def move(self, node: Any, target: Any, zone: str | None) -> OpResult:
        """``moveNode``: ``before``/``after`` ``target`` (in its parent) or ``inside`` it (appended;
        an array node dropped inside an array node merges: its element nodes are appended to
        the target's and the dragged node disappears). Like the reference, a zone other than
        ``inside``/``after`` acts as ``before``; ``None`` (no drop zone) is refused."""
        n, t_ = self._node(node), self._node(target)
        if n is None or t_ is None:
            return self._refuse("mapping.why.missing")
        if n.is_root:
            return self._refuse("mapping.why.root")
        if n.is_elem:
            return self._refuse("mapping.why.elem")
        if self._is_within(t_, n):
            return self._refuse("mapping.why.into_itself")
        if zone is None:
            return self._refuse("mapping.why.no_zone")
        msg = ""
        if zone == "inside" and t_.kind == "array":
            if n.kind != "array":
                return self._refuse("mapping.why.array_only")
            before = self._begin()
            self._detach(n)
            t_.children.extend(n.children)
            t_.open = True
            if t_.is_root:
                msg = t("mapping.toast.merged_root", node=n.key)
            else:
                msg = t("mapping.toast.merged", node=n.key, target=t_.key)
            return self._finish(before, msg, t_)
        if zone == "inside":
            if t_.kind != "object":
                return self._refuse("mapping.why.not_object")
            before = self._begin()
            self._detach(n)
            k = _unique_key(t_, n.key, n)
            if k != n.key:
                msg = t("mapping.toast.moved_renamed", old=n.key, new=k)
            n.key = k
            t_.children.append(n)
            t_.open = True
            return self._finish(before, msg, n)
        if not _can_sibling(t_):
            return self._refuse("mapping.why.no_sibling")
        before = self._begin()
        p = self._parent(t_)
        assert p is not None
        self._detach(n)
        i = _index_of(p.children, t_) + (1 if zone == "after" else 0)
        k = _unique_key(p, n.key, n)
        if k != n.key:
            msg = t("mapping.toast.moved_renamed", old=n.key, new=k)
        n.key = k
        p.children.insert(i, n)
        return self._finish(before, msg, n)

    def merge(self, node: Any, target: Any) -> OpResult:
        """Drop array ``node`` onto array ``target`` (= ``move(node, target, "inside")``)."""
        return self.move(node, target, "inside")

    def delete(self, node: Any) -> OpResult:
        """``deleteNode``: not the root; an element node only while its array has others."""
        n = self._node(node)
        if n is None:
            return self._refuse("mapping.why.missing")
        if n.is_root:
            return self._refuse("mapping.why.root")
        p = self._parent(n)
        if n.is_elem and (p is None or len(p.children) <= 1):
            return self._refuse("mapping.why.last_elem")
        before = self._begin()
        self._detach(n)
        msg = t("mapping.toast.deleted_elem") if n.is_elem else t("mapping.toast.deleted", name=n.key)
        return self._finish(before, msg, n)

    def unwrap(self, node: Any) -> OpResult:
        """``unwrapNode``: the children of object node ``node`` take its place in its parent (keys
        made unique one by one); groups can be dissolved too."""
        o = self._node(node)
        if o is None:
            return self._refuse("mapping.why.missing")
        if o.kind != "object":
            return self._refuse("mapping.why.not_object")
        if o.is_root:
            return self._refuse("mapping.why.root")
        if o.is_elem:
            return self._refuse("mapping.why.elem")
        before = self._begin()
        p = self._parent(o)
        assert p is not None
        i = _index_of(p.children, o)
        self._detach(o)
        renamed = 0
        for c in o.children:
            k = _unique_key(p, c.key, c)
            if k != c.key:
                renamed += 1
            c.key = k
            p.children.insert(i, c)
            i += 1
        count = len(o.children)
        if renamed:
            msg = t("mapping.toast.unwrapped_renamed", name=o.key, count=count, renamed=renamed)
        else:
            msg = _tc("mapping.toast.unwrapped", count, name=o.key, count=count)
        return self._finish(before, msg, p)

    def add_group(self, target: Any, name: str | None = None) -> OpResult:
        """``addGroup``: a new synthetic group appended to object node ``target`` (no toast).
        With ``name`` the group is renamed in a second undo step, like the prototype's inline
        rename; the result's message is then the rename's."""
        t_ = self._node(target)
        if t_ is None:
            return self._refuse("mapping.why.missing")
        if t_.kind != "object":
            return self._refuse("mapping.why.not_object")
        before = self._begin()
        g = MapNode(id=self._new_id(), key=_unique_key(t_, t("mapping.default_group_key"), None), kind="object",
                    src=None, synthetic=True, types=("object",), open=True)
        t_.children.append(g)
        t_.open = True
        res = self._finish(before, "", g)
        if name is not None:
            r2 = self.rename(g, name)
            if r2.applied:
                return OpResult(True, True, r2.message, g)
        return res

    def rename(self, node: Any, name: str) -> OpResult:
        """``renameNode``: ``name`` trimmed with the ECMAScript set; refused for the root,
        elements, an empty name or the current key; a taken name gets ``_2``, ``_3`` …"""
        n = self._node(node)
        name = js_trim(name if isinstance(name, str) else str(name))
        if n is None:
            return self._refuse("mapping.why.missing")
        if n.is_root:
            return self._refuse("mapping.why.root")
        if n.is_elem:
            return self._refuse("mapping.why.elem")
        if not name:
            return self._refuse("mapping.why.empty_name")
        if name == n.key:
            return self._refuse("mapping.why.same_name")
        before = self._begin()
        p = self._parent(n)
        assert p is not None
        k = _unique_key(p, name, n)
        n.key = k
        msg = t("mapping.toast.renamed_unique", name=name, new=k) if k != name else ""
        return self._finish(before, msg, n)

    # -- recursive flatten ----------------------------------------------------------------
    def can_flatten(self, node: Any) -> bool:
        n = self._node(node)
        return n is not None and can_flatten(n)

    def _has_population(self) -> bool:
        """Values to re-infer from exist (wrapped trees also need the wrapper and the record path)."""
        if self.population is None:
            return False
        return self.tree.root_kind != "wrapped" or bool(self.record_path)

    def _gather(self, src: tuple) -> Iterator[Any]:
        """The values at ``src`` over the whole population (the reference
        ``resolve(compile(E.src), null)`` from ``DATA``), in reference order, lazily."""
        kind = self.tree.root_kind
        population = self.population
        assert population is not None
        if kind == "wrapped":
            if self.record_path is None:
                raise ValueError("a wrapped tree needs wrapper and record_path to flatten")
            root: Any = _with_slot(self.wrapper, self.record_path)
            segs = src
        elif kind == "records":
            root, segs = _SLOT, src
        else:
            root, segs = _SLOT, (WILD,) + tuple(src)
        n = len(segs)
        stack: list[tuple[Iterator[Any], int]] = [(iter((root,)), 0)]
        while stack:
            it, i = stack[-1]
            v = next(it, _END)
            if v is _END:
                stack.pop()
                continue
            if i == n:
                yield v
                continue
            s = segs[i]
            if s is WILD:
                if v is _SLOT:
                    stack.append((iter(population()), i + 1))
                elif isinstance(v, list):
                    stack.append((iter(v), i + 1))
            elif isinstance(v, dict) and s in v:
                stack.append((iter((v[s],)), i + 1))

    def plan_flatten(self, node: Any, *, progress: Progress | None = None,
                     cancel: CancelToken | None = None) -> FlattenPlan | None:
        """Re-infer every flattenable element of array ``node`` over the population (no change
        to the tree). ``None`` when ``node`` cannot be flattened or there is no population.
        ``progress`` receives the number of items inferred."""
        A = self._node(node)
        if A is None or not can_flatten(A) or not self._has_population():
            return None
        entries = []
        truncated = False
        for E in A.children:
            C = find_rec(A, E)
            if C is None:
                continue
            k = C.src[-1]
            inf = _Inference(progress=progress, cancel=cancel, **self.limits)
            acc = inf.new_acc(len(E.src))
            items = 0
            for el, _d in _preorder(self._gather(tuple(E.src)), k):
                inf.walk(acc, el)
                items += 1
                inf.counted()
            truncated = truncated or inf.truncated
            entries.append((E, k, acc, items))
        check(cancel)
        return FlattenPlan(A.id, self._version, tuple(entries), truncated)

    def flatten(self, node: Any, *, progress: Progress | None = None, cancel: CancelToken | None = None,
                plan: FlattenPlan | None = None) -> OpResult:
        """``flattenRec``: every element of array ``node`` that has a recursive child array
        ``C`` gets ``rec_key`` = ``C``'s key, a ``_depth`` node first, its own fields
        re-inferred from all levels (earlier edits under it are lost; groups, computed and
        moved-in nodes stay after them), and the recursive key removed. One undo step."""
        A = self._node(node)
        if A is None:
            return self._refuse("mapping.why.missing")
        if not can_flatten(A):
            return self._refuse("mapping.why.not_flattenable")
        if plan is None:
            if not self._has_population():
                return self._refuse("mapping.why.no_data")
            plan = self.plan_flatten(A, progress=progress, cancel=cancel)
            assert plan is not None
        elif plan.version != self._version or plan.target_id != A.id:
            return self._refuse("mapping.why.stale")
        before = self._begin()
        total = 0
        key_name = ""
        new_paths: list[tuple] = []
        for E, k, acc, items in plan.entries:
            key_name = k
            esrc = tuple(E.src)
            foreign = [c for c in E.children if c.synthetic or c.computed or not _is_under(c.src, esrc)]
            kids = [MapNode(id=self._new_id(), key="_depth", kind="value", src=None, types=("number",),
                            computed="depth", ctx=esrc)]
            inf = _Inference(progress=None, cancel=None, **self.limits)
            for key, c in acc.props.items():
                if key != k:
                    kids.append(inf.build(c, key, esrc + (key,), acc.obj_count, self._new_id))
            E.children = kids + foreign
            for c in E.children:
                if c.computed:
                    c.key = _unique_key(E, c.key, c)
            E.rec_key = k
            E.count = acc.count
            E.open = True
            total += items
            stack = list(kids[1:])
            while stack:
                x = stack.pop()
                new_paths.append(tuple(x.src))
                stack.extend(x.children)
        A.open = True
        if self.tree.known_paths is not None and new_paths:
            self.tree.known_paths = self.tree.known_paths | frozenset(new_paths)
        if plan.truncated:
            self.tree.truncated = True
        msg = _tc("mapping.toast.flattened", total, key=key_name, total=total)
        return self._finish(before, msg, A)

    # -- history -------------------------------------------------------------------------
    def undo(self) -> OpResult:
        """Back to the state before the last step (``Oxirgi o'zgarish bekor qilindi``)."""
        if not self._undo:
            return self._refuse("mapping.why.nothing_to_undo")
        cur = self._freeze_now()
        snap = self._undo.pop()
        self._redo.append(cur)
        self._set_root(snap)
        self._version += 1
        return OpResult(True, not _frozen_equal(snap, cur), t("mapping.toast.undone"), None)

    def redo(self) -> OpResult:
        """Repeat the last undone step (until a new operation is applied)."""
        if not self._redo:
            return self._refuse("mapping.why.nothing_to_redo")
        cur = self._freeze_now()
        snap = self._redo.pop()
        self._undo.append(cur)
        self._set_root(snap)
        self._version += 1
        return OpResult(True, not _frozen_equal(snap, cur), t("mapping.toast.redone"), None)

    def reset(self) -> OpResult:
        """``resetTree``: back to the original tree; always an undoable step."""
        before = self._begin()
        self._set_root(self._orig)
        self.tree.touch()
        self._version += 1
        after = self._freeze_now()
        return OpResult(True, not _frozen_equal(before, after), t("mapping.toast.reset"), None)

    # -- UI state (no undo step, like the prototype) ---------------------------------------
    def set_open(self, node: Any, open_: bool) -> None:
        n = self._node(node)
        if n is not None:
            n.open = bool(open_)

    def expand_all(self) -> None:
        """"Hammasini ochish": every node open."""
        for n in self.tree.iter_nodes():
            n.open = True

    def collapse_all(self) -> None:
        """"Yig'ish": only the root and its element node stay open."""
        for n, _p, d in self.tree.walk():
            n.open = d == 0 or (d == 1 and n.is_elem)

    def actions(self, node: Any) -> dict[str, str | None]:
        """The row actions of the prototype -- ``group``, ``unwrap``, ``flatten``, ``rename``,
        ``delete`` -> ``None`` when enabled, else the localized reason."""
        n = self._node(node)
        if n is None:
            why = t("mapping.why.missing")
            return {"group": why, "unwrap": why, "flatten": why, "rename": why, "delete": why}
        p = self._parent(n)
        if n.kind != "object":
            unwrap: str | None = t("mapping.why.not_object")
        elif n.is_root:
            unwrap = t("mapping.why.root")
        elif n.is_elem:
            unwrap = t("mapping.why.elem")
        else:
            unwrap = None
        if n.is_root:
            rename: str | None = t("mapping.why.root")
            delete: str | None = t("mapping.why.root")
        elif n.is_elem:
            rename = t("mapping.why.elem")
            delete = None if p is not None and len(p.children) > 1 else t("mapping.why.last_elem")
        else:
            rename = delete = None
        return {
            "group": None if n.kind == "object" else t("mapping.why.not_object"),
            "unwrap": unwrap,
            "flatten": None if can_flatten(n) else t("mapping.why.not_flattenable"),
            "rename": rename,
            "delete": delete,
        }


def _normalize_ids(root: MapNode, top: int) -> int:
    """Give nodes without a usable or with a duplicate id a fresh one (like the harness);
    returns the highest id."""
    seen: set[int] = set()
    bad: list[MapNode] = []
    for n in _iter_pre(root):
        if not isinstance(n.id, int) or isinstance(n.id, bool) or n.id < 1 or n.id in seen:
            bad.append(n)
        else:
            seen.add(n.id)
            top = max(top, n.id)
    for n in bad:
        top += 1
        n.id = top
    return top


def _iter_pre(root: MapNode) -> Iterator[MapNode]:
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _preorder(top: Iterable[Any], k: str) -> Iterator[tuple[Any, int]]:
    """The reference ``preorder`` over a stream of top-level elements (explicit stack): an
    element, then the elements of its own array ``el[k]`` (one level deeper), then the next."""
    stack: list[tuple[Iterator[Any], int]] = [(iter(top), 1)]
    while stack:
        it, d = stack[-1]
        for el in it:
            yield el, d
            sub = el.get(k) if isinstance(el, dict) else None
            if isinstance(sub, list):
                stack.append((iter(sub), d + 1))
                break
        else:
            stack.pop()


# ----------------------------------------------------------------------------------
# badges and row texts (rowHTML)
# ----------------------------------------------------------------------------------
def enclosing_ctx(tree: MappingTree, node: MapNode) -> tuple[tuple, ...]:
    """The ``src`` of every element node above ``node`` (outermost first): the ``ctx`` of
    the reference ``walkRender``."""
    out = []
    p = tree.parent(node)
    while p is not None:
        if p.is_elem and p.src is not None:
            out.append(tuple(p.src))
        p = tree.parent(p)
    out.reverse()
    return tuple(out)


def relation(node: MapNode, ctx: Sequence[Sequence]) -> tuple[bool, bool]:
    """The reference ``relation``: ``(gathered, broadcast)`` of an ordinary node whose enclosing
    element nodes have the sources ``ctx``."""
    if node.synthetic or node.computed or node.is_elem or node.is_root or node.src is None:
        return False, False
    src = tuple(node.src)
    wk = [src[:i + 1] for i, s in enumerate(src) if s is WILD]
    ctxs = [tuple(c) for c in ctx]
    last = -1
    for i, k in enumerate(wk):
        if k in ctxs:
            last = i
    near = ctxs[-1] if ctxs else None
    return len(wk) - 1 > last, near is not None and near not in wk


def is_moved(node: MapNode, parent: MapNode | None) -> bool:
    """The reference ``isMoved``: an ordinary node not in the place its source suggests."""
    if node.synthetic or node.computed or node.is_elem or node.is_root or parent is None or node.src is None:
        return False
    if parent.synthetic:
        return True
    return tuple(node.src[:-1]) != tuple(parent.src or ())


def _is_opaque(node: MapNode) -> bool:
    return node.kind == "value" and node.computed is None and ("object" in node.types or "array" in node.types)


_AUTO = object()


def badges(tree: MappingTree, node: MapNode, parent: Any = _AUTO, ctx: Sequence[Sequence] | None = None
           ) -> list[tuple[str, str, str]]:
    """The row badges of ``node`` in the prototype's order as ``(text, tooltip, theme role)``:
    ``yangi guruh``, ``daraja raqami``, ``rekursiv · KEY``, ``manba: PATH``, ``ko'chirildi``,
    ``asl nomi: NAME``, ``ro'yxatga yig'iladi`` / ``har elementga takrorlanadi`` (then the
    opaque-node badge of the limits). ``parent``/``ctx`` default to the tree's."""
    if parent is _AUTO:
        parent = tree.parent(node)
    if ctx is None:
        ctx = enclosing_ctx(tree, node)
    n = node
    out: list[tuple[str, str, str]] = []
    if n.synthetic:
        out.append((t("mapping.badge.group"), "", "accent"))
    if n.computed:
        out.append((t("mapping.badge.depth"), t("mapping.badge.depth.tip"), "muted"))
    if n.is_elem and n.rec_key:
        out.append((t("mapping.badge.recursive", key=n.rec_key), t("mapping.badge.recursive.tip", key=n.rec_key), "ok"))
    if n.is_elem and parent is not None and not parent.is_root and n.src is not None \
            and tuple(n.src[:-1]) != tuple(parent.src or ()):
        out.append((t("mapping.badge.source", path=path_label(n.src[:-1])), t("mapping.badge.source.tip"), "array"))
    if not n.synthetic and not n.computed and not n.is_elem and not n.is_root and n.src is not None:
        if is_moved(n, parent):
            out.append((t("mapping.badge.moved"), t("mapping.badge.moved.tip", path=path_label(n.src)), "accent"))
        orig = n.src[-1] if n.src else None
        if orig is not WILD and n.key != orig:
            out.append((t("mapping.badge.orig_name", name=orig), "", "muted"))
        gathered, broadcast = relation(n, ctx)
        if gathered:
            out.append((t("mapping.badge.gathered"), t("mapping.badge.gathered.tip"), "warn"))
        elif broadcast:
            out.append((t("mapping.badge.broadcast"), t("mapping.badge.broadcast.tip"), "bool"))
    if _is_opaque(n):
        out.append((t("mapping.badge.opaque"), t("mapping.badge.opaque.tip"), "muted"))
    return out


def node_label(tree: MappingTree, node: MapNode, parent: Any = _AUTO, *, file_name: str | None = None) -> str:
    """Row label: the root -> ``file_name`` (keyed trees: "har bir a'zo"); element nodes ->
    "har bir yozuv" (the records' element) or "har bir element"; other nodes -> their key."""
    if node.is_root:
        if tree.root_kind == "keyed":
            return t("mapping.label.each_member")
        return file_name if file_name else t("mapping.label.root")
    if node.is_elem:
        if parent is _AUTO:
            parent = tree.parent(node)
        record_elem = (parent is not None and parent.is_root) or (
            tree.root_kind == "wrapped" and tree.record_src is not None and node.src is not None
            and tuple(node.src) == tuple(tree.record_src) + (WILD,))
        return t("mapping.label.each_record") if record_elem else t("mapping.label.each_element")
    return node.key


def type_chips(node: MapNode) -> list[tuple[str, str, str]]:
    """Type chips ``(glyph, tooltip, theme role)``: computed -> ``123``; an element that never
    held a value -> ``bo'sh``; else one chip per type in :data:`TYPE_ORDER`."""
    if node.computed:
        return [(TYPE_GLYPHS["number"], t("mapping.type.number"), "number")]
    if node.is_elem and not node.types:
        return [(t("mapping.type.empty"), t("mapping.type.empty.tip"), "null")]
    return [(TYPE_GLYPHS[ty], t("mapping.type." + ty), _TYPE_ROLES[ty]) for ty in node.types if ty in TYPE_GLYPHS]


def _group_digits(n: int) -> str:
    text = f"{int(n):,}"
    return text.replace(",", " ") if get_language() == "uz" else text


def node_meta(tree: MappingTree, node: MapNode, *, record_count: int | None = None) -> list[tuple[str, str]]:
    """Row meta ``(text, tooltip)``: ``NN%`` for fields present in part of the objects,
    ``N ta`` for element nodes, ``N ta yozuv`` (``N ta a'zo``) for the root when
    ``record_count`` is given."""
    n = node
    out: list[tuple[str, str]] = []
    if not n.is_elem and not n.is_root and not n.synthetic and not n.computed and n.pcount and n.count < n.pcount:
        pct = math.floor(n.count / n.pcount * 100 + 0.5)  # Math.round
        out.append((f"{pct}%", t("mapping.meta.pct.tip", count=n.count, pcount=n.pcount)))
    if n.is_elem and n.count:
        out.append((_tc("mapping.meta.items", n.count, count=_group_digits(n.count)), ""))
    if n.is_root and record_count is not None:
        key = "mapping.meta.members" if tree.root_kind == "keyed" else "mapping.meta.records"
        out.append((_tc(key, record_count, count=_group_digits(record_count)), ""))
    return out


def sample_display(node: MapNode) -> str:
    """The row's sample column: computed -> ``1, 2, 3 …``; value nodes -> their samples."""
    if node.computed:
        return "1, 2, 3 …"
    if node.kind == "value" and node.samples:
        return ", ".join(node.samples)
    return ""


def count_fields(tree: MappingTree) -> int:
    """The reference ``countFields``: nodes other than the root and element nodes."""
    return sum(1 for n in tree.iter_nodes() if not n.is_root and not n.is_elem)


def stats_text(tree: MappingTree, changes: int = 0) -> str:
    """The tree header: ``N maydon`` + `` · M o'zgarish`` when there are undoable steps."""
    fields_ = count_fields(tree)
    text = _tc("mapping.stats.fields", fields_, count=_group_digits(fields_))
    if changes:
        text += " · " + _tc("mapping.stats.changes", changes, count=_group_digits(changes))
    return text


# ----------------------------------------------------------------------------------
# known source paths (unmapped-fields count of the engine, ARCHITECTURE §13.0)
# ----------------------------------------------------------------------------------
def known_keys(tree: MappingTree) -> dict[tuple, frozenset[str]]:
    """``source path S -> keys known below S``: the child keys of ``S`` in the originally
    inferred tree (``known_paths``) and in the current tree. Paths are absolute ``src``
    tuples (``WILD`` for array steps)."""
    paths: set[tuple] = set(tree.known_paths or ())
    for n in tree.iter_nodes():
        if n.src is not None:
            paths.add(tuple(n.src))
    out: dict[tuple, set[str]] = {}
    for p in paths:
        if p and p[-1] is not WILD:
            out.setdefault(p[:-1], set()).add(p[-1])
    return {k: frozenset(v) for k, v in out.items()}


def root_kind_for(doc_kind: str) -> str:
    """The root kind of a mapping for a document kind (``Document.info.kind``)."""
    if doc_kind in _RECORD_DOC_KINDS:
        return "records"
    if doc_kind == "wrapped":
        return "wrapped"
    if doc_kind == "keyed":
        return "keyed"
    if doc_kind == "single":
        return "value"
    raise ValueError(f"unknown document kind {doc_kind!r}")


# ----------------------------------------------------------------------------------
# mapping files
# ----------------------------------------------------------------------------------
def _node_fields(n: MapNode) -> dict[str, Any]:
    """The file fields of one node (no children, no id)."""
    d: dict[str, Any] = {"key": n.key, "kind": n.kind, "src": _segs_out(n.src)}
    if n.is_root:
        d["is_root"] = True
    if n.is_elem:
        d["is_elem"] = True
    if n.synthetic:
        d["synthetic"] = True
    if n.rec_key is not None:
        d["rec_key"] = n.rec_key
    if n.computed is not None:
        d["computed"] = n.computed
    if n.ctx is not None:
        d["ctx"] = list(n.ctx)
    d["types"] = list(n.types)
    d["count"] = n.count
    d["pcount"] = n.pcount
    d["samples"] = list(n.samples)
    d["open"] = n.open
    return d


def node_to_dict(node: MapNode) -> dict[str, Any]:
    """A node and its subtree in the mapping-file format (no ids), built iteratively."""
    out: dict[str, Any] | None = None
    stack: list[tuple[MapNode, list | None]] = [(node, None)]
    while stack:
        n, dest = stack.pop()
        d = _node_fields(n)
        kids: list[dict[str, Any]] = []
        d["children"] = kids
        if dest is None:
            out = d
        else:
            dest.append(d)
        stack.extend((c, kids) for c in reversed(n.children))
    assert out is not None
    return out


_INDENT_CAP = 40  # nodes deeper than this are not indented further
_NL = b"\n"


def _encode_tree(root: MapNode, base: int) -> bytes:
    """A node tree as JSON, one node per line (its fields compact, children below it, indented
    by depth up to a cap). Iterative: any depth, linear in the output size."""
    parts: list[bytes] = []
    # (node, depth, is_last_child) entries, plus b"]}" closers carrying their own indentation
    stack: list[Any] = [(root, 0, True)]
    first = True
    while stack:
        item = stack.pop()
        if isinstance(item, bytes):
            parts.append(item)
            continue
        n, d, last = item
        head = codec.dumps_transient(_node_fields(n))[:-1]     # (the slice is a compact copy)
        pad = b"" if first else _NL + b" " * (base + min(d, _INDENT_CAP))
        first = False
        tail = b"" if last else b","
        if not n.children:
            parts.append(pad + head + b', "children": []}' + tail)
            continue
        parts.append(pad + head + b', "children": [')
        stack.append(_NL + b" " * (base + min(d, _INDENT_CAP)) + b"]}" + tail)
        kids = n.children
        for i in range(len(kids) - 1, -1, -1):
            stack.append((kids[i], d + 1, i == len(kids) - 1))
    return b"".join(parts)


def dumps_mapping(tree: MappingTree, *, input_info: dict | None = None, orig: MappingTree | None = None) -> bytes:
    """The mapping file bytes of ``tree`` (see the module docstring). ``orig`` (default
    ``tree.orig``) is stored as ``designer.orig`` when it differs from ``tree``."""
    if orig is None:
        orig = tree.orig
    orig_root = None
    if orig is not None and _plain_seq(orig.root) != _plain_seq(tree.root):
        orig_root = orig.root
    dumps = codec.dumps_transient       # every text is concatenated at once
    parts = [b"{" + _NL + b' "version": ' + str(MAPPING_VERSION).encode(),
             b' "input": ' + dumps(_json_safe(dict(input_info or {}))),
             b' "root_kind": ' + dumps(tree.root_kind),
             b' "tree": ' + _encode_tree(tree.root, 2)]
    known = _known_extra(tree.known_paths, tree.root, orig_root)
    if known is not None:
        paths = _known_out(known) or []
        parts.append(b' "known_paths": [' + b",".join(_NL + b"  " + dumps(p) for p in paths)
                     + (_NL + b" ]" if paths else b"]"))
    if orig_root is not None:
        parts.append(b' "designer": {"orig": ' + _encode_tree(orig_root, 2) + b"}")
    return (b"," + _NL).join(parts) + _NL + b"}" + _NL


def _plain_seq(root: MapNode) -> list[tuple]:
    """A comparable flat form of a subtree without ids: pre-order fields + child counts."""
    return [(n.key, n.kind, _tup(n.src), len(n.children), _tup(n.types), n.count, n.pcount, _tup(n.samples),
             n.synthetic, n.rec_key, n.computed, _tup(n.ctx), n.open, n.is_elem, n.is_root) for n in _iter_pre(root)]


def _json_safe(v: Any) -> Any:
    """``input_info`` values as JSON: paths as text, tuples as lists (iterative)."""
    def conv(x: Any) -> Any:
        if isinstance(x, os.PathLike):
            return os.fspath(x)
        if isinstance(x, tuple):
            return list(x)
        return x

    root: list[Any] = [conv(v)]
    stack: list[tuple[Any, Any]] = [(root, 0)]
    while stack:
        container, key = stack.pop()
        x = container[key]
        if isinstance(x, dict):
            x = container[key] = {str(k): conv(val) for k, val in x.items()}
            stack.extend((x, k) for k in x)
        elif isinstance(x, list):
            x = container[key] = [conv(val) for val in x]
            stack.extend((x, i) for i in range(len(x)))
    return root[0]


def save_mapping(tree: MappingTree, path: str | os.PathLike, *, input_info: dict | None = None,
                 orig: MappingTree | None = None) -> None:
    """Write the mapping file atomically (:func:`dumps_mapping`)."""
    from .fileio import atomic_write

    data = dumps_mapping(tree, input_info=input_info, orig=orig)
    with atomic_write(path) as f:
        f.write(data)


class _Ids:
    def __init__(self) -> None:
        self.last = 0

    def __call__(self) -> int:
        self.last += 1
        return self.last


def _short(v: Any) -> str:
    try:
        text = codec.dumps(v).decode("utf-8", "replace")
    except Exception:
        text = repr(v)
    return text if len(text) <= 60 else text[:59] + "…"


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _segments(v: Any, pointer: str, key: str) -> tuple | None:
    if v is None:
        return None
    if not isinstance(v, list) or not all(s is None or isinstance(s, str) for s in v):
        raise MappingError(key, pointer=pointer)
    return tuple(v)


def tree_from_dict(obj: Any, *, pointer: str = "/tree", ids: Callable[[], int] | None = None) -> MapNode:
    """Validate a mapping-file tree and build its nodes (ids in pre-order from ``ids``).
    :class:`MappingError` (``mapping.error.*``) names the JSON pointer of the bad node."""
    new_id = ids if ids is not None else _Ids()
    root: MapNode | None = None
    stack: list[tuple[Any, str, MapNode | None]] = [(obj, pointer, None)]
    while stack:
        d, ptr, parent = stack.pop()
        top = parent is None
        if not isinstance(d, dict):
            raise MappingError("mapping.error.node", pointer=ptr)
        kind = d.get("kind", "value")
        if kind not in NODE_KINDS:
            raise MappingError("mapping.error.kind", pointer=ptr, kind=_short(kind))
        key = d.get("key", "")
        if not isinstance(key, str):
            raise MappingError("mapping.error.field", pointer=ptr, field="key")
        flags = {}
        for name, default in (("is_root", top), ("is_elem", False), ("synthetic", False), ("open", False)):
            v = d.get(name, default)
            if not isinstance(v, bool):
                raise MappingError("mapping.error.field", pointer=ptr, field=name)
            flags[name] = v
        rec_key = d.get("rec_key")
        if rec_key is not None and not isinstance(rec_key, str):
            raise MappingError("mapping.error.field", pointer=ptr, field="rec_key")
        computed = d.get("computed")
        if computed is not None and computed != "depth":
            raise MappingError("mapping.error.computed", pointer=ptr)
        src = _segments(d.get("src", []), ptr, "mapping.error.src")
        ctx = _segments(d.get("ctx"), ptr, "mapping.error.ctx")
        types = d.get("types", [])
        if not isinstance(types, list) or not all(isinstance(x, str) and x in TYPE_ORDER for x in types):
            raise MappingError("mapping.error.field", pointer=ptr, field="types")
        count = d.get("count", 0)
        if not _is_int(count) or count < 0:
            raise MappingError("mapping.error.field", pointer=ptr, field="count")
        pcount = d.get("pcount")
        if pcount is not None and (not _is_int(pcount) or pcount < 0):
            raise MappingError("mapping.error.field", pointer=ptr, field="pcount")
        samples = d.get("samples", [])
        if not isinstance(samples, list) or not all(isinstance(x, str) for x in samples):
            raise MappingError("mapping.error.field", pointer=ptr, field="samples")
        children = d.get("children", [])
        if not isinstance(children, list):
            raise MappingError("mapping.error.field", pointer=ptr, field="children")
        is_root, is_elem, synthetic = flags["is_root"], flags["is_elem"], flags["synthetic"]
        # structure rules
        if top:
            if not is_root or is_elem:
                raise MappingError("mapping.error.root", pointer=ptr)
            if src != ():
                raise MappingError("mapping.error.root_src", pointer=ptr)
        elif is_root:
            raise MappingError("mapping.error.root", pointer=ptr)
        if parent is not None:
            if parent.kind == "array" and not is_elem:
                raise MappingError("mapping.error.array_children", pointer=ptr)
            if parent.kind != "array" and is_elem:
                raise MappingError("mapping.error.elem_outside_array", pointer=ptr)
        if synthetic:
            if kind != "object" or is_elem or computed is not None:
                raise MappingError("mapping.error.synthetic", pointer=ptr)
            if src is not None:
                raise MappingError("mapping.error.synthetic_src", pointer=ptr)
        if computed is not None:
            if kind != "value" or is_elem or not ctx or ctx[-1] is not WILD:
                raise MappingError("mapping.error.computed", pointer=ptr)
            if src is not None:
                raise MappingError("mapping.error.synthetic_src", pointer=ptr)
        elif ctx is not None:
            raise MappingError("mapping.error.ctx", pointer=ptr)
        if is_elem:
            if not src or src[-1] is not WILD:
                raise MappingError("mapping.error.elem_src", pointer=ptr)
        elif not top and not synthetic and computed is None:
            if not src or src[-1] is WILD:
                raise MappingError("mapping.error.field_src", pointer=ptr)
        if rec_key is not None and (not is_elem or kind != "object"):
            raise MappingError("mapping.error.rec_key", pointer=ptr)
        if kind == "value" and children:
            raise MappingError("mapping.error.value_children", pointer=ptr)
        if kind == "array" and not children:
            raise MappingError("mapping.error.array_children", pointer=ptr)
        n = MapNode(id=new_id(), key=key, kind=kind, src=src, types=tuple(types), count=count, pcount=pcount,
                    samples=tuple(samples), synthetic=synthetic, rec_key=rec_key, computed=computed, ctx=ctx,
                    open=flags["open"], is_elem=is_elem, is_root=is_root)
        if parent is None:
            root = n
        else:
            parent.children.append(n)
        for i in range(len(children) - 1, -1, -1):
            stack.append((children[i], f"{ptr}/children/{i}", n))
    assert root is not None
    return root


def _parse_iterative(data: bytes) -> Any:
    """JSON decoding without recursion, for trees deeper than the decoders allow: the events of
    :func:`jsonreshape.stream.exact_events` (pure Python, any depth, values exactly as
    :func:`jsonreshape.codec.loads` gives them, duplicate keys with dict semantics). Never ijson: its
    yajl2_c backend kills the process on an integer token of more than ``sys.get_int_max_str_digits()``
    digits (review R5A-X1) and a mapping file is untrusted input. The bytes are decoded strictly first
    (``exact_events`` would stop quietly at the first invalid byte; ``codec.loads`` refuses it)."""
    from .stream import exact_events

    text = data.decode("utf-8")              # UnicodeDecodeError: the caller reports mapping.error.json
    root: Any = None
    stack: list[Any] = []
    keys: list[Any] = []

    def add(v: Any) -> None:
        nonlocal root
        if not stack:
            root = v
        elif isinstance(stack[-1], list):
            stack[-1].append(v)
        else:
            stack[-1][keys[-1]] = v

    for event, value in exact_events(text):
        if event == "start_map":
            d: dict = {}
            add(d)
            stack.append(d)
            keys.append(None)
        elif event == "map_key":
            keys[-1] = value
        elif event == "end_map":
            stack.pop()
            keys.pop()
        elif event == "start_array":
            lst: list = []
            add(lst)
            stack.append(lst)
            keys.append(None)
        elif event == "end_array":
            stack.pop()
            keys.pop()
        else:
            add(value)
    return root


def _parse_mapping_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return codec.loads(data)
    except codec.JsonDecodeError as exc:
        first = exc
    try:
        return _parse_iterative(data)
    except Exception:
        raise MappingError("mapping.error.json", detail=first.params.get("detail") or first.message) from None


def load_mapping(path_or_bytes: str | os.PathLike | bytes | bytearray | memoryview) -> tuple[MappingTree, dict]:
    """Read and validate a mapping file: ``(tree, input info)``. ``tree.orig`` holds
    ``designer.orig`` and ``tree.known_paths`` the stored known paths when present.
    :class:`MappingError` on every problem (with the JSON pointer of a bad node); OS errors
    are localized like everywhere else."""
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        data = bytes(path_or_bytes)
    else:
        from .fileio import open_shared, os_error

        path = Path(os.fspath(path_or_bytes))
        try:
            with open_shared(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size > MAX_FILE_BYTES:
                    raise MappingError("mapping.error.file_too_big", path=str(path), size=size,
                                       limit=MAX_FILE_BYTES)
                data = f.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise os_error(exc, path) from exc
        if len(data) > MAX_FILE_BYTES:
            raise MappingError("mapping.error.file_too_big", path=str(path), size=len(data), limit=MAX_FILE_BYTES)
    doc = _parse_mapping_json(data)
    if not isinstance(doc, dict):
        raise MappingError("mapping.error.not_object")
    version = doc.get("version")
    if not _is_int(version) or version != MAPPING_VERSION:
        raise MappingError("mapping.error.version", version=_short(version))
    info = doc.get("input", {})
    if info is None:
        info = {}
    if not isinstance(info, dict):
        raise MappingError("mapping.error.input")
    if "tree" not in doc:
        raise MappingError("mapping.error.no_tree")
    ids = _Ids()
    root = tree_from_dict(doc["tree"], pointer="/tree", ids=ids)
    root_kind = doc.get("root_kind")
    if root_kind is not None:
        if root_kind not in ROOT_KINDS:
            raise MappingError("mapping.error.root_kind_name", kind=_short(root_kind))
    else:
        try:
            root_kind = root_kind_for(info.get("kind"))  # type: ignore[arg-type]
        except ValueError:
            root_kind = "records" if root.kind == "array" else "value"
    if root_kind == "records" and root.kind != "array":
        raise MappingError("mapping.error.root_kind", pointer="/tree", kind=root.kind, root_kind=root_kind)
    record_src = None
    rp = info.get("record_path")
    if root_kind == "wrapped" and rp is not None:
        if not isinstance(rp, list) or not all(isinstance(s, str) or _is_int(s) for s in rp):
            raise MappingError("mapping.error.record_path")
        record_src = _record_src(rp)
    orig = None
    designer = doc.get("designer")
    if designer is not None:
        if not isinstance(designer, dict):
            raise MappingError("mapping.error.designer", pointer="/designer")
        if designer.get("orig") is not None:
            orig_root = tree_from_dict(designer["orig"], pointer="/designer/orig", ids=ids)
            if root_kind == "records" and orig_root.kind != "array":
                raise MappingError("mapping.error.root_kind", pointer="/designer/orig", kind=orig_root.kind,
                                   root_kind=root_kind)
            orig = MappingTree(orig_root, root_kind=root_kind, record_src=record_src)
    known = None
    kp = doc.get("known_paths")
    if kp is not None:
        if not isinstance(kp, list):
            raise MappingError("mapping.error.known_paths", pointer="/known_paths")
        paths = []
        for i, p in enumerate(kp):
            segs = _segments(p, f"/known_paths/{i}", "mapping.error.known_paths")
            if segs is None:
                raise MappingError("mapping.error.known_paths", pointer=f"/known_paths/{i}")
            paths.append(segs)
        known = frozenset(paths) | _tree_paths(root, orig.root if orig is not None else None)
    tree = MappingTree(root, next_id=ids.last + 1, root_kind=root_kind, known_paths=known, record_src=record_src,
                       orig=orig)
    if orig is not None:
        orig.next_id = tree.next_id
        orig.known_paths = known
    return tree, dict(info)


def make_input_info(doc: Any, *, sample: int | None = None, full_scan: bool = False) -> dict[str, Any]:
    """The ``input`` section of a mapping file for a :class:`~jsonreshape.reader.Document`."""
    from . import __version__

    info = doc.info
    rp = info.record_path
    return {
        "source": str(doc.source_path),
        "kind": info.kind,
        "record_path": list(rp) if rp is not None else None,
        "sample": sample,
        "full_scan": bool(full_scan),
        "records": int(doc.record_count),
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app": __version__,
    }



# ----------------------------------------------------------------------------------
# the default tree of a document (CLI ``flatten``, the GUI structure builder)
# ----------------------------------------------------------------------------------
@dataclass
class DocumentTree:
    """The default mapping tree of a document (:func:`infer_document`) and what it was made of."""

    tree: MappingTree
    wrapper: Any = None                 # wrapped documents: the parsed wrapper, its record array empty
    record_path: tuple | None = None    # wrapped documents: the path of the record array in the wrapper
    source: list[int] | None = None     # the records read (positions); None = every record
    values: int = 0                     # record values the tree was inferred from
    skipped: int = 0                    # huge records left out (> Settings.large_node_bytes)
    total: int = 0                      # records of the document

    @property
    def full(self) -> bool:
        """Every record was read (a full scan, or a sample that covers every record)."""
        return self.source is None


def document_wrapper(doc: Any, *, cancel: CancelToken | None = None) -> Any:
    """The parsed wrapper of the wrapped document ``doc`` with an empty record array (``prefix +
    "[]" + suffix``, exact numbers): the ``wrapper`` of :func:`infer_tree` and :class:`Designer`.
    :class:`MappingError` ``engine.error.member_container`` when the records are object members
    (the engine refuses them) and ``engine.error.partial_wrapper`` when the record array never
    closes (a partial document)."""
    if getattr(doc, "member_records", False):
        raise MappingError("engine.error.member_container")
    parts = doc.wrapper_parts(cancel=cancel)
    if parts is None:
        raise MappingError("engine.error.partial_wrapper")
    prefix, suffix = parts
    return codec.loads(bytes(prefix) + b"[]" + bytes(suffix))


def sample_source(total: int, sample: int | None) -> list[int] | None:
    """The records a default tree is inferred from: ``sample`` evenly spread positions of
    :func:`jsonreshape.schema.sample_positions` (the schema view's sample); ``None`` = every
    record (``sample`` is ``None`` -- a full scan -- or the sample covers every record)."""
    if sample is None:
        return None
    from .schema import sample_positions

    total = max(0, int(total))
    positions = sample_positions(total, max(1, int(sample)))
    return positions if len(positions) < total else None


def document_values(doc: Any, source: Any = None, *, progress: Progress | None = None,
                    cancel: CancelToken | None = None, counts: list[int] | None = None) -> Iterator[Any]:
    """The parsed values (exact numbers) of the records ``source`` (positions; ``None``: every
    record) of the pinned document ``doc`` in that order, read in blocks; keyed documents: the
    member values. Records larger than ``Settings.large_node_bytes`` are left out. ``counts``
    (``[values, skipped]``, optional) counts the values given and the huge records left out.
    Progress in bytes of the records read; ``cancel`` is checked."""
    for i, value in doc.iter_records(source, include_huge=True, progress=progress, cancel=cancel):
        if value is None and doc.is_huge(i):
            if counts is not None:
                counts[1] += 1
            continue
        if counts is not None:
            counts[0] += 1
        yield value


def infer_document(doc: Any, *, sample: int | None = None, progress: Progress | None = None,
                   cancel: CancelToken | None = None) -> DocumentTree:
    """The default mapping tree of the pinned document ``doc`` (docs/ARCHITECTURE.md §13.0 "Tree
    source"; the CLI's ``flatten`` and the GUI's structure builder both infer it here): from
    ``sample`` records at the positions of :func:`sample_source` (``None``: every record -- a full
    scan). Record documents map their records; wrapped documents add the wrapper
    (:func:`document_wrapper`); keyed documents map the member values; a single document its
    value -- at most ``Settings.large_node_bytes``, else ``MappingError("engine.error.too_large")``.
    Huge records are left out (counted in :attr:`DocumentTree.skipped`). Progress in bytes of the
    records read (for a sample without a total yet, the total is set to the size of the sampled
    records); ``cancel`` is checked."""
    root_kind = root_kind_for(doc.kind)
    total = int(doc.record_count)
    wrapper: Any = None
    record_path: tuple | None = None
    if root_kind == "wrapped":
        wrapper = document_wrapper(doc, cancel=cancel)
        record_path = tuple(doc.record_path or ())
    elif root_kind == "value" and total:
        s, e = doc.record_span(0)
        large = int(doc.settings.large_node_bytes)
        if e - s > large:
            raise MappingError("engine.error.too_large", size=format_bytes(e - s), limit=format_bytes(large))
    source = sample_source(total, sample)
    if progress is not None and source is not None and getattr(progress, "total", None) is None:
        progress.set_total(sum(e - s for s, e in map(doc.record_span, source)))
    counts = [0, 0]
    tree = infer_tree(document_values(doc, source, progress=progress, cancel=cancel, counts=counts),
                      root_kind=root_kind, wrapper=wrapper, record_path=record_path, cancel=cancel)
    return DocumentTree(tree=tree, wrapper=wrapper, record_path=record_path, source=source, values=counts[0],
                        skipped=counts[1], total=total)
