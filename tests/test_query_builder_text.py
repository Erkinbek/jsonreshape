"""query.builder_from_text / builder_from_expr: expression text -> visual builder tree.

The inverse of ``Group.to_text()`` (docs/ARCHITECTURE.md §9, §11.8 "text -> builder when the
expression is representable"; AGENT_PROMPT §14 test 18):

* for every builder tree ``G``: ``builder_from_text(G.to_text()).to_text() == G.to_text()``
  (property test over generated trees: every operator, odd field names, wildcards, exact
  numbers, nested / negated / empty groups);
* for every representable text ``T`` (generated with the many spellings the parser accepts:
  ``=``, ``≠``, ``&&``, ``!``, ``is not null``, ``not in``, ``endswith``, ``empty(x)``, mixed
  precedence without parentheses ...): the builder's text filters exactly the same records
  as ``T`` (generated records, ``$key`` and ``$index`` included);
* everything else gives ``None``; invalid text raises the parser's QuerySyntaxError.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jsonreshape import codec
from jsonreshape.errors import QuerySyntaxError
from jsonreshape.paths import Wild, format_path, parse_pattern
from jsonreshape.query import (
    BUILDER_OPS,
    Condition,
    Filter,
    Group,
    builder_from_expr,
    builder_from_text,
    compile_filter,
    parse_filter,
)

# ----------------------------------------------------------------------------------
# strategies: builder trees
# ----------------------------------------------------------------------------------
_KEY_CHARS = st.characters(blacklist_categories=("Cs",))
_SEGMENT = st.one_of(
    st.sampled_from(["a", "b", "name", "and", "x y", "$key", "$x", "0", "-1", "#n", "my.key", 'q"uote', "",
                     "*", "**", "o'g'il", "Toshkent", "a\\b", "tab\there"]),
    st.text(_KEY_CHARS, max_size=5),
    st.integers(0, 12),
    st.sampled_from([Wild.ANY_INDEX, Wild.ANY_KEY, Wild.DESCENDANTS]),
)
_FIELD = st.one_of(
    st.sampled_from(["$", "$key", "$index", "name", "contact.city", "orders[*].id", "tags[0]", '["and"]',
                     "x.and", "*", "**.id", "a.*.b", '$["my key"]']),
    st.lists(_SEGMENT, min_size=1, max_size=4).map(lambda segs: format_path(tuple(segs))),
)
_NUMBER_TEXTS = ["1.10", "1e400", "-0", "0.5", "12345678901234567890", "1E-400", "2.50e+3"]
_SCALAR = st.one_of(
    st.none(), st.booleans(), st.integers(-10**20, 10**20),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(_KEY_CHARS, max_size=6),
    st.sampled_from(_NUMBER_TEXTS).map(codec.loads),
)
_VALUE = st.recursive(
    _SCALAR,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(_KEY_CHARS, max_size=3), inner, max_size=3),
    max_leaves=6,
)
_REGEX = st.one_of(st.sampled_from(["^a", "x+", "[0-9]{2}", "", "a|b", r"\d", "(?i)tosh"]),
                   st.text(_KEY_CHARS, max_size=4).map(re.escape))
_NON_REGEX_OPS = [op for op in BUILDER_OPS if op != "regex"]
_CONDITION = st.one_of(
    st.builds(Condition, field=_FIELD, op=st.sampled_from(_NON_REGEX_OPS), value=_VALUE, value2=_VALUE),
    st.builds(Condition, field=_FIELD, op=st.just("regex"), value=_REGEX),
)
_TREE = st.recursive(
    _CONDITION,
    lambda kids: st.builds(Group, op=st.sampled_from(["and", "or", "OR"]), items=st.lists(kids, max_size=4),
                           negate=st.booleans()),
    max_leaves=10,
)
_GROUP = st.builds(Group, op=st.sampled_from(["and", "or"]), items=st.lists(_TREE, max_size=4),
                   negate=st.booleans())


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_GROUP)
def test_builder_text_round_trip(group: Group) -> None:
    text = group.to_text()
    back = builder_from_text(text)
    assert back is not None, text
    assert back.to_text() == text
    # and the round trip is stable
    assert builder_from_text(back.to_text()).to_text() == text


@settings(max_examples=150, deadline=None)
@given(st.lists(_SEGMENT, max_size=5))
def test_field_text_round_trip(segs: list) -> None:
    """The builder field written for a parsed path reads back to the same path."""
    field = format_path(tuple(segs)) or "$"
    group = builder_from_text(Condition(field, "exists").to_text())
    assert group is not None
    (cond,) = group.items
    assert parse_pattern(cond.field).segments == tuple(segs)


# ----------------------------------------------------------------------------------
# strategies: representable texts + records
# ----------------------------------------------------------------------------------
_KEYS = ["a", "b", "c", "tags", "x y", "my key", "n"]
_STRINGS = ["", "a", "ab", "b", "x.y", "Toshkent", "toshkent", "A1", "1"]
_EXPR_FIELDS = ["a", "b", "c", "tags", "n", "a.c", "tags[*]", "tags[0]", "a..c", "`my key`", '$["x y"]', "$",
                "$key", "$index", "*", "a.*"]
_SCALAR_LITS = st.one_of(
    st.sampled_from(["null", "true", "false", "0", "1", "2", "-1", "1.10", "1.1", "1e400", "3", "10"]),
    st.sampled_from(_STRINGS).map(lambda s: codec.dumps(s).decode()),
    st.sampled_from(["'a'", "'ab'", "'Toshkent'"]),
)
_LITS = st.one_of(_SCALAR_LITS, st.lists(_SCALAR_LITS, max_size=3).map(lambda xs: "[" + ", ".join(xs) + "]"),
                  st.sampled_from(['{"a": 1}', "{}", "[]"]))
_STR_LITS = st.sampled_from(_STRINGS).map(lambda s: codec.dumps(s).decode())
_REGEX_LITS = st.sampled_from(['"^a"', '"b$"', '"[0-9]"', '"(?i)tosh"', '""', "'a|x'"])
_CMP = st.sampled_from(["==", "=", "!=", "≠", "<", "<=", "≤", ">", ">=", "≥"])


@st.composite
def _cond_text(draw) -> str:
    f = draw(st.sampled_from(_EXPR_FIELDS))
    kind = draw(st.integers(0, 10))
    if kind == 0:
        return f"{f} {draw(_CMP)} {draw(_LITS)}"
    if kind == 1:
        word = draw(st.sampled_from(["contains", "startswith", "CONTAINS", "StartsWith"]))
        return f"{f} {word} {draw(_LITS)}"
    if kind == 2:
        return f"{f} endswith {draw(_STR_LITS)}"
    if kind == 3:
        return f"{f} {draw(st.sampled_from(['matches', '~']))} {draw(_REGEX_LITS)}"
    if kind == 4:
        return f"{f} between {draw(_SCALAR_LITS)} and {draw(_SCALAR_LITS)}"
    if kind == 5:
        word = draw(st.sampled_from(["in", "not in"]))
        items = draw(st.lists(_SCALAR_LITS, max_size=3))
        return f"{f} {word} [{', '.join(items)}]"
    if kind == 6:
        what = draw(st.sampled_from(["null", "empty", "missing", "not null", "not empty", "not missing", "NULL"]))
        return f"{f} is {what}"
    if kind == 7:
        return f"{draw(st.sampled_from(['exists', 'missing', 'empty']))}({f})"
    if kind == 8:
        return f"len({f}) {draw(_CMP)} {draw(st.sampled_from(['0', '1', '2', '3', '1.0', '\"x\"']))}"
    if kind == 9:
        return f"({f}) == {draw(_LITS)}"
    return f"{f} {draw(_CMP)} ({draw(_SCALAR_LITS)})"


def _combine(kids):
    return st.one_of(
        st.tuples(kids, st.sampled_from([" and ", " && ", " AND ", " or ", " || ", " Or "]), kids)
        .map(lambda x: x[0] + x[1] + x[2]),
        kids.map(lambda a: f"not {a}"),
        kids.map(lambda a: f"!({a})"),
        kids.map(lambda a: f"({a})"),
        kids.map(lambda a: f"not ({a})"),
    )


_TEXT = st.recursive(_cond_text(), _combine, max_leaves=6)

_REC_SCALAR = st.one_of(st.none(), st.booleans(), st.integers(-1, 3), st.sampled_from(_STRINGS),
                        st.sampled_from(["1.10", "1e400", "10"]).map(codec.loads))
_REC_VALUE = st.recursive(
    _REC_SCALAR,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.sampled_from(_KEYS), inner, max_size=3),
    max_leaves=5,
)
_RECORD = st.one_of(st.dictionaries(st.sampled_from(_KEYS), _REC_VALUE, max_size=5), _REC_VALUE)
_CTX = st.tuples(_RECORD, st.one_of(st.none(), st.sampled_from(["k", "a", ""])),
                 st.one_of(st.none(), st.integers(0, 4)))


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_TEXT, st.lists(_CTX, min_size=1, max_size=12))
def test_representable_texts_filter_the_same(text: str, contexts: list) -> None:
    original = compile_filter(text)
    group = builder_from_text(text)
    assert group is not None, text
    rebuilt = compile_filter(group.to_text())
    direct = Filter.from_builder(group)
    ast_group = builder_from_expr(parse_filter(text))   # no parentheses kept: same meaning
    assert ast_group is not None
    from_ast = compile_filter(ast_group.to_text())
    for record, key, index in contexts:
        want = original.matches(record, key, index)
        assert rebuilt.matches(record, key, index) == want, (text, group.to_text(), record, key, index)
        assert direct.matches(record, key, index) == want
        assert from_ast.matches(record, key, index) == want


# ----------------------------------------------------------------------------------
# explicit cases
# ----------------------------------------------------------------------------------
def test_structure_of_a_typical_expression() -> None:
    g = builder_from_text('city == "Toshkent" and (age > 30 or not (tags contains "vip")) and len(children) >= 2')
    assert g == Group("and", [
        Condition("city", "eq", "Toshkent"),
        Group("or", [Condition("age", "gt", 30), Group("and", [Condition("tags", "contains", "vip")], True)]),
        Condition("children", "len_ge", 2),
    ])


@pytest.mark.parametrize(("text", "expected"), [
    ("a = 1", Group(items=[Condition("a", "eq", 1)])),
    ("a ≠ 'x'", Group(items=[Condition("a", "ne", "x")])),
    ("a ~ '^x'", Group(items=[Condition("a", "regex", "^x")])),
    ("a between 9 and 2", Group(items=[Condition("a", "between", 9, 2)])),
    ("a in [1, \"b\"]", Group(items=[Condition("a", "in", [1, "b"])])),
    ("a not in [1]", Group(items=[Condition("a", "in", [1])], negate=True)),
    ("a is null", Group(items=[Condition("a", "is_null")])),
    ("a is not null", Group(items=[Condition("a", "is_null")], negate=True)),
    ("a is empty", Group(items=[Condition("a", "is_empty")])),
    ("empty(a)", Group(items=[Condition("a", "is_empty")])),
    ("a is missing", Group(items=[Condition("a", "not_exists")])),
    ("missing(a)", Group(items=[Condition("a", "not_exists")])),
    ("a is not missing", Group(items=[Condition("a", "exists")])),
    ("exists(a.b[*].c)", Group(items=[Condition("a.b[*].c", "exists")])),
    ("len(name) != 3", Group(items=[Condition("name", "len_ne", 3)])),
    ("$key startswith 'k'", Group(items=[Condition("$key", "startswith", "k")])),
    ("$index <= 10", Group(items=[Condition("$index", "le", 10)])),
    ("$ == 5", Group(items=[Condition("$", "eq", 5)])),
    ("`my key` == 1", Group(items=[Condition('["my key"]', "eq", 1)])),
    ("$[\"$key\"] == 1", Group(items=[Condition('["$key"]', "eq", 1)])),
    ("true", Group()),
    ("a == 1 || b == 2 && c == 3",
     Group("or", [Condition("a", "eq", 1), Group("and", [Condition("b", "eq", 2), Condition("c", "eq", 3)])])),
    ("!a == 1", Group(items=[Condition("a", "eq", 1)], negate=True)),
])
def test_spellings(text: str, expected: Group) -> None:
    assert builder_from_text(text) == expected


def test_endswith_becomes_an_equivalent_regex() -> None:
    g = builder_from_text('name endswith "a.b (c)"')
    (cond,) = g.items
    assert cond.op == "regex" and re.search(cond.value, "xa.b (c)") and not re.search(cond.value, "xa.b (c)\n")
    f = Filter.from_builder(g)
    assert f.matches({"name": "zza.b (c)"}) and not f.matches({"name": "a.b (c)!"})
    assert not f.matches({"name": "a.b (c)\n"})   # "$" would match before a final newline; \Z does not
    assert not f.matches({"name": 5})


def test_exact_numbers_survive() -> None:
    g = builder_from_text("a == 1.10 and b > 12345678901234567890 and c between 1e-400 and 1e400")
    assert g.to_text() == "a == 1.10 and b > 12345678901234567890 and c between 1e-400 and 1e400"
    assert Filter.from_builder(g).matches({"a": codec.loads("1.1"), "b": 12345678901234567891, "c": 1})


@pytest.mark.parametrize("text", [
    "a == b", "lower(a) == 'x'", "type(a) == 'string'", "1 == a", "'x' in tags", "a", "not a", "$key",
    "a in [1, b]", "len(a) between 1 and 3", "len(a) in [1]", "len(a) contains 1", "a endswith 5",
    "a == 1 and true", "(true)", "false", "null", "exists(1)", "len(len(a)) == 1", "a == (b)",
    "number(a) > 3", "a is null and b", "[a] == [1]",
])
def test_not_representable(text: str) -> None:
    compile_filter(text)   # valid
    assert builder_from_text(text) is None


def test_too_deep_for_the_builder_is_not_representable() -> None:
    deep = "(" * 25 + "a == 1" + ")" * 25
    compile_filter(deep)
    assert builder_from_text(deep) is None
    value = "[" * 25 + "1" + "]" * 25
    compile_filter(f"a == {value}")
    assert builder_from_text(f"a == {value}") is None
    ok = "(" * 10 + "a == 1" + ")" * 10
    assert builder_from_text(ok).to_text() == ok


@pytest.mark.parametrize(("text", "pos"), [("a ==", 4), ("a == 1 and", 10), ("a ~ '('", 5), ("(a == 1", 7)])
def test_invalid_text_raises_the_parser_error(text: str, pos: int) -> None:
    with pytest.raises(QuerySyntaxError) as info:
        builder_from_text(text)
    with pytest.raises(QuerySyntaxError) as want:
        compile_filter(text)
    assert info.value.pos == want.value.pos == pos


def test_object_literal_with_parenthesised_member_is_not_representable() -> None:
    text = 'a == {"k": (1)}'
    compile_filter(text)
    assert builder_from_text(text) is None


def test_builder_from_expr_on_plain_ast() -> None:
    expr = parse_filter("(a == 1 and b == 2) and not (c == 3)")
    g = builder_from_expr(expr)
    assert g is not None
    assert g.to_text() == "(a == 1 and b == 2) and not (c == 3)"
    assert builder_from_expr(parse_filter("a == b")) is None
    assert builder_from_expr(parse_filter("true")) == Group()
