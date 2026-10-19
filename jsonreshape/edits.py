"""Editing session (docs/ARCHITECTURE.md §12.5): edit commands, undo/redo, bulk/save/patch entry points.

:class:`EditSession` is the one object that changes a document: it turns commands into *steps* (one undo unit
each), persists them through :class:`~jsonreshape.editstore.EditStore` (the continuous autosave) and produces the
next immutable :class:`~jsonreshape.overlay.OverlayState` plus a :class:`~jsonreshape.overlay.ChangeSet` that tells
holders of positions/nodes what moved. It is Qt-free and thread-agnostic: commands are serialized by an internal
lock (:meth:`EditSession.bulk_preview` does not hold it).

References (:class:`Ref`)
=========================
``Ref(record, path, ipath)`` -- ``record`` is an effective POSITION (§12.0); ``path`` holds keys/indices relative to
the record VALUE (member records: the value after ``"key":``); ``ipath`` the child indices along ``path`` when known
(complete ipaths select duplicate keys exactly and are verified against the path: a stale ref fails with
``edits.error.path_not_found``). ``record=None`` addresses the wrapper of a wrapped document (``path`` absolute from
the root); single documents use ``record=0`` (``None`` is accepted and normalized to 0). The GUI's ``NodeRef`` has
the same shape. Positions/paths in ChangeSets use the canonical form: array indices are ``int``, keys ``str``.

How a node command works
========================
The record's current bytes are read through ``doc.pinned(state)`` (a record larger than ``large_node_bytes`` ->
``edits.error.huge_record``; a unit that would exceed it too), member records are split into their member head
(kept byte for byte) and the value buffer, the nodes are located with :mod:`jsonreshape.editops`, spliced, every new
unit is validated with ``editops.check_unit`` (its flags go into the step), appended to segment 0 and ONE step is
committed (``store.commit`` -> ``editstore.apply_step``). Inside one command every resolved node is a tracked target:
after each splice the targets of that record are remapped exactly like a holder would remap them with the
NodeChange (array indices of ``path`` and ``ipath``), so several operations in one record never use stale offsets.

Decisions where §12.5 leaves room (all documented in the stage-3 report)
========================================================================
1. ``open(doc, *, settings, lock, restore, auto_apply=True, upto=None)``: ``auto_apply`` is an open() keyword too (a
   GUI worker must not apply the restored state from its thread); ``upto`` restores a damaged layer up to
   ``LayerDamagedError.last_good`` (the head is moved there; the later steps become redo steps). Preconditions:
   a closed document -> ``edits.error.closed``; index not complete -> ``editstore.error.not_indexed``; a JSON error
   -> ``edits.error.partial`` (new key); a source changed on disk -> ``FileChangedError`` (``doc.ensure_unchanged``;
   append-only growth is accepted, the snapshot is edited). A pending layer with an interrupted save journal ->
   ``edits.error.pending``
   (the caller rolls the journal first, :func:`jsonreshape.saver.roll_journal`). With ``auto_apply`` the session's
   state (also the initial one) is set on the document at open, so ``doc.gen`` equals ``session.state.gen``.
2. The lock: ``open`` takes ``LayerLock.for_doc(doc)`` itself or uses the HELD lock it is given; either way the
   session owns it once ``open`` returned and :meth:`EditSession.close` / a successful save release it (a lock given
   to a failing ``open`` stays the caller's).
3. ``store`` is ``None`` until the first commit (or a restored layer); a new store is opened with the same lock
   after :meth:`discard`. Before a commit the state gets the store's reader (``state.derive(reader=...)``; the gen
   the caller knows is not changed by this).
4. A command that changes nothing (same value, rename to the same key, a move onto itself, an empty selection)
   commits NO step and returns an empty ChangeSet of the current state (``records=frozenset()``, no remap).
5. ``ChangeSet.records`` = positions (after the step) that HAVE a preimage and whose content changed; inserted
   records are covered by ``remap`` (no preimage) and are not listed.
6. NodeChanges are emitted in execution order, which is ancestors first (operations are executed by container
   depth); every ipath is valid after the previous entries (§12.1). Replacing a container's value (set_value,
   change_type) is the NodeChange ``del 0..n_old, ins 0..n_new`` (all old children gone). Key renames change no
   positions: no NodeChange (``records`` / ``select`` carry them).
7. ``Step.select`` stores ``[after, before]``: the redo/forward selection and the undo selection (the first ref
   before the step); ChangeSets of undo use ``before``. Producers that store a single ``(record, path, ipath)``
   are accepted too.
8. ``Step.keys`` (member-record documents): ``(rid, old, new)`` for record renames, ``(rid, None, key)`` for new
   records, ``(rid, key, None)`` for deleted records (up to :data:`KEYS_MAX`; a bigger delete stores ``None``).
9. ``delete_records`` stores ``("dels", positions)`` (``("del", p, n)`` when contiguous) while the deleted records
   form at most :data:`DELS_RUNS_MAX` runs of consecutive rids, else ``("pieces", old, new)`` (no per-run tuples for
   ``old_rids``: memory stays ~16 bytes per piece). ``duplicate_records`` of more than :data:`MAX_ITEMS` records
   writes an array layer (value documents: the source spans, no new bytes; member documents: new units with free
   keys in a new segment). Progress: bytes of the records read (member documents), else the number of records.
   Positions may be given in any order (a bitmap normalizes unsorted input), duplicates are ignored; a RecordSet of
   another state -> ``edits.error.stale_scope``.
10. Wrapper (``record=None``): W = the root value's text with the record container replaced by ``[]``/``{}`` (the
    hole). The hole is located with FIRST-occurrence semantics (like the scanner's target path) and tracked through
    every splice; refused with ``edits.error.container_locked``: a ref whose path starts with the record path (the
    container or anything through it), any command whose target equals or contains the hole (set, change_type,
    delete, duplicate, move, rename of a key on the record path), any insert/delete/move of children of an ancestor
    ARRAY at an index <= the container's index (objects keep their keys, so members may be added/removed). After the
    splices the hole is located again in W' and must be the untouched placeholder. W' over ``large_node_bytes`` ->
    ``edits.error.huge_record``.
11. Moves: ``index`` is a child position before the step (``None`` = end); the moved items keep their DOCUMENT order
    (wrapper first, then records by position, then by ipath). Sources inside the target container are moved
    (NodeChange ``("move", positions, to)``, so holders keep them); items from elsewhere are inserted
    (``("inss", ...)``) and removed from their containers. One source moved inside its own container keeps every
    byte (``editops.move_child``); otherwise moved members are re-inserted with ``editops.encode_key`` (escapes of
    the key token normalized) and values from another container are re-formatted for their new place. Key rules of
    §12.4: free keys (``key_2`` ...) ignoring the moved members themselves; an element (or a value record) moved into
    an object gets its old index (position) as key; members moved into arrays (or to the record level of a value
    document) drop their key; a node moved to the record level of a member document keeps its key or gets a free one.
    Whole records moved into a node are removed from the record level (value records keep their key as
    ``str(position)``). ``move_by``: all refs must be siblings (else ``edits.error.not_siblings``, new key); the
    selection moves as one block before ``first + delta`` (up) / after ``last + delta`` (down), clamped.
12. ``paste`` at the record level of a member document: ``key`` or a free key from ``"pasted"``; ``as_records`` with
    an OBJECT value inserts every member as a record with its own (explicit) key; with an array value every element
    gets a free key from ``key or "pasted"``.
13. ``set_path`` creates missing members of objects only: an existing non-object on the way, or a missing ARRAY
    index, -> ``edits.error.not_object``.
14. :class:`KeyIndex` (member-record documents): see its docstring. It is built from the effective records of the
    state at the time of its first need (its "snapshot"), plus a persistent delta maintained at every transition:
    for steps touching at most :data:`MAX_ITEMS` records the current keys are READ (so foreign steps need no
    bookkeeping), bigger steps use ``Step.keys``; when neither is possible (a big ``("pieces", ...)`` step without
    keys) the index is reset and rebuilt at the next need. Lookups confirm every hash hit by decoding the current key.
15. Save: ``backup=None`` = ``settings.backup_on_save``; after a successful save the layer is discarded and the
    session closed (every command -> ``edits.error.closed``). The document passed to ``saver.save`` is pinned to the
    session's state. Once the file is written, a failure of that cleanup (tombstoning / deleting the layer) is
    logged and becomes the report's warning ``edits.warn.layer_left`` -- the save succeeded; the session is closed
    all the same (store and lock released; the layer is offered at the next open). ``discard`` tombstones the layer
    first, then resets the state, then deletes the files (those that states still read are deleted later, see
    :mod:`jsonreshape.editstore`).
16. Producers outside this module (bulk, JSON Patch) commit through :meth:`EditSession.commit_step` (inside the
    command lock: ``import_patch`` holds it, ``bulk_apply`` takes it) and append through
    :meth:`EditSession.ensure_store`. An applied bulk preview is released (``preview.discard()`` keeps the files
    of the committed step).
17. Whole-record commands (record delete / move / value-document duplicates) never read the record's bytes, so they
    also work on huge records; anything that needs the bytes (a node inside it, a member-record duplicate, a whole
    record moved INTO a node, a record-key rename) -> ``edits.error.huge_record``.
18. Error order of moves: ``move_into_self`` (a source is the target or one of its ancestors -- a whole record
    counts as the ancestor of all its nodes) is checked before ``not_container`` and the wrapper rules.
19. Units per synchronous command: changed records + new units + reused spans <= :data:`MAX_ITEMS`, else
    ``edits.error.too_many`` (moving many nodes to the record level counts every new record).

Public additions to §12.5: ``EditSession.ensure_store()``, ``commit_step(step, *, kind)``, ``lock`` (the command
RLock), ``settings``, ``warnings`` (store notices such as the beside-folder fallback), ``key_index_ready``,
``cancel_token`` and ``interrupt()`` (thread-safe, non-blocking: a command waiting for the record-key index stops
with ``Cancelled`` and the index build stops; ``close()`` calls it first, so closing from another thread never waits
for a build); :class:`KeyIndex` ``start()``, ``wait(cancel)`` (also honours the session's token), ``find(key)``
(waits), ``view()`` (the immutable per-state view installed as ``doc.key_index``: ``gen``, ``find``,
``positions(key, ignore)``), ``ready``, ``started``, ``gen``, ``update(state, changes)``, ``reset(state)``,
``interrupt()``, ``close()``; :func:`key_hash`.

Private helpers of other modules used deliberately: ``editops._Kids`` (lazy child access of one container, the
same machinery ``editops.locate`` uses), ``dupes._Runs`` (spilled sorted runs, shared with the duplicate finder),
``RecordSet._is_base_of``.
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import mmap
import operator
import os
import re
import secrets
import threading
import weakref
from array import array
from contextlib import contextmanager
from itertools import compress, islice
from operator import lt
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, NamedTuple, Sequence

from . import codec, editops
from .appdirs import work_dir
from .editstore import EditStore, LayerLock, Step, StepInfo, apply_step, find_pending, layer_key, step_label, undo_step
from .errors import EditError, JsonReshapeError
from .fileio import long_path, open_shared, read_range
from .i18n import t
from .overlay import (
    EDIT_BASE,
    RECORDS_MAX,
    ChangeSet,
    EntryBlocks,
    NodeChange,
    OverlayState,
    Remap,
    invert_node_changes,
    split_edit_offset,
    write_array_layer,
)
from .paths import canonical_index, format_path
from .reader import Document, split_member
from .recordset import RecordSet
from .settings import Settings, get_settings
from .tasks import BackgroundTask, CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "DELS_RUNS_MAX",
    "KEYS_MAX",
    "MAX_ITEMS",
    "EditSession",
    "KeyIndex",
    "Ref",
    "key_hash",
]

# ----------------------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------------------
MAX_ITEMS = RECORDS_MAX            # nodes / records / units of one synchronous command (edits.error.too_many)
DELS_RUNS_MAX = 65_536             # delete_records: more runs of consecutive deleted rids -> a "pieces" remap
KEYS_MAX = 1_000_000               # Step.keys of deleted member records beyond this -> None
KEY_TABLE_MEMORY = 64 << 20        # KeyIndex: snapshot tables up to this size stay in memory, bigger ones are mapped
KEY_ENTRY_BYTES = 60               # KeyIndex build: estimated memory of one (hash << 64 | rid) entry in a list
KEY_SPILL_MIN = 1 << 16            # KeyIndex build: a spilled run holds at least this many entries
_CHUNK = 1 << 16                   # positions handled per chunk (cancel checks)
_CONTAINERS = ("object", "array")
_MASK64 = (1 << 64) - 1
_MISSING: Any = object()
_DELETE: Any = object()

_KEY_RAW = re.compile(rb'"([^"\\]*)"')     # an unescaped member key token at the start of a member record


# ----------------------------------------------------------------------------------
# references and small helpers
# ----------------------------------------------------------------------------------
class Ref(NamedTuple):
    """A node of the edited document: ``record`` (position; ``None`` = the wrapper of a wrapped document; single
    documents use 0), ``path`` (relative to the record value; absolute for the wrapper) and ``ipath`` (child
    indices along ``path`` when known)."""

    record: int | None
    path: tuple = ()
    ipath: tuple | None = None


def _as_ref(ref: Any) -> Ref:
    if isinstance(ref, Ref):
        r = ref
    elif isinstance(ref, tuple) and 1 <= len(ref) <= 3:
        r = Ref(*ref)
    else:
        raise TypeError(f"expected a Ref (record, path, ipath), got {type(ref).__name__}")
    record = r.record
    if record is not None and (isinstance(record, bool) or not isinstance(record, int)):
        raise TypeError(f"Ref.record must be an int or None, got {type(record).__name__}")
    return Ref(record, tuple(r.path or ()), None if r.ipath is None else tuple(r.ipath))


def _ref_list(refs: Any) -> list[Ref]:
    """``refs``: one ref or an iterable of refs."""
    if isinstance(refs, Ref):
        return [refs]
    if isinstance(refs, tuple) and 1 <= len(refs) <= 3 and (refs[0] is None or (
            isinstance(refs[0], int) and not isinstance(refs[0], bool))):
        return [_as_ref(refs)]
    return [_as_ref(r) for r in refs]


def _index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            return operator.index(value)
        except TypeError:
            raise TypeError(f"expected an integer index, got {type(value).__name__}") from None
    return value


def _path_text(path: Sequence[Any]) -> str:
    path = tuple(path)
    if not path:
        return "$"
    try:
        return format_path(path)
    except (TypeError, ValueError):  # pragma: no cover - odd segment types
        return repr(path)


def _ref_text(record: int | None, path: Sequence[Any]) -> str:
    if record is None:
        return _path_text(path)
    return f"#{record}" if not path else f"#{record} {_path_text(path)}"


def _not_found(record: int | None, path: Sequence[Any]) -> EditError:
    return EditError("edits.error.path_not_found", path=_ref_text(record, path))


def _huge(size: int, limit: int) -> EditError:
    return EditError("edits.error.huge_record", size=format_bytes(size), limit=format_bytes(limit))


def _too_many(count: int) -> EditError:
    return EditError("edits.error.too_many", count=count, limit=MAX_ITEMS)


def _seg_eq(a: Any, b: Any) -> bool:
    """Path segments equal (an int index equals its canonical decimal text)."""
    if type(a) is type(b):
        return a == b
    if type(a) is int and type(b) is str:
        return canonical_index(b) == a
    if type(a) is str and type(b) is int:
        return canonical_index(a) == b
    return False


def _pairs(data: bytes) -> array:
    a = array("Q")
    a.frombytes(data)
    return a


def _ascending(a: Sequence[int]) -> bool:
    return len(a) < 2 or all(map(lt, a, islice(a, 1, None)))


def _kids(buf: bytes, loc: editops.Loc) -> Any:
    return editops._Kids(buf, loc)


def _count(buf: bytes, loc: editops.Loc) -> int:
    return editops._Kids(buf, loc).n


def _is_prefix(prefix: Sequence[int], seq: Sequence[int]) -> bool:
    return len(prefix) <= len(seq) and list(seq[:len(prefix)]) == list(prefix)


def _hole_walk(buf: bytes, path: Sequence[Any]) -> tuple[editops.Loc, list[int]] | None:
    """The node at ``path`` with FIRST-occurrence semantics for duplicate keys (the scanner's target rule)."""
    cur = editops.locate(buf, ())
    ipath: list[int] = []
    for seg in path:
        if cur.kind not in _CONTAINERS:
            return None
        kids = _kids(buf, cur)
        if kids.is_object:
            i = kids.find_key(seg, last=False) if type(seg) is str else None
        else:
            i = seg if type(seg) is int else (canonical_index(seg) if type(seg) is str else None)
            if i is not None and not 0 <= i < kids.n:
                i = None
        if i is None:
            return None
        cur = kids.loc(i)
        ipath.append(i)
    return cur, ipath


# ----------------------------------------------------------------------------------
# persistent map (KeyIndex delta)
# ----------------------------------------------------------------------------------
class _PMap:
    """An immutable int-keyed mapping; :meth:`with_changes` copies only the touched buckets (``key >> shift``)."""

    __slots__ = ("_b", "_n", "_shift")

    def __init__(self, shift: int, buckets: dict[int, dict] | None = None, n: int = 0) -> None:
        self._shift = shift
        self._b: dict[int, dict] = buckets if buckets is not None else {}
        self._n = n

    def __len__(self) -> int:
        return self._n

    def get(self, key: int, default: Any = None) -> Any:
        b = self._b.get(key >> self._shift)
        if b is None:
            return default
        return b.get(key, default)

    def items(self) -> Iterator[tuple[int, Any]]:
        for b in self._b.values():
            yield from b.items()

    def with_changes(self, changes: Mapping[int, Any]) -> _PMap:
        """A new map with ``changes`` applied (the value :data:`_DELETE` removes a key)."""
        if not changes:
            return self
        buckets = dict(self._b)
        copied: set[int] = set()
        n = self._n
        for k, v in changes.items():
            bid = k >> self._shift
            if bid not in copied:
                old = buckets.get(bid)
                buckets[bid] = dict(old) if old is not None else {}
                copied.add(bid)
            b = buckets[bid]
            if v is _DELETE:
                if k in b:
                    del b[k]
                    n -= 1
            else:
                if k not in b:
                    n += 1
                b[k] = v
        for bid in copied:
            if not buckets[bid]:
                del buckets[bid]
        return _PMap(self._shift, buckets, n)


# ----------------------------------------------------------------------------------
# KeyIndex
# ----------------------------------------------------------------------------------
def key_hash(key: str) -> int:
    """64-bit blake2b hash of a decoded record key (its UTF-8 bytes, lone surrogates passed through)."""
    return int.from_bytes(hashlib.blake2b(key.encode("utf-8", "surrogatepass"), digest_size=8).digest(), "little")


def _raw_key_hash(raw: bytes) -> int:
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "little")


def _member_key(data: bytes, s: int, e: int) -> str:
    """The decoded key of the member record ``data[s:e]``."""
    m = _KEY_RAW.match(data, s, e)
    if m is not None:
        return m.group(1).decode("utf-8")
    key, _ = split_member(data[s:e])
    return key


def _member_hash(data: bytes, s: int, e: int) -> int:
    """:func:`key_hash` of the key of the member record ``data[s:e]`` (unescaped keys: their raw bytes ARE the
    UTF-8 of the decoded key, so they are hashed without decoding)."""
    m = _KEY_RAW.match(data, s, e)
    if m is not None:
        return _raw_key_hash(m.group(1))
    key, _ = split_member(data[s:e])
    return key_hash(key)


class _KeyTable:
    """The snapshot of a KeyIndex: ``(hash, rid)`` pairs sorted by hash (two arrays in memory, or a mapped file
    of interleaved u64 pairs read through strided memoryviews)."""

    __slots__ = ("count", "hashes", "path", "rids", "_f", "_mm", "_views")

    def __init__(self, hashes: Any, rids: Any, count: int, *, mm: Any = None, f: Any = None,
                 path: Path | None = None, views: Sequence[memoryview] = ()) -> None:
        self.hashes = hashes
        self.rids = rids
        self.count = count
        self._mm = mm
        self._f = f
        self.path = path
        self._views = tuple(views)

    @classmethod
    def build(cls, entries: Iterable[int], count: int, directory: Path, cancel: CancelToken | None) -> _KeyTable:
        """From sorted ``(hash << 64) | rid`` ints (``count`` of them)."""
        if count * 16 <= KEY_TABLE_MEMORY:
            hashes, rids = array("Q"), array("Q")
            n = 0
            for e in entries:
                hashes.append(e >> 64)
                rids.append(e & _MASK64)
                n += 1
                if not n & 0xFFFF:
                    check(cancel)
            return cls(hashes, rids, n)
        os.makedirs(long_path(directory), exist_ok=True)
        path = directory / f"keyindex-{os.getpid()}-{secrets.token_hex(4)}.bin"
        f = mm = None
        try:
            n = 0
            with open_shared(path, "wb") as out:
                buf = array("Q")
                for e in entries:
                    buf.append(e >> 64)
                    buf.append(e & _MASK64)
                    n += 1
                    if len(buf) >= 1 << 17:
                        check(cancel)
                        out.write(buf.tobytes())
                        buf = array("Q")
                if buf:
                    out.write(buf.tobytes())
            if not n:
                _remove(path)
                return cls(array("Q"), array("Q"), 0)
            f = open_shared(path, "rb")
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            mv = memoryview(mm).cast("Q")
            hashes, rids = mv[0::2], mv[1::2]
            return cls(hashes, rids, n, mm=mm, f=f, path=path, views=(hashes, rids, mv))
        except BaseException:
            if mm is not None:
                mm.close()
            if f is not None:
                f.close()
            _remove(path)
            raise

    def rids_for(self, h: int) -> list[int]:
        hashes = self.hashes
        n = self.count
        i = bisect.bisect_left(hashes, h, 0, n)
        out = []
        while i < n and hashes[i] == h:
            out.append(int(self.rids[i]))
            i += 1
        return out

    def close(self) -> None:
        views, self._views = self._views, ()
        for v in views:
            try:
                v.release()
            except (ValueError, BufferError):  # pragma: no cover - a reader still holds a slice
                pass
        mm, self._mm = self._mm, None
        if mm is not None:
            try:
                mm.close()
            except BufferError:  # pragma: no cover
                log.debug("key table mapping still in use", exc_info=True)
        f, self._f = self._f, None
        if f is not None:
            f.close()
        path, self.path = self.path, None
        if path is not None:
            _remove(path)
        self.hashes = self.rids = array("Q")
        self.count = 0


def _remove(path: Path) -> None:
    try:
        os.remove(long_path(path))
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover - best effort (leftovers live in the work folder)
        log.debug("could not remove %s", path, exc_info=True)


class _KeyView:
    """Key lookups for ONE state (immutable; installed as ``doc.key_index``: the reader uses it only for the
    state whose gen it carries)."""

    __slots__ = ("_delta", "_dhash", "_doc", "_table", "gen", "state")

    def __init__(self, doc: Document, state: OverlayState, table: _KeyTable, delta: _PMap, dhash: _PMap) -> None:
        self._doc = doc
        self.state = state
        self.gen = state.gen
        self._table = table
        self._delta = delta
        self._dhash = dhash

    def _candidates(self, key: str, h: int) -> list[int]:
        delta = self._delta
        out = [rid for rid in self._table.rids_for(h) if delta.get(rid, _MISSING) is _MISSING]
        for rid in self._dhash.get(h, ()):
            if delta.get(rid) == key:
                out.append(rid)
        return out

    def positions(self, key: str, *, ignore: Iterable[int] = ()) -> list[int]:
        """Positions of the records whose key is ``key`` (confirmed by decoding their current key)."""
        if not isinstance(key, str):
            return []
        skip = set(ignore)
        pieces = self.state.pieces
        view = None
        out = []
        for rid in self._candidates(key, key_hash(key)):
            if rid in skip:
                continue
            pos = pieces.pos_of(rid)
            if pos is None:
                continue
            if view is None:
                view = self._doc.pinned(self.state)
            try:
                current = view.record_key(pos)
            except ValueError:   # not a member (cannot happen for a member-record document)
                continue
            if current == key:
                out.append(pos)
        out.sort()
        return out

    def find(self, key: str) -> int | None:
        """Position of the LAST record with ``key`` (dict semantics), or ``None``."""
        ps = self.positions(key)
        return ps[-1] if ps else None

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and bool(self.positions(key))


class _TakenKeys:
    """``key in taken``: a record of the state has ``key`` (rids in ``ignore`` excepted) or it is in ``extra``
    (keys given out by the running command) -- the container ``editops.free_key`` probes."""

    __slots__ = ("extra", "ignore", "view")

    def __init__(self, view: _KeyView, extra: set[str] | None = None, ignore: Iterable[int] = ()) -> None:
        self.view = view
        self.extra: set[str] = set() if extra is None else extra
        self.ignore = frozenset(ignore)

    def __contains__(self, key: object) -> bool:
        if key in self.extra:
            return True
        return isinstance(key, str) and bool(self.view.positions(key, ignore=self.ignore))


class KeyIndex:
    """Record keys of a member-record document (keyed / wrapped object) for the edit session (§12.5).

    * The snapshot: when first needed (:meth:`start` / :meth:`wait`), a background thread reads the effective
      records of the session's state at that moment (``doc.pinned(state).iter_blocks()``), hashes every key
      (:func:`key_hash`, blake2b-64 of the decoded key) with its rid and sorts the ``(hash << 64) | rid`` entries
      (sorted runs spilled to ``appdirs.work_dir`` beyond ``memory_limit_bytes // 4``, merged with ``heapq.merge``
      -- the duplicate finder's machinery). Tables up to :data:`KEY_TABLE_MEMORY` stay in memory, bigger ones are a
      mapped temporary file (deleted by :meth:`close`).
    * The delta: from the snapshot's state on, every transition (commit, undo, redo) gives ``rid -> current key``
      (``None`` = the record has no key in that state); a persistent map rid -> key plus hash -> rids, so every state
      has an immutable view (``gen``, ``find``) at the cost of the touched buckets only.
    * ``find(key)``: candidates from the snapshot (rids the delta does not override) and the delta; a candidate
      counts when its rid is alive in the state and its CURRENT key -- decoded from the record -- is ``key`` (hash
      collisions and untracked changes cannot produce a wrong answer, only a missed one); the highest position wins.

    The view of the session's current state is installed as ``doc.key_index`` when the snapshot is ready (the
    reader uses it only while its gen matches the state it reads)."""

    def __init__(self, doc: Document, settings: Settings, state: OverlayState, *,
                 cancel: CancelToken | None = None) -> None:
        self._doc = doc
        self._settings = settings
        self._cond = threading.Condition(threading.RLock())
        self._state = state
        self._epoch = 0
        self._task: BackgroundTask | None = None
        self._table: _KeyTable | None = None
        self._retired: list[_KeyTable] = []
        self._error: BaseException | None = None
        self._delta = _PMap(12)
        self._dhash = _PMap(52)
        self._view: _KeyView | None = None
        self._cancel = cancel           # the session's token: honoured by every wait (interrupt())
        self.closed = False

    def __repr__(self) -> str:
        return f"<KeyIndex {'ready' if self.ready else 'building' if self.started else 'idle'} gen={self.gen}>"

    @property
    def started(self) -> bool:
        """A snapshot is being built or ready (transitions are tracked)."""
        return self._task is not None

    @property
    def ready(self) -> bool:
        return self._view is not None

    @property
    def gen(self) -> int:
        """Gen of the state the index describes now."""
        return self._state.gen

    def view(self) -> _KeyView | None:
        """The lookup view of the current state (``None`` while the snapshot is not ready)."""
        return self._view

    # -- lifecycle ----------------------------------------------------------------------------
    def start(self, progress: Progress | None = None) -> None:
        """Start building the snapshot of the current state in the background (idempotent)."""
        with self._cond:
            self._start_locked(progress)

    def _start_locked(self, progress: Progress | None = None) -> None:
        if self.closed:
            raise EditError("edits.error.closed")
        if self._task is not None:
            return
        epoch = self._epoch
        state = self._state
        self._delta = _PMap(12)
        self._dhash = _PMap(52)
        self._error = None
        task = BackgroundTask(self._build, state, name="key-index", progress=progress)
        task.on_done = lambda tk, ep=epoch: self._finished(tk, ep)
        self._task = task
        task.start()

    def wait(self, cancel: CancelToken | None = None) -> _KeyView:
        """Block until the snapshot is ready (starting it when needed); the view of the current state. The build's
        error is raised (and the next call tries again). ``cancel`` and the session's token (:meth:`interrupt`)
        stop the wait with :class:`~jsonreshape.errors.Cancelled`."""
        with self._cond:
            while True:
                if self.closed:
                    raise EditError("edits.error.closed")
                if self._view is not None:
                    return self._view
                check(self._cancel)
                check(cancel)
                if self._task is None:
                    err, self._error = self._error, None
                    if err is not None:
                        raise err
                    self._start_locked()
                self._cond.wait(0.05)

    def interrupt(self) -> None:
        """Stop a running build and wake every waiter (thread-safe, never blocks): used when the session is
        being closed while a command waits for the snapshot (the waiters see the session's cancelled token)."""
        with self._cond:
            task = self._task
            if task is not None and self._view is None:
                self._epoch += 1            # the cancelled build's result is dropped
                self._task = None
                task.cancel()
            self._cond.notify_all()

    def find(self, key: str) -> int | None:
        """Position of the last record with ``key`` in the current state (waits for the snapshot)."""
        return self.wait().find(key)

    def _finished(self, task: BackgroundTask, epoch: int) -> None:
        with self._cond:
            stale = epoch != self._epoch or self.closed
            if task.exception is not None:
                if not stale:
                    self._error = task.exception
                    self._task = None
                self._cond.notify_all()
                return
            try:
                table = task.result(0)
            except BaseException:  # pragma: no cover - exception handled above
                table = None
            if stale:
                if table is not None:
                    table.close()
                return
            self._table = table
            self._refresh_locked()
            self._cond.notify_all()

    def _refresh_locked(self) -> None:
        if self._table is None or self.closed:
            return
        view = _KeyView(self._doc, self._state, self._table, self._delta, self._dhash)
        self._view = view
        self._doc.key_index = view

    def update(self, state: OverlayState, changes: Mapping[int, str | None] | None) -> None:
        """The session moved to ``state``; ``changes`` = rid -> current key of the rids whose key may have changed
        (``None`` value: no key / not alive); ``changes=None``: unknown -> the snapshot is dropped (rebuilt at the
        next need)."""
        with self._cond:
            self._state = state
            if self.closed:
                return
            if changes is None:
                self._reset_locked()
                return
            if self._task is None:
                return
            if changes:
                delta, dhash = self._delta, self._dhash
                dnew: dict[int, Any] = {}
                hnew: dict[int, set[int]] = {}

                def hset(h: int) -> set[int]:
                    s = hnew.get(h)
                    if s is None:
                        s = hnew[h] = set(dhash.get(h, ()))
                    return s

                for rid, key in changes.items():
                    old = dnew[rid] if rid in dnew else delta.get(rid, _MISSING)
                    if isinstance(old, str):
                        hset(key_hash(old)).discard(rid)
                    if isinstance(key, str):
                        hset(key_hash(key)).add(rid)
                    dnew[rid] = key
                self._delta = delta.with_changes(dnew)
                self._dhash = dhash.with_changes({h: (tuple(sorted(s)) if s else _DELETE) for h, s in hnew.items()})
            self._refresh_locked()

    def reset(self, state: OverlayState) -> None:
        """Forget the snapshot (discard / restore): the next need builds it again from ``state``."""
        with self._cond:
            self._state = state
            self._reset_locked()

    def _reset_locked(self) -> None:
        self._epoch += 1
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        table, self._table = self._table, None
        if table is not None:
            self._retired.append(table)      # a lookup in another thread may still use it: closed by close()
        self._error = None
        self._view = None
        self._delta = _PMap(12)
        self._dhash = _PMap(52)
        if isinstance(getattr(self._doc, "key_index", None), _KeyView):
            self._doc.key_index = None
        self._cond.notify_all()

    def close(self, timeout: float = 5.0) -> None:
        """Stop a running build, drop the snapshot (temporary file removed) and the document's hook."""
        with self._cond:
            if self.closed:
                return
            self.closed = True
            self._epoch += 1
            task, self._task = self._task, None
            tables = self._retired + ([self._table] if self._table is not None else [])
            self._retired = []
            self._table = None
            self._view = None
            if isinstance(getattr(self._doc, "key_index", None), _KeyView):
                self._doc.key_index = None
            self._cond.notify_all()
        if task is not None:
            task.cancel()
            task.join(timeout)
        for table in tables:
            table.close()

    # -- the build (background thread) --------------------------------------------------------
    def _build(self, s0: OverlayState, *, progress: Progress, cancel: CancelToken) -> _KeyTable:
        from .dupes import _Runs   # lazy: the duplicate finder's spilled runs

        doc = self._doc
        view = doc.pinned(s0)
        if progress is not None and not progress.message:
            progress.set_message(t("edits.progress.keys"))
        directory = work_dir(self._settings.cache_dir)
        limit = max(KEY_SPILL_MIN, (int(self._settings.memory_limit_bytes) // 4) // KEY_ENTRY_BYTES)
        runs = _Runs(directory, cancel)
        entries: list[int] = []
        total = 0
        try:
            for blk in view.iter_blocks(None, progress=progress, cancel=cancel):
                idx = blk.indices
                rids = s0.rids_at(idx)
                if blk.huge:
                    key = view.record_key(int(idx[0]))
                    entries.append((key_hash(key or "") << 64) | int(rids[0]))
                else:
                    data = blk.data
                    sp = _pairs(blk.spans)
                    app = entries.append
                    for k in range(len(idx)):
                        app((_member_hash(data, sp[2 * k], sp[2 * k + 1]) << 64) | rids[k])
                total += len(idx)
                if len(entries) >= limit:
                    runs.spill(entries)
                    entries = []
            merged = runs.merged(entries)
            entries = []
            try:
                return _KeyTable.build(merged, total, directory, cancel)
            finally:
                merged.close()
        finally:
            runs.cleanup()


# ----------------------------------------------------------------------------------
# the edit engine: hosts (a record's value buffer, or the wrapper text) and targets
# ----------------------------------------------------------------------------------
class _Target:
    """A resolved node of a host; ``path``/``ipath`` are kept valid through the splices of the command."""

    __slots__ = ("dead", "host", "ipath", "path")

    def __init__(self, host: _Host, path: Sequence[Any], ipath: Sequence[int]) -> None:
        self.host = host
        self.path = list(path)
        self.ipath = list(ipath)
        self.dead = False

    @property
    def whole(self) -> bool:
        """The whole record (the root of a record host)."""
        return self.host.record is not None and not self.ipath

    def order(self) -> tuple:
        """Document order: the wrapper first, then records by position, then by ipath."""
        return (self.host.order, tuple(self.ipath))

    def snapshot(self) -> list:
        """``[record, path, ipath]`` in the coordinates BEFORE the step (JSON-able)."""
        return [self.host.record, list(self.path), list(self.ipath)]


class _Host:
    """The bytes of one record (its value buffer; member records keep their head) or of the wrapper W. A record's
    bytes are read on first use (:attr:`buf`): whole-record commands (delete, record moves, value duplicates) never
    read them, so they also work on huge records."""

    def __init__(self, ed: _Edit, *, record: int | None, rid: int | None, buf: bytes | None, base_level: int) -> None:
        # a weak reference: a cycle ed <-> host would keep the command's pinned state (and with it the files of
        # a discarded / truncated layer, see editstore) alive until the cyclic garbage collector runs
        self.ed = ed if isinstance(ed, weakref.ProxyTypes) else weakref.proxy(ed)
        self.record = record
        self.rid = rid
        self.head = b""
        self._key: str | None = None
        self.mstart = 0
        self.kend = 0
        self.new_key: str | None = None
        self._buf = buf
        self.orig = buf
        self.base_level = base_level
        self.targets: list[_Target] = []
        self.nodes: list[tuple[tuple[int, ...], Remap, int]] = []
        self.removed = False
        self.hole: _Target | None = None        # wrapper: the record container's placeholder
        self.hole_path: tuple = ()
        self.placeholder = b""

    @property
    def is_wrapper(self) -> bool:
        return self.record is None

    @property
    def order(self) -> int:
        return -1 if self.record is None else self.record

    @property
    def loaded(self) -> bool:
        return self._buf is not None

    @property
    def buf(self) -> bytes:
        if self._buf is None:
            self.ed.load(self)
        assert self._buf is not None
        return self._buf

    @buf.setter
    def buf(self, value: bytes) -> None:
        self._buf = value

    @property
    def key(self) -> str | None:
        """The record key of a member record (read with the record's bytes)."""
        if self._buf is None and self.record is not None and self.ed.member:
            self.ed.load(self)
        return self._key

    @property
    def changed(self) -> bool:
        return self.new_key is not None or (self._buf is not None and self._buf != self.orig)

    def unit(self) -> bytes:
        head = self.head
        if self.new_key is not None:
            head = head[:self.mstart] + editops.encode_key(self.new_key) + head[self.kend:]
        return head + self.buf

    # -- targets ----------------------------------------------------------------------------
    def target(self, path: Sequence[Any], ipath: Sequence[int]) -> _Target:
        tg = _Target(self, path, ipath)
        self.targets.append(tg)
        return tg

    def walk(self, path: Sequence[Any], ipath: Sequence[int] | None) -> _Target:
        """Resolve ``path`` (dict semantics, or the complete ``ipath`` verified against it) to a target."""
        if not path:
            if ipath:
                raise _not_found(self.record, path)
            return self.target([], [])
        buf = self.buf
        cur = editops.locate(buf, ())
        exact = ipath is not None and len(ipath) == len(path)
        out_p: list[Any] = []
        out_i: list[int] = []
        for level, seg in enumerate(path):
            if cur.kind not in _CONTAINERS:
                raise _not_found(self.record, path)
            kids = _kids(buf, cur)
            if exact:
                i = ipath[level]    # type: ignore[index]
                if type(i) is not int or not 0 <= i < kids.n or not kids.matches(i, seg):
                    raise _not_found(self.record, path)
            else:
                found = kids.find(seg)
                if found is None:
                    raise _not_found(self.record, path)
                i = found
            cur = kids.loc(i)
            out_p.append(cur.key)
            out_i.append(i)
        return self.target(out_p, out_i)

    def locate(self, tg: _Target) -> editops.Loc:
        return editops.locate(self.buf, tuple(tg.path), tuple(tg.ipath))

    def _remap(self, c_ipath: list[int], remap: Remap, is_array: bool) -> None:
        k = len(c_ipath)
        for tg in self.targets:
            if tg.dead or len(tg.ipath) <= k or tg.ipath[:k] != c_ipath:
                continue
            j = remap.map(tg.ipath[k])
            if j is None:
                tg.dead = True
                continue
            tg.ipath[k] = j
            if is_array:
                tg.path[k] = j

    def _note(self, c: _Target, remap: Remap, count: int, is_array: bool) -> None:
        if remap.identity:
            return
        cip = list(c.ipath)
        self.nodes.append((tuple(cip), remap, count))
        self._remap(cip, remap, is_array)

    # -- wrapper rules (§12.5) ---------------------------------------------------------------------
    def check_node(self, tg: _Target) -> None:
        """The wrapper's target must not be the record container or one of its ancestors."""
        if self.hole is None:
            return
        if _is_prefix(tg.ipath, self.hole.ipath):
            raise self.ed.locked()

    def check_children(self, c: _Target, kind: str, removed: Iterable[int] = (), insert_at: int | None = None) -> None:
        """Children of the wrapper's container ``c`` change: refused for the record container itself and, in an
        ancestor ARRAY, at an index <= the container's index."""
        if self.hole is None:
            return
        hip = self.hole.ipath
        k = len(c.ipath)
        if c.ipath == hip:
            raise self.ed.locked()
        if k < len(hip) and hip[:k] == c.ipath and kind == "array":
            h = hip[k]
            rem = sorted(removed)
            before = bisect.bisect_left(rem, h)
            if before or (insert_at is not None and insert_at <= h - before):
                raise self.ed.locked()

    # -- operations ----------------------------------------------------------------------------
    def op_set(self, tg: _Target, value: bytes) -> None:
        loc = self.locate(tg)
        n_old = _count(self.buf, loc) if loc.kind in _CONTAINERS else 0
        self.buf = editops.set_value(self.buf, loc, value, self.ed.style, base_level=self.base_level)
        new = self.locate(tg)
        n_new = _count(self.buf, new) if new.kind in _CONTAINERS else 0
        if n_old or n_new:
            self._note(tg, Remap([("del", 0, n_old), ("ins", 0, n_new)]), n_new, loc.kind == "array")

    def op_rename(self, tg: _Target, key: str) -> None:
        loc = self.locate(tg)
        self.buf = editops.rename_key(self.buf, loc, key)
        k = len(tg.ipath) - 1
        ip = list(tg.ipath)
        for u in self.targets:
            if not u.dead and len(u.ipath) > k and u.ipath[:k + 1] == ip:
                u.path[k] = key

    def op_insert(self, c: _Target, index: int, value: bytes, key: str | None, *, verbatim: bool = False) -> None:
        loc = self.locate(c)
        n = _count(self.buf, loc)
        self.buf = editops.insert_child(self.buf, loc, index, value, self.ed.style, key=key,
                                        base_level=self.base_level, verbatim=verbatim)
        self._note(c, Remap([("ins", index, 1)]), n + 1, loc.kind == "array")

    def op_delete(self, c: _Target, indices: Sequence[int]) -> None:
        loc = self.locate(c)
        n = _count(self.buf, loc)
        d = sorted(set(indices))
        if not d:
            return
        self.buf = editops.delete_children(self.buf, loc, d)
        op = ("del", d[0], len(d)) if d[-1] - d[0] + 1 == len(d) else ("dels", array("Q", d))
        self._note(c, Remap([op]), n - len(d), loc.kind == "array")

    def op_place(self, c: _Target, own: list[int], to: int, items: list[tuple[int | None, bytes, Any, Any]]) -> None:
        """Container ``c`` takes its children ``own`` out and puts ``items`` (``(own index | None, value, key,
        original key)`` in final order) at ``to`` (coordinates without ``own``)."""
        loc = self.locate(c)
        n = _count(self.buf, loc)
        is_array = loc.kind == "array"
        style = self.ed.style
        externals = [j for j, it in enumerate(items) if it[0] is None]
        if len(items) == 1 and not externals and (is_array or items[0][2] == items[0][3]):
            self.buf = editops.move_child(self.buf, loc, own[0], to, style, base_level=self.base_level)
        else:
            if own:
                self.buf = editops.delete_children(self.buf, loc, own)
            for j, (oi, value, key, _orig) in enumerate(items):
                cur = self.locate(c)
                self.buf = editops.insert_child(self.buf, cur, to + j, value, style, key=None if is_array else key,
                                                base_level=self.base_level, verbatim=oi is not None)
        ops: list[tuple] = []
        if own:
            ops.append(("move", array("Q", own), to))
        if externals:
            ops.append(("inss", array("Q", [to + j for j in externals])))
        self._note(c, Remap(ops), n - len(own) + len(items), is_array)


class _Edit:
    """One command being built: the hosts it touched, the record structure, new records, keys, selection."""

    def __init__(self, session: EditSession) -> None:
        self.s = session
        self.doc = session.doc
        self.state = session.state
        self.view = session.doc.pinned(self.state)
        self.style = session.style
        self.member = bool(session.doc.member_records)
        self.large = int(session.doc.settings.large_node_bytes)
        self.single = session.doc.kind == "single"
        self.hosts: dict[Any, _Host] = {}
        self.struct: list[tuple] = []
        self.new_rids: list[tuple[int, int, int]] = []
        self.new_units: list[tuple[int, bytes]] = []
        self.new_spans: list[tuple[int, tuple[int, int]]] = []
        self.keys: list[tuple[int, str | None, str | None]] | None = []
        self.next_rid = self.state.next_rid
        self.push_layer: str | None = None
        self.segments: list[int] = []
        self.layer_flags = 0
        self.select: Any = None          # a _Target, or a ready (record after, path, ipath) tuple
        self.before: Any = None          # [record, path, ipath] before the step

    # -- errors ----------------------------------------------------------------------------------
    def locked(self) -> EditError:
        return EditError("edits.error.container_locked", path=_path_text(self.doc.record_path or ()))

    # -- hosts -----------------------------------------------------------------------------------
    def record_host(self, pos: int) -> _Host:
        """The host of the record at ``pos`` (its bytes are read on first use: :meth:`load`)."""
        h = self.hosts.get(("r", pos))
        if h is not None:
            return h
        st = self.state
        if not 0 <= pos < st.count:
            raise _not_found(pos, ())
        h = _Host(self, record=pos, rid=st.rid_at(pos), buf=None, base_level=self.style.record_level)
        self.hosts[("r", pos)] = h
        return h

    def load(self, h: _Host) -> None:
        """Read a record host's bytes (a record over ``large_node_bytes`` -> ``edits.error.huge_record``)."""
        pos = h.record
        assert pos is not None
        s, e = self.view.record_span(pos)
        if e - s > self.large:
            raise _huge(e - s, self.large)
        buf = self.view.record_bytes(pos)
        if self.member:
            ml = editops.member_loc(buf)
            h.head = buf[:ml.start]
            h._key = ml.key   # type: ignore[assignment]
            h.mstart = ml.mstart or 0
            h.kend = ml.kend or 0
            buf = buf[ml.start:]
        h._buf = buf
        h.orig = buf

    def wrapper_host(self) -> _Host:
        h = self.hosts.get(("w",))
        if h is not None:
            return h
        doc = self.doc
        if doc.kind != "wrapped":
            raise _not_found(None, ())
        st = self.state
        rp = tuple(doc.record_path or ())
        placeholder = b"{}" if self.member else b"[]"
        if st.wrapper is not None and st.wrapper_hole is not None:
            ws, we = st.wrapper
            if we - ws > self.large:
                raise _huge(we - ws, self.large)
            buf = st.read(ws, we)
            hole_rel = (st.wrapper_hole[0] - ws, st.wrapper_hole[1] - ws)
        else:
            span = doc.container_span
            if span is None:   # pragma: no cover - a document without a closed container has an error
                raise EditError("edits.error.partial")
            base = doc.pinned(None)
            root = base.root()
            cs, ce = span
            size = (cs - root.start) + len(placeholder) + (root.end - ce)
            if size > self.large:
                raise _huge(size, self.large)
            with base.open_data() as f:
                prefix = read_range(f, root.start, cs)
                suffix = read_range(f, ce, root.end)
            buf = prefix + placeholder + suffix
            hole_rel = (cs - root.start, cs - root.start + len(placeholder))
        found = _hole_walk(buf, rp)
        if found is None or (found[0].start, found[0].end) != hole_rel or buf[hole_rel[0]:hole_rel[1]] != placeholder:
            log.error("wrapper text: the record container placeholder is not at %r", hole_rel)
            raise EditError("edits.error.internal", detail=t("edits.detail.shape"))
        loc, hip = found
        h = _Host(self, record=None, rid=None, buf=buf, base_level=0)
        h.hole_path = rp
        h.placeholder = placeholder
        h.hole = h.target(list(rp), hip)
        self.hosts[("w",)] = h
        return h

    def resolve(self, ref: Ref) -> _Target:
        record, path, ipath = ref
        if record is None and self.single:
            record = 0
        if record is None:
            host = self.wrapper_host()
            rp = host.hole_path
            if len(path) >= len(rp) and all(_seg_eq(a, b) for a, b in zip(path, rp)):
                raise self.locked()
        else:
            if not 0 <= record < self.state.count:
                raise _not_found(record, path)
            host = self.record_host(record)
        return host.walk(path, ipath)

    def resolve_all(self, refs: Any) -> list[_Target]:
        refs = _ref_list(refs)
        if len(refs) > MAX_ITEMS:
            raise _too_many(len(refs))
        targets = [self.resolve(r) for r in refs]
        return _normalize(targets)

    def alloc(self, n: int) -> int:
        lo = self.next_rid
        self.next_rid += n
        return lo

    def value_of(self, tg: _Target) -> tuple[bytes, str]:
        """``(value bytes, base key)`` of a moved node / whole record (the key a member keeps, else the index or
        the record position as text)."""
        h = tg.host
        if tg.whole:
            return h.buf, (h.key if h.key is not None else str(h.record))
        loc = h.locate(tg)
        key = loc.key if isinstance(loc.key, str) else str(loc.key)
        return h.buf[loc.start:loc.end], key

    # -- record level ----------------------------------------------------------------------------
    def remove_records(self, positions: array, *, progress: Progress | None = None,
                       cancel: CancelToken | None = None) -> None:
        """Remove the records at ``positions`` (ascending, unique; coordinates of the current struct = before the
        step: removals are the first structural op of a command)."""
        k = len(positions)
        if not k:
            return
        st = self.state
        if positions[-1] - positions[0] + 1 == k:
            op: tuple = ("del", int(positions[0]), k)
        elif _rid_runs(st, positions, cancel) <= DELS_RUNS_MAX:
            op = ("dels", positions)
        else:
            op = ("pieces", st.pieces, st.pieces.delete_positions(positions))
        for h in self.hosts.values():
            if h.record is not None:
                i = bisect.bisect_left(positions, h.record)
                if i < k and positions[i] == h.record:
                    h.removed = True
        if self.member and self.keys is not None:
            if k > KEYS_MAX:
                self.keys = None
            else:
                self.keys.extend(self._deleted_keys(positions, progress, cancel))
        self.struct.append(op)

    def _deleted_keys(self, positions: array, progress: Progress | None,
                      cancel: CancelToken | None) -> list[tuple[int, str | None, None]]:
        st = self.state
        out: list[tuple[int, str | None, None]] = []
        if len(positions) <= MAX_ITEMS:
            for p in positions:
                p = int(p)
                h = self.hosts.get(("r", p))
                key = h.key if h is not None and h.loaded else self.view.record_key(p)
                out.append((st.rid_at(p), key, None))
            return out
        for blk in self.view.iter_blocks(positions, progress=progress, cancel=cancel):
            idx = blk.indices
            rids = st.rids_at(idx)
            if blk.huge:
                out.append((int(rids[0]), self.view.record_key(int(idx[0])), None))
                continue
            sp = _pairs(blk.spans)
            data = blk.data
            for k in range(len(idx)):
                out.append((int(rids[k]), _member_key(data, sp[2 * k], sp[2 * k + 1]), None))
        return out

    def duplicate_records(self, positions: array, *, progress: Progress | None = None,
                          cancel: CancelToken | None = None) -> None:
        """A copy right after each record of ``positions`` (ascending, unique, the first structural op)."""
        k = len(positions)
        if not k:
            return
        st = self.state
        final = array("Q", (int(p) + j + 1 for j, p in enumerate(positions)))
        rid_lo = self.alloc(k)
        self.struct.append(("inss", final))
        self.new_rids.append((int(final[0]), rid_lo, k))
        if not self.member:
            if k <= MAX_ITEMS:
                for j, p in enumerate(positions):
                    self.new_spans.append((rid_lo + j, self.view.record_span(int(p))))
                return
            self._span_layer(positions, rid_lo, progress, cancel)
            return
        view = self.s._keys_view()
        taken = _TakenKeys(view)
        big = k > MAX_ITEMS
        store = seg = None
        units: list[bytes] = []
        pending = 0
        starts, ends = array("Q"), array("Q")
        flags = 0
        keys: list[tuple[int, str | None, str | None]] = []
        if big:
            store = self.s.ensure_store()
            seg = store.new_segment()
            self.segments.append(seg)
        j = 0
        try:
            for blk in self.view.iter_blocks(positions, progress=progress, cancel=cancel):
                if blk.huge:
                    s, e = blk.span(0)
                    raise _huge(e - s, self.large)
                sp = _pairs(blk.spans)
                data = blk.data
                for kk in range(len(blk.indices)):
                    rec = data[sp[2 * kk]:sp[2 * kk + 1]]
                    ml = editops.member_loc(rec)
                    new_key = editops.free_key(taken, ml.key)
                    taken.extra.add(new_key)
                    unit = rec[:ml.mstart] + editops.encode_key(new_key) + rec[ml.kend:]
                    if len(unit) > self.large:
                        raise _huge(len(unit), self.large)
                    keys.append((rid_lo + j, None, new_key))
                    if big:
                        flags |= editops.check_unit(unit, member=True)
                        units.append(unit)
                        pending += len(unit)
                        if pending >= 1 << 22:
                            self._flush_units(store, seg, units, starts, ends)
                            pending = 0
                    else:
                        self.new_units.append((rid_lo + j, unit))
                    j += 1
                check(cancel)
            if big:
                self._flush_units(store, seg, units, starts, ends)
                store.sync([seg])
                rids = array("Q", range(rid_lo, rid_lo + k))
                path = store.new_layer_path()
                write_array_layer(path, EntryBlocks([(rids, starts, ends)]), flags=flags, segments=[seg], work_dir=None,
                                  cancel=cancel)
                self.push_layer = path.name
                self.layer_flags |= flags
        except BaseException:
            if big and store is not None and seg is not None:
                self.s._drop_pending(seg=seg, layer=self.push_layer)
                self.push_layer = None
            raise
        if self.keys is not None:
            self.keys.extend(keys)

    @staticmethod
    def _flush_units(store: EditStore, seg: int, units: list[bytes], starts: array, ends: array) -> None:
        if not units:
            return
        vs, _ve = store.append(b"".join(units), seg=seg)
        off = vs
        for u in units:
            starts.append(off)
            off += len(u)
            ends.append(off)
        units.clear()

    def _span_layer(self, positions: array, rid_lo: int, progress: Progress | None,
                    cancel: CancelToken | None) -> None:
        """Value documents, many copies: an array layer of the sources' current spans (no new bytes)."""
        store = self.s.ensure_store()
        k = len(positions)
        if progress is not None:
            progress.set_total(k)
        starts, ends = array("Q"), array("Q")
        segs: set[int] = set()
        view = self.view
        for a in range(0, k, _CHUNK):
            check(cancel)
            for p in positions[a:a + _CHUNK]:
                s, e = view.record_span(int(p))
                starts.append(s)
                ends.append(e)
                if s >= EDIT_BASE:
                    segs.add(split_edit_offset(s)[0])
            if progress is not None:
                progress.update(min(k, a + _CHUNK))
        rids = array("Q", range(rid_lo, rid_lo + k))
        path = store.new_layer_path()
        try:
            write_array_layer(path, EntryBlocks([(rids, starts, ends)]), flags=0, segments=sorted(segs),
                              work_dir=None, cancel=cancel)
        except BaseException:
            self.s._drop_pending(layer=path.name)
            raise
        self.push_layer = path.name

    # -- finishing ---------------------------------------------------------------------------------
    def verify_hole(self, h: _Host) -> editops.Loc:
        found = _hole_walk(h.buf, h.hole_path)
        if found is None or h.buf[found[0].start:found[0].end] != h.placeholder:
            raise self.locked()
        return found[0]

    def finish(self, label_key: str, params: Mapping[str, Any], *, kind: str = "edit") -> ChangeSet:
        s = self.s
        st = self.state
        changed = [h for h in self.hosts.values() if not h.removed and h.changed]
        if not changed and not self.struct and self.push_layer is None:
            return s._noop()
        if len(changed) + len(self.new_units) + len(self.new_spans) > MAX_ITEMS:
            raise _too_many(len(changed) + len(self.new_units) + len(self.new_spans))
        remap = Remap(self.struct) if self.struct else None

        def after(pos: int) -> int:
            if remap is None:
                return pos
            q = remap.map(pos)
            assert q is not None
            return q

        flags = 0
        units: list[bytes] = []
        owners: list[Any] = []
        holes: dict[int, editops.Loc] = {}
        for h in changed:
            if h.is_wrapper:
                unit = h.buf
                holes[id(h)] = self.verify_hole(h)
            else:
                unit = h.unit()
            if len(unit) > self.large:
                raise _huge(len(unit), self.large)
            flags |= editops.check_unit(unit, member=self.member and not h.is_wrapper)
            units.append(unit)
            owners.append(h)
        for rid, unit in self.new_units:
            if len(unit) > self.large:
                raise _huge(len(unit), self.large)
            flags |= editops.check_unit(unit, member=self.member)
            units.append(unit)
            owners.append(rid)
        store = s.ensure_store()
        spans: list[tuple[int, int]] = []
        if units:
            vs, _ve = store.append(b"".join(units))
            off = vs
            for u in units:
                spans.append((off, off + len(u)))
                off += len(u)
        content: list[tuple[int, Any, Any]] = []
        wrapper = None
        for owner, span in zip(owners, spans):
            if isinstance(owner, _Host):
                if owner.is_wrapper:
                    hole = holes[id(owner)]
                    wrapper = (st.wrapper, span, st.wrapper_hole, (span[0] + hole.start, span[0] + hole.end))
                else:
                    content.append((owner.rid, st.top.get(owner.rid), span))
            else:
                content.append((owner, None, span))
        for rid, span in self.new_spans:
            content.append((rid, None, span))
        records = sorted({after(h.record) for h in changed if h.record is not None})
        nodes: list[NodeChange] = []
        for h in sorted((h for h in self.hosts.values() if h.nodes and not h.removed), key=lambda h: h.order):
            rec = None if h.record is None else after(h.record)
            for ip, rm, cnt in h.nodes:
                nodes.append(NodeChange(rec, ip, rm, cnt))
        new_flags = st.flags | flags | self.layer_flags
        step = Step(kind=kind, label_key=label_key, label_params=dict(params), content=content,
                    struct=list(self.struct) or None, new_rids=list(self.new_rids), push_layer=self.push_layer,
                    segments=list(self.segments), wrapper=wrapper,
                    flags=(st.flags, new_flags) if new_flags != st.flags else None, nodes=nodes or None,
                    records=records, keys=(self.keys if self.member else None),
                    select=[self._select_after(after), self.before], next_rid=self.next_rid)
        try:
            return s._commit(step, kind)
        except BaseException:
            if self.push_layer is not None or self.segments:
                s._drop_pending(seg=self.segments[0] if self.segments else None, layer=self.push_layer)
            raise

    def _select_after(self, after: Any) -> list | None:
        sel = self.select
        if sel is None:
            return None
        if isinstance(sel, _Target):
            if sel.dead or (sel.host.record is not None and sel.host.removed):
                return None
            rec = None if sel.host.record is None else after(sel.host.record)
            return [rec, list(sel.path), list(sel.ipath)]
        rec, path, ipath = sel
        return [rec, list(path), list(ipath)]


def _normalize(targets: list[_Target]) -> list[_Target]:
    """Drop repeated targets and targets inside another target (a whole record covers its nodes); the given
    order is kept."""
    kept: dict[int, set[tuple[int, ...]]] = {}
    keep: set[int] = set()
    for tg in sorted(targets, key=lambda x: len(x.ipath)):
        seen = kept.setdefault(id(tg.host), set())
        ip = tuple(tg.ipath)
        if any(ip[:k] in seen for k in range(len(ip) + 1)):
            continue
        seen.add(ip)
        keep.add(id(tg))
    return [tg for tg in targets if id(tg) in keep]


def _group_children(targets: Iterable[_Target]) -> dict[tuple[int, tuple[int, ...]], list[_Target]]:
    """Targets grouped by (host, parent ipath)."""
    groups: dict[tuple[int, tuple[int, ...]], list[_Target]] = {}
    for tg in targets:
        groups.setdefault((id(tg.host), tuple(tg.ipath[:-1])), []).append(tg)
    return groups


def _rid_runs(state: OverlayState, positions: array, cancel: CancelToken | None) -> int:
    """Number of runs of consecutive rids among the records at ``positions``."""
    runs = 0
    prev = -2
    for a in range(0, len(positions), _CHUNK):
        check(cancel)
        rids = state.rids_at(positions[a:a + _CHUNK])
        for r in rids:
            if r != prev + 1:
                runs += 1
            prev = r
    return runs


def _sel_tuple(sel: Any) -> tuple | None:
    if sel is None:
        return None
    try:
        rec, path, ipath = sel
    except (TypeError, ValueError):
        return None
    return (rec, tuple(path), tuple(ipath))


def _split_select(select: Any) -> tuple[tuple | None, tuple | None]:
    """``(after, before)`` of a step's select: ``[after, before]`` (this module) or one ``(record, path, ipath)``."""
    if select is None:
        return None, None
    if isinstance(select, (list, tuple)) and len(select) == 3 and (
            select[0] is None or (isinstance(select[0], int) and not isinstance(select[0], bool))):
        return _sel_tuple(select), None
    if isinstance(select, (list, tuple)) and len(select) == 2:
        return _sel_tuple(select[0]), _sel_tuple(select[1])
    return None, None


def _changeset(kind: str, step: Step, state: OverlayState) -> ChangeSet:
    """The ChangeSet of a step applied forward (a command, bulk, patch, redo)."""
    after, _before = _split_select(step.select)
    return ChangeSet(state=state, kind=kind, label=step.label,
                     records=None if step.records is None else frozenset(step.records), remap=step.remap,
                     nodes=tuple(step.nodes or ()), wrapper=step.wrapper is not None, select=after)


def _undo_changeset(step: Step, state: OverlayState) -> ChangeSet:
    """The ChangeSet of an undone step: inverse remap, reversed inverse NodeChanges, the changed positions mapped
    back."""
    remap = step.remap
    inv = remap.inverse() if remap is not None else None
    records: set[int] | None
    if step.records is None:
        records = None
    else:
        records = set()
        for p in step.records:
            q = inv.map(p) if inv is not None else p
            if q is not None:
                records.add(q)
    _after, before = _split_select(step.select)
    return ChangeSet(state=state, kind="undo", label=step.label, records=records, remap=inv,
                     nodes=invert_node_changes(tuple(step.nodes or ()), remap), wrapper=step.wrapper is not None,
                     select=before)


# ----------------------------------------------------------------------------------
# EditSession
# ----------------------------------------------------------------------------------
class EditSession:
    """The editing session of one document (§12.5; see the module docstring). Use :meth:`open`."""

    doc: Document
    state: OverlayState
    store: EditStore | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("use EditSession.open(doc, ...)")

    # -- opening ---------------------------------------------------------------------------------
    @classmethod
    def open(cls, doc: Document, *, settings: Settings | None = None, lock: LayerLock | None = None,
             restore: bool = True, auto_apply: bool = True, upto: int | None = None) -> EditSession:
        """Open the session of ``doc`` (the live document; a pinned view is replaced by its origin).

        Preconditions (§12.0): open, index complete, no JSON error. Takes the layer lock (or uses the held
        ``lock``; ``edits.error.locked`` when another tab/instance holds it). A pending layer with steps is loaded
        when ``restore`` (``LayerDamagedError`` propagates; open again with ``upto=e.last_good``), else
        ``edits.error.pending``."""
        origin = getattr(doc, "origin", doc)
        if origin is not None:
            doc = origin
        s = settings if settings is not None else get_settings()
        if doc.closed:
            raise EditError("edits.error.closed")
        if not doc.index_complete:
            raise EditError("editstore.error.not_indexed")
        if doc.error is not None:
            raise EditError("edits.error.partial")
        doc.ensure_unchanged()            # FileChangedError: spans of another version must never be spliced
        own = lock is None
        if lock is None:
            lock = LayerLock.for_doc(doc)
            lock.acquire()
        elif not isinstance(lock, LayerLock) or not lock.held or lock.key != layer_key(doc):
            raise ValueError("lock must be the held LayerLock of this document's layer")
        store: EditStore | None = None
        try:
            pending = [p for p in find_pending(doc, settings=s, lock=lock) if p.matches and not p.locked]
            state = OverlayState.initial(doc.base_record_count, None)
            if pending:
                if not restore:
                    raise EditError("edits.error.pending")
                store = EditStore.open(doc, settings=s, lock=lock)
                assert store is not None
                if store.saving:
                    raise EditError("edits.error.pending")
                if upto is not None and store.created and upto < store.head:
                    state = store.load_state(upto=upto)
                    store.set_head(upto)
                else:
                    state = store.load_state()
            self = cls.__new__(cls)
            self.doc = doc
            self.settings = s
            self.state = state
            self.store = store
            self.style = editops.detect_style(doc)
            self.auto_apply = bool(auto_apply)
            self.closed = False
            self.warnings: list[tuple[str, dict[str, Any]]] = list(store.warnings) if store is not None else []
            self.cancel_token = CancelToken()     # interrupt(): a command waiting for the key index stops
            self.key_index = KeyIndex(doc, s, state, cancel=self.cancel_token) if doc.member_records else None
            self._layer_lock = lock
            self._lock = threading.RLock()
            self._labels: dict[int, tuple[str, dict]] = {}
            if self.auto_apply:
                doc.set_overlay(state)
            log.info("edit session opened for %s (%s, %d records, head %d)", doc.source_path.name, doc.kind,
                     state.count, state.version)
            return self
        except BaseException:
            if store is not None:
                try:
                    store.close()
                except Exception:  # pragma: no cover - best effort
                    log.debug("closing the store failed", exc_info=True)
            if own:
                lock.release()
            raise

    def __repr__(self) -> str:
        return (f"<EditSession {self.doc.source_path.name!r} gen={self.state.gen} head={self.state.version}"
                f"{' closed' if self.closed else ''}>")

    # -- properties ------------------------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        """Unsaved changes (the state's head > 0)."""
        return self.state.version > 0

    @property
    def can_undo(self) -> bool:
        store = self.store
        return not self.closed and store is not None and not store.closed and store.head > store.checkpoint_seq

    @property
    def can_redo(self) -> bool:
        store = self.store
        return not self.closed and store is not None and not store.closed and store.can_redo()

    @property
    def undo_label(self) -> str:
        return self._label(self.store.head) if self.can_undo and self.store is not None else ""

    @property
    def redo_label(self) -> str:
        return self._label(self.store.head + 1) if self.can_redo and self.store is not None else ""

    @property
    def key_index_ready(self) -> bool:
        """The record-key index of a member-record document is built (record-level key commands do not wait)."""
        ki = self.key_index
        return ki is not None and ki.ready

    @property
    def lock(self) -> threading.RLock:
        """The command lock (held by every command; producers committing through :meth:`commit_step` from
        another thread take it)."""
        return self._lock

    def history(self) -> list[StepInfo]:
        """Every stored step (also redo ones) with its localized label."""
        store = self.store
        if store is None or store.closed:
            return []
        return store.steps()

    def _label(self, seq: int) -> str:
        hit = self._labels.get(seq)
        if hit is None:
            store = self.store
            if store is None:
                return ""
            try:
                st = store.step(seq)
            except (JsonReshapeError, ValueError):
                return ""
            hit = (st.label_key, dict(st.label_params))
            self._labels[seq] = hit
        return step_label(hit[0], hit[1])

    # -- infrastructure ----------------------------------------------------------------------------
    @contextmanager
    def _command(self, expect_gen: int | None) -> Iterator[None]:
        with self._lock:
            self._check_usable()
            if expect_gen is not None and expect_gen != self.state.gen:
                raise EditError("edits.error.stale_refs")
            yield

    def _check_usable(self) -> None:
        if self.closed:
            raise EditError("edits.error.closed")

    def _noop(self) -> ChangeSet:
        return ChangeSet(self.state, "edit", "", frozenset(), None)

    def ensure_store(self) -> EditStore:
        """The session's EditStore (opened with the session's lock at the first need)."""
        store = self.store
        if store is None or store.closed:
            store = EditStore.open(self.doc, settings=self.settings, lock=self._layer_lock)
            assert store is not None
            self.store = store
            self.warnings.extend(store.warnings)
        return store

    def _drop_pending(self, *, seg: int | None = None, layer: str | None = None) -> None:
        """Remove a segment / layer file a failed command allocated (best effort)."""
        store = self.store
        if store is None or store.closed:
            return
        try:
            if layer is not None:
                store.drop_layer_file(layer)
            if seg is not None:
                store.drop_segment(seg)
        except (ValueError, JsonReshapeError, OSError):
            log.debug("dropping the pending segment/layer failed", exc_info=True)

    def _keys_view(self) -> _KeyView:
        ki = self.key_index
        if ki is None:   # pragma: no cover - callers check member_records
            raise RuntimeError("no key index: not a member-record document")
        return ki.wait(self.cancel_token)

    def interrupt(self) -> None:
        """Make a running command that waits (for the record-key index of a member-record document) stop with
        :class:`~jsonreshape.errors.Cancelled`, and stop that index's build. Thread-safe and non-blocking: the
        owner of a session that is being closed from another thread (the GUI's ``detach``) calls it before it
        asks the session's thread to :meth:`close`. Every later command that would wait fails the same way;
        :meth:`close` calls it too."""
        self.cancel_token.cancel()
        ki = self.key_index
        if ki is not None:
            ki.interrupt()

    def commit_step(self, step: Step, *, kind: str | None = None) -> ChangeSet:
        """Commit a step prepared by another producer (bulk, JSON Patch): its units must be appended to this
        session's store (:meth:`ensure_store`). The ChangeSet is built from the step's fields."""
        with self._lock:
            self._check_usable()
            return self._commit(step, kind or step.kind)

    def _commit(self, step: Step, kind: str) -> ChangeSet:
        store = self.ensure_store()
        state = self.state
        pre = state if state.reader is store.reader else state.derive(reader=store.reader)
        seq = store.commit(step, state=pre)
        try:
            new = apply_step(pre, step, layers=store)
        except BaseException:
            try:
                store.set_head(seq - 1)
            except Exception:  # pragma: no cover - the layer is unusable then
                log.exception("could not take back a step that does not apply")
            raise
        self._labels[seq] = (step.label_key, dict(step.label_params))
        self._transition(new, step, forward=True)
        try:
            store.maybe_trim(int(self.settings.undo_limit))
        except (JsonReshapeError, OSError):
            log.warning("trimming the edit history failed", exc_info=True)
        return _changeset(kind, step, new)

    def _transition(self, new: OverlayState, step: Step, *, forward: bool) -> None:
        self.state = new
        ki = self.key_index
        if ki is not None:
            changes = self._key_changes(step, new, forward) if ki.started else {}
            ki.update(new, changes)
        if self.auto_apply:
            self.doc.set_overlay(new)

    def _reset_state(self, new: OverlayState) -> None:
        self.state = new
        if self.key_index is not None:
            self.key_index.reset(new)
        if self.auto_apply:
            self.doc.set_overlay(new)

    def _key_changes(self, step: Step, new: OverlayState, forward: bool) -> dict[int, str | None] | None:
        """rid -> current key of the rids a transition may have given another key (module docstring, 14)."""
        touched: set[int] = {rid for rid, _o, _n in step.content}
        total = len(touched)
        pieces_op = False
        inserted = 0
        if step.struct is not None:
            pieces_op = any(op[0] == "pieces" for op in step.struct)
            ins = step.new_rids if forward else (step.old_rids or [])
            rem = (step.old_rids or []) if forward else step.new_rids
            inserted = sum(n for _p, _r, n in ins)
            total += inserted + sum(n for _p, _r, n in rem)
            if total <= MAX_ITEMS:
                for _p, r, n in list(ins) + list(rem):
                    touched.update(range(r, r + n))
        changes: dict[int, str | None] = {}
        if step.keys:
            for rid, old, newk in step.keys:
                changes[rid] = newk if forward else old
        if total <= MAX_ITEMS and not pieces_op:
            view = self.doc.pinned(new)
            pieces = new.pieces
            for rid in touched:
                pos = pieces.pos_of(rid)
                changes[rid] = None if pos is None else view.record_key(pos)
            return changes
        if pieces_op:
            return changes if step.keys is not None else None
        if inserted and (step.keys is None or len(step.keys) < inserted):
            return None
        return changes

    # -- positions (delete_records / duplicate_records) -------------------------------------------
    def _positions(self, positions: Any, cancel: CancelToken | None) -> array:
        """Ascending unique ``array('Q')`` of ``positions`` (RecordSet, array, range, iterable)."""
        count = self.state.count
        if isinstance(positions, RecordSet):
            gen = positions.gen
            if gen is not None and gen != self.state.gen:
                raise EditError("edits.error.stale_scope")
            if positions._is_base_of(self.doc):
                return array("Q", range(count))
            chunks: Iterable[Any] = positions.iter_chunks()
        elif isinstance(positions, (array, range, list, tuple)):
            chunks = [positions]
        else:
            it = iter(positions)
            chunks = iter(lambda: list(islice(it, _CHUNK)), [])
        out = array("Q")
        bits: bytearray | None = None
        last = -1
        for chunk in chunks:
            check(cancel)
            for a in range(0, len(chunk), _CHUNK):
                part = chunk[a:a + _CHUNK]
                try:
                    arr = part if isinstance(part, array) and part.typecode == "Q" else array("Q", part)
                except (TypeError, OverflowError) as exc:
                    raise TypeError("record positions must be non-negative integers") from exc
                if not arr:
                    continue
                top = max(arr)
                if top >= count:
                    raise EditError("edits.error.bad_index", index=int(top), count=count)
                if bits is None:
                    if arr[0] > last and _ascending(arr):
                        out.extend(arr)
                        last = arr[-1]
                        continue
                    bits = bytearray(count)
                    for p in out:
                        bits[p] = 1
                    out = array("Q")
                for p in arr:
                    bits[p] = 1
        if bits is not None:
            out = array("Q", compress(range(count), bits))
        return out

    # -- commands: values and keys -------------------------------------------------------------
    def set_value(self, ref: Any, value: str | bytes, *, expect_gen: int | None = None) -> ChangeSet:
        """Replace the value of the node ``ref`` by the JSON text ``value`` (formatted like its surroundings)."""
        with self._command(expect_gen):
            vbytes = editops.check_json(value)
            ed = _Edit(self)
            tg = ed.resolve(_as_ref(ref))
            ed.before = tg.snapshot()
            tg.host.check_node(tg)
            tg.host.op_set(tg, vbytes)
            ed.select = tg
            return ed.finish("edits.step.set_value", {})

    def set_path(self, ref: Any, value: str | bytes, *, expect_gen: int | None = None) -> ChangeSet:
        """Like :meth:`set_value`, creating missing OBJECT members along ``ref.path`` (table cells of missing
        fields); a non-object on the way -> ``edits.error.not_object``."""
        with self._command(expect_gen):
            r = _as_ref(ref)
            vbytes = editops.check_json(value)
            ed = _Edit(self)
            path = tuple(r.path)
            if r.ipath is not None and len(r.ipath) == len(path):
                try:
                    tg = ed.resolve(r)
                except EditError as exc:
                    if exc.key != "edits.error.path_not_found":
                        raise
                else:
                    return self._set_found(ed, tg, vbytes)
            record = 0 if r.record is None and ed.single else r.record
            if record is None:
                host = ed.wrapper_host()
                rp = host.hole_path
                if len(path) >= len(rp) and all(_seg_eq(a, b) for a, b in zip(path, rp)):
                    raise ed.locked()
            else:
                if not 0 <= record < ed.state.count:
                    raise _not_found(record, path)
                host = ed.record_host(record)
            buf = host.buf
            cur = editops.locate(buf, ())
            out_p: list[Any] = []
            out_i: list[int] = []
            missing = None
            for level, seg in enumerate(path):
                if cur.kind not in _CONTAINERS:
                    raise EditError("edits.error.not_object", path=_ref_text(record, path[:level]))
                kids = _kids(buf, cur)
                i = kids.find(seg)
                if i is None:
                    missing = level
                    break
                cur = kids.loc(i)
                out_p.append(cur.key)
                out_i.append(i)
            if missing is None:
                return self._set_found(ed, host.target(out_p, out_i), vbytes)
            if cur.kind != "object":
                raise EditError("edits.error.not_object", path=_ref_text(record, path[:missing]))
            rest = path[missing:]
            for j, seg in enumerate(rest):
                if type(seg) is not str:
                    raise EditError("edits.error.not_object", path=_ref_text(record, path[:missing + j]))
            nested = vbytes
            for seg in reversed(rest[1:]):
                nested = b"{" + editops.encode_key(seg) + b":" + nested + b"}"
            parent = host.target(out_p, out_i)
            n = _count(buf, cur)
            host.check_children(parent, "object", insert_at=n)
            ed.before = parent.snapshot()
            host.op_insert(parent, n, nested, rest[0])
            ed.select = host.target(parent.path + list(rest), parent.ipath + [n] + [0] * (len(rest) - 1))
            return ed.finish("edits.step.set_value", {})

    def _set_found(self, ed: _Edit, tg: _Target, vbytes: bytes) -> ChangeSet:
        ed.before = tg.snapshot()
        tg.host.check_node(tg)
        tg.host.op_set(tg, vbytes)
        ed.select = tg
        return ed.finish("edits.step.set_value", {})

    def rename_key(self, ref: Any, key: str, *, expect_gen: int | None = None) -> ChangeSet:
        """Rename an object member, or (path ``()`` in a member-record document) the record key. Keys must be
        unique: ``edits.error.key_exists``."""
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        with self._command(expect_gen):
            ed = _Edit(self)
            tg = ed.resolve(_as_ref(ref))
            ed.before = tg.snapshot()
            h = tg.host
            if tg.whole:
                if not ed.member:
                    raise EditError("edits.error.not_member")
                if key == h.key:
                    return self._noop()
                view = self._keys_view()
                if view.positions(key, ignore=(h.rid,)):
                    raise EditError("edits.error.key_exists", key=key)
                h.new_key = key
                assert ed.keys is not None
                ed.keys.append((h.rid, h.key, key))   # type: ignore[arg-type]
                ed.select = tg
                return ed.finish("edits.step.rename_key", {})
            loc = h.locate(tg)
            if loc.mstart is None:
                raise EditError("edits.error.not_member")
            if loc.key == key:
                return self._noop()
            h.check_node(tg)
            h.op_rename(tg, key)
            ed.select = tg
            return ed.finish("edits.step.rename_key", {})

    def change_type(self, refs: Any, to: str, *, expect_gen: int | None = None) -> ChangeSet:
        """Convert the values of ``refs`` to the JSON type ``to`` (§12.4 table); an impossible conversion raises
        ``ConversionError`` and nothing changes; values that already have the type stay untouched."""
        if to not in editops.JSON_TYPES:
            raise ValueError(f"unknown JSON type {to!r}")
        with self._command(expect_gen):
            ed = _Edit(self)
            targets = ed.resolve_all(refs)
            if not targets:
                return self._noop()
            ed.before = targets[0].snapshot()
            changed = 0
            for tg in targets:
                h = tg.host
                h.check_node(tg)
                loc = h.locate(tg)
                value = codec.loads(h.buf[loc.start:loc.end])
                new = editops.convert_value(value, to)
                if new is value:
                    continue
                h.op_set(tg, codec.dumps(new))
                changed += 1
            ed.select = targets[0]
            return ed.finish("edits.step.change_type", {"count": changed})

    # -- commands: inserting -----------------------------------------------------------------------
    def insert(self, parent: Any, index: int | None, value: str | bytes, *, key: str | None = None,
               expect_gen: int | None = None) -> ChangeSet:
        """A new child of ``parent`` at ``index`` (child position before the step; ``None`` = end) -- or, with
        ``parent=None``, a new record at position ``index`` (member-record documents need ``key``)."""
        with self._command(expect_gen):
            vbytes = editops.check_json(value)
            ed = _Edit(self)
            if parent is None:
                if ed.member and key is None:
                    raise EditError("edits.error.key_required")
                return self._insert_records(ed, index, [(vbytes, key)], base="pasted",
                                            label=("edits.step.insert_record", {}))
            tg = ed.resolve(_as_ref(parent))
            return self._insert_child(ed, tg, index, vbytes, key, free=False, label=("edits.step.insert", {}))

    def paste(self, parent: Any, text: str | bytes, *, index: int | None = None, key: str | None = None,
              as_records: bool = False, expect_gen: int | None = None) -> ChangeSet:
        """ONE new child of ``parent`` (object parents: ``key``, else a free key ``pasted``, ``pasted_2``, ...)
        or, with ``parent=None``, one new record (module docstring, 12 for ``as_records``)."""
        with self._command(expect_gen):
            vbytes = editops.check_json(text)
            ed = _Edit(self)
            if parent is None:
                items: list[tuple[bytes, str | None]] = [(vbytes, key)]
                if as_records:
                    root = editops.locate(vbytes, ())
                    if root.kind == "array":
                        items = [(vbytes[c.start:c.end], None) for c in editops.child_locs(vbytes, root)]
                    elif root.kind == "object" and ed.member:
                        items = [(vbytes[c.start:c.end], c.key) for c in editops.child_locs(vbytes, root)]   # type: ignore[misc]
                if not items:
                    return self._noop()
                return self._insert_records(ed, index, items, base=key or "pasted", label=("edits.step.paste", {}))
            tg = ed.resolve(_as_ref(parent))
            return self._insert_child(ed, tg, index, vbytes, key, free=True, label=("edits.step.paste", {}))

    def _insert_child(self, ed: _Edit, tg: _Target, index: int | None, vbytes: bytes, key: str | None, *,
                      free: bool, label: tuple[str, dict]) -> ChangeSet:
        h = tg.host
        ed.before = tg.snapshot()
        loc = h.locate(tg)
        if loc.kind not in _CONTAINERS:
            raise EditError("edits.error.not_container")
        n = _count(h.buf, loc)
        idx = n if index is None else _index(index)
        if not 0 <= idx <= n:
            raise EditError("edits.error.bad_index", index=idx, count=n)
        h.check_children(tg, loc.kind, insert_at=idx)
        k: str | None = None
        if loc.kind == "object":
            k = key
            if k is None and free:
                kids = _kids(h.buf, loc)
                k = editops.free_key({kids.key(i) for i in range(kids.n)}, "pasted")
        h.op_insert(tg, idx, vbytes, k)
        ed.select = h.target(tg.path + [k if loc.kind == "object" else idx], tg.ipath + [idx])
        return ed.finish(*label)

    def _insert_records(self, ed: _Edit, index: int | None, items: list[tuple[bytes, str | None]], *, base: str,
                        label: tuple[str, dict]) -> ChangeSet:
        if ed.single:
            raise EditError("edits.error.single_root")
        count = ed.state.count
        idx = count if index is None else _index(index)
        if not 0 <= idx <= count:
            raise EditError("edits.error.bad_index", index=idx, count=count)
        k = len(items)
        if k > MAX_ITEMS:
            raise _too_many(k)
        units: list[bytes] = []
        keys: list[str] = []
        if ed.member:
            taken = _TakenKeys(self._keys_view())
            for value, key in items:
                if key is None:
                    key = editops.free_key(taken, base)
                elif key in taken:
                    raise EditError("edits.error.key_exists", key=key)
                taken.extra.add(key)
                keys.append(key)
                units.append(editops.format_record(value, self.style, key=key))
        else:
            units = [editops.format_record(value, self.style) for value, _key in items]
        rid_lo = ed.alloc(k)
        ed.struct.append(("ins", idx, k))
        ed.new_rids.append((idx, rid_lo, k))
        for j, unit in enumerate(units):
            ed.new_units.append((rid_lo + j, unit))
        if ed.member and ed.keys is not None:
            ed.keys.extend((rid_lo + j, None, keys[j]) for j in range(k))
        ed.select = (idx, (), ())
        ed.before = [min(idx, count - 1), [], []] if count else None
        return ed.finish(*label)

    # -- commands: deleting / duplicating ----------------------------------------------------------
    def delete(self, refs: Any, *, expect_gen: int | None = None) -> ChangeSet:
        """Delete nodes and/or whole records (``Ref(pos, ())``); at most :data:`MAX_ITEMS`."""
        with self._command(expect_gen):
            ed = _Edit(self)
            targets = ed.resolve_all(refs)
            if not targets:
                return self._noop()
            first = targets[0]
            ed.before = first.snapshot()
            records = sorted({tg.host.record for tg in targets if tg.whole})   # type: ignore[type-var]
            nodes = [tg for tg in targets if not tg.whole]
            if records and ed.single:
                raise EditError("edits.error.single_root")
            for tg in nodes:
                if not tg.ipath:   # the wrapper root
                    raise ed.locked()
                tg.host.check_node(tg)
            sel: Any = None
            groups = _group_children(nodes)
            for _key, items in sorted(groups.items(), key=lambda kv: (kv[1][0].host.order, len(kv[0][1]))):
                h = items[0].host
                parent = h.target(items[0].path[:-1], items[0].ipath[:-1])
                ploc = h.locate(parent)
                idxs = sorted(tg.ipath[-1] for tg in items)
                h.check_children(parent, ploc.kind, removed=idxs)
                first_i = first.ipath[-1] if first in items else None
                h.op_delete(parent, idxs)
                if first_i is not None:
                    sel = self._neighbour(h, parent, first_i, idxs)
            if records:
                ed.remove_records(array("Q", records))
                if first.whole:
                    p = first.host.record
                    assert p is not None
                    remaining = ed.state.count - len(records)
                    j = p - bisect.bisect_left(records, p)
                    sel = (j, (), ()) if j < remaining else ((remaining - 1, (), ()) if remaining else None)
            ed.select = sel
            if not nodes:
                return ed.finish("edits.step.delete_records", {"count": len(records)})
            return ed.finish("edits.step.delete", {"count": len(targets)})

    @staticmethod
    def _neighbour(h: _Host, parent: _Target, i: int, removed: list[int]) -> _Target:
        """The node to select after deleting ``removed`` children of ``parent`` (``i`` = the first deleted)."""
        loc = h.locate(parent)
        kids = _kids(h.buf, loc)
        m = kids.n
        j = i - bisect.bisect_left(removed, i)
        if j >= m:
            j = m - 1
        if j < 0:
            return parent
        return h.target(parent.path + [kids.key(j)], parent.ipath + [j])

    def duplicate(self, refs: Any, *, expect_gen: int | None = None) -> ChangeSet:
        """A copy right after each node / record (member records and object members get free keys)."""
        with self._command(expect_gen):
            ed = _Edit(self)
            targets = ed.resolve_all(refs)
            if not targets:
                return self._noop()
            first = targets[0]
            ed.before = first.snapshot()
            records = sorted({tg.host.record for tg in targets if tg.whole})   # type: ignore[type-var]
            nodes = [tg for tg in targets if not tg.whole]
            if records and ed.single:
                raise EditError("edits.error.single_root")
            for tg in nodes:
                if not tg.ipath:
                    raise ed.locked()
                tg.host.check_node(tg)
            sel: Any = None
            groups = _group_children(nodes)
            for _key, items in sorted(groups.items(), key=lambda kv: (kv[1][0].host.order, len(kv[0][1]))):
                h = items[0].host
                parent = h.target(items[0].path[:-1], items[0].ipath[:-1])
                ploc = h.locate(parent)
                is_obj = ploc.kind == "object"
                kids = _kids(h.buf, ploc)
                taken = {kids.key(i) for i in range(kids.n)} if is_obj else set()
                plan = []
                for tg in sorted(items, key=lambda x: x.ipath[-1]):
                    i = tg.ipath[-1]
                    cl = kids.loc(i)
                    key = None
                    if is_obj:
                        key = editops.free_key(taken, cl.key)
                        taken.add(key)
                    plan.append((i, h.buf[cl.start:cl.end], key, tg))
                for i, _v, _k, _t in plan:
                    h.check_children(parent, ploc.kind, insert_at=i + 1)
                for i, value, key, tg in reversed(plan):
                    h.op_insert(parent, i + 1, value, key, verbatim=True)
                    copy = h.target(parent.path + [key if is_obj else i + 1], parent.ipath + [i + 1])
                    if tg is first:
                        sel = copy
            if records:
                ed.duplicate_records(array("Q", records))
                if first.whole:
                    p = first.host.record
                    j = records.index(p)   # type: ignore[arg-type]
                    sel = (p + j + 1, (), ())   # type: ignore[operator]
            ed.select = sel
            if not nodes:
                return ed.finish("edits.step.duplicate_records", {"count": len(records)})
            return ed.finish("edits.step.duplicate", {"count": len(targets)})

    def delete_records(self, positions: Any, *, expect_gen: int | None = None, progress: Progress | None = None,
                       cancel: CancelToken | None = None) -> ChangeSet:
        """Delete the records at ``positions`` (a RecordSet / array / any iterable, any size)."""
        with self._command(expect_gen):
            if self.doc.kind == "single":
                raise EditError("edits.error.single_root")
            if progress is not None:
                progress.set_message(t("edits.progress.delete_records"))
            pos = self._positions(positions, cancel)
            k = len(pos)
            if not k:
                return self._noop()
            ed = _Edit(self)
            ed.remove_records(pos, progress=progress, cancel=cancel)
            remaining = ed.state.count - k
            p0 = int(pos[0])
            ed.select = (p0, (), ()) if p0 < remaining else ((remaining - 1, (), ()) if remaining else None)
            ed.before = [p0, [], []]
            cs = ed.finish("edits.step.delete_records", {"count": k})
            if progress is not None:
                progress.finish()
            return cs

    def duplicate_records(self, positions: Any, *, expect_gen: int | None = None, progress: Progress | None = None,
                          cancel: CancelToken | None = None) -> ChangeSet:
        """A copy right after each record of ``positions`` (any size; big selections become an array layer)."""
        with self._command(expect_gen):
            if self.doc.kind == "single":
                raise EditError("edits.error.single_root")
            if progress is not None:
                progress.set_message(t("edits.progress.duplicate_records"))
            pos = self._positions(positions, cancel)
            k = len(pos)
            if not k:
                return self._noop()
            ed = _Edit(self)
            ed.duplicate_records(pos, progress=progress, cancel=cancel)
            ed.select = (int(pos[0]) + 1, (), ())
            ed.before = [int(pos[0]), [], []]
            cs = ed.finish("edits.step.duplicate_records", {"count": k})
            if progress is not None:
                progress.finish()
            return cs

    # -- commands: moving --------------------------------------------------------------------------
    def move(self, refs: Any, parent: Any, index: int | None, *, expect_gen: int | None = None) -> ChangeSet:
        """Move ``refs`` into ``parent`` (``None`` = the record level) before the child at ``index`` (a position
        before the step; ``None`` = the end). A node moved into itself or its subtree ->
        ``edits.error.move_into_self``."""
        with self._command(expect_gen):
            ed = _Edit(self)
            srcs = ed.resolve_all(refs)
            if not srcs:
                return self._noop()
            ed.before = srcs[0].snapshot()
            if parent is None:
                return self._move_records(ed, srcs, index)
            return self._move_nodes(ed, srcs, ed.resolve(_as_ref(parent)), index)

    def move_by(self, refs: Any, delta: int, *, expect_gen: int | None = None) -> ChangeSet:
        """Move sibling nodes (or records) ``delta`` places (-1 up, +1 down) as one block (module docstring, 11)."""
        delta = _index(delta)
        with self._command(expect_gen):
            ed = _Edit(self)
            srcs = ed.resolve_all(refs)
            if not srcs or not delta:
                return self._noop()
            ed.before = srcs[0].snapshot()
            if all(s.whole for s in srcs):
                pos = sorted(s.host.record for s in srcs)   # type: ignore[type-var]
                n = ed.state.count
                idx = max(0, pos[0] + delta) if delta < 0 else min(n, pos[-1] + 1 + delta)
                return self._move_records(ed, srcs, idx)
            h0 = srcs[0].host
            pip = srcs[0].ipath[:-1]
            if any(s.whole or s.host is not h0 or s.ipath[:-1] != pip or not s.ipath for s in srcs):
                raise EditError("edits.error.not_siblings")
            parent = h0.target(srcs[0].path[:-1], pip)
            n = _count(h0.buf, h0.locate(parent))
            idxs = sorted(s.ipath[-1] for s in srcs)
            idx = max(0, idxs[0] + delta) if delta < 0 else min(n, idxs[-1] + 1 + delta)
            return self._move_nodes(ed, srcs, parent, idx)

    def _check_source(self, ed: _Edit, s: _Target) -> None:
        h = s.host
        if s.whole:
            if ed.single:
                raise EditError("edits.error.single_root")
            return
        if not s.ipath:
            raise ed.locked()
        h.check_node(s)
        if h.is_wrapper:
            parent = h.target(s.path[:-1], s.ipath[:-1])
            h.check_children(parent, h.locate(parent).kind, removed=[s.ipath[-1]])

    def _delete_sources(self, ed: _Edit, sources: list[_Target], plan: list[tuple[int, int, Any]]) -> None:
        """Add the removal of moved nodes (grouped by container) to ``plan`` (host order, depth, fn)."""
        for _key, items in _group_children(sources).items():
            h = items[0].host
            parent = h.target(items[0].path[:-1], items[0].ipath[:-1])
            idxs = sorted(tg.ipath[-1] for tg in items)
            plan.append((h.order, len(parent.ipath), lambda h=h, parent=parent, idxs=idxs: h.op_delete(parent, idxs)))

    def _move_nodes(self, ed: _Edit, srcs: list[_Target], T: _Target, index: int | None) -> ChangeSet:
        ht = T.host
        for s in srcs:
            if (s.whole and s.host is ht) or (s.host is ht and _is_prefix(s.ipath, T.ipath)):
                raise EditError("edits.error.move_into_self")
        tloc = ht.locate(T)
        if tloc.kind not in _CONTAINERS:
            raise EditError("edits.error.not_container")
        for s in srcs:
            self._check_source(ed, s)
        n = _count(ht.buf, tloc)
        is_obj = tloc.kind == "object"
        own_t = [s for s in srcs if not s.whole and s.host is ht and s.ipath[:-1] == T.ipath]
        own = sorted(s.ipath[-1] for s in own_t)
        idx = n if index is None else _index(index)
        if not 0 <= idx <= n:
            raise EditError("edits.error.bad_index", index=idx, count=n)
        to = idx - bisect.bisect_left(own, idx)
        ht.check_children(T, tloc.kind, removed=own, insert_at=to)
        items = sorted(srcs, key=_Target.order)
        kids = _kids(ht.buf, tloc)
        own_set = set(own)
        own_ids = {id(s) for s in own_t}
        taken: set[str] = set()
        own_keys: dict[int, tuple[Any, Any]] = {}
        if is_obj:
            taken = {kids.key(i) for i in range(n) if i not in own_set}
            for i in own:
                k0 = kids.key(i)
                k1 = editops.free_key(taken, k0) if k0 in taken else k0
                taken.add(k1)
                own_keys[i] = (k0, k1)
        specs: list[tuple[int | None, bytes, Any, Any]] = []
        for s in items:
            if id(s) in own_ids:
                i = s.ipath[-1]
                cl = kids.loc(i)
                k0, k1 = own_keys.get(i, (None, None))
                specs.append((i, ht.buf[cl.start:cl.end], k1, k0))
            else:
                value, base = ed.value_of(s)
                key = None
                if is_obj:
                    key = editops.free_key(taken, base)
                    taken.add(key)
                specs.append((None, value, key, None))
        plan: list[tuple[int, int, Any]] = [(ht.order, len(T.ipath), lambda: ht.op_place(T, own, to, specs))]
        self._delete_sources(ed, [s for s in srcs if not s.whole and id(s) not in own_ids], plan)
        for _o, _d, fn in sorted(plan, key=lambda x: (x[0], x[1])):
            fn()
        records = sorted({s.host.record for s in srcs if s.whole})   # type: ignore[type-var]
        if records:
            ed.remove_records(array("Q", records))
        k0 = specs[0][2]
        ed.select = ht.target(T.path + [k0 if is_obj else to], T.ipath + [to])
        return ed.finish("edits.step.move", {"count": len(items)})

    def _move_records(self, ed: _Edit, srcs: list[_Target], index: int | None) -> ChangeSet:
        if ed.single:
            raise EditError("edits.error.single_root")
        count = ed.state.count
        idx = count if index is None else _index(index)
        if not 0 <= idx <= count:
            raise EditError("edits.error.bad_index", index=idx, count=count)
        P = sorted({s.host.record for s in srcs if s.whole})   # type: ignore[type-var]
        to = idx - bisect.bisect_left(P, idx)
        items = sorted(srcs, key=_Target.order)
        node_items = [s for s in items if not s.whole]
        for s in node_items:
            self._check_source(ed, s)
        new: list[tuple[int, bytes, str | None]] = []
        if node_items:
            taken = _TakenKeys(self._keys_view()) if ed.member else None
            for j, s in enumerate(items):
                if s.whole:
                    continue
                value, base = ed.value_of(s)
                key = None
                if taken is not None:
                    key = editops.free_key(taken, base)
                    taken.extra.add(key)
                new.append((j, editops.format_record(value, self.style, key=key), key))
        plan: list[tuple[int, int, Any]] = []
        self._delete_sources(ed, node_items, plan)
        for _o, _d, fn in sorted(plan, key=lambda x: (x[0], x[1])):
            fn()
        if P:
            ed.struct.append(("move", array("Q", P), to))
        if new:
            ed.struct.append(("inss", array("Q", [to + j for j, _u, _k in new])))
            rid_lo = ed.alloc(len(new))
            ed.new_rids.append((to + new[0][0], rid_lo, len(new)))
            for m, (_j, unit, key) in enumerate(new):
                ed.new_units.append((rid_lo + m, unit))
                if ed.member and ed.keys is not None:
                    ed.keys.append((rid_lo + m, None, key))
        ed.select = (to, (), ())
        if not node_items:
            return ed.finish("edits.step.move_records", {"count": len(P)})
        return ed.finish("edits.step.move", {"count": len(items)})

    # -- undo / redo -------------------------------------------------------------------------------
    def undo(self) -> ChangeSet | None:
        """Undo the last step (``None`` when there is nothing to undo). The new head is durable before the new
        state is returned."""
        with self._lock:
            self._check_usable()
            store = self.store
            if store is None or store.closed or store.head <= store.checkpoint_seq:
                return None
            seq = store.head
            step = store.step(seq)
            store.set_head(seq - 1)
            try:
                new = undo_step(self.state, step, layers=store)
            except BaseException:
                try:
                    store.set_head(seq)
                except Exception:  # pragma: no cover
                    log.exception("could not restore the head after a failed undo")
                raise
            self._labels.setdefault(seq, (step.label_key, dict(step.label_params)))
            self._transition(new, step, forward=False)
            return _undo_changeset(step, new)

    def redo(self) -> ChangeSet | None:
        """Redo the next step (``None`` when there is none)."""
        with self._lock:
            self._check_usable()
            store = self.store
            if store is None or store.closed or not store.can_redo():
                return None
            seq = store.head + 1
            step = store.step(seq)
            store.set_head(seq)
            try:
                new = apply_step(self.state, step, layers=store)
            except BaseException:
                try:
                    store.set_head(seq - 1)
                except Exception:  # pragma: no cover
                    log.exception("could not restore the head after a failed redo")
                raise
            self._labels.setdefault(seq, (step.label_key, dict(step.label_params)))
            self._transition(new, step, forward=True)
            return _changeset("redo", step, new)

    # -- bulk, patches, save -------------------------------------------------------------------------
    def bulk_preview(self, spec: Any, scope: Any = None, *, progress: Progress | None = None,
                     cancel: CancelToken | None = None) -> Any:
        """Compute a bulk operation (§12.6) on the current state; does NOT hold the command lock (it may run
        for minutes). Apply it with :meth:`bulk_apply` while the state is unchanged."""
        from . import bulk   # lazy: bulk imports a lot

        with self._lock:
            self._check_usable()
            state = self.state
            store = self.ensure_store()
            pinned = self.doc.pinned(state)
        return bulk.preview(pinned, state, store, spec, scope, progress=progress, cancel=cancel)

    def bulk_apply(self, preview: Any) -> ChangeSet:
        """Apply a preview computed on the current state (``preview.gen`` must equal ``state.gen``, else
        ``edits.error.stale_refs``)."""
        from . import bulk

        with self._lock:
            self._check_usable()
            if getattr(preview, "gen", None) != self.state.gen:
                raise EditError("edits.error.stale_refs")
            step = bulk.make_step(preview)
            cs = self._commit(step, "bulk")
            discard = getattr(preview, "discard", None)
            if callable(discard):      # frees the preview (its files now belong to the committed step)
                try:
                    discard()
                except Exception:  # pragma: no cover - best effort
                    log.debug("discarding an applied bulk preview failed", exc_info=True)
            return cs

    def import_patch(self, source: Any, *, progress: Progress | None = None,
                     cancel: CancelToken | None = None) -> ChangeSet:
        """Apply a JSON Patch (§12.8) as ONE step."""
        from . import jsonpatch

        with self._lock:
            self._check_usable()
            return jsonpatch.apply_patch(self, source, progress=progress, cancel=cancel)

    def export_patch(self, path: Any, *, diff: bool = True, progress: Progress | None = None,
                     cancel: CancelToken | None = None) -> int:
        """Write the net effect of the edits as a JSON Patch (§12.8); returns the number of operations."""
        from . import jsonpatch

        with self._lock:
            self._check_usable()
            return jsonpatch.export_patch(self, path, diff=diff, progress=progress, cancel=cancel)

    def save(self, target: Any = None, *, backup: bool | None = None, drop_broken: bool = False,
             progress: Progress | None = None, cancel: CancelToken | None = None) -> Any:
        """Save (``target=None``: in place) or Save As (§12.7); afterwards the layer is discarded and the session
        is CLOSED."""
        from . import saver

        with self._lock:
            self._check_usable()
            b = bool(self.settings.backup_on_save) if backup is None else bool(backup)
            report = saver.save(self.doc.pinned(self.state), target, backup=b, drop_broken=drop_broken,
                                settings=self.settings, progress=progress, cancel=cancel, store=self.store)
            # the file is written: what follows is cleanup. A failure there (e.g. the disk is full now that the
            # .bak keeps the old version) must not turn the save into a failed one: the session is closed all
            # the same and the report carries a warning (the layer is offered at the next open).
            try:
                self._shutdown(discard=True)
            except Exception as exc:
                log.warning("the edit layer of %s could not be deleted after saving", self.doc.source_path.name,
                            exc_info=True)
                detail = exc.message if isinstance(exc, JsonReshapeError) else str(exc)
                warnings = getattr(report, "warnings", None)
                if isinstance(warnings, list):
                    warnings.append(t("edits.warn.layer_left", detail=detail))
                keys = getattr(report, "warning_keys", None)
                if isinstance(keys, list):
                    keys.append("edits.warn.layer_left")
            return report

    # -- discard / close -----------------------------------------------------------------------------
    def discard(self) -> ChangeSet:
        """Delete the layer (tombstone first); the state becomes the initial one (a reset ChangeSet). The files
        are deleted once no state reads them any more (the document's state of a GUI session until the reset is
        applied, pinned views)."""
        with self._lock:
            self._check_usable()
            store = self.store
            if store is not None:
                store.mark_discarded()      # durable first: never offered for recovery again
            self.store = None
            self._labels.clear()
            new = OverlayState.initial(self.doc.base_record_count, None)
            self._reset_state(new)          # the old states are no longer the session's (nor, with auto_apply,
            if store is not None:           # the document's): their files can go as soon as nothing reads them
                try:
                    store.discard()
                except Exception:           # tombstoned: leftovers are purged at the next open
                    log.warning("deleting the discarded edit layer failed", exc_info=True)
            return ChangeSet(new, "discard", "", None, None, reset=True)

    def close(self) -> None:
        """Release the lock; a layer with steps is kept (recovery), a head-0 layer deleted. Idempotent. A command
        of another thread that waits for the record-key index is interrupted first (:meth:`interrupt`)."""
        self.interrupt()
        with self._lock:
            if self.closed:
                return
            self._shutdown(discard=False)

    def _shutdown(self, *, discard: bool) -> None:
        """Close the session: the key index, then the store (``discard``: the layer is deleted) and the lock -- all
        of them even when one step fails (the error is raised afterwards)."""
        self.closed = True
        ki = self.key_index
        if ki is not None:
            ki.close()
        store, self.store = self.store, None
        try:
            if store is not None:
                if discard:
                    try:
                        store.discard()
                    except BaseException:
                        try:                # never leave the database open (it holds the layer exclusively)
                            store.close()
                        except Exception:  # pragma: no cover - best effort
                            log.debug("closing the store failed", exc_info=True)
                        raise
                else:
                    store.close()
        finally:
            lock = self._layer_lock
            if lock is not None:
                lock.release()
        log.info("edit session closed for %s", self.doc.source_path.name)

    def __enter__(self) -> EditSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
