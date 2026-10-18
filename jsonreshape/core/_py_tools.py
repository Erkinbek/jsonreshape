"""Pure-Python ``numbers_plain``, ``MultiFinder`` and ``PathExtractor`` (spec §1.3, §1.5, §1.6)."""

from __future__ import annotations

import re
import sys
from array import array
from bisect import bisect_right
from typing import Iterator

from . import _py_patterns as P
from ._py_scanbase import decode_key
from .constants import MODE_SINGLE, SEG_ANY_INDEX, SEG_ANY_KEY, SEG_INDEX, SEG_KEY

_BIG_ENDIAN = sys.byteorder == "big"


def _as_bytes(data: object, what: str) -> bytes:
    if type(data) is bytes:
        return data  # type: ignore[return-value]
    if isinstance(data, str):
        raise TypeError(f"{what} must be bytes, not str")
    return bytes(data)  # bytearray / memoryview


def _u64_array(spans: object, group: int, what: str) -> array:
    raw = _as_bytes(spans, what)
    if len(raw) % (8 * group):
        raise ValueError(f"{what}: length {len(raw)} is not a multiple of {8 * group}")
    vals = array("Q")
    vals.frombytes(raw)
    if _BIG_ENDIAN:  # pragma: no cover
        vals.byteswap()
    return vals


def _to_le_bytes(vals: array) -> bytes:
    if _BIG_ENDIAN:  # pragma: no cover
        vals.byteswap()
    return vals.tobytes()


# =========================================================================================
# numbers_plain
# =========================================================================================
_NP_NUMBER = (
    rb"-?+(?:"
    rb"0\.0"                                                  # 0.0, -0.0
    rb"|[1-9](?:[0-9]{0,14}|[0-9]{14}0)\.0"                   # I.0: <= 16 digits, <= 15 significant
    rb"|(?=[0-9.]{3,16}(?![0-9.]))[1-9][0-9]*+\.[0-9]*[1-9]"  # I.F: <= 15 digits, F not ending in 0
    rb"|0\.0{0,4}[1-9](?:[0-9]{0,13}[1-9])?"                  # 0.F: <= 4 leading zeros, <= 15 digits
    rb"|(?<!-)0"                                              # 0 (but not -0)
    rb"|[1-9][0-9]{0,17}"                                     # integers up to 18 digits
    rb")(?![0-9.eE])"
)
_NP_ALL = re.compile(
    rb'(?:"(?:[^"\\]++|\\.)*+"|[^"\-0-9]++|' + _NP_NUMBER + rb")*+",
    re.DOTALL,
)


def numbers_plain(buf: bytes) -> bool:
    """True iff every number token outside strings in the (valid JSON) ``buf`` is *plain*
    (orjson parses and re-serialises it unchanged; see codec.is_plain_*_token)."""
    data = _as_bytes(buf, "buf")
    return _NP_ALL.match(data).end() == len(data)


# =========================================================================================
# MultiFinder
# =========================================================================================
class MultiFinder:
    """Search for several byte literals at once (pure byte semantics)."""

    __slots__ = ("_needles", "_search", "_occ")

    def __init__(self, needles: list[bytes]) -> None:
        if isinstance(needles, (bytes, bytearray, str)) or not hasattr(needles, "__iter__"):
            raise ValueError("needles must be a list of bytes")
        items = []
        for n in needles:
            if isinstance(n, str):
                raise TypeError("needles must be bytes, not str")
            b = bytes(n)
            if not b:
                raise ValueError("empty needle")
            items.append(b)
        if not items:
            raise ValueError("at least one needle is required")
        self._needles = tuple(items)
        # longest first: purely cosmetic (any alternative matching at a position is found)
        ordered = sorted(set(items), key=len, reverse=True)
        alternatives = b"|".join(re.escape(n) for n in ordered)
        self._search = re.compile(alternatives).search
        # leftmost position where some needle starts and ends before ``endpos`` (the
        # lookahead consumes nothing, so the needles at one position are all tried)
        self._occ = re.compile(b"(?=(?:" + alternatives + b"))").search

    @property
    def needles(self) -> tuple[bytes, ...]:
        return self._needles

    def find_first(self, buf: bytes, start: int = 0, end: int | None = None) -> int:
        """Smallest ``p`` in ``[start, end)`` where a needle occurs ending ``<= end``; ``-1`` if none."""
        data = _as_bytes(buf, "buf")
        n = len(data)
        if end is None:
            end = n
        if type(start) is not int or type(end) is not int:
            raise ValueError("start/end must be integers")
        if start < 0 or end < 0 or end > n:
            raise ValueError(f"invalid range [{start}, {end}) for a buffer of {n} bytes")
        if start >= end:
            return -1
        m = self._search(data, start, end)
        return -1 if m is None else m.start()

    def find_in_spans(self, buf: bytes, spans: bytes) -> bytes:
        """Indices (u64 LE) of the spans containing an occurrence of any needle."""
        data = _as_bytes(buf, "buf")
        vals = _u64_array(spans, 2, "spans")
        n = len(data)
        count = len(vals) // 2
        starts = vals[0::2]
        ends = vals[1::2]
        for i in range(count):
            if starts[i] > ends[i] or ends[i] > n:
                raise ValueError(f"span {i} ({starts[i]}, {ends[i]}) out of bounds for {n} bytes")
        search = self._search
        out = array("Q")
        i = 0
        while i < count:
            m = search(data, starts[i])
            if m is None:
                break
            p = m.start()
            # spans ending at or before p cannot contain an occurrence (p is the first one)
            j = bisect_right(ends, p, i)
            if j >= count:
                break
            if starts[j] > p:  # p lies in a gap: continue from span j
                i = j
                continue
            if search(data, p, ends[j]) is not None:
                out.append(j)
            i = j + 1
        return _to_le_bytes(out)

    def locate_tokens(self, buf: bytes, spans: bytes, member_spans: bool = False) -> list[tuple]:
        """Located tokens ``(span_idx, kind, token_start, token_end, path)`` (spec §1.5):
        every key/scalar token of every span in which a needle occurrence lying entirely
        inside the span starts, once, in document order; after the tokens of a span with
        duplicate keys anywhere, ``(span_idx, 5, 0, 0, ())``. Every span must hold one
        valid JSON value (an object member with ``member_spans``), else ``ValueError``."""
        data = _as_bytes(buf, "buf")
        vals = _u64_array(spans, 2, "spans")
        n = len(data)
        count = len(vals) // 2
        for i in range(count):
            if vals[2 * i] > vals[2 * i + 1] or vals[2 * i + 1] > n:
                raise ValueError(f"span {i} ({vals[2 * i]}, {vals[2 * i + 1]}) out of bounds for {n} bytes")
        member = bool(member_spans)
        occ = self._occ
        out: list[tuple] = []
        for i in range(count):
            s = vals[2 * i]
            e = vals[2 * i + 1]
            _root_span(data, s, e, member)  # validation (ValueError)
            m = occ(data, s, e)
            if m is not None:
                _locate_span(data, s, e, member, i, m.start(), occ, out)
        return out


# kinds of MultiFinder.locate_tokens
KIND_STRING, KIND_KEY, KIND_NUMBER, KIND_LITERAL, KIND_MEMBER_KEY, KIND_CONFIRM = range(6)

# the tokens of validated JSON: strings, numbers, literals, structure (whitespace is skipped)
_TOKEN = re.compile(rb'"(?:[^"\\]++|\\.)*+"|[-0-9][-+.0-9eE]*+|true|false|null|[\[\]{},:]', re.DOTALL)
_LITERAL_START = frozenset(b"tfn")


def _locate_span(data: bytes, s: int, e: int, member: bool, si: int, occ: int, search, out: list) -> None:
    """Append the located tokens of the validated span ``data[s:e]`` to ``out``.

    ``occ`` is the leftmost occurrence (fitting in the span) at or after the end of the
    last token examined (``e`` = none). A token ``[ts, te)`` is located when
    ``ts <= occ < te``; an ``occ`` before ``ts`` started on whitespace or structure and is
    ignored. After a located token the search resumes at ``te``. Duplicate keys are
    detected per object on the decoded keys."""
    first = len(out)
    dup = False
    stack: list[list] = []   # [is_object, current key | index, keys seen | element count, expecting a key]
    member_key = member
    for m in _TOKEN.finditer(data, s, e):
        ts = m.start()
        c = data[ts]
        if c == 0x2C:  # ,
            top = stack[-1]
            if top[0]:
                top[3] = True
            continue
        if c == 0x3A:  # :
            continue
        if c == 0x7D or c == 0x5D:  # } ]
            stack.pop()
            continue
        kind = -1
        if member_key:
            member_key = False
            kind = KIND_MEMBER_KEY
        elif stack:
            top = stack[-1]
            if top[0]:
                if top[3]:
                    top[3] = False
                    key = decode_key(data[ts + 1:m.end() - 1])
                    top[1] = key
                    if not dup:
                        if key in top[2]:
                            dup = True
                        else:
                            top[2].add(key)
                    kind = KIND_KEY
            else:
                top[1] = top[2]
                top[2] += 1
        if c == 0x7B:  # {
            stack.append([True, None, set(), True])
            continue
        if c == 0x5B:  # [
            stack.append([False, None, 0, False])
            continue
        if kind < 0:
            kind = KIND_STRING if c == 0x22 else KIND_LITERAL if c in _LITERAL_START else KIND_NUMBER
        te = m.end()
        if occ < te:
            if occ < ts:
                mm = search(data, ts, e)
                occ = mm.start() if mm is not None else e
            if occ < te:
                path = () if kind == KIND_MEMBER_KEY else tuple([f[1] for f in stack])
                out.append((si, kind, ts, te, path))
                mm = search(data, te, e)
                occ = mm.start() if mm is not None else e
    if dup and len(out) > first:
        out.append((si, KIND_CONFIRM, 0, 0, ()))


# =========================================================================================
# PathExtractor
# =========================================================================================
_CANON_INDEX = re.compile(rb"0|[1-9][0-9]*")
_WS = P.ws_all_match
_DEEP_TOKEN = re.compile(rb'["\[\]{}]').search
_LOOSE_STRING = re.compile(rb'"(?:[^"\\]++|\\.)*+"', re.DOTALL).match

_compiled: dict[str, object] = {}


def _pats():
    """Validating patterns with every construct allowed (compiled once, lazily)."""
    if not _compiled:
        body = P.string_body(7)
        v = P.value_pattern(P.FAST_DEPTH, 7, False)
        w = P.WS_ALL
        _compiled["value"] = re.compile(v).match
        _compiled["member_head"] = re.compile(w + b'"(' + body + b')"' + w + b":" + w).match
        _compiled["member"] = re.compile(
            w + b'"(' + body + b')"' + w + b":" + w + b"(" + v + b")" + w + rb"([,}])").match
        _compiled["elem"] = re.compile(w + b"(" + v + b")" + w + rb"([,\]])").match
    return _compiled


def _skip_deep(buf: bytes, pos: int) -> int:
    """End of the (validated) container starting at ``buf[pos]``."""
    depth = 0
    while True:
        m = _DEEP_TOKEN(buf, pos)
        if m is None:  # pragma: no cover - cannot happen for validated input
            raise ValueError("unterminated container")
        q = m.start()
        c = buf[q]
        if c == 0x22:
            pos = _LOOSE_STRING(buf, q).end()
            continue
        if c == 0x5B or c == 0x7B:
            depth += 1
        else:
            depth -= 1
        pos = q + 1
        if depth == 0:
            return pos


class _Seg:
    __slots__ = ("kind", "key", "key_utf8", "index")

    def __init__(self, kind: str, key: str | None = None, index: int | None = None) -> None:
        self.kind = kind
        self.key = key
        self.key_utf8 = key.encode("utf-8", "surrogatepass") if key is not None else None
        self.index = index


def _parse_segment(seg: object) -> _Seg:
    if not isinstance(seg, (tuple, list)) or not seg:
        raise ValueError(f"invalid path segment: {seg!r}")
    tag = seg[0]
    if tag == SEG_KEY and len(seg) == 2 and type(seg[1]) is str:
        key = seg[1]
        s = _Seg(SEG_KEY, key=key)
        if _CANON_INDEX.fullmatch(s.key_utf8):
            s.index = int(key)
        return s
    if tag == SEG_INDEX and len(seg) == 2 and type(seg[1]) is int and seg[1] >= 0:
        return _Seg(SEG_INDEX, index=seg[1])
    if tag == SEG_ANY_INDEX and len(seg) == 1:
        return _Seg(SEG_ANY_INDEX)
    if tag == SEG_ANY_KEY and len(seg) == 1:
        return _Seg(SEG_ANY_KEY)
    raise ValueError(f"invalid path segment: {seg!r}")


class PathExtractor:
    """Spans of the values at given paths inside JSON records (dict semantics)."""

    __slots__ = ("_paths", "_member")

    def __init__(self, paths: list[list[tuple]], member_spans: bool = False) -> None:
        if isinstance(paths, (str, bytes)) or not hasattr(paths, "__iter__"):
            raise ValueError("paths must be a list of paths")
        compiled = []
        for path in paths:
            if isinstance(path, (str, bytes)) or not isinstance(path, (list, tuple)):
                raise ValueError(f"invalid path: {path!r}")
            compiled.append(tuple(_parse_segment(s) for s in path))
        self._paths = tuple(compiled)
        self._member = bool(member_spans)

    def extract(self, buf: bytes, spans: bytes) -> bytes:
        """u64 LE quads ``(span_idx, path_idx, start, end)`` for every match."""
        data = _as_bytes(buf, "buf")
        vals = _u64_array(spans, 2, "spans")
        n = len(data)
        out = array("Q")
        paths = self._paths
        for i in range(len(vals) // 2):
            s = vals[2 * i]
            e = vals[2 * i + 1]
            if s > e or e > n:
                raise ValueError(f"span {i} ({s}, {e}) out of bounds for {n} bytes")
            vs, ve = self._root(data, s, e)
            for pi, path in enumerate(paths):
                for a, b in self._resolve(data, vs, ve, path):
                    out.append(i)
                    out.append(pi)
                    out.append(a)
                    out.append(b)
        return _to_le_bytes(out)

    # -- validation ---------------------------------------------------------------------
    def _root(self, data: bytes, s: int, e: int) -> tuple[int, int]:
        """Validate the span; return the span of its value (member value with member_spans)."""
        return _root_span(data, s, e, self._member)

    # -- navigation (input already validated) ---------------------------------------------
    def _resolve(self, data: bytes, vs: int, ve: int, path: tuple[_Seg, ...]) -> list[tuple[int, int]]:
        cur = [(vs, ve)]
        for seg in path:
            nxt: list[tuple[int, int]] = []
            kind = seg.kind
            for a, b in cur:
                c = data[a]
                if c == 0x7B:  # object
                    if kind == SEG_KEY:
                        r = _obj_get(data, a, seg)
                        if r is not None:
                            nxt.append(r)
                    elif kind == SEG_ANY_KEY:
                        nxt.extend(_obj_items(data, a))
                elif c == 0x5B:  # array
                    if kind == SEG_ANY_INDEX:
                        nxt.extend(_iter_elems(data, a))
                    elif seg.index is not None and kind != SEG_ANY_KEY:
                        r = _arr_get(data, a, seg.index)
                        if r is not None:
                            nxt.append(r)
            cur = nxt
            if not cur:
                break
        return cur


def _root_span(data: bytes, s: int, e: int, member: bool) -> tuple[int, int]:
    """Validate the span ``data[s:e]`` (one JSON value, or an object member ``"key":
    value`` when ``member``, optionally surrounded by whitespace; ``ValueError``
    otherwise) and return the span of its value."""
    pats = _pats()
    pos = s
    if member:
        m = pats["member_head"](data, s, e)
        if m is None:
            raise ValueError(f"invalid JSON object member in span at {s}")
        pos = m.end()
    else:
        pos = _WS(data, pos, e).end()
    m = pats["value"](data, pos, e)
    if m is not None and _WS(data, m.end(), e).end() == e:
        return pos, m.end()
    return _root_exact(data, pos, e)


def _root_exact(data: bytes, pos: int, e: int) -> tuple[int, int]:
    from ._py_scanner import Scanner

    sc = Scanner(MODE_SINGLE, base_offset=pos)
    got = sc.feed(data[pos:e]) + sc.finish()
    if sc.error is not None or len(got) != 16:
        err = sc.error
        where = err[0] if err else pos
        code = err[1] if err else "invalid"
        raise ValueError(f"invalid JSON in span at byte {where}: {code}")
    vals = array("Q")
    vals.frombytes(got)
    if _BIG_ENDIAN:  # pragma: no cover
        vals.byteswap()
    return vals[0], vals[1]


def _iter_members(data: bytes, a: int) -> Iterator[tuple[bytes, int, int]]:
    """``(raw key content, value start, value end)`` of every member of the object at ``a``."""
    pats = _pats()
    member = pats["member"]
    head = pats["member_head"]
    pos = _WS(data, a + 1).end()
    if data[pos] == 0x7D:
        return
    while True:
        m = member(data, pos)
        if m is not None:
            vs, ve = m.span(2)
            yield m.group(1), vs, ve
            if m.group(3) == b"}":
                return
            pos = m.end()
            continue
        m = head(data, pos)  # value nested deeper than the fast pattern
        vs = m.end()
        ve = _skip_deep(data, vs)
        yield m.group(1), vs, ve
        pos = _WS(data, ve).end()
        if data[pos] == 0x7D:
            return
        pos += 1


def _iter_elems(data: bytes, a: int) -> Iterator[tuple[int, int]]:
    elem = _pats()["elem"]
    pos = _WS(data, a + 1).end()
    if data[pos] == 0x5D:
        return
    while True:
        m = elem(data, pos)
        if m is not None:
            yield m.span(1)
            if m.group(2) == b"]":
                return
            pos = m.end()
            continue
        vs = _WS(data, pos).end()
        ve = _skip_deep(data, vs)
        yield vs, ve
        pos = _WS(data, ve).end()
        if data[pos] == 0x5D:
            return
        pos += 1


def _key_equals(raw: bytes, seg: _Seg) -> bool:
    if b"\\" not in raw:
        return raw == seg.key_utf8
    return decode_key(raw) == seg.key


def _obj_get(data: bytes, a: int, seg: _Seg) -> tuple[int, int] | None:
    found = None
    for raw, vs, ve in _iter_members(data, a):
        if _key_equals(raw, seg):
            found = (vs, ve)  # duplicate keys: the last one wins
    return found


def _obj_items(data: bytes, a: int) -> list[tuple[int, int]]:
    items: dict[str, tuple[int, int]] = {}
    for raw, vs, ve in _iter_members(data, a):
        items[decode_key(raw)] = (vs, ve)  # first-appearance order, last value
    return list(items.values())


def _arr_get(data: bytes, a: int, index: int) -> tuple[int, int] | None:
    for i, span in enumerate(_iter_elems(data, a)):
        if i == index:
            return span
    return None
