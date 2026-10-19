"""Tolerant JSON repair with a complete report (docs/ARCHITECTURE.md §14.5, AGENT_PROMPT §10 "Ta'mirlash").

:func:`repair_file` reads a broken JSON / JSON Lines file and writes a strictly valid one, listing every
change it made (:class:`RepairReport`): what, where (user-file byte offset, 1-based line and column computed
exactly like the reader's JSON errors, the offset in the repaired file, the record) and how many. Nothing is
changed silently; data the repair cannot interpret stops it (:class:`UnrepairableError`) instead of being
guessed. The design is s5/map_repair_diff.md §3 (fix catalogue §3.1, the H2 hybrid §3.2, decisions §3.6).

Fix catalogue (stable codes; category ``syntax`` = no value changed, ``data`` = a value changed,
``dropped`` = data removed)
================================================================================================
=================  ========  ======================================================================
code               category  repair
=================  ========  ======================================================================
``bom``            syntax    a byte-order mark at the start, between values or between tokens: removed
``comment``        syntax    ``//`` up to the line end (the ``\\r``/``\\n`` stay) and ``/* */`` (its
                             ``\\r``/``\\n`` bytes stay, so line numbers do not move): removed
``trailing_comma`` syntax    a comma before ``]``/``}`` (comments may be between), or after the last
                             value of a stream / a JSON Lines line: removed
``missing_comma``  syntax    two values (members) in a container with no comma, when the first one ends
                             with ``}`` ``]`` ``"`` ``'`` or whitespace/comments separate them: a comma is
                             inserted right after the first one (``01``, ``1-2``, ``truefalse`` stay errors)
``single_quote``   syntax    ``'abc'`` (value or key) -> ``"abc"``; ``\\'`` -> ``'``, ``"`` -> ``\\"``
``unquoted_key``   syntax    an identifier in key position (``[A-Za-z_$]`` or UTF-8 letters) -> ``"key"``
``nan``            data      ``NaN`` ``+NaN`` ``-NaN`` -> ``null`` (``RepairOptions.nan="string"``: ``"NaN"``)
``infinity``       data      ``Infinity`` ``+Infinity`` ``-Infinity`` -> ``null`` (or ``"Infinity"`` /
                             ``"-Infinity"``)
``python_literal`` syntax    ``True`` ``False`` ``None`` -> ``true`` ``false`` ``null``
``undefined``      data      ``undefined`` -> ``null``
``control_char``   syntax    a raw control character inside a string -> ``\\t`` ``\\n`` ``\\r`` ``\\b``
                             ``\\f`` or ``\\u00XX``
``invalid_escape`` data      ``\\'`` -> ``'``, ``\\xHH`` -> ``\\u00HH``, any other bad escape -> a literal
                             backslash (``\\d`` -> ``\\\\d``)
``invalid_utf8``   data      each maximal invalid UTF-8 subsequence inside a string -> U+FFFD (Python's
                             decoder); more than :data:`UTF8_LIMIT` -> stop ("probably not UTF-8")
``brace_list``     syntax    a ``{ {...}, {...} }`` root -> ``[ ... ]`` (one entry at the opening brace;
                             the root's closing ``}`` becomes ``]`` with it)
``concatenated``   syntax    values one after another (kind ``stream``) -> one array: ``[`` before the first
                             value, ``,`` right after every value but the last, ``]`` after the last (one
                             entry); with ``output_kind="jsonl"`` one value per line instead
``line_split``     syntax    JSON Lines: several values on one line -> one line each (a ``\\n`` inserted
                             after the first, or a separating comma replaced by it)
``truncated``      dropped   the data ends inside the document: everything after the last complete record
                             is dropped and the open containers are closed (:class:`Truncation`)
``trailing_data``  dropped   bytes after a complete root value that do not start another value (extra
                             closers, junk), and NUL padding at the end of the file: removed
``line_dropped``   dropped   JSON Lines: a line that cannot be repaired (or is cut off): removed with its
                             line feed
``dropped_tail``   dropped   the partial result before an unrepairable problem (``allow_partial``):
                             everything from the last complete record before it to the end of the
                             file is dropped and the open containers are closed (one entry; its
                             ``reason`` is the problem code; fix round R5, R5F-6)
=================  ========  ======================================================================

Unrepairable (:class:`RepairProblem` codes): ``mismatched_closer`` (``[1, 2}``), ``unquoted_value``
(``{"a": hello}``, ``ObjectId(...)``), ``invalid_number`` (``01``, ``1.``, ``.5``, ``0x1F``, ``+5`` inside
containers), ``unexpected_char`` (``#`` comments, a double comma, a missing colon, bytes that cannot start
anything), ``depth_limit``, ``not_utf8``, ``nothing_complete`` (a cut scalar root), ``extra_value`` (a
value after the root of a document whose kind was forced), ``disabled`` (a needed fix is not in
``RepairOptions.fixes``). The pass stops there; with ``allow_partial`` the records before the problem are
written, closed as for a truncation (``report.truncated``) and listed as one ``dropped_tail`` fix (also in
a report that wrote nothing: it tells what the partial result drops); otherwise nothing is written and
:class:`UnrepairableError` (``.report``) is raised. JSON Lines lines are repaired one by one: a line that
cannot be repaired is dropped (``line_dropped``, the reason in ``RepairFix.reason``), not a stop.

Architecture (s5/map_repair_diff.md §3.2, "H2")
================================================
* :class:`_Tokenizer` -- the tolerant tokenizer -- is the reference semantics. It reads windows of the data
  file (resumable at any byte: strings, comments and whitespace are consumed in pieces, a token cut by the
  window end waits for more data), copies valid stretches lazily (strict regex fast paths of
  ``core._py_patterns``: one match per record, runs of elements inside records, a strict key with its
  colon) and applies the fixes. Fast paths only ever consume what the state machine would accept the same
  way (a window of 1 byte never uses them, and the results are identical -- tested): a complete
  single-quoted string with nothing else to fix inside is converted in one step, and after
  :data:`REC_FAILS` broken records in a row the next :data:`REC_SKIP` records skip the record fast path
  (it would scan each of them in vain).
* The strict core ``Scanner`` (Rust or Python) is the accelerator (``RepairOptions.accelerate``): records it
  validates are copied verbatim; at a scanner error at ``E`` the tokenizer re-reads from the end of the last
  record ``R <= E`` (a sync point with a known state: :class:`_Sync`) to the first record end ``R' > E``;
  then a new scanner starts at ``R'`` behind a *synthetic prefix* (``[null``, ``{"data":[null``,
  ``{"":null``, ``{null``...) that recreates the nesting. Dense errors (eight failures within 4 KiB of the
  restart in a row) keep the tokenizer running for at least 1 MiB. Output and fix list are identical with
  and without the accelerator for every window size (tested): a verbatim record is exactly what the
  tokenizer emits for it, and every decision depends on source positions only. With the pure-Python core
  the scanner is skipped (the tokenizer is faster than the Python scanner) unless a backend was chosen with
  :func:`set_scanner_backend`.
* Kinds: ``array`` / ``keyed`` / ``brace_list`` (records = root children), ``wrapped`` (records = children
  of the record container; the containers on the record path are commit levels too), ``single`` (commit
  level = the root's children; one record), ``stream`` (records = top-level values, written as one array),
  ``jsonl`` (lines: the strict ``MODE_LINES`` scanner copies good lines, broken lines go through a line
  tokenizer one at a time).
* Commit model: output and fixes become final only at *commit points* -- the end of a record or of another
  child of a commit-level container, and right after a commit-level container opens. A cut-off file
  (``truncated``) or an unrepairable problem rolls back to the last commit point and appends the closers
  of the containers open there (``]}``), so nothing ever has to be rewound in the written output.
  Uncommitted output stays in memory up to 8 MiB and spills to a side file beyond (a huge broken record);
  every fix goes to a temporary JSON Lines file at once (``fixes_file``), the first ``max_items`` also stay
  in memory; a rollback truncates both. Fixes that must wait for an earlier entry (the ones inside a
  single-quoted string wait for the string's own entry, the ones behind a pending comma for its decision)
  are held in memory up to :data:`DEFER_MAX` rows and in a temporary file beyond, so memory stays bounded
  whatever the size of a record, string or gap.
* Gaps between tokens are held while a decision is pending (a comma that may turn out to be trailing, a
  comma that may have to be inserted right after the previous value) for at most :data:`HOLD_MAX` source
  bytes; beyond that (only pathological whitespace/comment runs) the decision falls back deterministically:
  the missing comma is inserted right before the next value, a pending comma is written in place and, if a
  closer follows, overwritten with a space (still reported as ``trailing_comma``).
* Output: UTF-8 without BOM (UTF-16 / UTF-8-BOM inputs are converted, reported), gzip only when the target
  name ends with ``.gz``; written through ``fileio.atomic_write`` (temp file, atomic replace) and validated
  by the core ``Scanner`` in the output kind's mode in a helper thread (the record count included; chunks
  of at least 1 MiB, so the Rust scanner runs without the GIL, at most :data:`QUEUE_BYTES` waiting): a
  failed validation is an internal error (``repair.error.invalid_output``) and nothing is replaced. The
  source's identity (``fileio.appended_bytes``) is checked before the commit: a changed source raises
  :class:`~jsonreshape.errors.FileChangedError` (nothing written); a source that only grew is repaired as
  the snapshot that was read (``reader.warn.appended``).
* Locations: ``RepairFix.offset`` is a byte offset of the user's file (UTF-16 sources mapped back through the
  decoded text, gzip sources: the offset in the decompressed data, plus the BOM the prepared copy left out
  -- exactly ``reader.Document._source_offset``); ``line`` / ``column`` are 1-based, the column in
  characters from the line start (``reader.Document._make_error``: non-continuation bytes, the first line
  starting after a UTF-8 BOM); ``out_offset`` is the byte offset in the repaired (uncompressed) output and
  ``record`` the 0-based output record holding the fix (``None`` between records). ``length`` /
  ``out_length`` give the exact replaced spans (data-file bytes / output bytes).

Decisions (README §15 D1-D10 and where §14.5 left room; all chosen to keep the user's data safe)
=================================================================================================
1. D1-D10 as s5/map_repair_diff.md §3.6: NaN/Infinity -> ``null`` (``nan="string"`` keeps the text), the
   missing-comma rule of §3.1, unquoted string values / mismatched closers / invalid numbers unrepairable,
   invalid UTF-8 -> U+FFFD with the 1 000 guard, output kind = input kind with ``brace_list`` / ``stream``
   -> ``array`` (JSON Lines stays JSON Lines), never in place, nothing written for an unrepairable file
   without ``allow_partial``.
2. Commit levels are finer than §3.2.5 for ``wrapped`` documents: every child of a container on the record
   path is a commit point (``{"result": {"count": 5, "items": [...], "more": tr`` keeps ``count`` and
   ``items``), and a commit-level container that opened is kept (closed) even with no complete child
   (``{"meta": {...}, "data": [{"a": 1`` -> ``{"meta": {...}, "data": []}``, 0 records kept).
3. A number that ends exactly where a cut-off document ends is not complete (``[1, 2, 12`` -> ``[1, 2]``,
   the reader's ``_drop_cut_number``); a top-level number of a stream / single value is complete.
4. Kind ``stream`` follows the strict ``MODE_STREAM`` tokenization at the top level (``01`` is the two values
   ``0`` and ``1`` there, like the reader shows them); inside containers and on JSON Lines lines ``01`` is an
   invalid number. Top-level commas between stream values are kept as separators.
5. Late discovery (D9): an automatically detected one-value document followed by another value restarts the
   repair once as ``jsonl`` (the root value is one line and a line break follows, the reader's
   ``format_hint`` rule) or ``stream`` (``repair.warn.restarted``); a forced kind stops with
   ``extra_value``. A root followed by a comma and another value is a stream.
6. Detection (D10): ``reader.detect_format`` with the forced options. When the head of the file is not
   strictly valid (comments, single quotes, ...) the detection runs again on the head as cleaned by the
   tokenizer (every complete value kept, open containers closed) and that result is used -- a commented
   array is an ``array``, a JSON5 file of lines ``jsonl``, a Python ``repr`` dump with a ``data`` list
   ``wrapped``. A forced format that the raw head contradicts (``--format array`` on a file starting with a
   comment) is checked against the cleaned head.
7. Output for kind ``stream`` with ``output_kind="jsonl"``: one value per line, line breaks inside a value
   written as spaces (same length, so ``out_offset`` stays exact); the gaps between values are replaced by
   the line feed (comments there reported as removed).
8. A trailing comma before EOF inside a document is part of the cut (no ``trailing_comma`` entry); NUL
   padding at the end (a preallocated file) is ``trailing_data`` and the data before it is treated as the
   end of the file. A BOM is removed wherever whitespace may stand (not only at the start / between values).
9. JSON Lines: several values on one line are split into lines (``line_split``, a code added to the §3.1
   catalogue: dropping the extra values would lose data); a line with only whitespace or comments stays
   (blank); invalid UTF-8 counts towards the guard across lines.
10. ``check_only=True`` never raises :class:`UnrepairableError`: the returned report has ``problem`` set
    (nothing is ever written). ``RepairReport.fixes_file`` is ``None`` when every fix fits in ``fixes``;
    :meth:`RepairReport.close` removes the temporary file (stale ones older than a day are removed by the
    next repair).
11. Fix round R5 (review "files"): :meth:`RepairReport.write` refuses the source (any spelling, a hard link:
    ``writer.error.same_file``), the prepared copy it was read through (:attr:`RepairReport.data_path`, gzip /
    UTF-16 sources) and the repaired output (``repair.error.report_target``): the report never replaces the
    data it describes (R5F-1). The partial result before a problem is ONE ``dropped_tail`` fix -- offset /
    line / column of the cut (the end of the last complete record), ``length`` = every byte from there to
    the end of the file (NUL padding included), ``after`` = the closers, ``reason`` = the problem code -- so
    ``total``, :meth:`RepairReport.data_changes_line`, :meth:`RepairReport.summary_lines` and the saved
    report show it (R5F-6). How many complete records follow the problem is not known: nothing after an
    unrepairable place is interpreted.

Speed and limits
================
Measured on 20 MB record arrays (Rust core, warm, without the fsync of the atomic write; Core Ultra 9):
clean ~280 MB/s, 1 % broken records ~180 MB/s, 10 % ~47 MB/s, ``NaN`` in every record ~17 MB/s, JSON5
style (comments, a trailing comma in every record) ~35 MB/s, every string single-quoted (a Python ``repr``
dump: 80 000 fixes per MB) ~1.5-2 MB/s; the pure tokenizer on clean data ~70 MB/s. End to end in a fresh
process (detection, regex compilation, fsync): 2 GB with ``NaN`` in 1 % of the records 156 MB/s with a
peak of 49 MB. A fresh process spends 0.2-0.5 s compiling the fast-path regexes once. Dense errors run at
the reference tokenizer's Python speed: every fix carries its exact location, previews and commit
bookkeeping. A single string or comment token is never held in memory, but one *gap* between tokens is
held up to :data:`HOLD_MAX`. The optional core addition ``Scanner.error_context`` (H1, ~5x on dense errors)
and a Rust tokenizer are not part of stage 5.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
import zlib
from array import array
from dataclasses import dataclass, field
from json.encoder import encode_basestring as _jstr
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Union

from . import core
from .core import _py_patterns as _P
from .core import MODE_BRACE_LIST, MODE_CONTAINER, MODE_LINES, MODE_SINGLE, MODE_STREAM
from .errors import FileChangedError, FileFormatError, JsonReshapeError, UserInputError
from .fileio import (
    appended_bytes,
    atomic_write,
    long_path,
    normalize_path,
    open_shared,
    prepare_data_file,
    read_range,
    release_prepared,
    retain_prepared,
)
from .i18n import t
from .settings import Settings, get_settings
from .tasks import CancelToken, Progress, check, format_bytes, format_duration

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]

__all__ = [
    "FIX_CATEGORY",
    "FIX_CODES",
    "PROBLEM_CODES",
    "RepairFix",
    "RepairOptions",
    "RepairProblem",
    "RepairReport",
    "Truncation",
    "UnrepairableError",
    "repair_file",
    "set_scanner_backend",
]

KiB = 1 << 10
MiB = 1 << 20

#: fix code -> category (s5/map_repair_diff.md §3.1; ``line_split`` added, decision 9; ``dropped_tail`` decision 11)
FIX_CATEGORY: dict[str, str] = {
    "bom": "syntax",
    "comment": "syntax",
    "trailing_comma": "syntax",
    "missing_comma": "syntax",
    "single_quote": "syntax",
    "unquoted_key": "syntax",
    "nan": "data",
    "infinity": "data",
    "python_literal": "syntax",
    "undefined": "data",
    "control_char": "syntax",
    "invalid_escape": "data",
    "invalid_utf8": "data",
    "brace_list": "syntax",
    "concatenated": "syntax",
    "line_split": "syntax",
    "truncated": "dropped",
    "trailing_data": "dropped",
    "line_dropped": "dropped",
    "dropped_tail": "dropped",
}
FIX_CODES: tuple[str, ...] = tuple(FIX_CATEGORY)
CATEGORIES = ("syntax", "data", "dropped")
PROBLEM_CODES: tuple[str, ...] = (
    "unexpected_char", "mismatched_closer", "unquoted_value", "invalid_number", "depth_limit", "not_utf8",
    "nothing_complete", "extra_value", "disabled",
)

HOLD_MAX = 1 * MiB            # a gap between tokens is held at most this far for a pending comma decision
LOOKAHEAD_MAX = 64 * KiB      # brace-list root look-ahead: undecided after this many bytes -> an object
UTF8_LIMIT = 1000             # more invalid UTF-8 sequences than this: "probably not UTF-8" (D5)
MAX_DEPTH = core.DEFAULT_MAX_DEPTH
SHOW_CHARS = 40               # before/after texts of a fix
PREVIEW_BYTES = 4 * SHOW_CHARS + 8
CONTEXT_BYTES = 60            # problem context on each side (reader.CONTEXT_BYTES)
MAX_ITEMS = 10000             # fixes kept in RepairReport.fixes by default
WINDOW_START = 4 * KiB        # scanner window after a (re)start, doubled while clean ...
WINDOW_MAX = 8 * MiB          # ... up to this (Rust core)
WINDOW_MAX_PY = 1 * MiB       # ... (pure-Python scanner: cancel/progress stay responsive)
TOKEN_START = 4 * KiB         # first read of a tokenizer run behind the scanner's error, doubled ...
TOKEN_FEED = 256 * KiB        # ... up to this (bytes handed to the tokenizer at a time)
SLICE = 256 * KiB             # a fast-path regex match never looks further than this (GIL bound)
OUT_MEMORY = 8 * MiB          # uncommitted output kept in memory before it spills to a side file
FLUSH_BATCH = 1 * MiB         # committed output is handed to the sink in batches of about this size
DENSE_FAILS = 8               # scanner restarts failing within DENSE_NEAR bytes in a row -> dense mode
DENSE_NEAR = 4 * KiB
DENSE_SPAN = 1 * MiB          # dense mode: the tokenizer keeps running at least this far
STALE_FIXES_SECONDS = 86400.0
FIXES_PREFIX = "repair-fixes-"
QUEUE_BYTES = 16 * MiB        # output waiting for the validator thread at most
DEFER_MAX = 4096              # held-back fix rows kept in memory (more go to a temporary file)
REC_FAILS = 4                 # the record fast path missed this often in a row ...
REC_SKIP = 32                 # ... -> the next records go straight to the state machine
PARTS_JOIN = 1024             # held output pieces joined into one every this many writes

_BOM = b"\xef\xbb\xbf"
_WS_BYTES = b" \t\r\n"
_CONT_BYTES = bytes(range(0x80, 0xC0))
_NOT_ASTRAL_LEADS = bytes(b for b in range(256) if not 0xF0 <= b <= 0xF4)
_ALL_BUT_CRLF = bytes(b for b in range(256) if b not in (0x0A, 0x0D))
_NL_TO_SPACE = bytes.maketrans(b"\r\n", b"  ")
_REPLACEMENT = "�".encode("utf-8")

_ESCAPE_OF = {0x08: b"\\b", 0x09: b"\\t", 0x0A: b"\\n", 0x0C: b"\\f", 0x0D: b"\\r"}
_VALUE_START = frozenset(b'{["\'-+0123456789') | frozenset(range(0x41, 0x5B)) | frozenset(range(0x61, 0x7B)) \
    | frozenset(b"_$")
_KEY_START = frozenset(b"\"'_$") | frozenset(range(0x41, 0x5B)) | frozenset(range(0x61, 0x7B)) \
    | frozenset(range(0x80, 0x100))
_SELF_DELIM = frozenset(b"}]\"'")
_STRICT_START = frozenset(b'{["-0123456789tfn')   # "another value follows" (reader._VALUE_START / format_hint)
_NUMBER_CONT = frozenset(b"0123456789.eE+-_$") | frozenset(range(0x41, 0x5B)) | frozenset(range(0x61, 0x7B))
_WORD_CONT = frozenset(range(0x80, 0x100))
_KNOWN_WORDS = (b"true", b"false", b"null", b"NaN", b"Infinity", b"True", b"False", b"None", b"undefined")
_LITERALS = (b"true", b"false", b"null")

# ---- regexes (strict fast paths come from the core's expanded patterns, all string flags on) ----------
_FLAGS = 7
_K = _P.FAST_DEPTH
_VAR = _P.variant(_FLAGS, False)
_WS = _P.WS_ALL
_WS_MATCH = re.compile(_WS).match
_STRING = _VAR.string                       # one strict string token
_KEY_COLON = _VAR.key_colon                 # a strict string, whitespace and a colon
_STR_RUN = _VAR.str_run                     # a run of strict string content
_SQ_BODY = (rb"(?:[^'\"\\\x00-\x1f\x80-\xff]++|\\(?:[\"\\/bfnrt]|u[0-9a-fA-F]{4})|" + _P.UTF8_SEQ
            + rb")*+")
_SQ_RUN = re.compile(_SQ_BODY).match       # single-quoted string content needing no change
_SQ_SIMPLE = re.compile(rb"'" + _SQ_BODY + rb"'").match   # a whole single-quoted string with only that
_NUMBER = re.compile(_P.NUMBER).match
_WORD = re.compile(rb"[A-Za-z_$][A-Za-z0-9_$]*+").match
_IDENT = re.compile(rb"[A-Za-z_$\x80-\xff][A-Za-z0-9_$\x80-\xff]*+").match
_HEX4 = re.compile(rb"[0-9a-fA-F]{4}").fullmatch
_HEX2 = re.compile(rb"[0-9a-fA-F]{2}").fullmatch
# fast paths: the core's expanded patterns (``_VAR.get(name, k)``: value, arr_skip, obj_skip, elem_arr,
# elem_brace, member), compiled once per process and shared with the pure-Python scanner


# ----------------------------------------------------------------------------------------------------
# scanner backend
# ----------------------------------------------------------------------------------------------------
_scanner_backend: str | None = None


def set_scanner_backend(name: str | None) -> str | None:
    """Use the core backend ``name`` (``"rust"`` / ``"python"``) for the accelerator and the output check
    (``None`` = the active ``core.BACKEND``, the default). Returns the previous choice. A backend chosen here
    is used even when it is the pure-Python one (tests); by default the pure-Python core skips the
    accelerator (the tokenizer is faster than the Python scanner)."""
    global _scanner_backend
    if name is not None:
        core.get_backend(name)
    old = _scanner_backend
    _scanner_backend = name
    return old


def _scanner() -> tuple[str, Callable[..., Any], bool]:
    """``(backend name, Scanner class, explicitly chosen)``."""
    name = _scanner_backend
    if name is None:
        return core.BACKEND, core.Scanner, False
    return name, core.get_backend(name).Scanner, True


# ----------------------------------------------------------------------------------------------------
# public types
# ----------------------------------------------------------------------------------------------------
@dataclass
class RepairOptions:
    """What :func:`repair_file` may do (s5/map_repair_diff.md §3.5).

    * ``nan``: ``"null"`` (default) or ``"string"`` (``NaN`` -> ``"NaN"``, ``-Infinity`` -> ``"-Infinity"``);
    * ``fixes``: ``None`` = every code of :data:`FIX_CODES`; else only these (a needed fix that is not
      allowed stops the repair: problem ``disabled``; ``dropped_tail`` follows ``allow_partial`` alone);
    * ``format`` / ``record_path``: forced input kind (``reader.KINDS``) / record path (wrapped);
    * ``output_kind``: ``None`` = the input kind (``stream`` / ``brace_list`` -> ``array``) or ``"jsonl"``
      (for ``stream`` and ``jsonl`` inputs);
    * ``allow_partial``: write the records before an unrepairable problem (closed like a truncation);
    * ``max_items``: fixes kept in ``RepairReport.fixes`` (all are counted and listed in ``fixes_file``);
    * ``accelerate``: use the strict core scanner for the clean stretches (same result either way);
    * ``window``: fixed read window in bytes (``None`` = adaptive; tests use 1, 7, 4096, 8 MiB).
    """

    nan: str = "null"
    fixes: frozenset[str] | None = None
    format: str | None = None
    record_path: Any = None
    output_kind: str | None = None
    allow_partial: bool = False
    max_items: int = MAX_ITEMS
    accelerate: bool = True
    window: int | None = None


@dataclass(frozen=True)
class RepairFix:
    """One change made by the repair.

    ``offset``: byte offset in the user's file (module docstring "Locations"); ``line`` / ``column``:
    1-based (column in characters); ``out_offset``: byte offset in the repaired output; ``record``: 0-based
    output record holding the fix, ``None`` outside records; ``before`` / ``after``: what was there / what
    is there now (at most 40 characters each, control characters escaped, ``""`` = nothing); ``length`` /
    ``out_length``: bytes replaced in the data file / written to the output; ``reason``: ``line_dropped``
    only -- the problem code that made the line unrepairable (``truncated`` for a cut-off line).
    """

    code: str
    category: str
    offset: int
    line: int
    column: int
    out_offset: int
    record: int | None
    before: str
    after: str
    length: int = 0
    out_length: int = 0
    reason: str = ""

    @property
    def name(self) -> str:
        """Localized name of the fix."""
        return fix_name(self.code, self.after)

    @property
    def category_name(self) -> str:
        return t(f"repair.category.{self.category}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "category": self.category, "offset": self.offset, "line": self.line,
            "column": self.column, "out_offset": self.out_offset, "record": self.record, "before": self.before,
            "after": self.after, "length": self.length, "out_length": self.out_length, "reason": self.reason,
        }

    def to_row(self) -> list[Any]:
        return [self.code, self.category, self.offset, self.line, self.column, self.out_offset, self.record,
                self.before, self.after, self.length, self.out_length, self.reason]

    @classmethod
    def from_row(cls, row: list[Any]) -> RepairFix:
        return cls(str(row[0]), str(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5]),
                   None if row[6] is None else int(row[6]), str(row[7]), str(row[8]), int(row[9]), int(row[10]),
                   str(row[11]))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepairFix:
        return cls(str(data["code"]), str(data["category"]), int(data["offset"]), int(data["line"]),
                   int(data["column"]), int(data["out_offset"]),
                   None if data.get("record") is None else int(data["record"]), str(data.get("before", "")),
                   str(data.get("after", "")), int(data.get("length", 0)), int(data.get("out_length", 0)),
                   str(data.get("reason", "")))


def fix_name(code: str, after: str = "") -> str:
    """Localized name of a fix code (``nan`` / ``infinity`` name the replacement, ``concatenated`` the
    output kind)."""
    if code in ("nan", "infinity"):
        return t(f"repair.fix.{code}", to=after or "null")
    if code == "concatenated" and after == "JSONL":
        return t("repair.fix.concatenated_jsonl")
    return t(f"repair.fix.{code}")


@dataclass(frozen=True)
class RepairProblem:
    """The unrepairable place that stopped the repair (location as :class:`RepairFix`; context = up to 60
    bytes of text on each side, like ``JsonSyntaxError``). ``expected`` (``json.expected.<code>``) for
    ``unexpected_char``; ``detail``: the fix code for ``disabled``."""

    code: str
    offset: int
    line: int
    column: int
    context_before: str
    context_after: str
    expected: str = ""
    detail: str = ""

    @property
    def reason(self) -> str:
        """Localized description."""
        if self.code == "disabled":
            text = t("repair.problem.disabled", fix=fix_name(self.detail))
        elif self.code == "not_utf8":
            text = t("repair.problem.not_utf8", count=UTF8_LIMIT)
        else:
            text = t(f"repair.problem.{self.code}")
        if self.expected and self.expected != "none":
            exp = t(f"json.expected.{self.expected}")
            if exp:
                text = f"{text}; {exp}"
        return text

    @property
    def message(self) -> str:
        return t("repair.problem.at", reason=self.reason, line=self.line, column=self.column, offset=self.offset)

    def context(self) -> str:
        """Two lines: the text around the place and a caret under it (as ``JsonSyntaxError.context``)."""
        before = self.context_before.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        after = self.context_after.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        return f"{before}{after}\n{' ' * len(before)}^"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "offset": self.offset, "line": self.line, "column": self.column,
                "context_before": self.context_before, "context_after": self.context_after,
                "expected": self.expected, "detail": self.detail, "message": self.reason}


@dataclass(frozen=True)
class Truncation:
    """Where a cut-off document (or the partial result before a problem) was closed: ``offset`` / ``line``
    / ``column`` of the cut (the end of the last complete record, user-file offset), ``kept_records``,
    ``dropped_bytes`` (data after the cut), ``partial_start`` / ``partial_line`` (start of the incomplete
    record, ``None`` when the data ended between records) and the ``closers`` appended (``"]}"``)."""

    offset: int
    line: int
    column: int
    kept_records: int
    dropped_bytes: int
    partial_start: int | None
    closers: str
    partial_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"offset": self.offset, "line": self.line, "column": self.column, "kept_records": self.kept_records,
                "dropped_bytes": self.dropped_bytes, "partial_start": self.partial_start,
                "partial_line": self.partial_line, "closers": self.closers}


_FIELDS = ("code", "name", "category", "offset", "line", "column", "out_offset", "record", "before", "after",
           "length", "out_length", "reason")


@dataclass
class RepairReport:
    """Everything a repair did (always produced, AGENT_PROMPT §10 "Har doim hisobot chiqadi").

    ``kind`` = the input kind used (detected or forced), ``output_kind`` the kind written, ``encoding`` /
    ``compression`` of the source; ``bytes_in`` = size of the source file, ``bytes_out`` = bytes of the
    repaired JSON text (uncompressed); ``records`` = records of the output (``reader`` counting);
    ``counts`` per fix code (every fix), ``fixes`` the first ``max_items`` in file order, ``fixes_file`` a
    temporary JSON Lines file with every fix when there are more (else ``None``), ``first`` the first fix
    of every code; ``truncated`` / ``problem``; ``written`` (the target was replaced); ``warnings`` i18n
    keys with ``warning_params``; ``data_path`` the prepared UTF-8 copy the repair read (gzip / UTF-16
    sources; ``None`` when it read the source itself).
    """

    source: Path
    target: Path | None
    kind: str
    output_kind: str
    encoding: str
    compression: str | None
    bytes_in: int = 0
    bytes_out: int = 0
    records: int = 0
    elapsed: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    fixes: list[RepairFix] = field(default_factory=list)
    fixes_file: Path | None = None
    truncated: Truncation | None = None
    problem: RepairProblem | None = None
    written: bool = False
    warnings: list[str] = field(default_factory=list)
    warning_params: list[dict[str, Any]] = field(default_factory=list)
    first: dict[str, RepairFix] = field(default_factory=dict)
    record_path: tuple[str | int, ...] | None = None
    check_only: bool = False
    backend: str = ""
    accelerated: bool = False
    data_path: Path | None = None

    # -- summary ------------------------------------------------------------------------------------
    @property
    def total(self) -> int:
        """Number of fixes (all, not only the listed ones)."""
        return sum(self.counts.values())

    def changed_data(self) -> bool:
        """A fix of category ``data`` or ``dropped`` happened (values changed or data removed), or a
        problem stopped the repair."""
        return self.problem is not None or any(
            n and FIX_CATEGORY.get(code) in ("data", "dropped") for code, n in self.counts.items())

    def warning_texts(self) -> list[str]:
        out = []
        for i, key in enumerate(self.warnings):
            params = self.warning_params[i] if i < len(self.warning_params) else {}
            out.append(t(key, **params))
        return out

    def add_warning(self, key: str, **params: Any) -> None:
        if key not in self.warnings:
            self.warnings.append(key)
            self.warning_params.append(params)

    def reopen_options(self) -> dict[str, Any]:
        """Options to open the repaired file with (``Document.open(target, **options)``): its kind, and
        the record path of a wrapped document."""
        opts: dict[str, Any] = {"format": self.output_kind}
        if self.output_kind == "wrapped" and self.record_path is not None:
            opts["record_path"] = self.record_path
        return opts

    def summary_lines(self) -> list[str]:
        """Localized summary: one line per fix code (count and the first location), the truncation and
        the problem (the CLI and the GUI show these)."""
        lines = []
        for code in sorted(self.counts, key=lambda c: (self.first[c].offset if c in self.first else 0,
                                                       FIX_CODES.index(c) if c in FIX_CODES else 99)):
            n = self.counts[code]
            if not n:
                continue
            first = self.first.get(code)
            if first is not None:
                lines.append(t("repair.summary.fix", name=fix_name(code, first.after), count=n, line=first.line,
                               column=first.column))
            else:  # pragma: no cover - every counted code has a first fix
                lines.append(t("repair.summary.fix_count", name=fix_name(code), count=n))
        tr = self.truncated
        if tr is not None and self.problem is None:
            lines.append(t("repair.summary.truncated", line=tr.line, column=tr.column, kept=tr.kept_records,
                           dropped=format_bytes(tr.dropped_bytes), closers=tr.closers or "-"))
        if self.problem is not None:
            p = self.problem
            lines.append(t("repair.summary.problem", reason=p.reason, line=p.line, column=p.column))
            if tr is not None:
                lines.append(t("repair.summary.partial_written" if self.written else "repair.summary.partial",
                               kept=tr.kept_records))
        return lines

    def headline(self) -> str:
        """One localized line: what was repaired/checked, sizes, records and time."""
        size = format_bytes(self.bytes_out if self.written else self.bytes_in)
        secs = format_duration(self.elapsed)
        if self.check_only:
            return t("repair.summary.checked", source=self.source.name, size=size, records=self.records,
                     seconds=secs)
        if self.written:
            return t("repair.summary.written", source=self.source.name,
                     target=self.target.name if self.target else "", size=size, records=self.records, seconds=secs)
        return t("repair.summary.not_written", source=self.source.name)

    def data_changes_line(self) -> str | None:
        """Localized line naming the fixes that changed or dropped data (``None`` when there are none)."""
        items = [t("repair.summary.data_item", name=fix_name(code, self.first[code].after if code in self.first else ""),
                   count=n)
                 for code, n in self.counts.items() if n and FIX_CATEGORY.get(code) in ("data", "dropped")]
        if not items:
            return None
        return t("repair.summary.data_changed", items=", ".join(items))

    # -- serialization --------------------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible dict (CLI ``--json``): counts, truncation, problem and the listed fixes."""
        return {
            "source": str(self.source),
            "target": None if self.target is None else str(self.target),
            "kind": self.kind,
            "output_kind": self.output_kind,
            "record_path": None if self.record_path is None else list(self.record_path),
            "encoding": self.encoding,
            "compression": self.compression,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "records": self.records,
            "elapsed": round(self.elapsed, 6),
            "check_only": self.check_only,
            "written": self.written,
            "total": self.total,
            "changed_data": self.changed_data(),
            "counts": dict(self.counts),
            "truncated": None if self.truncated is None else self.truncated.to_dict(),
            "problem": None if self.problem is None else self.problem.to_dict(),
            "warnings": self.warning_texts(),
            "fixes": [f.to_dict() for f in self.fixes],
            "fixes_listed": len(self.fixes),
        }

    def iter_fixes(self) -> Iterator[RepairFix]:
        """Every fix in file order (from ``fixes_file`` when the list holds only the first ones)."""
        path = self.fixes_file
        if path is None or not os.path.exists(long_path(path)):
            yield from self.fixes
            return
        with open(long_path(path), "rb") as f:
            for raw in f:
                if raw.strip():
                    yield RepairFix.from_row(json.loads(raw))

    def check_report_path(self, path: StrPath) -> None:
        """Refuse ``path`` as the report file when it is the repaired source -- under any spelling, a hard
        link, or the prepared copy the repair read (``writer.error.same_file``) -- or the repaired output
        (``repair.error.report_target``): the report never replaces the data it describes (fix round R5,
        R5F-1; the CLI ``--report`` and the GUI "save report" pass any name the user typed)."""
        out = Path(os.path.abspath(os.fspath(path)))
        for p in (self.source, self.data_path):
            if p is not None and _same_file(out, p):
                raise UserInputError("writer.error.same_file", path=os.fspath(path))
        if self.target is not None and _same_file(out, self.target):
            raise UserInputError("repair.error.report_target", path=os.fspath(path))

    def write(self, path: StrPath, fmt: str = "csv") -> None:
        """Write every fix to ``path`` (atomically): ``csv`` (UTF-8 with BOM, a localized ``name`` column),
        ``json`` (an array of fix objects) or ``jsonl`` (one object per line). ``path`` must not be the source,
        its prepared copy or the repaired output (:meth:`check_report_path`)."""
        fmt = (fmt or "csv").lower().lstrip(".")
        if fmt not in ("csv", "json", "jsonl"):
            raise UserInputError("repair.error.option", name="format", value=fmt)
        self.check_report_path(path)
        with atomic_write(path) as f:
            if fmt == "csv":
                text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
                w = csv.writer(text)
                w.writerow(_FIELDS)
                for fx in self.iter_fixes():
                    d = fx.to_dict()
                    w.writerow([d["code"], fx.name] + [("" if d[k] is None else d[k]) for k in _FIELDS[2:]])
                text.flush()
                text.detach()
            elif fmt == "json":
                f.write(b"[")
                first = True
                for fx in self.iter_fixes():
                    if not first:
                        f.write(b",")
                    first = False
                    d = fx.to_dict()
                    d["name"] = fx.name
                    f.write(b"\n" + json.dumps(d, ensure_ascii=False).encode("utf-8"))
                f.write(b"\n]\n" if not first else b"]\n")
            else:
                for fx in self.iter_fixes():
                    d = fx.to_dict()
                    d["name"] = fx.name
                    f.write(json.dumps(d, ensure_ascii=False).encode("utf-8") + b"\n")

    def close(self) -> None:
        """Remove the temporary fix list (``fixes_file``); :meth:`iter_fixes` then yields ``fixes`` only."""
        path = self.fixes_file
        self.fixes_file = None
        if path is not None:
            try:
                os.remove(long_path(path))
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("could not remove %s", path, exc_info=True)

    def __enter__(self) -> RepairReport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class UnrepairableError(JsonReshapeError):
    """The file has a problem the repair does not guess (``report.problem``); nothing was written
    (``RepairOptions.allow_partial`` writes the records before it instead)."""

    key = "repair.error.unrepairable"

    def __init__(self, report: RepairReport) -> None:
        p = report.problem
        params: dict[str, Any] = {}
        if p is not None:
            params = {"reason": p.reason, "line": p.line, "column": p.column, "offset": p.offset}
        super().__init__(None, **params)
        self.report = report

    @property
    def message(self) -> str:
        p = self.report.problem
        if p is None:  # pragma: no cover - always set
            return t(self.key)
        return t(self.key, reason=p.reason, line=p.line, column=p.column, offset=p.offset)


# ----------------------------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------------------------
def _char_count(raw: bytes) -> int:
    """Characters of UTF-8 ``raw`` the way the reader counts columns (non-continuation bytes)."""
    return len(raw) if raw.isascii() else len(raw.translate(None, _CONT_BYTES))


def _decode_prefix(raw: bytes, final: bool) -> str:
    return codecs.getincrementaldecoder("utf-8")("replace").decode(raw, final)


def _decode_suffix(raw: bytes) -> str:
    i = 0
    while i < len(raw) and i < 3 and 0x80 <= raw[i] < 0xC0:
        i += 1
    return raw[i:].decode("utf-8", "replace")


_PLAIN_ASCII = re.compile(rb"[\x20-\x7e]*").fullmatch


_SHOW_SPECIAL = re.compile("[\x00-\x1f\x7f﻿  ]")
_SHOW_ESCAPE = {chr(i): f"\\u{i:04x}" for i in (*range(0x20), 0x7F, 0xFEFF, 0x2028, 0x2029)}
_SHOW_ESCAPE.update({"\n": "\\n", "\r": "\\r", "\t": "\\t"})


def _show_text(raw: bytes | str) -> str:
    """Display text of a piece of data, not cut: invalid UTF-8 as ``\\xHH``, control characters, the BOM and
    the line / paragraph separators escaped."""
    if type(raw) is bytes and _PLAIN_ASCII(raw):
        return raw.decode("ascii")
    if isinstance(raw, (bytes, bytearray, memoryview)):
        text = bytes(raw).decode("utf-8", "backslashreplace")
    else:
        text = raw
    if _SHOW_SPECIAL.search(text) is None:
        return text
    return _SHOW_SPECIAL.sub(lambda m: _SHOW_ESCAPE[m.group()], text)


def _cut_text(text: str, limit: int = SHOW_CHARS) -> str:
    """At most ``limit`` characters (an ellipsis marks a cut)."""
    return text if len(text) <= limit else text[:limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def _show(raw: bytes | str, limit: int = SHOW_CHARS) -> str:
    """Display text of a piece of data (:func:`_show_text`), at most ``limit`` characters. Only the first
    ``limit + 1`` characters can matter, so at most that many are looked at (a huge comment's preview)."""
    if type(raw) is bytes and len(raw) <= limit and _PLAIN_ASCII(raw):
        return raw.decode("ascii")
    if isinstance(raw, str):
        raw = raw[:limit + 1]
    else:   # a character takes at most 4 bytes; a cut sequence at the end lies beyond the shown part
        raw = bytes(raw[:4 * limit + 8])
    return _cut_text(_show_text(raw), limit)


def _decode_key(raw: bytes) -> str | None:
    """Decoded text of the content of a (repaired, valid) JSON string; ``None`` when undecodable."""
    try:
        if b"\\" not in raw:
            return raw.decode("utf-8")
        return json.loads(b'"' + raw + b'"')
    except (UnicodeDecodeError, ValueError):
        return None


def _number_ok(buf: bytes, e: int, end: int) -> bool:
    """A fast match ending at ``e`` is final unless it is a number that the next byte could still continue
    (the window / slice end, or ``.`` / ``e`` / ``E`` after it): the core's rule."""
    c = buf[e - 1]
    if 0x30 <= c <= 0x39:
        if e >= end:
            return False
        n = buf[e]
        return n != 0x2E and n != 0x65 and n != 0x45
    return True


def _norm_path(record_path: Any) -> tuple[str | int, ...] | None:
    if record_path is None:
        return None
    return tuple(record_path)


# ----------------------------------------------------------------------------------------------------
# locations
# ----------------------------------------------------------------------------------------------------
class _Lines:
    """Line / column / user offset of data-file offsets, the way ``reader.Document._make_error`` and
    ``_source_offset`` compute them; offsets are located in increasing order (``state`` is an immutable
    tuple, so a commit point snapshots it by reference and a rollback restores it).

    ``state`` = ``(off, line, line_start, chars, units)``: the bytes before ``off`` are counted; ``line`` =
    line feeds before ``off``; ``line_start`` = the offset after the last of them (``data_start`` on the
    first line); ``chars`` = characters in ``[line_start, off)``; ``units`` = UTF-16 code units in
    ``[data_start, off)`` (UTF-16 sources only)."""

    def __init__(self, read: Callable[[int, int], bytes], data_start: int, *, utf16: bool, gzip: bool,
                 source_bom: int) -> None:
        self._read = read
        self.data_start = data_start
        self.utf16 = utf16
        self.converted = utf16 or gzip
        self.source_bom = source_bom
        self.state: tuple[int, int, int, int, int] = (data_start, 0, data_start, 0, 0)

    def _advance(self, state: tuple[int, int, int, int, int], data: bytes) -> tuple[int, int, int, int, int]:
        off, line, ls, chars, units = state
        nl = data.count(b"\n")
        if nl:
            j = data.rfind(b"\n")
            line += nl
            ls = off + j + 1
            chars = _char_count(data[j + 1:])
        else:
            chars += _char_count(data)
        if self.utf16:
            units += len(data.translate(None, _CONT_BYTES)) + len(data.translate(None, _NOT_ASTRAL_LEADS))
        return off + len(data), line, ls, chars, units

    def _walk(self, state: tuple[int, int, int, int, int], target: int) -> tuple[int, int, int, int, int]:
        off = state[0]
        while off < target:
            stop = min(target, off + MiB)
            state = self._advance(state, self._read(off, stop))
            off = stop
        return state

    def skip(self, a: int, data: bytes) -> None:
        """Hint: the bytes ``data`` at ``a`` were just copied (counting them here saves reading them again)."""
        state = self.state
        off = state[0]
        if off > a + len(data) or not data:
            return
        if off < a:
            state = self._walk(state, a)
            off = a
        self.state = self._advance(state, data[off - a:] if off > a else data)

    def locate(self, o: int) -> tuple[int, int, int]:
        """``(line, column, user offset)`` of the data offset ``o`` (1-based line and column)."""
        state = self.state
        off = state[0]
        if off <= o <= off + 4 * KiB and not self.utf16:   # the usual short step forward (same as _walk)
            data = self._read(off, o)
            nl = data.count(b"\n")
            if nl:
                j = data.rfind(b"\n")
                line = state[1] + nl
                chars = _char_count(data[j + 1:])
                state = (o, line, off + j + 1, chars, 0)
            else:
                line = state[1]
                chars = state[3] + _char_count(data)
                state = (o, line, state[2], chars, 0)
            self.state = state
            return line + 1, chars + 1, (o if not self.converted else self.source_bom + o - self.data_start)
        if o < self.data_start:   # inside the UTF-8 BOM: line 1, column 1
            return 1, 1, o
        if o < off:   # behind: start over (a rollback normally restores a snapshot instead)
            state = (self.data_start, 0, self.data_start, 0, 0)
        state = self._walk(state, o)
        self.state = state
        _off, line, _ls, chars, units = state
        return line + 1, chars + 1, self.user_offset(o, units)

    def user_offset(self, o: int, units: int | None = None) -> int:
        if not self.converted:
            return o
        if self.utf16:
            if units is None:
                start = self.state if self.state[0] <= o else (self.data_start, 0, self.data_start, 0, 0)
                units = self._walk(start, o)[4]
            return self.source_bom + 2 * units
        return self.source_bom + o - self.data_start


# ----------------------------------------------------------------------------------------------------
# output: commit / rollback, sink, validation
# ----------------------------------------------------------------------------------------------------
class _Validator:
    """A core ``Scanner`` in a helper thread checking the repaired UTF-8 stream (saver ``_Validator``
    pattern): ``finish()`` returns ``(error tuple | None, first broken line | None, record count)``.

    The queue is bounded by bytes (:data:`QUEUE_BYTES`): the sink hands over chunks of at least
    :data:`FLUSH_BATCH` bytes, so the Rust scanner checks them with the GIL released and the two threads
    do not hand the GIL back and forth for every small piece."""

    _END = object()

    def __init__(self, scanner_cls: Callable[..., Any], mode: int, target: list, cancel: CancelToken | None) -> None:
        self.mode = mode
        self.cancel = cancel
        self.scanner = scanner_cls(mode, list(target), 0, MAX_DEPTH)
        self.q: queue.Queue = queue.Queue()
        self.error: tuple | None = None
        self.broken: tuple | None = None
        self.exc: BaseException | None = None
        self.records = 0
        self._stop = False
        self._done = threading.Event()
        self._cv = threading.Condition()
        self._inflight = 0
        self._thread = threading.Thread(target=self._run, name="jsonreshape-repair-check", daemon=True)
        self._thread.start()

    def feed(self, chunk: Any) -> None:
        n = 0 if chunk is self._END else len(chunk)
        cv = self._cv
        with cv:
            while self._inflight and self._inflight + n > QUEUE_BYTES:
                if self._done.is_set():
                    return   # the check already failed (or was aborted): finish() tells
                check(self.cancel)
                cv.wait(0.05)
            if self._done.is_set():
                return
            self._inflight += n
        self.q.put(chunk)

    def finish(self) -> tuple[tuple | None, tuple | None, int]:
        if not self._done.is_set():
            self.feed(self._END)
        while not self._done.wait(0.05):
            check(self.cancel)
        self._thread.join()
        if self.exc is not None:
            raise self.exc
        return self.error, self.broken, self.records

    def abort(self) -> None:
        self._stop = True
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.q.put(self._END)
        with self._cv:
            self._inflight = 0
            self._cv.notify_all()
        self._thread.join(10)

    def _done_with(self, n: int) -> None:
        cv = self._cv
        with cv:
            self._inflight -= n
            cv.notify_all()

    def _run(self) -> None:
        sc = self.scanner
        lines = self.mode == MODE_LINES
        try:
            while True:
                item = self.q.get()
                if item is self._END or self._stop:
                    break
                sc.feed(item)
                self._done_with(len(item))
                if lines:
                    broken = sc.take_broken()
                    if broken:
                        self.broken = tuple(broken[0])
                        return
                if sc.error is not None:
                    self.error = tuple(sc.error)
                    return
            if self._stop:
                return
            sc.finish()
            if lines:
                broken = sc.take_broken()
                if broken:
                    self.broken = tuple(broken[0])
                    return
            if sc.error is not None:
                self.error = tuple(sc.error)
                return
            self.records = int(sc.record_count)
        except BaseException as exc:  # noqa: BLE001 - handed to the repairing thread
            log.exception("repair check failed")
            self.exc = exc
        finally:
            self._done.set()
            with self._cv:
                self._cv.notify_all()


class _Sink:
    """Final (committed) output: the validator, gzip and the temp target file (``None`` when checking).
    Small pieces are gathered into chunks of at least :data:`FLUSH_BATCH` bytes first."""

    def __init__(self, f: BinaryIO | None, gz: bool, validator: _Validator | None) -> None:
        self.f = f
        self.z = zlib.compressobj(6, zlib.DEFLATED, 31) if (gz and f is not None) else None
        self.validator = validator
        self.n = 0
        self.parts: list[bytes] = []
        self.held = 0

    def write(self, data: bytes) -> None:
        if not data:
            return
        n = len(data)
        self.n += n
        if not self.held and n >= FLUSH_BATCH:
            self._emit(data)
            return
        self.parts.append(data)
        self.held += n
        if self.held >= FLUSH_BATCH:
            self._drain()

    def _drain(self) -> None:
        parts = self.parts
        if parts:
            data = parts[0] if len(parts) == 1 else b"".join(parts)
            self.parts = []
            self.held = 0
            self._emit(data)

    def _emit(self, data: bytes) -> None:
        if self.validator is not None:
            self.validator.feed(data)
        f = self.f
        if f is not None:
            z = self.z
            if z is not None:
                data = z.compress(data)
                if data:
                    f.write(data)
            else:
                f.write(data)

    def finish(self) -> None:
        self._drain()
        if self.z is not None and self.f is not None:
            self.f.write(self.z.flush())
            self.z = None


class _Output:
    """The repaired byte stream with commit points (module docstring "Commit model").

    ``pos`` = bytes written so far; ``committed`` = bytes before it are final; ``sent`` = bytes handed to the
    sink. Bytes ``[sent, pos)`` are held: first in a side file (``spill``, from ``spill_off``) when they did
    not fit in memory, then in ``parts``. ``truncate_to`` / ``patch`` / ``peek`` work on held bytes only.
    ``nl_space``: line breaks written with :meth:`write` become spaces (stream -> JSON Lines output)."""

    def __init__(self, sink: _Sink, spill_dir: Callable[[], Path]) -> None:
        self.sink = sink
        self._spill_dir = spill_dir
        self.parts: list[bytes] = []
        self.joined = 0               # parts[:joined] are already joined pieces (_join_tail)
        self.held = 0                 # bytes in parts
        self.pos = 0
        self.sent = 0
        self.committed = 0
        self.spill: BinaryIO | None = None
        self.spill_path: Path | None = None
        self.spill_off = 0            # bytes of the spill file already sent
        self.spill_len = 0            # bytes in the spill file
        self.nl_space = False

    # -- writing ------------------------------------------------------------------------------------
    def write(self, data: bytes) -> None:
        if data:
            if self.nl_space:
                data = data.translate(_NL_TO_SPACE)
            parts = self.parts
            parts.append(data)
            n = len(data)
            self.held += n
            self.pos += n
            if self.held > OUT_MEMORY:
                self._relieve()
            elif len(parts) - self.joined >= PARTS_JOIN:
                self._join_tail()

    def write_raw(self, data: bytes) -> None:
        if data:
            self.parts.append(data)
            self.held += len(data)
            self.pos += len(data)
            if len(self.parts) - self.joined >= PARTS_JOIN:
                self._join_tail()

    def _join_tail(self) -> None:
        """Many small pieces (a record with millions of fixes) cost far more memory than their bytes: the
        recent ones become one piece (bounded work per call)."""
        parts = self.parts
        k = self.joined
        parts[k:] = [b"".join(parts[k:])]
        self.joined = len(parts)

    def write_final(self, data: bytes) -> None:
        """Bytes that are final at once (verbatim copies of validated data, everything before them being
        committed): straight to the sink, never held."""
        if not data:
            return
        if self.nl_space:
            data = data.translate(_NL_TO_SPACE)
        if self.committed < self.pos:  # pragma: no cover - copies only follow committed output
            self.write(data)
            self.commit()
            return
        if self.sent < self.pos:
            self._send(self.pos)
        self.sink.write(data)
        n = len(data)
        self.pos += n
        self.sent += n
        self.committed = self.pos

    def commit(self, upto: int | None = None) -> None:
        """Bytes before ``upto`` (default: everything) are final; batches of them go to the sink."""
        upto = self.pos if upto is None else upto
        if upto > self.committed:
            self.committed = upto
        if self.committed - self.sent >= FLUSH_BATCH:
            self._send(self.committed)

    def flush(self) -> None:
        """Send every committed byte to the sink (end of the pass)."""
        self._send(self.committed)

    # -- held bytes ------------------------------------------------------------------------------------
    def _parts_start(self) -> int:
        return self.sent + (self.spill_len - self.spill_off)

    def _send(self, upto: int) -> None:
        upto = min(upto, self.pos)
        sink = self.sink
        while self.sent < upto and self.spill_len > self.spill_off:
            k = min(upto - self.sent, self.spill_len - self.spill_off, 8 * MiB)
            assert self.spill is not None
            self.spill.seek(self.spill_off)
            data = self.spill.read(k)
            sink.write(data)
            self.spill_off += len(data)
            self.sent += len(data)
            if self.spill_off >= self.spill_len:
                self.spill.seek(0)
                self.spill.truncate()
                self.spill_off = self.spill_len = 0
        if self.sent >= upto:
            return
        k = upto - self.sent
        data = b"".join(self.parts)
        if k >= len(data):
            self.parts = []
            self.held = 0
        else:
            self.parts = [data[k:]]
            self.held = len(data) - k
            data = data[:k]
        self.joined = len(self.parts)
        sink.write(data)
        self.sent += len(data)

    def _relieve(self) -> None:
        """Too much held in memory: send what is committed, move the rest to the side file."""
        if self.committed > self.sent:
            self._send(self.committed)
        if self.held <= OUT_MEMORY // 2:
            return
        if self.spill is None:
            path = self._spill_dir() / f"repair-spill-{os.getpid()}-{secrets.token_hex(4)}.tmp"
            self.spill = open(long_path(path), "w+b")  # noqa: SIM115 - closed by close()
            self.spill_path = path
        self.spill.seek(self.spill_len)
        for part in self.parts:
            self.spill.write(part)
        self.spill_len += self.held
        self.parts = []
        self.joined = 0
        self.held = 0

    def truncate_to(self, p: int) -> None:
        """Drop the bytes after ``p`` (``p >= sent``: only held bytes are dropped)."""
        if p >= self.pos:
            return
        if p < self.sent:  # pragma: no cover - a commit point never lies before sent bytes
            raise AssertionError("output rollback before the committed data")
        ps = self._parts_start()
        if p >= ps:
            data = b"".join(self.parts)[:p - ps]
            self.parts = [data] if data else []
            self.held = len(data)
        else:
            self.parts = []
            self.held = 0
            self.spill_len = self.spill_off + (p - self.sent)
            assert self.spill is not None
            self.spill.seek(self.spill_len)
            self.spill.truncate()
        self.joined = len(self.parts)
        self.pos = p
        if self.committed > p:
            self.committed = p

    def patch(self, p: int, data: bytes) -> None:
        """Overwrite held bytes at ``p`` (same length)."""
        ps = self._parts_start()
        if p >= ps:
            whole = bytearray(b"".join(self.parts))
            whole[p - ps:p - ps + len(data)] = data
            self.parts = [bytes(whole)]
            self.joined = 1
        else:
            assert self.spill is not None
            self.spill.seek(self.spill_off + (p - self.sent))
            self.spill.write(data)

    def peek(self, p: int, n: int) -> bytes:
        """Up to ``n`` held bytes from ``p``."""
        out = b""
        ps = self._parts_start()
        if p < ps and self.spill is not None:
            k = min(n, ps - p)
            self.spill.seek(self.spill_off + (p - self.sent))
            out = self.spill.read(k)
            p += len(out)
            n -= len(out)
        if n > 0 and p >= ps:
            parts = self.parts
            i = len(parts)
            pos = self.pos
            while i > 0 and pos > p:   # from the end: ``p`` is usually recent
                i -= 1
                pos -= len(parts[i])
            off = p - pos
            chunks = []
            got = 0
            while i < len(parts) and got < n:
                piece = parts[i][off:off + n - got]
                chunks.append(piece)
                got += len(piece)
                off = 0
                i += 1
            out += b"".join(chunks)
        return out

    def close(self) -> None:
        spill, self.spill = self.spill, None
        if spill is not None:
            spill.close()
        path, self.spill_path = self.spill_path, None
        if path is not None:
            try:
                os.remove(long_path(path))
            except OSError:
                log.warning("could not remove %s", path, exc_info=True)


def _row_bytes(row: tuple) -> bytes:
    """One fix row (the tuple of :meth:`RepairFix.to_row`) as a JSON Lines line."""
    code, cat, offset, line, col, oo, rec, before, after, length, olen, reason = row
    return ('["%s","%s",%d,%d,%d,%d,%s,%s,%s,%d,%d,%s]\n' % (
        code, cat, offset, line, col, oo, "null" if rec is None else rec, _jstr(before), _jstr(after), length,
        olen, _jstr(reason))).encode("utf-8")


class _Deferred:
    """Fix rows held back until an earlier fix is logged (the fixes inside a single-quoted string wait for
    the string's own entry; the fixes after a comma whose fate is pending wait for that decision): in
    memory, beyond :data:`DEFER_MAX` rows in a temporary file (bounded memory for a huge string)."""

    __slots__ = ("rows", "f", "path", "n", "_directory", "_spills")

    def __init__(self, directory: Callable[[], Path] | None, spills: list | None = None) -> None:
        self.rows: list[tuple] = []
        self.f: BinaryIO | None = None
        self.path: Path | None = None
        self.n = 0
        self._directory = directory
        self._spills = spills     # registry of spilled buffers (closed with the job, e.g. on cancel)

    def __len__(self) -> int:
        return self.n

    def append(self, row: tuple) -> None:
        rows = self.rows
        rows.append(row)
        self.n += 1
        if len(rows) >= DEFER_MAX and self._directory is not None:
            f = self.f
            if f is None:
                self.path = self._directory() / f"repair-defer-{os.getpid()}-{secrets.token_hex(4)}.jsonl"
                f = self.f = open(long_path(self.path), "w+b")  # noqa: SIM115 - closed by close()
                if self._spills is not None:
                    self._spills.append(self)
            f.write(b"".join(_row_bytes(r) for r in rows))
            rows.clear()

    def __iter__(self) -> Iterator[tuple]:
        f = self.f
        if f is not None:
            f.flush()
            f.seek(0)
            for raw in f:
                if len(raw) > 2:
                    yield tuple(json.loads(raw))
        yield from self.rows

    def close(self) -> None:
        f, self.f = self.f, None
        if f is not None:
            f.close()
        path, self.path = self.path, None
        if path is not None:
            try:
                os.remove(long_path(path))
            except OSError:
                log.warning("could not remove %s", path, exc_info=True)
        self.rows = []
        self.n = 0


class _FixLog:
    """Every fix: appended to a temporary JSON Lines file at once (``fixes_file``), the first
    ``max_items`` also kept in memory, counted per code; ``state()`` / ``rollback()`` drop the fixes after
    a commit point."""

    def __init__(self, directory: Callable[[], Path], max_items: int) -> None:
        self._directory = directory
        self.max_items = max(0, int(max_items))
        self.f: BinaryIO | None = None
        self.path: Path | None = None
        self.nbytes = 0
        self.n = 0
        self.ram: list[RepairFix] = []
        self.counts: dict[str, int] = {}
        self.first: dict[str, tuple[int, RepairFix]] = {}

    def add(self, fx: RepairFix) -> None:
        self.add_row((fx.code, fx.category, fx.offset, fx.line, fx.column, fx.out_offset, fx.record, fx.before,
                      fx.after, fx.length, fx.out_length, fx.reason), fx)

    def add_row(self, row: tuple, fx: RepairFix | None = None) -> None:
        """Log a fix given as the tuple of :meth:`RepairFix.to_row` (the object is built only when kept)."""
        f = self.f
        if f is None:
            self.path = self._directory() / f"{FIXES_PREFIX}{os.getpid()}-{secrets.token_hex(6)}.jsonl"
            f = self.f = open(long_path(self.path), "w+b")  # noqa: SIM115 - closed by finish()/discard()
        code = row[0]
        data = _row_bytes(row)
        f.write(data)
        self.nbytes += len(data)
        counts = self.counts
        counts[code] = counts.get(code, 0) + 1
        keep = len(self.ram) < self.max_items
        if keep or code not in self.first:
            if fx is None:
                fx = RepairFix(*row)
            if keep:
                self.ram.append(fx)
            if code not in self.first:
                self.first[code] = (self.n, fx)
        self.n += 1

    def state(self) -> tuple[int, int, int]:
        return self.n, self.nbytes, len(self.ram)

    def rollback(self, state: tuple[int, int, int]) -> None:
        n0, b0, r0 = state
        if self.n <= n0:
            return
        assert self.f is not None
        self.f.flush()
        self.f.seek(b0)
        counts = self.counts
        for raw in self.f:   # one row at a time: a huge record may have logged millions of fixes
            if len(raw) > 2:
                code = raw[2:raw.index(b'"', 2)].decode("ascii")
                counts[code] -= 1
                if not counts[code]:
                    del counts[code]
        self.f.seek(b0)
        self.f.truncate()
        self.nbytes = b0
        del self.ram[r0:]
        for code, (i, _fx) in list(self.first.items()):
            if i >= n0:
                del self.first[code]
        self.n = n0

    def finish(self) -> Path | None:
        """Close the file; keep it only when it lists more fixes than ``ram`` (else remove it)."""
        f, self.f = self.f, None
        if f is not None:
            f.close()
        if self.path is None:
            return None
        if self.n <= len(self.ram):
            self.discard()
            return None
        return self.path

    def discard(self) -> None:
        f, self.f = self.f, None
        if f is not None:
            f.close()
        path, self.path = self.path, None
        if path is not None:
            try:
                os.remove(long_path(path))
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("could not remove %s", path, exc_info=True)


# ----------------------------------------------------------------------------------------------------
# the tolerant tokenizer (the reference semantics)
# ----------------------------------------------------------------------------------------------------
NEED, DONE, STOPPED, PROBLEM, RESTART = range(5)

# between tokens
S_ROOT, S_VALUE, S_VALUE_OR_END, S_KEY_OR_END, S_KEY, S_COLON, S_AFTER, S_END, S_TOP = range(9)
# inside a token (resumable at any byte)
S_STRING, S_LCOMMENT, S_BCOMMENT, S_SKIP = 10, 11, 12, 13

ARR, OBJ, BRACE = 1, 2, 3

_NOT_BOM = object()     # _bom(): not a byte-order mark
_NOMATCH = object()     # record loops: the first record did not match
_LINE_END = re.compile(rb"[\r\n]").search
_UTF8_ONE = re.compile(_P.UTF8_SEQ).match
_LA_DQ = re.compile(rb'"(?:[^"\\]|\\.)*+"', re.S).match
_LA_SQ = re.compile(rb"'(?:[^'\\]|\\.)*+'", re.S).match
_EXPECTED = {S_ROOT: "value", S_VALUE: "value", S_VALUE_OR_END: "value_or_end", S_KEY_OR_END: "key_or_end",
             S_KEY: "key", S_COLON: "colon", S_END: "end", S_TOP: "value"}


class _Problem(Exception):
    """An unrepairable place (data offset ``offset``)."""

    def __init__(self, code: str, offset: int, expected: str = "", detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.offset = offset
        self.expected = expected
        self.detail = detail


class _Restart(Exception):
    """Late discovery (decision 5): the document is really of kind ``kind``."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _Frame:
    """An open container: ``commit`` = its children's ends are commit points, ``records`` = its children
    are the records, ``sync`` = the accelerator may restart after each child, ``on_path`` = it lies on
    the record path (wrapped), ``idx`` / ``key_on`` = path tracking (element index / current key matches)."""

    __slots__ = ("kind", "commit", "records", "sync", "on_path", "idx", "key_on")

    def __init__(self, kind: int, commit: bool = False, records: bool = False, sync: bool = False,
                 on_path: bool = False) -> None:
        self.kind = kind
        self.commit = commit
        self.records = records
        self.sync = sync
        self.on_path = on_path
        self.idx = 0
        self.key_on = False

    def copy(self) -> _Frame:
        f = _Frame(self.kind, self.commit, self.records, self.sync, self.on_path)
        f.idx = self.idx
        f.key_on = self.key_on
        return f


@dataclass
class _Sync:
    """Tokenizer state at a sync point (the end of a record): enough to continue exactly there."""

    frames: list[_Frame]
    records: int
    values: int
    prev_delim: bool
    path_active: bool
    target_found: bool
    root_start: int = -1


class _Ctx:
    """What the tokenizers of one pass share."""

    def __init__(self, out: _Output, fixlog: _FixLog | None, lines: _Lines | None, *, enabled: frozenset[str],
                 nan_string: bool, end: int, read: Callable[[int, int], bytes], jsonl_out: bool = False,
                 concat_kind: Callable[[int, int, int], str] | None = None) -> None:
        self.out = out
        self.fixlog = fixlog
        self.lines = lines
        self.enabled = enabled
        self.nan_string = nan_string
        self.end = end
        self.read = read
        self.jsonl_out = jsonl_out
        self.concat_kind = concat_kind      # None: a value after the root is a problem (forced kind)
        self.utf8_errors = 0
        self.path_scalar = False            # wrapped: the record path ends at a scalar
        self.spills: list[_Deferred] = []   # held-back fix rows that went to temporary files


class _Tokenizer:
    """The tolerant tokenizer (module docstring "Architecture").

    ``mode``: ``doc`` (one JSON value of kind ``array`` / ``keyed`` / ``wrapped`` / ``single`` /
    ``brace_list``), ``stream`` (top-level values written as one array, or JSON Lines), ``line`` (one JSON
    Lines line: values, comments and junk on it), ``clean`` (format detection: values kept as they are,
    every value end a commit point, nothing logged). ``feed(data, final)`` appends ``data`` and runs; it
    returns ``NEED`` (more data please), ``DONE`` (``final`` and all processed), ``STOPPED`` (a sync point
    after ``stop_after`` was reached: ``stop_at``), ``PROBLEM`` (``problem``) or ``RESTART``
    (``restart``)."""

    def __init__(self, ctx: _Ctx, mode: str, *, kind: str = "", path: tuple = (), start: int = 0,
                 sync: _Sync | None = None, stop_after: int | None = None, records: int = 0) -> None:
        self.ctx = ctx
        self.out = ctx.out
        self.mode = mode
        self.kind = kind
        self.path = tuple(path) if path else ()
        self.loose = mode in ("stream", "clean")
        self.buf = b""
        self.base = start
        self.p = 0
        self.final = False
        self.copy_from = start
        self.nofast = -1
        self.frames: list[_Frame] = []
        self.st = S_TOP if mode in ("stream", "clean") else S_ROOT
        self.records = records
        self.values = 0
        self.cur_record: int | None = None
        self.path_active = bool(self.path)
        self.target_found = False
        self.prev_delim = False
        self.last_end = start
        self.stop_after = stop_after
        self.stop_at = -1
        self.problem: _Problem | None = None
        self.restart: str | None = None
        self.truncation: tuple[int, int, int, int, str] | None = None
        # the gap hold (module docstring)
        self.hold = False
        self.hold_from = -1
        self.comma = -1
        self.holes: list[tuple[int, int, bytes, str, str]] | None = None
        self.f_insert = False
        self.f_comma_out = -1
        self.f_comma_loc: tuple[int, int, int] | None = None
        self.defer: _Deferred | None = None       # fix rows held back (inside a single-quoted string ...)
        self.drop_ws = mode == "stream" and ctx.jsonl_out
        self.root_start = -1
        self.root_end = -1
        self.rec_start = -1
        self.begun = -1
        self.begun_on = False
        self.opened = False
        self.rec_fails = 0      # record fast path: misses in a row ...
        self.rec_skip = 0       # ... and records that skip it (_record_miss)
        # token sub-states
        self.s_quote = 0
        self.s_start = 0
        self.s_key = False
        self.s_out = 0
        self.s_loc: tuple[int, int, int] | None = None
        self.s_kb: bytearray | None = None
        self.s_kb_cap = 0
        self.c_start = 0
        self.c_ret = 0
        self.c_nl = bytearray()
        self.t_start = -1
        self.t_out = 0
        if sync is not None:
            self.frames = [f.copy() for f in sync.frames]
            self.records = sync.records
            self.values = sync.values
            self.prev_delim = sync.prev_delim
            self.path_active = sync.path_active
            self.target_found = sync.target_found
            self.root_start = sync.root_start
            self.opened = self.values > 0
            if self.frames:
                self.st = S_AFTER
                self._hold_begin(start)
                if mode == "doc" and kind == "single":
                    self.cur_record = 0   # inside the root of a one-value document
            elif mode == "stream" and self.values and not ctx.jsonl_out:
                self._hold_begin(start)
        # the commit point at the start
        self.m_src = start
        self.m_out = self.out.pos
        self.m_records = self.records
        self.m_values = self.values
        self.m_depth = len(self.frames)
        self.m_fix = ctx.fixlog.state() if ctx.fixlog is not None else None
        self.m_lines = ctx.lines.state if ctx.lines is not None else None

    # -- driving -----------------------------------------------------------------------------------
    def feed(self, data: bytes, final: bool) -> int:
        if self.p:
            self._compact()
        if data:
            self.buf = self.buf + data if self.buf else data
        self.final = final
        try:
            return self._run()
        except _Problem as exc:
            self.problem = exc
            return PROBLEM
        except _Restart as exc:
            self.restart = exc.kind
            return RESTART

    def sync(self) -> _Sync:
        return _Sync([f.copy() for f in self.frames], self.records, self.values, self.prev_delim, self.path_active,
                     self.target_found, self.root_start)

    def _compact(self) -> None:
        """Drop the consumed bytes before the next window (the held gap stays: ``[hold_from, p)``)."""
        p = self.p
        st = self.st
        base = self.base
        in_comment = st == S_LCOMMENT or st == S_BCOMMENT
        if self.hold and in_comment:
            # a long comment inside the gap: past HOLD_MAX the gap is written out up to the comment
            if base + p - (self.comma if self.comma >= 0 else self.hold_from) > HOLD_MAX:
                self._hold_force(self.c_start)
        if self.hold:
            self._flush(self.hold_from)
            keep = self.hold_from - base
        else:
            if not in_comment and st != S_SKIP and self.copy_from < base + p:
                self._flush(base + p)
            keep = p
        if keep > 0:
            self.buf = self.buf[keep:]
            self.base = base + keep
            self.p = p - keep
        self.nofast = -1

    def _flush(self, upto: int) -> None:
        cf = self.copy_from
        if upto > cf:
            base = self.base
            self.out.write(self.buf[cf - base:upto - base])
            self.copy_from = upto

    def _src(self, a: int, b: int) -> bytes:
        """Source bytes for a preview (from the buffer when they are still there)."""
        base = self.base
        b = min(b, self.ctx.end)
        if a >= base and b <= base + len(self.buf):
            return self.buf[a - base:b - base]
        return self.ctx.read(a, b) if b > a else b""

    def _need(self, code: str, a: int) -> None:
        if code not in self.ctx.enabled:
            raise _Problem("disabled", a, detail=code)

    def _log(self, code: str, a: int, length: int, o: int, olen: int, before: str, after: str,
             reason: str = "") -> None:
        ctx = self.ctx
        fl = ctx.fixlog
        if fl is None:
            return
        line, col, user = ctx.lines.locate(a)  # type: ignore[union-attr]
        row = (code, FIX_CATEGORY[code], user, line, col, o, self.cur_record, before, after, length, olen, reason)
        if self.defer is not None:
            self.defer.append(row)
        else:
            fl.add_row(row)

    def _log_at(self, loc: tuple[int, int, int] | None, code: str, length: int, o: int, olen: int, before: str,
                after: str) -> None:
        fl = self.ctx.fixlog
        if fl is None or loc is None:
            return
        line, col, user = loc
        fl.add_row((code, FIX_CATEGORY[code], user, line, col, o, self.cur_record, before, after, length, olen, ""))

    def _new_defer(self) -> _Deferred:
        """Start holding fix rows back (a pending earlier entry is logged first, keeping file order)."""
        if self.defer is not None:  # pragma: no cover - every hold is decided before the next one starts
            self._undefer()
        fl = self.ctx.fixlog
        self.defer = d = _Deferred(None if fl is None else fl._directory, self.ctx.spills)
        return d

    def _undefer(self) -> None:
        deferred, self.defer = self.defer, None
        if deferred is None:
            return
        fl = self.ctx.fixlog
        if deferred.f is None:   # never went to a file: nothing to clean up
            if deferred.rows and fl is not None:
                for row in deferred.rows:
                    fl.add_row(row)
            return
        try:
            if deferred and fl is not None:
                for row in deferred:
                    fl.add_row(row)
        finally:
            deferred.close()

    def _expected(self) -> str:
        st = self.st
        if st == S_AFTER:
            return "comma_or_bracket" if self.frames[-1].kind == ARR else "comma_or_brace"
        return _EXPECTED.get(st, "none")

    # -- main loop ---------------------------------------------------------------------------------------
    def _run(self) -> int:
        while True:
            st = self.st
            if st >= S_STRING:
                if st == S_STRING:
                    r = self._string()
                elif st == S_BCOMMENT:
                    r = self._bcomment()
                elif st == S_LCOMMENT:
                    r = self._lcomment()
                else:
                    r = self._skip()
                if r is not None:
                    return r
                continue
            buf = self.buf
            p = self.p
            if p >= len(buf):
                if not self.final:
                    return NEED
                return self._eof()
            c = buf[p]
            if c in _WS_BYTES:
                self.p = p = _WS_MATCH(buf, p).end()
                if self.hold:
                    self._hold_check(self.base + p)
                elif self.drop_ws:
                    self.copy_from = self.base + p
                if p >= len(buf):
                    if not self.final:
                        return NEED
                    return self._eof()
                c = buf[p]
            if c == 0x2F:
                r = self._comment(p)
            elif c == 0xEF and (r := self._bom(p)) is not _NOT_BOM:
                pass
            elif st == S_AFTER:
                r = self._st_after(p, c)
            elif st == S_VALUE or st == S_VALUE_OR_END:
                r = self._st_value(p, c, st)
            elif st == S_KEY or st == S_KEY_OR_END:
                r = self._st_key(p, c, st)
            elif st == S_COLON:
                if c != 0x3A:
                    raise _Problem("unexpected_char", self.base + p, "colon")
                self.p = p + 1
                self.st = S_VALUE
                continue
            elif st == S_ROOT:
                if c not in _VALUE_START:
                    raise _Problem("unexpected_char", self.base + p, "value")
                r = self._value(p, c)
            elif st == S_END:
                r = self._st_end(p, c)
            else:
                r = self._st_top(p, c)
            if r is not None:
                return r  # type: ignore[return-value]

    # -- states ----------------------------------------------------------------------------------------
    def _st_after(self, p: int, c: int) -> int | None:
        a = self.base + p
        f = self.frames[-1]
        if c == 0x2C:
            if self.hold:
                self.comma = a
            else:   # the gap was written out (fallback): hold again from the comma
                self._hold_at_comma(a)
            self.p = p + 1
            self.st = S_KEY if f.kind == OBJ else S_VALUE
            return None
        if c == 0x5D or c == 0x7D:
            self._resolve("close", a)
            return self._close(p, c)
        if c in (_KEY_START if f.kind == OBJ else _VALUE_START):
            if not (a > self.last_end or self.prev_delim):
                raise _Problem("unexpected_char", a, self._expected())
            self._need("missing_comma", a)
            self._resolve("value", a)
            self.st = S_KEY if f.kind == OBJ else S_VALUE
            return None
        raise _Problem("unexpected_char", a, self._expected())

    def _st_value(self, p: int, c: int, st: int) -> int | None:
        a = self.base + p
        if c == 0x5D or c == 0x7D:
            if self.comma >= 0:
                self._need("trailing_comma", self.comma)
                self._resolve("close", a)
                return self._close(p, c)
            if st == S_VALUE_OR_END:
                return self._close(p, c)
            raise _Problem("unexpected_char", a, "value")
        if c in _VALUE_START:
            if self.hold or self.comma >= 0 or self.f_insert:
                self._resolve("value", a)
            return self._value(p, c)
        raise _Problem("unexpected_char", a, "value" if st == S_VALUE else "value_or_end")

    def _st_key(self, p: int, c: int, st: int) -> int | None:
        a = self.base + p
        if c == 0x7D or c == 0x5D:
            if self.comma >= 0:
                self._need("trailing_comma", self.comma)
                self._resolve("close", a)
                return self._close(p, c)
            if st == S_KEY_OR_END:
                return self._close(p, c)
            raise _Problem("unexpected_char", a, "key")
        if c in _KEY_START:
            if self.hold or self.comma >= 0 or self.f_insert:
                self._resolve("value", a)
            return self._key(p, c)
        raise _Problem("unexpected_char", a, "key" if st == S_KEY else "key_or_end")

    def _st_end(self, p: int, c: int) -> int | None:
        a = self.base + p
        if self.mode == "line":
            if c == 0x2C and self.comma < 0 and self.f_comma_out < 0:
                if self.hold:
                    self.comma = a
                else:
                    self._hold_at_comma(a)
                self.p = p + 1
                return None
            if c in _STRICT_START:
                self._need("line_split", self.comma if self.comma >= 0 else a)
                self._resolve("value", a)
                self.st = S_ROOT
                return self._value(p, c)
            return self._trailing(p)
        if c in _STRICT_START:
            self._extra_value(a)
        if c == 0x2C:
            r = self._end_comma(p)
            if r is not None:
                return r
        return self._trailing(p)

    def _extra_value(self, a: int) -> None:
        ctx = self.ctx
        if ctx.concat_kind is not None:
            raise _Restart(ctx.concat_kind(self.root_start, self.root_end, a))
        raise _Problem("extra_value", a)

    def _end_comma(self, p: int) -> int | None:
        """A comma after the root: another value follows (a comma-separated stream) or it is junk."""
        buf = self.buf
        n = len(buf)
        q = _WS_MATCH(buf, p + 1).end()
        if q >= n:
            if not self.final and q - p < LOOKAHEAD_MAX:
                self.p = p
                return NEED
            return None
        if buf[q] in _STRICT_START:
            if self.ctx.concat_kind is not None:
                raise _Restart("stream")
            raise _Problem("extra_value", self.base + q)
        return None

    def _st_top(self, p: int, c: int) -> int | None:
        a = self.base + p
        if c == 0x2C:
            if self.values == 0:
                raise _Problem("unexpected_char", a, "value")
            if self.mode == "clean":
                self._flush(a)
                self.copy_from = a + 1
                self.p = p + 1
                return None
            if self.comma >= 0 or self.f_comma_out >= 0:
                raise _Problem("unexpected_char", a, "value")
            if self.drop_ws:
                self.comma = a
                self.copy_from = a + 1
            elif self.hold:
                self.comma = a
            else:
                self._hold_at_comma(a)
            self.p = p + 1
            return None
        if c in _VALUE_START:
            if self.mode == "stream":
                if self.values == 0:
                    if not self.opened:   # once: the value may be dispatched again after NEED
                        self._need("concatenated", a)
                        self.opened = True
                        if self.drop_ws:
                            self.copy_from = a
                            self._log("concatenated", a, 0, self.out.pos, 0, "", "JSONL")
                        else:
                            self._flush(a)
                            o = self.out.pos
                            self.out.write(b"[")
                            self._log("concatenated", a, 0, o, 1, "", "[")
                elif self.drop_ws:
                    self.comma = -1
                    self.copy_from = a
                elif self.hold or self.comma >= 0 or self.f_insert or self.f_comma_out >= 0:
                    self._resolve("value", a)
                self.drop_ws = False
            return self._value(p, c)
        raise _Problem("unexpected_char", a, "value")

    # -- values ----------------------------------------------------------------------------------------
    def _begin(self, a: int) -> bool:
        """Bookkeeping at the start of a value at ``a``; returns whether it lies on the record path.
        Idempotent for the same ``a`` (a token cut by the window end is dispatched again)."""
        if a == self.begun:
            return self.begun_on
        self.begun = a
        self.begun_on = on = self._begin_new(a)
        return on

    def _begin_new(self, a: int) -> bool:
        frames = self.frames
        if not frames:
            if self.mode == "doc":
                self.root_start = a
                if self.kind == "single":
                    self.cur_record = 0
            else:
                self.rec_start = a
                self.cur_record = self.records
            return False
        f = frames[-1]
        if f.kind == OBJ:
            return f.on_path and self.path_active and f.key_on
        i = f.idx
        f.idx = i + 1
        on = False
        if f.on_path and self.path_active:
            seg = self.path[len(frames) - 1]
            on = type(seg) is int and seg == i
        if f.commit:
            self.rec_start = a
            if f.records:
                self.cur_record = self.records
        return on

    def _value(self, p: int, c: int) -> int | None:
        buf = self.buf
        base = self.base
        on = self._begin(base + p)
        frames = self.frames
        d = len(frames)
        if on and c != 0x7B and c != 0x5B and d == len(self.path):
            # the record path ends at a scalar: there is no record container (warned)
            self.path_active = False
            self.ctx.path_scalar = True
            on = False
        if not on and c in _STRICT_START and p != self.nofast and (d or self.mode != "doc"):
            # (a fast match never starts at a byte that cannot start a strict value: ``'``, ``N``, ``+`` ...)
            k = _K if MAX_DEPTH - d >= _K else max(0, MAX_DEPTH - d)
            n = len(buf)
            endp = n if n - p <= SLICE else p + SLICE
            f = frames[-1] if d else None
            if f is not None and f.kind != OBJ and not (f.on_path and self.path_active):
                if f.commit:
                    if self.rec_skip:   # the record fast path kept failing: the state machine takes them
                        self.rec_skip -= 1
                    else:
                        r = self._records_seq(p, k, f)
                        if r is not _NOMATCH:
                            self.rec_fails = 0
                            return r  # type: ignore[return-value]
                        self._record_miss()
                else:
                    e = _VAR.get("arr_skip", k)(buf, p, endp).end()
                    if e > p:
                        return self._after_skip(e, f)
            else:
                m = _VAR.get("value", k)(buf, p, endp)
                if m is not None and _number_ok(buf, m.end(), endp):
                    e = m.end()
                    self.p = e
                    self.prev_delim = buf[e - 1] in _SELF_DELIM
                    return self._value_end(base + e)
        if c == 0x7B:
            return self._open(p, OBJ, on)
        if c == 0x5B:
            return self._open(p, ARR, on)
        if c == 0x22 or c == 0x27:
            self.s_kb = None
            return self._str_begin(p, c, False)
        if c == 0x2D or 0x30 <= c <= 0x39:
            return self._number(p, c)
        if c == 0x2B:
            return self._signed(p)
        return self._word(p)

    def _records(self, p: int, k: int, f: _Frame, member: bool) -> Any:
        """Records of a commit-level container, one strict regex match each (the core's ``elem_arr`` /
        ``elem_brace`` / ``member`` patterns: value, then ``, ws`` or the closer). A record whose next one
        does not match leaves its comma pending (the next token decides it)."""
        buf = self.buf
        base = self.base
        n = len(buf)
        if member:
            match = _VAR.get("member", k)
            close = 0x7D
        elif f.kind == BRACE:
            match = _VAR.get("elem_brace", k)
            close = 0x7D
        else:
            match = _VAR.get("elem_arr", k)
            close = 0x5D
        m = match(buf, p, n if n - p <= SLICE else p + SLICE)
        if m is None:
            return _NOMATCH
        records = f.records
        sync = f.sync and self.stop_after is not None
        while True:
            ve = m.end(1)
            e = base + ve
            if records:
                self.records += 1
                self.cur_record = None
            self.rec_start = -1
            self.last_end = e
            self.prev_delim = buf[ve - 1] in _SELF_DELIM
            self._mark(e)
            if sync and e > self.stop_after:  # type: ignore[operator]
                self.p = ve
                return self._stop(e)
            me = m.end()
            if buf[me - 1] == close:   # the closer follows: the state machine takes it
                self.p = ve
                self.st = S_AFTER
                self._hold_begin(e)
                return None
            m = match(buf, me, n if n - me <= SLICE else me + SLICE)
            if m is None:   # the comma's fate depends on what follows
                c = buf.index(b",", ve, me)
                self.p = c + 1
                self.nofast = me
                self.st = S_KEY if member else S_VALUE
                self._hold_at_comma(base + c)
                return None

    def _records_seq(self, p: int, k: int, f: _Frame) -> Any:
        return self._records(p, k, f, False)

    def _record_miss(self) -> None:
        """The record fast path did not match (a broken record: the regex scanned it in vain). After
        :data:`REC_FAILS` misses in a row the next :data:`REC_SKIP` records skip the attempt -- the state
        machine gives the same result, only the wasted scans of error-dense data are saved."""
        self.rec_fails += 1
        if self.rec_fails >= REC_FAILS:
            self.rec_fails = 0
            self.rec_skip = REC_SKIP

    def _after_skip(self, e: int, f: _Frame) -> None:
        """After a run of strict elements / members inside a record (``arr_skip`` / ``obj_skip``)."""
        buf = self.buf
        base = self.base
        self.p = e
        if buf[e - 1] == 0x2C:   # a comma whose fate the next token decides
            self._hold_at_comma(base + e - 1)
            self.st = S_KEY if f.kind == OBJ else S_VALUE
        else:                    # the closer follows (its whitespace was copied)
            self.last_end = base + e
            self.prev_delim = True
            self.st = S_AFTER
            self._hold_begin(base + e)
        return None

    def _open(self, p: int, kind: int, on: bool) -> int | None:
        a = self.base + p
        frames = self.frames
        d = len(frames)
        if d >= MAX_DEPTH:
            raise _Problem("depth_limit", a)
        mode = self.mode
        f = _Frame(kind)
        if mode == "clean":
            f.commit = True
            if d == 0 and kind == OBJ:
                r = self._brace_lookahead(p)
                if r is None:
                    self.p = p
                    return NEED
                if r:
                    f.kind = BRACE
        elif d == 0 and mode == "doc":
            if self.path:
                f.on_path = True
                f.commit = True
            else:
                f.commit = True
                f.sync = True
                f.records = self.kind != "single"
                if kind == OBJ:
                    if self.kind == "brace_list":
                        f.kind = BRACE
                    elif self.kind == "single":
                        r = self._brace_lookahead(p)
                        if r is None:
                            self.p = p
                            return NEED
                        if r:
                            f.kind = BRACE
        elif on:
            f.on_path = True
            f.commit = True
            if d == len(self.path):   # the record container
                f.records = True
                f.sync = True
                self.path_active = False
                self.target_found = True
        if f.kind == BRACE and mode != "clean":
            self._need("brace_list", a)
            self._flush(a)
            o = self.out.pos
            self.out.write(b"[")
            self.copy_from = a + 1
            self._log("brace_list", a, 1, o, 1, "{", "[")
        frames.append(f)
        self.p = p + 1
        self.st = S_KEY_OR_END if f.kind == OBJ else S_VALUE_OR_END
        if f.commit:
            self._mark(a + 1)
        return None

    def _close(self, p: int, c: int) -> int | None:
        frames = self.frames
        a = self.base + p
        if not frames:
            raise _Problem("unexpected_char", a, self._expected())
        f = frames[-1]
        if c != (0x5D if f.kind == ARR else 0x7D):
            raise _Problem("mismatched_closer", a)
        if f.kind == BRACE and self.mode != "clean":
            self._flush(a)
            self.out.write(b"]")
            self.copy_from = a + 1
        frames.pop()
        self.p = p + 1
        self.prev_delim = True
        return self._value_end(a + 1)

    def _value_end(self, e: int) -> int | None:
        self.last_end = e
        frames = self.frames
        if frames:
            f = frames[-1]
            if f.commit:
                if f.records:
                    self.records += 1
                    self.cur_record = None
                self.rec_start = -1
                self._mark(e)
                if f.sync and self.stop_after is not None and e > self.stop_after:
                    return self._stop(e)
            self.st = S_AFTER
            self._hold_begin(e)
            return None
        mode = self.mode
        if mode == "doc":
            self.root_end = e
            self.st = S_END
            if self.kind == "single":
                self.records = 1
            self.cur_record = None
            self._mark(e)
            return None
        if mode == "stream":
            self.values += 1
            self.records += 1
            self.cur_record = None
            self.rec_start = -1
            if self.ctx.jsonl_out:
                self._flush(e)
                self.out.write_raw(b"\n")
                self.drop_ws = True
            self._mark(e)
            self.st = S_TOP
            if self.stop_after is not None and e > self.stop_after:
                return self._stop(e)
            if not self.ctx.jsonl_out:
                self._hold_begin(e)
            return None
        if mode == "line":
            self.values += 1
            self.records += 1
            self.cur_record = None
            self.st = S_END
            self._hold_begin(e)
            return None
        self.values += 1     # clean
        self._mark(e)
        self.st = S_TOP
        return None

    # -- commit points ---------------------------------------------------------------------------------
    def _mark(self, e: int) -> None:
        out = self.out
        cf = self.copy_from
        m_out = out.pos + (e - cf) if cf < e else out.pos
        self.m_src = e
        self.m_out = m_out
        self.m_records = self.records
        self.m_values = self.values
        self.m_depth = len(self.frames)
        ctx = self.ctx
        if ctx.fixlog is not None:
            self.m_fix = ctx.fixlog.state()
            self.m_lines = ctx.lines.state  # type: ignore[union-attr]
        if m_out - out.committed >= FLUSH_BATCH:
            self._flush(e)
            out.commit(m_out)

    def _stop(self, e: int) -> int:
        self._flush(e)
        self.stop_at = e
        return STOPPED

    def _rollback(self) -> None:
        e = self.m_src
        if self.copy_from < e:
            self._flush(e)
        self.out.truncate_to(self.m_out)
        ctx = self.ctx
        if ctx.fixlog is not None:
            ctx.fixlog.rollback(self.m_fix)  # type: ignore[arg-type]
            ctx.lines.state = self.m_lines  # type: ignore[union-attr,assignment]
        self.records = self.m_records
        self.values = self.m_values
        self.copy_from = e
        self.hold = False
        self.holes = None
        self.comma = -1
        self.f_insert = False
        self.f_comma_out = -1
        if self.defer is not None:
            self.defer.close()
            self.defer = None

    def _cut(self, log_fix: bool) -> bytes:
        """Roll back to the last commit point and close the containers open there (truncation, or the
        partial result before a problem); fills ``truncation``."""
        if self.mode == "stream":
            closers = b"" if self.ctx.jsonl_out else (b"]" if self.m_values else b"[]")
        else:
            clean = self.mode == "clean"
            closers = bytes(0x7D if f.kind == OBJ or (clean and f.kind == BRACE) else 0x5D
                            for f in reversed(self.frames[:self.m_depth]))
        cut = self.m_src
        partial = self.rec_start if self.rec_start >= cut else -1
        root_open = self.m_depth > 0 or self.root_end >= 0
        self._rollback()
        o = self.out.pos
        self.out.write_raw(closers)
        text = closers.decode("ascii")
        if self.mode == "doc" and self.kind == "single" and root_open:
            self.records = 1
        self.cur_record = None
        dropped = self.ctx.end - cut
        if log_fix:
            self._log("truncated", cut, dropped, o, len(closers), _show(self._src(cut, cut + PREVIEW_BYTES)), text)
        self.truncation = (cut, self.records, dropped, partial, text)
        self.frames = []
        return closers

    def _truncate(self, end: int) -> int:
        if self.mode == "doc" and self.m_depth == 0 and self.root_end < 0:
            raise _Problem("nothing_complete", self.root_start if self.root_start >= 0 else end)
        self._need("truncated", self.m_src)
        self._cut(True)
        return DONE

    def _eof_in_value(self, a: int) -> int:
        """The data ended inside a token."""
        if self.mode == "line":
            raise _Problem("truncated", a)
        if self.mode == "clean":
            self._cut(False)
            return DONE
        return self._truncate(a)

    def _eof(self) -> int:
        mode = self.mode
        st = self.st
        end = self.base + len(self.buf)
        if mode == "doc":
            if st == S_END:
                self._flush(end)
                return DONE
            if st == S_ROOT:
                raise _Problem("nothing_complete", end)
            return self._truncate(end)
        if mode == "stream":
            if self.frames:
                return self._truncate(end)
            if self.values == 0:
                if self.drop_ws:
                    self.copy_from = end
                else:
                    self._need("concatenated", end)
                    self._flush(end)
                    o = self.out.pos
                    self.out.write(b"[]")
                    self._log("concatenated", end, 0, o, 2, "", "[")
                return DONE
            if self.comma >= 0:
                self._need("trailing_comma", self.comma)
            if self.drop_ws:
                if self.comma >= 0:
                    self._log("trailing_comma", self.comma, 1, self.out.pos, 0, ",", "")
                    self.comma = -1
                self.copy_from = end
            else:
                self._resolve("end", end)
            self._flush(end)
            return DONE
        if mode == "line":
            if self.frames or st not in (S_ROOT, S_END):
                raise _Problem("truncated", end)
            if self.comma >= 0:
                self._need("trailing_comma", self.comma)
            if self.hold or self.comma >= 0 or self.f_comma_out >= 0:
                self._resolve("end", end)
            self._flush(end)
            return DONE
        if not self.frames and st == S_TOP:    # clean
            self._flush(end)
            return DONE
        self._cut(False)
        return DONE

    # -- the gap hold --------------------------------------------------------------------------------
    # A hold is lazy: the gap's bytes stay in the buffer (``[hold_from, p)``, at most HOLD_MAX) and nothing is
    # written until the next token decides; comments / BOMs inside it are recorded in ``holes``.
    def _hold_begin(self, e: int) -> None:
        self.hold = True
        self.hold_from = e
        self.comma = -1
        self.holes = None
        self.f_insert = False
        self.f_comma_out = -1

    def _hold_at_comma(self, a: int) -> None:
        """A hold that starts at a comma whose fate the next token decides."""
        self.hold = True
        self.hold_from = a
        self.comma = a
        self.holes = None
        self.f_insert = False
        self.f_comma_out = -1

    def _hold_check(self, a: int) -> None:
        if a - (self.comma if self.comma >= 0 else self.hold_from) > HOLD_MAX:
            self._hold_force(a)

    def _hold_force(self, a: int) -> None:
        """The gap grew beyond HOLD_MAX: write it out up to ``a``, the comma in place; the pending decision falls
        back (module docstring)."""
        self._flush(self.hold_from)
        self._emit_gap(a, "forced")
        self.hold = False
        self.holes = None
        if self.comma < 0:
            self.f_insert = True

    def _emit_gap(self, a: int, comma_action: str | None) -> None:
        """Write the held gap ``[hold_from, a)`` applying its holes and ``comma_action`` (``keep`` / ``drop``
        (trailing comma) / ``newline`` (JSON Lines split) / ``forced`` (in place, remembered) / ``None``).
        Holes and a comma at or after ``a`` are left out (``_trailing``)."""
        out = self.out
        c = self.comma
        if c >= a:
            c = -1
        for hole in (self.holes or ()):
            hs = hole[0]
            if hs >= a:
                break
            if 0 <= c < hs:
                self._gap_comma(c, comma_action)
                c = -1
            he, repl, code, before = hole[1], hole[2], hole[3], hole[4]
            self._flush(hs)
            o = out.pos
            out.write(repl)
            self._log(code, hs, he - hs, o, len(repl), before, _show(repl))
            self.copy_from = he
        if c >= 0:
            self._gap_comma(c, comma_action)
        self._flush(a)

    def _gap_comma(self, c: int, action: str | None) -> None:
        if action == "drop":
            self._flush(c)
            self._log("trailing_comma", c, 1, self.out.pos, 0, ",", "")
            self.copy_from = c + 1
        elif action == "newline":
            self._flush(c)
            o = self.out.pos
            self.out.write_raw(b"\n")
            self._log("line_split", c, 1, o, 1, ",", "\\n")
            self.copy_from = c + 1
        elif action == "forced":
            self._flush(c)
            self.f_comma_out = self.out.pos
            ctx = self.ctx
            self.f_comma_loc = ctx.lines.locate(c) if ctx.fixlog is not None else None  # type: ignore[union-attr]
            self._new_defer()

    def _separator(self) -> tuple[bytes, str | None]:
        if not self.frames:
            if self.mode == "line":
                return b"\n", "line_split"
            if self.mode == "stream":
                return b",", None
        return b",", "missing_comma"

    def _resolve(self, what: str, a: int) -> None:
        """Decide the held gap before the token at ``a``: ``value`` (a value / key follows), ``close`` (a
        closer follows) or ``end`` (end of the data)."""
        out = self.out
        mode = self.mode
        line_top = mode == "line" and not self.frames
        if self.hold:
            self.hold = False
            comma = self.comma
            ins: bytes | None = None
            code: str | None = None
            if what == "value" and comma < 0:
                ins, code = self._separator()
            elif what == "end" and mode == "stream":
                ins = b"]"
            if comma < 0:
                action = None
            elif what == "value":
                action = "newline" if line_top else "keep"
            else:
                action = "drop"
            if ins is not None or self.holes or action == "drop" or action == "newline":
                self._flush(self.hold_from)
                if ins is not None:
                    o = out.pos
                    out.write_raw(ins)
                    if code is not None:
                        self._log(code, self.hold_from, 0, o, len(ins), "", _show(ins))
                self._emit_gap(a, action)
            # else: nothing changes, the gap is copied lazily with what follows
            self.holes = None
        else:
            if self.f_comma_out >= 0:
                deferred, self.defer = self.defer, None
                if what == "value":
                    if line_top:
                        out.patch(self.f_comma_out, b"\n")
                        self._log_at(self.f_comma_loc, "line_split", 1, self.f_comma_out, 1, ",", "\\n")
                else:
                    out.patch(self.f_comma_out, b" ")
                    self._log_at(self.f_comma_loc, "trailing_comma", 1, self.f_comma_out, 1, ",", " ")
                self.defer = deferred
                self._undefer()
            elif what == "value" and self.f_insert:
                sep, code = self._separator()
                self._flush(a)
                o = out.pos
                out.write_raw(sep)
                if code is not None:
                    self._log(code, a, 0, o, len(sep), "", _show(sep))
            if what == "end" and mode == "stream":
                self._flush(a)
                out.write(b"]")
        self.comma = -1
        self.f_insert = False
        self.f_comma_out = -1

    # -- strings ---------------------------------------------------------------------------------------
    def _str_begin(self, p: int, c: int, is_key: bool) -> int | None:
        a = self.base + p
        self.s_quote = c
        self.s_start = a
        self.s_key = is_key
        if c == 0x27:
            self._need("single_quote", a)
            buf = self.buf
            n = len(buf)
            m = _SQ_SIMPLE(buf, p, n if n - p <= SLICE else p + SLICE)
            if m is not None:
                return self._sq_simple(p, m.end())
            self._flush(a)
            self.s_out = self.out.pos
            self.out.write(b'"')
            self.copy_from = a + 1
            ctx = self.ctx
            self.s_loc = ctx.lines.locate(a) if ctx.fixlog is not None else None  # type: ignore[union-attr]
            self._new_defer()
        self.st = S_STRING
        self.p = p + 1
        return None

    def _kb(self, data: bytes) -> None:
        kb = self.s_kb
        if kb is not None:
            kb += data
            if len(kb) > self.s_kb_cap:
                self.s_kb = None

    def _string(self) -> int | None:
        buf = self.buf
        n = len(buf)
        p = self.p
        base = self.base
        quote = self.s_quote
        sq = quote == 0x27
        run = _SQ_RUN if sq else _STR_RUN
        out = self.out
        while True:
            end = n if n - p <= SLICE else p + SLICE
            q = run(buf, p, end).end()
            if q > p:
                if self.s_kb is not None:
                    self._kb(buf[p:q])
                p = q
                if q >= end and end < n:
                    continue
            if p >= n:
                self.p = p
                if self.final:
                    return self._eof_in_value(base + p)
                return NEED
            c = buf[p]
            a = base + p
            if c == quote:
                if sq:
                    self._flush(a)
                    out.write(b'"')
                    self.copy_from = a + 1
                    self._sq_done(a + 1)
                self.p = p + 1
                if self.s_key:
                    self._key_done()
                    self.st = S_COLON
                    return None
                self.prev_delim = True
                return self._value_end(a + 1)
            if c == 0x22:            # an unescaped double quote inside a single-quoted string
                self._flush(a)
                out.write(b'\\"')
                self.copy_from = a + 1
                self._kb(b'\\"')
                p += 1
                continue
            if c == 0x5C:
                r = self._escape(p)
            elif c < 0x20:
                self._need("control_char", a)
                self._flush(a)
                o = out.pos
                rep = _ESCAPE_OF.get(c) or b"\\u%04x" % c
                out.write(rep)
                self.copy_from = a + 1
                self._log("control_char", a, 1, o, len(rep), _show(bytes((c,))), rep.decode("ascii"))
                self._kb(rep)
                p += 1
                continue
            else:
                r = self._utf8(p)
            if r == -1:
                self.p = p
                return NEED
            if r == -2:
                self.p = p
                return self._eof_in_value(a)
            p = r

    def _escape(self, p: int) -> int:
        """A backslash at ``p`` that the strict run did not accept; returns the next position, ``-1`` (more
        data needed) or ``-2`` (the data ends here)."""
        buf = self.buf
        n = len(buf)
        a = self.base + p
        if p + 1 >= n:
            return -2 if self.final else -1
        c1 = buf[p + 1]
        out = self.out
        if c1 == 0x75:   # \u without four hex digits (or cut by the window)
            if p + 6 > n and not self.final:
                return -1
            if p + 6 <= n and _HEX4(buf, p + 2, p + 6):
                self._kb(buf[p:p + 6])
                return p + 6
            rep, length, before, after = b"\\\\", 1, "\\u", "\\\\u"
        elif c1 == 0x27:   # \'
            if self.s_quote == 0x27:   # part of the single-quote conversion
                self._flush(a)
                out.write(b"'")
                self.copy_from = a + 2
                self._kb(b"'")
                return p + 2
            rep, length, before, after = b"'", 2, "\\'", "'"
        elif c1 == 0x78:   # \xHH
            if p + 4 > n and not self.final:
                return -1
            if p + 4 <= n and _HEX2(buf, p + 2, p + 4):
                hh = buf[p + 2:p + 4]
                rep, length = b"\\u00" + hh, 4
                before, after = "\\x" + hh.decode("ascii"), rep.decode("ascii")
            else:
                rep, length, before, after = b"\\\\", 1, "\\x", "\\\\x"
        elif c1 in b'"\\/bfnrt':   # pragma: no cover - the strict run takes valid escapes
            self._kb(buf[p:p + 2])
            return p + 2
        else:   # any other character: a literal backslash
            ch = _show(buf[p + 1:p + 2])
            rep, length, before, after = b"\\\\", 1, "\\" + ch, "\\\\" + ch
        self._need("invalid_escape", a)
        self._flush(a)
        o = out.pos
        out.write(rep)
        self.copy_from = a + length
        self._log("invalid_escape", a, length, o, len(rep), before, after)
        self._kb(rep)
        return p + length

    def _utf8(self, p: int) -> int:
        """A byte >= 0x80 at ``p`` that the strict run did not accept: a valid character cut by the run's
        slice, an incomplete sequence at the window end (``-1``), or invalid UTF-8 (-> U+FFFD)."""
        buf = self.buf
        m = _UTF8_ONE(buf, p)
        if m is not None:
            self._kb(buf[p:m.end()])
            return m.end()
        n = len(buf)
        chunk = buf[p:p + 4]
        length = 1
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.start == 0:
                if exc.reason == "unexpected end of data" and p + len(chunk) >= n and not self.final:
                    return -1
                length = max(1, exc.end - exc.start)
        a = self.base + p
        self._need("invalid_utf8", a)
        ctx = self.ctx
        ctx.utf8_errors += 1
        if ctx.utf8_errors > UTF8_LIMIT:
            raise _Problem("not_utf8", a)
        self._flush(a)
        o = self.out.pos
        self.out.write(_REPLACEMENT)
        self.copy_from = a + length
        self._log("invalid_utf8", a, length, o, len(_REPLACEMENT), _show(buf[p:p + length]), "�")
        self._kb(_REPLACEMENT)
        return p + length

    def _sq_done(self, e: int) -> None:
        """End of a single-quoted string: its fix, then the fixes found inside it."""
        deferred = self.defer
        fl = self.ctx.fixlog
        if fl is None or self.s_loc is None:
            self._undefer()
            return
        self.defer = None
        a = self.s_start
        o = self.s_out
        olen = self.out.pos - o
        src = self._src(a, min(e, a + PREVIEW_BYTES))
        if not deferred and olen == e - a and len(src) == olen and b'"' not in src and b"\\" not in src:
            text = _show_text(src)   # only the quotes changed
            before = _cut_text(text)
            after = _cut_text('"' + text[1:-1] + '"')
        else:
            before = _show(src)
            after = _show(self.out.peek(o, min(olen, PREVIEW_BYTES)))
        line, col, user = self.s_loc
        fl.add_row(("single_quote", "syntax", user, line, col, o, self.cur_record, before, after, e - a, olen, ""))
        self.defer = deferred
        self._undefer()

    def _sq_simple(self, p: int, q: int) -> int | None:
        """A complete single-quoted string ``buf[p:q]`` with nothing else to fix inside (no ``"``, ``\\'``,
        invalid escape, control character or invalid UTF-8: what ``_SQ_RUN`` accepts) converted in one step:
        the same output and the same entry as ``_string`` + ``_sq_done`` produce for it."""
        buf = self.buf
        base = self.base
        a = base + p
        e = base + q
        out = self.out
        self._flush(a)
        o = out.pos
        content = buf[p + 1:q - 1]
        out.write(b'"' + content + b'"')
        self.copy_from = e
        if self.s_kb is not None:
            self._kb(content)
        ctx = self.ctx
        fl = ctx.fixlog
        if fl is not None:
            line, col, user = ctx.lines.locate(a)  # type: ignore[union-attr]
            n = q - p
            if n <= PREVIEW_BYTES and 0x5C not in content:
                text = _show_text(buf[p:q])
                before = _cut_text(text)
                after = _cut_text('"' + text[1:-1] + '"')
            else:
                before = _show(buf[p:p + min(n, PREVIEW_BYTES)])
                after = _show(b'"' + content + b'"' if n <= PREVIEW_BYTES else b'"' + content[:PREVIEW_BYTES - 1])
            row = ("single_quote", "syntax", user, line, col, o, self.cur_record, before, after, n, n, "")
            if self.defer is not None:  # pragma: no cover - a hold is always decided before a value starts
                self.defer.append(row)
            else:
                fl.add_row(row)
        self.p = q
        if self.s_key:
            self._key_done()
            self.st = S_COLON
            return None
        self.prev_delim = True
        return self._value_end(e)

    def _key_done(self) -> None:
        frames = self.frames
        f = frames[-1]
        if f.on_path and self.path_active:
            seg = self.path[len(frames) - 1]
            kb = self.s_kb
            f.key_on = kb is not None and isinstance(seg, str) and _decode_key(bytes(kb)) == seg
        self.s_kb = None

    # -- keys --------------------------------------------------------------------------------------------
    def _key(self, p: int, c: int) -> int | None:
        buf = self.buf
        base = self.base
        a = base + p
        frames = self.frames
        f = frames[-1]
        path_key = f.on_path and self.path_active
        if f.commit:
            self.rec_start = a
            if f.records:
                self.cur_record = self.records
        if c == 0x22 and p != self.nofast:
            n = len(buf)
            endp = n if n - p <= SLICE else p + SLICE
            if path_key:
                m = _STRING(buf, p, endp)
                if m is not None:
                    e = m.end()
                    seg = self.path[len(frames) - 1]
                    f.key_on = isinstance(seg, str) and _decode_key(buf[p + 1:e - 1]) == seg
                    self.p = e
                    self.st = S_COLON
                    return None
            else:
                d = len(frames)
                k = _K if MAX_DEPTH - d >= _K else max(0, MAX_DEPTH - d)
                if f.commit:
                    if self.rec_skip:
                        self.rec_skip -= 1
                    else:
                        r = self._records(p, k, f, True)
                        if r is not _NOMATCH:
                            self.rec_fails = 0
                            return r  # type: ignore[no-any-return]
                        self._record_miss()
                else:
                    e = _VAR.get("obj_skip", k)(buf, p, endp).end()
                    if e > p:
                        return self._after_skip(e, f)
                # the member's value needs the state machine: its strict key and colon in one step
                m = _KEY_COLON(buf, p, endp)
                if m is not None:
                    self.s_kb = None
                    self.p = m.end()
                    self.st = S_VALUE
                    return None
        if path_key:
            seg = self.path[len(frames) - 1]
            if isinstance(seg, str):
                self.s_kb = bytearray()
                self.s_kb_cap = 6 * len(seg.encode("utf-8", "surrogatepass")) + 16
            else:
                self.s_kb = None
        else:
            self.s_kb = None
        if c == 0x22 or c == 0x27:
            return self._str_begin(p, c, True)
        return self._ident_key(p)

    def _ident_key(self, p: int) -> int | None:
        buf = self.buf
        n = len(buf)
        a = self.base + p
        m = _IDENT(buf, p)
        if m is None:
            raise _Problem("unexpected_char", a, self._expected())
        e = m.end()
        if e >= n:
            if not self.final:
                self.p = p
                return NEED
            return self._eof_in_value(a)
        ident = buf[p:e]
        try:
            text = ident.decode("utf-8")
        except UnicodeDecodeError:
            raise _Problem("unexpected_char", a, self._expected()) from None
        self._need("unquoted_key", a)
        self._flush(a)
        o = self.out.pos
        rep = b'"' + ident + b'"'
        self.out.write(rep)
        self.copy_from = self.base + e
        self._log("unquoted_key", a, e - p, o, len(rep), _show(ident), _show(rep))
        f = self.frames[-1]
        if f.on_path and self.path_active:
            seg = self.path[len(self.frames) - 1]
            f.key_on = isinstance(seg, str) and text == seg
        self.s_kb = None
        self.p = e
        self.st = S_COLON
        return None

    # -- numbers and words ---------------------------------------------------------------------------
    def _number(self, p: int, c: int) -> int | None:
        buf = self.buf
        n = len(buf)
        a = self.base + p
        if c == 0x2D:
            if p + 1 >= n:
                if not self.final:
                    self.p = p
                    return NEED
                return self._eof_in_value(a)
            c1 = buf[p + 1]
            if c1 == 0x49 or c1 == 0x4E:   # -Infinity / -NaN
                return self._signed(p)
        m = _NUMBER(buf, p)
        if m is None:
            raise _Problem("invalid_number", a)
        e = m.end()
        if e >= n:
            if not self.final:
                self.p = p
                return NEED
            if self.frames:   # a number cut by the end of a document is not complete (decision 3)
                return self._eof_in_value(a)
        else:
            c2 = buf[e]
            if not self.final and n - e <= 2 and c2 in b".eE":
                self.p = p
                return NEED
            if c2 in _NUMBER_CONT and not (self.loose and not self.frames):
                raise _Problem("invalid_number", a)
        self.p = e
        self.prev_delim = False
        return self._value_end(self.base + e)

    def _signed(self, p: int) -> int | None:
        """``+`` / ``-`` followed by ``Infinity`` / ``NaN`` (else an invalid number)."""
        buf = self.buf
        n = len(buf)
        a = self.base + p
        if p + 1 >= n:
            if not self.final:
                self.p = p
                return NEED
            return self._eof_in_value(a)
        m = _WORD(buf, p + 1)
        if m is None:
            raise _Problem("invalid_number", a)
        e = m.end()
        if e >= n and not self.final:
            self.p = p
            return NEED
        if e < n and buf[e] in _WORD_CONT:
            raise _Problem("unquoted_value", a)
        w = buf[p + 1:e]
        if w == b"Infinity":
            code = "infinity"
            text = b'"-Infinity"' if buf[p] == 0x2D else b'"Infinity"'
        elif w == b"NaN":
            code = "nan"
            text = b'"NaN"'
        else:
            if e >= n and (b"Infinity".startswith(w) or b"NaN".startswith(w)):
                return self._eof_in_value(a)
            raise _Problem("invalid_number", a)
        return self._replace_value(p, e, code, text if self.ctx.nan_string else b"null")

    def _word(self, p: int) -> int | None:
        buf = self.buf
        n = len(buf)
        a = self.base + p
        m = _WORD(buf, p)
        if m is None:
            raise _Problem("unexpected_char", a, "value")
        e = m.end()
        if e >= n and not self.final:
            self.p = p
            return NEED
        w = buf[p:e]
        if self.loose and not self.frames:
            # the strict stream tokenization: a literal ends where its letters end (``truefalse`` = 2 values)
            for lit in _LITERALS:
                if len(w) > len(lit) and w.startswith(lit):
                    w = lit
                    e = p + len(lit)
                    break
        elif e < n and buf[e] in _WORD_CONT:
            raise _Problem("unquoted_value", a)
        if w in _LITERALS:
            self.p = e
            self.prev_delim = False
            return self._value_end(self.base + e)
        nan_string = self.ctx.nan_string
        if w == b"NaN":
            code, rep = "nan", (b'"NaN"' if nan_string else b"null")
        elif w == b"Infinity":
            code, rep = "infinity", (b'"Infinity"' if nan_string else b"null")
        elif w == b"True":
            code, rep = "python_literal", b"true"
        elif w == b"False":
            code, rep = "python_literal", b"false"
        elif w == b"None":
            code, rep = "python_literal", b"null"
        elif w == b"undefined":
            code, rep = "undefined", b"null"
        else:
            if e >= n and any(k.startswith(w) for k in _KNOWN_WORDS):
                return self._eof_in_value(a)
            raise _Problem("unquoted_value", a)
        return self._replace_value(p, e, code, rep)

    def _replace_value(self, p: int, e: int, code: str, rep: bytes) -> int | None:
        base = self.base
        a = base + p
        self._need(code, a)
        self._flush(a)
        o = self.out.pos
        self.out.write(rep)
        self.copy_from = base + e
        self._log(code, a, e - p, o, len(rep), _show(self.buf[p:e]), rep.decode("ascii"))
        self.p = e
        self.prev_delim = False
        return self._value_end(base + e)

    # -- comments, BOM, trailing data ----------------------------------------------------------------
    def _comment(self, p: int) -> int | None:
        buf = self.buf
        n = len(buf)
        a = self.base + p
        if p + 1 >= n:
            if not self.final:
                self.p = p
                return NEED
            raise _Problem("unexpected_char", a, self._expected())
        c1 = buf[p + 1]
        if c1 != 0x2F and c1 != 0x2A:
            raise _Problem("unexpected_char", a, self._expected())
        self._need("comment", a)
        self.c_start = a
        self.c_ret = self.st
        self.c_nl = bytearray()
        if not self.hold and not self.drop_ws:
            self._flush(a)
        self.st = S_BCOMMENT if c1 == 0x2A else S_LCOMMENT
        self.p = p + 2
        return None

    def _lcomment(self) -> int | None:
        buf = self.buf
        n = len(buf)
        m = _LINE_END(buf, self.p)
        if m is None:
            self.p = n
            if self.final:
                return self._comment_end(self.base + n, b"")
            return NEED
        self.p = m.start()
        return self._comment_end(self.base + self.p, b"")

    def _bcomment(self) -> int | None:
        buf = self.buf
        n = len(buf)
        p = self.p
        i = buf.find(b"*/", p)
        if i < 0:
            stop = n if self.final else max(p, n - 1)   # keep a last '*' for the next window
            if stop > p:
                self.c_nl += buf[p:stop].translate(None, _ALL_BUT_CRLF)
            self.p = stop
            if self.final:
                return self._comment_end(self.base + n, bytes(self.c_nl))
            return NEED
        self.c_nl += buf[p:i].translate(None, _ALL_BUT_CRLF)
        self.p = i + 2
        return self._comment_end(self.base + i + 2, bytes(self.c_nl))

    def _comment_end(self, e: int, repl: bytes) -> None:
        a = self.c_start
        self.st = self.c_ret
        before = _show(self._src(a, min(e, a + PREVIEW_BYTES)))
        if self.hold:
            if self.holes is None:
                self.holes = []
            self.holes.append((a, e, repl, "comment", before))
            self._hold_check(e)
            return None
        if self.drop_ws:
            repl = b""
        o = self.out.pos
        self.out.write(repl)
        self._log("comment", a, e - a, o, len(repl), before, _show(repl))
        self.copy_from = e
        return None

    def _bom(self, p: int) -> Any:
        buf = self.buf
        n = len(buf)
        if p + 3 > n:
            if not self.final and _BOM.startswith(buf[p:]):
                self.p = p
                return NEED
            return _NOT_BOM
        if buf[p + 1] != 0xBB or buf[p + 2] != 0xBF:
            return _NOT_BOM
        a = self.base + p
        self._need("bom", a)
        self.p = p + 3
        if self.hold:
            if self.holes is None:
                self.holes = []
            self.holes.append((a, a + 3, b"", "bom", "\\ufeff"))
            self._hold_check(a + 3)
            return None
        if not self.drop_ws:
            self._flush(a)
        self._log("bom", a, 3, self.out.pos, 0, "\\ufeff", "")
        self.copy_from = a + 3
        return None

    def _trailing(self, p: int) -> None:
        """Junk after the root value (or on a JSON Lines line after its values): dropped to the end."""
        a = self.base + p
        out = self.out
        start = self.comma if (self.mode == "line" and self.comma >= 0) else a
        self._need("trailing_data", start)
        if self.hold:   # keep the gap before the junk (its comments are reported), drop the rest
            self._flush(self.hold_from)
            self._emit_gap(start, None)
            self.hold = False
            self.holes = None
        elif self.f_comma_out >= 0:
            deferred, self.defer = self.defer, None
            out.patch(self.f_comma_out, b" ")
            self._log_at(self.f_comma_loc, "trailing_comma", 1, self.f_comma_out, 1, ",", " ")
            self.defer = deferred
            self._undefer()
            self._flush(a)
            start = a
        else:
            self._flush(a)
        self.comma = -1
        self.f_comma_out = -1
        self.f_insert = False
        self.t_start = start
        self.t_out = out.pos
        self.copy_from = a
        self.st = S_SKIP
        self.p = p
        return None

    def _skip(self) -> int | None:
        n = len(self.buf)
        self.p = n
        end = self.base + n
        self.copy_from = end
        if not self.final:
            return NEED
        start = self.t_start
        self.cur_record = None
        # the preview ends with the removed span (a JSON Lines line: the following, kept lines are not shown)
        self._log("trailing_data", start, end - start, self.t_out, 0,
                  _show(self._src(start, min(end, start + PREVIEW_BYTES))), "")
        self.st = S_END
        return self._eof()

    # -- brace-list root look-ahead ---------------------------------------------------------------------
    def _la_skip(self, i: int, end: int) -> int:
        """Skip whitespace, comments and BOMs from ``i`` (look-ahead, nothing recorded); ``-1`` when the view
        ends inside them."""
        buf = self.buf
        while True:
            i = _WS_MATCH(buf, i, end).end()
            if i >= end:
                return i
            c = buf[i]
            if c == 0x2F:
                if i + 1 >= end:
                    return -1
                c1 = buf[i + 1]
                if c1 == 0x2F:
                    m = _LINE_END(buf, i + 2, end)
                    if m is None:
                        return -1
                    i = m.start()
                    continue
                if c1 == 0x2A:
                    j = buf.find(b"*/", i + 2, end)
                    if j < 0:
                        return -1
                    i = j + 2
                    continue
                return i
            if c == 0xEF and buf.startswith(_BOM, i):
                i += 3
                continue
            return i

    def _brace_lookahead(self, p: int) -> bool | None:
        """Is the ``{`` at ``p`` the root of a brace list (``{`` followed by a value, or by a string followed
        by ``,`` / ``}``)? ``None``: more data needed. Undecided within LOOKAHEAD_MAX bytes -> an object."""
        buf = self.buf
        n = len(buf)
        end = min(n, p + LOOKAHEAD_MAX)
        undecided: bool | None = False if (self.final or end == p + LOOKAHEAD_MAX) else None
        i = self._la_skip(p + 1, end)
        if i < 0 or i >= end:
            return undecided
        c = buf[i]
        if c == 0x7D:
            return False
        if c == 0x22 or c == 0x27:
            m = (_LA_DQ if c == 0x22 else _LA_SQ)(buf, i, end)
            if m is None:
                return undecided
            j = self._la_skip(m.end(), end)
            if j < 0 or j >= end:
                return undecided
            return buf[j] in b",}"
        if c in _KEY_START:
            m = _IDENT(buf, i, end)
            if m is None:
                return False
            j = self._la_skip(m.end(), end)
            if j < 0 or j >= end:
                return undecided
            return buf[j] != 0x3A
        return c in b"{[-+0123456789."


# ----------------------------------------------------------------------------------------------------
# the driver
# ----------------------------------------------------------------------------------------------------
class _RestartPass(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _NoWrite(Exception):
    """Abandon the atomic write (an unrepairable problem without ``allow_partial``)."""


class _ListSink:
    """In-memory sink (detection on a cleaned head)."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.n = 0

    def write(self, data: bytes) -> None:
        self.parts.append(data)
        self.n += len(data)

    def finish(self) -> None:
        pass


def _work_dir(settings: Settings) -> Path:
    from .appdirs import work_dir

    return work_dir(getattr(settings, "cache_dir", None))


class _Job:
    """One repair: the passes over the data file (module docstring "Architecture")."""

    def __init__(self, source: Path, target: Path | None, prepared: Any, data_size: int, info: Any,
                 opts: RepairOptions, settings: Settings, progress: Progress | None, cancel: CancelToken | None,
                 check_only: bool) -> None:
        self.source = source
        self.target = target
        self.prepared = prepared
        self.info = info
        self.opts = opts
        self.settings = settings
        self.progress = progress
        self.cancel = cancel
        self.check_only = check_only
        self.data_path = Path(prepared.data_path)
        self.data_start = int(info.data_start)
        self.data_size = int(data_size)
        self.kind = info.kind
        self.path: tuple = tuple(info.record_path) if (info.kind == "wrapped" and info.record_path) else ()
        self.auto = bool(info.auto)
        self.backend, self.Scanner, explicit = _scanner()
        self.accelerate = bool(opts.accelerate) and (explicit or self.backend != "python")
        self.window = int(opts.window) if opts.window else None
        self.enabled = frozenset(FIX_CODES) if opts.fixes is None else frozenset(opts.fixes)
        self.nan_string = opts.nan == "string"
        self.f = open_shared(self.data_path, "rb", buffering=0)
        self.wbase = 0
        self.wdata = b""
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.end = self._effective_end()
        self.output_kind = ""
        self.out: _Output | None = None
        self.sink: _Sink | None = None
        self.validator: _Validator | None = None
        self.fixlog: _FixLog | None = None
        self.lines: _Lines | None = None
        self.ctx: _Ctx | None = None
        self.records = 0
        self.truncation: Truncation | None = None
        self.problem: RepairProblem | None = None
        self.partial = False
        self.target_found = False
        self.conv_open = -1
        self.restarted: str | None = None
        self.reported = False

    # -- reading --------------------------------------------------------------------------------------
    def _read(self, a: int, b: int) -> bytes:
        b = min(b, self.data_size)
        if b <= a:
            return b""
        wb = self.wbase
        if wb <= a and b <= wb + len(self.wdata):
            return self.wdata[a - wb:b - wb]
        return read_range(self.f, a, b)

    def _load(self, a: int, b: int) -> bytes:
        """Read the window ``[a, b)`` (kept for the copies that follow)."""
        data = read_range(self.f, a, b)
        self.wbase = a
        self.wdata = data
        return data

    def _chunk(self, a: int, b: int) -> bytes:
        """The bytes ``[a, b)``: a slice of the current window when it holds them (a scanner restart right
        after a tokenizer run, a tokenizer run inside the scanner's window), else read as the new window."""
        wb = self.wbase
        if wb <= a and b <= wb + len(self.wdata):
            return self.wdata[a - wb:b - wb]
        return self._load(a, b)

    def _byte(self, o: int) -> int:
        data = self._read(o, o + 1)
        return data[0] if data else -1

    def _first_byte(self) -> int:
        pos = self.data_start
        while pos < self.end:
            chunk = self._read(pos, min(self.end, pos + 64 * KiB))
            s = chunk.lstrip(_WS_BYTES)
            if s:
                return s[0]
            pos += len(chunk)
        return -1

    def _root_at(self) -> int:
        pos = self.data_start
        while pos < self.end:
            chunk = self._read(pos, min(self.end, pos + 64 * KiB))
            s = chunk.lstrip(_WS_BYTES)
            if s:
                return pos + len(chunk) - len(s)
            pos += len(chunk)
        return -1

    def _last_non_ws(self, end: int) -> int:
        pos = end
        while pos > self.data_start:
            a = max(self.data_start, pos - 64 * KiB)
            chunk = self._read(a, pos)
            s = chunk.rstrip(_WS_BYTES)
            if s:
                return a + len(s) - 1
            pos = a
        return -1

    def _has_newline(self, a: int, b: int) -> bool:
        while a < b:
            stop = min(b, a + MiB)
            if b"\n" in self._read(a, stop):
                return True
            a = stop
        return False

    def _effective_end(self) -> int:
        """The end of the data without NUL padding (decision 8)."""
        end = self.data_size
        start = self.data_start
        pos = end
        tail = start
        while pos > start:
            a = max(start, pos - 64 * KiB)
            chunk = self._read(a, pos)
            s = chunk.rstrip(b"\x00 \t\r\n")
            if s:
                tail = a + len(s)
                break
            pos = a
        pos = tail
        while pos < end:
            stop = min(end, pos + MiB)
            i = self._read(pos, stop).find(b"\x00")
            if i >= 0:
                return pos + i
            pos = stop
        return end

    def _progress(self, pos: int) -> None:
        if self.progress is not None:
            self.progress.update(max(0, pos - self.data_start))

    def _concat_kind(self, root_start: int, root_end: int, nxt: int) -> str:
        """Decision 5: ``jsonl`` when the root value is one line and a line break follows it, else
        ``stream`` (the reader's ``format_hint`` rule)."""
        if root_start < 0 or root_end < 0:
            return "stream"
        if not self._has_newline(root_start, root_end) and self._has_newline(root_end, nxt):
            return "jsonl"
        return "stream"

    # -- one pass ---------------------------------------------------------------------------------------
    def _output_kind(self) -> str:
        k = self.kind
        if self.opts.output_kind == "jsonl":
            if k not in ("stream", "jsonl"):
                raise UserInputError("repair.error.output_kind", kind=t(f"reader.kind.{k}"))
            return "jsonl"
        if k in ("brace_list", "stream"):
            return "array"
        return k

    def _setup(self, f: BinaryIO | None) -> None:
        self.output_kind = self._output_kind()
        ok = self.output_kind
        if ok == "jsonl":
            mode, target = MODE_LINES, []
        elif ok == "single":
            mode, target = MODE_SINGLE, []
        else:
            mode, target = MODE_CONTAINER, list(self.path)
        self.validator = _Validator(self.Scanner, mode, target, self.cancel)
        gz = f is not None and self.target is not None and self.target.name.lower().endswith(".gz")
        self.sink = _Sink(f, gz, self.validator)
        wd = self.settings
        self.out = _Output(self.sink, lambda: _work_dir(wd))
        jsonl_out = self.kind == "stream" and ok == "jsonl"
        self.out.nl_space = jsonl_out
        self.fixlog = _FixLog(lambda: _work_dir(wd), self.opts.max_items)
        enc = self.prepared.encoding
        self.lines = _Lines(self._read, self.data_start, utf16=enc.startswith("utf-16"),
                            gzip=self.prepared.compression is not None, source_bom=int(self.prepared.source_bom))
        concat = self._concat_kind if (self.auto and self.kind not in ("stream", "jsonl")) else None
        self.ctx = _Ctx(self.out, self.fixlog, self.lines, enabled=self.enabled, nan_string=self.nan_string,
                        end=self.end, read=self._read, jsonl_out=jsonl_out, concat_kind=concat)
        self.records = 0
        self.truncation = None
        self.problem = None
        self.partial = False
        self.target_found = False
        self.conv_open = -1

    def _close_spills(self) -> None:
        ctx = self.ctx
        if ctx is not None:
            for d in ctx.spills:
                d.close()
            ctx.spills.clear()

    def _teardown(self) -> None:
        if self.validator is not None:
            self.validator.abort()
            self.validator = None
        if self.out is not None:
            self.out.close()
        if self.fixlog is not None:
            self.fixlog.discard()
        self._close_spills()

    def close(self) -> None:
        try:
            if self.validator is not None:
                self.validator.abort()
                self.validator = None
            if self.out is not None:
                self.out.close()
            if self.fixlog is not None and not self.reported:
                self.fixlog.discard()
            self._close_spills()
        finally:
            try:
                self.f.close()
            except OSError:  # pragma: no cover
                pass

    def run(self, f: BinaryIO | None) -> None:
        """The repair pass (repeated once as another kind on a late discovery, decision 5)."""
        while True:
            self._setup(f)
            try:
                self._pass()
                return
            except _RestartPass as exc:
                if self.restarted is not None:  # pragma: no cover - stream / jsonl never restart
                    raise
                log.info("%s: a value follows the root value; repairing again as %s", self.source.name, exc.kind)
                self._teardown()
                if f is not None:
                    f.seek(0)
                    f.truncate()
                self.restarted = exc.kind
                self.kind = exc.kind
                self.path = ()
                self.warnings.append(("repair.warn.restarted", {"kind": t(f"reader.kind.{exc.kind}")}))

    def _pass(self) -> None:
        self._bom_fix()
        if self.problem is None:
            if self.kind == "jsonl":
                self._run_lines()
            else:
                self._run_doc()
        if self.problem is None and self.end < self.data_size:
            self._padding_fix()

    def _fix(self, code: str, a: int, length: int, o: int, olen: int, before: str, after: str,
             record: int | None = None, reason: str = "") -> None:
        assert self.lines is not None and self.fixlog is not None
        line, col, user = self.lines.locate(a)
        self.fixlog.add(RepairFix(code, FIX_CATEGORY[code], user, line, col, o, record, before, after, length, olen,
                                  reason))

    def _bom_fix(self) -> None:
        p = self.prepared
        bom = int(p.source_bom) if p.converted else self.data_start
        if not bom:
            return
        if "bom" not in self.enabled:
            self._set_problem(_Problem("disabled", 0, detail="bom"), partial=False)
            return
        assert self.fixlog is not None
        self.fixlog.add(RepairFix("bom", "syntax", 0, 1, 1, 0, None, "\\ufeff", "", self.data_start, 0))

    def _padding_fix(self) -> None:
        a = self.end
        if "trailing_data" not in self.enabled:
            self._set_problem(_Problem("disabled", a, detail="trailing_data"), partial=True)
            if self.truncation is None:      # (a cut-off document keeps its own truncation)
                self.truncation = self._truncation((a, self.records, self.data_size - a, -1, ""))
            self._dropped_tail(a, "", "disabled")
            return
        assert self.out is not None
        self._fix("trailing_data", a, self.data_size - a, self.out.pos, 0,
                  _show(self._read(a, min(self.data_size, a + PREVIEW_BYTES))), "")

    # -- problems -----------------------------------------------------------------------------------
    def _context(self, o: int) -> tuple[str, str]:
        o = min(o, self.data_size)
        lo = max(self.data_start, o - CONTEXT_BYTES)
        hi = min(self.data_size, o + CONTEXT_BYTES)
        before = _decode_suffix(self._read(lo, o)) if lo < o else ""
        after = _decode_prefix(self._read(o, hi), hi >= self.data_size) if o < hi else ""
        return before, after

    def _truncation(self, raw: tuple[int, int, int, int, str]) -> Truncation:
        assert self.lines is not None
        cut, kept, dropped, partial, closers = raw
        line, col, user = self.lines.locate(cut)
        pstart = pline = None
        if partial >= 0:
            pl, _pc, pu = self.lines.locate(partial)
            pstart, pline = pu, pl
        return Truncation(user, line, col, kept, dropped, pstart, closers, pline)

    def _set_problem(self, exc: _Problem, *, partial: bool) -> None:
        assert self.lines is not None
        line, col, user = self.lines.locate(exc.offset)
        before, after = self._context(exc.offset)
        self.problem = RepairProblem(exc.code, user, line, col, before, after, exc.expected, exc.detail)
        self.partial = partial

    def _dropped_tail(self, cut: int, closers: str, reason: str) -> None:
        """The partial result before an unrepairable problem drops everything from ``cut`` (the last commit
        point) to the end of the file -- NUL padding included -- and appends ``closers`` (just written): one
        ``dropped_tail`` fix whose ``reason`` is the problem code (decision 11). Logged after the rollback to
        the commit point, so it is the last entry; whether the result is written or not, the report tells
        what it holds."""
        assert self.out is not None
        olen = len(closers)
        self._fix("dropped_tail", cut, self.data_size - cut, self.out.pos - olen, olen,
                  _show(self._read(cut, min(self.data_size, cut + PREVIEW_BYTES))), closers, reason=reason)

    def _tok_problem(self, tok: _Tokenizer) -> None:
        exc = tok.problem
        assert exc is not None
        partial = exc.code != "nothing_complete" and not (tok.mode == "doc" and tok.m_depth == 0
                                                           and tok.root_end < 0)
        if partial:
            tok._cut(False)
            assert tok.truncation is not None
            self.truncation = self._truncation(tok.truncation)
            self._dropped_tail(tok.truncation[0], tok.truncation[4], exc.code)
        else:
            tok._rollback()
            self.truncation = None
        self.records = tok.records
        self._set_problem(exc, partial=partial)

    # -- documents (array / keyed / wrapped / single / brace_list / stream) ---------------------------
    def _tokenize(self, mode: str, start: int, sync: _Sync | None, stop_after: int | None) -> tuple[int, _Tokenizer]:
        assert self.ctx is not None and self.out is not None
        self.conv_open = -1
        tok = _Tokenizer(self.ctx, mode, kind=self.kind, path=self.path, start=start, sync=sync,
                         stop_after=stop_after, records=sync.records if sync is not None else 0)
        pos = start
        end = self.end
        window = self.window
        if window:
            size = window
        elif stop_after is None:
            size = TOKEN_FEED
        else:   # a broken record is usually short: read just past the error first, more when needed
            size = min(TOKEN_FEED, max(TOKEN_START, stop_after + TOKEN_START - start))
        while True:
            check(self.cancel)
            stop = min(end, pos + size)
            data = self._chunk(pos, stop) if stop > pos else b""
            st = tok.feed(data, stop >= end)
            pos = stop
            self._progress(pos)
            if st != NEED:
                break
            if not window and size < TOKEN_FEED:
                size = min(TOKEN_FEED, 2 * size)
        self.records = tok.records
        if tok.target_found:
            self.target_found = True
        if st == RESTART:
            raise _RestartPass(tok.restart or "stream")
        if st == PROBLEM:
            self._tok_problem(tok)
        elif st == DONE and tok.truncation is not None:
            self.truncation = self._truncation(tok.truncation)
        self.out.commit()
        return st, tok

    def _template(self) -> Any:
        """The frames at the record level as far as the format tells (``None``: let the tokenizer find
        them; ``"scalar"``: a one-value document without sync points)."""
        kind = self.kind
        if kind == "stream":
            return []
        if kind == "wrapped":
            return None
        if kind == "single":
            c = self._first_byte()
            if c == 0x5B:
                return [_Frame(ARR, commit=True, sync=True)]
            if c == 0x7B:
                return [_Frame(OBJ, commit=True, sync=True)]
            return "scalar"
        k = {"array": ARR, "keyed": OBJ, "brace_list": BRACE}.get(kind)
        if k is None:  # pragma: no cover - every kind is listed
            return None
        return [_Frame(k, commit=True, records=True, sync=True)]

    def _prefix(self, frames: list[_Frame]) -> bytes:
        """The synthetic prefix recreating the nesting at a sync point (after a record)."""
        parts = []
        last = len(frames) - 1
        for i, f in enumerate(frames):
            parts.append(b"[" if f.kind == ARR else b"{")
            if i < last:
                seg = self.path[i]
                if f.kind == OBJ:
                    parts.append(json.dumps(seg, ensure_ascii=True).encode("ascii") + b":")
                else:
                    parts.append(b"null," * int(seg))
            else:
                parts.append(b'"":null' if f.kind == OBJ else b"null")
        return b"".join(parts)

    def _run_doc(self) -> None:
        mode = "stream" if self.kind == "stream" else "doc"
        pos = self.data_start
        if (not self.accelerate or self.end <= pos or (self.kind == "stream" and "concatenated" not in self.enabled)
                or (self.kind == "brace_list" and "brace_list" not in self.enabled)):
            self._tokenize(mode, pos, None, None)
            return
        template = self._template()
        sync: _Sync | None = None
        if template is None:
            st, tok = self._tokenize(mode, pos, None, pos - 1)
            if st != STOPPED:
                return
            pos = tok.stop_at
            sync = tok.sync()
            template = sync.frames
        dense = 0
        dense_until = -1
        while True:
            res = self._scan(pos, sync, template)
            if res is None:
                return
            r, sync_r, e = res
            if sync is not None and e - pos < DENSE_NEAR:
                dense += 1
                if dense >= DENSE_FAILS:
                    dense_until = e + DENSE_SPAN
                    dense = 0
            else:
                dense = 0
            st, tok = self._tokenize(mode, r, sync_r, max(e, dense_until))
            if st != STOPPED:
                return
            pos = tok.stop_at
            sync = tok.sync()
            template = sync.frames

    def _scan(self, pos: int, sync: _Sync | None, template: Any) -> tuple[int, _Sync | None, int] | None:
        """A strict-scanner phase from the sync point ``pos``: records it validates are copied. Returns
        ``None`` at a clean end, else ``(last record end, its sync state, error offset)``."""
        out = self.out
        assert out is not None
        kind = self.kind
        frames = sync.frames if sync is not None else template
        scalar = isinstance(frames, str)
        stream = kind == "stream"
        if scalar:
            mode, target, prefix = MODE_SINGLE, [], b""
        elif stream:
            mode, target, prefix = MODE_STREAM, [], b""
        elif frames[-1].kind == BRACE:
            mode, target, prefix = MODE_BRACE_LIST, [], (b"{null" if sync is not None else b"")
        else:
            mode, target = MODE_CONTAINER, list(self.path)
            prefix = self._prefix(frames) if sync is not None else b""
        brace = not scalar and bool(frames) and frames[0].kind == BRACE
        self.conv_open = self._root_at() if (brace and sync is None) else -1
        sc = self.Scanner(mode, target, 0, MAX_DEPTH)
        plen = len(prefix)
        if plen:
            sc.feed(prefix)
        shift = pos - plen
        jsonl_out = out.nl_space
        count = kind != "single"
        records0 = sync.records if sync is not None else 0
        values = sync.values if sync is not None else 0
        n_new = 0
        copied = last = pos
        prev_e = pos
        fed = pos
        end = self.end
        maxw = self.window or (WINDOW_MAX if self.backend == "rust" else WINDOW_MAX_PY)
        window = self.window or WINDOW_START
        finished = False
        # A span is used only once the scanner accepted a byte after it: a record ending with a number or a
        # literal right before the error may continue there for the tokenizer (``[01]``, ``[truex]``, the
        # number cut by the end of the data), so the tokenizer re-reads it.
        pend: list[tuple[int, int]] = []
        while True:
            check(self.cancel)
            if fed < end:
                stop = min(end, fed + window)
                spans_raw = sc.feed(self._chunk(fed, stop))
                fed = stop
                if window < maxw:
                    window = min(maxw, window * 2)
                self._progress(fed)
            else:
                spans_raw = sc.finish()
                finished = True
            err_off = -1 if sc.error is None else int(sc.error[0]) + shift
            if scalar:
                if err_off >= 0:
                    return self.data_start, None, err_off
                if finished:
                    break
                continue
            if spans_raw:
                spans = array("Q")
                spans.frombytes(spans_raw)
                for i in range(0, len(spans), 2):
                    er = spans[i + 1]
                    if er > plen:
                        pend.append((spans[i] + shift, er + shift))
            if pend:
                take = len(pend)
                if err_off >= 0:
                    while take and (pend[take - 1][1] > err_off or (pend[take - 1][1] == err_off
                                                                    and self._byte(err_off - 1) not in _SELF_DELIM)):
                        take -= 1
                elif not finished and pend[-1][1] >= fed:
                    take -= 1
                if take:
                    done, pend = pend[:take], pend[take:]
                    if stream:
                        for s, e in done:
                            if values == 0:
                                if jsonl_out:
                                    self._fix("concatenated", s, 0, out.pos, 0, "", "JSONL")
                                else:
                                    self._copy(copied, s)
                                    o = out.pos
                                    out.write(b"[")
                                    self._fix("concatenated", s, 0, o, 1, "", "[")
                            elif jsonl_out:
                                pass
                            elif s - prev_e <= HOLD_MAX:
                                out.write(b",")
                                self._copy(prev_e, s)
                            else:
                                self._copy(prev_e, s)
                                out.write(b",")
                            self._copy(s, e)
                            if jsonl_out:
                                out.write_raw(b"\n")
                            copied = prev_e = last = e
                            values += 1
                        n_new += take
                        self.records = records0 + n_new
                    else:
                        e = done[-1][1]
                        self._copy(copied, e)
                        copied = last = e
                        n_new += take
                        if count:
                            self.records = records0 + n_new
                    out.commit()
            if err_off >= 0:
                if n_new == 0:
                    return last, sync, err_off
                prev = self._byte(last - 1) in _SELF_DELIM
                rec = records0 + (n_new if count else 0)
                return last, _Sync([f.copy() for f in frames], rec, values, prev,
                                   sync.path_active if sync is not None else False,
                                   sync.target_found if sync is not None else False,
                                   sync.root_start if sync is not None else self._root_at()), err_off
            if finished:
                break
        # clean end of the data
        if stream:
            if jsonl_out:
                pass
            elif values == 0:
                self._copy(copied, end)
                o = out.pos
                out.write(b"[]")
                self._fix("concatenated", end, 0, o, 2, "", "[")
            elif end - prev_e <= HOLD_MAX:
                out.write(b"]")
                self._copy(prev_e, end)
            else:
                self._copy(prev_e, end)
                out.write(b"]")
        elif brace:
            close_at = self._last_non_ws(end)
            self._copy(copied, close_at)
            out.write(b"]")
            self._copy(close_at + 1, end)
        else:
            self._copy(copied, end)
        if kind == "single":
            self.records = 1
        if sync is not None and sync.target_found:
            self.target_found = True
        out.commit()
        return None

    def _copy(self, a: int, b: int) -> None:
        """Copy the validated source range ``[a, b)`` (the brace-list root ``{`` becomes ``[``)."""
        if b <= a:
            return
        co = self.conv_open
        if a <= co < b:
            self._copy_raw(a, co)
            assert self.out is not None
            o = self.out.pos
            self.out.write(b"[")
            self._fix("brace_list", co, 1, o, 1, "{", "[")
            self.conv_open = -1
            a = co + 1
        self._copy_raw(a, b)

    def _copy_raw(self, a: int, b: int) -> None:
        """Verbatim copy of validated (final) data: committed at once, so it never waits in memory."""
        out = self.out
        lines = self.lines
        assert out is not None and lines is not None
        while a < b:
            wb = self.wbase
            we = wb + len(self.wdata)
            if wb <= a < we:
                stop = min(b, we)
                data = self.wdata[a - wb:stop - wb]
            else:
                stop = min(b, a + 8 * MiB)
                if a < wb < stop:
                    stop = wb
                check(self.cancel)
                data = read_range(self.f, a, stop)
            out.commit()
            out.write_final(data)
            lines.skip(a, data)
            a = stop

    # -- JSON Lines ------------------------------------------------------------------------------------
    def _run_lines(self) -> None:
        out = self.out
        assert out is not None
        pos = self.data_start
        end = self.end
        if not self.accelerate:
            while pos < end and self.problem is None:
                pos = self._line(pos)
            return
        sc = self.Scanner(MODE_LINES, [], 0, MAX_DEPTH)
        shift = pos
        copied = pos
        fed = pos
        maxw = self.window or (WINDOW_MAX if self.backend == "rust" else WINDOW_MAX_PY)
        window = self.window or WINDOW_START
        finished = False
        while True:
            check(self.cancel)
            if fed < end:
                stop = min(end, fed + window)
                spans_raw = sc.feed(self._chunk(fed, stop))
                fed = stop
                if window < maxw:
                    window = min(maxw, window * 2)
                self._progress(fed)
            else:
                spans_raw = sc.finish()
                finished = True
            broken = sc.take_broken()
            spans = array("Q")
            spans.frombytes(spans_raw)
            ends = spans[1::2]
            j = 0
            for line_no, ls_rel, le_rel, _off, _code, _exp in broken:
                ls = ls_rel + shift
                while j < len(ends) and ends[j] + shift <= ls:
                    j += 1
                    self.records += 1
                self._copy(copied, ls)
                out.commit()
                copied = self._line(ls)
                if self.problem is not None:
                    return
            self.records += len(ends) - j
            if finished:
                break
        self._copy(copied, end)
        out.commit()

    def _line(self, ls: int) -> int:
        """Repair the JSON Lines line starting at ``ls``; returns the offset after its line feed."""
        out = self.out
        fixlog = self.fixlog
        lines = self.lines
        ctx = self.ctx
        assert out is not None and fixlog is not None and lines is not None and ctx is not None
        m_out = out.pos
        m_fix = fixlog.state()
        m_lines = lines.state
        tok = _Tokenizer(ctx, "line", kind="jsonl", start=ls, records=self.records)
        end = self.end
        window = self.window
        size = window or TOKEN_START
        pos = ls
        st = NEED
        while True:
            check(self.cancel)
            stop = min(end, pos + size)
            data = self._chunk(pos, stop) if stop > pos else b""
            i = data.find(b"\n")
            if i >= 0:
                data = data[:i]
            last = i >= 0 or stop >= end
            if st == NEED:
                st = tok.feed(data, last)
            if i >= 0:
                le = pos + i
                nxt = le + 1
                has_nl = True
                break
            pos = stop
            self._progress(pos)
            if stop >= end:
                le = nxt = end
                has_nl = False
                break
            if not window and size < TOKEN_FEED:
                size = min(TOKEN_FEED, 2 * size)
        self._progress(nxt)
        if st == DONE:
            if has_nl:
                out.write_raw(b"\n")
            self.records = tok.records
            out.commit()
            return nxt
        exc = tok.problem
        assert exc is not None
        out.truncate_to(m_out)
        fixlog.rollback(m_fix)
        lines.state = m_lines
        if exc.code == "not_utf8" or "line_dropped" not in self.enabled:
            self.truncation = self._truncation((ls, self.records, end - ls, ls, ""))
            self._dropped_tail(ls, "", exc.code)     # (offsets are located in increasing order)
            self._set_problem(exc, partial=True)
            return nxt
        self._fix("line_dropped", ls, nxt - ls, out.pos, 0, _show(self._read(ls, min(le, ls + PREVIEW_BYTES))), "",
                  reason=exc.code)
        out.commit()
        return nxt

    # -- completion ------------------------------------------------------------------------------------
    def complete(self) -> None:
        """End of the pass: hand everything to the sink, check the output, check the source."""
        out, sink, validator = self.out, self.sink, self.validator
        assert out is not None and sink is not None
        out.commit()
        out.flush()
        sink.finish()
        if validator is not None:
            if self.problem is not None and not self.partial:
                validator.abort()
            else:
                err, broken, recs = validator.finish()
                ok = err is None and broken is None and recs == self.records
                if not ok and err is not None and err[1] == "target_not_found" and self.records == 0 \
                        and not self.target_found:
                    ok = True
                if not ok:
                    detail = (f"{err[1]} @ {err[0]}" if err is not None else
                              f"line {int(broken[0]) + 1}: {broken[4]}" if broken is not None else
                              f"{recs} records, {self.records} expected")
                    log.error("repair of %s: invalid output (%s)", self.source, detail)
                    raise JsonReshapeError("repair.error.invalid_output", detail=detail)
            self.validator = None
        grown = appended_bytes(self.source, self.prepared.identity)
        if grown is None:
            raise FileChangedError(path=str(self.source))
        if grown > 0:
            self.warnings.append(("reader.warn.appended", {"bytes": format_bytes(grown)}))

    def report(self, written: bool) -> RepairReport:
        fixlog = self.fixlog
        assert fixlog is not None and self.sink is not None
        self.reported = True
        rep = RepairReport(
            source=self.source, target=self.target, kind=self.kind, output_kind=self.output_kind,
            encoding=self.prepared.encoding, compression=self.prepared.compression,
            bytes_in=int(self.prepared.identity.size), bytes_out=self.sink.n, records=self.records,
            counts=dict(fixlog.counts), fixes=list(fixlog.ram),
            first={code: fx for code, (_i, fx) in fixlog.first.items()},
            truncated=self.truncation, problem=self.problem, written=written,
            record_path=self.path or None, check_only=self.check_only, backend=self.backend,
            accelerated=self.accelerate,
            data_path=Path(self.prepared.data_path) if self.prepared.converted else None)
        rep.fixes_file = fixlog.finish()
        for key in self.prepared.warnings:
            rep.add_warning(key)
        for key, params in self.warnings:
            rep.add_warning(key, **params)
        enc = self.prepared.encoding
        if enc.startswith("utf-16"):
            from .fileio import encoding_label

            rep.add_warning("repair.warn.transcoded", encoding=encoding_label(enc))
        if written and self.target is not None:
            gz_out = self.target.name.lower().endswith(".gz")
            if self.prepared.compression == "gzip" and not gz_out:
                rep.add_warning("repair.warn.uncompressed")
            elif gz_out and self.prepared.compression != "gzip":
                rep.add_warning("repair.warn.compressed")
        if self.path and not self.target_found:
            rep.add_warning("repair.warn.record_path_missing", path=_path_text(self.path))
        elif self.ctx is not None and self.ctx.path_scalar:
            rep.add_warning("repair.warn.record_path_missing", path=_path_text(self.path))
        return rep


def _path_text(path: tuple) -> str:
    from .paths import format_path

    return format_path(path) if path else "$"


# ----------------------------------------------------------------------------------------------------
# format detection (decision 6)
# ----------------------------------------------------------------------------------------------------
def _head_valid(prepared: Any, data_start: int, data_size: int, settings: Settings) -> bool:
    """The head of the data is strict JSON (values one after another; a head cut inside a value counts as
    valid): the reader's detection can be trusted."""
    head_bytes = int(getattr(settings, "head_bytes", 4 * MiB))
    stop = min(data_size, data_start + head_bytes)
    _name, scanner_cls, _explicit = _scanner()
    sc = scanner_cls(MODE_STREAM, [], 0)
    with open_shared(prepared.data_path, "rb", buffering=0) as f:
        pos = data_start
        while pos < stop and sc.error is None:
            nxt = min(stop, pos + 4 * MiB)
            sc.feed(read_range(f, pos, nxt))
            pos = nxt
    if sc.error is None and stop >= data_size:
        sc.finish()
        if sc.error is not None and sc.error[1] == "unexpected_eof":
            return True
    return sc.error is None


def _clean_head(prepared: Any, data_start: int, data_size: int, settings: Settings) -> bytes:
    """The head of the data as the tokenizer repairs it (every complete value kept, open containers
    closed, nothing guessed): what the reader's detection then looks at."""
    head_bytes = int(getattr(settings, "head_bytes", 4 * MiB))
    stop = min(data_size, data_start + head_bytes)
    with open_shared(prepared.data_path, "rb", buffering=0) as f:
        head = read_range(f, data_start, stop)
    sink = _ListSink()
    out = _Output(sink, lambda: _work_dir(settings))  # type: ignore[arg-type]
    try:
        ctx = _Ctx(out, None, None, enabled=frozenset(FIX_CODES), nan_string=False, end=stop,
                   read=lambda a, b: head[a - data_start:b - data_start])
        tok = _Tokenizer(ctx, "clean", start=data_start)
        if tok.feed(head, True) == PROBLEM:
            tok._cut(False)
        out.commit()
        out.flush()
    finally:
        out.close()
    return b"".join(sink.parts)


def _detect(source: Path, prepared: Any, data_size: int, opts: RepairOptions, settings: Settings,
            cancel: CancelToken | None) -> tuple[Any, bool]:
    """The format used for the repair: ``(FormatInfo, from the cleaned head)`` (decision 6)."""
    from .reader import FormatInfo, detect_format

    forced = opts.format is not None or opts.record_path is not None
    info = None
    error: JsonReshapeError | None = None
    try:
        info = detect_format(source, record_path=opts.record_path, format=opts.format, settings=settings,
                             cancel=cancel)
    except FileFormatError as exc:
        if exc.key == "reader.empty_file":
            raise
        error = exc
    data_start = info.data_start if info is not None else int(prepared.bom_len)
    if info is not None:
        if forced or info.kind == "array" or (info.kind == "jsonl" and info.auto and _jsonl_name(source)):
            return info, False
        if _head_valid(prepared, data_start, data_size, settings):
            return info, False
    check(cancel)
    cleaned = _clean_head(prepared, data_start, data_size, settings)
    if not cleaned.strip(_WS_BYTES):
        # no root value can be found at all: the forced format is used as given and the tokenizer reports
        # the problem at its place (a forced format that contradicts a real root stays an error, below)
        if info is not None:
            return info, False
        forced_info = _forced_info(opts, prepared, data_start)
        if forced_info is not None:
            return forced_info, False
        assert error is not None
        raise error
    suffix = ".jsonl" if _jsonl_name(source) else ".json"
    tmp = _work_dir(settings) / f"repair-head-{os.getpid()}-{secrets.token_hex(4)}{suffix}"
    try:
        with open(long_path(tmp), "wb") as f:
            f.write(cleaned)
        try:
            info2 = detect_format(tmp, record_path=opts.record_path, format=opts.format, settings=settings,
                                  cancel=cancel)
        except (FileFormatError, UserInputError):
            if info is not None:
                return info, False
            assert error is not None
            raise error from None
    finally:
        try:
            os.remove(long_path(tmp))
        except OSError:
            log.debug("could not remove %s", tmp, exc_info=True)
    if info is not None and info2.kind == info.kind and info2.record_path == info.record_path:
        return info, False
    merged = FormatInfo(kind=info2.kind, encoding=prepared.encoding, compression=prepared.compression,
                        record_path=info2.record_path, candidates=list(info2.candidates), auto=info2.auto,
                        data_start=data_start, container_kind=info2.container_kind)
    for key in prepared.warnings:
        merged.add_warning(key)
    for i, key in enumerate(info2.warnings):
        merged.add_warning(key, **(info2.warning_params[i] if i < len(info2.warning_params) else {}))
    return merged, True


def _forced_info(opts: RepairOptions, prepared: Any, data_start: int) -> Any:
    """A FormatInfo of exactly the forced options (``None`` when nothing is forced)."""
    from .paths import parse_path
    from .reader import KINDS, FormatInfo

    fmt = (opts.format or "").strip().lower()
    rp = opts.record_path
    if rp is not None:
        path = tuple(parse_path(rp)) if isinstance(rp, str) else tuple(rp)
        if path:
            return FormatInfo(kind="wrapped", encoding=prepared.encoding, compression=prepared.compression,
                              record_path=path, auto=False, data_start=data_start)
        fmt = fmt or "array"
    if fmt in ("", "auto") or fmt not in KINDS or fmt == "wrapped":
        return None
    return FormatInfo(kind=fmt, encoding=prepared.encoding, compression=prepared.compression, auto=False,
                      data_start=data_start)


def _jsonl_name(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    return name.endswith((".jsonl", ".ndjson", ".jsonlines"))


# ----------------------------------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------------------------------
def _check_options(opts: RepairOptions) -> None:
    if opts.nan not in ("null", "string"):
        raise UserInputError("repair.error.option", name="nan", value=str(opts.nan))
    if opts.output_kind not in (None, "jsonl"):
        raise UserInputError("repair.error.option", name="output_kind", value=str(opts.output_kind))
    if opts.fixes is not None:
        unknown = set(opts.fixes) - set(FIX_CODES)
        if unknown:
            raise UserInputError("repair.error.option", name="fixes", value=", ".join(sorted(unknown)))
    if opts.max_items is None or int(opts.max_items) < 0:
        raise UserInputError("repair.error.option", name="max_items", value=str(opts.max_items))
    if opts.window is not None and int(opts.window) < 1:
        raise UserInputError("repair.error.option", name="window", value=str(opts.window))


def _same_file(a: Path, b: Path) -> bool:
    try:
        if normalize_path(a) == normalize_path(b):
            return True
    except (OSError, ValueError):  # pragma: no cover
        pass
    try:
        return os.path.samefile(long_path(a), long_path(b))
    except (OSError, ValueError):
        return False


def _purge_stale(settings: Settings) -> None:
    """Remove temporary repair files (fix lists, spills, cleaned heads) older than a day."""
    try:
        d = _work_dir(settings)
        now = time.time()
        for entry in os.scandir(long_path(d)):
            name = entry.name
            if not name.startswith((FIXES_PREFIX, "repair-spill-", "repair-head-", "repair-defer-")):
                continue
            try:
                if now - entry.stat().st_mtime > STALE_FIXES_SECONDS:
                    os.remove(entry.path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - housekeeping is best effort
        log.debug("purging stale repair files failed", exc_info=True)


def repair_file(source: StrPath, target: StrPath | None, opts: RepairOptions | None = None, *,
                check_only: bool = False, settings: Settings | None = None, progress: Progress | None = None,
                cancel: CancelToken | None = None) -> RepairReport:
    """Repair ``source`` into ``target`` and report every change (module docstring).

    ``check_only``: run everything, write nothing (``target`` may be ``None``); the report then has
    ``problem`` set instead of raising. Otherwise an unrepairable problem raises
    :class:`UnrepairableError` (``.report``) and nothing is written -- unless ``opts.allow_partial``, which
    writes the records before it (``report.problem`` and ``report.truncated`` set). ``target`` must not be
    the source (``writer.error.same_file``); it is replaced atomically, gzip-compressed when its name ends
    with ``.gz``. Progress: bytes of the data file (gzip / UTF-16 sources are prepared first, with progress
    in source bytes); ``cancel`` -> :class:`~jsonreshape.errors.Cancelled`, nothing written. A source that
    changed meanwhile -> :class:`~jsonreshape.errors.FileChangedError`, nothing written.
    """
    opts = opts if opts is not None else RepairOptions()
    _check_options(opts)
    settings = settings or get_settings()
    src = Path(os.path.abspath(os.fspath(source)))
    tgt = None if target is None else Path(os.path.abspath(os.fspath(target)))
    if not check_only:
        if tgt is None:
            raise UserInputError("repair.error.no_target")
        if _same_file(src, tgt):
            raise UserInputError("writer.error.same_file", path=os.fspath(target))  # type: ignore[arg-type]
    started = time.perf_counter()
    _purge_stale(settings)
    check(cancel)
    prepared = prepare_data_file(src, settings=settings, progress=progress, cancel=cancel)
    retained = prepared.converted
    if retained:
        retain_prepared(prepared.data_path)
    try:
        data_size = os.stat(long_path(prepared.data_path)).st_size if prepared.converted else prepared.identity.size
        info, _cleaned = _detect(src, prepared, data_size, opts, settings, cancel)
        if progress is not None:
            progress.set_total(max(0, data_size - info.data_start))
            progress.update(0)
            progress.set_message(t("repair.progress.check" if check_only else "repair.progress.repair",
                                   name=src.name))
        job = _Job(src, tgt, prepared, data_size, info, opts, settings, progress, cancel, check_only)
        try:
            written = False
            if check_only:
                job.run(None)
                job.complete()
            else:
                try:
                    with atomic_write(tgt) as f:  # type: ignore[arg-type]
                        job.run(f)
                        if job.problem is not None and not (opts.allow_partial and job.partial):
                            raise _NoWrite
                        job.complete()
                    written = True
                except _NoWrite:
                    written = False
            report = job.report(written)
        finally:
            job.close()
    finally:
        if retained:
            release_prepared(prepared.data_path)
    report.elapsed = time.perf_counter() - started
    if progress is not None:
        progress.finish()
    log.info("repair of %s (%s): %d fixes, %d records%s%s in %.2f s", src.name, report.kind, report.total,
             report.records, "; truncated" if report.truncated is not None else "",
             f"; problem {report.problem.code}" if report.problem is not None else "", report.elapsed)
    if report.problem is not None and not report.written and not check_only:
        raise UnrepairableError(report)
    return report
