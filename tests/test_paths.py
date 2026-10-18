"""jsonreshape.paths: every syntax of docs/ARCHITECTURE.md §3, formatting round trips,
syntax errors with positions, PathPattern matching/resolution against a brute-force
reference, get() index coercion and parse_location (Ctrl+G, AGENT_PROMPT §5)."""

from __future__ import annotations

import random
import re
from typing import Any

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

import jsongen
from jsonreshape.errors import PathSyntaxError
from jsonreshape.i18n import t
from jsonreshape.paths import (
    MISSING,
    Location,
    PathPattern,
    Wild,
    canonical_index,
    format_path,
    get,
    iter_leaves,
    parse_location,
    parse_path,
    parse_pattern,
)

STYLES = ("dotted", "jsonpath", "pointer", "python", "js")
PATTERN_STYLES = ("dotted", "jsonpath", "python", "js")  # pointer cannot express wildcards

# ----------------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------------
PATH_CASES = [
    ("a", ("a",)),
    ("a.b[0].c", ("a", "b", 0, "c")),
    ('a["x.y"]', ("a", "x.y")),
    ("a['x']", ("a", "x")),
    ("a['it\\'s']", ("a", "it's")),
    ("", ()),
    ("$", ()),
    ("$.a", ("a",)),
    ("$.a.b[2]", ("a", "b", 2)),
    ("$[0]", (0,)),
    ('$["a b"]', ("a b",)),
    ("$['a']['b']", ("a", "b")),
    ("[0][1]", (0, 1)),
    ('["a"]["b"][0]["x y"]', ("a", "b", 0, "x y")),
    ("/a/0", ("a", "0")),
    ("/", ("",)),
    ("/a~1b/c~0d", ("a/b", "c~d")),
    ("/~01", ("~1",)),
    ("/a//b", ("a", "", "b")),
    ("/x y", ("x y",)),
    ("  a.b  ", ("a", "b")),
    ("a[ 0 ]", ("a", 0)),
    ("a[ 'k' ]", ("a", "k")),
    ('a["\\u0442\\n\\"q\\\\\\/"]', ("a", "т\n\"q\\/")),
    ("a.Тошкент.шаҳар", ("a", "Тошкент", "шаҳар")),
    ("a-b.c_d.$e", ("a-b", "c_d", "$e")),
    ("data[120].contact.address", ("data", 120, "contact", "address")),
    ("a.0", ("a", "0")),  # unquoted digits stay a str key (acting as an index on lists)
    ("a[007]", ("a", 7)),
    (".a", ("a",)),
    ("a*b.c", ("a*b", "c")),
    ('a["*"]', ("a", "*")),
    ("contact.address.city", ("contact", "address", "city")),
]


@pytest.mark.parametrize("text, expected", PATH_CASES)
def test_parse_path_syntaxes(text: str, expected: tuple) -> None:
    assert parse_path(text) == expected
    pattern = parse_pattern(text)
    assert pattern.segments == expected
    assert pattern.is_concrete
    assert pattern.to_path() == expected
    assert pattern.text == text.strip()


PATTERN_CASES = [
    ("*", (Wild.ANY_KEY,)),
    ("$.*", (Wild.ANY_KEY,)),
    ("a.*", ("a", Wild.ANY_KEY)),
    ("a[*]", ("a", Wild.ANY_INDEX)),
    ("a[]", ("a", Wild.ANY_INDEX)),
    ("a[ * ]", ("a", Wild.ANY_INDEX)),
    ("a[*].b", ("a", Wild.ANY_INDEX, "b")),
    ("a[*][*]", ("a", Wild.ANY_INDEX, Wild.ANY_INDEX)),
    ("**", (Wild.DESCENDANTS,)),
    ("a.**.b", ("a", Wild.DESCENDANTS, "b")),
    ("**[0]", (Wild.DESCENDANTS, 0)),
    ("$..name", (Wild.DESCENDANTS, "name")),
    ("..name", (Wild.DESCENDANTS, "name")),
    ("a..b", ("a", Wild.DESCENDANTS, "b")),
    ("$..[0]", (Wild.DESCENDANTS, 0)),
    ("$..*", (Wild.DESCENDANTS, Wild.ANY_KEY)),
    ("$..['x y']", (Wild.DESCENDANTS, "x y")),
    ("orders[].oid", ("orders", Wild.ANY_INDEX, "oid")),
]


@pytest.mark.parametrize("text, expected", PATTERN_CASES)
def test_parse_pattern_wildcards(text: str, expected: tuple) -> None:
    pattern = parse_pattern(text)
    assert pattern.segments == expected
    assert not pattern.is_concrete
    with pytest.raises(ValueError):
        pattern.to_path()
    with pytest.raises(PathSyntaxError):
        parse_path(text)


def test_pattern_equality_and_hash() -> None:
    a = parse_pattern("$.a[*]")
    b = parse_pattern("a[]")
    assert a == b and hash(a) == hash(b)
    assert a != parse_pattern("a.*")
    assert PathPattern(("a", Wild.ANY_INDEX)).text == "a[*]"
    assert "a" in repr(a)


def test_to_core() -> None:
    assert parse_pattern("a[0].*[*].b").to_core() == [
        ("key", "a"), ("index", 0), ("any_key",), ("any_index",), ("key", "b"),
    ]
    assert parse_pattern("").to_core() == []
    with pytest.raises(ValueError):
        parse_pattern("a.**").to_core()


# ----------------------------------------------------------------------------------
# errors with positions
# ----------------------------------------------------------------------------------
ERROR_CASES = [
    # (text, wildcards allowed, expected pos, detail key)
    ("a.", False, 2, "paths.err.key_after_dot"),
    ("a[", False, 1, "paths.err.unterminated_bracket"),
    ("a[x]", False, 2, "paths.err.bad_bracket"),
    ("a[-1]", False, 2, "paths.err.negative_index"),
    ("a[1", False, 1, "paths.err.expected_close"),
    ("a[1x]", False, 3, "paths.err.expected_close"),
    ('a["x', False, 2, "paths.err.unterminated_quote"),
    ('a["x\\', False, 4, "paths.err.unterminated_escape"),
    ('a["\\uZZZZ"]', False, 3, "paths.err.bad_u_escape"),
    ('a["\\q"]', False, 3, "paths.err.bad_escape"),
    ("a.*", False, 2, "paths.err.wildcard_not_allowed"),
    ("a[*]", False, 1, "paths.err.wildcard_not_allowed"),
    ("a..b", False, 1, "paths.err.wildcard_not_allowed"),
    ("$x", False, 1, "paths.err.expected_dot_after_root"),
    ("a b", False, 1, "paths.err.unexpected_char"),
    ("a]", False, 1, "paths.err.unexpected_char"),
    ("a.[0]", False, 2, "paths.err.bracket_after_dot"),
    ("a..", True, 3, "paths.err.key_after_descendants"),
    ("$...a", True, 3, "paths.err.empty_key"),
    ("/a~2", False, 1, "paths.err.pointer_tilde"),
    ("/a/b~", False, 3, "paths.err.pointer_tilde"),
]


@pytest.mark.parametrize("text, wild, pos, key", ERROR_CASES)
def test_syntax_errors_have_positions(text: str, wild: bool, pos: int, key: str, lang_en) -> None:
    parser = parse_pattern if wild else parse_path
    with pytest.raises(PathSyntaxError) as info:
        parser(text)
    exc = info.value
    assert exc.pos == pos, (text, exc.pos)
    assert exc.text == text.strip()
    if key == "paths.err.unexpected_char":
        expected_detail = t(key, char=repr(text[pos]))
    elif key == "paths.err.bad_escape":
        expected_detail = t(key, char=text[pos + 1])
    else:
        expected_detail = t(key)
    assert exc.params["detail"] == expected_detail
    message = str(exc)
    assert str(pos + 1) in message  # shown 1-based
    caret_line = exc.caret().splitlines()[1]
    assert caret_line == " " * pos + "^"


def test_syntax_error_is_localized(lang_uz) -> None:
    with pytest.raises(PathSyntaxError) as info:
        parse_path("a[")
    assert "Yo'l noto'g'ri" in str(info.value)
    assert info.value.hint


# ----------------------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------------------
def test_format_styles() -> None:
    p = ("a", "b", 0, "x y")
    assert format_path(p, "dotted") == 'a.b[0]["x y"]'
    assert format_path(p, "jsonpath") == "$.a.b[0]['x y']"
    assert format_path(p, "pointer") == "/a/b/0/x y"
    assert format_path(p, "python") == '["a"]["b"][0]["x y"]'
    assert format_path(p, "js") == 'a.b[0]["x y"]'
    assert format_path(("a/b", "c~d"), "pointer") == "/a~1b/c~0d"
    assert format_path((), "dotted") == ""
    assert format_path((), "jsonpath") == "$"
    assert format_path((), "pointer") == ""
    assert format_path(("Тошкент",), "dotted") == "Тошкент"
    with pytest.raises(ValueError):
        format_path(("a",), "yaml")
    with pytest.raises(ValueError):
        format_path(("a", Wild.ANY_KEY), "pointer")


WEIRD_KEYS = [
    "", " ", "a b", " lead", "trail ", "trail\t", "0", "00", "01", "-1", "1.5", "1e3", "*", "**", "[*]",
    "$", "$a", "$.a", "#1", "#", "/", "/a", "a/b", "~", "~0", "~1", ".", "..", "a.b", "[", "]", "[0]",
    "'", '"', "it's", '"q"', "\\", "\\u0041", "\n", "\r\n", "\t", "\x00", "\x1f", "\x7f", "Тошкент",
    "O'zbekiston", "Oʻzbekiston", "😀", "\u2028", " ", "a[0]", "true", "null", "\ud800", "a\udfff",
    "ﬁle", "a\\b", "$$", "a$", "_", "__proto__", "constructor",
] + [k for k in jsongen.KEYS]

key_strategy = st.one_of(
    st.sampled_from(WEIRD_KEYS),
    st.text(max_size=8),
    st.text(alphabet="ab.[]'\"\\ *$#/~0123456789", max_size=6),
)
segment_strategy = st.one_of(key_strategy, st.integers(min_value=0, max_value=10**12))
path_strategy = st.lists(segment_strategy, max_size=6).map(tuple)


def _expected_after_roundtrip(path: tuple, style: str) -> tuple:
    if style == "pointer":
        return tuple(str(s) for s in path)
    return path


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("key", WEIRD_KEYS)
def test_roundtrip_weird_keys(style: str, key: str) -> None:
    for path in ((key,), ("a", key), (key, 0, key), (0, key)):
        text = format_path(path, style)
        assert parse_path(text) == _expected_after_roundtrip(path, style), (style, path, text)


@pytest.mark.parametrize("style", STYLES)
@hsettings(deadline=None, max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(path=path_strategy)
def test_roundtrip_random_paths(style: str, path: tuple) -> None:
    text = format_path(path, style)
    assert parse_path(text) == _expected_after_roundtrip(path, style)


wild_strategy = st.sampled_from([Wild.ANY_KEY, Wild.ANY_INDEX, Wild.DESCENDANTS])
pattern_strategy = st.lists(st.one_of(segment_strategy, wild_strategy), max_size=6).map(tuple)


@pytest.mark.parametrize("style", PATTERN_STYLES)
@hsettings(deadline=None, max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(segments=pattern_strategy)
def test_roundtrip_random_patterns(style: str, segments: tuple) -> None:
    text = format_path(segments, style)
    assert parse_pattern(text).segments == segments, text


@pytest.mark.parametrize(
    "segments",
    [
        (Wild.DESCENDANTS, "name"),
        (Wild.DESCENDANTS, 0),
        (Wild.DESCENDANTS, Wild.ANY_KEY),
        (Wild.DESCENDANTS, Wild.ANY_INDEX),
        (Wild.DESCENDANTS, "x y"),
        ("a", Wild.DESCENDANTS),
        (Wild.DESCENDANTS, Wild.DESCENDANTS, "a"),
        ("a", Wild.ANY_KEY, Wild.DESCENDANTS, "b", Wild.ANY_INDEX),
    ],
)
@pytest.mark.parametrize("style", PATTERN_STYLES)
def test_roundtrip_descendant_patterns(segments: tuple, style: str) -> None:
    text = format_path(segments, style)
    assert parse_pattern(text).segments == segments, text


def test_jsonpath_descendants_use_double_dot() -> None:
    assert format_path((Wild.DESCENDANTS, "name"), "jsonpath") == "$..name"
    assert format_path(("a", Wild.DESCENDANTS, "b"), "jsonpath") == "$.a..b"


# ----------------------------------------------------------------------------------
# navigation: get()
# ----------------------------------------------------------------------------------
def test_get_and_index_coercion() -> None:
    doc = {"a": [10, {"b": "x"}], "0": "zero", "": {"": 1}}
    assert get(doc, ()) is doc
    assert get(doc, ("a", 0)) == 10
    assert get(doc, ("a", "0")) == 10  # canonical str acts as an index on lists
    assert get(doc, ("a", "1", "b")) == "x"
    assert get(doc, ("a", "01")) is MISSING  # not canonical
    assert get(doc, ("a", "-1")) is MISSING
    assert get(doc, ("a", 2)) is MISSING
    assert get(doc, ("a", -1)) is MISSING
    assert get(doc, ("0",)) == "zero"
    assert get(doc, (0,)) is MISSING  # an int never selects an object member
    assert get(doc, ("", "")) == 1
    assert get(doc, ("a", 0, "x")) is MISSING  # through a scalar
    assert get(doc, ("nope",), default=None) is None
    assert get(None, ("a",)) is MISSING
    assert get("text", (0,)) is MISSING


def test_missing_sentinel() -> None:
    assert not MISSING
    assert repr(MISSING) == "MISSING"
    import copy
    import pickle

    assert pickle.loads(pickle.dumps(MISSING)) is MISSING
    assert copy.deepcopy(MISSING) is MISSING


def test_canonical_index() -> None:
    assert canonical_index("0") == 0
    assert canonical_index("120") == 120
    for bad in ("", "00", "01", "-1", "1.0", " 1", "1e3", "١"):
        assert canonical_index(bad) is None


# ----------------------------------------------------------------------------------
# PathPattern vs brute force
# ----------------------------------------------------------------------------------
def all_nodes(value: Any) -> list[tuple[tuple, Any]]:
    """Every (path, value) in document (pre-)order; ints for array positions."""
    out: list[tuple[tuple, Any]] = []

    def rec(p: tuple, v: Any) -> None:
        out.append((p, v))
        if isinstance(v, dict):
            for k, c in v.items():
                rec(p + (k,), c)
        elif isinstance(v, list):
            for i, c in enumerate(v):
                rec(p + (i,), c)

    rec((), value)
    return out


_CANON = re.compile(r"0|[1-9][0-9]*")


def ref_seg(pseg: Any, seg: Any) -> bool:
    if pseg is Wild.ANY_INDEX:
        return isinstance(seg, int)
    if pseg is Wild.ANY_KEY:
        return isinstance(seg, str)
    if isinstance(pseg, int):
        return isinstance(seg, int) and seg == pseg
    if isinstance(seg, str):
        return seg == pseg
    return _CANON.fullmatch(pseg) is not None and int(pseg) == seg


def ref_match(psegs: tuple, path: tuple) -> bool:
    if not psegs:
        return not path
    head, rest = psegs[0], psegs[1:]
    if head is Wild.DESCENDANTS:
        return any(ref_match(rest, path[k:]) for k in range(len(path) + 1))
    return bool(path) and ref_seg(head, path[0]) and ref_match(rest, path[1:])


def random_pattern(rng: random.Random, nodes: list[tuple[tuple, Any]]) -> tuple:
    base = list(rng.choice(nodes)[0])
    segs: list[Any] = []
    i = 0
    while i < len(base):
        seg = base[i]
        r = rng.random()
        if r < 0.15:
            segs.append(Wild.DESCENDANTS)
            i += rng.randint(0, 2)
            continue
        if r < 0.4:
            segs.append(Wild.ANY_INDEX if isinstance(seg, int) else Wild.ANY_KEY)
        elif r < 0.5 and isinstance(seg, int):
            segs.append(str(seg))  # str segment acting as an index
        elif r < 0.55:
            segs.append(rng.choice(["0", 0, "id", "zzz", Wild.ANY_KEY, Wild.ANY_INDEX]))
        else:
            segs.append(seg)
        i += 1
    if rng.random() < 0.15:
        segs.append(rng.choice([Wild.DESCENDANTS, Wild.ANY_KEY, Wild.ANY_INDEX, "name", 0]))
    return tuple(segs)


@pytest.mark.parametrize("seed", range(40))
def test_pattern_against_brute_force(seed: int) -> None:
    rng = random.Random(seed)
    doc = jsongen.rand_value(rng, 0, max_depth=5, max_items=4) if seed % 2 else jsongen.records(3, seed)
    nodes = all_nodes(doc)
    paths = [p for p, _ in nodes]
    for _ in range(40):
        segs = random_pattern(rng, nodes)
        pattern = PathPattern(segs)
        expected = [(p, v) for p, v in nodes if ref_match(segs, p)]
        got = pattern.resolve(doc)
        assert [p for p, _ in got] == [p for p, _ in expected], segs
        assert all(a is b for (_, a), (_, b) in zip(got, expected))
        assert [v for _, v in got] == pattern.values(doc) or all(
            a is b for a, b in zip(pattern.values(doc), [v for _, v in got])
        )
        for p in paths:
            assert pattern.matches(p) == ref_match(segs, p), (segs, p)
            assert pattern.matches_prefix_of(p) == any(ref_match(segs, p[:k]) for k in range(len(p) + 1)), (segs, p)


@pytest.mark.parametrize("seed", range(10))
def test_get_matches_every_node(seed: int) -> None:
    rng = random.Random(1000 + seed)
    doc = jsongen.rand_value(rng, 0, max_depth=5, max_items=4)
    for p, v in all_nodes(doc):
        assert get(doc, p) is v
        as_str = tuple(str(s) if isinstance(s, int) else s for s in p)
        assert get(doc, as_str) is v  # pointer-style segments navigate arrays too
        pointer = format_path(p, "pointer")
        assert get(doc, parse_path(pointer)) is v
    assert [p for p, _ in iter_leaves(doc)] == [p for p, _ in all_nodes(doc)]


def test_resolve_semantics_examples() -> None:
    doc = {"orders": [{"oid": 7}, {"oid": 8}, {"x": 1}], "a": {"b": {"a": {"b": 2}}}, "0": {"oid": 9}}
    assert parse_pattern("orders[*].oid").values(doc) == [7, 8]
    assert parse_pattern("orders[].oid").resolve(doc) == [(("orders", 0, "oid"), 7), (("orders", 1, "oid"), 8)]
    assert parse_pattern("orders.1.oid").resolve(doc) == [(("orders", 1, "oid"), 8)]
    assert parse_pattern("$..oid").values(doc) == [7, 8, 9]
    assert [p for p, _ in parse_pattern("**.b").resolve(doc)] == [("a", "b"), ("a", "b", "a", "b")]
    assert parse_pattern("*.oid").values(doc) == [9]
    assert parse_pattern("[0]").values(doc) == []  # an index never selects an object member
    assert parse_pattern("nope.x").values(doc) == []
    assert parse_pattern("$").resolve(doc) == [((), doc)]
    # '**' also matches the node itself; duplicates from '**.**' are removed
    assert len(parse_pattern("**.**").resolve(doc)) == len(all_nodes(doc))
    assert parse_pattern("a.**").matches(("a",))
    assert parse_pattern("a.b").matches_prefix_of(("a", "b", "a"))
    assert not parse_pattern("a.b").matches_prefix_of(("a",))
    assert parse_pattern("a[*]").matches(("a", 3)) and not parse_pattern("a[*]").matches(("a", "3"))
    assert parse_pattern("a.*").matches(("a", "k")) and not parse_pattern("a.*").matches(("a", 0))
    assert parse_pattern("a.0").matches(("a", 0)) and parse_pattern("a.0").matches(("a", "0"))
    assert not parse_pattern("a[0]").matches(("a", "0"))  # "0" here is an object key


# ----------------------------------------------------------------------------------
# parse_location (Ctrl+G)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, record, path",
    [
        ("#120", 120, ()),
        ("#0", 0, ()),
        ("# 5", 5, ()),
        ("  #120  ", 120, ()),
        ("#120.a", 120, ("a",)),
        ("#120.contact.address", 120, ("contact", "address")),
        ("#120[0]", 120, (0,)),
        ('#120["x y"]', 120, ("x y",)),
        ("#120/a/0", 120, ("a", "0")),
        ("data[120].contact.address", None, ("data", 120, "contact", "address")),
        ("$.data[120]", None, ("data", 120)),
        ("/data/120", None, ("data", "120")),
        ("", None, ()),
        ("$", None, ()),
    ],
)
def test_parse_location(text: str, record: int | None, path: tuple) -> None:
    loc = parse_location(text)
    assert isinstance(loc, Location)
    assert loc.record == record
    assert loc.path == path
    assert loc.text == text.strip()


@pytest.mark.parametrize(
    "text, pos",
    [
        ("#", 1),
        ("#x", 1),
        ("#12abc", 3),
        ("#12.", 4),      # error inside the rest: position in the original text
        ("#12[", 3),
        ("#12/a~2", 4),
        ("#12[0]]", 6),
    ],
)
def test_parse_location_errors(text: str, pos: int) -> None:
    with pytest.raises(PathSyntaxError) as info:
        parse_location(text)
    assert info.value.pos == pos, info.value.pos
    assert info.value.text == text.strip()


def test_dotted_format_quotes_control_characters():
    from jsonreshape.paths import format_path, parse_path

    for cp in (0, 7, 0x1B, 0x7F):
        key = "a" + chr(cp) + "b"
        text = format_path(("x", key), "dotted")
        assert text.startswith('x["')  # quoted form, never a bare key
        if cp < 0x20:
            assert chr(cp) not in text  # control characters are JSON-escaped
        assert parse_path(text) == ("x", key)
