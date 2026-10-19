"""Bulk operations: preview -> exact counts -> apply (docs/ARCHITECTURE.md §12.6, the bulk part;
the duplicate finder is :mod:`jsonreshape.dupes`).

A bulk operation is described by a :class:`BulkSpec` (``kind`` + ``params``). :func:`preview` runs the
operation over a *scope* of records of one pinned edit state and computes its exact result at once (§12.0
"Bulk rules"): every changed record is re-serialized in the document's style, validated and appended to a
segment of the edit layer of its own (``EditStore.new_segment()``), and the ``(rid, span)`` pairs go to a
*pending array layer* file (:func:`jsonreshape.overlay.write_array_layer`, fsynced). The returned
:class:`BulkPreview` holds the counts, the first 5 examples (before -> after) and the first 5 problems.
:func:`make_step` turns a preview into the :class:`~jsonreshape.editstore.Step` the edit session commits:
content operations push the pending layer (``push_layer`` + ``segments=[the preview's segment]``),
``dedupe`` is a structural step (``struct=[("dels", positions)]``). Because the applied step IS the
previewed result, the pre-count equals the applied change exactly (AGENT_PROMPT §14 test 20).
:meth:`BulkPreview.discard` deletes the segment and the layer file of a preview that is not applied.

Operations (§12.6 table; every path is relative to the record VALUE -- for keyed / wrapped-object
documents the member value; record keys are never touched)
===========================================================================================

``replace`` (``find``, ``replacement`` = ``""``, ``mode`` = ``"text"`` | ``"regex"``, ``case_sensitive`` =
False, ``whole_word`` = False, ``path`` = None)
    String VALUES (never keys) whose path passes ``pattern.matches_prefix_of(path)`` (the search rule; no
    path = every string). Text mode: ``TextQuery(find, "plain", case_sensitive, whole_word)`` (case
    folding, apostrophe equivalence ``' ‘ ’ ʻ ʼ`` ..., ``ß`` ~ ``ss``, the Uzbek whole-word rule; no
    transliteration) -- every span of ``find_spans(s)`` is replaced by ``replacement`` as LITERAL text.
    Regex mode: the TextQuery's compiled pattern (same flags, same whole-word guards) ``.sub(replacement,
    s)``, i.e. Python template syntax (``\\1``, ``\\g<name>``, ``\\n``; ``\\\\`` for a backslash); an invalid
    template is refused by :meth:`BulkSpec.validate`. ``values_changed`` = strings whose text changed.
``rename_key`` (``old``, ``new`` != ``old``, ``path`` = None)
    In every object whose path ``pattern.matches(path)`` (exactly; no path = every object; ``**``-patterns
    select nested objects too) the member ``old`` becomes ``new`` at the same position. An object that
    already has ``new`` is a conflict (one per object; the other objects are still renamed). Objects are
    selected by their ORIGINAL paths (a rename never changes which objects are selected).
    ``values_changed`` = renamed members.
``delete_field`` (``path``)
    Removes every object member whose path ``pattern.matches`` it, outermost matches only (the value of a
    removed member is not visited); array elements are never fields (not removed, but searched). A path
    without segments (the record itself) is refused. ``values_changed`` = removed members.
``set_field`` (``path`` (concrete, non-empty), ``value`` (JSON text), ``mode`` = ``"always"`` |
``"if_missing"`` | ``"if_exists"``)
    Sets the value, creating missing intermediate OBJECTS (``always`` / ``if_missing``). A value already
    equal to the new one (query equality: exact numbers, deep, no member order) is no change. Conflicts
    (counted per record, nothing changed): a parent that is not an object where a key follows
    (``bulk.problem.not_object``; also a scalar or ``null`` record), an object where an index follows
    (``not_array``), an array without the element (``no_element``; elements are never created), a missing
    parent that would have to be an array (``missing_array``). ``if_exists`` only replaces existing values
    and never reports a conflict (a field that cannot exist does not exist). ``values_changed`` = records
    whose value changed (= ``records_changed``).
``clean_strings`` (``trim`` = False, ``collapse_spaces`` = False, ``path`` = None; at least one on)
    String VALUES on the ``replace`` path rule: ``trim`` = ``str.strip()``, ``collapse_spaces`` =
    ``re.sub(r"\\s+", " ", s)`` (exactly the ``str.isspace`` characters; checked). ``values_changed`` =
    strings changed.
``convert_type`` (``path`` (a pattern, ``"$"`` = the record value itself), ``to``)
    :func:`jsonreshape.editops.convert_value` on the outermost nodes whose path ``pattern.matches`` it
    (members, elements or the record value); nothing below a matched node is visited, whatever the
    outcome. Values already of the target type are untouched; an impossible conversion is a failure (the
    value stays). ``values_changed`` = values converted.
``dedupe`` (``key`` (concrete path or ``"$key"``), ``keep`` = ``"first"`` | ``"last"``)
    :func:`jsonreshape.dupes.find_duplicates`; ``records_changed`` = records deleted, ``values_changed``
    = 0, ``failures`` = records whose key could not be read (``DuplicateReport.skipped``), ``warnings`` =
    the report's warnings; the first problem record of each warning is listed in ``problems``.

Counting (what the naive reference of the tests implements)
===========================================================
* ``scanned``: records of the scope (each position once), huge ones included.
* Every record of the scope larger than ``Settings.large_node_bytes`` counts in ``skipped_huge`` (whether
  or not the operation would touch it: huge records are never parsed, §12.0); so does a changed record
  whose new unit would be larger than the limit (no producer writes a unit above it).
* A record whose value changes is checked for duplicate keys (the ``json`` C scanner with an
  ``object_pairs_hook``; keys compared after decoding escapes, so ``"a"`` and ``"\\u0061"`` are
  duplicates): such a record is skipped and counts as ONE conflict (``bulk.problem.duplicate_keys``).
  Unchanged records are not checked (parsing follows dict semantics everywhere: the last duplicate wins).
* A skipped record (duplicate keys, result too large) contributes only its skip; otherwise a record's
  conflicts and failures count whether or not it changed.
* A record that cannot be parsed in memory (nesting deeper than the decoder allows) counts as a failure
  (``bulk.problem.unreadable``).
* ``records_changed``: records written; ``bytes_new``: bytes of their new units.

Examples and problems: the first :data:`EXAMPLE_COUNT` value changes and the first
:data:`PROBLEM_COUNT` conflicts/failures in position order (document order inside a record). ``path`` is
the dotted path (``"$"`` for the record value), ``before`` / ``after`` the compact JSON text of the
values (keys of ``rename_key`` as JSON strings; ``(deleted)`` / ``(missing)`` markers) cut to
:data:`TEXT_CHARS` characters; a problem's ``after`` is the localized reason.

Scope, pinning and order
========================
``scope``: ``None`` or the document's base RecordSet (all records), a ``range``, a RecordSet, a list /
tuple / ``array`` or any iterable of positions. Its ``gen`` must be ``None`` or ``state.gen``; a RecordSet
made on the plain file (``gen == 0``) is also accepted while ``state`` shows the file as it is (an initial
or fully undone state: positions are the same), else ``EditError("bulk.error.stale_scope")`` -- as for a
position outside ``0 .. count-1``. The records are read through ``doc_pinned.pinned(state)`` (the view of
exactly that state; the plain-file view for a ``gen == 0`` scope), in ASCENDING position order: an
unordered or repeating scope is turned into a bitmap of ``record_count`` bits first (each position is
examined once).

Speed: candidate records are found with ``core.MultiFinder`` where that is sound -- the needles of
``TextQuery.prefilter_needles(doc.flags)`` (text-mode ``replace``), and the JSON token of the key for
``rename_key`` / ``delete_field`` (pattern ending in a key) when ``doc.flags`` has no ``FLAG_ESCAPES``
(without escapes a key can only be written one way) -- other records are not parsed. Records are parsed
with :func:`jsonreshape.codec.loads` (exact numbers: ``0.1``, ``1e400``, ``12345678901234567890``,
``-0.0``, ``1.10`` stay as written) and changed ones written with :func:`jsonreshape.codec.dumps` +
:func:`jsonreshape.editops.format_record` in the style of :func:`jsonreshape.editops.detect_style`
(keyed: the original member head ``"key": `` is kept; for a compact ``":"`` / ``","`` style, whose
``format_record`` output is ``codec.dumps``' whitespace-free text byte for byte, the Reformatter pass is
skipped -- the tests compare every unit with ``format_record``). Units are validated exactly like
:func:`jsonreshape.editops.check_unit` does it (the core ``Scanner``; values must be one value, members
one member, no outer whitespace), but in ONE scanner pass per append batch of :data:`APPEND_BYTES`
(values as a ``MODE_STREAM`` text joined by newlines, members as ``{`` + units joined by ``,`` + ``}`` in
``MODE_CONTAINER``; every unit must be exactly one emitted span); any difference falls back to
``check_unit`` per unit (``edits.error.internal`` for a bad unit: a bug). The scanner flags become the
state's flags. Documented deviation (§12.6): a changed record is re-serialized -- string escapes are
normalized (``"\\u00e9"`` -> ``"é"``) and the whitespace inside it follows the detected style; unchanged
records keep every byte.

More speed (stage-3 performance work; results are the same byte for byte, the tests compare both paths):

* **Worker processes** (like ``engine.apply``, docs/ARCHITECTURE.md §13.2): a scope of at least
  :data:`POOL_MIN_BYTES` is worked on by :func:`auto_workers` processes (``min(cpu_count - 1, 8)``, at most
  half of ``Settings.memory_limit_bytes`` at :data:`WORKER_MEMORY` each: 4 with the default budget;
  ``spawn``, only when the main module cannot run the caller's program again, :func:`_spawn_safe`).
  The parent reads the blocks (:data:`POOL_BLOCK_BYTES` of records each, at most ``2 * workers`` in
  flight), a worker does everything a record needs (:class:`_Runner`: prefilter, parse, rule,
  duplicate-key check, unit, validation) and returns the block's units, counts and first examples /
  problems; the parent merges the blocks in position order (counts, the first examples / problems, the
  units appended in batches with the free-space checks, the layer entries), checks ``cancel`` while it
  waits and keeps the progress. When the workers cannot run (``BrokenProcessPool``), the pass starts again
  in this thread. ``preview(..., workers=n)`` asks for a number (``1``: this thread).
* **Duplicate keys** of a changed record: a record whose text is byte for byte what ``codec.dumps``
  writes for its parsed value cannot repeat a key, so that comparison (the value dumped BEFORE the rule
  changes it) replaces the ``json`` decode while it pays (compact documents, most parsed records changing;
  :class:`_Runner`); records with :data:`FAST_DUP_DEPTH` or more brackets always take the decoder.
* The rules walk a parsed record recursively down to ``_WALK_DEPTH`` levels (an explicit stack below).
  Text ``replace`` checks every string with the prefilter needles of unescaped text first (a sound
  necessary condition, one MultiFinder call with the Rust core) before ``TextQuery.find_spans``.
* A block's units are copied into one buffer as they are made (an ``orjson`` result occupies ~4 KiB
  whatever its length: a block's worth of them must not be kept).

Disk space, progress, cancellation, threads
===========================================
Before the pass the free space of the layer folder must hold ``scope bytes * 1.2 + 1 GiB``
(:data:`DISK_FACTOR`, :data:`DISK_RESERVE`); every :data:`DISK_CHECK_BYTES` written the remaining part
(``(scope bytes - bytes read) * 1.2 + 1 GiB``) is checked again: ``EditError("bulk.error.disk_space")``.
``dedupe`` writes no units and is not checked. Progress is reported in bytes of the records read: the
scan fills the bar up to :data:`SCAN_END` (``dedupe``: the duplicate finder up to :data:`DEDUPE_END`, then
the keys of member records), writing the layer the rest; ``cancel`` is checked between blocks and every
:data:`CHECK_RECORDS` records. On any error -- cancellation included -- the preview's segment, layer file
and temporary files are deleted. When the pass has finished, ``doc.ensure_unchanged()`` makes sure the
file was not changed meanwhile (``FileChangedError``). A preview may run in any thread while the session
commits other steps: it reads one frozen state and writes only its own segment, layer file and
temporary files (the store's methods are locked); its result is applied only to the same ``gen``.

Extensions of §12.6 (documented decisions)
==========================================
* :class:`BulkPreview` also has ``warnings`` (localized texts), ``layer`` (file name of the pending
  layer, ``None`` when nothing changed), ``segment``, ``flags`` (``(old, new)`` state flags),
  ``records`` (changed positions, ``None`` when more than ``overlay.RECORDS_MAX``), ``positions`` and
  ``keys`` (``dedupe``: the positions to delete; ``(rid, key, None)`` for member-record documents),
  ``scope_gen`` and ``discarded``.
* :func:`make_step` refuses a preview that changes nothing (``UserInputError("bulk.error.nothing")``)
  or was discarded (``EditError("bulk.error.discarded")``). The step: kind ``"bulk"``, label
  ``edits.step.bulk.<kind>`` with params ``count`` (``values_changed``; ``records_changed`` for
  ``set_field`` and ``dedupe``), ``kind`` and ``rule`` (the normalized params); content operations:
  ``push_layer``, ``segments``, ``flags`` (only when they change), ``records``; ``dedupe``:
  ``struct=[("dels", positions)]``, ``records=[]``, ``keys`` (member-record documents).
* :meth:`BulkPreview.discard` is safe at any time: after the step made by :func:`make_step` was
  committed (``Step.seq > 0``) the files belong to the layer and are never deleted (the store refuses as
  well); after a failed commit they are deleted.
* :meth:`BulkSpec.validate` is strict: unknown parameters and wrongly typed values are refused
  (``bulk.error.unknown_param`` / ``param_type``); ``set_field`` and ``delete_field`` refuse an empty
  path; a regex replacement template is checked. :meth:`BulkSpec.label` never raises.
* :func:`kind_title` (localized name of a kind) and :func:`set_core_backend` (tests: the core backend
  of the MultiFinder prefilter and of the unit check; worker processes get the same backends and the
  caller's UI language).
* ``preview(..., workers=None)`` (``None``: automatic) and ``BulkPreview.workers`` (worker processes
  used, ``1`` = this thread); :func:`auto_workers`.
* Uses ``TextQuery.regex`` / ``TextQuery.regex_postfilter`` (the compiled pattern the spec asks for),
  ``textmatch.is_whole_word`` (the search's whole-word rule) and ``query.json_equal`` (query equality).
"""

from __future__ import annotations

import bisect
import json
import logging
import operator
import os
import re
import secrets
import shutil
import struct
import sys
import time
from array import array
from dataclasses import dataclass, field
from itertools import accumulate, repeat
from operator import add
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO, Callable, Iterable, Iterator

from . import codec, core, dupes, editops
from .core import FLAG_ESCAPES, MODE_CONTAINER, MODE_STREAM
from .editstore import Step
from .errors import ConversionError, EditError, JsonReshapeError, UserInputError
from .fileio import open_shared
from .i18n import t
from .overlay import RECORDS_MAX, EntryBlocks, OverlayState, write_array_layer
from .paths import MISSING, PathPattern, Wild, canonical_index, format_path, parse_path, parse_pattern
from .query import json_equal
from .reader import _edited_state, split_member
from .recordset import RecordSet
from .tasks import CancelToken, Progress, check, format_bytes
from .textmatch import TextQuery, is_whole_word, regex_error_text

log = logging.getLogger(__name__)

__all__ = [
    "CHECK_RECORDS",
    "POOL_MIN_BYTES",
    "DISK_CHECK_BYTES",
    "DISK_FACTOR",
    "DISK_RESERVE",
    "EXAMPLE_COUNT",
    "KINDS",
    "PROBLEM_COUNT",
    "TEXT_CHARS",
    "BulkExample",
    "BulkPreview",
    "BulkSpec",
    "auto_workers",
    "kind_title",
    "make_step",
    "preview",
    "set_core_backend",
]

KiB = 1 << 10
MiB = 1 << 20
GiB = 1 << 30

KINDS = ("replace", "rename_key", "delete_field", "set_field", "clean_strings", "convert_type", "dedupe")
EXAMPLE_COUNT = 5              # examples kept (the first changes in position order)
PROBLEM_COUNT = 5              # problems kept (the first conflicts / failures)
TEXT_CHARS = 120               # example texts are cut to this many characters
DISK_FACTOR = 1.2              # free space needed: scope bytes * DISK_FACTOR + DISK_RESERVE
DISK_RESERVE = 1 * GiB
DISK_CHECK_BYTES = 256 * MiB   # the free space is checked again every this many bytes written
APPEND_BYTES = 1 * MiB         # units are appended to the preview's segment in batches of this size
ENTRY_BATCH = 1 << 20          # layer entries kept in memory before they are spilled to a temporary file
CHECK_RECORDS = 256            # records between cancellation checks inside a block
SCAN_END = 0.95                # the scan fills the progress bar up to here; writing the layer the rest
DEDUPE_END = 0.9               # dedupe: the duplicate finder fills the bar up to here
NFA_CACHE_MAX = 65536          # cached steps of a path pattern automaton (cleared beyond)

_SET_MODES = ("always", "if_missing", "if_exists")
_REPLACE_MODES = ("text", "regex")
_TYPES = editops.JSON_TYPES
_WS_RUN = re.compile(r"\s+")
_NONZERO = re.compile(rb"[^\x00]+")
_BIT_OFFSETS = tuple(tuple(k for k in range(8) if b >> k & 1) for b in range(256))
_ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
_BIG_ENDIAN = sys.byteorder == "big"
_REQUIRED: Any = object()
_DEAD: frozenset[int] = frozenset()
_SPILL_HEAD = struct.Struct("<Q")

# value markers of the example texts
_DELETED: Any = object()

# the parameters of every kind with their defaults (_REQUIRED: must be given)
_PARAMS: dict[str, dict[str, Any]] = {
    "replace": {"find": _REQUIRED, "replacement": "", "mode": "text", "case_sensitive": False,
                "whole_word": False, "path": None},
    "rename_key": {"old": _REQUIRED, "new": _REQUIRED, "path": None},
    "delete_field": {"path": _REQUIRED},
    "set_field": {"path": _REQUIRED, "value": _REQUIRED, "mode": "always"},
    "clean_strings": {"trim": False, "collapse_spaces": False, "path": None},
    "convert_type": {"path": _REQUIRED, "to": _REQUIRED},
    "dedupe": {"key": _REQUIRED, "keep": "first"},
}
_BOOL_PARAMS = frozenset({"case_sensitive", "whole_word", "trim", "collapse_spaces"})

# core backend of the MultiFinder prefilter (None = the active jsonreshape.core)
_backend: ModuleType | None = None


def set_core_backend(backend: str | ModuleType | None) -> ModuleType | None:
    """Use a specific core backend (``"rust"`` / ``"python"`` or a module) for the prefilter and the unit
    check of this module; ``None`` = the active ``jsonreshape.core``. Returns the previous override (tests)."""
    global _backend
    old = _backend
    _backend = core.get_backend(backend) if isinstance(backend, str) else backend
    return old


def _api() -> Any:
    return _backend if _backend is not None else core


def kind_title(kind: str) -> str:
    """The localized name of an operation kind (``bulk.kind.<kind>``)."""
    return t(f"bulk.kind.{kind}") if kind in KINDS else str(kind)


# ----------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------
def _cut(text: str, limit: int = TEXT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit - 1] + _ELLIPSIS


def _text(value: Any) -> str:
    """Example text of a value: compact JSON (strings quoted) cut to TEXT_CHARS; markers localized."""
    if value is _DELETED:
        return t("bulk.example.deleted")
    if value is MISSING:
        return t("bulk.example.missing")
    if isinstance(value, str) and len(value) > TEXT_CHARS:
        value = value[:TEXT_CHARS + 1]          # the cut text needs no more (no huge encodes)
    try:
        text = codec.dumps(value).decode("utf-8", "replace")
    except (TypeError, ValueError):  # pragma: no cover - parsed JSON values always encode
        text = repr(value)
    return _cut(text)


def _path_text(path: tuple) -> str:
    return format_path(tuple(path)) if path else "$"


def _pairs(data: bytes) -> array:
    a = array("Q")
    a.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a.byteswap()
    return a


def _items(node: Any) -> Iterator[tuple[Any, Any]]:
    return iter(node.items()) if isinstance(node, dict) else enumerate(node)


def _is_ascending(seq: Any) -> bool:
    return all(map(operator.lt, seq, seq[1:])) if len(seq) > 1 else True


class _DupFound(Exception):
    """Raised by the duplicate-key hook (stops the decode at the first duplicate)."""


def _dup_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)                 # (C speed; the value itself is not used)
    if len(obj) != len(pairs):
        raise _DupFound
    return obj


_DUP_DECODER = json.JSONDecoder(object_pairs_hook=_dup_hook, parse_float=str, parse_int=str, parse_constant=str)
_DUP_SCAN = _DUP_DECODER.scan_once          # the C scanner (json.scanner.c_make_scanner) of that decoder


def _has_duplicate_keys(value_bytes: bytes) -> bool:
    """Whether any object of the JSON value has a key twice (decoded keys; the ``json`` C scanner with
    an ``object_pairs_hook``). ``RecursionError`` for values nested deeper than the decoder allows,
    ``ValueError`` for text that is not one JSON value."""
    if b"{" not in value_bytes:
        return False
    text = value_bytes.decode("utf-8")
    try:
        _obj, end = _DUP_SCAN(text, 0)        # a record value starts at its first byte (no whitespace)
    except _DupFound:
        return True
    except StopIteration:
        raise ValueError("not a JSON value") from None
    if text[end:].strip(" \t\r\n"):
        raise ValueError("not a single JSON value")
    return False


# ----------------------------------------------------------------------------------
# path pattern automaton (incremental PathPattern.matches / matches_prefix_of)
# ----------------------------------------------------------------------------------
class _Nfa:
    """``PathPattern`` matching along a walk: a node's state is the frozenset of pattern positions
    reached (closed over ``**``); ``n in state`` = the node's path matches the pattern exactly; an empty
    state = nothing at or below the node can match. Steps are cached."""

    __slots__ = ("segs", "n", "start", "_closures", "_steps")

    def __init__(self, pattern: PathPattern) -> None:
        self.segs = pattern.segments
        self.n = len(self.segs)
        self._closures: dict[frozenset[int], frozenset[int]] = {}
        self._steps: dict[tuple[frozenset[int], Any], frozenset[int]] = {}
        self.start = self._closure(frozenset((0,)))

    def _closure(self, states: frozenset[int]) -> frozenset[int]:
        hit = self._closures.get(states)
        if hit is None:
            out = set(states)
            for i in states:
                j = i
                while j < self.n and self.segs[j] is Wild.DESCENDANTS:
                    j += 1
                    out.add(j)
            hit = self._closures[states] = frozenset(out)
        return hit

    def step(self, states: frozenset[int], seg: str | int) -> frozenset[int]:
        key = (states, seg)
        hit = self._steps.get(key)
        if hit is not None:
            return hit
        nxt = set()
        segs = self.segs
        for i in states:
            if i >= self.n:
                continue
            p = segs[i]
            if p is Wild.DESCENDANTS:
                nxt.add(i)
            elif p is Wild.ANY_INDEX:
                if isinstance(seg, int):
                    nxt.add(i + 1)
            elif p is Wild.ANY_KEY:
                if isinstance(seg, str):
                    nxt.add(i + 1)
            elif isinstance(p, int):
                if isinstance(seg, int) and seg == p:
                    nxt.add(i + 1)
            elif isinstance(seg, int):
                if canonical_index(p) == seg:
                    nxt.add(i + 1)
            elif seg == p:
                nxt.add(i + 1)
        res = self._closure(frozenset(nxt)) if nxt else _DEAD
        if len(self._steps) >= NFA_CACHE_MAX:
            self._steps.clear()
        self._steps[key] = res
        return res


# ----------------------------------------------------------------------------------
# per-record event collector
# ----------------------------------------------------------------------------------
class _Ctx:
    """Counts of one record and, while still needed, its example / problem events."""

    __slots__ = ("changes", "conflicts", "failures", "want_changes", "want_problems", "changed", "problems")

    def __init__(self) -> None:
        self.reset(False, False)

    def reset(self, want_changes: bool, want_problems: bool) -> None:
        self.changes = 0
        self.conflicts = 0
        self.failures = 0
        self.want_changes = want_changes
        self.want_problems = want_problems
        self.changed: list[tuple[str, str, str]] = []
        self.problems: list[tuple[str, str, str]] = []

    def change(self, path: tuple, before: Any, after: Any) -> None:
        self.changes += 1
        if self.want_changes and len(self.changed) < EXAMPLE_COUNT:
            self.changed.append((_path_text(path), _text(before), _text(after)))

    def change_at(self, path: tuple, key: Any, before: Any, after: Any) -> None:
        self.changes += 1
        if self.want_changes and len(self.changed) < EXAMPLE_COUNT:
            self.changed.append((_path_text(path + (key,)), _text(before), _text(after)))

    def conflict(self, path: tuple, before: Any, reason: str) -> None:
        self.conflicts += 1
        if self.want_problems and len(self.problems) < PROBLEM_COUNT:
            self.problems.append((_path_text(path), _text(before), reason))

    def failure(self, path: tuple, before: Any, reason: str) -> None:
        self.failures += 1
        if self.want_problems and len(self.problems) < PROBLEM_COUNT:
            self.problems.append((_path_text(path), _text(before), reason))


# ----------------------------------------------------------------------------------
# the rules
# ----------------------------------------------------------------------------------
class _Rule:
    """A validated operation: ``apply(value, ctx)`` changes the parsed record value (in place where it
    can; returns the new root) and reports to ``ctx``; ``needles(flags)`` the sound prefilter."""

    kind = ""

    def needles(self, flags: int) -> list[bytes] | None:
        return None

    def apply(self, value: Any, ctx: _Ctx) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


def _key_needles(key: str | None, flags: int) -> list[bytes] | None:
    """The JSON token of an object key: sound when the file has no escapes (a key is then written in
    exactly one way; a key that would need an escape cannot occur at all)."""
    if key is None or flags & FLAG_ESCAPES:
        return None
    return [codec.dumps(key)]


def _map_strings(root: Any, fn: Callable[[str], str], nfa: _Nfa | None, ctx: _Ctx) -> Any:
    """Replace every string VALUE whose path passes the prefix rule by ``fn(s)`` (document order)."""
    tr = type(root)
    if tr is str:
        if nfa is None or nfa.n in nfa.start:
            new = fn(root)
            if new != root:
                ctx.change((), root, new)
                return new
        return root
    if tr is not dict and tr is not list:
        return root
    if nfa is None:
        _walk_strings(root, fn, ctx, (), 0)
    elif nfa.n in nfa.start:
        _walk_strings(root, fn, ctx, (), 0)
    else:
        _walk_strings_nfa(root, fn, nfa, ctx, (), nfa.start, 0)
    return root


# The rules walk a parsed record recursively (about twice as fast as an explicit stack) down to
# _WALK_DEPTH levels; a deeper subtree is finished by the iterative walk of the same rule (parsed
# values can be nested thousands of levels deep). Parsed values hold only exact dict / list / str /
# number / bool / None objects (codec), so ``type(v) is ...`` tests are exact.
_WALK_DEPTH = 48


def _walk_strings(node: Any, fn: Callable[[str], str], ctx: _Ctx, path: tuple, depth: int) -> None:
    """Every string value below ``node`` (a dict or list) -> ``fn(s)``, in document order."""
    for k, v in (node.items() if type(node) is dict else enumerate(node)):
        tv = type(v)
        if tv is str:
            new = fn(v)
            if new != v:
                node[k] = new
                ctx.change_at(path, k, v, new)
        elif tv is dict or tv is list:
            if depth < _WALK_DEPTH:
                _walk_strings(v, fn, ctx, path + (k,), depth + 1)
            else:
                _map_strings_iter(v, fn, None, ctx, path + (k,), None, True)


def _walk_strings_nfa(node: Any, fn: Callable[[str], str], nfa: _Nfa, ctx: _Ctx, path: tuple, st: Any,
                      depth: int) -> None:
    """Like :func:`_walk_strings` below a node whose own path does not pass the filter (state ``st``)."""
    n = nfa.n
    step = nfa.step
    for k, v in (node.items() if type(node) is dict else enumerate(node)):
        tv = type(v)
        if tv is str:
            if n not in step(st, k):
                continue
            new = fn(v)
            if new != v:
                node[k] = new
                ctx.change_at(path, k, v, new)
        elif tv is dict or tv is list:
            st2 = step(st, k)
            if not st2:
                continue
            if depth >= _WALK_DEPTH:
                _map_strings_iter(v, fn, nfa, ctx, path + (k,), st2, n in st2)
            elif n in st2:
                _walk_strings(v, fn, ctx, path + (k,), depth + 1)
            else:
                _walk_strings_nfa(v, fn, nfa, ctx, path + (k,), st2, depth + 1)


def _map_strings_iter(root: Any, fn: Callable[[str], str], nfa: _Nfa | None, ctx: _Ctx, path0: tuple,
                      st0: Any, ok0: bool) -> None:
    """The iterative walk of :func:`_map_strings` from ``root`` (a dict or list at ``path0``, filter state
    ``st0``; ``ok0``: the root's own path passed the filter)."""
    frames: list[tuple[Any, Iterator, tuple, Any, bool]] = [(root, _items(root), path0, st0, ok0)]
    while frames:
        node, it, path, st, ok = frames[-1]
        for k, v in it:
            if isinstance(v, str):
                if not ok and nfa.n not in nfa.step(st, k):  # type: ignore[union-attr]
                    continue
                new = fn(v)
                if new != v:
                    node[k] = new
                    ctx.change_at(path, k, v, new)
            elif isinstance(v, (dict, list)):
                if ok:
                    frames.append((v, _items(v), path + (k,), None, True))
                else:
                    st2 = nfa.step(st, k)  # type: ignore[union-attr]
                    if not st2:
                        continue
                    frames.append((v, _items(v), path + (k,), st2, nfa.n in st2))  # type: ignore[union-attr]
                break
        else:
            frames.pop()


class _Replace(_Rule):
    kind = "replace"

    def __init__(self, find: str, replacement: str, regex: bool, case_sensitive: bool, whole_word: bool,
                 nfa: _Nfa | None) -> None:
        self.query = TextQuery(find, "regex" if regex else "plain", case_sensitive, whole_word)
        self.replacement = replacement
        self.regex = regex
        self.nfa = nfa
        self.fn: Callable[[str], str]
        if regex:
            # the TextQuery's own compiled pattern: same flags and whole-word guards as the search
            self.rx: re.Pattern[str] = self.query.regex  # type: ignore[assignment]
            self.postfilter = self.query.regex_postfilter
            try:
                self.rx.sub(replacement, "")       # compiles (and so checks) the replacement template
            except (re.error, IndexError, OverflowError, RecursionError) as exc:
                raise UserInputError("bulk.error.replacement", param="replacement",
                                     detail=regex_error_text(exc)) from None
            self.fn = self._regex_sub
        else:
            self.fn = self._text_sub
        # Text mode, a sound pre-check per string: a match needs one of the prefilter needles of UNESCAPED
        # text (``prefilter_needles(0)``, the record prefilter's rule) in the string's UTF-8 -- a decoded
        # string is unescaped text. With the Rust core one MultiFinder call costs far less than
        # ``find_spans`` on the many strings that cannot match (speed only; results are the same).
        self.pre: Any = None
        api = _api()
        rust = api.__name__.endswith("_rscore") if api is not core else core.BACKEND == "rust"
        if not regex and rust:
            needles = self.query.prefilter_needles(0)
            if needles:
                self.pre = api.MultiFinder(needles)

    def needles(self, flags: int) -> list[bytes] | None:
        return None if self.regex else self.query.prefilter_needles(flags)

    def _text_sub(self, s: str) -> str:
        pre = self.pre
        if pre is not None:
            try:
                if pre.find_first(s.encode("utf-8")) < 0:
                    return s
            except UnicodeEncodeError:
                pass                        # lone surrogates: find_spans decides
        spans = self.query.find_spans(s)
        if not spans:
            return s
        repl = self.replacement
        parts: list[str] = []
        prev = 0
        for a, b in spans:          # non-overlapping, left to right (= replacing them right to left)
            parts.append(s[prev:a])
            parts.append(repl)
            prev = b
        parts.append(s[prev:])
        return "".join(parts)

    def _regex_sub(self, s: str) -> str:
        if not self.postfilter:
            return self.rx.sub(self.replacement, s)
        repl = self.replacement
        return self.rx.sub(lambda m: m.expand(repl) if is_whole_word(s, m.start(), m.end()) else m.group(0), s)

    def apply(self, value: Any, ctx: _Ctx) -> Any:
        return _map_strings(value, self.fn, self.nfa, ctx)


class _Clean(_Rule):
    kind = "clean_strings"

    def __init__(self, trim: bool, collapse: bool, nfa: _Nfa | None) -> None:
        self.trim = trim
        self.collapse = collapse
        self.nfa = nfa

    def fn(self, s: str) -> str:
        if self.trim:
            s = s.strip()
        if self.collapse:
            s = _WS_RUN.sub(" ", s)
        return s

    def apply(self, value: Any, ctx: _Ctx) -> Any:
        return _map_strings(value, self.fn, self.nfa, ctx)


class _Rename(_Rule):
    kind = "rename_key"

    def __init__(self, old: str, new: str, nfa: _Nfa | None) -> None:
        self.old = old
        self.new = new
        self.nfa = nfa
        self.reason = t("bulk.problem.key_exists", key=new)

    def needles(self, flags: int) -> list[bytes] | None:
        return _key_needles(self.old, flags)

    def _enter(self, node: Any, path: tuple, st: Any, ctx: _Ctx) -> tuple[Iterator, tuple, Any]:
        if isinstance(node, dict):
            old = self.old
            if old in node and (self.nfa is None or self.nfa.n in st):
                new = self.new
                if new in node:
                    ctx.conflict(path, old, self.reason)
                else:
                    items = list(node.items())     # the ORIGINAL keys: children keep their old paths
                    node.clear()
                    for k, v in items:
                        node[new if k == old else k] = v
                    ctx.change(path, old, new)
                    return iter(items), path, st
            return iter(node.items()), path, st
        return enumerate(node), path, st

    def apply(self, value: Any, ctx: _Ctx) -> Any:
        tv = type(value)
        if tv is not dict and tv is not list:
            return value
        nfa = self.nfa
        self._walk(value, (), None if nfa is None else nfa.start, ctx, 0)
        return value

    def _walk(self, node: Any, path: tuple, st: Any, ctx: _Ctx, depth: int) -> None:
        """The recursive walk (document order; see :data:`_WALK_DEPTH`)."""
        it, _path, _st = self._enter(node, path, st, ctx)
        nfa = self.nfa
        for k, v in it:
            tv = type(v)
            if tv is dict or tv is list:
                if nfa is None:
                    st2 = None
                else:
                    st2 = nfa.step(st, k)
                    if not st2:
                        continue
                if depth < _WALK_DEPTH:
                    self._walk(v, path + (k,), st2, ctx, depth + 1)
                else:
                    self._iter(v, path + (k,), st2, ctx)

    def _iter(self, value: Any, path0: tuple, st0: Any, ctx: _Ctx) -> None:
        """The iterative walk from ``value`` (a dict or list at ``path0``, filter state ``st0``)."""
        nfa = self.nfa
        frames = [self._enter(value, path0, st0, ctx)]
        while frames:
            it, path, st = frames[-1]
            for k, v in it:
                if isinstance(v, (dict, list)):
                    if nfa is None:
                        st2 = None
                    else:
                        st2 = nfa.step(st, k)
                        if not st2:
                            continue
                    frames.append(self._enter(v, path + (k,), st2, ctx))
                    break
            else:
                frames.pop()


class _Delete(_Rule):
    kind = "delete_field"

    def __init__(self, nfa: _Nfa, last_key: str | None) -> None:
        self.nfa = nfa
        self.last_key = last_key

    def needles(self, flags: int) -> list[bytes] | None:
        return _key_needles(self.last_key, flags)

    def apply(self, value: Any, ctx: _Ctx) -> Any:
        tv = type(value)
        if tv is not dict and tv is not list:
            return value
        self._walk(value, (), self.nfa.start, ctx, 0)
        return value

    def _walk(self, node: Any, path: tuple, st: Any, ctx: _Ctx, depth: int) -> None:
        """The recursive walk (document order; see :data:`_WALK_DEPTH`): matched members are removed after
        their object was walked."""
        nfa = self.nfa
        n = nfa.n
        step = nfa.step
        is_obj = type(node) is dict
        dels: list[Any] | None = None
        for k, v in (node.items() if is_obj else enumerate(node)):
            st2 = step(st, k)
            if not st2:
                continue
            if is_obj and n in st2:                  # a matched member: removed, not visited
                if dels is None:
                    dels = []
                dels.append(k)
                ctx.change_at(path, k, v, _DELETED)
                continue
            tv = type(v)
            if tv is dict or tv is list:
                if depth < _WALK_DEPTH:
                    self._walk(v, path + (k,), st2, ctx, depth + 1)
                else:
                    self._iter(v, path + (k,), st2, ctx)
        if dels:
            for k in dels:
                del node[k]

    def _iter(self, value: Any, path0: tuple, st0: Any, ctx: _Ctx) -> None:
        """The iterative walk from ``value`` (a dict or list at ``path0``, filter state ``st0``)."""
        nfa = self.nfa
        n = nfa.n
        frames: list[list[Any]] = [[value, _items(value), path0, st0, [], isinstance(value, dict)]]
        while frames:
            fr = frames[-1]
            node, it, path, st, dels, is_obj = fr
            for k, v in it:
                st2 = nfa.step(st, k)
                if not st2:
                    continue
                if is_obj and n in st2:              # a matched member: removed, not visited
                    dels.append(k)
                    ctx.change_at(path, k, v, _DELETED)
                    continue
                if isinstance(v, (dict, list)):
                    frames.append([v, _items(v), path + (k,), st2, [], isinstance(v, dict)])
                    break
            else:
                for k in dels:
                    del node[k]
                frames.pop()


class _Convert(_Rule):
    kind = "convert_type"

    def __init__(self, nfa: _Nfa, to: str) -> None:
        self.nfa = nfa
        self.to = to

    def _convert(self, path: tuple, v: Any, ctx: _Ctx) -> Any:
        if codec.json_type(v) == self.to:
            return v
        try:
            new = editops.convert_value(v, self.to)
        except ConversionError as exc:
            if ctx.want_problems:
                ctx.failure(path, v, exc.message)
            else:
                ctx.failures += 1
            return v
        ctx.change(path, v, new)
        return new

    def apply(self, value: Any, ctx: _Ctx) -> Any:
        nfa = self.nfa
        n = nfa.n
        if n in nfa.start:
            return self._convert((), value, ctx)
        tv = type(value)
        if tv is not dict and tv is not list:
            return value
        self._walk(value, (), nfa.start, ctx, 0)
        return value

    def _walk(self, node: Any, path: tuple, st: Any, ctx: _Ctx, depth: int) -> None:
        """The recursive walk (document order; see :data:`_WALK_DEPTH`)."""
        nfa = self.nfa
        n = nfa.n
        step = nfa.step
        for k, v in (node.items() if type(node) is dict else enumerate(node)):
            st2 = step(st, k)
            if not st2:
                continue
            if n in st2:                             # outermost match: converted, not visited
                new = self._convert(path + (k,), v, ctx)
                if new is not v:
                    node[k] = new
                continue
            tv = type(v)
            if tv is dict or tv is list:
                if depth < _WALK_DEPTH:
                    self._walk(v, path + (k,), st2, ctx, depth + 1)
                else:
                    self._iter(v, path + (k,), st2, ctx)

    def _iter(self, value: Any, path0: tuple, st0: Any, ctx: _Ctx) -> None:
        """The iterative walk from ``value`` (a dict or list at ``path0``, filter state ``st0``)."""
        nfa = self.nfa
        n = nfa.n
        frames = [(value, _items(value), path0, st0)]
        while frames:
            node, it, path, st = frames[-1]
            for k, v in it:
                st2 = nfa.step(st, k)
                if not st2:
                    continue
                if n in st2:                         # outermost match: converted, not visited
                    new = self._convert(path + (k,), v, ctx)
                    if new is not v:
                        node[k] = new
                    continue
                if isinstance(v, (dict, list)):
                    frames.append((v, _items(v), path + (k,), st2))
                    break
            else:
                frames.pop()


class _SetField(_Rule):
    kind = "set_field"

    def __init__(self, path: tuple, value: Any, mode: str) -> None:
        self.path = path
        self.value = value
        self.mode = mode

    def _blocked(self, ctx: _Ctx, path: tuple, node: Any, key: str, **params: Any) -> None:
        if self.mode == "if_exists":
            return                     # a field that cannot exist does not exist: no change
        if ctx.want_problems:
            ctx.conflict(path, node, t(key, path=_path_text(path), **params))
        else:
            ctx.conflicts += 1

    def apply(self, root: Any, ctx: _Ctx) -> Any:
        segs = self.path
        value = self.value
        mode = self.mode
        last = len(segs) - 1
        cur = root
        for i, seg in enumerate(segs):
            if isinstance(cur, dict):
                if isinstance(seg, int):
                    self._blocked(ctx, segs[:i], cur, "bulk.problem.not_array")
                    return root
                if seg in cur:
                    if i == last:
                        old = cur[seg]
                        if mode == "if_missing" or json_equal(old, value):
                            return root
                        cur[seg] = value
                        ctx.change(segs, old, value)
                        return root
                    cur = cur[seg]
                    continue
                if mode == "if_exists":
                    return root
                for j in range(i + 1, last + 1):
                    if isinstance(segs[j], int):     # an array would have to be created
                        self._blocked(ctx, segs[:j], MISSING, "bulk.problem.missing_array")
                        return root
                node = cur
                for s in segs[i:last]:               # the missing intermediate objects
                    child: dict[str, Any] = {}
                    node[s] = child
                    node = child
                node[segs[last]] = value
                ctx.change(segs, MISSING, value)
                return root
            if isinstance(cur, list):
                idx = seg if isinstance(seg, int) else canonical_index(seg)
                if idx is None:
                    self._blocked(ctx, segs[:i], cur, "bulk.problem.not_object")
                    return root
                if idx >= len(cur):
                    self._blocked(ctx, segs[:i], cur, "bulk.problem.no_element", index=idx)
                    return root
                if i == last:
                    old = cur[idx]
                    if mode == "if_missing" or json_equal(old, value):
                        return root
                    cur[idx] = value
                    ctx.change(segs, old, value)
                    return root
                cur = cur[idx]
                continue
            # a string, number, boolean or null where a container is needed
            self._blocked(ctx, segs[:i], cur, "bulk.problem.not_object")
            return root
        return root  # pragma: no cover - segs is never empty


# ----------------------------------------------------------------------------------
# BulkSpec
# ----------------------------------------------------------------------------------
def _param_error(param: str, expected: str) -> UserInputError:
    return UserInputError("bulk.error.param_type", param=param, expected=t(expected))


def _opt_pattern(text: Any, param: str = "path") -> PathPattern | None:
    """An optional path filter: ``None`` / blank -> no filter."""
    if text is None:
        return None
    if not isinstance(text, str):
        raise _param_error(param, "bulk.expect.path")
    if not text.strip():
        return None
    return parse_pattern(text)       # PathSyntaxError (a UserInputError) for bad syntax


def _req_pattern(text: Any, param: str = "path") -> PathPattern:
    if not isinstance(text, str):
        raise _param_error(param, "bulk.expect.path")
    if not text.strip():
        raise UserInputError("bulk.error.path_required", param=param)
    return parse_pattern(text)


def _filter_nfa(pattern: PathPattern | None) -> _Nfa | None:
    """The automaton of a prefix-rule filter; ``None`` when it passes everything."""
    if pattern is None:
        return None
    nfa = _Nfa(pattern)
    return None if nfa.n in nfa.start else nfa


@dataclass
class BulkSpec:
    """A bulk operation: ``kind`` (one of :data:`KINDS`) and its ``params`` (module docstring)."""

    kind: str
    params: dict = field(default_factory=dict)

    # -- validation ---------------------------------------------------------------------
    def normalized(self) -> dict[str, Any]:
        """The params with defaults filled in, checked for presence and types (``UserInputError``)."""
        kind = self.kind
        if kind not in _PARAMS:
            raise UserInputError("bulk.error.kind", kind=str(kind))
        if not isinstance(self.params, dict):
            raise UserInputError("bulk.error.params")
        known = _PARAMS[kind]
        for name in self.params:
            if name not in known:
                raise UserInputError("bulk.error.unknown_param", param=str(name))
        out: dict[str, Any] = {}
        for name, default in known.items():
            if name in self.params:
                value = self.params[name]
            elif default is _REQUIRED:
                raise UserInputError("bulk.error.missing_param", param=name)
            else:
                value = default
            if name in _BOOL_PARAMS:
                if not isinstance(value, bool):
                    raise _param_error(name, "bulk.expect.bool")
            elif name == "path":
                if value is not None and not isinstance(value, str):
                    raise _param_error(name, "bulk.expect.path")
            elif not isinstance(value, str):
                raise _param_error(name, "bulk.expect.text")
            out[name] = value
        return out

    def _compile(self) -> _Rule | None:
        """Validate and build the rule (``None`` for ``dedupe``, which :mod:`jsonreshape.dupes` runs)."""
        p = self.normalized()
        kind = self.kind
        if kind == "replace":
            if not p["find"]:
                raise UserInputError("bulk.error.find_empty", param="find")
            if p["mode"] not in _REPLACE_MODES:
                raise UserInputError("bulk.error.mode", param="mode", value=p["mode"])
            nfa = _filter_nfa(_opt_pattern(p["path"]))
            return _Replace(p["find"], p["replacement"], p["mode"] == "regex", p["case_sensitive"], p["whole_word"],
                            nfa)
        if kind == "rename_key":
            if p["old"] == p["new"]:
                raise UserInputError("bulk.error.same_key", param="new")
            pattern = _opt_pattern(p["path"])
            return _Rename(p["old"], p["new"], None if pattern is None else _Nfa(pattern))
        if kind == "delete_field":
            pattern = _req_pattern(p["path"])
            if not pattern.segments:
                raise UserInputError("bulk.error.path_root", param="path")
            last = pattern.segments[-1]
            return _Delete(_Nfa(pattern), last if isinstance(last, str) else None)
        if kind == "set_field":
            if not p["path"] or not p["path"].strip():
                raise UserInputError("bulk.error.path_required", param="path")
            segs = parse_path(p["path"])      # concrete (wildcards -> PathSyntaxError)
            if not segs:
                raise UserInputError("bulk.error.path_root", param="path")
            if p["mode"] not in _SET_MODES:
                raise UserInputError("bulk.error.set_mode", param="mode", value=p["mode"])
            try:
                token = editops.check_json(p["value"])
            except EditError as exc:
                raise UserInputError("bulk.error.value", param="value", line=exc.params.get("line"),
                                     column=exc.params.get("column"), reason=exc.params.get("reason")) from None
            return _SetField(tuple(segs), codec.loads(token), p["mode"])
        if kind == "clean_strings":
            if not (p["trim"] or p["collapse_spaces"]):
                raise UserInputError("bulk.error.clean_nothing", param="trim")
            return _Clean(p["trim"], p["collapse_spaces"], _filter_nfa(_opt_pattern(p["path"])))
        if kind == "convert_type":
            if p["to"] not in _TYPES:
                raise UserInputError("bulk.error.type", param="to", value=p["to"])
            return _Convert(_Nfa(_req_pattern(p["path"])), p["to"])
        # dedupe: the duplicate finder's own checks (same texts as everywhere else)
        dupes._parse_key(p["key"])
        dupes._check_keep(p["keep"])
        return None

    def validate(self) -> None:
        """Check the kind and the params: ``UserInputError("bulk.error.*", param=...)`` (also
        ``PathSyntaxError`` / ``QuerySyntaxError`` for a bad path / regex and the ``dupes.error.*`` texts
        for a bad ``dedupe`` key)."""
        self._compile()

    # -- description ------------------------------------------------------------------------
    def label(self) -> str:
        """A localized one-line description of the operation (never raises)."""
        kind = self.kind
        try:
            p = self.normalized()
        except JsonReshapeError:
            p = dict(self.params) if isinstance(self.params, dict) else {}
        if kind not in KINDS:
            return str(kind)

        def s(name: str, limit: int = 40) -> str:
            v = p.get(name)
            return _cut("" if v is None else str(v), limit)

        def where() -> str:
            path = p.get("path")
            if isinstance(path, str) and path.strip():
                return t("bulk.label.where", path=_cut(path.strip(), 60))
            return ""

        try:
            if kind == "replace":
                opts = []
                if p.get("mode") == "regex":
                    opts.append(t("bulk.label.regex"))
                if p.get("case_sensitive"):
                    opts.append(t("bulk.label.case"))
                if p.get("whole_word"):
                    opts.append(t("bulk.label.whole_word"))
                options = t("bulk.label.options", options=", ".join(opts)) if opts else ""
                return t("bulk.label.replace", find=s("find"), replacement=s("replacement"), options=options,
                         where=where())
            if kind == "rename_key":
                return t("bulk.label.rename_key", old=s("old"), new=s("new"), where=where())
            if kind == "delete_field":
                return t("bulk.label.delete_field", path=s("path", 60))
            if kind == "set_field":
                mode = p.get("mode") if p.get("mode") in _SET_MODES else "always"
                return t("bulk.label.set_field", path=s("path", 60), value=s("value"),
                         mode=t(f"bulk.label.mode.{mode}"))
            if kind == "clean_strings":
                what = []
                if p.get("trim"):
                    what.append(t("bulk.label.trim"))
                if p.get("collapse_spaces"):
                    what.append(t("bulk.label.collapse"))
                return t("bulk.label.clean_strings", what=", ".join(what), where=where())
            if kind == "convert_type":
                to = p.get("to")
                type_text = t(f"edits.type.{to}") if to in _TYPES else s("to")
                path = s("path", 60) or "$"
                return t("bulk.label.convert_type", path=path, type=type_text)
            keep = p.get("keep") if p.get("keep") in ("first", "last") else "first"
            key = p.get("key")
            key_text = t("bulk.label.record_key") if isinstance(key, str) and key.strip() == "$key" else s("key", 60)
            return t("bulk.label.dedupe", key=key_text, keep=t(f"bulk.label.keep.{keep}"))
        except Exception:  # pragma: no cover - a label must never fail
            log.debug("bulk label of %r failed", self, exc_info=True)
            return kind_title(kind)


# ----------------------------------------------------------------------------------
# results
# ----------------------------------------------------------------------------------
@dataclass
class BulkExample:
    """One change (or, in ``BulkPreview.problems``, one problem: ``after`` = the reason)."""

    position: int
    path: str
    before: str
    after: str


@dataclass(eq=False)
class BulkPreview:
    """The exact result of a bulk operation on one state (§12.6; extensions in the module docstring)."""

    spec: BulkSpec
    gen: int
    scanned: int = 0
    records_changed: int = 0
    values_changed: int = 0
    conflicts: int = 0
    failures: int = 0
    skipped_huge: int = 0
    examples: list[BulkExample] = field(default_factory=list)
    problems: list[BulkExample] = field(default_factory=list)
    bytes_new: int = 0
    elapsed: float = 0.0
    # -- extensions ---------------------------------------------------------------------
    warnings: list[str] = field(default_factory=list)
    layer: str | None = None
    segment: int | None = None
    flags: tuple[int, int] = (0, 0)
    records: list[int] | None = None
    positions: array | None = None
    keys: list[tuple[int, str, None]] | None = None
    scope_gen: int | None = None
    discarded: bool = False
    workers: int = 1
    _store: Any = field(default=None, repr=False)
    _step: Step | None = field(default=None, repr=False)

    @property
    def empty(self) -> bool:
        """Nothing would change (:func:`make_step` refuses it)."""
        return self.records_changed == 0

    def discard(self) -> None:
        """Delete the preview's segment and pending layer file (idempotent). Files of a committed step
        (the step :func:`make_step` made was committed) are never deleted: they belong to the layer."""
        if self.discarded:
            return
        self.discarded = True
        store = self._store
        committed = self._step is not None and self._step.seq > 0
        if store is not None and not committed:
            if self.layer is not None:
                try:
                    store.drop_layer_file(self.layer)
                except ValueError:
                    log.debug("bulk preview: layer %s belongs to a step; kept", self.layer)
                except (JsonReshapeError, OSError):
                    log.warning("bulk preview: layer %s not removed", self.layer, exc_info=True)
            if self.segment is not None:
                try:
                    store.drop_segment(self.segment)
                except ValueError:
                    log.debug("bulk preview: segment %s belongs to a step; kept", self.segment)
                except (JsonReshapeError, OSError):
                    log.warning("bulk preview: segment %s not removed", self.segment, exc_info=True)
        self.positions = None
        self.keys = None
        self._store = None


# ----------------------------------------------------------------------------------
# scope, document, disk
# ----------------------------------------------------------------------------------
def _scan_view(doc_pinned: Any, state: Any, scope: Any) -> Any:
    """The view to read (module docstring "Scope, pinning and order"): ``EditError(bulk.error.stale_scope)``
    for a scope of another state."""
    gen = getattr(scope, "gen", None) if scope is not None else None
    if gen is None or gen == state.gen:
        return doc_pinned.pinned(state)
    if gen == 0 and not _edited_state(state):
        return doc_pinned.pinned(None)          # positions of the plain file = positions of this state
    raise EditError("bulk.error.stale_scope")


def _data_bytes(doc: Any) -> int:
    """Bytes of all records: the data file without its BOM, or the effective records' bytes."""
    if doc.edited:
        return max(1, int(doc.effective_bytes()))
    return max(1, int(doc.data_size) - int(doc.info.data_start))


class _BitmapSource:
    """Ascending, unique positions of a bitmap (``iter_chunks`` like a RecordSet)."""

    def __init__(self, bits: bytearray, count: int) -> None:
        self.bits = bits
        self.count = count
        self.n = int.from_bytes(bits, "little").bit_count()

    def __len__(self) -> int:
        return self.n

    def iter_chunks(self, size: int = 65536) -> Iterator[array]:
        bits = self.bits
        offsets = _BIT_OFFSETS
        out = array("Q")
        for m in _NONZERO.finditer(bits):
            for b in range(m.start(), m.end()):
                base = b << 3
                out.extend([base + k for k in offsets[bits[b]]])
                if len(out) >= size:
                    yield out
                    out = array("Q")
        if out:
            yield out


def _iter_scope(scope: Any) -> Iterator[array]:
    """The positions of a scope as ``array('Q')`` chunks (a negative position -> stale scope)."""
    try:
        if isinstance(scope, (list, tuple, range, array)):
            for a in range(0, len(scope), 65536):
                yield array("Q", [operator.index(p) for p in scope[a:a + 65536]])
        elif hasattr(scope, "iter_chunks"):
            for chunk in scope.iter_chunks():
                yield array("Q", chunk)
        else:
            batch = array("Q")
            for p in scope:
                batch.append(operator.index(p))
                if len(batch) >= 65536:
                    yield batch
                    batch = array("Q")
            if batch:
                yield batch
    except OverflowError:
        raise EditError("bulk.error.stale_scope") from None


def _plan_source(doc: Any, scope: Any, count: int, cancel: CancelToken | None) -> tuple[Any, int]:
    """``(iter_blocks source, number of records)``: ``None`` = all records; otherwise ascending unique
    positions (a range / an ascending RecordSet or sequence as it is, else a bitmap)."""
    if scope is None or (isinstance(scope, RecordSet) and scope._is_base_of(doc)):
        return None, count
    if isinstance(scope, range):
        if not len(scope):
            return range(0), 0
        lo, hi = min(scope[0], scope[-1]), max(scope[0], scope[-1])
        if lo < 0 or hi >= count:
            raise EditError("bulk.error.stale_scope")
        if scope.step == 1:
            return scope, len(scope)
        if scope.step < 0:
            scope = scope[::-1]
        return scope, len(scope)
    direct = False
    if isinstance(scope, RecordSet):
        direct = scope.is_ascending() and scope.complete
        n = len(scope)
        if direct and n and (scope[0] < 0 or scope[n - 1] >= count):
            raise EditError("bulk.error.stale_scope")
        if direct:
            return scope, n
    elif isinstance(scope, (list, tuple, array)):
        if _is_ascending(scope):
            if scope and (scope[0] < 0 or scope[-1] >= count):
                raise EditError("bulk.error.stale_scope")
            return scope, len(scope)
    bits = bytearray((count + 7) >> 3)
    for chunk in _iter_scope(scope):
        check(cancel)
        if chunk and max(chunk) >= count:
            raise EditError("bulk.error.stale_scope")
        for p in chunk:
            bits[p >> 3] |= 1 << (p & 7)
    src = _BitmapSource(bits, count)
    return src, len(src)


def _free_space(path: Path) -> int | None:
    """Free bytes on the disk of ``path`` (its nearest existing ancestor); ``None`` when unknown."""
    p = Path(path)
    for _ in range(64):
        try:
            return int(shutil.disk_usage(str(p)).free)
        except FileNotFoundError:
            parent = p.parent
            if parent == p:
                return None
            p = parent
        except OSError:
            log.debug("bulk: free space of %s unknown", p, exc_info=True)
            return None
    return None


def _check_disk(path: Path, need: int) -> None:
    free = _free_space(path)
    if free is not None and free < need:
        raise EditError("bulk.error.disk_space", need=format_bytes(need), free=format_bytes(free), path=str(path))


def _need(remaining_bytes: int) -> int:
    return int(max(0, remaining_bytes) * DISK_FACTOR) + DISK_RESERVE


def _check_document(doc: Any, progress: Progress | None, cancel: CancelToken | None) -> None:
    if not doc.index_complete:
        doc.build_index(progress=progress, cancel=cancel)
    if doc.error is not None:
        raise EditError("bulk.error.partial")


# ----------------------------------------------------------------------------------
# the work on the records of one block (in the calling thread or in a worker process)
# ----------------------------------------------------------------------------------
FAST_DUP_DEPTH = 1000          # _Runner: a record with this many brackets is always checked by the decoder
_ADAPT_EVERY = 1024            # _Runner: parsed records between the decisions about the dump comparison


class _Runner:
    """The work on the records of ONE block of a content operation -- prefilter, parse, rule, duplicate-key
    check, the new unit, the validation of the block's units -- in the calling thread (:meth:`_Pass.block`)
    or in a worker process (:func:`_pool_block`), the same code either way. :meth:`run` returns the block's
    units and changed positions (position order), its counts, the scanner flags of its units and its first
    examples / problems; the blocks are merged in position order, so the merged lists are exactly the first
    examples / problems of the whole scope.

    Duplicate keys of a changed record are found with the ``json`` C scanner (:func:`_has_duplicate_keys`)
    unless the record's text is byte for byte what ``codec.dumps`` writes for its parsed value: such a text
    has no object with a repeated key (the parse keeps one member per key, so a repeated key makes the
    serialization differ). The comparison needs the dump of the value BEFORE the rule changes it, so it is
    used only while it pays -- compact documents, most parsed records changing, most comparisons equal --
    which affects the speed, never the result. A record with :data:`FAST_DUP_DEPTH` or more brackets (and
    so possibly nested that deep) always goes through the decoder, whose nesting limit decides "unreadable"
    exactly as before."""

    def __init__(self, rule: _Rule, style: Any, member: bool, large: int, needles: list[bytes] | None) -> None:
        self.rule = rule
        self.style = style
        self.member = member
        self.large = large
        self.plain_compact = style.unit is None and style.colon == b":" and style.comma == b","
        self.finder = _api().MultiFinder(needles) if needles else None
        self.ctx = _Ctx()
        self.fast = self.plain_compact
        self.parsed = 0
        self.changed = 0
        self.tries = 0
        self.hits = 0
        self.next_adapt = _ADAPT_EVERY

    def _adapt(self) -> None:
        self.next_adapt = self.parsed + _ADAPT_EVERY
        if self.changed * 4 < self.parsed or (self.tries >= 64 and self.hits * 4 < self.tries * 3):
            self.fast = False

    def run(self, data: bytes, spans: bytes, idx: Any, want_ex: bool, want_pb: bool,
            cancel: CancelToken | None) -> tuple:
        """The records ``idx`` (positions) of a block: ``(units, sizes, positions, (records_changed,
        values_changed, conflicts, failures, skipped_huge, bytes_new), flags, examples, problems, first_huge)``
        -- ``units`` the new units back to back (a ``bytearray``; every unit is copied there at once: an
        ``orjson`` result occupies ~4 KiB whatever its length, so a block's worth of them must not be kept),
        ``sizes`` their lengths; examples / problems are ``(position, path, before, after)`` tuples, built only
        while ``want_ex`` / ``want_pb`` (the caller still needs some)."""
        sp = _pairs(spans)
        finder = self.finder
        cands: Iterable[int] = _pairs(finder.find_in_spans(data, spans)) if finder is not None else range(len(idx))
        member = self.member
        loads = codec.loads
        dumps = codec.dumps_transient       # results are compared / copied into the block at once, never kept
        apply = self.rule.apply
        ctx = self.ctx
        plain_compact = self.plain_compact
        style = self.style
        large = self.large
        out = bytearray()                              # the units back to back (the segment's bytes)
        vtext = bytearray(b"{" if member else b"")     # the units as one text for the scanner (validate)
        vsep = b"," if member else b"\n"
        sizes = array("Q")
        positions = array("Q")
        examples: list[tuple] = []
        problems: list[tuple] = []
        changed = values = conflicts = failures = huge = bytes_new = 0
        first_huge: int | None = None
        done = 0
        for k in cands:
            done += 1
            if cancel is not None and not done % CHECK_RECORDS:
                check(cancel)
            pos = int(idx[k])
            raw = data[sp[2 * k]:sp[2 * k + 1]]
            want_problems = want_pb and len(problems) < PROBLEM_COUNT
            head: bytes | None = None
            vb = raw
            try:
                if member:
                    _key, vo = split_member(raw)
                    head = raw[:vo]
                    vb = raw[vo:]
                value = loads(vb)
            except (codec.JsonDecodeError, ValueError, RecursionError):
                failures += 1
                if want_problems:
                    problems.append((pos, "$", "", t("bulk.problem.unreadable")))
                continue
            self.parsed += 1
            if self.parsed >= self.next_adapt:
                self._adapt()
            pre = dumps(value) if self.fast else None       # before the rule changes the value in place
            ctx.reset(want_ex and len(examples) < EXAMPLE_COUNT, want_problems)
            new_value = apply(value, ctx)
            if not ctx.changes:
                conflicts += ctx.conflicts
                failures += ctx.failures
                if ctx.problems:
                    for path, before, reason in ctx.problems:
                        if len(problems) >= PROBLEM_COUNT:
                            break
                        problems.append((pos, path, before, reason))
                continue
            self.changed += 1
            dup = None
            if pre is not None:
                self.tries += 1
                if pre == vb and (len(vb) < 2 * FAST_DUP_DEPTH or vb.count(b"[") + vb.count(b"{") < FAST_DUP_DEPTH):
                    self.hits += 1
                    dup = False
            if dup is None:
                try:
                    dup = _has_duplicate_keys(vb)
                except (RecursionError, ValueError):
                    failures += 1
                    if want_problems:
                        problems.append((pos, "$", "", t("bulk.problem.unreadable")))
                    continue
            if dup:
                conflicts += 1
                if want_problems:
                    problems.append((pos, "$", _cut(vb.decode("utf-8", "replace")), t("bulk.problem.duplicate_keys")))
                continue
            body = dumps(new_value)
            if plain_compact:
                # editops.format_record of a compact ":" / "," style re-emits codec.dumps' whitespace-free
                # output byte for byte: skip the Reformatter pass (the tests compare with format_record)
                unit = body if head is None else head + body
            else:
                unit = editops.format_record(body, style, member_head=head)
            size = len(unit)
            if size > large:
                huge += 1
                if first_huge is None or pos < first_huge:
                    first_huge = pos
                continue
            conflicts += ctx.conflicts
            failures += ctx.failures
            values += ctx.changes
            changed += 1
            bytes_new += size
            if ctx.changed and len(examples) < EXAMPLE_COUNT:
                for path, before, after in ctx.changed:
                    if len(examples) >= EXAMPLE_COUNT:
                        break
                    examples.append((pos, path, before, after))
            if ctx.problems:
                for path, before, reason in ctx.problems:
                    if len(problems) >= PROBLEM_COUNT:
                        break
                    problems.append((pos, path, before, reason))
            out += unit
            vtext += unit
            vtext += vsep
            sizes.append(size)
            positions.append(pos)
        flags = self.validate(vtext, out, sizes) if sizes else 0
        return (out, sizes, positions, (changed, values, conflicts, failures, huge, bytes_new), flags, examples,
                problems, first_huge)

    def validate(self, vtext: bytearray, units: bytearray, sizes: array) -> int:
        """``editops.check_unit`` of every unit, in ONE core Scanner pass over the block's units (``vtext``):
        values as a ``MODE_STREAM`` text (each unit followed by a newline), members as ``{`` + units joined by
        ``,`` + ``}`` in ``MODE_CONTAINER`` -- every unit must be exactly one emitted span. Returns the OR of
        the scanner flags. Any difference falls back to ``check_unit`` per unit (which raises
        ``edits.error.internal`` for a bad one: a bug)."""
        member = self.member
        n = len(sizes)
        if member:
            vtext[-1:] = b"}"             # the separator after the last member closes the object
        sc = _api().Scanner(MODE_CONTAINER if member else MODE_STREAM, [], 0)
        out = sc.feed(bytes(vtext))
        out += sc.finish()
        if sc.error is None:
            starts = array("Q", accumulate(map(add, sizes, repeat(1)), initial=1 if member else 0))
            want = array("Q", bytes(16 * n))
            want[0::2] = starts[:n]
            want[1::2] = array("Q", map(add, starts[:n], sizes))
            if _pairs(out) == want:
                return int(sc.flags)
        log.warning("bulk: a batch of %d units failed the batch check; checking them one by one", n)
        flags = 0
        view = memoryview(units)
        b = 0
        for size in sizes:
            flags |= editops.check_unit(bytes(view[b:b + size]), member=member)
            b += size
        return flags


# ----------------------------------------------------------------------------------
# worker processes (like engine.apply, docs/ARCHITECTURE.md §13.2)
# ----------------------------------------------------------------------------------
POOL_MIN_BYTES = 64 * MiB      # a scope smaller than this is worked on in the calling thread
POOL_MAX_WORKERS = 8
WORKER_MEMORY = 64 * MiB       # memory one worker process needs (measured peak ~45 MB: interpreter + one block)
POOL_BLOCK_BYTES = 2 * MiB     # record bytes per job of a worker
POOL_WAIT = 0.05               # seconds between cancellation checks while a result is awaited

_WORKER: dict[str, Any] = {}


def _backend_name(mod: ModuleType | None) -> str | None:
    if mod is None:
        return None
    return "rust" if mod.__name__.endswith("_rscore") else "python"


def _pool_init(kind: str, params: dict, style: Any, member: bool, large: int, needles: list[bytes] | None,
               backends: tuple[str | None, str | None], lang: str) -> None:
    """Worker process start: the rule compiled from the same spec, the caller's core backends and language.
    Automatic garbage collection is off: the values of a block are acyclic and freed by reference counting."""
    import gc

    from . import i18n

    gc.disable()
    i18n.set_language(lang)
    if backends[0]:
        set_core_backend(backends[0])
    if backends[1]:
        editops.set_core_backend(backends[1])
    rule = BulkSpec(kind, params)._compile()
    assert rule is not None
    _WORKER["runner"] = _Runner(rule, style, member, large, needles)


def _pool_block(data: bytes, spans: bytes, indices: bytes, want_ex: bool, want_pb: bool) -> tuple:
    """One block in a worker: ``("ok", units joined, unit sizes, positions, counts, flags, examples, problems,
    first_huge)`` or ``("error", class name, key, params)`` for a :class:`JsonReshapeError`."""
    runner: _Runner = _WORKER["runner"]
    idx = _pairs(indices)
    try:
        res = runner.run(data, spans, idx, want_ex, want_pb, None)
    except JsonReshapeError as exc:
        return ("error", type(exc).__name__, exc.key, dict(exc.params))
    return ("ok", *res)


def auto_workers(scope_bytes: int, settings: Any) -> int:
    """Worker processes :func:`preview` uses by itself: none below :data:`POOL_MIN_BYTES` of records, else
    ``min(cpu_count - 1, POOL_MAX_WORKERS)``, and no more than half of ``Settings.memory_limit_bytes``
    allows at :data:`WORKER_MEMORY` per worker (4 with the default 512 MiB budget, like ``engine.apply``)."""
    if scope_bytes < POOL_MIN_BYTES:
        return 1
    by_cpu = min((os.cpu_count() or 1) - 1, POOL_MAX_WORKERS)
    by_memory = (int(settings.memory_limit_bytes) // 2) // WORKER_MEMORY
    return max(1, min(by_cpu, by_memory))


def _spawn_safe() -> bool:
    """Spawned worker processes import the parent's main module again (as ``__mp_main__``): automatic workers
    only when that cannot run the caller's program a second time (the rule of ``engine._spawn_safe``: a
    package ``__main__`` such as ``python -m jsonreshape`` / ``python -m pytest``, a console-script launcher,
    jsonreshape's own modules, an interactive interpreter). A plain script asks for ``workers`` explicitly
    (behind ``if __name__ == "__main__":``)."""
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    name = getattr(spec, "name", None)
    if name is not None:
        return name == "__main__" or name.endswith(".__main__") or name.startswith("jsonreshape.")
    return getattr(main, "__file__", None) is None


def _worker_error(res: tuple) -> JsonReshapeError:
    from . import errors

    _tag, cls_name, key, params = res
    cls = getattr(errors, cls_name, None)
    if not (isinstance(cls, type) and issubclass(cls, JsonReshapeError)):
        cls = EditError
    try:
        return cls(key, **params)
    except TypeError:  # pragma: no cover - an error class with another signature
        return EditError(key, **params)


# ----------------------------------------------------------------------------------
# the pass of the content operations
# ----------------------------------------------------------------------------------
class _Pass:
    """One preview of a content operation (see :func:`preview`): reads the blocks of the scope, has their
    records worked on by a :class:`_Runner` -- in this thread (:meth:`block`) or in worker processes
    (:meth:`pooled`) -- and merges the results in position order: the counts, the first examples / problems,
    the units (appended to the preview's own segment in batches of at least :data:`APPEND_BYTES`, each batch
    followed by the free-space check every :data:`DISK_CHECK_BYTES`) and the layer entries."""

    def __init__(self, doc: Any, state: Any, store: Any, spec: BulkSpec, rule: _Rule,
                 cancel: CancelToken | None) -> None:
        self.doc = doc
        self.state = state
        self.store = store
        self.spec = spec
        self.rule = rule
        self.cancel = cancel
        self.member = bool(doc.member_records)
        self.style = editops.detect_style(doc)
        self.large = int(doc.settings.large_node_bytes)
        self.needles = rule.needles(int(doc.flags))
        self.runner = _Runner(rule, self.style, self.member, self.large, self.needles)
        self.scanned = 0
        self.records_changed = 0
        self.values_changed = 0
        self.conflicts = 0
        self.failures = 0
        self.skipped_huge = 0
        self.first_huge: int | None = None
        self.bytes_new = 0
        self.unit_flags = 0
        self.examples: list[BulkExample] = []
        self.problems: list[BulkExample] = []
        self.records: list[int] | None = []
        self.seg: int | None = None
        self.layer_path: Path | None = None
        self.workers = 1
        # units waiting to be appended: (bytes or memoryview, sizes, positions)
        self.pend: list[tuple[Any, array, array]] = []
        self.pend_bytes = 0
        # layer entries (rid, vstart, vend)
        self.e_rids = array("Q")
        self.e_starts = array("Q")
        self.e_ends = array("Q")
        self.spill_path: Path | None = None
        self.spill_f: BinaryIO | None = None
        self.spilled = 0
        # disk checks
        self.total = 1
        self.read_bytes = 0
        self.written = 0
        self.next_check = DISK_CHECK_BYTES

    # -- blocks ---------------------------------------------------------------------------------
    def _count(self, blk: Any) -> bool:
        """Scanned records and read bytes of a block; ``True`` for a huge block (only counted as skipped)."""
        idx = blk.indices
        n = len(idx)
        self.scanned += n
        if blk.huge:
            self.skipped_huge += n
            if self.first_huge is None or idx[0] < self.first_huge:
                self.first_huge = int(idx[0])
            self.read_bytes += blk.selected_bytes
            return True
        sp = _pairs(blk.spans)
        self.read_bytes += sum(sp[1::2]) - sum(sp[0::2])
        return False

    def block(self, blk: Any) -> None:
        """The records of one block, worked on in this thread."""
        if self._count(blk):
            return
        self.merge(*self.runner.run(blk.data, blk.spans, blk.indices, len(self.examples) < EXAMPLE_COUNT,
                                    len(self.problems) < PROBLEM_COUNT, self.cancel))

    def pooled(self, src: Any, workers: int, progress: Progress | None) -> None:
        """The scope's records in blocks of :data:`POOL_BLOCK_BYTES`, worked on by ``workers`` worker processes
        (at most ``2 * workers`` blocks in flight); the results are merged in order. ``BrokenProcessPool``
        when the workers cannot run (the caller falls back to this thread)."""
        import multiprocessing
        from collections import deque
        from concurrent.futures import ProcessPoolExecutor

        from . import i18n

        payload = (self.spec.kind, dict(self.spec.params), self.style, self.member, self.large, self.needles,
                   (_backend_name(_backend), _backend_name(editops._backend)), i18n.get_language())
        ex = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_pool_init, initargs=payload)
        self.workers = workers
        inflight = max(2, 2 * workers)
        waiting: deque = deque()
        blocks = self.doc.iter_blocks(src, block_bytes=POOL_BLOCK_BYTES, progress=progress, cancel=self.cancel)
        try:
            for blk in blocks:
                check(self.cancel)
                if self._count(blk):
                    continue
                waiting.append(ex.submit(_pool_block, blk.data, blk.spans, blk.indices.tobytes(),
                                         len(self.examples) < EXAMPLE_COUNT, len(self.problems) < PROBLEM_COUNT))
                while len(waiting) >= inflight:
                    self._take(waiting.popleft())
            while waiting:
                self._take(waiting.popleft())
        finally:
            blocks.close()
            for fut in waiting:
                fut.cancel()
            ex.shutdown(wait=True, cancel_futures=True)

    def _take(self, fut: Any) -> None:
        from concurrent.futures import TimeoutError as FutureTimeout

        while True:
            try:
                res = fut.result(timeout=POOL_WAIT)
                break
            except FutureTimeout:
                check(self.cancel)
        if res[0] != "ok":
            raise _worker_error(res)
        self.merge(*res[1:])

    def merge(self, blob: bytes | bytearray, sizes: array, positions: array, counts: tuple, flags: int, examples: list,
              problems: list, first_huge: int | None) -> None:
        """Add the result of one block (blocks in position order)."""
        changed, values, conflicts, failures, huge, bytes_new = counts
        self.records_changed += changed
        self.values_changed += values
        self.conflicts += conflicts
        self.failures += failures
        self.skipped_huge += huge
        self.bytes_new += bytes_new
        self.unit_flags |= flags
        if first_huge is not None and (self.first_huge is None or first_huge < self.first_huge):
            self.first_huge = first_huge
        for ex in examples:
            if len(self.examples) >= EXAMPLE_COUNT:
                break
            self.examples.append(BulkExample(*ex))
        for pb in problems:
            if len(self.problems) >= PROBLEM_COUNT:
                break
            self.problems.append(BulkExample(*pb))
        if not len(positions):
            return
        records = self.records
        if records is not None:
            if len(records) + len(positions) > RECORDS_MAX:
                self.records = None
            else:
                records.extend(positions)
        self._queue(blob, sizes, positions)

    # -- units and entries ------------------------------------------------------------------
    def _queue(self, blob: bytes | bytearray, sizes: array, positions: array) -> None:
        """Queue units (``blob`` = the units back to back, ``sizes``); a batch is appended as soon as it
        holds :data:`APPEND_BYTES` (the unit that reaches the size ends the batch)."""
        n = len(sizes)
        view = memoryview(blob)
        ends = list(accumulate(sizes))
        i = 0
        b = 0
        while i < n:
            j = bisect.bisect_left(ends, b + APPEND_BYTES - self.pend_bytes, i)
            if j >= n:
                self.pend.append((view[b:], sizes[i:], positions[i:]))
                self.pend_bytes += ends[-1] - b
                return
            e = ends[j]
            self.pend.append((view[b:e], sizes[i:j + 1], positions[i:j + 1]))
            self.pend_bytes += e - b
            self.flush()
            i = j + 1
            b = e

    def flush(self) -> None:
        if not self.pend:
            return
        assert self.seg is not None
        if len(self.pend) == 1:
            data, sizes, positions = self.pend[0]
        else:
            data = b"".join([p[0] for p in self.pend])
            sizes = array("Q")
            positions = array("Q")
            for _d, s, p in self.pend:
                sizes.extend(s)
                positions.extend(p)
        vstart, _vend = self.store.append(data, seg=self.seg)
        ends = array("Q", accumulate(sizes, initial=vstart))
        self.e_starts.extend(ends[:-1])
        self.e_ends.extend(ends[1:])
        self.e_rids.extend(self.state.rids_at(positions))
        self.pend = []
        self.pend_bytes = 0
        self.written += len(data)
        if self.written >= self.next_check:
            self.next_check = self.written + DISK_CHECK_BYTES
            _check_disk(self.store.dir, _need(self.total - self.read_bytes))
        if len(self.e_rids) >= ENTRY_BATCH:
            self._spill()

    def _spill(self) -> None:
        if self.spill_f is None:
            self.spill_path = self.store.dir / f"bulk-{os.getpid()}-{secrets.token_hex(6)}.entries.tmp"
            self.spill_f = open_shared(self.spill_path, "w+b")
        f = self.spill_f
        n = len(self.e_rids)
        f.write(_SPILL_HEAD.pack(n))
        for col in (self.e_rids, self.e_starts, self.e_ends):
            if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
                col = array("Q", col)
                col.byteswap()
            f.write(col.tobytes())
        self.spilled += n
        self.e_rids = array("Q")
        self.e_starts = array("Q")
        self.e_ends = array("Q")

    def entry_blocks(self) -> Iterator[tuple[array, array, array]]:
        """Every layer entry: the spilled batches, then the ones in memory."""
        f = self.spill_f
        if f is not None:
            f.flush()
            f.seek(0)
            while True:
                head = f.read(_SPILL_HEAD.size)
                if not head:
                    break
                (n,) = _SPILL_HEAD.unpack(head)
                cols = []
                for _ in range(3):
                    col = array("Q")
                    col.frombytes(f.read(8 * n))
                    if _BIG_ENDIAN:  # pragma: no cover
                        col.byteswap()
                    cols.append(col)
                check(self.cancel)
                yield cols[0], cols[1], cols[2]
        if self.e_rids:
            yield self.e_rids, self.e_starts, self.e_ends

    def remove_spill(self) -> None:
        if self.spill_f is not None:
            try:
                self.spill_f.close()
            except OSError:  # pragma: no cover
                pass
            self.spill_f = None
        if self.spill_path is not None:
            try:
                os.remove(self.spill_path)
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("bulk: temporary file %s not removed", self.spill_path, exc_info=True)
            self.spill_path = None

    def cleanup(self) -> None:
        """Delete everything the pass created (an error or cancellation)."""
        self.remove_spill()
        store = self.store
        if self.layer_path is not None:
            try:
                store.drop_layer_file(self.layer_path)
            except Exception:  # noqa: BLE001 - cleanup must not hide the original error
                log.warning("bulk: layer %s not removed", self.layer_path, exc_info=True)
            self.layer_path = None
        if self.seg is not None:
            try:
                store.drop_segment(self.seg)
            except Exception:  # noqa: BLE001
                log.warning("bulk: segment %s not removed", self.seg, exc_info=True)
            self.seg = None


def preview(doc_pinned: Any, state: Any, store: Any, spec: BulkSpec, scope: Any = None, *,
            progress: Progress | None = None, cancel: CancelToken | None = None,
            workers: int | None = None) -> BulkPreview:
    """Compute the exact result of ``spec`` over ``scope`` of ``state`` (module docstring).

    ``doc_pinned``: the document (any view of it; it is re-pinned to ``state``); ``state``: the
    :class:`~jsonreshape.overlay.OverlayState` the session shows; ``store``: its
    :class:`~jsonreshape.editstore.EditStore` (the preview's segment and pending layer file are created
    there); ``scope``: ``None`` = all records, or a RecordSet / ``range`` / sequence of positions of
    ``state``; ``workers``: worker processes for the records (``None``: automatic, :func:`auto_workers`;
    ``1``: this thread only; the result is the same either way). Raises ``UserInputError`` (bad spec),
    ``EditError`` (``bulk.error.stale_scope``, ``bulk.error.disk_space``, ``bulk.error.partial``, ...),
    ``Cancelled`` and ``FileChangedError``; on any error nothing is left behind."""
    started = time.perf_counter()
    if not isinstance(spec, BulkSpec):
        raise TypeError(f"expected a BulkSpec, not {type(spec).__name__}")
    if not isinstance(state, OverlayState):
        raise TypeError(f"expected an OverlayState, not {type(state).__name__}")
    rule = spec._compile()
    doc = _scan_view(doc_pinned, state, scope)
    _check_document(doc, None, cancel)
    scope_gen = getattr(scope, "gen", None) if scope is not None else None
    if rule is None:
        return _preview_dedupe(doc, state, spec, scope, scope_gen, progress, cancel, started)
    return _preview_content(doc, state, store, spec, rule, scope, scope_gen, progress, cancel, started, workers)


def _scan(doc: Any, state: Any, store: Any, spec: BulkSpec, rule: _Rule, src: Any, n_scope: int, total: int,
          workers: int, scan_p: Progress | None, cancel: CancelToken | None) -> _Pass:
    """The pass over the scope's records (worker processes when ``workers > 1``; when they cannot run,
    everything is done again in this thread); the remaining units are appended."""
    from concurrent.futures.process import BrokenProcessPool

    run = _Pass(doc, state, store, spec, rule, cancel)
    run.total = total
    run.seg = store.new_segment()
    try:
        if n_scope and workers > 1:
            try:
                run.pooled(src, workers, scan_p)
            except (BrokenProcessPool, OSError) as exc:
                if isinstance(exc, OSError) and not isinstance(exc, BrokenProcessPool) and run.written:
                    raise                            # a write error of the segment, not of the workers
                log.warning("bulk: the worker processes failed (%s); the preview runs in one thread", exc)
                run.cleanup()
                run = _Pass(doc, state, store, spec, rule, cancel)
                run.total = total
                run.seg = store.new_segment()
                workers = 1
        if n_scope and workers <= 1:
            blocks = doc.iter_blocks(src, progress=scan_p, cancel=cancel)
            try:
                for blk in blocks:
                    check(cancel)
                    run.block(blk)
            finally:
                blocks.close()
        run.flush()
    except BaseException:
        run.cleanup()
        raise
    return run


def _preview_content(doc: Any, state: Any, store: Any, spec: BulkSpec, rule: _Rule, scope: Any,
                     scope_gen: int | None, progress: Progress | None, cancel: CancelToken | None,
                     started: float, workers: int | None = None) -> BulkPreview:
    count = int(doc.record_count)
    src, n_scope = _plan_source(doc, scope, count, cancel)
    full = _data_bytes(doc)
    total = full if src is None else max(1, int(full * min(1.0, n_scope / max(1, count))))
    name = doc.source_path.name
    _check_disk(Path(store.dir), _need(total))
    if workers is None:
        workers = auto_workers(total, doc.settings) if _spawn_safe() else 1
    workers = max(1, int(workers))
    if progress is not None:
        if not progress.message:
            progress.set_message(t("bulk.progress.scan", name=name))
        if progress.total is None:
            progress.set_total(total)
    scan_p = progress.child(0.0, SCAN_END, total=total) if progress is not None else None
    run = _scan(doc, state, store, spec, rule, src, n_scope, total, workers, scan_p, cancel)
    try:
        check(cancel)
        doc.ensure_unchanged()           # results of a file another program changed are not reported
        if scan_p is not None:
            scan_p.finish()
        layer_name: str | None = None
        if run.records_changed:
            store.sync([run.seg])
            if progress is not None:
                progress.set_message(t("bulk.progress.layer", name=name))
            run.layer_path = store.new_layer_path()
            n = write_array_layer(run.layer_path, EntryBlocks(run.entry_blocks()), flags=run.unit_flags & 0xFFFFFFFF,
                                  segments=[run.seg], work_dir=None, cancel=cancel)
            if n != run.records_changed:  # pragma: no cover - would be a bug
                raise RuntimeError(f"bulk: {n} layer entries for {run.records_changed} changed records")
            layer_name = run.layer_path.name
        else:
            store.drop_segment(run.seg)
            run.seg = None
        run.remove_spill()
    except BaseException:
        run.cleanup()
        raise
    if progress is not None:
        progress.finish()
    warnings: list[str] = []
    if run.skipped_huge:
        warnings.append(t("bulk.warn.huge", count=run.skipped_huge, limit=format_bytes(run.large),
                          record=run.first_huge if run.first_huge is not None else "?"))
    old_flags = int(state.flags)
    pv = BulkPreview(
        spec=spec, gen=state.gen, scanned=run.scanned, records_changed=run.records_changed,
        values_changed=run.values_changed, conflicts=run.conflicts, failures=run.failures,
        skipped_huge=run.skipped_huge, examples=run.examples, problems=run.problems, bytes_new=run.bytes_new,
        elapsed=time.perf_counter() - started, warnings=warnings, layer=layer_name, segment=run.seg,
        flags=(old_flags, old_flags | (run.unit_flags if run.records_changed else 0)),
        records=None if run.records is None else sorted(run.records), scope_gen=scope_gen, workers=run.workers,
        _store=store)
    log.info("bulk %s preview of %s: %d scanned, %d records / %d values changed, %d conflicts, %d failures, "
             "%d huge, %s new (%.2f s, %d worker process(es))", spec.kind, name, pv.scanned, pv.records_changed,
             pv.values_changed, pv.conflicts, pv.failures, pv.skipped_huge, format_bytes(pv.bytes_new), pv.elapsed,
             run.workers if run.workers > 1 else 0)
    return pv

# ----------------------------------------------------------------------------------
# dedupe
# ----------------------------------------------------------------------------------
def _record_text(doc: Any, blk: Any, k: int, pos: int) -> str:
    """Example text of a whole record (member records: ``"key": value``)."""
    if blk.huge:
        return t("bulk.example.huge")
    raw = blk.record_bytes(k)
    try:
        if doc.member_records:
            key, vo = split_member(raw)
            return _cut(_text(key) + ": " + _text(codec.loads(raw[vo:])))
        return _text(codec.loads(raw))
    except (codec.JsonDecodeError, ValueError, RecursionError):
        return _cut(raw.decode("utf-8", "replace"))


def _preview_dedupe(doc: Any, state: Any, spec: BulkSpec, scope: Any, scope_gen: int | None,
                    progress: Progress | None, cancel: CancelToken | None, started: float) -> BulkPreview:
    p = spec.normalized()
    name = doc.source_path.name
    find_p = keys_p = None
    if progress is not None:
        if not progress.message:
            progress.set_message(t("bulk.progress.scan", name=name))
        if progress.total is None:
            progress.set_total(_data_bytes(doc))
        find_p = progress.child(0.0, DEDUPE_END)
    report = dupes.find_duplicates(doc, p["key"], scope, keep=p["keep"], progress=find_p, cancel=cancel)
    try:
        positions = array("Q")
        for chunk in report.records.iter_chunks():
            check(cancel)
            positions.extend(chunk)
    finally:
        report.close()
    if len(positions) != report.duplicates:  # pragma: no cover - would be a bug
        raise RuntimeError(f"dedupe: {len(positions)} positions for {report.duplicates} duplicates")
    examples: list[BulkExample] = []
    keys: list[tuple[int, str, None]] | None = None
    key_text = report.key
    if positions:
        member = bool(doc.member_records)
        rids = state.rids_at(positions)
        if member:
            keys = []
            if progress is not None:
                progress.set_message(t("bulk.progress.keys", name=name))
                keys_p = progress.child(DEDUPE_END, 1.0)
            blocks = doc.iter_blocks(positions, progress=keys_p, cancel=cancel)
            j = 0
            try:
                for blk in blocks:
                    check(cancel)
                    for k in range(len(blk.indices)):
                        pos = int(blk.indices[k])
                        if blk.huge:
                            key = doc.record_key(pos)
                        else:
                            key = split_member(blk.record_bytes(k))[0]
                        keys.append((int(rids[j]), key, None))
                        if len(examples) < EXAMPLE_COUNT:
                            examples.append(BulkExample(pos, key_text, _record_text(doc, blk, k, pos),
                                                        t("bulk.example.deleted")))
                        j += 1
            finally:
                blocks.close()
        else:
            blocks = doc.iter_blocks(positions[:EXAMPLE_COUNT], cancel=cancel)
            try:
                for blk in blocks:
                    for k in range(len(blk.indices)):
                        pos = int(blk.indices[k])
                        examples.append(BulkExample(pos, key_text, _record_text(doc, blk, k, pos),
                                                    t("bulk.example.deleted")))
            finally:
                blocks.close()
    check(cancel)
    doc.ensure_unchanged()
    problems: list[BulkExample] = []
    texts = report.warning_texts()
    for key, params, text in zip(report.warnings, report.warning_params, texts):
        record = params.get("record") if isinstance(params, dict) else None
        if isinstance(record, int) and key in ("dupes.warn.too_large", "dupes.warn.unreadable") \
                and len(problems) < PROBLEM_COUNT:
            problems.append(BulkExample(record, key_text, "", text))
    if progress is not None:
        progress.finish()
    pv = BulkPreview(
        spec=spec, gen=state.gen, scanned=report.scanned, records_changed=report.duplicates, values_changed=0,
        conflicts=0, failures=report.skipped, skipped_huge=0, examples=examples, problems=problems, bytes_new=0,
        elapsed=time.perf_counter() - started, warnings=texts, flags=(int(state.flags), int(state.flags)),
        records=[], positions=positions, keys=keys, scope_gen=scope_gen)
    log.info("bulk dedupe preview of %s by %s: %d scanned, %d duplicates in %d groups (%.2f s)", name, key_text,
             report.scanned, report.duplicates, report.groups, pv.elapsed)
    return pv


# ----------------------------------------------------------------------------------
# the step
# ----------------------------------------------------------------------------------
def make_step(preview: BulkPreview) -> Step:
    """The :class:`~jsonreshape.editstore.Step` that applies ``preview`` (module docstring): commit it with
    ``store.commit(step, state=state)`` and build the state with ``editstore.apply_step``."""
    if preview.discarded:
        raise EditError("bulk.error.discarded")
    if preview.records_changed <= 0:
        raise UserInputError("bulk.error.nothing")
    spec = preview.spec
    kind = spec.kind
    try:
        rule = spec.normalized()
    except JsonReshapeError:  # pragma: no cover - a preview exists only for a valid spec
        rule = {}
    count = preview.records_changed if kind in ("set_field", "dedupe") else preview.values_changed
    params = {"count": count, "kind": kind, "rule": rule}
    label = f"edits.step.bulk.{kind}"
    if kind == "dedupe":
        positions = preview.positions
        if positions is None or len(positions) != preview.records_changed:
            raise EditError("bulk.error.discarded")
        step = Step("bulk", label, params, struct=[("dels", array("Q", positions))], records=[],
                    keys=list(preview.keys) if preview.keys else None)
    else:
        if preview.layer is None or preview.segment is None:
            raise EditError("bulk.error.discarded")
        old, new = preview.flags
        step = Step("bulk", label, params, push_layer=preview.layer, segments=[preview.segment],
                    flags=(old, new) if new != old else None, records=preview.records)
    preview._step = step
    return step
