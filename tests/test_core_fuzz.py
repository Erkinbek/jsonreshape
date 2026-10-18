"""Randomised tests of the byte-level core (docs/ARCHITECTURE.md §1).

* random valid documents (``jsongen.rand_value`` in several encode styles) and random
  mutations of them are scanned in every mode with random chunkings: results must not
  depend on the chunking, emitted spans must decode to exactly the expected
  children/records, and valid/invalid classification must agree with a strict
  independent oracle (``json.loads`` with ``parse_constant`` raising, on text decoded
  strictly as UTF-8);
* ``PathExtractor`` is checked against ``paths.PathPattern.resolve`` on parsed values;
* ``Reformatter`` output must re-parse to the same value and equal the canonical
  formatting (orjson / json.dumps);
* ``test_backends_identical`` compares the Python and Rust backends on all of the above
  (skipped while the Rust extension is unavailable).
"""

from __future__ import annotations

import codecs
import json
import random
import struct

import orjson
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jsonreshape import codec
from jsonreshape.core.constants import (
    MODE_BRACE_LIST,
    MODE_CONTAINER,
    MODE_LINES,
    MODE_SINGLE,
    MODE_STREAM,
)
from jsonreshape.paths import PathPattern, Wild

import jsongen

STYLES = ("compact", "pretty2", "messy", "escaped")
ALL_MODES = (MODE_CONTAINER, MODE_BRACE_LIST, MODE_LINES, MODE_STREAM, MODE_SINGLE)
# ``core_backend`` is a stateless module, so sharing it between examples is fine;
# deterministic examples and no example database (nothing is written to disk)
FUZZ = settings(deadline=None, max_examples=40, derandomize=True, database=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                       HealthCheck.function_scoped_fixture])


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------
def u64(b: bytes) -> list[int]:
    return list(struct.unpack("<%dQ" % (len(b) // 8), b))


def pairs(b: bytes) -> list[tuple[int, int]]:
    v = u64(b)
    return [(v[i], v[i + 1]) for i in range(0, len(v), 2)]


def quads(b: bytes) -> list[tuple[int, ...]]:
    v = u64(b)
    return [tuple(v[i:i + 4]) for i in range(0, len(v), 4)]


def pack(vals) -> bytes:
    flat = [x for p in vals for x in p]
    return struct.pack("<%dQ" % len(flat), *flat)


def split_by_sizes(data: bytes, sizes: list[int]) -> list[bytes]:
    out, i, k = [], 0, 0
    while i < len(data):
        n = sizes[k % len(sizes)] if sizes else len(data)
        out.append(data[i:i + n])
        i += n
        k += 1
    return out


def random_sizes(rng: random.Random) -> list[int]:
    return [rng.choice((1, 1, 2, 3, 5, 8, 17, 64, 1000)) for _ in range(rng.randint(1, 6))]


def run_scanner(core, data: bytes, mode: int, path=(), sizes=None, **kw):
    sc = core.Scanner(mode, list(path), **kw)
    out = b""
    broken = []
    for part in (split_by_sizes(data, sizes) if sizes else [data]):
        out += sc.feed(part)
        broken += sc.take_broken()
    out += sc.finish()
    broken += sc.take_broken()
    return (pairs(out), sc.error, sc.flags, sc.lines, sc.consumed, sc.record_count, sc.target_found,
            sc.container_span, sc.finished, broken)


def scan_invariant(core, data: bytes, mode: int, path=(), rng: random.Random | None = None, **kw):
    """Scan whole, byte by byte and with random chunk sizes; all results must be equal."""
    ref = run_scanner(core, data, mode, path, **kw)
    assert run_scanner(core, data, mode, path, [1], **kw) == ref, (data, mode, path)
    rng = rng or random.Random(len(data))
    for _ in range(2):
        sizes = random_sizes(rng)
        assert run_scanner(core, data, mode, path, sizes, **kw) == ref, (data, mode, path, sizes)
    return ref


# ---------------------------------------------------------------------------------------
# strict independent oracle
# ---------------------------------------------------------------------------------------
class Pairs(list):
    """A JSON object decoded as its list of ``(key, value)`` pairs (order + duplicates kept)."""


def _reject_constant(name):
    raise ValueError(f"{name} is not JSON")


INVALID = object()
NOTFOUND = object()
_WS = " \t\n\r"


def _text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse(text: str):
    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=Pairs)


def _parse_span(data: bytes):
    return json.loads(data.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=Pairs)


def oracle_single(data: bytes):
    text = _text(data)
    if text is None:
        return INVALID
    try:
        return _parse(text)
    except ValueError:
        return INVALID


def oracle_stream(data: bytes):
    text = _text(data)
    if text is None:
        return INVALID
    dec = json.JSONDecoder(parse_constant=_reject_constant, object_pairs_hook=Pairs)
    values, i, n = [], 0, len(text)
    while True:
        while i < n and text[i] in _WS:
            i += 1
        if i >= n:
            return values
        try:
            v, i = dec.raw_decode(text, i)
        except ValueError:
            return INVALID
        values.append(v)


def oracle_lines(data: bytes) -> list[tuple[int, object]]:
    out = []
    for no, line in enumerate(data.split(b"\n")):
        if not line.strip(b" \t\r"):
            continue
        out.append((no, oracle_single(line)))
    return out


def oracle_brace_list(data: bytes):
    text = _text(data)
    if text is None:
        return INVALID
    s = text.strip(_WS)
    if len(s) < 2 or s[0] != "{" or s[-1] != "}":
        return INVALID
    try:
        return _parse("[" + s[1:-1] + "]")
    except ValueError:
        return INVALID


def _find_first(node, path, depth=0):
    if depth == len(path):
        return node
    seg = path[depth]
    if isinstance(node, Pairs):
        if isinstance(seg, str):
            for k, v in node:
                if k == seg:
                    r = _find_first(v, path, depth + 1)
                    if r is not NOTFOUND:
                        return r
    elif isinstance(node, list) and isinstance(seg, int) and seg < len(node):
        return _find_first(node[seg], path, depth + 1)
    return NOTFOUND


def oracle_container(data: bytes, path=()):
    """Children of the target (list of values / Pairs of members) or INVALID / NOTFOUND /
    'scalar'."""
    root = oracle_single(data)
    if root is INVALID:
        return INVALID
    node = _find_first(root, list(path))
    if node is NOTFOUND:
        return NOTFOUND
    if not isinstance(node, list):
        return "scalar"
    return node


def check_against_oracle(data: bytes, mode: int, res, path=()) -> None:
    spans, error, broken = res[0], res[1], res[9]
    if mode == MODE_LINES:
        assert error is None
        expected = oracle_lines(data)
        bad = [no for no, v in expected if v is INVALID]
        assert [b[0] for b in broken] == bad, (data, broken)
        good = [v for no, v in expected if v is not INVALID]
        assert [_parse_span(data[a:b]) for a, b in spans] == good, data
        for line_no, line_start, line_end, off, code, exp in broken:
            assert line_start <= off <= line_end
            assert data[line_end:line_end + 1] in (b"\n", b"")
        return
    if mode == MODE_SINGLE:
        expected = oracle_single(data)
        if expected is INVALID:
            assert error is not None, data
        else:
            assert error is None, (data, error)
            assert [_parse_span(data[a:b]) for a, b in spans] == [expected]
        return
    if mode == MODE_STREAM:
        expected = oracle_stream(data)
    elif mode == MODE_BRACE_LIST:
        expected = oracle_brace_list(data)
    else:
        expected = oracle_container(data, path)
        if expected is NOTFOUND or isinstance(expected, str):
            assert error is not None, (data, path)
            return
    if expected is INVALID:
        assert error is not None, (data, mode)
        return
    assert error is None, (data, mode, error)
    if isinstance(expected, Pairs):
        got = [_parse_span(b"{" + data[a:b] + b"}")[0] for a, b in spans]
        assert got == list(expected)
    else:
        assert [_parse_span(data[a:b]) for a, b in spans] == list(expected)


# ---------------------------------------------------------------------------------------
# independent reference parser (exact error semantics of spec §1.2, whole buffer, simple)
# ---------------------------------------------------------------------------------------
class RefError(Exception):
    def __init__(self, off: int, code: str, expected: str) -> None:
        super().__init__(off, code, expected)
        self.off, self.code, self.expected = off, code, expected


_DIGITS = b"0123456789"
_HEX = b"0123456789abcdefABCDEF"
_LITS = {ord("t"): b"true", ord("f"): b"false", ord("n"): b"null"}
_SCALAR_START = set(b'"-0123456789tfn')


class RefParser:
    """Straightforward recursive-descent implementation of the Scanner contract."""

    def __init__(self, data: bytes, mode: int, path=(), max_depth: int = 100000) -> None:
        self.d = data
        self.mode = mode
        self.path = list(path)
        self.max_depth = max_depth
        self.flags = 0
        self.spans: list[tuple[int, int]] = []
        self.target_found = False
        self.container_span = None
        self.n = len(data)

    # -- tokens --------------------------------------------------------------------------
    def ws(self, i: int) -> int:
        while i < self.n and self.d[i] in b" \t\n\r":
            i += 1
        return i

    def string(self, i: int) -> int:
        d, n = self.d, self.n
        i += 1
        while True:
            if i >= n:
                raise RefError(n, "unexpected_eof", "string_end")
            c = d[i]
            if c == 0x22:
                return i + 1
            if c == 0x5C:
                if i + 1 >= n:
                    raise RefError(n, "unexpected_eof", "escape")
                e = d[i + 1]
                if e in b'"\\/bfnrt':
                    self.flags |= 1
                    i += 2
                    continue
                if e != ord("u"):
                    raise RefError(i + 1, "invalid_escape", "escape")
                for k in range(4):
                    j = i + 2 + k
                    if j >= n:
                        raise RefError(n, "unexpected_eof", "hex")
                    if d[j] not in _HEX:
                        raise RefError(j, "invalid_hex", "hex")
                self.flags |= 3
                i += 6
                continue
            if c < 0x20:
                raise RefError(i, "control_char", "string_end")
            if c >= 0x80:
                dec = codecs.getincrementaldecoder("utf-8")()
                j = i
                while True:
                    if j >= n:
                        raise RefError(n, "unexpected_eof", "string_end")
                    try:
                        out = dec.decode(d[j:j + 1])
                    except UnicodeDecodeError:
                        raise RefError(i, "invalid_utf8", "string_end") from None
                    j += 1
                    if out:
                        break
                self.flags |= 4
                i = j
                continue
            i += 1

    def number(self, i: int) -> int:
        d, n = self.d, self.n

        def need_digit(j: int) -> None:
            if j >= n:
                raise RefError(n, "unexpected_eof", "digit")
            if d[j] not in _DIGITS:
                raise RefError(j, "invalid_number", "digit")

        j = i
        if d[j] == ord("-"):
            j += 1
            need_digit(j)
        if d[j] == ord("0"):
            j += 1
        else:
            while j < n and d[j] in _DIGITS:
                j += 1
        if j < n and d[j] == ord("."):
            j += 1
            need_digit(j)
            while j < n and d[j] in _DIGITS:
                j += 1
        if j < n and d[j] in b"eE":
            j += 1
            if j < n and d[j] in b"+-":
                j += 1
            need_digit(j)
            while j < n and d[j] in _DIGITS:
                j += 1
        return j

    def literal(self, i: int) -> int:
        lit = _LITS[self.d[i]]
        for k in range(len(lit)):
            j = i + k
            if j >= self.n:
                raise RefError(self.n, "unexpected_eof", "literal")
            if self.d[j] != lit[k]:
                raise RefError(j, "invalid_literal", "literal")
        return i + len(lit)

    # -- values ------------------------------------------------------------------------------
    def value(self, i: int, depth: int, label: str, ppos=None) -> int:
        """Value at ``i`` whose parent has ``depth``; ``ppos`` = path segments matched."""
        if i >= self.n:
            raise RefError(self.n, "unexpected_eof", label)
        c = self.d[i]
        is_target = ppos is not None and ppos == len(self.path) and not self.target_found
        if c in (0x5B, 0x7B):
            if depth + 1 > self.max_depth:
                raise RefError(i, "depth_limit", "none")
            if is_target:
                self.target_found = True
                end = self.container(i, depth + 1, None, records=True)
                self.container_span = (i, end)
                return end
            return self.container(i, depth + 1, ppos if (ppos is not None and ppos < len(self.path)) else None)
        if c in _SCALAR_START:
            if is_target:
                raise RefError(i, "target_not_container", "none")
            if c == 0x22:
                return self.string(i)
            if c in _LITS:
                return self.literal(i)
            return self.number(i)
        raise RefError(i, "unexpected_char", label)

    def container(self, i: int, depth: int, ppos, records: bool = False, brace_list: bool = False) -> int:
        d = self.d
        is_obj = d[i] == 0x7B and not brace_list
        close = 0x7D if (is_obj or brace_list) else 0x5D
        after = "comma_or_brace" if (is_obj or brace_list) else "comma_or_bracket"
        i = self.ws(i + 1)
        label = "key_or_end" if is_obj else "value_or_end"
        index = 0
        while True:
            if i >= self.n:
                raise RefError(self.n, "unexpected_eof", label)
            if label in ("key_or_end", "value_or_end") and d[i] == close:
                return i + 1
            start = i
            child = None
            if is_obj:
                if d[i] != 0x22:
                    raise RefError(i, "unexpected_char", label)
                kend = self.string(i)
                if ppos is not None and isinstance(self.path[ppos], str):
                    key = json.loads(d[i:kend].decode("utf-8"))
                    if key == self.path[ppos]:
                        child = ppos + 1
                i = self.ws(kend)
                if i >= self.n:
                    raise RefError(self.n, "unexpected_eof", "colon")
                if d[i] != 0x3A:
                    raise RefError(i, "unexpected_char", "colon")
                i = self.ws(i + 1)
                vlabel = "value"
            else:
                if ppos is not None and isinstance(self.path[ppos], int) and self.path[ppos] == index:
                    child = ppos + 1
                vlabel = label
            end = self.value(i, depth, vlabel, child)
            if records:
                self.spans.append((start, end))
            index += 1
            i = self.ws(end)
            if i >= self.n:
                raise RefError(self.n, "unexpected_eof", after)
            if d[i] == 0x2C:
                i = self.ws(i + 1)
                label = "key" if is_obj else "value"
                continue
            if d[i] == close:
                return i + 1
            raise RefError(i, "unexpected_char", after)

    # -- modes -----------------------------------------------------------------------------
    def run(self):
        """(spans, error triple or None, flags, broken, target_found, container_span)."""
        broken = []
        try:
            if self.mode == MODE_LINES:
                ls = 0
                for no, line in enumerate(self.d.split(b"\n")):
                    le = ls + len(line)
                    self.n = le
                    i = self.ws(ls)
                    if i < le:
                        try:
                            end = self.value(i, 0, "value")
                            j = self.ws(end)
                            if j < le:
                                raise RefError(j, "trailing_data", "end")
                            self.spans.append((i, end))
                        except RefError as err:
                            broken.append((no, ls, le, err.off, err.code, err.expected))
                    ls = le + 1
                return self.spans, None, self.flags, broken, False, None
            i = self.ws(0)
            if self.mode == MODE_STREAM:
                while i < self.n:
                    end = self.value(i, 0, "value")
                    self.spans.append((i, end))
                    i = self.ws(end)
                return self.spans, None, self.flags, broken, False, None
            if self.mode == MODE_BRACE_LIST:
                if i >= self.n:
                    raise RefError(self.n, "unexpected_eof", "open_brace")
                if self.d[i] != 0x7B:
                    raise RefError(i, "unexpected_char", "open_brace")
                end = self.container(i, 1, None, records=True, brace_list=True)
            elif self.mode == MODE_SINGLE:
                end = self.value(i, 0, "value")
                self.spans.append((i, end))
            else:
                end = self.value(i, 0, "value", 0)
            j = self.ws(end)
            if j < self.n:
                raise RefError(j, "trailing_data", "end")
            if self.mode == MODE_CONTAINER and not self.target_found:
                raise RefError(self.n, "target_not_found", "none")
            err = None
        except RefError as e:
            err = (e.off, e.code, e.expected)
        return self.spans, err, self.flags, broken, self.target_found, self.container_span


def check_exact(data: bytes, mode: int, res, path=(), max_depth: int = 100000) -> None:
    spans, error, flags, broken, found, cspan = RefParser(data, mode, path, max_depth).run()
    got_error = res[1]
    if got_error is not None:
        off = got_error[0]
        assert got_error[3:] == (data.count(b"\n", 0, off), data.rfind(b"\n", 0, off) + 1)
        got_error = got_error[:3]
    assert (res[0], got_error, res[2], res[9]) == (spans, error, flags, broken), (data, mode, path)
    if mode == MODE_CONTAINER:
        assert (res[6], res[7]) == (found, cspan), (data, path)


# ---------------------------------------------------------------------------------------
# document generators
# ---------------------------------------------------------------------------------------
def encode_with_duplicates(value, rng: random.Random) -> bytes:
    """Compact encoding where objects randomly repeat keys (with other values)."""
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            kt = json.dumps(k, ensure_ascii=False).encode()
            if rng.random() < 0.3:
                parts.append(kt + b":" + encode_with_duplicates(jsongen.rand_value(rng, 2, 3), rng))
            parts.append(kt + b":" + encode_with_duplicates(v, rng))
        return b"{" + b",".join(parts) + b"}"
    if isinstance(value, list):
        return b"[" + b",".join(encode_with_duplicates(v, rng) for v in value) + b"]"
    return jsongen.encode(value, "compact")


def random_document(rng: random.Random) -> bytes:
    value = jsongen.rand_value(rng, 0, 5)
    if rng.random() < 0.15:
        return encode_with_duplicates(value, rng)
    return jsongen.encode(value, rng.choice(STYLES), seed=rng.randint(0, 1000))


INTERESTING = (b'{}[],:"\\ \t\n\r0123456789-+.eEtrufalsn\x00\x1f\x7f'
               b"\x80\xbf\xc3\xd0\xe4\xed\xef\xf0\xf4\xff")


def mutate(data: bytes, rng: random.Random) -> bytes:
    for _ in range(rng.randint(1, 3)):
        k = rng.random()
        pos = rng.randint(0, len(data))
        if not data or k < 0.3:
            data = data[:pos] + bytes([rng.choice(INTERESTING)]) + data[pos:]
        elif k < 0.55:
            data = data[:pos] + data[pos + 1:]
        elif k < 0.85:
            p = min(pos, len(data) - 1)
            data = data[:p] + bytes([rng.choice(INTERESTING)]) + data[p + 1:]
        else:
            data = data[:pos]
    return data


def make_lines(values_enc: list[bytes], rng: random.Random) -> bytes:
    out = []
    for enc in values_enc:
        if rng.random() < 0.2:
            out.append(rng.choice([b"", b"  ", b"\r", b"\t "]))
        out.append(rng.choice([b"", b" "]) + enc + rng.choice([b"", b" ", b"\r"]))
    tail = b"\n" if rng.random() < 0.7 else b""
    return b"\n".join(out) + tail


# ---------------------------------------------------------------------------------------
# Scanner fuzzing
# ---------------------------------------------------------------------------------------
@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_valid_documents_all_modes(core_backend, seed):
    rng = random.Random(seed)
    values = [jsongen.rand_value(rng, 0, 5) for _ in range(rng.randint(1, 4))]
    style = rng.choice(STYLES)
    encs = [jsongen.encode(v, style, seed=seed + i) for i, v in enumerate(values)]
    # MODE_SINGLE / MODE_CONTAINER on each value
    for v, enc in zip(values, encs):
        doc = b" \n" + enc + b"\r\n" if rng.random() < 0.5 else enc
        res = scan_invariant(core_backend, doc, MODE_SINGLE, rng=rng)
        assert res[1] is None and len(res[0]) == 1
        check_against_oracle(doc, MODE_SINGLE, res)
        res = scan_invariant(core_backend, doc, MODE_CONTAINER, rng=rng)
        check_against_oracle(doc, MODE_CONTAINER, res)
        if isinstance(v, (list, dict)):
            assert res[1] is None and res[6] is True
            assert res[7] == (doc.index(enc), doc.index(enc) + len(enc))
    # array of all values, wrapped object, stream, brace list, lines
    arr = b"[" + b",".join(encs) + b"]"
    res = scan_invariant(core_backend, arr, MODE_CONTAINER, rng=rng)
    assert res[1] is None and [arr[a:b] for a, b in res[0]] == encs
    wrapped = b'{"meta": {"data": 0}, "data": ' + arr + b', "tail": [1]}'
    res = scan_invariant(core_backend, wrapped, MODE_CONTAINER, ["data"], rng=rng)
    assert res[1] is None and [wrapped[a:b] for a, b in res[0]] == encs
    stream = b"".join(e + rng.choice([b"\n", b" ", b"\r\n", b"\t"]) for e in encs)
    res = scan_invariant(core_backend, stream, MODE_STREAM, rng=rng)
    assert res[1] is None and [stream[a:b] for a, b in res[0]] == encs
    brace = b"{" + b" , ".join(encs) + b"}"
    res = scan_invariant(core_backend, brace, MODE_BRACE_LIST, rng=rng)
    assert res[1] is None and [brace[a:b] for a, b in res[0]] == encs
    compact = [jsongen.encode(v, "escaped" if style == "escaped" else "compact") for v in values]
    lines = make_lines(compact, rng)
    res = scan_invariant(core_backend, lines, MODE_LINES, rng=rng)
    assert res[9] == [] and [lines[a:b] for a, b in res[0]] == compact
    check_against_oracle(lines, MODE_LINES, res)


@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_mutations_against_oracle(core_backend, seed):
    rng = random.Random(seed)
    base = random_document(rng)
    for _ in range(3):
        data = mutate(base, rng)
        for mode in ALL_MODES:
            doc = data
            if mode == MODE_BRACE_LIST and rng.random() < 0.7:
                doc = b"{" + data + b"," + mutate(data, rng) + b"}"
            elif mode == MODE_LINES:
                doc = data + b"\n" + base.replace(b"\n", b" ") + b"\n" + mutate(data, rng)
            elif mode == MODE_STREAM and rng.random() < 0.5:
                doc = data + b" " + data
            res = scan_invariant(core_backend, doc, mode, rng=rng)
            check_against_oracle(doc, mode, res)


def _random_target_path(value, rng: random.Random) -> list:
    path = []
    node = value
    while isinstance(node, (list, dict)) and node and rng.random() < 0.8:
        if isinstance(node, dict):
            key = rng.choice(list(node))
            path.append(key)
            node = node[key]
        else:
            i = rng.randrange(len(node))
            path.append(i)
            node = node[i]
    if rng.random() < 0.15:
        path.append(rng.choice(["missing", 0, 5, "id"]))
    return path


@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_container_target_paths_against_oracle(core_backend, seed):
    rng = random.Random(seed)
    value = jsongen.rand_value(rng, 0, 5)
    for _ in range(4):
        path = _random_target_path(value, rng)
        if rng.random() < 0.3:
            doc = encode_with_duplicates(value, rng)
        else:
            doc = jsongen.encode(value, rng.choice(STYLES), seed=seed)
        res = scan_invariant(core_backend, doc, MODE_CONTAINER, path, rng=rng)
        check_against_oracle(doc, MODE_CONTAINER, res, path)
        if res[1] is None:
            assert res[6] is True and res[7] is not None


@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_exact_results_against_reference_parser(core_backend, seed):
    """Spans, exact error tuples, flags and broken lines equal the reference parser's."""
    rng = random.Random(seed)
    base = random_document(rng)
    value = json.loads(base) if oracle_single(base) is not INVALID else None
    for _ in range(3):
        data = mutate(base, rng) if rng.random() < 0.8 else base
        for mode in ALL_MODES:
            doc = data
            if mode == MODE_BRACE_LIST and rng.random() < 0.5:
                doc = b"{" + data + b"," + base + b"}"
            elif mode == MODE_LINES:
                doc = data + b"\n" + base.replace(b"\n", b" ") + b"\r\n" + mutate(data, rng)
            res = scan_invariant(core_backend, doc, mode, rng=rng)
            check_exact(doc, mode, res)
        path = _random_target_path(value, rng) if value is not None else rng.choice([[], ["data"], [0]])
        res = scan_invariant(core_backend, data, MODE_CONTAINER, path, rng=rng)
        check_exact(data, MODE_CONTAINER, res, path)


def _deep_value(rng: random.Random, depth: int):
    if depth == 0:
        return jsongen.rand_scalar(rng)
    inner = [_deep_value(rng, depth - 1)] + [jsongen.rand_value(rng, 0, 2) for _ in range(rng.randint(0, 2))]
    rng.shuffle(inner)
    if rng.random() < 0.5:
        return inner
    return {f"k{i}": v for i, v in enumerate(inner)}


@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_deep_documents_and_max_depth(core_backend, seed):
    """Values nested deeper than the fast paths, with max_depth around the real depth."""
    rng = random.Random(seed)
    depth = rng.randint(3, 25)
    value = _deep_value(rng, depth)
    doc = jsongen.encode(value, rng.choice(STYLES), seed=seed)
    arr = b"[" + doc + b"," + doc + b"]"
    for max_depth in sorted({1, 2, max(1, depth - 2), depth, depth + 1, depth + 2}):
        for mode, data in ((MODE_SINGLE, doc), (MODE_CONTAINER, arr), (MODE_STREAM, doc + b" " + doc),
                           (MODE_BRACE_LIST, b"{" + doc + b"}")):
            res = scan_invariant(core_backend, data, mode, rng=rng, max_depth=max_depth)
            check_exact(data, mode, res, max_depth=max_depth)
        lines = doc.replace(b"\n", b"") + b"\n" + arr.replace(b"\n", b"")
        res = scan_invariant(core_backend, lines, MODE_LINES, rng=rng, max_depth=max_depth)
        check_exact(lines, MODE_LINES, res, max_depth=max_depth)
    mutated = mutate(arr, rng)
    res = scan_invariant(core_backend, mutated, MODE_CONTAINER, rng=rng)
    check_exact(mutated, MODE_CONTAINER, res)


# ---------------------------------------------------------------------------------------
# PathExtractor vs PathPattern.resolve
# ---------------------------------------------------------------------------------------
def _random_pattern(value, rng: random.Random) -> tuple:
    segs = []
    node = value
    for _ in range(rng.randint(0, 4)):
        r = rng.random()
        if isinstance(node, dict) and node:
            if r < 0.25:
                segs.append(Wild.ANY_KEY)
                node = rng.choice(list(node.values()))
            elif r < 0.9:
                key = rng.choice(list(node))
                segs.append(key)
                node = node[key]
            else:
                segs.append(rng.choice(["nope", 0, Wild.ANY_INDEX]))
                node = None
        elif isinstance(node, list) and node:
            if r < 0.3:
                segs.append(Wild.ANY_INDEX)
                node = rng.choice(node)
            elif r < 0.8:
                i = rng.randrange(len(node))
                segs.append(i if rng.random() < 0.7 else str(i))
                node = node[i]
            else:
                segs.append(rng.choice([len(node), "x", Wild.ANY_KEY, "01"]))
                node = None
        else:
            segs.append(rng.choice(["a", 0, Wild.ANY_KEY, Wild.ANY_INDEX]))
            node = None
    return tuple(segs)


@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_path_extractor_against_resolve(core_backend, seed):
    rng = random.Random(seed)
    values = [jsongen.rand_value(rng, 0, 5) for _ in range(rng.randint(1, 5))]
    encs = []
    for i, v in enumerate(values):
        if rng.random() < 0.3:
            encs.append(encode_with_duplicates(v, rng))
        else:
            encs.append(jsongen.encode(v, rng.choice(STYLES), seed=seed + i))
    buf = b"[" + b",\n".join(encs) + b"]"
    spans = core_backend.Scanner(MODE_CONTAINER).feed(buf)
    patterns = [PathPattern(_random_pattern(rng.choice(values), rng)) for _ in range(4)]
    pe = core_backend.PathExtractor([p.to_core() for p in patterns])
    got = quads(pe.extract(buf, spans))
    expected = []
    for si, enc in enumerate(encs):
        parsed = json.loads(enc)  # dict semantics: duplicate keys -> last wins
        for pi, pat in enumerate(patterns):
            for _, v in pat.resolve(parsed):
                expected.append((si, pi, v))
    assert [(q[0], q[1]) for q in got] == [(e[0], e[1]) for e in expected]
    assert [json.loads(buf[q[2]:q[3]]) for q in got] == [e[2] for e in expected]
    # member spans: the members of an object record
    obj = {"k%d" % i: v for i, v in enumerate(values)}
    doc = jsongen.encode(obj, rng.choice(STYLES), seed=seed)
    mspans = core_backend.Scanner(MODE_CONTAINER).feed(doc)
    pe = core_backend.PathExtractor([p.to_core() for p in patterns], member_spans=True)
    got = quads(pe.extract(doc, mspans))
    expected = []
    for si, v in enumerate(values):
        for pi, pat in enumerate(patterns):
            for _, x in pat.resolve(v):
                expected.append((si, pi, x))
    assert [(q[0], q[1]) for q in got] == [(e[0], e[1]) for e in expected]
    assert [json.loads(doc[q[2]:q[3]]) for q in got] == [e[2] for e in expected]


# ---------------------------------------------------------------------------------------
# MultiFinder.locate_tokens
# ---------------------------------------------------------------------------------------
_WS_BYTES = b" \t\r\n"


def _oracle_tokens(buf: bytes, s: int, e: int, member: bool) -> tuple[list[tuple], bool]:
    """Every key/scalar token ``(kind, start, end, path)`` of the valid span in document
    order, and whether some object repeats a key (decoded with ``json``): a recursive
    descent written independently of both cores."""
    toks: list[tuple] = []
    dup = False

    def ws(i: int) -> int:
        while i < e and buf[i] in _WS_BYTES:
            i += 1
        return i

    def string_end(i: int) -> int:
        j = i + 1
        while buf[j] != 0x22:
            j += 2 if buf[j] == 0x5C else 1
        return j + 1

    def value(i: int, path: tuple) -> int:
        nonlocal dup
        i = ws(i)
        c = buf[i]
        if c == 0x7B:
            keys: set[str] = set()
            i = ws(i + 1)
            if buf[i] == 0x7D:
                return i + 1
            while True:
                j = string_end(i)
                key = json.loads(buf[i:j])
                dup = dup or key in keys
                keys.add(key)
                toks.append((1, i, j, path + (key,)))
                i = value(ws(j) + 1, path + (key,))
                i = ws(i)
                if buf[i] == 0x7D:
                    return i + 1
                i = ws(i + 1)
        if c == 0x5B:
            i = ws(i + 1)
            if buf[i] == 0x5D:
                return i + 1
            n = 0
            while True:
                i = ws(value(i, path + (n,)))
                n += 1
                if buf[i] == 0x5D:
                    return i + 1
                i += 1
        if c == 0x22:
            j = string_end(i)
            toks.append((0, i, j, path))
            return j
        j = i
        while j < e and buf[j] not in b" \t\r\n,]}":
            j += 1
        toks.append((3 if c in b"tfn" else 2, i, j, path))
        return j

    i = ws(s)
    if member:
        j = string_end(i)
        toks.append((4, i, j, ()))
        i = ws(j) + 1
    value(i, ())
    return toks, dup


def oracle_locate(needles: list[bytes], buf: bytes, spans: list[tuple[int, int]], member: bool) -> list[tuple]:
    """``locate_tokens`` from its definition: the tokens in which an occurrence lying
    entirely inside the span starts (brute force), plus the duplicate-key entry."""
    out: list[tuple] = []
    for si, (s, e) in enumerate(spans):
        toks, dup = _oracle_tokens(buf, s, e, member)
        starts = sorted(p for p in range(s, e) if any(buf.startswith(n, p) and p + len(n) <= e for n in needles))
        located = [(si, kind, a, b, path) for kind, a, b, path in toks
                   if any(a <= p < b for p in starts)]
        out += located
        if located and dup:
            out.append((si, 5, 0, 0, ()))
    return out


def _random_needles(buf: bytes, rng: random.Random) -> list[bytes]:
    """Pieces of ``buf`` (so they occur) and a few typical bytes."""
    needles = []
    for _ in range(rng.randint(1, 4)):
        if buf and rng.random() < 0.7:
            p = rng.randrange(len(buf))
            needles.append(buf[p:p + rng.randint(1, 4)])
        else:
            needles.append(rng.choice([b'"', b",", b" ", b"e", b"1", b"\\", b"a", b'":', b"\xd0", b"ull"]))
    return needles


@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_locate_tokens_against_oracle(core_backend, seed):
    rng = random.Random(seed)
    values = [jsongen.rand_value(rng, 0, 5) for _ in range(rng.randint(1, 5))]
    encs = []
    for i, v in enumerate(values):
        if rng.random() < 0.3:
            encs.append(encode_with_duplicates(v, rng))
        else:
            encs.append(jsongen.encode(v, rng.choice(STYLES), seed=seed + i))
    buf = b"[" + b",\n".join(encs) + b"]"
    spans = pairs(core_backend.Scanner(MODE_CONTAINER).feed(buf))
    for _ in range(3):
        needles = _random_needles(buf, rng)
        got = core_backend.MultiFinder(needles).locate_tokens(buf, pack(spans))
        assert got == oracle_locate(needles, buf, spans, False), (buf, needles)
    # member spans: the members of an object
    obj = {jsongen.rand_string(rng): v for v in values}
    doc = jsongen.encode(obj, rng.choice(STYLES), seed=seed)
    mspans = pairs(core_backend.Scanner(MODE_CONTAINER).feed(doc))
    for _ in range(3):
        needles = _random_needles(doc, rng)
        got = core_backend.MultiFinder(needles).locate_tokens(doc, pack(mspans), True)
        assert got == oracle_locate(needles, doc, mspans, True), (doc, needles)


def test_locate_tokens_oracle_follows_the_parsed_walk():
    """The oracle's token list of a record without duplicate keys is the document-order
    walk of ``codec.loads`` (keys at the member path, scalars with their exact text)."""

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                yield 1, path + (k,), k
                yield from walk(x, path + (k,))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                yield from walk(x, path + (i,))
        else:
            yield ((0 if isinstance(v, str) else 3 if v is None or isinstance(v, bool) else 2), path,
                   v if isinstance(v, str) else json.dumps(v) if v is None or isinstance(v, bool)
                   else codec.number_text(v))

    rng = random.Random(4321)
    for i in range(200):
        rec = jsongen.encode(jsongen.rand_value(rng, 0, 5), STYLES[i % len(STYLES)], seed=i)
        toks, dup = _oracle_tokens(rec, 0, len(rec), False)
        assert not dup
        got = [(kind, path, json.loads(rec[a:b]) if kind < 2 else rec[a:b].decode()) for kind, a, b, path in toks]
        assert got == list(walk(codec.loads(rec), ())), rec


def test_locate_tokens_backends_identical(both_backends):
    """python == rust: results on valid spans, the exception type on invalid ones."""
    py, rs = both_backends
    rng = random.Random(99)
    for round_ in range(150):
        values = [jsongen.rand_value(rng, 0, 5) for _ in range(rng.randint(1, 6))]
        encs = [encode_with_duplicates(v, rng) if rng.random() < 0.3
                else jsongen.encode(v, rng.choice(STYLES), seed=round_ + i) for i, v in enumerate(values)]
        buf = b"[" + b",\n".join(encs) + b"]"
        spans = py.Scanner(MODE_CONTAINER).feed(buf)
        obj = jsongen.encode({jsongen.rand_string(rng): v for v in values}, rng.choice(STYLES), seed=round_)
        mspans = py.Scanner(MODE_CONTAINER).feed(obj)
        # random spans over the buffer: mostly invalid JSON
        cuts = sorted(rng.sample(range(len(buf) + 1), min(len(buf) + 1, 6)))
        bad = pack(list(zip(cuts[::2], cuts[1::2])))
        mutated = mutate(buf, rng)
        mut_spans = pack([(0, len(mutated))])
        for _ in range(3):
            needles = _random_needles(buf, rng)
            mp, mr = py.MultiFinder(needles), rs.MultiFinder(needles)
            assert mp.locate_tokens(buf, spans) == mr.locate_tokens(buf, spans), (buf, needles)
            assert _observe(lambda: mp.locate_tokens(buf, bad)) == _observe(lambda: mr.locate_tokens(buf, bad))
            assert _observe(lambda: mp.locate_tokens(mutated, mut_spans)) == \
                _observe(lambda: mr.locate_tokens(mutated, mut_spans)), mutated
            assert _observe(lambda: mp.locate_tokens(buf, spans, True)) == \
                _observe(lambda: mr.locate_tokens(buf, spans, True))
            m_needles = _random_needles(obj, rng)
            assert py.MultiFinder(m_needles).locate_tokens(obj, mspans, True) == \
                rs.MultiFinder(m_needles).locate_tokens(obj, mspans, member_spans=True), (obj, m_needles)
    # case-insensitive needle sets (every ASCII case variant: the Rust core folds the case)
    text = b'["Toshkent", "TOSHKENT shahri", {"tOsH": "kent"}, "Stra\xc3\x9fe", "toshkENT"]'
    tspans = py.Scanner(MODE_CONTAINER).feed(text)
    variants = [b""]
    for c in b"tosh":
        variants = [v + x for v in variants for x in (bytes([c]), bytes([c]).upper())]
    for needles in (variants, variants + [b"\xc3\x9f"], variants[1:]):
        assert py.MultiFinder(needles).locate_tokens(text, tspans) == rs.MultiFinder(needles).locate_tokens(text, tspans)
        assert py.MultiFinder(needles).find_in_spans(text, tspans) == rs.MultiFinder(needles).find_in_spans(text, tspans)


# ---------------------------------------------------------------------------------------
# Reformatter
# ---------------------------------------------------------------------------------------
def _json_dumps_friendly(value) -> bool:
    """Numbers formatted identically by orjson and json.dumps (so both can be compared)."""
    if isinstance(value, dict):
        return all(_json_dumps_friendly(v) for v in value.values())
    if isinstance(value, list):
        return all(_json_dumps_friendly(v) for v in value)
    if isinstance(value, (codec.JsonFloat, codec.JsonInt)):
        return False  # raw text that orjson would not reproduce (1.10, -0, 1e400, ...)
    if isinstance(value, float):
        return json.dumps(value) == orjson.dumps(value).decode()
    if isinstance(value, int) and not isinstance(value, bool):
        return -2**63 <= value < 2**64
    return True


def _plain_value(rng: random.Random):
    while True:
        v = jsongen.rand_value(rng, 0, 5)
        try:
            enc = orjson.dumps(v)
        except (TypeError, orjson.JSONEncodeError):
            continue
        if orjson.loads(enc) == v and _json_dumps_friendly(v) and json.loads(enc) == v:
            return v


def reformat(core, data: bytes, indent, sizes=None, base_level=0) -> bytes:
    f = core.Reformatter(indent, base_level)
    out = b"".join(f.feed(p) for p in (split_by_sizes(data, sizes) if sizes else [data]))
    return out + f.finish()


@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_reformatter_canonical(core_backend, seed):
    rng = random.Random(seed)
    value = _plain_value(rng)
    src = jsongen.encode(value, rng.choice(("compact", "pretty2", "pretty4", "tab", "messy")), seed=seed)
    sizes = random_sizes(rng)
    compact = orjson.dumps(value)
    assert reformat(core_backend, src, None) == compact
    assert reformat(core_backend, src, None, sizes) == compact
    pretty2 = orjson.dumps(value, option=orjson.OPT_INDENT_2)
    assert reformat(core_backend, src, 2) == pretty2
    assert reformat(core_backend, src, 2, sizes) == pretty2
    for indent in (4, "\t", 0, 3):
        expected = json.dumps(value, indent=indent, ensure_ascii=False).encode()
        assert reformat(core_backend, src, indent, sizes) == expected
    # any output re-parses to the same value
    for indent in (None, 1, 2, "\t"):
        assert json.loads(reformat(core_backend, src, indent, [1])) == value


@FUZZ
@given(seed=st.integers(0, 2**32 - 1))
def test_reformatter_keeps_tokens(core_backend, seed):
    """Strings and number tokens are copied byte for byte (escaped styles, exact numbers)."""
    rng = random.Random(seed)
    value = jsongen.rand_value(rng, 0, 5)
    style = rng.choice(("escaped", "messy_escaped", "messy", "compact"))
    src = jsongen.encode(value, style, seed=seed)
    sizes = random_sizes(rng)
    canon = jsongen.encode(value, "escaped" if "escaped" in style else "compact")
    out = reformat(core_backend, src, None, sizes)
    assert out == canon
    for indent in (2, "\t"):
        out = reformat(core_backend, src, indent, sizes)
        assert reformat(core_backend, out, None) == reformat(core_backend, src, None)


# ---------------------------------------------------------------------------------------
# cross-backend equality (runs once the Rust extension is available)
# ---------------------------------------------------------------------------------------
def _observe(fn):
    try:
        return ("ok", fn())
    except Exception as exc:  # compare exception types across backends
        return ("exc", type(exc).__name__)


def test_backends_identical(both_backends):
    py, rs = both_backends
    rng = random.Random(1234)
    docs = []
    for i in range(60):
        d = random_document(rng)
        docs.append(d)
        docs.append(mutate(d, rng))
    docs += [b"", b" ", b"[1,]", b'{"a":1}\n[1,\n"x', b"\xef\xbb\xbf[]", b"1 2 3", b"{1,2}"]
    for d in docs:
        for mode in ALL_MODES:
            sizes = random_sizes(rng)
            for sz in (None, [1], sizes):
                assert _observe(lambda: run_scanner(py, d, mode, (), sz)) == \
                    _observe(lambda: run_scanner(rs, d, mode, (), sz)), (d, mode, sz)
        value = oracle_single(d)
        if value is not INVALID:
            for path in (["data"], [0], [1, "id"], ["a", 0, "b"]):
                assert _observe(lambda: run_scanner(py, d, MODE_CONTAINER, path)) == \
                    _observe(lambda: run_scanner(rs, d, MODE_CONTAINER, path))
        for kw in ({"base_offset": 17}, {"max_depth": 3}):
            assert _observe(lambda: run_scanner(py, d, MODE_SINGLE, (), [3], **kw)) == \
                _observe(lambda: run_scanner(rs, d, MODE_SINGLE, (), [3], **kw))
    # Reformatter
    for d in docs[:80]:
        for indent in (None, 0, 2, 4, "\t"):
            for sz in (None, [1], random_sizes(rng)):
                assert _observe(lambda: reformat(py, d, indent, sz, 1)) == \
                    _observe(lambda: reformat(rs, d, indent, sz, 1))
    # numbers_plain
    for d in docs:
        assert _observe(lambda: py.numbers_plain(d)) == _observe(lambda: rs.numbers_plain(d))
    for tok in jsongen.EDGE_NUMBER_TOKENS:
        assert py.numbers_plain(tok.encode()) == rs.numbers_plain(tok.encode())
    # MultiFinder
    big = b"\n".join(docs)
    spans = []
    pos = 0
    while pos < len(big):
        n = rng.randint(0, 40)
        spans.append((pos, min(len(big), pos + n)))
        pos += n + rng.randint(0, 5)
    spans = [(a, b) for a, b in spans if a <= b <= len(big)]
    for needles in ([b"a"], [b"1", b"12", b"x"], [b'"id"', b"null"], [b"\xd0", b"\\u"]):
        mp, mr = py.MultiFinder(needles), rs.MultiFinder(needles)
        assert mp.find_in_spans(big, pack(spans)) == mr.find_in_spans(big, pack(spans))
        for _ in range(50):
            s = rng.randint(0, len(big))
            e = rng.randint(s, len(big))
            assert mp.find_first(big, s, e) == mr.find_first(big, s, e)
    # PathExtractor
    values = [jsongen.rand_value(rng, 0, 5) for _ in range(40)]
    buf = b"[" + b",".join(encode_with_duplicates(v, rng) for v in values) + b"]"
    rec_spans = py.Scanner(MODE_CONTAINER).feed(buf)
    pats = [PathPattern(_random_pattern(rng.choice(values), rng)).to_core() for _ in range(10)]
    for member in (False, True):
        assert _observe(lambda: py.PathExtractor(pats, member).extract(buf, rec_spans)) == \
            _observe(lambda: rs.PathExtractor(pats, member).extract(buf, rec_spans))
