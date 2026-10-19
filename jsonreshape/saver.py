"""Save and Save As of a (possibly edited) document in its own kind and encoding (docs/ARCHITECTURE.md §12.7).

:func:`save` writes the effective records of a document -- the file plus the edit layer of the
overlay state it shows (§12.0) -- back in the **same kind** (array, keyed object, the wrapper around
the record container, JSON Lines, concatenated values, the single value; a ``brace_list`` becomes a
JSON array) and the **same encoding and compression** (UTF-8 with or without BOM, UTF-16 LE/BE with or
without BOM, gzip), validates the whole output with the core ``Scanner`` while it is written, and
replaces the file atomically (in place, keeping ``<file>.bak``) or writes another file (Save As).
:func:`roll_journal` finishes or undoes an in-place save that was interrupted (a crash, a power cut)
when the document is opened again (:func:`find_journals` / :func:`roll_journal_at` when the file itself is
missing); :func:`sweep_leftovers` removes what crashed saves left next to the file;
:func:`save_questions` tells what to ask before a save.

What is written
===============
Bytes, in order: the **head**, the records in effective order, the **tail**. Everything that is not
edited is copied byte for byte from the data file -- untouched records, the separators between
records that were neighbours in the file, the bytes around the records -- and every edited or new
record is its unit from the edit layer (already formatted like its neighbours by the edit session):

* array / keyed / brace_list / single: head = ``[data_start, first record of the file)``, tail =
  ``[last record of the file, data_size)``; brace_list: the opening ``{`` of the head is written as
  ``[`` and the closing ``}`` of the tail as ``]`` (every other byte kept; ``saver.warn.brace_list``);
* wrapped: head = the effective wrapper prefix (``Document.wrapper_spans()[0]``; an EDITED wrapper's
  prefix is a span of the edit layer and gets the file's whitespace before the root value in front)
  + the file's ``[container start, first record)``; tail = ``[last record, container end)`` + the
  effective suffix (an edited wrapper: + the file's whitespace after the root value);
* jsonl / stream: head = ``[data_start, first record)``, tail = ``[last record, data_size)``;
* between two output records ``a`` and ``b``: the original gap of the file when both are records of
  the file and ``b`` came right after ``a`` there (``rid_b == rid_a + 1``; also when their content
  is edited), else ``DocStyle.record_sep``. A run of untouched records is copied as ONE range
  ``[start of the first, end of the last)``;
* zero records: comma kinds = the head's leading whitespace + ``DocStyle.empty`` + the tail's
  trailing whitespace; wrapped = effective prefix + ``DocStyle.empty`` + effective suffix; jsonl /
  stream = an empty output (``reopen_options`` reopens it with 0 records);
* a file without records (``[]``, a forced keyed ``{}``, a forced wrapped empty container, JSON Lines
  of broken lines only) that gets records: head = up to and including the opening bracket (+ newline
  and indentation when the document is pretty), tail = (newline and indentation +) the closing
  bracket onwards; jsonl / stream: the records + one newline;
* JSON Lines with broken lines: ``drop_broken=False`` refuses (``EditError("saver.error.broken_lines",
  count)``; the GUI asks first). With ``drop_broken=True`` every byte range copied from the file (head,
  tail, gaps, the gaps inside copied runs) is filtered: a line holding a non-whitespace byte outside a
  record -- exactly the lines ``iter_broken_lines()`` lists, also beyond the 10 000 / 100 000 kept --
  is removed with its line end (an unterminated broken last line without it); blank lines stay.

Validation and encoding
=======================
The UTF-8 stream (without a BOM) goes through a bounded queue (8 x 1 MiB) to a helper thread that runs
a core ``Scanner`` of the document's backend in the kind's mode (wrapped: ``MODE_CONTAINER`` with the
record path; array/keyed/brace_list: ``MODE_CONTAINER []``; jsonl: ``MODE_LINES``, where a broken line
also fails; stream: ``MODE_STREAM``; single: ``MODE_SINGLE``). A JSON error or a record count other than
``doc.record_count`` raises ``ExportError("saver.error.invalid_output", detail)``; nothing is replaced
(the details are logged). The stream is then encoded: UTF-16 LE/BE again (a BOM iff the source had one,
``PreparedFile.source_bom == 2``), a UTF-8 BOM iff ``info.encoding == "utf-8-bom"`` (also inside gzip),
gzip with ``zlib.compressobj(6, DEFLATED, 31)`` (one member; header mtime 0). Save As compresses iff
the target name ends with ``.gz`` (``saver.warn.compressed`` / ``saver.warn.uncompressed`` when that
differs from the source); data after the source's gzip stream is not kept (``saver.warn.gzip_trailing``,
``saver.warn.gzip_truncated``; an in-place save without a backup is asked about first:
:func:`save_questions`).

Reading: pieces are planned in windows of 65 536 positions (``OverlayState.runs()``; the spans a window
needs are fetched in batches: one index read per cluster of nearby record ids, one ``spans_in`` per
content layer), runs of untouched records and the gaps next to them merge into single file ranges,
ranges of at least 256 KiB are copied in 1 MiB reads, smaller pieces are gathered into batches whose
reads are coalesced (file pieces and the units of each segment separately; neighbours at most 32 KiB
apart while the read stays half used, reads of at most 8 MiB). A run of at least 8 edited records that
were neighbours in the file (what a bulk operation leaves) is assembled at once: its units with one
read, its original gaps with one read of the file range around them, sliced at C level. Memory stays at
a few MiB whatever the file size; with the Rust core the validation runs beside the writing (its
``feed`` releases the GIL). Progress counts the UTF-8 output bytes against an O(1) estimate (the file's
size, its records' part scaled by the effective record count).

Committing
==========
The temporary file ``.<name>.<hex>.jrsave`` (a name the cache eviction never touches) is written next
to the target, fsynced and closed; then:

* **in place** (``target`` None, or a path that is the same file):
  0. the temp file gets the source's attributes (decision 12): compression / EFS encryption while it is
     still empty, hidden / system / not-content-indexed (+ the archive bit) after it was written;
  1. growth check now: ``fileio.appended_bytes(source, identity)`` must be exactly 0 (``None`` ->
     ``FileChangedError``, ``> 0`` -> ``EditError("saver.error.grown", bytes, path)``), then
     ``doc.ensure_unchanged()``; a read-only source -> ``UserInputError("fileio.error.read_only")`` (also
     checked before writing);
  2. the journal (``store.journal``; only when ``store`` is given) -- see :func:`roll_journal`;
  3. backup on: an existing ``<source>.bak`` (read-only bit cleared) is renamed to
     ``.<name>.<hex>.old``, then ``fileio.hard_link(source, <source>.bak)``;
  4. ``fileio.replace_posix(temp, source)`` -- atomic, works while our documents still read the file;
  5. where hard links or POSIX renames do not exist: the source is renamed to ``<source>.bak`` (backup
     on) or ``.<name>.<hex>.old`` (off), then the temp file to the source; on failure the first rename
     is undone (when even that fails: ``JsonReshapeError("saver.error.original_moved", path)`` naming
     where the original is, the journal is kept);
  6. success: this save's ``.old`` files are deleted, the journal is cleared.
  Any failure before step 4/5 completed undoes steps 3 (the new link removed, the old ``.bak`` put back
  with its read-only bit) and removes the temp file.
* **Save As**: the source is not touched; a source that changed meanwhile raises ``FileChangedError``,
  one that only grew adds ``reader.warn.appended`` (the snapshot opened is written); the temp file
  replaces the target with ``replace_posix`` (works even while another tab shows the target), or with
  ``os.replace`` where POSIX renames do not exist.

:class:`SaveReport` tells what was written; ``reopen_options`` (``format`` -- ``array`` for a
brace_list -- and ``record_path``) MUST be used to open the result again: detection could take a
document of 0 or 1 records for another kind.

Parts (docs/ARCHITECTURE.md §14.4, the split of :mod:`jsonreshape.filetools`)
=============================================================================
:func:`write_part` runs the same planner, executor, validator and encoder over the positions
``[p0, p1)`` only: container kinds and single documents keep the document's head and tail in every
part (``wrapper=False``: a wrapped document's record container alone); JSON Lines and concatenated
values take the file head only in the ``first`` part and the file tail only in the ``last`` one --
any other part ends with the original gap after its last record when the next position holds that
record's neighbour of the file, else the newline -- so the parts of an unedited JSON Lines file laid
end to end ARE the file. The validator expects ``p1 - p0`` records. The caller owns naming and
committing (temporary file in, fsynced file out).

Decisions where §12.7 leaves room (all chosen to keep the user's data safe)
===========================================================================
1. ``backup=None`` (the default) means ``Settings.backup_on_save``. ``store`` (the session's
   :class:`~jsonreshape.editstore.EditStore`) is optional: without it there is no journal (an in-place
   save is still atomic where POSIX renames exist; only the fallback has a window without a journal).
2. An incomplete index is built first (no error); ``doc.error`` (a partial document) raises
   ``UserInputError("saver.error.partial", line, column)``.
3. The original gap is copied between consecutive records of the file even when one of them is edited
   (§12.7 "both are base records"), so an edit never reformats its neighbours' separators. A stream gap
   that was EMPTY (``{}{}``) next to an edited record becomes ``record_sep`` (two numbers would merge).
4. A file without records that gets records also gets the newline + indentation before its closing
   bracket when it is pretty (§12.7 only names the head's); jsonl/stream get one final newline.
5. Save As to a path that is the same file under another name (``os.path.samefile``: another spelling,
   a short 8.3 name, a hard link) takes the in-place path on the document's source path. A symlinked
   source is saved into the file it points to (its ``.bak`` and temp file next to that file).
6. ``fileio.hard_link`` treats "access denied" as "no hard links here" (some SMB servers answer so),
   so the fallback renames run; ``replace_posix`` never falls back on "access denied" (a read-only
   target must stay protected). An existing ``<source>.bak`` that is a folder ->
   ``UserInputError("fileio.error.is_directory")``.
7. The journal (``store.journal``, written once before step 3, after the temp file was written, fsynced,
   closed and given its attributes): ``{"v": 2, "op": "save", "source", "temp", "backup", "had_backup",
   "bak_read_only", "old_backup", "aside", "old_identity": FileIdentity dict, "old_file": {"dev", "ino"},
   "new": {"size", "hashes", "mtime_ns", "dev", "ino"}, "ids", "layer": {"key", "kind", "record_path",
   "base_count"}, "time"}`` -- every name decided before the first rename, so :func:`roll_journal` can
   tell from the files alone how far the save got. The content hashes cover ~2 MiB of a big file, so a
   same-size edit elsewhere leaves them equal (review S1): what decides is what a rename keeps and a
   content edit cannot fake -- the file id (``os.stat`` ``st_dev`` / ``st_ino``: volume serial number and
   file id on Windows, kept as strings) and the modification time, besides the size and the hashes.
   ``ids`` is true when the two files have distinct non-zero ids on a volume whose ids survive renames
   (NTFS, ReFS: :func:`fileio.stable_file_ids`); elsewhere (FAT, exFAT, ...) the modification time, the
   size and the hashes decide, and whatever they cannot tell apart is a conflict. ``layer`` (only with a
   real store) lets :func:`roll_journal_at` open the layer without the document. A journal of version 1
   (no ids, no times of the new file) is still rolled safely: never "completed" while its temp file
   exists, and an old and a new content the hashes cannot tell apart are a conflict.
8. :func:`roll_journal` returns a :class:`RollResult` (a ``str`` holding the localized message, with
   ``.action`` = ``"forward"`` / ``"completed"`` / ``"back"`` / ``"conflict"`` / ``"invalid"``) and, given
   ``store=``, clears the journal unless the action is ``"conflict"`` (then nothing is touched and the
   files involved are named). It decides from the files: the source is the NEW file and the temp file is
   gone -> ``"completed"`` (the save's cleanup: the previous ``.bak`` copy goes only when the new ``.bak``
   is there and is the original, the original's ``.old`` copy of a save without backup only when it is
   the original; anything else is kept and named); the source is the OLD file -> ``"back"`` (the previous
   ``.bak`` put back with its read-only bit, or our ``.bak`` link removed; then the temp file removed);
   the source is missing -> ``"forward"`` when the temp file is the complete NEW file, else ``"back"`` from
   the original's ``.old`` / ``.bak`` copy; anything else (both, neither, the temp file still there while
   the source looks new, an OS error) -> ``"conflict"``. Journal paths outside the source's folder or not
   named like this module's files -> ``"invalid"``.
9. Leftovers of interrupted saves (``.<name>.<8 hex>.jrsave`` / ``.old``, review S8) are removed by
   :func:`sweep_leftovers` -- at the start of every save of that name and by the GUI after a document
   opened or a journal was rolled -- only when the source exists; no live journal of a layer of the file
   names them (``editstore.find_pending``; a layer or the physical file held by another tab or program
   stops the sweep: its journal cannot be read); they are not in use (Windows: a sharing violation keeps
   the file); and they are older than :data:`SWEEP_MIN_AGE` (a temp file by its modification time, an
   ``.old`` copy also by its change time: a rename keeps the modification time). An ``.old`` copy goes
   only when ``<file>.bak`` exists and is at least as new (the newest backup copy never goes). Names of 48
   and more characters share their prefix with other files: their ``.old`` copies are always kept and
   nothing is removed while another file of that prefix looks interrupted.
10. :class:`SaveReport` has three extra fields: ``in_place``, ``warning_keys`` (i18n keys of
    ``warnings``) and ``broken_removed`` (JSON Lines broken lines removed).
11. The new file is a new file: it gets the folder's inherited ACL (POSIX: the original's permission
    bits are copied); the ``.bak`` hard link keeps the original's ACL and attributes. The optional
    "write the new index from the validator's spans" of §12.7 is not done (the reopen indexes the file).
12. File attributes of an in-place save (review S9, Windows): the source's hidden, system and
    not-content-indexed attributes are set on the new file, and the archive bit (the content changed:
    backup programs that use the bit must see it -- a source whose bit a backup program had cleared still
    gets it); NTFS compression (``FSCTL_SET_COMPRESSION``) and EFS encryption (``EncryptFileW``) of the
    source are applied to the still empty temp file -- when that fails the file is saved all the same with
    ``saver.warn.compression_lost`` / ``saver.warn.encryption_lost``. Read-only sources are refused
    (step 1). Save As writes a new file with the folder's defaults. A previous ``.bak`` moved aside and put
    back (an undo, a roll "back") gets its read-only bit back.
13. :func:`save_questions` lists the questions a GUI asks before it saves (a brace_list written as an
    array, JSON Lines broken lines left out, data after a gzip stream that an in-place save without a
    backup would lose); :func:`save` itself does not ask and keeps its behaviour (broken lines refuse
    without ``drop_broken``; the other two are written with a warning).
14. An interrupted save of a file that cannot be opened -- the fallback stopped between its two renames,
    the file is missing (review S2): :func:`find_journals` finds the journal from the path alone and
    :func:`roll_journal_at` rolls it under the layer's lock and clears it through the layer's store; after
    "forward" / "completed" the layer is deleted (its changes are in the file) unless it still matches the
    file. A version-1 journal cannot name its layer's document: after "forward" its layer is deleted, after
    "back" the journal stays and is rolled again ("back" once more) when the file is opened.
    :func:`layer_store_at` opens such a layer read-only (export as JSON Patch).

A private helper of another module is used deliberately: ``Document._spans_bytes(i, n, sequential=True)``
(the file spans of records ``i .. i+n-1`` in one index read, also what the reader's own windows use); a
document without it is read with ``record_span`` per record.
"""

from __future__ import annotations

import bisect
import codecs
import logging
import os
import queue
import re
import secrets
import shutil
import stat
import threading
import time
import zlib
from array import array
from dataclasses import dataclass, field
from itertools import compress, repeat
from operator import eq, ge, sub
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Iterator, Union

from . import core, editops, fileio
from .core import MODE_CONTAINER, MODE_LINES, MODE_SINGLE, MODE_STREAM
from .errors import EditError, ExportError, FileChangedError, JsonReshapeError, UserInputError
from .fileio import FileIdentity, long_path
from .i18n import has, t
from .overlay import EDIT_BASE, SEG_SHIFT
from .reader import APPENDED_WARNING, BRACE_LIST_EXAMPLE
from .settings import get_settings
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]

__all__ = [
    "BAK_SUFFIX",
    "CHUNK_BYTES",
    "GZIP_LEVEL",
    "JOURNAL_VERSION",
    "QUEUE_CHUNKS",
    "SWEEP_MIN_AGE",
    "TEMP_SUFFIX",
    "RollResult",
    "SaveQuestion",
    "SaveReport",
    "SweepResult",
    "find_journals",
    "layer_store_at",
    "roll_journal",
    "roll_journal_at",
    "save",
    "save_questions",
    "source_encoding",
    "sweep_leftovers",
    "write_part",
]

KiB = 1 << 10
MiB = 1 << 20

CHUNK_BYTES = 1 * MiB          # output chunks: validator queue items, reads of big pieces
QUEUE_CHUNKS = 8               # validator queue: at most this many chunks wait
SMALL_PIECE = 256 * KiB        # pieces at least this big are copied on their own in CHUNK_BYTES reads
BATCH_BYTES = 8 * MiB          # small pieces gathered before their reads are planned
READ_BYTES = 8 * MiB           # a coalesced read covers at most this many bytes
READ_GAP = 32 * KiB            # ... pieces at most this far apart are read together
READ_SMALL_GAP = 1 * KiB       # ... gaps this small are always read along
READ_DENSE = 0.5               # ... larger gaps while the pieces fill this share of the read
WINDOW = 65536                 # positions planned together (OverlayState.runs windows)
CLUSTER_GAP = 4096             # record ids this close are fetched with one index read / spans_in
RUN_MIN = 8                    # a run of at least this many edited records of the file is assembled at once
RUN_SUB = 2048                 # ... in parts of at most this many records
WRITE_BUFFER = 1 * MiB
GZIP_LEVEL = 6
CHANGE_CHECK_BYTES = 64 * MiB  # the source is checked against changes every this many output bytes
STALE_TEMP_SECONDS = 86400.0   # a day (leftovers this old are certainly stale; tests backdate files by it)
SWEEP_MIN_AGE = 180.0          # leftovers younger than this are never removed (a save of another program in flight)
TEMP_SUFFIX = ".jrsave"        # never ".tmp": the cache eviction removes old ".<name>.<hex>.tmp" files
OLD_SUFFIX = ".old"
BAK_SUFFIX = ".bak"
JOURNAL_VERSION = 2            # 2: file ids + modification times decide (review S1); 1 is still rolled
NAME_PREFIX = 48               # temp / .old names carry the first 48 characters of the file name

# Windows file attributes (decision 12)
_FA_READONLY = 0x1
_FA_HIDDEN = 0x2
_FA_SYSTEM = 0x4
_FA_ARCHIVE = 0x20
_FA_NORMAL = 0x80
_FA_TEMPORARY = 0x100
_FA_OFFLINE = 0x1000
_FA_COMPRESSED = 0x800
_FA_NOT_CONTENT_INDEXED = 0x2000
_FA_ENCRYPTED = 0x4000
_FA_KEEP = _FA_HIDDEN | _FA_SYSTEM | _FA_NOT_CONTENT_INDEXED          # copied from the source
_FA_SETTABLE = (_FA_READONLY | _FA_HIDDEN | _FA_SYSTEM | _FA_ARCHIVE | _FA_TEMPORARY | _FA_OFFLINE
                | _FA_NOT_CONTENT_INDEXED)                             # what SetFileAttributesW takes

_WS = b" \t\r\n"
_OPENERS = (b"[", b"{")
_CLOSERS = (b"]", b"}")

# planned output pieces: (FILE, start, end) data-file bytes, (SEG, vstart, vend) edit-layer bytes,
# (LIT, bytes, 0) literal bytes
FILE, SEG, LIT = 0, 1, 2
# (RUN, unit starts, unit ends, gap starts, gap ends): k edited records of the file that were neighbours in
# it, with the k-1 original gaps between them (arrays) -- assembled with C-level slicing, not piece by piece
RUN = 3
_EMPTY = array("Q")


# ----------------------------------------------------------------------------------
# report
# ----------------------------------------------------------------------------------
@dataclass
class SaveReport:
    """What :func:`save` wrote (docs/ARCHITECTURE.md §12.7).

    ``records``: records written; ``bytes_written``: size of the new file; ``copied_bytes``: bytes
    copied unchanged from the data file (UTF-8, before encoding); ``edited_records``: records written
    from the edit layer (edited or new); ``backup``: the ``.bak`` file of an in-place save; ``kind`` /
    ``encoding`` / ``compression``: the new file's; ``reopen_options``: the ``Document.open`` keyword
    arguments that open it as this kind (MUST be used); ``warnings``: localized texts (their keys in
    ``warning_keys``); ``in_place``; ``broken_removed``: JSON Lines broken lines left out."""

    target: Path
    records: int
    bytes_written: int
    copied_bytes: int
    edited_records: int
    backup: Path | None
    elapsed: float
    warnings: list[str]
    kind: str
    encoding: str
    compression: str | None
    reopen_options: dict
    in_place: bool = False
    warning_keys: list[str] = field(default_factory=list)
    broken_removed: int = 0

    @property
    def mb_per_s(self) -> float:
        """Bytes written per second, in MB/s."""
        return self.bytes_written / max(self.elapsed, 1e-9) / 1e6


class RollResult(str):
    """The localized message of :func:`roll_journal` (a ``str``) plus ``action``: ``"forward"`` (the
    interrupted save was completed), ``"completed"`` (it had completed; leftovers removed), ``"back"``
    (the file is as before the save), ``"conflict"`` (neither is possible: the file changed meanwhile;
    nothing was touched), ``"invalid"`` (the journal is damaged)."""

    action: str

    def __new__(cls, text: str, action: str) -> RollResult:
        obj = super().__new__(cls, text)
        obj.action = action
        return obj


class _OriginalMoved(JsonReshapeError):
    """Saving failed after the original was renamed away and it could not be put back."""

    key = "saver.error.original_moved"


@dataclass(frozen=True)
class SaveQuestion:
    """A question to ask before :func:`save` (:func:`save_questions`): ``key`` -- ``"brace_list"``,
    ``"broken_lines"`` (yes -> ``save(..., drop_broken=True)``) or ``"gzip_trailing"`` --, ``text`` the
    localized yes/no question (yes = save as described, no = do not save) and its ``params``."""

    key: str
    text: str
    params: dict = field(default_factory=dict)


def save_questions(doc: Any, target: StrPath | None = None, *, backup: bool | None = None,
                   settings: Any = None) -> list[SaveQuestion]:
    """The questions to ask, in this order, before ``doc`` is saved to ``target`` (``None`` = in place;
    decision 13) -- without touching the disk (safe in the GUI thread; a Save As target that is the source
    under another spelling counts as Save As here):

    * ``brace_list``: the file is a list in curly braces; it is written as a JSON array;
    * ``broken_lines``: JSON Lines with broken lines; they are left out (answer yes -> ``drop_broken=True``);
    * ``gzip_trailing``: an in-place save without a backup (``backup`` ``None`` = ``Settings.backup_on_save``)
      of a gzip file with data after its compressed stream: that data is not written and nothing keeps it
      (with a backup the ``.bak`` keeps the whole original; Save As leaves the source alone).

    :func:`save` itself never asks: without ``drop_broken`` it refuses broken lines; a brace_list and the
    gzip trailing data are written with a warning (``saver.warn.*``) -- the behaviour when nobody asks."""
    out: list[SaveQuestion] = []
    kind = doc.kind
    if kind == "brace_list":
        out.append(SaveQuestion("brace_list", t("saver.ask.brace_list", example=BRACE_LIST_EXAMPLE),
                                {"example": BRACE_LIST_EXAMPLE}))
    broken = int(getattr(doc, "broken_count", 0) or 0) if kind == "jsonl" else 0
    if broken:
        key = "saver.ask.broken_lines.one" if broken == 1 and has("saver.ask.broken_lines.one") \
            else "saver.ask.broken_lines"
        out.append(SaveQuestion("broken_lines", t(key, count=broken), {"count": broken}))
    in_place = target is None or fileio.normalize_path(target) == fileio.normalize_path(doc.source_path)
    if in_place and doc.info.compression == "gzip":
        if settings is None:
            settings = getattr(doc, "settings", None) or get_settings()
        keep = bool(getattr(settings, "backup_on_save", True)) if backup is None else bool(backup)
        trailing = "fileio.warning.gzip_trailing" in tuple(getattr(doc.prepared, "warnings", ()) or ())
        if trailing and not keep:
            out.append(SaveQuestion("gzip_trailing", t("saver.ask.gzip_trailing", name=Path(doc.source_path).name),
                                    {"name": Path(doc.source_path).name}))
    return out


# ----------------------------------------------------------------------------------
# save
# ----------------------------------------------------------------------------------
def save(doc: Any, target: StrPath | None = None, *, backup: bool | None = None, drop_broken: bool = False,
         settings: Any = None, progress: Progress | None = None, cancel: CancelToken | None = None,
         store: Any = None) -> SaveReport:
    """Write the effective document ``doc`` (pinned here: one overlay state is written) to ``target``
    (``None`` = in place) in its own kind and encoding, validated, and commit it atomically (module
    docstring). ``backup`` (``None`` = ``Settings.backup_on_save``) keeps ``<file>.bak`` of an in-place
    save; ``drop_broken`` removes JSON Lines broken lines (else they refuse the save); ``store`` (the
    session's EditStore) receives the save journal. Errors leave the file and any existing target
    untouched and no temporary file behind. Returns a :class:`SaveReport`."""
    started = time.perf_counter()
    check(cancel)
    pinned = getattr(doc, "pinned", None)
    if callable(pinned):
        doc = pinned()
    if settings is None:
        settings = getattr(doc, "settings", None) or get_settings()
    if backup is None:
        backup = bool(getattr(settings, "backup_on_save", True))
    if not doc.index_complete:
        doc.build_index(cancel=cancel)
    _check_document(doc)
    source = Path(doc.source_path)
    out_path, in_place = _resolve_target(doc, target)
    commit_path = _real_path(source) if in_place else out_path
    kind = doc.kind
    broken = int(getattr(doc, "broken_count", 0) or 0) if kind == "jsonl" else 0
    if broken and not drop_broken:
        raise EditError("saver.error.broken_lines", count=broken)
    if in_place:
        if fileio.is_read_only(commit_path):
            raise UserInputError("fileio.error.read_only", path=str(commit_path))
        _check_growth(doc)

    info = doc.info
    encoding = str(info.encoding)
    src_gzip = info.compression == "gzip"
    gz = src_gzip if in_place else out_path.name.lower().endswith(".gz")
    bom, utf16 = source_encoding(doc)
    style = editops.detect_style(doc)
    count = int(doc.record_count)
    mode, target_path = _validator_mode(doc)
    scanner_cls = _scanner_class(doc)
    filter_broken = bool(broken)
    total = _estimate_total(doc)
    if progress is not None:
        progress.set_total(total)
        if not progress.message:
            progress.set_message(t("saver.progress.save", name=commit_path.name))
    swept = _sweep_before_save(commit_path, doc if in_place else None, store, settings)

    fd, tmp = fileio.create_temp(commit_path, TEMP_SUFFIX)
    keep_temp = False
    backup_path: Path | None = None
    warn_keys: list[str] = []
    warn_texts: list[str] = []
    try:
        if in_place:
            fd = _prepare_temp(commit_path, tmp, fd, warn_keys, warn_texts)
        meter = _Meter(doc, progress, total, in_place, cancel)
        planner, executor, written = _write(doc, fd, commit_path, style, mode, target_path, scanner_cls, count,
                                            filter_broken, bom, utf16, gz, meter.add, cancel)
        check(cancel)
        if in_place:
            try:                      # POSIX: the file keeps its permission bits (Windows: no-op)
                shutil.copymode(long_path(commit_path), long_path(tmp))
            except OSError:  # pragma: no cover - best effort
                log.debug("could not copy the mode of %s", commit_path, exc_info=True)
            _copy_attributes(commit_path, tmp)
            new_identity = fileio.file_identity(tmp)
            try:
                backup_path, leftovers = _commit_in_place(doc, tmp, new_identity, commit_path, backup, store)
            except _OriginalMoved:
                keep_temp = True      # the journal may still roll the save forward with it
                raise
            for p in leftovers:
                _warn(warn_keys, warn_texts, "saver.warn.leftover", path=str(p))
        else:
            grown = _commit_save_as(doc, tmp, out_path)
            if grown:
                _warn(warn_keys, warn_texts, APPENDED_WARNING, bytes=format_bytes(grown))
    except BaseException:
        if not keep_temp:
            fileio.remove_quiet(tmp)
        raise

    if kind == "brace_list":
        _warn(warn_keys, warn_texts, "saver.warn.brace_list", example=BRACE_LIST_EXAMPLE)
    if broken:
        _warn(warn_keys, warn_texts, "saver.warn.broken_dropped", count=broken)
    if src_gzip:
        prepared_warnings = tuple(getattr(doc.prepared, "warnings", ()) or ())
        if "fileio.warning.gzip_trailing" in prepared_warnings:
            _warn(warn_keys, warn_texts, "saver.warn.gzip_trailing")
        if "fileio.warning.gzip_truncated" in prepared_warnings:
            _warn(warn_keys, warn_texts, "saver.warn.gzip_truncated")
    if gz != src_gzip:
        _warn(warn_keys, warn_texts, "saver.warn.compressed" if gz else "saver.warn.uncompressed")
    if swept.removed:
        _warn(warn_keys, warn_texts, "saver.sweep.removed", count=len(swept.removed), size=format_bytes(swept.freed))
    out_kind = "array" if kind == "brace_list" else kind
    record_path = doc.record_path
    report = SaveReport(
        target=source if in_place else out_path,
        records=count,
        bytes_written=written,
        copied_bytes=executor.copied,
        edited_records=planner.edited_records,
        backup=backup_path,
        elapsed=time.perf_counter() - started,
        warnings=warn_texts,
        kind=out_kind,
        encoding=encoding,
        compression="gzip" if gz else None,
        reopen_options={"format": out_kind, "record_path": tuple(record_path) if record_path is not None else None},
        in_place=in_place,
        warning_keys=warn_keys,
        broken_removed=broken,
    )
    if progress is not None:
        progress.finish()
    log.info("saved %s%s: %s %s%s, %d records (%d edited), %d bytes (%d copied) in %.2f s%s%s",
             report.target, " in place" if in_place else "", out_kind, encoding, " gzip" if gz else "",
             count, report.edited_records, written, report.copied_bytes, report.elapsed,
             f"; backup {backup_path}" if backup_path is not None else "",
             f"; warnings: {', '.join(warn_keys)}" if warn_keys else "")
    return report


def _write(doc: Any, fd: int, target: Path, style: Any, mode: int, target_path: list, scanner_cls: Any,
           count: int, filter_broken: bool, bom: bytes, utf16: str | None, gz: bool,
           meter: Callable[[int], None] | None, cancel: CancelToken | None,
           **part: Any) -> tuple[_Planner, _Executor, int]:
    """Write and validate the temp file ``fd`` (fsynced and closed on return; ``count`` records
    expected). ``meter(n)`` receives the size of every UTF-8 chunk written; ``part``: the
    :class:`_Planner` range arguments of a part (``p0``, ``p1``, ``first``, ``last``, ``wrapper``)."""
    try:
        fobj = os.fdopen(fd, "wb", buffering=WRITE_BUFFER)
    except BaseException:
        os.close(fd)
        raise
    ok = False
    try:
        sink = _Sink(fobj, target, bom=bom, utf16=utf16, gzip=gz)
        validator = _Validator(scanner_cls, mode, target_path, count, cancel)
        validator.start()
        try:
            def emit(chunk: bytes) -> None:
                validator.feed(chunk)
                sink.write(chunk)
                if meter is not None:
                    meter(len(chunk))

            with doc.open_data(buffering=0) as vf:
                planner = _Planner(doc, style, vf, filter_broken, cancel, **part)
                executor = _Executor(vf, emit, cancel, str(doc.source_path))
                executor.run(planner.batches())
            sink.close()
            validator.finish()
        except BaseException:
            validator.abort()
            raise
        try:
            fobj.flush()
            os.fsync(fobj.fileno())
            written = os.fstat(fobj.fileno()).st_size
        except OSError as exc:
            raise fileio.os_error(exc, target, writing=True) from exc
        ok = True
    finally:
        _close_output(fobj, target, raising=ok)
    if planner.broken_seen:
        log.info("%s: %d broken lines left out", doc.source_path.name, planner.broken_seen)
    return planner, executor, written


def _close_output(f: Any, target: Path, *, raising: bool) -> None:
    """Close the temp file object (its descriptor is closed even when the last flush fails). Without an
    error in flight (``raising``) a failure is raised as a localized error; while another error propagates
    -- a disk full while writing, a cancel -- it is only logged: the buffered bytes cannot be written either,
    and a raw ``OSError`` of this second flush must not replace the error being raised (review R5F-4; the
    same rule as :func:`fileio.atomic_write`)."""
    try:
        f.close()
    except OSError as exc:
        if raising:
            raise fileio.os_error(exc, target, writing=True) from exc
        log.debug("closing the temporary file of %s failed after an earlier error: %s", target, exc)


def source_encoding(doc: Any) -> tuple[bytes, str | None]:
    """``(bom, utf16)`` of the source as :func:`save` writes it again: the UTF-8 BOM iff
    ``info.encoding == "utf-8-bom"``, UTF-16 LE/BE (``utf16`` = the codec name) with its BOM iff
    the source had one (``PreparedFile.source_bom == 2``), else ``(b"", None)``."""
    encoding = str(doc.info.encoding)
    utf16 = encoding if encoding in ("utf-16-le", "utf-16-be") else None
    if encoding == "utf-8-bom":
        return codecs.BOM_UTF8, None
    if utf16 is not None and int(getattr(doc.prepared, "source_bom", 0) or 0) == 2:
        return "﻿".encode(utf16), utf16
    return b"", utf16


def write_part(doc: Any, fd: int, target: StrPath, p0: int, p1: int, *, first: bool = True, last: bool = True,
               wrapper: bool = True, drop_broken: bool = False, bom: bytes | None = None, utf16: str | None = None,
               gzip: bool | None = None, meter: Callable[[int], None] | None = None,
               cancel: CancelToken | None = None) -> int:
    """Write the records at positions ``[p0, p1)`` of ``doc`` (a pinned document: the caller pins it
    once for all its parts) into the open temporary file ``fd`` -- in the document's own kind, byte
    for byte like :func:`save` (head, records with their original gaps, tail; module docstring) --,
    validated by the core Scanner (exactly ``p1 - p0`` records), fsynced and closed (also on an
    error; the caller removes the file then). One *part* of a split (docs/ARCHITECTURE.md §14.4):

    * container kinds and single documents: every part gets the document's head and tail (a
      ``brace_list`` is written as a JSON array); ``wrapper=False``: a wrapped document's record
      container alone (a plain array / object);
    * JSON Lines and concatenated values: the file head only when ``first``, the file tail only
      when ``last``, otherwise the original gap after the last record when the next position holds
      that record's neighbour of the file, else the newline -- consecutive parts of an unedited
      file laid end to end are the file byte for byte;
    * ``drop_broken``: JSON Lines broken lines are left out (without it, a document with broken
      lines raises ``EditError("saver.error.broken_lines")``);
    * the encoding: ``bom``/``utf16`` default to the source's (:func:`source_encoding`), ``gzip``
      to "``target`` ends with ``.gz``"; ``meter(n)`` receives every UTF-8 chunk size (progress).

    ``target`` is the final name (only used in error messages). Preconditions: the index is
    complete, ``doc.error is None`` (``saver.error.partial``) and ``0 <= p0 <= p1 <=
    record_count``. Returns the bytes of the written file. ``fd`` is closed in every case (also
    when a precondition fails before anything is written)."""
    try:
        check(cancel)
        if not doc.index_complete:
            doc.build_index(cancel=cancel)
        _check_document(doc)
        count = int(doc.record_count)
        p0, p1 = int(p0), int(p1)
        if not 0 <= p0 <= p1 <= count:
            raise ValueError(f"write_part: positions [{p0}, {p1}) outside 0..{count}")
        broken = int(getattr(doc, "broken_count", 0) or 0) if doc.kind == "jsonl" else 0
        if broken and not drop_broken:
            raise EditError("saver.error.broken_lines", count=broken)
        target = Path(os.path.abspath(os.fspath(target)))
        src_bom, src_utf16 = source_encoding(doc)
        if bom is None:
            bom, utf16 = src_bom, src_utf16
        gz = target.name.lower().endswith(".gz") if gzip is None else bool(gzip)
        if doc.kind == "wrapped" and not wrapper:
            mode, target_path = MODE_CONTAINER, []
        else:
            mode, target_path = _validator_mode(doc)
        style = editops.detect_style(doc)
    except BaseException:
        os.close(fd)
        raise
    _planner, _executor, written = _write(doc, fd, target, style, mode, target_path, _scanner_class(doc), p1 - p0,
                                          bool(broken), bom, utf16, gz, meter, cancel, p0=p0, p1=p1, first=first,
                                          last=last, wrapper=wrapper)
    return written


# ----------------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------------
def _check_document(doc: Any) -> None:
    err = doc.error
    if err is not None:
        raise UserInputError("saver.error.partial", line=getattr(err, "line", 0), column=getattr(err, "column", 0))
    if doc.kind == "wrapped" and doc.container_span is None:   # pragma: no cover - a complete wrapped document
        raise UserInputError("saver.error.partial", line=0, column=0)


def _resolve_target(doc: Any, target: StrPath | None) -> tuple[Path, bool]:
    """``(path, in_place)``: ``None``, the source path or the same file under another name -> in place."""
    source = Path(doc.source_path)
    if target is None:
        return source, True
    out = Path(os.path.abspath(os.fspath(target)))
    if fileio.normalize_path(out) == fileio.normalize_path(source):
        return source, True
    try:
        if os.path.exists(long_path(out)) and os.path.samefile(long_path(out), long_path(source)):
            return source, True
    except OSError:
        pass
    if os.path.isdir(long_path(out)):
        raise UserInputError("fileio.error.is_directory", path=str(out))
    if not os.path.isdir(long_path(out.parent)):
        raise UserInputError("saver.error.no_dir", path=str(out.parent))
    prepared = getattr(doc, "prepared", None)
    if prepared is not None and prepared.converted \
            and fileio.normalize_path(out) == fileio.normalize_path(doc.data_path):
        raise UserInputError("saver.error.target_cache", path=str(out))
    return out, False


def _real_path(source: Path) -> Path:
    """The file an in-place save replaces: a symlinked source is saved into its target."""
    try:
        if os.path.islink(long_path(source)):
            return Path(os.path.realpath(source))
    except OSError:  # pragma: no cover - defensive
        pass
    return source


def _check_growth(doc: Any) -> None:
    """The source must be exactly the version opened (``fileio.appended_bytes`` == 0)."""
    grown = fileio.appended_bytes(doc.source_path, doc.identity)
    if grown is None:
        raise FileChangedError(path=str(doc.source_path))
    if grown:
        raise EditError("saver.error.grown", bytes=format_bytes(grown), path=str(doc.source_path))


def _validator_mode(doc: Any) -> tuple[int, list]:
    kind = doc.kind
    if kind == "wrapped":
        return MODE_CONTAINER, list(doc.record_path or ())
    if kind in ("array", "keyed", "brace_list"):
        return MODE_CONTAINER, []
    return {"jsonl": MODE_LINES, "stream": MODE_STREAM, "single": MODE_SINGLE}[kind], []


def _scanner_class(doc: Any) -> Any:
    """The core Scanner of the backend the document was indexed with."""
    name = getattr(doc, "backend", None)
    if name in ("rust", "python"):
        try:
            return core.get_backend(name).Scanner
        except ImportError:  # pragma: no cover - the document was indexed with it
            pass
    return core.Scanner


def _estimate_total(doc: Any) -> int:
    """Expected bytes of the UTF-8 output (progress total; O(1)): the bytes of the file, the records'
    part scaled by the effective record count of an edited state."""
    try:
        base = doc.pinned(None)
        n = int(base.base_record_count)
        extent = base.record_span(n - 1)[1] - base.record_span(0)[0] if n else 0
        around = max(0, int(doc.data_size) - int(doc.info.data_start) - extent)
        ov = doc.overlay
        if ov is not None and n:
            extent = extent * int(ov.count) // n
        return max(1, extent + around)
    except Exception:  # pragma: no cover - an estimate only
        return max(1, int(doc.data_size))


def _sweep_before_save(target: Path, doc: Any, store: Any, settings: Any) -> SweepResult:
    """:func:`sweep_leftovers` next to ``target`` before a save writes there (decision 9); never fails the
    save (``store``: the session's EditStore -- its layer and lock are ours)."""
    lock = getattr(store, "lock", None)
    own_key = getattr(store, "key", None)
    try:
        return sweep_leftovers(target, doc=doc, own_key=own_key if isinstance(own_key, str) else None,
                               own_file_lock=bool(getattr(lock, "held", False)), settings=settings)
    except Exception:  # housekeeping only
        log.warning("removing leftovers of interrupted saves next to %s failed", target, exc_info=True)
        return SweepResult(skipped="error")


def _prepare_temp(source: Path, tmp: Path, fd: int, warn_keys: list[str], warn_texts: list[str]) -> int:
    """NTFS compression / EFS encryption of the still empty temp file like the source's (decision 12).
    Returns the descriptor to write (reopened after the encryption, which needs the file closed). A
    failure is a warning; only a failed reopen raises (the descriptor is closed then)."""
    attrs = fileio.file_attributes(source) or 0
    if not attrs & (_FA_COMPRESSED | _FA_ENCRYPTED):
        return fd
    have = fileio.file_attributes(tmp) or 0
    if attrs & _FA_COMPRESSED and not have & _FA_COMPRESSED and not fileio.set_compressed(tmp):
        log.warning("the new %s cannot be compressed like the original", source)
        _warn(warn_keys, warn_texts, "saver.warn.compression_lost")
    if attrs & _FA_ENCRYPTED and not have & _FA_ENCRYPTED:
        os.close(fd)                      # EncryptFileW needs the file closed
        ok = fileio.encrypt_file(tmp)
        flags = os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        try:
            fd = os.open(long_path(tmp), flags)
        except OSError as exc:
            raise fileio.os_error(exc, source, writing=True) from exc
        if not ok:
            log.warning("the new %s cannot be encrypted like the original (EFS)", source)
            _warn(warn_keys, warn_texts, "saver.warn.encryption_lost")
    return fd


def _copy_attributes(source: Path, tmp: Path) -> None:
    """The source's hidden / system / not-content-indexed attributes and the archive bit on the written
    temp file (decision 12; Windows, best effort: a failure is logged)."""
    attrs = fileio.file_attributes(source)
    have = fileio.file_attributes(tmp)
    if attrs is None or have is None:
        return
    current = have & _FA_SETTABLE
    want = (current & ~(_FA_KEEP | _FA_READONLY)) | (attrs & _FA_KEEP) | _FA_ARCHIVE
    if want != current and not fileio.set_file_attributes(tmp, want or _FA_NORMAL):
        log.warning("the attributes of %s could not be given to the new file", source)


def _warn(keys: list[str], texts: list[str], key: str, **params: Any) -> None:
    count = params.get("count")
    text_key = key + ".one" if count == 1 and has(key + ".one") else key
    keys.append(key)
    texts.append(t(text_key, **params))


# ----------------------------------------------------------------------------------
# planning: the output as pieces
# ----------------------------------------------------------------------------------
def _segment(offset: int) -> int:
    return (offset - EDIT_BASE) >> SEG_SHIFT


def _clusters(ranges: list[tuple[int, int]], dense: bool) -> list[tuple[int, int]]:
    """Sorted record-id ranges merged into clusters fetched together: neighbours at most
    :data:`CLUSTER_GAP` ids apart join while the cluster spans at most ``2 * WINDOW`` ids and --
    ``dense`` (index reads, whose cost is the extent) -- stays at least half covered."""
    out: list[list[int]] = []
    limit = 2 * WINDOW
    for lo, hi in ranges:
        if out:
            c = out[-1]
            top = hi if hi > c[1] else c[1]
            if lo - c[1] <= CLUSTER_GAP and top - c[0] <= limit and (
                    not dense or 2 * (c[2] + hi - lo) >= top - c[0]):
                c[1] = top
                c[2] += hi - lo
                continue
        out.append([lo, hi, hi - lo])
    return [(c[0], c[1]) for c in out]


class _WindowSpans:
    """The spans one window of runs needs, fetched in a few batched reads: the file spans of its records
    of the file (one index read per cluster of nearby ids; of an untouched run only its first and last
    record unless ``whole_runs``) and the content spans of its edited records (one ``spans_in`` per layer
    and cluster). A run whose ids several layers share is resolved by ``OverlayState.contents``."""

    __slots__ = ("blo", "bspans", "clo", "ctabs", "ov")

    def __init__(self, planner: _Planner, ov: Any, runs: list[tuple[int, int, int, bool]],
                 whole_runs: bool = False) -> None:
        n_base = planner.N
        self.ov = ov
        self.blo: list[int] = []
        self.bspans: list[array] = []
        ranges: list[tuple[int, int]] = []
        for _p, r, k, pure in runs:
            if r >= n_base:
                continue
            if pure and k > 2 and not whole_runs:
                ranges.append((r, r + 1))
                ranges.append((r + k - 1, r + k))
            else:
                ranges.append((r, min(r + k, n_base)))
        ranges.sort()
        for lo, hi in _clusters(ranges, True):
            self.blo.append(lo)
            self.bspans.append(planner._spans(lo, hi - lo))
        self.clo: list[int] = []
        self.ctabs: list[list[tuple[Any, Any]]] = []
        impure = sorted((r, r + k) for _p, r, k, pure in runs if not pure)
        if impure and ov is not None:
            layers = [layer for layer in reversed(ov.layers) if len(layer)]     # newest first
            for lo, hi in _clusters(impure, False):
                tabs = []
                for layer in layers:
                    keys, pairs = layer.spans_in(lo, hi)
                    if len(keys):
                        tabs.append((keys, pairs))
                self.clo.append(lo)
                self.ctabs.append(tabs)

    def base(self, rid: int, n: int) -> array:
        """Flat file spans of the file's records ``rid .. rid+n-1`` (fetched as one range)."""
        c = bisect.bisect_right(self.blo, rid) - 1
        o = 2 * (rid - self.blo[c])
        return self.bspans[c][o:o + 2 * n]

    def ends(self, rid: int, n: int) -> tuple[int, int]:
        """``(start of rid, end of rid+n-1)`` of an untouched run."""
        blo, bspans = self.blo, self.bspans
        c = bisect.bisect_right(blo, rid) - 1
        first = bspans[c][2 * (rid - blo[c])]
        last_rid = rid + n - 1
        c = bisect.bisect_right(blo, last_rid) - 1
        return first, bspans[c][2 * (last_rid - blo[c]) + 1]

    def content(self, rid: int, n: int) -> array:
        """Flat content spans of the edited records ``rid .. rid+n-1`` (the newest layer that has them)."""
        c = bisect.bisect_right(self.clo, rid) - 1
        if c >= 0:
            end = rid + n
            for keys, pairs in self.ctabs[c]:
                j = bisect.bisect_left(keys, rid)
                if j >= len(keys) or keys[j] >= end:
                    continue                      # this layer has none of them
                if keys[j] == rid and j + n <= len(keys) and keys[j + n - 1] == end - 1:
                    return pairs[2 * j:2 * (j + n)]
                break                             # several layers share the run
        return self.ov.contents(rid, n)


class _Planner:
    """The output of a document as pieces in order (module docstring "What is written").

    A *part* (``write_part``, the split of docs/ARCHITECTURE.md §14.4) is the records at positions
    ``[p0, p1)``: container kinds and single documents get the whole head and tail of the document
    (``wrapper=False``: a wrapped document's record container only, without the wrapper); JSON
    Lines and concatenated values get the file head only when ``first`` and the file tail only
    when ``last``, otherwise the original gap after the part's last record when the next position
    holds its neighbour of the file, else the newline -- so the parts of a split laid end to end
    are the document."""

    def __init__(self, doc: Any, style: Any, reader: Any, filter_broken: bool, cancel: CancelToken | None, *,
                 p0: int = 0, p1: int | None = None, first: bool = True, last: bool = True,
                 wrapper: bool = True) -> None:
        self.doc = doc
        self.ov = doc.overlay
        self.kind = doc.kind
        self.n = int(doc.record_count)
        self.N = int(doc.base_record_count)
        self.style = style
        self.sep = bytes(style.record_sep)
        self.f = reader
        self.filter = filter_broken
        self.cancel = cancel
        self.data_start = int(doc.info.data_start)
        self.data_size = int(doc.data_size)
        self.p0 = max(0, int(p0))
        self.p1 = self.n if p1 is None else min(self.n, int(p1))
        self.first = bool(first)
        self.last = bool(last)
        self.wrapper = bool(wrapper)
        self.last_prev: tuple[int, bool, int] = (-1, False, -1)
        self.edited_records = 0
        self.broken_seen = 0
        self._spans_fn: Callable[..., bytes] | None = getattr(doc, "_spans_bytes", None)
        self._base: Any = None
        self._trimmed: int | None = None

    # -- reading --------------------------------------------------------------------
    def _read(self, a: int, b: int) -> bytes:
        f = self.f
        f.seek(a)
        data = f.read(b - a)
        if len(data) != b - a:
            raise FileChangedError(path=str(self.doc.source_path))
        return data

    def _byte_is(self, offset: int, allowed: tuple[bytes, ...]) -> None:
        """The data file still holds one of ``allowed`` at ``offset`` (else it changed)."""
        if self._read(offset, offset + 1) not in allowed:
            raise FileChangedError(path=str(self.doc.source_path))

    def _spans(self, rid: int, k: int) -> array:
        """Flat ``(start, end)`` data-file spans of the file's records ``rid .. rid+k-1``."""
        fn = self._spans_fn
        if fn is not None:
            out = array("Q")
            out.frombytes(fn(rid, k, sequential=True))
            if len(out) == 2 * k:
                return out
        if self._base is None:   # pragma: no cover - a reader without the batch read
            self._base = self.doc.pinned(None)
        out = array("Q")
        for i in range(rid, rid + k):
            out.extend(self._base.record_span(i))
        return out

    def _trimmed_end(self) -> int:
        """Offset after the last non-whitespace byte of the data file."""
        if self._trimmed is None:
            pos = self.data_size
            lo = self.data_start
            while pos > lo:
                start = max(lo, pos - 4 * KiB)
                stripped = self._read(start, pos).rstrip(_WS)
                if stripped:
                    pos = start + len(stripped)
                    break
                pos = start
            self._trimmed = pos
        return self._trimmed

    # -- the whole output -------------------------------------------------------------------
    def batches(self) -> Iterator[list[tuple]]:
        """The output as lists of pieces, in order (one list per window of records, plus the head and
        the tail)."""
        if self.kind in ("jsonl", "stream"):
            if self.p1 <= self.p0:
                return                      # an empty output (reopened with 0 records)
            if self.first and self.N:
                yield self._copy(self.data_start, self._spans(0, 1)[0])
            yield from self._body()
            if self.last:
                if self.N:
                    yield self._copy(self._spans(self.N - 1, 1)[1], self.data_size)
                else:
                    yield [(LIT, self.style.newline, 0)]
                return
            # a part that is not the last one: the original gap after its last record when the next
            # position holds that record's neighbour of the file, else a line end
            p_rid, _pure, p_end = self.last_prev
            nxt = self.ov.rid_at(self.p1) if self.ov is not None else self.p1
            if p_end >= 0 and nxt == p_rid + 1 and nxt < self.N:
                yield self._copy(p_end, self._spans(nxt, 1)[0])
            else:
                yield [(LIT, self.style.newline, 0)]
            return
        head, tail, empty = self._frame()
        if self.p1 <= self.p0:
            yield empty
            return
        yield head
        yield from self._body()
        yield tail

    def _open_indent(self) -> list[tuple]:
        unit = self.style.unit
        if unit is None:
            return []
        return [(LIT, self.style.newline + (unit * self.style.record_level).encode("ascii"), 0)]

    def _close_indent(self) -> list[tuple]:
        unit = self.style.unit
        if unit is None:
            return []
        return [(LIT, self.style.newline + (unit * max(0, self.style.record_level - 1)).encode("ascii"), 0)]

    def _frame(self) -> tuple[list[tuple], list[tuple], list[tuple]]:
        """``(head, tail, zero-record output)`` pieces of the container kinds and single documents."""
        ds, dz, N = self.data_start, self.data_size, self.N
        empty_lit = (LIT, bytes(self.style.empty), 0)
        if self.kind == "wrapped":
            if not self.wrapper:         # the record container alone (a part without the wrapper)
                prefix: list[tuple] = []
                suffix: list[tuple] = []
            else:
                (ps, pe), (ss, se) = self.doc.wrapper_spans()
                if ps >= EDIT_BASE:      # the edited wrapper text W' + the file's whitespace around the root value
                    te = self._trimmed_end()
                    prefix = [(FILE, ds, int(self.doc.info.root_start)), (SEG, ps, pe)]
                    suffix = [(SEG, ss, se), (FILE, te, dz)]
                else:
                    prefix = [(FILE, ps, pe)]
                    suffix = [(FILE, ss, se)]
            cs0, cs1 = self.doc.container_span
            self._byte_is(cs0, _OPENERS)
            self._byte_is(cs1 - 1, _CLOSERS)
            empty = prefix + [empty_lit] + suffix
            if N:
                head = prefix + [(FILE, cs0, self._spans(0, 1)[0])]
                tail = [(FILE, self._spans(N - 1, 1)[1], cs1)] + suffix
            else:
                head = prefix + [(FILE, cs0, cs0 + 1)] + self._open_indent()
                tail = self._close_indent() + [(FILE, cs1 - 1, cs1)] + suffix
            return head, tail, empty
        rs = int(self.doc.info.root_start)
        te = self._trimmed_end()
        if self.kind == "single":
            s0, e0 = self._spans(0, 1) if N else (rs, te)
            return [(FILE, ds, s0)], [(FILE, e0, dz)], [(FILE, ds, rs), empty_lit, (FILE, te, dz)]
        self._byte_is(rs, _OPENERS)
        self._byte_is(te - 1, _CLOSERS)
        lead = [(FILE, ds, rs)]
        trail = [(FILE, te, dz)]
        if self.kind == "brace_list":                         # written as a JSON array
            opener = [(LIT, b"[", 0)]
            closer = [(LIT, b"]", 0)]
        else:
            opener = [(FILE, rs, rs + 1)]
            closer = [(FILE, te - 1, te)]
        empty = lead + [empty_lit] + trail
        if N:
            head = lead + opener + [(FILE, rs + 1, self._spans(0, 1)[0])]
            tail = [(FILE, self._spans(N - 1, 1)[1], te - 1)] + closer + trail
        else:
            head = lead + opener + self._open_indent()
            tail = self._close_indent() + closer + trail
        return head, tail, empty

    # -- the records -----------------------------------------------------------------------
    def _body(self) -> Iterator[list[tuple]]:
        """The records at positions ``[p0, p1)`` with the bytes between them, one list of pieces per
        window of positions (``last_prev`` holds the state after the last one)."""
        p1, ov = self.p1, self.ov
        prev = (-1, False, -1)          # (rid of the previous output record or -1, pure, its file end or -1)
        for w in range(self.p0, p1, WINDOW):
            check(self.cancel)
            w1 = min(p1, w + WINDOW)
            runs = list(ov.runs(w, w1)) if ov is not None else [(w, w, w1 - w, True)]
            out: list[tuple] = []
            prev = self._window(runs, _WindowSpans(self, ov, runs, self.filter), out, prev)
            yield out
        self.last_prev = prev

    def _window(self, runs: list[tuple[int, int, int, bool]], win: _WindowSpans, out: list[tuple],
                prev: tuple[int, bool, int]) -> tuple[int, bool, int]:
        """Append the pieces of ``runs`` to ``out``: untouched runs as one file range each, edited and
        new records one by one, and between two records the original gap when they were neighbours in
        the file, else the record separator (§12.7)."""
        n_base = self.N
        stream = self.kind == "stream"
        filt = self.filter
        sep = (LIT, self.sep, 0)
        append = out.append
        p_rid, p_pure, p_end = prev
        for _pos, r, k, pure in runs:
            if pure:
                if filt:
                    sp = win.base(r, k)
                    first, last = sp[0], sp[-1]
                else:
                    first, last = win.ends(r, k)
                if p_rid >= 0:
                    if r == p_rid + 1:          # (a run of the file's records: r < n_base)
                        if stream and p_end == first and not p_pure:
                            append(sep)        # values that touched in the file, one of them edited
                        elif filt:
                            out.extend(self._filtered(p_end, first))
                        else:
                            append((FILE, p_end, first))
                    else:
                        append(sep)
                if filt:
                    self._filtered_run(sp, out)
                else:
                    append((FILE, first, last))
                p_rid, p_pure, p_end = r + k - 1, True, last
                continue
            cont = win.content(r, k)
            nb = min(k, n_base - r) if r < n_base else 0
            self.edited_records += k
            if nb:
                bsp = win.base(r, nb)
                bs, be = bsp[0::2], bsp[1::2]
            else:
                bs = be = _EMPTY
            cs, ce = cont[0::2], cont[1::2]
            if nb == k and k >= RUN_MIN and not filt:
                gs, ge = be[:-1], bs[1:]
                if not (stream and any(map(eq, gs, ge))):
                    if p_rid >= 0:                     # the gap before the run's first record
                        if r == p_rid + 1:
                            if stream and p_end == bs[0]:
                                append(sep)
                            else:
                                append((FILE, p_end, bs[0]))
                        else:
                            append(sep)
                    append((RUN, cs, ce, gs, ge))
                    p_rid, p_pure, p_end = r + k - 1, False, be[k - 1]
                    continue
            for i in range(k):
                rid = r + i
                if p_rid >= 0:
                    if rid == p_rid + 1 and i < nb:
                        g1 = bs[i]
                        if stream and p_end == g1:
                            append(sep)
                        elif filt:
                            out.extend(self._filtered(p_end, g1))
                        else:
                            append((FILE, p_end, g1))
                    else:
                        append(sep)
                s = cs[i]
                append((SEG if s >= EDIT_BASE else FILE, s, ce[i]))
                p_rid = rid
                p_end = be[i] if i < nb else -1
            p_pure = False
        return p_rid, p_pure, p_end

    def _copy(self, a: int, b: int) -> list[tuple]:
        """The data-file range ``[a, b)`` (JSON Lines with broken lines: filtered)."""
        if self.filter:
            return list(self._filtered(a, b))
        return [(FILE, a, b)]

    # -- JSON Lines broken lines -------------------------------------------------------------
    def _filtered_run(self, sp: array, out: list[tuple]) -> None:
        """A run of the file's records (flat spans ``sp``) whose gaps may hold broken lines: only a gap
        of at least 3 bytes can (``\\n`` + a non-whitespace byte + ``\\n``)."""
        starts = sp[0::2]
        ends = sp[1::2]
        k = len(starts)
        seg_start = starts[0]
        if k > 1:
            lens = map(sub, starts[1:], ends[:-1])
            for i in compress(range(k - 1), map(ge, lens, repeat(3))):
                g0, g1 = ends[i], starts[i + 1]
                if not self._has_content(g0, g1):
                    continue
                out.append((FILE, seg_start, g0))
                out.extend(self._filtered(g0, g1))
                seg_start = g1
        out.append((FILE, seg_start, ends[-1]))

    def _has_content(self, a: int, b: int) -> bool:
        pos = a
        while pos < b:
            stop = min(b, pos + CHUNK_BYTES)
            if self._read(pos, stop).strip(_WS):
                return True
            pos = stop
        return False

    def _filtered(self, a: int, b: int) -> Iterator[tuple]:
        """``[a, b)`` of a JSON Lines file without its broken lines: a line holding a non-whitespace
        byte is left out with its line end (an unterminated last one without; blank lines stay)."""
        pending = bytearray()          # the current line so far (whitespace only)
        broken = False                 # the current line holds a non-whitespace byte
        pos = a
        while pos < b:
            check(self.cancel)
            stop = min(b, pos + CHUNK_BYTES)
            chunk = self._read(pos, stop)
            pos = stop
            out = bytearray()
            i = 0
            m = len(chunk)
            while i < m:
                nl = chunk.find(b"\n", i)
                end = m if nl < 0 else nl
                if not broken:
                    part = chunk[i:end]
                    if part.strip(_WS):
                        broken = True
                        pending.clear()
                    else:
                        pending += part
                if nl < 0:
                    break
                if broken:
                    self.broken_seen += 1
                else:
                    out += pending
                    out += b"\n"
                pending.clear()
                broken = False
                i = nl + 1
            if out:
                yield (LIT, bytes(out), 0)
        if broken:
            self.broken_seen += 1
        elif pending:
            yield (LIT, bytes(pending), 0)


# ----------------------------------------------------------------------------------
# reading the pieces
# ----------------------------------------------------------------------------------
class _Executor:
    """Reads the planned pieces and hands the output on in chunks of about :data:`CHUNK_BYTES`."""

    def __init__(self, reader: Any, emit: Callable[[bytes], None], cancel: CancelToken | None, path: str) -> None:
        self.f = reader
        self.emit = emit
        self.cancel = cancel
        self.path = path
        self.batch: list[tuple] = []
        self.batch_bytes = 0
        self.parts: list[Any] = []
        self.parts_bytes = 0
        self.copied = 0                # bytes copied from the data file

    def run(self, batches: Iterable[list[tuple]]) -> None:
        """Read and hand on the pieces of ``batches`` in order. Adjacent file ranges (and adjacent ranges
        of one segment) are merged first, so a run of untouched records with the gaps around it becomes
        one range; empty pieces are dropped."""
        cur: tuple | None = None
        take = self._take
        for pieces in batches:
            for p in pieces:
                kind = p[0]
                if kind == LIT:
                    if not p[1]:
                        continue
                elif kind != RUN:
                    a, b = p[1], p[2]
                    if b <= a:
                        if b < a:
                            raise ExportError("saver.error.invalid_output", detail=t(
                                "saver.detail.json", reason=t("json.error.unexpected_char"), offset=a))
                        continue
                    if cur is not None and cur[0] == kind and cur[2] == a:
                        cur = (kind, cur[1], b)
                        continue
                if cur is not None:
                    take(cur)
                cur = p
        if cur is not None:
            take(cur)
        self._flush_batch()
        self._flush_parts()

    def _take(self, p: tuple) -> None:
        kind = p[0]
        if kind == RUN:
            self._flush_batch()
            self._run_piece(p)
            return
        if kind == LIT:
            self.batch.append(p)
            self.batch_bytes += len(p[1])
        else:
            size = p[2] - p[1]
            if kind == FILE:
                self.copied += size
            if size >= SMALL_PIECE:
                self._flush_batch()
                self._stream(kind, p[1], p[2])
                return
            self.batch.append(p)
            self.batch_bytes += size
        if self.batch_bytes >= BATCH_BYTES:
            self._flush_batch()

    def _read(self, kind: int, a: int, b: int) -> bytes:
        f = self.f
        f.seek(a)
        data = f.read(b - a)
        if len(data) != b - a:
            if kind == FILE:
                raise FileChangedError(path=self.path)
            raise EditError("edits.error.layer_corrupt", path=self.path,       # pragma: no cover - DatReader raises
                            detail=f"short read of the span ({a}, {b})")
        return data

    def _stream(self, kind: int, a: int, b: int) -> None:
        self._flush_parts()
        pos = a
        while pos < b:
            check(self.cancel)
            stop = min(b, pos + CHUNK_BYTES)
            self.emit(self._read(kind, pos, stop))
            pos = stop

    def _flush_batch(self) -> None:
        batch = self.batch
        if not batch:
            return
        self.batch = []
        self.batch_bytes = 0
        check(self.cancel)
        bufs = self._fetch(batch)
        parts = self.parts
        for idx, p in enumerate(batch):
            if p[0] == LIT:
                part = p[1]
            else:
                buf, off = bufs[idx]
                part = buf[off:off + p[2] - p[1]]
            parts.append(part)
            self.parts_bytes += len(part)
            if self.parts_bytes >= CHUNK_BYTES:
                self._flush_parts()
                parts = self.parts

    def _flush_parts(self) -> None:
        parts = self.parts
        if not parts:
            return
        data = b"".join(parts) if len(parts) > 1 else bytes(parts[0])
        self.parts = []
        self.parts_bytes = 0
        if len(data) <= 2 * CHUNK_BYTES:
            self.emit(data)
            return
        view = memoryview(data)                 # bytes are immutable: slices can be handed on as they are
        for off in range(0, len(data), CHUNK_BYTES):
            self.emit(view[off:off + CHUNK_BYTES])

    def _run_piece(self, p: tuple) -> None:
        """A RUN piece: in parts of at most :data:`RUN_SUB` records, the units are read with one read
        (when they lie close together in one space) and the gaps with one read of the file range around
        them, and the output is assembled with C-level slicing; parts whose bytes are spread out go
        piece by piece."""
        _kind, cs, ce, gs, ge = p
        k = len(cs)
        i = 0
        while i < k:
            check(self.cancel)
            j = min(k, i + RUN_SUB)
            ng = min(j, k - 1) - i             # the gaps after units i .. i+ng-1
            if ng > 0 and ge[i + ng - 1] - gs[i] > READ_BYTES:
                j = max(i + 1, bisect.bisect_right(ge, gs[i] + READ_BYTES, i, i + ng))
                ng = min(j, k - 1) - i
            us, ue = cs[i:j], ce[i:j]
            lo, hi = min(us), max(ue)
            unit_bytes = sum(ue) - sum(us)
            same_space = (lo >= EDIT_BASE and _segment(lo) == _segment(hi - 1)) or hi <= EDIT_BASE
            if not same_space or hi - lo > READ_BYTES or hi - lo > 2 * unit_bytes + READ_GAP or (
                    ng and ge[i + ng - 1] - gs[i] > READ_BYTES):
                take = self._take
                for t in range(i, j):
                    take((SEG if cs[t] >= EDIT_BASE else FILE, cs[t], ce[t]))
                    if t < i + ng:
                        take((FILE, gs[t], ge[t]))
                i = j
                continue
            self._flush_batch()
            ukind = SEG if lo >= EDIT_BASE else FILE
            ubuf = memoryview(self._read(ukind, lo, hi))
            parts = list(map(ubuf.__getitem__, map(slice, map(sub, us, repeat(lo)), map(sub, ue, repeat(lo)))))
            total = unit_bytes
            if ukind == FILE:
                self.copied += unit_bytes
            if ng:
                gss, ges = gs[i:i + ng], ge[i:i + ng]
                g0 = gss[0]
                gbuf = memoryview(self._read(FILE, g0, ges[-1]))
                gaps = list(map(gbuf.__getitem__, map(slice, map(sub, gss, repeat(g0)), map(sub, ges, repeat(g0)))))
                gap_bytes = sum(ges) - sum(gss)
                total += gap_bytes
                self.copied += gap_bytes
                both: list[Any] = [None] * (len(parts) + ng)
                both[0::2] = parts
                both[1::2] = gaps
                parts = both
            self.parts.extend(parts)
            self.parts_bytes += total
            if self.parts_bytes >= CHUNK_BYTES:
                self._flush_parts()
            i = j

    def _fetch(self, batch: list[tuple]) -> dict[int, tuple[memoryview, int]]:
        """Read the file pieces and the edit-layer pieces of ``batch`` with coalesced reads (each space,
        and each segment, on its own): ``{piece index: (buffer, offset)}``."""
        out: dict[int, tuple[memoryview, int]] = {}
        for kind in (FILE, SEG):
            idx = sorted((i for i, p in enumerate(batch) if p[0] == kind), key=lambda i: batch[i][1])
            m = len(idx)
            j = 0
            while j < m:
                first = idx[j]
                lo, hi = batch[first][1], batch[first][2]
                seg = _segment(lo) if kind == SEG else -1
                selected = hi - lo
                group = [first]
                j += 1
                while j < m:
                    i = idx[j]
                    s, e = batch[i][1], batch[i][2]
                    top = e if e > hi else hi
                    gap = s - hi
                    if top - lo > READ_BYTES or gap > READ_GAP or (kind == SEG and _segment(s) != seg):
                        break
                    if gap > READ_SMALL_GAP and selected + (e - s) < READ_DENSE * (top - lo):
                        break
                    group.append(i)
                    hi = top
                    selected += e - s
                    j += 1
                data = memoryview(self._read(kind, lo, hi))
                for i in group:
                    out[i] = (data, batch[i][1] - lo)
        return out


# ----------------------------------------------------------------------------------
# validation, encoding, progress
# ----------------------------------------------------------------------------------
_END = object()


class _Validator:
    """A core Scanner in a helper thread checking the UTF-8 output (module docstring)."""

    def __init__(self, scanner_cls: Any, mode: int, target: list, expected: int, cancel: CancelToken | None) -> None:
        self.mode = mode
        self.expected = expected
        self.cancel = cancel
        self.scanner = scanner_cls(mode, list(target), 0)
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_CHUNKS)
        self.problem: tuple | None = None       # ("json", error) | ("broken", entry) | ("count", found)
        self.exc: BaseException | None = None
        self.records = 0
        self.fed = 0
        self._stop = False
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jsonreshape-save-check", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def feed(self, chunk: Any) -> None:
        """Queue ``chunk`` (blocks while the queue is full; raises at once when the check failed)."""
        while True:
            if self._done.is_set():
                self._raise()
                raise RuntimeError("the output check ended early")   # pragma: no cover - defensive
            check(self.cancel)
            try:
                self.q.put(chunk, timeout=0.05)
                return
            except queue.Full:
                continue

    def finish(self) -> int:
        """End of the output: wait for the check; :class:`ExportError` when it failed."""
        self.feed(_END)
        while not self._done.wait(0.05):
            check(self.cancel)
        self._thread.join()
        self._raise()
        return self.records

    def abort(self) -> None:
        """Stop the helper thread (an error or a cancellation of the writer)."""
        self._stop = True
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        try:
            self.q.put_nowait(_END)
        except queue.Full:  # pragma: no cover - just drained
            pass
        self._thread.join(10)

    def _raise(self) -> None:
        if self.exc is not None:
            raise self.exc
        problem = self.problem
        if problem is None:
            return
        what, value = problem
        if what == "json":
            offset, code, expected = int(value[0]), str(value[1]), str(value[2])
            detail = t("saver.detail.json", reason=_reason(code, expected), offset=offset)
        elif what == "broken":
            line_no, _start, _end, offset, code, expected = value
            detail = t("saver.detail.broken", line=int(line_no) + 1, reason=_reason(str(code), str(expected)))
        else:
            detail = t("saver.detail.count", found=value, expected=self.expected)
        raise ExportError("saver.error.invalid_output", detail=detail)

    def _run(self) -> None:
        sc = self.scanner
        lines = self.mode == MODE_LINES
        try:
            while True:
                item = self.q.get()
                if item is _END or self._stop:
                    break
                sc.feed(item)
                self.fed += len(item)
                if lines:
                    broken = sc.take_broken()
                    if broken:
                        self.problem = ("broken", tuple(broken[0]))
                        log.error("save check: broken line in the output: %r", broken[0])
                        return
                if sc.error is not None:
                    self.problem = ("json", tuple(sc.error))
                    log.error("save check: invalid output: %r", sc.error)
                    return
            if self._stop:
                return
            sc.finish()
            if lines:
                broken = sc.take_broken()
                if broken:
                    self.problem = ("broken", tuple(broken[0]))
                    log.error("save check: broken line in the output: %r", broken[0])
                    return
            if sc.error is not None:
                self.problem = ("json", tuple(sc.error))
                log.error("save check: invalid output: %r", sc.error)
                return
            self.records = int(sc.record_count)
            if self.records != self.expected:
                self.problem = ("count", self.records)
                log.error("save check: %d records in the output, %d expected", self.records, self.expected)
        except BaseException as exc:  # noqa: BLE001 - handed to the writer thread
            log.exception("save check failed")
            self.exc = exc
        finally:
            self._done.set()


def _reason(code: str, expected: str) -> str:
    text = t(f"json.error.{code}")
    if expected and expected != "none":
        exp = t(f"json.expected.{expected}")
        if exp:
            text = f"{text}; {exp}"
    return text


class _Sink:
    """The temp file behind the encodings: UTF-16 again, the BOM, gzip."""

    def __init__(self, f: Any, target: Path, *, bom: bytes, utf16: str | None, gzip: bool) -> None:
        self.f = f
        self.target = target
        self.bom = bom
        self.encoding = utf16
        self.dec = codecs.getincrementaldecoder("utf-8")("strict") if utf16 else None
        self.z = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, 31) if gzip else None
        self.started = False

    def _begin(self) -> None:
        self.started = True
        if self.bom:
            self._out(self.bom)

    def write(self, chunk: bytes) -> None:
        if not self.started:
            self._begin()
        if self.dec is not None:
            try:
                chunk = self.dec.decode(chunk).encode(self.encoding)
            except UnicodeError as exc:
                raise ExportError("saver.error.invalid_output",
                                  detail=t("saver.detail.encoding", encoding=fileio.encoding_label(self.encoding))
                                  ) from exc
        self._out(chunk)

    def _out(self, data: bytes) -> None:
        z = self.z
        if z is not None:
            data = z.compress(data)
            if not data:
                return
        try:
            self.f.write(data)
        except OSError as exc:
            raise fileio.os_error(exc, self.target, writing=True) from exc

    def close(self) -> None:
        if not self.started:
            self._begin()
        if self.dec is not None:
            try:
                rest = self.dec.decode(b"", final=True)
            except UnicodeError as exc:
                raise ExportError("saver.error.invalid_output",
                                  detail=t("saver.detail.encoding", encoding=fileio.encoding_label(self.encoding))
                                  ) from exc
            if rest:
                self._out(rest.encode(self.encoding))
        if self.z is not None:
            tail = self.z.flush()
            self.z = None
            try:
                self.f.write(tail)
            except OSError as exc:
                raise fileio.os_error(exc, self.target, writing=True) from exc


class _Meter:
    """Progress of the output and the periodic check of the source against changes."""

    def __init__(self, doc: Any, progress: Progress | None, total: int, in_place: bool,
                 cancel: CancelToken | None) -> None:
        self.doc = doc
        self.progress = progress
        self.total = total
        self.in_place = in_place
        self.cancel = cancel
        self.done = 0
        self.next_check = CHANGE_CHECK_BYTES

    def add(self, n: int) -> None:
        self.done += n
        if self.progress is not None:
            self.progress.update(min(self.done, self.total))
        if self.done >= self.next_check:
            self.next_check = self.done + CHANGE_CHECK_BYTES
            doc = self.doc
            if doc.check_changed():
                raise FileChangedError(path=str(doc.source_path))
            if self.in_place and int(getattr(doc, "appended_bytes", 0) or 0) > 0:
                _check_growth(doc)


# ----------------------------------------------------------------------------------
# committing
# ----------------------------------------------------------------------------------
def _free_name(target: Path, suffix: str) -> Path:
    """A name ``.<name[:48]>.<8 hex><suffix>`` next to ``target`` that does not exist now."""
    while True:
        p = target.with_name(f".{target.name[:48]}.{secrets.token_hex(4)}{suffix}")
        if not os.path.lexists(long_path(p)):
            return p


def _exists(p: Path | None) -> bool:
    return p is not None and os.path.lexists(long_path(p))


def _commit_in_place(doc: Any, tmp: Path, new: FileIdentity, source: Path, backup: bool,
                     store: Any) -> tuple[Path | None, list[Path]]:
    """Replace ``source`` by ``tmp`` (module docstring "Committing"). Returns ``(backup path, .old files
    that could not be removed)``."""
    _check_growth(doc)
    doc.ensure_unchanged()
    if fileio.is_read_only(source):
        raise UserInputError("fileio.error.read_only", path=str(source))
    identity = doc.identity
    old_st = _stat_of(source)          # the version just checked: its file id goes into the journal
    if old_st.st_size != identity.size or old_st.st_mtime_ns != identity.mtime_ns:
        raise FileChangedError(path=str(doc.source_path))
    tmp_st = _stat_of(tmp)
    if tmp_st.st_size != new.size or tmp_st.st_mtime_ns != new.mtime_ns:   # pragma: no cover - nobody writes it
        raise FileChangedError(path=str(tmp))
    bak = source.with_name(source.name + BAK_SUFFIX) if backup else None
    had_bak = _exists(bak)
    if had_bak and os.path.isdir(long_path(bak)):
        raise UserInputError("fileio.error.is_directory", path=str(bak))
    bak_ro = had_bak and fileio.is_read_only(bak)
    old_bak = _free_name(source, OLD_SUFFIX) if had_bak else None
    aside = _free_name(source, OLD_SUFFIX)
    journal = {
        "v": JOURNAL_VERSION, "op": "save", "source": str(source), "temp": str(tmp),
        "backup": None if bak is None else str(bak), "had_backup": had_bak, "bak_read_only": bak_ro,
        "old_backup": None if old_bak is None else str(old_bak), "aside": str(aside),
        "old_identity": identity.to_dict(),
        "old_file": {"dev": str(old_st.st_dev), "ino": str(old_st.st_ino)},
        "new": {"size": new.size, "hashes": list(new.hashes), "mtime_ns": new.mtime_ns,
                "dev": str(tmp_st.st_dev), "ino": str(tmp_st.st_ino)},
        "ids": _stable_ids(source, old_st, tmp_st),
        "time": time.time(),
    }
    layer = _layer_info(doc, store)
    if layer is not None:
        journal["layer"] = layer
    if store is not None:
        store.journal(journal)
    moved_bak = linked = False
    dest: Path | None = None
    try:
        if had_bak:
            assert bak is not None and old_bak is not None
            _os_step(lambda: fileio.clear_read_only(bak), bak)
            _os_step(lambda: os.replace(long_path(bak), long_path(old_bak)), bak)
            moved_bak = True
        posix = True
        if bak is not None:
            linked = _os_step(lambda: fileio.hard_link(source, bak), bak)
            posix = linked
        if posix:
            try:
                _os_step(lambda: fileio.replace_posix(tmp, source), source)
            except fileio.ReplaceUnsupported:
                log.info("no POSIX-semantics rename for %s; renaming the original away first", source)
                if linked:
                    _os_step(lambda: os.remove(long_path(bak)), bak)
                    linked = False
                posix = False
        if not posix:
            dest = bak if bak is not None else aside
            _os_step(lambda: os.replace(long_path(source), long_path(dest)), source)
            try:
                fileio.replace_file(tmp, source)
            except BaseException as exc:
                try:
                    os.replace(long_path(dest), long_path(source))
                except OSError as undo_exc:
                    log.error("the original %s could not be renamed back from %s: %s", source, dest, undo_exc)
                    raise _OriginalMoved(path=str(dest), source=str(source)) from exc
                dest = None
                raise
    except _OriginalMoved:
        raise
    except BaseException:
        if _undo_backup(bak, old_bak, linked, moved_bak, bak_ro) and store is not None:
            _clear_journal(store)
        raise
    leftovers: list[Path] = []
    if moved_bak and old_bak is not None and not fileio.remove_quiet(old_bak):
        leftovers.append(old_bak)
    if dest is not None and dest == aside and not fileio.remove_quiet(aside):
        leftovers.append(aside)
    if store is not None:
        _clear_journal(store)
    return bak, leftovers


def _os_step(fn: Callable[[], Any], path: Path) -> Any:
    """``fn()`` with its :class:`OSError` localized as an error about ``path``
    (:class:`fileio.ReplaceUnsupported` passes: the caller falls back)."""
    try:
        return fn()
    except fileio.ReplaceUnsupported:
        raise
    except OSError as exc:
        raise fileio.os_error(exc, path, writing=True) from exc


def _undo_backup(bak: Path | None, old_bak: Path | None, linked: bool, moved_bak: bool,
                 read_only: bool = False) -> bool:
    """Undo step 3 after a failure: remove our hard link, put the previous ``.bak`` back (``read_only``:
    with the read-only bit it had)."""
    ok = True
    if linked and bak is not None and not fileio.remove_quiet(bak):
        ok = False
    if moved_bak and bak is not None and old_bak is not None:
        try:
            os.replace(long_path(old_bak), long_path(bak))
        except OSError:
            log.error("the previous backup %s could not be renamed back from %s", bak, old_bak, exc_info=True)
            ok = False
        else:
            if read_only:
                _set_read_only(bak)
    return ok


def _set_read_only(p: Path) -> None:
    """Give ``p`` the read-only bit again (best effort)."""
    try:
        fileio.set_read_only(p)
    except OSError:
        log.warning("the read-only bit of %s could not be restored", p, exc_info=True)


def _stat_of(p: Path) -> os.stat_result:
    try:
        return os.stat(long_path(p))
    except OSError as exc:
        raise fileio.os_error(exc, p) from exc


def _stable_ids(source: Path, a: os.stat_result, b: os.stat_result) -> bool:
    """The two files have distinct non-zero ids on one volume whose ids survive renames (decision 7)."""
    if not (a.st_ino and b.st_ino) or a.st_ino == b.st_ino or a.st_dev != b.st_dev:
        return False
    return fileio.stable_file_ids(source)


def _layer_info(doc: Any, store: Any) -> dict | None:
    """What :func:`roll_journal_at` needs to open the layer of ``store`` without the document: its key and
    the document's format kind, record path (JSON form) and base record count (``None``: no real store)."""
    key = getattr(store, "key", None)
    if not isinstance(key, str):
        return None
    try:
        info = doc.info
        rp = info.record_path
        return {"key": key, "kind": str(info.kind),
                "record_path": None if rp is None else [s if isinstance(s, int) and not isinstance(s, bool)
                                                         else str(s) for s in rp],
                "base_count": int(doc.base_record_count)}
    except Exception:  # pragma: no cover - a document without these: the journal works without them
        log.debug("no layer information for the save journal", exc_info=True)
        return None


def _clear_journal(store: Any) -> None:
    try:
        store.journal(None)
    except Exception:  # the file is saved; a stale journal is rolled ("completed") at the next open
        log.warning("could not clear the save journal", exc_info=True)


def _commit_save_as(doc: Any, tmp: Path, target: Path) -> int:
    """Replace ``target`` by ``tmp``; returns the bytes the source grew by meanwhile (a warning)."""
    grown = fileio.appended_bytes(doc.source_path, doc.identity)
    if grown is None:
        raise FileChangedError(path=str(doc.source_path))
    doc.ensure_unchanged()
    try:
        fileio.replace_posix(tmp, target)
    except fileio.ReplaceUnsupported:
        log.info("no POSIX-semantics rename for %s; using a plain replace", target)
        fileio.replace_file(tmp, target)
    except OSError as exc:
        raise fileio.os_error(exc, target, writing=True) from exc
    return int(grown)


# ----------------------------------------------------------------------------------
# an interrupted save (decisions 7, 8, 14)
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Version:
    """One version of the file as the journal knows it: size and content hashes, the modification time
    (the new version's only in journals of version 2) and the file id (version 2; strings)."""

    size: int
    hashes: tuple[str, ...]
    mtime_ns: int | None = None
    dev: str | None = None
    ino: str | None = None


def _stat_or_none(p: Path | None) -> os.stat_result | None:
    """``os.stat`` of ``p``; ``None`` when there is no such file. Other OS errors pass (a roll that cannot
    look at a file is a conflict)."""
    if p is None:
        return None
    try:
        return os.stat(long_path(p))
    except (FileNotFoundError, NotADirectoryError):
        return None


class _Doubt(Exception):
    """A roll found something it cannot account for: it stops (a conflict) before it changes anything."""


def _present(p: Path | None) -> bool:
    """Something is at ``p`` (a file, a link, a folder). OS errors other than "not found" pass."""
    if p is None:
        return False
    try:
        os.lstat(long_path(p))
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


class _Journal:
    """A parsed save journal (decision 7) and the moves of :func:`roll_journal` (decision 8). Raises
    ``KeyError`` / ``TypeError`` / ``ValueError`` for a damaged or foreign journal."""

    def __init__(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise TypeError("the journal is not an object")
        self.version = int(data.get("v", 1))
        if self.version not in (1, 2) or data.get("op", "save") != "save":
            raise ValueError(f"unknown journal version {self.version}")
        self.source = Path(str(data["source"]))
        self.temp = Path(str(data["temp"]))
        self.bak = Path(str(data["backup"])) if data.get("backup") else None
        self.old_bak = Path(str(data["old_backup"])) if data.get("old_backup") else None
        self.aside = Path(str(data["aside"])) if data.get("aside") else None
        self.had_bak = bool(data.get("had_backup"))
        self.bak_read_only = data.get("bak_read_only") is True
        old_id = FileIdentity.from_dict(data["old_identity"])
        new = data["new"]
        hashes = tuple(str(h) for h in new["hashes"])
        if len(hashes) != 3:
            raise ValueError("hashes")
        if self.version >= 2:
            old_file = data["old_file"]
            self.old = _Version(old_id.size, old_id.hashes, old_id.mtime_ns, str(old_file["dev"]),
                                str(old_file["ino"]))
            self.new = _Version(int(new["size"]), hashes, int(new["mtime_ns"]), str(new["dev"]), str(new["ino"]))
            self.ids = data.get("ids") is True
        else:   # version 1: no file ids; the old version's time is in its identity, the new one's unknown
            self.old = _Version(old_id.size, old_id.hashes, old_id.mtime_ns)
            self.new = _Version(int(new["size"]), hashes)
            self.ids = False
        self._check_names()

    def _check_names(self) -> None:
        """Every file the journal names lies next to the source and is named like the files of this module:
        a damaged journal never makes a roll touch anything else."""
        src = self.source
        folder = fileio.normalize_path(src.parent)
        stem = re.escape(src.name[:NAME_PREFIX])
        for p, suffix in ((self.temp, TEMP_SUFFIX), (self.old_bak, OLD_SUFFIX), (self.aside, OLD_SUFFIX)):
            if p is None:
                continue
            if fileio.normalize_path(p.parent) != folder or not re.fullmatch(
                    r"\." + stem + r"\.[0-9a-f]{8}" + re.escape(suffix), p.name):
                raise ValueError(f"unexpected file in the journal: {p}")
        if self.bak is not None and fileio.normalize_path(self.bak) != fileio.normalize_path(
                src.with_name(src.name + BAK_SUFFIX)):
            raise ValueError(f"unexpected backup in the journal: {self.bak}")
        if self.had_bak and self.bak is None:
            raise ValueError("a previous backup without a backup name")

    def files(self) -> list[Path]:
        return [p for p in (self.temp, self.bak, self.old_bak, self.aside) if p is not None]

    def is_version(self, v: _Version, p: Path | None, st: os.stat_result | None = None) -> bool:
        """The file at ``p`` is the version ``v``: size, modification time (when known), file id (when ids
        decide) and the content hashes agree."""
        if p is None:
            return False
        if st is None:
            st = _stat_or_none(p)
        if st is None or not stat.S_ISREG(st.st_mode) or st.st_size != v.size:
            return False
        if v.mtime_ns is not None and st.st_mtime_ns != v.mtime_ns:
            return False
        if self.ids and v.ino is not None and (str(st.st_ino) != v.ino or str(st.st_dev) != v.dev):
            return False
        fid = fileio.file_identity(p)
        return fid.size == v.size and fid.hashes == v.hashes

    def roll(self) -> tuple[str, list[Path]]:
        """``(action, files left in place that the roll would have removed)``; nothing is touched for a
        conflict, and a doubt is always a conflict."""
        src = self.source
        st = _stat_or_none(src)
        if st is not None:
            if not stat.S_ISREG(st.st_mode):
                return "conflict", []
            old = self.is_version(self.old, src, st)
            if _present(self.temp):
                # the commit renames the temp file away: it did not happen, so the source can only be the
                # original -- or something else (changed since): never "completed" while the temp is there
                if not old:
                    return "conflict", []
                self._put_backup_back()     # undo step 3 first, then the temp file
                return "back", self._remove([self.temp])
            new = self.is_version(self.new, src, st)
            if new == old:              # both: the evidence cannot tell them apart; neither: changed since
                return "conflict", []
            if new:
                return "completed", self._cleanup()
            self._put_backup_back()         # the original never left the source (the temp file was removed)
            return "back", []
        # the source is missing: the fallback stopped between its two renames (or the file was deleted)
        if self.is_version(self.new, self.temp):
            fileio.replace_file(self.temp, src)
            return "forward", self._cleanup()
        for cand in (self.aside, self.bak):
            if cand is not None and self.is_version(self.old, cand):
                os.replace(long_path(cand), long_path(src))
                if cand == self.bak:
                    self._put_backup_back()
                return "back", self._remove([self.temp])   # not the complete new file (or gone)
        return "conflict", []

    def _put_backup_back(self) -> None:
        """Undo step 3: the previous ``.bak`` back from its ``.old`` copy (with its read-only bit), or our
        ``.bak`` link of the original removed while the original is at the source. A ``.bak`` that is not
        our link of the original is never replaced (:class:`_Doubt`: a conflict)."""
        bak = self.bak
        if bak is None:
            return
        if self.old_bak is not None and _present(self.old_bak):
            if _present(bak) and not self.is_version(self.old, bak):
                raise _Doubt(f"{bak} is not the link of the original")
            os.replace(long_path(self.old_bak), long_path(bak))       # replaces our link, if any
            if self.bak_read_only:
                _set_read_only(bak)
        elif (not self.had_bak and _present(bak) and self.is_version(self.old, bak)
              and self.is_version(self.old, self.source)):
            fileio.remove_quiet(bak)

    def _cleanup(self) -> list[Path]:
        """What the save does after its commit: the previous ``.bak`` copy goes when the new ``.bak`` is the
        original, the original's ``.old`` copy of a save without backup goes. Anything else stays (named)."""
        left: list[Path] = []
        if self.old_bak is not None and _present(self.old_bak):
            if self.bak is not None and self.is_version(self.old, self.bak):
                left += self._remove([self.old_bak])
            else:
                left.append(self.old_bak)
        if self.bak is None and self.aside is not None and _present(self.aside):
            if self.is_version(self.old, self.aside):
                left += self._remove([self.aside])
            else:
                left.append(self.aside)
        return left

    @staticmethod
    def _remove(paths: list[Path]) -> list[Path]:
        """Remove the files; returns those that are still there."""
        return [p for p in paths if _present(p) and not fileio.remove_quiet(p)]


def roll_journal(journal: dict, *, store: Any = None) -> RollResult:
    """Finish or undo the in-place save the journal ``journal`` describes (``PendingInfo.saving``; the
    save stopped between writing the journal and clearing it). The files decide, by evidence a rename
    keeps and a content edit cannot fake (module docstring, decisions 7 and 8): the source is the NEW file
    and the temp file is gone -> ``"completed"`` (the save's cleanup); the source is the OLD file ->
    ``"back"`` (the previous ``.bak`` put back, the temp file removed); the source is missing ->
    ``"forward"`` when the complete temp file can take its place, else ``"back"`` from where the original
    went; anything else, or any doubt -> ``"conflict"`` (nothing touched, the files involved named).
    Returns a :class:`RollResult` (the localized message for the user). With ``store`` the journal is
    cleared unless the result is a conflict."""
    try:
        j = _Journal(journal)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.error("damaged save journal (%s): %r", exc, journal)
        result = RollResult(t("saver.journal.invalid"), "invalid")
        if store is not None:
            _clear_journal(store)
        return result
    try:
        action, left = j.roll()
    except _Doubt as exc:
        log.warning("interrupted save of %s: %s", j.source, exc)
        action, left = "conflict", []
    except (OSError, JsonReshapeError):
        log.exception("rolling the interrupted save of %s failed", j.source)
        action, left = "conflict", []
    if action == "conflict":
        files = ", ".join(str(p) for p in j.files() if _exists(p))
        log.warning("interrupted save of %s: cannot roll forward or back (files: %s)", j.source, files or "-")
        return RollResult(t("saver.journal.conflict", path=str(j.source), files=files or "-"), action)
    log.info("interrupted save of %s rolled: %s%s", j.source, action,
             f"; left in place: {', '.join(map(str, left))}" if left else "")
    if store is not None:
        _clear_journal(store)
    text = t(f"saver.journal.{action}", path=str(j.source))
    if left:
        text = f"{text} {t('saver.journal.left', files=', '.join(map(str, left)))}"
    return RollResult(text, action)


# ----------------------------------------------------------------------------------
# a journal without an opened document (decision 14)
# ----------------------------------------------------------------------------------
class _LayerDoc:
    """A stand-in for a :class:`~jsonreshape.reader.Document` with what :func:`editstore.find_pending` and
    :meth:`editstore.EditStore.open` read of one -- the source path, an identity, the format kind, the
    record path and the base record count -- for a file that cannot be opened (nothing is read from it)."""

    index_complete = True
    closed = False

    def __init__(self, source: StrPath, identity: FileIdentity, kind: str = "", record_path: Any = None,
                 base_count: int = 0) -> None:
        self.source_path = Path(os.path.abspath(os.fspath(source)))
        self.identity = identity
        self.info = SimpleNamespace(kind=kind, record_path=record_path)
        self.base_record_count = self.record_count = int(base_count)
        self.data_size = 0
        self.settings = None

    @classmethod
    def for_path(cls, path: StrPath) -> _LayerDoc:
        """Every layer of ``path`` is "another version" for this stand-in (its identity matches no file)."""
        p = Path(os.path.abspath(os.fspath(path)))
        return cls(p, FileIdentity(path=fileio.normalize_path(p), size=-1, mtime_ns=-1, head_hash=""))

    @classmethod
    def for_layer(cls, info: Any) -> _LayerDoc | None:
        """The document version the pending layer ``info`` was made for, as its journal (version 2,
        ``layer``) records it; ``None`` when the journal does not tell or the key does not come out the same."""
        from .editstore import layer_key

        journal = getattr(info, "saving", None)
        layer = journal.get("layer") if isinstance(journal, dict) else None
        if not isinstance(layer, dict):
            return None
        try:
            rp = layer.get("record_path")
            doc = cls(getattr(info, "source", None) or journal["source"],
                      FileIdentity.from_dict(journal["old_identity"]), str(layer["kind"]),
                      list(rp) if rp is not None else None, int(layer["base_count"]))
            return doc if layer_key(doc) == info.key else None
        except (KeyError, TypeError, ValueError, JsonReshapeError):
            return None


def find_journals(path: StrPath, *, settings: Any = None) -> list:
    """The interrupted save journals of the file ``path`` -- also when it is missing or cannot be opened
    (decision 14): the pending layers of that path (both locations; ``editstore.find_pending`` with a
    stand-in document) whose meta holds a ``saving`` journal, most recent first. Layers held by another
    tab or program are left out (their journal cannot be read now)."""
    from .editstore import find_pending

    return [p for p in find_pending(_LayerDoc.for_path(path), settings=settings) if p.saving and not p.locked]


def _open_journal_layer(info: Any, settings: Any) -> tuple[Any, bool, Any]:
    """``(store, writable, lock to release)`` of the pending layer ``info`` opened without its document."""
    from .editstore import EditStore, LayerLock

    doc = _LayerDoc.for_layer(info)
    if doc is not None:
        lock = LayerLock(info.key)
        if not lock.try_acquire():
            raise EditError("edits.error.locked")
        try:
            store = EditStore.open(doc, settings=settings, dir=Path(info.dir), lock=lock, create=False)
        except EditError as exc:
            lock.release()
            if exc.key != "editstore.error.stale":
                raise
            log.warning("the layer %s does not match its save journal; it is opened read-only", info.dir)
        except BaseException:
            lock.release()
            raise
        else:
            if store is None:
                lock.release()
                raise EditError("saver.error.layer_gone", path=str(info.dir))
            return store, True, lock
    store = EditStore.open(_LayerDoc.for_path(info.source), settings=settings, dir=Path(info.dir),
                           read_only=True, allow_stale=True)
    if store is None:
        raise EditError("saver.error.layer_gone", path=str(info.dir))
    return store, False, None


def _layer_still_matches(journal: dict) -> bool:
    """The file now at the journal's source is still the version the layer was made for -- then the layer
    stays whatever the roll said (it may hold the only copy of the changes; review S1). ``True`` when it
    cannot be told."""
    try:
        old = FileIdentity.from_dict(journal["old_identity"])
        now = fileio.file_identity(journal["source"])
    except (KeyError, TypeError, ValueError, OSError, JsonReshapeError):
        return True
    return now.size == old.size and now.mtime_ns == old.mtime_ns and now.hashes == old.hashes


def roll_journal_at(info: Any, *, settings: Any = None) -> RollResult:
    """Roll the interrupted save of the pending layer ``info`` (from :func:`find_journals`) without an
    opened document, under the layer's lock (decision 14): :func:`roll_journal` on the layer's current
    journal, cleared through the layer's store; after ``"forward"`` / ``"completed"`` the layer is deleted
    (its changes are in the file) unless it still matches the file. A version-1 journal cannot be cleared
    this way (its layer's document is unknown): after ``"back"`` it stays and is rolled again when the file
    is opened. Raises ``EditError("edits.error.locked")`` while another tab or program holds the layer,
    ``EditError("saver.error.layer_gone")`` when it is no longer there."""
    store, writable, lock = _open_journal_layer(info, settings)
    try:
        journal = store.saving
        if not isinstance(journal, dict) or not journal:
            store.close()
            return RollResult(t("saver.journal.none"), "invalid")
        try:
            result = roll_journal(journal, store=store if writable else None)
            drop = result.action in ("forward", "completed") and not _layer_still_matches(journal)
        except BaseException:
            store.close()
            raise
        if drop:
            store.discard()
        else:
            store.close()
        return result
    finally:
        if lock is not None:
            lock.release()


def layer_store_at(info: Any, *, settings: Any = None) -> Any:
    """A read-only :class:`~jsonreshape.editstore.EditStore` of the pending layer ``info`` opened without
    its document (decision 14) -- e.g. to export the changes of a layer whose file cannot be opened with
    ``jsonpatch.export_layer(store, path)``. The caller closes it (``close(close_reader=True)``). Raises
    ``EditError("edits.error.locked")`` / ``EditError("saver.error.layer_gone")``."""
    from .editstore import EditStore

    store = EditStore.open(_LayerDoc.for_path(info.source), settings=settings, dir=Path(info.dir),
                           read_only=True, allow_stale=True)
    if store is None:
        raise EditError("saver.error.layer_gone", path=str(info.dir))
    return store


# ----------------------------------------------------------------------------------
# leftovers of interrupted saves (decision 9)
# ----------------------------------------------------------------------------------
@dataclass
class SweepResult:
    """What :func:`sweep_leftovers` did: the files ``removed``, the bytes ``freed``, and ``skipped`` -- why
    it looked at nothing ("" = it looked)."""

    removed: list[Path] = field(default_factory=list)
    freed: int = 0
    skipped: str = ""

    def message(self) -> str:
        """One localized line for the status bar ("" when nothing was removed)."""
        if not self.removed:
            return ""
        count = len(self.removed)
        key = "saver.sweep.removed"
        if count == 1 and has(key + ".one"):
            key += ".one"
        return t(key, count=count, size=format_bytes(self.freed))


def _journal_files(journal: Any) -> set[str]:
    """Normalized paths of the files a save journal names."""
    out: set[str] = set()
    if isinstance(journal, dict):
        for key in ("temp", "old_backup", "aside", "backup"):
            value = journal.get(key)
            if isinstance(value, str) and value:
                try:
                    out.add(fileio.normalize_path(value))
                except (TypeError, ValueError):
                    pass
    return out


def sweep_leftovers(source: StrPath, *, doc: Any = None, own_key: str | None = None, own_file_lock: bool = False,
                    settings: Any = None, min_age: float = SWEEP_MIN_AGE, now: float | None = None) -> SweepResult:
    """Remove the leftovers of interrupted saves next to ``source`` -- ``.<name>.<8 hex>.jrsave`` temp files
    and ``.old`` copies -- that nothing needs any more (decision 9; review S8): the source exists; no live
    journal of a layer of the file names them (``editstore.find_pending``: both locations and the stale
    layers of the path); no layer of the file and not the physical file is held by another tab or program
    (their journals cannot be read: nothing is removed); the file is not in use (Windows: a sharing
    violation keeps it) and is older than ``min_age`` seconds (a temp file by its modification time, an
    ``.old`` copy by its change time too, since a rename keeps the modification time); an ``.old`` copy
    also needs ``<file>.bak`` at least as new. Names of 48 and more characters share their prefix with
    other files: their ``.old`` copies always stay, the journals of the other files of the prefix count,
    and nothing is removed while one of them looks interrupted (its ``.bak`` without it, an ``.old`` copy).

    ``doc``: the opened document of ``source`` (its layer is found even without ``index.json``);
    ``own_key`` / ``own_file_lock``: the caller's edit session holds that layer key / the physical file
    lock and has no save in flight; ``now``: the clock (tests). Every removal is logged. OS errors while
    looking skip the sweep; ``editstore`` errors pass."""
    from .editstore import LayerLock, file_lock_key, find_pending

    src = Path(os.path.abspath(os.fspath(source)))
    try:
        if not os.path.isfile(long_path(src)):
            return SweepResult(skipped="missing")
        names = os.listdir(long_path(src.parent))
    except OSError:
        return SweepResult(skipped="unreadable")
    name = src.name
    prefix = name[:NAME_PREFIX]
    pattern = re.compile(r"\." + re.escape(prefix) + r"\.[0-9a-f]{8}(?:" + re.escape(TEMP_SUFFIX) + "|"
                         + re.escape(OLD_SUFFIX) + r")\Z")
    cands = sorted(n for n in names if pattern.match(n))
    if not cands:
        return SweepResult()
    shared = len(name) >= NAME_PREFIX
    siblings: list[str] = []
    if shared:
        listed = set(names)
        if any(n.endswith(OLD_SUFFIX) for n in cands):
            return SweepResult(skipped="shared")
        for n in names:
            if n == name or n[:NAME_PREFIX] != prefix:
                continue
            if n.endswith(BAK_SUFFIX) and n[:-len(BAK_SUFFIX)] != name and n[:-len(BAK_SUFFIX)] not in listed:
                return SweepResult(skipped="shared")
            if os.path.isfile(long_path(src.parent / n)):
                siblings.append(n)
    fkey = file_lock_key(src)
    if fkey is not None and not own_file_lock and LayerLock.is_locked(fkey):
        return SweepResult(skipped="locked")
    referenced: set[str] = set()
    for d in [doc if doc is not None else _LayerDoc.for_path(src)] + [_LayerDoc.for_path(src.parent / n)
                                                                      for n in siblings]:
        for info in find_pending(d, settings=settings):
            if info.locked and info.key != own_key:
                return SweepResult(skipped="locked")
            referenced |= _journal_files(info.saving)
    clock = time.time() if now is None else float(now)
    try:
        bak_st: os.stat_result | None = os.stat(long_path(src.with_name(name + BAK_SUFFIX)))
    except OSError:
        bak_st = None
    result = SweepResult()
    for n in cands:
        p = src.parent / n
        if fileio.normalize_path(p) in referenced:
            continue
        try:
            st = os.lstat(long_path(p))
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if n.endswith(OLD_SUFFIX):
            if bak_st is None or not stat.S_ISREG(bak_st.st_mode) or st.st_mtime_ns > bak_st.st_mtime_ns:
                continue                  # possibly the newest copy of the file's content: kept
            changed = fileio.change_time(p)
            if changed is None:
                continue                  # its age cannot be told (e.g. FAT): kept
            age = clock - max(st.st_mtime, changed)
        else:
            age = clock - st.st_mtime
        if age < min_age:
            continue
        try:
            os.remove(long_path(p))
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.info("the leftover %s is kept (in use or protected: %s)", p, exc)
            continue
        log.info("removed %s, a leftover of an interrupted save (%s)", p, format_bytes(st.st_size))
        result.removed.append(p)
        result.freed += st.st_size
    return result
