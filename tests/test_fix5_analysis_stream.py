"""R5A-X1 / R5A-10 (stage-5 review "analysis"): tokens ijson's yajl2_c backend mishandles.

* ``stream.py`` careful mode (an integer token over 640 digits gets ``e0``; a lone low surrogate
  escape becomes ``\\u003f``; the record is parsed again and the delivered events are skipped):
  randomized differential test against an independent model of yajl2_c's semantics, every chunk
  size; huge (streamed) records with lone low surrogate escapes / huge integers are readable
  (R5A-10); a number beyond ``Decimal``'s exponent range makes the record unreadable with the
  localized error (it raised a raw ``decimal.InvalidOperation``); cancel is honoured.
* ``stream.exact_events`` (exact pure-Python parser) against the ``json`` module; ``needs_exact``.
* the reader's head analysis: keys with lone surrogate escapes are exact (yajl2_c's ``"?"`` gave a
  record path that did not exist), a lone low surrogate escape no longer ends the analysis, and a
  number beyond ``Decimal``'s range no longer makes ``Document.open`` raise.

Tests that parse huge integers in this process first make sure (``crash_guard``, a subprocess)
that doing so cannot kill pytest -- the crash itself is tested in test_fix5_analysis_crash.py.
"""

from __future__ import annotations

import io
import json
import os
import random
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import ijson
import pytest

from jsonreshape import codec, dupes, reader, stats
from jsonreshape import stream as stream_mod
from jsonreshape.errors import Cancelled, JsonReshapeError
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.stream import exact_events, iter_events, needs_exact, stream_values
from jsonreshape.tasks import CancelToken

ROOT = Path(__file__).resolve().parents[1]
BS = "\\"


def big(n: int) -> str:
    return ("1234567890" * (n // 10 + 1))[:n]


@pytest.fixture(scope="module")
def crash_guard(tmp_path_factory) -> None:
    """Fail (instead of crashing pytest) when parsing a huge integer still kills the process."""
    tmp = tmp_path_factory.mktemp("guard")
    code = ("import io, sys\nsys.path.insert(0, sys.argv[1])\nfrom jsonreshape import stream\n"
            "data = b'[[' + b'7' * 5000 + b'], [1], 2.5, 17]'\n"
            "r = stream._RangeReader(io.BytesIO(data), 0, len(data))\n"
            "print(sum(1 for _ in stream._parse(r)))\n")
    env = dict(os.environ, JSONRESHAPE_HOME=str(tmp))
    proc = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, env=env, timeout=120)
    if proc.returncode != 0:
        pytest.fail(f"parsing a 5000-digit integer kills the process (exit {proc.returncode}): R5A-X1 is back")


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


# ==================================================================================
# a generator of JSON texts with the expected events (an independent model of yajl2_c +
# careful mode: lone high escape -> "?", lone low escape -> "?", a high escape takes any \u
# escape after it, numbers as codec.loads gives the token)
# ==================================================================================
def yajl_pair(cp: int, sur: int) -> str:
    """What yajl2_c makes of a high escape ``cp`` followed by the escape ``sur``."""
    return chr(((cp & 0x3F) << 10) | ((((cp >> 6) & 0xF) + 1) << 16) | (sur & 0x3FF))


# (raw JSON text inside the quotes, yajl2_c's value, exact value = json.loads)
PIECES = [
    ("a", "a", "a"), ("ў", "ў", "ў"), ("O'zbek ", "O'zbek ", "O'zbek "), ("\U0001F600", "\U0001F600", "\U0001F600"),
    (BS + "n", "\n", "\n"), (BS + '"', '"', '"'), (BS + BS, BS, BS), (BS + "/", "/", "/"),
    (BS + "u0041", "A", "A"), (BS + "u0410", "А", "А"),
    (BS + "ud83d" + BS + "ude00", "\U0001F600", "\U0001F600"),
    (BS + "uD83D" + BS + "uDE00", "\U0001F600", "\U0001F600"),
    (BS + "ud800x", "?x", "\ud800x"),
    (BS + "udc00", "?", "\udc00"), (BS + "uDFFF", "?", "\udfff"),
    (BS + "ud800" + BS + "ud800" + BS + "udc00", yajl_pair(0xD800, 0xD800) + "?", "\ud800\U00010000"),
    (BS + "ud800" + BS + "u0041", yajl_pair(0xD800, 0x41), "\ud800A"),
    (BS + BS + "udc00", BS + "udc00", BS + "udc00"),
    (big(700), big(700), big(700)),
    (big(5000), big(5000), big(5000)),
]
SIMPLE = 10   # the first pieces carry no surrogate escape and no long digit run
INT_TOKENS = ["0", "7", "-1", "12345678901234567890", big(640), big(641), "-" + big(641), big(4300), big(4301),
              "-" + big(5000), big(20000)]
FLOAT_TOKENS = ["1.5", "-2.25e-7", "1.10", "1e400", "282.0", "0." + "3" * 700, big(5000) + ".5", "1E+2",
                big(700) + "e5", "-" + big(4400) + "E-3"]
WS = ["", "", " ", "\n", "\r\n", "\t "]


class Gen:
    def __init__(self, seed: int, risky: bool = True) -> None:
        self.rng = random.Random(seed)
        self.risky = risky

    def ws(self) -> str:
        return self.rng.choice(WS)

    def string(self, short: bool = False) -> tuple[str, str, str]:
        rng = self.rng
        pieces = []
        for _ in range(rng.randint(0, 3 if short else 6)):
            if self.risky and rng.random() < 0.3:
                pieces.append(rng.choice(PIECES[SIMPLE:-1] if short else PIECES[SIMPLE:]))
            else:
                pieces.append(rng.choice(PIECES[:SIMPLE]))
        return "".join(p[0] for p in pieces), "".join(p[1] for p in pieces), "".join(p[2] for p in pieces)

    def number(self) -> str:
        rng = self.rng
        if self.risky and rng.random() < 0.35:
            return rng.choice(INT_TOKENS[4:] + FLOAT_TOKENS[5:])
        return rng.choice(INT_TOKENS[:4] + FLOAT_TOKENS[:5])

    def value(self, depth: int = 0) -> tuple[str, list[tuple[str, Any, Any]]]:
        """(text, events as (event, yajl2_c value, exact value)); numbers carry their token."""
        rng = self.rng
        r = rng.random()
        if depth >= 4 or r < 0.35:
            k = rng.random()
            if k < 0.4:
                raw, yv, ev = self.string()
                return '"' + raw + '"', [("string", yv, ev)]
            if k < 0.8:
                tok = self.number()
                return tok, [("number", tok, tok)]
            word, event, v = rng.choice([("true", "boolean", True), ("false", "boolean", False), ("null", "null", None)])
            return word, [(event, v, v)]
        if r < 0.65:
            items = [self.value(depth + 1) for _ in range(rng.randint(0, 5))]
            text = "[" + ",".join(self.ws() + t + self.ws() for t, _ in items) + "]"
            return text, [("start_array", None, None)] + [e for _, ev in items for e in ev] + [("end_array", None, None)]
        parts, events = [], [("start_map", None, None)]
        for _ in range(rng.randint(0, 5)):
            kraw, kyv, kev = self.string(short=True)
            vt, ve = self.value(depth + 1)
            parts.append(self.ws() + '"' + kraw + '"' + self.ws() + ":" + self.ws() + vt + self.ws())
            events.append(("map_key", kyv, kev))
            events.extend(ve)
        return "{" + ",".join(parts) + "}", events + [("end_map", None, None)]


def _is_int_token(tok: str) -> bool:
    return not any(c in tok for c in ".eE")


def _same_number(got: Any, tok: str, streamed: bool) -> bool:
    """``got`` (a codec-typed number) is what ``codec.loads`` gives ``tok`` -- for a streamed
    float token: ``codec.loads(str(Decimal(tok)))`` (the documented exponent normalization)."""
    want = codec.loads(tok if (_is_int_token(tok) or not streamed) else str(Decimal(tok)))
    return type(got) is type(want) and codec.number_text(got) == codec.number_text(want)


def _parse_stream(data: bytes, member: bool = False) -> list[tuple[str, Any]]:
    pre, suf = (b"{", b"}") if member else (b"", b"")
    return list(stream_mod._parse(stream_mod._RangeReader(io.BytesIO(data), 0, len(data), pre, suf)))


# ==================================================================================
# stream.py careful mode
# ==================================================================================
@pytest.mark.parametrize("seed", range(4))
def test_careful_mode_matches_the_yajl_model(seed: int, monkeypatch, crash_guard) -> None:
    gen = Gen(seed)
    rng = random.Random(1000 + seed)
    careful = 0
    for _ in range(120):
        text, want = gen.value()
        member = rng.random() < 0.25
        if member:
            kraw, kyv, _ = gen.string(short=True)
            text = '"' + kraw + '":' + gen.ws() + text
            want = [("start_map", None, None), ("map_key", kyv, None)] + want + [("end_map", None, None)]
        data = text.encode("utf-8")
        size = rng.choice([1, 2, 3, 5, 7, 64, 4096])
        monkeypatch.setattr(stream_mod, "BUF_SIZE", size)
        monkeypatch.setattr(stream_mod, "BUF_MAX", size * rng.choice([1, 2, 64, 1024]))
        got = _parse_stream(data, member)
        careful += needs_exact(data)
        assert [e for e, _ in got] == [w[0] for w in want], text[:300]
        for (event, value), (_, yv, _) in zip(got, want):
            if event == "number":
                assert _same_number(stream_mod._number(value), yv, True), (yv[:60], value)
            else:
                assert value == yv, (text[:300], value, yv)
    assert careful > 10   # the generator did produce texts that need careful mode


def test_careful_mode_restarts_late_and_skips_exactly(tmp_path: Path, crash_guard) -> None:
    """The long integer far behind the start (many chunks of events delivered already)."""
    head = ",".join('{"i":%d,"s":"%s"}' % (i, "x" * (i % 50)) for i in range(20_000))
    text = "[" + head + ',{"big":' + big(5000) + ',"low":"' + BS + 'udc00"},' + head + "]"
    got = _parse_stream(text.encode())
    want = list(exact_events(text.replace(BS + "udc00", "?")))
    assert len(got) == len(want)
    for (ge, gv), (we, wv) in zip(got, want):
        assert ge == we
        if ge == "number":
            assert type(stream_mod._number(gv)) is type(wv) and codec.number_text(stream_mod._number(gv)) == \
                codec.number_text(wv)
        else:
            assert gv == wv


def test_careful_filter_unit_cases() -> None:
    """Straight cases of the lexical filter, fed in every split."""
    cases = [
        ('[' + big(700) + ']', '[' + big(700) + 'e0]'),
        ('[-' + big(700) + ',1]', '[-' + big(700) + 'e0,1]'),
        ('["' + big(700) + '"]', '["' + big(700) + '"]'),                        # in a string
        ('{"' + big(700) + '":1}', '{"' + big(700) + '":1}'),                    # in a key
        ('["a' + BS + '"' + big(700) + '"]', '["a' + BS + '"' + big(700) + '"]'),   # after an escaped quote
        ('["a' + BS + BS + '",' + big(700) + ']', '["a' + BS + BS + '",' + big(700) + 'e0]'),
        ('[' + big(700) + '.5,0.' + big(700) + ',1e' + big(20) + ']',
         '[' + big(700) + '.5,0.' + big(700) + ',1e' + big(20) + ']'),          # fraction / exponent parts
        ('[1E-' + big(700) + ']', '[1E-' + big(700) + ']'),
        ('[' + big(640) + ']', '[' + big(640) + ']'),
        ('["' + BS + 'udc00"]', '["' + BS + 'u003f"]'),
        ('["' + BS + 'ud83d' + BS + 'ude00"]', '["' + BS + 'ud83d' + BS + 'ude00"]'),
        ('["' + BS + BS + 'udc00"]', '["' + BS + BS + 'udc00"]'),               # not an escape
        ('["' + BS + BS + BS + 'udc00"]', '["' + BS + BS + BS + 'u003f"]'),
        ('["' + BS + 'ud800' + BS + 'ud800' + BS + 'udc00"]', '["' + BS + 'ud800' + BS + 'ud800' + BS + 'u003f"]'),
        ('["' + BS + 'ud800' + BS + 'udc00' + BS + 'udc00"]', '["' + BS + 'ud800' + BS + 'udc00' + BS + 'u003f"]'),
        (big(700), big(700) + "e0"),                                             # at the very end
    ]
    for text, want in cases:
        data = text.encode()
        for cut in range(len(data) + 1):
            for second in sorted({cut, min(len(data), cut + 1), min(len(data), cut + 7)}):
                f = stream_mod._CarefulFilter()
                out = f.feed(data[:cut]) + f.feed(data[cut:second]) + f.feed(data[second:]) + f.feed(b"", final=True)
                assert out.decode() == want, (text[:80], cut, second)


def _huge_doc(tmp_path: Path, lines: list[str]) -> Document:
    p = tmp_path / "huge.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    doc = Document.open(p, settings=Settings(large_node_bytes=1024))
    doc.build_index()
    return doc


def test_huge_records_with_lone_surrogate_escapes_are_readable(tmp_path: Path) -> None:
    pad = "x" * 2000
    notes = [BS + "ud800", "ok", BS + "udc00 tail", BS + "udfff", BS + "ud800 tail", BS + "ud83d" + BS + "ude00"]
    doc = _huge_doc(tmp_path, ['{"id": %d, "note": "%s", "pad": "%s"}' % (i, n, pad) for i, n in enumerate(notes)])
    try:
        assert all(doc.record_span(i)[1] - doc.record_span(i)[0] > 1024 for i in range(doc.record_count))
        got = [stream_values(doc, doc.record_span(i), ["note"])[0] for i in range(doc.record_count)]
        assert got == [["?"], ["ok"], ["? tail"], ["?"], ["? tail"], ["\U0001F600"]]
        (fid, fnote) = stats.field_stats(doc, ["id", "note"])
        assert (fid.values, fid.skipped, fnote.values, fnote.skipped) == (6, 0, 6, 0)
        rep = dupes.find_duplicates(doc, "note")
        assert (rep.records_with_key, rep.skipped, rep.groups) == (6, 0, 2)    # "?" x2, "? tail" x2
    finally:
        doc.close()


def test_huge_records_with_huge_integers_are_readable(tmp_path: Path, crash_guard) -> None:
    pad = "x" * 2000
    toks = [big(4301), "-" + big(10_000), big(641), "5"]
    doc = _huge_doc(tmp_path, ['{"id": %d, "grid": [%s, 1], "pad": "%s", "k": 282.0}' % (i, t, pad)
                               for i, t in enumerate(toks)])
    try:
        for i, tok in enumerate(toks):
            (vals,) = stream_values(doc, doc.record_span(i), ["grid[0]"]).values()
            assert _same_number(vals[0], tok, True), tok[:20]
        fs = stats.field_stats(doc, ["grid[*]", "k", "id"])
        assert [(f.values, f.skipped) for f in fs] == [(8, 0), (4, 0), (4, 0)]
    finally:
        doc.close()


def test_number_beyond_decimal_range_makes_the_record_unreadable_not_crash(tmp_path: Path) -> None:
    pad = "x" * 2000
    doc = _huge_doc(tmp_path, ['{"id": 1, "v": [1e99999999999999999999, 5], "pad": "%s"}' % pad,
                               '{"id": 2, "v": [7], "pad": "%s"}' % pad])
    try:
        with pytest.raises(JsonReshapeError) as ei:
            list(iter_events(doc, doc.record_span(0)))
        assert ei.value.key == "stream.error.parse"
        (fid,) = stats.field_stats(doc, ["id"])
        assert (fid.values, fid.skipped) == (1, 1)     # counted, with a warning -- not "Ichki xato"
        assert fid.warning_texts()
    finally:
        doc.close()


def test_careful_mode_checks_cancel(tmp_path: Path, crash_guard) -> None:
    pad = "x" * 2000
    doc = _huge_doc(tmp_path, ['{"v": [%s], "pad": "%s"}' % (big(5000), pad)])
    try:
        token = CancelToken()
        token.cancel()
        with pytest.raises(Cancelled):
            list(iter_events(doc, doc.record_span(0), cancel=token))
    finally:
        doc.close()


# ==================================================================================
# exact_events / needs_exact
# ==================================================================================
def _reference_events(text: str) -> list[tuple[str, Any]]:
    """Events of ``json.loads`` (pairs kept in order, numbers through codec.loads)."""
    tree = json.loads(text, object_pairs_hook=lambda pairs: ("obj", pairs), parse_int=codec.loads,
                      parse_float=codec.loads, parse_constant=None)
    out: list[tuple[str, Any]] = []
    stack: list[Any] = [tree]
    while stack:
        v = stack.pop()
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "end":
            out.append((v[1], None))
        elif isinstance(v, tuple) and len(v) == 2 and v[0] == "obj":
            out.append(("start_map", None))
            stack.append(("end", "end_map"))
            for k, x in reversed(v[1]):
                stack.append(x)
                stack.append(("key", k))
        elif isinstance(v, tuple) and len(v) == 2 and v[0] == "key":
            out.append(("map_key", v[1]))
        elif isinstance(v, list):
            out.append(("start_array", None))
            stack.append(("end", "end_array"))
            stack.extend(reversed(v))
        elif isinstance(v, str):
            out.append(("string", v))
        elif v is None:
            out.append(("null", None))
        elif isinstance(v, bool):
            out.append(("boolean", v))
        else:
            out.append(("number", v))
    return out


def _same_events(got: list, want: list) -> bool:
    if [e for e, _ in got] != [e for e, _ in want]:
        return False
    for (e, g), (_, w) in zip(got, want):
        if e == "number":
            if type(g) is not type(w) or codec.number_text(g) != codec.number_text(w):
                return False
        elif g != w:
            return False
    return True


@pytest.mark.parametrize("seed", range(4))
def test_exact_events_equal_the_json_module(seed: int) -> None:
    gen = Gen(100 + seed)
    for _ in range(150):
        text, _ = gen.value()
        assert _same_events(list(exact_events(text.encode("utf-8"))), _reference_events(text)), text[:300]
        assert _same_events(list(exact_events(text)), _reference_events(text))
        toks = [v for e, v in exact_events(text, convert=False) if e == "number"]
        assert all(isinstance(v, str) for v in toks)


@pytest.mark.parametrize("seed", range(3))
def test_exact_events_stop_like_ijson_on_truncated_and_broken_texts(seed: int) -> None:
    gen = Gen(200 + seed, risky=False)
    rng = random.Random(seed)
    for _ in range(300):
        text, _ = gen.value()
        data = text.encode("utf-8")
        if rng.random() < 0.7 and len(data) > 1:
            data = data[:rng.randint(1, len(data) - 1)]
        else:
            data = data + rng.choice([b" x", b",", b"]", b" 1"])
        assert not needs_exact(data)

        def names(events: Any) -> tuple[list[str], bool]:
            out: list[str] = []
            try:
                for e, _ in events:
                    out.append(e)
            except ijson.JSONError:
                return out, False
            return out, True

        assert names(exact_events(data)) == names(ijson.basic_parse(io.BytesIO(data), use_float=False)), data[:200]


def test_exact_events_any_depth() -> None:
    depth = 100_000
    text = "[" * depth + "1" + "]" * depth
    events = list(exact_events(text))
    assert len(events) == 2 * depth + 1 and events[depth] == ("number", 1)


@pytest.mark.parametrize("seed", range(3))
def test_needs_exact_false_means_ijson_reads_it_exactly(seed: int, crash_guard) -> None:
    gen = Gen(300 + seed)
    seen_false = seen_true = 0
    for _ in range(200):
        text, _ = gen.value()
        data = text.encode("utf-8")
        if needs_exact(data):
            seen_true += 1
            continue
        seen_false += 1
        fast = list(ijson.basic_parse(io.BytesIO(data), use_float=False))
        exact = list(exact_events(data))
        assert [e for e, _ in fast] == [e for e, _ in exact]
        for (e, f), (_, x) in zip(fast, exact):
            if e == "number":   # equal values (ijson may normalize an exponent's text)
                assert Decimal(codec.number_text(stream_mod._number(f))) == Decimal(codec.number_text(x))
            else:
                assert f == x, text[:200]
    assert seen_false > 20 and seen_true > 20


@pytest.mark.parametrize("text, want", [
    ('{"a": [' + big(641) + ']}', True), ('{"a": "' + big(641) + '"}', True), ('{"a": [' + big(640) + ']}', False),
    ('["' + BS + 'ud800x"]', True), ('["' + BS + 'udc00"]', True), ('["' + BS + 'ud83d' + BS + 'ude00"]', False),
    ('["' + BS + 'uD83D' + BS + 'uDE00 ' + BS + 'u0041"]', False),
    ('["' + BS + 'ud800' + BS + 'ud800' + BS + 'udc00"]', True), ('["' + BS + BS + 'ud83d' + BS + 'ude00"]', True),
    ('["' + BS + 'ud800' + BS + 'u0041"]', True), ('{"a": "\U0001F600 ўқ"}', False), ("", False),
])
def test_needs_exact(text: str, want: bool) -> None:
    assert needs_exact(text.encode("utf-8")) is want


# ==================================================================================
# the reader's head analysis
# ==================================================================================
@pytest.mark.parametrize("text, kind, path, n", [
    ('{"' + BS + 'ud800x": [{"a":1},{"a":2},{"a":3}]}', "wrapped", ("\ud800x",), 3),
    ('{"' + BS + 'udc00x": [{"a":1},{"a":2},{"a":3}]}', "wrapped", ("\udc00x",), 3),
    ('{"k1": {"a":1}, "' + BS + 'udc00x": {"a":2}, "k3": {"a":3}}', "keyed", None, 3),
    ('{"k1": {"a":"' + BS + 'udc00"}, "k2": {"a":2}, "k3": {"a":3}}', "keyed", None, 3),
    ('{"k1": {"a":[1e99999999999999999999, 2]}, "k2": {"a":2}, "k3": {"a":3}}', "keyed", None, 3),
    ('{"' + big(700) + '": [{"a":1},{"a":2}], "m": 1}', "wrapped", (big(700),), 2),
    ('{"meta": {"n": ' + big(5000) + '}, "data": [{"a":1},{"a":2},{"a":3}]}', "wrapped", ("data",), 3),
])
def test_head_analysis_is_exact(tmp_path: Path, backend: str, text: str, kind: str, path: Any, n: int,
                                crash_guard) -> None:
    p = tmp_path / "head.json"
    p.write_text(text, encoding="utf-8")
    doc = Document.open(p)
    try:
        doc.build_index()
        assert (doc.kind, doc.record_path, doc.record_count, doc.error) == (kind, path, n, None)
    finally:
        doc.close()


def test_forced_record_path_through_a_lone_surrogate_key(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "forced.json"
    p.write_text('{"' + BS + 'ud800x": {"0": [{"a":1},{"a":2}]}}', encoding="utf-8")
    doc = Document.open(p, record_path=["\ud800x", "0"])
    try:
        doc.build_index()
        # the head shows an object at "0": the segment stays a member key
        assert (doc.kind, doc.record_path, doc.record_count, doc.error) == ("wrapped", ("\ud800x", "0"), 2, None)
    finally:
        doc.close()
