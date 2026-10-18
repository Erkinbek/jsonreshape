"""jsonreshape.gui.copyfmt (no Qt): path styles, value formats with exact numbers,
CSV rows like the writer's join mode, Excel TSV, table cell texts."""

from __future__ import annotations

import ast
import csv
import io

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from jsonreshape import codec
from jsonreshape.gui import copyfmt
from jsonreshape.gui.copyfmt import (
    COPY_FORMATS,
    PATH_STYLES,
    VALUE_FORMATS,
    csv_header_and_row,
    csv_line,
    escape_surrogates,
    flatten_row,
    format_label,
    key_text,
    path_text,
    scalar_cell_text,
    style_label,
    tsv_text,
    value_text,
)
from jsonreshape.paths import parse_path
from jsonreshape.settings import Settings, set_settings

EXACT_TOKENS = ["0.1", "1e400", "-1e400", "12345678901234567890", "-0.0", "1.10", "1E+2", "-0", "1e-400",
                "3.141592653589793238462643", "123456789012345678901234567890.5", "0", "-17", "2.5E-3"]
UNICODE_TEXTS = ["O'zbekiston", "Oʻzbekiston gʻalla", "Тошкент шаҳри", "emoji 😀👍🏽", "tab\there", "nl\nx",
                 'q"uote', "back\\slash", "nul\x00", "\u200b zero width"]


def _loads(text: str):
    return codec.loads(text)


# ----------------------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------------------
def test_constants() -> None:
    assert PATH_STYLES == ("dotted", "jsonpath", "pointer", "python", "js")
    assert VALUE_FORMATS == ("json", "json_compact", "python", "csv")
    assert COPY_FORMATS == VALUE_FORMATS + ("csv_header",)


@pytest.mark.parametrize("style, expected", [
    ("dotted", 'data[120].contact["x y"].city'),
    ("jsonpath", "$.data[120].contact['x y'].city"),
    ("pointer", "/data/120/contact/x y/city"),
    ("python", '["data"][120]["contact"]["x y"]["city"]'),
    ("js", 'data[120].contact["x y"].city'),
])
def test_path_styles(style: str, expected: str) -> None:
    path = ("data", 120, "contact", "x y", "city")
    assert path_text(path, style) == expected
    assert path_text(list(path), style) == expected  # any iterable


@pytest.mark.parametrize("style, expected", [
    ("dotted", "$"), ("jsonpath", "$"), ("pointer", ""), ("python", ""), ("js", ""),
])
def test_empty_path(style: str, expected: str) -> None:
    assert path_text((), style) == expected
    assert parse_path(path_text((), style)) == ()


@pytest.mark.parametrize("key", ["a.b", "x y", "it's", 'say "hi"', "$a", "0", "12", "", "*", "**", "a[0]",
                                 "/slash", "~tilde", "#hash", "Тошкент", "Oʻzbek", "😀", "tab\tkey", "nl\nkey",
                                 "back\\slash"])
@pytest.mark.parametrize("style", PATH_STYLES)
def test_keys_needing_quotes_round_trip(key: str, style: str) -> None:
    path = ("root", key, 3, key)
    text = path_text(path, style)
    parsed = parse_path(text)
    if style == "pointer":
        assert parsed == ("root", key, "3", key)  # a pointer cannot tell indices from keys
    else:
        assert parsed == path
    # the key in first position too (dotted/js must not start with an ambiguous form)
    first = path_text((key,), style)
    assert parse_path(first) == (key,)


def test_pointer_escapes() -> None:
    assert path_text(("a/b", "c~d", ""), "pointer") == "/a~1b/c~0d/"


def test_leading_index() -> None:
    assert path_text((0, "a"), "dotted") == "[0].a"
    assert path_text((0, "a"), "jsonpath") == "$[0].a"
    assert path_text((0, "a"), "pointer") == "/0/a"
    assert path_text((0, "a"), "python") == '[0]["a"]'


def test_unicode_keys_stay_raw() -> None:
    path = ("shahar", "Тошкент", "😀")
    assert path_text(path, "dotted") == "shahar.Тошкент.😀"
    assert path_text(path, "python") == '["shahar"]["Тошкент"]["😀"]'
    assert "\\u" not in path_text(path, "jsonpath")


@pytest.mark.parametrize("style", ["dotted", "jsonpath", "python", "js"])
def test_lone_surrogate_keys_are_escaped_and_round_trip(style: str) -> None:
    path = ("a", "\ud800", 0, "x y", "\udc00z", "b")
    text = path_text(path, style)
    assert "\ud800" not in text and "\udc00" not in text
    assert "\\ud800" in text
    assert parse_path(text) == path
    # first segment
    assert parse_path(path_text(("\ud83d",), style)) == ("\ud83d",)


def test_lone_surrogate_pointer_is_escaped_text() -> None:
    text = path_text(("a", "\ud800/"), "pointer")
    assert text == "/a/\\ud800~1"


def test_path_text_unknown_style() -> None:
    with pytest.raises(ValueError):
        path_text(("a",), "xpath")


_special = st.sampled_from([chr(0xD800), chr(0xDFFF), ".", "[", "]", '"', "'", "~", "/", " ", "$", "*", "0", "\\",
                            "#", "\n"])
_keys = st.text(alphabet=st.one_of(st.characters(), _special), min_size=0, max_size=8)
_segments = st.lists(st.one_of(_keys, st.integers(min_value=0, max_value=10**6)), min_size=0, max_size=6)


def _has_surrogate(path: tuple) -> bool:
    return any(isinstance(s, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in s) for s in path)


@hsettings(max_examples=400, deadline=None, derandomize=True, database=None)
@given(_segments)
def test_every_style_round_trips(path: list) -> None:
    path = tuple(path)
    for style in PATH_STYLES:
        text = path_text(path, style)
        assert escape_surrogates(text) == text      # always clipboard-safe
        parsed = parse_path(text)
        if style == "pointer":
            if not _has_surrogate(path):            # pointer: surrogates become literal text
                assert parsed == tuple(str(s) for s in path)
        else:
            assert parsed == path, style


def test_key_text() -> None:
    assert key_text(()) is None
    assert key_text(("a", "b c")) == "b c"
    assert key_text(("a", 12)) == "12"
    assert key_text(("\ud800",)) == "\\ud800"


# ----------------------------------------------------------------------------------
# exact numbers in every format
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("token", EXACT_TOKENS)
def test_exact_number_every_format(token: str) -> None:
    v = _loads(token)
    assert value_text(v, "json") == token
    assert value_text(v, "json_compact") == token
    assert value_text(v, "csv") == token
    assert value_text(v, "csv_header") == "$\n" + token
    assert scalar_cell_text(v) == token
    py = value_text(v, "python")
    assert py in (token, f'float("{token}")')
    assert "inf" not in py.lower().replace("float", "")


@pytest.mark.parametrize("token", EXACT_TOKENS)
def test_exact_numbers_inside_containers(token: str) -> None:
    v = _loads('{"n": %s, "list": [%s, "x"]}' % (token, token))
    assert value_text(v, "json_compact") == '{"n":%s,"list":[%s,"x"]}' % (token, token)
    assert f'"n": {token}' in value_text(v, "json")
    assert value_text(v, "csv", delimiter=",", sep=";") == f"{token},{token};x"
    py = value_text(v, "python", indent=None)
    assert py in (f"{{'n': {token}, 'list': [{token}, 'x']}}",
                  f"{{'n': float(\"{token}\"), 'list': [float(\"{token}\"), 'x']}}")


def test_python_numbers_read_back_as_python_reads_json() -> None:
    plain = ["0.1", "12345678901234567890", "-0.0", "1.10", "1E+2", "-0", "3.141592653589793238462643"]
    for token in plain:
        text = value_text(_loads(token), "python")
        assert text == token
        assert ast.literal_eval(text) == float(token) if "." in token or "E" in token \
            else ast.literal_eval(text) == int(token)
    # numbers a Python literal would turn into inf / 0.0 are spelled as float("...")
    assert value_text(_loads("1e400"), "python") == 'float("1e400")'
    assert value_text(_loads("-1e400"), "python") == 'float("-1e400")'
    assert value_text(_loads("1e-400"), "python") == 'float("1e-400")'
    assert value_text(_loads("0e-400"), "python") == "0e-400"   # zero is zero
    assert value_text(_loads("-0.0e5"), "python") == "-0.0e5"
    expr = ast.parse('float("1e400")', mode="eval").body
    assert isinstance(expr, ast.Call) and expr.func.id == "float"


def test_python_huge_integer_token_stays_an_exact_integer() -> None:
    """codec keeps integer tokens of more than 4000 digits as JsonFloat (Python's int()
    digit limit); the python copy must not turn them into float("...") = inf (A-F8)."""
    import sys

    for token in ("7" * 5000, "-" + "1" + "0" * 4500):
        v = _loads(token)
        assert isinstance(v, codec.JsonFloat)
        text = value_text(v, "python")
        assert text == token
        nested = value_text(_loads('{"n": [%s]}' % token), "python", indent=None)
        assert nested == "{'n': [%s]}" % token
        old_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(0)
        try:
            assert ast.literal_eval(text) == int(token)
        finally:
            sys.set_int_max_str_digits(old_limit)
    # real non-integers out of the float range keep the float("...") spelling
    assert value_text(_loads("7" * 5000 + ".5"), "python") == 'float("%s.5")' % ("7" * 5000)
    assert value_text(_loads("1" + "0" * 400 + "e5"), "python").startswith('float("1')


def test_python_literal_round_trip_with_unicode() -> None:
    value = {"matn": UNICODE_TEXTS, "ok": True, "no": False, "nil": None, "n": [1, -2, 0.5],
             "ichki": {"bo'sh": {}, "ro'yxat": []}, "Тошкент": "шаҳар"}
    for indent in (2, 4, "\t", None):
        text = value_text(value, "python", indent=indent)
        assert ast.literal_eval(text) == value
    pretty = value_text(value, "python", indent=2)
    assert pretty.startswith("{\n  'matn': [\n    \"O'zbekiston\",")
    assert "'Тошкент': 'шаҳар'" in pretty      # no \u escapes for printable text
    assert "😀👍🏽" in pretty
    assert value_text(value, "python", indent=None).startswith("{'matn': [\"O'zbekiston\", ")
    assert value_text({}, "python") == "{}"
    assert value_text([], "python") == "[]"
    assert value_text({"a": []}, "python") == "{\n  'a': []\n}"


def test_python_lone_surrogate() -> None:
    v = _loads(r'"x\ud800y"')
    text = value_text(v, "python")
    assert text == "'x\\ud800y'"
    assert ast.literal_eval(text) == "x\ud800y"


def test_json_formats_unicode_and_indent() -> None:
    value = _loads('{"shahar": "Тошкент", "a": [1, {"b": "Oʻzbek 😀"}], "e": {}, "l": []}')
    pretty = value_text(value, "json")
    assert "\\u" not in pretty
    assert pretty == ('{\n  "shahar": "Тошкент",\n  "a": [\n    1,\n    {\n      "b": "Oʻzbek 😀"\n    }\n  ],\n'
                      '  "e": {},\n  "l": []\n}')
    assert value_text(value, "json", indent=4).splitlines()[1].startswith('    "shahar"')
    assert value_text(value, "json", indent="\t").splitlines()[1].startswith('\t"shahar"')
    assert value_text(value, "json", indent=None) == value_text(value, "json_compact")
    assert value_text(value, "json_compact") == '{"shahar":"Тошкент","a":[1,{"b":"Oʻzbek 😀"}],"e":{},"l":[]}'
    for fmt in ("json", "json_compact"):
        assert codec.loads(value_text(value, fmt)) == value


def test_json_lone_surrogate_is_escaped() -> None:
    v = _loads(r'{"k\udc00": "a\ud800"}')
    assert value_text(v, "json_compact") == '{"k\\udc00":"a\\ud800"}'
    assert codec.loads(value_text(v, "json")) == v


def test_bad_format_and_indent() -> None:
    with pytest.raises(ValueError):
        value_text(1, "yaml")
    with pytest.raises(ValueError):
        value_text([1], "json", indent=99)
    with pytest.raises(ValueError):
        value_text([1], "python", indent="x")


def test_deep_values_do_not_recurse() -> None:
    v: list = []
    for _ in range(5000):
        v = [v]
    text = value_text(v, "python", indent=None)
    assert text == "[" * 5001 + "]" * 5001
    assert value_text(v, "json_compact") == text
    assert value_text(v, "python").count("\n") == 2 * 5000


# ----------------------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------------------
NESTED = ('{"id": 7, "name": "Ali, \\"Vali\\"", "contact": {"address": {"city": "Toshkent", "zip": null},'
          ' "phones": ["+998 90", "+998 91"]}, "tags": [], "orders": [{"oid": 1}, 2, "x"], "ok": true,'
          ' "meta": {}, "a.b": 1.10, "text": "ikki\\nqator"}')


def test_flatten_row_like_join_mode() -> None:
    names, cells = flatten_row(_loads(NESTED), sep=";")
    assert names == ["id", "name", "contact.address.city", "contact.address.zip", "contact.phones", "tags",
                     "orders", "ok", "meta", '["a.b"]', "text"]
    assert cells == ["7", 'Ali, "Vali"', "Toshkent", "", "+998 90;+998 91", "", '{"oid":1};2;x', "true", "{}",
                     "1.10", "ikki\nqator"]


def test_csv_row_and_header_quoting() -> None:
    v = _loads(NESTED)
    row = value_text(v, "csv", sep="|", delimiter=",")
    assert row == ('7,"Ali, ""Vali""",Toshkent,,+998 90|+998 91,,"{""oid"":1}|2|x",true,{},1.10,"ikki\nqator"')
    both = csv_header_and_row(v, sep="|", delimiter=",")
    header, rest = both.split("\n", 1)
    assert header == 'id,name,contact.address.city,contact.address.zip,contact.phones,tags,orders,ok,meta,' \
                     '"[""a.b""]",text'
    assert rest == row
    # the csv module reads it back to the same cells
    parsed = list(csv.reader(io.StringIO(both)))
    assert parsed[1] == flatten_row(v, sep="|")[1]
    assert parsed[0] == flatten_row(v, sep="|")[0]


def test_csv_semicolon_delimiter() -> None:
    v = {"a": "x;y", "b": [1, 2]}
    assert value_text(v, "csv", sep=",", delimiter=";") == '"x;y";1,2'
    assert value_text(v, "csv", sep=";", delimiter=";") == '"x;y";"1;2"'
    assert value_text(v, "csv", sep="|", delimiter="\t") == "x;y\t1|2"


def test_csv_uses_settings_defaults() -> None:
    v = {"a": [1, 2], "b": "p;q"}
    assert value_text(v, "csv") == "1;2,p;q"   # defaults: array_join_sep ";", csv_delimiter ","
    set_settings(Settings(csv_delimiter=";", array_join_sep="|"))
    assert value_text(v, "csv") == '1|2;"p;q"'
    assert value_text(v, "csv_header") == 'a;b\n1|2;"p;q"'


@pytest.mark.parametrize("text, cells", [
    ("null", [""]), ("true", ["true"]), ('"Тошкент"', ["Тошкент"]), ("[1, [2, 3], {\"a\": null}]",
                                                                      ['1;[2,3];{"a":null}']),
    ("{}", ["{}"]), ("[]", [""]), ('[null, "x"]', [";x"]),
])
def test_csv_non_object_values_are_one_column(text: str, cells: list) -> None:
    names, got = flatten_row(_loads(text), sep=";")
    assert names == ["$"]
    assert got == cells


def test_csv_deep_objects_become_json() -> None:
    v: dict = {"leaf": 1}
    for _ in range(70):
        v = {"k": v}
    names, cells = flatten_row(v, sep=";")
    assert len(names) == 1
    assert names[0] == ".".join(["k"] * 64)
    assert cells[0] == codec.dumps(paths_get(v, ("k",) * 64)).decode()


def paths_get(v, path):
    for seg in path:
        v = v[seg]
    return v


def test_csv_many_columns() -> None:
    v = {f"f{i}": i for i in range(20_000)}
    names, cells = flatten_row(v, sep=";")
    assert len(names) == 20_000 and names[-1] == "f19999" and cells[-1] == "19999"
    assert value_text(v, "csv").count(",") == 19_999


def test_csv_lone_surrogate_is_text() -> None:
    v = _loads(r'{"k\ud800": "a\udc00"}')
    header, row = value_text(v, "csv_header", delimiter=",").split("\n")
    assert row == "a\\udc00"
    # the column name is the quoted, escaped path ["k\ud800"] (CSV-quoted: it holds quotes)
    assert header == '"[""k\\ud800""]"'


def test_csv_matches_the_writer(tmp_path) -> None:
    """The CSV row equals what a CSV export (automatic columns, join mode) writes for the
    same single record."""
    from jsonreshape.reader import Document
    from jsonreshape.writer import ExportOptions, export

    src = tmp_path / "one.jsonl"
    src.write_text(NESTED + "\n", encoding="utf-8")
    out = tmp_path / "one.csv"
    with Document.open(src, format="jsonl") as doc:
        export(doc, out, ExportOptions("csv", csv_bom=False, join_sep=";", csv_delimiter=","))
    written = out.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    ours = csv_header_and_row(_loads(NESTED), sep=";", delimiter=",")
    assert list(csv.reader(io.StringIO(written))) == list(csv.reader(io.StringIO(ours)))


def test_csv_line_rfc4180() -> None:
    assert csv_line(["a", "b,c", 'd"e', "f\r\ng", ""], ",") == 'a,"b,c","d""e","f\r\ng",'
    assert csv_line([], ",") == ""


# ----------------------------------------------------------------------------------
# TSV / cells / labels
# ----------------------------------------------------------------------------------
def test_tsv_text_excel_quoting() -> None:
    rows = [["id", "name", "note"], ["1", "a\tb", 'say "hi"'], ["2", "line1\nline2", None], ["3", "cr\rx", ""]]
    assert tsv_text(rows) == ('id\tname\tnote\n1\t"a\tb"\t"say ""hi"""\n2\t"line1\nline2"\t\n3\t"cr\rx"\t')
    assert tsv_text([]) == ""
    assert tsv_text([["\ud800x"]]) == "\\ud800x"
    assert tsv_text([["Тошкент", "😀"]]) == "Тошкент\t😀"


def test_tsv_reads_back_with_excel_dialect() -> None:
    rows = [["a\tb", 'q"x', "n\nl", "plain"], ["", "1.10", "1e400", '"lead']]
    text = tsv_text(rows)
    assert list(csv.reader(io.StringIO(text), dialect="excel-tab")) == rows


def test_scalar_cell_text() -> None:
    assert scalar_cell_text("plain") == "plain"
    assert scalar_cell_text(None) == "null"
    assert scalar_cell_text(True) == "true"
    assert scalar_cell_text(False) == "false"
    assert scalar_cell_text(0) == "0"
    assert scalar_cell_text(_loads("1.10")) == "1.10"
    assert scalar_cell_text([1, 2, 3]) == "[3]"
    assert scalar_cell_text({f"k{i}": i for i in range(5)}) == "{5}"
    assert scalar_cell_text({}) == "{0}"
    assert scalar_cell_text("x" * 10, 5) == "xxxx…"
    assert len(scalar_cell_text("y" * 1000, 50)) == 50
    assert scalar_cell_text("short", 5) == "short"
    assert scalar_cell_text("a\r\nb\nc\td", single_line=True) == "a↵b↵c d"
    assert scalar_cell_text("a\nb", 100) == "a\nb"
    assert scalar_cell_text("\ud800") == "\\ud800"
    assert scalar_cell_text("abc", 0) == ""


def test_labels(lang_uz) -> None:
    assert style_label("dotted") == "Nuqtali"
    assert format_label("csv") == "CSV qatori"
    for style in PATH_STYLES:
        assert style_label(style)
    for fmt in COPY_FORMATS:
        assert format_label(fmt)
    with pytest.raises(ValueError):
        style_label("nope")
    with pytest.raises(ValueError):
        format_label("nope")


def test_labels_english(lang_en) -> None:
    assert style_label("dotted") == "Dotted"
    assert format_label("json") == "JSON (pretty)"


def test_escape_surrogates() -> None:
    assert escape_surrogates("plain") == "plain"
    assert escape_surrogates("a\ud800b\udfff") == "a\\ud800b\\udfff"
    assert escape_surrogates("😀") == "😀"
    assert copyfmt.ROOT_COLUMN == "$"
