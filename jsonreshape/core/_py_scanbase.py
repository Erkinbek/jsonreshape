"""Exact (slow-path) state machine of the pure-Python Scanner.

:class:`ScanBase` holds the complete scanner state and the token-level handlers that work
byte by byte (with small regex runs) and can be suspended at *any* byte: strings
(escapes, ``\\uXXXX``, UTF-8 sequences), numbers, literals, record bookkeeping, target
path tracking, errors and MODE_LINES broken-line reporting. The structural main loop and
the fast paths live in :mod:`jsonreshape.core._py_scanner`.
"""

from __future__ import annotations

import json
from array import array

from . import _py_patterns as P
from .constants import (
    ERR_CONTROL_CHAR,
    ERR_INVALID_ESCAPE,
    ERR_INVALID_HEX,
    ERR_INVALID_LITERAL,
    ERR_INVALID_NUMBER,
    ERR_INVALID_UTF8,
    ERR_TARGET_NOT_CONTAINER,
    ERR_DEPTH_LIMIT,
    ERR_UNEXPECTED_CHAR,
    ERR_UNEXPECTED_EOF,
    FLAG_ESCAPES,
    FLAG_NON_ASCII,
    FLAG_U_ESCAPES,
    MODE_CONTAINER,
    MODE_LINES,
    MODE_STREAM,
)

# -- structural states (between tokens) -------------------------------------------------
ST_VALUE = 0          # a value is required (after ':' / ',' in arrays / root)
ST_VALUE_OR_END = 1   # after '[' (or the '{' of a brace list)
ST_KEY_OR_END = 2     # after '{'
ST_KEY = 3            # after ',' in an object
ST_COLON = 4          # after a key
ST_AFTER_ARR = 5      # after an array element
ST_AFTER_OBJ = 6      # after an object member / brace-list value
ST_OPEN_BRACE = 7     # MODE_BRACE_LIST: root '{' required
ST_END = 8            # after the root value (or the value of a JSON line)
ST_TOP = 9            # MODE_STREAM top level / MODE_LINES line start: value or end
# -- token states ------------------------------------------------------------------------
ST_STRING = 10
ST_NUMBER = 11
ST_LITERAL = 12
ST_SKIP_LINE = 13     # MODE_LINES: skipping the rest of a broken line
ST_DONE = 14          # stopped by an error (non-LINES modes)

#: ``expected`` label reported for an error in each structural state
LABELS = (
    "value",
    "value_or_end",
    "key_or_end",
    "key",
    "colon",
    "comma_or_bracket",
    "comma_or_brace",
    "open_brace",
    "end",
    "value",
)

# container kinds on the stack
K_ARR = 1
K_OBJ = 2
K_BRACE = 3  # root of a MODE_BRACE_LIST document

# string sub-states
SS_NORMAL = 0
SS_ESCAPE = 1  # after a backslash
SS_HEX = 2     # inside \uXXXX (``_hex_left`` digits missing)
SS_UTF8 = 3    # inside a multi-byte UTF-8 sequence
_SS_EOF_LABEL = ("string_end", "escape", "hex", "string_end")

# number sub-states
N_MINUS = 0
N_ZERO = 1
N_INT = 2
N_DOT = 3
N_FRAC = 4
N_EXP = 5
N_EXP_SIGN = 6
N_EXP_DIGITS = 7
N_TERMINAL = (False, True, True, False, True, False, False, True)

_ESCAPE_CHARS = frozenset(b'"\\/bfnrt')
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")
_LITERALS = {0x74: b"true", 0x66: b"false", 0x6E: b"null"}

# UTF-8 lead byte -> (continuation bytes, allowed range of the first continuation byte)
_U8: list[tuple[int, int, int] | None] = [None] * 256
for _c in range(0xC2, 0xE0):
    _U8[_c] = (1, 0x80, 0xBF)
_U8[0xE0] = (2, 0xA0, 0xBF)
for _c in range(0xE1, 0xED):
    _U8[_c] = (2, 0x80, 0xBF)
_U8[0xED] = (2, 0x80, 0x9F)
_U8[0xEE] = (2, 0x80, 0xBF)
_U8[0xEF] = (2, 0x80, 0xBF)
_U8[0xF0] = (3, 0x90, 0xBF)
for _c in (0xF1, 0xF2, 0xF3):
    _U8[_c] = (3, 0x80, 0xBF)
_U8[0xF4] = (3, 0x80, 0x8F)
del _c


def decode_key(raw: bytes) -> str:
    """Decoded text of validated string content ``raw`` (Python json semantics)."""
    if b"\\" not in raw:
        return raw.decode("utf-8")
    return json.loads(b'"' + raw + b'"')


class ScanBase:
    """Scanner state + exact token-level handlers (see module docstring)."""

    __slots__ = (
        "_mode", "_target", "_tlen", "_base", "_max_depth",
        "_consumed", "_lines", "_line_start", "_flags", "_variant",
        "_error", "_finished", "_finish_called", "_record_count",
        "_stack", "_state", "_rec_depth", "_rec_start", "_pending", "_out",
        "_path_active", "_opd", "_path_idx", "_key_ok", "_target_found",
        "_target_start", "_container_span",
        "_is_key", "_ss", "_hex_left", "_u8_left", "_u8_lo", "_u8_hi", "_u8_lead",
        "_key_need", "_key_buf", "_key_cap", "_ns", "_lit", "_lit_i",
        "_line_no", "_cur_line_start", "_broken", "_broken_pending",
        "_cbase", "_clines", "_cline_start", "_nofast",
    )

    _mode: int
    _stack: bytearray
    _out: array | None

    # -- errors, lines ------------------------------------------------------------------
    def _line_info(self, off: int, buf: bytes | None) -> tuple[int, int]:
        """(line index, line start) of absolute offset ``off`` (within the current chunk,
        or before it when no line feed can lie in between)."""
        rel = off - self._cbase
        if buf is not None and rel > 0:
            if rel > len(buf):
                rel = len(buf)
            n = buf.count(b"\n", 0, rel)
            if n:
                return self._clines + n, self._cbase + buf.rfind(b"\n", 0, rel) + 1
        return self._clines, self._cline_start

    def _fail(self, buf: bytes | None, off: int, code: str, expected: str) -> None:
        """Error at absolute offset ``off``: stop (non-LINES) or break the current line."""
        if self._mode == MODE_LINES:
            if self._broken_pending is None:
                self._broken_pending = (self._line_no, self._cur_line_start, off, code, expected)
            del self._stack[:]
            self._rec_start = -1
            self._pending = None
            self._state = ST_SKIP_LINE
            return
        line, line_start = self._line_info(off, buf)
        self._error = (off, code, expected, line, line_start)
        self._state = ST_DONE
        self._finished = True

    def _close_broken(self, line_end: int) -> None:
        pending = self._broken_pending
        if pending is not None:
            line_no, line_start, off, code, expected = pending
            self._broken.append((line_no, line_start, line_end, off, code, expected))
            self._broken_pending = None

    def _newline(self, rel: int) -> None:
        """MODE_LINES: the line feed at buffer position ``rel`` ends the current line."""
        self._line_no += 1
        self._cur_line_start = self._cbase + rel + 1
        self._state = ST_TOP
        self._rec_start = -1
        self._pending = None

    def _set_flags(self, bits: int) -> None:
        flags = self._flags | bits
        if flags != self._flags:
            self._flags = flags
            self._variant = P.variant(flags, self._mode == MODE_LINES)

    # -- values ----------------------------------------------------------------------------
    def _value_end(self, end: int) -> None:
        """A value ended at absolute offset ``end``: emit records, go to the after-value state."""
        stack = self._stack
        d = len(stack)
        if d == self._rec_depth and self._rec_start >= 0:
            if self._mode == MODE_LINES:
                self._pending = (self._rec_start, end)
            else:
                out = self._out
                out.append(self._rec_start)
                out.append(end)
            self._rec_start = -1
        if d == 0:
            self._state = ST_TOP if self._mode == MODE_STREAM else ST_END
        elif stack[-1] == K_ARR:
            self._state = ST_AFTER_ARR
        else:
            self._state = ST_AFTER_OBJ

    def _pop(self, end: int) -> None:
        """Close the innermost container; ``end`` = absolute offset after the bracket."""
        stack = self._stack
        d = len(stack)
        stack.pop()
        if d == self._rec_depth:
            if self._mode == MODE_CONTAINER:
                self._container_span = (self._target_start, end)
            self._rec_depth = -1
        elif self._path_active and d == self._opd:
            self._opd = d - 1
        self._value_end(end)

    def _child_on_path(self) -> bool:
        """MODE_CONTAINER: is the value starting now a child on the target path?
        (Counts array elements; call exactly once per child of an on-path container.)"""
        stack = self._stack
        d = len(stack)
        if d == 0:
            return True
        seg = self._target[d - 1]
        if stack[-1] == K_ARR:
            i = self._path_idx[d]
            self._path_idx[d] = i + 1
            return type(seg) is int and i == seg
        return type(seg) is str and self._key_ok

    def _start_value(self, buf: bytes, pos: int, on_path: bool, label: str) -> int:
        """First byte of a value at ``buf[pos]`` (slow path). Returns the next position."""
        c = buf[pos]
        off = self._cbase + pos
        stack = self._stack
        d = len(stack)
        if c == 0x7B or c == 0x5B:  # '{' '['
            if d >= self._max_depth:
                self._fail(buf, off, ERR_DEPTH_LIMIT, "none")
                return pos
            if d == self._rec_depth and self._rec_start < 0:
                self._rec_start = off
            if on_path:
                if d == self._tlen:
                    self._target_found = True
                    self._target_start = off
                    self._path_active = False
                    self._rec_depth = d + 1
                else:
                    self._opd = d + 1
                    self._path_idx[d + 1] = 0
                    self._key_ok = False
            if c == 0x7B:
                stack.append(K_OBJ)
                self._state = ST_KEY_OR_END
            else:
                stack.append(K_ARR)
                self._state = ST_VALUE_OR_END
            return pos + 1
        if on_path and d == self._tlen and (c == 0x22 or c == 0x2D or 0x30 <= c <= 0x39 or c in _LITERALS):
            # a scalar starts at the full target path (bytes that cannot start a value
            # at all are reported as unexpected_char below)
            self._fail(buf, off, ERR_TARGET_NOT_CONTAINER, "none")
            return pos
        if c == 0x22:  # '"'
            self._is_key = False
            self._key_need = False
            self._ss = SS_NORMAL
            self._state = ST_STRING
        elif c == 0x2D or 0x30 <= c <= 0x39:
            self._ns = N_MINUS if c == 0x2D else (N_ZERO if c == 0x30 else N_INT)
            self._state = ST_NUMBER
        elif c in _LITERALS:
            self._lit = _LITERALS[c]
            self._lit_i = 1
            self._state = ST_LITERAL
        else:
            self._fail(buf, off, ERR_UNEXPECTED_CHAR, label)
            return pos
        if d == self._rec_depth and self._rec_start < 0:
            self._rec_start = off
        return pos + 1

    def _start_key(self, buf: bytes, pos: int) -> int:
        """Opening quote of an object key at ``buf[pos]`` (slow path)."""
        d = len(self._stack)
        self._is_key = True
        self._ss = SS_NORMAL
        if self._path_active and d == self._opd and type(self._target[d - 1]) is str:
            self._key_need = True
            self._key_buf = bytearray()
            self._key_ok = False
        else:
            self._key_need = False
        self._state = ST_STRING
        return pos + 1

    def _collect(self, buf: bytes, a: int, b: int) -> None:
        kb = self._key_buf
        if kb is not None:
            if len(kb) + (b - a) > self._key_cap:
                self._key_buf = None  # longer than the target key: cannot match
            else:
                kb += buf[a:b]

    def _string_done(self, end: int) -> None:
        if self._is_key:
            if self._key_need:
                kb = self._key_buf
                self._key_ok = kb is not None and decode_key(bytes(kb)) == self._target[len(self._stack) - 1]
                self._key_need = False
                self._key_buf = None
            self._state = ST_COLON
        else:
            self._value_end(end)

    # -- token handlers (resumable at any byte) -------------------------------------------
    def _string(self, buf: bytes, pos: int, end: int) -> int:
        lines_mode = self._mode == MODE_LINES
        cbase = self._cbase
        while pos < end:
            ss = self._ss
            if ss == SS_NORMAL:
                p = self._variant.str_run(buf, pos, end).end()
                if self._key_need and p > pos:
                    self._collect(buf, pos, p)
                pos = p
                if pos >= end:
                    break
                c = buf[pos]
                if c == 0x22:
                    pos += 1
                    self._string_done(cbase + pos)
                    return pos
                if c == 0x5C:
                    if self._key_need:
                        self._collect(buf, pos, pos + 1)
                    self._ss = SS_ESCAPE
                    pos += 1
                    continue
                if c >= 0x80:
                    info = _U8[c]
                    if info is None:
                        self._fail(buf, cbase + pos, ERR_INVALID_UTF8, "string_end")
                        return pos
                    self._u8_left, self._u8_lo, self._u8_hi = info
                    self._u8_lead = cbase + pos
                    if self._key_need:
                        self._collect(buf, pos, pos + 1)
                    self._ss = SS_UTF8
                    pos += 1
                    continue
                if lines_mode and c == 0x0A:
                    self._fail(buf, cbase + pos, ERR_UNEXPECTED_EOF, "string_end")
                else:
                    self._fail(buf, cbase + pos, ERR_CONTROL_CHAR, "string_end")
                return pos
            c = buf[pos]
            if ss == SS_ESCAPE:
                if c in _ESCAPE_CHARS:
                    self._ss = SS_NORMAL
                    self._set_flags(FLAG_ESCAPES)
                elif c == 0x75:  # 'u'
                    self._ss = SS_HEX
                    self._hex_left = 4
                else:
                    if lines_mode and c == 0x0A:
                        self._fail(buf, cbase + pos, ERR_UNEXPECTED_EOF, "escape")
                    else:
                        self._fail(buf, cbase + pos, ERR_INVALID_ESCAPE, "escape")
                    return pos
            elif ss == SS_HEX:
                if c in _HEX_DIGITS:
                    self._hex_left -= 1
                    if self._hex_left == 0:
                        self._ss = SS_NORMAL
                        self._set_flags(FLAG_ESCAPES | FLAG_U_ESCAPES)
                else:
                    if lines_mode and c == 0x0A:
                        self._fail(buf, cbase + pos, ERR_UNEXPECTED_EOF, "hex")
                    else:
                        self._fail(buf, cbase + pos, ERR_INVALID_HEX, "hex")
                    return pos
            else:  # SS_UTF8
                if self._u8_lo <= c <= self._u8_hi:
                    self._u8_left -= 1
                    if self._u8_left == 0:
                        self._ss = SS_NORMAL
                        self._set_flags(FLAG_NON_ASCII)
                    else:
                        self._u8_lo = 0x80
                        self._u8_hi = 0xBF
                else:
                    if lines_mode and c == 0x0A:
                        self._fail(buf, cbase + pos, ERR_UNEXPECTED_EOF, "string_end")
                    else:
                        self._fail(buf, self._u8_lead, ERR_INVALID_UTF8, "string_end")
                    return pos
            if self._key_need:
                self._collect(buf, pos, pos + 1)
            pos += 1
        return pos

    def _number(self, buf: bytes, pos: int, end: int) -> int:
        ns = self._ns
        digits = P.digits_match
        while pos < end:
            if ns == N_INT or ns == N_FRAC or ns == N_EXP_DIGITS:
                pos = digits(buf, pos, end).end()
                if pos >= end:
                    break
                c = buf[pos]
                if ns != N_EXP_DIGITS:
                    if c == 0x2E and ns == N_INT:
                        ns = N_DOT
                        pos += 1
                        continue
                    if c == 0x65 or c == 0x45:
                        ns = N_EXP
                        pos += 1
                        continue
                self._ns = ns
                self._value_end(self._cbase + pos)
                return pos
            c = buf[pos]
            if ns == N_ZERO:
                if c == 0x2E:
                    ns = N_DOT
                    pos += 1
                    continue
                if c == 0x65 or c == 0x45:
                    ns = N_EXP
                    pos += 1
                    continue
                self._ns = ns
                self._value_end(self._cbase + pos)
                return pos
            # N_MINUS, N_DOT, N_EXP, N_EXP_SIGN: a digit is required (or a sign after 'e')
            if 0x30 <= c <= 0x39:
                if ns == N_MINUS:
                    ns = N_ZERO if c == 0x30 else N_INT
                elif ns == N_DOT:
                    ns = N_FRAC
                else:
                    ns = N_EXP_DIGITS
                pos += 1
                continue
            if ns == N_EXP and (c == 0x2B or c == 0x2D):
                ns = N_EXP_SIGN
                pos += 1
                continue
            self._ns = ns
            if self._mode == MODE_LINES and c == 0x0A:
                self._fail(buf, self._cbase + pos, ERR_UNEXPECTED_EOF, "digit")
            else:
                self._fail(buf, self._cbase + pos, ERR_INVALID_NUMBER, "digit")
            return pos
        self._ns = ns
        return pos

    def _literal(self, buf: bytes, pos: int, end: int) -> int:
        lit = self._lit
        i = self._lit_i
        n = len(lit)
        while pos < end:
            c = buf[pos]
            if c != lit[i]:
                if self._mode == MODE_LINES and c == 0x0A:
                    self._fail(buf, self._cbase + pos, ERR_UNEXPECTED_EOF, "literal")
                else:
                    self._fail(buf, self._cbase + pos, ERR_INVALID_LITERAL, "literal")
                return pos
            pos += 1
            i += 1
            if i == n:
                self._value_end(self._cbase + pos)
                return pos
        self._lit_i = i
        return pos
