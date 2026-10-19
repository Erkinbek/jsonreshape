"""jsonreshape.repair (docs/ARCHITECTURE.md §14.5, AGENT_PROMPT §10 "Ta'mirlash", §14 test 23).

Every fix code of the catalogue with EXACT output bytes and EXACT report entries (code, user offset, 1-based
line / column, output offset, record, spans, texts); the edge cases of s5/map_repair_diff.md §3.9 (BOMs,
comments everywhere, trailing commas, quotes, NaN/Infinity, brace lists, concatenation, truncation of every
kind, extensions, NUL padding); the unrepairable cases with and without ``allow_partial``; the report API
(counts, summary texts in both languages, ``write`` in every format, the temporary fix list); encodings
(UTF-8 BOM, UTF-16, gzip: user-file offsets equal the reader's error offsets); options, cancellation,
progress and the source identity check. Every repair runs with the accelerator on and off and the results
must be identical (``repair_helpers.repair_bytes``).
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

from jsonreshape import repair
from jsonreshape.errors import Cancelled, FileChangedError, UserInputError
from jsonreshape.i18n import t
from jsonreshape.reader import Document
from jsonreshape.repair import FIX_CATEGORY, FIX_CODES, PROBLEM_CODES, RepairOptions, UnrepairableError, repair_file
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from repair_helpers import fix_tuple, repair_bytes, short, strict_records


def run(tmp_path: Path, data: bytes, name: str = "in.json", **opts):
    return repair_bytes(tmp_path, data, name, windows=(None, 1, 7), **opts)


# ----------------------------------------------------------------------------------------------------
# the catalogue: exact output and exact report entries
# ----------------------------------------------------------------------------------------------------
def test_valid_json_is_copied_unchanged(tmp_path: Path) -> None:
    data = b'[\n  {"a": 1, "b": [1.10, 1e400, -0.0, 12345678901234567890]},\n  {"c": "\xd1\x82\xd0\xb5\xd1\x81\xd1\x82 \\u0442"}\n]\n'
    out, rep = run(tmp_path, data)
    assert out == data
    assert rep.total == 0 and rep.fixes == [] and rep.counts == {} and rep.records == 2
    assert rep.kind == "array" and rep.output_kind == "array" and rep.written and rep.problem is None
    assert not rep.changed_data()


def test_bom_at_start(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b"\xef\xbb\xbf[1]")
    assert out == b"[1]"
    assert [short(f) for f in rep.fixes] == [("bom", 0, 1, 1, 0, None)]
    fx = rep.fixes[0]
    assert (fx.length, fx.out_length, fx.before, fx.after, fx.category) == (3, 0, "\\ufeff", "", "syntax")


def test_bom_between_values(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{"a":1}\xef\xbb\xbf{"a":2}')
    assert out == b'[{"a":1},{"a":2}]'
    assert rep.kind == "stream" and rep.output_kind == "array" and rep.records == 2
    assert [short(f) for f in rep.fixes] == [("concatenated", 0, 1, 1, 0, None), ("bom", 7, 1, 8, 9, None)]


def test_bom_between_tokens(tmp_path: Path) -> None:
    # decision 8: a BOM is removed wherever whitespace may stand
    out, rep = run(tmp_path, b'[1,\xef\xbb\xbf2, {"a":\xef\xbb\xbf 3}]')
    assert out == b'[1,2, {"a": 3}]'
    # the first BOM counts as one character of line 1 (the reader's column rule)
    assert [short(f) for f in rep.fixes] == [("bom", 3, 1, 4, 3, None), ("bom", 14, 1, 13, 11, 2)]
    assert all((f.length, f.out_length, f.before, f.after) == (3, 0, "\\ufeff", "") for f in rep.fixes)


def test_bom_at_jsonl_line_start(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{"a":1}\n\xef\xbb\xbf{"a":2}\n', name="in.jsonl")
    assert out == b'{"a":1}\n{"a":2}\n'
    assert rep.kind == "jsonl" and rep.records == 2
    assert [short(f) for f in rep.fixes] == [("bom", 8, 2, 1, 8, None)]


@pytest.mark.parametrize("data, expected, fixes", [
    # at the top, before the root
    (b"// top\n[1, 2]", b"\n[1, 2]", [("comment", 0, 1, 1, 0, None)]),
    # between records (after the comma)
    (b"[1, // c\n 2]", b"[1, \n 2]", [("comment", 4, 1, 5, 4, None)]),
    # inside a record
    (b'[{"a": 1, /* x */ "b": 2}]', b'[{"a": 1,  "b": 2}]', [("comment", 10, 1, 11, 10, 0)]),
    # after the root, at the end without a line feed
    (b"[1, 2] // end", b"[1, 2] ", [("comment", 7, 1, 8, 7, None)]),
    (b"[1, 2] /* end", b"[1, 2] ", [("comment", 7, 1, 8, 7, None)]),
    # a // comment keeps the CR LF that ends it
    (b"[1, // c\r\n 2]", b"[1, \r\n 2]", [("comment", 4, 1, 5, 4, None)]),
])
def test_comments(tmp_path: Path, data: bytes, expected: bytes, fixes: list) -> None:
    out, rep = run(tmp_path, data)
    assert out == expected
    assert [short(f) for f in rep.fixes] == fixes
    assert rep.kind == "array"


def test_block_comment_keeps_line_numbers(tmp_path: Path) -> None:
    data = b"[\n/* a\nb\nc */ 1,\n NaN]"
    out, rep = run(tmp_path, data)
    assert out == b"[\n\n\n 1,\n null]"
    assert [short(f) for f in rep.fixes] == [("comment", 2, 2, 1, 2, None), ("nan", 18, 5, 2, 9, 1)]
    c = rep.fixes[0]
    assert (c.length, c.out_length, c.before, c.after) == (11, 2, "/* a\\nb\\nc */", "\\n\\n")
    # the NaN is on line 5 of the output too
    assert out[:9].count(b"\n") == 4


@pytest.mark.parametrize("data, expected, fixes", [
    (b"[1, 2,]", b"[1, 2]", [("trailing_comma", 5, 1, 6, 5, None)]),
    (b'{"a": 1,}', b'{"a": 1}', [("trailing_comma", 7, 1, 8, 7, 0)]),
    (b'[{"a": [1, 2,],}, 3,]', b'[{"a": [1, 2]}, 3]',
     [("trailing_comma", 12, 1, 13, 12, 0), ("trailing_comma", 14, 1, 15, 13, 0),
      ("trailing_comma", 19, 1, 20, 17, None)]),
    # a comment between the comma and the closer
    (b"[1, 2, // c\n]", b"[1, 2 \n]", [("trailing_comma", 5, 1, 6, 5, None), ("comment", 7, 1, 8, 6, None)]),
    (b"[1, 2 , ]", b"[1, 2  ]", [("trailing_comma", 6, 1, 7, 6, None)]),
])
def test_trailing_commas(tmp_path: Path, data: bytes, expected: bytes, fixes: list) -> None:
    out, rep = run(tmp_path, data)
    assert out == expected
    assert [short(f) for f in rep.fixes] == fixes
    assert all((f.length, f.out_length, f.before, f.after) == (1, 0, ",", "")
               for f in rep.fixes if f.code == "trailing_comma")


@pytest.mark.parametrize("data, expected, fixes", [
    (b'[{"a":1}{"a":2}]', b'[{"a":1},{"a":2}]', [("missing_comma", 8, 1, 9, 8, None)]),
    (b"[1 2]", b"[1, 2]", [("missing_comma", 2, 1, 3, 2, None)]),
    (b'["a""b"]', b'["a","b"]', [("missing_comma", 4, 1, 5, 4, None)]),
    (b'[{"a":1}\n{"a":2}]', b'[{"a":1},\n{"a":2}]', [("missing_comma", 8, 1, 9, 8, None)]),
    (b'{"a":1 "b":2}', b'{"a":1, "b":2}', [("missing_comma", 6, 1, 7, 6, 0)]),
])
def test_missing_commas(tmp_path: Path, data: bytes, expected: bytes, fixes: list) -> None:
    out, rep = run(tmp_path, data)
    assert out == expected
    assert [short(f) for f in rep.fixes] == fixes
    fx = rep.fixes[0]
    assert (fx.length, fx.out_length, fx.before, fx.after, fx.category) == (0, 1, "", ",", "syntax")


def test_single_quotes_value_and_key(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b"[{'k': 'it\\'s \"x\"'}]")
    assert out == b'[{"k": "it\'s \\"x\\""}]'
    assert [short(f) for f in rep.fixes] == [("single_quote", 2, 1, 3, 2, 0), ("single_quote", 7, 1, 8, 7, 0)]
    key, val = rep.fixes
    assert (key.length, key.out_length, key.before, key.after) == (3, 3, "'k'", '"k"')
    assert (val.length, val.out_length, val.before, val.after) == (11, 12, "'it\\'s \"x\"'", '"it\'s \\"x\\""')


def test_single_quoted_string_with_inner_fixes(tmp_path: Path) -> None:
    # the single_quote entry comes first, the fixes inside the string follow it
    out, rep = run(tmp_path, b"['a\tb\\x41']")
    assert out == b'["a\\tb\\u0041"]'
    assert [short(f) for f in rep.fixes] == [("single_quote", 1, 1, 2, 1, 0), ("control_char", 3, 1, 4, 3, 0),
                                             ("invalid_escape", 5, 1, 6, 6, 0)]


def test_many_fixes_inside_one_single_quoted_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the fixes inside a single-quoted string wait for the string's own entry; beyond DEFER_MAX rows they
    # wait in a temporary file (bounded memory): same output, same fix list, no file left
    body = b"a\\x41\tb" * 40
    data = b"['" + body + b"', 'x\\d', NaN]"
    out1, rep1 = run(tmp_path, data)
    assert out1 == b'["' + b"a\\u0041\\tb" * 40 + b'", "x\\\\d", null]'
    assert rep1.counts == {"single_quote": 2, "invalid_escape": 41, "control_char": 40, "nan": 1}
    codes = [f.code for f in rep1.fixes]
    assert codes[0] == "single_quote" and codes[1:3] == ["invalid_escape", "control_char"]
    assert codes[81:] == ["single_quote", "invalid_escape", "nan"]
    assert [f.offset for f in rep1.fixes] == sorted(f.offset for f in rep1.fixes)
    monkeypatch.setattr(repair, "DEFER_MAX", 3)
    out2, rep2 = run(tmp_path, data)
    assert out2 == out1 and [short(f) for f in rep2.fixes] == [short(f) for f in rep1.fixes]
    assert [(f.before, f.after) for f in rep2.fixes] == [(f.before, f.after) for f in rep1.fixes]
    # cut inside the string: the held-back rows are dropped with the partial record
    out3, rep3 = run(tmp_path, data[:150])
    assert out3 == b"[]" and rep3.counts == {"truncated": 1}
    assert not list(repair._work_dir(Settings()).glob("repair-defer-*"))


@pytest.mark.parametrize("nan, expected, outs", [
    ("null", b"[null, null, null, null, null, null]", [1, 7, 13, 19, 25, 31]),
    ("string", b'["NaN", "Infinity", "-Infinity", "Infinity", "NaN", "NaN"]', [1, 8, 20, 33, 45, 52]),
])
def test_nan_and_infinity(tmp_path: Path, nan: str, expected: bytes, outs: list) -> None:
    data = b"[NaN, Infinity, -Infinity, +Infinity, -NaN, +NaN]"
    out, rep = run(tmp_path, data, nan=nan)
    assert out == expected
    assert [(f.code, f.offset, f.out_offset, f.record) for f in rep.fixes] == [
        ("nan", 1, outs[0], 0), ("infinity", 6, outs[1], 1), ("infinity", 16, outs[2], 2),
        ("infinity", 27, outs[3], 3), ("nan", 38, outs[4], 4), ("nan", 44, outs[5], 5)]
    assert [f.before for f in rep.fixes] == ["NaN", "Infinity", "-Infinity", "+Infinity", "-NaN", "+NaN"]
    assert all(f.category == "data" for f in rep.fixes)
    assert rep.changed_data()
    assert rep.fixes[0].name == ("NaN → null" if nan == "null" else 'NaN → "NaN"')


@pytest.mark.parametrize("data, kind, expected, records", [
    (b'{ {"a":1},{"a":2} }', "brace_list", b'[ {"a":1},{"a":2} ]', 2),
    (b'{ "a", "b" }', "brace_list", b'[ "a", "b" ]', 2),
    (b'{1, 2, [3]}', "brace_list", b'[1, 2, [3]]', 3),
])
def test_brace_lists(tmp_path: Path, data: bytes, kind: str, expected: bytes, records: int) -> None:
    out, rep = run(tmp_path, data)
    assert out == expected
    assert rep.kind == kind and rep.output_kind == "array" and rep.records == records
    assert [short(f) for f in rep.fixes] == [("brace_list", 0, 1, 1, 0, None)]
    fx = rep.fixes[0]
    assert (fx.length, fx.out_length, fx.before, fx.after) == (1, 1, "{", "[")
    assert strict_records(out, repair.MODE_CONTAINER) == records


def test_empty_object_is_not_a_brace_list(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b"{ }")
    assert out == b"{ }" and rep.total == 0 and rep.kind == "single"
    out, rep = run(tmp_path, b"{}", format="brace_list")
    assert out == b"[]" and [f.code for f in rep.fixes] == ["brace_list"] and rep.records == 0


def test_brace_list_with_comment_before_the_first_value(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{ // list\n {"a":1}, {"a":2}, }')
    assert out == b'[ \n {"a":1}, {"a":2} ]'
    assert rep.kind == "brace_list"
    assert [short(f) for f in rep.fixes] == [("brace_list", 0, 1, 1, 0, None), ("comment", 2, 1, 3, 2, None),
                                             ("trailing_comma", 27, 2, 18, 20, None)]


def test_concatenated_values_become_an_array(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{"a":1}{"a":2}')
    assert out == b'[{"a":1},{"a":2}]'
    assert rep.kind == "stream" and rep.output_kind == "array" and rep.records == 2
    assert [short(f) for f in rep.fixes] == [("concatenated", 0, 1, 1, 0, None)]
    fx = rep.fixes[0]
    assert (fx.length, fx.out_length, fx.before, fx.after) == (0, 1, "", "[")


def test_concatenated_values_keep_their_layout(tmp_path: Path) -> None:
    data = b'\n{"a": 1}\n\n{"b": 2} // two\n{"c": 3},\n'
    out, rep = run(tmp_path, data, format="stream")
    assert out == b'\n[{"a": 1},\n\n{"b": 2}, \n{"c": 3}]\n'
    assert [short(f) for f in rep.fixes] == [("concatenated", 1, 2, 1, 1, None), ("comment", 20, 4, 10, 23, None),
                                             ("trailing_comma", 35, 5, 9, 33, None)]
    assert strict_records(out, repair.MODE_CONTAINER) == 3


def test_concatenated_values_as_json_lines(tmp_path: Path) -> None:
    data = b'{"a": 1,\n "b": [1,\n 2]} {"c": 3} // x\n{"d": NaN}'
    out, rep = run(tmp_path, data, output_kind="jsonl", out_name="out.jsonl")
    assert out == b'{"a": 1,  "b": [1,  2]}\n{"c": 3}\n{"d": null}\n'
    assert rep.kind == "stream" and rep.output_kind == "jsonl" and rep.records == 3
    assert [(f.code, f.offset, f.out_offset, f.out_length, f.record) for f in rep.fixes] == [
        ("concatenated", 0, 0, 0, None), ("comment", 33, 33, 0, None), ("nan", 44, 39, 4, 2)]
    assert rep.fixes[0].after == "JSONL" and rep.fixes[0].name == t("repair.fix.concatenated_jsonl")
    assert strict_records(out, repair.MODE_LINES) == 3


def test_json_lines_stay_json_lines(tmp_path: Path) -> None:
    data = b'{"a":1}\n{"a":2}\n'
    out, rep = run(tmp_path, data)
    assert out == data and rep.kind == "jsonl" and rep.total == 0 and rep.records == 2


def test_missing_comma_inside_array_of_objects(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'[{"a":1}{"a":2}{"a":3}]')
    assert out == b'[{"a":1},{"a":2},{"a":3}]'
    assert [f.offset for f in rep.fixes] == [8, 15] and rep.records == 3


# ----------------------------------------------------------------------------------------------------
# truncation: every kind
# ----------------------------------------------------------------------------------------------------
def _trunc(rep):
    tr = rep.truncated
    return tr.offset, tr.line, tr.column, tr.kept_records, tr.dropped_bytes, tr.partial_start, tr.closers


@pytest.mark.parametrize("data, opts, expected, trunc", [
    # array: cut inside a string, after a comma, inside a number (the reader's cut-number rule)
    (b'[{"a": 1}, {"b": "xy', {}, b'[{"a": 1}]', (9, 1, 10, 1, 11, 11, "]")),
    (b"[1, 2, ", {}, b"[1, 2]", (5, 1, 6, 2, 2, None, "]")),
    (b"[1, 2, 12", {}, b"[1, 2]", (5, 1, 6, 2, 4, 7, "]")),
    (b"[1, 2, true", {}, b"[1, 2, true]", (11, 1, 12, 3, 0, None, "]")),
    (b"[[1,2],[3,4],[5,", {}, b"[[1,2],[3,4]]", (12, 1, 13, 2, 4, 13, "]")),
    (b'[\n  {"a": 1},\n  {"a', {}, b'[\n  {"a": 1}]', (12, 2, 11, 1, 7, 16, "]")),
    # nothing complete inside the root array: the container stays
    (b'[{"a": 1', {}, b"[]", (1, 1, 2, 0, 7, 1, "]")),
    # wrapped: cut inside the records, in the prefix, in the suffix (decision 2)
    (b'{"data": [{"a": 1}, {"a": 2}, {"a": 3', {}, b'{"data": [{"a": 1}, {"a": 2}]}', (28, 1, 29, 2, 9, 30, "]}")),
    (b'{"meta": {"x": 1}, "dat', {"record_path": "data"}, b'{"meta": {"x": 1}}', (17, 1, 18, 0, 6, 19, "}")),
    (b'{"data": [{"a": 1}], "tail": {"y', {"record_path": "data"}, b'{"data": [{"a": 1}]}',
     (19, 1, 20, 1, 13, 21, "}")),
    # keyed
    (b'{"k1": {"a": 1}, "k2": {"a": 2}, "k3": {"a', {}, b'{"k1": {"a": 1}, "k2": {"a": 2}}',
     (31, 1, 32, 2, 11, 33, "}")),
    # a single object: its complete members stay
    (b'{"a": 1, "b": {"c": 2', {}, b'{"a": 1}', (7, 1, 8, 1, 14, 9, "}")),
    # ... but an array open at the end is a record array for the reader (wrapped, rule 5a)
    (b'{"a": 1, "b": [1, 2', {}, b'{"a": 1, "b": [1]}', (16, 1, 17, 1, 3, 18, "]}")),
    # a brace list
    (b'{ {"a":1}, {"a"', {}, b'[ {"a":1}]', (9, 1, 10, 1, 6, 11, "]")),
])
def test_truncation(tmp_path: Path, data: bytes, opts: dict, expected: bytes, trunc: tuple) -> None:
    out, rep = run(tmp_path, data, **opts)
    assert out == expected
    assert rep.truncated is not None and _trunc(rep) == trunc
    t_fix = [f for f in rep.fixes if f.code == "truncated"]
    assert len(t_fix) == 1
    fx = t_fix[0]
    assert (fx.offset, fx.line, fx.column, fx.record) == trunc[:3] + (None,)
    assert fx.length == trunc[4] and fx.out_length == len(trunc[6]) and fx.after == trunc[6]
    assert fx.category == "dropped" and rep.changed_data()
    assert rep.records == trunc[3]


def test_truncated_wrapped_validates_with_the_record_path(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{"result": {"count": 5, "items": [{"a": 1}, {"a": 2}], "more": tr',
                   record_path="result.items")
    assert out == b'{"result": {"count": 5, "items": [{"a": 1}, {"a": 2}]}}'
    assert rep.kind == "wrapped" and rep.records == 2 and rep.truncated.closers == "}}"
    assert strict_records(out, repair.MODE_CONTAINER, ["result", "items"]) == 2
    assert rep.reopen_options() == {"format": "wrapped", "record_path": ("result", "items")}


def test_truncated_scalar_root_is_unrepairable(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'"abc')
    assert out is None and rep.problem is not None and rep.problem.code == "nothing_complete"
    assert (rep.problem.offset, rep.problem.line, rep.problem.column) == (0, 1, 1)
    # a top-level number at the end of a one-value document is complete
    out, rep = run(tmp_path, b"12")
    assert out == b"12" and rep.total == 0


@pytest.mark.parametrize("output_kind, expected, closers", [
    (None, b'[{"a": 1}, {"b": [2]}]', "]"),
    ("jsonl", b'{"a": 1}\n{"b": [2]}\n', ""),
])
def test_truncated_stream(tmp_path: Path, output_kind, expected: bytes, closers: str) -> None:
    data = b'{"a": 1} {"b": [2]}\n{"c": [3, 4'
    opts = {"output_kind": output_kind} if output_kind else {}
    out, rep = run(tmp_path, data, format="stream", out_name="out.jsonl" if output_kind else "out.json", **opts)
    assert out == expected
    assert rep.kind == "stream" and rep.records == 2
    assert _trunc(rep) == (19, 1, 20, 2, 12, 20, closers)
    assert [f.code for f in rep.fixes] == ["concatenated", "truncated"]
    strict_records(out, repair.MODE_LINES if output_kind else repair.MODE_CONTAINER)


def test_truncated_json_lines_last_line_is_dropped(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{"a":1}\n{"a":2}\n{"a":', name="in.jsonl")
    assert out == b'{"a":1}\n{"a":2}\n'
    assert [short(f) for f in rep.fixes] == [("line_dropped", 16, 3, 1, 16, None)]
    fx = rep.fixes[0]
    assert (fx.length, fx.out_length, fx.reason, fx.before) == (5, 0, "truncated", '{"a":')
    assert rep.truncated is None and rep.records == 2


def test_truncation_and_nul_padding(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b"[1, 2, 3\x00\x00\x00")
    assert out == b"[1, 2]"
    assert [(f.code, f.offset, f.length, f.out_offset) for f in rep.fixes] == [
        ("truncated", 5, 3, 5), ("trailing_data", 8, 3, 6)]


# ----------------------------------------------------------------------------------------------------
# extensions
# ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("data, expected, fixes", [
    (b"[True, False, None]", b"[true, false, null]",
     [("python_literal", 1, 1, 2, 1, 0), ("python_literal", 7, 1, 8, 7, 1), ("python_literal", 14, 1, 15, 14, 2)]),
    (b"[undefined]", b"[null]", [("undefined", 1, 1, 2, 1, 0)]),
    (b"{a: 1, $b_2: 2, \xd1\x88\xd0\xb0\xd1\x85\xd0\xb0\xd1\x80: 3}",
     b'{"a": 1, "$b_2": 2, "\xd1\x88\xd0\xb0\xd1\x85\xd0\xb0\xd1\x80": 3}',
     [("unquoted_key", 1, 1, 2, 1, 0), ("unquoted_key", 7, 1, 8, 9, 0), ("unquoted_key", 16, 1, 17, 20, 0)]),
    (b'["a\tb", "c\nd", "\x01"]', b'["a\\tb", "c\\nd", "\\u0001"]',
     [("control_char", 3, 1, 4, 3, 0), ("control_char", 10, 1, 11, 11, 1), ("control_char", 16, 2, 6, 18, 2)]),
    (b'["\\x41", "\\d", "\\\'", "\\u12G4"]', b'["\\u0041", "\\\\d", "\'", "\\\\u12G4"]',
     [("invalid_escape", 2, 1, 3, 2, 0), ("invalid_escape", 10, 1, 11, 12, 1), ("invalid_escape", 16, 1, 17, 19, 2),
      ("invalid_escape", 22, 1, 23, 24, 3)]),
    (b'["\xff\xfeab", "\xe2\x82x", "\xed\xa0\x80"]',
     b'["\xef\xbf\xbd\xef\xbf\xbdab", "\xef\xbf\xbdx", "\xef\xbf\xbd\xef\xbf\xbd\xef\xbf\xbd"]',
     [("invalid_utf8", 2, 1, 3, 2, 0), ("invalid_utf8", 3, 1, 4, 5, 0), ("invalid_utf8", 10, 1, 11, 14, 1),
      # columns count non-continuation bytes (the reader's rule): 0x82 / 0xa0 are not counted
      ("invalid_utf8", 17, 1, 17, 22, 2), ("invalid_utf8", 18, 1, 18, 25, 2), ("invalid_utf8", 19, 1, 18, 28, 2)]),
    (b"[1, 2]]]", b"[1, 2]", [("trailing_data", 6, 1, 7, 6, None)]),
    (b"[1, 2] xyz \n", b"[1, 2] ", [("trailing_data", 7, 1, 8, 7, None)]),
    (b"[1,2]\x00\x00\x00", b"[1,2]", [("trailing_data", 5, 1, 6, 5, None)]),
    (b"[1,2]\n\x00\x00 \x00", b"[1,2]\n", [("trailing_data", 6, 2, 1, 6, None)]),
])
def test_extensions(tmp_path: Path, data: bytes, expected: bytes, fixes: list) -> None:
    out, rep = run(tmp_path, data)
    assert out == expected
    assert [short(f) for f in rep.fixes] == fixes
    for f in rep.fixes:
        assert f.category == FIX_CATEGORY[f.code]


def test_invalid_escape_and_utf8_texts(tmp_path: Path) -> None:
    _out, rep = run(tmp_path, b'["\\x41\\d", "\xff"]')
    assert [(f.before, f.after, f.length, f.out_length) for f in rep.fixes] == [
        ("\\x41", "\\u0041", 4, 6), ("\\d", "\\\\d", 1, 2), ("\\xff", "\ufffd", 1, 3)]


def test_invalid_utf8_guard(tmp_path: Path) -> None:
    data = b'["' + b"\xff" * (repair.UTF8_LIMIT + 1) + b'"]'
    out, rep = run(tmp_path, data)
    assert out is None and rep.problem.code == "not_utf8" and rep.problem.offset == 2 + repair.UTF8_LIMIT
    assert "1000" in rep.problem.reason
    data = b'["' + b"\xff" * repair.UTF8_LIMIT + b'"]'
    out, rep = run(tmp_path, data)
    assert out is not None and rep.counts == {"invalid_utf8": repair.UTF8_LIMIT}


def test_json_lines_line_split_and_commas(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{"a":1}{"a":2}\n{"a":3},{"a":4}\n{"a":5},\n', name="in.jsonl")
    assert out == b'{"a":1}\n{"a":2}\n{"a":3}\n{"a":4}\n{"a":5}\n'
    assert rep.records == 5
    assert [(f.code, f.offset, f.out_offset, f.length, f.out_length, f.record) for f in rep.fixes] == [
        ("line_split", 7, 7, 0, 1, None), ("line_split", 22, 23, 1, 1, None), ("trailing_comma", 38, 39, 1, 0, None)]


def test_json_lines_broken_lines(tmp_path: Path) -> None:
    data = b'{"a":1}\n{"a": hello}\n{"a":3,}\n  \n{"a":4} junk\n'
    out, rep = run(tmp_path, data, name="in.jsonl")
    assert out == b'{"a":1}\n{"a":3}\n  \n{"a":4} \n'
    assert [(f.code, f.offset, f.line, f.column, f.out_offset, f.reason) for f in rep.fixes] == [
        ("line_dropped", 8, 2, 1, 8, "unquoted_value"), ("trailing_comma", 27, 3, 7, 14, ""),
        ("trailing_data", 41, 5, 9, 27, "")]
    assert rep.records == 3
    assert rep.fixes[0].before == '{"a": hello}' and rep.fixes[0].length == 13


# ----------------------------------------------------------------------------------------------------
# unrepairable problems
# ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("data, code, offset, expected, partial", [
    (b"[1, 2}", "mismatched_closer", 5, "", b"[1, 2]"),
    (b"[1, {\"a\": 1]]", "mismatched_closer", 11, "", b"[1]"),
    (b"[01]", "invalid_number", 1, "", b"[]"),
    (b"[1, 0x1F]", "invalid_number", 4, "", b"[1]"),
    (b"[1., 2]", "invalid_number", 1, "", b"[]"),
    (b"[+5]", "invalid_number", 1, "", b"[]"),
    (b"[1, .5]", "unexpected_char", 4, "value", b"[1]"),
    (b'{"a": hello}', "unquoted_value", 6, "", b"{}"),
    (b"[1, ObjectId(1)]", "unquoted_value", 4, "", b"[1]"),
    (b"[1,,2]", "unexpected_char", 3, "value", b"[1]"),
    (b'{"a" 1}', "unexpected_char", 5, "colon", b"{}"),
    (b'[1-2]', "invalid_number", 1, "", b"[]"),
    (b"[truefalse]", "unquoted_value", 1, "", b"[]"),
])
def test_unrepairable(tmp_path: Path, data: bytes, code: str, offset: int, expected: str, partial: bytes) -> None:
    out, rep = run(tmp_path, data)
    assert out is None and not rep.written
    p = rep.problem
    assert (p.code, p.offset, p.line, p.column, p.expected) == (code, offset, 1, offset + 1, expected)
    assert p.context_before == data[:offset].decode() and p.context_after == data[offset:].decode()
    # with allow_partial the records before the problem are written, closed like a truncation
    out, rep = run(tmp_path, data, allow_partial=True)
    assert out == partial and rep.written and rep.problem.code == code
    assert rep.truncated is not None and rep.truncated.closers == partial[-1:].decode()
    assert not any(f.code == "truncated" for f in rep.fixes)


def test_unrepairable_error_message_and_report(tmp_path: Path, lang_en) -> None:
    src = tmp_path / "bad.json"
    src.write_bytes(b'[1,\n {"a": hello}]')
    dst = tmp_path / "out.json"
    with pytest.raises(UnrepairableError) as ei:
        repair_file(src, dst)
    err = ei.value
    assert not dst.exists()
    assert err.report.problem.code == "unquoted_value" and (err.report.problem.line, err.report.problem.column) == (2, 8)
    assert "line 2, column 8" in str(err) and "Nothing was written" in str(err)
    assert err.hint and "partial" in err.hint
    assert err.report.summary_lines()[-2].startswith("Unrepairable:")
    assert err.report.problem.context().splitlines() == ['[1,  {"a": hello}]', " " * 11 + "^"]
    from jsonreshape.i18n import set_language
    set_language("uz")
    assert "2-qator, 8-ustun" in str(err)


def test_hash_comment_and_bad_bytes_are_unrepairable(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b"# comment\n[1]", format="array")
    assert out is None and rep.problem.code == "unexpected_char" and rep.problem.offset == 0
    out, rep = run(tmp_path, b"[1, \x80]")
    assert out is None and rep.problem.code == "unexpected_char" and rep.problem.offset == 4


def test_depth_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair, "MAX_DEPTH", 50)
    data = b"[" * 60 + b"]" * 60
    out, rep = run(tmp_path, data, format="array")
    assert out is None and rep.problem.code == "depth_limit" and rep.problem.offset == 50


def test_forced_kind_with_another_value_is_a_problem(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{"a": 1}\n{"b": 2}\n', format="single", allow_partial=True)
    assert out == b'{"a": 1}' and rep.problem.code == "extra_value" and rep.problem.line == 2
    assert rep.truncated.closers == ""


def test_disabled_fix_stops(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b"[1, NaN, 2,]", fixes=frozenset({"trailing_comma"}))
    assert out is None and rep.problem.code == "disabled" and rep.problem.detail == "nan"
    assert rep.problem.offset == 4
    assert fix_name_in(rep.problem.reason, "nan")
    out, rep = run(tmp_path, b"[1, 2,]", fixes=frozenset({"trailing_comma"}))
    assert out == b"[1, 2]"
    out, rep = run(tmp_path, b"[1, 2", fixes=frozenset({"nan"}))
    assert out is None and rep.problem.code == "disabled" and rep.problem.detail == "truncated"


def fix_name_in(text: str, code: str) -> bool:
    return repair.fix_name(code) in text


# ----------------------------------------------------------------------------------------------------
# late discovery, detection on a broken head
# ----------------------------------------------------------------------------------------------------
def test_late_concatenation_restarts_as_stream(tmp_path: Path) -> None:
    big = b'{"x": "' + b"a" * 300 + b'"}'
    settings = Settings(head_bytes=64)
    out, rep = repair_bytes(tmp_path, big + b" " + big + b" " + big, settings=settings)
    assert out == b"[" + big + b", " + big + b", " + big + b"]"
    assert rep.kind == "stream" and rep.records == 3 and rep.counts == {"concatenated": 1}
    assert "repair.warn.restarted" in rep.warnings


def test_late_concatenation_restarts_as_json_lines(tmp_path: Path) -> None:
    big = b'{"x": "' + b"a" * 300 + b'"}'
    # a JSON5-ish first line keeps the reader's detection from seeing several lines
    data = big[:-1] + b",}\n" + big + b"\n" + big + b"\n"
    out, rep = repair_bytes(tmp_path, data, settings=Settings(head_bytes=64))
    assert out == big + b"\n" + big + b"\n" + big + b"\n"
    assert rep.kind == "jsonl" and rep.records == 3 and rep.counts == {"trailing_comma": 1}


def test_detection_on_a_cleaned_head(tmp_path: Path) -> None:
    # a comment before the root: the reader alone would call this a single value
    out, rep = run(tmp_path, b"// export\n[1, 2, 3,]\n")
    assert rep.kind == "array" and rep.records == 3
    # JSON5-ish lines are JSON Lines
    out, rep = run(tmp_path, b'{"a":1,}\n{"a":2,}\n{"a":3,}\n')
    assert rep.kind == "jsonl" and rep.records == 3 and out == b'{"a":1}\n{"a":2}\n{"a":3}\n'
    # a Python repr dump of a dict with a record list is wrapped
    data = b"{'meta': {'n': 3}, 'data': [{'a': 1}, {'a': 2}, {'a': 3}]}"
    out, rep = run(tmp_path, data)
    assert rep.kind == "wrapped" and rep.record_path == ("data",) and rep.records == 3
    assert out == data.replace(b"'", b'"')


def test_wrapped_record_path_not_found_warns(tmp_path: Path) -> None:
    out, rep = run(tmp_path, b'{"a": [1, 2], "b": NaN}', record_path="data")
    assert out == b'{"a": [1, 2], "b": null}' and rep.records == 0
    assert "repair.warn.record_path_missing" in rep.warnings


# ----------------------------------------------------------------------------------------------------
# the gap hold fallback and spilled output
# ----------------------------------------------------------------------------------------------------
def test_hold_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair, "HOLD_MAX", 8)
    # a missing comma after a long gap goes right before the next value
    out, rep = run(tmp_path, b"[1" + b" " * 10 + b"2]")
    assert out == b"[1" + b" " * 10 + b",2]"
    assert [short(f) for f in rep.fixes] == [("missing_comma", 12, 1, 13, 12, None)]
    # a trailing comma followed by a long gap is overwritten with a space
    out, rep = run(tmp_path, b"[1," + b" " * 10 + b"]")
    assert out == b"[1" + b" " * 11 + b"]"
    fx = rep.fixes[0]
    assert (fx.code, fx.offset, fx.out_offset, fx.length, fx.out_length, fx.after) == (
        "trailing_comma", 2, 2, 1, 1, " ")
    # a comma that is kept stays where it was
    out, rep = run(tmp_path, b"[1," + b" " * 10 + b"2]")
    assert out == b"[1," + b" " * 10 + b"2]" and rep.total == 0
    # comments in a long gap after a trailing comma: the comma's entry comes first
    out, rep = run(tmp_path, b"[1, /* a */ /* b */ ]")
    assert out == b"[1    ]"
    assert [(f.code, f.offset) for f in rep.fixes] == [("trailing_comma", 2), ("comment", 4), ("comment", 12)]


@pytest.mark.parametrize("data", [
    b"[['a\\tb', 'c\\x41\"d', NaN, 'e'], 1]",                       # previews read held output (peek)
    b"[1, [NaN, NaN, 2," + b" " * 12 + b"], 3]",                     # a written comma overwritten (patch)
    b"[1, [NaN, 'x', None, " + b"2, " * 30,                         # a cut record rolled back (truncate)
])
def test_joined_output_pieces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    # held output pieces are joined every PARTS_JOIN writes (memory of a record with millions of fixes):
    # the result is the same as with every piece kept apart
    monkeypatch.setattr(repair, "HOLD_MAX", 8)
    expected = run(tmp_path, data)
    monkeypatch.setattr(repair, "PARTS_JOIN", 2)
    got = run(tmp_path, data)
    assert got[0] == expected[0]
    assert [fix_tuple(f) for f in got[1].fixes] == [fix_tuple(f) for f in expected[1].fixes]


def test_huge_broken_record_spills_and_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair, "OUT_MEMORY", 64)
    monkeypatch.setattr(repair, "FLUSH_BATCH", 16)
    rec2 = b'{"b": "' + b"x" * 500 + b'", "c": NaN, "d": "' + b"y" * 500
    data = b'[{"a": NaN}, ' + rec2
    out, rep = run(tmp_path, data)
    assert out == b'[{"a": null}]'
    assert [f.code for f in rep.fixes] == ["nan", "truncated"] and rep.records == 1
    assert not list(repair._work_dir(Settings()).glob("repair-spill-*"))


# ----------------------------------------------------------------------------------------------------
# encodings and compression: user-file offsets are the reader's error offsets
# ----------------------------------------------------------------------------------------------------
TEXT = '[{"a": "Тошкент 😀"},\n {"b": NaN}]'


@pytest.mark.parametrize("encoding, bom", [("utf-16-le", True), ("utf-16-be", False), ("utf-16-le", False),
                                           ("utf-8", True)])
def test_encoded_sources(tmp_path: Path, encoding: str, bom: bool) -> None:
    raw = TEXT.encode(encoding)
    if bom:
        raw = (b"\xef\xbb\xbf" if encoding == "utf-8" else "\ufeff".encode(encoding)) + raw
    out, rep = repair_bytes(tmp_path, raw)
    assert out == TEXT.replace("NaN", "null").encode("utf-8")
    nan = [f for f in rep.fixes if f.code == "nan"][0]
    with Document.open(tmp_path / "in.json") as doc:
        doc.build_index()
        err = doc.error
    assert (nan.offset, nan.line, nan.column) == (err.offset, err.line, err.column)
    assert [f.code for f in rep.fixes] == (["bom", "nan"] if bom else ["nan"])
    if bom:
        assert short(rep.fixes[0]) == ("bom", 0, 1, 1, 0, None)
    if encoding.startswith("utf-16"):
        assert "repair.warn.transcoded" in rep.warnings


def test_gzip_source_and_target(tmp_path: Path) -> None:
    raw = b'\xef\xbb\xbf[1, 2,\n 3, NaN]'
    src = tmp_path / "in.json.gz"
    src.write_bytes(gzip.compress(raw))
    rep = repair_file(src, tmp_path / "out.json.gz")
    assert gzip.decompress((tmp_path / "out.json.gz").read_bytes()) == b"[1, 2,\n 3, null]"
    with Document.open(src) as doc:
        doc.build_index()
        err = doc.error
    nan = rep.fixes[-1]
    assert (nan.offset, nan.line, nan.column) == (err.offset, err.line, err.column) == (14, 2, 5)
    assert rep.compression == "gzip" and "repair.warn.uncompressed" not in rep.warnings
    rep = repair_file(src, tmp_path / "out.json")
    assert (tmp_path / "out.json").read_bytes() == b"[1, 2,\n 3, null]"
    assert "repair.warn.uncompressed" in rep.warnings
    plain = tmp_path / "p.json"
    plain.write_bytes(b"[1,]")
    rep = repair_file(plain, tmp_path / "p.out.json.gz")
    assert gzip.decompress((tmp_path / "p.out.json.gz").read_bytes()) == b"[1]"
    assert "repair.warn.compressed" in rep.warnings


def test_locations_match_the_reader(tmp_path: Path) -> None:
    data = ('[\n  {"ism": "Ўзбекистон", "x": NaN},\n\t{"kalit bo\'shliq": \'qiymat\', "y": [1, 2,]},\n'
            '  {"z": "😀😀", "w": True}\n]').encode("utf-8")
    _out, rep = run(tmp_path, data)
    with Document.open(tmp_path / "in.json") as doc:
        doc.build_index()
        for fx in rep.fixes:
            line0 = data.count(b"\n", 0, fx.offset)
            ls = data.rfind(b"\n", 0, fx.offset) + 1
            err = doc._make_error((fx.offset, "x", "none", line0, ls))
            assert (fx.line, fx.column) == (err.line, err.column), fx


# ----------------------------------------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------------------------------------
def test_report_fix_list_and_files(tmp_path: Path) -> None:
    data = b"[" + b", ".join(b"NaN" for _ in range(30)) + b"]"
    out, rep = run(tmp_path, data, max_items=5)
    src = tmp_path / "in.json"
    rep = repair_file(src, tmp_path / "o.json", RepairOptions(max_items=5))
    try:
        assert rep.total == 30 and len(rep.fixes) == 5 and rep.counts == {"nan": 30}
        assert rep.fixes_file is not None and rep.fixes_file.exists()
        everything = list(rep.iter_fixes())
        assert len(everything) == 30 and everything[:5] == rep.fixes
        assert [f.offset for f in everything] == [1 + 5 * i for i in range(30)]
        assert rep.first["nan"] == rep.fixes[0]
        for fmt in ("csv", "json", "jsonl"):
            path = tmp_path / f"report.{fmt}"
            rep.write(path, fmt)
            raw = path.read_bytes()
            if fmt == "csv":
                lines = raw.decode("utf-8-sig").splitlines()
                assert lines[0].split(",")[:4] == ["code", "name", "category", "offset"]
                assert len(lines) == 31 and lines[1].startswith("nan,")
            elif fmt == "json":
                items = json.loads(raw)
                assert len(items) == 30 and items[0]["code"] == "nan" and items[0]["name"]
            else:
                items = [json.loads(x) for x in raw.splitlines()]
                assert len(items) == 30 and items[-1]["offset"] == 146
        with pytest.raises(UserInputError):
            rep.write(tmp_path / "r.xml", "xml")
    finally:
        rep.close()
    assert rep.fixes_file is None
    # every fix in memory: no temporary file
    rep = repair_file(src, tmp_path / "o2.json")
    assert rep.fixes_file is None and len(rep.fixes) == 30
    assert not list(repair._work_dir(Settings()).glob(f"{repair.FIXES_PREFIX}*"))


def test_report_to_dict_and_texts(tmp_path: Path, lang_en) -> None:
    out, rep = run(tmp_path, b'[1, 2,, 3', allow_partial=True)
    d = rep.to_dict()
    json.dumps(d)
    assert d["problem"]["code"] == "unexpected_char" and d["truncated"]["closers"] == "]"
    out, rep = run(tmp_path, b'// c\n[{"a": NaN},\n {"b": 2},]\n')
    d = rep.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["counts"] == {"comment": 1, "nan": 1, "trailing_comma": 1} and d["total"] == 3 and d["changed_data"]
    lines = rep.summary_lines()   # in the order of their first occurrence
    assert lines == ["  comment removed: 1 (first at line 1, column 1)",
                     "  NaN → null: 1 (first at line 2, column 8)",
                     "  trailing comma removed: 1 (first at line 3, column 10)"]
    assert rep.data_changes_line() == "Fixes that changed or dropped data: NaN → null 1."
    assert rep.headline().startswith("Repaired: in.json → r")
    from jsonreshape.i18n import set_language
    set_language("uz")
    assert rep.summary_lines()[1] == "  NaN → null: 1 ta (birinchisi 2-qator, 8-ustun)"
    assert rep.data_changes_line() == "Ma'lumotni o'zgartirgan yoki tashlab yuborgan tuzatishlar: NaN → null 1 ta."
    assert rep.headline().startswith("Ta'mirlandi: in.json → r")


def test_no_fix_kept_in_memory(tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(b"[NaN, 1,]")
    rep = repair_file(src, tmp_path / "o.json", RepairOptions(max_items=0))
    try:
        assert rep.fixes == [] and rep.counts == {"nan": 1, "trailing_comma": 1} and rep.total == 2
        assert [f.code for f in rep.iter_fixes()] == ["nan", "trailing_comma"]
        assert set(rep.first) == {"nan", "trailing_comma"} and len(rep.summary_lines()) == 2
    finally:
        rep.close()


def test_invalid_output_is_an_internal_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a repair that produced invalid JSON (a program error) is caught by the output check: nothing replaced
    monkeypatch.setattr(repair, "_REPLACEMENT", b'"')
    src = tmp_path / "in.json"
    src.write_bytes(b'["\xff"]')
    dst = tmp_path / "o.json"
    dst.write_bytes(b"old")
    for acc in (True, False):
        with pytest.raises(repair.JsonReshapeError) as ei:
            repair_file(src, dst, RepairOptions(accelerate=acc))
        assert ei.value.key == "repair.error.invalid_output"
        assert dst.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.json", "o.json"]


def test_every_code_has_texts() -> None:
    for code in FIX_CODES:
        assert t(f"repair.fix.{code}") != f"repair.fix.{code}"
        assert t(f"repair.category.{FIX_CATEGORY[code]}") != f"repair.category.{FIX_CATEGORY[code]}"
    for code in PROBLEM_CODES:
        assert t(f"repair.problem.{code}") != f"repair.problem.{code}"


# ----------------------------------------------------------------------------------------------------
# API: check only, options, identity of the source, cancellation, progress
# ----------------------------------------------------------------------------------------------------
def test_check_only_writes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(b"[1, 2,]")
    rep = repair_file(src, None, check_only=True)
    assert not rep.written and rep.check_only and rep.counts == {"trailing_comma": 1}
    assert rep.target is None and rep.bytes_out == 6
    src.write_bytes(b"[1, 2}")
    rep = repair_file(src, tmp_path / "out.json", check_only=True)
    assert rep.problem.code == "mismatched_closer" and not (tmp_path / "out.json").exists()
    src.write_bytes(b"[1, 2]")
    rep = repair_file(src, None, check_only=True)
    assert rep.total == 0 and rep.problem is None


def test_target_rules(tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(b"[1,]")
    with pytest.raises(UserInputError) as ei:
        repair_file(src, src)
    assert ei.value.key == "writer.error.same_file"
    with pytest.raises(UserInputError) as ei:
        repair_file(src, tmp_path / "." / "in.json")
    assert ei.value.key == "writer.error.same_file"
    with pytest.raises(UserInputError) as ei:
        repair_file(src, None)
    assert ei.value.key == "repair.error.no_target"


@pytest.mark.parametrize("opts", [
    RepairOptions(nan="zero"), RepairOptions(output_kind="csv"), RepairOptions(fixes=frozenset({"nope"})),
    RepairOptions(max_items=-1), RepairOptions(window=0),
])
def test_bad_options(tmp_path: Path, opts: RepairOptions) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(b"[1]")
    with pytest.raises(UserInputError) as ei:
        repair_file(src, tmp_path / "o.json", opts)
    assert ei.value.key == "repair.error.option"


def test_jsonl_output_needs_values_or_lines(tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(b"[1]")
    with pytest.raises(UserInputError) as ei:
        repair_file(src, tmp_path / "o.jsonl", RepairOptions(output_kind="jsonl"))
    assert ei.value.key == "repair.error.output_kind"


def test_source_changed_during_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(b"[1,]")
    dst = tmp_path / "o.json"
    monkeypatch.setattr(repair, "appended_bytes", lambda p, identity: None)
    with pytest.raises(FileChangedError):
        repair_file(src, dst)
    assert not dst.exists()
    monkeypatch.setattr(repair, "appended_bytes", lambda p, identity: 7)
    rep = repair_file(src, dst)
    assert dst.read_bytes() == b"[1]" and "reader.warn.appended" in rep.warnings


def test_cancel_leaves_nothing(tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(b"[" + b"NaN, " * 2000 + b"1]")
    dst = tmp_path / "o.json"
    token = CancelToken()
    calls = []

    def cb(info):
        calls.append(info.done)
        if info.done:
            token.cancel()

    prog = Progress(callback=cb, min_interval=0)
    with pytest.raises(Cancelled):
        repair_file(src, dst, RepairOptions(window=512), progress=prog, cancel=token)
    assert not dst.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.json"]
    assert not list(repair._work_dir(Settings()).glob("repair-*"))


def test_progress_reports_bytes(tmp_path: Path) -> None:
    data = b"[" + b",".join(b'{"a": %d, "b": NaN}' % i for i in range(3000)) + b"]"
    src = tmp_path / "in.json"
    src.write_bytes(data)
    infos = []
    prog = Progress(callback=infos.append, min_interval=0)
    repair_file(src, tmp_path / "o.json", RepairOptions(window=4096), progress=prog)
    assert infos[-1].finished and infos[-1].done == infos[-1].total == len(data)
    assert infos[-1].unit == "bytes"
    assert any(0 < i.done < len(data) for i in infos)


def test_backends_agree(tmp_path: Path, core_backend) -> None:
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = repair.set_scanner_backend(name)
    try:
        data = b'[{"a": 1}, {"b": NaN}, {"c": [1, 2,]}, {"d": 3}, {"e": 4} {"f": 5}, {"g": "\xff"}, {"h'
        out, rep = repair_bytes(tmp_path, data, windows=(None, 1, 7, 4096))
        assert out == b'[{"a": 1}, {"b": null}, {"c": [1, 2]}, {"d": 3}, {"e": 4}, {"f": 5}, {"g": "\xef\xbf\xbd"}]'
        assert rep.records == 7 and rep.truncated.closers == "]"
    finally:
        repair.set_scanner_backend(old)


def test_no_leftover_temporary_files(tmp_path: Path) -> None:
    for data in (b"[1, 2]", b"[NaN]", b"[1, 2}", b'{"a": 1}{"b": 2}'):
        src = tmp_path / "in.json"
        src.write_bytes(data)
        try:
            repair_file(src, tmp_path / "o.json").close()
        except UnrepairableError:
            pass
    left = [p.name for p in repair._work_dir(Settings()).iterdir() if p.name.startswith("repair-")]
    assert left == []
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []


def test_stale_temporary_files_are_purged(tmp_path: Path) -> None:
    d = repair._work_dir(Settings())
    old = d / f"{repair.FIXES_PREFIX}1-dead.jsonl"
    old.write_bytes(b"[]\n")
    fresh = d / f"{repair.FIXES_PREFIX}2-live.jsonl"
    fresh.write_bytes(b"[]\n")
    os.utime(old, (1, 1))
    src = tmp_path / "in.json"
    src.write_bytes(b"[1]")
    repair_file(src, tmp_path / "o.json")
    assert not old.exists() and fresh.exists()
