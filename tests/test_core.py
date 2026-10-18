"""Explicit, hand-checked tests of the byte-level core API (docs/ARCHITECTURE.md §1).

Every test runs against every available backend (``core_backend`` fixture), so the Rust
extension is held to exactly the same expectations as the pure-Python reference.
"""

from __future__ import annotations

import json
import random
import struct

import orjson
import pytest

from jsonreshape import codec
from jsonreshape.core.constants import (
    DEFAULT_MAX_DEPTH,
    FLAG_ESCAPES,
    FLAG_NON_ASCII,
    FLAG_U_ESCAPES,
    MODE_BRACE_LIST,
    MODE_CONTAINER,
    MODE_LINES,
    MODE_SINGLE,
    MODE_STREAM,
)

import jsongen

ALL_MODES = (MODE_CONTAINER, MODE_BRACE_LIST, MODE_LINES, MODE_STREAM, MODE_SINGLE)


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------
def u64(b: bytes) -> list[int]:
    assert len(b) % 8 == 0
    return list(struct.unpack("<%dQ" % (len(b) // 8), b))


def pairs(b: bytes) -> list[tuple[int, int]]:
    v = u64(b)
    return [(v[i], v[i + 1]) for i in range(0, len(v), 2)]


def quads(b: bytes) -> list[tuple[int, int, int, int]]:
    v = u64(b)
    return [tuple(v[i:i + 4]) for i in range(0, len(v), 4)]


def pack(vals) -> bytes:
    flat = [x for p in vals for x in p]
    return struct.pack("<%dQ" % len(flat), *flat)


def chunked(data: bytes, how) -> list[bytes]:
    """``"whole"``, ``"bytes"`` (one byte per chunk) or an int seed (random sizes)."""
    if how == "whole":
        return [data]
    if how == "bytes":
        return [data[i:i + 1] for i in range(len(data))]
    rng = random.Random(how)
    out, i = [], 0
    while i < len(data):
        n = rng.choice((1, 2, 3, 4, 5, 7, 11, 16, 64, 333))
        out.append(data[i:i + n])
        i += n
    return out


def scan(core, data: bytes, mode: int, path=(), how="whole", **kw) -> dict:
    """Run a scanner over ``data`` and collect every observable result."""
    sc = core.Scanner(mode, list(path), **kw)
    spans = b""
    broken = []
    for part in chunked(data, how):
        spans += sc.feed(part)
        broken += sc.take_broken()
    spans += sc.finish()
    broken += sc.take_broken()
    return {
        "spans": pairs(spans),
        "error": sc.error,
        "flags": sc.flags,
        "lines": sc.lines,
        "consumed": sc.consumed,
        "record_count": sc.record_count,
        "target_found": sc.target_found,
        "container_span": sc.container_span,
        "finished": sc.finished,
        "broken": broken,
    }


def scan_all(core, data: bytes, mode: int, path=(), **kw) -> dict:
    """Scan with several chunkings, assert identical results and return them."""
    ref = scan(core, data, mode, path, "whole", **kw)
    for how in ("bytes", 1, 2, 3):
        got = scan(core, data, mode, path, how, **kw)
        assert got == ref, f"chunking {how!r} differs for {data!r}"
    assert ref["record_count"] == len(ref["spans"])
    return ref


def texts(data: bytes, spans) -> list[bytes]:
    return [data[a:b] for a, b in spans]


# ---------------------------------------------------------------------------------------
# Scanner: modes and record spans
# ---------------------------------------------------------------------------------------
def test_container_array_root(core_backend):
    data = b'[ 1 , "x" , [ ] , { } , null , -1.5e3 , true,{"a":[1,{"b":2}]} ]'
    r = scan_all(core_backend, data, MODE_CONTAINER)
    assert texts(data, r["spans"]) == [b"1", b'"x"', b"[ ]", b"{ }", b"null", b"-1.5e3", b"true",
                                       b'{"a":[1,{"b":2}]}']
    assert r["error"] is None
    assert r["target_found"] is True
    assert r["container_span"] == (0, len(data))
    assert r["finished"] is True
    assert r["consumed"] == len(data)


def test_container_object_root_member_spans(core_backend):
    data = b'{ "a" : 1 ,\n "b":[1, 2], "c" : {"d": null} }'
    r = scan_all(core_backend, data, MODE_CONTAINER)
    assert texts(data, r["spans"]) == [b'"a" : 1', b'"b":[1, 2]', b'"c" : {"d": null}']
    assert r["container_span"] == (0, len(data))
    assert r["lines"] == 1


def test_container_empty_containers(core_backend):
    for data in (b"[]", b"{}", b" [ \n ] ", b"{\t}"):
        r = scan_all(core_backend, data, MODE_CONTAINER)
        assert r["spans"] == [] and r["error"] is None and r["target_found"]
        start = data.index(data.strip()[:1])
        assert r["container_span"] == (start, len(data.rstrip()))


def test_single_mode(core_backend):
    data = b'  {"a": [1]}  '
    r = scan_all(core_backend, data, MODE_SINGLE)
    assert r["spans"] == [(2, 12)]
    for data, span in ((b"1", (0, 1)), (b' "s" ', (1, 4)), (b"\n-0.5e-3\n", (1, 8)), (b"null", (0, 4)),
                       (b"[[[[[[[[[[[[1]]]]]]]]]]]]", (0, 25))):
        r = scan_all(core_backend, data, MODE_SINGLE)
        assert r["spans"] == [span] and r["error"] is None
        assert r["target_found"] is False and r["container_span"] is None


def test_stream_mode(core_backend):
    data = b'1 2 "a"[3]{}truefalse 0123\n{"x":1}\t-1-2 null'
    r = scan_all(core_backend, data, MODE_STREAM)
    assert texts(data, r["spans"]) == [b"1", b"2", b'"a"', b"[3]", b"{}", b"true", b"false", b"0", b"123",
                                       b'{"x":1}', b"-1", b"-2", b"null"]
    assert r["error"] is None and r["lines"] == 1


def test_brace_list_mode(core_backend):
    data = b'{ {"a":1}, [2], 3 ,"s",{}}'
    r = scan_all(core_backend, data, MODE_BRACE_LIST)
    assert texts(data, r["spans"]) == [b'{"a":1}', b"[2]", b"3", b'"s"', b"{}"]
    assert r["error"] is None
    r = scan_all(core_backend, b" { } ", MODE_BRACE_LIST)
    assert r["spans"] == [] and r["error"] is None


def test_lines_mode_records(core_backend):
    data = b'{"a":1}\n  [1, 2]  \r\n"s"\n\n7'
    r = scan_all(core_backend, data, MODE_LINES)
    assert texts(data, r["spans"]) == [b'{"a":1}', b"[1, 2]", b'"s"', b"7"]
    assert r["broken"] == [] and r["error"] is None and r["lines"] == 4


def test_record_spans_trimmed(core_backend):
    data = b'[\n  "a b" ,\n  12.5e+3\t,\r\n  {"k": [ ]}\n]'
    r = scan_all(core_backend, data, MODE_CONTAINER)
    assert texts(data, r["spans"]) == [b'"a b"', b"12.5e+3", b'{"k": [ ]}']


def test_numbers_complete_only_at_next_byte(core_backend):
    """A number at a chunk end is not emitted before the following byte is seen."""
    sc = core_backend.Scanner(MODE_CONTAINER)
    assert pairs(sc.feed(b"[1,12")) == [(1, 2)]
    assert pairs(sc.feed(b"3")) == []
    assert pairs(sc.feed(b"]")) == [(3, 6)]
    assert pairs(sc.finish()) == []
    sc = core_backend.Scanner(MODE_STREAM)
    assert pairs(sc.feed(b"1 23")) == [(0, 1)]
    assert pairs(sc.finish()) == [(2, 4)]
    sc = core_backend.Scanner(MODE_LINES)
    assert pairs(sc.feed(b'{"a":1}')) == []
    assert pairs(sc.feed(b"\n")) == [(0, 7)]


def test_base_offset(core_backend):
    data = b'[1,{"a":2}]'
    r = scan_all(core_backend, data, MODE_CONTAINER, base_offset=3)
    assert r["spans"] == [(4, 5), (6, 13)]
    assert r["container_span"] == (3, 14)
    assert r["consumed"] == 3 + len(data)
    r = scan_all(core_backend, b"[1,\n x]", MODE_CONTAINER, base_offset=100)
    assert r["error"] == (105, "unexpected_char", "value", 1, 104)
    r = scan_all(core_backend, b"1\nx\n", MODE_LINES, base_offset=10)
    assert r["spans"] == [(10, 11)]
    assert r["broken"] == [(1, 12, 13, 12, "unexpected_char", "value")]
    sc = core_backend.Scanner(MODE_SINGLE, base_offset=7)
    assert sc.consumed == 7
    sc.feed(b"")
    assert sc.consumed == 7


# ---------------------------------------------------------------------------------------
# Scanner: target paths
# ---------------------------------------------------------------------------------------
WRAPPED = b'{"meta": {"data": [9]}, "data": [ {"id":1}, {"id":2} ], "tail": {"x": [1]}}'


def test_target_path_wrapped(core_backend):
    r = scan_all(core_backend, WRAPPED, MODE_CONTAINER, ["data"])
    assert texts(WRAPPED, r["spans"]) == [b'{"id":1}', b'{"id":2}']
    start = WRAPPED.index(b"[ {")
    assert r["container_span"] == (start, WRAPPED.index(b"],") + 1)
    assert r["target_found"] and r["error"] is None


def test_target_path_nested_and_index(core_backend):
    data = b'{"a": [0, {"b": {"c": [5, 6]}}, 2], "z": 1}'
    r = scan_all(core_backend, data, MODE_CONTAINER, ["a", 1, "b", "c"])
    assert texts(data, r["spans"]) == [b"5", b"6"]
    r = scan_all(core_backend, data, MODE_CONTAINER, ["a", 1, "b"])
    assert texts(data, r["spans"]) == [b'"c": [5, 6]']
    r = scan_all(core_backend, data, MODE_CONTAINER, ["a"])
    assert texts(data, r["spans"]) == [b"0", b'{"b": {"c": [5, 6]}}', b"2"]
    data2 = b"[[1, 2], [3, [4, 5]]]"
    r = scan_all(core_backend, data2, MODE_CONTAINER, [1, 1])
    assert texts(data2, r["spans"]) == [b"4", b"5"]
    assert r["container_span"] == (data2.index(b"[4"), data2.index(b"5]") + 2)


def test_target_path_escaped_key(core_backend):
    data = b'{"\\u0064ata": [1, 2], "data": [3]}'
    r = scan_all(core_backend, data, MODE_CONTAINER, ["data"])
    assert texts(data, r["spans"]) == [b"1", b"2"]
    assert r["flags"] == FLAG_ESCAPES | FLAG_U_ESCAPES
    data = '{"t\\u00e9st": 0, "tést": [7]}'.encode()
    r = scan_all(core_backend, data, MODE_CONTAINER, ["tést"])
    assert r["error"] == (data.index(b": 0") + 2, "target_not_container", "none", 0, 0)
    data = b'{"\\ud83d\\ude00": [1], "x": 0}'
    r = scan_all(core_backend, data, MODE_CONTAINER, ["\U0001F600"])
    assert texts(data, r["spans"]) == [b"1"]
    data = b'{"\\ud800": [2]}'
    r = scan_all(core_backend, data, MODE_CONTAINER, ["\ud800"])
    assert texts(data, r["spans"]) == [b"2"]


def test_target_path_duplicates_first_match(core_backend):
    data = b'{"data": [1], "data": [2, 3]}'
    r = scan_all(core_backend, data, MODE_CONTAINER, ["data"])
    assert texts(data, r["spans"]) == [b"1"]
    assert r["container_span"] == (9, 12) and r["error"] is None
    # intermediate duplicates: a later duplicate may still lead to the target
    data = b'{"a": {"x": 1}, "a": {"b": [7]}, "a": {"b": [8]}}'
    r = scan_all(core_backend, data, MODE_CONTAINER, ["a", "b"])
    assert texts(data, r["spans"]) == [b"7"]
    # str segment never matches an array element, int never an object member
    data = b'{"0": [1], "x": [[2]]}'
    r = scan_all(core_backend, data, MODE_CONTAINER, [0])
    assert r["error"] == (len(data), "target_not_found", "none", 0, 0)
    r = scan_all(core_backend, data, MODE_CONTAINER, ["x", "0"])
    assert r["error"] == (len(data), "target_not_found", "none", 0, 0)
    r = scan_all(core_backend, data, MODE_CONTAINER, ["x", 0])
    assert texts(data, r["spans"]) == [b"2"]


def test_target_not_found_and_not_container(core_backend):
    r = scan_all(core_backend, b'{"a": 1}\n', MODE_CONTAINER, ["b"])
    assert r["error"] == (9, "target_not_found", "none", 1, 9)
    assert r["target_found"] is False and r["container_span"] is None
    r = scan_all(core_backend, b'{"a": [1]}', MODE_CONTAINER, ["a", 3])
    assert r["error"] == (10, "target_not_found", "none", 0, 0)
    r = scan_all(core_backend, b'{"a": "s"}', MODE_CONTAINER, ["a"])
    assert r["error"] == (6, "target_not_container", "none", 0, 0)
    r = scan_all(core_backend, b'  "x"', MODE_CONTAINER)
    assert r["error"] == (2, "target_not_container", "none", 0, 0)
    r = scan_all(core_backend, b"1", MODE_CONTAINER)
    assert r["error"] == (0, "target_not_container", "none", 0, 0)
    # decided at the first byte of the scalar (even if the scalar itself is invalid) ...
    r = scan_all(core_backend, b'{"a": tru}', MODE_CONTAINER, ["a"])
    assert r["error"] == (6, "target_not_container", "none", 0, 0)
    # ... but a byte that cannot start any value is a syntax error
    r = scan_all(core_backend, b'{"a": ]}', MODE_CONTAINER, ["a"])
    assert r["error"] == (6, "unexpected_char", "value", 0, 0)
    # a syntax error after the root wins over target_not_found
    r = scan_all(core_backend, b'{"a": 1} x', MODE_CONTAINER, ["b"])
    assert r["error"] == (9, "trailing_data", "end", 0, 0)
    # the rest of the document is still validated after the target closed
    r = scan_all(core_backend, b'{"a": [1], "b": tru}', MODE_CONTAINER, ["a"])
    assert r["spans"] == [(7, 8)]
    assert r["error"] == (19, "invalid_literal", "literal", 0, 0)
    assert r["container_span"] == (6, 9)


# ---------------------------------------------------------------------------------------
# Scanner: errors
# ---------------------------------------------------------------------------------------
ERROR_CASES = [
    # (data, mode, (offset, code, expected))
    (b"[1,]", MODE_CONTAINER, (3, "unexpected_char", "value")),
    (b"[01]", MODE_CONTAINER, (2, "unexpected_char", "comma_or_bracket")),
    (b"[1x]", MODE_CONTAINER, (2, "unexpected_char", "comma_or_bracket")),
    (b"[1 2]", MODE_CONTAINER, (3, "unexpected_char", "comma_or_bracket")),
    (b"[-]", MODE_CONTAINER, (2, "invalid_number", "digit")),
    (b"[-a]", MODE_CONTAINER, (2, "invalid_number", "digit")),
    (b"[1.]", MODE_CONTAINER, (3, "invalid_number", "digit")),
    (b"[1.x]", MODE_CONTAINER, (3, "invalid_number", "digit")),
    (b"[1e]", MODE_CONTAINER, (3, "invalid_number", "digit")),
    (b"[1E+]", MODE_CONTAINER, (4, "invalid_number", "digit")),
    (b"[1.5.3]", MODE_CONTAINER, (4, "unexpected_char", "comma_or_bracket")),
    (b"[+1]", MODE_CONTAINER, (1, "unexpected_char", "value_or_end")),
    (b"[.5]", MODE_CONTAINER, (1, "unexpected_char", "value_or_end")),
    (b"[tru]", MODE_CONTAINER, (4, "invalid_literal", "literal")),
    (b"[nulL]", MODE_CONTAINER, (4, "invalid_literal", "literal")),
    (b"[falsy]", MODE_CONTAINER, (5, "invalid_literal", "literal")),
    (b"[truex]", MODE_CONTAINER, (5, "unexpected_char", "comma_or_bracket")),
    (b"[True]", MODE_CONTAINER, (1, "unexpected_char", "value_or_end")),
    (b'["a\\qb"]', MODE_CONTAINER, (4, "invalid_escape", "escape")),
    (b'["\\u12G4"]', MODE_CONTAINER, (6, "invalid_hex", "hex")),
    (b'["a\tb"]', MODE_CONTAINER, (3, "control_char", "string_end")),
    (b'["a\nb"]', MODE_CONTAINER, (3, "control_char", "string_end")),
    (b'["\x00"]', MODE_CONTAINER, (2, "control_char", "string_end")),
    (b'["\xff"]', MODE_CONTAINER, (2, "invalid_utf8", "string_end")),
    (b'["ok", "\xe4\xb8"]', MODE_CONTAINER, (8, "invalid_utf8", "string_end")),
    (b'["\xe4\xb8\x41"]', MODE_CONTAINER, (2, "invalid_utf8", "string_end")),
    (b'["\xed\xa0\x80"]', MODE_CONTAINER, (2, "invalid_utf8", "string_end")),
    (b'["\xc0\x80"]', MODE_CONTAINER, (2, "invalid_utf8", "string_end")),
    (b'["\xe0\x80\x80"]', MODE_CONTAINER, (2, "invalid_utf8", "string_end")),
    (b'["\xf4\x90\x80\x80"]', MODE_CONTAINER, (2, "invalid_utf8", "string_end")),
    (b'["\x80"]', MODE_CONTAINER, (2, "invalid_utf8", "string_end")),
    (b"[\xc3\xa9]", MODE_CONTAINER, (1, "unexpected_char", "value_or_end")),
    (b"\xef\xbb\xbf[1]", MODE_CONTAINER, (0, "unexpected_char", "value")),
    (b"[1] x", MODE_CONTAINER, (4, "trailing_data", "end")),
    (b"[1][2]", MODE_CONTAINER, (3, "trailing_data", "end")),
    (b'{"a" 1}', MODE_CONTAINER, (5, "unexpected_char", "colon")),
    (b'{"a":1,}', MODE_CONTAINER, (7, "unexpected_char", "key")),
    (b"{,}", MODE_CONTAINER, (1, "unexpected_char", "key_or_end")),
    (b"{1:2}", MODE_CONTAINER, (1, "unexpected_char", "key_or_end")),
    (b'{"a":1 "b":2}', MODE_CONTAINER, (7, "unexpected_char", "comma_or_brace")),
    (b'{"a":}', MODE_CONTAINER, (5, "unexpected_char", "value")),
    (b'{"a":1]', MODE_CONTAINER, (6, "unexpected_char", "comma_or_brace")),
    (b"[}", MODE_CONTAINER, (1, "unexpected_char", "value_or_end")),
    (b"[1}", MODE_CONTAINER, (2, "unexpected_char", "comma_or_bracket")),
    (b"]", MODE_CONTAINER, (0, "unexpected_char", "value")),
    (b"[1", MODE_CONTAINER, (2, "unexpected_eof", "comma_or_bracket")),
    (b"[1,", MODE_CONTAINER, (3, "unexpected_eof", "value")),
    (b"[", MODE_CONTAINER, (1, "unexpected_eof", "value_or_end")),
    (b"{", MODE_CONTAINER, (1, "unexpected_eof", "key_or_end")),
    (b'{"a":1,', MODE_CONTAINER, (7, "unexpected_eof", "key")),
    (b'{"a"', MODE_CONTAINER, (4, "unexpected_eof", "colon")),
    (b'{"a":', MODE_CONTAINER, (5, "unexpected_eof", "value")),
    (b'{"a":1', MODE_CONTAINER, (6, "unexpected_eof", "comma_or_brace")),
    (b'{"a', MODE_CONTAINER, (3, "unexpected_eof", "string_end")),
    (b'["abc', MODE_CONTAINER, (5, "unexpected_eof", "string_end")),
    (b'["a\\', MODE_CONTAINER, (4, "unexpected_eof", "escape")),
    (b'["\\u12', MODE_CONTAINER, (6, "unexpected_eof", "hex")),
    (b'["\xe4\xb8', MODE_CONTAINER, (4, "unexpected_eof", "string_end")),
    (b"[tr", MODE_CONTAINER, (3, "unexpected_eof", "literal")),
    (b"[-", MODE_CONTAINER, (2, "unexpected_eof", "digit")),
    (b"[1e-", MODE_CONTAINER, (4, "unexpected_eof", "digit")),
    (b"[12", MODE_CONTAINER, (3, "unexpected_eof", "comma_or_bracket")),
    (b"", MODE_CONTAINER, (0, "unexpected_eof", "value")),
    (b" \n\t ", MODE_CONTAINER, (4, "unexpected_eof", "value")),
    (b"", MODE_SINGLE, (0, "unexpected_eof", "value")),
    (b"1 2", MODE_SINGLE, (2, "trailing_data", "end")),
    (b"1x", MODE_SINGLE, (1, "trailing_data", "end")),
    (b'"a""b"', MODE_SINGLE, (3, "trailing_data", "end")),
    (b"-", MODE_SINGLE, (1, "unexpected_eof", "digit")),
    (b"1.", MODE_SINGLE, (2, "unexpected_eof", "digit")),
    (b"tru", MODE_SINGLE, (3, "unexpected_eof", "literal")),
    (b"1.x", MODE_SINGLE, (2, "invalid_number", "digit")),
    (b'{"a": 1.x}', MODE_SINGLE, (8, "invalid_number", "digit")),
    (b"", MODE_BRACE_LIST, (0, "unexpected_eof", "open_brace")),
    (b"  ", MODE_BRACE_LIST, (2, "unexpected_eof", "open_brace")),
    (b"[1]", MODE_BRACE_LIST, (0, "unexpected_char", "open_brace")),
    (b"{1 2}", MODE_BRACE_LIST, (3, "unexpected_char", "comma_or_brace")),
    (b"{1,}", MODE_BRACE_LIST, (3, "unexpected_char", "value")),
    (b"{]", MODE_BRACE_LIST, (1, "unexpected_char", "value_or_end")),
    (b"{}x", MODE_BRACE_LIST, (2, "trailing_data", "end")),
    (b"{1", MODE_BRACE_LIST, (2, "unexpected_eof", "comma_or_brace")),
    (b"{", MODE_BRACE_LIST, (1, "unexpected_eof", "value_or_end")),
    (b'{"a":1}', MODE_BRACE_LIST, (4, "unexpected_char", "comma_or_brace")),
    (b"1 ]", MODE_STREAM, (2, "unexpected_char", "value")),
    (b"1.5.3", MODE_STREAM, (3, "unexpected_char", "value")),
    (b"1.x", MODE_STREAM, (2, "invalid_number", "digit")),
    (b"[1", MODE_STREAM, (2, "unexpected_eof", "comma_or_bracket")),
    (b'"ab', MODE_STREAM, (3, "unexpected_eof", "string_end")),
]


@pytest.mark.parametrize("data,mode,expected", ERROR_CASES)
def test_error_cases(core_backend, data, mode, expected):
    r = scan_all(core_backend, data, mode)
    off, code, exp = expected
    line = data.count(b"\n", 0, off)
    line_start = data.rfind(b"\n", 0, off) + 1
    assert r["error"] == (off, code, exp, line, line_start)
    assert r["finished"] is True


def test_error_line_and_column(core_backend):
    # error on line 2 (1-based), column 5 (1-based): line index 1, offset = line_start + 4
    data = b'{\n"a" 1}'
    r = scan_all(core_backend, data, MODE_SINGLE)
    assert r["error"] == (6, "unexpected_char", "colon", 1, 2)
    off, _, _, line, line_start = r["error"]
    assert (line + 1, off - line_start + 1) == (2, 5)
    data = b"[1,\r\n  2,\r\n  x]"
    r = scan_all(core_backend, data, MODE_CONTAINER)
    assert r["error"] == (13, "unexpected_char", "value", 2, 11)
    assert r["spans"] == [(1, 2), (7, 8)]
    assert r["lines"] == 2


def test_utf8_error_split_across_chunks(core_backend):
    data = b'["a",\n"\xe4\xb8\x41"]'
    lead = data.index(b"\xe4")
    for cut in range(lead, lead + 3):
        sc = core_backend.Scanner(MODE_CONTAINER)
        sc.feed(data[:cut])
        sc.feed(data[cut:])
        sc.finish()
        assert sc.error == (lead, "invalid_utf8", "string_end", 1, 6)


def test_records_before_error_stay_emitted(core_backend):
    sc = core_backend.Scanner(MODE_CONTAINER)
    out = sc.feed(b'[{"a":1},2,x,4]')
    assert pairs(out) == [(1, 8), (9, 10)]
    assert sc.error == (11, "unexpected_char", "value", 0, 0)
    assert sc.finished is True
    assert sc.feed(b"[1]") == b""  # input after an error is ignored
    assert sc.finish() == b""
    assert sc.record_count == 2
    assert sc.error == (11, "unexpected_char", "value", 0, 0)


def test_depth_limit(core_backend):
    r = scan_all(core_backend, b"[[1]]", MODE_SINGLE, max_depth=2)
    assert r["error"] is None
    r = scan_all(core_backend, b"[[1]]", MODE_SINGLE, max_depth=1)
    assert r["error"] == (1, "depth_limit", "none", 0, 0)
    r = scan_all(core_backend, b'{"a":[{"b":[]}]}', MODE_SINGLE, max_depth=3)
    assert r["error"] == (11, "depth_limit", "none", 0, 0)
    r = scan_all(core_backend, b"{[1]}", MODE_BRACE_LIST, max_depth=1)
    assert r["error"] == (1, "depth_limit", "none", 0, 0)
    deep = b"[" * 30 + b"]" * 30
    assert scan_all(core_backend, deep, MODE_SINGLE, max_depth=30)["error"] is None
    assert scan_all(core_backend, deep, MODE_SINGLE, max_depth=29)["error"] == (29, "depth_limit", "none", 0, 0)
    very_deep = b"[" * 5000 + b"1" + b"]" * 5000
    r = scan(core_backend, very_deep, MODE_SINGLE, how=7)
    assert r["error"] is None and r["spans"] == [(0, len(very_deep))]
    r = scan(core_backend, b"[" * (DEFAULT_MAX_DEPTH + 1), MODE_SINGLE)
    assert r["error"] == (DEFAULT_MAX_DEPTH, "depth_limit", "none", 0, 0)
    r = scan_all(core_backend, b"[[1]]\n[1]\n", MODE_LINES, max_depth=1)
    assert r["broken"] == [(0, 0, 5, 1, "depth_limit", "none")]
    assert r["spans"] == [(6, 9)]


def test_empty_input_per_mode(core_backend):
    for data in (b"", b"  \n\r\n\t"):
        for mode in ALL_MODES:
            r = scan_all(core_backend, data, mode)
            assert r["spans"] == []
            if mode in (MODE_LINES, MODE_STREAM):
                assert r["error"] is None and r["broken"] == []
            else:
                exp = "open_brace" if mode == MODE_BRACE_LIST else "value"
                lines = data.count(b"\n")
                assert r["error"] == (len(data), "unexpected_eof", exp, lines, data.rfind(b"\n") + 1)


# ---------------------------------------------------------------------------------------
# Scanner: MODE_LINES broken lines
# ---------------------------------------------------------------------------------------
def test_lines_broken_lines(core_backend):
    data = b'{"a":1}\r\n\r\n  \n[1,2] [3]\n{"b":\n"x\n1\n'
    r = scan_all(core_backend, data, MODE_LINES)
    assert texts(data, r["spans"]) == [b'{"a":1}', b"1"]
    assert r["broken"] == [
        (3, 14, 23, 20, "trailing_data", "end"),
        (4, 24, 29, 29, "unexpected_eof", "value"),
        (5, 30, 32, 32, "unexpected_eof", "string_end"),
    ]
    assert r["error"] is None
    assert r["lines"] == 7
    assert r["finished"] is True


@pytest.mark.parametrize("data,broken", [
    (b"1\n[1", [(1, 2, 4, 4, "unexpected_eof", "comma_or_bracket")]),
    (b"x\n1", [(0, 0, 1, 0, "unexpected_char", "value")]),
    (b"tr\n1", [(0, 0, 2, 2, "unexpected_eof", "literal")]),
    (b"-\n1", [(0, 0, 1, 1, "unexpected_eof", "digit")]),
    (b'"ab\\\n1', [(0, 0, 4, 4, "unexpected_eof", "escape")]),
    (b'"\\u00\n1', [(0, 0, 5, 5, "unexpected_eof", "hex")]),
    (b'"\xd0\n1', [(0, 0, 2, 2, "unexpected_eof", "string_end")]),
    (b'"\xd0x"\n1', [(0, 0, 4, 1, "invalid_utf8", "string_end")]),
    (b"[1,\n1", [(0, 0, 3, 3, "unexpected_eof", "value")]),
    (b'{"a"\r\n1', [(0, 0, 5, 5, "unexpected_eof", "colon")]),
    (b"1 2\r\n1", [(0, 0, 4, 2, "trailing_data", "end")]),
    (b"[1]]\n1", [(0, 0, 4, 3, "trailing_data", "end")]),
    (b'"a\tb"\n1', [(0, 0, 5, 2, "control_char", "string_end")]),
])
def test_lines_broken_variants(core_backend, data, broken):
    r = scan_all(core_backend, data, MODE_LINES)
    assert r["broken"] == broken
    assert texts(data, r["spans"]) == [b"1"]
    assert r["error"] is None


def test_lines_take_broken_incremental(core_backend):
    sc = core_backend.Scanner(MODE_LINES)
    sc.feed(b"x\n1\n")
    assert sc.take_broken() == [(0, 0, 1, 0, "unexpected_char", "value")]
    assert sc.take_broken() == []
    sc.feed(b"[")  # broken line not finished yet
    assert sc.take_broken() == []
    assert sc.finished is False
    sc.finish()
    assert sc.take_broken() == [(2, 4, 5, 5, "unexpected_eof", "value_or_end")]
    assert sc.error is None and sc.finished is True
    assert core_backend.Scanner(MODE_CONTAINER).take_broken() == []


# ---------------------------------------------------------------------------------------
# Scanner: flags
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("data,flags", [
    (b'["abc", 1, {"k": true}]', 0),
    (b'["a\\nb"]', FLAG_ESCAPES),
    (b'["a\\/b", "\\\\"]', FLAG_ESCAPES),
    (b'["\\u00e9"]', FLAG_ESCAPES | FLAG_U_ESCAPES),
    ('["é"]'.encode(), FLAG_NON_ASCII),
    ('{"é": 1}'.encode(), FLAG_NON_ASCII),
    (b'{"\\u00e9": 1}', FLAG_ESCAPES | FLAG_U_ESCAPES),
    ('{"\\t": "😀"}'.encode(), FLAG_ESCAPES | FLAG_NON_ASCII),
    (b'["\\x"]', 0),           # escape cut by an error sets nothing
    (b'["\\u00', 0),
    ('["é", "'.encode() + b'\xff"]', FLAG_NON_ASCII),  # valid sequence, then an invalid byte
    (b'["\xe4\xb8"]', 0),
])
def test_flags(core_backend, data, flags):
    r = scan_all(core_backend, data, MODE_CONTAINER)
    assert r["flags"] == flags


def test_flags_are_cumulative(core_backend):
    sc = core_backend.Scanner(MODE_STREAM)
    sc.feed(b'"\\n" ')
    assert sc.flags == FLAG_ESCAPES
    sc.feed('"ж" "x"'.encode())
    assert sc.flags == FLAG_ESCAPES | FLAG_NON_ASCII
    sc.feed(b' "\\u0041"')
    sc.finish()
    assert sc.flags == FLAG_ESCAPES | FLAG_NON_ASCII | FLAG_U_ESCAPES


# ---------------------------------------------------------------------------------------
# Scanner: chunking invariance and API
# ---------------------------------------------------------------------------------------
def _corpus() -> list[bytes]:
    docs = [WRAPPED, b'{"\\u0064ata": [1, 2], "data": [3]}', b"[1,12,123, -0.5e+10 ,true]",
            '["Тошкент", "O‘zbekiston", "😀", {"k\\"ey": "v\\\\"}]'.encode()]
    rng = random.Random(42)
    for i in range(8):
        value = jsongen.rand_value(rng, 0, 5)
        for style in ("compact", "pretty2", "messy", "escaped"):
            docs.append(jsongen.encode([value, {"r": value}], style, seed=i))
    return docs


def test_chunking_identical(core_backend):
    for data in _corpus():
        for mode in (MODE_CONTAINER, MODE_SINGLE, MODE_STREAM, MODE_LINES):
            ref = scan(core_backend, data, mode, (), "whole")
            assert scan(core_backend, data, mode, (), "bytes") == ref
            for seed in range(3):
                assert scan(core_backend, data, mode, (), seed) == ref
            if mode in (MODE_CONTAINER, MODE_SINGLE):
                assert ref["error"] is None


def test_scanner_argument_errors(core_backend):
    S = core_backend.Scanner
    for bad in (0, 6, -1):
        with pytest.raises(ValueError):
            S(bad)
    with pytest.raises(ValueError):
        S(MODE_CONTAINER, [-1])
    with pytest.raises(ValueError):
        S(MODE_CONTAINER, [1.5])
    with pytest.raises(ValueError):
        S(MODE_CONTAINER, [True])
    with pytest.raises(ValueError):
        S(MODE_SINGLE, ["data"])
    with pytest.raises(ValueError):
        S(MODE_CONTAINER, [], -1)
    with pytest.raises(ValueError):
        S(MODE_CONTAINER, [], 0, 0)
    sc = S(MODE_SINGLE)
    sc.feed(b"1")
    sc.finish()
    with pytest.raises(ValueError):
        sc.feed(b"2")
    assert sc.finish() == b""


def test_scanner_defaults(core_backend):
    sc = core_backend.Scanner(MODE_CONTAINER)
    assert sc.error is None and sc.record_count == 0 and sc.consumed == 0 and sc.lines == 0
    assert sc.flags == 0 and sc.target_found is False and sc.container_span is None
    assert sc.finished is False
    sc.feed(b"[1,\n2]")
    assert sc.target_found is True and sc.container_span == (0, 6)
    assert sc.lines == 1 and sc.consumed == 6 and sc.record_count == 2


@pytest.mark.bench
def test_scanner_throughput(core_backend):
    """Pure-Python target: >= 15 MB/s for MODE_CONTAINER on record arrays (Rust: >= 400)."""
    import time

    data = codec.dumps(jsongen.records(60000, seed=1))
    sc = core_backend.Scanner(MODE_CONTAINER)
    sc.feed(data[:200000])  # warm-up (pattern compilation)
    t0 = time.perf_counter()
    sc = core_backend.Scanner(MODE_CONTAINER)
    for i in range(0, len(data), 8 << 20):
        sc.feed(data[i:i + (8 << 20)])
    sc.finish()
    rate = len(data) / 1e6 / (time.perf_counter() - t0)
    assert sc.error is None and sc.record_count == 60000
    assert rate >= (15 if core_backend.__name__.endswith("_pycore") else 400), rate


# ---------------------------------------------------------------------------------------
# Reformatter
# ---------------------------------------------------------------------------------------
def refmt(core, data: bytes, indent=None, base_level: int = 0, how="whole") -> bytes:
    f = core.Reformatter(indent, base_level)
    return b"".join(f.feed(p) for p in chunked(data, how)) + f.finish()


def refmt_all(core, data: bytes, indent=None, base_level: int = 0) -> bytes:
    ref = refmt(core, data, indent, base_level)
    for how in ("bytes", 1, 2, 3):
        assert refmt(core, data, indent, base_level, how) == ref, how
    return ref


SRC = b' { "a" : [ 1 , 2 ] ,\n "b" : { } , "c" : [\t] , "d" : { "e" : null } } '


def test_reformatter_compact(core_backend):
    assert refmt_all(core_backend, SRC) == b'{"a":[1,2],"b":{},"c":[],"d":{"e":null}}'
    assert refmt_all(core_backend, SRC, None, 3) == b'{"a":[1,2],"b":{},"c":[],"d":{"e":null}}'


def test_reformatter_indents(core_backend):
    two = b'{\n  "a": [\n    1,\n    2\n  ],\n  "b": {},\n  "c": [],\n  "d": {\n    "e": null\n  }\n}'
    assert refmt_all(core_backend, SRC, 2) == two
    assert refmt_all(core_backend, SRC, 4) == two.replace(b"  ", b"    ")
    assert refmt_all(core_backend, SRC, "\t") == two.replace(b"  ", b"\t")
    assert refmt_all(core_backend, SRC, 0) == two.replace(b"  ", b"")
    assert refmt_all(core_backend, SRC, 16) == two.replace(b"  ", b" " * 16)
    value = json.loads(SRC)
    assert refmt_all(core_backend, SRC, 2) == orjson.dumps(value, option=orjson.OPT_INDENT_2)
    for indent in (4, "\t", 0, 1):
        assert refmt_all(core_backend, SRC, indent) == json.dumps(value, indent=indent).encode()


def test_reformatter_base_level(core_backend):
    assert refmt_all(core_backend, b'{"a":1}', 2, 1) == b'{\n    "a": 1\n  }'
    assert refmt_all(core_backend, b'[1,[2]]', "\t", 2) == b"[\n\t\t\t1,\n\t\t\t[\n\t\t\t\t2\n\t\t\t]\n\t\t]"
    assert refmt_all(core_backend, b"[]", 2, 5) == b"[]"


def test_reformatter_empty_containers(core_backend):
    assert refmt_all(core_backend, b"[ \n ]", 2) == b"[]"
    assert refmt_all(core_backend, b"{\t}", 2) == b"{}"
    assert refmt_all(core_backend, b"[[]]", 2) == b"[\n  []\n]"
    assert refmt_all(core_backend, b"[{}, [ ], {\"a\": {}}]", 2) == b'[\n  {},\n  [],\n  {\n    "a": {}\n  }\n]'


def test_reformatter_strings_untouched(core_backend):
    esc = b"\\" + b"u00e9" + b"\\" + b"/"  # two JSON escapes: e-acute and the solidus
    src = br'{ "x" : "a,b:[c]{d}\"e\\" , "y" : "  spaced \t " , "z" : "' + esc + b'" }'
    assert refmt_all(core_backend, src) == br'{"x":"a,b:[c]{d}\"e\\","y":"  spaced \t ","z":"' + esc + b'"}'
    exp = (b'{\n  ' + br'"x": "a,b:[c]{d}\"e\\"' + b',\n  ' + br'"y": "  spaced \t "' + b',\n  '
           + b'"z": "' + esc + b'"' + b"\n}")
    assert refmt_all(core_backend, src, 2) == exp
    text = '["Тошкент", "😀", "a\\"]"]'.encode()
    assert refmt_all(core_backend, text, None) == text.replace(b", ", b",")
    # chunk boundary exactly after a backslash and inside a multi-byte character
    data = b'["a\\"b", "\xd0\x96"]'
    for cut in range(len(data) + 1):
        f = core_backend.Reformatter(None)
        assert f.feed(data[:cut]) + f.feed(data[cut:]) + f.finish() == b'["a\\"b","\xd0\x96"]'


def test_reformatter_fragments(core_backend):
    prefix = b'{"meta" : {"v":1} , "data" :'
    assert refmt_all(core_backend, prefix, 2) == b'{\n  "meta": {\n    "v": 1\n  },\n  "data": '
    f = core_backend.Reformatter(2)
    assert f.feed(b'{"data":[') == b'{\n  "data": '
    assert f.finish() == b"["  # pending '[' is flushed as is
    assert refmt_all(core_backend, b'],"x":1}', 2) == b'\n],\n"x": 1\n}'
    assert refmt_all(core_backend, b"1 2 \"a\" {} []", 2) == b'12"a"{}[]'
    big = b'"' + b"x" * 100000 + b'"'
    assert refmt(core_backend, b"[" + big + b"]", 2, how=5) == b"[\n  " + big + b"\n]"
    assert refmt(core_backend, b"[123456789, -1.5e+300]", None, how="bytes") == b"[123456789,-1.5e+300]"


def test_reformatter_large_chunks(core_backend):
    """Big single chunks (an implementation may tokenize them in windows internally)."""
    recs = jsongen.records(1500, seed=4)
    data = codec.dumps(recs)
    assert len(data) > 300_000
    assert refmt(core_backend, data, None, how=11) == data
    assert refmt(core_backend, data, "\t") == refmt(core_backend, data, "\t", how=12)
    assert refmt(core_backend, data, 2) == refmt(core_backend, data, 2, how=13)
    assert json.loads(refmt(core_backend, data, 4)) == json.loads(data)
    for k in range(-3, 4):  # strings/escapes ending around 256 KiB
        s = b'["' + b"a" * ((1 << 18) - 2 + k) + b'\\"x", 1, "' + b"b" * 10 + b'", 23456]'
        assert refmt(core_backend, s, None) == s.replace(b", ", b",")
        assert json.loads(refmt(core_backend, s, 2)) == json.loads(s)


def test_reformatter_argument_errors(core_backend):
    R = core_backend.Reformatter
    for bad in (17, -1, "  ", 2.0, True, "x", b"\t"):
        with pytest.raises(ValueError):
            R(bad)
    for bad in (-1, 1.5):
        with pytest.raises(ValueError):
            R(2, bad)
    f = R(2)
    f.feed(b"[1]")
    assert f.finish() == b""
    assert f.finish() == b""
    with pytest.raises(ValueError):
        f.feed(b"1")


# ---------------------------------------------------------------------------------------
# MultiFinder
# ---------------------------------------------------------------------------------------
def test_multifinder_find_first(core_backend):
    mf = core_backend.MultiFinder([b"abc", b"bcd", b"x"])
    buf = b"zzabcdzzx"
    assert mf.find_first(buf) == 2
    assert mf.find_first(buf, 3) == 3
    assert mf.find_first(buf, 4) == 8
    assert mf.find_first(buf, 0, 4) == -1  # "abc" would end at 5 > 4
    assert mf.find_first(buf, 0, 5) == 2   # ends exactly at the bound
    assert mf.find_first(buf, 3, 5) == -1
    assert mf.find_first(buf, 3, 6) == 3
    assert mf.find_first(buf, 9) == -1
    assert mf.find_first(buf, 2, 2) == -1
    assert mf.find_first(buf, 5, 2) == -1
    assert mf.find_first(buf, 0, None) == 2
    assert mf.find_first(b"") == -1
    aa = core_backend.MultiFinder([b"aa"])
    assert aa.find_first(b"baaa") == 1
    assert aa.find_first(b"baaa", 2) == 2
    assert aa.find_first(b"baaa", 3) == -1
    assert core_backend.MultiFinder([b"\xd0\x96", b"\x00"]).find_first(b"ab\x00\xd0\x96") == 2


def test_multifinder_find_in_spans(core_backend):
    mf = core_backend.MultiFinder([b"needle"])
    buf = b"needle--needle--nee--dle"
    assert u64(mf.find_in_spans(buf, pack([(0, 6), (7, 13), (13, 24)]))) == [0]  # crossing match ignored
    assert u64(mf.find_in_spans(buf, pack([(0, 24)]))) == [0]
    assert u64(mf.find_in_spans(buf, pack([(1, 7), (8, 14), (16, 24)]))) == [1]
    assert u64(mf.find_in_spans(buf, pack([(5, 5), (6, 6)]))) == []
    assert mf.find_in_spans(buf, b"") == b""
    # a longer needle crossing a boundary must not hide a shorter one inside the next span
    ab = core_backend.MultiFinder([b"ab", b"b"])
    assert u64(ab.find_in_spans(b"xab", pack([(0, 2), (2, 3)]))) == [1]
    aa = core_backend.MultiFinder([b"aa"])
    assert u64(aa.find_in_spans(b"aaaa", pack([(0, 1), (1, 3), (3, 4)]))) == [1]
    many = b"".join(b"rec%d;" % i for i in range(1000))
    spans = []
    pos = 0
    for i in range(1000):
        n = len(b"rec%d;" % i)
        spans.append((pos, pos + n))
        pos += n
    mf = core_backend.MultiFinder([b"7;", b"c99"])
    expect = [i for i in range(1000) if str(i).endswith("7") or b"c99" in b"rec%d;" % i]
    assert u64(mf.find_in_spans(many, pack(spans))) == expect


def test_multifinder_argument_errors(core_backend):
    M = core_backend.MultiFinder
    for bad in ([], [b""], [b"a", b""]):
        with pytest.raises(ValueError):
            M(bad)
    mf = M([b"a"])
    with pytest.raises(ValueError):
        mf.find_in_spans(b"abc", b"\x00" * 8)
    with pytest.raises(ValueError):
        mf.find_in_spans(b"abc", pack([(0, 4)]))
    with pytest.raises(ValueError):
        mf.find_in_spans(b"abc", pack([(2, 1)]))
    with pytest.raises(ValueError):
        mf.find_first(b"abc", -1)
    with pytest.raises(ValueError):
        mf.find_first(b"abc", 0, 4)


def _case_variants(word: bytes) -> list[bytes]:
    out = [b""]
    for c in word:
        ch = bytes([c])
        out = [v + x for v in out for x in ([ch.lower(), ch.upper()] if ch.isalpha() else [ch])]
    return out


def test_multifinder_case_variant_sets(core_backend):
    """Needle sets holding every ASCII case variant of some words (what a case-insensitive
    query produces) are searched by case folding in the Rust core: same results."""
    needles = _case_variants(b"tosh") + _case_variants(b"k'") + [b"\xc3\x9f", b"--"]
    mf = core_backend.MultiFinder(needles)
    buf = b"xx ToSH \xc3\x9f K' tOsh -- k'"
    assert mf.find_first(buf) == 3
    assert mf.find_first(buf, 4) == 8
    assert mf.find_first(buf, 9) == 11
    assert mf.find_first(buf, 12) == 14
    assert mf.find_first(buf, 14, 17) == -1  # "tOsh" would end at 18
    assert mf.find_first(buf, 14, 18) == 14
    assert mf.find_first(buf, 15) == 19
    assert mf.find_first(buf, 20) == 22
    assert mf.find_first(b"[@`{TOS") == -1  # bytes next to the letter ranges are not letters
    spans = pack([(0, 3), (3, 7), (7, 11), (11, 15), (19, 24)])
    assert u64(mf.find_in_spans(buf, spans)) == [1, 2, 3, 4]
    incomplete = _case_variants(b"ab")[1:]  # not every variant: exact search
    m2 = core_backend.MultiFinder(incomplete)
    hits = [m2.find_first(v + b"!") for v in _case_variants(b"ab")]
    assert hits.count(-1) == 1 and hits.count(0) == 3


# ---------------------------------------------------------------------------------------
# MultiFinder.locate_tokens
# ---------------------------------------------------------------------------------------
BSL = b"\x5c"  # a backslash (kept out of the literals below)


def locate(core, needles, buf, spans, member=False) -> list[tuple[int, int, bytes, tuple]]:
    """``locate_tokens`` with each token's bytes instead of its offsets."""
    got = core.MultiFinder(needles).locate_tokens(buf, spans, member)
    assert isinstance(got, list)
    out = []
    for item in got:
        si, kind, a, b, path = item
        assert type(item) is tuple and all(type(x) is int for x in (si, kind, a, b)) and type(path) is tuple
        out.append((si, kind, buf[a:b], path))
    return out


def test_locate_tokens_kinds(core_backend):
    rec = b'{"key": "a key", "n": [1, 2.5e3, {"t": true}], "nul": null, "f": false, "keyed": "x"}'
    buf, spans = _records(rec)
    assert locate(core_backend, [b"key"], buf, spans) == [
        (0, 1, b'"key"', ("key",)), (0, 0, b'"a key"', ("key",)), (0, 1, b'"keyed"', ("keyed",))]
    assert locate(core_backend, [b"5e"], buf, spans) == [(0, 2, b"2.5e3", ("n", 1))]
    assert locate(core_backend, [b"ru", b"ul", b"als"], buf, spans) == [
        (0, 3, b"true", ("n", 2, "t")), (0, 1, b'"nul"', ("nul",)), (0, 3, b"null", ("nul",)),
        (0, 3, b"false", ("f",))]
    buf, spans = _records(b'"xx"', b'[[1], 21, [], {"a": [3, 1]}]', b" 17 ")
    assert locate(core_backend, [b"x", b"1"], buf, spans) == [
        (0, 0, b'"xx"', ()), (1, 2, b"1", (0, 0)), (1, 2, b"21", (1,)), (1, 2, b"1", (3, "a", 1)),
        (2, 2, b"17", ())]
    # offsets are absolute positions in buf (quotes included)
    buf = b'xx ["a"] {"b": "a"}'
    assert core_backend.MultiFinder([b"a"]).locate_tokens(buf, pack([(3, 8), (9, 19)])) == [
        (0, 0, 4, 7, (0,)), (1, 0, 15, 18, ("b",))]
    # every span on its own: unsorted and overlapping spans are fine
    buf = b'["a", "ba"]'
    assert locate(core_backend, [b"a"], buf, pack([(0, 11), (6, 10), (1, 4)])) == [
        (0, 0, b'"a"', (0,)), (0, 0, b'"ba"', (1,)), (1, 0, b'"ba"', ()), (2, 0, b'"a"', ())]


def test_locate_tokens_escaped_keys_in_paths(core_backend):
    rec = (b'{"' + BSL + b'u00e9t": {"q' + BSL + b'"r": ["x"]}, "' + BSL + b"ud83d" + BSL + b'ude00": "x", "'
           + BSL + b'ud800": "x", "a' + BSL + BSL + b'b": "x", "sl' + BSL + b'/": "x", "' + BSL + b'n": "x"}')
    buf, spans = _records(rec)
    assert [p for _, _, _, p in locate(core_backend, [b'"x"'], buf, spans)] == [
        (chr(0xE9) + "t", 'q"r', 0), (chr(0x1F600),), (chr(0xD800),), ("a" + chr(92) + "b",), ("sl/",), ("\n",)]
    assert json.loads(rec) == {chr(0xE9) + "t": {'q"r': ["x"]}, chr(0x1F600): "x", chr(0xD800): "x",
                               "a" + chr(92) + "b": "x", "sl/": "x", "\n": "x"}


def test_locate_tokens_duplicate_flag(core_backend):
    recs = [
        b'{"a": "x", "a": 1}',                                    # duplicate key
        b'["x", {"b": {"a": 1, "' + BSL + b'u0061": 2}}]',         # equal after decoding, nested
        b'["x", {"a": 1}, {"a": 2}]',                             # one key per object: no flag
        b'{"a": 1, "a": 2, "b": "y"}',                            # nothing located: no flag
        b'{"k": "x", "k": "x"}',                                  # both located, one flag
        b'{"x": [], "' + BSL + b'u0078": {}}',                     # the duplicate itself located
    ]
    buf, spans = _records(*recs)
    got = core_backend.MultiFinder([b"x"]).locate_tokens(buf, spans)
    assert [(si, kind, path) for si, kind, _, _, path in got] == [
        (0, 0, ("a",)), (0, 5, ()),
        (1, 0, (0,)), (1, 5, ()),
        (2, 0, (0,)),
        (4, 0, ("k",)), (4, 0, ("k",)), (4, 5, ()),
        (5, 1, ("x",)), (5, 5, ())]
    assert [g[2:] for g in got if g[1] == 5] == [(0, 0, ())] * 4
    # large objects (more keys than a pairwise comparison handles)
    big = b"{" + b", ".join(b'"k%d": %d' % (i, i) for i in range(40))
    clean = big + b', "z": "x"}'
    dup = big + b', "z": "x", "k' + BSL + b'u0033": 0}'
    buf, spans = _records(clean, dup)
    assert [(si, kind) for si, kind, *_ in core_backend.MultiFinder([b'"x"']).locate_tokens(buf, spans)] == [
        (0, 0), (1, 0), (1, 5)]


def test_locate_tokens_occurrence_positions(core_backend):
    buf, spans = _records(b'{"a": {"b": [1, 2]}}')
    # occurrences starting on structure or whitespace locate nothing
    assert core_backend.MultiFinder([b'{"', b", ", b": ", b"]}", b" "]).locate_tokens(buf, spans) == []
    # an occurrence starting in one token and ending in the next belongs to the first
    assert locate(core_backend, [b"1, 2"], buf, spans) == [(0, 2, b"1", ("a", "b", 0))]
    assert locate(core_backend, [b'a": {"b'], buf, spans) == [(0, 1, b'"a"', ("a",))]
    # the quotes belong to the token
    assert locate(core_backend, [b'"b'], buf, spans) == [(0, 1, b'"b"', ("a", "b"))]
    assert locate(core_backend, [b"2]"], buf, spans) == [(0, 2, b"2", ("a", "b", 1))]
    # overlapping needles and occurrences: every token once
    buf, spans = _records(b'["aaaa", "a", "aa", "baab"]')
    assert locate(core_backend, [b"aa", b"aaa"], buf, spans) == [
        (0, 0, b'"aaaa"', (0,)), (0, 0, b'"aa"', (2,)), (0, 0, b'"baab"', (3,))]
    # the second occurrence overlaps the first one, which crosses into the next token
    buf, spans = _records(b'["ab", "bc"]')
    assert locate(core_backend, [b'b", "', b'"bc'], buf, spans) == [(0, 0, b'"ab"', (0,)), (0, 0, b'"bc"', (1,))]
    # only occurrences lying entirely inside the span count (one ending at its end does)
    buf = b'["xy"]["xy"]'
    assert locate(core_backend, [b'y"]'], buf, pack([(0, 6), (6, 12)])) == [
        (0, 0, b'"xy"', (0,)), (1, 0, b'"xy"', (0,))]
    assert locate(core_backend, [b'y"]['], buf, pack([(0, 6), (6, 12)])) == []


def test_locate_tokens_member_spans(core_backend):
    doc = b'{"ab": {"c": ["ab"]}, "x" : 1, "' + BSL + b'u0061b": "ab"}'
    spans = core_backend.Scanner(MODE_CONTAINER).feed(doc)
    assert locate(core_backend, [b"ab"], doc, spans, member=True) == [
        (0, 4, b'"ab"', ()), (0, 0, b'"ab"', ("c", 0)), (2, 0, b'"ab"', ())]
    assert locate(core_backend, [b"x", b"1"], doc, spans, member=True) == [
        (1, 4, b'"x"', ()), (1, 2, b"1", ()), (2, 4, b'"' + BSL + b'u0061b"', ())]  # raw bytes: "0061"
    with pytest.raises(ValueError):
        core_backend.MultiFinder([b"x"]).locate_tokens(doc, spans)  # a member is not a value


def test_locate_tokens_every_token_is_the_parsed_walk(core_backend):
    """With a needle at the first byte of every token, every key and scalar is located:
    the same list (kinds, paths, texts) as a document-order walk of the parsed record."""
    every = [b'"', b"-", b"t", b"f", b"n"] + [bytes([c]) for c in b"0123456789"]

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                yield 1, path + (k,), k
                yield from walk(x, path + (k,))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                yield from walk(x, path + (i,))
        elif isinstance(v, str):
            yield 0, path, v
        elif v is None or isinstance(v, bool):
            yield 3, path, json.dumps(v)
        else:
            yield 2, path, codec.number_text(v)

    rng = random.Random(77)
    for i in range(60):
        value = jsongen.rand_value(rng, 0, 5)
        rec = jsongen.encode(value, ("compact", "pretty2", "messy", "escaped")[i % 4], seed=i)
        buf, spans = _records(rec)
        got = [(kind, path, codec.loads(tok) if kind < 2 else tok.decode())
               for _, kind, tok, path in locate(core_backend, every, buf, spans)]
        assert got == list(walk(codec.loads(rec), ())), rec


def test_locate_tokens_errors(core_backend):
    mf = core_backend.MultiFinder([b"a"])
    for rec in (b'{"a":1', b"[1,]", b'{"a":1} x', b'"\xff"', b"", b"  ", b"{'a': 1}", b"NaN", b'{"a":01}',
                b'["a" "b"]', b'{"a" 1}', b"[-]", b'"a\x01"'):
        buf, spans = _records(rec)
        with pytest.raises(ValueError):
            mf.locate_tokens(buf, spans)
    buf, spans = _records(b'["b"]', b"[1,]")  # spans without an occurrence are validated too
    with pytest.raises(ValueError):
        mf.locate_tokens(buf, spans)
    with pytest.raises(ValueError):
        mf.locate_tokens(b"[1]", pack([(0, 3)]), True)  # not a member
    with pytest.raises(ValueError):
        mf.locate_tokens(b"{}", pack([(0, 3)]))
    with pytest.raises(ValueError):
        mf.locate_tokens(b"{}", pack([(2, 1)]))
    with pytest.raises(ValueError):
        mf.locate_tokens(b"{}", b"\x00" * 8)
    with pytest.raises(TypeError):
        mf.locate_tokens("{}", pack([(0, 2)]))
    assert mf.locate_tokens(b"{}", b"") == []
    assert mf.locate_tokens(b"{}", pack([(0, 2)])) == []
    assert mf.locate_tokens(bytearray(b'["a"]'), memoryview(pack([(0, 5)]))) == [(0, 0, 1, 4, (0,))]
    deep = b"[" * 3000 + b'"a"' + b"]" * 3000
    assert mf.locate_tokens(deep, pack([(0, len(deep))])) == [(0, 0, 3000, 3003, (0,) * 3000)]
    too_deep = b"[" * (DEFAULT_MAX_DEPTH + 1) + b"]" * (DEFAULT_MAX_DEPTH + 1)
    with pytest.raises(ValueError):
        mf.locate_tokens(too_deep, pack([(0, len(too_deep))]))


# ---------------------------------------------------------------------------------------
# PathExtractor
# ---------------------------------------------------------------------------------------
REC0 = b'{"id": 1, "name": "A", "tags": ["x", "y"], "contact": {"city": "T", "zip": 100}}'
REC1 = b'{"id": 2, "id": 3, "tags": [], "contact": null, "\\u0030": "zero"}'
REC2 = b'[10, [20, 21], {"a": 1}]'


def _records(*recs: bytes) -> tuple[bytes, bytes]:
    buf = b"\n".join(recs)
    spans, pos = [], 0
    for r in recs:
        spans.append((pos, pos + len(r)))
        pos += len(r) + 1
    return buf, pack(spans)


def extract(core, paths, buf, spans, member=False) -> list[tuple[int, int, bytes]]:
    pe = core.PathExtractor(paths, member)
    return [(s, p, buf[a:b]) for s, p, a, b in quads(pe.extract(buf, spans))]


def test_path_extractor_segments(core_backend):
    buf, spans = _records(REC0, REC1, REC2)
    paths = [
        [("key", "id")],
        [("key", "tags"), ("any_index",)],
        [("key", "contact"), ("key", "city")],
        [("any_key",)],
        [("index", 1), ("index", 0)],
        [("key", "1"), ("any_index",)],
        [],
    ]
    assert extract(core_backend, paths, buf, spans) == [
        (0, 0, b"1"), (0, 1, b'"x"'), (0, 1, b'"y"'), (0, 2, b'"T"'),
        (0, 3, b"1"), (0, 3, b'"A"'), (0, 3, b'["x", "y"]'), (0, 3, b'{"city": "T", "zip": 100}'),
        (0, 6, REC0),
        (1, 0, b"3"),
        (1, 3, b"3"), (1, 3, b"[]"), (1, 3, b"null"), (1, 3, b'"zero"'),
        (1, 6, REC1),
        (2, 4, b"20"), (2, 5, b"20"), (2, 5, b"21"), (2, 6, REC2),
    ]
    pe = core_backend.PathExtractor(paths)
    q = quads(pe.extract(buf, spans))
    assert q[0] == (0, 0, REC0.index(b"1"), REC0.index(b"1") + 1)  # absolute positions in buf


def test_path_extractor_duplicates_and_escapes(core_backend):
    rec = b'{"a": 1, "b": {"x": 2}, "a": [3], "\\u0062": {"y": 4}}'
    buf, spans = _records(rec)
    got = extract(core_backend, [[("key", "a")], [("any_key",)], [("key", "b"), ("key", "x")],
                                 [("key", "b"), ("key", "y")]], buf, spans)
    assert got == [(0, 0, b"[3]"), (0, 1, b"[3]"), (0, 1, b'{"y": 4}'), (0, 3, b"4")]
    buf, spans = _records(b'{"a": 1, "b": 2, "a": 3}')
    pe = core_backend.PathExtractor([[("any_key",)]])
    q = quads(pe.extract(buf, spans))
    assert [buf[a:b] for _, _, a, b in q] == [b"3", b"2"]
    assert q[0][2] > q[1][2]  # dict order (first appearance), not document order of the values
    rec = '{"é": 1, "\\u00e9": 2, "\\ud83d\\ude00": 3}'.encode()
    buf, spans = _records(rec)
    assert extract(core_backend, [[("key", "é")], [("key", "\U0001F600")]], buf, spans) == [
        (0, 0, b"2"), (0, 1, b"3")]


def test_path_extractor_index_coercion_and_nesting(core_backend):
    buf, spans = _records(b'[["a", "b"], ["c"], 7]', b'{"1": "one", "01": "z"}')
    paths = [[("key", "1")], [("key", "01")], [("key", "-1")], [("key", "1.0")], [("index", 0), ("index", 1)],
             [("any_index",), ("any_index",)], [("index", 5)], [("key", "0"), ("key", "0")]]
    assert extract(core_backend, paths, buf, spans) == [
        (0, 0, b'["c"]'), (0, 4, b'"b"'), (0, 5, b'"a"'), (0, 5, b'"b"'), (0, 5, b'"c"'), (0, 7, b'"a"'),
        (1, 0, b'"one"'), (1, 1, b'"z"'),
    ]
    deep = b"[" * 30 + b'{"k": [1, 2]}' + b"]" * 30
    buf, spans = _records(deep, b" 5 ")
    paths = [[("index", 0)] * 30 + [("key", "k"), ("index", 1)], []]
    assert extract(core_backend, paths, buf, spans) == [(0, 0, b"2"), (0, 1, deep), (1, 1, b"5")]


def test_path_extractor_member_spans(core_backend):
    doc = b'{"a": {"x": 1}, "b" : [1, 2], "c": 3, "\\u0064": {"x": [5]}}'
    spans = core_backend.Scanner(MODE_CONTAINER).feed(doc)
    paths = [[("key", "x")], [("index", 1)], []]
    assert extract(core_backend, paths, doc, spans, member=True) == [
        (0, 0, b"1"), (0, 2, b'{"x": 1}'), (1, 1, b"2"), (1, 2, b"[1, 2]"), (2, 2, b"3"),
        (3, 0, b"[5]"), (3, 2, b'{"x": [5]}'),
    ]


def test_path_extractor_errors(core_backend):
    P = core_backend.PathExtractor
    for bad in ([("key", 1)], [("index", -1)], [("index", True)], [("index", "1")], [("nope",)],
                [("any_key", 1)], [("any_index", 0)], ["key"], [()], "key"):
        with pytest.raises(ValueError):
            P([bad] if not isinstance(bad, str) else bad)
    pe = P([[("key", "a")]])
    for rec in (b'{"a":1', b"[1,]", b'{"a":1} x', b'"\xff"', b"", b"  ", b"{'a': 1}", b"NaN", b'{"a":01}'):
        buf, spans = _records(rec)
        with pytest.raises(ValueError):
            pe.extract(buf, spans)
    with pytest.raises(ValueError):
        P([[]], True).extract(b"[1]", pack([(0, 3)]))  # not a member
    with pytest.raises(ValueError):
        pe.extract(b"{}", pack([(0, 3)]))
    with pytest.raises(ValueError):
        pe.extract(b"{}", b"\x00" * 8)
    assert P([]).extract(b"{}", pack([(0, 2)])) == b""
    assert pe.extract(b"{}", b"") == b""


# ---------------------------------------------------------------------------------------
# numbers_plain
# ---------------------------------------------------------------------------------------
def _is_plain(tok: str) -> bool:
    return codec.is_plain_int_token(tok) or codec.is_plain_float_token(tok)


EXTRA_TOKENS = ["0.5", "-0.5", "10.5", "100.50", "0.00001", "0.000001", "0.0001234567890123",
                "123456789012345.6", "12345678901234.5", "1234567890123450.0", "1234567890123456.0",
                "12345678901234560.0", "999999999999999999", "1000000000000000000", "-999999999999999999",
                "0.123456789012345", "0.1234567890123456", "1.0", "1.00", "-0.0", "5.0e1", "1e0", "1E5"]


def test_numbers_plain_edge_tokens(core_backend):
    np_ = core_backend.numbers_plain
    for tok in jsongen.EDGE_NUMBER_TOKENS + EXTRA_TOKENS:
        expected = _is_plain(tok)
        assert np_(tok.encode()) is expected, tok
        doc = '{"a": [%s, "1e400", {"k": %s}], "s": "1.10", "t": true}' % (tok, tok)
        assert np_(doc.encode()) is expected, tok
        assert np_((' "key" :\n %s ' % tok).encode()) is expected, tok
        if expected:  # the spec's guarantee: orjson reproduces plain tokens exactly
            assert orjson.dumps(orjson.loads(tok)) == tok.encode(), tok
    assert np_(b'["1e5", "-0", true, null, {}]') is True
    assert np_(b'[1, 2.5, 1e5]') is False
    assert np_(b'"\\"1e5"') is True


def _random_number_token(rng: random.Random) -> str:
    sign = "-" if rng.random() < 0.3 else ""
    if rng.random() < 0.2:
        ip = "0"
    else:
        n = rng.choice((1, 2, 5, 14, 15, 16, 17, 18, 19, 20, 25))
        ip = str(rng.randint(1, 9)) + "".join(rng.choice("0123456789") for _ in range(n - 1))
        if rng.random() < 0.2:
            ip = ip[:-3] + "000" if len(ip) > 3 else ip
    tok = sign + ip
    if rng.random() < 0.6:
        lead = "0" * rng.choice((0, 0, 1, 3, 4, 5, 6))
        body = "".join(rng.choice("0123456789") for _ in range(rng.choice((0, 1, 2, 10, 13, 14, 15, 16))))
        frac = lead + body
        if rng.random() < 0.2:
            frac += "0"
        tok += "." + (frac or "0")
    if rng.random() < 0.15:
        tok += rng.choice("eE") + rng.choice(("", "+", "-")) + str(rng.randint(0, 400))
    return tok


def test_numbers_plain_random_tokens(core_backend):
    rng = random.Random(7)
    np_ = core_backend.numbers_plain
    for _ in range(4000):
        tok = _random_number_token(rng)
        assert np_(tok.encode()) is _is_plain(tok), tok
        assert np_(b"[" + tok.encode() + b", 1]") is _is_plain(tok), tok
