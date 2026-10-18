"""Document: format detection, record index, record access, node tree (docs/ARCHITECTURE.md §5).

Opening (:meth:`Document.open`) is cheap: the file is prepared (gzip / UTF-16 inputs are
turned into a UTF-8 *data file* by :func:`jsonreshape.fileio.prepare_data_file`), the
format is detected from the first ``Settings.head_bytes`` of the data, and a complete
cached index is reused when one exists. Everything heavy happens in
:meth:`Document.build_index` (usually in the background via :meth:`Document.start_index`):
the data is streamed through the core ``Scanner`` and the record spans go to an on-disk
index (:mod:`jsonreshape.index`) that is readable while it grows, so the first records
are usable at once (AGENT_PROMPT §3 "Indeks").

All offsets are byte offsets in the data file (``doc.data_path``; for gzip/UTF-16
sources that is the prepared UTF-8 copy) -- except the offset a JSON error *reports* (see
"Errors" below). Record numbers are 0-based.

Format detection (spec §5.1)
============================
``head`` = the first ``head_bytes`` of the data after the BOM (leading whitespace
skipped); ``complete`` = the head reaches the end of the data. An empty or
whitespace-only file raises :class:`~jsonreshape.errors.FileFormatError`
(``reader.empty_file``). Then, in this order:

1. **Forced options win.** ``record_path`` ``""``/``"$"``/``()`` = the root container:
   an array root gives ``array``, an object root ``keyed``. Any other record path gives
   ``wrapped`` (the scanner's ``target_path``). A forced ``format`` is used as given
   (``array`` on an object root / ``keyed`` on an array root is a ``FileFormatError``);
   ``wrapped`` without a record path takes the best candidate (see 5), an error when
   there is none. Record paths follow the rule of :mod:`jsonreshape.paths` for every
   other path: a ``str`` segment that is a canonical non-negative decimal (``/data/0``,
   ``data.0``) addresses an array element, so it becomes an index -- unless the head
   shows an object at that place (then it stays the member key ``"0"``).
2. **Extension.** ``.jsonl``, ``.ndjson``, ``.jsonlines`` (also before ``.gz``) give
   ``jsonl``.
3. **Several values.** A ``MODE_STREAM`` scanner runs over the head. At least two values
   where the first value contains no ``\\n`` and a ``\\n`` separates the first two values
   give ``jsonl``; at least two values otherwise give ``stream``. One complete value
   followed by the start of another one when the head ends counts as two values (records
   bigger than half the head). When the stream scan stops at a syntax error before the
   second value (e.g. a broken first line), the complete lines of the head are scanned
   with ``MODE_LINES``: at least two valid lines and no more broken than valid lines give
   ``jsonl`` (the broken lines are then reported by the index).

   A first value that does not end inside the head (a JSON Lines file whose first record
   is bigger than the head, e.g. a MongoDB export saved as ``.json``) is scanned further,
   up to :data:`EXTEND_SCAN_BYTES` (256 MiB), but only when the end of the file looks like
   several values: its last line (within the last :data:`TAIL_PROBE_BYTES` .. ``head_bytes``)
   is a complete JSON value by itself. A big single document never has such a last line
   (``]``, ``}``, or no line break at all), so opening it costs one small read at the end
   instead of a scan. When the first value ends and another value follows, the rule above
   decides between ``jsonl`` and ``stream``.
4. **Root character.** ``[`` gives ``array``. ``{`` whose first member is a *value*
   instead of ``"key":`` -- the next token after ``{`` is a value that is not a string,
   or a string followed by ``,`` or ``}`` -- gives ``brace_list`` with the warning
   ``reader.warn_brace_list``. Any root other than ``{`` gives ``single``.
5. **Object root.** ``ijson.basic_parse`` runs over the head (a truncated head simply
   ends the analysis). Candidates are arrays at depth 1..3 reachable through object
   keys only (``("data",)``, ``("result", "items")``; the first occurrence of a path
   wins, like the scanner's target), each with ``closed_in_head``, ``items_in_head``,
   ``objects_in_head`` and ``values_in_head`` (all values inside it, any depth). Since
   such arrays cannot nest, at most one candidate is open at the end of the head. With
   *members* = root members whose value started in the head and the *keyed test* = at
   least 2 members and at least 80 % of their values are objects:

   a. an array still open at the end of the head gives ``wrapped`` with that array --
      unless the keyed test passes and the open array has fewer items than there are
      members (a keyed document cut inside one record's inner array);
   b. complete head: the candidate with the most items (tie: shallowest, then first)
      gives ``wrapped`` when it has at least 2 items, more than half of them objects,
      and holds more than half of all values of the document (it *dominates* the
      document; e.g. ``{"meta": {..}, "a": {"b": [records]}, "tail": {..}}``); else the
      keyed test gives ``keyed``; else a candidate at depth <= 2 with at least 2 items
      gives ``wrapped`` (most items; tie: shallowest); else ``single``;
   c. incomplete head without a chosen open array: the keyed test on the members seen
      gives ``keyed``; else ``single``.

   Every automatic ``wrapped`` choice adds the warning ``reader.record_path_auto``
   naming the path. ``FormatInfo.candidates`` lists the candidates, open first, then by
   items (descending), for the GUI/CLI record-path chooser.

Index modes: ``array``/``keyed`` -> ``MODE_CONTAINER`` with ``[]``; ``wrapped`` ->
``MODE_CONTAINER`` with the record path; ``jsonl`` -> ``MODE_LINES``; ``stream`` ->
``MODE_STREAM``; ``brace_list`` -> ``MODE_BRACE_LIST``; ``single`` -> ``MODE_SINGLE``.
The data is read and fed in 1 MiB pieces (the first one 256 KiB, so the first records
appear within milliseconds): as fast as 8 MiB reads with the Rust core (measured), but a
span batch stays below 8 MiB even for tiny records and cancellation/progress stay
responsive with the pure-Python core.

Several processes indexing the same file (a batch script exporting one file to several
formats, the GUI and the CLI) take turns: :class:`~jsonreshape.index.BuildLock` lets one
of them build, the others wait (progress message ``reader.progress.wait_index``) and then
use the index it installed; an index that cannot be replaced because another process has
it mapped is reused when it is equivalent (``IndexWriter.finalize(accept_existing=...)``).

Reading records
===============
Each thread reads through its own *unbuffered* shared-read handle (``open_shared``,
AGENT_PROMPT §1.7): a read asks the OS for exactly the requested bytes. A
``BufferedReader`` turns every seek + small read into a read of a whole buffer -- 128 KiB
since Python 3.14 -- which made a random record cost ~30 us instead of ~3.5 us (measured
on syn_2g.json, warm cache). A small read that continues a forward walk fetches a
:data:`READ_AHEAD` window and the next ones are served from it (:meth:`Document._read`).

:meth:`Document.iter_blocks` reads ascending runs with one read per block (8 MiB),
including the gaps between the records while they are small: at most
:data:`CONTIGUOUS_GAP` (reading 8 KiB costs less than fetching the next record on its own,
measured), or at most :data:`SCATTER_GAP` while the records cover at least half of the
range (:data:`DENSE_MIN`). Once the source comes back over bytes it already passed -- the
runs of a sort order, e.g. the records of each value of a low-cardinality key -- only the
density rule applies: reading every run as one range multiplied the bytes read by the
number of runs (33.5 GB read to export a sort of the 2.1 GB syn_2g.json by city). A
*scattered* block (records in another order, or ascending but too sparse for one read) is
fetched in file order: on Windows a block of at least :data:`MAP_MIN_RECORDS` records
slices transient read-only mappings of the data file; smaller blocks, and POSIX systems
(where a file truncated under a live mapping kills the process with SIGBUS), use exact
reads that merge neighbours at most :data:`SCATTER_GAP` apart while the merged range stays
dense (gaps up to :data:`SCATTER_SMALL_GAP` are always merged). Measured over random
records of syn_2g.json: ~3.8 us per record mapped, ~6 us read, ~13 us with the old
buffered reads; and while another Python thread is busy, ~7 us mapped but ~1 ms read
(~2 ms the old way) -- every read releases the GIL and then waits up to the 5 ms switch
interval to get it back (the convoy effect); slicing a mapping keeps it. A live mapping
makes Windows refuse to truncate or rewrite the file in place, so no mapping outlives the
block it serves: each one is closed after :data:`MAP_BATCH_BYTES` of records (which also
bounds the working set its touched pages add) and before the block is yielded -- an
idle, paused or finished iteration never keeps the source file mapped.

Measured on syn_2g.json (2.13 GB, 6.5 M records, warm cache), reading all records in the
order of a sort by ``contact.address.city`` (sparse ascending runs): the old planner read
31.1 GB in 16.1 s; now 0 bytes of ``ReadFile`` in 13.5 s with the mappings (page faults
are not file reads) and 2.7 GB in 21 s on the read path (POSIX, or no mapping possible;
8.1 GB / 18 s when 4 KiB gaps were always merged, 24 GB through the read-ahead window
before :meth:`_read_pieces` asked for exact reads). End to end, ``jsonreshape sort
syn_2g.json --by contact.address.city`` read 2.4 GB instead of 33.5 GB. Single ascending
passes are as fast as before (every 2nd .. 16th record: 1.9 .. 0.9 s, one contiguous read
of the file) or faster (every 50th: 0.37 s instead of 0.83 s, scattered). On a file that
is not in the page cache every pass still touches the pages of its records: a sort by a
low-cardinality key reads the file about once per distinct value at page granularity --
inherent to the order, not to the planner.

Progress of ``iter_blocks``: for ``source=None`` the bytes of the data read (total: the
data size); for any other source the bytes of the *selected records* (``end - start``
summed), the unit callers compute their totals in -- never more than the selection.
:attr:`Block.selected_bytes` gives the same number per block.

Records of ``keyed`` documents (and of ``wrapped`` documents whose record container is an
object) are object *members*: the span covers ``"key": value``; :meth:`Document.record`
returns the value and :meth:`Document.record_key` the decoded key.

Changes while reading (AGENT_PROMPT §1.7)
==========================================
The file version is fixed when the document is opened (:attr:`Document.identity`: size,
mtime, hashes of the first MiB, the last MiB and 16 sampled pages in between -- see
:class:`~jsonreshape.fileio.FileIdentity`). Spans of an old version applied to new bytes
would silently produce broken output, so:

* every handle opened on the data file -- each thread's handle, the build's handle,
  :meth:`Document.open_data` -- is checked with ``fstat`` against the identity (size and
  mtime; the size of a prepared copy), and the handle is checked again after every block
  of :meth:`Document.iter_blocks`, every :data:`VERIFY_BUILD_CHUNKS` MiB of a build and
  every :data:`VERIFY_READS` reads of :meth:`Document._read` (``fstat`` ~4 us);
* every record of a scattered block, a sample of at most :data:`SANITY_SAMPLES` records of
  a contiguous block, a huge record and :meth:`Document.record_bytes` are checked to start
  with a byte that can start a value (a ``"`` for member records) and to end with a byte
  that can end one (``}`` ``]`` ``"`` a digit, ``e`` or ``l``): ~60 ns per record, which
  is why contiguous blocks are sampled;
* a build checks the whole identity (all hashes) before it installs the index;
* :meth:`Document.ensure_unchanged` compares the identity of the file now at the path --
  operations call it before they commit an output or report results.

Any mismatch raises :class:`~jsonreshape.errors.FileChangedError` ("run the command
again"): nothing is written or installed from mixed data. An edit that keeps the size,
restores the mtime and touches none of the hashed ranges (the first MiB, the last MiB,
the 16 sampled pages: ~2 MiB of the file) can still go unnoticed -- also by the cache key,
so a stale index is used -- where no span check happens to look: in the middle of a big
file most such edits are not detected. Only re-reading the whole file would tell.

**A file that only grows** (an active JSON Lines log another program appends to) is not a
change: the document keeps working on the *snapshot* it opened -- the ``data_size``
bytes of the identified version; reads never go beyond it and the index covers the
records up to it. "Only grows" = the file is longer than at open and the head, tail and
sample hashes of the *original* range ``[0, size at open)`` are unchanged
(:func:`~jsonreshape.fileio.grown_from`; the modification time is not compared, the span
checks above still apply to every record read). Then the handle checks and
:meth:`Document.check_changed` / :meth:`Document.ensure_unchanged` accept it,
:attr:`Document.appended_bytes` holds the number of bytes appended after the file was
opened (the largest growth seen so far; 0 when none) and the warning
``reader.warn.appended`` (its size kept up to date) is added once to ``info.warnings`` --
callers show ``info.warning_texts()`` at the end of an operation. While the file keeps
growing, the handle checks hash the original range again at most every
:data:`GROWTH_RECHECK_SECONDS` (``ensure_unchanged`` always does). Truncation, a rewrite
(also a longer one whose hashed original ranges differ), a same-size edit and a
replacement by another file still raise :class:`~jsonreshape.errors.FileChangedError`; an
edit of the original range outside the hashed ranges while the file grows goes as
unnoticed as the same-size edit above. gzip / UTF-16
sources: the prepared copy holds the identified version only (bytes appended while it is
made are not copied), growth of the source is judged the same way. A line that was being
written at the moment the file was opened is cut by the snapshot: in a JSON Lines file it
is reported as a broken last line (loggers that write whole lines never cause this).

Errors
======
JSON errors do not raise: :attr:`Document.error` holds a
:class:`~jsonreshape.errors.JsonSyntaxError` (1-based line, 1-based column in
characters, up to 60 bytes of context on each side) and the records before the error
stay usable (``doc.partial``) -- except a number record that ends exactly where a
truncated document ends (``[1, 2, 12`` may be the cut ``[1, 2, 123]``; the core completes
numbers at EOF), which is dropped. JSON Lines broken lines are skipped and listed in
:attr:`Document.broken_lines` (first 10000) / :attr:`Document.broken_count`.

The error's ``offset`` is a byte offset in the *user's* file: for a UTF-16 source the
data-file offset is mapped back (BOM + 2 bytes per UTF-16 code unit of the text before
the error); for a gzip source it is the offset in the decompressed data, and the message
says so (``reader.error.json_syntax_decompressed``). :attr:`Document.error_data_offset`
keeps the offset in the data file.

When an automatically detected document stops with ``trailing_data`` right after its
root value and another value follows, :attr:`Document.format_hint` is ``"jsonl"`` (the
root value is one line and a line break follows) or ``"stream"``: the file is most likely
JSON Lines / concatenated JSON whose first value was too big to detect (callers suggest
``--format jsonl``/``stream``). A record path that is not found (``target_not_found``) or
not a container (``target_not_container``) is reported through ``doc.error`` with those
codes as well (the caller decides how to present it).

Node tree
=========
:meth:`Document.root` / :meth:`Document.node` give lazy, offset-based :class:`Node`
objects. A container's children are found by streaming its bytes through a
``MODE_CONTAINER`` scanner (never loading it whole); containers larger than
``Settings.large_node_bytes`` or with more than ``Settings.large_node_children``
children keep their child spans in an on-disk index (cached, key extra
``"node:<start>"``), smaller ones in an ``array('Q')`` (a short list found by scanning at
least 4 MiB is also written to the cache, so reopening never rescans a big node). The
records of
array/keyed/jsonl/stream/brace_list documents are the children of the root (served
from the main index, never rescanned); for wrapped documents the ancestors of the
record container are expanded by scanning their bytes with the record container
replaced by the placeholder ``[]`` (so a multi-GB record array is never rescanned).

Known limitations (Stage 1; the GUI stage revisits both)
=========================================================
* gzip and UTF-16 inputs are prepared (decompressed / transcoded) completely in
  :meth:`Document.open` before the first record is available (~0.5 s per 75 MB of gzip);
  plain UTF-8 inputs show their first records while the index is still being built.
* While a document holds handles on the source, other programs can read, append,
  overwrite, rename and delete it, but cannot *replace* it with ``MoveFileEx`` /
  ``os.replace`` (the save method of some editors fails with "Access denied") until the
  document is closed; the change detection above covers every way that works.
"""

from __future__ import annotations

import bisect
import codecs
import io
import json
import logging
import mmap
import operator
import os
import re
import sys
import threading
import time
from array import array
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Union

import ijson

from . import codec, core
from .core import FLAG_ESCAPES, MODE_BRACE_LIST, MODE_CONTAINER, MODE_LINES, MODE_SINGLE, MODE_STREAM
from .errors import (
    FileChangedError,
    FileFormatError,
    JsonReshapeError,
    JsonSyntaxError,
    TooLargeError,
    UserInputError,
)
from .fileio import (
    FileIdentity,
    PreparedFile,
    grown_from,
    long_path,
    open_shared,
    prepare_data_file,
    read_range,
    release_prepared,
    retain_prepared,
)
from .fileio import appended_bytes as _appended_to
from .i18n import t
from .index import (
    FLAG_BROKEN_LINES,
    FLAG_ERROR,
    FLAG_MEMBERS,
    FLAG_NODE,
    BuildLock,
    IndexReader,
    IndexWriter,
    cache_key,
    evict,
    find_index,
    index_path,
    lock_path,
)
from .paths import canonical_index, format_path, parse_path
from .settings import Settings, get_settings
from .tasks import BackgroundTask, CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]
Path_ = tuple[Union[str, int], ...]

__all__ = [
    "BLOCK_BYTES",
    "KINDS",
    "Block",
    "BrokenLine",
    "Document",
    "FormatInfo",
    "Node",
    "RecordPathCandidate",
    "detect_format",
    "set_scanner_backend",
    "split_member",
]

KiB = 1 << 10
MiB = 1 << 20

KINDS = ("array", "jsonl", "brace_list", "wrapped", "keyed", "single", "stream")
JSONL_EXTENSIONS = (".jsonl", ".ndjson", ".jsonlines")
BLOCK_BYTES = 8 * MiB             # default iter_blocks() block size
READ_CHUNK = 8 * MiB              # default read size of sequential range reads
FIRST_CHUNKS = (256 * KiB,)        # ramp-up: the first records are visible at once
FEED_BYTES = 1 * MiB              # build/node scans read and feed the scanner this much at a time:
                                  # bounds a span batch (<= 8 MiB), keeps cancel/progress responsive
                                  # (pure-Python backend) and is as fast as 8 MiB reads (measured)
NODE_CACHE_MIN_BYTES = 4 * MiB    # child lists of node scans at least this big are also cached on disk
SPAN_BATCH = 65536                # spans fetched per batch by iter_blocks()
BROKEN_KEEP = 10000               # broken lines kept in memory
BROKEN_META_CAP = 100000          # broken lines stored in the index metadata
CONTEXT_BYTES = 60                # error context on each side of the offset
PREVIEW_CHARS = 120               # broken-line preview length
MAX_CANDIDATE_DEPTH = 3
SCATTER_GAP = 32 * KiB            # scattered blocks: coalesce reads of records this close
SCATTER_GROUP = 4 * MiB           # ... into ranges of at most this many bytes
MAP_SCATTERED = sys.platform == "win32"  # scattered blocks slice transient mappings (module docstring)
MAP_MIN_RECORDS = 16              # ... from this many records (map + close ~15-25 us: what ~16 records save)
MAP_MAX_RECORD = 64 * KiB         # ... records larger than this are read (no long faulting copy under the GIL)
MAP_BATCH_BYTES = 16 * MiB        # ... a mapping is closed after this many bytes (+ 1 page per record)
READ_AHEAD = 32 * KiB             # Document._read: a small read continuing a forward walk fetches this much
READ_AHEAD_MAX = 8 * KiB          # ... "small": at most this many bytes
READ_AHEAD_GAP = 4 * KiB          # ... "continuing": starting at most this far after the previous read
CONTIGUOUS_GAP = 8 * KiB          # a contiguous block reads a gap this small (single pass; module docstring)
DENSE_MIN = 0.5                   # a contiguous read / merged read: selected bytes >= this share of the range
SCATTER_SMALL_GAP = 1 * KiB       # ... gaps this small are always merged into one read (measured below)
SANITY_SAMPLES = 256              # contiguous blocks: records whose first/last byte is checked
VERIFY_READS = 1024               # Document._read: the handle is checked against the identity this often
VERIFY_BUILD_CHUNKS = 16          # a build checks its handle every this many FEED_BYTES pieces
EXTEND_SCAN_BYTES = 256 * MiB     # detection: a first value bigger than the head is scanned this far
TAIL_PROBE_BYTES = 64 * KiB       # detection: first look at the end of the file (then up to head_bytes)
KEY_PROBE = 4 * KiB               # bytes read to decode a member key
MEM_CHILDREN_CACHE_BYTES = 32 * MiB
MEM_CHILDREN_CACHE_ENTRIES = 256
WAIT_POLL_SECONDS = 0.1           # waiting for another process's build of the same index
CHANGE_RETRY_SECONDS = 1.0        # check_changed(): a source locked by another program is retried this long
GROWTH_RECHECK_SECONDS = 1.0      # a growing source: handle checks hash its original range at most this often
APPENDED_WARNING = "reader.warn.appended"   # info.warnings key: the source grew after it was opened
_WS = b" \t\r\n"
_CONT_BYTES = bytes(range(0x80, 0xC0))   # UTF-8 continuation bytes
_NOT_ASTRAL_LEADS = bytes(b for b in range(256) if not 0xF0 <= b <= 0xF4)   # deleted: 4-byte UTF-8 leads stay
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
BRACE_LIST_EXAMPLE = "{ {...}, {...} }"

_STRING_TOKEN = rb'"[^"\\]*(?:\\.[^"\\]*)*"'
_STRING_RE = re.compile(_STRING_TOKEN, re.S)
_MEMBER_RE = re.compile(rb"[ \t\r\n]*(" + _STRING_TOKEN + rb")[ \t\r\n]*:[ \t\r\n]*", re.S)
_WS_RUN_RE = re.compile(r"\s+")

_KIND_BY_BYTE = {
    ord("{"): "object",
    ord("["): "array",
    ord('"'): "string",
    ord("t"): "bool",
    ord("f"): "bool",
    ord("n"): "null",
}
_VALUE_START = frozenset(b'{["-0123456789tfn')
_VALUE_END = frozenset(b'}]"0123456789el')       # last byte of a value: true/false end in e, null in l
_VALUE_START_BYTES = bytes(sorted(_VALUE_START))
_VALUE_END_BYTES = bytes(sorted(_VALUE_END))
_QUOTE = ord('"')


# ----------------------------------------------------------------------------------
# scanner backend (tests and benchmarks compare backends)
# ----------------------------------------------------------------------------------
_scanner_backend: str | None = None


def set_scanner_backend(name: str | None) -> str | None:
    """Select the core backend (``"rust"`` / ``"python"``) used by Documents opened from
    now on; ``None`` = the active ``core.BACKEND``. Returns the previous choice.
    ``ImportError`` when the backend is unavailable."""
    global _scanner_backend
    if name is not None:
        core.get_backend(name)
    old = _scanner_backend
    _scanner_backend = name
    return old


def _scanner_factory() -> tuple[str, Callable[..., Any]]:
    name = _scanner_backend
    if name is None:
        return core.BACKEND, core.Scanner
    return name, core.get_backend(name).Scanner


# ----------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------
def _pairs(data: bytes) -> array:
    a = array("Q")
    a.frombytes(data)
    return a


def _decode_key_token(tok: bytes) -> str:
    """Decode a JSON string token (quotes included) like Python's ``json``."""
    if b"\\" not in tok:
        try:
            return tok[1:-1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid UTF-8 in key: {exc}") from None
    try:
        value = json.loads(tok.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid key token: {exc}") from None
    if not isinstance(value, str):  # pragma: no cover - the regex only matches strings
        raise ValueError("key is not a string")
    return value


def split_member(buf: bytes | bytearray | memoryview) -> tuple[str, int]:
    """Split an object member ``"key": value`` (leading whitespace allowed).

    Returns ``(decoded key, offset of the value in buf)``; ``ValueError`` when ``buf``
    does not start with a member (or the key is cut off).
    """
    m = _MEMBER_RE.match(buf)
    if m is None:
        raise ValueError("not an object member")
    return _decode_key_token(bytes(m.group(1))), m.end()


def _kind_of(byte: int) -> str:
    return _KIND_BY_BYTE.get(byte, "number")


def _skip_ws(buf: bytes, i: int) -> int:
    n = len(buf)
    while i < n and buf[i] in _WS:
        i += 1
    return i


def _looks_like_record(buf: bytes | memoryview, member: bool) -> bool:
    """The bytes of a record span start with a byte that can start a value (a ``"`` for
    a member record) and end with a byte that can end one (``}`` ``]`` ``"`` a digit,
    ``e``, ``l``): what stale spans applied to a changed file usually violate."""
    if not buf:
        return False
    b0 = buf[0]
    return buf[-1] in _VALUE_END and (b0 == _QUOTE if member else b0 in _VALUE_START)


def _char_count(raw: bytes) -> int:
    """Characters in UTF-8 ``raw`` (non-continuation bytes; equals
    ``len(raw.decode("utf-8", "replace"))`` for the valid UTF-8 the scanner accepted)."""
    return len(raw.translate(None, _CONT_BYTES))


def _decode_prefix(raw: bytes, final: bool) -> str:
    """Decode ``raw`` with ``errors="replace"``; when not ``final`` a trailing incomplete
    UTF-8 sequence is dropped instead of replaced."""
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    return dec.decode(raw, final)


def _decode_suffix(raw: bytes) -> str:
    """Decode ``raw`` that may start inside a UTF-8 sequence (leading continuation bytes
    are dropped)."""
    i = 0
    while i < len(raw) and i < 3 and 0x80 <= raw[i] < 0xC0:
        i += 1
    return raw[i:].decode("utf-8", "replace")


def _norm_record_path(record_path: Any) -> Path_ | None:
    if record_path is None:
        return None
    if isinstance(record_path, str):
        return tuple(parse_path(record_path))
    if isinstance(record_path, (list, tuple)):
        out: list[str | int] = []
        for seg in record_path:
            if isinstance(seg, bool) or not isinstance(seg, (str, int)):
                raise UserInputError("reader.bad_record_path", path=repr(record_path))
            if isinstance(seg, int) and seg < 0:
                raise UserInputError("reader.bad_record_path", path=repr(record_path))
            out.append(seg)
        return tuple(out)
    raise UserInputError("reader.bad_record_path", path=repr(record_path))


def _path_text(path: Path_) -> str:
    return format_path(path) if path else "$"


# ----------------------------------------------------------------------------------
# public dataclasses
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class RecordPathCandidate:
    """An array found in the head that could hold the records (wrapped documents).

    ``path``: keys from the root (depth 1..3); ``closed_in_head``: the array ended inside
    the head; ``items_in_head``: direct elements seen; ``objects_in_head`` /
    ``arrays_in_head``: how many of them are objects / arrays; ``values_in_head``: all
    JSON values inside it (any depth).
    """

    path: tuple[str | int, ...]
    closed_in_head: bool
    items_in_head: int
    objects_in_head: int
    values_in_head: int = 0
    arrays_in_head: int = 0

    @property
    def depth(self) -> int:
        return len(self.path)

    @property
    def items_kind(self) -> str:
        """``empty``, ``objects`` (all items objects), ``arrays`` (all items arrays: rows
        of a matrix), ``mixed`` (objects or arrays among other items) or ``values`` (only
        scalars)."""
        if not self.items_in_head:
            return "empty"
        if self.objects_in_head == self.items_in_head:
            return "objects"
        if self.arrays_in_head == self.items_in_head:
            return "arrays"
        return "mixed" if self.objects_in_head or self.arrays_in_head else "values"

    @property
    def text(self) -> str:
        """Dotted path text (``data``, ``result.items``)."""
        return _path_text(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": list(self.path),
            "closed_in_head": self.closed_in_head,
            "items_in_head": self.items_in_head,
            "objects_in_head": self.objects_in_head,
            "values_in_head": self.values_in_head,
            "arrays_in_head": self.arrays_in_head,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordPathCandidate:
        return cls(
            path=tuple(data["path"]),
            closed_in_head=bool(data["closed_in_head"]),
            items_in_head=int(data["items_in_head"]),
            objects_in_head=int(data["objects_in_head"]),
            values_in_head=int(data.get("values_in_head", 0)),
            arrays_in_head=int(data.get("arrays_in_head", 0)),
        )


@dataclass
class FormatInfo:
    """Detected (or forced) format of a document (spec §5.1).

    * ``kind``: one of :data:`KINDS`; ``encoding`` / ``compression`` / ``warnings`` partly
      come from :class:`~jsonreshape.fileio.PreparedFile`;
    * ``record_path``: wrapped documents: path of the record container (else ``None``);
    * ``candidates``: record-path candidates (object roots), open first, then by items;
    * ``auto``: the kind/record path were detected (``False`` when forced);
    * ``warnings``: i18n keys, with ``warning_params`` (same order) for their placeholders;
    * ``data_start``: first byte after a BOM; ``root_start``: first non-whitespace byte;
      ``root_char``: that byte as text; ``head_complete``: the head held all data;
    * ``container_kind``: wrapped: ``"array"``/``"object"`` when the record container was
      seen in the head (else ``None``).
    """

    kind: str
    encoding: str = "utf-8"
    compression: str | None = None
    record_path: tuple[str | int, ...] | None = None
    candidates: list[RecordPathCandidate] = field(default_factory=list)
    auto: bool = True
    warnings: list[str] = field(default_factory=list)
    data_start: int = 0
    warning_params: list[dict[str, Any]] = field(default_factory=list)
    root_start: int = 0
    root_char: str = ""
    head_complete: bool = False
    container_kind: str | None = None
    # a forced record path with canonical-index segments whose container was not seen in
    # the head: the same path with those segments as member keys (tried when the first
    # one is not found; not serialized)
    record_path_alt: tuple[str | int, ...] | None = field(default=None, repr=False, compare=False)

    def add_warning(self, key: str, **params: Any) -> None:
        self.warnings.append(key)
        self.warning_params.append(params)

    def warning_texts(self) -> list[str]:
        """The warnings in the current UI language."""
        out = []
        for i, key in enumerate(self.warnings):
            params = self.warning_params[i] if i < len(self.warning_params) else {}
            out.append(t(key, **params))
        return out

    @property
    def label(self) -> str:
        """Localized format name for the status bar."""
        return t(f"reader.kind.{self.kind}")

    @property
    def record_path_text(self) -> str | None:
        return None if self.record_path is None else _path_text(self.record_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "encoding": self.encoding,
            "compression": self.compression,
            "record_path": None if self.record_path is None else list(self.record_path),
            "candidates": [c.to_dict() for c in self.candidates],
            "auto": self.auto,
            "warnings": list(self.warnings),
            "warning_params": [dict(p) for p in self.warning_params],
            "data_start": self.data_start,
            "root_start": self.root_start,
            "root_char": self.root_char,
            "head_complete": self.head_complete,
            "container_kind": self.container_kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FormatInfo:
        rp = data.get("record_path")
        return cls(
            kind=str(data["kind"]),
            encoding=str(data.get("encoding", "utf-8")),
            compression=data.get("compression"),
            record_path=None if rp is None else tuple(rp),
            candidates=[RecordPathCandidate.from_dict(c) for c in data.get("candidates", ())],
            auto=bool(data.get("auto", True)),
            warnings=[str(w) for w in data.get("warnings", ())],
            data_start=int(data.get("data_start", 0)),
            warning_params=[dict(p) for p in data.get("warning_params", ())],
            root_start=int(data.get("root_start", data.get("data_start", 0))),
            root_char=str(data.get("root_char", "")),
            head_complete=bool(data.get("head_complete", False)),
            container_kind=data.get("container_kind"),
        )


@dataclass(frozen=True)
class BrokenLine:
    """A JSON Lines line that could not be parsed (skipped by the index).

    ``line`` is 1-based; ``start``/``end`` delimit the line (``end`` excludes ``\\n`` but
    includes a trailing ``\\r``); ``offset`` is the first bad byte; ``column`` the 1-based
    character column of the error; ``preview`` at most 120 characters of the line.
    """

    line: int
    start: int
    end: int
    offset: int
    code: str
    expected: str
    column: int = 0
    preview: str = ""

    @property
    def reason(self) -> str:
        text = t(f"json.error.{self.code}")
        if self.expected and self.expected != "none":
            exp = t(f"json.expected.{self.expected}")
            if exp:
                text = f"{text}; {exp}"
        return text

    @property
    def message(self) -> str:
        return t("reader.broken_line", line=self.line, column=self.column, reason=self.reason)

    def to_list(self) -> list[Any]:
        return [self.line, self.start, self.end, self.offset, self.code, self.expected, self.column, self.preview]

    @classmethod
    def from_list(cls, data: list[Any]) -> BrokenLine:
        line, start, end, offset, code, expected = data[:6]
        column = int(data[6]) if len(data) > 6 else 0
        preview = str(data[7]) if len(data) > 7 else ""
        return cls(int(line), int(start), int(end), int(offset), str(code), str(expected), column, preview)


@dataclass
class Block:
    """A batch of records read together (see :meth:`Document.iter_blocks`).

    * ``data``: the bytes holding the records (``b""`` for a huge block);
    * ``indices``: record indices in iteration order (``array('Q')``);
    * ``spans``: u64 LE ``(start, end)`` pairs relative to ``data`` (same order, sorted
      and non-overlapping); for a huge block the record's *absolute* span;
    * ``contiguous``: ``data`` is one range of the file starting at ``base`` (records in
      ascending file order, gaps included); otherwise the records were fetched one by
      one and joined with ``b"\\n"`` in iteration order (``base`` = -1);
    * ``huge``: a single record larger than ``Settings.large_node_bytes`` that was not
      read (use :mod:`jsonreshape.stream`).
    """

    data: bytes
    indices: array
    spans: bytes
    contiguous: bool
    huge: bool
    base: int = -1

    def __len__(self) -> int:
        return len(self.indices)

    @property
    def selected_bytes(self) -> int:
        """Bytes of the block's records (``end - start`` summed; a huge block: the size of
        its record) -- the unit of ``iter_blocks`` progress for selections."""
        a = _pairs(self.spans)
        return sum(a[1::2]) - sum(a[0::2])

    def span(self, k: int) -> tuple[int, int]:
        a = _pairs(self.spans[16 * k:16 * k + 16])
        return a[0], a[1]

    def record_bytes(self, k: int) -> bytes:
        if self.huge:
            raise TooLargeError(size=format_bytes(self.span(k)[1] - self.span(k)[0]))
        s, e = self.span(k)
        return self.data[s:e]


# ----------------------------------------------------------------------------------
# format detection
# ----------------------------------------------------------------------------------
@dataclass
class _Head:
    data: bytes
    start: int        # absolute offset of data[0] (first non-whitespace byte)
    complete: bool    # data reaches the end of the file


def _read_head(f: Any, data_start: int, data_size: int, head_bytes: int, path: StrPath,
               cancel: CancelToken | None) -> _Head:
    pos = data_start
    while True:
        check(cancel)
        stop = min(data_size, pos + head_bytes)
        raw = read_range(f, pos, stop)
        stripped = raw.lstrip(_WS)
        if stripped:
            lead = len(raw) - len(stripped)
            start = pos + lead
            if lead and stop < data_size:
                # top up the head to head_bytes after skipping the leading whitespace
                stop = min(data_size, start + head_bytes)
                stripped = read_range(f, start, stop)
            return _Head(stripped, start, stop >= data_size)
        if stop >= data_size:
            raise FileFormatError("reader.empty_file", path=os.fspath(path))
        pos = stop


@dataclass
class _Source:
    """Random access to the data file during detection: ``read(start, end)`` (exact
    bytes), ``size`` = data size, ``head_bytes`` = ``Settings.head_bytes``."""

    read: Callable[[int, int], bytes]
    size: int
    head_bytes: int


def _several_kind(first_has_newline: bool, gap_has_newline: bool) -> str:
    """``jsonl`` when the first value is one line and a line break separates it from the
    next value, else ``stream``."""
    return "jsonl" if not first_has_newline and gap_has_newline else "stream"


def _next_value(src: _Source, pos: int, cancel: CancelToken | None) -> tuple[int, bool] | None:
    """The first non-whitespace byte at or after ``pos``: ``(offset, a line break was
    skipped)``; ``None`` at the end of the data (or after :data:`EXTEND_SCAN_BYTES` of
    whitespace)."""
    newline = False
    limit = min(src.size, pos + EXTEND_SCAN_BYTES)
    while pos < limit:
        check(cancel)
        stop = min(limit, pos + 64 * KiB)
        raw = src.read(pos, stop)
        stripped = raw.lstrip(_WS)
        lead = len(raw) - len(stripped)
        newline = newline or b"\n" in raw[:lead]
        if stripped:
            return pos + lead, newline
        pos = stop
    return None


def _tail_is_value_line(src: _Source, lo: int, scanner_cls: Callable[..., Any]) -> bool:
    """The end of the data looks like several values: the last non-blank line (starting
    after ``lo`` and found within the last :data:`TAIL_PROBE_BYTES`, then within the last
    ``head_bytes``) is a complete JSON value by itself. A single document ends with a line
    holding a closing bracket, or has no line break at all."""
    end = src.size
    window = TAIL_PROBE_BYTES
    while True:
        start = max(lo, end - window)
        if start >= end:
            return False
        stripped = src.read(start, end).rstrip(_WS)
        nl = stripped.rfind(b"\n")
        if nl >= 0 or start <= lo:
            break
        if window >= max(TAIL_PROBE_BYTES, src.head_bytes) or not stripped:
            return False
        window = max(TAIL_PROBE_BYTES, src.head_bytes)
    if nl < 0:
        return False
    line = stripped[nl + 1:].lstrip(_WS)
    if not line or line[0] not in _VALUE_START:
        return False
    sc = scanner_cls(MODE_SINGLE, [], 0)
    out = sc.feed(line)
    if sc.error is None:
        out += sc.finish()
    return sc.error is None and len(out) == 16


def _lines_or_stream(head: _Head, scanner_cls: Callable[..., Any], src: _Source | None = None,
                     cancel: CancelToken | None = None) -> str | None:
    """Rule 3: ``jsonl``/``stream`` when the data holds several top-level values (see the
    module docstring; ``src`` gives access beyond the head)."""
    sc = scanner_cls(MODE_STREAM, [], head.start)
    out = sc.feed(head.data)
    if head.complete and sc.error is None:
        out += sc.finish()
    if len(out) >= 32:
        a = _pairs(out[:32])
        s0, e0, s1 = a[0] - head.start, a[1] - head.start, a[2] - head.start
        return _several_kind(b"\n" in head.data[s0:e0], b"\n" in head.data[e0:s1])
    if sc.error is not None:
        data = head.data
        if not head.complete:
            data = data[:data.rfind(b"\n") + 1]
        if data:
            ls = scanner_cls(MODE_LINES, [], head.start)
            good = len(ls.feed(data)) // 16
            good += len(ls.finish()) // 16
            broken = len(ls.take_broken())
            if good >= 2 and good >= broken:
                return "jsonl"
        return None
    if head.complete or src is None:
        return None
    head_end = head.start + len(head.data)
    if len(out) == 16:
        # the first value ended in the head, the head ends inside or before a second one
        a = _pairs(out)
        s0, e0 = a[0] - head.start, a[1] - head.start
        first_nl = b"\n" in head.data[s0:e0]
        rest = head.data[e0:]
        stripped = rest.lstrip(_WS)
        if stripped:
            return _several_kind(first_nl, b"\n" in rest[:len(rest) - len(stripped)])
        nxt = _next_value(src, head_end, cancel)
        if nxt is None or src.read(nxt[0], nxt[0] + 1)[0] not in _VALUE_START:
            return None
        return _several_kind(first_nl, b"\n" in rest or nxt[1])
    # the head ends inside the first value: look further only when the end of the file
    # looks like several values (a big single document is not scanned)
    if not _tail_is_value_line(src, head_end, scanner_cls):
        return None
    newlines = head.data.count(b"\n")   # the first value did not end in the head
    pos = head_end
    limit = min(src.size, head.start + EXTEND_SCAN_BYTES)
    while True:
        if pos >= limit:
            return None   # too big to decide (or one value up to the end): the index tells
        check(cancel)
        stop = min(limit, pos + FEED_BYTES)
        chunk = src.read(pos, stop)
        out = sc.feed(chunk)
        if sc.error is not None:
            return None
        if out:
            e0 = _pairs(out[:16])[1]
            within = e0 - pos
            newlines += chunk.count(b"\n", 0, within)
            rest = chunk[within:]
            break
        newlines += chunk.count(b"\n")
        pos = stop
    stripped = rest.lstrip(_WS)
    if stripped:
        if stripped[0] not in _VALUE_START:
            return None
        return _several_kind(newlines > 0, b"\n" in rest[:len(rest) - len(stripped)])
    nxt = _next_value(src, stop, cancel)
    if nxt is None or src.read(nxt[0], nxt[0] + 1)[0] not in _VALUE_START:
        return None
    return _several_kind(newlines > 0, b"\n" in rest or nxt[1])


def _brace_list_like(data: bytes) -> bool:
    """Rule 4: ``{`` followed by a value instead of ``"key":``."""
    i = _skip_ws(data, 1)
    if i >= len(data):
        return False
    c = data[i]
    if c == ord("}"):
        return False  # {} is an empty object
    if c == ord('"'):
        m = _STRING_RE.match(data, i)
        if m is None:
            return False  # string cut by the head: assume a key
        j = _skip_ws(data, m.end())
        return j < len(data) and data[j] in b",}"
    return c in _VALUE_START


@dataclass
class _Analysis:
    candidates: list[RecordPathCandidate]   # sorted: open first, then items desc
    open_candidate: RecordPathCandidate | None
    members: int
    object_members: int
    total_values: int
    target_kind: str | None
    prefix_kinds: dict[int, str] = field(default_factory=dict)   # len(prefix) -> kind of its first value


def _segment_matches(path_seg: str | int, target_seg: str | int) -> bool:
    """A path segment of the head analysis (``int`` = array element, ``str`` = member key)
    against a record-path segment: a canonical-index ``str`` also addresses an element."""
    if isinstance(path_seg, int):
        if isinstance(target_seg, int):
            return path_seg == target_seg
        return canonical_index(target_seg) == path_seg
    return isinstance(target_seg, str) and path_seg == target_seg


def _analyze_object_head(head: _Head, target: Path_ | None = None) -> _Analysis:
    """Rule 5: walk the head with ``ijson.basic_parse`` collecting record-path candidates
    and root-member statistics (the analysis ends at the end of the head or at the first
    syntax error). With ``target``, the kind of the first value seen at each prefix of it
    (``prefix_kinds``; canonical-index ``str`` segments match array elements too)."""
    cands: dict[Path_, list[Any]] = {}       # path -> [closed, items, objects, values, arrays]
    order: list[Path_] = []
    # stack frames: [is_map, path, key, count, candidate_path_or_None]
    stack: list[list[Any]] = []
    active: list[Any] | None = None          # stats of the open candidate (cannot nest)
    members = object_members = total = 0
    prefix_kinds: dict[int, str] = {}
    tlen = len(target) if target is not None else -1

    def value_start(kind: str) -> Path_:
        nonlocal members, object_members, total
        total += 1
        if not stack:
            path: Path_ = ()
        else:
            parent = stack[-1]
            if parent[0]:
                path = parent[1] + (parent[2],)
                if len(stack) == 1:
                    members += 1
                    if kind == "object":
                        object_members += 1
            else:
                path = parent[1] + (parent[3],)
                parent[3] += 1
                if parent[4] is not None:
                    st = cands[parent[4]]
                    st[1] += 1
                    if kind == "object":
                        st[2] += 1
                    elif kind == "array":
                        st[4] += 1
        if active is not None:
            active[3] += 1
        if target is not None and len(path) <= tlen and len(path) not in prefix_kinds \
                and all(_segment_matches(p, q) for p, q in zip(path, target)):
            prefix_kinds[len(path)] = kind
        return path

    try:
        # use_float=False: with floats yajl rejects big integers and 1e400 ("overflow")
        for event, value in ijson.basic_parse(io.BytesIO(head.data), use_float=False, buf_size=1 << 20):
            if event == "map_key":
                stack[-1][2] = value
            elif event == "start_map":
                path = value_start("object")
                stack.append([True, path, None, 0, None])
            elif event == "start_array":
                path = value_start("array")
                cand = None
                if 1 <= len(path) <= MAX_CANDIDATE_DEPTH and all(isinstance(s, str) for s in path) \
                        and path not in cands:
                    cands[path] = [False, 0, 0, 0, 0]
                    order.append(path)
                    cand = path
                    active = cands[path]
                stack.append([False, path, None, 0, cand])
            elif event in ("end_map", "end_array"):
                frame = stack.pop()
                if frame[4] is not None:
                    cands[frame[4]][0] = True
                    active = None
            else:
                value_start("scalar")
    except (ijson.JSONError, ValueError, SystemError):
        # a truncated head, a syntax error (the scanner reports it) or an integer
        # longer than Python's int-from-string limit: the analysis ends here
        pass
    out = [RecordPathCandidate(p, bool(cands[p][0]), cands[p][1], cands[p][2], cands[p][3], cands[p][4])
           for p in order]
    open_cand = next((c for c in out if not c.closed_in_head), None)
    pos = {p: i for i, p in enumerate(order)}
    out.sort(key=lambda c: (c.closed_in_head, -c.items_in_head, c.depth, pos[c.path]))
    return _Analysis(out, open_cand, members, object_members, total, prefix_kinds.get(tlen), prefix_kinds)


def _resolve_record_path(record_path: Path_, kinds: dict[int, str]) -> tuple[Path_, Path_ | None]:
    """Canonical-index ``str`` segments of a forced record path become indices -- the
    rule of :mod:`jsonreshape.paths` -- unless the head shows an object at that place
    (``kinds``: kind of the value at each prefix length). Returns ``(path, alternative)``:
    ``alternative`` keeps the segments whose container was not seen in the head as member
    keys (tried when the first path is not found), else ``None``."""
    out = list(record_path)
    alt = list(record_path)
    unknown = False
    for j, seg in enumerate(record_path):
        if not isinstance(seg, str):
            continue
        idx = canonical_index(seg)
        if idx is None:
            continue
        kind = kinds.get(j)
        if kind == "object":
            continue
        out[j] = idx
        if kind is None:
            unknown = True
        else:
            alt[j] = idx
    return tuple(out), (tuple(alt) if unknown else None)


def _best_candidate(cands: list[RecordPathCandidate], max_depth: int = MAX_CANDIDATE_DEPTH,
                    min_items: int = 0) -> RecordPathCandidate | None:
    """Most items; tie: shallowest; then the earlier one (``cands`` is sorted with
    document order as the last key)."""
    best = None
    for c in cands:
        if c.depth > max_depth or c.items_in_head < min_items:
            continue
        if best is None or (c.items_in_head, -c.depth) > (best.items_in_head, -best.depth):
            best = c
    return best


def _decide_object(info: FormatInfo, analysis: _Analysis, complete: bool) -> None:
    """Rule 5 decision; sets ``info.kind`` / ``info.record_path``."""
    keyed_ok = analysis.members >= 2 and analysis.object_members * 5 >= analysis.members * 4
    oc = analysis.open_candidate
    chosen: RecordPathCandidate | None = None
    if oc is not None and not (keyed_ok and oc.items_in_head < analysis.members):
        chosen = oc
    elif complete:
        best = _best_candidate(analysis.candidates)
        if best is not None and best.items_in_head >= 2 and best.objects_in_head * 2 > best.items_in_head \
                and best.values_in_head * 2 > analysis.total_values:
            chosen = best
        elif keyed_ok:
            info.kind = "keyed"
            return
        else:
            chosen = _best_candidate(analysis.candidates, max_depth=2, min_items=2)
            if chosen is not None and chosen.objects_in_head * 2 <= chosen.items_in_head:
                # arrays of scalars in a small config-like object are not records
                # (ARCHITECTURE §5.1: "mostly objects"); the user can still pick them
                chosen = None
    elif keyed_ok:
        info.kind = "keyed"
        return
    if chosen is None:
        info.kind = "single"
        return
    info.kind = "wrapped"
    info.record_path = chosen.path
    info.add_warning("reader.record_path_auto", path=chosen.text)


def _extension_kind(path: StrPath) -> str | None:
    name = os.fspath(path).lower()
    if name.endswith(".gz"):
        name = name[:-3]
    return "jsonl" if name.endswith(JSONL_EXTENSIONS) else None


def _detect(prepared: PreparedFile, data_size: int, source: StrPath, settings: Settings,
            record_path: Path_ | None, fmt: str | None, scanner_cls: Callable[..., Any],
            cancel: CancelToken | None) -> FormatInfo:
    data_start = prepared.bom_len
    info = FormatInfo(kind="single", encoding=prepared.encoding, compression=prepared.compression,
                      data_start=data_start)
    for w in prepared.warnings:
        info.add_warning(w)
    with open_shared(prepared.data_path, "rb") as f:
        head = _read_head(f, data_start, data_size, settings.head_bytes, source, cancel)
        src = _Source(lambda s, e: read_range(f, s, min(e, data_size)), data_size, settings.head_bytes)
        _detect_kind(info, head, src, source, record_path, fmt, scanner_cls, cancel)
    return info


def _detect_kind(info: FormatInfo, head: _Head, src: _Source, source: StrPath, record_path: Path_ | None,
                 fmt: str | None, scanner_cls: Callable[..., Any], cancel: CancelToken | None) -> None:
    info.root_start = head.start
    info.root_char = chr(head.data[0]) if head.data[0] < 0x80 else ""
    info.head_complete = head.complete
    root = head.data[0]
    forced = fmt is not None or record_path is not None
    info.auto = not forced

    def object_analysis(target: Path_ | None = None) -> _Analysis | None:
        if root != ord("{"):
            return None
        analysis = _analyze_object_head(head, target)
        info.candidates = analysis.candidates
        return analysis

    if record_path is not None and record_path != ():
        if fmt not in (None, "wrapped"):
            raise UserInputError("reader.record_path_conflict", format=fmt, path=_path_text(record_path))
        info.kind = "wrapped"
        kinds: dict[int, str] = {}
        if root == ord("{"):
            kinds = object_analysis(record_path).prefix_kinds  # type: ignore[union-attr]
        elif root == ord("["):
            kinds = _analyze_object_head(head, record_path).prefix_kinds
        info.record_path, info.record_path_alt = _resolve_record_path(record_path, kinds)
        if kinds.get(len(record_path)) in ("array", "object"):
            info.container_kind = kinds[len(record_path)]
        return
    if record_path == ():
        if fmt not in (None, "wrapped", "array", "keyed"):
            raise UserInputError("reader.record_path_conflict", format=fmt, path="$")
        if root == ord("["):
            want = "array"
        elif root == ord("{"):
            want = "keyed"
        else:
            raise FileFormatError("reader.root_not_container", path=os.fspath(source))
        if fmt in ("array", "keyed") and fmt != want:
            raise FileFormatError("reader.format_mismatch", format=fmt, path=os.fspath(source))
        info.kind = want
        object_analysis()
        return
    if fmt is not None:
        if fmt == "wrapped":
            analysis = object_analysis()
            best = None
            if analysis is not None:
                best = analysis.open_candidate or _best_candidate(analysis.candidates)
            if best is None:
                raise FileFormatError("reader.no_record_path", path=os.fspath(source))
            info.kind = "wrapped"
            info.record_path = best.path
            info.container_kind = "array"
            info.add_warning("reader.record_path_auto", path=best.text)
            return
        if fmt in ("array", "keyed"):
            want = "array" if root == ord("[") else "keyed" if root == ord("{") else None
            if want != fmt:
                raise FileFormatError("reader.format_mismatch", format=fmt, path=os.fspath(source))
            if fmt == "keyed":
                object_analysis()
        elif fmt == "brace_list":
            info.add_warning("reader.warn_brace_list", example=BRACE_LIST_EXAMPLE)
        info.kind = fmt
        return

    # -- automatic detection ---------------------------------------------------------
    ext = _extension_kind(source)
    if ext is not None:
        info.kind = ext
        return
    several = _lines_or_stream(head, scanner_cls, src, cancel)
    if several is not None:
        info.kind = several
        return
    if root == ord("["):
        info.kind = "array"
        return
    if root != ord("{"):
        info.kind = "single"
        return
    if _brace_list_like(head.data):
        info.kind = "brace_list"
        info.add_warning("reader.warn_brace_list", example=BRACE_LIST_EXAMPLE)
        return
    analysis = object_analysis()
    assert analysis is not None
    _decide_object(info, analysis, head.complete)
    if info.kind == "wrapped":
        info.container_kind = "array"


def _data_size(prepared: PreparedFile, source: StrPath) -> int:
    """Bytes of data a document of ``prepared`` reads: the size of the prepared copy, or
    -- for a source read directly -- the size of the identified version (taken from the
    open handle: a file that grew since is read as that snapshot; one that is shorter now
    fails at its first read with :class:`FileChangedError`). :class:`FileChangedError`
    when the data file vanished right after it was prepared."""
    try:
        size = os.stat(long_path(prepared.data_path)).st_size
    except OSError as exc:
        raise FileChangedError(path=os.fspath(source)) from exc
    return size if prepared.converted else prepared.identity.size


def _normalize_format(fmt: str | None) -> str | None:
    if fmt is None:
        return None
    if not isinstance(fmt, str):
        raise UserInputError("reader.bad_format", format=repr(fmt), kinds=", ".join(KINDS))
    f = fmt.strip().lower()
    if f in ("", "auto"):
        return None
    if f not in KINDS:
        raise UserInputError("reader.bad_format", format=fmt, kinds=", ".join(KINDS))
    return f


def detect_format(path: StrPath, *, record_path: Any = None, format: str | None = None,
                  settings: Settings | None = None, progress: Progress | None = None,
                  cancel: CancelToken | None = None) -> FormatInfo:
    """Detect the format of ``path`` without indexing it (see the module docstring)."""
    settings = settings or get_settings()
    prepared = prepare_data_file(path, progress=progress, cancel=cancel, settings=settings)
    data_size = _data_size(prepared, path)
    _, scanner_cls = _scanner_factory()
    return _detect(prepared, data_size, path, settings, _norm_record_path(record_path),
                   _normalize_format(format), scanner_cls, cancel)


# ----------------------------------------------------------------------------------
# child span sources (node tree)
# ----------------------------------------------------------------------------------
class _Children:
    """Spans of a container's children: ``member`` = object member spans."""

    member: bool = False

    @property
    def count(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    def get(self, i: int) -> tuple[int, int]:  # pragma: no cover - abstract
        raise NotImplementedError

    def get_many(self, i: int, n: int) -> array:  # pragma: no cover - abstract
        raise NotImplementedError

    def scan_many(self, i: int, n: int) -> array:
        """``get_many`` for sequential scans (see ``IndexReader.read_many``)."""
        return self.get_many(i, n)

    @property
    def nbytes(self) -> int:
        return 0


class _MemChildren(_Children):
    __slots__ = ("spans", "member")

    def __init__(self, spans: array, member: bool) -> None:
        self.spans = spans
        self.member = member

    @property
    def count(self) -> int:
        return len(self.spans) // 2

    def get(self, i: int) -> tuple[int, int]:
        if i < 0 or i >= self.count:
            raise IndexError(i)
        return self.spans[2 * i], self.spans[2 * i + 1]

    def get_many(self, i: int, n: int) -> array:
        return self.spans[2 * i:2 * min(self.count, i + n)]

    @property
    def nbytes(self) -> int:
        return len(self.spans) * 8


class _DiskChildren(_Children):
    __slots__ = ("reader", "member")

    def __init__(self, reader: IndexReader, member: bool) -> None:
        self.reader = reader
        self.member = member

    @property
    def count(self) -> int:
        return self.reader.count

    def get(self, i: int) -> tuple[int, int]:
        return self.reader.get(i)

    def get_many(self, i: int, n: int) -> array:
        return _pairs(self.reader.get_many(i, n))

    def scan_many(self, i: int, n: int) -> array:
        return _pairs(self.reader.read_many(i, n))


class _RecordChildren(_Children):
    """The records of the document (main index)."""

    __slots__ = ("doc",)

    def __init__(self, doc: Document) -> None:
        self.doc = doc

    @property
    def member(self) -> bool:  # type: ignore[override]
        return self.doc.member_records

    @property
    def count(self) -> int:
        return self.doc.record_count

    def get(self, i: int) -> tuple[int, int]:
        return self.doc.record_span(i)

    def get_many(self, i: int, n: int) -> array:
        return _pairs(self.doc._spans_bytes(i, n))

    def scan_many(self, i: int, n: int) -> array:
        return _pairs(self.doc._spans_bytes(i, n, sequential=True))


# ----------------------------------------------------------------------------------
# data file handles
# ----------------------------------------------------------------------------------
class _ReadHandle:
    """One thread's shared-read handle on the data file (:meth:`Document._fh`).

    ``raw`` is an unbuffered ``FileIO`` (see "Reading records" in the module docstring);
    ``win`` holds the bytes from ``win_start`` fetched ahead for a forward walk of small
    reads and ``last_start``/``last_end`` the previous read (:meth:`Document._read`).
    ``lock`` is held for each read and while a mapping made through the handle is live,
    so :meth:`close` -- called from any thread by :meth:`Document.close` -- waits for
    them (the buffered reader's internal lock used to give this) and never closes a
    descriptor under a read in progress. ``unverified``: reads since the handle was last
    checked against the document's identity (:meth:`Document._verify_handle`).
    """

    __slots__ = ("raw", "lock", "win", "win_start", "last_start", "last_end", "unverified")

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.lock = threading.Lock()
        self.win = b""
        self.win_start = 0
        self.last_start = self.last_end = -1
        self.unverified = 0

    @property
    def closed(self) -> bool:
        return self.raw.closed

    def close(self) -> None:
        with self.lock:
            self.raw.close()


# ----------------------------------------------------------------------------------
# Document
# ----------------------------------------------------------------------------------
class Document:
    """An opened JSON file: format, record index, record access and node tree.

    Create with :meth:`open`. Thread-safe: records may be read from several threads
    (each thread uses its own shared-read handle) while the index is being built in the
    background. Use as a context manager or call :meth:`close`.

    The document reads the version of the file it opened (``data_size`` bytes of data);
    a file that only grows meanwhile is read as that snapshot -- :attr:`appended_bytes`
    and the ``reader.warn.appended`` warning in ``info.warnings`` tell how much was
    appended -- any other change raises :class:`~jsonreshape.errors.FileChangedError`
    (module docstring, "Changes while reading").
    """

    # -- construction ------------------------------------------------------------------
    def __init__(self, *, source_path: Path, prepared: PreparedFile, data_size: int, info: FormatInfo,
                 settings: Settings, backend: str, scanner_cls: Callable[..., Any],
                 forced_record_path: Path_ | None, forced_format: str | None) -> None:
        self.source_path = source_path
        self.prepared = prepared
        self.data_path = Path(prepared.data_path)
        self.identity: FileIdentity = prepared.identity
        self.size = prepared.identity.size
        self.data_size = data_size
        self.info = info
        self.settings = settings
        self.backend = backend
        self._scanner_cls = scanner_cls
        self._forced_record_path = forced_record_path
        self._forced_format = forced_format
        # index state
        self.error: JsonSyntaxError | None = None
        self.broken_lines: list[BrokenLine] = []
        self.broken_count = 0
        self.flags = 0
        self.container_span: tuple[int, int] | None = None
        self.member_records = info.kind == "keyed" or (info.kind == "wrapped" and info.container_kind == "object")
        self._member_known = info.kind != "wrapped" or info.container_kind is not None
        self.index_path: Path | None = None
        self.index_from_cache = False
        self.index_stats: dict[str, Any] = {}
        self._spans: IndexReader | IndexWriter | None = None
        self._complete = False
        self._closed = False
        self._state_lock = threading.RLock()
        self._build_lock = threading.Lock()
        self._cond = threading.Condition(threading.Lock())
        self._pending_builds = 0
        self._task: BackgroundTask | None = None
        self._broken_meta: list[list[Any]] = []
        # per-thread file handles
        self._local = threading.local()
        self._handles: list[Any] = []
        self._handles_lock = threading.Lock()
        # node children caches
        self._node_readers: dict[int, IndexReader] = {}
        self._mem_children: OrderedDict[int, _MemChildren] = OrderedDict()
        self._mem_children_bytes = 0
        self._children_lock = threading.RLock()
        self._trimmed_end_cache: int | None = None
        self._open_bounds: tuple[tuple[int, int] | None] | None = None
        self.bytes_scanned_for_nodes = 0   # diagnostics: bytes fed to node scanners
        self._use_cache = True
        self.format_hint: str | None = None          # "jsonl" / "stream": see the module docstring
        self.error_data_offset: int | None = None    # the error's offset in the data file
        # growth of the source after it was opened ("Changes while reading")
        self.appended_bytes = 0                      # bytes appended since the open (largest growth seen)
        self._notes_lock = threading.Lock()          # appended_bytes, the growth checks, info.warnings
        self._growth_seen: tuple[int, int] | None = None   # (size, mtime_ns) last verified as append-only
        self._growth_at = 0.0                        # time.monotonic() of that verification
        self._appended_params: dict[str, Any] | None = None   # params of the reader.warn.appended warning

    @classmethod
    def open(cls, path: StrPath, *, record_path: Any = None, format: str | None = None,
             settings: Settings | None = None, use_cache: bool = True,
             progress: Progress | None = None, cancel: CancelToken | None = None) -> Document:
        """Open ``path``: prepare it, detect the format, reuse a complete cached index.

        ``record_path``: path of the record container (``"data"``, ``"result.items"``,
        ``("a", 0)``, ``"/data/0"``; ``""``/``"$"`` = the root); ``format``: ``auto`` or one
        of :data:`KINDS`; ``use_cache=False`` ignores an existing cached index (a new one
        is still written; an index another process finishes while this one waits for it
        is used). Nothing heavy happens here; call :meth:`build_index` or
        :meth:`start_index` to index the records.
        """
        settings = settings or get_settings()
        fmt = _normalize_format(format)
        rp = _norm_record_path(record_path)
        source = Path(os.path.abspath(os.fspath(path)))
        prepared = prepare_data_file(source, progress=progress, cancel=cancel, settings=settings)
        data_size = _data_size(prepared, source)
        backend, scanner_cls = _scanner_factory()
        info = _detect(prepared, data_size, source, settings, rp, fmt, scanner_cls, cancel)
        doc = cls(source_path=source, prepared=prepared, data_size=data_size, info=info, settings=settings,
                  backend=backend, scanner_cls=scanner_cls, forced_record_path=rp, forced_format=fmt)
        doc._use_cache = bool(use_cache)
        if prepared.converted:
            # keep the prepared (gunzipped / transcoded) copy out of cache pruning while open
            retain_prepared(prepared.data_path)
            doc._retained_prepared = True
        try:
            if use_cache:
                doc._load_cached_index()
            doc._take_dir_notices()
        except BaseException:
            doc.close()
            raise
        log.info("opened %s: %s%s, %s, %d bytes%s", source, info.kind,
                 f" {info.record_path_text}" if info.record_path else "", info.encoding, data_size,
                 " (cached index)" if doc._complete else "")
        return doc

    # -- basic properties ----------------------------------------------------------------
    @property
    def kind(self) -> str:
        return self.info.kind

    @property
    def record_path(self) -> tuple[str | int, ...] | None:
        return self.info.record_path

    @property
    def index_complete(self) -> bool:
        """The index is final (scanned to the end or to the first JSON error)."""
        return self._complete

    @property
    def partial(self) -> bool:
        """A JSON error stopped indexing; only the records before it are available."""
        return self.error is not None

    @property
    def record_count(self) -> int:
        """Records indexed so far (grows live while the index is built)."""
        src = self._spans
        return src.count if src is not None else 0

    @property
    def closed(self) -> bool:
        return self._closed

    def __repr__(self) -> str:
        state = "complete" if self._complete else "indexing" if self._pending_builds else "not indexed"
        rp = f" {self.info.record_path_text}" if self.info.record_path else ""
        return (f"<Document {self.source_path.name!r} {self.info.kind}{rp} records={self.record_count} "
                f"{state}{' partial' if self.partial else ''}{' closed' if self._closed else ''}>")

    def __enter__(self) -> Document:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("document is closed")

    # -- file access -------------------------------------------------------------------
    def _changed(self) -> FileChangedError:
        return FileChangedError(path=str(self.source_path))

    def _verify_handle(self, raw: Any) -> None:
        """``fstat`` of an open handle on the data file against the version opened:
        :class:`FileChangedError` when the file is shorter now, or (a source read
        directly) has the same size and another modification time -- it was replaced or
        rewritten; a prepared copy must keep its size. A source that is longer now must
        only have grown (:meth:`_check_growth`)."""
        try:
            st = os.fstat(raw.fileno())
        except (OSError, ValueError) as exc:
            raise self._changed() from exc
        size = st.st_size
        if size == self.data_size:
            if self.prepared.converted or st.st_mtime_ns == self.identity.mtime_ns:
                return
            raise self._changed()
        if size < self.data_size or self.prepared.converted:
            raise self._changed()
        self._check_growth(raw, size, st.st_mtime_ns)

    def _check_growth(self, raw: Any, size: int, mtime_ns: int) -> None:
        """The source (read directly) behind ``raw`` is ``size`` bytes now, more than at
        open: accept it when bytes were only appended -- the head, tail and sample hashes
        of the original range are unchanged (:func:`~jsonreshape.fileio.grown_from`, ~2 ms;
        ``raw``'s position is kept) -- else :class:`FileChangedError`. The hashes are
        computed again when size or time changed since the last verification, but at most
        every :data:`GROWTH_RECHECK_SECONDS` while the file keeps growing (a file that got
        shorter is caught by its size at once; :meth:`ensure_unchanged` always hashes).
        The growth is recorded (:meth:`_note_appended`)."""
        state = (size, mtime_ns)
        with self._notes_lock:
            seen, at = self._growth_seen, self._growth_at
        recent = seen is not None and size >= seen[0] and (
            state == seen or time.monotonic() - at < GROWTH_RECHECK_SECONDS)
        if not recent:
            try:
                ok = grown_from(raw, self.identity)
            except (OSError, ValueError) as exc:
                raise self._changed() from exc
            if not ok:
                raise self._changed()
            with self._notes_lock:
                if self._growth_seen is None or size >= self._growth_seen[0]:
                    self._growth_seen = state
                    self._growth_at = time.monotonic()
        self._note_appended(size - self.data_size)

    def _note_appended(self, n: int) -> None:
        """``n`` bytes were appended to the source after it was opened (they are not
        read): :attr:`appended_bytes` becomes the largest growth seen, and the warning
        :data:`APPENDED_WARNING` is added to ``info.warnings`` once (its size parameter
        follows later growth)."""
        if n <= self.appended_bytes:
            return
        with self._notes_lock:
            if n <= self.appended_bytes:
                return
            first = self.appended_bytes == 0
            self.appended_bytes = n
            size = format_bytes(n)
            if self._appended_params is None:
                self.info.add_warning(APPENDED_WARNING, bytes=size)
                self._appended_params = self.info.warning_params[-1]
            else:
                self._appended_params["bytes"] = size
        if first:
            log.info("%s grew by %d bytes after it was opened; the appended data is not read",
                     self.source_path.name, n)

    def open_data(self, buffering: int = -1) -> Any:
        """A new shared-read handle on the data file (``open_shared``), checked against
        the version this document opened (:class:`FileChangedError` otherwise). The
        caller owns and closes it."""
        self._check_open()
        f = open_shared(self.data_path, "rb", buffering=buffering)
        try:
            self._verify_handle(f)
        except BaseException:
            f.close()
            raise
        return f

    def _fh(self) -> _ReadHandle:
        """This thread's handle on the data file (opened on first use and checked against
        the identity: a handle opened after the file was replaced reads the new file)."""
        h = getattr(self._local, "f", None)
        if h is None or h.raw.closed:
            self._check_open()
            raw = open_shared(self.data_path, "rb", buffering=0)
            try:
                self._verify_handle(raw)
            except BaseException:
                raw.close()
                raise
            h = _ReadHandle(raw)
            with self._handles_lock:
                if self._closed:
                    h.close()
                    raise ValueError("document is closed")
                self._handles.append(h)
            self._local.f = h
        return h

    def _verify_fh(self) -> None:
        """Check this thread's handle now (after a block, before results are used)."""
        h = self._fh()
        with h.lock:
            h.unverified = 0
            self._verify_handle(h.raw)

    def _read(self, start: int, end: int, exact: bool = False) -> bytes:
        """Bytes ``[start, end)`` of the data file through this thread's handle.

        An exact-size read, except that a small read (at most :data:`READ_AHEAD_MAX`
        bytes) continuing a forward walk -- it starts after the previous read's start and
        at most :data:`READ_AHEAD_GAP` after its end, e.g. ``record_bytes(i)`` for
        consecutive ``i`` -- fetches :data:`READ_AHEAD` bytes, and the reads that fall
        inside them are served from memory (what the buffered reader did for every read).
        ``exact``: never fetch ahead (the caller planned its reads: :meth:`_read_pieces`).
        Every :data:`VERIFY_READS` reads the handle is checked against the identity.
        """
        if end > self.data_size:
            raise FileChangedError(path=str(self.source_path))
        h = self._fh()
        with h.lock:
            if start < 0 or end <= start:
                return read_range(h.raw, start, end)   # b"" or ValueError
            h.unverified += 1
            if h.unverified >= VERIFY_READS:
                h.unverified = 0
                self._verify_handle(h.raw)
            ws = h.win_start
            if ws <= start and end - ws <= len(h.win):
                out = h.win[start - ws:end - ws]
            elif not exact and end - start <= READ_AHEAD_MAX and h.last_start < start <= h.last_end + READ_AHEAD_GAP:
                raw = h.raw
                raw.seek(start)
                win = raw.read(min(self.data_size, start + READ_AHEAD) - start)
                if win is not None and len(win) >= end - start:
                    h.win = win
                    h.win_start = start
                    out = win[:end - start]
                else:   # the file is shorter now: read_range retries and raises FileChangedError
                    out = read_range(raw, start, end)
            else:
                out = read_range(h.raw, start, end)
            h.last_start = start
            h.last_end = end
            return out

    def _iter_chunks(self, start: int, end: int, chunk: int = READ_CHUNK,
                     cancel: CancelToken | None = None) -> Iterator[bytes]:
        pos = start
        while pos < end:
            check(cancel)
            stop = min(end, pos + chunk)
            yield self._read(pos, stop)
            pos = stop

    def _trimmed_end(self) -> int:
        """Offset after the last non-whitespace byte of the data."""
        if self._trimmed_end_cache is None:
            pos = self.data_size
            lo = max(self.info.root_start, self.info.data_start)
            while pos > lo:
                start = max(lo, pos - 4096)
                raw = self._read(start, pos)
                stripped = raw.rstrip(_WS)
                if stripped:
                    pos = start + len(stripped)
                    break
                pos = start
            self._trimmed_end_cache = pos
        return self._trimmed_end_cache

    # -- cached index ------------------------------------------------------------------
    def _main_key(self) -> str:
        return cache_key(self.identity, self.info.kind, self.info.record_path)

    def _index_dir(self) -> Path:
        from .appdirs import index_dir

        return index_dir(self.settings.cache_dir)

    def _take_dir_notices(self) -> None:
        """Folder fallbacks of :mod:`jsonreshape.appdirs` (an unusable cache or data
        folder) become warnings of this document, so the user sees them once."""
        from .appdirs import take_notices

        for key, params in take_notices():
            with self._notes_lock:
                self.info.add_warning(key, **params)

    def _meta_matches(self, meta: dict[str, Any]) -> bool:
        """An index's metadata describes this document's data and format."""
        return (meta.get("kind") == "document" and meta.get("data_size") == self.data_size
                and meta.get("data_start") == self.info.data_start
                and (meta.get("format") or {}).get("kind") == self.info.kind)

    def _use_record_path_alt(self) -> bool:
        """Switch to the alternative record path (canonical-index segments whose
        container the head did not show taken as member keys); False when there is none."""
        alt = self.info.record_path_alt
        if alt is None:
            return False
        log.info("%s: record path %s not found; trying %s", self.source_path.name, self.info.record_path_text,
                 _path_text(alt))
        self.info.record_path_alt = None
        self.info.record_path = alt
        self.info.container_kind = None
        self.member_records = False
        self._member_known = False
        return True

    def _load_cached_index(self) -> bool:
        reader = find_index(self._main_key(), self._index_dir())
        if reader is None:
            return False
        meta = reader.meta
        if not self._meta_matches(meta):
            reader.close()
            return False
        try:
            err = meta.get("error")
            error = self._decorate_error(JsonSyntaxError.from_dict(err)) if err else None
            broken_raw = meta.pop("broken_lines", None) or []
            broken = [BrokenLine.from_list(b) for b in broken_raw[:BROKEN_KEEP]]
            span = meta.get("container_span")
            container_span = (int(span[0]), int(span[1])) if span else None
            hint = meta.get("format_hint")
            hint = hint if hint in ("jsonl", "stream") and self.info.auto else None
            data_offset = meta.get("error_data_offset")
            data_offset = int(data_offset) if data_offset is not None else (error.offset if error else None)
        except (KeyError, TypeError, ValueError, IndexError):
            log.warning("ignoring cached index %s with invalid metadata", reader.path)
            reader.close()
            return False
        if error is not None and error.code == "target_not_found" and self._use_record_path_alt():
            reader.close()
            return self._load_cached_index()
        with self._state_lock:
            self.error = error
            self.error_data_offset = data_offset
            self.format_hint = hint
            self.broken_lines = broken
            self.broken_count = int(meta.get("broken_count", len(broken)))
            self.container_span = container_span
            self.flags = int(meta.get("flags", 0))
            self.member_records = bool(meta.get("member_records", self.member_records))
            self._member_known = True
            if self.info.kind == "wrapped":
                self.info.container_kind = "object" if self.member_records else "array"
            self.index_stats = dict(meta.get("timings") or {})
            self.index_path = reader.path
            self.index_from_cache = True
            self._spans = reader
            self._complete = True
        return True

    # -- building ------------------------------------------------------------------------
    def start_index(self, progress: Progress | None = None, cancel: CancelToken | None = None) -> BackgroundTask:
        """Build the index in a daemon thread (returns the running task when one exists)."""
        self._check_open()
        with self._state_lock:
            task = self._task
            if task is not None and not task.done:
                return task
            with self._cond:
                self._pending_builds += 1

            def run(*, progress: Progress, cancel: CancelToken) -> None:
                try:
                    self.build_index(progress=progress, cancel=cancel)
                finally:
                    with self._cond:
                        self._pending_builds -= 1
                        self._cond.notify_all()

            task = BackgroundTask(run, name="index", progress=progress, cancel=cancel)
            self._task = task
            try:
                task.start()
            except BaseException:
                with self._cond:
                    self._pending_builds -= 1
                raise
            return task

    def wait_index(self, timeout: float | None = None) -> bool:
        """Wait for a background build started with :meth:`start_index`; returns
        :attr:`index_complete`."""
        task = self._task
        if task is not None:
            task.join(timeout)
        return self._complete

    def build_index(self, progress: Progress | None = None, cancel: CancelToken | None = None) -> None:
        """Index the records (blocking; idempotent; waits for a build running in another
        thread). JSON errors are not raised: see :attr:`error`. ``Cancelled`` leaves
        the document unindexed (``index_complete`` False)."""
        self._check_open()
        with self._cond:
            self._pending_builds += 1
        try:
            while not self._build_lock.acquire(timeout=0.05):
                check(cancel)
            try:
                if self._complete:
                    return
                self._check_open()
                self._build(progress, cancel)
            finally:
                self._build_lock.release()
        finally:
            with self._cond:
                self._pending_builds -= 1
                self._cond.notify_all()

    def _scan_mode(self) -> tuple[int, list[str | int]]:
        kind = self.info.kind
        if kind in ("array", "keyed"):
            return MODE_CONTAINER, []
        if kind == "wrapped":
            return MODE_CONTAINER, list(self.info.record_path or ())
        return {"jsonl": MODE_LINES, "stream": MODE_STREAM, "brace_list": MODE_BRACE_LIST,
                "single": MODE_SINGLE}[kind], []

    def _build(self, progress: Progress | None, cancel: CancelToken | None) -> None:
        self._build_locked(progress, cancel)
        if self.error is not None and self.error.code == "target_not_found" and self.info.record_path_alt is not None:
            # "/data/0" where "data" was not in the head and is an object with a key "0"
            with self._state_lock:
                old = self._spans
                self._spans = None
                self._complete = False
            if isinstance(old, IndexReader):
                old.close()
            self._use_record_path_alt()
            self._build_locked(None, cancel)

    def _build_locked(self, progress: Progress | None, cancel: CancelToken | None) -> None:
        """Index under the inter-process build lock of the key: a process that had to wait
        uses the index the other one installed (see the module docstring)."""
        waited = False
        while True:
            key = self._main_key()
            directory = self._index_dir()
            lock = BuildLock(lock_path(key, directory))
            while not lock.acquire():
                if not waited:
                    waited = True
                    log.info("%s is being indexed by another process; waiting for it", self.source_path.name)
                    if progress is not None:
                        progress.set_message(t("reader.progress.wait_index", name=self.source_path.name))
                check(cancel)
                time.sleep(WAIT_POLL_SECONDS)
            try:
                if (waited or self._use_cache) and self._load_cached_index():
                    log.info("using the index of %s built meanwhile", self.source_path.name)
                    if progress is not None:
                        progress.finish()
                    return
                if self._main_key() != key:
                    continue   # the cached index switched to the alternative record path: its own lock
                if waited and progress is not None:
                    progress.set_message(t("reader.progress.index", name=self.source_path.name))
                self._build_scan(progress, cancel, index_path(key, directory))
                break
            finally:
                lock.release()
        self._take_dir_notices()

    def _build_scan(self, progress: Progress | None, cancel: CancelToken | None, final: Path) -> None:
        started = time.perf_counter()
        mode, target = self._scan_mode()
        data_start = self.info.data_start
        total = self.data_size - data_start
        scanner = self._scanner_cls(mode, target, data_start)
        writer = IndexWriter(final)
        if progress is not None:
            if progress.total is None:
                progress.set_total(total)
            if not progress.message:
                progress.set_message(t("reader.progress.index", name=self.source_path.name))
        with self._state_lock:
            self.error = None
            self.error_data_offset = None
            self.format_hint = None
            self.broken_lines = []
            self.broken_count = 0
            self._broken_meta = []
            self.flags = 0
            self.container_span = None
            self._spans = writer
        lines_mode = mode == MODE_LINES
        sizes = list(FIRST_CHUNKS)
        first_seconds: float | None = None
        try:
            with open_shared(self.data_path, "rb", buffering=0) as f:
                self._verify_handle(f)
                f.seek(data_start)
                pos = data_start
                pieces = 0
                while pos < self.data_size:
                    check(cancel)
                    want = min(sizes.pop(0) if sizes else FEED_BYTES, self.data_size - pos)
                    chunk = f.read(want)
                    if not chunk:
                        raise FileChangedError(path=str(self.source_path))
                    pos += len(chunk)
                    pieces += 1
                    if pieces % VERIFY_BUILD_CHUNKS == 0:
                        self._verify_handle(f)
                    out = scanner.feed(chunk)
                    if out:
                        self._append_spans(writer, out)
                        if first_seconds is None:
                            first_seconds = time.perf_counter() - started
                    if lines_mode:
                        broken = scanner.take_broken()
                        if broken:
                            self._add_broken(broken)
                    self._notify()
                    if progress is not None:
                        progress.update(pos - data_start)
                    if scanner.error is not None:
                        break
                self._verify_handle(f)
            if scanner.error is None:
                out = scanner.finish()
                if out and scanner.error is not None and mode in (MODE_CONTAINER, MODE_BRACE_LIST):
                    out = self._drop_cut_number(out, scanner.error)
                if out:
                    self._append_spans(writer, out)
                    if first_seconds is None:
                        first_seconds = time.perf_counter() - started
                if lines_mode:
                    broken = scanner.take_broken()
                    if broken:
                        self._add_broken(broken)
            check(cancel)
            error = self._make_error(scanner.error) if scanner.error is not None else None
            hint = self._format_hint(scanner.error, mode) if scanner.error is not None else None
            if self.info.kind == "wrapped" and not self._member_known:
                self._resolve_member_from_container(scanner)
            container_span = scanner.container_span if mode == MODE_CONTAINER else None
            if not self.prepared.converted and self.check_changed():
                # read directly from the source: an index of a file that changed while it
                # was scanned (or of mixed versions) must never be installed
                raise FileChangedError(path=str(self.source_path))
            elapsed = time.perf_counter() - started
            scanned = scanner.consumed - data_start
            stats = {
                "seconds": round(elapsed, 6),
                "bytes": scanned,
                "mb_per_s": round(scanned / elapsed / 1e6, 3) if elapsed > 0 else 0.0,
                "first_record_seconds": None if first_seconds is None else round(first_seconds, 6),
                "backend": self.backend,
            }
            self.format_hint = hint
            data_offset = None if scanner.error is None else min(int(scanner.error[0]), self.data_size)
            meta = self._index_meta(error, container_span, scanner.flags, stats, data_offset)
            flags = 0
            if error is not None:
                flags |= FLAG_ERROR
            if self.member_records:
                flags |= FLAG_MEMBERS
            if self.broken_count:
                flags |= FLAG_BROKEN_LINES
            reader = writer.finalize(meta, flags, accept_existing=self._equivalent_index(writer, meta))
        except BaseException:
            writer.abort()
            with self._state_lock:
                if self._spans is writer:
                    self._spans = None
                self.broken_lines = []
                self.broken_count = 0
                self._broken_meta = []
                self.format_hint = None
            self._notify()
            raise
        with self._state_lock:
            self.error = error
            self.error_data_offset = data_offset
            self.container_span = (int(container_span[0]), int(container_span[1])) if container_span else None
            self.flags = scanner.flags
            self.index_stats = stats
            self.index_path = reader.path
            self.index_from_cache = False
            self._spans = reader
            self._complete = True
            self._broken_meta = []
        self._notify()
        if progress is not None:
            progress.finish()
        log.info("indexed %s: %d records, %.1f MB in %.2f s (%.0f MB/s, %s)%s", self.source_path.name,
                 reader.count, scanned / 1e6, elapsed, stats["mb_per_s"], self.backend,
                 f"; JSON error {error.code} at line {error.line}" if error is not None else "")
        self._evict()

    def _equivalent_index(self, writer: IndexWriter, meta: dict[str, Any]) -> Callable[[IndexReader], bool]:
        """For ``IndexWriter.finalize``: an installed index of the same key that cannot be
        replaced (another process has it mapped) is used instead of a duplicate when it
        has the same records -- count, a sample of spans and the error/format metadata."""
        count = writer.count
        picks = sorted({0, count - 1, *range(0, count, max(1, count // 62))}) if count else []
        sample = [(i, tuple(writer.get(i))) for i in picks if 0 <= i < count]

        def essentials(m: dict[str, Any]) -> tuple[Any, ...]:
            err = m.get("error") or {}
            return (err.get("code"), err.get("offset"), err.get("line"), err.get("column"), m.get("container_span"),
                    m.get("member_records"), m.get("broken_count"))

        want = essentials(meta)

        def accept(existing: IndexReader) -> bool:
            em = existing.meta
            if existing.count != count or not self._meta_matches(em) or essentials(em) != want:
                return False
            return all(tuple(existing.get(i)) == span for i, span in sample)

        return accept

    def _evict(self) -> None:
        """Keep the cache within ``Settings.cache_limit_bytes`` (best effort): index files
        and prepared copies share the budget."""
        try:
            from .appdirs import work_dir

            pdir = work_dir(self.settings.cache_dir) / "prepared"
            evict(self.settings.cache_limit_bytes, keep=self._paths_in_use(), directory=self._index_dir(),
                  prepared_dir=pdir if os.path.isdir(long_path(pdir)) else None)
        except Exception:
            log.warning("cache eviction failed", exc_info=True)

    def _paths_in_use(self) -> list[Path]:
        paths = [r.path for r in self._node_readers.values()]
        if self.index_path is not None:
            paths.append(self.index_path)
        return paths

    def _drop_cut_number(self, out: bytes, err: tuple[int, str, str, int, int]) -> bytes:
        """A truncated document (``unexpected_eof``) whose data ends right after a number
        record: the core completes a number at EOF, but the cut may have removed digits
        (``[1, 2, 123]`` cut to ``[1, 2, 12``), so that record is not kept."""
        if err[1] != "unexpected_eof":
            return out
        last = _pairs(out[-16:])
        if last[1] != self.data_size or self._read(last[1] - 1, last[1]) not in b"0123456789":
            return out
        log.info("%s: dropping the number record at %d cut by the end of the data", self.source_path.name, last[0])
        return out[:-16]

    def _append_spans(self, writer: IndexWriter, out: bytes) -> None:
        if not self._member_known:
            self._resolve_member_from_span(_pairs(out[:16]))
        writer.append(out)

    def _notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _resolve_member_from_span(self, first: array) -> None:
        """Wrapped documents with an unknown container kind: a member span is a string
        token followed by ``:`` (decided before the first span becomes visible; reads
        grow from 4 KiB only as far as the key token needs)."""
        s, e = first[0], first[1]
        member = False
        probe = KEY_PROBE
        while True:
            stop = min(e, s + probe)
            raw = self._read(s, stop)
            if raw[:1] != b'"':
                break
            m = _STRING_RE.match(raw)
            if m is not None and s + m.end() >= e:
                break  # the whole span is one string: an array element
            j = _skip_ws(raw, m.end()) if m is not None else len(raw)
            if j < len(raw):
                member = raw[j] == ord(":")
                break
            if stop >= e:
                break
            probe *= 8
        self.member_records = member
        self.info.container_kind = "object" if member else "array"
        self._member_known = True

    def _resolve_member_from_container(self, scanner: Any) -> None:
        span = getattr(scanner, "container_span", None)
        if span is not None:
            self.member_records = self._read(span[0], span[0] + 1) == b"{"
            self.info.container_kind = "object" if self.member_records else "array"
        self._member_known = True

    def _index_meta(self, error: JsonSyntaxError | None, container_span: tuple[int, int] | None, flags: int,
                    stats: dict[str, Any], error_data_offset: int | None = None) -> dict[str, Any]:
        fmt = self.info.to_dict()
        if APPENDED_WARNING in fmt["warnings"]:   # a note about this session, not about the indexed version
            keep = [i for i, key in enumerate(fmt["warnings"]) if key != APPENDED_WARNING]
            params = fmt["warning_params"]
            fmt["warnings"] = [fmt["warnings"][i] for i in keep]
            fmt["warning_params"] = [params[i] for i in keep if i < len(params)]
        return {
            "kind": "document",
            "format": fmt,
            "identity": self.identity.to_dict(),
            "source": str(self.source_path),
            "data_start": self.info.data_start,
            "data_size": self.data_size,
            "error": error.to_dict() if error is not None else None,
            "error_data_offset": error_data_offset,
            "format_hint": self.format_hint,
            "broken_count": self.broken_count,
            "broken_lines": self._broken_meta[:BROKEN_META_CAP],
            "container_span": list(container_span) if container_span else None,
            "flags": flags,
            "member_records": self.member_records,
            "backend": self.backend,
            "timings": stats,
            "created": time.time(),
        }

    # -- errors ----------------------------------------------------------------------------
    def _count_chars(self, start: int, end: int) -> int:
        total = 0
        for chunk in self._iter_chunks(start, end, MiB):
            total += _char_count(chunk)
        return total

    def _utf16_units(self, start: int, end: int) -> int:
        """UTF-16 code units of the UTF-8 data ``[start, end)``: one per character, two
        for a character outside the BMP (a 4-byte UTF-8 sequence)."""
        total = 0
        for chunk in self._iter_chunks(start, end, MiB):
            total += len(chunk.translate(None, _CONT_BYTES)) + len(chunk.translate(None, _NOT_ASTRAL_LEADS))
        return total

    def _source_offset(self, offset: int) -> int:
        """A data-file offset as a byte offset of the user's file: UTF-16 sources map back
        through the decoded text (BOM + 2 bytes per code unit); gzip sources give the offset
        in the decompressed data (a BOM the copy left out included)."""
        p = self.prepared
        if not p.converted:
            return offset
        base = p.source_bom
        if self.info.encoding.startswith("utf-16"):
            return base + 2 * self._utf16_units(self.info.data_start, offset)
        return base + offset - self.info.data_start

    def _decorate_error(self, err: JsonSyntaxError) -> JsonSyntaxError:
        """gzip sources: the message says that the byte offset is one of the decompressed
        data (``reader.error.json_syntax_decompressed``)."""
        if self.info.compression:
            err.key = "reader.error.json_syntax_decompressed"
        return err

    def _make_error(self, err: tuple[int, str, str, int, int]) -> JsonSyntaxError:
        offset, code, expected, line, line_start = err
        offset = min(offset, self.data_size)
        line_start = min(line_start, offset)
        column = 1 + self._count_chars(line_start, offset)
        lo = max(self.info.data_start, offset - CONTEXT_BYTES)
        hi = min(self.data_size, offset + CONTEXT_BYTES)
        before = _decode_suffix(self._read(lo, offset)) if lo < offset else ""
        after = _decode_prefix(self._read(offset, hi), final=hi >= self.data_size) if offset < hi else ""
        return self._decorate_error(JsonSyntaxError(
            code=code, offset=self._source_offset(offset), line=line + 1, column=column, expected=expected,
            context_before=before, context_after=after, source=str(self.source_path)))

    def _format_hint(self, err: tuple[int, str, str, int, int], mode: int) -> str | None:
        """``jsonl``/``stream`` when an automatically detected one-value document stops
        with ``trailing_data`` right after its root value and another value follows (see
        the module docstring); else ``None``."""
        offset, code, _expected, line, _line_start = err
        if code != "trailing_data" or not self.info.auto or mode not in (MODE_SINGLE, MODE_CONTAINER):
            return None
        if offset >= self.data_size or self._read(offset, offset + 1)[0] not in _VALUE_START:
            return None
        # back over the whitespace between the root value and the next value
        lo = max(self.info.root_start, self.info.data_start)
        pos = offset
        gap_newlines = 0
        while pos > lo:
            start = max(lo, pos - 64 * KiB)
            raw = self._read(start, pos)
            stripped = raw.rstrip(_WS)
            gap_newlines += raw.count(b"\n", len(stripped))
            if stripped:
                break
            pos = start
        if gap_newlines == 0:
            return "stream"
        # line breaks before the error = before the root (leading whitespace) + inside it + in the gap
        before_root = sum(c.count(b"\n") for c in self._iter_chunks(self.info.data_start, lo, MiB))
        inside = line - before_root - gap_newlines
        return "jsonl" if inside <= 0 else "stream"

    def _broken_line(self, raw: tuple[int, int, int, int, str, str]) -> BrokenLine:
        line_no, start, end, offset, code, expected = raw
        offset = min(offset, end)
        column = 1 + self._count_chars(start, offset)
        stop = min(end, start + 4 * PREVIEW_CHARS + 4)
        text = _decode_prefix(self._read(start, stop), final=stop >= end)
        if stop >= end and text.endswith("\r"):
            text = text[:-1]
        if len(text) > PREVIEW_CHARS or stop < end:
            text = text[:PREVIEW_CHARS - 1] + ELLIPSIS
        return BrokenLine(line_no + 1, start, end, offset, code, expected, column, text)

    def _add_broken(self, broken: list[tuple[int, int, int, int, str, str]]) -> None:
        for raw in broken:
            if len(self.broken_lines) < BROKEN_KEEP:
                bl = self._broken_line(raw)
                self.broken_lines.append(bl)
                self._broken_meta.append(bl.to_list())
            elif len(self._broken_meta) < BROKEN_META_CAP:
                line_no, start, end, offset, code, expected = raw
                self._broken_meta.append([line_no + 1, start, end, offset, code, expected])
            self.broken_count += 1

    def iter_broken_lines(self) -> Iterator[BrokenLine]:
        """All recorded broken lines (the first 100000; ``broken_count`` has the total);
        lines beyond the first 10000 get their column/preview computed on the fly."""
        yield from self.broken_lines
        src = self._spans
        if not self._complete or not isinstance(src, IndexReader) or self.broken_count <= len(self.broken_lines):
            return
        rows = src.read_meta().get("broken_lines") or []
        for row in rows[len(self.broken_lines):]:
            bl = BrokenLine.from_list(row)
            yield self._broken_line((bl.line - 1, bl.start, bl.end, bl.offset, bl.code, bl.expected))

    # -- waiting for records -----------------------------------------------------------
    def _ensure_complete(self, progress: Progress | None = None, cancel: CancelToken | None = None) -> None:
        if not self._complete:
            self.build_index(progress=progress, cancel=cancel)

    def _wait_records(self, n: int, cancel: CancelToken | None) -> int:
        """Wait until at least ``n`` records are indexed or the index is final; builds the
        index (blocking) when nobody is building it. Returns the current count."""
        while True:
            count = self.record_count
            if count >= n or self._complete:
                return count
            if self._pending_builds == 0:
                self.build_index(cancel=cancel)
                continue
            with self._cond:
                if self.record_count < n and not self._complete and self._pending_builds:
                    self._cond.wait(0.05)
            check(cancel)

    # -- records -----------------------------------------------------------------------
    def _spans_bytes(self, i: int, n: int, sequential: bool = False) -> bytes:
        """Spans ``i .. i+n-1`` (u64 LE pairs). ``sequential``: a batch of a scan over
        many records -- read from the final index file with an ordinary read, so the
        scanned index pages do not accumulate in this process's working set."""
        src = self._spans
        if src is None:
            return b""
        if sequential and isinstance(src, IndexReader):
            return src.read_many(i, n)
        return src.get_many(i, n)

    def record_span(self, i: int) -> tuple[int, int]:
        """Absolute ``(start, end)`` of record ``i`` (``IndexError`` when not indexed)."""
        i = operator.index(i)
        src = self._spans
        if src is None or i < 0 or i >= src.count:
            self._check_open()
            raise IndexError(f"record {i} out of range ({self.record_count} records)")
        s, e = src.get(i)
        return int(s), int(e)

    def is_huge(self, i: int) -> bool:
        """Record ``i`` is larger than ``Settings.large_node_bytes``."""
        s, e = self.record_span(i)
        return e - s > self.settings.large_node_bytes

    def record_bytes(self, i: int) -> bytes:
        """Raw bytes of record ``i`` (member records: ``"key": value``).
        :class:`TooLargeError` above ``Settings.large_node_bytes``;
        :class:`FileChangedError` when the bytes at the record's span cannot be a record
        any more (the file changed: see the module docstring)."""
        s, e = self.record_span(i)
        if e - s > self.settings.large_node_bytes:
            raise TooLargeError(size=format_bytes(e - s))
        buf = self._read(s, e)
        if not _looks_like_record(buf, self.member_records):
            raise self._changed()
        return buf

    def record(self, i: int) -> Any:
        """Parsed record ``i`` (member records: the member *value*)."""
        buf = self.record_bytes(i)
        if self.member_records:
            _, vo = split_member(buf)
            return codec.loads(buf[vo:])
        return codec.loads(buf)

    def _member_prefix(self, s: int, e: int) -> tuple[str, int]:
        """(key, value offset relative to ``s``) of the member span ``(s, e)``."""
        probe = KEY_PROBE
        while True:
            stop = min(e, s + probe)
            raw = self._read(s, stop)
            try:
                return split_member(raw)
            except ValueError:
                if stop >= e:
                    raise
                probe *= 8

    def record_key(self, i: int) -> str | None:
        """Decoded member key of record ``i`` (``None`` unless :attr:`member_records`)."""
        if not self.member_records:
            return None
        s, e = self.record_span(i)
        return self._member_prefix(s, e)[0]

    def record_value_span(self, i: int) -> tuple[int, int]:
        """Span of the record's value (the member value for member records)."""
        s, e = self.record_span(i)
        if not self.member_records:
            return s, e
        return s + self._member_prefix(s, e)[1], e

    @staticmethod
    def split_member(buf: bytes | bytearray | memoryview) -> tuple[str, int]:
        """``(decoded key, offset of the value in buf)`` of a member span."""
        return split_member(buf)

    # -- blocks ----------------------------------------------------------------------------
    def iter_blocks(self, source: Any = None, *, block_bytes: int = BLOCK_BYTES, progress: Progress | None = None,
                    cancel: CancelToken | None = None) -> Iterator[Block]:
        """Read records in blocks (spec §5.3).

        ``source``: ``None`` (all records, following a growing index until it is final;
        builds the index when nobody does), a ``range``, a sequence with
        ``__len__``/``__getitem__`` (e.g. a RecordSet; ``iter_chunks()`` is used when
        present) or any iterable of record indices. Ascending runs are read with single
        contiguous reads (gaps included) up to ``block_bytes`` while the gaps are small or
        the selected records cover at least :data:`DENSE_MIN` of the bytes read (only the
        latter once the source came back over bytes it had passed, as the runs of a sort
        order do); other orders (and sparse ascending selections) are fetched record by
        record in file order and joined with
        ``b"\\n"`` in iteration order (see "Reading records" in the module docstring: no
        mapping of the file outlives the block it serves); a record larger than
        ``Settings.large_node_bytes`` comes alone in a ``huge`` block.

        Progress: ``source=None`` -- bytes of the data read (the total, when not set, is
        the data size); any other source -- bytes of the selected records
        (:attr:`Block.selected_bytes`), so a total computed from the spans is never
        exceeded. Every block is checked against changes of the file
        (:class:`FileChangedError`, see "Changes while reading").
        """
        self._check_open()
        block_bytes = max(1, int(block_bytes))
        large = self.settings.large_node_bytes
        tracker = _ByteProgress(progress)
        if source is None:
            if progress is not None and progress.total is None:
                progress.set_total(max(0, self.data_size - self.info.data_start))
            i = 0
            while True:
                count = self._wait_records(i + 1, cancel)
                if i >= count:
                    break
                yield from self._range_blocks(i, count, block_bytes, large, tracker, cancel, read_bytes=True)
                i = count
            tracker.finish()
            return
        if isinstance(source, range) and source.step == 1:
            if len(source):
                if source.start < 0:
                    raise IndexError(f"record {source.start} out of range")
                count = self._wait_records(source.stop, cancel)
                if source.stop > count:
                    raise IndexError(f"record {source.stop - 1} out of range ({count} records)")
                yield from self._range_blocks(source.start, source.stop, block_bytes, large, tracker, cancel)
            tracker.finish()
            return
        yield from self._index_blocks(self._iter_indices(source), block_bytes, large, tracker, cancel)
        tracker.finish()

    @staticmethod
    def _iter_indices(source: Any) -> Iterator[int]:
        if isinstance(source, (list, tuple, array, range)):
            yield from source
        elif hasattr(source, "iter_chunks"):
            for chunk in source.iter_chunks():
                yield from chunk
        elif hasattr(source, "__len__") and hasattr(source, "__getitem__") and not isinstance(source, (dict, str)):
            for k in range(len(source)):
                yield source[k]
        else:
            yield from source

    # -- block checks (see "Changes while reading" in the module docstring) ---------------
    def _check_contiguous(self, data: bytes, base: int, spans: Any, j: int, k: int) -> None:
        """Sample at most :data:`SANITY_SAMPLES` records ``j .. k-1`` of a contiguous block
        (absolute pairs in ``spans``, ``data`` starting at ``base``): each must still look
        like a record; then check this thread's handle."""
        member = self.member_records
        step = max(1, (k - j) // SANITY_SAMPLES)
        for r in (*range(j, k, step), k - 1):
            s = spans[2 * r] - base
            e = spans[2 * r + 1] - base
            if e <= s or data[e - 1] not in _VALUE_END or (
                    data[s] != _QUOTE if member else data[s] not in _VALUE_START):
                raise self._changed()
        self._verify_fh()

    def _huge_block(self, i: int, s: int, e: int) -> Block:
        """A huge record's block: its first and last byte are checked (two tiny reads)."""
        member = self.member_records
        first = self._read(s, s + 1)
        last = self._read(e - 1, e)
        if last[0] not in _VALUE_END or (first[0] != _QUOTE if member else first[0] not in _VALUE_START):
            raise self._changed()
        self._verify_fh()
        return Block(b"", array("Q", (i,)), array("Q", (s, e)).tobytes(), False, True)

    def _range_blocks(self, i0: int, i1: int, block_bytes: int, large: int, tracker: _ByteProgress,
                      cancel: CancelToken | None, read_bytes: bool = False) -> Iterator[Block]:
        """Records ``i0 .. i1-1`` in contiguous blocks. Progress: the bytes read
        (``read_bytes``, whole-document iteration) or the bytes of the records."""
        i = i0
        while i < i1:
            check(cancel)
            n = min(SPAN_BATCH, i1 - i)
            a = _pairs(self._spans_bytes(i, n, sequential=True))
            n = len(a) // 2
            if n == 0:
                raise IndexError(f"record {i} out of range")
            starts = a[0::2]
            ends = a[1::2]
            # with large_node_bytes >= block_bytes only a record that does not fit a block
            # can be huge (checked below); otherwise find the huge records explicitly
            bigs = [k for k in range(n) if ends[k] - starts[k] > large] if large < block_bytes else []
            bi = 0
            j = 0
            while j < n:
                while bi < len(bigs) and bigs[bi] < j:
                    bi += 1
                stop = bigs[bi] if bi < len(bigs) else n
                if j == stop:
                    tracker.add(ends[j] - starts[j])
                    yield self._huge_block(i + j, starts[j], ends[j])
                    j += 1
                    continue
                s0 = starts[j]
                k = bisect.bisect_right(ends, s0 + block_bytes, j, stop)
                if k <= j:
                    e0 = ends[j]
                    if e0 - s0 > large:
                        tracker.add(e0 - s0)
                        yield self._huge_block(i + j, s0, e0)
                        j += 1
                        continue
                    k = j + 1
                e_last = ends[k - 1]
                data = self._read(s0, e_last)
                self._check_contiguous(data, s0, a, j, k)
                rel = array("Q", [v - s0 for v in a[2 * j:2 * k]])
                tracker.add(len(data) if read_bytes else sum(ends[j:k]) - sum(starts[j:k]))
                yield Block(data, array("Q", range(i + j, i + k)), rel.tobytes(), True, False, s0)
                j = k
                check(cancel)
            i += n

    def _span_wait(self, i: int, cancel: CancelToken | None) -> tuple[int, int]:
        i = operator.index(i)
        if i < 0:
            raise IndexError(f"record {i} out of range")
        if i >= self.record_count:
            count = self._wait_records(i + 1, cancel)
            if i >= count:
                raise IndexError(f"record {i} out of range ({count} records)")
        return self.record_span(i)

    def _index_blocks(self, indices: Iterable[int], block_bytes: int, large: int, tracker: _ByteProgress,
                      cancel: CancelToken | None) -> Iterator[Block]:
        idx: list[int] = []
        spans: list[int] = []      # absolute pairs
        mode = 0                   # 0 = one record, 1 = contiguous, 2 = scattered
        blk_start = blk_end = 0
        selected = 0               # record bytes of the block (contiguous: the density test)
        scatter_bytes = 0
        steps = 0
        dense = DENSE_MIN
        small_gap = CONTIGUOUS_GAP
        high = -1                  # the highest record end seen so far
        revisits = False           # the source came back over bytes it had passed (sort order runs)

        def flush() -> Block:
            if mode == 2:
                return self._scatter_block(idx, spans, tracker)
            data = self._read(blk_start, blk_end)
            self._check_contiguous(data, blk_start, spans, 0, len(idx))
            tracker.add(selected)
            rel = array("Q", [v - blk_start for v in spans])
            return Block(data, array("Q", idx), rel.tobytes(), True, False, blk_start)

        # fast path: spans straight from the memory-mapped final index
        src = self._spans
        view = src._spans if self._complete and isinstance(src, IndexReader) else None
        count = src.count if view is not None else 0
        for i in indices:
            steps += 1
            if steps & 1023 == 0:
                check(cancel)
            if view is not None and 0 <= i < count:
                s = view[2 * i]
                e = view[2 * i + 1]
            else:
                s, e = self._span_wait(i, cancel)
            size = e - s
            if s < high:
                revisits = True
            elif e > high:
                high = e
            if size > large:
                if idx:
                    yield flush()
                    idx, spans, mode = [], [], 0
                tracker.add(size)
                yield self._huge_block(i, s, e)
                continue
            if idx:
                # extend a contiguous read over a gap of at most CONTIGUOUS_GAP (cheaper to
                # read than to fetch the next record on its own), or of at most SCATTER_GAP
                # while the records cover DENSE_MIN of the range; once the source has come
                # back over bytes it already passed (the runs of a sort order) only the
                # density rule: each run read as a whole multiplied the bytes read by the
                # number of runs (see "Reading records" in the module docstring)
                gap = s - blk_end
                if mode != 2 and gap >= 0 and e - blk_start <= block_bytes and (
                        (gap <= small_gap and not revisits)
                        or (gap <= SCATTER_GAP and selected + size >= dense * (e - blk_start))):
                    idx.append(i)
                    spans += (s, e)
                    blk_end = e
                    selected += size
                    mode = 1
                    continue
                if mode != 1 and scatter_bytes + 1 + size <= block_bytes:
                    idx.append(i)
                    spans += (s, e)
                    scatter_bytes += 1 + size
                    mode = 2
                    continue
                yield flush()
                idx, spans = [], []
            idx.append(i)
            spans += (s, e)
            mode = 0
            blk_start, blk_end = s, e
            selected = size
            scatter_bytes = size
        if idx:
            yield flush()

    def _scatter_block(self, idx: list[int], spans: list[int], tracker: _ByteProgress) -> Block:
        """Records in arbitrary order (absolute ``spans``), joined with ``b"\\n"`` in
        iteration order. The bytes are fetched in file order: from transient mappings
        (:meth:`_map_pieces`, Windows, at least :data:`MAP_MIN_RECORDS` records) or by
        coalesced reads (:meth:`_read_pieces`). Every record is checked to still look like
        one. Progress: the bytes of the records."""
        n = len(idx)
        starts = spans[0::2]
        ends = spans[1::2]
        order = sorted(range(n), key=starts.__getitem__)
        pieces = self._map_pieces(starts, ends, order) if MAP_SCATTERED and n >= MAP_MIN_RECORDS else None
        if pieces is None:
            pieces = self._read_pieces(starts, ends, order)
        member = self.member_records
        rel = array("Q")
        pos = 0
        for p in pieces:
            if not _looks_like_record(p, member):
                raise self._changed()
            rel.append(pos)
            rel.append(pos + len(p))
            pos += len(p) + 1
        self._verify_fh()
        tracker.add(sum(ends) - sum(starts))
        return Block(b"\n".join(pieces), array("Q", idx), rel.tobytes(), False, False, -1)

    def _read_pieces(self, starts: list[int], ends: list[int], order: list[int]) -> list[bytes]:
        """The records ``starts[k]:ends[k]`` (``order``: indices by file position) read in
        file order; a neighbour is read together with the previous ones when the gap is
        at most :data:`SCATTER_SMALL_GAP`, or at most :data:`SCATTER_GAP` while the merged
        range stays :data:`DENSE_MIN` dense -- in ranges of at most :data:`SCATTER_GROUP`
        bytes. Returns the pieces (``k`` order)."""
        n = len(order)
        pieces = [b""] * n
        dense = DENSE_MIN
        j = 0
        while j < n:
            first = order[j]
            lo, hi = starts[first], ends[first]
            selected = hi - lo
            group = [first]
            j += 1
            while j < n:
                k = order[j]
                s, e = starts[k], ends[k]
                top = e if e > hi else hi
                if s - hi > SCATTER_GAP or top - lo > SCATTER_GROUP:
                    break
                if s - hi > SCATTER_SMALL_GAP and selected + e - s < dense * (top - lo):
                    break
                group.append(k)
                hi = top
                selected += e - s
                j += 1
            data = self._read(lo, hi, exact=True)
            for g in group:
                pieces[g] = data[starts[g] - lo:ends[g] - lo]
        return pieces

    def _map_pieces(self, starts: list[int], ends: list[int], order: list[int]) -> list[bytes] | None:
        """Like :meth:`_read_pieces` (same pieces), but slicing transient read-only
        mappings of the whole data file; ``None`` when the file cannot be mapped (the
        caller reads instead). A mapping never outlives this call: it is closed after
        :data:`MAP_BATCH_BYTES` (the pages it touched leave the working set) and at the
        end, under the handle's lock (:meth:`close` waits for it). Records larger than
        :data:`MAP_MAX_RECORD` are read. :class:`FileChangedError` when the file is now
        shorter than the records."""
        n = len(order)
        need = max(ends)
        if need > self.data_size:
            raise FileChangedError(path=str(self.source_path))
        max_record, page = MAP_MAX_RECORD, mmap.PAGESIZE
        pieces = [b""] * n
        h = self._fh()
        with h.lock:
            raw = h.raw
            fd = raw.fileno()
            j = 0
            while j < n:
                try:
                    mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
                except ValueError:   # "cannot mmap an empty file": truncated to nothing
                    raise FileChangedError(path=str(self.source_path)) from None
                except OSError:
                    log.debug("cannot map %s; reading the records instead", self.data_path, exc_info=True)
                    return None
                try:
                    if len(mm) < need:
                        raise FileChangedError(path=str(self.source_path))
                    budget = MAP_BATCH_BYTES
                    while j < n and budget > 0:
                        k = order[j]
                        j += 1
                        s = starts[k]
                        e = ends[k]
                        size = e - s
                        pieces[k] = mm[s:e] if size <= max_record else read_range(raw, s, e)
                        budget -= size + page
                finally:
                    mm.close()
        return pieces

    def iter_records(self, source: Any = None, *, include_huge: bool = False, block_bytes: int = BLOCK_BYTES,
                     progress: Progress | None = None, cancel: CancelToken | None = None) -> Iterator[tuple[int, Any]]:
        """``(index, value)`` for each record (member records: the value). Huge records
        are skipped, or yielded as ``(index, None)`` with ``include_huge=True``."""
        member = self.member_records
        loads = codec.loads
        for blk in self.iter_blocks(source, block_bytes=block_bytes, progress=progress, cancel=cancel):
            if blk.huge:
                if include_huge:
                    yield int(blk.indices[0]), None
                continue
            sp = _pairs(blk.spans)
            data = blk.data
            for k, i in enumerate(blk.indices):
                buf = data[sp[2 * k]:sp[2 * k + 1]]
                if member:
                    _, vo = split_member(buf)
                    yield i, loads(buf[vo:])
                else:
                    yield i, loads(buf)

    def iter_record_bytes(self, source: Any = None, *, include_huge: bool = False, block_bytes: int = BLOCK_BYTES,
                          progress: Progress | None = None,
                          cancel: CancelToken | None = None) -> Iterator[tuple[int, bytes | None]]:
        """``(index, raw record bytes)``; huge records skipped or ``(index, None)``."""
        for blk in self.iter_blocks(source, block_bytes=block_bytes, progress=progress, cancel=cancel):
            if blk.huge:
                if include_huge:
                    yield int(blk.indices[0]), None
                continue
            sp = _pairs(blk.spans)
            data = blk.data
            for k, i in enumerate(blk.indices):
                yield i, data[sp[2 * k]:sp[2 * k + 1]]

    # -- wrapper ---------------------------------------------------------------------------
    def wrapper_spans(self, progress: Progress | None = None,
                      cancel: CancelToken | None = None) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """Wrapped documents: ``((prefix_start, prefix_end), (suffix_start, suffix_end))``
        -- the bytes before and after the record container (brackets excluded from both:
        the prefix ends at the container's ``[``/``{``, the suffix starts after its
        closing bracket). Builds the index when needed. ``None`` for other kinds and when
        the container never closed (a partial document)."""
        if self.info.kind != "wrapped":
            return None
        self._ensure_complete(progress, cancel)
        span = self.container_span
        if span is None:
            return None
        return (self.info.data_start, span[0]), (span[1], self.data_size)

    def wrapper_parts(self, progress: Progress | None = None,
                      cancel: CancelToken | None = None) -> tuple[bytes, bytes] | None:
        """Raw ``(prefix, suffix)`` bytes around the record container (see
        :meth:`wrapper_spans`); :class:`TooLargeError` when a part exceeds
        ``Settings.large_node_bytes`` (stream it with :meth:`wrapper_spans` instead)."""
        spans = self.wrapper_spans(progress, cancel)
        if spans is None:
            return None
        parts = []
        for s, e in spans:
            if e - s > self.settings.large_node_bytes:
                raise TooLargeError(size=format_bytes(e - s))
            parts.append(self._read(s, e))
        return parts[0], parts[1]

    # -- changes ---------------------------------------------------------------------------
    def ensure_unchanged(self) -> None:
        """Raise :class:`~jsonreshape.errors.FileChangedError` when the source file
        changed (or vanished) since it was opened (AGENT_PROMPT §1.7). Operations call
        this before committing an output and at the end of long scans, so data read from
        a file that another program rewrote meanwhile is never reported as a success. A
        file that only grew does not raise (see :meth:`check_changed`): the operation used
        the snapshot opened, and ``info.warnings`` says how much was appended. Cost: a
        ``stat`` and, when size and time still match or the file grew, hashing ~2 MiB
        (~2 ms)."""
        if self.check_changed():
            raise FileChangedError(path=str(self.source_path))

    def check_changed(self) -> bool:
        """True when the source file changed (or vanished) since it was opened: shorter,
        the same size with another modification time, other hashes of the first MiB, the
        last MiB or the sampled pages (a same-size edit with the time restored), or longer
        with any of those hashes of the *original* range changed; for a gzip/UTF-16 source
        also a changed prepared copy. A file that only grew -- bytes appended to the
        version opened, like an active log -- is **not** changed: the document keeps
        reading that snapshot, and :attr:`appended_bytes` / the ``reader.warn.appended``
        warning in ``info.warnings`` record the growth (module docstring, "Changes while
        reading"). A file that another program holds exclusively for a moment (a backup
        tool, a virus scanner: sharing violation) or a network drive that does not answer
        is retried for :data:`CHANGE_RETRY_SECONDS` before it counts as changed (so a
        deleted file is reported after that delay)."""
        deadline = time.monotonic() + CHANGE_RETRY_SECONDS
        delay = 0.02
        while True:
            state = self._change_state()
            if state is not None:
                return state
            if time.monotonic() >= deadline:
                return True
            time.sleep(delay)
            delay = min(delay * 2, 0.2)

    def _change_state(self) -> bool | None:
        """``True`` changed, ``False`` unchanged (or only grown: recorded with
        :meth:`_note_appended`), ``None`` cannot tell yet (the file is locked by another
        program, a network drive does not answer; a deleted file also gets the retry
        window before it counts as changed)."""
        # size and time come from an open handle (fstat), like the identity's own -- not
        # from the path's directory entry, which e.g. network file systems may cache
        try:
            grown = _appended_to(self.source_path, self.identity)   # hashes the version opened
        except JsonReshapeError as exc:
            return None if exc.key in ("fileio.error.in_use", "error.permission", "error.file_not_found") else True
        except Exception:
            return True
        if grown is None:
            return True
        if self.prepared.converted:
            try:
                if os.stat(long_path(self.data_path)).st_size != self.data_size:
                    return True
            except OSError:
                return True
        if grown:
            self._note_appended(grown)
        return False

    def reopen(self, *, use_cache: bool = True, progress: Progress | None = None,
               cancel: CancelToken | None = None) -> Document:
        """Open the current version of the file with the same forced options (a detected
        record path is detected again) and close this document. Returns the new one."""
        doc = Document.open(self.source_path, record_path=self._forced_record_path, format=self._forced_format,
                            settings=self.settings, use_cache=use_cache, progress=progress, cancel=cancel)
        self.close()
        return doc

    # -- closing ---------------------------------------------------------------------------
    def close(self) -> None:
        """Stop a running build, release the index and all file handles (idempotent)."""
        if self._closed:
            return
        task = self._task
        if task is not None and not task.done:
            task.cancel()
            task.join(30)
        self._closed = True
        with self._state_lock:
            src = self._spans
            self._spans = None
            self._complete = False
        if isinstance(src, IndexReader):
            src.close()
        elif isinstance(src, IndexWriter):
            src.abort()
        with self._children_lock:
            for reader in self._node_readers.values():
                reader.close()
            self._node_readers.clear()
            self._mem_children.clear()
            self._mem_children_bytes = 0
        with self._handles_lock:
            handles, self._handles = self._handles, []
        for f in handles:
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass
        if getattr(self, "_retained_prepared", False):
            self._retained_prepared = False
            release_prepared(self.prepared.data_path)
        self._notify()

    # -- node tree -------------------------------------------------------------------------
    def _record_base_path(self) -> Path_:
        return tuple(self.info.record_path or ()) if self.info.kind == "wrapped" else ()

    def _value_kind_at(self, offset: int) -> str:
        b = self._read(offset, offset + 1)
        return _kind_of(b[0]) if b else "null"

    def record_node(self, i: int) -> Node:
        """The :class:`Node` of record ``i`` (path ``(i,)``; keyed ``(key,)``; wrapped
        ``record_path + (i,)`` / ``+ (key,)``; single: the root value ``()``)."""
        s, e = self.record_span(i)
        if self.info.kind == "single":
            return Node(self, (), s, e, self._value_kind_at(s), None)
        if self.member_records:
            key, vo = self._member_prefix(s, e)
            vs = s + vo
            return Node(self, self._record_base_path() + (key,), vs, e, self._value_kind_at(vs), key)
        return Node(self, self._record_base_path() + (i,), s, e, self._value_kind_at(s), i)

    def _container_bounds(self) -> tuple[int, int] | None:
        """Record container span; for a container that never closed: (start, data end)."""
        if self.container_span is not None:
            return self.container_span
        if self.info.kind != "wrapped" or not self._complete:
            return None
        if self._open_bounds is None:
            self._open_bounds = (self._find_open_container(),)
        return self._open_bounds[0]

    def _find_open_container(self) -> tuple[int, int] | None:
        if self.record_count:
            first = self.record_span(0)[0]
            pos = first
            while pos > self.info.data_start:
                lo = max(self.info.data_start, pos - 4096)
                raw = self._read(lo, pos).rstrip(_WS)
                if raw:
                    if raw[-1:] in (b"[", b"{"):
                        return lo + len(raw) - 1, self._trimmed_end()
                    return None
                pos = lo
        return None

    def records_node(self) -> Node | None:
        """The node whose children are the records: the root container (array/keyed),
        the virtual root (jsonl/stream/brace_list) or the record container (wrapped,
        builds the index when needed). ``None`` for single documents."""
        kind = self.info.kind
        if kind in ("jsonl", "stream", "brace_list"):
            return Node(self, (), self.info.data_start, self.data_size, "array", None, role=_ROLE_VIRTUAL)
        if kind in ("array", "keyed"):
            if self.container_span is not None:
                s, e = self.container_span
            else:
                s, e = self.info.root_start, self._trimmed_end()
            return Node(self, (), s, e, "array" if kind == "array" else "object", None, role=_ROLE_RECORDS)
        if kind == "wrapped":
            self._ensure_complete()
            bounds = self._container_bounds()
            path = self._record_base_path()
            if bounds is None:
                raise UserInputError("reader.path_not_found", path=_path_text(path))
            kind_ = "object" if self.member_records else "array"
            key = path[-1] if path else None
            return Node(self, path, bounds[0], bounds[1], kind_, key, role=_ROLE_RECORDS)
        return None

    def root(self) -> Node:
        """The root node (see the module docstring)."""
        self._check_open()
        kind = self.info.kind
        if kind in ("array", "keyed", "jsonl", "stream", "brace_list"):
            node = self.records_node()
            assert node is not None
            return node
        start = self.info.root_start
        if kind == "single" and self._complete and self.record_count:
            s, e = self.record_span(0)
            return Node(self, (), s, e, self._value_kind_at(s), None)
        end = self._trimmed_end()
        role = _ROLE_ANCESTOR if kind == "wrapped" else None
        return Node(self, (), start, end, _kind_of(ord(self.info.root_char or "n")), None, role=role)

    def node(self, path: Any, *, cancel: CancelToken | None = None) -> Node:
        """Navigate to ``path`` (path text or segments; ``str`` segments that are
        canonical indices address array elements, like :func:`jsonreshape.paths.get`;
        duplicate keys resolve to the last occurrence). ``UserInputError``
        ``reader.path_not_found`` when there is no such node."""
        self._check_open()
        if isinstance(path, str):
            path = parse_path(path)
        path = tuple(path)
        kind = self.info.kind
        rp = self._record_base_path()
        cur: Node | None
        if kind in ("array", "keyed", "jsonl", "stream", "brace_list", "wrapped") and len(path) > len(rp) \
                and all(_segment_equal(a, b) for a, b in zip(path[:len(rp)], rp)):
            # a record: straight to the main index (no ancestor scan, no waiting for the
            # whole index unless a member key must be searched)
            cur = self._find_record_node(path[len(rp)], cancel)
            rest = path[len(rp) + 1:]
        elif kind == "wrapped" and len(path) == len(rp) and all(
                _segment_equal(a, b) for a, b in zip(path, rp)):
            cur, rest = self.records_node(), ()
        else:
            cur, rest = self.root(), path
        for seg in rest:
            if cur is None:
                break
            cur = cur.find_child(seg, cancel=cancel)
        if cur is None:
            raise UserInputError("reader.path_not_found", path=_path_text(path))
        return cur

    def _find_record_node(self, seg: str | int, cancel: CancelToken | None) -> Node | None:
        """The record addressed by path segment ``seg`` (index, or member key)."""
        i = self._find_record_index(seg, cancel)
        return None if i is None else self.record_node(i)

    def _find_record_index(self, seg: str | int, cancel: CancelToken | None) -> int | None:
        """Number of the record addressed by path segment ``seg`` (index, or member key:
        the last record with that key), ``None`` when there is none."""
        if not self._member_known:
            self._wait_records(1, cancel)  # the container kind is known with the first record
        if self.member_records:
            if not isinstance(seg, str):
                return None
            return self._find_record_by_key(seg, cancel)
        if isinstance(seg, bool):
            return None
        idx = seg if isinstance(seg, int) else canonical_index(seg)
        if idx is None or idx < 0:
            return None
        try:
            self._span_wait(idx, cancel)
        except IndexError:
            return None
        return idx

    def _find_record_by_key(self, key: str, cancel: CancelToken | None) -> int | None:
        """Index of the LAST record whose member key equals ``key`` (dict semantics)."""
        self._ensure_complete(cancel=cancel)
        token = None
        try:
            token = json.dumps(key, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:  # lone surrogates: only escaped keys can match
            token = None
        finder = core.MultiFinder([token]) if token is not None and not self.flags & FLAG_ESCAPES else None
        found: int | None = None
        for blk in self.iter_blocks(None, cancel=cancel):
            if blk.huge:
                i = int(blk.indices[0])
                if self.record_key(i) == key:
                    found = i
                continue
            sp = _pairs(blk.spans)
            if finder is not None:
                hits = _pairs(finder.find_in_spans(blk.data, blk.spans))
                cand = [int(h) for h in hits]
            else:
                cand = range(len(blk.indices))
            data = blk.data
            for k in cand:
                m = _MEMBER_RE.match(data, sp[2 * k], sp[2 * k + 1])
                if m is None:
                    continue
                tok = m.group(1)
                if (tok == token) if token is not None and b"\\" not in tok else _decode_key_token(tok) == key:
                    found = int(blk.indices[k])
        return found

    # -- node children ---------------------------------------------------------------------
    def _node_index_key(self, start: int) -> str:
        return cache_key(self.identity, self.info.kind, self.info.record_path, extra=f"node:{start}")

    def _cached_children(self, node: Node) -> _Children | None:
        with self._children_lock:
            mem = self._mem_children.get(node.start)
            if mem is not None:
                self._mem_children.move_to_end(node.start)
                return mem
            reader = self._node_readers.get(node.start)
            if reader is not None:
                return _DiskChildren(reader, node.kind == "object")
        return None

    def _remember_children(self, start: int, children: _Children) -> None:
        with self._children_lock:
            if isinstance(children, _DiskChildren):
                old = self._node_readers.get(start)
                if old is not None and old is not children.reader:
                    old.close()
                self._node_readers[start] = children.reader
                return
            if not isinstance(children, _MemChildren):
                return
            if start in self._mem_children:
                self._mem_children_bytes -= self._mem_children.pop(start).nbytes
            self._mem_children[start] = children
            self._mem_children_bytes += children.nbytes
            while self._mem_children and (self._mem_children_bytes > MEM_CHILDREN_CACHE_BYTES
                                          or len(self._mem_children) > MEM_CHILDREN_CACHE_ENTRIES):
                _, old_mem = self._mem_children.popitem(last=False)
                self._mem_children_bytes -= old_mem.nbytes

    def _children_of(self, node: Node, progress: Progress | None, cancel: CancelToken | None) -> _Children:
        if node.role in (_ROLE_RECORDS, _ROLE_VIRTUAL):
            return _RecordChildren(self)  # live: grows while the index is built
        cached = self._cached_children(node)
        if cached is not None:
            return cached
        children = self._scan_children(node, progress, cancel)
        self._remember_children(node.start, children)
        return children

    def _scan_children(self, node: Node, progress: Progress | None, cancel: CancelToken | None) -> _Children:
        """Index the children of ``node`` (on disk when large) by streaming its bytes."""
        member = node.kind == "object"
        large = self.settings.large_node_bytes
        bounds: tuple[int, int] | None = None
        if node.role == _ROLE_ANCESTOR:
            self._ensure_complete(progress, cancel)
            bounds = self._container_bounds()
            if bounds is not None and not (node.start <= bounds[0] and bounds[1] <= node.end):
                bounds = None
        if bounds is not None:
            segments = [(node.start, bounds[0]), None, (bounds[1], node.end)]
            scan_bytes = (bounds[0] - node.start) + (node.end - bounds[1]) + 2
        else:
            segments = [(node.start, node.end)]
            scan_bytes = node.size
        disk = scan_bytes > large
        limit = self.settings.large_node_children
        # an expensive scan is persisted even for a short child list; a node this big may
        # also have more than large_node_children children (each child takes at least 2
        # bytes): in both cases its children may already be in the on-disk cache
        persist = scan_bytes >= NODE_CACHE_MIN_BYTES
        if disk or persist or scan_bytes >= 2 * limit:
            reader = find_index(self._node_index_key(node.start), self._index_dir())
            if reader is not None:
                if reader.meta.get("kind") == "node" and reader.meta.get("start") == node.start \
                        and reader.meta.get("end") == node.end:
                    if disk or reader.count > limit:
                        return _DiskChildren(reader, member)
                    spans = reader.spans_array()  # a short list: keep it in memory
                    reader.close()
                    return _MemChildren(spans, member)
                reader.close()
        if progress is not None:
            if progress.total is None:
                progress.set_total(scan_bytes)
            if not progress.message:
                progress.set_message(t("reader.progress.children"))
        scanner = self._scanner_cls(MODE_CONTAINER, [], node.start)
        collector = _ChildCollector(self, node, disk, member, persist)
        delta = (bounds[1] - bounds[0] - 2) if bounds is not None else 0
        cstart = bounds[0] if bounds is not None else 0
        done = 0
        try:
            for seg in segments:
                if seg is None:
                    pieces: Iterable[bytes] = (b"[]",)
                else:
                    pieces = self._iter_chunks(seg[0], seg[1], FEED_BYTES, cancel)
                for piece in pieces:
                    out = scanner.feed(piece)
                    done += len(piece)
                    self.bytes_scanned_for_nodes += len(piece)
                    if out:
                        if bounds is not None:
                            out = _shift_spans(out, cstart, delta)
                        collector.add(out)
                    if progress is not None:
                        progress.update(done)
                    if scanner.error is not None:
                        break
                if scanner.error is not None:
                    break
            if scanner.error is None:
                out = scanner.finish()
                if out:
                    if bounds is not None:
                        out = _shift_spans(out, cstart, delta)
                    collector.add(out)
            if scanner.error is not None and not (bounds is not None and self.container_span is None):
                raise self._make_error(scanner.error)
            children = collector.finish()
        except BaseException:
            collector.abort()
            raise
        if collector.writer is not None:
            if isinstance(children, _DiskChildren):
                self._remember_children(node.start, children)  # in use: protected from eviction
            self._evict()
        return children


_ROLE_RECORDS = "records"
_ROLE_VIRTUAL = "virtual"
_ROLE_ANCESTOR = "ancestor"


def _segment_equal(a: str | int, b: str | int) -> bool:
    if type(a) is type(b):
        return a == b
    if isinstance(a, str) and isinstance(b, int):
        return canonical_index(a) == b
    return False


def _shift_spans(out: bytes, cstart: int, delta: int) -> bytes:
    """Map placeholder coordinates back to file offsets (``v > cstart`` -> ``v + delta``)."""
    a = _pairs(out)
    return array("Q", [v + delta if v > cstart else v for v in a]).tobytes()


class _ByteProgress:
    """Cumulative byte progress for iter_blocks()."""

    __slots__ = ("progress", "done")

    def __init__(self, progress: Progress | None) -> None:
        self.progress = progress
        self.done = 0

    def add(self, n: int) -> None:
        self.done += n
        if self.progress is not None:
            self.progress.update(self.done)

    def finish(self) -> None:
        if self.progress is not None:
            self.progress.finish()


class _ChildCollector:
    """Collects child spans in memory, spilling to an on-disk index when the node has
    more than ``large_node_children`` children (or always, for large nodes). A short
    list from an expensive scan (``persist``) is kept in memory and also written to the
    cache, so reopening the document does not scan the node again."""

    def __init__(self, doc: Document, node: Node, disk: bool, member: bool, persist: bool = False) -> None:
        self.doc = doc
        self.node = node
        self.member = member
        self.persist = persist
        self.limit = doc.settings.large_node_children
        self.mem: array | None = None if disk else array("Q")
        self.writer: IndexWriter | None = None
        if disk:
            self._open_writer()

    def _open_writer(self) -> None:
        doc = self.doc
        self.writer = IndexWriter(index_path(doc._node_index_key(self.node.start), doc._index_dir()))

    def add(self, out: bytes) -> None:
        if self.mem is not None:
            if len(self.mem) // 2 + len(out) // 16 > self.limit:
                self._open_writer()   # spill: memory spans first, then the new batch
                assert self.writer is not None
                self.writer.append(self.mem.tobytes())
                self.mem = None
                self.writer.append(out)
            else:
                self.mem.frombytes(out)
            return
        assert self.writer is not None
        self.writer.append(out)

    def _meta(self) -> tuple[dict[str, Any], int]:
        node = self.node
        meta = {"kind": "node", "start": node.start, "end": node.end, "path": list(node.path),
                "identity": self.doc.identity.to_dict(), "created": time.time()}
        return meta, FLAG_NODE | (FLAG_MEMBERS if self.member else 0)

    def finish(self) -> _Children:
        if self.mem is not None:
            if self.persist:
                try:
                    self._open_writer()
                    assert self.writer is not None
                    self.writer.append(self.mem.tobytes())
                    self.writer.finalize(*self._meta()).close()
                except Exception:  # caching is best effort
                    log.warning("could not cache the children of %s", self.node, exc_info=True)
                    self.abort()
            return _MemChildren(self.mem, self.member)
        assert self.writer is not None
        reader = self.writer.finalize(*self._meta())
        return _DiskChildren(reader, self.member)

    def abort(self) -> None:
        if self.writer is not None:
            self.writer.abort()


# ----------------------------------------------------------------------------------
# Node
# ----------------------------------------------------------------------------------
class Node:
    """A lazily expanded JSON value inside a :class:`Document`.

    ``path``: absolute path (tuple of keys/indices); ``start``/``end``: byte span of the
    value (for object members the value only, the key is ``key``); ``kind``: ``object``,
    ``array``, ``string``, ``number``, ``bool`` or ``null``; ``key``: member key (str),
    array index (int) or ``None`` for the root. Children are indexed on demand (see the
    module docstring).
    """

    __slots__ = ("doc", "path", "start", "end", "kind", "key", "role")

    def __init__(self, doc: Document, path: Path_, start: int, end: int, kind: str, key: str | int | None,
                 *, role: str | None = None) -> None:
        self.doc = doc
        self.path = tuple(path)
        self.start = start
        self.end = end
        self.kind = kind
        self.key = key
        self.role = role

    def __repr__(self) -> str:
        return f"Node({_path_text(self.path)!r}, {self.kind}, {self.start}..{self.end})"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Node) and other.doc is self.doc and other.path == self.path
                and other.start == self.start and other.end == self.end)

    def __hash__(self) -> int:
        return hash((id(self.doc), self.path, self.start, self.end))

    # -- basics ------------------------------------------------------------------------
    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def is_container(self) -> bool:
        return self.kind in ("object", "array")

    @property
    def is_virtual(self) -> bool:
        """The synthetic root of jsonl/stream/brace_list documents (no real bytes)."""
        return self.role == _ROLE_VIRTUAL

    @property
    def is_large(self) -> bool:
        return self.size > self.doc.settings.large_node_bytes

    @property
    def depth(self) -> int:
        return len(self.path)

    @property
    def parent_path(self) -> Path_ | None:
        return self.path[:-1] if self.path else None

    def parent(self) -> Node | None:
        """The parent node (``None`` for the root)."""
        pp = self.parent_path
        return None if pp is None else self.doc.node(pp)

    @property
    def path_text(self) -> str:
        return _path_text(self.path)

    # -- children ------------------------------------------------------------------------
    def _children(self, progress: Progress | None = None, cancel: CancelToken | None = None) -> _Children:
        return self.doc._children_of(self, progress, cancel)

    def child_count(self, progress: Progress | None = None, cancel: CancelToken | None = None) -> int:
        """Number of children (0 for scalars); indexes the children on first use."""
        if not self.is_container:
            return 0
        if self.role in (_ROLE_RECORDS, _ROLE_VIRTUAL):
            self.doc._ensure_complete(progress, cancel)  # the final number of records
        return self._children(progress, cancel).count

    @property
    def children_indexed(self) -> bool:
        """``child_count()`` will not scan (children already indexed or not needed)."""
        if not self.is_container:
            return True
        if self.role in (_ROLE_RECORDS, _ROLE_VIRTUAL):
            return self.doc.index_complete
        return self.doc._cached_children(self) is not None

    def child(self, i: int) -> Node:
        """Child ``i`` (``IndexError`` when out of range)."""
        i = operator.index(i)
        nodes = self.children(i, i + 1)
        if not nodes:
            raise IndexError(f"child {i} out of range")
        return nodes[0]

    def children(self, start: int = 0, stop: int | None = None) -> list[Node]:
        """Children ``start .. stop-1`` (``stop=None``: to the end). The records of a
        document being indexed are served as soon as they exist (only ``stop=None``
        waits for the whole index)."""
        if not self.is_container:
            return []
        if self.role in (_ROLE_RECORDS, _ROLE_VIRTUAL):
            if stop is None:
                self.doc._ensure_complete()
            else:
                self.doc._wait_records(stop, None)
        src = self._children()
        count = src.count
        stop = count if stop is None else min(stop, count)
        start = max(0, start)
        if start >= stop:
            return []
        spans = src.get_many(start, stop - start)
        doc = self.doc
        lo, hi = spans[0], spans[-1]
        buf: bytes | None = None
        if hi - lo <= MiB:
            buf = doc._read(lo, hi)
        out = []
        bounds = doc._container_bounds() if doc.info.kind == "wrapped" else None
        for k in range(len(spans) // 2):
            s, e = spans[2 * k], spans[2 * k + 1]
            idx = start + k
            if src.member:
                if buf is not None:
                    try:
                        key, vo = split_member(buf[s - lo:min(e, s + KEY_PROBE) - lo])
                    except ValueError:
                        key, vo = doc._member_prefix(s, e)
                else:
                    key, vo = doc._member_prefix(s, e)
                vs = s + vo
                ckey: str | int = key
            else:
                vs = s
                ckey = idx
            if buf is not None and vs < hi:
                kind = _kind_of(buf[vs - lo])
            else:
                kind = doc._value_kind_at(vs)
            role = None
            if bounds is not None:
                if (vs, e) == bounds:
                    role = _ROLE_RECORDS
                elif vs <= bounds[0] and bounds[1] <= e:
                    role = _ROLE_ANCESTOR
            out.append(Node(doc, self.path + (ckey,), vs, e, kind, ckey, role=role))
        return out

    def find_child(self, seg: str | int, *, cancel: CancelToken | None = None) -> Node | None:
        """The child at path segment ``seg`` (dict semantics: the last duplicate key
        wins; a canonical-index string addresses an array element), or ``None``."""
        i = self.find_child_index(seg, cancel=cancel)
        if i is None:
            return None
        if self.role in (_ROLE_RECORDS, _ROLE_VIRTUAL):
            return self.doc.record_node(i)
        return self.child(i)

    def find_child_index(self, seg: str | int, *, cancel: CancelToken | None = None) -> int | None:
        """The child index :meth:`find_child` resolves ``seg`` to (``child(i)``; for the
        node holding the records -- the root of array/keyed/jsonl/stream/brace_list
        documents, the record container of wrapped ones -- the record number), or
        ``None``. Object members: the position of the LAST member with key ``seg``.
        Indexes the children on first use (blocking: worker threads only in the GUI)."""
        if self.role in (_ROLE_RECORDS, _ROLE_VIRTUAL):
            return self.doc._find_record_index(seg, cancel)
        if self.kind == "array":
            idx = seg if isinstance(seg, int) and not isinstance(seg, bool) else (
                canonical_index(seg) if isinstance(seg, str) else None)
            if idx is None or idx < 0:
                return None
            if idx >= self.child_count(cancel=cancel):
                return None
            return idx
        if self.kind != "object" or not isinstance(seg, str):
            return None
        src = self._children(cancel=cancel)
        found: int | None = None
        token = None
        try:
            token = json.dumps(seg, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            token = None
        pos = 0
        count = src.count
        while pos < count:
            check(cancel)
            n = min(SPAN_BATCH, count - pos)
            spans = src.scan_many(pos, n)
            lo, hi = spans[0], spans[-1]
            buf = self.doc._read(lo, hi) if hi - lo <= 4 * MiB else None
            for k in range(len(spans) // 2):
                s, e = spans[2 * k], spans[2 * k + 1]
                if buf is not None:
                    m = _MEMBER_RE.match(buf, s - lo, e - lo)
                    if m is None:
                        continue
                    tok = m.group(1)
                    match = (tok == token) if token is not None and b"\\" not in tok else (
                        _decode_key_token(tok) == seg)
                else:
                    match = self.doc._member_prefix(s, e)[0] == seg
                if match:
                    found = pos + k
            pos += n
        return found

    # -- values ------------------------------------------------------------------------
    def value(self) -> Any:
        """The parsed value (:class:`TooLargeError` above ``Settings.large_node_bytes``)."""
        doc = self.doc
        large = doc.settings.large_node_bytes
        if self.role == _ROLE_VIRTUAL:
            doc._ensure_complete()
            total = 0
            if doc.record_count:
                total = doc.record_span(doc.record_count - 1)[1] - doc.record_span(0)[0]
            if total > large:
                raise TooLargeError(size=format_bytes(total))
            return [v for _, v in doc.iter_records()]
        if self.size > large:
            raise TooLargeError(size=format_bytes(self.size))
        return codec.loads(doc._read(self.start, self.end))

    def raw(self) -> bytes:
        """The raw bytes of the value (:class:`TooLargeError` when large)."""
        if self.size > self.doc.settings.large_node_bytes:
            raise TooLargeError(size=format_bytes(self.size))
        return self.doc._read(self.start, self.end)

    def scalar_text(self, max_chars: int = 200) -> str:
        """Short display text: the raw token of a scalar (long strings/numbers truncated
        with an ellipsis); for containers a whitespace-collapsed prefix of the JSON."""
        max_chars = max(2, int(max_chars))
        if self.role == _ROLE_VIRTUAL:
            return "[" + ELLIPSIS + "]"
        n = min(self.size, 4 * max_chars + 8)
        raw = self.doc._read(self.start, self.start + n)
        text = _decode_prefix(raw, final=n >= self.size)
        truncated = n < self.size
        if self.is_container:
            text = _WS_RUN_RE.sub(" ", text)
        if truncated or len(text) > max_chars:
            return text[:max_chars - 1] + ELLIPSIS
        return text
