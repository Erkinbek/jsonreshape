"""Tests for jsonreshape.query: filter language (§9), JMESPath adapter, builder model.

Covers grammar and precedence, every operator and function, missing/null/empty
semantics, wildcards, $key/$index, exact numbers, regex, error positions, JMESPath,
builder <-> expression equivalence (AGENT_PROMPT §14 test 18, hypothesis), matches_values
equivalence and a static "no eval/exec/compile" check.
"""

from __future__ import annotations

import ast
import pickle
import random
import re
import string
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec, i18n
from jsonreshape import query as Q
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.errors import PathSyntaxError, QuerySyntaxError, UserInputError
from jsonreshape.paths import PathPattern, Wild, parse_pattern
from jsonreshape.query import (
    And,
    Between,
    Call,
    Compare,
    Condition,
    Filter,
    Group,
    In,
    Is,
    ListExpr,
    Literal,
    Not,
    Or,
    PathExpr,
    SpecialExpr,
    compile_filter,
    format_expr,
    parse_filter,
)

HYP = settings(deadline=None, max_examples=150, derandomize=True, database=None,
               suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

REC: dict[str, Any] = {
    "id": 7,
    "name": "Toshkent shahri",
    "contact": {"phone": "+998901234567", "address": {"city": "Toshkent", "zip": 100000}},
    "children": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
    "orders": [{"oid": 7, "sum": 1.5}, {"oid": 8, "sum": None}],
    "tags": ["x", "yy"],
    "score": 1.10,
    "active": True,
    "note": None,
    "empty_s": "",
    "empty_a": [],
    "empty_o": {},
    "zero": 0,
    "and": "kw",
    "my key": 5,
    "dot.key": "d",
}


def m(text: str, value: Any = REC, key: str | None = None, index: int | None = None) -> bool:
    flt = compile_filter(text)
    result = flt.matches(value, key=key, index=index)
    # every evaluation must agree with the pre-extracted-values path
    values = {p: p.values(value) for p in flt.required_patterns}
    assert flt.matches_values(values, key=key, index=index) is result
    return result


def lit(value: Any) -> Literal:
    return Literal(value, codec.dumps(value).decode("utf-8"))


def path(text: str) -> PathExpr:
    return PathExpr(parse_pattern(text))


def syntax_error(text: str) -> QuerySyntaxError:
    with pytest.raises(QuerySyntaxError) as info:
        compile_filter(text)
    return info.value


# ==================================================================================
# grammar / lexer
# ==================================================================================
class TestGrammar:
    def test_basic_ast(self):
        assert parse_filter("a == 1") == Compare("==", path("a"), lit(1))
        assert parse_filter("a = 1") == parse_filter("a == 1")
        assert parse_filter("a ≠ 1") == Compare("!=", path("a"), lit(1))
        assert parse_filter("a ≤ 1") == Compare("<=", path("a"), lit(1))
        assert parse_filter("a ≥ 1") == Compare(">=", path("a"), lit(1))
        assert parse_filter("a ~ 'x'") == Compare("matches", path("a"), lit("x"))
        assert parse_filter("a") == path("a")
        assert parse_filter("(((a)))") == path("a")

    def test_precedence(self):
        a, b, c, d = (Compare("==", path(n), lit(1)) for n in "abcd")
        assert parse_filter("a == 1 or b == 1 and c == 1") == Or((a, And((b, c))))
        assert parse_filter("a == 1 and b == 1 or c == 1 and d == 1") == Or((And((a, b)), And((c, d))))
        assert parse_filter("not a == 1 and b == 1") == And((Not(a), b))
        assert parse_filter("not a == 1 or b == 1") == Or((Not(a), b))
        assert parse_filter("not not a == 1") == Not(Not(a))
        assert parse_filter("!a == 1 && b == 1 || c == 1") == Or((And((Not(a), b)), c))
        assert parse_filter("a == 1 and (b == 1 or c == 1)") == And((a, Or((b, c))))
        assert parse_filter("a == 1 and (b == 1 and c == 1)") == And((a, And((b, c))))
        assert parse_filter("a == 1 and b == 1 and c == 1") == And((a, b, c))
        # not binds looser than a comparison
        assert parse_filter("not x between 1 and 3") == Not(Between(path("x"), lit(1), lit(3)))
        assert parse_filter("not x in [1]") == Not(In(path("x"), lit([1])))

    def test_precedence_semantics(self):
        rec = {"a": 1, "b": 0, "c": 1}
        assert m("a == 1 or b == 1 and c == 2", rec) is True
        assert m("(a == 1 or b == 1) and c == 2", rec) is False
        assert m("not a == 2 and b == 0", rec) is True
        assert m("not (a == 1 and b == 0)", rec) is False

    def test_keywords_case_insensitive(self):
        assert parse_filter("a == 1 AND b == 2 OR NOT c Is Null") == parse_filter("a == 1 and b == 2 or not c is null")
        assert parse_filter("x == TRUE") == Compare("==", path("x"), lit(True))
        assert parse_filter("x IN [1] And y Between 1 AND 2") == parse_filter("x in [1] and y between 1 and 2")
        assert parse_filter("x is EMPTY") == Is(path("x"), "empty")
        assert parse_filter("x is not Missing") == Is(path("x"), "missing", True)
        assert parse_filter("LEN(x) > 1") == Compare(">", Call("len", (path("x"),)), lit(1))

    def test_strings(self):
        assert parse_filter('x == "a\\"b"').right.value == 'a"b'
        assert parse_filter("x == 'it\\'s'").right.value == "it's"
        assert parse_filter("x == 'say \"hi\"'").right.value == 'say "hi"'
        assert parse_filter('x == "\\\\ \\/ \\b \\f \\n \\r \\t"').right.value == "\\ / \b \f \n \r \t"
        assert parse_filter('x == "\\u0041\\u00e9"').right.value == "Aé"
        assert parse_filter('x == "\\ud83d\\ude00"').right.value == "\U0001F600"
        assert parse_filter('x == "\\ud800"').right.value == "\ud800"  # lone surrogate kept, like json.loads
        assert parse_filter('x == "\\ud800\\u0041"').right.value == "\ud800A"
        assert parse_filter('x == "Тошкент ‘o‘ ʻ"').right.value == "Тошкент ‘o‘ ʻ"
        assert parse_filter('x == "multi\nline"').right.value == "multi\nline"
        assert parse_filter("x == ''").right.value == ""
        # same decoding as json.loads for every JSON-escaped string
        for s in jsongen.EDGE_STRINGS + ["\ud800", "\udfff", "\x00\x1f\x7f", "`'\""]:
            encoded = codec.dumps(s).decode("utf-8")
            assert parse_filter(f"x == {encoded}").right.value == s

    def test_numbers_exact(self):
        assert parse_filter("x == 1.10").right.value.raw == "1.10"
        assert isinstance(parse_filter("x == 1.10").right.value, JsonFloat)
        assert parse_filter("x == 1e400").right.value == float("inf")
        assert parse_filter("x == 1e400").right.text == "1e400"
        assert type(parse_filter("x == 12345678901234567890").right.value) is int
        assert isinstance(parse_filter("x == -0").right.value, JsonInt)
        assert parse_filter("x == 0.1").right.value == 0.1
        assert parse_filter("x==-5").right.value == -5
        assert parse_filter("x<-5").right == lit(-5)
        for tok in jsongen.EDGE_NUMBER_TOKENS:
            node = parse_filter(f"x == {tok}").right
            assert node.text == codec.dumps(codec.loads(tok)).decode()
            assert node.value == codec.loads(tok) or node.value != node.value

    def test_literals_lists_objects(self):
        assert parse_filter("x == [1, 'a', [true, null]]").right == lit([1, "a", [True, None]])
        assert parse_filter("x == []").right == lit([])
        assert parse_filter('x == {"a": [1, {"b": null}], "c": -1.5}').right == lit({"a": [1, {"b": None}], "c": -1.5})
        assert parse_filter("x == {}").right == lit({})
        assert parse_filter('x == {"a": 1, "a": 2}').right.value == {"a": 2}  # last wins, like json
        node = parse_filter("x in [1, y, z[*]]").right
        assert isinstance(node, ListExpr) and node.items == (lit(1), path("y"), path("z[*]"))
        assert m("x in [1, y, z[*]]", {"x": 3, "y": 2, "z": [5, 3]}) is True
        assert m("x in [1, y, z[*]]", {"x": 4, "y": 2, "z": [5, 3]}) is False
        assert m("[nope] == []", {}) is True  # a missing item contributes nothing

    @pytest.mark.parametrize("text,segments", [
        ("a.b[0].c", ("a", "b", 0, "c")),
        ('a["x.y"]', ("a", "x.y")),
        ("a['x y']", ("a", "x y")),
        ("a[*]", ("a", Wild.ANY_INDEX)),
        ("a[]", ("a", Wild.ANY_INDEX)),
        ("a[ 1 ]", ("a", 1)),
        ("a[ * ]", ("a", Wild.ANY_INDEX)),
        ("a.*", ("a", Wild.ANY_KEY)),
        ("*.city", (Wild.ANY_KEY, "city")),
        ("**.b", (Wild.DESCENDANTS, "b")),
        ("a..b", ("a", Wild.DESCENDANTS, "b")),
        ("..b", (Wild.DESCENDANTS, "b")),
        ("a..[0]", ("a", Wild.DESCENDANTS, 0)),
        ("`my key`.x", ("my key", "x")),
        ("a.`b c`", ("a", "b c")),
        ("`and`", ("and",)),
        ("`*`", ("*",)),
        ("$", ()),
        ("$.a", ("a",)),
        ('$["and"]', ("and",)),
        ("$[0]", (0,)),
        ("$..a", (Wild.DESCENDANTS, "a")),
        ("x.and", ("x", "and")),
        ("x.null.in", ("x", "null", "in")),
        ("a.0.b", ("a", "0", "b")),
        ("_id.$oid", ("_id", "$oid")),
        ("@type", ("@type",)),
        ("first-name", ("first-name",)),
        ("a*b", ("a*b",)),
        ("Тошкент.шаҳар", ("Тошкент", "шаҳар")),
        ("ko‘cha", ("ko‘cha",)),
        ("koʻcha", ("koʻcha",)),
        ("a#1", ("a#1",)),
    ])
    def test_paths(self, text, segments):
        node = parse_filter(text)
        assert isinstance(node, PathExpr)
        assert node.pattern.segments == segments
        # rendering gives back exactly the same pattern
        assert parse_filter(Q.render_path(node.pattern)).pattern == node.pattern

    def test_specials(self):
        assert parse_filter("$key") == SpecialExpr("key")
        assert parse_filter("$index") == SpecialExpr("index")
        assert parse_filter("$KEY") == SpecialExpr("key")
        assert parse_filter("$.key") == path("key")

    def test_function_names_are_not_reserved(self):
        rec = {"type": "a", "len": 3, "string": "s", "number": 1, "missing": True, "empty": "", "exists": 0}
        assert m('type == "a"', rec) is True
        assert m("len == 3", rec) is True
        assert m('string == "s" and number == 1', rec) is True
        assert m("missing == true", rec) is True
        assert m("empty is empty", rec) is True
        assert m("exists == 0 and exists(exists)", rec) is True

    def test_keyword_keys_need_quoting(self):
        assert m('$["and"] == "kw"') is True
        assert m('`and` == "kw"') is True
        err = syntax_error('and == "kw"')
        assert err.pos == 0

    def test_whitespace_variants(self):
        assert parse_filter("a==1&&b!=2||c<3") == parse_filter("a == 1 and b != 2 or c < 3")
        assert parse_filter(" \t\n a \r\n ==  1 ") == parse_filter("a == 1")
        assert parse_filter("len ( x ) > 1") == parse_filter("len(x) > 1")
        assert parse_filter("x in[1,2]") == parse_filter("x in [1, 2]")


# ==================================================================================
# semantics
# ==================================================================================
class TestSemantics:
    @pytest.mark.parametrize("value,null,empty,missing,truthy", [
        (None, True, False, False, False),
        ("", False, True, False, False),
        ([], False, True, False, False),
        ({}, False, True, False, False),
        (0, False, False, False, False),
        (0.0, False, False, False, False),
        (JsonInt("-0"), False, False, False, False),
        (False, False, False, False, False),
        ("0", False, False, False, True),
        ([0], False, False, False, True),
        ({"a": None}, False, False, False, True),
        (True, False, False, False, True),
        (-1, False, False, False, True),
        (JsonFloat("1.10"), False, False, False, True),
        ("missing", False, False, True, False),
    ])
    def test_null_empty_missing_truthiness(self, value, null, empty, missing, truthy):
        rec = {} if value == "missing" else {"x": value}
        assert m("x is null", rec) is null
        assert m("x is not null", rec) is (not null)
        assert m("x is empty", rec) is empty
        assert m("empty(x)", rec) is empty
        assert m("x is not empty", rec) is (not empty)
        assert m("x is missing", rec) is missing
        assert m("missing(x)", rec) is missing
        assert m("exists(x)", rec) is (not missing)
        assert m("x is not missing", rec) is (not missing)
        assert m("x", rec) is truthy
        assert m("not x", rec) is (not truthy)
        assert m("x == null", rec) is null

    def test_not_equal_is_negated_equality(self):
        assert m('city != "X"', {}) is True  # missing -> not (missing == "X")
        assert m('city != "X"', {"city": "X"}) is False
        assert m("t[*] != 1", {"t": [1, 2]}) is False  # some value == 1
        assert m("t[*] != 3", {"t": [1, 2]}) is True
        assert m("x not in [1, 2]", {}) is True

    def test_no_type_coercion(self):
        assert m("x == 1", {"x": "1"}) is False
        assert m('x == "1"', {"x": 1}) is False
        assert m("x == 1", {"x": True}) is False
        assert m("x == true", {"x": 1}) is False
        assert m("x == 0", {"x": False}) is False
        assert m("x == false", {"x": None}) is False
        assert m("x == null", {"x": False}) is False
        assert m("x == 1", {"x": 1.0}) is True
        assert m("x == []", {"x": {}}) is False
        assert m('x == ""', {"x": []}) is False
        assert m("x == [1, 2]", {"x": [1, 2]}) is True
        assert m("x == [2, 1]", {"x": [1, 2]}) is False
        assert m("x == [1, [true]]", {"x": [1.0, [True]]}) is True
        assert m("x == [1, [1]]", {"x": [1, [True]]}) is False
        assert m('x == {"a": [1, {"b": null}], "c": 2}', {"x": {"c": 2.0, "a": [1, {"b": None}]}}) is True
        assert m('x == {"a": 1}', {"x": {"a": 1, "b": 2}}) is False

    def test_ordering(self):
        assert m("x < 5", {"x": 3}) is True
        assert m("x < 5", {"x": "3"}) is False
        assert m('x < "5"', {"x": 3}) is False
        assert m('x > "a"', {"x": "b"}) is True
        assert m('x >= "b" and x <= "b"', {"x": "b"}) is True
        assert m('x < "a"', {"x": "B"}) is True  # code point order
        assert m("x > 0", {"x": True}) is False
        assert m("x > 0", {"x": None}) is False
        assert m("x > 0", {"x": [1]}) is False
        assert m("x <= 1.5 and x >= 1.5", {"x": 1.5}) is True
        assert m("x between 1 and 3", {"x": 1}) is True
        assert m("x between 1 and 3", {"x": 3}) is True
        assert m("x between 1 and 3", {"x": 3.5}) is False
        assert m('x between "a" and "c"', {"x": "b"}) is True
        assert m('x between 1 and "c"', {"x": 2}) is False
        assert m("x between 1 and 3", {"x": "2"}) is False
        assert m("x[*] between 10 and 20", {"x": [1, 15]}) is True
        assert m("x between lo and hi", {"x": 5, "lo": 1, "hi": 9}) is True
        assert m("x between lo and hi", {"x": 5, "lo": 1}) is False

    def test_numbers_exact(self):
        assert m("x == 1.10", {"x": 1.1}) is True
        assert m("x == 1.1", {"x": JsonFloat("1.10")}) is True
        big = codec.loads("12345678901234567890")
        assert m("x == 12345678901234567890", {"x": big}) is True
        assert m("x == 12345678901234567891", {"x": big}) is False
        assert m("x > 12345678901234567889", {"x": big}) is True
        assert m("x > 9007199254740992", {"x": 9007199254740993}) is True
        assert m("x == 9007199254740992.0", {"x": 9007199254740993}) is False  # exact int/float compare
        huge = codec.loads("123456789012345678901234567890")
        assert isinstance(huge, JsonInt)
        assert m("x == 123456789012345678901234567890", {"x": huge}) is True
        assert m("x == 123456789012345678901234567891", {"x": huge}) is False
        assert m("x == 1e400", {"x": codec.loads("1e400")}) is True
        assert m("x > 1e308", {"x": codec.loads("1e400")}) is True
        assert m("x < -1e308", {"x": codec.loads("-1e400")}) is True
        assert m("x == 0", {"x": codec.loads("-0")}) is True
        assert m("x == -0.0", {"x": 0}) is True
        assert m("x == 0.1", {"x": codec.loads("0.1")}) is True
        assert m('string(x) == "1.10"', {"x": JsonFloat("1.10")}) is True
        assert m('string(x) == "1e400"', {"x": codec.loads("1e400")}) is True
        assert m('string(x) == "12345678901234567890"', {"x": big}) is True
        assert m('string(x) == "-0"', {"x": codec.loads("-0")}) is True
        # every edge token equals itself after a round trip through a data file
        for tok in jsongen.EDGE_NUMBER_TOKENS:
            rec = codec.loads('{"x": %s}' % tok)
            assert m(f"x == {tok}", rec) is True, tok
            assert m(f"x in [{tok}]", rec) is True, tok
            assert m(f"x between {tok} and {tok}", rec) is True, tok

    def test_decimal_values(self):
        # ijson streams numbers as int/Decimal; they behave exactly like codec numbers
        rec = {"x": Decimal("1.10"), "y": Decimal("1E+400"), "z": Decimal("-0.0"), "w": [Decimal("2.5")]}
        assert m("x == 1.1", rec) is True
        assert m("x == 1.10 and x > 1 and x < 1.2", rec) is True
        assert m("y == 1e400", rec) is True
        assert m("z == 0", rec) is True
        assert m("x in [1.1, 2]", rec) is True
        assert m("w contains 2.5", rec) is True
        assert m("x between 1 and 2", rec) is True
        assert m('type(x) == "number"', rec) is True
        assert m("number(x) == 1.1", rec) is True
        assert m("x", rec) is True
        assert m("z", rec) is False

    def test_contains_startswith_endswith(self):
        rec = {"s": "Toshkent", "a": [1, "x", [2], {"k": 1}], "o": {"k": 1}, "n": 12}
        assert m('s contains "shk"', rec) is True
        assert m('s contains "SHK"', rec) is False  # case-sensitive
        assert m('s contains ""', rec) is True
        assert m("a contains 1", rec) is True
        assert m("a contains 1.0", rec) is True
        assert m('a contains "x"', rec) is True
        assert m("a contains [2]", rec) is True
        assert m('a contains {"k": 1}', rec) is True
        assert m("a contains 2", rec) is False
        assert m('o contains "k"', rec) is True
        assert m("o contains 1", rec) is False
        assert m("n contains 1", rec) is False
        assert m('s startswith "Tosh"', rec) is True
        assert m('s endswith "kent"', rec) is True
        assert m('s startswith "kent"', rec) is False
        assert m("n startswith 1", rec) is False
        assert m('a startswith "x"', rec) is False
        assert m("s startswith s", rec) is True  # dynamic right operand

    def test_in(self):
        rec = {"x": "a", "n": 1, "b": True, "lst": ["a", "b"], "o": {"k": 1}, "s": "xaby"}
        assert m('x in ["a", "b"]', rec) is True
        assert m('x in ["b"]', rec) is False
        assert m("n in [1.0, 5]", rec) is True
        assert m("n in [true]", rec) is False
        assert m("b in [1]", rec) is False
        assert m("b in [true]", rec) is True
        assert m('n in ["1"]', rec) is False
        assert m("x in lst", rec) is True
        assert m('"b" in lst', rec) is True
        assert m('"ab" in s', rec) is True  # substring
        assert m('"k" in o', rec) is True  # key
        assert m('"k" in {"k": 2}', rec) is True
        assert m("x not in lst", rec) is False
        assert m("nope in lst", rec) is False
        assert m("nope not in lst", rec) is True
        assert m("x in nope", rec) is False
        assert m("x in []", rec) is False
        assert m("[1, 2] in [[1, 2], 3]", rec) is True
        assert m("n in [1, 'a', null]", rec) is True

    def test_regex(self):
        rec = {"s": "Toshkent 2024", "t": ["abc", "xyz"], "n": 5, "p": "^x", "bad": "a("}
        assert m('s matches "\\\\d{4}"', rec) is True
        assert m("s ~ '[0-9]+$'", rec) is True
        assert m('s matches "^kent"', rec) is False  # re.search, anchored
        assert m('s matches "kent"', rec) is True  # re.search finds inside
        assert m('s matches "toshkent"', rec) is False
        assert m('s matches "(?i)toshkent"', rec) is True
        assert m('t[*] ~ "^x"', rec) is True
        assert m('n matches "5"', rec) is False  # no coercion
        assert m("t[*] matches p", rec) is True  # pattern from the data
        assert m("s matches bad", rec) is False  # invalid regex from data matches nothing
        assert m("s matches n", rec) is False

    def test_wildcards(self):
        rec = {"orders": [{"oid": 7}, {"oid": 8, "x": {"city": "B"}}], "m": {"a": {"city": "A"}, "b": {"city": "B"}}}
        assert m("orders[*].oid == 8", rec) is True
        assert m("orders[].oid == 9", rec) is False
        assert m("orders[0].oid == 8", rec) is False
        assert m("orders.1.oid == 8", rec) is True  # str index on a list
        assert m('m.*.city == "B"', rec) is True
        assert m('*.a.city == "A"', rec) is True
        assert m('**.city == "B"', rec) is True
        assert m('$..city == "A"', rec) is True
        assert m("len(orders[*].oid) is missing", rec) is True  # len of the first value (a number)
        assert m("orders[*].oid > 7", rec) is True
        assert m("orders[*].oid < 7", rec) is False

    def test_key_and_index(self):
        assert m('$key == "id5"', {}, key="id5") is True
        assert m('$key startswith "id"', {}, key="id5") is True
        assert m('$key == "id5"', {}) is False
        assert m("$key is missing", {}) is True
        assert m("$index == 3", {}, index=3) is True
        assert m("$index < 3", {}, index=3) is False
        assert m("$index in [0, 3]", {}, index=3) is True
        assert m("$index is missing", {}) is True
        assert m("$index == 0", {}, index=0) is True
        flt = compile_filter('$index >= 10 and $key != "x"')
        assert flt.required_patterns == [] and flt.uses_index and flt.uses_key
        assert compile_filter("a == 1").uses_key is False

    def test_root_value(self):
        assert m("$ == 5", 5) is True
        assert m("$ > 4 and $ < 6", 5) is True
        assert m('$ == "s"', "s") is True
        assert m("len($) == 2", [1, 2]) is True
        assert m('type($) == "array"', [1]) is True
        assert m("$ contains 2", [1, 2]) is True
        assert m("$", {}) is False
        assert m("$ is empty", {}) is True
        assert m("$[0] == 1", [1, 2]) is True
        assert m("a == 1", [1, 2]) is False
        assert compile_filter("$ == 1").required_patterns == [PathPattern(())]

    def test_functions(self):
        rec = {"s": "  Toshkent ", "e": "😀a", "a": [1, 2, 3], "o": {"x": 1}, "n": 5, "t": ["Ab", 3, "cD"],
               "ns": ["12", " 1.10 ", "+3", "x", "1e400", "12345678901234567890", "-0", "1.", ""], "b": True}
        assert m("len(s) == 11", rec) is True
        assert m("len(e) == 2", rec) is True  # code points (astral char counts once)
        assert m("len(a) == 3 and len(o) == 1", rec) is True
        assert m("len(n) is missing", rec) is True
        assert m("len(n) > 0", rec) is False
        assert m("len(n) != 1", rec) is True
        assert m("len(nope) is missing", rec) is True
        assert m("len(t[*]) == 2", rec) is True  # first value only
        assert m("len(t[*]) == 1", rec) is False
        assert m('len("abc") == 3 and len([1, 2]) == 2', rec) is True
        assert m('lower(t[*]) == "ab"', rec) is True
        assert m('upper(t[*]) == "CD"', rec) is True
        assert m("lower(n) == 5", rec) is False  # non-strings dropped
        assert m("lower(n) is missing", rec) is True
        assert m('trim(s) == "Toshkent"', rec) is True
        assert m('lower(trim(s)) startswith "tosh"', rec) is True
        for field, name in [("s", "string"), ("a", "array"), ("o", "object"), ("n", "number"), ("b", "bool"),
                            ("nn", "null")]:
            assert m(f'type({field}) == "{name}"', dict(rec, nn=None)) is True
        assert m('type(t[*]) == "number"', rec) is True
        assert m("type(nope) is missing", rec) is True
        assert m("number(ns[*]) == 12", rec) is True
        assert m("number(ns[*]) == 1.1", rec) is True
        assert m("number(ns[*]) == 3", rec) is True
        assert m("number(ns[*]) == 1e400", rec) is True
        assert m("number(ns[*]) == 12345678901234567890", rec) is True
        assert m("number(ns[*]) == -0", rec) is True
        assert m("number(n) == 5", rec) is True
        assert m("number(b) is missing", rec) is True
        assert m('string(n) == "5"', rec) is True
        assert m('string(b) == "true"', rec) is True
        assert m('string(nope) is missing', rec) is True
        assert m('string(o) == "{\\"x\\":1}"', rec) is True
        assert m('string(x) == "[\\"Тошкент\\",null]"', {"x": ["Тошкент", None]}) is True
        assert m('string(x) == "null"', {"x": None}) is True
        assert m("exists(a) and missing(nope) and empty(z)", dict(rec, z=[])) is True

    def test_function_arity_and_unknown(self):
        for name in Q.FUNCTIONS:
            err = syntax_error(f"{name}() == 1")
            assert err.pos == 0
            err = syntax_error(f"x == {name}(a, b)")
            assert err.pos == 5
        err = syntax_error("x == foo(a)")
        assert err.pos == 5 and "foo" in str(err)
        assert "len" in str(err)  # lists the available functions
        err = syntax_error("a.b(1)")
        assert err.pos == 3

    def test_bare_operand_truthiness_existential(self):
        assert m("t[*]", {"t": [0, "", None]}) is False
        assert m("t[*]", {"t": [0, "", 1]}) is True
        assert m("t", {"t": [0]}) is True
        assert m("exists(t[*])", {"t": []}) is False
        assert m("1", {}) is True and m("0", {}) is False and m('""', {}) is False and m("[]", {}) is False
        assert m("(a == 1) == true", {"a": 1}) is True
        assert m("[a == 1, b] contains false", {"a": 2}) is True

    def test_comparison_of_two_paths(self):
        rec = {"a": 1, "b": 1.0, "c": [1, 2], "d": [2, 3], "s": "x"}
        assert m("a == b", rec) is True
        assert m("c[*] == d[*]", rec) is True  # existential on both sides
        assert m("c[*] > d[*]", rec) is False
        assert m("c[*] < d[*]", rec) is True
        assert m("a == nope", rec) is False
        assert m("a != nope", rec) is True


# ==================================================================================
# errors
# ==================================================================================
class TestErrors:
    @pytest.mark.parametrize("text,pos,key", [
        ("", 0, "query.syntax.empty"),
        ("   ", 0, "query.syntax.empty"),
        ("a ==", 4, "query.syntax.expected_operand_end"),
        ("a == ", 5, "query.syntax.expected_operand_end"),
        ("== 1", 0, "query.syntax.expected_operand"),
        ("a == and", 5, "query.syntax.expected_operand"),
        ("a == 1 b == 2", 7, "query.syntax.missing_operator"),
        ("a == 1 'x'", 7, "query.syntax.missing_operator"),
        ("a == 1 == 2", 7, "query.syntax.chained_comparison"),
        ("a < b < c", 6, "query.syntax.chained_comparison"),
        ("a == 1 contains 2", 7, "query.syntax.chained_comparison"),
        ("(a == 1", 7, "query.syntax.expected_close_paren"),
        ("a == (1 2)", 8, "query.syntax.expected_close_paren"),
        ("a == 1)", 6, "query.syntax.unexpected_token"),
        ("a == 1 ]", 7, "query.syntax.unexpected_token"),
        ("[1, 2", 5, "query.syntax.expected_list_end"),
        ("[1, ]", 4, "query.syntax.expected_operand"),
        ("len(a", 5, "query.syntax.expected_args_end"),
        ("foo(1)", 0, "query.syntax.unknown_function"),
        ("len()", 0, "query.syntax.arg_count"),
        ("len(1, 2)", 0, "query.syntax.arg_count"),
        ('a == "abc', 5, "query.syntax.unterminated_string"),
        ("a == 'abc", 5, "query.syntax.unterminated_string"),
        ('a == "abc\\', 5, "query.syntax.unterminated_string"),
        ("`abc", 0, "query.syntax.unterminated_name"),
        ('a == "\\x"', 6, "query.syntax.bad_escape"),
        ('a == "ok\\q"', 8, "query.syntax.bad_escape"),
        ('a == "\\u12"', 6, "query.syntax.bad_unicode_escape"),
        ('a == "\\u12zz"', 6, "query.syntax.bad_unicode_escape"),
        ("a == 1.", 5, "query.syntax.invalid_number"),
        ("a == .5", 5, "query.syntax.invalid_number"),
        ("a == -.5", 5, "query.syntax.invalid_number"),
        ("a == 1e", 5, "query.syntax.invalid_number"),
        ("a == 01", 5, "query.syntax.invalid_number"),
        ("a == 1.2.3", 5, "query.syntax.invalid_number"),
        ("a == 12abc", 5, "query.syntax.invalid_number"),
        ("a == 0x1F", 5, "query.syntax.invalid_number"),
        ("a == -", 5, "query.syntax.unexpected_char"),
        ("a == 1 & b", 7, "query.syntax.unexpected_char"),
        ("a == 1 | b", 7, "query.syntax.unexpected_char"),
        ("a == #1", 5, "query.syntax.unexpected_char"),
        (".a == 1", 0, "query.syntax.unexpected_char"),
        ("a == “x”", 5, "query.syntax.curly_quote"),
        ("a == ‘x’", 5, "query.syntax.curly_quote"),
        ("a == «x»", 5, "query.syntax.curly_quote"),
        ("a[", 1, "query.syntax.unterminated_bracket"),
        ("a[0", 3, "query.syntax.expected_bracket_close"),
        ("a['x' 1]", 6, "query.syntax.expected_bracket_close"),
        ("a[x]", 2, "query.syntax.bad_bracket"),
        ("a[-1]", 2, "query.syntax.negative_index"),
        ("a.", 2, "query.syntax.expected_key"),
        ("a..", 3, "query.syntax.expected_key"),
        ("a. b", 2, "query.syntax.expected_key"),
        ("$foo", 0, "query.syntax.unknown_special"),
        ("x == $keys", 5, "query.syntax.unknown_special"),
        ("$key.x", 4, "query.syntax.special_no_path"),
        ("$index[0]", 6, "query.syntax.special_no_path"),
        ("a is", 4, "query.syntax.expected_is_target"),
        ("a is nothing", 5, "query.syntax.expected_is_target"),
        ("a is not 1", 9, "query.syntax.expected_is_target"),
        ("a not", 5, "query.syntax.expected_in"),
        ("a not like 1", 6, "query.syntax.expected_in"),
        ("a between 1", 11, "query.syntax.expected_between_and"),
        ("a between 1 or 2", 12, "query.syntax.expected_between_and"),
        ("{1: 2} == a", 1, "query.syntax.expected_object_key"),
        ('{"a" 1} == x', 5, "query.syntax.expected_colon"),
        ('{"a": x} == y', 6, "query.syntax.object_constant"),
        ('{"a": 1 == y', 8, "query.syntax.object_constant"),
        ('{"a": 1 2} == y', 8, "query.syntax.expected_object_end"),
        ("#1 == a", 0, "query.syntax.unexpected_char"),
        ("not", 3, "query.syntax.expected_operand_end"),
        ("a and", 5, "query.syntax.expected_operand_end"),
        ("a matches 5", 10, "query.syntax.regex_not_string"),
        ("a ~ null", 4, "query.syntax.regex_not_string"),
        ('a matches "("', 11, "query.syntax.bad_regex"),
    ])
    def test_error_positions(self, text, pos, key, lang_en):
        err = syntax_error(text)
        assert err.pos == pos, (text, err.pos, str(err))
        assert err.text == text
        assert err.key == "error.query_syntax"
        detail_template = i18n.MESSAGES[key]["en"]
        prefix = next(iter(string.Formatter().parse(detail_template)))[0]  # literal text before 1st field
        assert prefix in err.params["detail"], (err.params["detail"], prefix)
        if text:
            assert err.caret().splitlines()[1] == " " * pos + "^"

    def test_regex_error_positions_map_through_escapes(self):
        cases = [
            ('name matches "a(b"', '('),
            ("name ~ 'x[a-'", "["),
            ('name ~ "\\u0041\\u0042("', "("),
            ('name ~ "\\t\\n(ab"', "("),
            ('name ~ "\\u00e9\\\\d(?P<x>a)(?P<x>b)"', "x"),
            ('name ~ ("abc)")', ")"),
        ]
        for text, char in cases:
            err = syntax_error(text)
            assert text[err.pos] == char, (text, err.pos, str(err))
        # unterminated character set after an escaped backslash: re reports the '['
        text = 'n ~ "a\\\\db[x"'  # pattern a\db[x
        err = syntax_error(text)
        assert text[err.pos] == "[" and err.pos == 10

    def test_nesting_limit(self):
        ok = "(" * 50 + "a == 1" + ")" * 50
        assert compile_filter(ok).matches({"a": 1})
        assert compile_filter("not " * 50 + "a").matches({"a": 1}) is True
        assert compile_filter("[" * 40 + "]" * 40 + " == x").matches({"x": 1}) is False
        for text in ["(" * 150 + "a" + ")" * 150, "not " * 150 + "a", "[" * 150 + "]" * 150, "!" * 300 + "a",
                     '{"a":' * 120 + "1" + "}" * 120]:
            err = syntax_error(text)
            assert "100" in str(err)

    def test_localized_messages(self):
        old = i18n.get_language()
        try:
            i18n.set_language("uz")
            err = syntax_error("a == foo(1)")
            assert str(err).startswith("Ifodada xato (6-belgi)")
            assert "noma'lum funksiya foo()" in str(err)
            assert err.hint and "Misol" in err.hint
            i18n.set_language("en")
            err = syntax_error("a == foo(1)")
            assert str(err).startswith("Expression error at character 6")
            assert "unknown function foo()" in str(err)
        finally:
            i18n.set_language(old)

    def test_all_message_keys_exist(self):
        src = Path(Q.__file__).read_text(encoding="utf-8")
        keys = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
        keys |= {"query.jmespath.runtime", "query.builder.error"} | {f"query.op.{op}" for op in Q.BUILDER_OPS}
        assert len(keys) > 60
        for key in sorted(keys):
            assert key.startswith("query."), key
            assert key in i18n.MESSAGES, key
            entry = i18n.MESSAGES[key]
            assert entry.get("uz") and entry.get("en"), key

    def test_messages_format_cleanly(self, lang_uz):
        # templates with literal braces must still substitute their parameters
        err = syntax_error('{"a": 1 2} == y')
        assert "'}'" in err.params["detail"] and "{token}" not in err.params["detail"]
        assert "'2'" in err.params["detail"]

    def test_bare_word_warning(self):
        flt = compile_filter("status == active")
        assert len(flt.warnings) == 1 and "active" in flt.warnings[0]
        assert compile_filter('status == "active"').warnings == []
        assert compile_filter("x in tags").warnings == []
        assert compile_filter("price > cost").warnings == []
        assert compile_filter("a.b == c.d").warnings == []
        assert compile_filter("a == b or c == b").warnings == compile_filter("a == b").warnings  # de-duplicated

    def test_type_and_lang_errors(self):
        with pytest.raises(TypeError):
            compile_filter(b"a == 1")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            compile_filter("a == 1", lang="sql")


# ==================================================================================
# format_expr / rendering
# ==================================================================================
class TestFormat:
    @pytest.mark.parametrize("text,expected", [
        ("a==1", "a == 1"),
        ("a = 'x'", 'a == "x"'),
        ("a ≠ 1 && b ≥ 2 || !c", "a != 1 and b >= 2 or not c"),
        ("not (a or b)", "not (a or b)"),
        ("a and (b and c)", "a and (b and c)"),
        ("(a and b) and c", "(a and b) and c"),
        ("a or (b or c)", "a or (b or c)"),
        ("(a or b) and c", "(a or b) and c"),
        ("(a and b) or c", "a and b or c"),
        ("x in [1, 2,3]", "x in [1,2,3]"),
        ("x not in [y, 1]", "x not in [y, 1]"),
        ("x is not empty", "x is not empty"),
        ("(a == 1) == true", "(a == 1) == true"),
        ("`my key` == $['and']", '$["my key"] == $["and"]'),
        ("x.`a b`[0][*].*", 'x["a b"][0][*].*'),
        ("$..a", "**.a"),
        ("a ~ 'x'", 'a matches "x"'),
        ("len(x) between 1 and 2", "len(x) between 1 and 2"),
        ("LEN(x)", "len(x)"),
        ("x == {\"b\": 1, 'a': [1.10, 1e400]}", 'x == {"b":1,"a":[1.10,1e400]}'),
    ])
    def test_canonical_text(self, text, expected):
        node = parse_filter(text)
        assert format_expr(node) == expected
        assert parse_filter(expected) == node

    @pytest.mark.parametrize("segments,expected", [
        ((), "$"),
        (("a", "b"), "a.b"),
        (("a b",), '$["a b"]'),
        (("and",), '$["and"]'),
        (("AND",), '$["AND"]'),
        (("empty",), '$["empty"]'),
        (("x", "and"), 'x["and"]'),
        (("0",), '$["0"]'),
        ((0,), "$[0]"),
        (("",), '$[""]'),
        (("$oid",), '$["$oid"]'),
        (("-x",), '$["-x"]'),
        (("*",), '$["*"]'),
        (("**",), '$["**"]'),
        ((Wild.ANY_KEY, "a"), "*.a"),
        ((Wild.DESCENDANTS,), "**"),
        ((Wild.ANY_INDEX,), "$[*]"),
        (("a", Wild.ANY_INDEX, Wild.ANY_KEY, Wild.DESCENDANTS, 3), "a[*].*.**[3]"),
        (('q"uote', "back\\slash"), '$["q\\"uote"]["back\\\\slash"]'),
        (("‘x",), '$["‘x"]'),
        (("koʻcha", "Тошкент"), "koʻcha.Тошкент"),
        (("\ud800",), '$["\\ud800"]'),
    ])
    def test_render_path(self, segments, expected):
        assert Q.render_path(segments) == expected
        node = parse_filter(expected)
        assert isinstance(node, PathExpr) and node.pattern.segments == segments

    def test_render_path_roundtrip_random_keys(self):
        rng = random.Random(5)
        for _ in range(500):
            segs = []
            for _ in range(rng.randint(1, 4)):
                r = rng.random()
                if r < 0.2:
                    segs.append(rng.randint(0, 20))
                elif r < 0.3:
                    segs.append(rng.choice(list(Wild)))
                else:
                    segs.append(rng.choice(jsongen.KEYS + sorted(Q.KEYWORDS)) if rng.random() < 0.5
                                else jsongen.rand_string(rng))
            text = Q.render_path(tuple(segs))
            node = parse_filter(text)
            assert isinstance(node, PathExpr) and node.pattern.segments == tuple(segs), (segs, text)


# ==================================================================================
# JMESPath
# ==================================================================================
class TestJmespath:
    def jm(self, text: str, value: Any = REC) -> bool:
        flt = compile_filter(text, lang="jmespath")
        result = flt.matches(value)
        assert flt.matches_values({PathPattern(()): [value]}) is result
        return result

    def test_truthiness(self):
        assert self.jm("contact.address.city == 'Toshkent'") is True
        assert self.jm("zero") is True  # 0 is TRUE in JMESPath
        assert self.jm("note") is False
        assert self.jm("empty_s") is False
        assert self.jm("empty_a") is False
        assert self.jm("empty_o") is False
        assert self.jm("active") is True
        assert self.jm("nope") is False
        assert self.jm("orders[?oid == `8`]") is True
        assert self.jm("orders[?oid == `9`]") is False
        assert self.jm("length(children) > `2`") is True
        assert self.jm("contains(tags, 'x') && !contains(tags, 'z')") is True

    def test_exact_numbers_in_functions(self):
        rec = {"x": JsonFloat("1.10"), "xs": [JsonFloat("1.10"), 2, JsonInt("-0")], "big": codec.loads("1e400"),
               "items": [{"v": JsonFloat("2.50")}, {"v": 1}]}
        assert self.jm("abs(x) > `1`", rec) is True
        assert self.jm("x == `1.1`", rec) is True
        assert self.jm("max(xs) == `2`", rec) is True
        assert self.jm("min(xs) == `0`", rec) is True
        assert self.jm("sum(xs) > `3`", rec) is True
        assert self.jm("type(x) == 'number'", rec) is True
        assert self.jm("to_string(x) == '1.10'", rec) is True
        assert self.jm("to_string(big) == '1e400'", rec) is True
        assert self.jm("sort_by(items, &v)[0].v == `1`", rec) is True
        assert self.jm("max_by(items, &v).v == `2.5`", rec) is True
        assert self.jm("to_number('1.5') == `1.5`", rec) is True

    def test_decimal_values_via_matches_values(self):
        flt = compile_filter("x > `1` && abs(x) == `1.1`", lang="jmespath")
        assert flt.matches_values({PathPattern(()): [{"x": Decimal("1.10")}]}) is True
        assert flt.matches_values({PathPattern(()): [{"x": Decimal("0.5")}]}) is False
        nested = {"a": [{"b": Decimal("1E+400")}, Decimal("-0")], "x": Decimal("1.1")}
        assert compile_filter("a[0].b > `1` && x == `1.1`", lang="jmespath").matches_values(
            {PathPattern(()): [nested]}) is True

    def test_metadata(self):
        flt = compile_filter("a.b", lang="jmespath")
        assert flt.required_patterns is None
        assert flt.extractable is False
        assert flt.expr is None and flt.lang == "jmespath"
        with pytest.raises(ValueError):
            flt.matches_values({PathPattern(("a",)): [1]})

    @pytest.mark.parametrize("text,pos", [
        ("", 0),
        ("a ==", 4),
        ("a = 1", 2),
        ("a == 'x", 5),
        ("[?a ==", 6),
        ("foo(@)", 0),
        ("a && bar(@, b)", 5),
        ("length(@, @)", 0),
        ("a $ b", 2),
        ("a b", 2),
    ])
    def test_syntax_errors(self, text, pos):
        with pytest.raises(QuerySyntaxError) as info:
            compile_filter(text, lang="jmespath")
        assert info.value.pos == pos, (text, info.value.pos, str(info.value))
        assert info.value.text == text

    def test_runtime_error(self, lang_en):
        flt = compile_filter("abs(name) > `1`", lang="jmespath")
        with pytest.raises(UserInputError) as info:
            flt.matches(REC)
        assert "JMESPath" in str(info.value)

    def test_pickle(self):
        flt = compile_filter("id == `7`", lang="jmespath")
        clone = pickle.loads(pickle.dumps(flt))
        assert clone.matches(REC) is True and clone.lang == "jmespath"

    def test_one_interpreter_per_filter_and_thread(self, monkeypatch):
        """jmespath's ParsedResult.search builds a TreeInterpreter (a reference cycle) per
        call; the filter reuses one per thread (review T1: cyclic garbage per record made
        worker threads run the GC). Results stay identical."""
        import threading

        from jmespath import visitor

        made: list[int] = []
        original_init = visitor.TreeInterpreter.__init__

        def counting_init(self, *args, **kwargs):
            made.append(threading.get_ident())
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(visitor.TreeInterpreter, "__init__", counting_init)
        flt = compile_filter("contact.address.city == 'Toshkent' && length(children) > `2`", lang="jmespath")
        other = {"contact": {"address": {"city": "Samarqand"}}, "children": [1, 2, 3]}
        results = [flt.matches(REC if i % 2 else other) for i in range(1000)]
        assert results == [bool(i % 2) for i in range(1000)]
        assert len(made) == 1
        # the sort_by/&expr functions and errors use the same interpreter
        rec = {"items": [{"v": 2}, {"v": 1}]}
        flt2 = compile_filter("sort_by(items, &v)[0].v == `1`", lang="jmespath")
        assert all(flt2.matches(rec) for _ in range(100))
        bad = compile_filter("abs(name) > `1`", lang="jmespath")
        for _ in range(3):
            with pytest.raises(UserInputError):
                bad.matches(REC)
        assert len(made) == 3
        # another thread gets its own interpreter
        out: list[bool] = []
        thread = threading.Thread(target=lambda: out.extend(flt.matches(REC) for _ in range(100)))
        thread.start()
        thread.join()
        assert out == [True] * 100 and len(made) == 4 and made[-1] != made[0]

    def test_no_cyclic_garbage_per_record(self):
        import gc

        flt = compile_filter("contact.address.city == 'Toshkent' && score > `100`", lang="jmespath")
        flt.matches(REC)
        gc.collect()
        was = gc.isenabled()
        gc.disable()
        try:
            before = len(gc.get_objects())
            for _ in range(2000):
                flt.matches(REC)
            grown = len(gc.get_objects()) - before
        finally:
            if was:
                gc.enable()
        assert grown < 100, grown          # was ~ 2 objects per record (interpreter + method cache)


# ==================================================================================
# Filter API
# ==================================================================================
class TestFilterApi:
    def test_required_patterns_order_and_dedup(self):
        flt = compile_filter("b == 1 and a.x > 2 or b == 3 or $ is empty or exists(c[*])")
        assert flt.required_patterns == [parse_pattern("b"), parse_pattern("a.x"), PathPattern(()),
                                         parse_pattern("c[*]")]
        assert flt.extractable is True
        assert compile_filter("**.a == 1").extractable is False
        assert compile_filter("$key == 1").required_patterns == []

    def test_matches_values_by_index_and_missing(self):
        flt = compile_filter("a == 1 and b[*] == 2")
        assert flt.matches_values({0: [1], 1: [3, 2]}) is True
        assert flt.matches_values({parse_pattern("a"): [1], 1: [3]}) is False
        assert flt.matches_values({parse_pattern("a"): (1,), parse_pattern("b[*]"): iter([2])}) is True
        with pytest.raises(ValueError):
            flt.matches_values({parse_pattern("a"): [1]})

    def test_matches_values_with_streamed_decimals(self):
        flt = compile_filter("x == 1.10 and y[*] > 2.5")
        assert flt.matches_values({parse_pattern("x"): [Decimal("1.1")], parse_pattern("y[*]"): [1, Decimal("2.6")]})

    def test_lazy_resolution_short_circuit(self, monkeypatch):
        calls: list[str] = []
        original = PathPattern.values

        def spy(self, value):
            calls.append(self.text)
            return original(self, value)

        monkeypatch.setattr(PathPattern, "values", spy)
        flt = compile_filter("a == 1 or b == 2")
        assert flt.matches({"a": 1, "b": 2}) is True
        assert calls == ["a"]  # b is never resolved
        calls.clear()
        assert compile_filter("a > 0 and a < 5 and a != 3").matches({"a": 1}) is True
        assert calls == ["a"]  # each pattern is resolved once per record

    def test_pickle_and_repr(self):
        flt = compile_filter('contact.address.city == "Toshkent" and len(children) > 2')
        clone = pickle.loads(pickle.dumps(flt))
        assert clone.matches(REC) is True and clone.text == flt.text
        assert "Toshkent" in repr(flt)

    def test_threads(self):
        import threading

        flt = compile_filter('id in [1, 7] and name startswith "Tosh" and lower(name) ~ "shahri$"')
        errors: list[BaseException] = []

        def work():
            try:
                for _ in range(2000):
                    assert flt.matches(REC) is True
                    assert flt.matches({"id": 2}) is False
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors


# ==================================================================================
# builder model
# ==================================================================================
class TestBuilder:
    @pytest.mark.parametrize("cond,text", [
        (Condition("city", "eq", "Toshkent"), 'city == "Toshkent"'),
        (Condition("city", "ne", None), "city != null"),
        (Condition("name", "contains", "o'z"), 'name contains "o\'z"'),
        (Condition("name", "startswith", "Tosh"), 'name startswith "Tosh"'),
        (Condition("name", "regex", "^T.*t$"), 'name matches "^T.*t$"'),
        (Condition("n", "gt", 1.10), "n > 1.1"),
        (Condition("n", "lt", JsonFloat("1.10")), "n < 1.10"),
        (Condition("n", "ge", 12345678901234567890), "n >= 12345678901234567890"),
        (Condition("n", "le", -1), "n <= -1"),
        (Condition("n", "between", 1, 5), "n between 1 and 5"),
        (Condition("contact.address", "exists"), "exists(contact.address)"),
        (Condition("x", "not_exists", 99), "missing(x)"),
        (Condition("note", "is_null"), "note is null"),
        (Condition("tags", "is_empty"), "tags is empty"),
        (Condition("city", "in", ["A", "B"]), 'city in ["A","B"]'),
        (Condition("tags", "len_eq", 2), "len(tags) == 2"),
        (Condition("tags", "len_ne", 2), "len(tags) != 2"),
        (Condition("tags", "len_gt", 2), "len(tags) > 2"),
        (Condition("tags", "len_lt", 2), "len(tags) < 2"),
        (Condition("tags", "len_ge", 2), "len(tags) >= 2"),
        (Condition("tags", "len_le", 2), "len(tags) <= 2"),
        (Condition("and", "eq", 1), '$["and"] == 1'),
        (Condition('["kalit bo\'shliq bilan"]', "eq", 1), '$["kalit bo\'shliq bilan"] == 1'),
        (Condition("a.b[0][*].c", "eq", {"k": [1]}), 'a.b[0][*].c == {"k":[1]}'),
        (Condition("$key", "startswith", "id"), '$key startswith "id"'),
        (Condition("$index", "lt", 10), "$index < 10"),
        (Condition("", "eq", 5), "$ == 5"),
        (Condition("$", "is_empty"), "$ is empty"),
        (Condition("/a/0", "eq", 1), 'a["0"] == 1'),
        (Condition(parse_pattern("x.y"), "eq", 1), "x.y == 1"),
        (Condition("s", "eq", "\ud800"), 's == "\\ud800"'),
    ])
    def test_condition_text(self, cond, text):
        assert cond.to_text() == text
        assert Group(items=[cond]).to_text() == text
        flt = Filter.from_builder(cond)
        assert flt.text == text
        assert flt.expr == parse_filter(text)

    def test_groups(self):
        a = Condition("a", "eq", 1)
        b = Condition("b", "eq", 2)
        c = Condition("c", "eq", 3)
        cases = [
            (Group("and", [a, b]), "a == 1 and b == 2"),
            (Group("OR", [a, b]), "a == 1 or b == 2"),
            (Group("and", [a, Group("or", [b, c])]), "a == 1 and (b == 2 or c == 3)"),
            (Group("and", [a, Group("and", [b, c])]), "a == 1 and (b == 2 and c == 3)"),
            (Group("and", [a, b], negate=True), "not (a == 1 and b == 2)"),
            (Group("and", [a], negate=True), "not (a == 1)"),
            (Group("or", [a, Group("and", [b, c], negate=True)]), "a == 1 or not (b == 2 and c == 3)"),
            (Group("or", [Group("and", [b])]), "(b == 2)"),
            (Group("and", [a, Group("or", []), Group("and", [Group("or")])]), "a == 1"),
            (Group("and", []), "true"),
            (Group("or", [], negate=True), "true"),
            (Group("and", [Group("or", [Group("and", [])])], negate=True), "true"),
        ]
        for group, text in cases:
            assert group.to_text() == text
            flt = Filter.from_builder(group)
            assert flt.expr == parse_filter(text), text
            for rec in ({"a": 1, "b": 2, "c": 3}, {"a": 1}, {"b": 2, "c": 3}, {}):
                assert flt.matches(rec) == compile_filter(text).matches(rec)

    def test_empty_group_matches_everything(self):
        for group in (Group(), Group("or"), Group("or", negate=True), Group("and", [Group("or", [])], negate=True)):
            flt = Filter.from_builder(group)
            assert flt.matches({}) and flt.matches(None) and flt.required_patterns == []

    def test_builder_semantics(self):
        g = Group("and", [
            Condition("contact.address.city", "in", ["Toshkent", "Тошкент"]),
            Group("or", [Condition("children", "len_gt", 2), Condition("note", "is_null")]),
            Condition("name", "regex", "(?i)^tosh"),
        ])
        flt = Filter.from_builder(g)
        assert flt.matches(REC) is True
        assert flt.matches(dict(REC, name="Samarqand")) is False
        assert flt.matches(dict(REC, children=[], note=1)) is False
        assert flt.matches(dict(REC, children=[], note=None)) is True

    def test_dict_roundtrip(self):
        g = Group("or", [Condition("a", "between", 1, JsonFloat("2.50")),
                         Group("and", [Condition("b", "exists")], negate=True)])
        data = g.to_dict()
        assert codec.loads(codec.dumps(data)) == data  # JSON-serialisable (exact numbers kept)
        assert Group.from_dict(data) == g
        assert Group.from_dict(codec.loads(codec.dumps(data))).to_text() == g.to_text()
        assert Condition.from_dict({"field": "x", "op": "eq", "value": 1}) == Condition("x", "eq", 1)
        data = Condition(parse_pattern('a["b c"][*]'), "eq", 1).to_dict()
        assert codec.loads(codec.dumps(data)) == data
        assert data["field"] == 'a["b c"][*]'
        assert Condition.from_dict(data).to_text() == 'a["b c"][*] == 1'

    def test_op_metadata(self, lang_uz):
        assert len(Q.BUILDER_OPS) == 21 and len(set(Q.BUILDER_OPS)) == 21
        assert {op: Q.op_arity(op) for op in ("eq", "between", "exists", "in")} == {
            "eq": 1, "between": 2, "exists": 0, "in": 1}
        assert Q.op_label("contains") == "ichida bor"
        assert Q.op_label("in") == "ro'yxatda bor"
        with pytest.raises(ValueError):
            Q.op_arity("like")

    def builder_error(self, group: Any) -> QuerySyntaxError:
        with pytest.raises(QuerySyntaxError) as e1:
            Filter.from_builder(group)
        with pytest.raises(QuerySyntaxError) as e2:
            group.to_text()
        assert type(e1.value) is type(e2.value)
        assert e1.value.params == e2.value.params and str(e1.value) == str(e2.value)
        return e1.value

    def test_errors(self, lang_en):
        err = self.builder_error(Group(items=[Condition("a", "eq", 1), Condition("b", "like", 1)]))
        assert err.key == "query.builder.error" and err.params["item"] == (1,)
        assert "unknown filter operator: like" in str(err)
        err = self.builder_error(Group("xor", [Condition("a", "eq", 1)]))
        assert "xor" in str(err) and err.params["item"] == ()
        err = self.builder_error(Group(items=[Group(items=["a == 1"])]))  # type: ignore[list-item]
        assert err.params["item"] == (0, 0)
        for bad in (float("nan"), float("inf"), Decimal("NaN"), [1, float("-inf")]):
            err = self.builder_error(Group(items=[Condition("a", "eq", bad)]))
            assert "finite" in str(err)
        for bad in ({1, 2}, object(), {1: "x"}, b"bytes"):
            err = self.builder_error(Group(items=[Condition("a", "eq", bad)]))
            assert err.key == "query.builder.error"
        err = self.builder_error(Group(items=[Condition(5, "eq", 1)]))  # type: ignore[arg-type]
        assert err.key == "query.builder.error"
        err = self.builder_error(Group(items=[Condition("a", "eq", 1), Condition("a b", "eq", 1)]))
        assert isinstance(err, PathSyntaxError) and err.params["item"] == (1,)

    def test_depth_limits(self):
        deep = Group(items=[Condition("a", "eq", 1)])
        for _ in range(Q.BUILDER_MAX_DEPTH - 1):
            deep = Group("or", [deep], negate=True)
        flt = Filter.from_builder(deep)  # within limits: renders text that parses
        assert compile_filter(flt.text).expr == flt.expr
        self.builder_error(Group(items=[deep]))
        value: Any = 1
        for _ in range(Q.BUILDER_MAX_DEPTH):
            value = {"k": [value]} if isinstance(value, list) else [value]
        self.builder_error(Group(items=[Condition("a", "eq", value)]))
        cyclic: list = []
        cyclic.append(cyclic)
        self.builder_error(Group(items=[Condition("a", "eq", cyclic)]))
        # deepest accepted value still renders text the parser accepts
        value = 1
        for _ in range(Q.BUILDER_MAX_DEPTH - 1):
            value = {"k": value}
        deep_value = Group(items=[Condition("a", "eq", [value])], negate=True)
        for _ in range(Q.BUILDER_MAX_DEPTH - 1):
            deep_value = Group("and", [deep_value], negate=True)
        flt = Filter.from_builder(deep_value)
        assert compile_filter(flt.text).expr == flt.expr

    def test_regex_errors_identical(self, lang_en):
        g = Group(items=[Condition("x", "eq", "A\n"), Condition("name", "regex", "\t(a")])
        err = self.builder_error(g)
        with pytest.raises(QuerySyntaxError) as parsed:
            compile_filter('x == "A\\n" and name matches "\\t(a"')
        assert err.pos == parsed.value.pos and str(err) == str(parsed.value)
        assert 'x == "A\\n" and name matches "\\t(a"'[err.pos] == "("
        err = self.builder_error(Group(items=[Condition("name", "regex", 5)]))
        assert "regex" in str(err)


# ==================================================================================
# property tests: builder <-> expression, parse <-> format, matches_values
# ==================================================================================
FIELDS = [
    "id", "name", "contact.address.city", "contact.address.zip", "contact", "contact.*", "orders", "orders[*].oid",
    "orders[0].sum", "orders[*]", "orders[*].*", "tags", "tags[*]", "tags[0]", "score", "active", "note", "extra",
    "extra.**", "**", "*", "nope", "$", "", "$key", "$index", '["kalit bo\'shliq bilan"]', '["dot.key"]', "Тошкент",
    '[""]', '["0"]', "0", "a/b", "x~y", "and", "or", "null", "empty", "missing", "len", "id.x", '["$oid"]', "/a/0",
]
REGEXES = ["^T", "kent$", "(?i)TOSH", "[0-9]+", "^$", ".", "a|b", "\\d{2,}", "Тош", "o['ʻ‘’]z", "(", "[a-", "*x"]
EDGE_SCALARS = [codec.loads(tok) for tok in jsongen.EDGE_NUMBER_TOKENS] + jsongen.EDGE_STRINGS[:30] + [
    True, False, None, 0, 1, 2, 3, 7, 8, -1, 1.5, "Toshkent", "Тошкент", "x", "yy", ""]

scalars = st.one_of(
    st.sampled_from(EDGE_SCALARS),
    st.integers(-5, 10),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(max_size=6),
)
json_values = st.recursive(
    scalars,
    lambda children: st.one_of(st.lists(children, max_size=3),
                               st.dictionaries(st.sampled_from(jsongen.KEYS[:8]), children, max_size=3)),
    max_leaves=6,
)


@st.composite
def conditions(draw) -> Condition:
    op = draw(st.sampled_from(Q.BUILDER_OPS))
    field = draw(st.sampled_from(FIELDS))
    if op == "regex":
        value = draw(st.one_of(st.sampled_from(REGEXES), st.text(max_size=4)))
    elif op == "in":
        value = draw(st.one_of(st.lists(scalars, max_size=4), json_values))
    elif op.startswith("len_"):
        value = draw(st.one_of(st.integers(-1, 6), scalars))
    else:
        value = draw(json_values)
    value2 = draw(json_values) if op == "between" else None
    return Condition(field, op, value, value2)


builder_trees = st.recursive(
    conditions(),
    lambda children: st.builds(Group, op=st.sampled_from(["and", "or", "AND"]),
                               items=st.lists(children, max_size=4), negate=st.booleans()),
    max_leaves=8,
).map(lambda item: item if isinstance(item, Group) else Group(items=[item]))


def sample_records(seed: int) -> list[tuple[Any, str | None, int | None]]:
    rng = random.Random(seed)
    recs: list[Any] = jsongen.records(6, seed=seed)
    recs += [jsongen.rand_value(rng, 0, 3) for _ in range(3)]
    recs += [{"and": 1, "or": [1, 2], "null": None, "empty": "", "missing": 0, "len": "abc", "0": "zero",
              "a/b": 1.10, "x~y": "x", "": "blank", "$oid": "o", "Тошкент": "Toshkent",
              "kalit bo'shliq bilan": [], "dot.key": {}, "a": ["first", "second"],
              "extra": {"city": "Toshkent", "deep": {"x": [1, 2, 3]}}}]
    return [(rec, rng.choice([None, "id1", "Тошкент", ""]), rng.choice([None, 0, 3, 7])) for rec in recs]


def ref_values(field: Any, rec: Any, key: Any, index: Any) -> list:
    if isinstance(field, str) and field.strip().lower() == "$key":
        return [] if key is None else [key]
    if isinstance(field, str) and field.strip().lower() == "$index":
        return [] if index is None else [index]
    pattern = field if isinstance(field, PathPattern) else parse_pattern(field)
    return pattern.values(rec)


def is_num(v: Any) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def ref_num(v: Any) -> Decimal:
    """Exact decimal value of a number (numbers compare exactly: 1e400 < 2e400)."""
    return v if isinstance(v, Decimal) else Decimal(codec.number_text(v))


def ref_eq(a: Any, b: Any) -> bool:
    """Independent (recursive) JSON equality used as the reference."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if a is None or b is None:
        return a is None and b is None
    if is_num(a) and is_num(b):
        return ref_num(a) == ref_num(b)
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(ref_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(ref_eq(a[k], b[k]) for k in a)
    return False


def ref_order(a: Any, b: Any, op: str) -> bool:
    if not ((isinstance(a, str) and isinstance(b, str)) or (is_num(a) and is_num(b))):
        return False
    if is_num(a):
        a, b = ref_num(a), ref_num(b)
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def ref_contains(container: Any, item: Any) -> bool:
    if isinstance(container, str):
        return isinstance(item, str) and item in container
    if isinstance(container, list):
        return any(ref_eq(x, item) for x in container)
    if isinstance(container, dict):
        return isinstance(item, str) and item in container
    return False


def ref_condition(cond: Condition, rec: Any, key: Any, index: Any) -> bool:
    vals = ref_values(cond.field, rec, key, index)
    v = codec.loads(codec.dumps(cond.value))
    v2 = codec.loads(codec.dumps(cond.value2))
    op = cond.op
    if op == "eq":
        return any(ref_eq(a, v) for a in vals)
    if op == "ne":
        return not any(ref_eq(a, v) for a in vals)
    if op == "contains":
        return any(ref_contains(a, v) for a in vals)
    if op == "startswith":
        return any(isinstance(a, str) and isinstance(v, str) and a.startswith(v) for a in vals)
    if op == "regex":
        return any(isinstance(a, str) and re.search(v, a) is not None for a in vals)
    if op in ("gt", "lt", "ge", "le"):
        sym = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}[op]
        return any(ref_order(a, v, sym) for a in vals)
    if op == "between":
        if ref_order(v, v2, ">"):  # reversed bounds are swapped
            v, v2 = v2, v
        return any(ref_order(v, a, "<=") and ref_order(a, v2, "<=") for a in vals)
    if op == "exists":
        return bool(vals)
    if op == "not_exists":
        return not vals
    if op == "is_null":
        return any(a is None for a in vals)
    if op == "is_empty":
        return any(isinstance(a, (str, list, dict)) and len(a) == 0 for a in vals)
    if op == "in":
        return any(ref_contains(v, a) for a in vals)
    first = vals[0] if vals else None
    length = len(first) if vals and isinstance(first, (str, list, dict)) else None
    if op == "len_eq":
        return length is not None and ref_eq(length, v)
    if op == "len_ne":
        return not (length is not None and ref_eq(length, v))
    sym = {"len_gt": ">", "len_lt": "<", "len_ge": ">=", "len_le": "<="}[op]
    return length is not None and ref_order(length, v, sym)


def ref_is_empty(item: Any) -> bool:
    return isinstance(item, Group) and all(ref_is_empty(sub) for sub in item.items)


def ref_group(group: Group, rec: Any, key: Any, index: Any) -> bool:
    live = [item for item in group.items if not ref_is_empty(item)]
    if not live:
        return True
    results = [ref_group(item, rec, key, index) if isinstance(item, Group) else ref_condition(item, rec, key, index)
               for item in live]
    res = all(results) if group.op.lower() == "and" else any(results)
    return not res if group.negate else res


bad_conditions = st.builds(
    Condition,
    field=st.sampled_from(FIELDS[:6] + ["a b", "a[", "a..", 5, None]),
    op=st.sampled_from(Q.BUILDER_OPS + ("like", "EQ", "")),
    value=st.one_of(json_values, st.sampled_from([float("nan"), float("-inf"), Decimal("sNaN"), {1, 2}, b"x",
                                                   {1: "x"}, "(", "[a-"])),
    value2=st.one_of(json_values, st.just(float("inf"))),
)
bad_trees = st.recursive(
    st.one_of(conditions(), bad_conditions, st.builds(Condition, field=st.sampled_from(FIELDS), op=st.just("regex"),
                                                      value=st.sampled_from(["(", "[a-", "*x", "a{1", "\\"]))),
    lambda children: st.builds(Group, op=st.sampled_from(["and", "or"] * 6 + ["xor", ""]),
                               items=st.lists(st.one_of(children, children, children, children, st.just("junk")),
                                              max_size=3),
                               negate=st.booleans()),
    max_leaves=6,
).map(lambda item: item if isinstance(item, Group) else Group(items=[item]))


class TestProperties:
    @HYP
    @given(tree=bad_trees)
    def test_builder_errors_equal(self, tree: Group):
        """Invalid trees fail identically in from_builder() and to_text() + compile_filter()."""
        outcome = []
        for build in (lambda: Filter.from_builder(tree), lambda: compile_filter(tree.to_text())):
            try:
                flt = build()
            except QuerySyntaxError as exc:
                outcome.append(("error", type(exc), exc.key, exc.pos, str(exc), exc.params.get("item")))
            else:
                outcome.append(("ok", flt.expr))
        assert outcome[0] == outcome[1]

    @settings(HYP, max_examples=400)
    @given(tree=builder_trees, seed=st.integers(0, 10_000))
    def test_builder_equals_expression(self, tree: Group, seed: int):
        """AGENT_PROMPT §14 test 18: builder and expression give identical results."""
        e1 = e2 = None
        try:
            f1 = Filter.from_builder(tree)
        except QuerySyntaxError as exc:
            e1 = exc
        try:
            text = tree.to_text()
            f2 = compile_filter(text)
        except QuerySyntaxError as exc:
            e2 = exc
        assert (e1 is None) == (e2 is None), (e1, e2)
        if e1 is not None:
            assert str(e1) == str(e2) and e1.pos == e2.pos
            return
        assert f1.text == text
        assert f1.expr == f2.expr
        assert f1.required_patterns == f2.required_patterns
        assert format_expr(f2.expr) == format_expr(f1.expr)
        for rec, key, index in sample_records(seed):
            r1 = f1.matches(rec, key=key, index=index)
            assert r1 == f2.matches(rec, key=key, index=index)
            assert r1 == ref_group(tree, rec, key, index), (text, rec, key, index)
            values = {p: p.values(rec) for p in f1.required_patterns}
            assert f1.matches_values(values, key=key, index=index) == r1

    @HYP
    @given(data=st.data())
    def test_parse_format_roundtrip(self, data):
        text = data.draw(expr_texts(3))
        try:
            node = parse_filter(text)
        except QuerySyntaxError:
            return  # e.g. an invalid regex literal
        canonical = format_expr(node)
        node2 = parse_filter(canonical)
        assert node2 == node, (text, canonical)
        assert format_expr(node2) == canonical
        f1 = compile_filter(text)
        f2 = compile_filter(canonical)
        assert f1.required_patterns == f2.required_patterns
        for rec, key, index in sample_records(data.draw(st.integers(0, 1000))):
            r1 = f1.matches(rec, key=key, index=index)
            assert r1 == f2.matches(rec, key=key, index=index)
            values = {p: p.values(rec) for p in f1.required_patterns}
            assert f1.matches_values(values, key=key, index=index) == r1
            assert f1.matches_values({i: values[p] for i, p in enumerate(f1.required_patterns)},
                                     key=key, index=index) == r1


EXPR_PATHS = ["id", "name", "contact.address.city", "orders[*].oid", "orders[0].sum", "tags", "tags[*]", "score",
              "active", "note", "extra", "extra.*", "**.city", "$", "$key", "$index", "$[\"kalit bo'shliq bilan\"]",
              "`dot.key`", "nope", "x.and", "$['and']", "*", "a[1]", "a[]", "Тошкент", "`null`", "$..x"]
EXPR_LITERALS = (["true", "false", "null", "TRUE", "'Toshkent'", '"Тошкент"', '""', "'o\\'z'", '"\\u0041"',
                  "[]", "[1, 'a']", '{"k": [1, null]}', "{}", "[[1], {}]"]
                 + jsongen.EDGE_NUMBER_TOKENS + [codec.dumps(s).decode() for s in jsongen.EDGE_STRINGS[:25]])


@st.composite
def expr_operand(draw, depth: int) -> str:
    choices = ["path", "literal", "call"] + (["paren", "list"] if depth > 0 else [])
    kind = draw(st.sampled_from(choices))
    if kind == "path":
        return draw(st.sampled_from(EXPR_PATHS))
    if kind == "literal":
        return draw(st.sampled_from(EXPR_LITERALS))
    if kind == "call":
        name = draw(st.sampled_from(sorted(Q.FUNCTIONS)))
        arg = draw(expr_operand(depth - 1)) if depth > 0 else draw(st.sampled_from(EXPR_PATHS))
        return f"{name}({arg})"
    if kind == "paren":
        return "(" + draw(expr_texts(depth - 1)) + ")"
    items = draw(st.lists(expr_operand(depth - 1), max_size=3))
    return "[" + ", ".join(items) + "]"


@st.composite
def expr_cmp(draw, depth: int) -> str:
    left = draw(expr_operand(depth))
    form = draw(st.integers(0, 6))
    if form == 0:
        return left
    if form == 1:
        op = draw(st.sampled_from(["==", "=", "!=", "≠", "<", "<=", "≤", ">", ">=", "≥"]))
        return f"{left} {op} {draw(expr_operand(depth))}"
    if form == 2:
        op = draw(st.sampled_from(["contains", "startswith", "endswith"]))
        return f"{left} {op} {draw(expr_operand(depth))}"
    if form == 3:
        op = draw(st.sampled_from(["matches", "~"]))
        rx = draw(st.sampled_from(REGEXES[:10] + ["'x'", "name"]))
        rx = rx if rx in ("'x'", "name") else codec.dumps(rx).decode()
        return f"{left} {op} {rx}"
    if form == 4:
        return f"{left} between {draw(expr_operand(depth))} and {draw(expr_operand(depth))}"
    if form == 5:
        neg = draw(st.sampled_from(["", "not "]))
        return f"{left} {neg}in {draw(expr_operand(depth))}"
    neg = draw(st.sampled_from(["", "not "]))
    return f"{left} is {neg}{draw(st.sampled_from(['null', 'empty', 'missing']))}"


@st.composite
def expr_not(draw, depth: int) -> str:
    prefix = draw(st.sampled_from(["", "", "not ", "!", "not not "]))
    return prefix + draw(expr_cmp(depth))


@st.composite
def expr_texts(draw, depth: int) -> str:
    ors = []
    for _ in range(draw(st.integers(1, 3))):
        ands = [draw(expr_not(depth)) for _ in range(draw(st.integers(1, 3)))]
        ors.append(draw(st.sampled_from([" and ", " && ", " AND "])).join(ands))
    return draw(st.sampled_from([" or ", " || "])).join(ors)


# ==================================================================================
# static safety checks
# ==================================================================================
def test_no_eval_exec_compile_in_query_module():
    src = Path(Q.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"eval", "exec", "compile", "__import__", "execfile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in forbidden, f"forbidden name {node.id!r} at line {node.lineno}"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert node.value.id != "builtins", f"builtins access at line {node.lineno}"
            if node.attr in ("eval", "exec"):
                pytest.fail(f"forbidden attribute {node.attr!r} at line {node.lineno}")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module
                                                    else [])
            for name in names:
                root = name.split(".")[0]
                assert root not in {"socket", "requests", "ssl", "pickle", "marshal", "builtins"}, name
                assert name not in {"urllib.request", "http.client"}, name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print", f"print() at line {node.lineno}"
