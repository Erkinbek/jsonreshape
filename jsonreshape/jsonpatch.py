"""JSON Patch (RFC 6902) import and export of the edits, JSON Pointers (RFC 6901) (docs/ARCHITECTURE.md §12.8).

* :func:`iter_patch` streams a patch -- the top-level array is scanned with the core ``Scanner``
  (``MODE_CONTAINER``), every operation is parsed on its own with exact numbers (``codec`` types) and validated
  structurally (RFC 6902 §4): :class:`PatchOp` objects, or ``PatchError("patch.error.*", index=<op number>, op,
  path)``.
* :func:`apply_patch` applies a patch to an :class:`~jsonreshape.edits.EditSession` as ONE step (kind
  ``"patch"``): pass 1 validates every operation and collects the record keys the pointers use (member-record
  documents); pass 2 applies the operations in order on a working copy; nothing changes when any operation
  fails (the segment the patch wrote is deleted).
* :func:`export_patch` writes the net effect of the edits of a session (against the original file) as a patch;
  :func:`export_layer` does the same for a pending or stale layer (:class:`~jsonreshape.editstore.EditStore`).

Pointers (RFC 6901, :func:`jsonreshape.paths.parse_pointer`: strict, no stripping, ``~0``/``~1``)
==============================================================================================
The patched value is the document itself: array / jsonl / stream / brace_list documents are an ARRAY of records
(``/12/name`` = member ``name`` of record 12, ``/-`` = after the last record); keyed documents are an OBJECT of
records (``/id42/name``; the record is found through the session's :class:`~jsonreshape.edits.KeyIndex`); a
wrapped document is its root value with the records at the record path (``/data/12/name``; ``/data/-``);
single documents are their root value. Record positions are the effective positions of the working copy at
the moment of the operation (every operation sees the result of the previous ones, RFC 6902 §3). Array indices
follow RFC 6901: ``0`` or digits without a leading zero; ``-`` (the end of an array) only as the last segment
of an ``add`` path or of a ``move`` / ``copy`` target; anywhere else -> ``patch.error.dash``.

Import (decisions where §12.8 leaves room; all chosen to keep the user's data safe)
=================================================================================
1. Sources: a file path (``str`` / ``os.PathLike``), the patch text as ``bytes``, a binary file object
   (read from its current position), or a ``list`` of operation objects (already parsed). Files may be UTF-8
   (with or without a BOM), UTF-16 / UTF-32 (BOM or NUL pattern, :func:`jsonreshape.fileio.sniff`) and gzip;
   the text is streamed (memory: the largest operation + 1 MiB), read twice (pass 1, pass 2) unless it is at
   most :data:`KEEP_OPS_BYTES` or cannot be read again (a non-seekable stream): its operations are then kept
   from pass 1. A JSON error in the patch is ``PatchError("patch.error.json", line, column, reason)`` (a user
   error, CLI exit 1).
2. An operation object with a key twice (RFC 6902 appendix A.13), also inside its value, is refused
   (``patch.error.op_duplicate_key``): duplicate keys have no defined meaning. Members other than ``op``,
   ``path``, ``from`` and ``value`` are ignored (RFC 6902 §4, appendix A.11).
3. Records are parsed with exact numbers into an LRU (at most ``Settings.memory_limit_bytes // 4``, estimated
   as :data:`LRU_FACTOR` x the record's bytes); a changed record that leaves the LRU is serialized in the
   document's style (``editops.format_record``; a member record keeps its original ``"key":`` head while its
   key is unchanged) and appended to the patch's own segment (``store.new_segment()``); a touched record is
   re-read from there. A record whose key is only renamed keeps every byte but the key token. Changed records
   are re-serialized (string escapes normalized, whitespace in the document's style; values exact) -- the same
   documented deviation as the bulk operations; so is a patched wrapper (``editops.format_value`` at level 0:
   the members around the record container follow the detected style, the record container's own bytes are
   kept by the saver). Records, values or units over ``Settings.large_node_bytes``
   -> ``patch.error.huge_record`` (whole-record ``remove`` / ``move`` and value-document ``copy`` never read a
   record, so they also work on huge ones).
4. A touched record (or the wrapper) that has the same key twice in one object, and a record key that more
   than one record has (keyed documents), cannot be addressed safely: ``patch.error.duplicate_keys``.
5. Objects: ``add`` onto an existing member replaces its value in place (RFC 6902 §4.1); a new member is
   appended; ``move`` between two members of the SAME object renames the member in place (keyed records: the
   record keeps its position); when the target exists it is replaced and the source removed. Object member
   order is not part of JSON Patch semantics (RFC 6902 / RFC 8259); these choices keep the user's layout.
   Keyed documents: a new record is appended after the last one.
6. ``test`` compares with query equality (``jsonreshape.query``: numbers by their exact decimal value, so
   ``1 == 1.0``; strings exactly; containers deeply; no type coercion: ``"10" != 10``). A failed test ->
   ``patch.error.test_failed`` (CLI exit 1) and nothing changes.
7. The record container: ``test`` compares it record by record (streamed); ``add`` / ``replace`` of it
   replace ALL records (the value must be an array -- an object for member-record documents --:
   ``patch.error.container_type``); ``remove`` / ``move`` of it -> ``patch.error.container_locked``
   (``patch.error.root`` for the root of a non-wrapped document). Wrapped documents: the ancestors of the
   container may only be tested; an ``add`` / ``remove`` in an ancestor array at an index that would shift
   the container -> ``patch.error.container_locked`` (the rules of the edit session, §12.5). ``copy`` FROM
   the container or an ancestor materializes it (at most ``large_node_bytes``, else
   ``patch.error.too_large``). Single documents: the root may be replaced, not removed.
8. The step: kind ``"patch"``, label ``edits.step.patch`` (``count`` = operations); more than
   :data:`~jsonreshape.overlay.RECORDS_MAX` changed records -> one array layer; a changed record structure is
   ONE ``("pieces", old, new)`` Remap; ``Step.keys`` (member-record documents) lists every renamed, new and
   deleted record (``None`` after the container was replaced: the KeyIndex is rebuilt); ``records`` =
   changed positions that existed before (``None`` when many); no NodeChanges (like a bulk step). A patch
   that changes nothing commits no step (an empty ChangeSet of the current state, kind ``"patch"``).
9. A JSON Patch file that is not an array -> ``patch.error.not_array``; an empty file -> ``patch.error.empty``.
   Operation numbers in messages are 0-based (the project's convention: the first operation is #0).

Export (net effect against the original file)
============================================
* Value documents (array / jsonl / stream / brace_list / wrapped array): the final record order is the piece
  table (base records keep their original index as record id). A copy of an original record (a new record
  whose content is still that record's original span: a duplicate) is a ``copy``; when the original itself
  was deleted, its first copy (in final order) takes its place instead (a ``move``, no content). The kept
  original records are the maximum-weight increasing subsequence of the runs of original records (weight =
  run length); then, in this order: (1) ``remove`` of the deleted originals, descending; (2) ``move`` of the
  other surviving originals, in ascending final order, each right after its final predecessor (current
  positions from Fenwick trees: O(m log m)); (3)+(4) ``copy`` / ``add`` of the new records in ONE ascending
  pass of final positions (the copies' sources still hold their original content); (5) for originals with
  changed content a structural diff at the FINAL positions (objects: ``remove`` / ``add`` / ``replace`` per
  key; arrays of equal length per index, else ``replace``; numbers and strings by their exact text; a
  changed type -> ``replace``); (6) the wrapper (wrapped documents): a structural diff of the wrapper text
  around the record container (arrays on the record path: per index before the container, the tail as
  ``remove`` / ``add``; the container itself is never replaced).
* Member-record documents (keyed / wrapped object): (1) ``remove`` of deleted original records by their
  original key; (2) value diffs of the changed originals at their ORIGINAL keys (one pass, nothing kept in
  memory); (3) renames as ``move`` (``from`` = original key) when the new key is free at that point -- the
  renames form disjoint paths, emitted from their free end, and cycles, broken through a temporary key
  ``jsonreshape-tmp-<hex>`` (O(renames)); (4) ``add`` of new records (appended: record order is not
  represented, like any object member order, RFC 6902); (5) the wrapper. Original keys are read from the
  file (``diff=True``: exact even after history trimming) or taken from ``Step.keys`` (``diff=False``:
  ``(rid, old, new)`` of renames, ``(rid, key, None)`` of deletions; the first recorded ``old`` of a record
  is its original key).
* Single documents: a diff of the root (``diff=False``: ``replace ""``).
* ``diff=False`` (the source changed on disk; a stale layer): no byte of the data file is read: records with
  changed content are written whole (``replace`` at the final position; ``add`` by key in member-record
  documents, which also creates a record whose rename could not be recovered); the edited wrapper is written
  as ``add`` of every member of the objects on the record path (and ``replace`` of the array elements before
  the container). Known gaps, reported as warnings (``warnings=`` list, localized): wrapper members that were
  deleted, keys lost with history trimming (``maybe_trim``) or with deletions of more than
  ``edits.KEYS_MAX`` records, and -- only without any document of the layer's file version (a stale layer)
  -- copies of original records (their content is in the old file; they are left out).
* :func:`export_layer`: the state is ``store.load_state()`` (a damaged layer: up to ``last_good``); with a
  ``base_doc`` of the SAME file version (``layer_key(base_doc) == store.key`` and not ``store.stale``) the
  export is like :func:`export_patch` (``diff=True`` when the file is unchanged on disk), otherwise
  ``diff=False`` semantics without any document. The store's kind and record path come from its meta
  (``store._meta``, a deliberate private access); the container kind of a wrapped layer from its wrapper
  placeholder / step keys / units / ``base_doc``. :func:`export_layer` never closes the store (the caller
  owns it: ``close(close_reader=True)``).
* The patch file is written atomically (``fileio.atomic_write``): ``[``, one operation per line
  (``{"op": "...", "from": "...", "path": "...", "value": ...}``, values compact with exact numbers; raw
  record values keep every token byte for byte), ``]``. The functions return the number of operations. A
  target that is the document's own file (source or data file) is refused (``writer.error.same_file``).
  Progress counts the bytes of the records read (no total: it is not known before the pass).

Speed and memory (the results are unchanged)
============================================
* Export: changed records are read in chunks of :data:`EXPORT_CHUNK` with coalesced reads (the units of a
  step lie side by side, the originals in file order; the spans of nearby originals come from one index
  read, ``Document._spans_bytes``) instead of one read per record and side; the diff
  skips equal subtrees at once (equal ``codec.dumps`` texts; scalars by type and value, which is the same
  for codec values) and carries the pointer texts; the bookkeeping of the original records uses arrays
  (no per-run lists, no sort when no record moved). Many changed records: the diffs of the chunks run in
  worker processes (``workers``; automatic -- ``None`` -- once :data:`POOL_MIN_BYTES` of changed records were
  diffed in the calling thread and ``bulk._spawn_safe()`` allows it, ``bulk.auto_workers`` of them: 4 with
  the default memory budget); the calling thread reads the records and writes the operations of the chunks
  in order (at most ``2 * workers`` chunks in flight): the same patch byte for byte. A chunk a worker cannot
  do is done in the calling thread (an error is raised exactly as without workers); ``BrokenProcessPool``:
  the rest is done there too.
* Import: an operation or record is parsed with ``codec.loads`` and kept when its ``codec.dumps`` text equals
  the input without whitespace (no key can be repeated then), else by the exact decoder (repeated keys,
  escapes, very deep values); pointers without ``~`` are split directly; the units of an append batch are
  validated in one scanner pass; the spans of the written records are a compact log (:class:`_SpanLog`:
  ~24 bytes per write, one byte per record id) instead of a dict, and the step is built from arrays
  (``write_array_layer`` keeps the last write of a record) after the record cache was freed; with the state's
  own record table (nothing added, deleted or moved) the written records of the step are the ones that
  existed (no position lookup per record); :data:`LRU_FACTOR` is the measured memory of a parsed record per
  byte, so the record cache stays near its budget (``memory_limit_bytes // 4``).

Extensions of §12.8 (all optional): ``apply_patch(..., stats=dict)`` (``ops``, ``records``, ``changed``; the
CLI's summary), ``export_patch(..., warnings=list)`` / ``export_layer(..., warnings=list)`` (the localized
warnings of the known gaps), ``export_patch(..., workers=)`` / ``export_layer(..., workers=)`` (worker
processes, see above), :func:`make_op` (validation of one parsed operation), :data:`NO_VALUE`,
:func:`set_core_backend` (tests); ``iter_patch(source, *, progress, cancel)``. ``EditSession.import_patch``
/ ``export_patch`` delegate here (the session's ``export_patch`` has no ``warnings`` parameter: callers that
want them call :func:`export_patch` directly, it takes the session's lock itself).

Private helpers of other modules used deliberately: ``codec._parse_float`` / ``_parse_int`` /
``_reject_constant`` (the exact number hooks, for a decoder that also detects duplicate keys),
``EditStore._meta`` (see above), ``bulk._spawn_safe`` (the worker-process rule of the bulk previews),
``Document._spans_bytes`` / ``_base_span`` (spans of original records, without the overlay). Query equality
is ``query.json_equal`` (like ``bulk``).
"""

from __future__ import annotations

import bisect
import codecs
import copy
import itertools
import json
import logging
import math
import operator
import os
import secrets
import sys
import zlib
from array import array
from collections import OrderedDict
from itertools import accumulate, compress
from operator import add, sub
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO, Callable, Iterable, Iterator, NamedTuple, Sequence

from . import codec, core, editops
from .core.constants import MODE_CONTAINER, MODE_STREAM
from .editstore import LayerDamagedError, Step, layer_key
from .errors import EditError, JsonReshapeError, PathSyntaxError, UserInputError
from .fileio import atomic_write, open_shared, read_range, sniff
from .i18n import t
from .overlay import EDIT_BASE, RECORDS_MAX, SEG_SHIFT, ChangeSet, EntryBlocks, OverlayState, PieceTable, \
    split_edit_offset, write_array_layer
from .paths import canonical_index, format_pointer, parse_pointer
from .query import json_equal
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "KEEP_OPS_BYTES",
    "LRU_FACTOR",
    "NO_VALUE",
    "OPS",
    "PatchError",
    "PatchOp",
    "apply_patch",
    "export_layer",
    "export_patch",
    "iter_patch",
    "make_op",
    "set_core_backend",
]

# ----------------------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------------------
OPS = ("add", "remove", "replace", "move", "copy", "test")
_NEEDS_VALUE = frozenset({"add", "replace", "test"})
_NEEDS_FROM = frozenset({"move", "copy"})
READ_CHUNK = 1 << 20               # bytes of the patch file read per step
KEEP_OPS_BYTES = 8 << 20           # a patch up to this size keeps its parsed operations from pass 1
LRU_FACTOR = 9                     # estimated memory of a parsed record per byte of its JSON text (measured:
                                   # ~8x for the values + the cache entry)
LRU_MIN = 16 << 20                 # smallest record cache (bytes, estimated)
APPEND_BATCH = 4 << 20             # units are appended to the patch's segment in batches of this size
ORDER_RUNS = 128                   # runs per block of a working record order (split beyond)
_CHECK_EVERY = 256                 # operations / records between cancel checks in tight loops
EXPORT_CHUNK = 4096                # export: changed records read (coalesced) and diffed together
COALESCE_GAP = 64 << 10            # export: spans this close are read with one read ...
COALESCE_MAX = 8 << 20             # ... of at most this many bytes
FAST_DEPTH = 1000                  # _load_checked: a text with this many brackets goes to the exact decoder
SPAN_FREEZE = 65536                # _SpanLog: latest writes kept in a dict before they become sorted arrays
SPAN_SPILL = 1 << 21               # _SpanLog: sorted writes kept in memory before they go to a temporary file
SPAN_SPARSE = 512                  # _SpanLog: every this many rids of a spilled run are kept in memory

_MISSING: Any = object()


class _NoValue:
    """The ``value`` of an operation that has none (remove / move / copy)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NO_VALUE"


NO_VALUE: Any = _NoValue()

_core_override: ModuleType | None = None


def set_core_backend(backend: str | ModuleType | None) -> ModuleType | None:
    """Use a specific core backend (``"rust"`` / ``"python"`` or a module) for the Scanner and Reformatter of
    this module; ``None`` = the active ``jsonreshape.core``. Returns the previous override (tests)."""
    global _core_override
    old = _core_override
    _core_override = core.get_backend(backend) if isinstance(backend, str) else backend
    return old


def _api() -> Any:
    return _core_override if _core_override is not None else core


# ----------------------------------------------------------------------------------
# errors
# ----------------------------------------------------------------------------------
class PatchError(UserInputError):
    """A JSON Patch that cannot be read or applied (``patch.error.*``). ``index`` = the 0-based operation
    number, ``op`` / ``path`` the operation (when known)."""

    key = "patch.error.generic"


class _DupKey(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        seen: set[str] = set()
        for k, _v in pairs:
            if k in seen:
                raise _DupKey(k)
            seen.add(k)
    return obj


# exact numbers (codec's hooks) + duplicate keys detected in every object
_DECODER = json.JSONDecoder(object_pairs_hook=_unique_pairs, parse_float=codec._parse_float,
                            parse_int=codec._parse_int, parse_constant=codec._reject_constant)


def _decode_exact(data: bytes | bytearray | memoryview) -> Any:
    """One JSON value with exact numbers; ``_DupKey`` for an object with a key twice, ``RecursionError`` for
    values nested deeper than the decoder allows, ``ValueError`` for invalid text."""
    text = bytes(data).decode("utf-8")
    return _DECODER.decode(text)


def _load_checked(data: bytes) -> Any:
    """:func:`_decode_exact` with a fast path: ``codec.loads`` (exact numbers, orjson when the tokens allow)
    and, when that value's ``codec.dumps`` text equals the input or the input without whitespace (so no
    object repeats a key: the parse keeps one member per key), that value. Anything else -- a repeated key,
    escapes written another way, a text with :data:`FAST_DEPTH` or more brackets (possibly nested that deep:
    the exact decoder's limit decides), invalid text -- goes to :func:`_decode_exact` and its errors."""
    data = bytes(data)
    if len(data) < 2 * FAST_DEPTH or data.count(b"[") + data.count(b"{") < FAST_DEPTH:
        try:
            value = codec.loads(data)
        except (codec.JsonDecodeError, ValueError, RecursionError):
            pass
        else:
            text = codec.dumps(value)
            if text == data or text == _compact(data):
                return value
    return _decode_exact(data)


def _ptr(segs: Sequence[Any]) -> str:
    return format_pointer(tuple(segs))


def _json_reason(code: str, expected: str) -> str:
    text = t(f"json.error.{code}")
    if expected and expected != "none":
        exp = t(f"json.expected.{expected}")
        if exp:
            text = f"{text}; {exp}"
    return text


# ----------------------------------------------------------------------------------
# operations
# ----------------------------------------------------------------------------------
class PatchOp(NamedTuple):
    """One validated JSON Patch operation: ``index`` (0-based), ``op``, ``path`` (the pointer text) and its
    ``segments``, ``from_`` / ``from_segments`` (move / copy, else ``None``), ``value`` (:data:`NO_VALUE` for
    remove / move / copy; exact numbers), ``size`` (bytes of the operation's JSON text: a memory estimate)."""

    index: int
    op: str
    path: str
    segments: tuple
    from_: str | None
    from_segments: tuple | None
    value: Any
    size: int


def _pointer_segments(text: Any, index: int, op: str, member: str) -> tuple[str, ...]:
    if not isinstance(text, str):
        raise PatchError("patch.error.member_type", index=index, op=op, member=member)
    if text[:1] == "/" and "~" not in text:
        return tuple(text[1:].split("/"))          # parse_pointer of a pointer without escapes
    try:
        return parse_pointer(text)
    except PathSyntaxError as exc:
        raise PatchError("patch.error.pointer", index=index, op=op, member=member, pointer=text,
                         detail=str(exc.params.get("detail") or exc.message)) from None


def make_op(index: int, obj: Any, size: int = 0) -> PatchOp:
    """Validate one operation object (RFC 6902 §4) -> :class:`PatchOp` (``PatchError`` otherwise)."""
    if not isinstance(obj, dict):
        raise PatchError("patch.error.op_not_object", index=index)
    op = obj.get("op", _MISSING)
    if op is _MISSING:
        raise PatchError("patch.error.member_missing", index=index, op="?", member="op")
    if not isinstance(op, str):
        raise PatchError("patch.error.member_type", index=index, op="?", member="op")
    if op not in OPS:
        raise PatchError("patch.error.op_unknown", index=index, op=op)
    if "path" not in obj:
        raise PatchError("patch.error.member_missing", index=index, op=op, member="path")
    path = obj["path"]
    segs = _pointer_segments(path, index, op, "path")
    from_text: str | None = None
    from_segs: tuple | None = None
    if op in _NEEDS_FROM:
        if "from" not in obj:
            raise PatchError("patch.error.member_missing", index=index, op=op, member="from")
        from_text = obj["from"]
        from_segs = _pointer_segments(from_text, index, op, "from")
        if op == "move" and len(from_segs) < len(segs) and segs[:len(from_segs)] == from_segs:
            raise PatchError("patch.error.move_into_self", index=index, op=op, path=path, pointer=from_text)
    value = NO_VALUE
    if op in _NEEDS_VALUE:
        if "value" not in obj:
            raise PatchError("patch.error.member_missing", index=index, op=op, member="value")
        value = obj["value"]
    return PatchOp(index, op, path, segs, from_text, from_segs, value, int(size))


# ----------------------------------------------------------------------------------
# reading patch files
# ----------------------------------------------------------------------------------
class _Source:
    """A patch that can be read more than once: a path, bytes, a seekable binary file or a list of ops."""

    def __init__(self, source: Any) -> None:
        self.ops: list | None = None
        self.data: bytes | None = None
        self.path: Path | None = None
        self.file: Any = None
        self.start = 0
        if isinstance(source, (list, tuple)):
            self.ops = list(source)
        elif isinstance(source, (bytes, bytearray, memoryview)):
            self.data = bytes(source)
        elif isinstance(source, (str, os.PathLike)):
            self.path = Path(source)
        elif hasattr(source, "read"):
            self.file = source
            try:
                self.start = source.tell() if source.seekable() else -1
            except (OSError, ValueError, AttributeError):
                self.start = -1
        else:
            raise TypeError(f"a JSON Patch source is a path, bytes, a binary file or a list, "
                            f"not {type(source).__name__}")
        self._reads = 0

    @property
    def is_list(self) -> bool:
        return self.ops is not None

    @property
    def rereadable(self) -> bool:
        """The source can be read a second time (a non-seekable stream cannot)."""
        return self.file is None or self.start >= 0

    def size(self) -> int | None:
        if self.data is not None:
            return len(self.data)
        if self.path is not None:
            try:
                return os.stat(self.path).st_size
            except OSError:
                return None
        if self.file is not None and self.start >= 0:
            try:
                pos = self.file.tell()
                end = self.file.seek(0, os.SEEK_END)
                self.file.seek(pos)
                return max(0, end - self.start)
            except (OSError, ValueError, AttributeError):
                return None
        return None

    def raw_chunks(self) -> Iterator[bytes]:
        """The raw bytes of the patch in chunks (a fresh pass on every call)."""
        self._reads += 1
        if self.data is not None:
            data = self.data
            for i in range(0, len(data), READ_CHUNK):
                yield data[i:i + READ_CHUNK]
            return
        if self.path is not None:
            with open_shared(self.path, "rb", buffering=0) as f:
                while True:
                    try:
                        chunk = f.read(READ_CHUNK)
                    except OSError as exc:
                        from .fileio import os_error

                        raise os_error(exc, self.path) from exc
                    if not chunk:
                        return
                    yield chunk
        f = self.file
        if self._reads > 1:
            if self.start < 0:
                raise ValueError("the JSON Patch file object cannot be read twice (not seekable)")
            f.seek(self.start)
        while True:
            chunk = f.read(READ_CHUNK)
            if not chunk:
                return
            yield bytes(chunk)


def _gunzip(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Decompress gzip data (any number of members), at most :data:`READ_CHUNK` of output per step."""
    d = zlib.decompressobj(31)
    started = False
    for chunk in chunks:
        data = chunk
        while data:
            if d is None:
                if not data.strip(b"\0"):
                    break
                d = zlib.decompressobj(31)
            try:
                out = d.decompress(data, READ_CHUNK)
            except zlib.error as exc:
                raise PatchError("patch.error.encoding", detail=f"gzip: {exc}") from None
            started = True
            if out:
                yield out
            if d.eof:
                data = d.unused_data
                d = None
            else:
                data = d.unconsumed_tail
                while not data and not d.eof:
                    out = d.decompress(b"", READ_CHUNK)
                    if not out:
                        break
                    yield out
    if d is not None and started and not d.eof:
        raise PatchError("patch.error.encoding", detail="gzip: truncated")


def _transcode(chunks: Iterator[bytes], encoding: str) -> Iterator[bytes]:
    dec = codecs.getincrementaldecoder(encoding)("strict")
    try:
        for chunk in chunks:
            text = dec.decode(chunk)
            if text:
                yield text.encode("utf-8")
        text = dec.decode(b"", final=True)
        if text:
            yield text.encode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise PatchError("patch.error.encoding", detail=f"{encoding}: {exc.reason}") from None


def _head(chunks: Iterator[bytes], n: int = 4) -> tuple[bytes, Iterator[bytes]]:
    """At least ``n`` leading bytes (fewer at the end of the data) and an iterator over ALL the data."""
    head = b""
    it = iter(chunks)
    for chunk in it:
        head += chunk
        if len(head) >= n:
            break
    return head, itertools.chain([head] if head else [], it)


def _utf8_chunks(raw: Iterator[bytes]) -> Iterator[bytes]:
    """The patch text as UTF-8 chunks: gzip decompressed, UTF-16/32 transcoded, a UTF-8 BOM dropped."""
    head, chunks = _head(raw)
    sn = sniff(head)
    if sn.gzip:
        head, chunks = _head(_gunzip(chunks))
        sn = sniff(head)
    if sn.encoding in ("utf-8", "utf-8-bom"):
        skip = sn.bom_len
        for chunk in chunks:
            if skip:
                chunk = chunk[skip:]
                skip = 0
            if chunk:
                yield chunk
        return
    enc = {"utf-16-le": "utf-16-le", "utf-16-be": "utf-16-be", "utf-32-le": "utf-32-le",
           "utf-32-be": "utf-32-be"}[sn.encoding]

    def drop_bom() -> Iterator[bytes]:
        skip = sn.bom_len
        for chunk in chunks:
            if skip:
                take = min(skip, len(chunk))
                chunk = chunk[take:]
                skip -= take
            if chunk:
                yield chunk

    yield from _transcode(drop_bom(), enc)


def _counted(chunks: Iterator[bytes], on_bytes: Callable[[int], None] | None) -> Iterator[bytes]:
    for chunk in chunks:
        yield chunk
        if on_bytes is not None:
            on_bytes(len(chunk))


def _scan_ops(chunks: Iterator[bytes], cancel: CancelToken | None) -> Iterator[tuple[int, bytes]]:
    """``(index, JSON text)`` of every element of the top-level array of the UTF-8 ``chunks``."""
    sc = _api().Scanner(MODE_CONTAINER, [], 0)
    buf = bytearray()
    base = 0
    index = 0
    seen = False
    spans = array("Q")
    for chunk in chunks:
        check(cancel)
        if not seen:
            lead = chunk.lstrip(b" \t\r\n")
            if lead:
                seen = True
                if lead[0] == 0x7B:   # "{": an object is not a JSON Patch (the scanner would emit its members)
                    raise PatchError("patch.error.not_array")
        out = sc.feed(chunk)
        buf += chunk
        if out:
            spans = array("Q")
            spans.frombytes(out)
            for k in range(0, len(spans), 2):
                s, e = spans[k], spans[k + 1]
                yield index, bytes(buf[s - base:e - base])
                index += 1
            cut = spans[-1]
            del buf[:cut - base]
            base = cut
        if sc.error is not None:
            raise _scan_error(sc.error, buf, base, seen)
    out = sc.finish()
    if out:
        spans = array("Q")
        spans.frombytes(out)
        for k in range(0, len(spans), 2):
            s, e = spans[k], spans[k + 1]
            yield index, bytes(buf[s - base:e - base])
            index += 1
    if sc.error is not None:
        raise _scan_error(sc.error, buf, base, seen)


def _scan_error(err: tuple, buf: bytearray, base: int, seen: bool) -> PatchError:
    offset, code, expected, line, line_start = err
    if code == "unexpected_eof" and not seen:
        return PatchError("patch.error.empty")
    if code in ("target_not_container", "target_not_found"):
        return PatchError("patch.error.not_array")
    offset, line_start = int(offset), int(line_start)
    if base <= line_start <= offset <= base + len(buf):
        column = len(bytes(buf[line_start - base:offset - base]).decode("utf-8", "replace")) + 1
    else:
        column = offset - line_start + 1
    return PatchError("patch.error.json", line=int(line) + 1, column=column, reason=_json_reason(code, expected))


def _parse_op(index: int, text: bytes) -> PatchOp:
    if not text.startswith(b"{"):
        raise PatchError("patch.error.op_not_object", index=index)
    try:
        obj = _load_checked(text)
    except _DupKey as exc:
        raise PatchError("patch.error.op_duplicate_key", index=index, key=exc.key) from None
    except RecursionError:
        raise PatchError("patch.error.op_unreadable", index=index, detail=t("patch.detail.too_deep")) from None
    except ValueError as exc:   # cannot happen for scanner-validated text (except numbers beyond any limit)
        raise PatchError("patch.error.op_unreadable", index=index, detail=str(exc)) from None
    return make_op(index, obj, len(text))


def iter_patch(source: Any, *, progress: Progress | None = None,
               cancel: CancelToken | None = None) -> Iterator[PatchOp]:
    """Stream the operations of a JSON Patch (module docstring, import 1): every element of the top-level array
    is parsed with exact numbers and validated (RFC 6902 §4) -> :class:`PatchOp`; ``PatchError`` otherwise.
    ``progress`` counts the bytes of the source read."""
    src = source if isinstance(source, _Source) else _Source(source)
    if src.is_list:
        assert src.ops is not None
        for i, obj in enumerate(src.ops):
            if not i % _CHECK_EVERY:
                check(cancel)
            yield make_op(i, obj, _value_size(obj))
        return
    on_bytes = None
    if progress is not None:
        size = src.size()
        if size is not None:
            progress.set_total(size)
        on_bytes = progress.advance
    for index, text in _scan_ops(_utf8_chunks(_counted(src.raw_chunks(), on_bytes)), cancel):
        yield _parse_op(index, text)


def _value_size(obj: Any) -> int:
    """A rough size estimate of an already parsed operation (bytes of its JSON text)."""
    try:
        return len(codec.dumps(obj))
    except (TypeError, ValueError):
        return 256


# ----------------------------------------------------------------------------------
# the patched document: layout and pointer classification
# ----------------------------------------------------------------------------------
_CONTAINER, _ANCESTOR, _WRAPPER, _RECORD = range(4)


def _seg_matches(seg: str, cseg: Any) -> bool:
    """A pointer segment names the record-path segment ``cseg`` (an int = array index, a str = key)."""
    if isinstance(cseg, int) and not isinstance(cseg, bool):
        return canonical_index(seg) == cseg
    return seg == cseg


class _Layout:
    """Where the records are in the patched value (module docstring, "Pointers")."""

    def __init__(self, kind: str, record_path: Any, member: bool) -> None:
        self.kind = kind
        self.single = kind == "single"
        self.wrapped = kind == "wrapped"
        self.member = bool(member)
        self.cpath: tuple = tuple(record_path or ()) if self.wrapped else ()
        self.cptr: tuple[str, ...] = tuple(str(s) for s in self.cpath)
        self.placeholder = b"{}" if self.member else b"[]"

    def classify(self, segs: tuple) -> tuple:
        """``(_CONTAINER,)`` | ``(_ANCESTOR,)`` | ``(_WRAPPER,)`` | ``(_RECORD, head, rest)`` (``head`` =
        the record's index / key segment; ``None`` for the root record of a single document)."""
        if self.single:
            return (_RECORD, None, segs)
        cpath = self.cpath
        n = len(cpath)
        if n:
            k = 0
            while k < n and k < len(segs) and _seg_matches(segs[k], cpath[k]):
                k += 1
            if k < n:
                return (_ANCESTOR,) if k == len(segs) else (_WRAPPER,)
        if len(segs) == n:
            return (_CONTAINER,)
        return (_RECORD, segs[n], segs[n + 1:])


class _Op:
    """Error context of the operation being applied."""

    __slots__ = ("index", "op", "path")

    def __init__(self, op: PatchOp) -> None:
        self.index = op.index
        self.op = op.op
        self.path = op.path

    def err(self, msg_key: str, /, **params: Any) -> PatchError:
        return PatchError(msg_key, index=self.index, op=self.op, path=self.path, **params)

    def not_found(self, segs: Sequence[str]) -> PatchError:
        return self.err("patch.error.path_not_found", pointer=_ptr(segs))


def _array_index(ctx: _Op, arr_len: int, seg: str, ptr: Sequence[str], *, end: bool) -> int:
    """The array index ``seg`` names (RFC 6901): ``end`` allows ``len`` and ``-`` (add positions)."""
    if seg == "-":
        if not end:
            raise ctx.err("patch.error.dash", pointer=_ptr(ptr))
        return arr_len
    i = canonical_index(seg)
    if i is None:
        raise ctx.err("patch.error.bad_index", pointer=_ptr(ptr), segment=seg)
    if i > arr_len or (i == arr_len and not end):
        raise ctx.err("patch.error.index_range", pointer=_ptr(ptr), segment=seg, count=arr_len)
    return i


class _Box:
    """A mutable holder of a root value (a record value, the wrapper)."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def _v_get(ctx: _Op, root: Any, segs: Sequence[str], base: tuple) -> Any:
    cur = root
    for k, seg in enumerate(segs):
        if isinstance(cur, dict):
            if seg not in cur:
                raise ctx.not_found(base + tuple(segs[:k + 1]))
            cur = cur[seg]
        elif isinstance(cur, list):
            cur = cur[_array_index(ctx, len(cur), seg, base + tuple(segs[:k + 1]), end=False)]
        else:
            raise ctx.not_found(base + tuple(segs[:k + 1]))
    return cur


def _v_parent(ctx: _Op, root: Any, segs: Sequence[str], base: tuple) -> Any:
    """The container holding ``segs[-1]`` (``patch.error.not_container`` for a scalar)."""
    parent = _v_get(ctx, root, segs[:-1], base)
    if not isinstance(parent, (dict, list)):
        raise ctx.err("patch.error.not_container", pointer=_ptr(base + tuple(segs[:-1])))
    return parent


def _v_add(ctx: _Op, box: _Box, segs: Sequence[str], value: Any, base: tuple) -> None:
    if not segs:
        box.value = value
        return
    parent = _v_parent(ctx, box.value, segs, base)
    last = segs[-1]
    if isinstance(parent, dict):
        parent[last] = value                 # an existing member keeps its place (RFC 6902 §4.1: replaced)
    else:
        parent.insert(_array_index(ctx, len(parent), last, base + tuple(segs), end=True), value)


def _v_existing_parent(ctx: _Op, root: Any, segs: Sequence[str], base: tuple) -> Any:
    """The container holding the EXISTING node ``segs`` (a scalar parent: the node does not exist)."""
    parent = _v_get(ctx, root, segs[:-1], base)
    if not isinstance(parent, (dict, list)):
        raise ctx.not_found(base + tuple(segs))
    return parent


def _v_remove(ctx: _Op, box: _Box, segs: Sequence[str], base: tuple) -> Any:
    assert segs, "the root is removed by the caller"
    parent = _v_existing_parent(ctx, box.value, segs, base)
    last = segs[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise ctx.not_found(base + tuple(segs))
        return parent.pop(last)
    return parent.pop(_array_index(ctx, len(parent), last, base + tuple(segs), end=False))


def _v_replace(ctx: _Op, box: _Box, segs: Sequence[str], value: Any, base: tuple) -> None:
    if not segs:
        box.value = value
        return
    parent = _v_existing_parent(ctx, box.value, segs, base)
    last = segs[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise ctx.not_found(base + tuple(segs))
        parent[last] = value
    else:
        parent[_array_index(ctx, len(parent), last, base + tuple(segs), end=False)] = value


def _v_rename(ctx: _Op, box: _Box, parent_segs: Sequence[str], old: str, new: str, base: tuple) -> bool:
    """``move`` between two members of the same object: rename in place (module docstring, import 5). Returns
    False when the parent is not an object (the caller moves generically)."""
    parent = _v_get(ctx, box.value, parent_segs, base)
    if not isinstance(parent, dict):
        return False
    if old not in parent:
        raise ctx.not_found(base + tuple(parent_segs) + (old,))
    if new in parent:
        parent[new] = parent.pop(old)        # remove "from", then "add" replaces the existing member in place
        return True
    items = list(parent.items())
    parent.clear()
    for k, v in items:
        parent[new if k == old else k] = v
    return True


def _cost(nbytes: int) -> int:
    return max(256, int(nbytes) * LRU_FACTOR)


class _Order:
    """The mutable record order of a working copy (value documents): runs of consecutive rids ``(lo, n)`` in
    blocks of at most :data:`ORDER_RUNS` runs, a Fenwick tree over the block sizes. ``rid_at`` / ``insert`` /
    ``delete`` cost O(log blocks + runs per block) -- an immutable PieceTable rebuilds a chunk of up to 4096
    pieces per change, which made a patch with many structural operations ~10x slower. :meth:`table` gives
    the PieceTable at the end."""

    __slots__ = ("blo", "bn", "bsize", "count", "fen", "top")

    def __init__(self, runs: Iterable[tuple[int, int]]) -> None:
        lo: list[int] = []
        ns: list[int] = []
        for r, n in runs:
            if n:
                lo.append(int(r))
                ns.append(int(n))
        half = ORDER_RUNS // 2
        self.blo: list[list[int]] = [lo[i:i + half] for i in range(0, len(lo), half)] or [[]]
        self.bn: list[list[int]] = [ns[i:i + half] for i in range(0, len(ns), half)] or [[]]
        self.bsize = [sum(b) for b in self.bn]
        self.count = sum(self.bsize)
        self._rebuild()

    @classmethod
    def of(cls, table: PieceTable) -> _Order:
        return cls((r, n) for _p, r, n in table.runs())

    def _rebuild(self) -> None:
        n = len(self.bsize)
        fen = [0] * (n + 1)
        for i, size in enumerate(self.bsize, 1):
            fen[i] += size
            j = i + (i & -i)
            if j <= n:
                fen[j] += fen[i]
        self.fen = fen
        self.top = 1 << (n.bit_length() - 1) if n else 0

    def _grow(self, b: int, delta: int) -> None:
        self.bsize[b] += delta
        self.count += delta
        fen = self.fen
        i = b + 1
        n = len(fen) - 1
        while i <= n:
            fen[i] += delta
            i += i & -i

    def _locate(self, pos: int) -> tuple[int, int, int]:
        """``(block, run, offset in the run)`` of the record at ``0 <= pos < count``."""
        idx, rem, bit, fen = 0, pos, self.top, self.fen
        n = len(fen) - 1
        while bit:
            nxt = idx + bit
            if nxt <= n and fen[nxt] <= rem:
                idx = nxt
                rem -= fen[nxt]
            bit >>= 1
        ns = self.bn[idx]
        k = 0
        while rem >= ns[k]:
            rem -= ns[k]
            k += 1
        return idx, k, rem

    def rid_at(self, pos: int) -> int:
        b, k, off = self._locate(pos)
        return self.blo[b][k] + off

    def insert(self, pos: int, rid: int) -> None:
        """A new record ``rid`` at ``0 <= pos <= count``."""
        if pos >= self.count:
            b = len(self.bsize) - 1
            lo, ns = self.blo[b], self.bn[b]
            if ns and lo[-1] + ns[-1] == rid:
                ns[-1] += 1
            else:
                lo.append(rid)
                ns.append(1)
        else:
            b, k, off = self._locate(pos)
            lo, ns = self.blo[b], self.bn[b]
            if off == 0:
                if k > 0 and lo[k - 1] + ns[k - 1] == rid:
                    ns[k - 1] += 1
                elif lo[k] == rid + 1:
                    lo[k] = rid
                    ns[k] += 1
                else:
                    lo.insert(k, rid)
                    ns.insert(k, 1)
            else:
                r0, n0 = lo[k], ns[k]
                ns[k] = off
                lo[k + 1:k + 1] = [rid, r0 + off]
                ns[k + 1:k + 1] = [1, n0 - off]
        self._grow(b, 1)
        if len(lo) > ORDER_RUNS:
            self._split(b)

    def delete(self, pos: int) -> int:
        """Remove the record at ``0 <= pos < count``; returns its rid."""
        b, k, off = self._locate(pos)
        lo, ns = self.blo[b], self.bn[b]
        r0, n0 = lo[k], ns[k]
        rid = r0 + off
        if n0 == 1:
            del lo[k]
            del ns[k]
        elif off == 0:
            lo[k] = r0 + 1
            ns[k] = n0 - 1
        elif off == n0 - 1:
            ns[k] = n0 - 1
        else:
            ns[k] = off
            lo.insert(k + 1, rid + 1)
            ns.insert(k + 1, n0 - off - 1)
        self._grow(b, -1)
        if len(lo) > ORDER_RUNS:
            self._split(b)
        elif not lo and len(self.bsize) > 1:
            del self.blo[b], self.bn[b], self.bsize[b]
            self._rebuild()
        return rid

    def _split(self, b: int) -> None:
        lo, ns = self.blo[b], self.bn[b]
        h = len(lo) // 2
        self.blo[b:b + 1] = [lo[:h], lo[h:]]
        self.bn[b:b + 1] = [ns[:h], ns[h:]]
        self.bsize[b:b + 1] = [sum(ns[:h]), sum(ns[h:])]
        self._rebuild()

    def runs(self) -> Iterator[tuple[int, int]]:
        for lo, ns in zip(self.blo, self.bn):
            yield from zip(lo, ns)

    def table(self) -> PieceTable:
        lo, hi = array("Q"), array("Q")
        for r, n in self.runs():
            lo.append(r)
            hi.append(r + n)
        return PieceTable.from_pairs(lo, hi)


class _SpanRun:
    """One frozen run of a :class:`_SpanLog`: rid-sorted ``(rid, start, end)`` arrays in memory, or in the log's
    spill file at ``off`` with every :data:`SPAN_SPARSE`-th rid kept in memory (``sparse``)."""

    __slots__ = ("e", "n", "off", "r", "s", "sparse")

    def __init__(self, r: array, s: array, e: array) -> None:
        self.r: array | None = r
        self.s: array | None = s
        self.e: array | None = e
        self.n = len(r)
        self.off = -1
        self.sparse: array | None = None


class _SpanLog:
    """``rid -> (vstart, vend)`` of the last unit written for each record (:class:`_Applier`), compact for
    patches that change millions of records: the latest writes in a dict, older ones frozen into rid-sorted
    arrays (24 bytes per write instead of a ~220-byte dict entry) -- beyond :data:`SPAN_SPILL` entries those
    arrays go to a temporary file next to the layer (``spill_dir()``) with a sparse rid index in memory -- and
    one byte per record id telling whether the record has a live write (:meth:`pop` clears it: a deleted
    record). :meth:`close` deletes the temporary file."""

    __slots__ = ("f", "live", "mem", "path", "recent", "runs", "spill_dir")

    def __init__(self, spill_dir: Callable[[], Path] | None = None) -> None:
        self.live = bytearray()
        self.recent: dict[int, tuple[int, int]] = {}
        self.runs: list[_SpanRun] = []
        self.mem = 0                                  # entries of the runs held in memory
        self.spill_dir = spill_dir
        self.f: BinaryIO | None = None
        self.path: Path | None = None

    def put(self, rid: int, s: int, e: int) -> None:
        live = self.live
        if rid >= len(live):
            live.extend(bytes(max(rid + 1 - len(live), len(live) // 2 + 1024)))
        live[rid] = 1
        self.recent[rid] = (s, e)
        if len(self.recent) >= SPAN_FREEZE:
            self._freeze()

    def _freeze(self) -> None:
        items = sorted(self.recent.items())
        self.runs.append(_SpanRun(array("Q", [k for k, _v in items]), array("Q", [v[0] for _k, v in items]),
                                  array("Q", [v[1] for _k, v in items])))
        self.mem += len(items)
        self.recent = {}
        if self.mem >= SPAN_SPILL and self.spill_dir is not None:
            self._spill()

    def _spill(self) -> None:
        """The runs held in memory -> the spill file (rids, starts, ends of each run one after the other)."""
        if self.f is None:
            folder = self.spill_dir()   # type: ignore[misc]
            self.path = Path(folder) / f"patch-{os.getpid()}-{secrets.token_hex(6)}.spans.tmp"
            self.f = open_shared(self.path, "w+b")
        f = self.f
        for run in self.runs:
            if run.r is None:
                continue
            f.seek(0, os.SEEK_END)
            run.off = f.tell()
            for col in (run.r, run.s, run.e):
                f.write(_le_u64(col))
            run.sparse = run.r[::SPAN_SPARSE]
            run.r = run.s = run.e = None
        self.mem = 0

    def _read(self, off: int, n: int) -> array:
        assert self.f is not None
        out = array("Q")
        self.f.seek(off)
        out.frombytes(self.f.read(8 * n))
        if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
            out.byteswap()
        if len(out) != n:   # pragma: no cover - a truncated temporary file
            raise EditError("edits.error.internal", detail="patch span log: short read")
        return out

    def _run_get(self, run: _SpanRun, rid: int) -> tuple[int, int] | None:
        if run.r is not None:
            i = bisect.bisect_left(run.r, rid)
            if i < run.n and run.r[i] == rid:
                return run.s[i], run.e[i]       # type: ignore[index]
            return None
        sparse = run.sparse
        assert sparse is not None
        c = bisect.bisect_right(sparse, rid) - 1
        if c < 0:
            return None
        a = c * SPAN_SPARSE
        k = min(SPAN_SPARSE, run.n - a)
        rids = self._read(run.off + 8 * a, k)
        i = bisect.bisect_left(rids, rid)
        if i >= k or rids[i] != rid:
            return None
        j = a + i
        se = self._read(run.off + 8 * run.n + 8 * j, 1)[0], self._read(run.off + 16 * run.n + 8 * j, 1)[0]
        return se

    def get(self, rid: int) -> tuple[int, int] | None:
        live = self.live
        if rid >= len(live) or not live[rid]:
            return None
        hit = self.recent.get(rid)
        if hit is not None:
            return hit
        for run in reversed(self.runs):              # newest first
            hit = self._run_get(run, rid)
            if hit is not None:
                return hit
        return None   # pragma: no cover - a live rid always has a write

    def pop(self, rid: int) -> None:
        if rid < len(self.live):
            self.live[rid] = 0
        self.recent.pop(rid, None)

    def clear(self) -> None:
        self.close()
        self.live = bytearray()
        self.recent = {}
        self.runs = []
        self.mem = 0

    def close(self) -> None:
        """Delete the spill file (idempotent)."""
        f, self.f = self.f, None
        if f is not None:
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass
        p, self.path = self.path, None
        if p is not None:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("patch: temporary file %s not removed", p, exc_info=True)

    def live_rids(self) -> array:
        """The record ids with a live write, ascending."""
        return array("Q", compress(range(len(self.live)), self.live))

    def blocks(self) -> Iterator[tuple[array, array, array]]:
        """Every write of a live record as ``(rids, starts, ends)`` blocks, oldest first (a record written
        more than once appears more than once: its LAST entry is its span)."""
        if self.recent:
            self._freeze()
        live = self.live
        for run in self.runs:
            if run.r is not None:
                parts = [(run.r, run.s, run.e)]
            else:
                parts = []
                for a in range(0, run.n, _SPAN_BLOCK):
                    k = min(_SPAN_BLOCK, run.n - a)
                    parts.append((self._read(run.off + 8 * a, k), self._read(run.off + 8 * (run.n + a), k),
                                  self._read(run.off + 8 * (2 * run.n + a), k)))
            for r, s, e in parts:
                keep = bytes(map(live.__getitem__, r))
                if keep.count(1) == len(r):
                    yield r, s, e      # type: ignore[misc]
                elif keep.count(1):
                    yield (array("Q", compress(r, keep)), array("Q", compress(s, keep)),   # type: ignore[arg-type]
                           array("Q", compress(e, keep)))   # type: ignore[arg-type]


_SPAN_BLOCK = 1 << 16
_BIG_ENDIAN = sys.byteorder == "big"


def _le_u64(a: array) -> bytes:
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a = array("Q", a)
        a.byteswap()
    return a.tobytes()


class _Rec:
    """A parsed record of the working copy."""

    __slots__ = ("box", "cost", "dirty", "head", "last")

    def __init__(self, value: Any, *, dirty: bool, cost: int, head: bytes | None = None,
                 last: _Op | None = None) -> None:
        self.box = _Box(value)
        self.dirty = dirty
        self.cost = cost
        self.head = head                     # member records: the original "key": head (kept while the key is)
        self.last = last                     # the operation that changed it last (error context)


# ----------------------------------------------------------------------------------
# import: the working copy
# ----------------------------------------------------------------------------------
class _Applier:
    """Pass 2 of :func:`apply_patch`: the operations applied in order on a working copy of the session's state
    (module docstring, "Import")."""

    def __init__(self, session: Any, cancel: CancelToken | None) -> None:
        self.s = session
        self.doc = session.doc
        self.state: OverlayState = session.state
        self.view = self.doc.pinned(self.state)
        self.style = session.style
        self.settings = session.settings
        self.layout = _Layout(self.doc.kind, self.doc.record_path, self.doc.member_records)
        self.large = int(self.settings.large_node_bytes)
        self.budget = max(LRU_MIN, int(self.settings.memory_limit_bytes) // 4)
        self.cancel = cancel
        st = self.state
        self.order = _Order.of(st.pieces) if not self.layout.member else None   # value documents
        self.next_rid = st.next_rid
        self.new_lo = st.next_rid                    # rids from here on were created by this patch
        # member-record documents (records found by key; their order changes only by appending / deleting)
        self.kmap: dict[str, list[int]] = {}         # key -> rids with that key (the keys the pointers use)
        self.rkey: dict[int, str] = {}               # rid -> current key
        self.okey: dict[int, str] = {}               # rid -> key in the session's state
        self.deleted: set[int] = set()               # rids of the state that were deleted
        self.appended: dict[int, None] = {}          # new rids, in order
        self.all_deleted = False                     # the record container was replaced
        self.renamed: set[int] = set()               # rids whose key changed
        # content
        self.lru: OrderedDict[int, _Rec] = OrderedDict()
        self.lru_cost = 0
        self.written = _SpanLog(lambda: self._seg_dir())   # rid -> span of its last unit in the patch's segment
        self.shared: dict[int, tuple[int, int]] = {}     # new rid -> the content span it shares (a copy)
        self.pending: list[tuple[int, bytes]] = []       # units not appended yet
        self.pending_map: dict[int, bytes] = {}
        self.pending_bytes = 0
        self.store: Any = None
        self.seg: int | None = None
        self.appended_to = 0                         # virtual end of the last append
        self.synced_to = 0
        self.layer_path: Path | None = None
        self.flags = 0
        # wrapper
        self.w: _Box | None = None
        self.w_dirty = False
        self.hole: Any = None
        self.result_records = 0

    # -- segment / units ---------------------------------------------------------------------------
    def _seg_dir(self) -> Path:
        """The folder of the patch's segment (temporary files of this patch go there)."""
        self._seg()
        return Path(self.store.dir)

    def _seg(self) -> int:
        if self.seg is None:
            self.store = self.s.ensure_store()
            self.seg = self.store.new_segment()
        return self.seg

    def _huge(self, ctx: _Op | None, ptr: Sequence[str], size: int) -> PatchError:
        params = {"pointer": _ptr(ptr), "size": format_bytes(size), "limit": format_bytes(self.large)}
        if ctx is not None:
            return ctx.err("patch.error.huge_record", **params)
        return PatchError("patch.error.huge_unit", **params)

    def _queue(self, rid: int, unit: bytes, ctx: _Op | None, ptr: Sequence[str]) -> None:
        if len(unit) > self.large:
            raise self._huge(ctx, ptr, len(unit))
        self.pending.append((rid, unit))
        self.pending_map[rid] = unit
        self.pending_bytes += len(unit)
        if self.pending_bytes >= APPEND_BATCH:
            self._append_pending()

    def _check_pending(self) -> None:
        """``editops.check_unit`` of the queued units in ONE core Scanner pass (values as a ``MODE_STREAM``
        text joined by newlines, members as ``{`` + units joined by ``,`` + ``}``; every unit exactly one
        span), their flags OR'ed into the patch's flags; any difference -> ``check_unit`` one by one (which
        raises ``edits.error.internal`` for a bad unit: a bug)."""
        units = [u for _r, u in self.pending]
        if not units:
            return
        member = self.layout.member
        sc = _api().Scanner(MODE_CONTAINER if member else MODE_STREAM, [], 0)
        out = sc.feed(b"{" + b",".join(units) + b"}" if member else b"\n".join(units))
        out += sc.finish()
        if sc.error is None:
            lens = array("Q", map(len, units))
            n = len(lens)
            starts = array("Q", accumulate(map(add, lens, itertools.repeat(1)), initial=1 if member else 0))
            want = array("Q", bytes(16 * n))
            want[0::2] = starts[:n]
            want[1::2] = array("Q", map(add, starts[:n], lens))
            got = array("Q")
            got.frombytes(out)
            if got == want:
                self.flags |= int(sc.flags)
                return
        for u in units:
            self.flags |= editops.check_unit(u, member=member)

    def _append_pending(self) -> None:
        if not self.pending:
            return
        self._check_pending()
        seg = self._seg()
        vs, ve = self.store.append(b"".join(u for _r, u in self.pending), seg=seg)
        off = vs
        for rid, u in self.pending:
            self.written.put(rid, off, off + len(u))
            off += len(u)
        self.appended_to = ve
        self.pending.clear()
        self.pending_map.clear()
        self.pending_bytes = 0

    def drop(self) -> None:
        """Remove the segment / layer file of a patch that failed (best effort)."""
        store = self.store
        if store is None:
            return
        try:
            if self.layer_path is not None:
                store.drop_layer_file(self.layer_path)
            if self.seg is not None:
                store.drop_segment(self.seg)
        except (ValueError, JsonReshapeError, OSError):
            log.debug("dropping the segment of a failed patch failed", exc_info=True)

    # -- reading records -------------------------------------------------------------------------------
    def _read_span(self, s: int, e: int) -> bytes:
        if s >= EDIT_BASE:
            if self.seg is not None and split_edit_offset(s)[0] == self.seg:
                if e > self.synced_to:
                    self.store.sync([self.seg])
                    self.synced_to = self.appended_to
                return self.store.reader.read(s, e)
            return self.state.read(s, e)
        return self.doc._read(s, e)                 # the data file (read ahead on a forward walk)

    def _state_pos(self, rid: int) -> int:
        pos = self.state.pieces.pos_of(rid)
        if pos is None:   # pragma: no cover - only live records are reached
            raise EditError("edits.error.internal", detail=f"record id {rid} is not in the state")
        return pos

    def _record_bytes(self, ctx: _Op | None, rid: int, ptr: Sequence[str]) -> bytes:
        unit = self.pending_map.get(rid)
        if unit is not None:
            return unit
        # the newest content: this patch's writes, a shared copy, the state's layers, else the file's record
        span = self.written.get(rid) or self.shared.get(rid) or self.state.content(rid)
        s, e = span if span is not None else self.doc._base_span(rid)
        if e - s > self.large:
            raise self._huge(ctx, ptr, e - s)
        return self._read_span(s, e)

    def _parse(self, ctx: _Op | None, data: bytes, ptr: Sequence[str]) -> Any:
        def err(msg_key: str, /, **params: Any) -> PatchError:
            if ctx is not None:
                return ctx.err(msg_key, pointer=_ptr(ptr), **params)
            return PatchError(msg_key, index="-", op="-", path="", pointer=_ptr(ptr), **params)

        try:
            return _load_checked(data)
        except _DupKey as exc:
            raise err("patch.error.duplicate_keys", key=exc.key) from None
        except RecursionError:
            raise err("patch.error.unreadable", detail=t("patch.detail.too_deep")) from None
        except ValueError as exc:
            raise err("patch.error.unreadable", detail=str(exc)) from None

    def _rec(self, ctx: _Op | None, rid: int, ptr: Sequence[str]) -> _Rec:
        r = self.lru.get(rid)
        if r is not None:
            self.lru.move_to_end(rid)
            return r
        data = self._record_bytes(ctx, rid, ptr)
        head = None
        vb = data
        if self.layout.member:
            ml = editops.member_loc(data)
            head = data[:ml.start]
            vb = data[ml.start:]
            if rid not in self.rkey:          # first sight of this record: its key is the state's
                self.rkey[rid] = ml.key       # type: ignore[assignment]
                if rid < self.new_lo:
                    self.okey.setdefault(rid, ml.key)   # type: ignore[arg-type]
        r = _Rec(self._parse(ctx, vb, ptr), dirty=False, cost=_cost(len(data)), head=head)
        self._lru_put(rid, r)
        return r

    def _lru_put(self, rid: int, r: _Rec) -> None:
        old = self.lru.pop(rid, None)
        if old is not None:
            self.lru_cost -= old.cost
        self.lru[rid] = r
        self.lru_cost += r.cost

    def _evict(self) -> None:
        while self.lru_cost > self.budget and len(self.lru) > 1:
            rid, r = self.lru.popitem(last=False)
            self.lru_cost -= r.cost
            if r.dirty:
                self._write_rec(rid, r)

    def _dirty(self, rid: int, r: _Rec, size: int, ctx: _Op) -> None:
        r.dirty = True
        r.last = ctx
        if size:
            grow = size * LRU_FACTOR
            r.cost += grow
            if rid in self.lru:
                self.lru_cost += grow

    def _forget(self, rid: int) -> None:
        old = self.lru.pop(rid, None)
        if old is not None:
            self.lru_cost -= old.cost
        self.written.pop(rid)
        self.shared.pop(rid, None)
        self.renamed.discard(rid)

    # -- units -------------------------------------------------------------------------------------------
    def _key_ptr(self, rid: int) -> tuple:
        if self.layout.single:
            return ()
        if self.layout.member:
            return self.layout.cptr + (self.rkey.get(rid, "?"),)
        return self.layout.cptr + ("?",)

    def _orig_head(self, rid: int) -> bytes:
        """The original ``"key":`` head of a member record of the state (a small read)."""
        pos = self._state_pos(rid)
        s, _e = self.view.record_span(pos)
        vs, _ve = self.view.record_value_span(pos)
        return self._read_span(s, vs)

    def _unit(self, rid: int, r: _Rec) -> bytes:
        vb = codec.dumps(r.box.value)
        style = self.style
        # editops.format_record of a compact ":" / "," style re-emits codec.dumps' whitespace-free text byte for
        # byte (as in bulk): the Reformatter pass is skipped
        plain = style.unit is None and style.colon == b":" and style.comma == b","
        if not self.layout.member:
            # (a copy: an orjson result occupies ~4 KiB whatever its length, and units wait in a batch)
            return bytes(memoryview(vb)) if plain else editops.format_record(vb, style)
        key = self.rkey[rid]
        if rid < self.new_lo and key == self.okey.get(rid):
            # the key of the state: keep the record's own "key": head when it still names that key
            head = r.head if r.head is not None else self._orig_head(rid)
            if editops.member_loc(head + b"0").key == key:
                return head + vb if plain else editops.format_record(vb, style, member_head=head)
        return editops.encode_key(key) + style.colon + vb if plain else editops.format_record(vb, style, key=key)

    def _write_rec(self, rid: int, r: _Rec) -> None:
        self._queue(rid, self._unit(rid, r), r.last, self._key_ptr(rid))

    def _write_renamed(self, rid: int) -> None:
        """A record whose key changed but whose value did not: the key token replaced, every other byte kept."""
        data = self._record_bytes(None, rid, self._key_ptr(rid))
        ml = editops.member_loc(data)
        assert ml.mstart is not None and ml.kend is not None
        unit = data[:ml.mstart] + editops.encode_key(self.rkey[rid]) + data[ml.kend:]
        self._queue(rid, unit, None, self._key_ptr(rid))

    # -- records ---------------------------------------------------------------------------------------
    def _new_rid(self) -> int:
        rid = self.next_rid
        self.next_rid += 1
        return rid

    def resolve_keys(self, keys: Iterable[str], cancel: CancelToken | None) -> None:
        """The records of the keys the pointers use (member-record documents; the session's KeyIndex)."""
        keys = [k for k in keys if k not in self.kmap]
        if not keys:
            return
        ki = self.s.key_index
        st = self.state
        if ki is None:   # pragma: no cover - member-record sessions always have one
            raise EditError("edits.error.internal", detail="no key index")
        view = ki.wait(cancel)
        for i, key in enumerate(keys):
            if not i % _CHECK_EVERY:
                check(cancel)
            rids = [st.rid_at(p) for p in view.positions(key)]
            self.kmap[key] = rids
            for rid in rids:
                self.rkey.setdefault(rid, key)
                self.okey.setdefault(rid, key)

    def _lookup(self, ctx: _Op, key: str, ptr: Sequence[str]) -> int | None:
        rids = self.kmap.get(key)
        if rids is None:
            if self.all_deleted:
                rids = self.kmap[key] = []
            else:
                self.resolve_keys([key], self.cancel)
                rids = self.kmap[key]
        if len(rids) > 1:
            raise ctx.err("patch.error.duplicate_keys", pointer=_ptr(ptr), key=key)
        return rids[0] if rids else None

    def _record_of(self, ctx: _Op, head: str | None) -> tuple[int, tuple]:
        """``(rid, pointer prefix)`` of an existing record."""
        lay = self.layout
        if head is None:
            return self.order.rid_at(0), ()    # type: ignore[union-attr]
        ptr = lay.cptr + (head,)
        if lay.member:
            rid = self._lookup(ctx, head, ptr)
            if rid is None:
                raise ctx.not_found(ptr)
            return rid, ptr
        order = self.order
        assert order is not None
        i = _array_index(ctx, order.count, head, ptr, end=False)
        return order.rid_at(i), ptr

    def _set_value(self, ctx: _Op, rid: int, value: Any, cost: int) -> None:
        old = self.lru.get(rid)
        head = old.head if old is not None else None
        self._lru_put(rid, _Rec(value, dirty=True, cost=cost, head=head, last=ctx))
        self.shared.pop(rid, None)

    def _rl_add(self, ctx: _Op, head: str, value: Any, cost: int) -> None:
        lay = self.layout
        ptr = lay.cptr + (head,)
        if lay.member:
            rid = self._lookup(ctx, head, ptr)
            if rid is not None:
                self._set_value(ctx, rid, value, cost)      # add onto an existing member replaces it
                return
            rid = self._new_rid()
            self.appended[rid] = None
            self.kmap.setdefault(head, []).append(rid)
            self.rkey[rid] = head
        else:
            order = self.order
            assert order is not None
            i = _array_index(ctx, order.count, head, ptr, end=True)
            rid = self._new_rid()
            order.insert(i, rid)
        self._lru_put(rid, _Rec(value, dirty=True, cost=cost, last=ctx))

    def _md_delete(self, rid: int, key: str) -> None:
        self.kmap[key].remove(rid)
        if rid >= self.new_lo:
            self.appended.pop(rid, None)
        else:
            self.deleted.add(rid)
        self._forget(rid)

    def _rl_remove(self, ctx: _Op, head: str) -> None:
        """Delete a whole record."""
        lay = self.layout
        ptr = lay.cptr + (head,)
        if lay.member:
            rid = self._lookup(ctx, head, ptr)
            if rid is None:
                raise ctx.not_found(ptr)
            self._md_delete(rid, head)
            return
        order = self.order
        assert order is not None
        i = _array_index(ctx, order.count, head, ptr, end=False)
        self._forget(order.delete(i))

    def _md_rename(self, ctx: _Op, a: str, b: str) -> None:
        """``move`` of a whole record to another key (member-record documents, module docstring import 5)."""
        lay = self.layout
        pa, pb = lay.cptr + (a,), lay.cptr + (b,)
        rid_a = self._lookup(ctx, a, pa)
        if rid_a is None:
            raise ctx.not_found(pa)
        rid_b = self._lookup(ctx, b, pb)
        if rid_b is not None:
            r = self._rec(ctx, rid_a, pa)
            value, cost = r.box.value, r.cost
            self._md_delete(rid_a, a)
            self._set_value(ctx, rid_b, value, cost)
            return
        self.kmap[a].remove(rid_a)
        self.kmap.setdefault(b, []).append(rid_a)
        self.rkey[rid_a] = b
        self.renamed.add(rid_a)

    def _vd_move(self, ctx: _Op, a: str, b: str) -> None:
        ptr = self.layout.cptr
        order = self.order
        assert order is not None
        n = order.count
        i = _array_index(ctx, n, a, ptr + (a,), end=False)
        j = _array_index(ctx, n - 1, b, ptr + (b,), end=True)   # in the array without the moved record
        if i != j:
            order.insert(j, order.delete(i))

    def _vd_copy(self, ctx: _Op, a: str, b: str) -> None:
        ptr = self.layout.cptr
        order = self.order
        assert order is not None
        n = order.count
        i = _array_index(ctx, n, a, ptr + (a,), end=False)
        j = _array_index(ctx, n, b, ptr + (b,), end=True)
        src = order.rid_at(i)
        rid = self._new_rid()
        order.insert(j, rid)
        r = self.lru.get(src)
        if r is not None and r.dirty:
            self._lru_put(rid, _Rec(copy.deepcopy(r.box.value), dirty=True, cost=r.cost, last=ctx))
            return
        if src in self.pending_map:
            self._append_pending()
        span = self.written.get(src) or self.shared.get(src)
        if span is None:
            span = self.view.record_span(self._state_pos(src))
        self.shared[rid] = span                     # no new bytes: the copy shares the source's content

    # -- the wrapper (wrapped documents) -----------------------------------------------------------------
    def _wrapper(self, ctx: _Op) -> _Box:
        if self.w is not None:
            return self.w
        st, doc, lay = self.state, self.doc, self.layout
        if st.wrapper is not None and st.wrapper_hole is not None:
            ws, we = st.wrapper
            if we - ws > self.large:
                raise self._huge(ctx, (), we - ws)
            buf = st.read(ws, we)
        else:
            span = doc.container_span
            if span is None:   # pragma: no cover - an editable document has a closed container
                raise EditError("edits.error.partial")
            base = doc.pinned(None)
            root = base.root()
            cs, ce = span
            size = (cs - root.start) + len(lay.placeholder) + (root.end - ce)
            if size > self.large:
                raise self._huge(ctx, (), size)
            with base.open_data() as f:
                prefix = read_range(f, root.start, cs)
                suffix = read_range(f, ce, root.end)
            buf = prefix + lay.placeholder + suffix
        value = self._parse(ctx, buf, ())
        hole = self._hole_in(value)
        if hole is None or not isinstance(hole, (list, dict)) or hole:
            log.error("wrapper: the record container placeholder was not found at %r", lay.cpath)
            raise EditError("edits.error.internal", detail=t("edits.detail.shape"))
        self.hole = hole
        self.w = _Box(value)
        return self.w

    def _hole_in(self, value: Any) -> Any:
        cur = value
        for seg in self.layout.cpath:
            if isinstance(cur, dict) and isinstance(seg, str) and seg in cur:
                cur = cur[seg]
            elif isinstance(cur, list) and isinstance(seg, int) and 0 <= seg < len(cur):
                cur = cur[seg]
            else:
                return None
        return cur

    def _locked(self, ctx: _Op) -> PatchError:
        return ctx.err("patch.error.container_locked", container=_ptr(self.layout.cptr))

    def _w_shift_check(self, ctx: _Op, segs: tuple, *, adding: bool) -> None:
        """An add / remove in an ancestor ARRAY of the record container at an index that would move the
        container -> locked (§12.5)."""
        cpath = self.layout.cpath
        k = len(segs) - 1
        if k < 0 or k >= len(cpath) or not all(_seg_matches(segs[j], cpath[j]) for j in range(k)):
            return
        h = cpath[k]
        if not isinstance(h, int) or isinstance(h, bool):
            return
        parent = self._hole_parent(k)
        last = segs[-1]
        i = len(parent) if last == "-" else canonical_index(last)
        if i is None:
            return                                   # not an index: the value operation reports it
        if (adding and i <= h) or (not adding and i < h):
            raise self._locked(ctx)

    def _hole_parent(self, k: int) -> Any:
        assert self.w is not None
        cur = self.w.value
        for seg in self.layout.cpath[:k]:
            cur = cur[seg]
        return cur

    def _w_changed(self, ctx: _Op) -> None:
        self.w_dirty = True
        assert self.w is not None
        if self._hole_in(self.w.value) is not self.hole:   # pragma: no cover - the rules above prevent it
            raise self._locked(ctx)

    # -- the record container ----------------------------------------------------------------------------
    def _rids_in_order(self) -> Iterator[int]:
        if self.order is not None:
            for r, n in list(self.order.runs()):
                yield from range(r, r + n)
            return
        if not self.all_deleted:
            deleted = self.deleted
            for _p, r, n in self.state.pieces.runs():
                for rid in range(r, r + n):
                    if rid not in deleted:
                        yield rid
        yield from list(self.appended)

    def _key_of(self, rid: int) -> str:
        key = self.rkey.get(rid)
        if key is None:
            key = self.view.record_key(self._state_pos(rid))
            assert key is not None
            self.rkey[rid] = key
            self.okey.setdefault(rid, key)
        return key

    def _container_equals(self, ctx: _Op, value: Any) -> bool:
        lay = self.layout
        if lay.member:
            if not isinstance(value, dict):
                return False
            seen: dict[str, int] = {}
            for n, rid in enumerate(self._rids_in_order()):
                if not n % _CHECK_EVERY:
                    check(self.cancel)
                key = self._key_of(rid)
                if key not in value:
                    return False
                seen[key] = rid                       # dict semantics: the last record with a key counts
            if len(seen) != len(value):
                return False
            for n, (key, rid) in enumerate(seen.items()):
                if not n % _CHECK_EVERY:
                    check(self.cancel)
                if not json_equal(self._rec(ctx, rid, lay.cptr + (key,)).box.value, value[key]):
                    return False
                self._evict()
            return True
        if not isinstance(value, list) or self.order is None or len(value) != self.order.count:
            return False
        for n, rid in enumerate(self._rids_in_order()):
            if not n % _CHECK_EVERY:
                check(self.cancel)
            if not json_equal(self._rec(ctx, rid, lay.cptr + (str(n),)).box.value, value[n]):
                return False
            self._evict()
        return True

    def _materialize(self, ctx: _Op) -> Any:
        """The record container as a value (``copy`` from it), at most ``large_node_bytes``."""
        lay = self.layout
        out: Any = {} if lay.member else []
        total = 0
        for n, rid in enumerate(self._rids_in_order()):
            if not n % _CHECK_EVERY:
                check(self.cancel)
            key = self._key_of(rid) if lay.member else str(n)
            r = self._rec(ctx, rid, lay.cptr + (key,))
            total += r.cost // LRU_FACTOR
            if total > self.large:
                raise ctx.err("patch.error.too_large", pointer=_ptr(lay.cptr), size=format_bytes(total),
                              limit=format_bytes(self.large))
            v = copy.deepcopy(r.box.value)
            if lay.member:
                out[key] = v
            else:
                out.append(v)
            self._evict()
        return out

    def _container_set(self, ctx: _Op, value: Any) -> None:
        """``add`` / ``replace`` of the record container: every record replaced by the value's items."""
        lay = self.layout
        want = dict if lay.member else list
        if not isinstance(value, want):
            raise ctx.err("patch.error.container_type", pointer=_ptr(lay.cptr),
                          expected=t("edits.type.object" if lay.member else "edits.type.array"))
        self.lru.clear()
        self.lru_cost = 0
        self.written.clear()
        self.shared.clear()
        self.renamed.clear()
        self._check_pending()                       # (their flags count, as they always did)
        self.pending.clear()
        self.pending_map.clear()
        self.pending_bytes = 0
        items = list(value.items()) if lay.member else list(enumerate(value))
        lo = self.next_rid
        self.next_rid += len(items)
        if lay.member:
            self.all_deleted = True
            self.deleted.clear()
            self.appended = {}
            self.kmap = {}
            self.rkey = {}
        else:
            self.order = _Order([(lo, len(items))])
        for j, (key, v) in enumerate(items):
            rid = lo + j
            if lay.member:
                self.appended[rid] = None
                self.kmap[key] = [rid]
                self.rkey[rid] = key
            self._lru_put(rid, _Rec(v, dirty=True, cost=_cost(_value_size(v)), last=ctx))
            if not j % _CHECK_EVERY:
                check(self.cancel)
                self._evict()

    # -- ancestors of the container (wrapped documents) --------------------------------------------------
    def _ancestor_equals(self, ctx: _Op, segs: tuple, value: Any) -> bool:
        w = self._wrapper(ctx)
        node = _v_get(ctx, w.value, segs, ())
        cpath = self.layout.cpath

        def eq(node: Any, val: Any, depth: int) -> bool:
            if depth == len(cpath):
                return self._container_equals(ctx, val)
            nxt = cpath[depth]
            if isinstance(node, dict):
                if not isinstance(val, dict) or len(val) != len(node) or any(k not in val for k in node):
                    return False
                return all(eq(v, val[k], depth + 1) if k == nxt else json_equal(v, val[k])
                           for k, v in node.items())
            if not isinstance(val, list) or len(val) != len(node):
                return False
            return all(eq(v, val[i], depth + 1) if i == nxt else json_equal(v, val[i]) for i, v in enumerate(node))

        return eq(node, value, len(segs))

    def _materialize_ancestor(self, ctx: _Op, segs: tuple) -> Any:
        w = self._wrapper(ctx)
        node = _v_get(ctx, w.value, segs, ())
        records = self._materialize(ctx)
        hole = self.hole

        def build(v: Any) -> Any:
            if v is hole:
                return records
            if isinstance(v, dict):
                return {k: build(x) for k, x in v.items()}
            if isinstance(v, list):
                return [build(x) for x in v]
            return v

        return build(node)

    # -- generic operations on locations -------------------------------------------------------------------
    def _get(self, ctx: _Op, segs: tuple) -> Any:
        loc = self.layout.classify(segs)
        kind = loc[0]
        if kind == _RECORD:
            _k, head, rest = loc
            rid, base = self._record_of(ctx, head)
            return _v_get(ctx, self._rec(ctx, rid, base).box.value, rest, base)
        if kind == _WRAPPER:
            return _v_get(ctx, self._wrapper(ctx).value, segs, ())
        if kind == _CONTAINER:
            return self._materialize(ctx)
        return self._materialize_ancestor(ctx, segs)

    def _add(self, ctx: _Op, segs: tuple, value: Any, cost: int) -> None:
        loc = self.layout.classify(segs)
        kind = loc[0]
        if kind == _RECORD:
            _k, head, rest = loc
            if not rest and head is not None:
                self._rl_add(ctx, head, value, cost)
                return
            rid, base = self._record_of(ctx, head)
            r = self._rec(ctx, rid, base)
            _v_add(ctx, r.box, rest, value, base)
            self._dirty(rid, r, cost // LRU_FACTOR, ctx)
        elif kind == _WRAPPER:
            w = self._wrapper(ctx)
            self._w_shift_check(ctx, segs, adding=True)
            _v_add(ctx, w, segs, value, ())
            self._w_changed(ctx)
        elif kind == _CONTAINER:
            self._container_set(ctx, value)
        else:
            raise self._locked(ctx)

    def _remove(self, ctx: _Op, segs: tuple) -> Any:
        """Remove the node at ``segs``; returns its value (``_MISSING`` for whole records)."""
        loc = self.layout.classify(segs)
        kind = loc[0]
        if kind == _RECORD:
            _k, head, rest = loc
            if not rest:
                if head is None:
                    raise ctx.err("patch.error.root")
                self._rl_remove(ctx, head)
                return _MISSING
            rid, base = self._record_of(ctx, head)
            r = self._rec(ctx, rid, base)
            v = _v_remove(ctx, r.box, rest, base)
            self._dirty(rid, r, 0, ctx)
            return v
        if kind == _WRAPPER:
            w = self._wrapper(ctx)
            self._w_shift_check(ctx, segs, adding=False)
            v = _v_remove(ctx, w, segs, ())
            self._w_changed(ctx)
            return v
        if kind == _CONTAINER and not self.layout.wrapped:
            raise ctx.err("patch.error.root")
        raise self._locked(ctx)

    def _replace(self, ctx: _Op, segs: tuple, value: Any, cost: int) -> None:
        loc = self.layout.classify(segs)
        kind = loc[0]
        if kind == _RECORD:
            _k, head, rest = loc
            rid, base = self._record_of(ctx, head)
            if not rest and head is not None:
                self._set_value(ctx, rid, value, cost)
                return
            r = self._rec(ctx, rid, base)
            _v_replace(ctx, r.box, rest, value, base)
            self._dirty(rid, r, cost // LRU_FACTOR, ctx)
        elif kind == _WRAPPER:
            w = self._wrapper(ctx)
            _v_replace(ctx, w, segs, value, ())
            self._w_changed(ctx)
        elif kind == _CONTAINER:
            self._container_set(ctx, value)
        else:
            raise self._locked(ctx)

    def _take(self, ctx: _Op, segs: tuple) -> tuple[Any, int]:
        """``(value, cost)`` of the node at ``segs``, removed (the first half of ``move``)."""
        loc = self.layout.classify(segs)
        if loc[0] == _RECORD and not loc[2] and loc[1] is not None:
            rid, base = self._record_of(ctx, loc[1])
            r = self._rec(ctx, rid, base)
            value, cost = r.box.value, r.cost
            self._rl_remove(ctx, loc[1])
            return value, cost
        if loc[0] in (_CONTAINER, _ANCESTOR):
            if loc[0] == _CONTAINER and not self.layout.wrapped:
                raise ctx.err("patch.error.root")
            raise self._locked(ctx)
        value = self._remove(ctx, segs)
        return value, _cost(_value_size(value)) if isinstance(value, (dict, list)) else 256

    # -- the six operations --------------------------------------------------------------------------------
    def apply(self, op: PatchOp) -> None:
        ctx = _Op(op)
        name = op.op
        if name == "add":
            self._add(ctx, op.segments, op.value, _cost(op.size))
        elif name == "remove":
            self._remove(ctx, op.segments)
        elif name == "replace":
            self._replace(ctx, op.segments, op.value, _cost(op.size))
        elif name == "move":
            self._move(ctx, op)
        elif name == "copy":
            self._copy(ctx, op)
        else:
            self._test(ctx, op)
        self._evict()

    def _move(self, ctx: _Op, op: PatchOp) -> None:
        fs, ps = op.from_segments, op.segments
        assert fs is not None
        if fs == ps:
            self._get(ctx, fs)                       # "from" must exist; moving a node onto itself changes nothing
            return
        lay = self.layout
        fl, pl = lay.classify(fs), lay.classify(ps)
        if fl[0] == pl[0] == _RECORD:
            if fl[1] is not None and pl[1] is not None and not fl[2] and not pl[2]:   # whole record, record level
                if lay.member:
                    self._md_rename(ctx, fl[1], pl[1])
                else:
                    self._vd_move(ctx, fl[1], pl[1])
                return
            if fl[1] == pl[1] and fl[2] and pl[2] and fl[2][:-1] == pl[2][:-1]:   # members of one object
                rid, base = self._record_of(ctx, fl[1])
                r = self._rec(ctx, rid, base)
                if _v_rename(ctx, r.box, fl[2][:-1], fl[2][-1], pl[2][-1], base):
                    self._dirty(rid, r, 0, ctx)
                    return
        if fl[0] == pl[0] == _WRAPPER and fs[:-1] == ps[:-1]:
            w = self._wrapper(ctx)
            if _v_rename(ctx, w, fs[:-1], fs[-1], ps[-1], ()):
                self._w_changed(ctx)
                return
        value, cost = self._take(ctx, fs)
        self._add(ctx, ps, value, cost)

    def _copy(self, ctx: _Op, op: PatchOp) -> None:
        fs, ps = op.from_segments, op.segments
        assert fs is not None
        lay = self.layout
        fl, pl = lay.classify(fs), lay.classify(ps)
        if (fl[0] == pl[0] == _RECORD and fl[1] is not None and pl[1] is not None and not fl[2] and not pl[2]
                and not lay.member):
            self._vd_copy(ctx, fl[1], pl[1])
            return
        value = copy.deepcopy(self._get(ctx, fs))
        self._add(ctx, ps, value, _cost(_value_size(value)) if isinstance(value, (dict, list)) else 256)

    def _test(self, ctx: _Op, op: PatchOp) -> None:
        loc = self.layout.classify(op.segments)
        if loc[0] == _CONTAINER:
            ok = self._container_equals(ctx, op.value)
        elif loc[0] == _ANCESTOR:
            ok = self._ancestor_equals(ctx, op.segments, op.value)
        else:
            ok = json_equal(self._get(ctx, op.segments), op.value)
        if not ok:
            raise ctx.err("patch.error.test_failed")

    # -- the step ------------------------------------------------------------------------------------------
    def _member_table(self) -> PieceTable:
        st = self.state
        if self.all_deleted:
            table = PieceTable.identity(0)
        elif self.deleted:
            positions = array("Q", sorted(self._state_pos(r) for r in self.deleted))
            table = st.pieces.delete_positions(positions)
        else:
            table = st.pieces
        rids = list(self.appended)
        i = 0
        while i < len(rids):
            j = i + 1
            while j < len(rids) and rids[j] == rids[j - 1] + 1:
                j += 1
            table = table.insert(table.count, rids[i], j - i)
            i = j
        return table

    def _write_wrapper(self) -> tuple:
        st, lay = self.state, self.layout
        assert self.w is not None
        wb = editops.format_value(codec.dumps(self.w.value), 0, self.style)
        if len(wb) > self.large:
            raise self._huge(None, (), len(wb))
        flags = editops.check_unit(wb, member=False)
        try:
            loc: Any = editops.locate(wb, lay.cpath)
        except EditError:   # pragma: no cover - the hole is tracked through every operation
            loc = None
        if loc is None or wb[loc.start:loc.end] != lay.placeholder:   # pragma: no cover
            log.error("patched wrapper: the record container placeholder is missing")
            raise EditError("edits.error.internal", detail=t("edits.detail.shape"))
        self.flags |= flags
        seg = self._seg()
        vs, ve = self.store.append(wb, seg=seg)
        self.appended_to = ve
        return (st.wrapper, (vs, ve), st.wrapper_hole, (vs + loc.start, vs + loc.end))

    def finish(self, nops: int) -> ChangeSet:
        st = self.state
        lay = self.layout
        final = self._member_table() if lay.member else self.order.table()   # type: ignore[union-attr]
        table_changed = final is not st.pieces and final != st.pieces
        pos_of = final.pos_of
        for n, (rid, r) in enumerate(list(self.lru.items())):
            if not n % _CHECK_EVERY:
                check(self.cancel)
            if r.dirty and pos_of(rid) is not None:
                self._write_rec(rid, r)
        for rid in sorted(self.renamed):
            r = self.lru.get(rid)
            if pos_of(rid) is None or (r is not None and r.dirty):
                continue
            if rid < self.new_lo and self.rkey.get(rid) == self.okey.get(rid):
                continue                                         # renamed back: nothing changed
            self._write_renamed(rid)
        self._append_pending()
        self.lru.clear()                   # every change is in the segment now: the cache is freed before the
        self.lru_cost = 0                  # layer is built (a patch may change millions of records)
        new_lo = self.new_lo

        def kept(r: array, *cols: array) -> tuple[array, ...]:
            """Entries (rid-sorted) of the records in the final table. The state's own table (no record added,
            deleted or moved): every record that existed is in it and no new one."""
            if not table_changed:
                k = bisect.bisect_left(r, new_lo)
                return (r, *cols) if k == len(r) else (r[:k], *(c[:k] for c in cols))
            keep = final._pos_of_array(r)[1]
            if keep.count(1) == len(r):
                return (r, *cols)
            return tuple(array("Q", compress(c, keep)) for c in (r, *cols))

        # the changed records: copies sharing a span, then every write (a record's LAST write is its span);
        # only records of the final table. Arrays, not dicts: a patch may change millions of records.
        shared = {rid: span for rid, span in self.shared.items() if pos_of(rid) is not None}
        alive = kept(self.written.live_rids())[0]
        n_changes = len(alive) + sum(1 for rid in shared if self.written.get(rid) is None)
        n_orig = bisect.bisect_left(alive, new_lo)            # (copies sharing a span are new records)
        records = (sorted(pos_of(rid) for rid in alive[:n_orig])   # type: ignore[type-var]
                   if n_orig <= RECORDS_MAX else None)
        any_written = len(alive) > 0
        del alive
        wrapper = self._write_wrapper() if self.w_dirty else None
        self.result_records = n_changes
        if not n_changes and not table_changed and wrapper is None:
            self.drop()
            return ChangeSet(st, "patch", "", frozenset(), None)
        store = self.store if self.store is not None else self.s.ensure_store()
        self.store = store
        push_layer = None
        content: list = []
        if n_changes <= RECORDS_MAX:
            changes = dict(shared)
            for blk in self.written.blocks():
                check(self.cancel)
                r, s, e = kept(*blk)
                for rid, a, b in zip(r, s, e):
                    changes[rid] = (a, b)
            top = st.top
            content = [(rid, top.get(rid), span) for rid, span in sorted(changes.items())]
        else:
            segs = {split_edit_offset(s)[0] for s, _e in shared.values() if s >= EDIT_BASE}
            if self.seg is not None and any_written:
                segs.add(self.seg)
            blocks: list[tuple[array, array, array]] = []
            if shared:
                srids = sorted(shared)
                blocks.append((array("Q", srids), array("Q", (shared[r][0] for r in srids)),
                               array("Q", (shared[r][1] for r in srids))))

            def written_blocks() -> Iterator[tuple[array, ...]]:
                yield from blocks
                for blk in self.written.blocks():
                    r, s, e = kept(*blk)
                    if len(r):
                        yield r, s, e

            self.layer_path = store.new_layer_path()
            n = write_array_layer(self.layer_path, EntryBlocks(written_blocks()), flags=self.flags,
                                  segments=sorted(segs), work_dir=None, cancel=self.cancel)
            if n != n_changes:   # pragma: no cover - would be a bug
                raise RuntimeError(f"patch: {n} layer entries for {n_changes} changed records")
            push_layer = self.layer_path.name
        keys: list | None = None
        if lay.member and not self.all_deleted:
            keys = [(rid, self.okey.get(rid), None) for rid in sorted(self.deleted)]
            for rid in sorted(self.renamed):
                if rid < self.new_lo and pos_of(rid) is not None and self.rkey.get(rid) != self.okey.get(rid):
                    keys.append((rid, self.okey.get(rid), self.rkey[rid]))
            for rid in self.appended:
                keys.append((rid, None, self.rkey[rid]))
        new_flags = st.flags | self.flags
        step = Step(kind="patch", label_key="edits.step.patch", label_params={"count": nops}, content=content,
                    struct=[("pieces", st.pieces, final)] if table_changed else None, push_layer=push_layer,
                    segments=[self.seg] if self.seg is not None else [], wrapper=wrapper,
                    flags=(st.flags, new_flags) if new_flags != st.flags else None, records=records, keys=keys,
                    select=None, next_rid=self.next_rid)
        return self.s.commit_step(step, kind="patch")


# ----------------------------------------------------------------------------------
# import: the entry point
# ----------------------------------------------------------------------------------
def _iter_ops(src: _Source, cancel: CancelToken | None, on_bytes: Callable[[int], None] | None) -> Iterator[PatchOp]:
    if src.is_list:
        assert src.ops is not None
        for i, obj in enumerate(src.ops):
            if not i % _CHECK_EVERY:
                check(cancel)
            op = make_op(i, obj, _value_size(obj))
            if on_bytes is not None:
                on_bytes(1)
            yield op
        return
    for index, text in _scan_ops(_utf8_chunks(_counted(src.raw_chunks(), on_bytes)), cancel):
        yield _parse_op(index, text)


def apply_patch(session: Any, source: Any, *, progress: Progress | None = None,
                cancel: CancelToken | None = None, stats: dict | None = None) -> ChangeSet:
    """Apply the JSON Patch ``source`` to the edit session as ONE step (kind ``"patch"``; module docstring,
    "Import"); every operation sees the result of the previous ones. Any failure (an invalid patch, a failed
    ``test``, a path that does not exist ...) raises and changes nothing. ``progress``: bytes of the patch
    read in pass 1 and applied in pass 2 (operations for a list source). ``stats`` (a dict, optional) receives
    ``ops`` (operations), ``records`` (records whose content was written or shared: changed and new ones) and
    ``changed`` (whether a step was committed)."""
    src = _Source(source)
    with session.lock:
        if session.closed:
            raise EditError("edits.error.closed")
        doc = session.doc
        layout = _Layout(doc.kind, doc.record_path, doc.member_records)
        size = len(src.ops) if src.ops is not None else src.size()
        on_bytes = None
        if progress is not None:
            if size is not None:
                progress.set_total(2 * size)
            progress.set_message(t("patch.progress.reading"))
            on_bytes = progress.advance
        keep = src.is_list or not src.rereadable or (size is not None and size <= KEEP_OPS_BYTES)
        kept: list[PatchOp] | None = [] if keep else None
        keys: dict[str, None] = {}
        nops = 0
        for op in _iter_ops(src, cancel, on_bytes):
            nops += 1
            if kept is not None:
                kept.append(op)
            if layout.member:
                for segs in (op.segments, op.from_segments):
                    if segs is not None:
                        loc = layout.classify(segs)
                        if loc[0] == _RECORD and loc[1] is not None:
                            keys[loc[1]] = None
        ap = _Applier(session, cancel)
        try:
            if keys:
                if progress is not None:
                    progress.set_message(t("patch.progress.keys"))
                ap.resolve_keys(keys, cancel)
            if progress is not None:
                progress.set_message(t("patch.progress.applying"))
            ops: Iterable[PatchOp] = kept if kept is not None else _iter_ops(src, cancel, None)
            for n, op in enumerate(ops):
                if not n % _CHECK_EVERY:
                    check(cancel)
                ap.apply(op)
                if progress is not None and size:
                    progress.update(size + (n + 1) * size // max(1, nops))   # pass 2: the same units
            check(cancel)
            if progress is not None:
                progress.set_message(t("patch.progress.writing"))
            cs = ap.finish(nops)
        except BaseException:
            ap.drop()
            raise
        finally:
            ap.written.close()
        if progress is not None:
            progress.finish()
        if stats is not None:
            stats.update(ops=nops, records=ap.result_records, changed=cs.state is not ap.state)
        log.info("JSON Patch applied to %s: %d operations, %s", doc.source_path.name, nops,
                 "no change" if cs.state is ap.state else f"gen {cs.gen}")
        return cs


# ----------------------------------------------------------------------------------
# export: patch files, Fenwick trees, the maximum-weight increasing subsequence
# ----------------------------------------------------------------------------------
def _compact(raw: bytes) -> bytes:
    """``raw`` JSON text without whitespace outside strings (every token kept byte for byte)."""
    fmt = _api().Reformatter(None)
    if len(raw) <= READ_CHUNK:
        return fmt.feed(raw) + fmt.finish()
    view = memoryview(raw)
    parts = [fmt.feed(view[i:i + READ_CHUNK]) for i in range(0, len(raw), READ_CHUNK)]
    parts.append(fmt.finish())
    return b"".join(parts)


class _PatchWriter:
    """Writes operations one per line inside ``[`` ... ``]``."""

    def __init__(self, f: BinaryIO, cancel: CancelToken | None) -> None:
        self.f = f
        self.count = 0
        self.cancel = cancel

    def op(self, op: str, path: Sequence[str], *, value: Any = NO_VALUE, raw: bytes | None = None,
           from_: Sequence[str] | None = None) -> None:
        self.op_at(op, _ptr(path), value=value, raw=raw, from_=None if from_ is None else _ptr(from_))

    def op_at(self, op: str, ptr: str, *, value: Any = NO_VALUE, raw: bytes | None = None,
              from_: str | None = None) -> None:
        """:meth:`op` with the pointers as texts (``_ptr`` of the segments)."""
        parts = [b"[\n" if self.count == 0 else b",\n", b'{"op": "', op.encode("ascii"), b'"']
        if from_ is not None:
            parts += [b', "from": ', codec.dumps(from_)]
        parts += [b', "path": ', codec.dumps(ptr)]
        if raw is not None:
            parts += [b', "value": ', raw]
        elif value is not NO_VALUE:
            parts += [b', "value": ', codec.dumps(value)]
        parts.append(b"}")
        self.f.write(b"".join(parts))
        self.count += 1
        if not self.count % _CHECK_EVERY:
            check(self.cancel)

    def write_ops(self, data: bytes, n: int) -> None:
        """``n`` operations as another writer wrote them after an earlier one (each starts with ``,\\n``: a
        writer whose ``count`` started at 1, see :func:`_pool_job`)."""
        if not n:
            return
        if self.count == 0:
            self.f.write(b"[\n")
            self.f.write(memoryview(data)[2:])
        else:
            self.f.write(data)
        before = self.count
        self.count += n
        if before // _CHECK_EVERY != self.count // _CHECK_EVERY:
            check(self.cancel)

    def close(self) -> None:
        self.f.write(b"\n]\n" if self.count else b"[]\n")


class _Fenwick:
    """Counts over ``n`` slots: ``add(i)``, ``prefix(i)`` = marked slots ``< i``."""

    __slots__ = ("n", "tree")

    def __init__(self, n: int) -> None:
        self.n = n
        self.tree = array("q", bytes(8 * (n + 1)))

    def add(self, i: int) -> None:
        i += 1
        tree, n = self.tree, self.n
        while i <= n:
            tree[i] += 1
            i += i & -i

    def prefix(self, i: int) -> int:
        tree = self.tree
        s = 0
        while i > 0:
            s += tree[i]
            i -= i & -i
        return s


def _mwis(lo: array, weight: array) -> bytearray:
    """Keep-mask of the maximum-weight subsequence of runs whose ``lo`` values increase (weighted LIS,
    O(R log R) with a prefix-max Fenwick tree over the ranks of ``lo``)."""
    r = len(lo)
    keep = bytearray(b"\x01") * r
    if r <= 1 or all(lo[i] < lo[i + 1] for i in range(r - 1)):
        return keep
    order = sorted(range(r), key=lo.__getitem__)
    rank = array("q", bytes(8 * r))
    for k, i in enumerate(order):
        rank[i] = k
    best_w = array("q", bytes(8 * (r + 1)))
    best_i = array("q", [-1]) * (r + 1)
    dp = array("q", bytes(8 * r))
    parent = array("q", [-1]) * r
    for i in range(r):
        q = rank[i]
        bw, bi = 0, -1
        while q > 0:
            if best_w[q] > bw:
                bw, bi = best_w[q], best_i[q]
            q -= q & -q
        dp[i] = bw + weight[i]
        parent[i] = bi
        q = rank[i] + 1
        while q <= r:
            if dp[i] > best_w[q]:
                best_w[q] = dp[i]
                best_i[q] = i
            q += q & -q
    keep = bytearray(r)
    i = max(range(r), key=dp.__getitem__)
    while i >= 0:
        keep[i] = 1
        i = parent[i]
    return keep


def _ascending(a: Sequence[int]) -> bool:
    """Strictly ascending (C-level iteration)."""
    return all(map(operator.lt, a, itertools.islice(a, 1, None)))


def _merge_sorted(lo: array, hi: array, singles: list[int]) -> tuple[array, array]:
    """Sorted disjoint intervals ``[lo, hi)`` plus the single ids ``singles`` (sorted), as sorted intervals."""
    out_lo, out_hi = array("Q"), array("Q")
    i = 0
    n = len(lo)
    for b in singles:
        j = bisect.bisect_left(lo, b, i)
        out_lo.extend(lo[i:j])
        out_hi.extend(hi[i:j])
        out_lo.append(b)
        out_hi.append(b + 1)
        i = j
    out_lo.extend(lo[i:n])
    out_hi.extend(hi[i:n])
    return out_lo, out_hi


def _join_touching(lo: array, hi: array) -> tuple[array, array]:
    """Sorted disjoint intervals with touching neighbours (``hi[i] == lo[i + 1]``) joined."""
    n = len(lo)
    if n <= 1:
        return array("Q", lo), array("Q", hi)
    cuts = list(itertools.compress(range(1, n), map(operator.ne, itertools.islice(lo, 1, None), hi)))
    starts = [0, *cuts]
    ends = [*cuts, n]
    return array("Q", (lo[a] for a in starts)), array("Q", (hi[b - 1] for b in ends))


def _same_scalar(a: Any, b: Any) -> bool:
    """Equal JSON scalars by their exact text (``1.0`` != ``1``; ``-0`` != ``0``)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return False
    try:
        return codec.number_text(a) == codec.number_text(b)
    except TypeError:   # pragma: no cover - not a JSON value
        return False


def _esc(key: str) -> str:
    """One JSON Pointer segment (RFC 6901: ``~`` -> ``~0``, ``/`` -> ``~1``; as ``paths.format_pointer``)."""
    return key.replace("~", "~0").replace("/", "~1")


def _diff(w: _PatchWriter, old: Any, new: Any, prefix: tuple) -> None:
    """Operations turning ``old`` into ``new`` at ``prefix`` (module docstring, export (5))."""
    _diff_at(w, old, new, _ptr(prefix))


def _same_text(x: Any, y: Any) -> bool:
    """Whether two parsed values have the same JSON text (``codec.dumps``: exact number tokens, strings,
    member order). Values of different Python types never do (``int`` / ``float`` / ``JsonFloat`` /
    ``JsonInt`` / ``bool`` follow the token's form); plain ``str`` / ``int`` / ``bool`` / ``None`` by value;
    a plain ``float`` by value and the sign of a zero (its text is orjson's, unique per value); everything
    else (containers, ``JsonFloat`` / ``JsonInt``) by the texts."""
    tx = type(x)
    if tx is not type(y):
        return False
    if tx is str or tx is int or tx is bool or x is None:
        return x == y
    if tx is float:
        return x == y and (x != 0.0 or math.copysign(1.0, x) == math.copysign(1.0, y))
    return codec.dumps(x) == codec.dumps(y)


def _diff_at(w: _PatchWriter, old: Any, new: Any, base: str) -> None:
    """:func:`_diff` at the pointer text ``base``. Two (sub)values with the same JSON text produce no
    operation (same members in the same order, same elements, the same exact number tokens and strings), so
    they are skipped at once (:func:`_same_text`); two scalars with different texts are exactly the pairs
    ``_same_scalar`` calls different (exact token text; ``true`` != ``1``; ``-0.0`` != ``0.0``). Parsed values
    hold only exact dict / list / str / number / bool / None objects (codec)."""
    same = _same_text
    stack: list[tuple[Any, Any, str]] = [(old, new, base)]
    while stack:
        a, b, p = stack.pop()
        ta = type(a)
        tb = type(b)
        if ta is dict and tb is dict:
            for k in a:
                if k not in b:
                    w.op_at("remove", p + "/" + _esc(k))
            for k, v in b.items():
                if k not in a:
                    w.op_at("add", p + "/" + _esc(k), value=v)
            kids = [(a[k], b[k], p + "/" + _esc(k)) for k in a if k in b and not same(a[k], b[k])]
            if kids:
                stack.extend(reversed(kids))
        elif ta is list and tb is list and len(a) == len(b):
            kids = [(a[i], b[i], f"{p}/{i}") for i in range(len(a)) if not same(a[i], b[i])]
            if kids:
                stack.extend(reversed(kids))
        elif not same(a, b):        # (a pair taken from ``kids`` is known to differ; the root is checked)
            w.op_at("replace", p, value=b)


def _diff_wrapper(w: _PatchWriter, old: Any, new: Any, cpath: tuple) -> None:
    """The wrapper diff: along the record path, objects member by member, arrays per index before the
    container and as a tail after it; the container itself is never touched (module docstring, export (6))."""
    a, b, p = old, new, ()
    for nxt in cpath:
        if isinstance(a, dict) and isinstance(b, dict) and isinstance(nxt, str) and nxt in a and nxt in b:
            for k in a:
                if k not in b:
                    w.op("remove", p + (k,))
            for k, v in b.items():
                if k not in a:
                    w.op("add", p + (k,), value=v)
            for k in a:
                if k in b and k != nxt:
                    _diff(w, a[k], b[k], p + (k,))
            a, b, p = a[nxt], b[nxt], p + (nxt,)
        elif (isinstance(a, list) and isinstance(b, list) and isinstance(nxt, int) and nxt < len(a)
              and nxt < len(b)):
            h = nxt
            for i in range(h):
                _diff(w, a[i], b[i], p + (str(i),))
            ta, tb = len(a) - h - 1, len(b) - h - 1
            m = min(ta, tb)
            for i in range(m):
                _diff(w, a[h + 1 + i], b[h + 1 + i], p + (str(h + 1 + i),))
            for i in range(len(a) - 1, h + m, -1):
                w.op("remove", p + (str(i),))
            for i in range(m, tb):
                w.op("add", p + (str(h + 1 + i),), value=b[h + 1 + i])
            a, b, p = a[h], b[h], p + (str(h),)
        else:   # pragma: no cover - the session never changes the record path
            _diff(w, a, b, p)
            return


# ----------------------------------------------------------------------------------
# export: the diffs of many changed records in worker processes (like bulk.preview)
# ----------------------------------------------------------------------------------
POOL_MIN_BYTES = 64 << 20          # automatic workers: once this many changed record bytes were diffed here
POOL_WAIT = 0.05                   # seconds between cancellation checks while a result is awaited


def _values_ops(w: _PatchWriter, cbase: str, positions: Sequence[int], olds: Sequence[bytes | None],
                news: Sequence[bytes]) -> None:
    """Step (5) of :meth:`_Exporter._values` for a chunk of changed original value records: the diff at the
    final position against the original bytes ``olds[k]``, without them (a huge original, ``diff=False``)
    the whole record as ``replace``."""
    loads = codec.loads
    for pos, new_raw, old in zip(positions, news, olds):
        ptr = f"{cbase}/{pos}"
        if old is not None:
            _diff_at(w, loads(old), loads(new_raw), ptr)
        else:
            w.op_at("replace", ptr, raw=_compact(new_raw))


def _members_ops(w: _PatchWriter, cptr: tuple, units: Sequence[bytes], braws: Sequence[bytes | None],
                 okeys: Sequence[str | None]) -> list[tuple[str, str]]:
    """Step (2) of :meth:`_Exporter._members` for a chunk of changed original member records: the value diff at
    the ORIGINAL key (taken from the original bytes ``braws[k]``), without them ``add`` of the whole value at
    ``okeys[k]`` (``None``: the current key); returns the renames ``(original key, key)`` in order."""
    from .reader import split_member

    renames: list[tuple[str, str]] = []
    for unit, braw, okey in zip(units, braws, okeys):
        key, vo = split_member(unit)
        if braw is not None:
            orig_key, bvo = split_member(braw)
            _diff(w, codec.loads(braw[bvo:]), codec.loads(unit[vo:]), cptr + (orig_key,))
        else:
            orig_key = key if okey is None else okey
            w.op("add", cptr + (orig_key,), raw=_compact(unit[vo:]))
        if orig_key != key:
            renames.append((orig_key, key))
    return renames


_JOBS: dict[str, Callable[..., Any]] = {"values": _values_ops, "members": _members_ops}
_PACKED: dict[str, tuple[int, ...]] = {"values": (2, 3), "members": (1, 2)}   # the arguments that list bytes


def _pack(items: Sequence[bytes | None]) -> tuple[bytes, array, bytes | None]:
    """Byte strings (``None`` allowed) as one blob, their end offsets and a presence mask (``None``: all
    present): one object to pickle instead of thousands."""
    mask = None
    if None in items:
        mask = bytes([x is not None for x in items])
        items = [b"" if x is None else x for x in items]
    return b"".join(items), array("Q", accumulate(map(len, items))), mask


def _unpack(packed: tuple[bytes, array, bytes | None]) -> list[Any]:
    """:func:`_pack` reversed."""
    blob, ends, mask = packed
    out: list[Any] = list(map(blob.__getitem__, map(slice, itertools.chain((0,), ends), ends)))
    if mask is not None:
        out = [x if m else None for x, m in zip(out, mask)]
    return out


def _pack_args(kind: str, args: tuple) -> tuple:
    idx = _PACKED[kind]
    return tuple(_pack(a) if i in idx else a for i, a in enumerate(args))


def _unpack_args(kind: str, args: tuple) -> tuple:
    idx = _PACKED[kind]
    return tuple(_unpack(a) if i in idx else a for i, a in enumerate(args))


def _backend_name(mod: ModuleType | None) -> str | None:
    if mod is None:
        return None
    return "rust" if mod.__name__.endswith("_rscore") else "python"


def _pool_init(backend: str | None, lang: str) -> None:
    """Worker process start: the caller's core backend (:func:`_compact`) and language. Automatic garbage
    collection is off: parsed records are acyclic and freed by reference counting."""
    import gc

    from . import i18n

    gc.disable()
    i18n.set_language(lang)
    if backend:
        set_core_backend(backend)


def _pool_job(kind: str, packed: tuple) -> tuple:
    """One chunk in a worker (``packed``: :func:`_pack_args`): ``("ok", operations, count, result)`` -- the
    operations as a :class:`_PatchWriter` writes them after an earlier one (each starts with ``,\\n``) -- or
    ``("redo",)`` when anything failed (the caller then does the chunk itself and so raises its error as
    without workers)."""
    import io

    buf = io.BytesIO()
    w = _PatchWriter(buf, None)
    w.count = 1
    try:
        extra = _JOBS[kind](w, *_unpack_args(kind, packed))
    except Exception:   # noqa: BLE001 - redone (and raised) by the caller
        return ("redo",)
    return ("ok", buf.getvalue(), w.count - 1, extra)


def _executor(workers: int) -> Any:
    """A pool of ``workers`` spawned processes for :func:`_pool_job`."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    from . import i18n

    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                               initializer=_pool_init, initargs=(_backend_name(_core_override), i18n.get_language()))


class _DiffPool:
    """The chunks of changed records of an export, diffed and written in order (``run(kind, args, ...)`` =
    ``_JOBS[kind](writer, *args)``): in this thread, or in worker processes -- ``workers`` > 1: from the first
    chunk; ``None`` (automatic): once :data:`POOL_MIN_BYTES` of changed records were diffed here, when
    ``bulk._spawn_safe()``, ``bulk.auto_workers`` of them. The caller reads the records; the results are
    written in submission order (at most ``2 * workers`` chunks in flight), so the patch is byte-identical
    either way. A chunk a worker could not do is done here (its error is raised as without workers);
    ``BrokenProcessPool``: the chunks in flight and the rest are done here."""

    def __init__(self, w: _PatchWriter, workers: int | None, settings: Any, cancel: CancelToken | None) -> None:
        self.w = w
        self.want = workers
        self.settings = settings
        self.cancel = cancel
        self.done = 0                      # changed record bytes handed out so far
        self.ex: Any = None
        self.off = workers is not None and workers <= 1
        self.used = 1                      # worker processes asked for (1: none)
        self.inflight = 2
        self.waiting: Any = None

    def _start(self) -> None:
        want = self.want
        if want is None:
            if self.done < POOL_MIN_BYTES:
                return
            from . import bulk

            want = bulk.auto_workers(self.done, self.settings) if bulk._spawn_safe() else 1
        if want <= 1:
            self.off = True
            return
        from collections import deque

        try:
            self.ex = _executor(want)
        except (OSError, ValueError, RuntimeError) as exc:   # pragma: no cover - no processes here
            log.warning("JSON Patch export: no worker processes (%s); the diffs run in one thread", exc)
            self.off = True
            return
        self.waiting = deque()
        self.used = want
        self.inflight = 2 * want

    def run(self, kind: str, args: tuple, nbytes: int, sink: Callable[[Any], Any] | None = None) -> None:
        """One chunk of ``nbytes`` record bytes; ``sink`` receives the job's result (in chunk order)."""
        from concurrent.futures.process import BrokenProcessPool

        if self.ex is None and not self.off:
            self._start()
        self.done += nbytes
        if self.ex is not None and not self.off:
            packed = _pack_args(kind, args)
            del args
            try:
                fut = self.ex.submit(_pool_job, kind, packed)
            except BrokenProcessPool as exc:
                self._broken(exc)
                args = _unpack_args(kind, packed)
            else:
                self.waiting.append((fut, kind, packed, sink))
                while len(self.waiting) >= self.inflight:
                    self._take()
                return
        self.finish()                       # chunks still in flight of workers that broke
        self._local(kind, args, sink)

    def _broken(self, exc: BaseException) -> None:
        if not self.off:
            log.warning("JSON Patch export: the worker processes failed (%s); the rest runs in one thread", exc)
        self.off = True
        self.used = 1

    def _local(self, kind: str, args: tuple, sink: Callable[[Any], Any] | None) -> None:
        extra = _JOBS[kind](self.w, *args)
        if sink is not None:
            sink(extra)

    def _take(self) -> None:
        from concurrent.futures.process import BrokenProcessPool

        fut, kind, packed, sink = self.waiting.popleft()
        try:
            while True:
                try:
                    res = fut.result(timeout=POOL_WAIT)
                    break
                except TimeoutError:
                    check(self.cancel)
        except BrokenProcessPool as exc:
            self._broken(exc)
            res = ("redo",)
        if res[0] != "ok":
            self._local(kind, _unpack_args(kind, packed), sink)
            return
        _tag, data, n, extra = res
        self.w.write_ops(data, n)
        if sink is not None:
            sink(extra)

    def finish(self) -> None:
        """Write the results of every chunk in flight."""
        while self.waiting:
            self._take()

    def close(self) -> None:
        """Stop the workers (idempotent; results not taken are dropped)."""
        waiting, self.waiting = self.waiting, None
        for fut, *_rest in waiting or ():
            fut.cancel()
        ex, self.ex = self.ex, None
        if ex is not None:
            ex.shutdown(wait=True, cancel_futures=True)


# ----------------------------------------------------------------------------------
# export: the net effect
# ----------------------------------------------------------------------------------
class _Exporter:
    """The net effect of an edit state against the original file, as JSON Patch operations (module docstring,
    "Export")."""

    def __init__(self, *, state: OverlayState, layout: _Layout, doc: Any, diff: bool, steps: Callable[[], Any],
                 settings: Any, warnings: list[str] | None, progress: Progress | None,
                 cancel: CancelToken | None, trimmed: bool = False, workers: int | None = None) -> None:
        self.workers = workers                             # diffs in worker processes (_DiffPool)
        self.pool_used = 1
        self.settings = settings
        self.st = state
        self.n_base = state.base_count
        self.layout = layout
        self.doc = doc                                     # a Document of THIS file version, or None
        self.base = doc.pinned(None) if doc is not None else None
        self.eff = doc.pinned(state) if doc is not None else None
        self.diff = bool(diff and doc is not None)
        self.steps = steps
        self.trimmed = trimmed                             # steps were folded into the checkpoint (keys lost)
        self.large = int(settings.large_node_bytes)
        self.warnings = warnings
        self.progress = progress
        self.cancel = cancel
        self._first_keys: dict[int, str] | None = None

    # -- helpers ----------------------------------------------------------------------------------------
    def _warn(self, key: str, **params: Any) -> None:
        text = t(key, **params)
        log.warning("JSON Patch export: %s", text)
        if self.warnings is not None:
            self.warnings.append(text)

    def _tick(self, nbytes: int) -> None:
        if self.progress is not None:
            self.progress.advance(nbytes)

    def _read(self, s: int, e: int) -> bytes:
        """Bytes of a content span: the edit layer, or (with a document) the data file."""
        if s >= EDIT_BASE:
            data = self.st.read(s, e)
        else:
            assert self.base is not None
            with self.base.open_data() as f:
                data = read_range(f, s, e)
        self._tick(e - s)
        return data

    def _base_rid_of_span(self, s: int, e: int) -> int | None:
        """The original record whose span is ``(s, e)`` (needs the document's index)."""
        base = self.base
        if base is None:
            return None
        n = self.n_base
        i = bisect.bisect_left(range(n), s, key=lambda k: base.record_span(k)[0])
        if i < n and base.record_span(i) == (s, e):
            return i
        return None

    def _base_raw(self, rid: int) -> bytes | None:
        """The original bytes of a record; ``None`` above ``large_node_bytes`` (written whole instead)."""
        assert self.base is not None
        s, e = self.base.record_span(rid)
        if e - s > self.large:
            return None
        data = self.base.record_bytes(rid)
        self._tick(e - s)
        return data

    def _read_spans(self, starts: Sequence[int], ends: Sequence[int]) -> list[bytes]:
        """The bytes of many content spans (edit layer or data file), in the given order: neighbours of one
        address space / segment at most :data:`COALESCE_GAP` apart are read together (ranges of at most
        :data:`COALESCE_MAX` bytes) -- the units of one step lie side by side, the originals in file order."""
        n = len(starts)
        out = [b""] * n
        if not n:
            return out
        order = range(n) if _ascending(starts) else sorted(range(n), key=starts.__getitem__)
        f = None
        j = 0
        try:
            while j < n:
                k0 = order[j]
                lo, hi = starts[k0], ends[k0]
                edit = lo >= EDIT_BASE
                if edit:                              # a segment: the group ends before the next segment
                    limit = EDIT_BASE + ((((lo - EDIT_BASE) >> SEG_SHIFT) + 1) << SEG_SHIFT)
                else:                                 # the data file: the group ends before the edit space
                    limit = EDIT_BASE
                group = [k0]
                j += 1
                while j < n:
                    k = order[j]
                    s, e = starts[k], ends[k]
                    top = e if e > hi else hi
                    if s - hi > COALESCE_GAP or top - lo > COALESCE_MAX or s >= limit:
                        break
                    group.append(k)
                    hi = top
                    j += 1
                if edit:
                    data = self.st.read(lo, hi)
                else:
                    assert self.base is not None
                    if f is None:
                        f = self.base.open_data()
                    data = read_range(f, lo, hi)
                for k in group:
                    out[k] = data[starts[k] - lo:ends[k] - lo]
        finally:
            if f is not None:
                f.close()
        self._tick(sum(ends) - sum(starts))
        return out

    def _base_spans(self, rids: Sequence[int]) -> tuple[Sequence[int], Sequence[int]]:
        """``(starts, ends)`` of original records: ascending rids close together come from ONE read of the
        index (``Document._spans_bytes``), the others record by record (``record_span``)."""
        base: Any = self.base
        n = len(rids)
        if n > 1 and _ascending(rids):
            lo = rids[0]
            m = rids[-1] + 1 - lo
            if m <= 4 * n:
                raw = base._spans_bytes(lo, m)
                if len(raw) == 16 * m:
                    flat = array("Q")
                    flat.frombytes(raw)
                    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
                        flat.byteswap()
                    s_all, e_all = flat[0::2], flat[1::2]
                    if m == n:
                        return s_all, e_all
                    idx = [r - lo for r in rids]
                    return [s_all[i] for i in idx], [e_all[i] for i in idx]
        spans = [base.record_span(r) for r in rids]
        return [s for s, _e in spans], [e for _s, e in spans]

    def _base_raws(self, rids: Sequence[int]) -> list[bytes | None]:
        """:meth:`_base_raw` of many original records (coalesced reads; ``None`` for a huge record)."""
        assert self.base is not None
        starts, ends = self._base_spans(rids)
        large = self.large
        if max(map(sub, ends, starts), default=0) <= large:
            return self._read_spans(starts, ends)       # type: ignore[return-value]
        small = [k for k in range(len(rids)) if ends[k] - starts[k] <= large]
        out: list[bytes | None] = [None] * len(rids)
        got = self._read_spans([starts[k] for k in small], [ends[k] for k in small])
        for k, data in zip(small, got):
            out[k] = data
        return out

    def _orig_keys(self) -> dict[int, str]:
        """Original keys of records from ``Step.keys``: the first ``old`` recorded for each record."""
        if self._first_keys is None:
            out: dict[int, str] = {}
            for st in self.steps():
                check(self.cancel)
                for rid, old, _new in st.keys or ():
                    if rid < self.n_base and old is not None and rid not in out:
                        out[rid] = old
            self._first_keys = out
        return self._first_keys

    # -- the whole export ---------------------------------------------------------------------------------
    def write(self, path: Any, source: Any = None) -> int:
        lay = self.layout
        for src in (source, getattr(self.doc, "source_path", None), getattr(self.doc, "data_path", None)):
            if src is not None and _same_file(path, src):
                raise UserInputError("writer.error.same_file", path=str(path))
        with atomic_write(path) as f:
            w = _PatchWriter(f, self.cancel)
            if self.progress is not None:
                self.progress.set_message(t("patch.progress.export"))
            if lay.single:
                self._single(w)
            elif lay.member:
                self._members(w)
            else:
                self._values(w)
            if lay.wrapped and self.st.wrapper is not None:
                self._wrapper(w)
            w.close()
            check(self.cancel)
            if self.diff:
                self.doc.ensure_unchanged()   # nothing is written from a file that changed meanwhile
        return w.count

    # -- single documents ---------------------------------------------------------------------------------
    def _single(self, w: _PatchWriter) -> None:
        st = self.st
        if st.count != 1:   # pragma: no cover - single documents have one record
            return
        rid = st.rid_at(0)
        span = st.content(rid)
        if span is None:
            return
        new_raw = self._read(*span)
        if self.diff:
            old = self._base_raw(rid)
            if old is not None:
                _diff(w, codec.loads(old), codec.loads(new_raw), ())
                return
        w.op("replace", (), raw=_compact(new_raw))

    # -- value documents ----------------------------------------------------------------------------------
    def _values(self, w: _PatchWriter) -> None:
        st, n_base, cancel = self.st, self.n_base, self.cancel
        cptr = self.layout.cptr
        # A. the final sequence: runs of original records, new records (copies / adds)
        b_pos, b_lo, b_n = array("Q"), array("Q"), array("Q")
        n_pos, n_s, n_e = array("Q"), array("Q"), array("Q")
        n_src = array("q")        # >= 0: a copy of that original record; -1: add; -2: an unknown copy
        for k, (p, r, n) in enumerate(st.pieces.runs()):
            if not k % _CHECK_EVERY:
                check(cancel)
            if r < n_base:
                m = min(n, n_base - r)
                b_pos.append(p)
                b_lo.append(r)
                b_n.append(m)
                p, r, n = p + m, r + m, n - m
            if n:
                spans = st.contents(r, n)
                for j in range(n):
                    s, e = spans[2 * j], spans[2 * j + 1]
                    n_pos.append(p + j)
                    n_s.append(s)
                    n_e.append(e)
                    if s < EDIT_BASE:
                        src = self._base_rid_of_span(s, e)
                        n_src.append(src if src is not None else -2)
                    else:
                        n_src.append(-1)
        # B. present originals; the first copy of a deleted original takes its place (the runs are sorted
        # only when records were moved: no list of the runs otherwise)
        if _ascending(b_lo):
            plo = b_lo
            phi = array("Q", map(add, b_lo, b_n))
        else:
            order = sorted(range(len(b_lo)), key=b_lo.__getitem__)
            plo = array("Q", (b_lo[i] for i in order))
            phi = array("Q", (b_lo[i] + b_n[i] for i in order))
            del order

        def present(b: int) -> bool:
            i = bisect.bisect_right(plo, b) - 1
            return i >= 0 and b < phi[i]

        inherit: dict[int, int] = {}          # original id -> index of the new record that takes its place
        omit = array("Q")                     # final positions left out (unknown copies)
        for k in range(len(n_pos)):
            b = n_src[k]
            if b >= 0 and not present(b) and b not in inherit:
                inherit[b] = k
            elif b == -2:
                omit.append(n_pos[k])
        if omit:
            self._warn("patch.warn.copies_unknown", count=len(omit))

        def adj(pos: int) -> int:
            return pos - bisect.bisect_left(omit, pos) if omit else pos

        # identity runs in final order: original runs + inherited copies (length 1)
        inh = sorted((n_pos[k], b) for b, k in inherit.items())
        i_pos, i_lo, i_n = array("Q"), array("Q"), array("Q")
        a = c = 0
        while a < len(b_pos) or c < len(inh):
            if c >= len(inh) or (a < len(b_pos) and b_pos[a] < inh[c][0]):
                i_pos.append(adj(b_pos[a]))
                i_lo.append(b_lo[a])
                i_n.append(b_n[a])
                a += 1
            else:
                i_pos.append(adj(inh[c][0]))
                i_lo.append(inh[c][1])
                i_n.append(1)
                c += 1
        # survivors (present + inherited) as sorted intervals with prefix counts -> rank(b)
        if inherit:
            surv_lo, surv_hi = _merge_sorted(plo, phi, sorted(inherit))
        else:
            surv_lo, surv_hi = plo, phi
        s_lo, s_hi = _join_touching(surv_lo, surv_hi)
        del surv_lo, surv_hi
        s_cum = array("Q", accumulate(map(sub, s_hi, s_lo), initial=0))

        def rank(b: int) -> int:
            i = bisect.bisect_right(s_lo, b) - 1
            return s_cum[i] + b - s_lo[i]

        # (1) removes of deleted originals, descending
        prev = n_base
        for i in range(len(s_lo) - 1, -2, -1):
            gap_lo = s_hi[i] if i >= 0 else 0
            for x in range(prev - 1, gap_lo - 1, -1):
                w.op("remove", cptr + (str(x),))
            if i >= 0:
                prev = s_lo[i]
        # (2) moves of the originals outside the kept increasing runs
        keep = _mwis(i_lo, i_n)
        m_ranks = array("Q")
        for i in range(len(i_lo)):
            if not keep[i]:
                r0 = rank(i_lo[i])
                m_ranks.extend(range(r0, r0 + i_n[i]))
        if m_ranks:
            m_ranks = array("Q", sorted(m_ranks))
            fen = _Fenwick(len(m_ranks))
            placed = array("q")               # anchor ranks of the moved records, non-decreasing
            anchor = -1
            for i in range(len(i_lo)):
                lo, n = i_lo[i], i_n[i]
                if keep[i]:
                    anchor = rank(lo + n - 1)
                    continue
                for b in range(lo, lo + n):
                    s = rank(b)
                    x = bisect.bisect_left(m_ranks, s)
                    src_pos = s - fen.prefix(x) + bisect.bisect_left(placed, s)
                    fen.add(x)
                    if anchor >= 0:
                        dst = anchor + 1 - fen.prefix(bisect.bisect_right(m_ranks, anchor)) + len(placed)
                    else:
                        dst = len(placed)
                    w.op("move", cptr + (str(dst),), from_=cptr + (str(src_pos),))
                    placed.append(anchor)
        # (3) + (4) copies and adds, ascending final positions
        if len(n_pos) > len(inherit) + len(omit):
            by_lo = sorted(range(len(i_lo)), key=i_lo.__getitem__)
            x_lo = array("Q", (i_lo[i] for i in by_lo))
            x_pos = array("Q", (i_pos[i] for i in by_lo))
            new_final = array("Q", (adj(n_pos[k]) for k in range(len(n_pos))
                                    if n_src[k] != -2 and not (n_src[k] >= 0 and inherit.get(n_src[k]) == k)))
            inserted = 0
            cbase = _ptr(cptr)
            for c0 in range(0, len(n_pos), EXPORT_CHUNK):
                check(cancel)
                ks = range(c0, min(len(n_pos), c0 + EXPORT_CHUNK))
                adds = [k for k in ks if n_src[k] == -1]
                raws = dict(zip(adds, self._read_spans([n_s[k] for k in adds], [n_e[k] for k in adds])))
                for k in ks:
                    b = n_src[k]
                    if b == -2 or (b >= 0 and inherit.get(b) == k):
                        continue
                    p = adj(n_pos[k])
                    if b >= 0:
                        j = bisect.bisect_right(x_lo, b) - 1
                        fb = x_pos[j] + b - x_lo[j]
                        cur = fb if fb < p else fb - bisect.bisect_left(new_final, fb) + inserted
                        w.op_at("copy", f"{cbase}/{p}", from_=f"{cbase}/{cur}")
                    else:
                        w.op_at("add", f"{cbase}/{p}", raw=_compact(raws[k]))
                    inserted += 1
        # (5) changed originals: a diff at the final positions (read and diffed in chunks; many: in workers)
        cbase = _ptr(cptr)
        chunk: list[tuple[int, int, int, int]] = []
        pool = _DiffPool(w, self.workers, self.settings, cancel)
        try:
            for p, r, n, pure in st.runs():
                check(cancel)
                if pure or r >= n_base:
                    continue
                m = min(n, n_base - r)
                spans = st.contents(r, m)
                for j in range(m):
                    s, e = spans[2 * j], spans[2 * j + 1]
                    b = r + j
                    if s < EDIT_BASE:
                        if self.base is None or self.base.record_span(b) == (s, e):
                            continue                                  # its own original bytes
                        if not self.diff:                             # pragma: no cover - needs the data file
                            continue
                    chunk.append((adj(p + j), b, s, e))
                    if len(chunk) >= EXPORT_CHUNK:
                        self._changed_values(pool, cbase, chunk)
                        chunk = []
            if chunk:
                self._changed_values(pool, cbase, chunk)
            pool.finish()
        finally:
            pool.close()
        self.pool_used = pool.used

    def _changed_values(self, pool: _DiffPool, cbase: str, chunk: list[tuple[int, int, int, int]]) -> None:
        """Ops for original value records whose content is now ``(s, e)`` -- ``(final position, rid, s, e)`` in
        final order: a diff against the original (``diff``; a huge original: the whole record), else the whole
        record as ``replace`` (:func:`_values_ops`)."""
        check(self.cancel)
        news = self._read_spans([c[2] for c in chunk], [c[3] for c in chunk])
        olds = self._base_raws([c[1] for c in chunk]) if self.diff else [None] * len(chunk)
        nbytes = sum(map(len, news)) + sum(len(o) for o in olds if o is not None)
        pool.run("values", (cbase, array("Q", [c[0] for c in chunk]), olds, news), nbytes)

    # -- member-record documents ----------------------------------------------------------------------
    def _base_keys(self, rids: array) -> Iterator[tuple[int, str]]:
        """``(rid, original key)`` of original records, read from the file in blocks."""
        assert self.base is not None
        from .reader import split_member

        for blk in self.base.iter_blocks(rids, cancel=self.cancel):
            idx = blk.indices
            if blk.huge:
                yield int(idx[0]), self.base.record_key(int(idx[0])) or ""
                continue
            sp = array("Q")
            sp.frombytes(blk.spans)
            data = blk.data
            for k in range(len(idx)):
                key, _vo = split_member(data[sp[2 * k]:sp[2 * k + 1]])
                yield int(idx[k]), key

    def _members(self, w: _PatchWriter) -> None:
        from .reader import split_member

        st, n_base, cancel = self.st, self.n_base, self.cancel
        cptr = self.layout.cptr
        present_lo, present_hi = array("Q"), array("Q")
        new_rids = array("Q")
        for k, (_p, r, n) in enumerate(st.pieces.runs()):
            if not k % _CHECK_EVERY:
                check(cancel)
            if r < n_base:
                m = min(n, n_base - r)
                present_lo.append(r)
                present_hi.append(r + m)
                r, n = r + m, n - m
            if n:
                new_rids.extend(range(r, r + n))
        order = sorted(range(len(present_lo)), key=present_lo.__getitem__)
        plo = [present_lo[i] for i in order]
        phi = [present_hi[i] for i in order]
        # (1) removes of the deleted originals, by their original key
        deleted = array("Q")
        prev = 0
        for lo, hi in zip(plo + [n_base], phi + [n_base]):
            deleted.extend(range(prev, lo))
            prev = hi
        unknown = 0
        if deleted:
            if self.diff:
                for _rid, key in self._base_keys(deleted):
                    w.op("remove", cptr + (key,))
            else:
                keys = self._orig_keys()
                for rid in deleted:
                    key = keys.get(rid)
                    if key is None:
                        unknown += 1
                    else:
                        w.op("remove", cptr + (key,))
        # (2) changed originals: value diffs at their ORIGINAL keys, renames collected (read in chunks; many:
        # diffed in workers)
        renames: list[tuple[str, str]] = []
        lost = [0]                         # diff=False: changed records whose rename may be in trimmed history
        chunk: list[tuple[int, int, int]] = []
        pool = _DiffPool(w, self.workers, self.settings, cancel)
        try:
            for _p, r, n, pure in st.runs():
                check(cancel)
                if pure or r >= n_base:
                    continue
                m = min(n, n_base - r)
                spans = st.contents(r, m)
                for j in range(m):
                    s, e = spans[2 * j], spans[2 * j + 1]
                    if s < EDIT_BASE:
                        continue                                       # original bytes (never produced)
                    chunk.append((r + j, s, e))
                    if len(chunk) >= EXPORT_CHUNK:
                        self._changed_members(pool, chunk, renames, lost)
                        chunk = []
            if chunk:
                self._changed_members(pool, chunk, renames, lost)
            pool.finish()
        finally:
            pool.close()
        self.pool_used = pool.used
        maybe_lost = lost[0]
        if unknown:
            self._warn("patch.warn.keys_unknown", count=unknown)
        if maybe_lost and self.trimmed:
            self._warn("patch.warn.history_trimmed")
        # (3) renames: a move when the new key is free at that point; cycles through a temporary key
        self._renames(w, renames)
        # (4) new records, appended in final order
        for k, rid in enumerate(new_rids):
            if not k % _CHECK_EVERY:
                check(cancel)
            span = st.content(rid)
            if span is None:   # pragma: no cover - a new record always has content
                raise EditError("edits.error.layer_corrupt", path="", detail=f"record id {rid} has no content")
            unit = self._read(*span)
            key, vo = split_member(unit)
            w.op("add", cptr + (key,), raw=_compact(unit[vo:]))

    def _changed_members(self, pool: _DiffPool, chunk: list[tuple[int, int, int]], renames: list[tuple[str, str]],
                         lost: list[int]) -> None:
        """Step (2) of :meth:`_members` for ``(rid, s, e)`` of changed original member records (rid order of the
        runs): the value diff at the ORIGINAL key (a huge original: ``add`` of the whole value), renames noted
        (:func:`_members_ops`). The original key without original bytes: the file's (``diff``, a huge record),
        else the first one ``Step.keys`` recorded (none: the current key, counted in ``lost``)."""
        check(self.cancel)
        units = self._read_spans([c[1] for c in chunk], [c[2] for c in chunk])
        braws = self._base_raws([c[0] for c in chunk]) if self.diff else [None] * len(chunk)
        okeys: list[str | None] = []
        if self.diff:
            base: Any = self.base
            for (rid, _s, _e), braw in zip(chunk, braws):
                okeys.append(None if braw is not None else (base.record_key(rid) or ""))
        else:
            first = self._orig_keys()
            for rid, _s, _e in chunk:
                okey = first.get(rid)
                if okey is None:
                    lost[0] += 1
                okeys.append(okey)
        nbytes = sum(map(len, units)) + sum(len(b) for b in braws if b is not None)
        pool.run("members", (self.layout.cptr, units, braws, okeys), nbytes, renames.extend)

    def _renames(self, w: _PatchWriter, renames: list[tuple[str, str]]) -> None:
        """Record renames ``old -> new`` as ``move`` operations, each when its target key is free. Keys are
        unique, so the renames form disjoint paths (emitted from their free end backwards) and cycles (broken
        through a temporary key): O(renames)."""
        cptr = self.layout.cptr
        nxt: dict[str, str] = dict(renames)
        targets = set(nxt.values())
        done: set[str] = set()

        def move(a: str, b: str) -> None:
            w.op("move", cptr + (b,), from_=cptr + (a,))

        for start in nxt:
            if start in targets:
                continue                                    # inside a path or a cycle: reached from its start
            chain = [start]
            while nxt[chain[-1]] in nxt and nxt[chain[-1]] not in done and len(chain) <= len(nxt):
                chain.append(nxt[chain[-1]])
            for a in reversed(chain):                       # the last link's target is free
                if a not in done:
                    move(a, nxt[a])
                    done.add(a)
        for start in nxt:
            if start in done:
                continue
            cycle = [start]                                 # only cycles are left
            while (nxt[cycle[-1]] != start and nxt[cycle[-1]] in nxt and nxt[cycle[-1]] not in done
                   and len(cycle) <= len(nxt)):
                cycle.append(nxt[cycle[-1]])
            tmp = f"jsonreshape-tmp-{secrets.token_hex(8)}"
            move(start, tmp)
            for a in reversed(cycle[1:]):
                move(a, nxt[a])
            move(tmp, nxt[start])
            done.update(cycle)

    # -- the wrapper --------------------------------------------------------------------------------------
    def _wrapper(self, w: _PatchWriter) -> None:
        st, lay = self.st, self.layout
        assert st.wrapper is not None
        new = codec.loads(self._read(*st.wrapper))
        if self.diff:
            doc = self.doc
            span = doc.container_span
            root = self.base.root()                              # type: ignore[union-attr]
            cs, ce = span
            with self.base.open_data() as f:                     # type: ignore[union-attr]
                old_raw = read_range(f, root.start, cs) + lay.placeholder + read_range(f, ce, root.end)
            _diff_wrapper(w, codec.loads(old_raw), new, lay.cpath)
            return
        # diff=False: every member of the objects on the record path (``add`` replaces or creates it)
        cur, p = new, ()
        partial = True
        for nxt in lay.cpath:
            if isinstance(cur, dict) and isinstance(nxt, str) and nxt in cur:
                for k, v in cur.items():
                    if k != nxt:
                        w.op("add", p + (k,), value=v)
                cur, p = cur[nxt], p + (nxt,)
            elif isinstance(cur, list) and isinstance(nxt, int) and nxt < len(cur):
                for i in range(nxt):
                    w.op("replace", p + (str(i),), value=cur[i])
                cur, p = cur[nxt], p + (str(nxt),)
            else:   # pragma: no cover
                break
        if partial:
            self._warn("patch.warn.wrapper_partial")


# ----------------------------------------------------------------------------------
# export: the entry points
# ----------------------------------------------------------------------------------
def _same_file(a: Any, b: Any) -> bool:
    """``a`` and ``b`` name the same file (the patch must never replace the user's data file)."""
    from .fileio import long_path, normalize_path

    try:
        if normalize_path(a) == normalize_path(b):
            return True
        return os.path.samefile(long_path(a), long_path(b))
    except (OSError, ValueError, TypeError):
        return False


def _steps_loader(store: Any, version: int) -> Callable[[], Iterator[Any]]:
    def load() -> Iterator[Any]:
        if store is None or getattr(store, "closed", False):
            return
        for seq in range(int(store.checkpoint_seq) + 1, int(version) + 1):
            yield store.step(seq)
    return load


def export_patch(session: Any, path: Any, *, diff: bool = True, progress: Progress | None = None,
                 cancel: CancelToken | None = None, warnings: list[str] | None = None,
                 workers: int | None = None) -> int:
    """Write the net effect of the session's edits against the original file as a JSON Patch to ``path``
    (atomically); returns the number of operations (module docstring, "Export"). ``diff=False``: no byte of
    the data file is read (the source changed on disk). ``warnings``: a list that receives the localized
    warnings of the known gaps. ``workers``: worker processes for the diffs of changed records (``None``:
    automatic for many, see "Speed and memory"; ``1``: this thread only; the patch is the same either way)."""
    with session.lock:
        if session.closed:
            raise EditError("edits.error.closed")
        doc = session.doc
        state = session.state
        if diff:
            doc.ensure_unchanged()
        layout = _Layout(doc.kind, doc.record_path, doc.member_records)
        store = session.store
        ex = _Exporter(state=state, layout=layout, doc=doc, diff=diff,
                       steps=_steps_loader(store, state.version), settings=session.settings,
                       warnings=warnings, progress=progress, cancel=cancel,
                       trimmed=store is not None and not store.closed and store.checkpoint_seq > 0, workers=workers)
        n = ex.write(path)
        if progress is not None:
            progress.finish()
        log.info("JSON Patch export of %s: %d operations (diff=%s, workers=%d)", doc.source_path.name, n, diff,
                 ex.pool_used)
        return n


def _layer_member(store: Any, state: OverlayState, kind: str, base_doc: Any, steps: Callable[[], Any]) -> bool:
    """Whether a layer's records are object members (keyed; wrapped with an object container)."""
    if kind == "keyed":
        return True
    if kind != "wrapped":
        return False
    if state.wrapper_hole is not None:
        try:
            return state.read(*state.wrapper_hole) == b"{}"
        except JsonReshapeError:   # pragma: no cover
            pass
    for st in steps():
        if st.keys:
            return True
    for _p, r, n, pure in state.runs():
        if not pure:
            s, e = state.contents(r, 1)[:2]
            if s >= EDIT_BASE:
                return state.read(s, min(e, s + 1)) == b'"'
    return bool(base_doc is not None and getattr(base_doc, "member_records", False))


def export_layer(store: Any, path: Any, *, base_doc: Any = None, progress: Progress | None = None,
                 cancel: CancelToken | None = None, warnings: list[str] | None = None,
                 workers: int | None = None) -> int:
    """Export a pending or stale layer (an open :class:`~jsonreshape.editstore.EditStore`, usually opened with
    ``read_only=True, allow_stale=True``) as a JSON Patch; returns the number of operations. ``base_doc``: a
    Document of the layer's file version (used only when it matches: module docstring); otherwise
    ``diff=False`` semantics. The store is not closed. ``workers``: as :func:`export_patch`."""
    try:
        state = store.load_state(cancel=cancel)
    except LayerDamagedError as exc:
        if exc.last_good is None:
            raise
        state = store.load_state(upto=exc.last_good, cancel=cancel)
        msg = t("patch.warn.damaged", seq=exc.seq, steps=exc.last_good)
        log.warning("JSON Patch export of a damaged layer: %s", msg)
        if warnings is not None:
            warnings.append(msg)
    meta = getattr(store, "_meta", None) or getattr(store, "_doc_meta", None) or {}
    kind = str(meta.get("kind") or (base_doc.kind if base_doc is not None else "array"))
    record_path = meta.get("record_path")
    if record_path is None and base_doc is not None:
        record_path = base_doc.record_path
    doc = None
    if base_doc is not None and not getattr(store, "stale", False):
        origin = getattr(base_doc, "origin", base_doc)
        try:
            if (origin.index_complete and layer_key(origin) == store.key
                    and origin.base_record_count == state.base_count):
                doc = origin
        except JsonReshapeError:
            doc = None
    steps = _steps_loader(store, state.version)
    member = doc.member_records if doc is not None else _layer_member(store, state, kind, base_doc, steps)
    diff = doc is not None and not doc.check_changed()
    from .settings import get_settings

    settings = doc.settings if doc is not None else get_settings()
    layout = _Layout(kind, tuple(record_path) if record_path is not None else None, member)
    ex = _Exporter(state=state, layout=layout, doc=doc, diff=diff, steps=steps, settings=settings,
                   warnings=warnings, progress=progress, cancel=cancel, trimmed=store.checkpoint_seq > 0,
                   workers=workers)
    n = ex.write(path, meta.get("source"))
    if progress is not None:
        progress.finish()
    log.info("JSON Patch export of the layer %s: %d operations (diff=%s, workers=%d)", getattr(store, "key", "?"),
             n, diff, ex.pool_used)
    return n
