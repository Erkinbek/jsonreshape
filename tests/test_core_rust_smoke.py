"""Smoke tests for the Rust core (``jsonreshape.core._rscore``) against the spec.

These tests use explicit expected values derived from docs/ARCHITECTURE.md §1 (they do
not need the Python reference). Every Scanner case is also checked for chunk-split
invariance (whole input, byte-by-byte feeding, a 2-way split at every position). The
backend-parametrized conformance suite (tests/test_core.py, tests/test_core_fuzz.py)
compares both backends with each other.

Skipped when the Rust extension is unavailable (e.g. ``JSONRESHAPE_BACKEND=python``).

Note: escape sequences are built at run time (``BS + b"u00e9"``) instead of being
written literally in this file.
"""

from __future__ import annotations

import json
import os
import random
import struct
import subprocess
import sys
import zlib
from array import array

import pytest

from jsonreshape import core
from jsonreshape.core import constants as C

try:
    R = core.get_backend("rust")
except ImportError as exc:  # pragma: no cover - depends on the build
    pytest.skip(f"Rust core unavailable: {exc}", allow_module_level=True)

import jsongen  # noqa: E402  (tests/ is on sys.path via conftest)

BS = b"\x5c"  # backslash
U = BS + b"u"  # start of a \uXXXX escape

ALL_MODES = (C.MODE_CONTAINER, C.MODE_BRACE_LIST, C.MODE_LINES, C.MODE_STREAM, C.MODE_SINGLE)


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------
def pairs(raw: bytes) -> list[tuple[int, int]]:
    a = array("Q", raw)
    return list(zip(a[0::2], a[1::2]))


def u64s(raw: bytes) -> list[int]:
    return array("Q", raw).tolist()


def span_bytes(spans: list[tuple[int, int]]) -> bytes:
    return array("Q", [v for sp in spans for v in sp]).tobytes()


def run_scanner(data: bytes, mode: int, chunks: list[bytes] | None = None, **kw) -> dict:
    s = R.Scanner(mode, **kw)
    out = b""
    broken = []
    for c in chunks if chunks is not None else [data]:
        out += s.feed(c)
        broken += s.take_broken()
    out += s.finish()
    broken += s.take_broken()
    return {
        "spans": pairs(out),
        "error": s.error,
        "broken": broken,
        "flags": s.flags,
        "lines": s.lines,
        "record_count": s.record_count,
        "target_found": s.target_found,
        "container_span": s.container_span,
        "finished": s.finished,
        "consumed": s.consumed,
    }


def scan(data: bytes, mode: int = C.MODE_CONTAINER, **kw) -> dict:
    """Scan whole; assert byte-by-byte and every 2-way split give identical results."""
    whole = run_scanner(data, mode, None, **kw)
    assert run_scanner(data, mode, [data[i:i + 1] for i in range(len(data))], **kw) == whole
    for k in range(len(data) + 1):
        assert run_scanner(data, mode, [data[:k], data[k:]], **kw) == whole, f"split at {k}"
    assert whole["record_count"] == len(whole["spans"])
    assert whole["finished"] is True
    return whole


def err(data: bytes, mode: int = C.MODE_CONTAINER, **kw):
    return scan(data, mode, **kw)["error"]


# ---------------------------------------------------------------------------------
# module
# ---------------------------------------------------------------------------------
def test_module_constants_match_python():
    for name in dir(C):
        if name.startswith(("MODE_", "FLAG_", "ERR_")) or name == "DEFAULT_MAX_DEPTH":
            assert getattr(R, name) == getattr(C, name), name
    for api in ("Scanner", "Reformatter", "MultiFinder", "PathExtractor", "numbers_plain"):
        assert hasattr(R, api)


# ---------------------------------------------------------------------------------
# Scanner: records
# ---------------------------------------------------------------------------------
def test_container_root_array():
    r = scan(b'[1, "a", {"b": [true]}]')
    assert r["spans"] == [(1, 2), (4, 7), (9, 22)]
    assert r["error"] is None
    assert r["target_found"] is True
    assert r["container_span"] == (0, 23)
    assert r["consumed"] == 23
    assert (r["lines"], r["flags"]) == (0, 0)


def test_container_root_object_member_spans():
    r = scan(b'{"a": 1, "b": [2]}')
    assert r["spans"] == [(1, 7), (9, 17)]
    assert r["container_span"] == (0, 18)


def test_container_empty_and_whitespace():
    r = scan(b" \r\n\t[ ]\n ")
    assert r["spans"] == [] and r["error"] is None
    assert r["container_span"] == (4, 7)
    assert r["lines"] == 2


def test_base_offset():
    r = scan(b"[1]", base_offset=100)
    assert r["spans"] == [(101, 102)]
    assert r["container_span"] == (100, 103)
    assert r["consumed"] == 103
    assert err(b"[x", base_offset=3) == (4, "unexpected_char", "value_or_end", 0, 3)
    assert err(b"[\n]x", base_offset=10) == (13, "trailing_data", "end", 1, 12)


def test_target_path_keys_indices_and_duplicates():
    doc = b'{"meta": {"data": [0]}, "data": [10, 20], "data": [30]}'
    r = scan(doc, target_path=["data"])
    p = doc.index(b"10")
    assert r["spans"] == [(p, p + 2), (p + 4, p + 6)]
    assert r["container_span"] == (doc.index(b"[10"), doc.index(b"]", p) + 1)
    doc = b'{"data": [{"x": [1]}, {"x": [2, 3]}]}'
    assert scan(doc, target_path=["data", 1, "x"])["spans"] == [(29, 30), (32, 33)]
    # intermediate duplicates: every value on the path is followed, the first full match wins
    doc = b'{"a": {"x": 1}, "a": {"b": [5]}, "a": {"b": [6]}}'
    r = scan(doc, target_path=["a", "b"])
    assert r["spans"] == [(doc.index(b"5"), doc.index(b"5") + 1)]


def test_target_object_members():
    doc = b'{"o": {"k": 1, "l": [2]}}'
    assert scan(doc, target_path=["o"])["spans"] == [(7, 13), (15, 23)]


def test_target_key_decoding():
    # escaped key
    doc = b'{"d' + U + b'0061ta": [7]}'
    assert scan(doc, target_path=["data"])["spans"] == [(15, 16)]
    # non-ASCII key
    doc = '{"шаҳар": [1, 2]}'.encode()
    r = scan(doc, target_path=["шаҳар"])
    assert len(r["spans"]) == 2 and r["error"] is None
    # surrogate pair escape == astral character
    doc = b'{"' + U + b"d83d" + U + b'de00": [7]}'
    assert scan(doc, target_path=[chr(0x1F600)])["error"] is None
    # lone surrogate escape == lone surrogate in the Python string
    doc = b'{"' + U + b'd800": [7]}'
    assert scan(doc, target_path=[chr(0xD800)])["error"] is None
    assert scan(doc, target_path=["x"])["error"][1] == "target_not_found"


def test_target_errors():
    assert err(b'{"a": 5}', target_path=["a"]) == (6, "target_not_container", "none", 0, 0)
    assert err(b'{"a": 5, "a": [1]}', target_path=["a"])[1] == "target_not_container"
    assert err(b'{"b": [1]}\n', target_path=["a"]) == (11, "target_not_found", "none", 1, 11)
    # str never matches an array element, int never matches an object member
    assert err(b'{"0": [1]}', target_path=[0])[1] == "target_not_found"
    assert err(b"[[1]]", target_path=["0"])[1] == "target_not_found"
    assert err(b"5")[:3] == (0, "target_not_container", "none")
    # the whole document is validated after the target
    r = scan(b'{"a": [1], "b": x}', target_path=["a"])
    assert r["spans"] == [(7, 8)] and r["error"] == (16, "unexpected_char", "value", 0, 0)
    assert r["container_span"] == (6, 9)


@pytest.mark.parametrize(
    "doc, expected",
    [
        (b"[01]", (2, "unexpected_char", "comma_or_bracket")),
        (b"[1x]", (2, "unexpected_char", "comma_or_bracket")),
        (b"[-]", (2, "invalid_number", "digit")),
        (b"[-a]", (2, "invalid_number", "digit")),
        (b"[tru]", (4, "invalid_literal", "literal")),
        (b"[nul1]", (4, "invalid_literal", "literal")),
        (b"[fx]", (2, "invalid_literal", "literal")),
        (b"[1.]", (3, "invalid_number", "digit")),
        (b"[1.e5]", (3, "invalid_number", "digit")),
        (b"[1e]", (3, "invalid_number", "digit")),
        (b"[1e+]", (4, "invalid_number", "digit")),
        (b'["' + BS + b'x"]', (3, "invalid_escape", "escape")),
        (b'["' + U + b'12G4"]', (6, "invalid_hex", "hex")),
        (b'["a\tb"]', (3, "control_char", "string_end")),
        (b'["a\x00"]', (3, "control_char", "string_end")),
        (b'["\xff"]', (2, "invalid_utf8", "string_end")),
        (b'["ok\xc3("]', (4, "invalid_utf8", "string_end")),
        (b'["\xe0\x80\x80"]', (2, "invalid_utf8", "string_end")),
        (b'["\xed\xa0\x80"]', (2, "invalid_utf8", "string_end")),
        (b'["\xf4\x90\x80\x80"]', (2, "invalid_utf8", "string_end")),
        (b'["\xc0\xaf"]', (2, "invalid_utf8", "string_end")),
        (b"[1] x", (4, "trailing_data", "end")),
        (b"[1]]", (3, "trailing_data", "end")),
        (b"[1,]", (3, "unexpected_char", "value")),
        (b'{"a":1,}', (7, "unexpected_char", "key")),
        (b'{"a" 1}', (5, "unexpected_char", "colon")),
        (b"{1:2}", (1, "unexpected_char", "key_or_end")),
        (b'{"a":1 "b":2}', (7, "unexpected_char", "comma_or_brace")),
        (b"[}", (1, "unexpected_char", "value_or_end")),
        (b'{"a":]', (5, "unexpected_char", "value")),
        (b"[\xc3\xa9]", (1, "unexpected_char", "value_or_end")),
        (b"\x0c[]", (0, "unexpected_char", "value")),
        (b"[", (1, "unexpected_eof", "value_or_end")),
        (b"{", (1, "unexpected_eof", "key_or_end")),
        (b"[1,", (3, "unexpected_eof", "value")),
        (b'{"a"', (4, "unexpected_eof", "colon")),
        (b'{"a":', (5, "unexpected_eof", "value")),
        (b'{"a":1', (6, "unexpected_eof", "comma_or_brace")),
        (b'{"a":1,', (7, "unexpected_eof", "key")),
        (b"[1", (2, "unexpected_eof", "comma_or_bracket")),
        (b'["ab', (4, "unexpected_eof", "string_end")),
        (b'["a' + BS, (4, "unexpected_eof", "escape")),
        (b'["' + U + b"12", (6, "unexpected_eof", "hex")),
        (b"[-", (2, "unexpected_eof", "digit")),
        (b"[1.", (3, "unexpected_eof", "digit")),
        (b"[1e-", (4, "unexpected_eof", "digit")),
        (b"[tr", (3, "unexpected_eof", "literal")),
        (b"", (0, "unexpected_eof", "value")),
        (b"   ", (3, "unexpected_eof", "value")),
    ],
)
def test_container_errors(doc, expected):
    e = err(doc)
    assert e is not None and e[:3] == expected
    assert e[3:] == (0, 0)


def test_error_line_and_line_start():
    assert err(b"[\n1,\n  x]") == (7, "unexpected_char", "value", 2, 5)
    assert err(b"[\r\n1\r\n") == (6, "unexpected_eof", "comma_or_bracket", 2, 6)


def test_records_before_error_are_kept_and_scanner_stops():
    s = R.Scanner(C.MODE_CONTAINER)
    out = s.feed(b"[1, 2, x, 3]\n")
    assert pairs(out) == [(1, 2), (4, 5)]
    assert s.error == (7, "unexpected_char", "value", 0, 0)
    assert s.finished is True
    assert s.feed(b"[4]\n\n") == b""
    # consumed and lines keep counting every byte passed to feed()
    assert (s.consumed, s.lines) == (18, 3)
    assert s.finish() == b""
    assert s.finish() == b""
    assert s.error == (7, "unexpected_char", "value", 0, 0)
    assert s.record_count == 2
    with pytest.raises(ValueError):
        s.feed(b"1")
    with pytest.raises(TypeError):
        s.feed("text")


def test_feed_after_finish_is_value_error():
    for mode in ALL_MODES:
        s = R.Scanner(mode)
        s.feed(b"[1]")
        s.finish()
        with pytest.raises(ValueError):
            s.feed(b"")
        assert s.finish() == b""


def test_depth_limit():
    assert err(b"[[[]]]", max_depth=2) == (2, "depth_limit", "none", 0, 0)
    assert scan(b"[[]]", max_depth=2)["error"] is None
    deep = b"[" * 100001 + b"]" * 100001
    assert run_scanner(deep, C.MODE_SINGLE)["error"] == (100000, "depth_limit", "none", 0, 0)
    ok = b"[" * 100000 + b"]" * 100000
    assert run_scanner(ok, C.MODE_SINGLE)["error"] is None


def test_single_mode():
    assert scan(b" 123 ", C.MODE_SINGLE)["spans"] == [(1, 4)]
    assert scan(b'"x"', C.MODE_SINGLE)["spans"] == [(0, 3)]
    r = scan(b"1 2", C.MODE_SINGLE)
    assert r["spans"] == [(0, 1)] and r["error"] == (2, "trailing_data", "end", 0, 0)
    assert err(b"", C.MODE_SINGLE) == (0, "unexpected_eof", "value", 0, 0)
    assert scan(b"[1]", C.MODE_SINGLE)["container_span"] is None
    assert scan(b"[1]", C.MODE_SINGLE)["target_found"] is False


def test_stream_mode():
    r = scan(b'1 2 [3]{"a":4}"s"truefalse1-2\n', C.MODE_STREAM)
    assert r["error"] is None
    assert r["spans"] == [(0, 1), (2, 3), (4, 7), (7, 14), (14, 17), (17, 21), (21, 26), (26, 27), (27, 29)]
    assert scan(b"", C.MODE_STREAM)["spans"] == [] and scan(b" \n", C.MODE_STREAM)["error"] is None
    r = scan(b"1 ]", C.MODE_STREAM)
    assert r["spans"] == [(0, 1)] and r["error"] == (2, "unexpected_char", "value", 0, 0)
    assert err(b"[1", C.MODE_STREAM) == (2, "unexpected_eof", "comma_or_bracket", 0, 0)


def test_brace_list_mode():
    r = scan(b'{ {"a":1}, [2], 3 }', C.MODE_BRACE_LIST)
    assert r["spans"] == [(2, 9), (11, 14), (16, 17)] and r["error"] is None
    assert scan(b"{}", C.MODE_BRACE_LIST)["spans"] == []
    assert err(b"", C.MODE_BRACE_LIST) == (0, "unexpected_eof", "open_brace", 0, 0)
    assert err(b"[1]", C.MODE_BRACE_LIST) == (0, "unexpected_char", "open_brace", 0, 0)
    assert err(b"{1,}", C.MODE_BRACE_LIST)[:3] == (3, "unexpected_char", "value")
    assert err(b"{1", C.MODE_BRACE_LIST)[:3] == (2, "unexpected_eof", "comma_or_brace")
    assert err(b"{1} x", C.MODE_BRACE_LIST)[:3] == (4, "trailing_data", "end")
    assert err(b"{]", C.MODE_BRACE_LIST)[:3] == (1, "unexpected_char", "value_or_end")
    r = scan(b'{"a":1}', C.MODE_BRACE_LIST)
    assert r["spans"] == [(1, 4)] and r["error"] == (4, "unexpected_char", "comma_or_brace", 0, 0)


def test_lines_mode_records_and_broken_lines():
    doc = b'{"a":1}\n{"b":\n[1] x\n"ok"'
    r = scan(doc, C.MODE_LINES)
    assert r["error"] is None
    assert r["spans"] == [(0, 7), (20, 24)]
    assert r["broken"] == [
        (1, 8, 13, 13, "unexpected_eof", "value"),
        (2, 14, 19, 18, "trailing_data", "end"),
    ]
    assert r["lines"] == 3
    r = scan(b'[2]\r\n\n  3  \n"ab\r\n12\n-\n', C.MODE_LINES)
    assert r["spans"] == [(0, 3), (8, 9), (17, 19)]
    assert r["broken"] == [
        (3, 12, 16, 15, "control_char", "string_end"),
        (5, 20, 21, 21, "unexpected_eof", "digit"),
    ]
    r = scan(b'"abc\n[1', C.MODE_LINES)
    assert r["broken"] == [(0, 0, 4, 4, "unexpected_eof", "string_end"), (1, 5, 7, 7, "unexpected_eof", "comma_or_bracket")]
    r = scan(b" \n\r\n\t", C.MODE_LINES)
    assert r["spans"] == [] and r["broken"] == []


def test_lines_take_broken_clears_and_error_stays_none():
    s = R.Scanner(C.MODE_LINES)
    assert pairs(s.feed(b"x\n1\n")) == [(2, 3)]
    assert s.take_broken() == [(0, 0, 1, 0, "unexpected_char", "value")]
    assert s.take_broken() == []
    s.feed(b"[")
    assert s.take_broken() == []  # line not finished yet
    s.finish()
    assert s.take_broken() == [(2, 4, 5, 5, "unexpected_eof", "value_or_end")]
    assert s.error is None and s.finished is True
    assert s.lines == 2


def test_flags():
    def flags(doc: bytes) -> int:
        return scan(doc, C.MODE_SINGLE)["flags"]

    assert flags(b'["plain", 1]') == 0
    assert flags(b'["a' + BS + b'nb"]') == C.FLAG_ESCAPES
    assert flags(b'["' + U + b'00e9"]') == C.FLAG_ESCAPES | C.FLAG_U_ESCAPES
    assert flags('["é"]'.encode()) == C.FLAG_NON_ASCII
    assert flags(b'{"' + BS + b't":1}') == C.FLAG_ESCAPES  # keys count too
    assert flags(('{"ключ' + "\\" + 'n":"é"}').encode()) == C.FLAG_ESCAPES | C.FLAG_NON_ASCII
    # only complete constructs set flags; a construct cut by an error (or EOF) sets nothing
    assert flags(b'["a' + BS + b'x"]') == 0
    assert flags(b'["a' + BS) == 0
    assert flags(b'["' + U + b"12") == 0
    assert flags(b'["' + U + b'12G4"]') == 0
    assert flags(b'["\xc3') == 0
    assert flags(b'["\xc3("]') == 0
    assert flags(b'["\xff"]') == 0
    assert flags(b'["' + BS + b"n") == C.FLAG_ESCAPES
    # MODE_LINES keeps the flags of content before an error on a broken line
    assert scan(b'["\xc3\xa9", x]\n', C.MODE_LINES)["flags"] == C.FLAG_NON_ASCII


def test_eof_inside_utf8_sequence_is_unexpected_eof():
    assert err(b'["\xc3') == (3, "unexpected_eof", "string_end", 0, 0)
    assert err(b'["\xf0\x9f\x98') == (5, "unexpected_eof", "string_end", 0, 0)
    assert err(b'["\xc3\n"]') == (2, "invalid_utf8", "string_end", 0, 0)
    r = scan(b'"\xc3\n1\n', C.MODE_LINES)
    assert r["spans"] == [(3, 4)]
    assert r["broken"] == [(0, 0, 2, 2, "unexpected_eof", "string_end")]


def test_utf8_split_across_chunks():
    text = 'ab"Тошкент 😀 Oʻzbekiston'
    doc = json.dumps([text], ensure_ascii=False).encode()
    r = scan(doc)
    assert r["error"] is None and r["flags"] == C.FLAG_NON_ASCII | C.FLAG_ESCAPES
    doc = b'["' + "Тош".encode()[:-1] + b'"]'  # truncated 2-byte sequence before the quote
    assert err(doc)[:3] == (6, "invalid_utf8", "string_end")


def test_invalid_arguments():
    for bad in (0, 6, -1, "x", None, 1.5, True):
        with pytest.raises(ValueError):
            R.Scanner(bad)
    for kw in ({"base_offset": -1}, {"base_offset": 1.0}, {"base_offset": True}, {"max_depth": 0},
               {"max_depth": True}, {"max_depth": 2.0}):
        with pytest.raises(ValueError):
            R.Scanner(C.MODE_CONTAINER, **kw)
    for tp in ([1.5], [None], [-1], "data", [b"x"], [["a"]], [True], {"a"}, iter(["a"])):
        with pytest.raises(ValueError):
            R.Scanner(C.MODE_CONTAINER, target_path=tp)
    for mode in (C.MODE_SINGLE, C.MODE_LINES, C.MODE_STREAM, C.MODE_BRACE_LIST):
        with pytest.raises(ValueError):
            R.Scanner(mode, ["a"])
        R.Scanner(mode, [])
    R.Scanner(C.MODE_CONTAINER, ["a", 0], 0, 10)  # positional form
    R.Scanner(C.MODE_CONTAINER, target_path=("a", 0))
    R.Scanner(C.MODE_CONTAINER, target_path=None)
    with pytest.raises(TypeError):
        R.Scanner(C.MODE_CONTAINER).feed("text")
    assert R.Scanner(C.MODE_SINGLE).take_broken() == []


def test_bytes_like_inputs():
    s = R.Scanner(C.MODE_CONTAINER)
    out = s.feed(bytearray(b"[1,")) + s.feed(memoryview(b"2]")) + s.finish()
    assert pairs(out) == [(1, 2), (3, 4)]
    # typed buffers (e.g. span arrays) are taken as raw bytes, like bytes(obj)
    buf = b"[1, 22]"
    spans = array("Q", [1, 2, 4, 6])
    assert u64s(R.MultiFinder([b"2"]).find_in_spans(buf, spans)) == [1]
    assert len(R.PathExtractor([[]]).extract(buf, spans)) == 64
    assert R.numbers_plain(memoryview(b"[1]")) is True


# ---------------------------------------------------------------------------------
# Scanner: generated documents in every format (records round-trip, any chunking)
# ---------------------------------------------------------------------------------
def _expected_spans(data: bytes, pieces: list[bytes]) -> list[tuple[int, int]]:
    out, pos = [], 0
    for p in pieces:
        start = data.index(p, pos)
        out.append((start, start + len(p)))
        pos = start + len(p)
    return out


def _chunkings(data: bytes, rng: random.Random):
    yield [data]
    for _ in range(3):
        chunks, i = [], 0
        while i < len(data):
            n = rng.choice([1, 2, 3, 7, 64, 1000, 65536])
            chunks.append(data[i:i + n])
            i += n
        yield chunks


def _chunk_seed(fmt: str, style: str) -> int:
    """Seed of the random chunkings of a generated document: the same in every run (a str
    ``hash()`` is salted per process, so a failure could not be replayed with it)."""
    return zlib.crc32(f"{fmt}/{style}".encode("utf-8")) & 0xFFFF


def test_chunk_seeds_are_the_same_in_every_run():
    code = ("import test_core_rust_smoke as m, jsongen\n"
            "print([m._chunk_seed(f, s) for f in jsongen.FORMATS\n"
            "       for s in ('compact', 'pretty2', 'tab', 'messy', 'escaped', 'messy_escaped')])\n")
    outputs = []
    for hash_seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        cp = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(os.path.abspath(__file__)), env=env,
                            capture_output=True, timeout=120)
        assert cp.returncode == 0, cp.stderr.decode("utf-8", errors="replace")
        outputs.append(cp.stdout)
    assert outputs[0] == outputs[1], outputs


@pytest.mark.parametrize("fmt", jsongen.FORMATS)
@pytest.mark.parametrize("style", ["compact", "pretty2", "tab", "messy", "escaped", "messy_escaped"])
def test_generated_documents(fmt, style):
    seed = _chunk_seed(fmt, style)
    where = f"{fmt}/{style}, chunkings of random.Random({seed})"
    rng = random.Random(seed)
    recs = jsongen.records(40, seed=7)
    if fmt == "keyed":
        keys = [f"id{i}" for i in range(len(recs))]
    data = jsongen.build_document(recs, fmt, style=style, newline="\r\n" if style == "messy" else "\n", seed=3)
    enc = [jsongen.encode(r, style, 3 + i) for i, r in enumerate(recs)]
    mode, kw = {
        "array": (C.MODE_CONTAINER, {}),
        "jsonl": (C.MODE_LINES, {}),
        "brace_list": (C.MODE_BRACE_LIST, {}),
        "wrapped": (C.MODE_CONTAINER, {"target_path": ["data"]}),
        "keyed": (C.MODE_CONTAINER, {}),
        "single": (C.MODE_SINGLE, {}),
        "stream": (C.MODE_STREAM, {}),
    }[fmt]
    if fmt == "jsonl":
        enc = [jsongen.encode(r, "compact" if style != "escaped" else style, 3 + i) for i, r in enumerate(recs)]
    elif fmt == "keyed":
        enc = [json.dumps(k, ensure_ascii=False).encode() + b":" + e for k, e in zip(keys, enc)]
    elif fmt == "single":
        enc = [data]
    expected = _expected_spans(data, enc)
    results = [run_scanner(data, mode, chunks, **kw) for chunks in _chunkings(data, rng)]
    for k, r in enumerate(results):
        assert r == results[0], f"{where}: chunking {k}"
    r = results[0]
    assert r["error"] is None and r["broken"] == [], where
    assert r["spans"] == expected, where
    assert r["consumed"] == len(data), where
    assert r["lines"] == data.count(b"\n"), where
    assert r["flags"] == _expected_flags(data), where


def _expected_flags(data: bytes) -> int:
    """Flags of valid JSON text (backslashes and bytes >= 0x80 occur only in strings)."""
    flags = C.FLAG_NON_ASCII if any(b >= 0x80 for b in data) else 0
    i = data.find(BS)
    while i != -1:
        flags |= C.FLAG_ESCAPES
        if data[i + 1:i + 2] == b"u":
            flags |= C.FLAG_U_ESCAPES
        i = data.find(BS, i + 2)
    return flags


def test_generated_wrapped_deep_target_path():
    recs = jsongen.records(10, seed=2)
    data = jsongen.build_document(recs, "wrapped", style="pretty2", wrapper_path=("a", "b c", "d"))
    r = run_scanner(data, C.MODE_CONTAINER, target_path=["a", "b c", "d"])
    assert r["error"] is None and len(r["spans"]) == len(recs)
    for i, ((s, e), rec) in enumerate(zip(r["spans"], recs)):
        assert data[s:e] == jsongen.encode(rec, "pretty2", i)


def test_mutation_fuzz_chunk_invariance():
    """Random corruptions: any chunking gives the same result and nothing crashes."""
    rng = random.Random(1234)
    base_docs = [jsongen.build_document(jsongen.records(3, seed=s), fmt, style=st)
                 for s, fmt, st in [(1, "array", "compact"), (2, "jsonl", "compact"), (3, "wrapped", "messy"),
                                    (4, "stream", "pretty2"), (5, "brace_list", "escaped")]]
    junk = [b"", b"x", b"\x00", b"\xff", b"\xc3", b'"', BS, b"{", b"}", b"[", b"]", b",", b":", b"\n", b"-", b"0",
            b"e", b".", b"t", b"nul", U, b"\xed\xa0\x80"]
    for _ in range(300):
        doc = bytearray(rng.choice(base_docs))
        for _ in range(rng.randint(1, 3)):
            op = rng.random()
            pos = rng.randint(0, len(doc))
            if op < 0.4:
                doc[pos:pos] = rng.choice(junk)
            elif op < 0.7 and doc:
                del doc[pos:pos + rng.randint(1, 4)]
            else:
                doc = doc[:pos]
        data = bytes(doc)
        for mode in ALL_MODES:
            kw = {"target_path": ["data"]} if mode == C.MODE_CONTAINER and rng.random() < 0.3 else {}
            results = [run_scanner(data, mode, ch, **kw) for ch in _chunkings(data, rng)]
            assert all(x == results[0] for x in results), (data, mode)
            r = results[0]
            if r["error"] is not None:
                off, code, expected, line, line_start = r["error"]
                assert code in C.ERROR_CODES and expected in C.EXPECTED_CODES
                assert 0 <= off <= len(data)
                assert line == data.count(b"\n", 0, off)
                assert line_start == (data.rfind(b"\n", 0, off) + 1)
                assert mode != C.MODE_LINES
            for (s, e) in r["spans"]:
                assert 0 <= s < e <= len(data)
            for b in r["broken"]:
                assert b[4] in C.ERROR_CODES and b[5] in C.EXPECTED_CODES


# ---------------------------------------------------------------------------------
# Reformatter
# ---------------------------------------------------------------------------------
def reformat(data: bytes, chunks: list[bytes] | None = None, **kw) -> bytes:
    f = R.Reformatter(**kw)
    out = b"".join(f.feed(c) for c in (chunks if chunks is not None else [data]))
    return out + f.finish()


def reformat_all_splits(data: bytes, **kw) -> bytes:
    whole = reformat(data, **kw)
    assert reformat(data, [data[i:i + 1] for i in range(len(data))], **kw) == whole
    for k in range(len(data) + 1):
        assert reformat(data, [data[:k], data[k:]], **kw) == whole
    return whole


def test_reformatter_compact_and_indent():
    src = b' { "a" : [ 1 , 2.50 ] ,\n "b" : { } , "c" : [ ] , "s" : "x ] \\" {" } '
    assert reformat_all_splits(src) == b'{"a":[1,2.50],"b":{},"c":[],"s":"x ] \\" {"}'
    assert reformat_all_splits(src, indent=2) == (
        b'{\n  "a": [\n    1,\n    2.50\n  ],\n  "b": {},\n  "c": [],\n  "s": "x ] \\" {"\n}'
    )
    assert reformat_all_splits(b"[1,[2]]", indent="\t") == b"[\n\t1,\n\t[\n\t\t2\n\t]\n]"
    assert reformat_all_splits(b"[1,2]", indent=0) == b"[\n1,\n2\n]"
    assert reformat_all_splits(b"[1]", indent=2, base_level=1) == b"[\n    1\n  ]"
    assert reformat_all_splits(b"1 2 [] {}", indent=2) == b"12[]{}"


def test_reformatter_fragments_and_pending_open():
    f = R.Reformatter(indent=2)
    assert f.feed(b'{"meta":{"x":1},"data":') == b'{\n  "meta": {\n    "x": 1\n  },\n  "data": '
    assert f.feed(b"[") == b""
    assert f.finish() == b"["
    assert f.finish() == b""
    with pytest.raises(ValueError):
        f.feed(b"]")
    with pytest.raises(ValueError):  # checked before the argument type
        f.feed("text")
    f = R.Reformatter(indent=2)
    assert f.feed(b"[") == b""
    assert f.feed(b"]") == b"[]"
    assert R.Reformatter(indent=4).feed(b'],"tail":1}') == b'\n],\n"tail": 1\n}'
    # compact output never holds a bracket back
    f = R.Reformatter()
    assert f.feed(b" [ ") == b"["
    assert f.feed(b"] ") == b"]"
    assert f.finish() == b""
    with pytest.raises(TypeError):
        R.Reformatter().feed("text")


def test_reformatter_matches_json_dumps():
    rng = random.Random(5)

    def value(depth=0):
        r = rng.random()
        if depth > 3 or r < 0.3:
            return rng.choice([0, -12, 3.25, True, False, None, "s", "Тошкент", 'q"\\', "", 10**20])
        if r < 0.65:
            return [value(depth + 1) for _ in range(rng.randint(0, 4))]
        return {rng.choice("abcdefgh") + str(i): value(depth + 1) for i in range(rng.randint(0, 4))}

    for _ in range(200):
        v = value()
        compact = json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode()
        assert reformat(compact) == compact
        for indent in (0, 1, 2, 4, "\t"):
            assert reformat(compact, indent=indent) == json.dumps(v, ensure_ascii=False, indent=indent).encode()
        pretty = json.dumps(v, ensure_ascii=False, indent=3).encode()
        assert reformat(pretty) == compact


def test_reformatter_invalid_arguments():
    for bad in (-1, 17, "  ", "x", 2.0, [2], True, b"\t"):
        with pytest.raises(ValueError):
            R.Reformatter(indent=bad)
    for bad in (-1, True, 1.0, "1"):
        with pytest.raises(ValueError):
            R.Reformatter(indent=2, base_level=bad)
    for ok in (None, 0, 16, "\t"):
        R.Reformatter(indent=ok)


# ---------------------------------------------------------------------------------
# MultiFinder
# ---------------------------------------------------------------------------------
def brute_spans(needles, buf, spans):
    return [k for k, (s, e) in enumerate(spans) if any(buf.find(n, s, e) != -1 for n in needles)]


def brute_first(needles, buf, start=0, end=None):
    found = [p for p in (buf.find(n, start, end) for n in needles) if p != -1]
    return min(found) if found else -1


def test_multifinder_against_brute_force():
    rng = random.Random(9)
    for _ in range(400):
        alphabet = rng.choice([b"ab", b"abc", "aбв".encode()])
        buf = bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
        needles = list({bytes(rng.choice(alphabet) for _ in range(rng.randint(1, 4))) for _ in range(rng.randint(1, 5))})
        spans, p = [], 0
        while p < len(buf):
            s = p + rng.randint(0, 3)
            e = min(len(buf), s + rng.randint(0, 12))
            if s > e:
                break
            spans.append((s, e))
            p = e
        mf = R.MultiFinder(needles)
        assert u64s(mf.find_in_spans(buf, span_bytes(spans))) == brute_spans(needles, buf, spans)
        assert mf.find_first(buf) == brute_first(needles, buf)
        a, b = rng.randint(-10, len(buf) + 5), rng.randint(-10, len(buf) + 5)
        if a < 0 or b < 0 or b > len(buf):
            with pytest.raises(ValueError):
                mf.find_first(buf, a, b)
        else:
            assert mf.find_first(buf, a, b) == brute_first(needles, buf, a, b)
        if a >= 0:
            assert mf.find_first(buf, a) == brute_first(needles, buf, a)
            assert mf.find_first(buf, a, None) == brute_first(needles, buf, a)


def test_multifinder_large_buffer_and_records():
    recs = [json.dumps(r, ensure_ascii=False).encode() for r in jsongen.records(3000, seed=4)]
    data = b"[" + b",".join(recs) + b"]"
    s = R.Scanner(C.MODE_CONTAINER)
    spans = pairs(s.feed(data) + s.finish())
    for needles in ([b"Toshkent"], ["Тошкент".encode(), b"Samarqand"], [b"zz%d" % i for i in range(16)] + [b'"id"']):
        mf = R.MultiFinder(needles)
        assert u64s(mf.find_in_spans(data, span_bytes(spans))) == brute_spans(needles, data, spans)
        assert u64s(mf.find_in_spans(bytearray(data), memoryview(span_bytes(spans)))) == brute_spans(needles, data, spans)


def test_multifinder_errors():
    for bad in ([], [b""], [b"a", b""], b"abc", "abc", bytearray(b"ab"), 5, [0]):
        with pytest.raises(ValueError):
            R.MultiFinder(bad)
    for bad in (["str"], [b"a", "str"], [None], [1.5]):
        with pytest.raises(TypeError):
            R.MultiFinder(bad)
    mf = R.MultiFinder([b"a"])
    for start, end in ((-1, None), (0, -1), (0, 4), (True, 2), (0, 2.0), ("0", 2)):
        with pytest.raises(ValueError):
            mf.find_first(b"abc", start, end)
    assert mf.find_first(b"abc", 3, 3) == -1
    assert mf.find_first(b"abc", 5, 3) == -1
    with pytest.raises(TypeError):
        mf.find_first("abc")
    with pytest.raises(ValueError):
        mf.find_in_spans(b"abc", span_bytes([(0, 4)]))
    with pytest.raises(ValueError):
        mf.find_in_spans(b"abc", span_bytes([(2, 1)]))
    with pytest.raises(ValueError):
        mf.find_in_spans(b"abc", b"\x00" * 8)
    assert mf.find_in_spans(b"abc", b"") == b""


# ---------------------------------------------------------------------------------
# PathExtractor
# ---------------------------------------------------------------------------------
def navigate(value, path):
    cur = [value]
    for seg in path:
        nxt = []
        for v in cur:
            tag = seg[0]
            if tag == "key":
                if isinstance(v, dict):
                    if seg[1] in v:
                        nxt.append(v[seg[1]])
                elif isinstance(v, list):
                    k = seg[1]
                    if k.isdigit() and k.isascii() and (k == "0" or k[0] != "0") and int(k) < len(v):
                        nxt.append(v[int(k)])
            elif tag == "index":
                if isinstance(v, list) and seg[1] < len(v):
                    nxt.append(v[seg[1]])
            elif tag == "any_index":
                if isinstance(v, list):
                    nxt.extend(v)
            elif tag == "any_key":
                if isinstance(v, dict):
                    nxt.extend(v.values())
        cur = nxt
    return cur


PATHS = [
    [],
    [("key", "id")],
    [("key", "name")],
    [("key", "contact"), ("key", "address"), ("key", "city")],
    [("key", "orders"), ("any_index",), ("key", "sum")],
    [("key", "orders"), ("index", 1)],
    [("key", "orders"), ("key", "0"), ("key", "oid")],
    [("key", "tags"), ("any_index",)],
    [("any_key",)],
    [("key", "extra"), ("any_key",), ("any_index",)],
    [("key", "missing")],
    [("key", "Тошкент")],
    [("key", "")],
]


def test_path_extractor_matches_navigation():
    recs = jsongen.records(300, seed=11)
    for style in ("compact", "messy", "escaped"):
        enc = [jsongen.encode(r, style, i) for i, r in enumerate(recs)]
        data = b"[" + b",".join(enc) + b"]"
        spans = _expected_spans(data, enc)
        px = R.PathExtractor(PATHS)
        quads = array("Q", px.extract(data, span_bytes(spans))).tolist()
        got: dict[tuple[int, int], list] = {}
        order = []
        for i in range(0, len(quads), 4):
            si, pi, s, e = quads[i:i + 4]
            order.append((si, pi))
            got.setdefault((si, pi), []).append(json.loads(data[s:e]))
        assert order == sorted(order)
        for si, raw in enumerate(enc):
            value = json.loads(raw)
            for pi, path in enumerate(PATHS):
                assert got.get((si, pi), []) == navigate(value, path), (si, path)


def test_path_extractor_dict_semantics_and_spans():
    buf = b' {"a": 1, "b": {"c": [10, 20, {"d": null}]}, "a": "last", "' + U + b'0061": 3.5} '
    px = R.PathExtractor([[("key", "a")], [("any_key",)], [("key", "b"), ("key", "c"), ("any_index",)], []])
    quads = array("Q", px.extract(buf, span_bytes([(0, len(buf))]))).tolist()
    got = [(quads[i + 1], buf[quads[i + 2]:quads[i + 3]]) for i in range(0, len(quads), 4)]
    assert got == [
        (0, b"3.5"),
        (1, b"3.5"),
        (1, b'{"c": [10, 20, {"d": null}]}'),
        (2, b"10"),
        (2, b"20"),
        (2, b'{"d": null}'),
        (3, buf.strip()),
    ]


def test_path_extractor_member_spans():
    doc = b'{"k1": {"x": [1, 2]}, "k2": {"x": []}, "k3": 5}'
    s = R.Scanner(C.MODE_CONTAINER)
    spans = s.feed(doc) + s.finish()
    px = R.PathExtractor([[("key", "x"), ("index", 0)], []], member_spans=True)
    quads = array("Q", px.extract(doc, spans)).tolist()
    got = [(quads[i], quads[i + 1], doc[quads[i + 2]:quads[i + 3]]) for i in range(0, len(quads), 4)]
    assert got == [(0, 0, b"1"), (0, 1, b'{"x": [1, 2]}'), (1, 1, b'{"x": []}'), (2, 1, b"5")]


@pytest.mark.parametrize("bad", [b'{"a":1,}', b"[1 2]", b"[01]", b"[1.]", b'"\x01"', b'"\xc3"', b'"' + BS + b'x"',
                                 b"tru", b"{} x", b"", b"   ", b"[", b'{"a" 1}', b"[\xc3\xa9]", b'"\xed\xa0\x80"'])
def test_path_extractor_invalid_json(bad):
    with pytest.raises(ValueError):
        R.PathExtractor([[]]).extract(bad, span_bytes([(0, len(bad))]))


def test_path_extractor_duplicate_keys_random():
    """Objects with duplicate keys (some written with escapes): dict semantics, exact occurrence."""
    rng = random.Random(21)
    pool = ["a", "b", "0", "1", "é", "k y"]
    counter = [0]

    def key_text(k: str) -> bytes:
        if rng.random() < 0.3:  # escaped form of the same key
            return b'"' + b"".join(U + b"%04x" % ord(ch) for ch in k) + b'"'
        return json.dumps(k, ensure_ascii=False).encode()

    def gen(depth: int) -> bytes:
        r = rng.random()
        if depth > 3 or r < 0.3:
            counter[0] += 1
            return str(counter[0]).encode()
        if r < 0.6:
            return b"[" + b",".join(gen(depth + 1) for _ in range(rng.randint(0, 4))) + b"]"
        members = [key_text(rng.choice(pool)) + b":" + gen(depth + 1) for _ in range(rng.randint(0, 6))]
        return b"{" + b", ".join(members) + b"}"

    def rand_path() -> list[tuple]:
        path = []
        for _ in range(rng.randint(0, 3)):
            r = rng.random()
            if r < 0.5:
                path.append(("key", rng.choice(pool + ["2", "01"])))
            elif r < 0.7:
                path.append(("index", rng.randint(0, 3)))
            elif r < 0.85:
                path.append(("any_index",))
            else:
                path.append(("any_key",))
        return path

    for _ in range(300):
        docs = [gen(0) for _ in range(rng.randint(1, 4))]
        data = b"\n".join(docs)
        spans = _expected_spans(data, docs)
        paths = [rand_path() for _ in range(rng.randint(1, 4))]
        quads = u64s(R.PathExtractor(paths).extract(data, span_bytes(spans)))
        got: dict[tuple[int, int], list] = {}
        for i in range(0, len(quads), 4):
            got.setdefault((quads[i], quads[i + 1]), []).append(json.loads(data[quads[i + 2]:quads[i + 3]]))
        for si, doc in enumerate(docs):
            value = json.loads(doc)
            for pi, path in enumerate(paths):
                assert got.get((si, pi), []) == navigate(value, path), (doc, path)


def test_path_extractor_validates_like_the_scanner():
    """Random corruptions: extract() raises ValueError exactly when the Scanner reports an error."""
    rng = random.Random(77)
    recs = [jsongen.encode(r, rng.choice(["compact", "messy", "escaped"]), i)
            for i, r in enumerate(jsongen.records(60, seed=5))]
    junk = [b"x", b"\x00", b"\xff", b"\xc3", b'"', BS, b"{", b"}", b"[", b"]", b",", b":", b"-", b"0", b"e",
            b".", b"t", U, b"\xed\xa0\x80", b" "]
    px = R.PathExtractor([[("key", "id")], [("any_key",)]])
    checked = 0
    for _ in range(1500):
        doc = bytearray(rng.choice(recs))
        if rng.random() < 0.8:
            pos = rng.randint(0, len(doc))
            if rng.random() < 0.6:
                doc[pos:pos] = rng.choice(junk)
            else:
                del doc[pos:pos + rng.randint(1, 3)]
        data = bytes(doc)
        s = R.Scanner(C.MODE_SINGLE)
        s.feed(data)
        s.finish()
        try:
            px.extract(data, span_bytes([(0, len(data))]))
            ok = True
        except ValueError:
            ok = False
        assert ok == (s.error is None), (data, s.error)
        checked += 1
    assert checked == 1500


def test_path_extractor_depth_limit():
    ok = b"[" * 100000 + b"]" * 100000
    deep = b"[" * 100001 + b"]" * 100001
    px = R.PathExtractor([[("index", 0)]])
    assert len(px.extract(ok, span_bytes([(0, len(ok))]))) == 32
    with pytest.raises(ValueError):
        px.extract(deep, span_bytes([(0, len(deep))]))


def test_path_extractor_invalid_arguments():
    for paths in ([[("key", 1)]], [[("index", -1)]], [[("index", "1")]], [[("nope",)]], [[("any_key", 1)]],
                  [["key"]], ["key"], [[("key",)]], [[("descendants",)]], [[("index", True)]], [[()]],
                  "paths", b"paths", 5, [{("key", "a")}]):
        with pytest.raises(ValueError):
            R.PathExtractor(paths)
    R.PathExtractor([])
    R.PathExtractor(([("index", 2**70)],))
    px = R.PathExtractor([[]])
    with pytest.raises(ValueError):
        px.extract(b"[1]", span_bytes([(0, 4)]))
    with pytest.raises(ValueError):
        px.extract(b"[1]", b"\x01")
    assert px.extract(b"[1]", b"") == b""
    R.PathExtractor([[["key", "a"]]])  # lists accepted like tuples


# ---------------------------------------------------------------------------------
# numbers_plain
# ---------------------------------------------------------------------------------
def test_numbers_plain_tokens_match_codec_predicates():
    from jsonreshape.codec import is_plain_float_token, is_plain_int_token

    rng = random.Random(3)
    tokens = list(jsongen.EDGE_NUMBER_TOKENS)
    for _ in range(3000):
        ip = rng.choice(["0", str(rng.randint(1, 9)) + "".join(rng.choice("0123456789") for _ in range(rng.randint(0, 20)))])
        tok = rng.choice(["", "-"]) + ip
        r = rng.random()
        if r < 0.6:
            tok += "." + "".join(rng.choice("0000123456789") for _ in range(rng.randint(1, 20)))
        elif r < 0.7:
            tok += rng.choice(["e", "E"]) + rng.choice(["", "+", "-"]) + str(rng.randint(0, 400))
        tokens.append(tok)
    for tok in tokens:
        want = is_plain_int_token(tok) or is_plain_float_token(tok)
        assert R.numbers_plain(tok.encode()) is want, tok
        assert R.numbers_plain(b'{"k": [' + tok.encode() + b', "1e5"]}') is want, tok


def test_numbers_plain_documents():
    assert R.numbers_plain(b'{"a": [1, 2.5, -3, "1.10", "1e400"], "1E5": true}') is True
    assert R.numbers_plain(b'"k" : 12') is True
    assert R.numbers_plain(b'{"a' + BS + b'"1e5": 1.10}') is False
    assert R.numbers_plain(b'["' + BS + BS + b'", 1e5]') is False
    assert R.numbers_plain(b"[1, 2, 3, -0]") is False
    assert R.numbers_plain(b"") is True
    assert R.numbers_plain(bytearray(b"[0.1]")) is True
    for junk in (b"-", b"[1.", b'"', b"1e", b"--1", b"\xff\xfe", BS):
        R.numbers_plain(junk)  # unspecified result, must not crash


def test_numbers_plain_orjson_roundtrip_property():
    orjson = pytest.importorskip("orjson")
    rng = random.Random(8)
    for _ in range(3000):
        tok = rng.choice(["", "-"]) + str(rng.randint(0, 10 ** rng.randint(1, 20)))
        if rng.random() < 0.7:
            tok += "." + str(rng.randint(0, 10 ** rng.randint(1, 17)))
        if R.numbers_plain(tok.encode()):
            assert orjson.dumps(orjson.loads(tok)) == tok.encode(), tok


def test_gil_released_large_inputs_still_correct():
    # inputs above the GIL-release threshold go through py.detach
    data = b"[" + b",".join(b'{"id":%d,"s":"Toshkent"}' % i for i in range(40000)) + b"]"
    assert len(data) > 512 * 1024
    s = R.Scanner(C.MODE_CONTAINER)
    spans = s.feed(data) + s.finish()
    assert s.error is None and len(spans) == 40000 * 16
    assert len(R.MultiFinder([b"Toshkent"]).find_in_spans(data, spans)) == 40000 * 8
    assert len(R.PathExtractor([[("key", "id")]]).extract(data, spans)) == 40000 * 32
    assert R.numbers_plain(data) is True
    assert len(R.Reformatter(2).feed(data)) > len(data)
    assert struct.unpack_from("<QQ", spans, 16 * 39999) == (data.rindex(b"{"), len(data) - 1)
