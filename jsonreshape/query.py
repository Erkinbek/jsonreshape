r"""Filter language, JMESPath adapter and the visual filter-builder model.

Implements docs/ARCHITECTURE.md §6 (query part) and §9; requirements AGENT_PROMPT §6.3 and
§14 test 18.

* :func:`compile_filter` turns a filter expression (``lang="expr"``) or a JMESPath
  expression (``lang="jmespath"``) into a :class:`Filter`;
* :func:`parse_filter` returns the AST of an expression and :func:`format_expr` renders an
  AST back to canonical text;
* :class:`Condition` / :class:`Group` model the GUI visual filter builder.
  :meth:`Group.to_text` renders a valid expression and :meth:`Filter.from_builder` builds
  the same AST directly from the model; both always give identical results.
  :func:`builder_from_text` is the inverse for the expressions the builder can express
  (``None`` for the others).

Safety: expressions are read by a hand-written lexer and recursive-descent parser and are
evaluated by closures built from the AST. The ``eval``/``exec``/``compile`` builtins are
never used (a test scans this module's AST).

Expression syntax
-----------------
::

    expr     := or
    or       := and (("or" | "||") and)*
    and      := not (("and" | "&&") not)*
    not      := ("not" | "!") not | cmp
    cmp      := operand ( cmpop operand
                        | "between" operand "and" operand
                        | ["not"] "in" operand
                        | "is" ["not"] ("null" | "empty" | "missing")
                        | ("contains" | "startswith" | "endswith" | "matches" | "~") operand )?
    cmpop    := "==" | "=" | "!=" | "≠" | "<" | "<=" | "≤" | ">" | ">=" | "≥"
    operand  := literal | list | object | call | path | "(" expr ")"
    literal  := string | number | "true" | "false" | "null"
    list     := "[" [expr ("," expr)*] "]"
    object   := "{" [string ":" constant ("," string ":" constant)*] "}"
    call     := name "(" [expr ("," expr)*] ")"

* Precedence: ``not`` > ``and`` > ``or``. A comparison binds tighter than ``not``
  (``not a == 1`` means ``not (a == 1)``); comparisons do not chain (``a < b < c`` is a
  syntax error).
* Keywords (``and or not in is between contains startswith endswith matches true false
  null``) are case-insensitive (``AND`` works). ``empty``/``missing`` are special only
  after ``is``, and function names only right before ``(`` — so ``type == "x"`` reads the
  field ``type``.
* Strings: ``"..."`` or ``'...'`` with JSON escapes (``\" \' \\ \/ \b \f \n \r \t \uXXXX``;
  surrogate pairs are combined exactly like ``json.loads`` does). Raw characters
  (including newlines) are allowed inside quotes.
* Numbers: JSON number tokens ``-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?`` decoded by
  :func:`jsonreshape.codec.loads`, so ``1.10``, ``1e400`` and ``12345678901234567890`` are
  exactly the values a data file would produce. Malformed tokens (``01``, ``1.``, ``.5``,
  ``1e``) are errors.
* Object literals are an *extension* of §9 so that every JSON value (e.g. a builder value)
  can be written; their member values must be constants.

Paths (the §3 syntax inside expressions): ``a.b[0].c``, ``a["x.y"]``, ``a['x y']``,
``a[*]`` / ``a[]`` (any index), ``*`` (any key), ``**`` and ``a..b`` (descendants),
backquoted names ``\`my key\```, ``$`` (the record value), ``$.a`` / ``$["and"]``
(explicit root), ``$key`` (member key of a keyed-root record) and ``$index`` (the 0-based
record number). There is no whitespace inside a path except within brackets. An unquoted
key is a run of characters other than whitespace and
``. [ ] ( ) { } " ' ` , = ! < > ~ & | : ; ? / \ + % ^ ≠ ≤ ≥ “ ” „ « »``; the *first*
segment must not start with a digit, ``-``, ``$``, ``#`` or a curly quote and must not be
a keyword — write ``$["and"]`` or ``\`and\``` instead (after a dot keywords are ordinary
keys: ``x.and``). A leading ``[`` always starts a list literal, so a bracketed first
segment needs the ``$`` prefix: ``$["my key"]``. JSON Pointer syntax (``/a/0``) is not
accepted inside expressions.

Semantics (§9)
--------------
* A path yields the list of values it resolves to (empty when missing, several for
  wildcards; dict semantics of :meth:`jsonreshape.paths.PathPattern.values`). A literal
  yields one value; a list literal yields one list holding *all* values of its items (a
  missing item contributes nothing); ``$key`` / ``$index`` yield nothing when not given.
* ``a OP b`` for ``== < <= > >= contains startswith endswith matches`` is true iff some
  value of ``a`` and some value of ``b`` satisfy it; ``a in b`` iff ``b contains a`` for
  some values; ``x between lo and hi`` iff ``lo <= x <= hi`` (inclusive) for some values,
  where reversed bounds are swapped (``x between 9 and 2`` is ``x between 2 and 9``, the
  rule of ``search --number '9..2'``; the builder's ``between`` is the same).
  ``a != b`` is ``not (a == b)``, ``a not in b`` is ``not (a in b)`` and ``is not X`` is
  ``not (is X)`` — so ``city != "X"`` also matches records without ``city``.
* Equality: no type coercion (``"1" != 1``, ``true != 1``, ``null != false``); numbers
  compare by their exact decimal value (``1 == 1.0``, ``1.10 == 1.1``, big integers
  exactly, ``1e400 < 2e400``, ``1e-400 > 0``, ``12345678901234567890.5 <
  12345678901234567891.5``) -- the comparison of ``search --number`` and of the sort keys,
  so the three agree. Plain numbers (``int`` / ``float`` that ``orjson`` reproduces) are
  compared natively, which is exact for them; whenever one side is a
  :class:`~jsonreshape.codec.JsonFloat` its token text is compared exactly
  (:func:`jsonreshape.textmatch.number_key`). ``Decimal`` values, e.g. from ijson, are
  first converted with ``codec.loads(str(d))``. Strings compare exactly; arrays and
  objects deeply. The equality is public as :func:`json_equal` (``_eq`` is its private alias).
* Ordering (``< <= > >=`` and ``between``): number–number (exact, as above) or
  string–string (Unicode code point order) only; anything else is false.
* ``contains``: substring for strings, element (deep equality) for arrays, key for objects.
  ``startswith`` / ``endswith``: strings only. ``matches`` / ``~``: Python ``re.search`` on
  string values. A literal pattern is compiled when the filter is compiled (an invalid one
  raises :class:`~jsonreshape.errors.QuerySyntaxError` pointing at the exact character,
  with the localized description of :func:`jsonreshape.textmatch.regex_error_text` --
  the same texts as ``search --regex``); a pattern taken from the data that is not a
  valid regex matches nothing.
* ``is null``: present and null. ``is empty``: present and ``""`` / ``[]`` / ``{}``.
  ``is missing``: no value.
* Functions (one argument each): ``len(x)`` — length of the *first* value (string
  characters, array/object size; nothing for other values); ``exists(x)``,
  ``missing(x)``, ``empty(x)`` (≡ ``x is empty``) — booleans; ``lower``/``upper``/``trim``
  — map over the string values (other values are dropped); ``type(x)`` — one of
  ``object array string number bool null`` per value; ``number(x)`` — numbers unchanged,
  numeric strings parsed exactly (others dropped); ``string(x)`` — strings unchanged,
  exact number text, ``true``/``false``/``null``, compact JSON for containers.
* A bare operand used as a condition is truthy when some value is not
  ``false``/``null``/``""``/``0``/``[]``/``{}`` (``0`` exactly: ``1e-400`` is truthy).

JMESPath (``lang="jmespath"``): ``jmespath.compile(text).search(value)`` with JMESPath
truthiness (``false``, ``null``, ``""``, ``[]``, ``{}`` are false; ``0`` is true). The
built-in functions accept :class:`~jsonreshape.codec.JsonFloat` /
:class:`~jsonreshape.codec.JsonInt` as numbers, and ``to_string`` writes exact numbers and
raw (unescaped) non-ASCII text through :func:`jsonreshape.codec.dumps`. An expression that
cannot be evaluated on a record (``contains(tags, 'x')`` with ``tags: null``, ``n >
`1``` with a string ``n``, ``floor(1e400)``) raises :class:`FilterEvalError` from
:meth:`Filter.matches`: a localized sentence (``reason``) with JMESPath's own text as
``detail``. :func:`jsonreshape.recordset.filter_records` treats such a record as not
matching and counts it (the filter never aborts because of one record -- only when the
expression fails on every record it is evaluated on). Syntax errors are localized
:class:`~jsonreshape.errors.QuerySyntaxError` with the position, also those of the
JMESPath lexer (an unclosed ``'...'`` / ``"..."`` / backtick value, a backtick value that
is not JSON, an unexpected character, an invalid quoted name); JMESPath's own English
message is kept only as the parameter ``raw``.

Huge records
------------
:meth:`Filter.matches_stream` evaluates an expression filter on a record that is never
loaded (records over ``Settings.large_node_bytes``): ``len()``, ``exists`` / ``missing``
/ ``is missing``, ``is null``, ``is empty``, bare truthiness and comparisons of a path (or
of ``lower/upper/trim/type/number`` of a path) with constants -- ``== != < <= > >=
contains startswith endswith matches in between``, also ``"x" in path`` -- are decided
on the ijson event stream (:func:`jsonreshape.stream.stream_probe`) without building the
matched containers or keeping the matched values. Every other part of the expression
gets the values of its paths from :func:`jsonreshape.stream.stream_values` within a byte
budget (``max_bytes``); a record whose values would exceed it raises
:class:`~jsonreshape.stream.CaptureLimitExceeded` (the caller skips the record with a
warning). Results equal :meth:`Filter.matches` on the parsed record (dict semantics for
duplicate keys included: a record with a duplicate key on a matched path is evaluated
from captured values).

Named filters
-------------
:class:`FilterPresets` stores named filters (``app_home()/filter_presets.json``;
AGENT_PROMPT §6.3 "Filtrlar nom bilan saqlanadi (preset)"). Changes are made under an
inter-process lock (:func:`file_lock`, also used by the search history), so presets
saved by several processes at the same time are all kept.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import operator
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Union

import jmespath
from jmespath import exceptions as jmespath_exceptions
from jmespath import functions as jmespath_functions
from jmespath import visitor as jmespath_visitor

from . import codec
from .appdirs import app_home
from .errors import JsonReshapeError, PathSyntaxError, QuerySyntaxError, UserInputError
from .fileio import atomic_write, long_path, make_dirs, open_shared, os_error, read_limited
from .i18n import t
from .paths import PathPattern, Wild, format_path, parse_pattern
from .stream import DuplicateKeys, stream_probe, stream_values
from .textmatch import compare_numbers, number_key, regex_error_text

log = logging.getLogger(__name__)

__all__ = [
    "And",
    "BUILDER_MAX_DEPTH",
    "BUILDER_OPS",
    "Between",
    "Call",
    "Compare",
    "Condition",
    "Expr",
    "FUNCTIONS",
    "Filter",
    "FilterEvalError",
    "FilterPresets",
    "Group",
    "In",
    "Is",
    "KEYWORDS",
    "LANGUAGES",
    "ListExpr",
    "Literal",
    "MAX_NESTING",
    "Not",
    "Or",
    "PathExpr",
    "SpecialExpr",
    "builder_from_expr",
    "builder_from_text",
    "compile_filter",
    "format_expr",
    "json_equal",
    "op_arity",
    "op_label",
    "parse_filter",
    "render_field",
    "render_path",
]

LANGUAGES = ("expr", "jmespath")

#: Maximum nesting of parentheses / ``not`` / lists / calls / builder groups.
MAX_NESTING = 100

#: Reserved words (matched case-insensitively). ``empty``/``missing`` are contextual.
KEYWORDS = frozenset(
    {"and", "or", "not", "in", "is", "between", "contains", "startswith", "endswith", "matches", "true", "false",
     "null"}
)
_CONTEXT_WORDS = frozenset({"empty", "missing"})

#: Function name -> number of arguments.
FUNCTIONS: dict[str, int] = {
    "len": 1,
    "exists": 1,
    "missing": 1,
    "empty": 1,
    "lower": 1,
    "upper": 1,
    "trim": 1,
    "type": 1,
    "number": 1,
    "string": 1,
}
_BOOL_FUNCS = frozenset({"exists", "missing", "empty"})

_CMP_OPS = ("==", "!=", "<", "<=", ">", ">=")
_WORD_OPS = ("contains", "startswith", "endswith", "matches")


# ==================================================================================
# AST
# ==================================================================================
@dataclass(frozen=True)
class Expr:
    """Base class of AST nodes; ``pos`` is the 0-based source offset (ignored by ``==``)."""

    pos: int = dc_field(default=0, compare=False, kw_only=True)


@dataclass(frozen=True)
class Literal(Expr):
    """A constant JSON value. ``text`` (canonical JSON, compared) identifies it exactly;
    ``value`` is the decoded value (exact numbers)."""

    value: Any = dc_field(compare=False)
    text: str = ""


@dataclass(frozen=True)
class ListExpr(Expr):
    """A list literal with at least one non-constant item (constant lists are Literals)."""

    items: tuple[Expr, ...]


@dataclass(frozen=True)
class PathExpr(Expr):
    """A path; ``$`` is the empty pattern (the record value itself)."""

    pattern: PathPattern


@dataclass(frozen=True)
class SpecialExpr(Expr):
    """``$key`` (``name="key"``) or ``$index`` (``name="index"``)."""

    name: str


@dataclass(frozen=True)
class Call(Expr):
    name: str
    args: tuple[Expr, ...]


@dataclass(frozen=True)
class Compare(Expr):
    """``op``: ``== != < <= > >= contains startswith endswith matches``."""

    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Between(Expr):
    operand: Expr
    low: Expr
    high: Expr


@dataclass(frozen=True)
class In(Expr):
    left: Expr
    right: Expr
    negate: bool = False


@dataclass(frozen=True)
class Is(Expr):
    """``what``: ``null``, ``empty`` or ``missing``."""

    operand: Expr
    what: str
    negate: bool = False


@dataclass(frozen=True)
class Not(Expr):
    operand: Expr


@dataclass(frozen=True)
class And(Expr):
    items: tuple[Expr, ...]


@dataclass(frozen=True)
class Or(Expr):
    items: tuple[Expr, ...]


def _literal(value: Any, pos: int = 0) -> Literal:
    return Literal(value, codec.dumps(value).decode("utf-8"), pos=pos)


# ==================================================================================
# lexer
# ==================================================================================
_WS_RE = re.compile(r"\s+")
# unquoted key characters (see module docstring)
_WORD_RE = re.compile("[^\\s.\\[\\](){}\"'`,=!<>~&|:;?/\\\\+%^≠≤≥“”„«»\\x00-\\x1f\\x7f]+")
_CURLY_QUOTES = frozenset("“”„«»‘’")  # never start a token
_BAD_WORD_START = frozenset("-#$‘’")  # allowed inside keys, not as the first character
_NUM_START = frozenset("0123456789-.")
_DIGITS = frozenset("0123456789")
_NUM_CAND_RE = re.compile(r"-?(?:[0-9]|\.[0-9])(?:[eE][+-]?|[0-9A-Za-z_.])*")
_JSON_NUM_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_INDEX_RE = re.compile(r"[0-9]+")
_HEX4_RE = re.compile(r"[0-9A-Fa-f]{4}")
_STOP_RE = {q: re.compile("[" + q + "\\\\]") for q in ('"', "'", "`")}
_ESCAPES = {'"': '"', "'": "'", "`": "`", "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
            "t": "\t"}

_OPS2 = {
    "==": ("op", "=="),
    "!=": ("op", "!="),
    "<=": ("op", "<="),
    ">=": ("op", ">="),
    "&&": ("kw", "and"),
    "||": ("kw", "or"),
}
_OPS1 = {
    "=": ("op", "=="),
    "<": ("op", "<"),
    ">": ("op", ">"),
    "≠": ("op", "!="),
    "≤": ("op", "<="),
    "≥": ("op", ">="),
    "!": ("kw", "not"),
    "~": ("kw", "matches"),
    "(": ("punct", "("),
    ")": ("punct", ")"),
    "[": ("punct", "["),
    "]": ("punct", "]"),
    "{": ("punct", "{"),
    "}": ("punct", "}"),
    ",": ("punct", ","),
    ":": ("punct", ":"),
}


class _Tok:
    """A token. ``kind``: kw | op | punct | str | num | path | eof.

    ``value``: keyword (lower case), canonical operator, punctuation, decoded string,
    number value, or the Expr node of a path. ``bare`` is the word of a path made of one
    unquoted key (candidate function name / contextual keyword), else ``None``.
    """

    __slots__ = ("kind", "value", "pos", "end", "bare")

    def __init__(self, kind: str, value: Any, pos: int, end: int, bare: str | None = None) -> None:
        self.kind = kind
        self.value = value
        self.pos = pos
        self.end = end
        self.bare = bare

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Tok({self.kind}, {self.value!r}, {self.pos}:{self.end})"


def _char_repr(c: str) -> str:
    return repr(c)


def _syntax_error(text: str, pos: int, detail: str) -> QuerySyntaxError:
    return QuerySyntaxError(pos=max(0, min(pos, len(text))), text=text, detail=detail)


def _lex_quoted(text: str, start: int, quote: str, want_offsets: bool = False) -> tuple[str, int, list[int] | None]:
    """Decode the quoted string/name whose opening quote is ``text[start]``.

    Returns ``(value, end, offsets)``: ``end`` is the index after the closing quote;
    ``offsets[i]`` is the source offset of decoded character ``i`` (escapes map to their
    backslash) followed by one extra entry for the closing quote (``None`` unless
    ``want_offsets``).
    """
    n = len(text)
    stop = _STOP_RE[quote]
    i = start + 1
    out: list[str] = []
    offs: list[int] | None = [] if want_offsets else None
    while True:
        m = stop.search(text, i)
        if m is None:
            key = "query.syntax.unterminated_name" if quote == "`" else "query.syntax.unterminated_string"
            raise _syntax_error(text, start, t(key, quote=quote))
        j = m.start()
        if j > i:
            out.append(text[i:j])
            if offs is not None:
                offs.extend(range(i, j))
        if text[j] == quote:
            if offs is not None:
                offs.append(j)
            return "".join(out), j + 1, offs
        # backslash escape
        if j + 1 >= n:
            key = "query.syntax.unterminated_name" if quote == "`" else "query.syntax.unterminated_string"
            raise _syntax_error(text, start, t(key, quote=quote))
        esc = text[j + 1]
        if esc == "u":
            code = _hex4(text, j + 2)
            if code is None:
                raise _syntax_error(text, j, t("query.syntax.bad_unicode_escape"))
            k = j + 6
            if 0xD800 <= code <= 0xDBFF and text.startswith("\\u", k):
                low = _hex4(text, k + 2)
                if low is not None and 0xDC00 <= low <= 0xDFFF:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    if offs is not None:
                        offs.append(j)
                    i = k + 6
                    continue
            out.append(chr(code))
            if offs is not None:
                offs.append(j)
            i = k
            continue
        mapped = _ESCAPES.get(esc)
        if mapped is None:
            raise _syntax_error(text, j, t("query.syntax.bad_escape", char=esc))
        out.append(mapped)
        if offs is not None:
            offs.append(j)
        i = j + 2


def _hex4(text: str, pos: int) -> int | None:
    if _HEX4_RE.fullmatch(text, pos, pos + 4) is None:
        return None
    return int(text[pos:pos + 4], 16)


def _word_segment(word: str) -> str | Wild:
    if word == "*":
        return Wild.ANY_KEY
    if word == "**":
        return Wild.DESCENDANTS
    return word


class _Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)

    def error(self, detail: str, pos: int) -> QuerySyntaxError:
        return _syntax_error(self.text, pos, detail)

    def skip_ws(self, pos: int) -> int:
        m = _WS_RE.match(self.text, pos)
        return m.end() if m is not None else pos

    def tokenize(self) -> list[_Tok]:
        text, n = self.text, self.n
        toks: list[_Tok] = []
        pos = 0
        while True:
            pos = self.skip_ws(pos)
            if pos >= n:
                toks.append(_Tok("eof", None, n, n))
                return toks
            c = text[pos]
            nxt = text[pos + 1] if pos + 1 < n else ""
            if c == '"' or c == "'":
                value, end, _ = _lex_quoted(text, pos, c)
                toks.append(_Tok("str", value, pos, end))
                pos = end
                continue
            if c in _NUM_START:
                m = _NUM_CAND_RE.match(text, pos)
                if m is not None:
                    tok = m.group()
                    if _JSON_NUM_RE.fullmatch(tok) is None:
                        raise self.error(t("query.syntax.invalid_number", token=tok), pos)
                    toks.append(_Tok("num", codec.loads(tok), pos, m.end()))
                    pos = m.end()
                    continue
            if c == "$" or c == "`" or (c == "." and nxt == "."):
                node, end, bare = self.path(pos)
                toks.append(_Tok("path", node, pos, end, bare))
                pos = end
                continue
            op = _OPS2.get(text[pos:pos + 2])
            if op is not None:
                toks.append(_Tok(op[0], op[1], pos, pos + 2))
                pos += 2
                continue
            op = _OPS1.get(c)
            if op is not None:
                toks.append(_Tok(op[0], op[1], pos, pos + 1))
                pos += 1
                continue
            if c in _CURLY_QUOTES:
                raise self.error(t("query.syntax.curly_quote", char=_char_repr(c)), pos)
            if c not in _BAD_WORD_START:
                m = _WORD_RE.match(text, pos)
                if m is not None:
                    low = m.group().lower()
                    if low in KEYWORDS:
                        toks.append(_Tok("kw", low, pos, m.end()))
                        pos = m.end()
                        continue
                    node, end, bare = self.path(pos)
                    toks.append(_Tok("path", node, pos, end, bare))
                    pos = end
                    continue
            raise self.error(t("query.syntax.unexpected_char", char=_char_repr(c)), pos)

    # -- paths ---------------------------------------------------------------------
    def path(self, pos: int) -> tuple[Expr, int, str | None]:
        text, n = self.text, self.n
        start = pos
        segs: list[str | int | Wild] = []
        bare: str | None = None
        c = text[pos]
        if c == "$":
            m = _WORD_RE.match(text, pos + 1)
            if m is not None:
                name = m.group()
                low = name.lower()
                if low in ("key", "index"):
                    end = m.end()
                    if end < n and text[end] in ".[":
                        raise self.error(t("query.syntax.special_no_path", name="$" + low), end)
                    return SpecialExpr(low, pos=start), end, None
                raise self.error(t("query.syntax.unknown_special", name="$" + name), pos)
            pos += 1
        elif c == "`":
            key, pos, _ = _lex_quoted(text, pos, "`")
            segs.append(key)
        elif c != ".":
            m = _WORD_RE.match(text, pos)
            assert m is not None
            word = m.group()
            pos = m.end()
            seg = _word_segment(word)
            segs.append(seg)
            if isinstance(seg, str):
                bare = word
        while pos < n:
            c = text[pos]
            if c == ".":
                bare = None
                if pos + 1 < n and text[pos + 1] == ".":
                    segs.append(Wild.DESCENDANTS)
                    pos += 2
                    if pos < n and text[pos] == "[":
                        continue
                    pos = self.key_after(pos, segs, "..")
                else:
                    pos = self.key_after(pos + 1, segs, ".")
            elif c == "[":
                bare = None
                pos = self.bracket(pos, segs)
            else:
                break
        return PathExpr(PathPattern(tuple(segs)), pos=start), pos, bare

    def key_after(self, pos: int, segs: list, sep: str) -> int:
        if pos < self.n and self.text[pos] == "`":
            key, end, _ = _lex_quoted(self.text, pos, "`")
            segs.append(key)
            return end
        m = _WORD_RE.match(self.text, pos)
        if m is None:
            raise self.error(t("query.syntax.expected_key", sep=sep), pos)
        segs.append(_word_segment(m.group()))
        return m.end()

    def bracket(self, pos: int, segs: list) -> int:
        text, n = self.text, self.n
        start = pos
        pos = self.skip_ws(pos + 1)
        if pos >= n:
            raise self.error(t("query.syntax.unterminated_bracket"), start)
        c = text[pos]
        if c == "]":
            segs.append(Wild.ANY_INDEX)
            return pos + 1
        if c == "*":
            segs.append(Wild.ANY_INDEX)
            return self.close_bracket(pos + 1)
        if c == '"' or c == "'":
            key, end, _ = _lex_quoted(text, pos, c)
            segs.append(key)
            return self.close_bracket(end)
        m = _INDEX_RE.match(text, pos)
        if m is not None:
            try:
                segs.append(int(m.group()))
            except ValueError:  # more digits than int() accepts
                raise self.error(t("query.syntax.bad_bracket"), pos) from None
            return self.close_bracket(m.end())
        if c == "-" and pos + 1 < n and text[pos + 1] in _DIGITS:
            raise self.error(t("query.syntax.negative_index"), pos)
        raise self.error(t("query.syntax.bad_bracket"), pos)

    def close_bracket(self, pos: int) -> int:
        pos = self.skip_ws(pos)
        if pos >= self.n or self.text[pos] != "]":
            raise self.error(t("query.syntax.expected_bracket_close"), pos)
        return pos + 1


# ==================================================================================
# parser
# ==================================================================================
_TRAILING_CMP_WORDS = frozenset({"contains", "startswith", "endswith", "matches", "in", "between", "is"})
_WARN_OPS = frozenset({"==", "!=", "contains", "startswith", "endswith", "matches"})


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks = _Lexer(text).tokenize()
        self.i = 0
        self.depth = 0
        self.warnings: list[str] = []

    # -- helpers -----------------------------------------------------------------------
    def peek(self, k: int = 0) -> _Tok:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def advance(self) -> _Tok:
        tok = self.toks[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def at_kw(self, word: str) -> bool:
        tok = self.toks[self.i]
        return tok.kind == "kw" and tok.value == word

    def at_punct(self, p: str) -> bool:
        tok = self.toks[self.i]
        return tok.kind == "punct" and tok.value == p

    def err(self, detail: str, pos: int) -> QuerySyntaxError:
        return _syntax_error(self.text, pos, detail)

    def desc(self, tok: _Tok) -> str:
        if tok.kind == "eof":
            return t("query.token.end")
        snippet = self.text[tok.pos:tok.end]
        if len(snippet) > 40:
            snippet = snippet[:37] + "..."
        return snippet if tok.kind == "str" else f"'{snippet}'"

    def enter(self, pos: int) -> None:
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise self.err(t("query.syntax.too_deep", limit=MAX_NESTING), pos)

    # -- grammar -----------------------------------------------------------------------
    def parse(self) -> Expr:
        if self.peek().kind == "eof":
            raise self.err(t("query.syntax.empty"), 0)
        node = self.parse_or()
        tok = self.peek()
        if tok.kind != "eof":
            raise self.trailing_error(tok)
        return node

    def trailing_error(self, tok: _Tok) -> QuerySyntaxError:
        if tok.kind == "op" or (tok.kind == "kw" and tok.value in _TRAILING_CMP_WORDS):
            return self.err(t("query.syntax.chained_comparison"), tok.pos)
        if tok.kind in ("path", "str", "num") or (tok.kind == "kw" and tok.value in ("true", "false", "null", "not")) \
                or (tok.kind == "punct" and tok.value in ("(", "[", "{")):
            return self.err(t("query.syntax.missing_operator", token=self.desc(tok)), tok.pos)
        return self.err(t("query.syntax.unexpected_token", token=self.desc(tok)), tok.pos)

    def parse_or(self) -> Expr:
        self.enter(self.peek().pos)
        try:
            first = self.parse_and()
            items = [first]
            while self.at_kw("or"):
                self.advance()
                items.append(self.parse_and())
            return first if len(items) == 1 else Or(tuple(items), pos=first.pos)
        finally:
            self.depth -= 1

    def parse_and(self) -> Expr:
        first = self.parse_not()
        items = [first]
        while self.at_kw("and"):
            self.advance()
            items.append(self.parse_not())
        return first if len(items) == 1 else And(tuple(items), pos=first.pos)

    def parse_not(self) -> Expr:
        tok = self.peek()
        if tok.kind == "kw" and tok.value == "not":
            self.advance()
            self.enter(tok.pos)
            try:
                operand = self.parse_not()
            finally:
                self.depth -= 1
            return Not(operand, pos=tok.pos)
        return self.parse_cmp()

    def parse_cmp(self) -> Expr:
        left = self.parse_operand()
        tok = self.peek()
        if tok.kind == "op":
            self.advance()
            right = self.parse_rhs(tok.value)
            return Compare(tok.value, left, right, pos=tok.pos)
        if tok.kind != "kw":
            return left
        word = tok.value
        if word in _WORD_OPS:
            self.advance()
            node = Compare(word, left, self.parse_rhs(word), pos=tok.pos)
            if word == "matches":
                _check_regex(node, self.text)
            return node
        if word == "between":
            self.advance()
            low = self.parse_rhs(word)
            tok2 = self.peek()
            if not (tok2.kind == "kw" and tok2.value == "and"):
                raise self.err(t("query.syntax.expected_between_and", token=self.desc(tok2)), tok2.pos)
            self.advance()
            high = self.parse_rhs(word)
            return Between(left, low, high, pos=tok.pos)
        if word == "in":
            self.advance()
            return In(left, self.parse_rhs(word), False, pos=tok.pos)
        if word == "not":
            tok2 = self.peek(1)
            if tok2.kind == "kw" and tok2.value == "in":
                self.advance()
                self.advance()
                return In(left, self.parse_rhs("in"), True, pos=tok.pos)
            raise self.err(t("query.syntax.expected_in", token=self.desc(tok2)), tok2.pos)
        if word == "is":
            self.advance()
            negate = False
            tok2 = self.peek()
            if tok2.kind == "kw" and tok2.value == "not":
                negate = True
                self.advance()
                tok2 = self.peek()
            what = None
            if tok2.kind == "kw" and tok2.value == "null":
                what = "null"
            elif tok2.kind == "path" and tok2.bare is not None and tok2.bare.lower() in _CONTEXT_WORDS:
                what = tok2.bare.lower()
            if what is None:
                raise self.err(t("query.syntax.expected_is_target", token=self.desc(tok2)), tok2.pos)
            self.advance()
            return Is(left, what, negate, pos=tok.pos)
        return left

    def parse_rhs(self, op: str) -> Expr:
        """Right-hand operand; warns when a bare word is probably a string without quotes."""
        tok = self.peek()
        node = self.parse_operand()
        if op in _WARN_OPS and tok.kind == "path" and tok.bare is not None and node is tok.value:
            msg = t("query.warn.bare_word", word=tok.bare)
            if msg not in self.warnings:
                self.warnings.append(msg)
        return node

    def parse_operand(self) -> Expr:
        tok = self.peek()
        kind = tok.kind
        if kind == "str" or kind == "num":
            self.advance()
            return _literal(tok.value, tok.pos)
        if kind == "kw" and tok.value in ("true", "false", "null"):
            self.advance()
            return _literal({"true": True, "false": False, "null": None}[tok.value], tok.pos)
        if kind == "path":
            self.advance()
            nxt = self.peek()
            if nxt.kind == "punct" and nxt.value == "(":
                if tok.bare is None:
                    raise self.err(t("query.syntax.unexpected_token", token=self.desc(nxt)), nxt.pos)
                return self.parse_call(tok)
            return tok.value
        if kind == "punct":
            if tok.value == "(":
                self.advance()
                inner = self.parse_or()
                close = self.peek()
                if not (close.kind == "punct" and close.value == ")"):
                    raise self.err(t("query.syntax.expected_close_paren", token=self.desc(close)), close.pos)
                self.advance()
                return inner
            if tok.value == "[":
                return self.parse_list()
            if tok.value == "{":
                return self.parse_object()
        if kind == "eof":
            raise self.err(t("query.syntax.expected_operand_end"), tok.pos)
        raise self.err(t("query.syntax.expected_operand", token=self.desc(tok)), tok.pos)

    def parse_call(self, name_tok: _Tok) -> Expr:
        assert name_tok.bare is not None
        name = name_tok.bare.lower()
        arity = FUNCTIONS.get(name)
        if arity is None:
            raise self.err(
                t("query.syntax.unknown_function", name=name_tok.bare, names=", ".join(FUNCTIONS)), name_tok.pos
            )
        self.advance()  # '('
        args: list[Expr] = []
        if not self.at_punct(")"):
            while True:
                args.append(self.parse_or())
                if self.at_punct(","):
                    self.advance()
                    continue
                break
        close = self.peek()
        if not self.at_punct(")"):
            raise self.err(t("query.syntax.expected_args_end", token=self.desc(close)), close.pos)
        self.advance()
        if len(args) != arity:
            raise self.err(t("query.syntax.arg_count", name=name, expected=arity, got=len(args)), name_tok.pos)
        return Call(name, tuple(args), pos=name_tok.pos)

    def parse_list(self) -> Expr:
        open_tok = self.advance()  # '['
        items: list[Expr] = []
        if not self.at_punct("]"):
            while True:
                items.append(self.parse_or())
                if self.at_punct(","):
                    self.advance()
                    continue
                break
        close = self.peek()
        if not self.at_punct("]"):
            raise self.err(t("query.syntax.expected_list_end", token=self.desc(close)), close.pos)
        self.advance()
        if all(isinstance(item, Literal) for item in items):
            return _literal([item.value for item in items], open_tok.pos)  # type: ignore[union-attr]
        return ListExpr(tuple(items), pos=open_tok.pos)

    def parse_object(self) -> Expr:
        open_tok = self.advance()  # '{'
        self.enter(open_tok.pos)
        try:
            obj: dict[str, Any] = {}
            if not self.at_punct("}"):
                while True:
                    key_tok = self.peek()
                    if key_tok.kind != "str":
                        raise self.err(t("query.syntax.expected_object_key", token=self.desc(key_tok)), key_tok.pos)
                    self.advance()
                    colon = self.peek()
                    if not self.at_punct(":"):
                        raise self.err(t("query.syntax.expected_colon", token=self.desc(colon)), colon.pos)
                    self.advance()
                    value = self.parse_or()
                    if not isinstance(value, Literal):
                        raise self.err(t("query.syntax.object_constant"), value.pos)
                    obj[key_tok.value] = value.value
                    if self.at_punct(","):
                        self.advance()
                        continue
                    break
            close = self.peek()
            if not self.at_punct("}"):
                raise self.err(t("query.syntax.expected_object_end", token=self.desc(close)), close.pos)
            self.advance()
            return _literal(obj, open_tok.pos)
        finally:
            self.depth -= 1


def _regex_error_pos(text: str, lit_pos: int, exc: re.error) -> int:
    """Map ``exc.pos`` (index into the decoded pattern) to an offset in ``text``."""
    if exc.pos is None or not (0 <= lit_pos < len(text)) or text[lit_pos] not in "\"'":
        return lit_pos
    try:
        _, _, offs = _lex_quoted(text, lit_pos, text[lit_pos], want_offsets=True)
    except QuerySyntaxError:  # pragma: no cover - the literal was lexed before
        return lit_pos
    assert offs is not None
    return offs[max(0, min(exc.pos, len(offs) - 1))]


def _check_regex(node: Compare, text: str) -> None:
    """Validate the literal pattern of a ``matches`` comparison (QuerySyntaxError with the
    localized regex description of :func:`jsonreshape.textmatch.regex_error_text`)."""
    right = node.right
    if not isinstance(right, Literal):
        return  # pattern from the data: checked per value
    if not isinstance(right.value, str):
        raise _syntax_error(text, right.pos, t("query.syntax.regex_not_string"))
    try:
        re.compile(right.value)
    except re.error as exc:
        raise _syntax_error(text, _regex_error_pos(text, right.pos, exc),
                            t("query.syntax.bad_regex", error=regex_error_text(exc))) from None
    except (RecursionError, OverflowError, ValueError) as exc:
        raise _syntax_error(text, right.pos, t("query.syntax.bad_regex", error=regex_error_text(exc))) from None


def parse_filter(text: str) -> Expr:
    """Parse a filter expression into its AST (raises QuerySyntaxError)."""
    if not isinstance(text, str):
        raise TypeError(f"filter text must be str, not {type(text).__name__}")
    return _Parser(text).parse()


# ==================================================================================
# rendering (AST / paths -> text)
# ==================================================================================
def _is_safe_word(seg: str) -> bool:
    """True when ``seg`` can be written as an unquoted key anywhere in a path (printable
    characters only, so lone surrogates and invisible characters are always escaped)."""
    return (
        _WORD_RE.fullmatch(seg) is not None
        and seg.isprintable()
        and seg[0] not in _DIGITS
        and seg[0] not in _BAD_WORD_START
        and seg not in ("*", "**")
        and seg.lower() not in KEYWORDS
        and seg.lower() not in _CONTEXT_WORDS
    )


def _quote_key(seg: str) -> str:
    return codec.dumps(seg).decode("utf-8")


def _render_segment(seg: str | int | Wild) -> str:
    if seg is Wild.ANY_INDEX:
        return "[*]"
    if seg is Wild.ANY_KEY:
        return ".*"
    if seg is Wild.DESCENDANTS:
        return ".**"
    if isinstance(seg, int):
        return f"[{seg}]"
    if _is_safe_word(seg):
        return "." + seg
    return f"[{_quote_key(seg)}]"


def render_path(path: PathPattern | tuple | list) -> str:
    """Render a path (segments or :class:`PathPattern`) in expression syntax.

    The result always parses back to exactly the same segments: unusual keys are
    bracket-quoted (``$["my key"]``, ``a["and"]``), the root alone is ``$``.
    """
    segments = tuple(path.segments if isinstance(path, PathPattern) else path)
    if not segments:
        return "$"
    first = segments[0]
    out: list[str] = []
    if isinstance(first, str) and _is_safe_word(first):
        out.append(first)
    elif first is Wild.ANY_KEY:
        out.append("*")
    elif first is Wild.DESCENDANTS:
        out.append("**")
    else:
        out.append("$" + _render_segment(first))
    out.extend(_render_segment(seg) for seg in segments[1:])
    return "".join(out)


def render_field(field: str | PathPattern) -> str:
    """Render a builder field (paths.py syntax, ``$key`` or ``$index``) in expression syntax."""
    return _builder_field(field, ())[1]


_PRECEDENCE_CMP = 4


def _precedence(node: Expr) -> int:
    if isinstance(node, Or):
        return 1
    if isinstance(node, And):
        return 2
    if isinstance(node, Not):
        return 3
    if isinstance(node, (Compare, Between, In, Is)):
        return _PRECEDENCE_CMP
    return 5


def format_expr(node: Expr) -> str:
    """Canonical text of an AST; ``parse_filter(format_expr(e)) == e`` for parsed ASTs."""
    return _Formatter().format(node, 0)


class _Formatter:
    def format(self, node: Expr, depth: int) -> str:
        if depth > MAX_NESTING * 4:
            raise ValueError("expression nested too deeply to format")
        d = depth + 1
        if isinstance(node, Literal):
            return node.text
        if isinstance(node, PathExpr):
            return render_path(node.pattern)
        if isinstance(node, SpecialExpr):
            return "$" + node.name
        if isinstance(node, ListExpr):
            return "[" + ", ".join(self.format(item, d) for item in node.items) + "]"
        if isinstance(node, Call):
            return node.name + "(" + ", ".join(self.format(arg, d) for arg in node.args) + ")"
        if isinstance(node, Compare):
            return f"{self.operand(node.left, d)} {node.op} {self.operand(node.right, d)}"
        if isinstance(node, Between):
            return (f"{self.operand(node.operand, d)} between {self.operand(node.low, d)} "
                    f"and {self.operand(node.high, d)}")
        if isinstance(node, In):
            word = "not in" if node.negate else "in"
            return f"{self.operand(node.left, d)} {word} {self.operand(node.right, d)}"
        if isinstance(node, Is):
            return f"{self.operand(node.operand, d)} is {'not ' if node.negate else ''}{node.what}"
        if isinstance(node, Not):
            inner = node.operand
            text = self.format(inner, d)
            return "not " + (f"({text})" if _precedence(inner) < 3 else text)
        if isinstance(node, And):
            return " and ".join(
                f"({self.format(item, d)})" if _precedence(item) <= 2 else self.format(item, d) for item in node.items
            )
        if isinstance(node, Or):
            return " or ".join(
                f"({self.format(item, d)})" if _precedence(item) <= 1 else self.format(item, d) for item in node.items
            )
        raise TypeError(f"not an expression node: {type(node).__name__}")

    def operand(self, node: Expr, depth: int) -> str:
        text = self.format(node, depth)
        return text if _precedence(node) == 5 else f"({text})"


# ==================================================================================
# value semantics
# ==================================================================================
_NUMBER_TYPES = (int, float, Decimal)
_ARRAY_TYPES = (list, tuple)
_CONTAINER_TYPES = (list, tuple, dict)
_SIZED_TYPES = (str, list, tuple, dict)
_TRUE_L: list = [True]
_FALSE_L: list = [False]
_EMPTY: list = []


def _is_num(v: Any) -> bool:
    return isinstance(v, _NUMBER_TYPES) and not isinstance(v, bool)


def _dec(d: Decimal) -> Any:
    """Exact codec number for a Decimal (e.g. ``Decimal('1.10')`` -> ``JsonFloat('1.10')``)."""
    if d.is_finite():
        try:
            return codec.loads(str(d))
        except codec.JsonDecodeError:  # pragma: no cover - str(Decimal) is a JSON number
            pass
    if d.is_nan():
        return math.nan
    return float(d)


def _num(v: Any) -> Any:
    return _dec(v) if isinstance(v, Decimal) else v


_JsonFloat = codec.JsonFloat


@lru_cache(maxsize=65536)
def _text_key(text: str) -> Any:
    return number_key(text)


def _exact_key(v: Any) -> Any:
    """Exact :func:`jsonreshape.textmatch.number_key` of a number (after :func:`_num`);
    ``None`` for values without a decimal value (``inf``/``nan`` Python floats)."""
    if type(v) is _JsonFloat:
        return _text_key(v.raw)
    try:
        return _text_key(codec.number_text(v))
    except (TypeError, ValueError):  # pragma: no cover - not a number / absurd int
        return None


def _num_cmp(a: Any, b: Any) -> int:
    """``-1``/``0``/``1``: exact comparison of two numbers (already :func:`_num`). Plain
    ``int``/``float`` values compare natively (exact for them); a ``JsonFloat`` on either
    side is compared by its exact decimal value."""
    if type(a) is _JsonFloat or type(b) is _JsonFloat:
        ka = _exact_key(a)
        kb = _exact_key(b)
        if ka is not None and kb is not None:
            return compare_numbers(ka, kb)
    return (a > b) - (a < b)


def _scalar_eq(a: Any, b: Any) -> bool:
    if isinstance(a, str):
        return isinstance(b, str) and a == b
    if a is None:
        return b is None
    if isinstance(a, bool):
        return isinstance(b, bool) and a == b
    if isinstance(a, _NUMBER_TYPES):
        return _is_num(b) and _num_cmp(_num(a), _num(b)) == 0
    return False


def _deep_eq(a: Any, b: Any) -> bool:
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if isinstance(x, dict):
            if not isinstance(y, dict) or len(x) != len(y):
                return False
            for k, xv in x.items():
                if k not in y:
                    return False
                stack.append((xv, y[k]))
        elif isinstance(x, _ARRAY_TYPES):
            if not isinstance(y, _ARRAY_TYPES) or len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif not _scalar_eq(x, y):
            return False
    return True


def json_equal(a: Any, b: Any) -> bool:
    """JSON equality -- the filter language's ``==`` (§9): no type coercion (``1 != "1"``, ``true != 1``,
    ``null`` only equals ``null``), numbers by their exact value (``1.10 == 1.1``, ``-0 == 0``, ``1e400 ==
    10e399``, big integers exactly), strings by code points, objects by their members whatever their order
    (same keys, equal values), arrays element by element, deeply (iterative: any depth).

    Public since fix round L1 for the modules that must agree with the filter (``bulk``'s ``set_field``
    "unchanged" test, ``jsonpatch``'s ``test`` operation); ``_eq`` stays as the private alias."""
    if isinstance(a, _CONTAINER_TYPES):
        return isinstance(b, _CONTAINER_TYPES) and _deep_eq(a, b)
    return _scalar_eq(a, b)


_eq = json_equal


_ORDER_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
#: operator -> test of a three-way comparison result (``_num_cmp``)
_ORDER_TESTS: dict[str, Callable[[int], bool]] = {
    "<": lambda c: c < 0,
    "<=": lambda c: c <= 0,
    ">": lambda c: c > 0,
    ">=": lambda c: c >= 0,
}


def _ordered(op: str) -> Callable[[Any, Any], bool]:
    cmp = _ORDER_OPS[op]
    test = _ORDER_TESTS[op]

    def compare(a: Any, b: Any) -> bool:
        if isinstance(a, str):
            return isinstance(b, str) and cmp(a, b)
        if _is_num(a) and _is_num(b):
            a = _num(a)
            b = _num(b)
            if type(a) is _JsonFloat or type(b) is _JsonFloat:
                return test(_num_cmp(a, b))
            return cmp(a, b)
        return False

    return compare


_lt = _ordered("<")
_le = _ordered("<=")
_gt = _ordered(">")
_ge = _ordered(">=")


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, str):
        return isinstance(item, str) and item in container
    if isinstance(container, _ARRAY_TYPES):
        for x in container:
            if _eq(x, item):
                return True
        return False
    if isinstance(container, dict):
        return isinstance(item, str) and item in container
    return False


def _startswith(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.startswith(b)


def _endswith(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.endswith(b)


_BINARY: dict[str, Callable[[Any, Any], bool]] = {
    "==": _eq,
    "<": _lt,
    "<=": _le,
    ">": _gt,
    ">=": _ge,
    "contains": _contains,
    "startswith": _startswith,
    "endswith": _endswith,
}


def _is_empty(v: Any) -> bool:
    return isinstance(v, _SIZED_TYPES) and len(v) == 0


def _truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if v is True:
        return True
    if isinstance(v, _SIZED_TYPES):
        return len(v) > 0
    if isinstance(v, _NUMBER_TYPES):
        v = _num(v)
        if type(v) is _JsonFloat:  # 1e-400 is not 0 (its double is)
            key = _exact_key(v)
            return key.sign != 0 if key is not None else v != 0
        return v != 0
    return True


def _never(_a: Any) -> bool:
    return False


def _num_pred(op: str, b: Any) -> Callable[[Any], bool]:
    """Predicate ``a -> (a OP b)`` for a constant number ``b`` (exact; fast path for
    plain ``int``/``float`` operands)."""
    nb = _num(b)
    cmp = operator.eq if op == "==" else _ORDER_OPS[op]
    test = (lambda c: c == 0) if op == "==" else _ORDER_TESTS[op]
    if type(nb) is _JsonFloat:
        kb = _exact_key(nb)

        def pred_exact(a: Any) -> bool:
            if not _is_num(a):
                return False
            a = _num(a)
            ka = _exact_key(a)
            if ka is None or kb is None:
                return cmp(a, nb)
            return test(compare_numbers(ka, kb))

        return pred_exact

    def pred(a: Any) -> bool:
        t_ = type(a)
        if t_ is int or t_ is float:
            return cmp(a, nb)
        if not _is_num(a):
            return False
        a = _num(a)
        if type(a) is _JsonFloat:
            return test(_num_cmp(a, nb))
        return cmp(a, nb)

    return pred


def _const_pred(op: str, b: Any) -> Callable[[Any], bool]:
    """Predicate ``a -> (a OP b)`` specialised for a constant right operand."""
    if op == "==":
        if isinstance(b, str):
            return lambda a: isinstance(a, str) and a == b
        if b is None:
            return lambda a: a is None
        if isinstance(b, bool):
            return lambda a: isinstance(a, bool) and a == b
        if isinstance(b, _NUMBER_TYPES):
            return _num_pred("==", b)
        return lambda a: _eq(a, b)
    if op in _ORDER_OPS:
        cmp = _ORDER_OPS[op]
        if isinstance(b, str):
            return lambda a: isinstance(a, str) and cmp(a, b)
        if _is_num(b):
            return _num_pred(op, b)
        return _never
    if op == "startswith":
        if isinstance(b, str):
            return lambda a: isinstance(a, str) and a.startswith(b)
        return _never
    if op == "endswith":
        if isinstance(b, str):
            return lambda a: isinstance(a, str) and a.endswith(b)
        return _never
    fn = _BINARY[op]
    return lambda a: fn(a, b)


def _in_const_pred(container: Any) -> Callable[[Any], bool]:
    """Predicate ``a -> (a in container)`` for a constant container (set lookups when possible)."""
    if isinstance(container, _ARRAY_TYPES) and container:
        if all(isinstance(x, str) for x in container):
            strings = frozenset(container)
            return lambda a: isinstance(a, str) and a in strings
        if all(_is_num(x) for x in container):
            members = [_num(x) for x in container]
            plain = frozenset(x for x in members if type(x) is not _JsonFloat)
            if len(plain) == len(members) and all(isinstance(x, int) for x in members):
                # integers only: exact by hashing (a JsonFloat value is compared exactly)
                def in_ints(a: Any) -> bool:
                    t_ = type(a)
                    if t_ is int or t_ is float:
                        return a in plain
                    if not _is_num(a):
                        return False
                    a = _num(a)
                    if type(a) is _JsonFloat:
                        return any(_num_cmp(a, x) == 0 for x in members)
                    return a in plain

                return in_ints
            # exact decimal values (equal numbers have equal keys)
            keys = frozenset(k for k in map(_exact_key, members) if k is not None)

            def in_numbers(a: Any) -> bool:
                if not _is_num(a):
                    return False
                a = _num(a)
                if type(a) is not _JsonFloat and a in plain:
                    return True
                k = _exact_key(a)
                return k is not None and k in keys

            return in_numbers
    return lambda a: _contains(container, a)


def _between_pred(lo: Any, hi: Any) -> Callable[[Any], bool]:
    """Predicate ``a -> lo <= a <= hi`` for constant bounds; reversed bounds are swapped
    (the rule of ``search --number '9..2'``)."""
    if _gt(lo, hi):
        lo, hi = hi, lo
    above = _const_pred(">=", lo)
    below = _const_pred("<=", hi)
    return lambda a: above(a) and below(a)


def _type_name(v: Any) -> str:
    try:
        return codec.json_type(v)
    except TypeError:
        return "unknown"


def _to_number(v: Any) -> Any:
    if _is_num(v):
        return _num(v)
    if isinstance(v, str):
        s = v.strip()
        if s[:1] == "+" and s[1:2] in _DIGITS:
            s = s[1:]
        if _JSON_NUM_RE.fullmatch(s) is not None:
            return codec.loads(s)
    return None


def _to_string(v: Any) -> str:
    if isinstance(v, str):
        return v
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if _is_num(v):
        return codec.number_text(_num(v))
    return codec.dumps_transient(v).decode("utf-8")


@lru_cache(maxsize=256)
def _dynamic_regex(pattern: str) -> re.Pattern[str] | None:
    """Compiled regex for a pattern taken from the data, ``None`` when invalid."""
    try:
        return re.compile(pattern)
    except (re.error, RecursionError, OverflowError, ValueError):
        log.debug("ignoring invalid regex from data: %r", pattern[:200])
        return None


# ==================================================================================
# evaluation: AST -> closures
# ==================================================================================
class _Ctx:
    """Per-record evaluation state: lazily resolved values of each required pattern."""

    __slots__ = ("value", "key", "index", "cache", "patterns")

    def __init__(self, value: Any, key: Any, index: Any, cache: list, patterns: tuple[PathPattern, ...]) -> None:
        self.value = value
        self.key = key
        self.index = index
        self.cache = cache
        self.patterns = patterns

    def get(self, slot: int) -> list:
        res = self.cache[slot]
        if res is None:
            res = self.patterns[slot].values(self.value)
            self.cache[slot] = res
        return res


_TestFn = Callable[[_Ctx], bool]
_ValsFn = Callable[[_Ctx], list]


def _const_true(_ctx: _Ctx) -> bool:
    return True


def _const_false(_ctx: _Ctx) -> bool:
    return False


class _Compiler:
    """Builds evaluation closures; collects the required path patterns (slots)."""

    def __init__(self) -> None:
        self.patterns: list[PathPattern] = []
        self._slots: dict[PathPattern, int] = {}
        self.uses_key = False
        self.uses_index = False

    def slot(self, pattern: PathPattern) -> int:
        slot = self._slots.get(pattern)
        if slot is None:
            slot = len(self.patterns)
            self._slots[pattern] = slot
            self.patterns.append(pattern)
        return slot

    # -- value context -------------------------------------------------------------
    def vals(self, node: Expr) -> _ValsFn:
        if isinstance(node, Literal):
            one = [node.value]
            return lambda ctx: one
        if isinstance(node, PathExpr):
            slot = self.slot(node.pattern)
            return lambda ctx: ctx.get(slot)
        if isinstance(node, SpecialExpr):
            if node.name == "key":
                self.uses_key = True
                return lambda ctx: _EMPTY if ctx.key is None else [ctx.key]
            if node.name == "index":
                self.uses_index = True
                return lambda ctx: _EMPTY if ctx.index is None else [ctx.index]
            raise ValueError(f"unknown special name: ${node.name}")
        if isinstance(node, ListExpr):
            fns = tuple(self.vals(item) for item in node.items)

            def list_vals(ctx: _Ctx) -> list:
                out: list = []
                for fn in fns:
                    out.extend(fn(ctx))
                return [out]

            return list_vals
        if isinstance(node, Call) and node.name not in _BOOL_FUNCS:
            return self.call_vals(node)
        test = self.test(node)
        return lambda ctx: _TRUE_L if test(ctx) else _FALSE_L

    def call_vals(self, node: Call) -> _ValsFn:
        if len(node.args) != FUNCTIONS.get(node.name, -1):
            raise ValueError(f"bad call {node.name}() with {len(node.args)} argument(s)")
        arg = self.vals(node.args[0])
        name = node.name
        if name == "len":
            def len_vals(ctx: _Ctx) -> list:
                vs = arg(ctx)
                if vs:
                    v = vs[0]
                    if isinstance(v, _SIZED_TYPES):
                        return [len(v)]
                return _EMPTY

            return len_vals
        if name == "lower":
            return lambda ctx: [v.lower() for v in arg(ctx) if isinstance(v, str)]
        if name == "upper":
            return lambda ctx: [v.upper() for v in arg(ctx) if isinstance(v, str)]
        if name == "trim":
            return lambda ctx: [v.strip() for v in arg(ctx) if isinstance(v, str)]
        if name == "type":
            return lambda ctx: [_type_name(v) for v in arg(ctx)]
        if name == "number":
            def number_vals(ctx: _Ctx) -> list:
                out = []
                for v in arg(ctx):
                    n = _to_number(v)
                    if n is not None:
                        out.append(n)
                return out

            return number_vals
        if name == "string":
            return lambda ctx: [_to_string(v) for v in arg(ctx)]
        raise ValueError(f"unknown function: {name}()")

    # -- boolean context -----------------------------------------------------------
    def test(self, node: Expr) -> _TestFn:
        if isinstance(node, Literal):
            return _const_true if _truthy(node.value) else _const_false
        if isinstance(node, And):
            fns = tuple(self.test(item) for item in node.items)

            def and_test(ctx: _Ctx) -> bool:
                for fn in fns:
                    if not fn(ctx):
                        return False
                return True

            return and_test
        if isinstance(node, Or):
            fns = tuple(self.test(item) for item in node.items)

            def or_test(ctx: _Ctx) -> bool:
                for fn in fns:
                    if fn(ctx):
                        return True
                return False

            return or_test
        if isinstance(node, Not):
            inner = self.test(node.operand)
            return lambda ctx: not inner(ctx)
        if isinstance(node, Compare):
            return self.compare(node)
        if isinstance(node, Between):
            return self.between(node)
        if isinstance(node, In):
            return self.in_test(node)
        if isinstance(node, Is):
            return self.is_test(node)
        if isinstance(node, Call) and node.name in _BOOL_FUNCS:
            return self.bool_call(node)
        if isinstance(node, PathExpr):
            slot = self.slot(node.pattern)

            def path_truthy(ctx: _Ctx) -> bool:
                for v in ctx.get(slot):
                    if _truthy(v):
                        return True
                return False

            return path_truthy
        vals = self.vals(node)

        def truthy(ctx: _Ctx) -> bool:
            for v in vals(ctx):
                if _truthy(v):
                    return True
            return False

        return truthy

    def bool_call(self, node: Call) -> _TestFn:
        if len(node.args) != 1:
            raise ValueError(f"bad call {node.name}() with {len(node.args)} argument(s)")
        arg = self.vals(node.args[0])
        if node.name == "exists":
            return lambda ctx: len(arg(ctx)) > 0
        if node.name == "missing":
            return lambda ctx: len(arg(ctx)) == 0

        def empty_test(ctx: _Ctx) -> bool:
            for v in arg(ctx):
                if _is_empty(v):
                    return True
            return False

        return empty_test

    def compare(self, node: Compare) -> _TestFn:
        op = node.op
        if op == "!=":
            eq = self.binop("==", node.left, node.right)
            return lambda ctx: not eq(ctx)
        if op == "matches":
            return self.matches(node)
        if op not in _BINARY:
            raise ValueError(f"unknown comparison operator: {op!r}")
        return self.binop(op, node.left, node.right)

    def binop(self, op: str, left_node: Expr, right_node: Expr) -> _TestFn:
        left = self.vals(left_node)
        if isinstance(right_node, Literal):
            pred = _const_pred(op, right_node.value)

            def const_test(ctx: _Ctx) -> bool:
                for a in left(ctx):
                    if pred(a):
                        return True
                return False

            return const_test
        fn = _BINARY[op]
        right = self.vals(right_node)

        def dyn_test(ctx: _Ctx) -> bool:
            lv = left(ctx)
            if not lv:
                return False
            for b in right(ctx):
                for a in lv:
                    if fn(a, b):
                        return True
            return False

        return dyn_test

    def matches(self, node: Compare) -> _TestFn:
        left = self.vals(node.left)
        right_node = node.right
        if isinstance(right_node, Literal):
            _check_regex(node, "")
            search = re.compile(right_node.value).search

            def rx_test(ctx: _Ctx) -> bool:
                for a in left(ctx):
                    if isinstance(a, str) and search(a) is not None:
                        return True
                return False

            return rx_test
        right = self.vals(right_node)

        def dyn_rx_test(ctx: _Ctx) -> bool:
            lv = left(ctx)
            if not lv:
                return False
            for b in right(ctx):
                if isinstance(b, str):
                    rx = _dynamic_regex(b)
                    if rx is not None:
                        for a in lv:
                            if isinstance(a, str) and rx.search(a) is not None:
                                return True
            return False

        return dyn_rx_test

    def between(self, node: Between) -> _TestFn:
        operand = self.vals(node.operand)
        if isinstance(node.low, Literal) and isinstance(node.high, Literal):
            pred = _between_pred(node.low.value, node.high.value)

            def between_const(ctx: _Ctx) -> bool:
                for a in operand(ctx):
                    if pred(a):
                        return True
                return False

            return between_const
        low = self.vals(node.low)
        high = self.vals(node.high)

        def between_test(ctx: _Ctx) -> bool:
            av = operand(ctx)
            if not av:
                return False
            lows = low(ctx)
            if not lows:
                return False
            highs = high(ctx)
            for lo in lows:
                for hi in highs:
                    a_lo, a_hi = (hi, lo) if _gt(lo, hi) else (lo, hi)  # reversed bounds are swapped
                    for a in av:
                        if _le(a_lo, a) and _le(a, a_hi):
                            return True
            return False

        return between_test

    def in_test(self, node: In) -> _TestFn:
        left = self.vals(node.left)
        if isinstance(node.right, Literal):
            pred = _in_const_pred(node.right.value)

            def in_const(ctx: _Ctx) -> bool:
                for a in left(ctx):
                    if pred(a):
                        return True
                return False

            test = in_const
        else:
            right = self.vals(node.right)

            def in_dyn(ctx: _Ctx) -> bool:
                lv = left(ctx)
                if not lv:
                    return False
                for b in right(ctx):
                    for a in lv:
                        if _contains(b, a):
                            return True
                return False

            test = in_dyn
        if node.negate:
            return lambda ctx: not test(ctx)
        return test

    def is_test(self, node: Is) -> _TestFn:
        vals = self.vals(node.operand)
        what = node.what
        if what == "null":
            def is_null(ctx: _Ctx) -> bool:
                for v in vals(ctx):
                    if v is None:
                        return True
                return False

            test = is_null
        elif what == "empty":
            def is_empty(ctx: _Ctx) -> bool:
                for v in vals(ctx):
                    if _is_empty(v):
                        return True
                return False

            test = is_empty
        elif what == "missing":
            def is_missing(ctx: _Ctx) -> bool:
                return not vals(ctx)

            test = is_missing
        else:
            raise ValueError(f"unknown 'is' target: {what!r}")
        if node.negate:
            return lambda ctx: not test(ctx)
        return test


# ==================================================================================
# streamed evaluation (huge records): atoms decided on the ijson event stream
# ==================================================================================
_DROP = object()
_FLIPPED_OPS = {"==": "==", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


def _derive(name: str | None) -> Callable[[Any], Any] | None:
    """The per-value function of ``lower/upper/trim/type/number`` (``_DROP`` for values
    the function drops), ``None`` for the identity."""
    if name is None:
        return None
    if name == "lower":
        return lambda v: v.lower() if isinstance(v, str) else _DROP
    if name == "upper":
        return lambda v: v.upper() if isinstance(v, str) else _DROP
    if name == "trim":
        return lambda v: v.strip() if isinstance(v, str) else _DROP
    if name == "type":
        return _type_name
    if name == "number":
        def to_number(v: Any) -> Any:
            n = _to_number(v)
            return _DROP if n is None else n

        return to_number
    raise ValueError(f"no streamed form of {name}()")


_STREAM_DERIVED = frozenset({"lower", "upper", "trim", "type", "number"})


class _AtomSpec:
    """One condition decided by a probe: ``kind`` ``"any"`` (some value satisfies
    ``pred``; containers per ``cmode``: ``"false"`` never, ``"truthy"`` when non-empty,
    ``"empty"`` when empty, ``"contains"`` when a direct element equals / a key is
    ``item``), ``"count"`` (``pred`` on the number of values) or ``"first_len"``
    (``pred`` on ``len()`` of the first value)."""

    __slots__ = ("slot", "kind", "pred", "derive", "cmode", "item")

    def __init__(self, slot: int, kind: str, pred: Callable[[Any], bool], derive: str | None = None,
                 cmode: str = "false", item: Any = None) -> None:
        self.slot = slot
        self.kind = kind
        self.pred = pred
        self.derive = _derive(derive)
        self.cmode = cmode if derive is None else "false"
        self.item = item

    def probe(self) -> Any:
        if self.kind == "any":
            return _AnyProbe(self)
        if self.kind == "count":
            return _CountProbe(self)
        return _FirstLenProbe(self)


class _AnyProbe:
    __slots__ = ("spec", "hit", "watch")

    def __init__(self, spec: _AtomSpec) -> None:
        self.spec = spec
        self.hit = False
        self.watch: list[list[Any]] = []   # [is_map, children so far] of watched containers

    def value(self, v: Any) -> None:
        if self.hit:
            return
        derive = self.spec.derive
        if derive is not None:
            v = derive(v)
            if v is _DROP:
                return
        if self.spec.pred(v):
            self.hit = True

    def open(self, is_map: bool) -> bool:
        if self.hit:
            return False
        spec = self.spec
        if spec.derive is not None:
            v = spec.derive({} if is_map else [])  # only type() keeps containers
            if v is not _DROP and spec.pred(v):
                self.hit = True
            return False
        if spec.cmode == "false":
            return False
        self.watch.append([is_map, 0])
        return True

    def child(self, kind: str, v: Any) -> None:
        top = self.watch[-1]
        top[1] += 1
        if self.hit:
            return
        mode = self.spec.cmode
        if mode == "truthy":
            self.hit = True
        elif mode == "contains":
            item = self.spec.item
            if top[0]:
                if kind == "key" and v == item and isinstance(item, str):
                    self.hit = True
            elif kind == "value" and _eq(v, item):
                self.hit = True

    def close(self) -> None:
        top = self.watch.pop()
        if self.spec.cmode == "empty" and top[1] == 0:
            self.hit = True

    def result(self) -> bool:
        return self.hit


class _CountProbe:
    __slots__ = ("spec", "n")

    def __init__(self, spec: _AtomSpec) -> None:
        self.spec = spec
        self.n = 0

    def value(self, v: Any) -> None:
        self.n += 1

    def open(self, is_map: bool) -> bool:
        self.n += 1
        return False

    def child(self, kind: str, v: Any) -> None:  # pragma: no cover - open() never watches
        pass

    def close(self) -> None:  # pragma: no cover - open() never watches
        pass

    def result(self) -> bool:
        return self.spec.pred(self.n)


class _FirstLenProbe:
    __slots__ = ("spec", "state", "length")

    def __init__(self, spec: _AtomSpec) -> None:
        self.spec = spec
        self.state = 0          # 0: no value yet, 1: counting the first container, 2: decided
        self.length: int | None = None

    def value(self, v: Any) -> None:
        if self.state == 0:
            self.state = 2
            self.length = len(v) if isinstance(v, str) else None

    def open(self, is_map: bool) -> bool:
        if self.state == 0:
            self.state = 1
            self.length = 0
            return True
        return False

    def child(self, kind: str, v: Any) -> None:
        self.length += 1  # type: ignore[operator]

    def close(self) -> None:
        self.state = 2

    def result(self) -> bool:
        return self.length is not None and self.spec.pred(self.length)


class _StreamCtx(_Ctx):
    __slots__ = ("atoms",)

    def __init__(self, key: Any, index: Any, cache: list, patterns: tuple[PathPattern, ...], atoms: list) -> None:
        super().__init__(None, key, index, cache, patterns)
        self.atoms = atoms


def _is_container(v: Any) -> bool:
    return isinstance(v, _CONTAINER_TYPES)


class _StreamPlan:
    """The compiled streamed form of an expression filter."""

    __slots__ = ("test", "patterns", "specs", "capture", "captures_root")

    def __init__(self, test: _TestFn, patterns: tuple[PathPattern, ...], specs: list[_AtomSpec],
                 capture: frozenset[int]) -> None:
        self.test = test
        self.patterns = patterns
        self.specs = specs
        self.capture = capture
        self.captures_root = any(not patterns[s].segments for s in capture)


class _StreamCompiler(_Compiler):
    """Like :class:`_Compiler`, but conditions a probe can decide on the event stream
    become *atoms* (``ctx.atoms[i]``); the paths of everything else are captured."""

    def __init__(self) -> None:
        super().__init__()
        self.specs: list[_AtomSpec] = []
        self.capture: set[int] = set()

    def slot(self, pattern: PathPattern) -> int:  # values needed: capture them
        s = super().slot(pattern)
        self.capture.add(s)
        return s

    def probe_slot(self, pattern: PathPattern) -> int:
        return super().slot(pattern)

    def test(self, node: Expr) -> _TestFn:
        found = self.atom(node)
        if found is None:
            return super().test(node)
        spec, negate = found
        i = len(self.specs)
        self.specs.append(spec)
        if negate:
            return lambda ctx: not ctx.atoms[i]
        return lambda ctx: ctx.atoms[i]

    def plan(self, expr: Expr) -> _StreamPlan:
        test = self.test(expr)
        return _StreamPlan(test, tuple(self.patterns), self.specs, frozenset(self.capture))

    # -- atoms -----------------------------------------------------------------------
    def source(self, node: Expr) -> tuple[int, str | None] | None:
        """``(slot, derive)`` of a path or of ``lower/upper/trim/type/number/len(path)``."""
        if isinstance(node, PathExpr):
            return self.probe_slot(node.pattern), None
        if isinstance(node, Call) and len(node.args) == 1 and isinstance(node.args[0], PathExpr):
            if node.name in _STREAM_DERIVED or node.name == "len":
                return self.probe_slot(node.args[0].pattern), node.name
        return None

    def atom(self, node: Expr) -> tuple[_AtomSpec, bool] | None:
        if isinstance(node, PathExpr):
            return _AtomSpec(self.probe_slot(node.pattern), "any", _truthy, cmode="truthy"), False
        if isinstance(node, Call) and node.name not in _BOOL_FUNCS:
            src = self.source(node)  # truthiness of len(x), lower(x), ...
            return None if src is None else (self.make(src, _truthy), False)
        if isinstance(node, Is) and isinstance(node.operand, PathExpr):
            slot = self.probe_slot(node.operand.pattern)
            if node.what == "missing":
                return _AtomSpec(slot, "count", lambda n: n == 0), node.negate
            if node.what == "null":
                return _AtomSpec(slot, "any", lambda v: v is None), node.negate
            return _AtomSpec(slot, "any", _is_empty, cmode="empty"), node.negate
        if isinstance(node, Call) and node.name in _BOOL_FUNCS and len(node.args) == 1 \
                and isinstance(node.args[0], PathExpr):
            slot = self.probe_slot(node.args[0].pattern)
            if node.name == "exists":
                return _AtomSpec(slot, "count", lambda n: n > 0), False
            if node.name == "missing":
                return _AtomSpec(slot, "count", lambda n: n == 0), False
            return _AtomSpec(slot, "any", _is_empty, cmode="empty"), False
        if isinstance(node, Compare):
            return self.compare_atom(node)
        if isinstance(node, Between):
            if not (isinstance(node.low, Literal) and isinstance(node.high, Literal)):
                return None
            src = self.source(node.operand)
            if src is None:
                return None
            return self.make(src, _between_pred(node.low.value, node.high.value)), False
        if isinstance(node, In):
            if isinstance(node.right, Literal):
                container = node.right.value
                if isinstance(container, _ARRAY_TYPES) and any(_is_container(x) for x in container):
                    return None  # deep equality with container values: needs the values
                src = self.source(node.left)
                if src is None:
                    return None
                return self.make(src, _in_const_pred(container)), node.negate
            if isinstance(node.left, Literal) and not _is_container(node.left.value):
                src = self.source(node.right)
                if src is None or src[1] is not None:
                    return None
                item = node.left.value
                return _AtomSpec(src[0], "any", lambda c: _contains(c, item), cmode="contains", item=item), \
                    node.negate
        return None

    def compare_atom(self, node: Compare) -> tuple[_AtomSpec, bool] | None:
        op, left, right = node.op, node.left, node.right
        if isinstance(left, Literal) and not isinstance(right, Literal) and op in _FLIPPED_OPS:
            left, right, op = right, left, _FLIPPED_OPS[op]
        if not isinstance(right, Literal):
            return None
        src = self.source(left)
        if src is None:
            return None
        b = right.value
        negate = op == "!="
        if negate:
            op = "=="
        if op == "matches":
            if not isinstance(b, str):
                return None
            search = re.compile(b).search
            return self.make(src, lambda v: isinstance(v, str) and search(v) is not None), negate
        if op == "contains":
            if _is_container(b):
                return None  # deep equality of elements with a container: needs the values
            pred = _const_pred(op, b)
            if src[1] is None:
                return _AtomSpec(src[0], "any", pred, cmode="contains", item=b), negate
            return self.make(src, pred), negate
        if op == "==" and _is_container(b):
            return None  # container equality needs the values
        if op not in _BINARY:
            return None
        return self.make(src, _const_pred(op, b)), negate

    @staticmethod
    def make(src: tuple[int, str | None], pred: Callable[[Any], bool]) -> _AtomSpec:
        slot, derive = src
        if derive == "len":
            return _AtomSpec(slot, "first_len", pred)
        return _AtomSpec(slot, "any", pred, derive=derive)


# ==================================================================================
# JMESPath
# ==================================================================================
_EXACT_NUMBER_TYPE_NAMES = ("JsonFloat", "JsonInt", "Decimal")


def _with_exact_numbers(names: Any) -> Any:
    if "float" in names:
        return type(names)(list(names) + list(_EXACT_NUMBER_TYPE_NAMES))
    return names


class _JmesFunctions(jmespath_functions.Functions):
    """JMESPath built-ins that treat codec.JsonFloat/JsonInt (and Decimal) as numbers."""

    def _get_allowed_pytypes(self, types):  # type: ignore[override]
        allowed, subtypes = super()._get_allowed_pytypes(types)
        return _with_exact_numbers(allowed), [_with_exact_numbers(s) for s in subtypes]

    def _convert_to_jmespath_type(self, pyobject):  # type: ignore[override]
        if pyobject in _EXACT_NUMBER_TYPE_NAMES:
            return "number"
        return super()._convert_to_jmespath_type(pyobject)

    @jmespath_functions.signature({"types": []})
    def _func_to_string(self, arg):
        if isinstance(arg, str):
            return arg
        return codec.dumps(arg).decode("utf-8")


_JMES_OPTIONS = jmespath.Options(custom_functions=_JmesFunctions())


def _find_call(text: str, name: str) -> int:
    m = re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"\s*\(", text)
    return m.start() if m is not None else 0


def _jmes_check_functions(parsed: Any, text: str) -> None:
    """Report unknown functions and wrong argument counts at compile time."""
    table = _JmesFunctions.FUNCTION_TABLE
    stack = [parsed]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("type") == "function_expression":
            name = node.get("value")
            args = node.get("children") or []
            spec = table.get(name)
            if spec is None:
                raise _syntax_error(text, _find_call(text, str(name)),
                                    t("query.jmespath.unknown_function", name=name, names=", ".join(sorted(table))))
            signature = spec["signature"]
            variadic = bool(signature) and bool(signature[-1].get("variadic"))
            if (variadic and len(args) < len(signature)) or (not variadic and len(args) != len(signature)):
                raise _syntax_error(text, _find_call(text, str(name)),
                                    t("query.jmespath.arg_count", name=name, expected=len(signature), got=len(args)))
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))


#: JMESPath's "Unclosed X delimiter": what was left open
_JMES_UNCLOSED = {"'": "query.syntax.unterminated_string", '"': "query.jmespath.unclosed_name",
                  "`": "query.jmespath.unclosed_literal"}
#: JMESPath parser messages that mean "this token is not allowed here"
_JMES_UNEXPECTED = ("invalid token", "syntax error", "Unexpected token", "Expecting:", "Invalid function name")


def _jmes_lexer_detail(message: str, value: Any) -> str:
    """Localized description of a JMESPath lexer error: an unclosed ``'``/``"``/backtick,
    an invalid JSON literal between backticks ("Bad token"), an unexpected character
    ("Unknown token") or an invalid quoted name; JMESPath's English text only for others."""
    if message.startswith("Unclosed ") and message.endswith(" delimiter"):
        quote = message[len("Unclosed "):-len(" delimiter")]
        key = _JMES_UNCLOSED.get(quote)
        if key is not None:
            return t(key, quote=quote)
    if message.startswith("Bad token "):
        return t("query.jmespath.bad_literal", token=message[len("Bad token "):].strip())
    if message.startswith("Unknown token") and value:
        return t("query.syntax.unexpected_char", char=repr(str(value)))
    if isinstance(value, str) and value.startswith('"'):  # a quoted name that is not valid JSON text
        return t("query.jmespath.bad_name", name=value)
    return t("query.jmespath.syntax", error=message)


def _jmes_compile(text: str) -> Any:
    """``jmespath.compile`` with errors turned into localized QuerySyntaxError (JMESPath's
    own English message is kept as the parameter ``raw``)."""
    try:
        parsed = jmespath.compile(text)
    except jmespath_exceptions.EmptyExpressionError:
        raise _syntax_error(text, 0, t("query.syntax.empty")) from None
    except jmespath_exceptions.LexerError as exc:
        message = str(exc.message or "")
        err = _syntax_error(text, int(exc.lexer_position or 0), _jmes_lexer_detail(message, exc.lexer_value))
        err.params["raw"] = message
        raise err from None
    except jmespath_exceptions.IncompleteExpressionError:
        raise _syntax_error(text, len(text), t("query.jmespath.incomplete")) from None
    except jmespath_exceptions.ParseError as exc:
        msg = str(exc.msg or "")
        if msg.startswith(_JMES_UNEXPECTED):
            detail = t("query.syntax.unexpected_token", token=f"'{exc.token_value}'")
        elif msg.startswith("Quoted identifier not allowed"):
            detail = t("query.jmespath.quoted_function")
        else:
            detail = t("query.jmespath.syntax", error=f"{msg} ('{exc.token_value}')")
        err = _syntax_error(text, int(exc.lex_position or 0), detail)
        err.params["raw"] = msg
        raise err from None
    except jmespath_exceptions.JMESPathError as exc:
        raise _syntax_error(text, 0, t("query.jmespath.syntax", error=str(exc))) from None
    except RecursionError:
        raise _syntax_error(text, 0, t("query.syntax.too_deep", limit=MAX_NESTING)) from None
    _jmes_check_functions(parsed.parsed, text)
    return parsed


def _jmes_truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (str, list, dict)) and len(v) == 0:
        return False
    return True


class FilterEvalError(UserInputError):
    """A filter could not be evaluated on one record (a JMESPath runtime error such as a
    function given a value of the wrong type).

    ``params``: ``reason`` -- a localized sentence; ``detail`` -- the evaluator's own
    (English) text. :func:`jsonreshape.recordset.filter_records` counts such records as
    not matching (``FilterReport.eval_errors``) instead of aborting.
    """

    key = "query.jmespath.runtime"


#: Python / JMESPath type names -> the JSON type names of schema.type.*
_JMES_TYPE_NAMES = {
    "NoneType": "null", "null": "null", "str": "string", "string": "string", "int": "number", "float": "number",
    "JsonFloat": "number", "JsonInt": "number", "Decimal": "number", "number": "number", "bool": "bool",
    "boolean": "bool", "list": "array", "array": "array", "dict": "object", "object": "object",
}
_PY_COMPARE_RE = re.compile(r"'(?P<op>[^']+)' not supported between instances of '(?P<a>\w+)' and '(?P<b>\w+)'")


def _jmes_type_label(name: Any) -> str:
    """Localized JSON type name of a JMESPath / Python type name (``array-number`` ->
    ``massiv (son)``)."""
    text = str(name)
    base, _, sub = text.partition("-")
    json_name = _JMES_TYPE_NAMES.get(base)
    label = t("schema.type." + json_name) if json_name else text
    if sub:
        sub_name = _JMES_TYPE_NAMES.get(sub)
        label += " (" + (t("schema.type." + sub_name) if sub_name else sub) + ")"
    return label


def _jmes_reason(exc: BaseException) -> str:
    """A localized sentence describing a JMESPath runtime error."""
    if isinstance(exc, jmespath_exceptions.JMESPathTypeError):
        expected = exc.expected_types if isinstance(exc.expected_types, (list, tuple)) else [exc.expected_types]
        return t("query.jmespath.err.type", function=exc.function_name, received=_jmes_type_label(exc.actual_type),
                 expected=", ".join(_jmes_type_label(x) for x in expected))
    if isinstance(exc, TypeError):
        m = _PY_COMPARE_RE.search(str(exc))
        if m is not None:
            return t("query.jmespath.err.compare", op=m.group("op"), left=_jmes_type_label(m.group("a")),
                     right=_jmes_type_label(m.group("b")))
    if isinstance(exc, (ArithmeticError, ValueError)) and not isinstance(exc, jmespath_exceptions.JMESPathError):
        return t("query.jmespath.err.number")
    if isinstance(exc, RecursionError):
        return t("query.jmespath.err.too_deep")
    return t("query.jmespath.err.other")


def _jmes_eval_error(exc: BaseException) -> FilterEvalError:
    detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return FilterEvalError(reason=_jmes_reason(exc), detail=detail[:300])


def _undecimal(value: Any) -> Any:
    """Copy of ``value`` with every Decimal replaced by its exact codec number."""
    if isinstance(value, Decimal):
        return _dec(value)
    if not isinstance(value, (list, dict)):
        return value
    holder: list = [None]
    stack: list[tuple[Any, Any, Any]] = [(value, holder, 0)]
    while stack:
        src, parent, slot = stack.pop()
        if isinstance(src, dict):
            dst: Any = {}
            items = src.items()
        else:
            dst = [None] * len(src)
            items = enumerate(src)
        parent[slot] = dst
        for k, x in items:
            if isinstance(x, (list, dict)):
                if isinstance(dst, dict):
                    dst[k] = None  # keep key order
                stack.append((x, dst, k))
            elif isinstance(x, Decimal):
                dst[k] = _dec(x)
            else:
                dst[k] = x
    return holder[0]


# ==================================================================================
# Filter
# ==================================================================================
_ROOT_PATTERN = PathPattern(())
_NOT_GIVEN = object()


def _rebuild_filter(lang: str, text: str) -> Filter:
    return compile_filter(text, lang)


class Filter:
    """A compiled record filter (thread-safe; immutable after construction -- the
    streamed form used by :meth:`matches_stream` is compiled on first use, an idempotent
    cache).

    Build with :func:`compile_filter` or :meth:`from_builder`.

    * ``lang``: ``"expr"`` or ``"jmespath"``; ``text``: the expression (for builder filters
      the rendering of :meth:`Group.to_text`); ``expr``: the AST (``None`` for JMESPath);
      ``warnings``: localized hints, e.g. a bare word that was probably meant as a string.
    * ``uses_key`` / ``uses_index``: the expression refers to ``$key`` / ``$index``.
    * :meth:`matches` (parsed record), :meth:`matches_values` (pre-extracted values),
      :meth:`matches_stream` (a record streamed from the file).
    """

    __slots__ = ("lang", "text", "expr", "warnings", "uses_key", "uses_index", "_patterns", "_test", "_jmes",
                 "_jmes_local", "_stream")

    def __init__(self, *, lang: str, text: str, expr: Expr | None = None, jmes: Any = None,
                 warnings: list[str] | None = None) -> None:
        self.lang = lang
        self.text = text
        self.expr = expr
        self.warnings: list[str] = list(warnings or [])
        self._jmes = jmes
        # one JMESPath interpreter per thread, reused for every record: jmespath's own
        # ParsedResult.search() builds a new TreeInterpreter per call, and each one is a
        # reference cycle (its method cache holds bound methods) -- millions of cyclic
        # garbage objects per filter run (review T1)
        self._jmes_local = threading.local() if jmes is not None else None
        self._stream: _StreamPlan | None = None   # compiled on first use
        if expr is not None:
            comp = _Compiler()
            self._test: _TestFn | None = comp.test(expr)
            self._patterns: tuple[PathPattern, ...] = tuple(comp.patterns)
            self.uses_key = comp.uses_key
            self.uses_index = comp.uses_index
        else:
            self._test = None
            self._patterns = ()
            self.uses_key = False
            self.uses_index = False

    def __repr__(self) -> str:
        return f"Filter(lang={self.lang!r}, text={self.text!r})"

    def __reduce__(self):
        return (_rebuild_filter, (self.lang, self.text))

    # -- metadata ------------------------------------------------------------------
    @property
    def required_patterns(self) -> list[PathPattern] | None:
        """Patterns whose values the filter needs, in first-use order (``$`` is the empty
        pattern = the whole record). ``None`` for JMESPath (needs the whole value)."""
        if self._jmes is not None:
            return None
        return list(self._patterns)

    @property
    def extractable(self) -> bool:
        """True when every required pattern works with ``core.PathExtractor`` (no ``**``)."""
        return self._jmes is None and all(Wild.DESCENDANTS not in p.segments for p in self._patterns)

    # -- evaluation ----------------------------------------------------------------
    def matches(self, value: Any, key: str | None = None, index: int | None = None) -> bool:
        """Does the record ``value`` match? ``key``: member key of a keyed-root record
        (``$key``); ``index``: record number (``$index``). A JMESPath expression that
        cannot be evaluated on this value raises :class:`FilterEvalError`."""
        if self._jmes is not None:
            return self._jmes_matches(value)
        assert self._test is not None
        n = len(self._patterns)
        return self._test(_Ctx(value, key, index, [None] * n if n else _EMPTY, self._patterns))

    def matches_values(self, values: Mapping[Any, Any], key: str | None = None, index: int | None = None) -> bool:
        """Evaluate from pre-extracted values (huge records streamed with ijson).

        ``values`` maps each required :class:`PathPattern` (or its position in
        :attr:`required_patterns`) to the list of its values; the result equals
        :meth:`matches` whenever ``values[p] == p.values(record)``. ``Decimal`` numbers are
        accepted. JMESPath filters need the whole record under the empty pattern ``$``.
        """
        if self._jmes is not None:
            vals = values.get(_ROOT_PATTERN, _NOT_GIVEN)
            if vals is _NOT_GIVEN or len(vals) != 1:
                raise ValueError("JMESPath filters need the whole record: values[PathPattern(())] == [record]")
            return self._jmes_matches(_undecimal(vals[0]))
        assert self._test is not None
        cache: list = []
        for i, pattern in enumerate(self._patterns):
            vals = values.get(pattern, _NOT_GIVEN)
            if vals is _NOT_GIVEN:
                vals = values.get(i, _NOT_GIVEN)
                if vals is _NOT_GIVEN:
                    raise ValueError(f"matches_values: no values given for required pattern {pattern.text!r}")
            cache.append(vals if isinstance(vals, list) else list(vals))
        return self._test(_Ctx(None, key, index, cache, self._patterns))

    # -- streamed evaluation (huge records) -------------------------------------------
    def _stream_plan(self) -> _StreamPlan:
        plan = self._stream
        if plan is None:
            assert self.expr is not None
            plan = self._stream = _StreamCompiler().plan(self.expr)
        return plan

    @property
    def stream_captures_root(self) -> bool:
        """True when :meth:`matches_stream` needs the whole record value in memory
        (JMESPath, or a part of the expression that uses ``$`` as a value, e.g.
        ``$ == {...}`` or ``string($)``) -- impossible for a huge record."""
        if self._jmes is not None:
            return True
        return self._stream_plan().captures_root

    def matches_stream(self, doc: Any, span: tuple[int, int], member: bool = False, *, key: str | None = None,
                       index: int | None = None, max_bytes: int | None = None, cancel: Any = None) -> bool:
        """:meth:`matches` for a record that is streamed instead of loaded (``span`` of
        ``doc``'s data; ``member``: the span is an object member, see
        :mod:`jsonreshape.stream`). See "Huge records" in the module docstring: conditions
        decided on the event stream keep nothing; the values other parts of the expression
        need are captured within ``max_bytes`` (:class:`~jsonreshape.stream.CaptureLimitExceeded`
        when they would exceed it)."""
        if self._jmes is not None:
            vals = stream_values(doc, span, [_ROOT_PATTERN], member, max_bytes=max_bytes, cancel=cancel)[0]
            return self.matches_values({_ROOT_PATTERN: vals}, key=key, index=index)
        plan = self._stream_plan()
        if not plan.patterns:
            return plan.test(_StreamCtx(key, index, [], (), []))
        atoms = [spec.probe() for spec in plan.specs]
        probes: dict[int, list[Any]] = {}
        for spec, atom in zip(plan.specs, atoms):
            probes.setdefault(spec.slot, []).append(atom)
        captured: dict[int, list[Any]] | None
        try:
            captured = stream_probe(doc, span, plan.patterns, probes, member, capture=plan.capture,
                                    max_bytes=max_bytes, cancel=cancel)
        except DuplicateKeys:
            captured = None
        if captured is None:
            # dict semantics (the last of duplicate keys wins) need the captured values
            log.debug("filter %r: duplicate keys in the streamed record; evaluating captured values", self.text)
            values = stream_values(doc, span, self._patterns, member, max_bytes=max_bytes, cancel=cancel)
            return self.matches_values(values, key=key, index=index)
        cache = [captured.get(s) for s in range(len(plan.patterns))]
        return plan.test(_StreamCtx(key, index, cache, plan.patterns, [a.result() for a in atoms]))

    def _jmes_matches(self, value: Any) -> bool:
        local = self._jmes_local
        interpreter = getattr(local, "interpreter", None)
        if interpreter is None:
            interpreter = local.interpreter = jmespath_visitor.TreeInterpreter(_JMES_OPTIONS)
        try:
            result = interpreter.visit(self._jmes.parsed, value)
        except (jmespath_exceptions.JMESPathError, TypeError, ValueError, ArithmeticError, RecursionError) as exc:
            raise _jmes_eval_error(exc) from exc
        return _jmes_truthy(result)

    # -- builder -------------------------------------------------------------------
    @classmethod
    def from_builder(cls, group: Group | Condition) -> Filter:
        """Build a filter directly from the builder model (the AST is not parsed from
        text); equivalent to ``compile_filter(group.to_text())``, errors included."""
        if isinstance(group, Condition):
            group = Group(items=[group])
        if not isinstance(group, Group):
            raise TypeError(f"expected Group or Condition, not {type(group).__name__}")
        built = _BuilderCompiler()
        expr = built.build(group)
        return cls(lang="expr", text=built.text, expr=expr)


def compile_filter(text: str, lang: str = "expr") -> Filter:
    """Compile a filter expression (``lang="expr"``, §9) or JMESPath (``lang="jmespath"``).

    Raises :class:`~jsonreshape.errors.QuerySyntaxError` (``pos`` = 0-based offset) for
    invalid expressions; ``ValueError`` for an unknown ``lang``.
    """
    if not isinstance(text, str):
        raise TypeError(f"filter text must be str, not {type(text).__name__}")
    if lang == "expr":
        parser = _Parser(text)
        expr = parser.parse()
        return Filter(lang="expr", text=text, expr=expr, warnings=parser.warnings)
    if lang == "jmespath":
        return Filter(lang="jmespath", text=text, jmes=_jmes_compile(text))
    raise ValueError(f"unknown filter language {lang!r}; expected one of {LANGUAGES}")


# ==================================================================================
# named filters (presets)
# ==================================================================================
PRESETS_FILE = "filter_presets.json"
PRESETS_VERSION = 1
PRESET_NAME_MAX = 200           # characters
_PRESETS_MAX_BYTES = 8 << 20    # a larger presets file is treated as corrupt
_PRESET_BAD_NAME_RE = re.compile(r"[\x00-\x1f\x7f]")
LOCK_TIMEOUT = 10.0             # seconds to wait for another process changing the same file
_OPEN_RETRY = 0.5               # seconds a lock file that cannot be opened is retried

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - closing the handle releases it anyway
            pass
else:  # pragma: no cover - POSIX
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextlib.contextmanager
def file_lock(path: str | os.PathLike[str], busy_key: str = "query.preset.busy",
              timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive lock between processes on the small JSON file ``path`` while it is
    read, changed and written back (a read-modify-write of two processes would otherwise
    lose one of the changes): the file ``<path>.lock`` next to it, locked with
    ``msvcrt.locking`` (Windows) / ``fcntl.flock`` (POSIX). The OS drops the lock when the
    holder's handle closes -- also when the process is killed -- so it is never stale; the
    empty lock file is left in place (deleting it would race with the next opener). The
    folder is created first (:func:`jsonreshape.fileio.make_dirs`: long paths work). Waits
    up to ``timeout`` seconds, then raises ``JsonReshapeError(busy_key, path=...)``; OS
    errors are raised localized (:func:`jsonreshape.fileio.os_error`)."""
    target = Path(path)
    lock_path = target.with_name(target.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0) \
        | getattr(os, "O_CLOEXEC", 0)
    started = time.monotonic()
    deadline = started + timeout
    delay = 0.005
    try:
        make_dirs(target.parent)
    except OSError as exc:
        raise os_error(exc, target.parent, writing=True) from exc
    while True:
        try:
            fd = os.open(long_path(lock_path), flags, 0o666)
            break
        except PermissionError as exc:  # e.g. a virus scanner holds it for a moment
            if time.monotonic() >= min(deadline, started + _OPEN_RETRY):  # an unwritable folder fails soon
                raise os_error(exc, target, writing=True) from exc
        except OSError as exc:
            raise os_error(exc, target, writing=True) from exc
        time.sleep(delay)
        delay = min(delay * 2, 0.1)
    try:
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise JsonReshapeError(busy_key, path=str(target))
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _preset_name(name: Any) -> str:
    if not isinstance(name, str):
        raise UserInputError("query.preset.bad_name", name=repr(name), limit=PRESET_NAME_MAX)
    clean = name.strip()
    if not clean or len(clean) > PRESET_NAME_MAX or _PRESET_BAD_NAME_RE.search(clean):
        raise UserInputError("query.preset.bad_name", name=repr(name), limit=PRESET_NAME_MAX)
    return clean


class FilterPresets:
    """Named filters (AGENT_PROMPT §6.3 "Filtrlar nom bilan saqlanadi (preset)").

    A JSON file (default ``app_home()/filter_presets.json``) holding entries
    ``{"name", "text", "lang", "saved_at"}``: ``lang`` is ``"expr"`` or ``"jmespath"``,
    ``saved_at`` the time of the last save (``time.time()``, seconds since the epoch).
    Names are trimmed, 1..200 characters without control characters, and unique
    regardless of case (saving ``Toshkent`` replaces ``toshkent``; the new spelling is
    kept). :meth:`save` only stores expressions that compile (``QuerySyntaxError``
    otherwise). A missing or corrupt file reads as empty; writes are atomic. :meth:`save`
    and :meth:`delete` read, change and write the file under :func:`file_lock` (and a
    thread lock), so several processes saving presets at the same time (two consoles, a
    batch script, the GUI and the CLI) never lose one another's entries; the empty file
    ``filter_presets.json.lock`` stays next to it. The folder is created with
    :func:`jsonreshape.fileio.make_dirs` (long paths work); file-system errors are raised
    localized (:func:`jsonreshape.fileio.os_error`), a lock held longer than
    :data:`LOCK_TIMEOUT` as ``query.preset.busy``.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else app_home() / PRESETS_FILE
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            with open_shared(self.path, "rb") as f:
                raw = read_limited(f, _PRESETS_MAX_BYTES)
        except Exception as exc:  # missing (normal before the first save) or unreadable
            if os.path.exists(long_path(self.path)):
                log.warning("cannot read the filter presets %s: %s", self.path, exc)
            return []
        if len(raw) > _PRESETS_MAX_BYTES:
            log.warning("filter presets file %s is too large; ignoring it", self.path)
            return []
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            log.warning("filter presets file %s is corrupt (%s); ignoring it", self.path, exc)
            return []
        items = data.get("presets") if isinstance(data, dict) else None
        if not isinstance(items, list):
            log.warning("filter presets file %s has an unexpected layout; ignoring it", self.path)
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            name, text, lang, when = item.get("name"), item.get("text"), item.get("lang", "expr"), item.get("saved_at")
            if not isinstance(name, str) or not name.strip() or not isinstance(text, str) or lang not in LANGUAGES:
                continue
            folded = name.strip().casefold()
            if folded in seen:
                continue
            seen.add(folded)
            out.append({
                "name": name.strip(),
                "text": text,
                "lang": lang,
                "saved_at": float(when) if isinstance(when, (int, float)) and not isinstance(when, bool) else 0.0,
            })
        return out

    def _save(self, entries: list[dict[str, Any]]) -> None:
        try:
            make_dirs(self.path.parent)
        except OSError as exc:
            raise os_error(exc, self.path.parent, writing=True) from exc
        entries = sorted(entries, key=lambda e: (e["name"].casefold(), e["name"]))
        payload = {"version": PRESETS_VERSION, "presets": entries}
        data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8", "backslashreplace") + b"\n"
        with atomic_write(self.path, "wb") as f:
            f.write(data)

    def save(self, name: str, text: str, lang: str = "expr") -> None:
        """Store (or replace) the filter ``text`` (language ``lang``) under ``name``.

        Raises ``UserInputError`` (``query.preset.bad_name`` / ``query.preset.bad_lang`` /
        ``query.preset.empty``) or the :class:`QuerySyntaxError` of an invalid expression.
        """
        clean = _preset_name(name)
        if lang not in LANGUAGES:
            raise UserInputError("query.preset.bad_lang", lang=repr(lang), langs=", ".join(LANGUAGES))
        if not isinstance(text, str) or not text.strip():
            raise UserInputError("query.preset.empty", name=clean)
        compile_filter(text, lang)  # only valid filters are saved
        folded = clean.casefold()
        with self._lock, file_lock(self.path, "query.preset.busy"):
            entries = [e for e in self._load() if e["name"].casefold() != folded]
            entries.append({"name": clean, "text": text, "lang": lang, "saved_at": time.time()})
            self._save(entries)

    def get(self, name: str) -> dict[str, Any]:
        """``{"name", "text", "lang", "saved_at"}`` of the preset ``name`` (any case);
        ``UserInputError("query.preset.not_found")`` when there is none."""
        clean = _preset_name(name)
        folded = clean.casefold()
        with self._lock:
            for entry in self._load():
                if entry["name"].casefold() == folded:
                    return entry
        raise UserInputError("query.preset.not_found", name=clean)

    def list(self) -> list[dict[str, Any]]:
        """Every preset (copies), sorted by name (case-insensitively)."""
        with self._lock:
            entries = self._load()
        return sorted(entries, key=lambda e: (e["name"].casefold(), e["name"]))

    def names(self) -> list[str]:
        """Just the preset names, sorted."""
        return [e["name"] for e in self.list()]

    def delete(self, name: str) -> bool:
        """Remove the preset ``name`` (any case); returns whether it existed."""
        clean = _preset_name(name)
        folded = clean.casefold()
        with self._lock:
            if not any(e["name"].casefold() == folded for e in self._load()):
                return False  # nothing to change: no lock (and no folder) needed
            with file_lock(self.path, "query.preset.busy"):
                entries = self._load()
                kept = [e for e in entries if e["name"].casefold() != folded]
                if len(kept) == len(entries):
                    return False
                self._save(kept)
                return True

    def filter(self, name: str) -> Filter:
        """The compiled filter of the preset ``name``."""
        entry = self.get(name)
        return compile_filter(entry["text"], entry["lang"])


# ==================================================================================
# visual builder model
# ==================================================================================
#: Builder operators (GUI order).
BUILDER_OPS = (
    "eq",
    "ne",
    "contains",
    "startswith",
    "regex",
    "gt",
    "lt",
    "ge",
    "le",
    "between",
    "exists",
    "not_exists",
    "is_null",
    "is_empty",
    "in",
    "len_eq",
    "len_ne",
    "len_gt",
    "len_lt",
    "len_ge",
    "len_le",
)
_OP_ARITY = {
    "eq": 1, "ne": 1, "contains": 1, "startswith": 1, "regex": 1, "gt": 1, "lt": 1, "ge": 1, "le": 1,
    "between": 2, "exists": 0, "not_exists": 0, "is_null": 0, "is_empty": 0, "in": 1,
    "len_eq": 1, "len_ne": 1, "len_gt": 1, "len_lt": 1, "len_ge": 1, "len_le": 1,
}
_SIMPLE_OPS = {
    "eq": "==", "ne": "!=", "contains": "contains", "startswith": "startswith", "regex": "matches",
    "gt": ">", "lt": "<", "ge": ">=", "le": "<=",
}
_LEN_OPS = {"len_eq": "==", "len_ne": "!=", "len_gt": ">", "len_lt": "<", "len_ge": ">=", "len_le": "<="}


def op_arity(op: str) -> int:
    """Number of values a builder operator uses (0, 1 or 2); ``ValueError`` if unknown."""
    try:
        return _OP_ARITY[op]
    except KeyError:
        raise ValueError(f"unknown builder operator: {op!r}") from None


def op_label(op: str) -> str:
    """Localized GUI label of a builder operator."""
    op_arity(op)
    return t("query.op." + op)


@dataclass
class Condition:
    """One builder row.

    ``field``: a path in paths.py syntax (as produced by ``paths.format_path``; ``""``/``"$"``
    = the record itself) or ``"$key"`` / ``"$index"``; ``op``: one of :data:`BUILDER_OPS`;
    ``value`` / ``value2``: JSON values (``value2`` only for ``between``; ``in`` expects a
    list). Value-less operators ignore the values.
    """

    field: str
    op: str
    value: Any = None
    value2: Any = None

    def to_text(self) -> str:
        """The condition as an expression."""
        return Group(items=[self]).to_text()

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (for filter presets); see :meth:`from_dict`."""
        field = self.field.text if isinstance(self.field, PathPattern) else self.field
        return {"field": field, "op": self.op, "value": self.value, "value2": self.value2}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Condition:
        return cls(field=data.get("field", ""), op=data.get("op", ""), value=data.get("value"),
                   value2=data.get("value2"))


@dataclass
class Group:
    """AND/OR group of conditions and sub-groups, optionally negated.

    An empty group imposes no condition: nested empty groups are skipped and an empty
    top-level group matches every record (rendered as ``true``) — also when negated.
    """

    op: str = "and"
    items: list[Union[Condition, Group]] = dc_field(default_factory=list)
    negate: bool = False

    def to_text(self) -> str:
        """A valid filter expression equivalent to :meth:`Filter.from_builder`.

        Raises the same errors as :meth:`Filter.from_builder` for invalid trees:
        :class:`QuerySyntaxError` (key ``query.builder.error``, ``params["item"]`` = index
        path of the bad row) or :class:`PathSyntaxError` for a bad field, and the regular
        positioned QuerySyntaxError for an invalid regex value.
        """
        built = _BuilderCompiler()
        built.build(self)
        return built.text

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "negate": self.negate, "items": [item.to_dict() for item in self.items]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Group:
        items: list[Condition | Group] = []
        for item in data.get("items") or []:
            items.append(Group.from_dict(item) if "items" in item else Condition.from_dict(item))
        return cls(op=data.get("op", "and"), items=items, negate=bool(data.get("negate", False)))


#: Maximum nesting of builder groups and of container values inside a builder value.
#: Small enough that every rendered builder text stays within the parser's MAX_NESTING
#: (worst case 1 + 2*20 + 1 + 2*20 = 82 levels), so ``to_text()`` always parses.
BUILDER_MAX_DEPTH = 20


def _builder_error(detail: str, item: tuple[int, ...]) -> QuerySyntaxError:
    """Error in a builder tree; ``item`` (``exc.params["item"]``) is the index path of the
    offending row/group from the root group (for highlighting it in the GUI)."""
    return QuerySyntaxError("query.builder.error", pos=0, text="", detail=detail, item=item)


def _builder_field(field: Any, item: tuple[int, ...]) -> tuple[Expr, str]:
    """(AST node, expression text) of a builder field."""
    if isinstance(field, PathPattern):
        pattern = field
    elif isinstance(field, str):
        text = field.strip()
        low = text.lower()
        if low == "$key":
            return SpecialExpr("key"), "$key"
        if low == "$index":
            return SpecialExpr("index"), "$index"
        try:
            pattern = parse_pattern(text)
        except PathSyntaxError as exc:
            raise PathSyntaxError(pos=exc.pos, text=exc.text, detail=exc.params.get("detail", ""), item=item) from None
    else:
        raise _builder_error(t("query.builder.bad_field", type=type(field).__name__), item)
    return PathExpr(pattern), render_path(pattern)


def _check_builder_value(value: Any, item: tuple[int, ...]) -> None:
    """A builder value must be a finite JSON value nested at most BUILDER_MAX_DEPTH deep
    (which also rejects self-referencing containers)."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        x, depth = stack.pop()
        if x is None or isinstance(x, (bool, str)):
            continue
        if isinstance(x, float):
            if not isinstance(x, codec.JsonFloat) and not math.isfinite(x):
                raise _builder_error(t("query.builder.non_finite"), item)
            continue
        if isinstance(x, int):
            continue
        if isinstance(x, Decimal):
            if not x.is_finite():
                raise _builder_error(t("query.builder.non_finite"), item)
            continue
        if isinstance(x, (list, tuple, dict)):
            if depth >= BUILDER_MAX_DEPTH:
                raise _builder_error(t("query.syntax.too_deep", limit=BUILDER_MAX_DEPTH), item)
            if isinstance(x, dict):
                for k, v in x.items():
                    if not isinstance(k, str):
                        raise _builder_error(t("query.builder.bad_value", type=type(k).__name__), item)
                    stack.append((v, depth + 1))
            else:
                stack.extend((v, depth + 1) for v in x)
            continue
        raise _builder_error(t("query.builder.bad_value", type=type(x).__name__), item)


class _BuilderCompiler:
    """Renders a builder tree to text and, in the same walk, builds its AST directly
    from the model (field patterns, operators, exact literal values) — never by parsing
    the text. Node positions point into the rendered text so that errors (bad regex)
    are reported exactly like ``compile_filter(to_text())`` reports them."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.regex_nodes: list[Compare] = []
        self.text = ""

    def emit(self, s: str) -> None:
        self.parts.append(s)
        self.length += len(s)

    def build(self, group: Group) -> Expr:
        self.validate(group, (), 0)
        if self.is_empty(group):
            self.emit("true")
            node: Expr = _literal(True, 0)
        else:
            node = self.group(group, (), True)
        self.text = "".join(self.parts)
        for cmp_node in self.regex_nodes:
            _check_regex(cmp_node, self.text)
        return node

    # -- validation ----------------------------------------------------------------
    def validate(self, group: Any, path: tuple[int, ...], depth: int) -> None:
        if depth >= BUILDER_MAX_DEPTH:
            raise _builder_error(t("query.syntax.too_deep", limit=BUILDER_MAX_DEPTH), path)
        if not isinstance(group.op, str) or group.op.lower() not in ("and", "or"):
            raise _builder_error(t("query.builder.bad_group_op", op=group.op), path)
        if not isinstance(group.items, (list, tuple)):
            raise _builder_error(t("query.builder.bad_item", type=type(group.items).__name__), path)
        for i, item in enumerate(group.items):
            sub = path + (i,)
            if isinstance(item, Group):
                self.validate(item, sub, depth + 1)
            elif isinstance(item, Condition):
                if not isinstance(item.op, str) or item.op not in _OP_ARITY:
                    raise _builder_error(t("query.builder.unknown_op", op=item.op), sub)
            else:
                raise _builder_error(t("query.builder.bad_item", type=type(item).__name__), sub)

    def is_empty(self, item: Any) -> bool:
        if isinstance(item, Condition):
            return False
        return all(self.is_empty(sub) for sub in item.items)

    # -- rendering + AST -----------------------------------------------------------
    def group(self, group: Group, path: tuple[int, ...], top: bool) -> Expr:
        op = group.op.lower()
        live = [(i, item) for i, item in enumerate(group.items) if not self.is_empty(item)]
        start = self.length
        wrap = group.negate or not top
        if group.negate:
            self.emit("not (")
        elif wrap:
            self.emit("(")
        nodes: list[Expr] = []
        for k, (i, item) in enumerate(live):
            if k:
                self.emit(f" {op} ")
            if isinstance(item, Group):
                nodes.append(self.group(item, path + (i,), False))
            else:
                nodes.append(self.condition(item, path + (i,)))
        if wrap:
            self.emit(")")
        if len(nodes) == 1:
            node = nodes[0]
        else:
            node = (And if op == "and" else Or)(tuple(nodes), pos=nodes[0].pos)
        if group.negate:
            node = Not(node, pos=start)
        return node

    def literal(self, value: Any, item: tuple[int, ...]) -> Literal:
        _check_builder_value(value, item)
        try:
            text = codec.dumps(value).decode("utf-8")
        except (TypeError, ValueError):
            raise _builder_error(t("query.builder.bad_value", type=type(value).__name__), item) from None
        lit = Literal(codec.loads(text), text, pos=self.length)
        self.emit(text)
        return lit

    def field(self, cond: Condition, item: tuple[int, ...]) -> Expr:
        node, text = _builder_field(cond.field, item)
        if isinstance(node, PathExpr):
            node = PathExpr(node.pattern, pos=self.length)
        else:
            node = SpecialExpr(node.name, pos=self.length)  # type: ignore[attr-defined]
        self.emit(text)
        return node

    def condition(self, cond: Condition, item: tuple[int, ...]) -> Expr:
        op = cond.op
        start = self.length
        if op in ("exists", "not_exists"):
            name = "exists" if op == "exists" else "missing"
            self.emit(name + "(")
            field = self.field(cond, item)
            self.emit(")")
            return Call(name, (field,), pos=start)
        if op in _LEN_OPS:
            self.emit("len(")
            field = self.field(cond, item)
            self.emit(")")
            sym = _LEN_OPS[op]
            sym_pos = self.length + 1
            self.emit(f" {sym} ")
            return Compare(sym, Call("len", (field,), pos=start), self.literal(cond.value, item), pos=sym_pos)
        field = self.field(cond, item)
        word_pos = self.length + 1
        if op in ("is_null", "is_empty"):
            what = "null" if op == "is_null" else "empty"
            self.emit(" is " + what)
            return Is(field, what, False, pos=word_pos)
        if op == "between":
            self.emit(" between ")
            low = self.literal(cond.value, item)
            self.emit(" and ")
            high = self.literal(cond.value2, item)
            return Between(field, low, high, pos=word_pos)
        if op == "in":
            self.emit(" in ")
            return In(field, self.literal(cond.value, item), False, pos=word_pos)
        sym = _SIMPLE_OPS[op]
        self.emit(f" {sym} ")
        node = Compare(sym, field, self.literal(cond.value, item), pos=word_pos)
        if sym == "matches":
            self.regex_nodes.append(node)
        return node


# ==================================================================================
# expression text -> builder model (inverse of Group.to_text)
# ==================================================================================
@dataclass(frozen=True)
class _Paren(Expr):
    """A parenthesised sub-expression; produced only by :class:`_GroupingParser` (it keeps
    the parentheses that :class:`_Parser` drops, because they are builder groups)."""

    inner: Expr


class _GroupingParser(_Parser):
    """The expression parser, except that ``( expr )`` operands stay visible as
    :class:`_Paren` nodes."""

    def parse_operand(self) -> Expr:
        tok = self.peek()
        if tok.kind == "punct" and tok.value == "(":
            self.advance()
            inner = self.parse_or()
            close = self.peek()
            if not (close.kind == "punct" and close.value == ")"):
                raise self.err(t("query.syntax.expected_close_paren", token=self.desc(close)), close.pos)
            self.advance()
            return _Paren(inner, pos=tok.pos)
        return super().parse_operand()


class _NotRepresentable(Exception):
    """The expression (or a part of it) has no builder form."""


_CMP_TO_BUILDER = {
    "==": "eq", "!=": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge",
    "contains": "contains", "startswith": "startswith", "matches": "regex",
}
_LEN_TO_BUILDER = {sym: op for op, sym in _LEN_OPS.items()}


def _unparen(node: Expr) -> Expr:
    while isinstance(node, _Paren):
        node = node.inner
    return node


def _field_text(pattern: PathPattern) -> str:
    """Builder field (paths.py syntax) of a path pattern; ``$`` for the record itself."""
    return format_path(pattern.segments) or "$"


class _ExprToBuilder:
    """Turns an AST (optionally with :class:`_Paren` nodes) into a builder tree; raises
    :class:`_NotRepresentable` for anything the builder cannot express."""

    def top(self, node: Expr) -> Group:
        if isinstance(node, (And, Or)):
            return self.group_of(node)
        if isinstance(node, _Paren):
            return Group(items=[self.item(node)])     # "(...)" stays a sub-group
        item = self.item(node)
        if isinstance(item, Group) and item.negate:
            return item                               # "not (...)" = a negated top group
        return Group(items=[item])

    def group_of(self, node: Expr, negate: bool = False) -> Group:
        """The group whose rendered items are ``node`` (an and/or chain or one item)."""
        if isinstance(node, And):
            return Group("and", [self.item(x) for x in node.items], negate)
        if isinstance(node, Or):
            return Group("or", [self.item(x) for x in node.items], negate)
        return Group("and", [self.item(node)], negate)

    def item(self, node: Expr) -> Condition | Group:
        if isinstance(node, _Paren):
            return self.group_of(node.inner)
        if isinstance(node, Not):
            operand = node.operand
            inner = operand.inner if isinstance(operand, _Paren) else operand
            return self.group_of(inner, negate=True)
        if isinstance(node, (And, Or)):   # mixed precedence without parentheses
            return self.group_of(node)
        return self.condition(node)

    # -- conditions ------------------------------------------------------------------
    @staticmethod
    def field(node: Expr) -> str:
        node = _unparen(node)
        if isinstance(node, SpecialExpr) and node.name in ("key", "index"):
            return "$" + node.name
        if isinstance(node, PathExpr):
            return _field_text(node.pattern)
        raise _NotRepresentable

    @staticmethod
    def value(node: Expr) -> Any:
        node = _unparen(node)
        if isinstance(node, Literal):
            return node.value
        raise _NotRepresentable

    def condition(self, node: Expr) -> Condition | Group:
        if isinstance(node, Compare):
            left = _unparen(node.left)
            if isinstance(left, Call):
                if left.name != "len" or node.op not in _LEN_TO_BUILDER:
                    raise _NotRepresentable
                return Condition(self.field(left.args[0]), _LEN_TO_BUILDER[node.op], self.value(node.right))
            field = self.field(left)
            value = self.value(node.right)
            if node.op == "endswith":
                # no builder operator: the equivalent regex (re.search(escaped + r"\Z"))
                if not isinstance(value, str):
                    raise _NotRepresentable
                return Condition(field, "regex", re.escape(value) + r"\Z")
            op = _CMP_TO_BUILDER.get(node.op)
            if op is None:
                raise _NotRepresentable
            return Condition(field, op, value)
        if isinstance(node, Between):
            return Condition(self.field(node.operand), "between", self.value(node.low), self.value(node.high))
        if isinstance(node, In):
            cond = Condition(self.field(node.left), "in", self.value(node.right))
            return Group(items=[cond], negate=True) if node.negate else cond
        if isinstance(node, Is):
            field = self.field(node.operand)
            if node.what == "missing":
                return Condition(field, "exists" if node.negate else "not_exists")
            cond = Condition(field, "is_null" if node.what == "null" else "is_empty")
            return Group(items=[cond], negate=True) if node.negate else cond
        if isinstance(node, Call) and node.name in _BOOL_FUNCS and len(node.args) == 1:
            op = {"exists": "exists", "missing": "not_exists", "empty": "is_empty"}[node.name]
            return Condition(self.field(node.args[0]), op)
        raise _NotRepresentable   # bare operands, literals, other functions, ...


def builder_from_expr(expr: Expr) -> Group | None:
    """The builder tree of an expression AST, or ``None`` when the builder cannot express
    it (functions other than ``len``/``exists``/``missing``/``empty`` around the field,
    path-vs-path comparisons, literals on the left, bare truthy operands, list literals
    with paths, ``len(x) between ...``, ...). The literal ``true`` is the empty group.

    The result filters exactly like ``expr``. ``a endswith "x"`` becomes the regex
    ``x\\Z``, ``is missing`` / ``is not missing`` become ``not_exists`` / ``exists``;
    ``is not null`` / ``is not empty`` / ``not in`` become negated one-condition groups.
    An AST from :func:`parse_filter` has no record of parentheses, so the rendered text
    may group differently than the source text (same meaning); :func:`builder_from_text`
    keeps the parentheses.
    """
    if isinstance(expr, Literal) and expr.value is True:
        return Group()   # Group().to_text() == "true"
    try:
        group = _ExprToBuilder().top(expr)
    except _NotRepresentable:
        return None
    try:
        group.to_text()   # deeper than the builder allows, value nesting, ...
    except (JsonReshapeError, ValueError, TypeError, RecursionError):
        return None
    return group


def builder_from_text(text: str) -> Group | None:
    """The visual-builder tree of a filter expression (the inverse of
    :meth:`Group.to_text`), or ``None`` when the expression is valid but the builder
    cannot express it (see :func:`builder_from_expr`).

    Raises :class:`~jsonreshape.errors.QuerySyntaxError` for an invalid expression (the
    error of :func:`compile_filter`). For every builder tree ``g``,
    ``builder_from_text(g.to_text()).to_text() == g.to_text()``; for every representable
    text, ``compile_filter(builder_from_text(text).to_text())`` matches the same records
    as ``compile_filter(text)``. Parenthesised parts become sub-groups, ``not (...)``
    negated groups.
    """
    parse_filter(text)   # the regular errors (regex patterns included)
    try:
        node = _GroupingParser(text).parse()
    except QuerySyntaxError:   # e.g. a constant in parentheses inside {...}
        return None
    return builder_from_expr(node)
