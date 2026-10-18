"""Pure logic of the table view (jsonreshape/gui/tableview.py) -- no Qt objects needed.

* :func:`quick_filter_expr`: every form of the quick filter syntax, checked by the
  expression text and by evaluating the compiled filter on records with exact numbers,
  wildcards, ``$key`` / ``$index`` / ``$`` columns; invalid input raises a localized
  ``UserInputError``; random text never raises anything else.
* cell texts (:func:`cell_data`, :func:`copy_text`, :func:`values_text`) and column specs.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from jsonreshape import codec
from jsonreshape.errors import JsonReshapeError, PathSyntaxError, UserInputError
from jsonreshape.gui.tableview import (
    cell_data,
    column_spec,
    copy_text,
    quick_filter_expr,
    validate_column_path,
    values_text,
)
from jsonreshape.i18n import set_language, get_language
from jsonreshape.query import compile_filter

RECORDS_JSON = [
    '{"name":"Toshkent","n":5,"s":"12","z":null,"e":"","tags":["a","b"],"d":"2020-05-01","x":1e400}',
    '{"name":"samarqand","n":12,"s":"abc","z":0,"e":"x","tags":["c"],"d":"2019-01-01","x":1.10}',
    '{"name":"BUXORO","n":5.0,"s":"5","z":"","e":[],"tags":[],"d":"2021-12-31","x":12345678901234567891}',
    '{}',
    '{"name":"Тошкент","n":"5","e":{},"tags":["c","a"],"x":-0}',
    '{"name":"say \\"null\\" a\\\\b","n":-3.5,"z":false,"e":[0],"x":1e399}',
]
RECORDS = [codec.loads(r) for r in RECORDS_JSON]
KEYS = ["k0", "k1", "kk", "k3", "x", "k5"]


def matching(path: str, text: str) -> set[int]:
    expr = quick_filter_expr(path, text)
    assert expr is not None
    flt = compile_filter(expr)
    return {i for i, rec in enumerate(RECORDS) if flt.matches(rec, key=KEYS[i], index=i)}


# ----------------------------------------------------------------------------------
# expression texts
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("path, text, expected", [
    ("name", "abc", 'lower(string(name)) contains "abc"'),
    ("name", "  ABC  ", 'lower(string(name)) contains "abc"'),
    ("name", '"Null"', 'lower(string(name)) contains "null"'),
    ("name", '""', 'name == ""'),
    ("name", "!abc", 'not (lower(string(name)) contains "abc")'),
    ("n", "=12", "n == 12"),
    ("n", "==12", "n == 12"),
    ("n", '="12"', 'n == "12"'),
    ("n", "= Toshkent", 'n == "Toshkent"'),
    ("n", "=true", "n == true"),
    ("n", "=null", "n == null"),
    ("n", "=[1,2]", "n == [1,2]"),
    ("n", "=1.10", "n == 1.10"),
    ("n", "=1e400", "n == 1e400"),
    ("n", "=-0", "n == -0"),
    ("n", "!=x", 'n != "x"'),
    ("n", ">5", "n > 5"),
    ("n", ">= 5", "n >= 5"),
    ("n", "<5", "n < 5"),
    ("n", "<=-1.5", "n <= -1.5"),
    ("d", '>="2020-01-01"', 'd >= "2020-01-01"'),
    ("s", "~^a+$", 's matches "^a+$"'),
    ("s", "!~^a", 'not (s matches "^a")'),
    ("z", "null", "z is null"),
    ("z", "NULL", "z is null"),
    ("z", "!null", "exists(z) and z is not null"),
    ("z", "exists", "exists(z)"),
    ("z", "!exists", "missing(z)"),
    ("z", "missing", "missing(z)"),
    ("z", "!missing", "exists(z)"),
    ("e", "empty", "e is empty"),
    ("e", "!empty", "exists(e) and e is not empty"),
    ("n", "5..10", "n between 5 and 10"),
    ("n", "5 .. 10", "n between 5 and 10"),
    ("n", "-5..-1", "n between -5 and -1"),
    ("n", "5..", "n >= 5"),
    ("n", "..10", "n <= 10"),
    ("n", "1.5..2.5", "n between 1.5 and 2.5"),
    ("d", '"a".."m"', 'd between "a" and "m"'),
    ("n", "a..b", 'lower(string(n)) contains "a..b"'),
    ("n", "..", 'lower(string(n)) contains ".."'),
    ("n", "1...5", 'lower(string(n)) contains "1...5"'),
    ("$key", "x", 'lower(string($key)) contains "x"'),
    ("$index", "<3", "$index < 3"),
    ("$", "=7", "$ == 7"),
    ("orders[*].id", ">1", "orders[*].id > 1"),
    ("and", "=1", '$["and"] == 1'),
    ('["a b"].c', "=1", '$["a b"].c == 1'),
    ("a", 'q"uote\\', 'lower(string(a)) contains "q\\"uote\\\\"'),
])
def test_expression_text(path: str, text: str, expected: str) -> None:
    assert quick_filter_expr(path, text) == expected
    compile_filter(expected)   # always a valid expression


@pytest.mark.parametrize("text", ["", "   ", "\t\n", None])
def test_empty_text_is_no_filter(text) -> None:
    assert quick_filter_expr("a", text) is None


# ----------------------------------------------------------------------------------
# semantics on records
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("path, text, expected", [
    ("name", "tosh", {0}),                    # case-insensitive
    ("name", "TOSH", {0}),
    ("name", "тош", {4}),                     # Cyrillic lower-casing
    ("name", "bux", {2}),
    ("name", '"null"', {5}),                  # quoted keyword = text
    ("name", 'a\\b', {5}),
    ("name", "!tosh", {1, 2, 3, 4, 5}),       # missing counts as "does not contain"
    ("n", "12", {1}),                         # numbers by their text
    ("n", "5", {0, 2, 4, 5}),                 # by text: 5, 5.0, "5", -3.5
    ("n", "=5", {0, 2}),                      # exact decimal equality: 5 == 5.0, never "5"
    ("n", '="5"', {4}),
    ("n", "!=5", {1, 3, 4, 5}),
    ("n", ">5", {1}),
    ("n", ">=5", {0, 1, 2}),
    ("n", "<5", {5}),
    ("n", "<=5", {0, 2, 5}),
    ("n", "5..12", {0, 1, 2}),
    ("n", "12..5", {0, 1, 2}),               # reversed bounds are swapped
    ("n", "..0", {5}),
    ("n", "6..", {1}),
    ("d", '>="2020-01-01"', {0, 2}),
    ("d", '"2019".."2020"', {1}),
    ("s", "~^[0-9]+$", {0, 2}),
    ("s", "!~^[0-9]+$", {1, 3, 4, 5}),
    ("z", "null", {0}),
    ("z", "!null", {1, 2, 5}),
    ("z", "exists", {0, 1, 2, 5}),
    ("z", "missing", {3, 4}),
    ("e", "empty", {0, 2, 4}),
    ("e", "!empty", {1, 5}),
    ("e", '""', {0}),
    ("tags[*]", "=c", {1, 4}),               # wildcard: some value
    ("tags", "empty", {2}),
    ("tags", '"c"', {1, 4}),                 # containers by their compact JSON
    ("x", "=1e400", {0}),                     # exact numbers
    ("x", "=1.1", {1}),
    ("x", ">12345678901234567890", {0, 2, 5}),
    ("x", "=0", {4}),                         # -0 == 0
    ("x", ">1e398", {0, 5}),
    ("$key", "=k1", {1}),
    ("$key", "k", {0, 1, 2, 3, 5}),
    ("$index", "<2", {0, 1}),
    ("$index", "2..3", {2, 3}),
    ("$", "exists", {0, 1, 2, 3, 4, 5}),
])
def test_semantics(path: str, text: str, expected: set[int]) -> None:
    assert matching(path, text) == expected


# ----------------------------------------------------------------------------------
# invalid input
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("path, text, key", [
    ("a", "=", "gui.table.qf.need_value"),
    ("a", "==  ", "gui.table.qf.need_value"),
    ("a", "!=", "gui.table.qf.need_value"),
    ("a", ">", "gui.table.qf.need_value"),
    ("a", "~", "gui.table.qf.need_value"),
    ("a", "!", "gui.table.qf.need_value"),
    ("a", ">abc", "gui.table.qf.need_number"),
    ("a", ">=true", "gui.table.qf.need_number"),
    ("a", "<[1]", "gui.table.qf.need_number"),
    ("a", "<=null", "gui.table.qf.need_number"),
    ("a", "~[", "gui.table.qf.invalid"),
    ("a", "!~(x", "gui.table.qf.invalid"),
    ("a", "={}", "gui.table.qf.no_objects"),
    ("a", '=[1,{"b":2}]', "gui.table.qf.no_objects"),
    ("a", '5.."x"', "gui.table.qf.range_types"),
    ("a b", "x", "gui.table.qf.bad_column"),
    ("", "x", "gui.table.qf.bad_column"),
    ("a[", "x", "gui.table.qf.bad_column"),
])
def test_invalid_input(path: str, text: str, key: str) -> None:
    with pytest.raises(UserInputError) as info:
        quick_filter_expr(path, text)
    assert info.value.key == key


def test_error_messages_are_localized() -> None:
    old = get_language()
    try:
        texts = {}
        for lang in ("uz", "en"):
            set_language(lang)
            with pytest.raises(UserInputError) as info:
                quick_filter_expr("a", ">abc")
            texts[lang] = info.value.message
            assert ">" in texts[lang] and "abc" in texts[lang]
            with pytest.raises(UserInputError) as info:
                quick_filter_expr("a", "~[")
            assert info.value.message and "gui.table" not in info.value.message
        assert texts["uz"] != texts["en"]
    finally:
        set_language(old)


@settings(max_examples=300, deadline=None)
@given(text=st.text(alphabet=st.sampled_from(list('ab1 .=!<>~"\\[]{}-e0*$')), max_size=12),
       path=st.sampled_from(["a", "a.b", "a[*].c", "$key", "$index", "$"]))
def test_random_text_gives_a_valid_expression_or_a_user_error(text: str, path: str) -> None:
    try:
        expr = quick_filter_expr(path, text)
    except UserInputError:
        return
    if expr is None:
        assert not text.strip()
        return
    flt = compile_filter(expr)                  # must compile
    for i, rec in enumerate(RECORDS):
        flt.matches(rec, key=KEYS[i], index=i)  # and evaluate without raising


# ----------------------------------------------------------------------------------
# cells and columns
# ----------------------------------------------------------------------------------
def test_column_specs() -> None:
    assert column_spec("$key").kind == "key" and column_spec(" $index ").kind == "index"
    spec = column_spec("orders[*].id")
    assert spec.kind == "value" and spec.prefix() == ("orders",) and spec.sort_block() is None
    assert column_spec("a.b[2].c").prefix() == ("a", "b", 2, "c")
    assert column_spec("**.id").sort_block()
    bad = column_spec("a[")
    assert bad.pattern is None and bad.error and bad.sort_block() == bad.error
    assert validate_column_path(" a.b ") == "a.b"
    assert validate_column_path("$key") == "$key"
    with pytest.raises(PathSyntaxError):
        validate_column_path("a[")
    with pytest.raises(UserInputError):
        validate_column_path("  ")


def test_cell_texts_and_copy_texts() -> None:
    value = codec.loads('{"a":1.10,"b":[1,{"x":2}],"c":"l1\\nl2\\tt","d":null,"t":true,'
                        '"o":[{"id":1},{"id":"2"},{"q":0}],"big":-123456789012345678901234567890,'
                        '"s":"\\ud800x"}')
    cases = {
        "a": ("1.10", "number", ("a",), False, "1.10"),
        "b": ("[2]", "array", ("b",), True, '[1,{"x":2}]'),
        "b[1]": ("{1}", "object", ("b", 1), True, '{"x":2}'),
        "b.1.x": ("2", "number", ("b", 1, "x"), False, "2"),
        "c": ("l1↵l2 t", None, ("c",), False, "l1\nl2\tt"),
        "d": ("null", "null", ("d",), False, "null"),
        "t": ("true", "bool", ("t",), False, "true"),
        "o[*].id": ("1; 2", None, ("o",), False, "1; 2"),
        "o[*]": ("{1}; {1}; {1}", "object", ("o",), False, '{"id":1}; {"id":"2"}; {"q":0}'),
        "big": ("-123456789012345678901234567890", "number", ("big",), False, "-123456789012345678901234567890"),
        "nope": ("", None, (), False, ""),
        "$": ("{8}", "object", (), True, None),
        "s": ("\\ud800x", None, ("s",), False, "\\ud800x"),   # lone surrogates escaped
    }
    for path, (text, role, ref_path, container, copied) in cases.items():
        spec = column_spec(path)
        cd = cell_data(value, spec, 4, None)
        assert (cd.cell.text, cd.cell.role, cd.path, cd.container) == (text, role, ref_path, container), path
        if copied is not None:
            assert copy_text(value, spec, 4) == copied, path
    assert cell_data(value, column_spec("c"), 0).cell.tooltip == "l1\nl2\tt"
    assert cell_data(value, column_spec("a"), 0).cell.tooltip is None
    key = cell_data(value, column_spec("$key"), 3, "kalit")
    assert (key.cell.text, key.cell.role, key.path) == ("kalit", "key", ())
    assert cell_data(value, column_spec("$key"), 3, None).cell.text == ""
    assert cell_data(value, column_spec("$index"), 3).cell.text == "3"
    assert copy_text(value, column_spec("$index"), 3) == "3"
    long = cell_data({"s": "y" * 5000}, column_spec("s"), 0)
    assert len(long.cell.text) == 1000 and long.cell.text.endswith("…") and long.full == "y" * 5000
    assert long.cell.tooltip.startswith("yyy") and len(long.cell.tooltip) <= 4001
    many = cell_data({"a": list(range(1500))}, column_spec("a[*]"), 0)
    assert many.full.endswith("; …") and many.full.count(";") == 1000
    assert copy_text({"a": list(range(1500))}, column_spec("a[*]"), 0).count(";") == 1499


def test_values_text_roles() -> None:
    assert values_text([]) == ("", None)
    assert values_text([1, 2.5]) == ("1; 2.5", "number")
    assert values_text([True, None]) == ("true; null", None)
    assert values_text(["a"], more=3) == ("a; …", None)
    assert values_text([{"a": 1}, {}]) == ("{1}; {0}", "object")


def test_every_message_key_exists() -> None:
    from jsonreshape.i18n import has

    for key in ("gui.table.qf.need_value", "gui.table.qf.need_number", "gui.table.qf.invalid",
                "gui.table.qf.no_objects", "gui.table.qf.range_types", "gui.table.qf.bad_column",
                "gui.table.qf.help"):
        assert has(key)
    assert isinstance(JsonReshapeError("gui.table.qf.help").message, str)
