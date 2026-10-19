"""jsonreshape.editops (docs/ARCHITECTURE.md §12.4): locating nodes, validation, encoding,
formatting, byte splices and type conversion.

Every splice result is checked to be valid JSON whose parsed value equals the expected
Python edit, and every byte outside the edited region to be unchanged. Locating runs on
both core backends (``core_backend``).
"""

from __future__ import annotations

import json
from array import array

import pytest

from jsonreshape import codec, editops
from jsonreshape.core.constants import FLAG_ESCAPES, FLAG_NON_ASCII, FLAG_U_ESCAPES
from jsonreshape.editops import DocStyle
from jsonreshape.errors import ConversionError, EditError, JsonReshapeError
from jsonreshape.i18n import has, t

PRETTY2 = DocStyle(newline=b"\n", record_sep=b",\n  ", record_level=1, unit="  ", colon=b": ", empty=b"[]",
                   comma=b", ")
PRETTY2_CRLF = DocStyle(newline=b"\r\n", record_sep=b",\r\n  ", record_level=1, unit="  ", colon=b": ",
                        empty=b"[]", comma=b", ")
TABS = DocStyle(newline=b"\n", record_sep=b",\n\t", record_level=1, unit="\t", colon=b": ", empty=b"[]", comma=b", ")
COMPACT = DocStyle()
PYLINE = DocStyle(newline=b"\n", record_sep=b"\n", record_level=0, unit=None, colon=b": ", empty=b"", comma=b", ")


@pytest.fixture(autouse=True)
def _backend(core_backend):
    """Run every test of this module on each core backend."""
    old = editops.set_core_backend(core_backend)
    yield core_backend
    editops.set_core_backend(old)


def exact(data: bytes) -> bytes:
    """Canonical compact text of JSON bytes (exact number tokens, member order kept)."""
    return codec.dumps(codec.loads(data))


def valid(data: bytes) -> bool:
    editops.check_unit(data, member=False)
    return True


# ----------------------------------------------------------------------------------
# locate / child_locs
# ----------------------------------------------------------------------------------
DOC = (b'{\n  "a": 1,\n  "b": [1, {"x": null}],\n  "esc\\u0061": "v",\n  "a": 3,\n'
       b'  "\xd1\x88": {"\\ud800": true, "\\ud83d\\ude00": [ ]}\n}')


def test_locate_root_and_children() -> None:
    root = editops.locate(DOC)
    assert (root.start, root.end, root.kind, root.depth, root.key, root.index) == (0, len(DOC), "object", 0, None, None)
    kids = editops.child_locs(DOC, root)
    assert [k.key for k in kids] == ["a", "b", "esca", "a", "ш"]
    assert [k.index for k in kids] == [0, 1, 2, 3, 4]
    assert [k.kind for k in kids] == ["number", "array", "string", "number", "object"]
    for k in kids:
        assert DOC[k.mstart:k.kend].startswith(b'"') and DOC[k.kend - 1:k.kend] == b'"'
        assert (k.pstart, k.pend, k.depth) == (root.start, root.end, 1)
        codec.loads(DOC[k.start:k.end])  # each value span is one JSON value
    assert DOC[kids[1].start:kids[1].end] == b'[1, {"x": null}]'


def test_duplicate_keys_path_last_wins_ipath_exact() -> None:
    last = editops.locate(DOC, ("a",))
    assert DOC[last.start:last.end] == b"3" and last.index == 3
    first = editops.locate(DOC, ("a",), (0,))
    assert DOC[first.start:first.end] == b"1" and first.index == 0
    assert editops.locate(DOC, ("a",), (3,)).index == 3
    with pytest.raises(EditError) as ei:
        editops.locate(DOC, ("a",), (1,))  # ipath names "b": the key must agree
    assert ei.value.key == "edits.error.path_not_found"
    # an incomplete ipath is ignored (path mode)
    assert editops.locate(DOC, ("a",), ()).index == 3


def test_locate_escaped_and_surrogate_keys() -> None:
    assert DOC[editops.locate(DOC, ("esca",)).start:][:3] == b'"v"'
    lone = editops.locate(DOC, ("ш", "\ud800"))
    assert lone.key == "\ud800" and DOC[lone.start:lone.end] == b"true"
    pair = editops.locate(DOC, ("ш", "\U0001F600"))
    assert pair.kind == "array" and DOC[pair.start:pair.end] == b"[ ]"
    assert editops.child_locs(DOC, pair) == []


def test_locate_arrays_and_canonical_indices() -> None:
    assert DOC[editops.locate(DOC, ("b", 1, "x")).start:][:4] == b"null"
    assert editops.locate(DOC, ("b", "1", "x")).kind == "null"
    for bad in (("b", "01"), ("b", 2), ("b", -1), ("b", True), ("a", "x"), ("zz",), ("b", 1, 0), ("a", 0)):
        with pytest.raises(EditError) as ei:
            editops.locate(DOC, bad)
        assert ei.value.key == "edits.error.path_not_found"
        assert ei.value.message  # localized, with the path


def test_locate_scalar_root_and_whitespace() -> None:
    buf = b' \r\n "x" \t'
    loc = editops.locate(buf)
    assert (loc.start, loc.end, loc.kind) == (4, 7, "string")
    with pytest.raises(EditError):
        editops.locate(buf, ("a",))
    with pytest.raises(EditError) as ei:
        editops.child_locs(buf, loc)
    assert ei.value.key == "edits.error.not_container"


def test_invalid_buffer_is_an_internal_error(caplog) -> None:
    with pytest.raises(EditError) as ei:
        editops.locate(b'{"a": [1, }', ("a",))
    assert ei.value.key == "edits.error.internal"
    assert "editops" in caplog.text


def test_member_loc() -> None:
    buf = b'"k\\u00e9y" :\t{"a": 1}'
    loc = editops.member_loc(buf)
    assert loc.key == "kéy" and buf[loc.start:loc.end] == b'{"a": 1}' and loc.kind == "object"
    assert buf[loc.mstart:loc.kend] == b'"k\\u00e9y"'
    assert editops.rename_key(buf, loc, "new") == b'"new" :\t{"a": 1}'
    assert editops.set_value(buf, loc, b"[1,2]", PRETTY2, base_level=1) == b'"k\\u00e9y" :\t[\n    1,\n    2\n  ]'


def test_line_level() -> None:
    buf = b'{\n    "a": {\n      "b": 1\n    }\n  }'
    assert editops.line_level(buf, 0, "  ", 1) == 1
    assert editops.line_level(buf, buf.index(b'"a"'), "  ", 1) == 2
    assert editops.line_level(buf, buf.index(b'"b"'), "  ", 1) == 3
    assert editops.line_level(buf, buf.index(b'"b"'), None, 5) == 5
    assert editops.line_level(b'{\n\t\t"a": 1\n\t}', 5, "\t", 0) == 2


# ----------------------------------------------------------------------------------
# check_json / check_unit / encoding
# ----------------------------------------------------------------------------------
def test_check_json_returns_stripped_bytes() -> None:
    assert editops.check_json(' \r\n {"a": [1, 2]}\t\n') == b'{"a": [1, 2]}'
    assert editops.check_json(b"  12345678901234567890 ") == b"12345678901234567890"
    assert editops.check_json('"Тошкент ўзбек"') == '"Тошкент ўзбек"'.encode()
    assert editops.check_json("1e400") == b"1e400"


def _invalid(text):
    with pytest.raises(EditError) as ei:
        editops.check_json(text)
    exc = ei.value
    assert exc.key == "edits.error.invalid_json"
    return exc


def test_check_json_offsets_are_code_points_of_the_original(lang_uz) -> None:
    text = ' \t\n  {"шаҳар": "Toshkent 😀", "x": 1 2}'
    exc = _invalid(text)
    assert exc.char_offset == text.index("2}")
    assert exc.offset == len(text[:exc.char_offset].encode())
    assert exc.line == 2
    assert exc.column == len('  {"шаҳар": "Toshkent 😀", "x": 1 ') + 1
    assert exc.params["code"] == "unexpected_char" and exc.params["expected"] == "comma_or_brace"
    assert "2-qator" in exc.message and t("json.error.unexpected_char") in exc.message
    assert exc.hint


def test_check_json_bytes_and_crlf() -> None:
    exc = _invalid(b"[1,\r\n 2,\r\n x]")
    assert (exc.line, exc.column, exc.char_offset, exc.offset) == (3, 2, 11, 11)
    exc = _invalid('[1,\r\n "ўқ",\r\n ]')
    assert (exc.line, exc.column) == (3, 2)
    assert exc.char_offset == len('[1,\r\n "ўқ",\r\n ')


def test_check_json_reasons(lang_en) -> None:
    empty = _invalid("  \n ")
    assert (empty.line, empty.column, empty.char_offset) == (2, 2, 4)
    assert empty.params["reason"] == t("edits.reason.empty")
    eof = _invalid('{"a": [1, 2')
    assert eof.params["reason"].startswith(t("edits.reason.unexpected_eof"))
    trailing = _invalid("[1] [2]")
    assert trailing.params["reason"] == f"{t('json.error.trailing_data')}; {t('edits.reason.expected_end')}"
    assert trailing.char_offset == 4
    for text in ('"a\\x"', "nul", "01", "[1,]", '{"a" 1}', "NaN", "'x'", '"\x01"'):
        assert _invalid(text).params["reason"]


def test_check_json_lone_surrogates_and_invalid_utf8() -> None:
    exc = _invalid('["ok", "\ud800"]')  # a raw lone surrogate in the str
    assert exc.char_offset == 8 and exc.params["code"] == "invalid_utf8"
    exc = _invalid(b'["\xff"]')
    assert exc.char_offset == 2 and exc.params["code"] == "invalid_utf8"
    assert editops.check_json('"\\ud800"') == b'"\\ud800"'  # an escaped lone surrogate is valid JSON
    with pytest.raises(TypeError):
        editops.check_json(12)  # type: ignore[arg-type]


def test_check_unit_flags_and_failures(caplog) -> None:
    assert editops.check_unit(b'{"a": [1, 2]}', member=False) == 0
    assert editops.check_unit(b'"a\\n"', member=False) == FLAG_ESCAPES
    assert editops.check_unit(b'"\\u0041"', member=False) == FLAG_ESCAPES | FLAG_U_ESCAPES
    assert editops.check_unit('"ö"'.encode(), member=False) == FLAG_NON_ASCII
    assert editops.check_unit(b'"k\\t": {"x": "\xc3\xb6"}', member=True) == FLAG_ESCAPES | FLAG_NON_ASCII
    for unit, member in ((b" 1", False), (b"1 ", False), (b"[1,", False), (b"1 2", False), (b"", False),
                         (b'"a": 1, "b": 2', True), (b' "a": 1', True), (b'"a": 1 ', True), (b"[1]", True),
                         (b"", True), (b'"a": ', True)):
        with pytest.raises(EditError) as ei:
            editops.check_unit(unit, member=member)
        assert ei.value.key == "edits.error.internal", unit
    assert "check_unit" in caplog.text


@pytest.mark.parametrize("s", ["", "Toshkent", "Oʻzbekiston", "Тошкент", 'q"uote', "back\\slash", "tab\tnew\nline",
                               "nul\x00", "  ", "emoji 😀", "\ud800", "a\udfffb", "/slash/"])
def test_encode_string_round_trip(s) -> None:
    tok = editops.encode_string(s)
    assert json.loads(tok.decode("utf-8")) == s
    assert editops.encode_key(s) == tok == codec.dumps(s)
    assert valid(tok)
    if not s.isascii() and "\ud800" not in s and "\udfff" not in s:
        assert s.encode("utf-8") in tok  # non-ASCII stays raw


def test_encode_string_type() -> None:
    with pytest.raises(TypeError):
        editops.encode_string(b"x")  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------
# formatting
# ----------------------------------------------------------------------------------
VALUE = b'{ "a" : [ 1 , 2.50 , {} ] , "s" : "x : y , z" , "e" : [ ] , "n" : 1e400 }'


def test_format_value_compact_variants() -> None:
    assert editops.format_value(VALUE, 3, COMPACT) == b'{"a":[1,2.50,{}],"s":"x : y , z","e":[],"n":1e400}'
    assert editops.format_value(VALUE, 3, PYLINE) == b'{"a": [1, 2.50, {}], "s": "x : y , z", "e": [], "n": 1e400}'
    assert editops.format_value(VALUE, 0, PRETTY2, compact=True) == editops.format_value(VALUE, 0, PYLINE)
    assert editops.format_value(b'  "x : y"  ', 2, PRETTY2) == b'"x : y"'
    assert editops.format_value(b"-0.0", 2, PRETTY2) == b"-0.0"


def test_format_value_pretty_levels_crlf_tabs_colon() -> None:
    assert editops.format_value(VALUE, 1, PRETTY2) == (
        b'{\n    "a": [\n      1,\n      2.50,\n      {}\n    ],\n    "s": "x : y , z",\n    "e": [],\n'
        b'    "n": 1e400\n  }')
    assert editops.format_value(VALUE, 1, PRETTY2_CRLF) == editops.format_value(VALUE, 1, PRETTY2).replace(
        b"\n", b"\r\n")
    assert editops.format_value(b'[{"k":1}]', 0, TABS) == b'[\n\t{\n\t\t"k": 1\n\t}\n]'
    tight = DocStyle(unit="    ", colon=b":", newline=b"\n")
    assert editops.format_value(b'{"k": {"a b": "c: d"}}', 0, tight) == (
        b'{\n    "k":{\n        "a b":"c: d"\n    }\n}')
    assert editops.format_value(b"[]", 4, PRETTY2) == b"[]"
    with pytest.raises(ValueError):
        editops.format_value(b"[1]", 0, DocStyle(unit=" x"))


def test_format_record() -> None:
    assert editops.format_record(b'{"a":1}', PRETTY2) == b'{\n    "a": 1\n  }'
    assert editops.format_record(b'{"a":1}', PRETTY2, key="k1") == b'"k1": {\n    "a": 1\n  }'
    assert editops.format_record(b'{"a":1}', COMPACT, key="Тошкент") == '"Тошкент":{"a":1}'.encode()
    assert editops.format_record(b'{"a":1}', PYLINE, key="q") == b'"q": {"a": 1}'
    head = b'"orig" :  '
    assert editops.format_record(b'[1]', PRETTY2, key="ignored", member_head=head) == b'"orig" :  [\n    1\n  ]'
    assert editops.format_record(b"  5 ", PRETTY2) == b"5"
    assert valid(editops.format_record(b'{"a":[1,{"b":[]}]}', TABS))


def test_free_key() -> None:
    assert editops.free_key({"a"}, "b") == "b"
    assert editops.free_key({"a", "a_2"}, "a") == "a_3"
    assert editops.free_key({"a", "a_3"}, "a") == "a_2"
    assert editops.free_key(["3"], "3") == "3_2"
    assert editops.free_key({"3", "3_2"}, 3) == "3_3" and editops.free_key(set(), 0) == "0"
    assert editops.free_key({"x_2"}, "x_2") == "x_2_2"
    assert editops.free_key(set(), "") == ""


# ----------------------------------------------------------------------------------
# splices
# ----------------------------------------------------------------------------------
PRETTY_RECORD = b'{\n    "id": 1,\n    "tags": ["a", "b"],\n    "m": {},\n    "list": [\n      1,\n      2\n    ]\n  }'


def _splice_ok(old: bytes, new: bytes, lo: int, hi_from_end: int) -> None:
    """``new`` keeps ``old[:lo]`` and the last ``hi_from_end`` bytes of ``old``."""
    assert new[:lo] == old[:lo]
    assert new[len(new) - hi_from_end:] == old[len(old) - hi_from_end:]
    assert valid(new)


def test_set_value_in_multiline_and_single_line_containers() -> None:
    loc = editops.locate(PRETTY_RECORD, ("m",))
    out = editops.set_value(PRETTY_RECORD, loc, b'{"x":[1,2]}', PRETTY2, base_level=1)
    assert b'"m": {\n      "x": [\n        1,\n        2\n      ]\n    },' in out
    _splice_ok(PRETTY_RECORD, out, loc.start, len(PRETTY_RECORD) - loc.end)
    loc = editops.locate(PRETTY_RECORD, ("tags", 1))
    out = editops.set_value(PRETTY_RECORD, loc, b'{"x":[1,2]}', PRETTY2, base_level=1)
    assert b'"tags": ["a", {"x": [1, 2]}],' in out
    assert json.loads(out)["tags"][1] == {"x": [1, 2]}
    loc = editops.locate(PRETTY_RECORD, ("list", 0))
    out = editops.set_value(PRETTY_RECORD, loc, b'{"x":1}', PRETTY2, base_level=1)
    assert b'"list": [\n      {\n        "x": 1\n      },\n      2\n    ]' in out


def test_set_value_root_and_compact_styles() -> None:
    root = editops.locate(PRETTY_RECORD)
    assert editops.set_value(PRETTY_RECORD, root, b'{"a":[1]}', PRETTY2, base_level=1) == (
        b'{\n    "a": [\n      1\n    ]\n  }')
    line = b'{"a": 1, "b": {"c": [1, 2]}}'
    loc = editops.locate(line, ("b", "c"))
    out = editops.set_value(line, loc, b'{"q" : "w"}', PYLINE, base_level=0)
    assert out == b'{"a": 1, "b": {"c": {"q": "w"}}}'
    out = editops.set_value(line, editops.locate(line), b'[1, {"k": 2}]', PYLINE, base_level=0)
    assert out == b'[1, {"k": 2}]' and b"\n" not in out
    # numbers are kept exactly
    out = editops.set_value(line, editops.locate(line, ("a",)), b"12345678901234567890", PYLINE, base_level=0)
    assert exact(out) == b'{"a":12345678901234567890,"b":{"c":[1,2]}}'


def test_set_value_crlf_record() -> None:
    rec = PRETTY_RECORD.replace(b"\n", b"\r\n")
    loc = editops.locate(rec, ("m",))
    out = editops.set_value(rec, loc, b'{"x":1}', PRETTY2_CRLF, base_level=1)
    assert b'"m": {\r\n      "x": 1\r\n    },' in out
    assert b"\n" not in out.replace(b"\r\n", b"")


def test_rename_key() -> None:
    loc = editops.locate(PRETTY_RECORD, ("tags",))
    out = editops.rename_key(PRETTY_RECORD, loc, 'ta"gs ўз')
    assert list(json.loads(out)) == ["id", 'ta"gs ўз', "m", "list"]
    _splice_ok(PRETTY_RECORD, out, loc.mstart, len(PRETTY_RECORD) - loc.kend)
    assert editops.rename_key(PRETTY_RECORD, loc, "tags") is PRETTY_RECORD
    with pytest.raises(EditError) as ei:
        editops.rename_key(PRETTY_RECORD, loc, "m")
    assert ei.value.key == "edits.error.key_exists" and ei.value.params == {"key": "m"}
    for bad in (editops.locate(PRETTY_RECORD, ("list", 0)), editops.locate(PRETTY_RECORD)):
        with pytest.raises(EditError) as ei:
            editops.rename_key(PRETTY_RECORD, bad, "x")
        assert ei.value.key == "edits.error.not_member"
    # a duplicate key may be renamed to anything not used by the OTHER members
    dup = b'{"a": 1, "a": 2, "b": 3}'
    first = editops.locate(dup, ("a",), (0,))
    assert editops.rename_key(dup, first, "c") == b'{"c": 1, "a": 2, "b": 3}'
    with pytest.raises(EditError):
        editops.rename_key(dup, first, "b")
    escaped = b'{"\\u0061": 1}'
    loc = editops.locate(escaped, ("a",))
    assert editops.rename_key(escaped, loc, "a") is escaped
    assert editops.rename_key(escaped, loc, "\ud800") == b'{"\\ud800": 1}'


def test_insert_child_separators_and_levels() -> None:
    lst = editops.locate(PRETTY_RECORD, ("list",))
    out = editops.insert_child(PRETTY_RECORD, lst, 1, b'{"z":0}', PRETTY2, base_level=1)
    assert b'"list": [\n      1,\n      {\n        "z": 0\n      },\n      2\n    ]' in out
    out = editops.insert_child(PRETTY_RECORD, lst, 2, b"3", PRETTY2, base_level=1)
    assert b'"list": [\n      1,\n      2,\n      3\n    ]' in out
    out = editops.insert_child(PRETTY_RECORD, lst, 0, b"0", PRETTY2, base_level=1)
    assert b'"list": [\n      0,\n      1,\n      2\n    ]' in out
    tags = editops.locate(PRETTY_RECORD, ("tags",))
    out = editops.insert_child(PRETTY_RECORD, tags, 2, b'{"k":[1,2]}', PRETTY2, base_level=1)
    assert b'"tags": ["a", "b", {"k": [1, 2]}],' in out
    root = editops.locate(PRETTY_RECORD)
    out = editops.insert_child(PRETTY_RECORD, root, 4, b'"v"', PRETTY2, key="new", base_level=1)
    assert out.endswith(b'      2\n    ],\n    "new": "v"\n  }')
    assert list(json.loads(out)) == ["id", "tags", "m", "list", "new"]


def test_insert_child_one_child_and_empty_containers() -> None:
    one = b'[ \n  1\n]'  # one child: b"," + the exact whitespace between "[" and it
    out = editops.insert_child(one, editops.locate(one), 1, b"2", PRETTY2, base_level=0)
    assert out == b'[ \n  1, \n  2\n]'
    out = editops.insert_child(one, editops.locate(one), 0, b"0", PRETTY2, base_level=0)
    assert out == b'[ \n  0, \n  1\n]'
    one = b'[\n  1\n]'
    assert editops.insert_child(one, editops.locate(one), 1, b"2", PRETTY2, base_level=0) == b'[\n  1,\n  2\n]'
    m = editops.locate(PRETTY_RECORD, ("m",))
    out = editops.insert_child(PRETTY_RECORD, m, 0, b'[1]', PRETTY2, key="k", base_level=1)
    assert b'"m": {\n      "k": [\n        1\n      ]\n    },' in out
    # the review example: an empty record in a pretty array gets indented children
    rec = b"{}"
    out = editops.insert_child(rec, editops.locate(rec), 0, b"1", PRETTY2, key="k", base_level=1)
    assert out == b'{\n    "k": 1\n  }'
    out = editops.insert_child(b"[ ]", editops.locate(b"[ ]"), 0, b'{"a":1}', COMPACT, base_level=0)
    assert out == b'[{"a":1}]'
    # an empty container inside a single-line parent stays on one line
    line = b'{"a": [], "b": 1}'
    out = editops.insert_child(line, editops.locate(line, ("a",)), 0, b'{"x":1}', PRETTY2, base_level=1)
    assert out == b'{"a": [{"x": 1}], "b": 1}'


def test_insert_child_errors_and_verbatim() -> None:
    root = editops.locate(PRETTY_RECORD)
    with pytest.raises(EditError) as ei:
        editops.insert_child(PRETTY_RECORD, root, 0, b"1", PRETTY2, key="id", base_level=1)
    assert ei.value.key == "edits.error.key_exists"
    with pytest.raises(EditError) as ei:
        editops.insert_child(PRETTY_RECORD, root, 0, b"1", PRETTY2, base_level=1)
    assert ei.value.key == "edits.error.key_required"
    for bad in (-1, 5, "1", None):
        with pytest.raises(EditError) as ei:
            editops.insert_child(PRETTY_RECORD, root, bad, b"1", PRETTY2, key="z", base_level=1)
        assert ei.value.key == "edits.error.bad_index" and ei.value.params["count"] == 4
    with pytest.raises(EditError) as ei:
        editops.insert_child(PRETTY_RECORD, editops.locate(PRETTY_RECORD, ("id",)), 0, b"1", PRETTY2, base_level=1)
    assert ei.value.key == "edits.error.not_container"
    lst = editops.locate(PRETTY_RECORD, ("list",))
    raw = b'{\n        "kept":  [1]\n      }'
    out = editops.insert_child(PRETTY_RECORD, lst, 2, raw, PRETTY2, key="ignored", base_level=1, verbatim=True)
    assert raw in out and json.loads(out)["list"] == [1, 2, {"kept": [1]}]


def test_delete_children_runs() -> None:
    arr = b"[0, 1, 2, 3, 4]"
    root = editops.locate(arr)
    cases = {(): arr, (1,): b"[0, 2, 3, 4]", (4,): b"[0, 1, 2, 3]", (0,): b"[1, 2, 3, 4]",
             (1, 2): b"[0, 3, 4]", (3, 4): b"[0, 1, 2]", (0, 4): b"[1, 2, 3]", (0, 2, 4): b"[1, 3]",
             (0, 1, 2, 3): b"[4]", (1, 2, 3, 4): b"[0]", (0, 1, 2, 3, 4): b"[]", (4, 1, 1, 2): b"[0, 3]"}
    for idx, want in cases.items():
        assert editops.delete_children(arr, root, idx) == want, idx
    # the review case: deleting {1, 2} of 3 leaves no dangling comma
    three = b'[\n  {"a": 1},\n  {"b": 2},\n  {"c": 3}\n]'
    assert editops.delete_children(three, editops.locate(three), [1, 2]) == b'[\n  {"a": 1}\n]'
    obj = b'{"a": 1, "b": [2], "c": {"d": 3}}'
    assert editops.delete_children(obj, editops.locate(obj), array("Q", [1])) == b'{"a": 1, "c": {"d": 3}}'
    assert editops.delete_children(obj, editops.locate(obj), [0, 1, 2]) == b"{}"
    pretty = b'{\n  "m": {\n    "x": 1,\n    "y": 2\n  }\n}'
    m = editops.locate(pretty, ("m",))
    assert editops.delete_children(pretty, m, [1]) == b'{\n  "m": {\n    "x": 1\n  }\n}'
    assert editops.delete_children(pretty, m, [0]) == b'{\n  "m": {\n    "y": 2\n  }\n}'
    assert editops.delete_children(pretty, m, [0, 1]) == b'{\n  "m": {}\n}'
    with pytest.raises(EditError) as ei:
        editops.delete_children(arr, root, [5])
    assert ei.value.key == "edits.error.bad_index"


def test_move_child_every_pair() -> None:
    for buf in (b"[0, 1, 2, 3]", b'[\n  0,\n  1 ,\n  2,\t3\n]', b'{"a": 0, "b": 1,\n "c": 2, "d": 3}'):
        root = editops.locate(buf)
        texts = [buf[k.node_start:k.end] for k in editops.child_locs(buf, root)]
        for src in range(4):
            for dst in range(4):
                out = editops.move_child(buf, root, src, dst, PRETTY2, base_level=0)
                order = list(range(4))
                order.insert(dst, order.pop(src))
                new_texts = [out[k.node_start:k.end] for k in editops.child_locs(out, editops.locate(out))]
                assert new_texts == [texts[i] for i in order], (buf, src, dst)
                assert valid(out)
                if src == dst:
                    assert out is buf
    with pytest.raises(EditError):
        editops.move_child(b"[1]", editops.locate(b"[1]"), 0, 1, PRETTY2, base_level=0)


# ----------------------------------------------------------------------------------
# convert_value
# ----------------------------------------------------------------------------------
N = codec.loads


@pytest.mark.parametrize("value, to, want", [
    # -> string
    (N("1.10"), "string", "1.10"), (N("1e400"), "string", "1e400"), (N("-0"), "string", "-0"),
    (N("12345678901234567890"), "string", "12345678901234567890"), (N("-0.0"), "string", "-0.0"),
    (N("0.1"), "string", "0.1"), (True, "string", "true"), (False, "string", "false"), (None, "string", ""),
    ({"a": [1, N("1.10")], "b": "Тошкент"}, "string", '{"a":[1,1.10],"b":"Тошкент"}'), ([], "string", "[]"),
    # -> number
    (" 1.10 ", "number", N("1.10")), ("1e400", "number", N("1e400")), ("-0", "number", N("-0")),
    ("12345678901234567890", "number", N("12345678901234567890")), ("\t0.1\n", "number", 0.1),
    (True, "number", 1), (False, "number", 0), (None, "number", 0),
    # -> bool
    ("true", "bool", True), (" TRUE ", "bool", True), ("1", "bool", True), ("Yes", "bool", True),
    ("ha", "bool", True), ("HA", "bool", True), ("ҳа", "bool", True), ("false", "bool", False), ("0", "bool", False),
    ("no", "bool", False), ("yo'q", "bool", False), ("yo‘q", "bool", False), ("yoʻq", "bool", False),
    ("yo’q", "bool", False), ("YOʼQ", "bool", False), ("yo`q", "bool", False), ("йўқ", "bool", False),
    (0, "bool", False), (N("-0"), "bool", False), (N("-0.0"), "bool", False), (N("0e5"), "bool", False),
    (N("1e-400"), "bool", True), (N("1e400"), "bool", True), (N("0.1"), "bool", True), (-3, "bool", True),
    (None, "bool", False),
    # -> null
    ("x", "null", None), (5, "null", None), (True, "null", None), ({"a": 1}, "null", None), ([1], "null", None),
    # -> object
    ('{"a": 1.10, "a": 2}', "object", {"a": 2}), ("", "object", {}), ("  ", "object", {}), (None, "object", {}),
    # -> array
    ('[1, "x", 1e400]', "array", [1, "x", N("1e400")]), ("", "array", []), ("abc", "array", ["abc"]),
    ("[1, 2", "array", ["[1, 2"]), ('{"a": 1}', "array", ['{"a": 1}']), (N("1.10"), "array", [N("1.10")]),
    (False, "array", [False]), (None, "array", []), ({"o": 1}, "array", [{"o": 1}]),
])
def test_convert_value_table(value, to, want) -> None:
    got = editops.convert_value(value, to)
    assert codec.json_type(got) == to
    assert codec.dumps(got) == codec.dumps(want)


@pytest.mark.parametrize("value", ["x", 5, True, None, {"a": 1}, [1], N("1.10")])
def test_convert_value_same_type_is_unchanged(value) -> None:
    assert editops.convert_value(value, codec.json_type(value)) is value


@pytest.mark.parametrize("value, to", [
    ("abc", "number"), ("1,5", "number"), ("+5", "number"), ("01", "number"), ("1 000", "number"), ("", "number"),
    ("１２", "number"), ({"a": 1}, "number"), ([1], "number"),
    ("maybe", "bool"), ("", "bool"), ("yo q", "bool"), ({}, "bool"), ([], "bool"),
    ("[1]", "object"), ("abc", "object"), ('{"a": ', "object"), (5, "object"), (True, "object"), ([1], "object"),
])
def test_convert_value_failures(value, to, lang_uz) -> None:
    with pytest.raises(ConversionError) as ei:
        editops.convert_value(value, to)
    exc = ei.value
    assert isinstance(exc, EditError) and isinstance(exc, JsonReshapeError)
    assert exc.key == "edits.error.convert"
    assert exc.params["source"] == codec.json_type(value) and exc.params["target"] == to
    assert t(f"edits.type.{to}") in exc.message
    assert not any(f"{{{name}}}" in exc.message for name in ("value", "source", "target"))
    assert exc.hint


def test_conversion_error_localizes_type_names_lazily(lang_uz) -> None:
    exc = ConversionError(value='"abc"', source="string", target="number")
    assert exc.message == t("edits.error.convert", value='"abc"', source="satr", target="son")
    from jsonreshape.i18n import set_language
    set_language("en")
    assert exc.message == 'The string value "abc" cannot be converted to number.'
    assert ConversionError(value="x", source="custom", target="z").message.count("custom") == 1


def test_convert_value_bad_target_and_preview() -> None:
    with pytest.raises(ValueError):
        editops.convert_value(1, "integer")
    with pytest.raises(ConversionError) as ei:
        editops.convert_value("x" * 500, "number")
    assert len(ei.value.params["value"]) == 60


# ----------------------------------------------------------------------------------
# errors and i18n
# ----------------------------------------------------------------------------------
def test_edit_error_attributes() -> None:
    exc = EditError("edits.error.invalid_json", line=2, column=3, reason="r", char_offset=7, offset=9)
    assert (exc.line, exc.column, exc.char_offset, exc.offset) == (2, 3, 7, 9)
    plain = EditError()
    assert plain.key == "edits.error.generic"
    assert (plain.line, plain.column, plain.char_offset, plain.offset) == (None, None, None, None)
    assert isinstance(ConversionError(), EditError) and ConversionError().key == "edits.error.convert"


SPEC_KEYS = (
    [f"edits.error.{k}" for k in ("generic", "path_not_found", "key_exists", "invalid_json", "internal", "convert",
                                   "huge_record", "container_locked", "single_root", "pending", "stale_refs",
                                   "stale_scope", "too_many", "not_object", "move_into_self", "closed", "dirty",
                                   "layer_corrupt", "locked", "target_changed", "not_member", "not_container",
                                   "bad_index", "key_required")]
    + [f"edits.step.{k}" for k in ("set_value", "rename_key", "insert", "insert_record", "delete", "delete_records",
                                    "duplicate", "duplicate_records", "move", "move_records", "change_type", "paste",
                                    "patch", "restore")]
    + [f"edits.step.bulk.{k}" for k in ("replace", "rename_key", "delete_field", "set_field", "clean_strings",
                                         "convert_type", "dedupe")]
    + [f"edits.type.{k}" for k in editops.JSON_TYPES]
    + ["edits.reason.empty", "edits.reason.unexpected_eof", "edits.reason.expected_end", "edits.detail.shape"]
)


@pytest.mark.parametrize("key", SPEC_KEYS)
def test_edits_keys_exist(key) -> None:
    assert has(key)
    from jsonreshape.i18n import MESSAGES
    assert MESSAGES[key]["uz"].strip() and MESSAGES[key]["en"].strip()


def test_step_labels_take_count(lang_en) -> None:
    assert t("edits.step.delete_records", count=3) == "Records deleted: 3"
    assert t("edits.step.set_value") == "Value changed"
    assert "{" not in t("edits.step.bulk.dedupe", count=2)
