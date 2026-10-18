"""Compiled regular expressions used as fast paths by the pure-Python core.

The pure-Python :class:`~jsonreshape.core._py_scanner.Scanner` is a byte-level state
machine. Running it byte by byte in Python would be far too slow, so whole runs of
valid JSON are consumed with ``re`` matches. Python's ``re`` cannot recurse, therefore
the value pattern ``V_k`` is *expanded* textually: ``V_0`` matches scalars, ``V_k``
matches scalars and containers whose children match ``V_{k-1}``. A value nested deeper
than ``k`` levels simply fails to match and the caller falls back to the exact state
machine for its outer levels (which again uses the fast patterns for the inner ones).

Every pattern only uses possessive quantifiers / atomic groups and alternatives that
start with distinct bytes, so a failing match costs time linear in what it scanned.

String patterns depend on the scanner flags observed so far (``FLAG_ESCAPES``,
``FLAG_U_ESCAPES``, ``FLAG_NON_ASCII``): a fast path never consumes a construct whose
flag is not yet set. The exact state machine consumes the first such construct, sets the
flag and switches to the next pattern *variant*. This keeps ``Scanner.flags`` exact for
any prefix of the input without a separate pass.

Variants are keyed by ``(flags & 7, lines)``; ``lines=True`` variants use ``[ \\t\\r]`` as
whitespace so that no fast path ever crosses a line feed (MODE_LINES).
"""

from __future__ import annotations

import re
import threading
from typing import Callable

#: depth of the expanded value pattern (containers nested inside one fast match)
FAST_DEPTH = 6

FLAG_ESCAPES = 1
FLAG_U_ESCAPES = 2
FLAG_NON_ASCII = 4

#: a complete, valid UTF-8 multi-byte sequence (no overlongs, no surrogates, <= U+10FFFF)
UTF8_SEQ = (
    rb"[\xc2-\xdf][\x80-\xbf]"
    rb"|\xe0[\xa0-\xbf][\x80-\xbf]"
    rb"|[\xe1-\xec\xee\xef][\x80-\xbf]{2}"
    rb"|\xed[\x80-\x9f][\x80-\xbf]"
    rb"|\xf0[\x90-\xbf][\x80-\xbf]{2}"
    rb"|[\xf1-\xf3][\x80-\xbf]{3}"
    rb"|\xf4[\x80-\x8f][\x80-\xbf]{2}"
)

WS_ALL = rb"[ \t\n\r]*+"
WS_LINE = rb"[ \t\r]*+"
NUMBER = rb"-?+(?:0|[1-9][0-9]*+)(?:\.[0-9]++)?+(?:[eE][+-]?+[0-9]++)?+"
_PLAIN = rb'[^"\\\x00-\x1f\x80-\xff]'

#: whitespace runs (always valid matches, possibly empty)
ws_all_match = re.compile(WS_ALL).match
ws_line_match = re.compile(WS_LINE).match
#: digit runs (number state machine)
digits_match = re.compile(rb"[0-9]*+").match


def _variant_key(flags: int) -> int:
    f = flags & 7
    if f & FLAG_U_ESCAPES:  # \u escapes always come with FLAG_ESCAPES
        f |= FLAG_ESCAPES
    return f


def string_body(flags: int) -> bytes:
    """Pattern for the content of a string allowed by ``flags`` (possessive, may be empty)."""
    alts = [_PLAIN + b"++"]
    if flags & FLAG_ESCAPES:
        if flags & FLAG_U_ESCAPES:
            alts.append(rb'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')
        else:
            alts.append(rb'\\["\\/bfnrt]')
    if flags & FLAG_NON_ASCII:
        alts.append(UTF8_SEQ)
    if len(alts) == 1:
        return _PLAIN + b"*+"
    return b"(?:" + b"|".join(alts) + b")*+"


def string_pattern(flags: int) -> bytes:
    return b'"' + string_body(flags) + b'"'


_value_cache: dict[tuple[int, int, bool], bytes] = {}


def value_pattern(k: int, flags: int, lines: bool) -> bytes:
    """``V_k``: any JSON value with at most ``k`` nested container levels (atomic)."""
    key = (k, flags, lines)
    cached = _value_cache.get(key)
    if cached is not None:
        return cached
    s = string_pattern(flags)
    scalars = s + b"|" + NUMBER + b"|true|false|null"
    if k <= 0:
        pat = b"(?>" + scalars + b")"
    else:
        w = WS_LINE if lines else WS_ALL
        inner = value_pattern(k - 1, flags, lines)
        arr = (rb"\[" + w + b"(?:" + inner + w + b"(?:," + w + rb"(?!\])|(?=\]))" + b")*+" + rb"\]")
        obj = (rb"\{" + w + b"(?:" + s + w + b":" + w + inner + w + b"(?:," + w + rb'(?=")|(?=\}))'
               + b")*+" + rb"\}")
        pat = b"(?>" + scalars + b"|" + arr + b"|" + obj + b")"
    _value_cache[key] = pat
    return pat


def _build(name: str, k: int, flags: int, lines: bool) -> bytes:
    w = WS_LINE if lines else WS_ALL
    s = string_pattern(flags)
    v = value_pattern(k, flags, lines)
    if name == "value":  # one complete value
        return v
    if name == "elem_arr":  # record loop in a target array: value, then ',' + ws or ']'
        return b"(" + v + b")" + w + b"(?:," + w + rb"|\])"
    if name == "elem_brace":  # record loop in a brace list
        return b"(" + v + b")" + w + b"(?:," + w + rb"|\})"
    if name == "member":  # record loop in a target object: key ':' value, then ',' + ws or '}'
        return s + w + b":" + w + b"(" + v + b")" + w + b"(?:," + w + rb"|\})"
    if name == "stream_val":  # top-level value in MODE_STREAM (+ following whitespace)
        return b"(" + v + b")" + WS_ALL
    if name == "line_val":  # one whole JSON line (blank lines allowed)
        return WS_LINE + b"(?:(" + v + b")" + WS_LINE + rb")?\n"
    if name == "arr_skip":  # run of array elements; stops after ',' or before ']'
        return b"(?:" + w + v + w + rb"(?:,|(?=\]))" + b")*+"
    if name == "obj_skip":  # run of object members; stops after ',' or before '}'
        return b"(?:" + w + s + w + b":" + w + v + w + rb"(?:,|(?=\}))" + b")*+"
    raise ValueError(name)


Matcher = Callable[..., "re.Match[bytes] | None"]


class Variant:
    """Compiled fast-path matchers for one ``(flags, lines)`` combination (lazy)."""

    __slots__ = ("flags", "lines", "string", "key_colon", "str_run", "_cache", "_lock")

    def __init__(self, flags: int, lines: bool) -> None:
        self.flags = flags
        self.lines = lines
        w = WS_LINE if lines else WS_ALL
        self.string: Matcher = re.compile(string_pattern(flags)).match
        self.key_colon: Matcher = re.compile(string_pattern(flags) + w + b":").match
        self.str_run: Matcher = re.compile(string_body(flags)).match
        self._cache: dict[tuple[str, int], Matcher] = {}
        self._lock = threading.Lock()

    def get(self, name: str, k: int) -> Matcher:
        key = (name, k)
        m = self._cache.get(key)
        if m is None:
            with self._lock:
                m = self._cache.get(key)
                if m is None:
                    m = re.compile(_build(name, k, self.flags, self.lines)).match
                    self._cache[key] = m
        return m


_variants: dict[tuple[int, bool], Variant] = {}
_variants_lock = threading.Lock()


def variant(flags: int, lines: bool) -> Variant:
    """The (cached) :class:`Variant` for the given scanner flags."""
    key = (_variant_key(flags), bool(lines))
    v = _variants.get(key)
    if v is None:
        with _variants_lock:
            v = _variants.get(key)
            if v is None:
                v = Variant(key[0], key[1])
                _variants[key] = v
    return v
