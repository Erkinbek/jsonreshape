"""Pure-Python reference implementation of the byte-level core (docs/ARCHITECTURE.md §1).

Exports exactly the API of the Rust extension ``_rscore``: :class:`Scanner`,
:class:`Reformatter`, :class:`MultiFinder`, :class:`PathExtractor` and
:func:`numbers_plain`. This module is the *reference*: the Rust backend must return
byte-identical results. Implementation modules:

* ``_py_patterns``  – expanded fast-path regexes (``V_k`` value patterns, flag variants)
* ``_py_scanbase``  – exact, resumable token-level state machine of the Scanner
* ``_py_scanner``   – Scanner main loop + record fast loops
* ``_py_reformat``  – Reformatter
* ``_py_tools``     – numbers_plain, MultiFinder (``locate_tokens`` included), PathExtractor

Interpretations
===============
Where the specification leaves room, the reference behaves as follows (the Rust port
mirrors every point).

Scanner — general
-----------------
1. *Emission timing.* A record span is returned by the ``feed()``/``finish()`` call that
   consumes the byte completing the record: the closing quote/bracket or last literal
   byte; a number completes at the first following byte that cannot continue it (or at
   EOF, i.e. in ``finish()``); a MODE_LINES record is returned when the ``\\n`` ending its
   line is consumed (or by ``finish()`` for a last line without ``\\n``). Spans are
   returned in document order. ``record_count`` is updated at the end of every call.
2. ``consumed`` = ``base_offset`` + number of bytes passed to ``feed()`` so far, and
   ``lines`` = number of ``\\n`` bytes passed to ``feed()`` so far — both keep counting
   after an error (``feed()`` then ignores the data and returns ``b""``).
3. *Flags* are set only by complete constructs consumed before the stop point:
   ``FLAG_ESCAPES`` by a complete escape (``\\n``… or a complete ``\\uXXXX``),
   ``FLAG_U_ESCAPES`` by a complete ``\\uXXXX``, ``FLAG_NON_ASCII`` by a complete valid
   multi-byte UTF-8 sequence. A construct cut by an error sets nothing. In MODE_LINES
   the flags of content before an error on a broken line are kept.
4. *Expected labels inside tokens*: string content, control characters, invalid UTF-8
   and EOF inside a string → ``string_end``; byte after a backslash → ``escape``; inside
   ``\\uXXXX`` → ``hex``; numbers → ``digit``; literals → ``literal``; ``depth_limit`` and
   ``target_*`` → ``none``; ``trailing_data`` → ``end``. MODE_STREAM top level and a
   MODE_LINES line start use ``value``.
5. *EOF inside a UTF-8 sequence* of a string is ``unexpected_eof``/``string_end`` at the
   end offset (the sequence is truncated, not invalid). An invalid sequence is
   ``invalid_utf8`` at its lead byte (= ``UnicodeDecodeError.start``), even when the
   offending byte lies in a later chunk.
6. *Depth*: every ``[``/``{`` counts one level, including the root container and the
   root ``{`` of a brace list; ``depth_limit`` is reported at the bracket that would make
   the depth exceed ``max_depth``.
7. MODE_STREAM: a value may directly follow the previous one when the previous token
   ended unambiguously: ``1"a"``, ``[1][2]``, ``truefalse``, ``1-2`` (= ``1``, ``-2``)
   and ``0123`` (= ``0``, ``123``) are valid streams of two values.
8. MODE_CONTAINER target path: keys are compared after JSON decoding with Python ``json``
   semantics (an escaped surrogate pair is one code point, lone surrogate escapes stay
   lone code points). At intermediate levels every member whose key matches is on the
   path (a later duplicate key can still lead to the target); the *first* value at the
   full path becomes the target. ``target_not_found`` is reported by ``finish()`` at the
   end offset, only after the whole document validated (an earlier syntax error or
   ``trailing_data`` wins). ``target_not_container`` is reported at the first byte of a
   scalar found at the full path (``"``, ``-``, a digit, ``t``/``f``/``n``) without
   validating the scalar; a byte that cannot start any value there is
   ``unexpected_char``. Object target records span from the key's opening quote to
   the end of the value. ``container_span`` = (offset of the target's opening bracket,
   offset after its closing bracket).
9. API misuse: ``feed()`` after ``finish()`` raises ``ValueError``; a second ``finish()``
   returns ``b""``. Constructor ``ValueError``: unknown mode; ``target_path`` segment not
   ``str``/``int`` (``bool`` rejected) or a negative index; non-empty ``target_path``
   outside MODE_CONTAINER; ``base_offset < 0``; ``max_depth < 1``. ``feed()`` accepts any
   bytes-like object (``str`` → ``TypeError``). ``take_broken()`` returns ``[]`` outside
   MODE_LINES.

Scanner — MODE_LINES
--------------------
10. The ``\\n`` byte is never part of a line document; it acts exactly like EOF for that
    document: an unterminated string/escape/``\\u``/literal/number/container ends in
    ``unexpected_eof`` at the ``\\n`` offset with the label of the interrupted state
    (e.g. ``"abc\\`` → ``escape``, ``-`` → ``digit``, ``tr`` → ``literal``,
    ``[1,`` → ``value``). A ``\\n`` interrupting a UTF-8 sequence is likewise
    ``unexpected_eof``/``string_end`` at the ``\\n`` offset.
11. ``take_broken()`` tuples ``(line_no, line_start, line_end, err_offset, code,
    expected)``: ``line_end`` = offset of the terminating ``\\n`` (or the EOF offset);
    a line is reported when its end has been consumed (the ``\\n``, or EOF in ``finish()``).
    Only the first error of a line is reported; the rest of the line is skipped.

Reformatter
-----------
12. Levels may become negative for fragments (e.g. a wrapper suffix); indentation is
    ``max(0, base_level + level)`` units. ``base_level`` must be an ``int >= 0``.
    ``feed()`` after ``finish()`` raises ``ValueError``; a second ``finish()`` returns
    ``b""``. Bytes that are not whitespace, structure or strings are copied unchanged
    (no validation).

MultiFinder
-----------
13. ``find_first``: ``end=None`` means ``len(buf)``; ``ValueError`` if ``start < 0``,
    ``end < 0`` or ``end > len(buf)``; ``start >= end`` returns ``-1``.
    ``find_in_spans``: ``ValueError`` when ``len(spans)`` is not a multiple of 16 or a
    span has ``start > end`` or ``end > len(buf)``. A ``str`` needle is a ``TypeError``.
    Unsorted or overlapping spans (a caller error) give an unspecified result, never a crash.
    ``locate_tokens``: the bounds of every span are checked before anything else; then
    each span is validated in order (spans without any occurrence too) and the first
    invalid one raises ``ValueError`` (no partial result). The occurrence that locates a
    token is found lazily: the leftmost one lying inside the span that starts at or after
    the end of the last token examined; it locates the token it starts in, or is ignored
    when it starts on whitespace or structure (the search resumes at the next token's
    start); after a located token the search resumes at the token's end. This counts every
    occurrence (overlapping ones included) exactly once per token. Duplicate keys are
    checked only in spans with an occurrence; the ``kind 5`` entry is appended only when
    at least one token was located. ``member_spans`` is taken by truth value. Results are
    ``int`` / ``tuple`` objects (the Rust backend shares equal key strings and paths
    between tuples; values are identical).

PathExtractor
-------------
14. Every span must hold exactly one valid JSON value (``"key": value`` with
    ``member_spans``), optionally surrounded by whitespace; anything else raises
    ``ValueError`` (validation is the Scanner's, nesting up to 100000 levels).
15. Results are ordered by span, then path, then the dict-semantics traversal order:
    ``("any_key",)`` yields one value per distinct decoded key in order of first
    appearance with the value of the last occurrence, so with duplicate keys the start
    offsets are not necessarily increasing.
16. Malformed segments (unknown tag, wrong arity, non-``str`` key, negative or ``bool``
    index) raise ``ValueError``; an empty list of paths is allowed.

numbers_plain
-------------
17. Implemented as one regular expression over the token stream; for invalid JSON it
    returns some ``bool`` (never raises, except ``TypeError`` for ``str`` input).

General
-------
18. Every ``bytes`` parameter also accepts ``bytearray``/``memoryview`` (copied); ``str``
    raises ``TypeError``. ``ValueError`` messages are developer-facing English text (callers
    translate them into localized errors); only the exception type is part of the contract.
"""

from __future__ import annotations

from ._py_reformat import Reformatter
from ._py_scanner import Scanner
from ._py_tools import MultiFinder, PathExtractor, numbers_plain
from .constants import *  # noqa: F401,F403  (same constants as the Rust module)

__all__ = ["MultiFinder", "PathExtractor", "Reformatter", "Scanner", "numbers_plain"]
