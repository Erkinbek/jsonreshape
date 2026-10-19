"""File tools: split one file into parts, merge several files into one, extract values into a file
(docs/ARCHITECTURE.md §14.4; the design and measurements are in the stage-5 analysis
``s5/map_convert.md`` §3-§5; AGENT_PROMPT §10 "Bo'lish va birlashtirish").

Split -- :func:`plan_split`, :func:`split`
=========================================
A document (all its records, a ``range`` of them, or any selection such as a filtered or sorted
view) is cut into parts of consecutive records, each written as its own file.

* **Planning** (:func:`plan_split`, cheap: it reads the index only): exactly one of
  ``records`` (records per part: boundaries ``0, R, 2R, ..., n``), ``parts`` (``N`` parts,
  balanced by bytes -- the boundary ``k`` is the first record starting at or after ``k/N`` of
  the bytes of the records, found by bisection; ``balance="records"``: ``n*k//N``; always exactly
  ``N`` non-empty parts, ``N > n`` is refused with ``filetools.error.too_many_parts``) or ``size``
  (bytes per part, greedy: the most records whose part -- head and tail of the document included --
  fits; a record larger than the size gets a part of its own, ``filetools.warn.oversized``).
  Bytes are UTF-8 data bytes: exact for an unedited UTF-8 document read in file order, an
  estimate otherwise (UTF-16 / gzip files, edited documents and views, where record sizes plus
  the record separator are summed in windows of 65 536 positions -- O(n) for those).
* **Engine A, the same kind** (no ``export`` options, the whole document or a ``range`` of it):
  every part is written by :func:`jsonreshape.saver.write_part` -- byte for byte like Save As,
  validated by the core Scanner while it is written, in the document's kind, encoding (BOM,
  UTF-16) and compression: array / keyed / wrapped / brace_list (written as an array) parts keep
  the document's own head and tail (the wrapper in every part; ``keep_wrapper=False``: the record
  container alone); JSON Lines and concatenated values take the file head in the first part, the
  file tail in the last one and the original gap after every other part's last record, so the
  parts of an unedited file laid end to end ARE the file. An edited document (pinned) is split as
  it is shown (its unsaved changes included).
* **Engine B, the writer** (``export`` options given -- another format, an indent, sorted keys --,
  or a selection that is not a ``range``): every part is :func:`jsonreshape.writer.export` of the
  part's slice of the selection; CSV/XLSX parts share ONE column plan (:func:`writer.plan_columns`)
  so every part has the same header; an XLSX part that exceeds ``xlsx_max_rows`` continues on new
  sheets (``xlsx_split`` "files" is not used inside a part).
* **Names**: ``<stem>_<k><ext>`` next to the input (``out_dir``), ``k`` 1-based and zero-padded to
  the digits of the part count (``_1 .. _9``, ``_01 .. _10``); double extensions are kept
  (``x.jsonl.gz`` -> ``x_01.jsonl.gz``); brace-list parts get ``.json``; engine B parts get the
  extension of their format (``.json``, ``.jsonl``, ``.csv``, ``.xlsx``, ``.sqlite``; ``.gz`` added
  for JSON / JSON Lines / CSV when the input is gzip). ``name``: a pattern with ``{stem}``, ``{n}``
  (required) and ``{ext}`` only (``filetools.error.name_pattern``). Compression follows the part
  name (``.gz``), like Save As; a gzip input named without ``.gz`` gets ``.gz`` added to its parts.
  :func:`part_names` returns these files before a split (the same code decides them).
* **Before any work**: the document must be complete (``filetools.error.partial``: repair it
  first), not a single value (``filetools.error.single``: open it with a record path), JSON Lines
  broken lines need ``drop_broken`` (``filetools.error.broken_lines``); no part may be the input or
  its data file (also under another name), a folder or a read-only file; the folder must exist;
  the free space must hold the estimated output plus 64 MiB (``filetools.error.disk_space``).
* **Writing and committing, all or nothing**: each part goes to a temporary file
  ``.<name>.<hex>.jrpart`` in the output folder (never ``.tmp``, which the cache eviction removes;
  leftovers of a crash older than a day are swept), fsynced; then ``doc.ensure_unchanged()`` and
  :func:`jsonreshape.fileio.commit_files` put every part in place at once (existing parts are
  renamed aside first -- a part this program still shows in a tab is replaced too, unless it has
  unsaved edits: decision 12 -- and restored on any failure). An error or a cancellation removes
  every temporary file and leaves the existing files as they were. Files that match the name
  pattern but are not parts of this result (an earlier split into more parts, another padding)
  are left alone and named -- with their count and what not to do with them -- in
  ``filetools.warn.stale_parts`` (``SplitReport.stale``); ``SplitOptions.remove_stale`` deletes
  the ones that are the rest of an earlier split in the same commit (decision 11).
* **Progress**: one bar in bytes over the estimated output (engine A: UTF-8 bytes written; engine
  B: the parts' shares), message ``filetools.progress.part`` ("k/n · name"); the source is checked
  against changes every 64 MiB.

Merge -- :func:`merge`
======================
Several documents (each opened by the caller; an open tab's pinned snapshot is fine) are written
into ONE JSON or JSON Lines file by :func:`jsonreshape.writer.export_many`: records in input
order, then file order; one Reformatter (exact tokens, no ``\\u`` escaping, ``indent`` /
``sort_keys`` as for convert); an object root only when every input has member records (keyed /
wrapped-object), else an array (member records become ``{"key": value}``); the first input's
wrapper is kept when its record container is of the root's kind. Other output formats are refused
(``filetools.error.merge_format``: merge to JSON Lines, then convert). Partial inputs are refused
unless ``allow_partial``. ``dedupe_by`` (a concrete path, ``$`` = the whole record, ``$key`` = the
member key) removes duplicates across all inputs with :func:`jsonreshape.dupes.find_duplicates_many`
(``keep="first"``: the earliest input wins; ``"last"``); the writer then reads every input through
its ascending "kept positions" source.

Extract -- :func:`extract`
==========================
* no ``path`` / ``node``: the records of the document without their wrapper (the document is
  opened with the record path of the array to extract -- ``convert --record-path P --no-wrapper``);
  every export format;
* ``node``: one node (a :class:`~jsonreshape.reader.Node` or a location text ``#5.orders``,
  ``/data/5/orders``, ``data[5].orders`` -- :func:`resolve_node`) written by
  :func:`jsonreshape.writer.export_node` (JSON: re-indented bytes; JSON Lines: one line per array
  element);
* ``path``: a record-relative pattern (``orders[*]``, ``contact.address``, ``tags[*]``, ``*``,
  ``**.id``) gathered from every record of ``source``: values in record order, then document order
  (``orders`` without ``[*]`` writes each record's array as one value). Values are copied byte for
  byte from :class:`core.PathExtractor` spans (no parsing; dict semantics: the last of duplicate
  keys); ``**`` patterns parse the records (values re-encoded: exact numbers, escapes normalized).
  Huge records (larger than ``Settings.large_node_bytes``): patterns of keys, indices, ``[*]`` and
  ``*`` walk the record's node tree and copy the values' bytes (streamed, any size; ``*`` keeps dict
  semantics -- distinct keys in the order of their first appearance, the last value -- for objects of
  up to :data:`WALK_MAX_MEMBERS` members, bigger objects are written member by member without that
  check, a known limit); ``**`` patterns use :func:`jsonreshape.stream.stream_values` within the
  capture budget (its documented ijson spellings: ``1e400`` -> ``1E+400``, ``-0`` -> ``0``) -- a record
  whose values exceed the budget is skipped (``filetools.warn.extract_too_large``).
* ``node`` / ``path`` write JSON or JSON Lines only (``filetools.error.extract_format``).

Record selections -- :func:`parse_select`
========================================
The CLI ``--select SPEC``: 0-based record numbers ``N``, half-open ranges ``A:B`` (``A`` .. ``B-1``,
like Python slices), ``A:`` (to the end), ``:B`` (from the start), separated by commas (kept in the
given order; an optional ``#`` before a number, as the GUI shows them). ``B`` beyond the end is cut
to the record count; any other number outside the document, an empty range or a syntax error raises
a localized :class:`~jsonreshape.errors.UserInputError`.

Decisions where §14.4 / map_convert leave room (all chosen to keep the user's data safe)
========================================================================================
1. ``parts`` always gives exactly ``N`` non-empty parts (a byte boundary that would leave a part
   empty moves by one record); more than :data:`MAX_PARTS` parts are refused
   (``filetools.error.too_many_files``).
2. A document without records cannot be split (``filetools.error.no_records``).
3. Engine A is used for the whole document and for ``range`` selections; any other selection (a
   view) uses engine B with the document's own format (JSON with its indentation; JSON Lines for
   JSON Lines and concatenated values) when no ``export`` options are given.
4. A part's compression follows its name, like Save As (default names keep ``.gz``).
5. Engine B refuses partial documents and broken JSON Lines lines exactly like engine A.
6. :class:`SplitReport` combines the warnings of the parts' exports (engine B) by key: the counts
   of all parts are added up (``ExportReport.warning_params``; the other parameters -- the first
   record named -- of the first part giving the warning); the warnings about the document itself
   (partial, broken lines, the wrapper not written), which every part gives alike, appear once;
   "columns not found" is reported only when no part found the columns.
7. Merge ``sources``: every input is merged whole (no per-input selection in v1); ``keep="none"``
   makes no sense for a merge and is refused.
8. Extract ``node`` ignores ``sort_keys`` (the node is copied token for token).
9. A name pattern may not contain characters that are not portable in file names (``< > : " | ? *``
   and control characters; on Windows ``:`` would name an NTFS stream) or a folder separator.
10. The free-space check scales the estimated output by the source's compression ratio when the
    parts are gzip-compressed (a 5 GB ``.jsonl.gz`` is not refused for its 50 GB of JSON).
11. Stale parts (fix round R5, R5F-9): old parts that complete the numbering of a newer split look
    like one set (12 parts, then 10: ``x_11``, ``x_12`` stay), so a merge of ``x_*`` would count
    records twice. They are never touched by default; the warning names them (all, or the first six
    and the last), with their count, and says not to merge them with the new parts.
    ``SplitOptions(remove_stale=True)`` deletes them in the same all-or-nothing commit
    (``fileio.commit_files(remove=...)``, listed in ``SplitReport.removed`` and
    ``filetools.warn.stale_removed``) -- but only files that are the rest of an earlier split: per
    number width the numbers ``lo .. M`` without a gap (``lo`` = this split's count + 1 for its own
    width, else 1; ``M`` padded to its own digits; at least two files); never the source, a read-only
    file or a file with unsaved edits. Anything else matching the pattern (``x_2023.json``, a lone
    ``x_1.json``) may be the user's own file: it stays and is named in the warning.
12. Unsaved edits (fix round R5, R5F-3): a part, a stale part to remove or a merge / extract output
    that has an edit layer of this program -- live in a tab or another instance, or pending for
    recovery (``editstore.find_pending`` for its path) -- is never replaced or removed:
    ``filetools.error.part_open_edited`` names it, before any part is written and again right before
    the commit. (Replacing it made the edits impossible to save: ``FileChangedError``.) A file shown
    in a tab without edits is replaced as before; the tab sees the change. The rule is
    :func:`jsonreshape.writer.edited_targets` (fix round L1: every writer output follows it; the layer
    index is read once with :func:`jsonreshape.editstore.layer_sources`).
"""

from __future__ import annotations

import bisect
import dataclasses
import logging
import os
import re
import string
import time
from array import array
from dataclasses import dataclass, field
from itertools import accumulate, chain
from operator import sub
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, Union

from . import codec, core, editops, fileio, saver, writer
from .errors import FileChangedError, JsonDecodeError, UserInputError
from .fileio import long_path, normalize_path
from .i18n import has, t
from .paths import Wild, parse_location, parse_pattern
from .reader import APPENDED_WARNING
from .stream import CaptureLimitExceeded, capture_budget, stream_values
from .tasks import CancelToken, Progress, check, format_bytes
from .writer import ExportOptions, ExportReport

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]

__all__ = [
    "BALANCES",
    "MAX_PARTS",
    "PART_SUFFIX",
    "MergeOptions",
    "MergeReport",
    "SplitOptions",
    "SplitPart",
    "SplitReport",
    "extract",
    "merge",
    "parse_select",
    "part_names",
    "plan_split",
    "resolve_node",
    "split",
]

KiB = 1 << 10
MiB = 1 << 20

BALANCES = ("bytes", "records")
KEEPS = ("first", "last")
PART_SUFFIX = ".jrpart"           # temporary files of parts (never ".tmp": the cache eviction removes those)
STALE_PART_SECONDS = 86400.0      # leftover part temps of a crashed split older than this are removed
DISK_RESERVE = 64 * MiB           # free space kept beyond the estimated output
CHANGE_CHECK_BYTES = 64 * MiB     # the source is checked against changes every this many output bytes
PLAN_WINDOW = 65536               # positions whose sizes are summed together (views, edited documents)
PLAN_CACHE = 4                    # windows of sizes kept while bisecting
MAX_PARTS = 100_000               # parts of one split, at most
DEFAULT_NAME = "{stem}_{n}{ext}"
NAME_FIELDS = frozenset({"stem", "n", "ext"})
EXTRACT_CHUNK = 1 * MiB           # node bytes read (and fed to the Reformatter) at a time
NODE_BATCH = 4096                 # children of a huge record's array fetched at a time
WALK_MAX_MEMBERS = 200_000        # "*" in a huge record: objects up to this size are checked for repeated keys
_SELECT_RE = re.compile(r"#?\s*(?P<n>\d+)|(?:#?\s*(?P<a>\d+))?\s*:\s*(?:#?\s*(?P<b>\d+))?")
_NAME_BAD_RE = re.compile(r'[<>:"|?*\x00-\x1f]')   # not portable in file names (":" makes an NTFS stream)
_EXT_OF = {"json": ".json", "jsonl": ".jsonl", "csv": ".csv", "xlsx": ".xlsx", "sqlite": ".sqlite"}
_DECODE_ERRORS = (JsonDecodeError, RecursionError, ValueError)


# ----------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------
def _tn(key: str, /, **params: Any) -> str:
    """:func:`t` with ``<key>.one`` when ``count`` is 1 and that key exists."""
    if params.get("count") == 1 and has(key + ".one"):
        return t(key + ".one", **params)
    return t(key, **params)


class _Warnings:
    """Counted warnings (localized at the end; a count of 1 uses ``<key>.one``) and warnings that
    come localized already (the reports of parts, of a deduplication)."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}

    def add(self, key: str, add: int = 1, first: dict[str, Any] | None = None, **params: Any) -> None:
        """Count ``key``: ``first`` holds parameters of its first occurrence, ``params`` are
        updated every time."""
        entry = self.entries.get(key)
        if entry is None:
            entry = self.entries[key] = {"count": 0}
            if first:
                entry.update(first)
        elif "_texts" in entry:          # (given as a text before: that text stays)
            return
        entry["count"] = int(entry.get("count", 0)) + add
        entry.update(params)

    def add_text(self, key: str, text: str, *, every: bool = False) -> None:
        """A warning already localized: the first text of ``key`` (``every``: every distinct text
        of it, in order -- e.g. one per input of a merge)."""
        entry = self.entries.get(key)
        if entry is None:
            self.entries[key] = {"_texts": [text]}
        elif every and "_texts" in entry and text not in entry["_texts"]:
            entry["_texts"].append(text)

    def texts(self) -> tuple[list[str], list[str]]:
        """``(texts, keys)`` in the order the keys were first given (a key given with several
        texts appears once per text)."""
        texts: list[str] = []
        keys: list[str] = []
        for k, entry in self.entries.items():
            for text in entry["_texts"] if "_texts" in entry else (_tn(k, **entry),):
                texts.append(text)
                keys.append(k)
        return texts, keys


def _pinned(doc: Any) -> Any:
    pinned = getattr(doc, "pinned", None)
    return pinned() if callable(pinned) else doc


def _check_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise UserInputError("filetools.error.option", name=name, value=repr(value))
    return value


def _positive(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise UserInputError("filetools.error.option", name=name, value=repr(value))
    return int(value)


def _check_complete(doc: Any, cancel: CancelToken | None, progress: Progress | None = None) -> None:
    """Index complete (built here when needed); ``doc.error`` -> ``filetools.error.partial``."""
    if not doc.index_complete:
        doc.build_index(progress=progress, cancel=cancel)
    err = doc.error
    if err is not None:
        raise UserInputError("filetools.error.partial", line=getattr(err, "line", 0), column=getattr(err, "column", 0))


def _commit_check(doc: Any, warnings: _Warnings) -> None:
    """Right before the outputs are committed: the source is the version read (a source that only
    grew is fine: the snapshot opened was written; ``reader.warn.appended``)."""
    grown = fileio.appended_bytes(doc.source_path, doc.identity)
    if grown is None:
        raise FileChangedError(path=str(doc.source_path))
    doc.ensure_unchanged()
    if grown:
        warnings.add(APPENDED_WARNING, add=0, bytes=format_bytes(grown))


def _space_needed(doc: Any, estimate: int, finals: list[Path], engine: str) -> int:
    """Free bytes a split needs: the estimated UTF-8 output (twice for a UTF-16 copy), scaled by the
    source's compression ratio when the parts are gzip-compressed too, plus :data:`DISK_RESERVE`."""
    need = estimate
    if engine == "same" and str(doc.info.encoding).startswith("utf-16"):
        need *= 2
    if finals and all(writer.gzip_path(p) for p in finals) and str(doc.info.compression or "") == "gzip":
        need = need * max(1, int(doc.size)) // max(1, int(doc.data_size))
    return need + DISK_RESERVE


def _check_space(folder: Path, need: int) -> None:
    free = fileio.free_space(folder)
    if free is not None and free < need:
        raise UserInputError("filetools.error.disk_space", need=format_bytes(need), free=format_bytes(free),
                             path=str(folder))


_PathDoc = writer._PathDoc       # (moved to the writer in fix round L1: every writer output has the rule)


def _edited_files(paths: Sequence[Path]) -> list[Path]:
    """The existing files of ``paths`` that have unsaved edits in this program -- a live or pending edit layer
    (decision 12): :func:`jsonreshape.writer.edited_targets` (the layer index read once through
    :func:`jsonreshape.editstore.layer_sources`, :func:`jsonreshape.editstore.find_pending` for the files it
    lists)."""
    return writer.edited_targets(paths)


def _refuse_edited(paths: Sequence[Path]) -> None:
    """``filetools.error.part_open_edited`` naming the files of ``paths`` that have unsaved edits
    (:func:`jsonreshape.writer.refuse_edited_targets`)."""
    writer.refuse_edited_targets(paths)


# ----------------------------------------------------------------------------------
# record selections (the CLI --select)
# ----------------------------------------------------------------------------------
def parse_select(spec: str, count: int) -> range | array:
    """Record positions of ``spec`` (module docstring "Record selections") for a document of
    ``count`` records: a ``range`` for one item, else an ``array('Q')`` of the items in order."""
    if not isinstance(spec, str) or not spec.strip():
        raise UserInputError("filetools.error.select_syntax", spec=str(spec), item="")
    count = max(0, int(count))
    last = count - 1
    ranges: list[range] = []
    for raw in spec.split(","):
        item = raw.strip()
        m = _SELECT_RE.fullmatch(item) if item else None
        if m is None:
            raise UserInputError("filetools.error.select_syntax", spec=spec.strip(), item=item)
        if m.group("n") is not None:
            pos = int(m.group("n"))
            if pos >= count:
                raise UserInputError("filetools.error.select_range", item=item, count=count, last=last)
            ranges.append(range(pos, pos + 1))
            continue
        a = int(m.group("a")) if m.group("a") is not None else 0
        b = min(int(m.group("b")), count) if m.group("b") is not None else count
        if count == 0 or (m.group("a") is not None and a >= count):
            raise UserInputError("filetools.error.select_range", item=item, count=count, last=last)
        if a >= b:
            raise UserInputError("filetools.error.select_empty", item=item)
        ranges.append(range(a, b))
    if len(ranges) == 1:
        return ranges[0]
    out = array("Q")
    for r in ranges:
        out.extend(r)
    return out


# ----------------------------------------------------------------------------------
# split: options, report
# ----------------------------------------------------------------------------------
def _check_pattern(pattern: str) -> str:
    """A part name pattern: ``{stem}``, ``{n}`` (required) and ``{ext}`` only, no folder."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise UserInputError("filetools.error.name_pattern", pattern=str(pattern))
    try:
        parsed = list(string.Formatter().parse(pattern))
    except ValueError:
        raise UserInputError("filetools.error.name_pattern", pattern=pattern) from None
    names = set()
    for _text, name, spec, conv in parsed:
        if name is None:
            continue
        if name not in NAME_FIELDS or spec or conv:
            raise UserInputError("filetools.error.name_pattern", pattern=pattern)
        names.add(name)
    if "n" not in names or "/" in pattern or "\\" in pattern or _NAME_BAD_RE.search(pattern):
        raise UserInputError("filetools.error.name_pattern", pattern=pattern)
    return pattern


@dataclass
class SplitOptions:
    """How :func:`split` cuts a document (module docstring "Split").

    * exactly one of ``parts`` (number of parts), ``size`` (bytes per part) and ``records``
      (records per part); ``balance``: ``"bytes"`` (default) or ``"records"`` for ``parts``;
    * ``out_dir``: the folder of the parts (default: the input's folder); ``name``: a pattern with
      ``{stem}``, ``{n}``, ``{ext}`` (default ``{stem}_{n}{ext}``);
    * ``keep_wrapper``: a wrapped document's wrapper in every part (``False``: the record
      container alone); ``drop_broken``: JSON Lines broken lines are left out (else refused);
    * ``export``: write the parts with these writer options (another format, an indent, sorted
      keys; then ``export.keep_wrapper`` decides about the wrapper); ``None`` = the document's own
      kind, byte for byte (engine A);
    * ``remove_stale``: delete, in the same all-or-nothing commit, the files of the name pattern
      that are the rest of an earlier split (module docstring "Split", decision 11); default
      ``False``: they are only named in a warning.

    A part written in the document's own kind holds at most ``size`` bytes (UTF-8, its head and
    tail included) unless a single record is bigger; a JSON Lines part may exceed it by the line
    ends after its last record.

    Values are validated on creation (``UserInputError("filetools.error.split_mode")`` /
    ``filetools.error.option`` / ``filetools.error.name_pattern``)."""

    parts: int | None = None
    size: int | None = None
    records: int | None = None
    balance: str = "bytes"
    out_dir: StrPath | None = None
    name: str | None = None
    keep_wrapper: bool = True
    drop_broken: bool = False
    export: ExportOptions | None = None
    remove_stale: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        given = [k for k in ("parts", "size", "records") if getattr(self, k) is not None]
        if len(given) != 1:
            raise UserInputError("filetools.error.split_mode")
        setattr(self, given[0], _positive(given[0], getattr(self, given[0])))
        balance = str(self.balance).strip().lower() if isinstance(self.balance, str) else self.balance
        if balance not in BALANCES:
            raise UserInputError("filetools.error.option", name="balance", value=repr(self.balance))
        self.balance = balance
        if self.name is not None:
            self.name = _check_pattern(self.name)
        self.keep_wrapper = _check_bool("keep_wrapper", self.keep_wrapper)
        self.drop_broken = _check_bool("drop_broken", self.drop_broken)
        self.remove_stale = _check_bool("remove_stale", self.remove_stale)
        if self.export is not None:
            if not isinstance(self.export, ExportOptions):
                raise TypeError("export must be an ExportOptions instance or None")
            self.export.validate()


@dataclass
class SplitPart:
    """One written part: its file, the selection position of its first record, its records and
    the bytes of the file."""

    path: Path
    first: int
    records: int
    bytes: int


@dataclass
class SplitReport:
    """What :func:`split` wrote: the parts in order, records and bytes in all, the warnings
    (localized; ``warning_keys`` their keys), ``stale``: files matching the name pattern that are
    not parts of this result (left unchanged); ``removed``: the parts of an earlier split deleted
    (``SplitOptions.remove_stale``); ``engine``: ``"same"`` (byte copy in the document's
    kind) or ``"export"`` (the writer); ``fmt``: the parts' format (the document's kind for engine
    A); ``rows`` / ``columns`` / ``tables`` / ``skipped``: as the writer reports them (engine B)."""

    parts: list[SplitPart]
    records: int
    bytes_written: int
    elapsed: float
    warnings: list[str] = field(default_factory=list)
    warning_keys: list[str] = field(default_factory=list)
    stale: list[Path] = field(default_factory=list)
    engine: str = "same"
    fmt: str = "json"
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    skipped: int = 0
    removed: list[Path] = field(default_factory=list)

    @property
    def mb_per_s(self) -> float:
        return self.bytes_written / self.elapsed / 1e6 if self.elapsed > 0 else 0.0

    @property
    def files(self) -> list[Path]:
        return [p.path for p in self.parts]

    def to_export_report(self) -> ExportReport:
        """The report as an :class:`~jsonreshape.writer.ExportReport` (the export report dialog,
        the CLI summary): every part in ``files``."""
        fmt = self.fmt if self.fmt in writer.FORMATS else "json"
        return ExportReport(fmt=fmt, records=self.records, rows=self.rows or self.records,
                            bytes_written=self.bytes_written, files=self.files, warnings=list(self.warnings),
                            elapsed=self.elapsed, skipped=self.skipped, columns=list(self.columns),
                            tables=list(self.tables), warning_keys=list(self.warning_keys))


# ----------------------------------------------------------------------------------
# split: planning
# ----------------------------------------------------------------------------------
class _FileMetric:
    """Byte offsets of the records ``p0 .. p0+n-1`` of an UNEDITED document, measured from the first
    one's start (exact: the gaps between them included). Random access through the index."""

    def __init__(self, doc: Any, p0: int, n: int) -> None:
        self.doc = doc
        self.p0 = p0
        self.n = n
        self.s0 = int(doc.record_span(p0)[0]) if n else 0
        self.total = self.hi(n - 1) if n else 0

    def lo(self, i: int) -> int:
        return int(self.doc.record_span(self.p0 + i)[0]) - self.s0

    def hi(self, i: int) -> int:
        return int(self.doc.record_span(self.p0 + i)[1]) - self.s0

    def first_lo_at_least(self, x: int) -> int:
        return bisect.bisect_left(range(self.n), x, key=self.lo)

    def last_hi_at_most(self, x: int) -> int:
        return bisect.bisect_right(range(self.n), x, key=self.hi) - 1


class _SumMetric:
    """Byte offsets of the ``n`` selected records estimated as the sum of the record sizes plus the
    record separator (an edited document, a view): ``fetch(i, k)`` gives the flat spans of the
    selection positions ``i .. i+k-1``. One pass over all windows of :data:`PLAN_WINDOW` positions
    sums them (``bases``); a lookup reloads one window (a few are cached)."""

    def __init__(self, n: int, fetch: Callable[[int, int], array], sep: int, cancel: CancelToken | None) -> None:
        self.n = n
        self.fetch = fetch
        self.sep = sep
        self.cancel = cancel
        self._cache: dict[int, tuple[list[int], array]] = {}
        bases = [0]
        for w0 in range(0, n, PLAN_WINDOW):
            check(cancel)
            sizes = self._sizes(w0)
            bases.append(bases[-1] + sum(sizes) + sep * len(sizes))
        self.bases = bases
        self.total = max(0, bases[-1] - sep) if n else 0

    def _sizes(self, w0: int) -> array:
        sp = self.fetch(w0, min(self.n, w0 + PLAN_WINDOW) - w0)
        return array("Q", map(sub, sp[1::2], sp[0::2]))

    def _window(self, w: int) -> tuple[list[int], array]:
        hit = self._cache.get(w)
        if hit is None:
            check(self.cancel)
            sizes = self._sizes(w * PLAN_WINDOW)
            sep = self.sep
            starts = list(accumulate((s + sep for s in sizes[:-1]), initial=self.bases[w]))
            hit = (starts, sizes)
            if len(self._cache) >= PLAN_CACHE:
                self._cache.pop(next(iter(self._cache)))
            self._cache[w] = hit
        return hit

    def lo(self, i: int) -> int:
        starts, _sizes = self._window(i // PLAN_WINDOW)
        return starts[i % PLAN_WINDOW]

    def hi(self, i: int) -> int:
        starts, sizes = self._window(i // PLAN_WINDOW)
        k = i % PLAN_WINDOW
        return starts[k] + sizes[k]

    def first_lo_at_least(self, x: int) -> int:
        """The first position whose start is at or after ``x`` (``n`` when none)."""
        w = max(0, bisect.bisect_right(self.bases, x) - 1)
        nwin = len(self.bases) - 1
        while w < nwin:
            starts, _sizes = self._window(w)
            k = bisect.bisect_left(starts, x)
            if k < len(starts):
                return w * PLAN_WINDOW + k
            w += 1
        return self.n

    def last_hi_at_most(self, x: int) -> int:
        """The last position whose end is at or before ``x`` (-1 when none)."""
        nwin = len(self.bases) - 1
        w = min(nwin - 1, max(0, bisect.bisect_right(self.bases, x) - 1))
        while w >= 0:
            starts, sizes = self._window(w)
            ends = list(map(int.__add__, starts, sizes))
            k = bisect.bisect_right(ends, x) - 1
            if k >= 0:
                return w * PLAN_WINDOW + k
            w -= 1
        return -1


@dataclass
class _Selection:
    """What a split reads: ``engine`` ``"same"`` (positions ``p0 .. p0+n-1`` of the document) or
    ``"export"`` (``source`` positions ``0 .. n-1`` of a view / the document)."""

    doc: Any
    source: Any
    n: int
    p0: int
    engine: str


def _whole(doc: Any, source: Any) -> bool:
    """``source`` selects every record of ``doc`` in file order (``None`` or the document's base set)."""
    if source is None:
        return True
    is_base = getattr(source, "_is_base_of", None)
    return bool(callable(is_base) and is_base(doc))


def _engine_of(doc: Any, source: Any, opts: SplitOptions) -> str:
    """``"same"`` (engine A, the byte copy) for the whole document or a ``range`` of it without export
    options, else ``"export"`` (engine B, the writer) -- decision 3."""
    if opts.export is None and (_whole(doc, source) or (isinstance(source, range) and source.step == 1)):
        return "same"
    return "export"


def _selection(doc: Any, source: Any, opts: SplitOptions) -> _Selection:
    total = int(doc.record_count)
    engine = _engine_of(doc, source, opts)
    if _whole(doc, source):
        return _Selection(doc, None, total, 0, engine)
    if isinstance(source, range) and source.step == 1:
        if len(source) and (source.start < 0 or source.stop > total):
            raise UserInputError("writer.error.bad_record", index=source.stop - 1, count=total)
        if engine == "same":
            return _Selection(doc, source, len(source), source.start, "same")
    src, count = writer._normalize_source(doc, source)
    return _Selection(doc, src, count, 0, "export")


def _contiguous_start(sel: _Selection) -> int | None:
    """The first position when the selection is consecutive positions of the document (the whole
    document or a ``range``), else ``None``."""
    if sel.engine == "same" or sel.source is None:
        return sel.p0
    if isinstance(sel.source, range) and sel.source.step == 1:
        return sel.source.start
    return None


def _metric(sel: _Selection, cancel: CancelToken | None) -> _FileMetric | _SumMetric:
    doc = sel.doc
    start = _contiguous_start(sel)
    if start is not None and not doc.edited:
        return _FileMetric(doc, start, sel.n)
    sep = len(editops.detect_style(doc).record_sep)
    ov = doc.overlay
    if sel.engine == "same":
        eff = getattr(doc, "_eff_spans", None)
        if ov is not None and callable(eff):
            p0 = sel.p0
            return _SumMetric(sel.n, lambda i, k: eff(ov, p0 + i, k), sep, cancel)
        p0 = sel.p0
        return _SumMetric(sel.n, lambda i, k: _spans_of(doc, range(p0 + i, p0 + i + k)), sep, cancel)
    source = sel.source
    if source is None:
        return _SumMetric(sel.n, lambda i, k: _spans_of(doc, range(i, i + k)), sep, cancel)
    return _SumMetric(sel.n, lambda i, k: _spans_of(doc, _slice(source, i, i + k)), sep, cancel)


def _spans_of(doc: Any, positions: Any) -> array:
    """Flat spans of the record positions ``positions`` (one index lookup each)."""
    span = doc.record_span
    return array("Q", chain.from_iterable(map(span, positions)))


def _slice(source: Any, a: int, b: int) -> Any:
    """Selection positions ``a .. b-1`` of ``source`` (a ``range`` slice, an ``array('Q')``)."""
    if source is None:
        return range(a, b)
    if isinstance(source, range):
        return source[a:b]
    if isinstance(source, array) and source.typecode == "Q":
        return source[a:b]
    try:
        part = source[a:b]
    except (TypeError, IndexError, KeyError):
        part = None
    if isinstance(part, (array, range)):
        return part
    if isinstance(part, (list, tuple)):
        return array("Q", part)
    return array("Q", (source[i] for i in range(a, b)))


def _frame_bytes(doc: Any, opts: SplitOptions, engine: str) -> int:
    """Bytes a part writes besides its records (engine A: the document's head and tail; JSON
    Lines: the larger of the file tail and a line end; engine B: 0)."""
    if engine != "same":
        return 0
    base = doc.pinned(None) if callable(getattr(doc, "pinned", None)) else doc
    N = int(base.base_record_count)
    ds, dz = int(doc.info.data_start), int(doc.data_size)
    if not N:
        return 2
    s0, eN = int(base.record_span(0)[0]), int(base.record_span(N - 1)[1])
    if doc.kind in ("jsonl", "stream"):
        return (s0 - ds) + max(dz - eN, len(editops.detect_style(doc).newline))
    if doc.kind == "wrapped" and not opts.keep_wrapper and doc.container_span is not None:
        cs0, cs1 = doc.container_span
        return (s0 - cs0) + (cs1 - eN)
    return (s0 - ds) + (dz - eN)


def _plan(sel: _Selection, opts: SplitOptions, cancel: CancelToken | None,
          warnings: _Warnings | None) -> tuple[list[tuple[int, int]], int]:
    """``(parts, estimated output bytes)`` of the selection."""
    n = sel.n
    if n == 0:
        raise UserInputError("filetools.error.no_records")
    frame = _frame_bytes(sel.doc, opts, sel.engine)
    metric: _FileMetric | _SumMetric | None = None
    if opts.records is not None:
        r = opts.records
        if (n + r - 1) // r > MAX_PARTS:
            raise UserInputError("filetools.error.too_many_files", parts=(n + r - 1) // r, max=MAX_PARTS)
        bounds = list(range(0, n, r)) + [n]
    elif opts.parts is not None:
        parts = opts.parts
        if parts > n:
            raise UserInputError("filetools.error.too_many_parts", parts=parts, count=n)
        if parts > MAX_PARTS:
            raise UserInputError("filetools.error.too_many_files", parts=parts, max=MAX_PARTS)
        if opts.balance == "records" or parts == 1:
            bounds = [n * k // parts for k in range(parts + 1)]
        else:
            metric = _metric(sel, cancel)
            total = metric.total
            bounds = [0]
            for k in range(1, parts):
                check(cancel)
                b = metric.first_lo_at_least(total * k // parts)
                b = max(bounds[-1] + 1, min(b, n - (parts - k)))
                bounds.append(b)
            bounds.append(n)
    else:
        assert opts.size is not None
        metric = _metric(sel, cancel)
        limit = max(0, opts.size - frame)
        bounds = [0]
        a = 0
        oversized = 0
        first_over = -1
        while a < n:
            check(cancel)
            j = metric.last_hi_at_most(metric.lo(a) + limit)
            if j < a:                  # the record alone is bigger than the size
                oversized += 1
                if first_over < 0:
                    first_over = a
                b = a + 1
            else:
                b = j + 1
            bounds.append(b)
            a = b
            if len(bounds) - 1 > MAX_PARTS:
                raise UserInputError("filetools.error.too_many_files", parts=f"> {MAX_PARTS}", max=MAX_PARTS)
        if oversized and warnings is not None:
            warnings.add("filetools.warn.oversized", add=oversized, first={"first": first_over},
                         size=format_bytes(opts.size))
    plan = list(zip(bounds[:-1], bounds[1:]))
    if metric is not None:
        body = metric.total
    else:
        body = _records_bytes(sel)
    return plan, body + frame * len(plan)


def _records_bytes(sel: _Selection) -> int:
    """An O(1) estimate of the bytes of the selected records (progress, free space)."""
    doc = sel.doc
    start = _contiguous_start(sel)
    if start is not None and not doc.edited:
        a, b = start, start + sel.n
        return int(doc.record_span(b - 1)[1]) - int(doc.record_span(a)[0])
    full = int(doc.effective_bytes()) if doc.edited else max(0, int(doc.data_size) - int(doc.info.data_start))
    total = max(1, int(doc.record_count))
    return full * sel.n // total


def plan_split(doc: Any, opts: SplitOptions, source: Any = None, *,
               cancel: CancelToken | None = None) -> list[tuple[int, int]]:
    """The parts :func:`split` would write, as half-open ranges of selection positions (``source``
    as for :func:`split`; ``None`` = all records). Cheap: it reads the index only (O(parts · log n)
    for an unedited document read in file order; O(n) for edited documents and views). Builds the
    index when needed; refuses what :func:`split` refuses about the document (partial, a single
    value, no records) and the plan (more parts than records, too many files). JSON Lines broken
    lines are not refused here (the plan does not depend on them; :func:`split` asks for
    ``drop_broken``)."""
    if not isinstance(opts, SplitOptions):
        raise TypeError("opts must be a SplitOptions instance")
    opts.validate()
    doc = writer._pin(_pinned(doc), source)
    _check_complete(doc, cancel)
    if doc.kind == "single":
        raise UserInputError("filetools.error.single")
    sel = _selection(doc, source, opts)
    plan, _estimate = _plan(sel, opts, cancel, None)
    return plan


def part_names(doc: Any, opts: SplitOptions, source: Any = None, *, count: int) -> list[Path]:
    """The files :func:`split` writes for ``count`` parts (``len(plan_split(doc, opts, source))``) of
    ``doc`` / ``source`` with ``opts``: absolute paths in the output folder, exactly
    ``[part.path for part in split(doc, opts, source).parts]`` -- the rule of the module docstring
    "Names" (``<stem>_<k><ext>`` or ``opts.name``; ``k`` 1-based and zero-padded to the digits of
    ``count``; double extensions kept; brace-list parts ``.json``; ``.gz`` added for a gzip input; the
    writer's parts -- export options, or a selection that is not a ``range`` -- the extension of their
    format), decided by the code :func:`split` uses. Cheap: nothing of the data is read. Raises
    ``filetools.error.name_pattern`` / ``filetools.error.option`` like :func:`split` (and
    ``edits.error.stale_scope`` for a selection of another state). For the CLI's and the GUI's
    "these files exist already" questions (fix round R5, an addition)."""
    if not isinstance(opts, SplitOptions):
        raise TypeError("opts must be a SplitOptions instance")
    opts.validate()
    count = _positive("count", count)
    doc = writer._pin(_pinned(doc), source)
    fmt = _parts_fmt(doc, opts, _engine_of(doc, source, opts))
    return _part_names(doc, opts, fmt, count, _out_dir(doc, opts))[0]


# ----------------------------------------------------------------------------------
# split: names
# ----------------------------------------------------------------------------------
def _stem_ext(name: str) -> tuple[str, str]:
    """``("x", ".jsonl.gz")`` for ``x.jsonl.gz``: a ``.gz`` keeps the extension before it."""
    if name.lower().endswith(".gz") and len(name) > 3:
        stem, ext = os.path.splitext(name[:-3])
        return stem, ext + name[-3:]
    return os.path.splitext(name)


def _default_fmt(doc: Any) -> str:
    """Engine B's format without export options: JSON Lines for JSON Lines and concatenated values, else
    JSON (decision 3)."""
    return "jsonl" if doc.kind in ("jsonl", "stream") else "json"


def _parts_fmt(doc: Any, opts: SplitOptions, engine: str) -> str | None:
    """The writer format of the parts (``None`` for engine A: the document's own kind)."""
    if engine == "same":
        return None
    return opts.export.fmt if opts.export is not None else _default_fmt(doc)


def _out_dir(doc: Any, opts: SplitOptions) -> Path:
    """The folder of the parts: ``opts.out_dir`` (absolute) or the input's folder."""
    if opts.out_dir is not None:
        return Path(os.path.abspath(os.fspath(opts.out_dir)))
    return Path(doc.source_path).parent


def _part_names(doc: Any, opts: SplitOptions, fmt: str | None, count: int,
                out_dir: Path) -> tuple[list[Path], str, str, str]:
    """``(final names, pattern, stem, ext)`` of ``count`` parts written in the writer format ``fmt``
    (``None``: engine A, the document's own kind)."""
    stem, ext = _stem_ext(Path(doc.source_path).name)
    gz_src = str(getattr(doc.info, "compression", None) or "") == "gzip"
    if fmt is None:
        if doc.kind == "brace_list":
            ext = ".json" + (".gz" if ext.lower().endswith(".gz") else "")
        if gz_src and not ext.lower().endswith(".gz"):
            ext += ".gz"
    else:
        ext = _EXT_OF[fmt] + (".gz" if gz_src and fmt in writer.GZIP_FORMATS else "")
    pattern = opts.name or DEFAULT_NAME
    width = len(str(count))
    names = [out_dir / pattern.format(stem=stem, n=str(k).zfill(width), ext=ext) for k in range(1, count + 1)]
    seen = set()
    for p in names:
        key = normalize_path(p)
        if key in seen or not p.name.strip() or p.name in (".", ".."):
            raise UserInputError("filetools.error.name_pattern", pattern=pattern)
        seen.add(key)
    return names, pattern, stem, ext


def _name_regex(pattern: str, stem: str, ext: str) -> re.Pattern[str]:
    """The part names of ``pattern`` with any number (group 1: its digits)."""
    regex = ""
    for text, name, _spec, _conv in string.Formatter().parse(pattern):
        regex += re.escape(text)
        if name == "stem":
            regex += re.escape(stem)
        elif name == "ext":
            regex += re.escape(ext)
        elif name == "n":
            regex += r"(\d+)"
    return re.compile(regex, re.IGNORECASE if os.name == "nt" else 0)


def _earlier_parts(stale: list[Path], rx: re.Pattern[str], count: int) -> list[Path]:
    """The ``stale`` files that are the rest of an earlier split into the same name pattern (decision 11):
    grouped by the width of their numbers (the zero padding), a group must hold exactly the numbers
    ``lo .. M`` -- ``lo`` = ``count + 1`` for the width of this split's numbers (the lower ones are
    replaced now), else 1 --, ``M`` written without extra zeros (a split into ``M`` parts pads to the
    digits of ``M``) and at least two files. Anything else -- a gap, a ``0``, a lone ``x_2023.json``, a
    lone ``x_1.json`` -- may be the user's own file and is left alone."""
    width_new = len(str(count))
    groups: dict[int, list[tuple[int, Path]]] = {}
    for p in stale:
        m = rx.fullmatch(p.name)
        if m is None or not m.groups():
            continue
        digits = m.group(1)
        groups.setdefault(len(digits), []).append((int(digits), p))
    out: list[Path] = []
    for width, items in groups.items():
        items.sort(key=lambda it: it[0])
        nums = [n for n, _p in items]
        top = nums[-1]
        lo = count + 1 if width == width_new else 1
        if len(items) >= 2 and len(str(top)) == width and nums == list(range(lo, top + 1)):
            out.extend(p for _n, p in items)
    return out


def _names_list(paths: Sequence[Path], shown: int = 6) -> str:
    """The names of ``paths``: all of them, or the first ``shown``, ``…`` and the last one."""
    names = [p.name for p in paths]
    if len(names) <= shown + 1:
        return ", ".join(names)
    return ", ".join(names[:shown] + ["\N{HORIZONTAL ELLIPSIS}", names[-1]])


def _stale_parts(out_dir: Path, pattern: str, stem: str, ext: str, finals: list[Path]) -> list[Path]:
    """Files in ``out_dir`` matching the name pattern (any number) that are not ``finals``."""
    rx = _name_regex(pattern, stem, ext)
    mine = {normalize_path(p) for p in finals}
    try:
        names = os.listdir(long_path(out_dir))
    except OSError:
        return []
    out = []
    for name in names:
        m = rx.fullmatch(name)
        if m is None:
            continue
        p = out_dir / name
        if normalize_path(p) in mine or os.path.isdir(long_path(p)):
            continue
        out.append((int(m.group(1)) if m.groups() else 0, name, p))
    out.sort()
    return [p for _k, _name, p in out]


def _sweep_part_temps(out_dir: Path, finals: list[Path]) -> None:
    """Remove ``.<name>.<8 hex>.jrpart`` files of these names older than a day (a split that
    crashed while writing); best effort."""
    wanted = {f.name[:48] for f in finals}
    rx = re.compile(r"\.(.*)\.[0-9a-f]{8}" + re.escape(PART_SUFFIX) + r"\Z")
    now = time.time()
    try:
        names = os.listdir(long_path(out_dir))
    except OSError:
        return
    for name in names:
        m = rx.match(name)
        if m is None or m.group(1) not in wanted:
            continue
        p = out_dir / name
        try:
            if now - os.stat(long_path(p)).st_mtime > STALE_PART_SECONDS:
                fileio.remove_quiet(p)
                log.info("removed the leftover part file %s", p)
        except OSError:
            pass


def _check_finals(doc: Any, finals: list[Path], out_dir: Path) -> None:
    if not os.path.isdir(long_path(out_dir)):
        raise UserInputError("filetools.error.no_dir", path=str(out_dir))
    for p in finals:
        writer._check_target(doc, p)
        if os.path.lexists(long_path(p)) and fileio.is_read_only(p):
            raise UserInputError("fileio.error.read_only", path=str(p))


# ----------------------------------------------------------------------------------
# split
# ----------------------------------------------------------------------------------
class _PartsMeter:
    """One progress bar over the parts (bytes), the part message, the periodic change check."""

    def __init__(self, doc: Any, progress: Progress | None, total: int, count: int,
                 cancel: CancelToken | None) -> None:
        self.doc = doc
        self.progress = progress
        self.total = max(1, int(total))
        self.count = count
        self.cancel = cancel
        self.done = 0
        self.next_check = CHANGE_CHECK_BYTES
        if progress is not None:
            progress.set_total(self.total)
            progress.update(0)
            progress.restart_clock()

    def part(self, k: int, path: Path) -> None:
        if self.progress is not None:
            self.progress.set_message(t("filetools.progress.part", k=k + 1, n=self.count, name=path.name))

    def add(self, n: int) -> None:
        self.done += n
        if self.progress is not None:
            self.progress.update(min(self.done, self.total))
        if self.done >= self.next_check:
            self.next_check = self.done + CHANGE_CHECK_BYTES
            if self.doc.check_changed():
                raise FileChangedError(path=str(self.doc.source_path))

    def set_done(self, done: int) -> None:
        self.done = max(self.done, int(done))
        if self.progress is not None:
            self.progress.update(min(self.done, self.total))


def _default_export(doc: Any, opts: SplitOptions) -> ExportOptions:
    """Engine B without export options: the document's own format (JSON Lines for JSON Lines and
    concatenated values, else JSON with the document's indentation)."""
    if _default_fmt(doc) == "jsonl":
        return ExportOptions("jsonl", keep_wrapper=opts.keep_wrapper)
    unit = editops.detect_style(doc).unit
    if unit is None:
        indent: int | str | None = None
    elif unit == "\t":
        indent = "\t"
    else:
        indent = min(16, len(unit))
    return ExportOptions("json", indent=indent, keep_wrapper=opts.keep_wrapper)


def split(doc: Any, opts: SplitOptions, source: Any = None, *, progress: Progress | None = None,
          cancel: CancelToken | None = None) -> SplitReport:
    """Cut ``doc`` (all records, a ``range`` of them, or a selection -- a RecordSet, an array of
    positions) into parts as ``opts`` says and write them all or nothing (module docstring
    "Split"). Returns a :class:`SplitReport`."""
    if not isinstance(opts, SplitOptions):
        raise TypeError("opts must be a SplitOptions instance")
    opts.validate()
    started = time.perf_counter()
    check(cancel)
    doc = writer._pin(_pinned(doc), source)
    _check_complete(doc, cancel, progress)
    if doc.kind == "single":
        raise UserInputError("filetools.error.single")
    broken = int(getattr(doc, "broken_count", 0) or 0) if doc.kind == "jsonl" else 0
    if broken and not opts.drop_broken:
        raise UserInputError("filetools.error.broken_lines", count=broken)
    warnings = _Warnings()
    sel = _selection(doc, source, opts)
    plan, estimate = _plan(sel, opts, cancel, warnings)
    export_opts = None
    if sel.engine == "export":
        export_opts = opts.export if opts.export is not None else _default_export(doc, opts)
        if export_opts.fmt == "xlsx" and export_opts.xlsx_split != "sheets":
            export_opts = dataclasses.replace(export_opts, xlsx_split="sheets")
    out_dir = _out_dir(doc, opts)
    finals, pattern, stem, ext = _part_names(doc, opts, _parts_fmt(doc, opts, sel.engine), len(plan), out_dir)
    _check_finals(doc, finals, out_dir)
    _check_space(out_dir, _space_needed(doc, estimate, finals, sel.engine))
    _sweep_part_temps(out_dir, finals)
    stale = [p for p in _stale_parts(out_dir, pattern, stem, ext, finals)
             if not fileio.same_file(p, doc.source_path) and not fileio.same_file(p, doc.data_path)]
    removing: list[Path] = []
    if opts.remove_stale and stale:
        removing = [p for p in _earlier_parts(stale, _name_regex(pattern, stem, ext), len(plan))
                    if not fileio.is_read_only(p)]
        gone = {normalize_path(p) for p in removing}
        stale = [p for p in stale if normalize_path(p) not in gone]
    _refuse_edited(finals + removing)
    meter = _PartsMeter(doc, progress, estimate, len(plan), cancel)
    staged: list[Path] = []
    parts: list[SplitPart] = []
    extra: dict[str, Any] = {}
    try:
        if sel.engine == "same":
            _write_same(sel, plan, finals, opts, staged, parts, meter, cancel)
        else:
            assert export_opts is not None
            _write_export(sel, plan, finals, export_opts, staged, parts, meter, warnings, extra, cancel)
        check(cancel)
        _commit_check(doc, warnings)
        _refuse_edited(finals + removing)      # an edit may have begun while the parts were written
        leftovers = fileio.commit_files(staged, finals, remove=removing)
    except BaseException:
        for tmp in staged:
            fileio.remove_quiet(tmp)
        raise
    for p in leftovers:
        warnings.add("filetools.warn.leftover", add=0, path=str(p))
    if stale:
        warnings.add("filetools.warn.stale_parts", add=len(stale), names=_names_list(stale))
    if removing:
        warnings.add("filetools.warn.stale_removed", add=len(removing), names=_names_list(removing))
    if broken and sel.engine == "same":    # (the writer's parts say so themselves)
        warnings.add("filetools.warn.broken_dropped", add=broken)
    if sel.engine == "same" and doc.kind == "brace_list":
        warnings.add("filetools.warn.brace_list", add=0)
    if progress is not None:
        progress.finish()
    texts, keys = warnings.texts()
    report = SplitReport(parts=parts, records=sum(p.records for p in parts), bytes_written=sum(p.bytes for p in parts),
                         elapsed=time.perf_counter() - started, warnings=texts, warning_keys=keys, stale=stale,
                         engine=sel.engine, fmt=export_opts.fmt if export_opts is not None else str(doc.kind),
                         rows=int(extra.get("rows", 0)), columns=list(extra.get("columns", [])),
                         tables=list(extra.get("tables", [])), skipped=int(extra.get("skipped", 0)),
                         removed=removing)
    log.info("split %s (%s, %d records) into %d parts in %s: %d bytes in %.2f s (%s)%s", doc.source_path.name,
             doc.kind, report.records, len(parts), out_dir, report.bytes_written, report.elapsed, sel.engine,
             f"; warnings: {', '.join(keys)}" if keys else "")
    return report


def _write_same(sel: _Selection, plan: list[tuple[int, int]], finals: list[Path], opts: SplitOptions,
                staged: list[Path], parts: list[SplitPart], meter: _PartsMeter, cancel: CancelToken | None) -> None:
    """Engine A: every part through the saver's planner (byte copy in the document's kind)."""
    doc = sel.doc
    n_doc = int(doc.record_count)
    for k, ((a, b), final) in enumerate(zip(plan, finals)):
        check(cancel)
        meter.part(k, final)
        fd, tmp = fileio.create_temp(final, PART_SUFFIX)
        staged.append(tmp)
        p0, p1 = sel.p0 + a, sel.p0 + b
        size = saver.write_part(doc, fd, final, p0, p1, first=p0 == 0, last=p1 == n_doc, wrapper=opts.keep_wrapper,
                                drop_broken=opts.drop_broken, meter=meter.add, cancel=cancel)
        parts.append(SplitPart(path=final, first=p0, records=b - a, bytes=size))


def _write_export(sel: _Selection, plan: list[tuple[int, int]], finals: list[Path], export_opts: ExportOptions,
                  staged: list[Path], parts: list[SplitPart], meter: _PartsMeter, warnings: _Warnings,
                  extra: dict[str, Any], cancel: CancelToken | None) -> None:
    """Engine B: every part is a writer export of its slice of the selection, staged next to its
    final name (CSV/XLSX parts share one column plan)."""
    doc = sel.doc
    column_plan = None
    if export_opts.fmt in ("csv", "xlsx"):
        column_plan = writer.plan_columns(doc, export_opts, sel.source, cancel=cancel)
        for key, text in zip(column_plan.warning_keys, column_plan.warnings):
            warnings.add_text(key, text)
        extra["columns"] = list(column_plan.header)
    total = meter.total
    weights = [b - a for a, b in plan]
    whole = max(1, sum(weights))
    progress = meter.progress
    base = 0.0
    reports: list[ExportReport] = []
    rows = skipped = 0
    tables: list[str] = []
    for k, ((a, b), final) in enumerate(zip(plan, finals)):
        check(cancel)
        meter.part(k, final)
        fd, tmp = fileio.create_temp(final, PART_SUFFIX)
        os.close(fd)
        staged.append(tmp)
        share = weights[k] / whole
        child = progress.child(base, min(1.0, base + share)) if progress is not None else None
        rep = writer.export(doc, tmp, export_opts, _slice(sel.source, a, b), progress=child, cancel=cancel,
                            gzip=writer.gzip_path(final), column_plan=column_plan)
        base += share
        meter.set_done(int(total * base))
        reports.append(rep)
        rows += rep.rows
        skipped += rep.skipped
        if not tables:
            tables = list(rep.tables)
        parts.append(SplitPart(path=final, first=a, records=rep.records, bytes=rep.bytes_written))
    _part_warnings(reports, warnings)
    extra["rows"] = rows
    extra["skipped"] = skipped
    extra["tables"] = tables


#: warnings about the document itself that the export of every part gives alike: reported once
_DOCUMENT_WARNINGS = frozenset({"writer.warn.partial", "writer.warn.broken_lines", "writer.note.wrapper_not_written",
                                "writer.warn.wrapper_dropped"})


def _part_warnings(reports: list[ExportReport], warnings: _Warnings) -> None:
    """The warnings of the parts' exports as the split's own: a warning about the document (the
    same in every part) once; ``writer.warn.columns_not_found`` for the explicit columns that NO
    part found; any other warning with the counts of all parts added up (its other parameters --
    e.g. the first record named -- from the first part that gave it)."""
    unfound_key = "writer.warn.columns_not_found"
    present = [set(r.warning_keys) for r in reports]
    unfound: list[str] | None = None         # the columns no part found so far (the first part's order)
    for rep in reports:
        params: list[Any] = list(rep.warning_params)
        if len(params) != len(rep.warning_keys):         # (a report without parameters: its texts)
            params = [None] * len(rep.warning_keys)
        part_unfound: list[str] = []
        for key, text, p in zip(rep.warning_keys, rep.warnings, params):
            if key == unfound_key:
                names = p.get("names") if isinstance(p, dict) else None
                if isinstance(names, list):
                    part_unfound = [str(name) for name in names]
                elif all(key in keys for keys in present):     # (no names given: the first part's text)
                    warnings.add_text(key, text)
            elif key in _DOCUMENT_WARNINGS or not isinstance(p, dict) or "count" not in p:
                warnings.add_text(key, text)
            else:
                warnings.add(key, add=int(p["count"]), first={k: v for k, v in p.items() if k != "count"})
        missing_here = set(part_unfound)
        unfound = part_unfound if unfound is None else [c for c in unfound if c in missing_here]
    if unfound:
        warnings.add(unfound_key, add=len(unfound), columns=writer._names_text(unfound, 10))


# ----------------------------------------------------------------------------------
# merge
# ----------------------------------------------------------------------------------
@dataclass
class MergeOptions:
    """How :func:`merge` writes: ``export`` (JSON or JSON Lines writer options: indent, sorted keys,
    keep_wrapper), ``dedupe_by`` (a concrete path, ``$`` = the whole record, ``$key`` = the member
    key; ``None`` = no deduplication), ``keep`` (``first``: the earliest input wins; ``last``),
    ``allow_partial`` (inputs with a JSON error contribute the records before it), ``memory_limit``
    (bytes for the deduplication's keys before they spill to disk; ``None`` = the settings')."""

    export: ExportOptions
    dedupe_by: str | None = None
    keep: str = "first"
    allow_partial: bool = False
    memory_limit: int | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.export, ExportOptions):
            raise TypeError("export must be an ExportOptions instance")
        self.export.validate()
        if self.export.fmt not in ("json", "jsonl"):
            raise UserInputError("filetools.error.merge_format", fmt=writer.FORMAT_NAMES[self.export.fmt])
        keep = self.keep.strip().lower() if isinstance(self.keep, str) else self.keep
        if keep not in KEEPS:
            raise UserInputError("filetools.error.option", name="keep", value=repr(self.keep))
        self.keep = keep
        if self.dedupe_by is not None and (not isinstance(self.dedupe_by, str) or not self.dedupe_by.strip()):
            raise UserInputError("filetools.error.option", name="dedupe_by", value=repr(self.dedupe_by))
        self.allow_partial = _check_bool("allow_partial", self.allow_partial)
        if self.memory_limit is not None:
            self.memory_limit = _positive("memory_limit", self.memory_limit)


@dataclass
class MergeReport:
    """What :func:`merge` wrote: the writer's report (``report.files`` = the output), per input
    ``(path, kind, records, written)``, the duplicates removed and their groups, the warnings
    (localized; ``warning_keys`` their keys: the writer's, the deduplication's and the merge's)."""

    report: ExportReport
    inputs: list[tuple[Path, str, int, int]]
    duplicates: int = 0
    groups: int = 0
    warnings: list[str] = field(default_factory=list)
    warning_keys: list[str] = field(default_factory=list)

    @property
    def records(self) -> int:
        return self.report.records

    @property
    def elapsed(self) -> float:
        return self.report.elapsed


def _data_bytes(doc: Any) -> int:
    if doc.edited:
        return max(1, int(doc.effective_bytes()))
    return max(1, int(doc.data_size) - int(doc.info.data_start))


def merge(docs: Sequence[Any], out_path: StrPath, opts: MergeOptions, *, progress: Progress | None = None,
          cancel: CancelToken | None = None) -> MergeReport:
    """Merge ``docs`` (opened documents; each is pinned once) into ``out_path`` (module docstring
    "Merge"). The output is written atomically; it may not be any input (also under another name).
    Returns a :class:`MergeReport`."""
    if not isinstance(opts, MergeOptions):
        raise TypeError("opts must be a MergeOptions instance")
    opts.validate()
    if not docs:
        raise UserInputError("writer.error.no_inputs")
    started = time.perf_counter()
    check(cancel)
    out = Path(os.path.abspath(os.fspath(out_path)))
    pinned = [_pinned(d) for d in docs]
    for d in pinned:
        writer._check_target(d, out)
    _refuse_edited([out])
    for d in pinned:
        if not d.index_complete:
            d.build_index(cancel=cancel)
        err = d.error
        if err is not None and not opts.allow_partial:
            raise UserInputError("filetools.error.partial_input", path=str(d.source_path),
                                 line=getattr(err, "line", 0), column=getattr(err, "column", 0))
    warnings = _Warnings()
    sources: list[Any] = [None] * len(pinned)
    duplicates = groups = 0
    write_progress = None
    if progress is not None:
        total = sum(_data_bytes(d) for d in pinned) * (2 if opts.dedupe_by else 1)
        progress.set_total(max(1, total))
        progress.update(0)
        # the writer reports into a child: the bar keeps the merge's own message
        write_progress = progress.child(0.0, 1.0)
    if opts.dedupe_by:
        from . import dupes   # deferred: the dedupe machinery is only needed here

        find_many = getattr(dupes, "find_duplicates_many", None)
        if find_many is None:  # pragma: no cover - dupes without the stage-5 extension
            raise UserInputError("filetools.error.dedupe_unavailable")
        if progress is not None:
            progress.set_message(t("filetools.progress.dedupe"))
        dedupe_progress = progress.child(0.0, 0.5) if progress is not None else None
        rep = find_many(pinned, opts.dedupe_by, keep=opts.keep, memory_limit=opts.memory_limit,
                        progress=dedupe_progress, cancel=cancel)
        sources = [rep.keep_source(i) for i in range(len(pinned))]
        duplicates = int(rep.duplicates)
        groups = int(rep.groups)
        for key, text in zip(rep.warnings, rep.warning_texts()):
            warnings.add_text(str(key), str(text), every=True)     # (one per input: e.g. records too large)
        write_progress = progress.child(0.5, 1.0) if progress is not None else None
    if progress is not None:
        progress.set_message(t("filetools.progress.merge", name=out.name))
    report = writer.export_many(list(zip(pinned, sources)), out, opts.export, progress=write_progress,
                                cancel=cancel)
    texts, keys = warnings.texts()
    report.elapsed = time.perf_counter() - started
    inputs = [(Path(d.source_path), str(d.kind), int(d.record_count), int(w))
              for d, w in zip(pinned, report.per_input)]
    if progress is not None:
        progress.finish()
    log.info("merged %d inputs into %s: %d records (%d duplicates removed) in %.2f s", len(pinned), out,
             report.records, duplicates, report.elapsed)
    return MergeReport(report=report, inputs=inputs, duplicates=duplicates, groups=groups,
                       warnings=texts + list(report.warnings), warning_keys=keys + list(report.warning_keys))


# ----------------------------------------------------------------------------------
# extract
# ----------------------------------------------------------------------------------
def resolve_node(doc: Any, location: Any, *, cancel: CancelToken | None = None) -> Any:
    """The :class:`~jsonreshape.reader.Node` at ``location``: a Node (returned as it is), a path
    (tuple of segments) or a location text -- ``#5`` / ``#5.orders`` / ``#5/orders`` (record 5,
    0-based, then a path inside it), ``data[5].orders``, ``$.data[5]``, ``/data/5/orders``
    (:func:`jsonreshape.paths.parse_location`). ``UserInputError("reader.path_not_found")`` when
    there is no such node; :class:`~jsonreshape.errors.PathSyntaxError` for a bad text."""
    if hasattr(location, "is_virtual") and hasattr(location, "start"):
        return location
    if isinstance(location, (tuple, list)):
        return doc.node(tuple(location), cancel=cancel)
    loc = parse_location(str(location))
    if loc.record is None:
        return doc.node(loc.path, cancel=cancel)
    if loc.record >= int(doc.record_count):
        raise UserInputError("reader.path_not_found", path=loc.text)
    node = doc.record_node(loc.record)
    for seg in loc.path:
        child = node.find_child(seg, cancel=cancel)
        if child is None:
            raise UserInputError("reader.path_not_found", path=loc.text)
        node = child
    return node


def extract(doc: Any, out_path: StrPath, opts: ExportOptions, *, path: str | None = None, node: Any = None,
            source: Any = None, progress: Progress | None = None, cancel: CancelToken | None = None) -> ExportReport:
    """Write values of ``doc`` into ``out_path`` (module docstring "Extract"): the records without
    their wrapper (neither ``path`` nor ``node``), one node (``node``) or a record-relative pattern
    gathered from the records of ``source`` (``path``). Atomic; the source must be unchanged
    before the commit. Returns the writer's :class:`~jsonreshape.writer.ExportReport`
    (``records``: the values written; ``skipped``: huge records whose values did not fit)."""
    if not isinstance(opts, ExportOptions):
        raise TypeError("opts must be an ExportOptions instance")
    opts.validate()
    if path is not None and node is not None:
        raise UserInputError("filetools.error.extract_args")
    _refuse_edited([Path(os.path.abspath(os.fspath(out_path)))])
    if path is None and node is None:
        return writer.export(doc, out_path, dataclasses.replace(opts, keep_wrapper=False), source,
                             progress=progress, cancel=cancel)
    if opts.fmt not in ("json", "jsonl"):
        raise UserInputError("filetools.error.extract_format", fmt=writer.FORMAT_NAMES[opts.fmt])
    if node is not None:
        doc = _pinned(doc)
        if not doc.index_complete:
            doc.build_index(cancel=cancel)
        target = resolve_node(doc, node, cancel=cancel)
        return writer.export_node(doc, target, out_path, opts.fmt, indent=opts.indent, progress=progress,
                                  cancel=cancel)
    assert path is not None
    return _gather(doc, out_path, opts, path, source, progress, cancel)


def _walkable(segments: tuple) -> bool:
    """A pattern the node walk resolves exactly: keys, indices, ``[*]`` and ``*`` (no ``**``)."""
    return all(isinstance(s, (str, int)) or s in (Wild.ANY_INDEX, Wild.ANY_KEY) for s in segments)


def _kids(node: Any, cancel: CancelToken | None) -> Iterator[Any]:
    """The children of a container node in order, fetched :data:`NODE_BATCH` at a time."""
    total = node.child_count(cancel=cancel)
    done = 0
    while done < total:
        check(cancel)
        kids = node.children(done, min(total, done + NODE_BATCH))
        if not kids:
            break
        yield from kids
        done += len(kids)


def _members(node: Any, cancel: CancelToken | None) -> Iterator[Any]:
    """The members of an object node with dict semantics (like a parsed record and the core
    PathExtractor): each distinct key once, in the order of its first appearance, with the value of
    its last occurrence. Objects of at most :data:`WALK_MAX_MEMBERS` members are checked for
    repeated keys (a dict of their keys); bigger ones are written member by member (module
    docstring, a known limit)."""
    total = node.child_count(cancel=cancel)
    if total > WALK_MAX_MEMBERS:
        log.info("extract: an object of %d members is walked without the repeated-key check", total)
        yield from _kids(node, cancel)
        return
    last: dict[str, int] = {}
    for k, kid in enumerate(_kids(node, cancel)):
        last[kid.key] = k
    if len(last) == total:
        yield from _kids(node, cancel)   # no repeated key: the members as they are
        return
    for idx in last.values():            # (insertion order = first appearance; the value = the last index)
        yield node.child(idx)


def _walk(node: Any, segments: tuple, i: int, cancel: CancelToken | None) -> Iterator[Any]:
    """The nodes below ``node`` matching ``segments[i:]`` in document order (dict semantics; children
    fetched :data:`NODE_BATCH` at a time)."""
    if i == len(segments):
        yield node
        return
    seg = segments[i]
    if seg is Wild.ANY_INDEX:
        if node.kind == "array":
            for kid in _kids(node, cancel):
                yield from _walk(kid, segments, i + 1, cancel)
        return
    if seg is Wild.ANY_KEY:
        if node.kind == "object":
            for kid in _members(node, cancel):
                yield from _walk(kid, segments, i + 1, cancel)
        return
    if isinstance(seg, int) and node.kind != "array":
        return                       # an index never matches an object key (PathExtractor semantics)
    child = node.find_child(seg, cancel=cancel)
    if child is not None:
        yield from _walk(child, segments, i + 1, cancel)


class _Gather:
    """Writes the gathered values (one Reformatter: a JSON array, or compact JSON Lines)."""

    def __init__(self, ctx: Any, sink: Any, json_out: bool, indent: Any) -> None:
        self.ctx = ctx
        self.sink = sink
        self.json_out = json_out
        self.rf = core.Reformatter(indent if json_out else None)
        self.feed = self.rf.feed
        self.first = True
        self.values = 0
        self.sort = bool(ctx.opts.sort_keys)

    def open(self) -> None:
        if self.json_out:
            self.sink.write(self.feed(b"["))

    def close(self) -> None:
        if self.json_out:
            self.sink.write(self.feed(b"]"))
        self.sink.write(self.rf.finish())

    def pieces(self, pieces: list[bytes]) -> None:
        """Complete values (JSON text)."""
        if not pieces:
            return
        if self.sort:
            pieces = [writer._sorted_record(self.ctx, p, member=False) for p in pieces]
        self.values += len(pieces)
        feed, sink = self.feed, self.sink
        if self.json_out:
            data = b",".join(pieces)
            writer._feed_all(feed, data if self.first else b"," + data, sink)
        else:
            parts: list[bytes] = []
            for p in pieces:
                parts += (feed(p), b"\n")
            sink.write(b"".join(parts))
        self.first = False

    def stream(self, chunks: Iterator[bytes]) -> None:
        """One value given in pieces (a node of a huge record)."""
        feed, sink = self.feed, self.sink
        if self.json_out and not self.first:
            sink.write(feed(b","))
        for chunk in chunks:
            sink.write(feed(chunk))
        if not self.json_out:
            sink.write(b"\n")
        self.first = False
        self.values += 1


def _gather(doc: Any, out_path: StrPath, opts: ExportOptions, path_text: str, source: Any,
            progress: Progress | None, cancel: CancelToken | None) -> ExportReport:
    started = time.perf_counter()
    pattern = parse_pattern(path_text)            # PathSyntaxError: a user error with its position
    doc = writer._pin(_pinned(doc), source)
    out = Path(os.path.abspath(os.fspath(out_path)))
    writer._check_target(doc, out)
    gz = writer._check_gzip(opts, out, None)
    writer._ensure_index(doc, progress, cancel)
    src, count = writer._normalize_source(doc, source)
    # values are gathered, not records: the wrapper is not missed (no wrapper notice)
    ctx = writer._Ctx(doc, dataclasses.replace(opts, keep_wrapper=False), src, count, progress, cancel, out)
    ctx.gzip = gz
    try:
        extractor = core.PathExtractor([pattern.to_core()], member_spans=bool(ctx.member))
    except ValueError:               # "**": the records are parsed
        extractor = None
    walkable = _walkable(pattern.segments)
    budget = capture_budget(doc.settings)
    member = ctx.member
    split = doc.split_member
    scanned = 0
    try:
        ctx.begin(1)
        if progress is not None:
            progress.set_message(t("filetools.progress.extract", name=out.name))
        with writer._output(out, gz) as sink:
            g = _Gather(ctx, sink, opts.fmt == "json", opts.indent)
            g.open()
            for blk in ctx.blocks():
                if blk.huge:
                    _gather_huge(ctx, g, doc, int(blk.indices[0]), blk.span(0), pattern, walkable, budget, cancel)
                elif extractor is not None:
                    data = blk.data
                    q = array("Q")
                    q.frombytes(extractor.extract(data, blk.spans))
                    g.pieces([data[s:e] for s, e in zip(q[2::4], q[3::4])])
                else:
                    sp = writer._pairs(blk.spans)
                    data = blk.data
                    pieces: list[bytes] = []
                    for k in range(len(blk.indices)):
                        if k & 1023 == 1023:
                            check(cancel)
                        raw = data[sp[2 * k]:sp[2 * k + 1]]
                        try:
                            if member:
                                raw = raw[split(raw)[1]:]
                            value = codec.loads(raw)
                        except _DECODE_ERRORS:
                            g.pieces(pieces)
                            pieces = []
                            _gather_huge(ctx, g, doc, int(blk.indices[k]), doc.record_span(int(blk.indices[k])),
                                         pattern, walkable, budget, cancel)
                            continue
                        pieces.extend(codec.dumps(v) for _p, v in pattern.resolve(value))
                    g.pieces(pieces)
                scanned += len(blk.indices)
                ctx.advance(blk)
            g.close()
            ctx.commit_check()
    finally:
        ctx.close()
    ctx.records = scanned
    ctx.finish()
    texts, keys = ctx.warning_texts()
    try:
        size = os.stat(long_path(out)).st_size
    except OSError:  # pragma: no cover - just written
        size = 0
    report = ExportReport(fmt=opts.fmt, records=g.values, rows=g.values, bytes_written=size, files=[out],
                          warnings=texts, elapsed=time.perf_counter() - started, skipped=ctx.skipped,
                          warning_keys=keys, warning_params=ctx.warning_params())
    log.info("extracted %d values (%s) of %d records of %s to %s: %d bytes in %.2f s", g.values, path_text, scanned,
             doc.source_path.name, out, size, report.elapsed)
    return report


def _gather_huge(ctx: Any, g: _Gather, doc: Any, index: int, span: tuple[int, int], pattern: Any, walkable: bool,
                 budget: int, cancel: CancelToken | None) -> None:
    """The values of a record that is not read in memory: the node walk (keys, indices, ``[*]``),
    else ``stream_values`` within the capture budget (a record whose values exceed it is skipped)."""
    if g.sort:
        ctx.warn("writer.warn.huge_unsorted")
    if walkable:
        root = doc.record_node(index)
        for node in _walk(root, pattern.segments, 0, cancel):
            g.stream(doc.node_chunks(node, chunk=EXTRACT_CHUNK, cancel=cancel))
        return
    try:
        values = stream_values(doc, span, [pattern], member=bool(ctx.member), max_bytes=budget,
                               cancel=cancel).get(0, [])
    except CaptureLimitExceeded:
        ctx.skipped += 1
        ctx.warn("filetools.warn.extract_too_large", first={"first": index}, limit=format_bytes(budget))
        return
    g.pieces([codec.dumps(v) for v in values])
