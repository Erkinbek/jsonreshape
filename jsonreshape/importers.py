r"""Import of CSV / TSV, YAML and XML files into JSON or JSON Lines (docs/ARCHITECTURE.md §17; stage 7).

:func:`import_file` reads a tabular, YAML or XML source and writes its records as JSON Lines (one compact line per
record) or as ONE JSON array / value with the writer's layouts (``indent`` 2 / 4 / tab or compact: the bytes of
``codec.dumps(records, indent=...)``, produced by the core ``Reformatter`` fed record by record). Everything is
streamed with bounded memory -- except YAML, which PyYAML cannot stream (see below) -- cancellable
(:class:`~jsonreshape.tasks.CancelToken`) and reports progress in bytes of the source read. The source is never
modified; the result is written to a temporary file and renamed over the target only on success
(:func:`jsonreshape.fileio.atomic_write`); the target may never be the source (``writer.error.same_file``) nor a file
with unsaved edits in this program (:func:`jsonreshape.writer.refuse_edited_targets`).

Input format: :func:`detect_format` by the extension (``.csv`` ``.tsv`` -> ``csv``, ``.yaml`` ``.yml`` -> ``yaml``,
``.xml`` -> ``xml``) or ``ImportOptions.fmt``. Output format: ``ImportOptions.output``, else the target's extension
(``.json`` -> one JSON value, ``.jsonl`` / ``.ndjson`` -> JSON Lines), else JSON Lines (YAML: JSON for one document,
JSON Lines for a stream of several). gzip inputs are refused (``importers.error.gzip``: unpack them first).

CSV / TSV (the ``csv`` module)
==============================
* Encoding: ``ImportOptions.encoding`` (any Python codec name; ``utf-8`` also accepts a BOM) or detected from the
  first bytes with :func:`jsonreshape.fileio.sniff`: UTF-8 with or without BOM, UTF-16 LE / BE with a BOM (and the
  BOM-less UTF-16 pattern of an ASCII start); UTF-32 is refused. The text is decoded chunk by chunk
  (``io.TextIOWrapper``, strict: a byte sequence that is not valid raises ``importers.error.decode`` with the line).
* Delimiter: ``ImportOptions.delimiter`` (one character; ``"\t"`` for tab) or sniffed from the first 64 KiB among
  ``,`` ``;`` tab ``|`` -- the first candidate (in that order; tab first for ``.tsv``) that gives every sample row the
  same number of fields (more than one); when none does, the one with the most fields. Quoting follows RFC 4180
  (``"`` quotes, ``""`` escapes a quote, newlines inside quoted fields are part of the value; ``strict``: a stray
  quote inside a field is an error).
* Header: the first non-empty row names the columns (``header=True``, default); an empty name becomes ``c<N>``
  (1-based column number); a name used twice is refused with the column numbers
  (``importers.error.csv_duplicate_header``). ``header=False``: every row is a record with the columns ``c1..cN``
  of its own width.
* Nested columns (``nest=True``, default): a name ``a.b.c`` -- every segment non-empty -- becomes the nested objects
  ``{"a": {"b": {"c": value}}}`` (``tags.0`` gives an object with the key ``"0"``, never an array); a column that is
  also the prefix of another (``a`` and ``a.b``) is refused (``importers.error.csv_nest_conflict``; use
  ``nest=False``). ``nest=False`` keeps every name as a flat key.
* Values (``types="auto"``, default): a cell that is exactly a JSON number token (RFC 8259: ``-?(0|[1-9][0-9]*)
  (\.[0-9]+)?([eE][+-]?[0-9]+)?``) becomes a number keeping its text through :mod:`jsonreshape.codec`'s exact types
  (``1.10`` stays ``1.10``, ``1e400`` and ``12345678901234567890`` stay as written; ``007``, ``+1``, ``1,5``, `` 1``
  stay strings); exactly ``true`` / ``false`` / ``null`` become those values; an empty cell becomes ``null``
  (``empty="null"``) or ``""`` (``empty="string"``); everything else is a string. ``types="string"`` keeps every
  non-empty cell a string (the empty rule still applies).
* Rows: a row whose number of fields differs from the header's is malformed, like a row the ``csv`` module refuses
  (bad quoting, a field above :data:`FIELD_LIMIT` = 64 MiB -- this bound keeps an unbalanced quote from swallowing
  the file into memory): an error naming the line where the row ends (``importers.error.csv_row_width`` /
  ``importers.error.csv_malformed``) or, with ``skip_bad=True``, skipped and counted (``importers.warn.skipped``
  names the first lines). Blank lines are ignored.

YAML (PyYAML, optional dependency ``jsonreshape[import]``)
=========================================================
* ``pyyaml`` is imported when a YAML file is converted; without it ``importers.error.yaml_missing`` is raised.
  ``yaml.CSafeLoader`` (libyaml) when available, else ``yaml.SafeLoader`` -- never an unsafe loader: ``!!python/...``
  tags are errors. The file is read as a stream (bytes consumed = progress) but PyYAML builds every document whole
  in memory: a file above :data:`YAML_BIG_BYTES` (256 MiB) gives ``importers.warn.yaml_big``, one above
  ``Settings.memory_limit_bytes`` x 2 is refused (``importers.error.yaml_too_big``). Cancellation takes effect at
  the next read of the parser (the stream ends early and the run is reported cancelled; nothing is written).
* One document -> one JSON value; a stream of several documents (``---``) -> JSON Lines, one line per document (a
  ``.json`` target gets an array of the documents; none -> ``[]`` with ``importers.warn.no_records``).
* Numbers keep their text: an ``!!int`` / ``!!float`` scalar whose text (underscores removed) is a JSON number
  token becomes the exact number (``1.10`` stays ``1.10``, ``-0`` stays ``-0``); other YAML 1.1 forms are written
  by value -- ``0x1F`` -> ``31``, ``017`` -> ``15`` (YAML 1.1 octal), ``+12`` -> ``12``, ``1:30`` (sexagesimal)
  -> ``90``, ``.5`` -> ``0.5``, ``1.`` -> ``1.0`` -- and counted in ``importers.note.yaml_values``; ``.inf`` /
  ``-.inf`` / ``.nan`` become ``null`` (``importers.warn.yaml_nonfinite``). What PyYAML resolves as a number is
  YAML 1.1's rule: ``1e3``, ``1.0e3`` and ``0o17`` (YAML 1.2 octal) are strings, ``yes`` / ``no`` / ``on`` /
  ``off`` are booleans.
* YAML-only scalars: ``~`` / empty / ``null`` -> ``null``; timestamps (``!!timestamp``: dates and date-times) -> ISO
  8601 strings (``date.isoformat()`` / ``datetime.isoformat()``: ``2001-12-14`` / ``2001-12-14T21:59:43.100000-05:00``);
  ``!!binary`` -> the base64 text as written, whitespace removed; ``!!set`` -> an object with ``null`` values
  (members sorted by their text: a set has no order); ``!!omap`` / ``!!pairs`` -> an array of ``[key, value]``
  arrays (PyYAML's tuples become arrays). Merge keys
  (``<<``) are resolved by the safe loader; anchors / aliases are expanded (an alias used twice
  is written twice). A document whose aliases are recursive (``&a [*a]``) is refused
  (``importers.error.yaml_recursive``); aliases expanding to more than ``2 x file size + 1 000 000`` values are
  refused as well (``importers.error.yaml_expanded``: the "billion laughs" shape). Mapping keys that are not strings
  (``1: a``, ``true: b``, ``~: c``, ``2001-01-01: d``) become their JSON text (``"1"``, ``"true"``, ``"null"``, the ISO
  string); a key given twice keeps the last value (PyYAML's rule; the keys ``1`` and ``true`` -- also ``1.0`` --
  are one Python dict key, so the last of them wins before this module sees the mapping).
* A syntax error is reported with its line and column (``importers.error.yaml``).

XML (``xml.etree.ElementTree.XMLPullParser``, streaming)
========================================================
* Safety: ``ElementTree`` never loads external DTDs or entities; internal entity declarations are expanded by expat,
  so a document whose prolog (the bytes before the root element) contains ``<!ENTITY`` is refused before anything
  is parsed (``importers.error.xml_entities``). A ``<!DOCTYPE`` without entity declarations is fine.
* Records (streamed: one element tree at a time, removed from the document once written): by default the children
  of the root element; ``record_tag="item"`` -- every element with that tag (its qualified ``prefix:name`` or its
  local name) that is not inside another such element; ``record_path="a/b/c"`` -- the elements at that path of tag
  names from the root (``a`` is the root; a leading ``/`` is allowed). No record (also a wrong path) -> an empty
  result with ``importers.warn.xml_no_records`` naming the root.
* Element -> JSON: attributes become ``"@name"`` members; child elements become members named by their tag, a tag
  repeated among the children of one element becomes an array (in document order; an element with the child once
  gets a value, with it twice an array -- the shapes of records may differ); text (``elem.text`` and the tails of
  the children, each stripped of surrounding whitespace, joined with one space -- mixed content loses the
  interleaving) becomes ``"#text"`` when the element also has attributes or children; an element with text only is
  that text; an empty element (no attributes, no children, no text) is ``null``. Whitespace-only text (indentation)
  is ignored. ``types="auto"`` applies the CSV value rule to text and attribute values (an empty attribute stays
  ``""``); comments and processing instructions are dropped.
* Namespaces: a tag ``{uri}local`` is written ``prefix:local`` with the prefix the document declares for that URI
  (the first declaration of a URI wins), the default namespace gives ``local`` alone, ``xml:lang`` keeps ``xml``.
* A malformed document is reported with the line and column of the parser (``importers.error.xml``); the records
  before it were written to the temporary file only, nothing is kept.

Report
======
:class:`ImportReport`: records written, bytes read / written, elapsed seconds, the encoding and delimiter used
(CSV), the column names (CSV with a header), the conversion counts (numbers, ``true``/``false``/``null`` literals,
empty cells, YAML dates / binaries / by-value numbers, non-finite values), the skipped rows with their first lines,
the XML root tag; ``warnings`` (localized; ``warning_keys`` parallel) -- skipped rows, a big YAML file, non-finite
YAML values, no records -- and ``notes`` (``note_keys``) -- the conversion counts worth telling. A count of 1 uses
the ``<key>.one`` text when it exists (English singular).
"""

from __future__ import annotations

import codecs
import csv
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import codec
from .errors import Cancelled, FileFormatError, JsonReshapeError, UserInputError
from .fileio import atomic_write, long_path, normalize_path, open_shared, sniff
from .i18n import has, t
from .settings import Settings, get_settings
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "EMPTY_MODES",
    "FIELD_LIMIT",
    "FORMATS",
    "OUTPUT_FORMATS",
    "TYPE_MODES",
    "YAML_BIG_BYTES",
    "ImportOptions",
    "ImportReport",
    "detect_format",
    "format_name",
    "import_file",
    "number_value",
    "output_format_for",
]

FORMATS = ("csv", "yaml", "xml")
OUTPUT_FORMATS = ("json", "jsonl")
TYPE_MODES = ("auto", "string")
EMPTY_MODES = ("null", "string")
INPUT_EXTENSIONS = {".csv": "csv", ".tsv": "csv", ".yaml": "yaml", ".yml": "yaml", ".xml": "xml"}
OUTPUT_EXTENSIONS = {".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl"}
DELIMITER_CANDIDATES = (",", ";", "\t", "|")

FIELD_LIMIT = 64 << 20          #: the biggest CSV field accepted (bounds the memory of an unbalanced quote)
SAMPLE_BYTES = 64 << 10         #: bytes sniffed for the encoding and the delimiter
SAMPLE_ROWS = 20                #: rows looked at by the delimiter sniff
CHUNK_BYTES = 8 << 20           #: XML read chunk
READ_BUFFER = 1 << 20           #: buffered reader size over the counting raw stream
ROWS_PER_CHECK = 256            #: CSV rows between progress updates / cancel checks
FEED_BYTES = 1 << 20            #: the Reformatter is fed at most this much at a time (pure-Python core memory)
YAML_BIG_BYTES = 256 << 20      #: a YAML file above this gets importers.warn.yaml_big
SKIPPED_SHOWN = 5               #: skipped-row lines named in the warning
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"

_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_NUMBER_START = frozenset("-0123456789")
_LITERALS: dict[str, Any] = {"true": True, "false": False, "null": None}
_NONFINITE = frozenset((".inf", "+.inf", "-.inf", ".nan"))


# ==================================================================================
# options and report
# ==================================================================================
def _invalid(name: str, value: Any, expected: str) -> UserInputError:
    return UserInputError("importers.error.option", name=name, value=str(value), expected=expected)


def _norm_indent(value: Any) -> int | str | None:
    """``None`` / ``"compact"`` -> compact; ``"tab"`` / ``"\\t"`` -> tab; ``0..16`` (also as text) -> spaces."""
    if value is None:
        return None
    if isinstance(value, str):
        if value == "\t":
            return "\t"
        s = value.strip().lower()
        if s in ("", "none", "compact"):
            return None
        if s in ("tab", "\\t"):
            return "\t"
        try:
            value = int(s)
        except ValueError:
            raise _invalid("indent", value, "0..16 | tab | compact") from None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 16:
        raise _invalid("indent", value, "0..16 | tab | compact")
    return value


def _norm_delimiter(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    if s.strip().lower() in ("tab", "\\t"):
        return "\t"
    if len(s) != 1 or s in ('"', "\r", "\n"):
        raise _invalid("delimiter", value, "one character | tab")
    return s


def _choice(name: str, value: Any, choices: tuple[str, ...]) -> str:
    s = str(value).strip().lower()
    if s not in choices:
        raise _invalid(name, value, " | ".join(choices))
    return s


@dataclass
class ImportOptions:
    """Options of :func:`import_file` (module docstring).

    ``fmt``: ``csv`` | ``yaml`` | ``xml`` | ``None`` (by the source extension); ``output``: ``json`` | ``jsonl`` |
    ``None`` (by the target extension, else JSON Lines; YAML: JSON for one document); ``indent``: the JSON array
    layout -- ``2`` (default), ``4``, ``"\\t"`` or ``None`` (compact; JSON Lines is always compact); ``types``:
    ``auto`` | ``string``; ``empty``: ``null`` | ``string`` (CSV empty cells); CSV: ``delimiter`` (one character or
    ``"\\t"``; ``None`` = sniffed), ``header``, ``nest``, ``encoding`` (codec name; ``None`` = detected),
    ``skip_bad``; XML: ``record_tag`` or ``record_path`` (``a/b/c``).
    """

    fmt: str | None = None
    output: str | None = None
    indent: int | str | None = 2
    types: str = "auto"
    empty: str = "null"
    delimiter: str | None = None
    header: bool = True
    nest: bool = True
    encoding: str | None = None
    skip_bad: bool = False
    record_tag: str | None = None
    record_path: str | None = None

    def __post_init__(self) -> None:
        if self.fmt is not None:
            self.fmt = _choice("fmt", self.fmt, FORMATS)
        if self.output is not None:
            self.output = _choice("output", self.output, OUTPUT_FORMATS)
        self.indent = _norm_indent(self.indent)
        self.types = _choice("types", self.types, TYPE_MODES)
        self.empty = _choice("empty", self.empty, EMPTY_MODES)
        self.delimiter = _norm_delimiter(self.delimiter)
        self.header = bool(self.header)
        self.nest = bool(self.nest)
        self.skip_bad = bool(self.skip_bad)
        if self.encoding is not None:
            enc = str(self.encoding).strip()
            try:
                info = codecs.lookup(enc)
            except LookupError:
                raise UserInputError("importers.error.encoding_unknown", encoding=enc) from None
            # a BOM is part of no header name: the signature codecs skip it
            self.encoding = "utf-8-sig" if info.name == "utf-8" else enc
        if self.record_tag is not None:
            self.record_tag = str(self.record_tag).strip() or None
        if self.record_path is not None:
            path = str(self.record_path).strip()
            if path and not [p for p in path.strip("/").split("/") if p]:
                raise _invalid("record_path", self.record_path, "a/b/c")
            self.record_path = path or None
        if self.record_tag and self.record_path:
            raise UserInputError("importers.error.record_both")


@dataclass
class ImportReport:
    """Result of :func:`import_file` (module docstring, "Report")."""

    fmt: str
    output_fmt: str
    source: Path
    output: Path
    records: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    elapsed: float = 0.0
    encoding: str | None = None
    delimiter: str | None = None
    columns: list[str] = field(default_factory=list)
    numbers: int = 0
    literals: int = 0
    empty_cells: int = 0
    skipped: int = 0
    skipped_lines: list[int] = field(default_factory=list)
    dates: int = 0
    binaries: int = 0
    yaml_values: int = 0
    nonfinite: int = 0
    root_tag: str | None = None
    warnings: list[str] = field(default_factory=list)
    warning_keys: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    note_keys: list[str] = field(default_factory=list)

    def warn(self, key: str, **params: Any) -> None:
        self.warning_keys.append(key)
        self.warnings.append(_tn(key, params.get("count"), **params))

    def note(self, key: str, **params: Any) -> None:
        self.note_keys.append(key)
        self.notes.append(_tn(key, params.get("count"), **params))

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.fmt,
            "output_format": self.output_fmt,
            "source": str(self.source),
            "output": str(self.output),
            "records": self.records,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "elapsed": self.elapsed,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "columns": list(self.columns),
            "numbers": self.numbers,
            "literals": self.literals,
            "empty_cells": self.empty_cells,
            "skipped": self.skipped,
            "skipped_lines": list(self.skipped_lines),
            "dates": self.dates,
            "binaries": self.binaries,
            "yaml_values": self.yaml_values,
            "nonfinite": self.nonfinite,
            "root_tag": self.root_tag,
            "warnings": list(self.warnings),
            "warning_keys": list(self.warning_keys),
            "notes": list(self.notes),
            "note_keys": list(self.note_keys),
        }


def _tn(key: str, count: Any, /, **params: Any) -> str:
    """:func:`t` with the ``<key>.one`` text when ``count`` is 1 and that key exists."""
    try:
        one = count is not None and int(count) == 1
    except (TypeError, ValueError):
        one = False
    if one and has(key + ".one"):
        return t(key + ".one", **params)
    return t(key, **params)


def detect_format(path: str | os.PathLike[str]) -> str | None:
    """``csv`` | ``yaml`` | ``xml`` by the extension of ``path`` (``.csv .tsv .yaml .yml .xml``), else ``None``."""
    return INPUT_EXTENSIONS.get(os.path.splitext(os.fspath(path))[1].lower())


def output_format_for(path: str | os.PathLike[str]) -> str | None:
    """``json`` | ``jsonl`` by the extension of ``path`` (``.json``; ``.jsonl`` / ``.ndjson``), else ``None``."""
    return OUTPUT_EXTENSIONS.get(os.path.splitext(os.fspath(path))[1].lower())


def format_name(fmt: str) -> str:
    """Localized name of an input format (``CSV / TSV``, ``YAML``, ``XML``)."""
    return t(f"importers.format.{fmt}") if fmt in FORMATS else str(fmt)


# ==================================================================================
# values
# ==================================================================================
def number_value(tok: str) -> Any:
    """The exact JSON value of a number token (``tok`` must match the JSON number grammar): plain tokens become
    ``int`` / ``float``, the others :class:`~jsonreshape.codec.JsonFloat` / :class:`~jsonreshape.codec.JsonInt`
    keeping the text (``1.10``, ``1e400``, ``-0``, integers beyond 64 bits) -- what :func:`codec.loads` gives."""
    if codec.is_plain_int_token(tok):
        return int(tok)
    if codec.is_plain_float_token(tok):
        return float(tok)
    return codec.loads_precise(tok)


class _Cells:
    """Turns CSV cells and XML text into JSON values (``types`` / ``empty`` rules) and counts the conversions."""

    __slots__ = ("auto", "empty_value", "numbers", "literals", "empty")

    def __init__(self, types: str, empty: str) -> None:
        self.auto = types == "auto"
        self.empty_value: Any = None if empty == "null" else ""
        self.numbers = 0
        self.literals = 0
        self.empty = 0

    def cell(self, s: str) -> Any:
        """A CSV cell: empty -> the ``empty`` rule, else :meth:`text`."""
        if not s:
            self.empty += 1
            return self.empty_value
        return self.text(s)

    def text(self, s: str) -> Any:
        """A non-empty text: a JSON number token -> exact number, ``true`` / ``false`` / ``null`` -> literal
        (``types="auto"``), anything else the string itself."""
        if self.auto and s:
            if s[0] in _NUMBER_START and _NUMBER_RE.fullmatch(s) is not None:
                self.numbers += 1
                return number_value(s)
            if s in _LITERALS:
                self.literals += 1
                return _LITERALS[s]
        return s


# ==================================================================================
# streams and the output writer
# ==================================================================================
class _Counter(io.RawIOBase):
    """Raw stream over an open binary file: counts the bytes handed out (progress) and stops at a cancellation --
    ``soft``: the stream ends instead of raising (libyaml's reader must not see Python exceptions; the caller
    checks :attr:`cancelled` afterwards)."""

    def __init__(self, f: Any, progress: Progress | None, cancel: CancelToken | None, *, soft: bool = False) -> None:
        super().__init__()
        self.f = f
        self.progress = progress
        self.cancel = cancel
        self.soft = soft
        self.count = 0
        self.cancelled = False

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        if self.cancel is not None and self.cancel.cancelled:
            self.cancelled = True
            if self.soft:
                return 0
            raise Cancelled()
        n = self.f.readinto(b)
        if n:
            self.count += n
            if self.progress is not None:
                self.progress.update(self.count)
        return n


class _Writer:
    """Writes records to the output file: JSON Lines (compact lines) or one JSON array -- bare value with
    ``single`` -- with the layout of ``codec.dumps(..., indent)`` (the core Reformatter fed record by record)."""

    def __init__(self, f: Any, fmt: str, indent: int | str | None) -> None:
        self.f = f
        self.jsonl = fmt == "jsonl"
        self.indent = indent
        self.count = 0
        self.bytes = 0
        self._fmt: Any = None
        self._opened = False
        self._single = False

    def _emit(self, data: bytes) -> None:
        fmt = self._fmt
        if fmt is None:
            self.f.write(data)
            self.bytes += len(data)
            return
        if len(data) <= FEED_BYTES:
            out = fmt.feed(data)
            if out:
                self.f.write(out)
                self.bytes += len(out)
            return
        view = memoryview(data)
        for i in range(0, len(data), FEED_BYTES):
            out = fmt.feed(view[i:i + FEED_BYTES])
            if out:
                self.f.write(out)
                self.bytes += len(out)

    def _open(self, single: bool) -> None:
        self._opened = True
        self._single = single
        if self.jsonl:
            return
        if self.indent is not None:
            from .core import Reformatter

            self._fmt = Reformatter(indent=self.indent)
        if not single:
            self._emit(b"[")

    def write(self, value: Any, *, single: bool = False) -> None:
        """One record. ``single`` (first record only; JSON output): the value is written bare, not in an array."""
        if not self._opened:
            self._open(single)
        data = codec.dumps_transient(value)
        if self.jsonl:
            self.f.write(data)
            self.f.write(b"\n")
            self.bytes += len(data) + 1
        else:
            if self.count:
                self._emit(b",")
            self._emit(data)
        self.count += 1

    def finish(self) -> None:
        if not self._opened:
            self._open(False)
        if not self.jsonl:
            if not self._single:
                self._emit(b"]")
            if self._fmt is not None:
                out = self._fmt.finish()
                if out:
                    self.f.write(out)
                    self.bytes += len(out)


# ==================================================================================
# the entry point
# ==================================================================================
def _check_source(path: str) -> None:
    lp = long_path(path)
    if os.path.isdir(lp):
        raise UserInputError("fileio.error.is_directory", path=path)
    if not os.path.exists(lp):
        raise UserInputError("error.file_not_found", path=path)


def _same_file(a: str, b: str) -> bool:
    try:
        if normalize_path(a) == normalize_path(b):
            return True
    except (OSError, ValueError):  # pragma: no cover
        pass
    try:
        return os.path.samefile(long_path(a), long_path(b))
    except (OSError, ValueError):
        return False


def _check_target(src: str, out: str) -> None:
    """The target is never the source (any spelling, a link), a folder, in a missing folder, or a file with unsaved
    edits in this program (:func:`jsonreshape.writer.refuse_edited_targets`)."""
    if _same_file(src, out):
        raise UserInputError("writer.error.same_file", path=out)
    if os.path.isdir(long_path(out)):
        raise UserInputError("fileio.error.is_directory", path=out)
    parent = os.path.dirname(os.path.abspath(out))
    if parent and not os.path.isdir(long_path(parent)):
        raise UserInputError("writer.error.no_dir", path=parent)
    from .writer import refuse_edited_targets

    refuse_edited_targets([out])


def import_file(source: str | os.PathLike[str], target: str | os.PathLike[str], opts: ImportOptions | None = None,
                *, settings: Settings | None = None, progress: Progress | None = None,
                cancel: CancelToken | None = None) -> ImportReport:
    """Convert ``source`` (CSV / TSV, YAML or XML; module docstring) into the JSON / JSON Lines file ``target``.

    ``progress`` counts bytes of the source (its total is set here); ``cancel`` stops the work at its next check
    (:class:`~jsonreshape.errors.Cancelled`; nothing is written). Errors: :class:`UserInputError` for options, a
    missing source, a refused target, a missing ``pyyaml``, an unsafe or too big input;
    :class:`~jsonreshape.errors.FileFormatError` for a malformed input (``importers.error.*`` with the line).
    """
    opts = opts if opts is not None else ImportOptions()
    settings = settings if settings is not None else get_settings()
    src = os.fspath(source)
    out = os.fspath(target)
    _check_source(src)
    fmt = opts.fmt or detect_format(src)
    if fmt is None:
        raise UserInputError("importers.error.unknown_format", path=src,
                             extensions=" ".join(sorted(INPUT_EXTENSIONS)))
    _check_target(src, out)
    size = os.path.getsize(long_path(src))
    out_fmt = opts.output or output_format_for(out)
    rep = ImportReport(fmt=fmt, output_fmt=out_fmt or ("jsonl" if fmt != "yaml" else "json"), source=Path(src),
                       output=Path(out))
    started = time.perf_counter()
    if progress is not None:
        progress.set_total(size)
        progress.set_message(t(f"importers.progress.{fmt}"))
    check(cancel)
    cells = _Cells(opts.types, opts.empty)
    with atomic_write(out, "wb") as f:
        if fmt == "csv":
            writer = _Writer(f, out_fmt or "jsonl", opts.indent)
            _import_csv(src, size, opts, writer, cells, rep, progress, cancel)
        elif fmt == "xml":
            writer = _Writer(f, out_fmt or "jsonl", opts.indent)
            _import_xml(src, size, opts, writer, cells, rep, progress, cancel)
        else:
            writer = _import_yaml(src, size, opts, out_fmt, f, rep, settings, progress, cancel)
        check(cancel)
        writer.finish()
        rep.output_fmt = "jsonl" if writer.jsonl else "json"
        rep.records = writer.count
        rep.bytes_written = writer.bytes
    rep.bytes_read = size
    rep.elapsed = time.perf_counter() - started
    _finish_report(rep, opts, cells)
    if progress is not None:
        progress.finish()
    log.info("import %s (%s) -> %s (%s): %d records, %.2f s", src, fmt, out, rep.output_fmt, rep.records,
             rep.elapsed)
    return rep


def _finish_report(rep: ImportReport, opts: ImportOptions, cells: _Cells) -> None:
    rep.numbers = cells.numbers
    rep.literals = cells.literals
    rep.empty_cells = cells.empty
    if rep.skipped:
        lines = ", ".join(str(n) for n in rep.skipped_lines[:SKIPPED_SHOWN])
        rep.warn("importers.warn.skipped", count=rep.skipped, lines=lines)
    if rep.records == 0:
        if rep.fmt == "xml" and rep.root_tag:
            rep.warn("importers.warn.xml_no_records", root=rep.root_tag)
        else:
            rep.warn("importers.warn.no_records")
    if rep.nonfinite:
        rep.warn("importers.warn.yaml_nonfinite", count=rep.nonfinite)
    if rep.numbers:
        rep.note("importers.note.numbers", count=rep.numbers)
    if rep.literals:
        rep.note("importers.note.literals", count=rep.literals)
    if rep.empty_cells:
        rep.note("importers.note.empty_null" if opts.empty == "null" else "importers.note.empty_string",
                 count=rep.empty_cells)
    if rep.yaml_values:
        rep.note("importers.note.yaml_values", count=rep.yaml_values)
    if rep.dates:
        rep.note("importers.note.dates", count=rep.dates)
    if rep.binaries:
        rep.note("importers.note.binary", count=rep.binaries)


# ==================================================================================
# CSV / TSV
# ==================================================================================
def _detect_encoding(head: bytes, given: str | None, path: str) -> str:
    """The Python codec of the source: ``given`` (already normalized) or sniffed (module docstring)."""
    sn = sniff(head)
    if sn.gzip:
        raise UserInputError("importers.error.gzip", path=path)
    if given:
        return given
    enc = sn.encoding
    if enc.startswith("utf-32"):
        raise FileFormatError("importers.error.utf32", path=path)
    if enc == "utf-8-bom":
        return "utf-8-sig"
    if enc.startswith("utf-16"):
        return "utf-16" if sn.bom_len else enc
    return "utf-8"


def _decode_error_line(path: str, encoding: str) -> int:
    """The 1-based line of the first byte sequence that ``encoding`` cannot decode (a second pass over the
    file, made only to report the error: the text wrapper decodes whole buffers, so the row reader's line is
    not the place)."""
    try:
        with open_shared(path) as f:
            if encoding == "utf-16":                      # the BOM decides the byte order of the whole file
                bom = f.read(2)
                encoding = "utf-16-be" if bom == b"\xfe\xff" else "utf-16-le"
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            line = 1
            while True:
                chunk = f.read(READ_BUFFER)
                if not chunk:
                    return line
                try:
                    text = decoder.decode(chunk)
                except UnicodeDecodeError as exc:
                    good = bytes(exc.object[:exc.start])
                    return line + good.decode(encoding, errors="replace").count("\n")
                line += text.count("\n")
    except (OSError, LookupError, JsonReshapeError):
        return 0


def _sniff_delimiter(head: bytes, encoding: str, path: str, whole: bool) -> str:
    """The delimiter of the sample ``head`` (module docstring): the first candidate giving every sample row the
    same field count above one, else the one with the most fields."""
    text = head.decode(encoding, errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not whole and lines:
        lines = lines[:-1]          # the last line of a cut sample is incomplete
    lines = lines[:SAMPLE_ROWS]
    candidates = DELIMITER_CANDIDATES
    if os.path.splitext(path)[1].lower() == ".tsv":
        candidates = ("\t",) + tuple(c for c in DELIMITER_CANDIDATES if c != "\t")
    if not lines:
        return candidates[0]
    best = candidates[0]
    best_fields = 0
    for d in candidates:
        try:
            counts = [len(row) for row in csv.reader(lines, delimiter=d)]
        except csv.Error:
            continue
        counts = [c for c in counts if c]
        if not counts:
            continue
        if len(set(counts)) == 1 and counts[0] > 1:
            return d
        if min(counts) > best_fields:
            best, best_fields = d, min(counts)
    return best


def _column_paths(names: list[str], nest: bool) -> list[tuple[str, ...]]:
    """The key path of every column (module docstring, "Nested columns"); duplicate names and nesting conflicts
    are refused."""
    seen: dict[str, list[int]] = {}
    for i, name in enumerate(names):
        seen.setdefault(name, []).append(i + 1)
    for name, cols in seen.items():
        if len(cols) > 1:
            raise FileFormatError("importers.error.csv_duplicate_header", name=name,
                                  columns=", ".join(str(c) for c in cols))
    paths: list[tuple[str, ...]] = []
    for name in names:
        if nest and "." in name:
            parts = tuple(name.split("."))
            paths.append(parts if all(parts) else (name,))
        else:
            paths.append((name,))
    if nest:
        by_path = {p: names[i] for i, p in enumerate(paths)}
        for p, name in by_path.items():
            for k in range(1, len(p)):
                other = by_path.get(p[:k])
                if other is not None:
                    raise FileFormatError("importers.error.csv_nest_conflict", first=other, second=name)
    return paths


class _Rows:
    """Builds the record object of a CSV row from the column paths (flat fast path)."""

    __slots__ = ("paths", "names", "flat", "cells", "width")

    def __init__(self, paths: list[tuple[str, ...]], cells: _Cells) -> None:
        self.paths = paths
        self.names = [p[0] for p in paths]
        self.flat = all(len(p) == 1 for p in paths)
        self.cells = cells
        self.width = len(paths)

    def record(self, row: list[str]) -> dict[str, Any]:
        cell = self.cells.cell
        if self.flat:
            return {name: cell(v) for name, v in zip(self.names, row)}
        rec: dict[str, Any] = {}
        for path, raw in zip(self.paths, row):
            value = cell(raw)
            if len(path) == 1:
                rec[path[0]] = value
                continue
            d = rec
            for key in path[:-1]:
                nxt = d.get(key)
                if nxt is None:
                    nxt = d[key] = {}
                d = nxt
            d[path[-1]] = value
        return rec


def _import_csv(src: str, size: int, opts: ImportOptions, writer: _Writer, cells: _Cells, rep: ImportReport,
                progress: Progress | None, cancel: CancelToken | None) -> None:
    if csv.field_size_limit() < FIELD_LIMIT:
        csv.field_size_limit(FIELD_LIMIT)
    with open_shared(src) as raw:
        head = raw.read(SAMPLE_BYTES)
        encoding = _detect_encoding(head, opts.encoding, src)
        delimiter = opts.delimiter or _sniff_delimiter(head, encoding, src, whole=len(head) < SAMPLE_BYTES)
        rep.encoding = encoding
        rep.delimiter = delimiter
        raw.seek(0)
        counter = _Counter(raw, progress, cancel)
        text = io.TextIOWrapper(io.BufferedReader(counter, READ_BUFFER), encoding=encoding, errors="strict",
                                newline="")
        try:
            reader = csv.reader(text, delimiter=delimiter, strict=True)
            rows: _Rows | None = None
            n = 0
            while True:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except UnicodeDecodeError as exc:
                    raise FileFormatError("importers.error.decode", encoding=encoding,
                                          line=_decode_error_line(src, encoding), detail=exc.reason) from None
                except csv.Error as exc:
                    if opts.skip_bad and rows is not None:
                        _skip(rep, reader.line_num)
                        continue
                    raise FileFormatError("importers.error.csv_malformed", line=reader.line_num,
                                          detail=str(exc)) from None
                if not row:
                    continue            # a blank line
                if rows is None and opts.header:
                    names = [name if name else f"c{i + 1}" for i, name in enumerate(row)]
                    rep.columns = list(names)
                    rows = _Rows(_column_paths(names, opts.nest), cells)
                    continue
                if rows is not None:
                    if len(row) != rows.width:
                        if opts.skip_bad:
                            _skip(rep, reader.line_num)
                            continue
                        raise FileFormatError("importers.error.csv_row_width", line=reader.line_num,
                                              expected=rows.width, got=len(row))
                    writer.write(rows.record(row))
                else:
                    cell = cells.cell
                    writer.write({f"c{i + 1}": cell(v) for i, v in enumerate(row)})
                n += 1
                if n % ROWS_PER_CHECK == 0:
                    check(cancel)
        finally:
            text.detach()          # the shared handle is closed by the with block, not by the wrapper


def _skip(rep: ImportReport, line: int) -> None:
    rep.skipped += 1
    if len(rep.skipped_lines) < SKIPPED_SHOWN:
        rep.skipped_lines.append(line)


# ==================================================================================
# YAML
# ==================================================================================
def _yaml_module() -> Any:
    try:
        import yaml
    except ImportError:
        raise UserInputError("importers.error.yaml_missing") from None
    return yaml


_LOADER: Any = None


class _YamlState:
    """Per-run counters the constructors update (``loader.jr_state``)."""

    __slots__ = ("values", "dates", "binaries", "nonfinite")

    def __init__(self) -> None:
        self.values = 0
        self.dates = 0
        self.binaries = 0
        self.nonfinite = 0


def _loader_class(yaml: Any) -> Any:
    """The safe loader (libyaml when available) with the exact-number, timestamp and binary constructors."""
    global _LOADER
    if _LOADER is not None:
        return _LOADER
    base = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
    safe = yaml.constructor.SafeConstructor

    class ExactLoader(base):  # type: ignore[misc, valid-type]
        jr_state: _YamlState

    def construct_int(loader: Any, node: Any) -> Any:
        tok = node.value.replace("_", "")
        if _NUMBER_RE.fullmatch(tok) is not None:
            return number_value(tok)
        loader.jr_state.values += 1
        return int(safe.construct_yaml_int(loader, node))

    def construct_float(loader: Any, node: Any) -> Any:
        tok = node.value.replace("_", "")
        if _NUMBER_RE.fullmatch(tok) is not None:
            return number_value(tok)
        if tok.lower() in _NONFINITE:
            loader.jr_state.nonfinite += 1
            return None
        loader.jr_state.values += 1
        return float(safe.construct_yaml_float(loader, node))

    def construct_timestamp(loader: Any, node: Any) -> Any:
        loader.jr_state.dates += 1
        return safe.construct_yaml_timestamp(loader, node).isoformat()

    def construct_binary(loader: Any, node: Any) -> Any:
        safe.construct_yaml_binary(loader, node)          # validates the base64 text
        loader.jr_state.binaries += 1
        return "".join(str(node.value).split())

    ExactLoader.add_constructor("tag:yaml.org,2002:int", construct_int)
    ExactLoader.add_constructor("tag:yaml.org,2002:float", construct_float)
    ExactLoader.add_constructor("tag:yaml.org,2002:timestamp", construct_timestamp)
    ExactLoader.add_constructor("tag:yaml.org,2002:binary", construct_binary)
    _LOADER = ExactLoader
    return ExactLoader


def _key_text(key: Any) -> str:
    if isinstance(key, str):
        return key
    text = codec.value_text(key)
    if text is not None:
        return text
    raise UserInputError("importers.error.yaml_key", key=repr(key))


def _json_shape(v: Any) -> Any:
    """A tuple (``!!omap`` / ``!!pairs`` items) as a list, a set (``!!set``) as an object with ``null`` values
    (keys as text, sorted: a set has no order); anything else unchanged."""
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, (set, frozenset)):
        return {k: None for k in sorted(_key_text(x) for x in v)}
    return v


def _enter(container: Any) -> list[Any]:
    """Normalize one container in place (keys -> text, tuples -> lists, sets -> objects) and return its child
    containers."""
    children: list[Any] = []
    if isinstance(container, dict):
        if any(not isinstance(k, str) for k in container):
            items = list(container.items())
            container.clear()
            for k, v in items:
                container[_key_text(k)] = v
        for k, v in container.items():
            if isinstance(v, (tuple, set, frozenset)):
                v = container[k] = _json_shape(v)
            if isinstance(v, (dict, list)):
                children.append(v)
    else:
        for i, v in enumerate(container):
            if isinstance(v, (tuple, set, frozenset)):
                v = container[i] = _json_shape(v)
            if isinstance(v, (dict, list)):
                children.append(v)
    return children


def _jsonify(value: Any, limit: int) -> Any:
    """A loaded YAML document made JSON-shaped in place (module docstring): refuses recursive aliases and
    expansions beyond ``limit`` values."""
    value = _json_shape(value)
    if not isinstance(value, (dict, list)):
        return value
    count = 0
    on_path = {id(value)}
    frames: list[tuple[Any, Iterator[Any]]] = [(value, iter(_enter(value)))]
    while frames:
        container, it = frames[-1]
        child = next(it, _END)
        if child is _END:
            frames.pop()
            on_path.discard(id(container))
            continue
        count += 1
        if count > limit:
            raise UserInputError("importers.error.yaml_expanded", count=limit)
        if id(child) in on_path:
            raise UserInputError("importers.error.yaml_recursive")
        on_path.add(id(child))
        frames.append((child, iter(_enter(child))))
    return value


_END = object()


def _yaml_error(exc: Exception) -> FileFormatError:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    if not problem:
        text = str(exc)
        problem = text.splitlines()[0] if text else type(exc).__name__
    if mark is not None:
        return FileFormatError("importers.error.yaml", line=mark.line + 1, column=mark.column + 1, reason=problem)
    return FileFormatError("importers.error.yaml_plain", reason=problem)


def _import_yaml(src: str, size: int, opts: ImportOptions, out_fmt: str | None, f: Any, rep: ImportReport,
                 settings: Settings, progress: Progress | None, cancel: CancelToken | None) -> _Writer:
    yaml = _yaml_module()
    limit = int(settings.memory_limit_bytes) * 2
    if size > limit:
        raise UserInputError("importers.error.yaml_too_big", size=format_bytes(size), limit=format_bytes(limit),
                             setting=t("settings.field.memory_limit_bytes"))
    if size > YAML_BIG_BYTES:
        rep.warn("importers.warn.yaml_big", size=format_bytes(size))
    loader_cls = _loader_class(yaml)
    state = _YamlState()
    node_limit = size * 2 + 1_000_000
    writer: _Writer | None = None
    with open_shared(src) as raw:
        head = raw.read(4)
        _detect_encoding(head, None, src)              # gzip refused
        raw.seek(0)
        counter = _Counter(raw, progress, cancel, soft=True)
        stream: Any = io.BufferedReader(counter, READ_BUFFER)
        if opts.encoding:
            stream = io.TextIOWrapper(stream, encoding=opts.encoding, errors="strict")
        rep.encoding = opts.encoding or "utf-8"
        loader = loader_cls(stream)
        loader.jr_state = state
        try:
            docs = _documents(loader, counter, cancel, src, rep.encoding)
            first = next(docs, _END)
            if first is _END:
                writer = _Writer(f, out_fmt or "json", opts.indent)       # no document: [] / an empty file
            else:
                second = next(docs, _END)
                fmt = out_fmt or ("json" if second is _END else "jsonl")
                writer = _Writer(f, fmt, opts.indent)
                writer.write(_jsonify(first, node_limit), single=(fmt == "json" and second is _END))
                if second is not _END:
                    writer.write(_jsonify(second, node_limit))
                    for doc in docs:
                        writer.write(_jsonify(doc, node_limit))
        finally:
            try:
                loader.dispose()
            except Exception:  # pragma: no cover - libyaml always disposes
                log.debug("yaml loader dispose failed", exc_info=True)
            if isinstance(stream, io.TextIOWrapper):
                stream.detach()
    rep.dates = state.dates
    rep.binaries = state.binaries
    rep.yaml_values = state.values
    rep.nonfinite = state.nonfinite
    return writer


def _documents(loader: Any, counter: _Counter, cancel: CancelToken | None, src: str, encoding: str) -> Iterator[Any]:
    """The documents of the stream one by one; a cancellation seen by the counter ends the run with
    :class:`Cancelled` (a parse error of the cut stream is never reported)."""
    yaml = _yaml_module()
    while True:
        try:
            more = loader.check_data()
            if not more:
                break
            doc = loader.get_data()
        except yaml.YAMLError as exc:
            if counter.cancelled:
                raise Cancelled() from None
            raise _yaml_error(exc) from None
        except UnicodeDecodeError as exc:
            if counter.cancelled:
                raise Cancelled() from None
            raise FileFormatError("importers.error.decode", encoding=encoding, line=_decode_error_line(src, encoding),
                                  detail=exc.reason) from None
        except RecursionError:
            raise UserInputError("importers.error.yaml_deep") from None
        if counter.cancelled:
            raise Cancelled()
        check(cancel)
        yield doc


# ==================================================================================
# XML
# ==================================================================================
class _Names:
    """Qualified names ``prefix:local`` from ElementTree's ``{uri}local`` with the document's prefix map."""

    __slots__ = ("ns", "cache")

    def __init__(self) -> None:
        self.ns: dict[str, str] = {XML_NAMESPACE: "xml"}
        self.cache: dict[str, str] = {}

    def declare(self, prefix: str, uri: str) -> None:
        if uri not in self.ns:
            self.ns[uri] = prefix
            self.cache.clear()

    def __call__(self, tag: str) -> str:
        name = self.cache.get(tag)
        if name is None:
            if tag.startswith("{"):
                uri, _, local = tag[1:].partition("}")
                prefix = self.ns.get(uri, "")
                name = f"{prefix}:{local}" if prefix else local
            else:
                name = tag
            if len(self.cache) < 4096:
                self.cache[tag] = name
        return name


def _element_value(elem: Any, name_of: _Names, cells: _Cells) -> Any:
    """The JSON value of a complete element (module docstring, "Element -> JSON"); iterative, so the depth of the
    document is not limited by the Python stack."""
    order: list[Any] = []
    stack = [elem]
    while stack:
        e = stack.pop()
        order.append(e)
        stack.extend(e)
    values: dict[int, Any] = {}
    for e in reversed(order):           # children before their parent
        obj: dict[str, Any] = {}
        for k, v in e.attrib.items():
            obj["@" + name_of(k)] = cells.text(v) if v else ""
        texts: list[str] = []
        if e.text:
            s = e.text.strip()
            if s:
                texts.append(s)
        repeated: set[str] | None = None
        for child in e:
            key = name_of(child.tag)
            val = values.pop(id(child))
            if key in obj:
                if repeated is not None and key in repeated:
                    obj[key].append(val)
                else:
                    obj[key] = [obj[key], val]
                    if repeated is None:
                        repeated = set()
                    repeated.add(key)
            else:
                obj[key] = val
            if child.tail:
                s = child.tail.strip()
                if s:
                    texts.append(s)
        if not obj:
            value = cells.text(" ".join(texts)) if texts else None
        else:
            if texts:
                obj["#text"] = cells.text(" ".join(texts))
            value = obj
        values[id(e)] = value
    return values[id(elem)]


class _Selector:
    """Which elements become records (module docstring, "Records")."""

    __slots__ = ("tag", "path")

    def __init__(self, tag: str | None, path: str | None) -> None:
        self.tag = tag
        self.path = tuple(p for p in path.strip("/").split("/") if p) if path else None

    def matches(self, names: list[str]) -> bool:
        if self.tag is not None:
            name = names[-1]
            return name == self.tag or name.rpartition(":")[2] == self.tag
        if self.path is not None:
            return len(names) == len(self.path) and tuple(names) == self.path
        return len(names) == 2


def _xml_error(exc: Exception) -> FileFormatError:
    text = str(exc)
    reason = text.rsplit(": line ", 1)[0] if ": line " in text else text
    position = getattr(exc, "position", None) or (0, 0)
    return FileFormatError("importers.error.xml", line=position[0], column=position[1] + 1, reason=reason)


def _import_xml(src: str, size: int, opts: ImportOptions, writer: _Writer, cells: _Cells, rep: ImportReport,
                progress: Progress | None, cancel: CancelToken | None) -> None:
    import xml.etree.ElementTree as ET

    selector = _Selector(opts.record_tag, opts.record_path)
    names = _Names()
    parser = ET.XMLPullParser(events=("start", "end", "start-ns"))
    stack: list[Any] = []
    stack_names: list[str] = []
    state = {"record": 0, "root": False}
    carry = b""

    def handle(events: Any) -> None:
        for event, payload in events:
            if event == "start":
                stack.append(payload)
                stack_names.append(names(payload.tag))
                if not state["root"]:
                    state["root"] = True
                    rep.root_tag = stack_names[0]
                if not state["record"] and selector.matches(stack_names):
                    state["record"] = len(stack)
            elif event == "end":
                depth = len(stack)
                if depth == state["record"]:
                    writer.write(_element_value(payload, names, cells))
                    state["record"] = 0
                    if depth > 1:
                        stack[-2].remove(payload)
                    else:
                        payload.clear()
                stack.pop()
                stack_names.pop()
            else:                                   # start-ns
                prefix, uri = payload
                names.declare(prefix, uri)

    with open_shared(src) as raw:
        head = raw.read(4)
        _detect_encoding(head, None, src)          # gzip refused
        raw.seek(0)
        read = 0
        while True:
            chunk = raw.read(CHUNK_BYTES)
            if not chunk:
                break
            read += len(chunk)
            if not state["root"] and b"<!ENTITY" in carry + chunk:
                raise UserInputError("importers.error.xml_entities", path=src)
            carry = chunk[-8:]
            try:
                parser.feed(chunk)
                handle(parser.read_events())
            except ET.ParseError as exc:
                raise _xml_error(exc) from None
            if progress is not None:
                progress.update(read)
            check(cancel)
        try:
            parser.close()
            handle(parser.read_events())
        except ET.ParseError as exc:
            raise _xml_error(exc) from None
