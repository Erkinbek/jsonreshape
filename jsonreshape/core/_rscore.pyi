"""Typing stub for the Rust core extension ``jsonreshape.core._rscore``.

Same API, results and exceptions as the pure-Python reference ``jsonreshape.core._pycore``
(docs/ARCHITECTURE.md §1; the interpretations are listed in the ``_pycore`` docstring).

* Byte arguments: ``bytes`` is used without copying; any other object goes through
  ``bytes(obj)`` (``bytearray``, ``memoryview``, ``mmap``, ``array('Q')`` ...); ``str`` is a
  ``TypeError``.
* Span arrays are little-endian ``uint64`` values.
* Inputs of 256 KiB or more are processed with the GIL released.
* Integer arguments must be ``int`` (``bool`` is rejected with ``ValueError``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Final, TypeAlias

Buffer: TypeAlias = bytes | bytearray | memoryview

MODE_CONTAINER: Final[int]
MODE_BRACE_LIST: Final[int]
MODE_LINES: Final[int]
MODE_STREAM: Final[int]
MODE_SINGLE: Final[int]
FLAG_ESCAPES: Final[int]
FLAG_U_ESCAPES: Final[int]
FLAG_NON_ASCII: Final[int]
DEFAULT_MAX_DEPTH: Final[int]
ERR_UNEXPECTED_CHAR: Final[str]
ERR_UNEXPECTED_EOF: Final[str]
ERR_INVALID_NUMBER: Final[str]
ERR_INVALID_LITERAL: Final[str]
ERR_INVALID_ESCAPE: Final[str]
ERR_INVALID_HEX: Final[str]
ERR_CONTROL_CHAR: Final[str]
ERR_INVALID_UTF8: Final[str]
ERR_TRAILING_DATA: Final[str]
ERR_DEPTH_LIMIT: Final[str]
ERR_TARGET_NOT_FOUND: Final[str]
ERR_TARGET_NOT_CONTAINER: Final[str]
__version__: Final[str]

class Scanner:
    """Validating, resumable record indexer (§1.2).

    ``error`` is ``(offset, code, expected, line, line_start)``; ``take_broken()`` returns
    ``(line_no, line_start, line_end, err_offset, code, expected)`` tuples (MODE_LINES;
    ``[]`` in other modes). ``consumed`` / ``lines`` count every byte / ``\\n`` passed to
    ``feed()`` (also after an error, when ``feed()`` ignores the data and returns ``b""``).

    ``ValueError``: unknown mode; ``target_path`` not a list/tuple of ``str`` / ``int >= 0``
    segments, or non-empty outside MODE_CONTAINER; ``base_offset < 0``; ``max_depth < 1``;
    ``feed()`` after ``finish()`` (a second ``finish()`` returns ``b""``).
    """

    def __init__(
        self,
        mode: int,
        target_path: Sequence[str | int] | None = ...,
        base_offset: int = 0,
        max_depth: int = 100000,
    ) -> None: ...
    def feed(self, chunk: Buffer) -> bytes: ...
    def finish(self) -> bytes: ...
    def take_broken(self) -> list[tuple[int, int, int, int, str, str]]: ...
    @property
    def error(self) -> tuple[int, str, str, int, int] | None: ...
    @property
    def record_count(self) -> int: ...
    @property
    def consumed(self) -> int: ...
    @property
    def lines(self) -> int: ...
    @property
    def flags(self) -> int: ...
    @property
    def target_found(self) -> bool: ...
    @property
    def container_span(self) -> tuple[int, int] | None: ...
    @property
    def finished(self) -> bool: ...

class Reformatter:
    """Streaming whitespace normaliser (§1.4).

    ``indent``: ``None`` (compact; nothing is ever held back), an ``int`` 0..16 or ``"\\t"``;
    ``base_level``: ``int >= 0``. ``ValueError`` otherwise, and for ``feed()`` after
    ``finish()``.
    """

    def __init__(self, indent: int | str | None = None, base_level: int = 0) -> None: ...
    def feed(self, chunk: Buffer) -> bytes: ...
    def finish(self) -> bytes: ...

LocatedToken: TypeAlias = tuple[int, int, int, int, tuple[str | int, ...]]

class MultiFinder:
    """Multi-literal byte search (§1.5).

    ``ValueError`` for no needles, an empty needle or a ``bytes``/``str`` instead of a list;
    ``TypeError`` for a ``str`` needle. ``find_first``: ``ValueError`` if ``start < 0``,
    ``end < 0`` or ``end > len(buf)``; ``start >= end`` returns ``-1``.

    ``locate_tokens``: ``(span_idx, kind, token_start, token_end, path)`` for every
    key/scalar token in which a needle occurrence (lying inside its span) starts, once,
    in document order -- kind 0 string value, 1 object key (path of its member value), 2
    number, 3 ``true``/``false``/``null``, 4 the member's own key (``member_spans``) --
    plus ``(span_idx, 5, 0, 0, ())`` after the tokens of a span whose record repeats an
    object key. Offsets are absolute in ``buf`` (quotes included); ``path`` holds decoded
    ``str`` keys and ``int`` indices relative to the record value. ``ValueError`` for an
    out-of-bounds span or a span that is not one JSON value (an object member with
    ``member_spans``).
    """

    def __init__(self, needles: Iterable[Buffer]) -> None: ...
    def find_in_spans(self, buf: Buffer, spans: Buffer) -> bytes: ...
    def find_first(self, buf: Buffer, start: int = 0, end: int | None = None) -> int: ...
    def locate_tokens(self, buf: Buffer, spans: Buffer, member_spans: bool = False) -> list[LocatedToken]: ...

class PathExtractor:
    """Value spans at paths (§1.6). Segments: ("key", str), ("index", int >= 0),
    ("any_index",), ("any_key",). ``extract`` returns u64 LE quads
    ``(span_idx, path_idx, start, end)``; invalid JSON in a span (or nesting deeper than
    100000) raises ``ValueError``, as do malformed paths/segments."""

    def __init__(self, paths: Iterable[Sequence[tuple[Any, ...]]], member_spans: bool = False) -> None: ...
    def extract(self, buf: Buffer, spans: Buffer) -> bytes: ...

def numbers_plain(buf: Buffer) -> bool:
    """True iff every number token outside strings is reproduced exactly by orjson (§1.3)."""
