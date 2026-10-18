"""Pure-Python ``Reformatter``: streaming whitespace normaliser (spec §1.4)."""

from __future__ import annotations

import re

# string content after the opening quote, up to (not including) the closing quote
_STR_BODY = re.compile(rb'(?:[^"\\]++|\\.)*+', re.DOTALL).match
# one token per match (whitespace between tokens is skipped by findall): a complete string,
# a scalar run (numbers, literals), a structural byte, or a string cut by the end of the
# window (matched only at the window end)
_TOKENS = re.compile(
    rb'"(?:[^"\\]++|\\.)*+"|[^ \t\n\r{}\[\],:"]++|[{}\[\],:]|"(?:[^"\\]++|\\.)*+\\?\Z',
    re.DOTALL,
).findall
_WINDOW = 1 << 18  # bytes tokenized per findall call (bounds the temporary token list)


def _check_indent(indent: object) -> bytes | None:
    if indent is None:
        return None
    if type(indent) is int:
        if 0 <= indent <= 16:
            return b" " * indent
        raise ValueError(f"indent must be between 0 and 16, got {indent}")
    if indent == "\t":
        return b"\t"
    raise ValueError(f"invalid indent: {indent!r} (None, 0..16 or '\\t')")


class Reformatter:
    """Re-emit JSON tokens unchanged with normalised whitespace (compact or indented).

    The input may be split anywhere (inside strings, escapes, numbers) and may be a
    fragment (e.g. a wrapper prefix); levels below zero are allowed for such fragments
    (indentation is ``max(0, base_level + level)`` units). Input that is not JSON is not
    validated: bytes other than whitespace, structure and strings are copied unchanged.
    """

    __slots__ = ("_unit", "_base", "_level", "_in_string", "_escape", "_pending", "_finished",
                 "_nls", "_commas")

    def __init__(self, indent: int | str | None = None, base_level: int = 0) -> None:
        self._unit = _check_indent(indent)
        if type(base_level) is not int or base_level < 0:
            raise ValueError(f"invalid base_level: {base_level!r}")
        self._base = base_level
        self._level = 0
        self._in_string = False
        self._escape = False
        self._pending = 0  # '{' or '[' waiting to know whether the container is empty
        self._finished = False
        self._nls: dict[int, bytes] = {}
        self._commas: dict[int, bytes] = {}

    def feed(self, chunk: bytes) -> bytes:
        if self._finished:
            raise ValueError("Reformatter.feed() called after finish()")
        if type(chunk) is not bytes:
            if isinstance(chunk, str):
                raise TypeError("Reformatter.feed() requires bytes, not str")
            chunk = bytes(chunk)
        out: list[bytes] = []
        n = len(chunk)
        pos = self._string_tail(chunk, 0, out)
        while pos < n:
            end = min(n, pos + _WINDOW)
            toks = _TOKENS(chunk, pos, end)
            if toks:
                if self._unit is None:
                    out.append(b"".join(toks))
                else:
                    self._indent_tokens(toks, out)
                last = toks[-1]
                if last[0] == 0x22:
                    self._check_cut_string(last)
            pos = self._string_tail(chunk, end, out)
        return b"".join(out)

    def finish(self) -> bytes:
        if self._finished:
            return b""
        self._finished = True
        if self._pending:
            p = self._pending
            self._pending = 0
            return bytes((p,))
        return b""

    # -- internals ---------------------------------------------------------------------
    def _nl(self, level: int) -> bytes:
        s = self._nls.get(level)
        if s is None:
            s = b"\n" + self._unit * max(0, self._base + level)
            self._nls[level] = s
        return s

    def _comma(self, level: int) -> bytes:
        s = self._commas.get(level)
        if s is None:
            s = b"," + self._nl(level)
            self._commas[level] = s
        return s

    def _check_cut_string(self, tok: bytes) -> None:
        """The last token of a window starts with a quote: is it cut by the window end?"""
        e = _STR_BODY(tok, 1).end()
        if e == len(tok) - 1 and tok[e] == 0x22:
            return  # complete string
        self._in_string = True
        self._escape = e < len(tok)  # a trailing lone backslash

    def _string_tail(self, buf: bytes, pos: int, out: list[bytes]) -> int:
        """Continue a string that started before ``pos`` (in an earlier chunk or window)."""
        if not self._in_string:
            return pos
        n = len(buf)
        start = pos
        if self._escape:
            if pos >= n:
                return pos
            pos += 1
            self._escape = False
        e = _STR_BODY(buf, pos).end()
        if e < n and buf[e] == 0x22:
            out.append(buf[start:e + 1])
            self._in_string = False
            return e + 1
        out.append(buf[start:n])  # the chunk ends inside the string
        if e < n:  # buf[e] is a trailing backslash
            self._escape = True
        return n

    def _indent_tokens(self, toks: list[bytes], out: list[bytes]) -> None:
        append = out.append
        level = self._level
        pending = self._pending
        nls = self._nls
        commas = self._commas
        for tok in toks:
            c = tok[0]
            if pending:
                if c == 0x7D or c == 0x5D:  # empty container: '{}' / '[]'
                    append(bytes((pending, c)))
                    pending = 0
                    level -= 1
                    continue
                append(bytes((pending,)))
                append(nls.get(level) or self._nl(level))
                pending = 0
            if c == 0x2C:
                append(commas.get(level) or self._comma(level))
            elif c == 0x3A:
                append(b": ")
            elif c == 0x7B or c == 0x5B:
                level += 1
                pending = c
            elif c == 0x7D or c == 0x5D:
                level -= 1
                append(nls.get(level) or self._nl(level))
                append(tok)
            else:
                append(tok)
        self._level = level
        self._pending = pending
