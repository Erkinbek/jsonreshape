"""Pure-Python ``Scanner``: validating, resumable record indexer (spec §1.2).

The structural main loop below dispatches on the scanner state. Wherever a whole value
(or a run of elements/members) is available in the current chunk it is consumed with one
of the expanded fast-path regexes of :mod:`._py_patterns`; everything else (chunk
boundaries, values nested deeper than the fast patterns, the target path, the first
occurrence of a flag-setting construct, errors) goes through the exact token handlers of
:class:`._py_scanbase.ScanBase`. A fast path is only ever used where it cannot change the
result, so the output is identical for every chunking of the input.
"""

from __future__ import annotations

import sys
from array import array

from . import _py_patterns as P
from ._py_scanbase import (
    K_ARR,
    K_BRACE,
    K_OBJ,
    LABELS,
    N_TERMINAL,
    ST_AFTER_ARR,
    ST_AFTER_OBJ,
    ST_COLON,
    ST_DONE,
    ST_END,
    ST_KEY,
    ST_KEY_OR_END,
    ST_LITERAL,
    ST_NUMBER,
    ST_OPEN_BRACE,
    ST_SKIP_LINE,
    ST_STRING,
    ST_TOP,
    ST_VALUE,
    ST_VALUE_OR_END,
    _SS_EOF_LABEL,
    ScanBase,
    decode_key,
)
from .constants import (
    DEFAULT_MAX_DEPTH,
    ERR_TARGET_NOT_FOUND,
    ERR_TRAILING_DATA,
    ERR_UNEXPECTED_CHAR,
    ERR_UNEXPECTED_EOF,
    MODE_BRACE_LIST,
    MODE_CONTAINER,
    MODE_LINES,
    MODE_SINGLE,
    MODE_STREAM,
    MODES,
)

_BIG_ENDIAN = sys.byteorder == "big"


def _to_bytes(chunk: object) -> bytes:
    if type(chunk) is bytes:
        return chunk  # type: ignore[return-value]
    if isinstance(chunk, str):
        raise TypeError("Scanner.feed() requires bytes, not str")
    return bytes(chunk)  # bytearray / memoryview / buffer objects


def _number_ok(buf: bytes, e: int, end: int) -> bool:
    """A fast match ending at ``e`` is final unless it is a number that the following
    byte could still continue (chunk end, or ``.``/``e``/``E`` after it)."""
    c = buf[e - 1]
    if 0x30 <= c <= 0x39:
        if e >= end:
            return False
        n = buf[e]
        return n != 0x2E and n != 0x65 and n != 0x45
    return True


class Scanner(ScanBase):
    """Validating, resumable JSON record scanner (see docs/ARCHITECTURE.md §1.2)."""

    __slots__ = ()

    def __init__(self, mode: int, target_path: list[str | int] | tuple = (), base_offset: int = 0,
                 max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        if type(mode) is not int or mode not in MODES:
            raise ValueError(f"invalid scanner mode: {mode!r}")
        if target_path is None:
            target_path = ()
        if not isinstance(target_path, (list, tuple)):
            raise ValueError("target_path must be a list of str/int segments")
        target = tuple(target_path)
        for seg in target:
            if type(seg) is int:
                if seg < 0:
                    raise ValueError(f"negative index in target_path: {seg}")
            elif type(seg) is not str:
                raise ValueError(f"invalid target_path segment: {seg!r}")
        if target and mode != MODE_CONTAINER:
            raise ValueError("target_path is only supported in MODE_CONTAINER")
        if type(base_offset) is not int or base_offset < 0:
            raise ValueError(f"invalid base_offset: {base_offset!r}")
        if type(max_depth) is not int or max_depth < 1:
            raise ValueError(f"invalid max_depth: {max_depth!r}")
        self._mode = mode
        self._target = target
        self._tlen = len(target)
        self._base = base_offset
        self._max_depth = max_depth
        self._consumed = base_offset
        self._lines = 0
        self._line_start = base_offset
        self._flags = 0
        self._variant = P.variant(0, mode == MODE_LINES)
        self._error = None
        self._finished = False
        self._finish_called = False
        self._record_count = 0
        self._stack = bytearray()
        if mode == MODE_CONTAINER:
            self._state = ST_VALUE
            self._rec_depth = -1
        elif mode == MODE_SINGLE:
            self._state = ST_VALUE
            self._rec_depth = 0
        elif mode == MODE_BRACE_LIST:
            self._state = ST_OPEN_BRACE
            self._rec_depth = 1
        else:  # STREAM, LINES
            self._state = ST_TOP
            self._rec_depth = 0
        self._rec_start = -1
        self._pending = None
        self._out = None
        self._path_active = mode == MODE_CONTAINER
        self._opd = 0
        self._path_idx = [0] * (len(target) + 2)
        self._key_ok = False
        self._target_found = False
        self._target_start = -1
        self._container_span = None
        self._is_key = False
        self._ss = 0
        self._hex_left = 0
        self._u8_left = 0
        self._u8_lo = 0
        self._u8_hi = 0
        self._u8_lead = -1
        self._key_need = False
        self._key_buf = None
        cap = 0
        for seg in target:
            if type(seg) is str:
                cap = max(cap, len(seg.encode("utf-8", "surrogatepass")))
        self._key_cap = 6 * cap + 16
        self._ns = 0
        self._lit = b""
        self._lit_i = 0
        self._line_no = 0
        self._cur_line_start = base_offset
        self._broken = []
        self._broken_pending = None
        self._cbase = base_offset
        self._clines = 0
        self._cline_start = base_offset
        self._nofast = -1

    # -- public API ---------------------------------------------------------------------------
    @property
    def error(self) -> tuple[int, str, str, int, int] | None:
        return self._error

    @property
    def record_count(self) -> int:
        return self._record_count

    @property
    def consumed(self) -> int:
        return self._consumed

    @property
    def lines(self) -> int:
        return self._lines

    @property
    def flags(self) -> int:
        return self._flags

    @property
    def target_found(self) -> bool:
        return self._target_found

    @property
    def container_span(self) -> tuple[int, int] | None:
        return self._container_span

    @property
    def finished(self) -> bool:
        return self._finished

    def take_broken(self) -> list[tuple[int, int, int, int, str, str]]:
        """MODE_LINES: broken lines reported since the previous call."""
        broken = self._broken
        self._broken = []
        return broken

    def feed(self, chunk: bytes) -> bytes:
        """Scan the next chunk; returns the record spans completed by it (u64 LE pairs)."""
        buf = _to_bytes(chunk)
        if self._finish_called:
            raise ValueError("Scanner.feed() called after finish()")
        if self._state == ST_DONE or not buf:
            self._account(buf)
            return b""
        out = array("Q")
        self._out = out
        self._cbase = self._consumed
        self._clines = self._lines
        self._cline_start = self._line_start
        self._nofast = -1
        try:
            self._run(buf, 0, len(buf))
        finally:
            self._out = None
            self._account(buf)
        self._record_count += len(out) >> 1
        if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only in practice
            out.byteswap()
        return out.tobytes()

    def finish(self) -> bytes:
        """Signal end of input; returns the remaining spans and sets ``error`` if any."""
        if self._finish_called:
            return b""
        self._finish_called = True
        out = array("Q")
        self._out = out
        self._cbase = self._consumed
        self._clines = self._lines
        self._cline_start = self._line_start
        try:
            if self._state != ST_DONE:
                self._eof()
        finally:
            self._out = None
            self._finished = True
        self._record_count += len(out) >> 1
        if _BIG_ENDIAN:  # pragma: no cover
            out.byteswap()
        return out.tobytes()

    def __repr__(self) -> str:
        return (f"<Scanner mode={self._mode} consumed={self._consumed} records={self._record_count} "
                f"error={self._error!r}>")

    # -- internals --------------------------------------------------------------------------
    def _account(self, buf: bytes) -> None:
        nl = buf.count(b"\n")
        if nl:
            self._lines += nl
            self._line_start = self._consumed + buf.rfind(b"\n") + 1
        self._consumed += len(buf)

    def _eof(self) -> None:
        end = self._consumed
        st = self._state
        if st == ST_STRING:
            self._fail(None, end, ERR_UNEXPECTED_EOF, _SS_EOF_LABEL[self._ss])
        elif st == ST_NUMBER:
            if N_TERMINAL[self._ns]:
                self._value_end(end)
            else:
                self._fail(None, end, ERR_UNEXPECTED_EOF, "digit")
        elif st == ST_LITERAL:
            self._fail(None, end, ERR_UNEXPECTED_EOF, "literal")
        st = self._state
        if st == ST_DONE:
            return
        if self._mode == MODE_LINES:
            if st == ST_END:
                if self._pending is not None:
                    self._out.extend(self._pending)
                    self._pending = None
            elif st == ST_SKIP_LINE:
                self._close_broken(end)
            elif st != ST_TOP:
                self._fail(None, end, ERR_UNEXPECTED_EOF, LABELS[st])
                self._close_broken(end)
            return
        if st == ST_END:
            if self._mode == MODE_CONTAINER and not self._target_found:
                self._fail(None, end, ERR_TARGET_NOT_FOUND, "none")
            return
        if st == ST_TOP:  # MODE_STREAM between values
            return
        self._fail(None, end, ERR_UNEXPECTED_EOF, LABELS[st])

    def _run(self, buf: bytes, pos: int, end: int) -> None:
        stack = self._stack
        lines_mode = self._mode == MODE_LINES
        ws = P.ws_line_match if lines_mode else P.ws_all_match
        cbase = self._cbase
        maxd = self._max_depth
        kfast = P.FAST_DEPTH
        while pos < end:
            st = self._state
            if st >= ST_STRING:
                if st == ST_STRING:
                    pos = self._string(buf, pos, end)
                elif st == ST_NUMBER:
                    pos = self._number(buf, pos, end)
                elif st == ST_LITERAL:
                    pos = self._literal(buf, pos, end)
                elif st == ST_SKIP_LINE:
                    i = buf.find(b"\n", pos, end)
                    if i < 0:
                        return
                    self._close_broken(cbase + i)
                    self._newline(i)
                    pos = i + 1
                else:  # ST_DONE
                    return
                continue
            pos = ws(buf, pos, end).end()
            if pos >= end:
                return
            c = buf[pos]
            if c == 0x0A:  # only reachable in MODE_LINES: the line document ends here
                if st == ST_END:
                    if self._pending is not None:
                        self._out.extend(self._pending)
                elif st != ST_TOP:
                    self._fail(buf, cbase + pos, ERR_UNEXPECTED_EOF, LABELS[st])
                    self._close_broken(cbase + pos)
                self._newline(pos)
                pos += 1
                continue
            d = len(stack)
            if st <= ST_VALUE_OR_END or st == ST_TOP:
                # ---------------------------------------------------------- value position
                if st == ST_VALUE_OR_END:
                    kind = stack[-1]
                    if (c == 0x5D and kind == K_ARR) or (c == 0x7D and kind == K_BRACE):
                        self._pop(cbase + pos + 1)
                        pos += 1
                        continue
                on_path = False
                sensitive = False
                if self._path_active and d == self._opd:
                    sensitive = d == 0 or ((type(self._target[d - 1]) is int) == (stack[-1] == K_ARR))
                    if sensitive:
                        on_path = self._child_on_path()
                if not on_path and pos != self._nofast:
                    k = kfast if maxd - d >= kfast else maxd - d
                    var = self._variant
                    if d == self._rec_depth:
                        if st == ST_TOP:
                            if lines_mode:
                                npos = self._records_lines(buf, pos, end, var, k)
                            else:
                                npos = self._records_stream(buf, pos, end, var, k)
                        elif d == 0:  # MODE_SINGLE root
                            m = var.get("value", k)(buf, pos, end)
                            if m is not None and _number_ok(buf, m.end(), end):
                                e = m.end()
                                self._out.append(cbase + pos)
                                self._out.append(cbase + e)
                                self._state = ST_END
                                pos = e
                                continue
                            npos = pos
                        elif stack[-1] == K_OBJ:  # member value of the target object
                            m = var.get("value", k)(buf, pos, end)
                            if m is not None and _number_ok(buf, m.end(), end):
                                pos = m.end()
                                self._value_end(cbase + pos)
                                continue
                            npos = pos
                        else:
                            npos = self._records_seq(buf, pos, end, var, k, stack[-1])
                        if npos != pos:
                            pos = npos
                            self._nofast = ws(buf, pos, end).end()
                            continue
                    elif not sensitive and stack[-1] == K_ARR:
                        e = var.get("arr_skip", k)(buf, pos, end).end()
                        if e > pos:
                            if buf[e - 1] == 0x2C:
                                self._state = ST_VALUE
                                self._nofast = ws(buf, e, end).end()
                            else:
                                self._state = ST_AFTER_ARR
                            pos = e
                            continue
                    else:
                        m = var.get("value", k)(buf, pos, end)
                        if m is not None and _number_ok(buf, m.end(), end):
                            self._state = ST_AFTER_ARR if stack[-1] == K_ARR else ST_AFTER_OBJ
                            pos = m.end()
                            continue
                pos = self._start_value(buf, pos, on_path, LABELS[st])
                continue
            if st == ST_KEY or st == ST_KEY_OR_END:
                # ------------------------------------------------------------ key position
                if c == 0x7D and st == ST_KEY_OR_END:
                    self._pop(cbase + pos + 1)
                    pos += 1
                    continue
                if c != 0x22:
                    self._fail(buf, cbase + pos, ERR_UNEXPECTED_CHAR, LABELS[st])
                    continue
                var = self._variant
                if d == self._rec_depth:  # members of the target object are the records
                    if pos != self._nofast:
                        k = kfast if maxd - d >= kfast else maxd - d
                        npos = self._records_obj(buf, pos, end, var, k)
                        if npos != pos:
                            pos = npos
                            self._nofast = ws(buf, pos, end).end()
                            continue
                    self._rec_start = cbase + pos
                elif self._path_active and d == self._opd and type(self._target[d - 1]) is str:
                    m = var.string(buf, pos, end)
                    if m is not None:
                        e = m.end()
                        self._key_ok = decode_key(buf[pos + 1:e - 1]) == self._target[d - 1]
                        self._state = ST_COLON
                        pos = e
                        continue
                    pos = self._start_key(buf, pos)
                    continue
                elif pos != self._nofast:
                    k = kfast if maxd - d >= kfast else maxd - d
                    e = var.get("obj_skip", k)(buf, pos, end).end()
                    if e > pos:
                        if buf[e - 1] == 0x2C:
                            self._state = ST_KEY
                            self._nofast = ws(buf, e, end).end()
                        else:
                            self._state = ST_AFTER_OBJ
                        pos = e
                        continue
                # slow member: key and colon at once when possible, the value goes slow
                m = var.key_colon(buf, pos, end)
                if m is not None:
                    pos = ws(buf, m.end(), end).end()
                    self._nofast = pos
                    self._state = ST_VALUE
                    continue
                pos = self._start_key(buf, pos)
                continue
            if st == ST_COLON:
                if c == 0x3A:
                    self._state = ST_VALUE
                    pos += 1
                    continue
            elif st == ST_AFTER_ARR:
                if c == 0x2C:
                    self._state = ST_VALUE
                    pos += 1
                    continue
                if c == 0x5D:
                    self._pop(cbase + pos + 1)
                    pos += 1
                    continue
            elif st == ST_AFTER_OBJ:
                if c == 0x2C:
                    self._state = ST_KEY if stack[-1] == K_OBJ else ST_VALUE
                    pos += 1
                    continue
                if c == 0x7D:
                    self._pop(cbase + pos + 1)
                    pos += 1
                    continue
            elif st == ST_OPEN_BRACE:
                if c == 0x7B:
                    stack.append(K_BRACE)
                    self._state = ST_VALUE_OR_END
                    pos += 1
                    continue
            elif st == ST_END:
                self._fail(buf, cbase + pos, ERR_TRAILING_DATA, "end")
                continue
            self._fail(buf, cbase + pos, ERR_UNEXPECTED_CHAR, LABELS[st])

    # -- record fast loops ------------------------------------------------------------------
    def _records_seq(self, buf: bytes, pos: int, end: int, var: P.Variant, k: int, kind: int) -> int:
        """Elements of the target array / values of a brace list, one regex match each."""
        if kind == K_ARR:
            match = var.get("elem_arr", k)
            close = 0x5D
            after = ST_AFTER_ARR
        else:
            match = var.get("elem_brace", k)
            close = 0x7D
            after = ST_AFTER_OBJ
        ap = self._out.append
        cb = self._cbase
        while True:
            m = match(buf, pos, end)
            if m is None:
                return pos
            ap(cb + pos)
            ap(cb + m.end(1))
            e = m.end()
            if buf[e - 1] == close:
                self._state = after
                return e - 1
            self._state = ST_VALUE
            pos = e

    def _records_obj(self, buf: bytes, pos: int, end: int, var: P.Variant, k: int) -> int:
        """Members of the target object (``"key": value`` spans)."""
        match = var.get("member", k)
        ap = self._out.append
        cb = self._cbase
        while True:
            m = match(buf, pos, end)
            if m is None:
                return pos
            ap(cb + pos)
            ap(cb + m.end(1))
            e = m.end()
            if buf[e - 1] == 0x7D:
                self._state = ST_AFTER_OBJ
                return e - 1
            self._state = ST_KEY
            pos = e

    def _records_stream(self, buf: bytes, pos: int, end: int, var: P.Variant, k: int) -> int:
        """MODE_STREAM top-level values."""
        match = var.get("stream_val", k)
        ap = self._out.append
        cb = self._cbase
        while True:
            m = match(buf, pos, end)
            if m is None:
                return pos
            ve = m.end(1)
            if not _number_ok(buf, ve, end):
                return pos
            ap(cb + pos)
            ap(cb + ve)
            pos = m.end()

    def _records_lines(self, buf: bytes, pos: int, end: int, var: P.Variant, k: int) -> int:
        """MODE_LINES: whole lines holding one fast-matchable value (or nothing)."""
        match = var.get("line_val", k)
        ap = self._out.append
        cb = self._cbase
        n = 0
        while True:
            m = match(buf, pos, end)
            if m is None:
                break
            s = m.start(1)
            if s >= 0:
                ap(cb + s)
                ap(cb + m.end(1))
            pos = m.end()
            n += 1
        if n:
            self._line_no += n
            self._cur_line_start = cb + pos
        return pos
