"""Byte-level edits of JSON records and the document's formatting style (docs/ARCHITECTURE.md §12.4).

Pure functions over ``bytes`` (plus :func:`detect_style`, which samples a document): an edit
produces the record's new bytes by a *splice* of its current bytes — every byte outside the
edited region stays byte-identical, the new value is formatted like its surroundings.

* :func:`detect_style` → :class:`DocStyle`: newline, record separator, indentation unit and
  level, colon/comma spacing and the empty record container of a document.
* :func:`locate` / :func:`child_locs` → :class:`Loc`: nodes inside a record *value buffer*
  (offsets relative to the buffer), found with the core ``Scanner`` (``MODE_CONTAINER`` over
  one container at a time, so both core backends give identical results).
* :func:`check_json` (user input), :func:`check_unit` (our own output, before commit).
* Splices: :func:`set_value`, :func:`rename_key`, :func:`insert_child`,
  :func:`delete_children`, :func:`move_child`; formatting: :func:`format_value`,
  :func:`format_record`; :func:`convert_value` (type change table of §12.4).

Decisions (where §12.4 leaves room; all chosen to keep the user's data and layout safe)
======================================================================================
1. ``DocStyle`` has one extra field, ``comma`` (``b", "`` or ``b","``): the item separator
   used when a value is written on ONE line (compact records such as Python's
   ``json.dumps`` default ``{"a": 1, "b": 2}`` need it as much as ``colon``). Detected from
   the first separator inside a single-line container of the sampled records; default
   ``b", "`` when ``colon == b": "`` else ``b","``.
2. ``Loc`` has two extra fields, ``pstart`` / ``pend``: the span of the parent container
   (``None`` for the root). They decide "the container spans several lines" exactly and
   let :func:`rename_key` check key uniqueness.
3. A replaced ROOT value (record value, keyed member value, wrapper) is pretty-printed iff
   the document is (``style.unit is not None``) at ``base_level``; other values follow
   §12.4: pretty at :func:`line_level` of the node's line (the member's line for object
   members) when the parent container spans several lines, else compact.
4. An EMPTY container receiving a child is expanded (``{`` newline indent child newline
   indent ``}``) when the document is pretty and the container is the root or sits in a
   multi-line parent; otherwise ``{child}``. Whitespace inside the empty container is
   replaced.
5. :func:`insert_child` takes ``verbatim=True`` to insert value bytes unchanged (a
   duplicate placed in the same container is already formatted for that level); keys are
   ignored for array containers (moving a member into an array drops its key, §12.4).
6. :func:`move_child` keeps every child's bytes and the separators at their positions
   (same container = same level); ``style`` / ``base_level`` are accepted for the §12.4
   signature but not needed.
7. :func:`detect_style` reads at most 64 KiB (:data:`STYLE_SAMPLE_BYTES`): one window of
   ≤ 60 KiB (from the start of the JSON text, or around record 0 when it starts further
   in) plus the first record gap (≤ 4 KiB) when it lies outside the window. Only BASE
   spans are sampled (``doc.pinned(None)`` when available; spans outside the data file are
   ignored). The newline is decided by the first ``\\n`` of the sample. The indentation unit
   comes from the first sampled record that is a non-empty container: a single-line one →
   ``None`` (compact); a multi-line one → the indentation step after opening brackets when
   every step agrees (a clean run of 1..16 spaces or one tab), else ``None`` (messy text).
   Without such a record the lines of the sample decide the same way. JSON Lines are
   always compact. ``record_level`` is measured from record 0's line when the unit is known,
   else structural (array/keyed/brace_list 1, wrapped ``len(record_path) + 1``,
   jsonl/stream/single 0). ``single`` documents: ``record_sep = newline``, ``empty = b""``.
   Results are cached per document (weakly; a pinned view uses its origin's entry) once
   the index is complete.
8. :func:`check_unit` also requires the unit to have no outer whitespace (a unit is a span).
9. :func:`convert_value`: a whitespace-only string converts to ``{}`` / ``[]`` like ``""``;
   string → bool also accepts the Uzbek Cyrillic ``ҳа`` / ``йўқ``.
10. Error keys beyond §12: ``edits.error.not_member`` (rename of an array element or root),
    ``edits.error.not_container`` (children of a scalar), ``edits.error.bad_index``,
    ``edits.error.key_required`` (object child without a key).
11. :func:`locate` with a complete ``ipath`` also checks that every step still names the
    child (same decoded key / same index as the path segment): a stale reference fails
    with ``edits.error.path_not_found`` instead of editing a neighbour.
12. :func:`member_loc` (a root ``Loc`` over a member record ``"key": value``) and
    :func:`free_key` are helpers for the session and the bulk operations;
    :func:`style_from_sample` is the pure part of :func:`detect_style`;
    :func:`set_core_backend` lets tests run everything on one core backend.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import weakref
from array import array
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import ModuleType
from typing import Any, Iterable, Sequence

from . import codec, core
from .core.constants import MODE_CONTAINER, MODE_SINGLE
from .errors import ConversionError, EditError, JsonReshapeError
from .i18n import t
from .paths import canonical_index, format_path
from .textmatch import fold

log = logging.getLogger(__name__)

__all__ = [
    "JSON_TYPES",
    "STYLE_SAMPLE_BYTES",
    "DocStyle",
    "Loc",
    "check_json",
    "check_unit",
    "child_locs",
    "convert_value",
    "delete_children",
    "detect_style",
    "encode_key",
    "encode_string",
    "format_record",
    "format_value",
    "free_key",
    "insert_child",
    "line_level",
    "locate",
    "member_loc",
    "move_child",
    "rename_key",
    "set_core_backend",
    "set_value",
    "style_from_sample",
]

JSON_TYPES = ("object", "array", "string", "number", "bool", "null")

STYLE_SAMPLE_BYTES = 64 * 1024
"""Upper bound of the bytes :func:`detect_style` reads."""

_GAP_BYTES = 4 * 1024            # the first record gap is read separately up to this size
_LOOKBACK = 4 * 1024             # bytes read before record 0 when the window cannot start at the text start
_STYLE_MAX_RECORDS = 256         # record spans sampled by detect_style
_PREVIEW_CHARS = 60              # value text shown in conversion errors
_REFORMAT_SLICE = 1 << 20        # Reformatter feed size for big values

_WS_SET = frozenset(b" \t\r\n")
_WS_RE = re.compile(rb"[ \t\r\n]*")
_INDENT_RE = re.compile(rb"[ \t]*")
_STRING_TOKEN = rb'"[^"\\]*(?:\\.[^"\\]*)*"'
_STRING_RE = re.compile(_STRING_TOKEN, re.S)
_STRING_SPLIT_RE = re.compile(b"(" + _STRING_TOKEN + b")", re.S)
_MEMBER_HEAD_RE = re.compile(rb"[ \t\r\n]*(" + _STRING_TOKEN + rb")([ \t\r\n]*:[ \t\r\n]*)", re.S)
_QUOTE_OR_COLON_RE = re.compile(rb'[":]')
_QUOTE_OR_COMMA_RE = re.compile(rb'[",]')
_JSON_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")

_KIND_BY_BYTE = {0x7B: "object", 0x5B: "array", 0x22: "string", 0x74: "bool", 0x66: "bool", 0x6E: "null"}
_CONTAINERS = ("object", "array")

_TRUE_TEXTS = frozenset({"true", "1", "yes", "ha", "ҳа"})
_FALSE_TEXTS = frozenset({"false", "0", "no", "yo'q", "йўқ"})


# ----------------------------------------------------------------------------------
# core backend (tests run every function on both backends)
# ----------------------------------------------------------------------------------
_backend: ModuleType | None = None


def set_core_backend(backend: str | ModuleType | None) -> ModuleType | None:
    """Use a specific core backend (``"rust"`` / ``"python"`` or a backend module) for the
    Scanner and Reformatter of this module; ``None`` = the active ``jsonreshape.core``.
    Returns the previous override (tests and benchmarks)."""
    global _backend
    old = _backend
    _backend = core.get_backend(backend) if isinstance(backend, str) else backend
    return old


def _api() -> Any:
    return _backend if _backend is not None else core


# ----------------------------------------------------------------------------------
# data classes
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class DocStyle:
    """How a document is formatted (see :func:`detect_style`)."""

    newline: bytes = b"\n"
    """``b"\\r\\n"`` iff the first ``\\n`` of the sample is preceded by ``\\r``."""
    record_sep: bytes = b","
    """Between two records: comma kinds the first record gap (else ``b","`` + newline +
    indentation, compact ``b","``); jsonl the newline; stream the first gap if non-empty
    (else the newline); single the newline (unused)."""
    record_level: int = 0
    """Indentation level of the record lines (pretty ``[\\n  {`` → 1; jsonl/stream/single 0)."""
    unit: str | None = None
    """Indentation unit inside records (``"  "``, ``"    "``, ``"\\t"``); ``None`` = compact."""
    colon: bytes = b":"
    """Member colon: ``b": "`` or ``b":"``."""
    empty: bytes = b"[]"
    """The record container without records: ``b"[]"``, ``b"{}"`` or ``b""`` (jsonl, stream, single)."""
    comma: bytes = b","
    """Item separator of single-line containers: ``b", "`` or ``b","`` (decision 1)."""


@dataclass(frozen=True, slots=True)
class Loc:
    """A node inside a record VALUE buffer (offsets relative to the buffer)."""

    start: int                 # value span
    end: int
    mstart: int | None         # member start (key quote) for object members
    kend: int | None           # end of the key token
    key: str | int | None      # member key / element index / None for the root
    kind: str                  # object|array|string|number|bool|null
    index: int | None          # child index in the parent; None for the root
    depth: int                 # 0 = the root
    pstart: int | None = None  # span of the parent container (None for the root)
    pend: int | None = None

    @property
    def is_member(self) -> bool:
        return self.mstart is not None

    @property
    def node_start(self) -> int:
        """Start of the node including its key (the member start for object members)."""
        return self.start if self.mstart is None else self.mstart


# ----------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------
def _as_bytes(data: Any) -> bytes:
    if type(data) is bytes:
        return data
    if isinstance(data, str):
        raise TypeError("expected bytes, got str")
    return bytes(data)


def _strip_ws(data: bytes) -> bytes:
    s = _WS_RE.match(data).end()
    e = len(data)
    while e > s and data[e - 1] in _WS_SET:
        e -= 1
    return data if s == 0 and e == len(data) else data[s:e]


def _kind_of(byte: int) -> str:
    return _KIND_BY_BYTE.get(byte, "number")


def _decode_key(token: bytes) -> str:
    """Decode a JSON string token (quotes included) exactly like Python's ``json``."""
    if b"\\" not in token:
        return token[1:-1].decode("utf-8")
    return json.loads(token.decode("utf-8"))


def _path_text(path: Sequence[Any]) -> str:
    if not path:
        return "$"
    try:
        return format_path(tuple(path))
    except (TypeError, ValueError):  # pragma: no cover - odd segment types
        return repr(tuple(path))


def _not_found(path: Sequence[Any]) -> EditError:
    return EditError("edits.error.path_not_found", path=_path_text(path))


def _internal(what: str, data: bytes = b"", offset: int | None = None, code: str | None = None) -> EditError:
    """Log a broken invariant (a bug) and return the localized internal error."""
    if code is not None:
        detail = f"{t(f'json.error.{code}')} @{offset}"
    else:
        detail = t("edits.detail.shape")
    excerpt = b""
    if data:
        at = 0 if offset is None else max(0, min(offset, len(data)))
        excerpt = data[max(0, at - 40):at + 40]
    log.error("editops: %s failed (%s, offset %s); bytes around: %r", what, code or "shape", offset, excerpt)
    return EditError("edits.error.internal", detail=detail)


def _indent(unit: str, level: int) -> bytes:
    return (unit * max(0, level)).encode("ascii")


def _ws_level(ws: bytes, unit: str) -> int:
    """Indentation level of a run of spaces/tabs measured in ``unit``."""
    if unit == "\t":
        return ws.count(b"\t") + ws.count(b" ") // 4
    width = len(unit)
    return (ws.count(b" ") + ws.count(b"\t") * width) // width


def _indent_arg(unit: str) -> int | str:
    if unit == "\t":
        return "\t"
    if 1 <= len(unit) <= 16 and not unit.strip(" "):
        return len(unit)
    raise ValueError(f"unsupported indentation unit {unit!r}")


def free_key(taken: Any, base: str | int) -> str:
    """``base`` when it is not in ``taken``, else the first free ``base_2``, ``base_3``, …
    (duplicate/move key rule of §12.4; ``taken`` is any container of keys; an int base —
    the old index of an array element moved into an object — becomes its decimal text)."""
    base = str(base)
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


# ----------------------------------------------------------------------------------
# locating nodes
# ----------------------------------------------------------------------------------
def _scan_children(buf: bytes, start: int, end: int) -> array:
    """Child spans of the container ``buf[start:end]`` (absolute in ``buf``; object
    members span from the key quote to the end of the value)."""
    sc = _api().Scanner(MODE_CONTAINER, [], start)
    out = sc.feed(buf[start:end])
    out += sc.finish()
    err = sc.error
    if err is not None:
        raise _internal("container scan", buf, err[0], err[1])
    span = sc.container_span
    if span is None or (int(span[0]), int(span[1])) != (start, end):
        raise _internal("container scan", buf, start)
    spans = array("Q")
    spans.frombytes(out)
    return spans


class _Kids:
    """The children of one container (lazy key decoding)."""

    __slots__ = ("buf", "container", "spans", "n", "is_object")

    def __init__(self, buf: bytes, container: Loc) -> None:
        if container.kind not in _CONTAINERS:
            raise EditError("edits.error.not_container")
        self.buf = buf
        self.container = container
        self.is_object = container.kind == "object"
        self.spans = _scan_children(buf, container.start, container.end)
        self.n = len(self.spans) // 2

    def start(self, i: int) -> int:
        """Start of child ``i`` (the key quote for object members)."""
        return int(self.spans[2 * i])

    def end(self, i: int) -> int:
        return int(self.spans[2 * i + 1])

    def parts(self, i: int) -> tuple[int, int]:
        """``(key end, value start)`` of member ``i``."""
        buf = self.buf
        m = _STRING_RE.match(buf, self.start(i))
        if m is None:  # pragma: no cover - the scanner validated the member
            raise _internal("member key", buf, self.start(i))
        kend = m.end()
        colon = _WS_RE.match(buf, kend).end()
        if buf[colon:colon + 1] != b":":  # pragma: no cover - validated
            raise _internal("member colon", buf, colon)
        return kend, _WS_RE.match(buf, colon + 1).end()

    def key(self, i: int) -> str | int:
        if not self.is_object:
            return i
        kend, _ = self.parts(i)
        return _decode_key(self.buf[self.start(i):kend])

    def colon(self, i: int) -> bytes:
        """The bytes between member ``i``'s key and value (``b": "``, ``b":"`` …)."""
        kend, vstart = self.parts(i)
        return self.buf[kend:vstart]

    def find_key(self, key: str, *, last: bool = True, exclude: int | None = None) -> int | None:
        """Index of the member whose decoded key equals ``key`` (the last one, or the
        first with ``last=False``); ``None`` when there is none."""
        try:
            raw = key.encode("utf-8")
        except UnicodeEncodeError:
            raw = None  # lone surrogates: only an escaped key can hold them
        buf = self.buf
        found = None
        for i in range(self.n):
            if i == exclude:
                continue
            m = _STRING_RE.match(buf, self.start(i))
            if m is None:  # pragma: no cover - validated
                raise _internal("member key", buf, self.start(i))
            tok = m.group()
            if b"\\" in tok:
                hit = _decode_key(tok) == key
            else:
                hit = raw is not None and tok[1:-1] == raw
            if hit:
                if not last:
                    return i
                found = i
        return found

    def find(self, seg: Any) -> int | None:
        """Path-mode child lookup (dict semantics: the last duplicate key wins)."""
        if self.is_object:
            return self.find_key(seg) if type(seg) is str else None
        if type(seg) is int:
            idx: int | None = seg
        elif type(seg) is str:
            idx = canonical_index(seg)
        else:
            return None
        return idx if idx is not None and 0 <= idx < self.n else None

    def matches(self, i: int, seg: Any) -> bool:
        """ipath-mode check: child ``i`` is the one ``seg`` names."""
        if self.is_object:
            return type(seg) is str and self.key(i) == seg
        if type(seg) is int:
            return seg == i
        if type(seg) is str:
            return canonical_index(seg) == i
        return False

    def loc(self, i: int) -> Loc:
        c = self.container
        if self.is_object:
            kend, vstart = self.parts(i)
            ms = self.start(i)
            return Loc(vstart, self.end(i), ms, kend, _decode_key(self.buf[ms:kend]), _kind_of(self.buf[vstart]),
                       i, c.depth + 1, c.start, c.end)
        s = self.start(i)
        return Loc(s, self.end(i), None, None, i, _kind_of(self.buf[s]), i, c.depth + 1, c.start, c.end)


def _root_loc(buf: bytes) -> Loc:
    s = _WS_RE.match(buf).end()
    e = len(buf)
    while e > s and buf[e - 1] in _WS_SET:
        e -= 1
    if s >= e:
        raise _internal("locate (empty buffer)", buf, 0)
    return Loc(s, e, None, None, None, _kind_of(buf[s]), None, 0)


def locate(buf: bytes, path: Sequence[Any] = (), ipath: Sequence[int] | None = None) -> Loc:
    """The node at ``path`` inside the value buffer ``buf``.

    A complete ``ipath`` (child indices, same length as ``path``) selects duplicate keys
    exactly — each step must still name the child (the key equals the path segment, or
    the index equals the array segment), else the node is not found; otherwise the path
    segments are followed with dict semantics (the last duplicate wins; a canonical
    decimal ``str`` addresses an array element). ``EditError("edits.error.path_not_found")``.
    """
    buf = _as_bytes(buf)
    path = tuple(path)
    exact = ipath is not None and len(ipath) == len(path)
    cur = _root_loc(buf)
    for level, seg in enumerate(path):
        if cur.kind not in _CONTAINERS:
            raise _not_found(path)
        kids = _Kids(buf, cur)
        if exact:
            i = ipath[level]  # type: ignore[index]
            if type(i) is not int or not 0 <= i < kids.n or not kids.matches(i, seg):
                raise _not_found(path)
        else:
            found = kids.find(seg)
            if found is None:
                raise _not_found(path)
            i = found
        cur = kids.loc(i)
    return cur


def child_locs(buf: bytes, container: Loc) -> list[Loc]:
    """Every child of ``container`` (core ``Scanner`` in ``MODE_CONTAINER`` over it).
    ``EditError("edits.error.not_container")`` for a scalar."""
    buf = _as_bytes(buf)
    kids = _Kids(buf, container)
    return [kids.loc(i) for i in range(kids.n)]


def member_loc(buf: bytes) -> Loc:
    """The member record ``buf`` = ``"key": value`` (keyed / wrapped-object records) as a
    root :class:`Loc`: ``start``/``end`` = the value, ``mstart``/``kend`` = the key token,
    ``key`` decoded. :func:`set_value` replaces the value and :func:`rename_key` the record
    key with it (record-key uniqueness is the session's business)."""
    buf = _as_bytes(buf)
    m = _MEMBER_HEAD_RE.match(buf)
    if m is None:
        raise _internal("member record", buf, 0)
    vstart = m.end()
    e = len(buf)
    while e > vstart and buf[e - 1] in _WS_SET:
        e -= 1
    if vstart >= e:
        raise _internal("member record", buf, vstart)
    return Loc(vstart, e, m.start(1), m.end(1), _decode_key(m.group(1)), _kind_of(buf[vstart]), None, 0)


def line_level(buf: bytes, off: int, unit: str | None, base_level: int) -> int:
    """Indentation level of the line holding ``off``; offsets on the buffer's first line
    (and every offset when ``unit`` is ``None``) get ``base_level``."""
    if unit is None:
        return base_level
    nl = buf.rfind(b"\n", 0, off)
    if nl < 0:
        return base_level
    ws = _INDENT_RE.match(buf, nl + 1).group()
    return _ws_level(ws, unit)


# ----------------------------------------------------------------------------------
# validation and encoding
# ----------------------------------------------------------------------------------
def _chars(data: bytes, start: int, end: int) -> int:
    piece = data[start:end]
    try:
        return len(piece.decode("utf-8", "surrogatepass"))
    except UnicodeDecodeError:  # pragma: no cover - the scanner consumed valid UTF-8
        return len(piece.decode("utf-8", "replace"))


def _json_reason(code: str, expected: str, empty: bool) -> str:
    if empty:
        return t("edits.reason.empty")
    text = t("edits.reason.unexpected_eof") if code == "unexpected_eof" else t(f"json.error.{code}")
    if expected and expected != "none":
        exp = t("edits.reason.expected_end") if expected == "end" else t(f"json.expected.{expected}")
        if exp:
            text = f"{text}; {exp}"
    return text


def check_json(text: str | bytes) -> bytes:
    """Validate ONE JSON value (core ``Scanner`` ``MODE_SINGLE``); returns its UTF-8 bytes
    without the outer whitespace.

    ``EditError("edits.error.invalid_json", line, column, reason)`` otherwise; the error
    also has ``.char_offset`` (code points of the ORIGINAL input, outer whitespace
    included, so editors can convert it to UTF-16 positions), ``.offset`` (bytes),
    ``.line`` / ``.column`` (1-based, the column in code points) and the params ``code``
    / ``expected`` (core scanner codes).
    """
    if isinstance(text, str):
        data = text.encode("utf-8", "surrogatepass")  # lone surrogates -> invalid_utf8
    elif isinstance(text, (bytes, bytearray, memoryview)):
        data = bytes(text)
    else:
        raise TypeError(f"expected str or bytes, got {type(text).__name__}")
    sc = _api().Scanner(MODE_SINGLE, [], 0)
    out = sc.feed(data)
    out += sc.finish()
    err = sc.error
    if err is None:
        spans = array("Q")
        spans.frombytes(out)
        if len(spans) != 2:  # pragma: no cover - MODE_SINGLE emits exactly one span
            raise _internal("check_json", data, 0)
        return data[spans[0]:spans[1]]
    offset, code, expected, line0, line_start = err
    offset = max(0, min(int(offset), len(data)))
    line_start = max(0, min(int(line_start), offset))
    empty = not data.strip(b" \t\r\n")
    raise EditError(
        "edits.error.invalid_json",
        line=int(line0) + 1,
        column=_chars(data, line_start, offset) + 1,
        reason=_json_reason(code, expected, empty),
        char_offset=_chars(data, 0, offset),
        offset=offset,
        code=code,
        expected=expected,
    )


def check_unit(unit: bytes, *, member: bool) -> int:
    """Validate a unit before it is committed: a value (``MODE_SINGLE``) or, with
    ``member``, one object member (``b"{" + unit + b"}"`` in ``MODE_CONTAINER`` holding
    exactly one record). The unit must be exactly the value / member (no outer
    whitespace). Returns the scanner flags (``FLAG_*``). A failure is a bug: it is logged
    and raised as ``EditError("edits.error.internal")``."""
    unit = _as_bytes(unit)
    api = _api()
    if member:
        sc = api.Scanner(MODE_CONTAINER, [], 0)
        out = sc.feed(b"{")
        out += sc.feed(unit)
        out += sc.feed(b"}")
        out += sc.finish()
        want = (1, 1 + len(unit))
    else:
        sc = api.Scanner(MODE_SINGLE, [], 0)
        out = sc.feed(unit)
        out += sc.finish()
        want = (0, len(unit))
    err = sc.error
    if err is not None:
        shift = 1 if member else 0
        raise _internal("check_unit (member)" if member else "check_unit", unit, int(err[0]) - shift, err[1])
    spans = array("Q")
    spans.frombytes(out)
    if len(spans) != 2 or (spans[0], spans[1]) != want:
        raise _internal("check_unit (member)" if member else "check_unit", unit, 0)
    return int(sc.flags)


def encode_string(s: str) -> bytes:
    """The JSON string token of ``s`` (codec rules: non-ASCII raw, lone surrogates as
    ``\\udXXX`` escapes)."""
    if not isinstance(s, str):
        raise TypeError(f"expected str, got {type(s).__name__}")
    return codec.dumps(str(s))


def encode_key(s: str) -> bytes:
    """The JSON token of an object key (same rules as :func:`encode_string`)."""
    return encode_string(s)


# ----------------------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------------------
def _reformat(value: bytes, indent: int | str | None, base_level: int = 0) -> bytes:
    fmt = _api().Reformatter(indent=indent, base_level=max(0, base_level))
    if len(value) <= _REFORMAT_SLICE:
        return fmt.feed(value) + fmt.finish()
    view = memoryview(value)
    parts = [fmt.feed(view[i:i + _REFORMAT_SLICE]) for i in range(0, len(value), _REFORMAT_SLICE)]
    parts.append(fmt.finish())
    return b"".join(parts)


def _outside_strings(data: bytes, replacements: Sequence[tuple[bytes, bytes]]) -> bytes:
    """Apply byte replacements to the text OUTSIDE string tokens."""
    parts = _STRING_SPLIT_RE.split(data)
    for k in range(0, len(parts), 2):
        piece = parts[k]
        if piece:
            for old, new in replacements:
                piece = piece.replace(old, new)
            parts[k] = piece
    return b"".join(parts)


def format_value(value: bytes, level: int, style: DocStyle, *, compact: bool = False) -> bytes:
    """``value`` (valid JSON) formatted for a place whose line has indentation ``level``:
    pretty (children at ``level + 1``, the closing bracket at ``level``; ``style.newline``,
    ``style.unit``, ``style.colon``) unless ``compact`` or the document is compact, then on
    one line with ``style.colon`` / ``style.comma``. Strings, numbers and literals are
    never altered."""
    value = _strip_ws(_as_bytes(value))
    if not value or value[0] not in (0x7B, 0x5B):
        return value  # a scalar token
    if compact or style.unit is None:
        if style.colon == b": ":
            # one item per line without indentation, then joined: raw newlines exist only
            # outside strings, so two plain replaces are exact (and run at C speed)
            return _reformat(value, 0).replace(b",\n", style.comma).replace(b"\n", b"")
        out = _reformat(value, None)
        repl = []
        if style.colon != b":":
            repl.append((b":", style.colon))
        if style.comma != b",":
            repl.append((b",", style.comma))
        return _outside_strings(out, repl) if repl else out
    out = _reformat(value, _indent_arg(style.unit), level)
    if style.colon != b": ":
        out = _outside_strings(out, ((b": ", style.colon),))
    if style.newline != b"\n":
        out = out.replace(b"\n", style.newline)  # raw newlines exist only outside strings
    return out


def format_record(value: bytes, style: DocStyle, *, key: str | None = None,
                  member_head: bytes | None = None) -> bytes:
    """A NEW record's unit: ``value`` formatted at ``style.record_level`` (pretty iff the
    document is), preceded by ``member_head`` (the original ``"key": `` bytes of an edited
    member record, kept as they are) or, for a new member record, ``"key"`` + colon."""
    body = format_value(value, style.record_level, style)
    if member_head is not None:
        return _as_bytes(member_head) + body
    if key is not None:
        return encode_key(key) + style.colon + body
    return body


# ----------------------------------------------------------------------------------
# splices
# ----------------------------------------------------------------------------------
def _parent_multiline(buf: bytes, loc: Loc) -> bool:
    return loc.pstart is not None and loc.pend is not None and buf.find(b"\n", loc.pstart, loc.pend) >= 0


def set_value(buf: bytes, loc: Loc, value: bytes, style: DocStyle, *, base_level: int) -> bytes:
    """Replace the value at ``loc`` by ``value`` (valid JSON), formatted like its
    surroundings (module docstring, decision 3); every other byte is kept."""
    buf = _as_bytes(buf)
    if loc.pstart is None:
        pretty = style.unit is not None
        level = base_level
    else:
        pretty = style.unit is not None and _parent_multiline(buf, loc)
        level = line_level(buf, loc.node_start, style.unit, base_level)
    new = format_value(value, level, style, compact=not pretty)
    return b"".join((buf[:loc.start], new, buf[loc.end:]))


def rename_key(buf: bytes, loc: Loc, new_key: str) -> bytes:
    """Replace the key token of the member at ``loc``. ``EditError("edits.error.key_exists")``
    when another member of the same object has ``new_key``; renaming to the same key
    returns ``buf`` unchanged; ``EditError("edits.error.not_member")`` for array elements
    and the root."""
    buf = _as_bytes(buf)
    if not isinstance(new_key, str):
        raise TypeError(f"key must be str, got {type(new_key).__name__}")
    if loc.mstart is None or loc.kend is None:
        raise EditError("edits.error.not_member")
    if new_key == loc.key:
        return buf
    if loc.pstart is not None and loc.pend is not None:
        parent = Loc(loc.pstart, loc.pend, None, None, None, "object", None, max(0, loc.depth - 1))
        if _Kids(buf, parent).find_key(new_key, last=False, exclude=loc.index) is not None:
            raise EditError("edits.error.key_exists", key=new_key)
    return b"".join((buf[:loc.mstart], encode_key(new_key), buf[loc.kend:]))


def insert_child(buf: bytes, container: Loc, index: int, value: bytes, style: DocStyle, *,
                 key: str | None = None, base_level: int, verbatim: bool = False) -> bytes:
    """Insert ``value`` as child ``index`` (0..n; n = append) of ``container``.

    Objects need a ``key`` that is not in use (``edits.error.key_required`` /
    ``edits.error.key_exists``); arrays ignore it. Separators come from the container:
    the bytes between its first two children; with one child ``b","`` + the whitespace
    between the opening bracket and that child; an empty container is expanded in a
    pretty context (decision 4). The member colon comes from the container's own
    members, else ``style.colon``. The value is formatted like its siblings unless
    ``verbatim``.
    """
    buf = _as_bytes(buf)
    value = _strip_ws(_as_bytes(value))
    kids = _Kids(buf, container)
    n = kids.n
    if type(index) is not int or not 0 <= index <= n:
        raise EditError("edits.error.bad_index", index=index, count=n)
    head = b""
    if kids.is_object:
        if key is None:
            raise EditError("edits.error.key_required")
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        if kids.find_key(key, last=False) is not None:
            raise EditError("edits.error.key_exists", key=key)
        head = encode_key(key) + (kids.colon(0) if n else style.colon)
    unit = style.unit
    if n == 0:
        if unit is not None and (container.pstart is None or _parent_multiline(buf, container)):
            level = line_level(buf, container.node_start, unit, base_level)
            body = value if verbatim else format_value(value, level + 1, style)
            nl = style.newline
            inner = b"".join((nl, _indent(unit, level + 1), head, body, nl, _indent(unit, level)))
        else:
            inner = head + (value if verbatim else format_value(value, 0, style, compact=True))
        return b"".join((buf[:container.start + 1], inner, buf[container.end - 1:]))
    if verbatim:
        body = value
    elif unit is not None and buf.find(b"\n", container.start, container.end) >= 0:
        ref = index if index < n else n - 1
        body = format_value(value, line_level(buf, kids.start(ref), unit, base_level), style)
    else:
        body = format_value(value, 0, style, compact=True)
    text = head + body
    sep = buf[kids.end(0):kids.start(1)] if n >= 2 else b"," + buf[container.start + 1:kids.start(0)]
    if index == n:
        pos = kids.end(n - 1)
        return b"".join((buf[:pos], sep, text, buf[pos:]))
    pos = kids.start(index)
    return b"".join((buf[:pos], text, sep, buf[pos:]))


def delete_children(buf: bytes, container: Loc, indices: Iterable[int]) -> bytes:
    """Remove the children at ``indices`` (any order, repeats allowed) of ``container``.

    Sorted unique indices are grouped into maximal runs ``[a, b]``: ``b < n-1`` removes
    ``[start_a, start_{b+1})`` (member starts for objects); ``b == n-1`` (``a > 0``)
    removes ``[end_{a-1}, end_{n-1})``; all children → the container becomes ``{}`` /
    ``[]``. Ranges are computed on the original buffer and the result is built in one pass.
    """
    buf = _as_bytes(buf)
    kids = _Kids(buf, container)
    n = kids.n
    wanted = set()
    for i in indices:
        if type(i) is not int or not 0 <= i < n:
            raise EditError("edits.error.bad_index", index=i, count=n)
        wanted.add(i)
    if not wanted:
        return buf
    if len(wanted) == n:
        empty = b"{}" if kids.is_object else b"[]"
        return b"".join((buf[:container.start], empty, buf[container.end:]))
    order = sorted(wanted)
    runs: list[tuple[int, int]] = []
    a = prev = order[0]
    for i in order[1:]:
        if i != prev + 1:
            runs.append((a, prev))
            a = i
        prev = i
    runs.append((a, prev))
    ranges = [(kids.start(a), kids.start(b + 1)) if b < n - 1 else (kids.end(a - 1), kids.end(n - 1))
              for a, b in runs]  # a > 0 when b == n-1: deleting every child was handled above
    pieces = []
    pos = 0
    for lo, hi in ranges:
        pieces.append(buf[pos:lo])
        pos = hi
    pieces.append(buf[pos:])
    return b"".join(pieces)


def move_child(buf: bytes, container: Loc, src: int, dst: int, style: DocStyle, *, base_level: int) -> bytes:
    """Move child ``src`` of ``container`` so that its FINAL index is ``dst``: one rebuild
    of the affected children with the container's separators kept at their positions
    (``style`` / ``base_level``: decision 6)."""
    del style, base_level
    buf = _as_bytes(buf)
    kids = _Kids(buf, container)
    n = kids.n
    for i in (src, dst):
        if type(i) is not int or not 0 <= i < n:
            raise EditError("edits.error.bad_index", index=i, count=n)
    if src == dst:
        return buf
    lo, hi = min(src, dst), max(src, dst)
    order = list(range(lo, hi + 1))
    order.remove(src)
    order.insert(dst - lo, src)
    pieces = []
    for k, child in enumerate(order):
        if k:
            pos = lo + k  # separator between final positions pos-1 and pos stays in place
            pieces.append(buf[kids.end(pos - 1):kids.start(pos)])
        pieces.append(buf[kids.start(child):kids.end(child)])
    return b"".join((buf[:kids.start(lo)], *pieces, buf[kids.end(hi):]))


# ----------------------------------------------------------------------------------
# type conversion
# ----------------------------------------------------------------------------------
def _preview(value: Any) -> str:
    try:
        text = codec.dumps(value).decode("utf-8")
    except (TypeError, ValueError, UnicodeDecodeError):  # pragma: no cover - JSON values always encode
        text = repr(value)
    if len(text) > _PREVIEW_CHARS:
        text = text[:_PREVIEW_CHARS - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return text


def _conversion_error(value: Any, source: str, target: str) -> ConversionError:
    return ConversionError(value=_preview(value), source=source, target=target)


def _is_zero(v: Any) -> bool:
    if isinstance(v, (codec.JsonFloat, codec.JsonInt)):
        try:
            return Decimal(v.raw) == 0
        except (InvalidOperation, ValueError):  # pragma: no cover - raw is a JSON number
            return float(v) == 0
    return v == 0


def _parse_text(text: str) -> Any:
    try:
        return codec.loads(text)
    except (JsonReshapeError, ValueError, RecursionError):
        return _NOT_JSON


_NOT_JSON = object()


def convert_value(value: Any, to: str) -> Any:
    """Convert a JSON value (``codec`` types, exact numbers) to the JSON type ``to``
    (``object|array|string|number|bool|null``) by the table of §12.4. A value that already
    has that type is returned unchanged; an impossible conversion raises
    :class:`~jsonreshape.errors.ConversionError` (``edits.error.convert``)."""
    if to not in JSON_TYPES:
        raise ValueError(f"unknown JSON type {to!r}")
    src = codec.json_type(value)
    if src == to:
        return value
    if to == "null":
        return None
    if to == "string":
        if src == "number":
            return codec.number_text(value)
        if src == "bool":
            return "true" if value else "false"
        if src == "null":
            return ""
        return codec.dumps(value).decode("utf-8")
    if to == "number":
        if src == "string":
            token = value.strip()
            if _JSON_NUMBER_RE.fullmatch(token):
                return codec.loads(token)
        elif src == "bool":
            return 1 if value else 0
        elif src == "null":
            return 0
        raise _conversion_error(value, src, to)
    if to == "bool":
        if src == "string":
            folded, _ = fold(value.strip())
            if folded in _TRUE_TEXTS:
                return True
            if folded in _FALSE_TEXTS:
                return False
        elif src == "number":
            return not _is_zero(value)
        elif src == "null":
            return False
        raise _conversion_error(value, src, to)
    if to == "object":
        if src == "string":
            text = value.strip()
            if not text:
                return {}
            parsed = _parse_text(text)
            if isinstance(parsed, dict):
                return parsed
        elif src == "null":
            return {}
        raise _conversion_error(value, src, to)
    # to == "array"
    if src == "string":
        text = value.strip()
        if not text:
            return []
        parsed = _parse_text(text)
        if isinstance(parsed, list):
            return parsed
        return [value]
    if src == "null":
        return []
    return [value]


# ----------------------------------------------------------------------------------
# document style
# ----------------------------------------------------------------------------------
_style_cache: weakref.WeakKeyDictionary[Any, DocStyle] = weakref.WeakKeyDictionary()
_style_lock = threading.Lock()


def detect_style(doc: Any) -> DocStyle:
    """The formatting style of ``doc`` (module docstring, decision 7). Reads at most
    :data:`STYLE_SAMPLE_BYTES` through ``doc.open_data()``; cached per document once its
    index is complete (a pinned view shares its origin's entry)."""
    origin = getattr(doc, "origin", None)
    if origin is None:
        origin = doc
    try:
        with _style_lock:
            hit = _style_cache.get(origin)
    except TypeError:  # pragma: no cover - an object without weak references
        hit = None
    if hit is not None:
        return hit
    style = _detect_style(origin)
    if getattr(origin, "index_complete", True):
        try:
            with _style_lock:
                style = _style_cache.setdefault(origin, style)
        except TypeError:  # pragma: no cover
            pass
    return style


def _base_view(doc: Any) -> Any:
    """The document as stored in the file (no edits), when the reader offers it."""
    pinned = getattr(doc, "pinned", None)
    if callable(pinned):
        try:
            return pinned(None)
        except Exception:  # pragma: no cover - a reader without base views
            log.debug("detect_style: no base view of %r", doc, exc_info=True)
    return doc


def _sample_spans(base: Any, lo: int, hi: int) -> list[tuple[int, int]]:
    """Base spans of the first records, in file order, within the sample budget."""
    try:
        count = int(base.record_count)
    except Exception:  # pragma: no cover - defensive
        return []
    out: list[tuple[int, int]] = []
    for i in range(min(count, _STYLE_MAX_RECORDS)):
        try:
            s, e = base.record_span(i)
        except (IndexError, ValueError):
            break
        s, e = int(s), int(e)
        if not lo <= s < e <= hi or (out and s < out[-1][1]):
            break  # not a span of the data file (edited content) or not in file order
        out.append((s, e))
        if len(out) >= 2 and e - out[0][0] > STYLE_SAMPLE_BYTES:
            break  # (records 0 and 1 are always sampled: their gap is the separator)
    return out


def _detect_style(doc: Any) -> DocStyle:
    kind = str(doc.kind)
    info = doc.info
    data_start = int(getattr(info, "data_start", 0) or 0)
    data_size = int(doc.data_size)
    member = bool(getattr(doc, "member_records", False))
    record_path = getattr(doc, "record_path", None) or ()
    spans = _sample_spans(_base_view(doc), data_start, data_size)
    budget = STYLE_SAMPLE_BYTES - _GAP_BYTES
    if spans and spans[0][0] - data_start > budget // 2:
        w0 = max(data_start, spans[0][0] - _LOOKBACK)
    else:
        w0 = data_start
    w1 = min(data_size, w0 + budget)
    gap: bytes | None = None
    window = b""
    if w1 > w0 or len(spans) >= 2:
        with doc.open_data() as f:
            if w1 > w0:
                f.seek(w0)
                window = f.read(w1 - w0)
            if len(spans) >= 2:
                e0, s1 = spans[0][1], spans[1][0]
                if s1 <= w0 + len(window):
                    gap = window[e0 - w0:s1 - w0]
                elif s1 - e0 <= _GAP_BYTES:
                    f.seek(e0)
                    gap = f.read(s1 - e0)
    return style_from_sample(kind, window, w0=w0, data_start=data_start, spans=spans, gap=gap,
                             member=member, record_depth=len(record_path))


def _first_newline(buf: bytes, unknown_start: bool) -> bytes | None:
    i = buf.find(b"\n")
    if i == 0 and unknown_start:
        i = buf.find(b"\n", 1)
    if i < 0:
        return None
    return b"\r\n" if i > 0 and buf[i - 1] == 0x0D else b"\n"


def _line_indent(window: bytes, pos: int, known_start: bool) -> tuple[bytes | None, bool]:
    """(indentation of the line holding ``pos`` or ``None`` when unknown, whether only
    that indentation precedes ``pos`` on its line)."""
    nl = window.rfind(b"\n", 0, pos)
    if nl < 0 and not known_start:
        return None, False
    ls = nl + 1
    ws = _INDENT_RE.match(window, ls).group()
    return ws, ls + len(ws) == pos


def _ends_with_opener(line: bytes) -> bool:
    return line.rstrip(b" \t\r")[-1:] in (b"{", b"[")


def _unit_from_text(text: bytes, first_indent: bytes | None) -> str | None:
    """The indentation step of a multi-line text: every line deeper than the previous
    non-blank line must follow an opening bracket and add the same clean step."""
    lines = text.split(b"\n")
    prev = first_indent
    opener = _ends_with_opener(lines[0])
    steps: set[bytes] = set()
    for line in lines[1:]:
        ws = _INDENT_RE.match(line).group()
        rest = line[len(ws):].rstrip(b" \t\r")
        if not rest:
            continue
        if prev is not None and len(ws) > len(prev):
            if not opener or not ws.startswith(prev):
                return None
            steps.add(ws[len(prev):])
        prev = ws
        opener = rest[-1:] in (b"{", b"[")
    if len(steps) != 1:
        return None
    step = steps.pop()
    if step == b"\t":
        return "\t"
    if 1 <= len(step) <= 16 and not step.strip(b" "):
        return step.decode("ascii")
    return None


def _first_colon(buf: bytes, start: int, end: int) -> bytes | None:
    i = start
    while True:
        m = _QUOTE_OR_COLON_RE.search(buf, i, end)
        if m is None:
            return None
        p = m.start()
        if buf[p] == 0x22:
            sm = _STRING_RE.match(buf, p, end)
            if sm is None:
                return None  # a string cut by the sample end
            i = sm.end()
            continue
        nxt = buf[p + 1:p + 2]
        return b": " if nxt and nxt in b" \t\r\n" else b":"


def _first_inline_comma(buf: bytes, start: int, end: int) -> bytes | None:
    i = start
    while True:
        m = _QUOTE_OR_COMMA_RE.search(buf, i, end)
        if m is None:
            return None
        p = m.start()
        if buf[p] == 0x22:
            sm = _STRING_RE.match(buf, p, end)
            if sm is None:
                return None
            i = sm.end()
            continue
        j = p + 1
        while j < end and buf[j] in (0x20, 0x09):
            j += 1
        if j >= end:
            return None
        if buf[j] in (0x0D, 0x0A):
            i = j  # a line-break separator (pretty text): not an inline one
            continue
        return b", " if j > p + 1 else b","


def _is_empty_container(value: bytes) -> bool:
    return len(value) >= 2 and value[-1:] in (b"}", b"]") and not value[1:-1].strip(b" \t\r\n")


def style_from_sample(kind: str, window: bytes, *, w0: int, data_start: int, spans: Sequence[tuple[int, int]],
                      gap: bytes | None, member: bool, record_depth: int = 0) -> DocStyle:
    """The style decided from sampled bytes (the pure part of :func:`detect_style`).

    ``window`` = data-file bytes from offset ``w0``; ``spans`` = absolute spans of the
    first records in file order; ``gap`` = the bytes between records 0 and 1 (or
    ``None``); ``member`` = records are object members; ``record_depth`` = the length of
    the record path (wrapped documents).
    """
    known_start = w0 <= data_start
    w_end = w0 + len(window)
    newline = _first_newline(window, not known_start) or (_first_newline(gap, False) if gap else None) or b"\n"
    # records inside the window: (start, end, value start, complete), relative offsets
    recs: list[tuple[int, int, int, bool]] = []
    for s, e in spans:
        if s >= w_end or s < w0:
            break
        rs, re_ = s - w0, min(e, w_end) - w0
        vs = rs
        if member:
            m = _MEMBER_HEAD_RE.match(window, rs, re_)
            vs = m.end() if m is not None else re_
        recs.append((rs, re_, vs, e <= w_end))
    unit: str | None = None
    decided = kind == "jsonl"  # JSON Lines records are always one line
    if not decided:
        for rs, re_, vs, complete in recs:
            if vs >= re_ or window[vs] not in (0x7B, 0x5B):
                continue  # a scalar (or a cut member head): no evidence
            value = window[vs:re_]
            if complete and _is_empty_container(value):
                continue
            decided = True
            if b"\n" in value:
                unit = _unit_from_text(value, _line_indent(window, vs, known_start)[0])
            break
    if not decided:
        if known_start:
            unit = _unit_from_text(window, _INDENT_RE.match(window).group())
        else:
            nl = window.find(b"\n")
            if nl >= 0:
                rest = window[nl + 1:]
                unit = _unit_from_text(rest, _INDENT_RE.match(rest).group())
    first_indent, begins_line = (_line_indent(window, recs[0][0], known_start) if recs else (None, False))
    # record level
    if kind in ("jsonl", "stream", "single"):
        level = 0
    else:
        level = record_depth + 1 if kind == "wrapped" else 1
        if unit is not None and first_indent is not None:
            level = _ws_level(first_indent, unit)
    # record separator
    if kind in ("jsonl", "single"):
        record_sep = newline
    elif kind == "stream":
        ok = gap is not None and 0 < len(gap) <= _GAP_BYTES and not gap.strip(b" \t\r\n")
        record_sep = gap if ok else newline  # type: ignore[assignment]
    else:
        if gap is not None and len(gap) <= _GAP_BYTES and gap.strip(b" \t\r\n") == b",":
            record_sep = gap
        elif begins_line and first_indent is not None:
            record_sep = b"," + newline + first_indent
        elif unit is not None:
            record_sep = b"," + newline + _indent(unit, level)
        else:
            record_sep = b","
    # colon and inline comma
    colon = None
    for rs, re_, _vs, _complete in recs:
        colon = _first_colon(window, rs, re_)
        if colon is not None:
            break
    if colon is None and known_start:
        colon = _first_colon(window, 0, recs[0][0] if recs else len(window))
    if colon is None:
        colon = b": " if unit is not None else b":"
    comma = None
    for _rs, re_, vs, _complete in recs:
        comma = _first_inline_comma(window, vs, re_)
        if comma is not None:
            break
    if comma is None:
        comma = b", " if colon == b": " else b","
    # the empty record container
    if kind in ("array", "brace_list"):
        empty = b"[]"
    elif kind == "keyed":
        empty = b"{}"
    elif kind == "wrapped":
        empty = b"{}" if member else b"[]"
    else:
        empty = b""
    return DocStyle(newline=newline, record_sep=record_sep, record_level=level, unit=unit, colon=colon,
                    empty=empty, comma=comma)
