"""jsonreshape.codec: exact numbers and Unicode (AGENT_PROMPT §12, §14 test 24).

* the tokens ``0.1``, ``1e400``, ``12345678901234567890``, ``-0.0``, ``1.10`` and every
  ``jsongen.EDGE_NUMBER_TOKENS`` survive ``loads`` -> ``dumps`` byte for byte inside
  objects and arrays, compact and indented, and never become strings;
* the orjson fast path and ``loads_precise`` return identical objects;
* duplicate keys, lone surrogates, big ints, non-ASCII output, NaN/Infinity rejection;
* JsonFloat / JsonInt behave like ordinary numbers.
"""

from __future__ import annotations

import copy
import json
import math
import pickle
import random
import re
from decimal import Decimal
from typing import Any

import orjson
import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec
from jsonreshape.codec import (
    JsonDecodeError,
    JsonFloat,
    JsonInt,
    dumps,
    is_number,
    is_plain_float_token,
    is_plain_int_token,
    json_type,
    loads,
    loads_precise,
    number_text,
    value_text,
)
from jsonreshape.errors import JsonReshapeError

REQUIRED_TOKENS = ["0.1", "1e400", "12345678901234567890", "-0.0", "1.10"]
ALL_TOKENS = list(dict.fromkeys(REQUIRED_TOKENS + list(jsongen.EDGE_NUMBER_TOKENS)))
STYLES = ("compact", "pretty2", "pretty4", "tab", "messy", "escaped", "messy_escaped")

# templates holding the token several times, in arrays and objects ("@" = the token)
TEMPLATES = [
    "@",
    "[@]",
    '{"a":@}',
    '[true,@,"@",{"b":@}]',
    '{"a":[null,@,{"b":@}],"c":{"d":[[@]],"e":"x"},"f":[]}',
]


def _reformatter_ok(indent: Any) -> bool:
    try:
        from jsonreshape.core import Reformatter

        Reformatter(indent=indent)
        return True
    except Exception:
        return False


requires_reformatter = pytest.mark.skipif(
    not _reformatter_ok(4),
    reason="needs jsonreshape.core.Reformatter (core backend not available yet)",
)


def fill(template: str, tok: str) -> str:
    # '"@"' stays a string containing '@' to prove strings are left alone
    return template.replace('"@"', "\0").replace("@", tok).replace("\0", '"@"')


def expected_indented(template: str, tok: str, indent: int | str) -> bytes:
    """json.dumps(indent=...) layout of the template with the raw token spliced in."""
    marker = "\u0001TOKEN\u0001"
    obj = json.loads(template.replace('"@"', '"\u0002"').replace("@", json.dumps(marker)).replace('"\u0002"', '"@"'))
    text = json.dumps(obj, indent=indent, ensure_ascii=False)
    return text.replace(json.dumps(marker), tok).encode("utf-8")


def number_leaves(v: Any) -> list[Any]:
    out: list[Any] = []
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
        elif is_number(x):
            out.append(x)
    return out


def same(a: Any, b: Any) -> bool:
    """Deep equality that also requires identical types and identical number text."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a.keys()) == list(b.keys()) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if is_number(a):
        return number_text(a) == number_text(b)
    return a == b


# ----------------------------------------------------------------------------------
# §12 / §14 test 24: exact number round trips
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("tok", ALL_TOKENS)
@pytest.mark.parametrize("template", TEMPLATES)
def test_number_tokens_survive_compact(tok: str, template: str) -> None:
    doc = fill(template, tok)
    value = loads(doc)
    assert dumps(value) == doc.encode()
    assert loads_precise(doc) is not None and same(loads_precise(doc), value)
    count = template.replace('"@"', "").count("@")
    leaves = [x for x in number_leaves(value) if number_text(x) == tok]
    assert len(leaves) == count  # still numbers (never strings), text preserved
    for x in leaves:
        assert json_type(x) == "number"
        assert not isinstance(x, str)
    if '"@"' in template:
        assert value[2] == "@"  # the string "@" stayed a string


@pytest.mark.parametrize("tok", ALL_TOKENS)
@pytest.mark.parametrize("template", TEMPLATES)
def test_number_tokens_survive_indent2(tok: str, template: str) -> None:
    doc = fill(template, tok)
    out = dumps(loads(doc), indent=2)
    assert out == expected_indented(template, tok, 2)
    assert dumps(loads(out)) == doc.encode()


@requires_reformatter
@pytest.mark.parametrize("indent", [4, "\t", 0])
@pytest.mark.parametrize("tok", REQUIRED_TOKENS + ["-0", "1E5", "18446744073709551616"])
@pytest.mark.parametrize("template", TEMPLATES)
def test_number_tokens_survive_other_indents(tok: str, template: str, indent: Any) -> None:
    doc = fill(template, tok)
    out = dumps(loads(doc), indent=indent)
    assert out == expected_indented(template, tok, indent)
    assert dumps(loads(out)) == doc.encode()


def test_required_tokens_types() -> None:
    v = loads("[0.1, 1e400, 12345678901234567890, -0.0, 1.10, -0, 1E5]")
    assert type(v[0]) is float and v[0] == 0.1
    assert isinstance(v[1], JsonFloat) and math.isinf(v[1]) and v[1].raw == "1e400"
    assert type(v[2]) is int and v[2] == 12345678901234567890  # fits uint64: plain int
    assert type(v[3]) is float and math.copysign(1.0, v[3]) == -1.0
    assert isinstance(v[4], JsonFloat) and v[4] == 1.1 and v[4].raw == "1.10"
    assert isinstance(v[5], JsonInt) and v[5] == 0 and v[5].raw == "-0"
    assert isinstance(v[6], JsonFloat) and v[6] == 100000.0
    big = loads("123456789012345678901234567890")
    assert isinstance(big, JsonInt) and big == 123456789012345678901234567890
    assert dumps(v) == b"[0.1,1e400,12345678901234567890,-0.0,1.10,-0,1E5]"


number_token = st.from_regex(r"-?(0|[1-9][0-9]{0,30})(\.[0-9]{1,25})?([eE][+-]?[0-9]{1,4})?", fullmatch=True)


@hsettings(deadline=None, max_examples=400)
@given(tok=number_token)
def test_any_number_token_roundtrips(tok: str) -> None:
    v = loads(tok)
    assert is_number(v) and json_type(v) == "number"
    assert number_text(v) == tok
    assert value_text(v) == tok
    assert dumps(v) == tok.encode()
    assert dumps([v, {"k": v}]) == f'[{tok},{{"k":{tok}}}]'.encode()
    assert same(loads(dumps({"k": [v]})), {"k": [v]})


@hsettings(deadline=None, max_examples=400)
@given(tok=number_token)
def test_plain_predicates_guarantee_orjson_roundtrip(tok: str) -> None:
    is_float = any(c in tok for c in ".eE")
    plain = is_plain_float_token(tok) if is_float else is_plain_int_token(tok)
    if plain:
        assert orjson.dumps(orjson.loads(tok)) == tok.encode()
        assert type(loads(tok)) is (float if is_float else int)
    else:
        assert type(loads(tok)) in (JsonFloat, JsonInt, int)


def test_plain_predicates_examples() -> None:
    for tok in ("0", "-1", "123456789012345678", "0.1", "-0.0", "0.0", "100.0", "3.14159265358979",
                "0.00001", "123456789012345.0"):
        assert (is_plain_float_token(tok) if "." in tok else is_plain_int_token(tok)), tok
    # conservative: 16 significant digits are never plain, even when orjson happens to
    # reproduce them (3.141592653589793)
    for tok in ("-0", "1234567890123456789", "1.10", "1e5", "1E5", "0.000001", "1.0e1", "12345678901234567.0",
                "0.1234567890123456", "3.141592653589793", "01", "1.", ".5", "abc", ""):
        assert not is_plain_float_token(tok), tok
        if "." not in tok and "e" not in tok.lower():
            assert not is_plain_int_token(tok), tok


# ----------------------------------------------------------------------------------
# random documents
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(25))
def test_random_documents_roundtrip_exactly(seed: int) -> None:
    rng = random.Random(seed)
    for i in range(8):
        value = jsongen.rand_value(rng, 0, max_depth=5, max_items=5)
        compact = jsongen.encode(value, "compact")
        for style in STYLES:
            text = jsongen.encode(value, style, seed=seed * 100 + i)
            parsed = loads(text)
            assert dumps(parsed) == compact, style
            assert same(parsed, loads_precise(text))
            assert same(loads(text.decode("utf-8")), parsed)
        assert dumps(loads(compact), indent=2) == dumps(loads(jsongen.encode(value, "pretty2")), indent=2)


def rand_plain_number(rng: random.Random) -> Any:
    if rng.random() < 0.4:
        return rng.randint(-(10**17), 10**17)
    while True:
        f = round(rng.uniform(-1e7, 1e7) * 10 ** rng.randint(-4, 0), rng.randint(0, 9))
        if is_plain_float_token(orjson.dumps(f).decode()):
            return f


def rand_plain_value(rng: random.Random, depth: int = 0) -> Any:
    r = rng.random()
    if depth >= 4 or r < 0.35:
        k = rng.random()
        if k < 0.4:
            return jsongen.rand_string(rng)
        if k < 0.8:
            return rand_plain_number(rng)
        return rng.choice([True, False, None])
    if r < 0.65:
        return [rand_plain_value(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    return {(rng.choice(jsongen.KEYS) if rng.random() < 0.6 else jsongen.rand_string(rng)): rand_plain_value(rng, depth + 1)
            for _ in range(rng.randint(0, 5))}


@pytest.mark.parametrize("seed", range(20))
def test_orjson_and_precise_agree_on_plain_documents(seed: int) -> None:
    rng = random.Random(seed)
    for i in range(15):
        value = rand_plain_value(rng)
        for text in (orjson.dumps(value), jsongen.encode(value, rng.choice(STYLES), seed=i)):
            a = orjson.loads(text)
            b = loads_precise(text)
            assert same(a, b), text[:200]


_TOKEN_RE = re.compile(rb'"(?:[^"\\]|\\.)*"|(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)', re.S)


def ref_numbers_plain(buf: bytes) -> bool:
    """Reference for core.numbers_plain (docs/ARCHITECTURE.md §1.3)."""
    for m in _TOKEN_RE.finditer(buf):
        tok = m.group(1)
        if tok is None:
            continue  # a string
        s = tok.decode("ascii")
        if any(c in s for c in ".eE"):
            if not is_plain_float_token(s):
                return False
        elif not is_plain_int_token(s):
            return False
    return True


def test_fast_path_dispatch_matches_precise(monkeypatch: pytest.MonkeyPatch) -> None:
    """codec.loads with a numbers_plain checker (as with the Rust core) == loads_precise."""
    calls = {"fast": 0, "precise": 0}

    def checker(buf: bytes) -> bool:
        plain = ref_numbers_plain(buf)
        calls["fast" if plain else "precise"] += 1
        return plain

    monkeypatch.setattr(codec, "_fast_checker", checker)
    rng = random.Random(42)
    for i in range(300):
        value = rand_plain_value(rng) if i % 2 else jsongen.rand_value(rng, 0, 4)
        text = jsongen.encode(value, rng.choice(STYLES), seed=i)
        expected = loads_precise(text)
        assert same(codec.loads(text), expected)
        assert same(codec.loads(text.decode("utf-8")), expected)
        assert same(codec.loads(bytearray(text)), expected)
        assert same(codec.loads(memoryview(text)), expected)
    assert calls["fast"] > 100 and calls["precise"] > 100
    # inputs orjson refuses although every number is plain: lone surrogates, raw surrogates
    for text in ('["\\ud800"]', '{"\\udfff":1}', '["\ud800"]'):
        assert same(codec.loads(text), loads_precise(text))
    with pytest.raises(JsonDecodeError):
        codec.loads("[1,]")


# ----------------------------------------------------------------------------------
# keys, strings, Unicode
# ----------------------------------------------------------------------------------
def test_duplicate_keys_last_wins_first_position(monkeypatch: pytest.MonkeyPatch) -> None:
    text = '{"a":1,"b":2,"a":3}'
    for fn in (loads, loads_precise, orjson.loads):
        v = fn(text)
        assert v == {"a": 3, "b": 2}
        assert list(v) == ["a", "b"]
    monkeypatch.setattr(codec, "_fast_checker", ref_numbers_plain)
    assert list(codec.loads(text)) == ["a", "b"] and codec.loads(text)["a"] == 3


def test_lone_surrogates() -> None:
    v = loads('["\\ud800", "a\\udfffb", "\\ud83d\\ude00", {"\\udc00": 1}]')
    assert v == ["\ud800", "a\udfffb", "😀", {"\udc00": 1}]
    out = dumps(v)
    assert out == '["\\ud800","a\\udfffb","😀",{"\\udc00":1}]'.encode("utf-8")
    assert loads(out) == v
    out.decode("utf-8")  # always valid UTF-8
    assert dumps("\udbff") == b'"\\udbff"'


@requires_reformatter
def test_lone_surrogates_indented() -> None:
    out = dumps({"k": ["\ud800", 1]}, indent=2)
    assert out == b'{\n  "k": [\n    "\\ud800",\n    1\n  ]\n}'


def test_non_ascii_is_never_escaped() -> None:
    value = {"shahar": "Тошкент", "emoji": "😀", "uz": "Oʻzbekiston ko‘chasi", "sep": "\u2028\u2029", "ß": "ﬁ"}
    for out in (dumps(value), dumps(value, indent=2), dumps(value, sort_keys=True),
                dumps([value, 2**70]), dumps([value, "\ud800"])):
        assert b"\\u0" not in out.replace(b"\\ud800", b"")
        text = out.decode("utf-8")
        for s in ("Тошкент", "😀", "Oʻzbekiston ko‘chasi", "\u2028\u2029", "ß", "ﬁ"):
            assert s in text
    # control characters are escaped exactly like orjson does
    assert dumps("a\x00\x1f\n\t\"\\/") == orjson.dumps("a\x00\x1f\n\t\"\\/")


def test_escaped_input_is_decoded() -> None:
    assert loads('"\\u0422\\u043e\\u0448\\u043a\\u0435\\u043d\\u0442 \\/ \\ud83d\\ude00"') == "Тошкент / 😀"
    assert dumps(loads('"\\u0422\\/"')) == '"Т/"'.encode()


# ----------------------------------------------------------------------------------
# big ints, invalid input, misc encoder behaviour
# ----------------------------------------------------------------------------------
def test_big_plain_python_ints() -> None:
    assert dumps(2**64) == b"18446744073709551616"
    assert dumps(-(2**63) - 1) == b"-9223372036854775809"
    assert dumps(2**64 - 1) == b"18446744073709551615"
    assert dumps([1, 2**100, {"x": -(2**80), "s": "Тошкент"}]) == (
        '[1,1267650600228229401496703205376,{"x":-1208925819614629174706176,"s":"Тошкент"}]'.encode()
    )
    assert dumps({"b": [2**70], "a": 1}, sort_keys=True) == b'{"a":1,"b":[1180591620717411303424]}'
    assert loads(dumps(10**300)) == 10**300


@requires_reformatter
def test_big_plain_python_ints_indented() -> None:
    assert dumps([2**70], indent=2) == b"[\n  1180591620717411303424\n]"


def test_nan_and_infinity_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = ["NaN", "[NaN]", "Infinity", "-Infinity", '{"a": Infinity}', "[1, -Infinity]", "nan", "inf"]
    for text in bad:
        with pytest.raises(JsonDecodeError):
            loads(text)
        with pytest.raises(JsonDecodeError):
            loads_precise(text)
    monkeypatch.setattr(codec, "_fast_checker", ref_numbers_plain)
    for text in bad:
        with pytest.raises(JsonDecodeError):
            codec.loads(text)
    # the encoder never emits NaN/Infinity (output stays valid JSON)
    out = dumps([float("nan"), float("inf"), -float("inf")])
    assert b"NaN" not in out and b"Infinity" not in out
    json.loads(out)


@pytest.mark.parametrize(
    "text",
    ["", " ", "[", "[1,]", "{'a':1}", "01", "[1] x", "tru", '"\x01"', "\ufeff[]", '{"a" 1}', "[1 2]", "-", "1.", ".5"],
)
def test_invalid_json_raises_localized_error(text: str, lang_en) -> None:
    with pytest.raises(JsonDecodeError) as info:
        loads(text)
    exc = info.value
    assert isinstance(exc, JsonReshapeError)
    assert str(exc).startswith("Could not decode JSON text")
    assert isinstance(exc.pos, int)


def test_invalid_utf8_reports_byte_position() -> None:
    with pytest.raises(JsonDecodeError) as info:
        loads(b'["ok", "\xff"]')
    assert info.value.pos == 8


def test_deep_nesting_never_crashes() -> None:
    for depth in (100, 2000, 200000):
        text = "[" * depth + "]" * depth
        try:
            v = loads(text)
        except JsonDecodeError:
            continue
        d = 0
        while v:
            v = v[0]
            d += 1
        assert d == depth - 1
    deep: list[Any] = []
    cur = deep
    for _ in range(5000):  # deeper than orjson's limit: pure-Python encoder
        nxt: list[Any] = []
        cur.append(nxt)
        cur = nxt
    assert dumps(deep) == b"[" * 5001 + b"]" * 5001


def test_nesting_beyond_the_decode_limit_is_refused_everywhere() -> None:
    """The same records are "too deep to decode in memory" on every platform, whatever its json module could
    decode (Python 3.14 with an unlimited Linux stack decodes millions of levels)."""
    limit = codec.DECODE_MAX_DEPTH
    for opener, closer in (("[", "]"), ('{"k":', "}")):
        for depth in (limit + 1, 3 * limit):
            text = opener * depth + "1" + closer * depth
            for data in (text, text.encode("utf-8")):
                with pytest.raises(JsonDecodeError):
                    loads(data)
                with pytest.raises(JsonDecodeError):
                    codec.loads_precise(data)
    # the limit is the nesting, not the number of brackets: wide records and a deep one inside a big record
    wide = "[" + ",".join(["[{}]"] * (2 * limit)) + "]"
    assert loads(wide) == [[{}]] * (2 * limit)
    inner = "[" * 900 + "7" + "]" * 900
    v = loads('{"pad": "' + "[" * (3 * limit) + '", "deep": ' + inner + ', "n": 1.10}')
    assert v["pad"] == "[" * (3 * limit) and str(v["n"]) == "1.10"
    assert codec._deeper_than(v, 901) is False and codec._deeper_than(v, 900) is True     # the record is level 1
    assert codec._deeper_than(1, 0) is False and codec._deeper_than([], 1) is False and codec._deeper_than([[]], 1)


def test_encoder_misc() -> None:
    assert dumps(JsonFloat("1.10")) == b"1.10"
    assert dumps(JsonInt("-0")) == b"-0"
    assert dumps((1, [2, (3,)])) == b"[1,[2,[3]]]"
    assert dumps({"b": 1, "a": {"d": 1, "c": 2}}, sort_keys=True) == b'{"a":{"c":2,"d":1},"b":1}'
    assert dumps([Decimal("1.10"), Decimal("-0"), Decimal("1E+2")]) == b"[1.10,-0,1E+2]"
    assert dumps(None) == b"null" and dumps(True) == b"true" and dumps("") == b'""'
    assert dumps({}) == b"{}" and dumps([], indent=2) == b"[]" and dumps({"a": []}, indent=2) == b'{\n  "a": []\n}'
    with pytest.raises(TypeError):
        dumps(object())
    with pytest.raises(TypeError):
        dumps(Decimal("NaN"))
    with pytest.raises(TypeError):
        dumps({1: "int keys are not JSON"})


# ----------------------------------------------------------------------------------
# JsonFloat / JsonInt behave like numbers
# ----------------------------------------------------------------------------------
def test_jsonfloat_behaves_like_float() -> None:
    x = JsonFloat("1.10")
    assert isinstance(x, float) and x == 1.1 and x == JsonFloat("1.1000")
    assert x + 1 == pytest.approx(2.1) and x * 2 == pytest.approx(2.2) and -x == -1.1
    assert x < 2 and x > 1 and x <= 1.1 and 1 < x
    assert math.floor(x) == 1 and round(x, 1) == 1.1 and float(x) == 1.1 and int(x) == 1
    assert hash(x) == hash(1.1) and {x: "v"}[1.1] == "v"
    assert str(x) == repr(x) == "1.10" and f"{x}" == "1.10"
    assert sorted([JsonFloat("2.50"), 1, JsonFloat("1e400"), -1.5]) == [-1.5, 1, 2.5, float("inf")]
    assert math.isinf(JsonFloat("1e400")) and JsonFloat("-1e400") < 0 and JsonFloat("1e-400") == 0.0
    assert json_type(x) == "number" and is_number(x) and number_text(x) == "1.10"
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        y = pickle.loads(pickle.dumps(x, protocol=proto))
        assert isinstance(y, JsonFloat) and y.raw == "1.10" and y == x
    assert copy.copy(x).raw == "1.10" and copy.deepcopy([x])[0].raw == "1.10"
    assert dumps(copy.deepcopy({"v": x})) == b'{"v":1.10}'


def test_jsonint_behaves_like_int() -> None:
    n = JsonInt("123456789012345678901234567890")
    assert isinstance(n, int) and n == 123456789012345678901234567890
    assert n + 1 == 123456789012345678901234567891 and n - n == 0 and n // 10 == 12345678901234567890123456789
    assert n > 2**64 and -n < 0 and n % 7 == 123456789012345678901234567890 % 7
    assert hash(n) == hash(123456789012345678901234567890)
    assert str(n) == repr(n) == "123456789012345678901234567890"
    z = JsonInt("-0")
    assert z == 0 and not z and str(z) == "-0" and dumps(z) == b"-0" and z + 5 == 5
    assert [JsonInt("-0")][0] in (0,)
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        m = pickle.loads(pickle.dumps(z, protocol=proto))
        assert isinstance(m, JsonInt) and m.raw == "-0"
    assert copy.copy(n).raw == n.raw and copy.deepcopy({"a": n})["a"].raw == n.raw
    assert json_type(n) == "number" and number_text(n) == n.raw
    assert sorted([3, JsonInt("-0"), n, -5]) == [-5, 0, 3, n]


def test_helpers() -> None:
    assert [json_type(v) for v in ({}, [], (), "", 1, 1.5, True, None, Decimal("1"))] == [
        "object", "array", "array", "string", "number", "number", "bool", "null", "number",
    ]
    with pytest.raises(TypeError):
        json_type(object())
    assert not is_number(True) and is_number(Decimal("1.5")) and not is_number("1")
    assert number_text(0.1) == "0.1" and number_text(-0.0) == "-0.0"
    assert number_text(1e16) == orjson.dumps(1e16).decode() == "1e+16"  # orjson's float format
    assert number_text(10**30) == "1" + "0" * 30 and number_text(Decimal("1.50")) == "1.50"
    assert number_text(float("nan")) == "NaN" and number_text(float("-inf")) == "-Infinity"
    assert value_text("x") == "x" and value_text(None) == "null" and value_text(False) == "false"
    assert value_text([1]) is None and value_text({}) is None
    with pytest.raises(TypeError):
        number_text("1")


@hsettings(deadline=None, max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(value=st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False, allow_infinity=False),
              st.text()),
    lambda children: st.one_of(st.lists(children, max_size=4), st.dictionaries(st.text(max_size=5), children, max_size=4)),
    max_leaves=25,
))
def test_dumps_loads_roundtrip_python_values(value: Any) -> None:
    out = dumps(value)
    back = loads(out)
    assert back == value
    assert dumps(back) == out
    assert json.loads(out) == value or any(isinstance(x, float) for x in number_leaves(value))


def test_dumps_reindent_large_values_in_bounded_slices(monkeypatch):
    """Indent 4 / tab output of values larger than one feed slice equals one-shot output."""
    from jsonreshape import codec

    value = [{"k": i, "s": "x" * 50, "n": [1.10 if False else 1.5, None]} for i in range(3000)]
    expected = codec.dumps(value, indent=4)
    monkeypatch.setattr(codec, "_REINDENT_SLICE", 997)
    assert codec.dumps(value, indent=4) == expected
    assert codec.loads(codec.dumps(value, indent="\t")) == value
