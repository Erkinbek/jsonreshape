"""Regression tests of the query workstream's review fixes (phase 5a).

Every test names the findings it covers (``lens/id`` of the review):

* blackbox/F8, codereview/CR-12 -- a JMESPath runtime error in one record is a non-match,
  counted in ``FilterReport`` with the first record number and a localized reason;
* codereview/CR-06, requirements/F13 -- exact number comparisons in the filter (agreeing
  with ``search --number`` and the sort), reversed ``between`` bounds are swapped;
* blackbox/F14 (query part) -- localized regex and JMESPath messages;
* requirements/F3, robustness/F5 -- bounded memory of streamed (huge) records:
  ``stream_values(max_bytes=...)``, conditions decided on the event stream, capture
  limit warnings, bounded sort keys of huge values;
* robustness/F4 -- incremental search hits, spans capped to the snippet, huge strings
  matched window by window;
* blackbox/F18 -- warnings for sort keys missing in (almost) every record;
* blackbox/F13, requirements/F19 -- Uzbek transliteration (ц, ўъ, сҳ);
* requirements/F20 -- named filter presets;
* requirements/F6, blackbox/F12, codereview/CR-04, codereview/CR-08 -- the memory budget
  in sort and search, a bounded number of sort runs, run files in the cache folder;
* requirements/F1 (filter / sort / search / schema part) -- ``doc.ensure_unchanged()``
  after the scan;
* blackbox/F10 (schema part) -- schema progress never exceeds its total.
"""

from __future__ import annotations

import json
import os
import random
import tracemalloc
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as hyp_st

import jsongen
from jsonreshape import codec, i18n
from jsonreshape import recordset as rsmod
from jsonreshape import search as S
from jsonreshape import stream as stream_mod
from jsonreshape import textmatch as tm
from jsonreshape.appdirs import app_home, recordsets_dir, work_dir
from jsonreshape.errors import FileChangedError, QuerySyntaxError, UserInputError
from jsonreshape.query import Condition, Filter, FilterEvalError, FilterPresets, compile_filter
from jsonreshape.reader import Document, split_member
from jsonreshape.recordset import filter_records, sort_records
from jsonreshape.schema import infer_schema
from jsonreshape.search import SearchOptions, iter_hits, make_snippet, reference_search, search
from jsonreshape.settings import Settings
from jsonreshape.sortkeys import SortKey, encode_token, record_key
from jsonreshape.stream import (CaptureLimitExceeded, DuplicateKeys, stream_probe, stream_text_prefix,
                                stream_values)
from jsonreshape.tasks import Progress
from jsonreshape.textmatch import TextQuery

MiB = 1 << 20


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []

    def make(recs: list[Any] | None = None, fmt: str = "array", *, raw: bytes | None = None,
             settings_: Settings | None = None, **kw: Any) -> Document:
        p = tmp_path / f"doc{len(docs)}.json"
        if raw is not None:
            p.write_bytes(raw)
        else:
            jsongen.write_file(p, recs or [], fmt, **kw)
        doc = Document.open(p, format=None if raw is not None else fmt, settings=settings_)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


def records_of(doc: Document) -> list[tuple[str | None, Any]]:
    out = []
    with open(doc.data_path, "rb") as fh:
        for i in range(doc.record_count):
            s, e = doc.record_span(i)
            fh.seek(s)
            buf = fh.read(e - s)
            if doc.member_records:
                key, vo = split_member(buf)
                out.append((key, codec.loads(buf[vo:])))
            else:
                out.append((None, codec.loads(buf)))
    return out


def brute(doc: Document, flt: Filter) -> list[int]:
    return [i for i, (key, value) in enumerate(records_of(doc)) if flt.matches(value, key=key, index=i)]


def run_filter(doc: Document, flt: Any, **kw: Any) -> tuple[list[int], Any]:
    rs = filter_records(doc, flt, **kw)
    try:
        return rs.to_list(), rs.report
    finally:
        rs.close()


def run_sort(doc: Document, keys: Any, **kw: Any) -> tuple[list[int], Any]:
    rs = sort_records(doc, keys, **kw)
    try:
        return rs.to_list(), rs.report
    finally:
        rs.close()


def traced_peak(fn: Any) -> tuple[Any, int]:
    tracemalloc.start()
    try:
        result = fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


# ==================================================================================
# blackbox/F8, codereview/CR-12, blackbox/F14: JMESPath runtime errors
# ==================================================================================
def test_jmespath_error_in_one_record_does_not_abort_the_filter(make_doc, lang_en) -> None:
    recs = [{"id": i, "tags": ["vip"] if i % 3 == 0 else ["x"]} for i in range(300)]
    recs[150]["tags"] = None
    doc = make_doc(recs, "jsonl")
    got, rep = run_filter(doc, compile_filter("contains(tags, 'vip')", "jmespath"))
    assert got == [i for i in range(300) if i % 3 == 0 and i != 150]
    assert rep.eval_errors == 1 and rep.first_error_record == 150
    assert rep.warnings == ("recordset.warn.eval_errors",)
    text = rep.warning_texts()[0]
    assert "#150" in text and "contains()" in text and "null" in text and "1 " in text


def test_jmespath_mixed_types_are_counted_with_a_localized_reason(make_doc, lang_uz) -> None:
    recs: list[Any] = [{"n": i} for i in range(10)]
    recs[4]["n"] = "four"
    recs[7]["n"] = "seven"
    doc = make_doc(recs)
    got, rep = run_filter(doc, compile_filter("n > `1`", "jmespath"))
    assert got == [2, 3, 5, 6, 8, 9]
    assert rep.eval_errors == 2 and rep.first_error_record == 4
    text = rep.warning_texts()[0]
    assert "2 ta yozuvda" in text and "#4" in text
    assert "satr va son turidagi qiymatlarni '>' bilan solishtirib bo'lmaydi" in text
    assert "not supported" not in text  # Python's English text only as the detail


def test_filter_eval_error_message_and_detail(lang_uz) -> None:
    flt = compile_filter("contains(tags, 'vip')", "jmespath")
    with pytest.raises(FilterEvalError) as info:
        flt.matches({"tags": None})
    err = info.value
    assert isinstance(err, UserInputError) and err.key == "query.jmespath.runtime"
    assert err.message == ("JMESPath ifodasini hisoblab bo'lmadi: contains() funksiyasiga null turidagi qiymat "
                           "berildi (kutilgan tur: massiv, satr)")
    assert "In function contains()" in err.params["detail"]
    assert err.hint is not None and err.hint.startswith("JMESPath xabari: In function contains()")
    with pytest.raises(FilterEvalError) as info:
        compile_filter("floor(x) > `1`", "jmespath").matches({"x": codec.loads(b"1e400")})
    assert info.value.message.endswith("sonni hisoblab bo'lmadi (masalan, son juda katta)")
    i18n.set_language("en")  # the reason is rendered in the language active when raising (like paths.py)
    with pytest.raises(FilterEvalError) as info:
        flt.matches({"tags": None})
    assert info.value.message == ("The JMESPath expression could not be evaluated: function contains() got a value "
                                  "of type null (expected: array, string)")


def test_jmespath_errors_in_streamed_records_are_counted(make_doc, monkeypatch) -> None:
    # a record codec.loads refuses is evaluated from the stream: its error counts as well
    raw = b'[{"t":["a"]},{"t":null,"marker":1},{"t":["a"]}]'
    doc = make_doc(raw=raw)
    real = codec.loads

    def fake(data: Any) -> Any:
        if b"marker" in (data.encode() if isinstance(data, str) else bytes(data)):
            raise codec.JsonDecodeError("simulated", 0)
        return real(data)

    monkeypatch.setattr(codec, "loads", fake)
    got, rep = run_filter(doc, compile_filter("contains(t, 'a')", "jmespath"))
    assert got == [0, 2] and rep.eval_errors == 1 and rep.first_error_record == 1 and rep.unparseable == 0


def test_filter_regex_errors_use_the_search_texts(lang_uz) -> None:
    with pytest.raises(QuerySyntaxError) as f_info:
        compile_filter('name matches "abc("')
    with pytest.raises(QuerySyntaxError) as s_info:
        TextQuery("abc(", mode="regex")
    with pytest.raises(QuerySyntaxError) as b_info:
        Filter.from_builder(Condition("name", "regex", "abc("))
    for info in (f_info, s_info, b_info):
        assert "'(' yopilmagan: ')' yetishmaydi" in info.value.message, info.value.message
    assert "missing" not in f_info.value.message and "subpattern" not in f_info.value.message
    with pytest.raises(QuerySyntaxError) as info:
        compile_filter('name ~ "(?P<a>x)(?P<a>y)"')
    assert "bir xil nomli ikkita guruh bor" in info.value.message


# ==================================================================================
# codereview/CR-06, requirements/F13: exact numbers, reversed between bounds
# ==================================================================================
NUMS = ["1e-400", "0", "1e400", "2e400", "-1e-400"]


@pytest.mark.parametrize("expr, want", [
    ("x > 0", ["1e-400", "1e400", "2e400"]),
    ("x == 0", ["0"]),
    ("x == 1e400", ["1e400"]),
    ("x < 2e400", ["1e-400", "0", "1e400", "-1e-400"]),
    ("x != 0", ["1e-400", "1e400", "2e400", "-1e-400"]),
    ("x", ["1e-400", "1e400", "2e400", "-1e-400"]),  # truthiness: only zero is false
    ("x in [0, 1e400]", ["0", "1e400"]),
    ("x in [2e400]", ["2e400"]),
    ("x >= -1e-400 and x <= 1e-400", ["1e-400", "0", "-1e-400"]),
    ("x between 1e400 and 1e-400", ["1e-400", "1e400"]),
    ("number(string(x)) > 1e399", ["1e400", "2e400"]),
    ("0 < x", ["1e-400", "1e400", "2e400"]),
])
def test_filter_compares_numbers_exactly(expr: str, want: list[str]) -> None:
    recs = {tok: codec.loads(b'{"x":' + tok.encode() + b"}") for tok in NUMS}
    flt = compile_filter(expr)
    assert [tok for tok in NUMS if flt.matches(recs[tok])] == want


def test_requirements_f13_huge_and_long_fractional_numbers(make_doc) -> None:
    toks = ["1e400", "1e401", "-1e400", "12345678901234567890.5", "12345678901234567891.5"]
    raw = b"[" + b",".join(b'{"v":%s,"t":"%s"}' % (x.encode(), x.encode()) for x in toks) + b"]"
    doc = make_doc(raw=raw)
    for expr, want in (("v == 1e400", [0]), ("v > 1e400", [1]), ("v >= 1e400", [0, 1]),
                       ("v == 12345678901234567890.5", [3]), ("v < 12345678901234567891.5 and v > 0", [3])):
        for strategy in ("extract", "parse"):
            assert run_filter(doc, expr, strategy=strategy)[0] == want, (expr, strategy)
    order, _ = run_sort(doc, ["v"])
    assert order == [2, 3, 4, 0, 1]
    res = search(doc, SearchOptions.build("> 1e400", mode="number"))
    assert [h.record for h in res.hits] == [1]
    res.close()


def _rand_token(rng: random.Random) -> str:
    sign = rng.choice(["", "", "-"])
    intp = rng.choice(["0", str(rng.randint(1, 9)), str(rng.randint(10, 10**22))])
    frac = rng.choice(["", "", "." + "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 22)))])
    exp = rng.choice(["", "", "e%d" % rng.randint(-450, 450), "E+%d" % rng.randint(0, 25), "e-%d" % rng.randint(0, 25)])
    return sign + intp + frac + exp


def test_filter_search_and_sort_agree_on_number_order() -> None:
    rng = random.Random(5)
    toks = [_rand_token(rng) for _ in range(300)] + jsongen.EDGE_NUMBER_TOKENS + ["1.10", "1.1", "-0.0", "0e5"]
    spec = SortKey("x")
    tests = {"<": lambda c: c < 0, "<=": lambda c: c <= 0, "==": lambda c: c == 0, "!=": lambda c: c != 0,
             ">": lambda c: c > 0, ">=": lambda c: c >= 0}
    compiled: dict[tuple[str, str], Filter] = {}
    for _ in range(2500):
        a, b = rng.choice(toks), rng.choice(toks)
        rec = codec.loads(b'{"x":' + a.encode() + b"}")
        ka, kb = encode_token(a.encode(), spec), encode_token(b.encode(), spec)
        order = (ka > kb) - (ka < kb)  # the sort's exact order
        for op, test in tests.items():
            flt = compiled.get((op, b))
            if flt is None:
                flt = compiled[(op, b)] = compile_filter(f"x {op} {b}")
            assert flt.matches(rec) == test(order), (a, op, b)
            assert TextQuery(f"{'=' if op == '==' else op} {b}", mode="number").match_number(a) == test(order)


def test_between_with_reversed_bounds_swaps_them() -> None:
    flt = compile_filter("n between 9 and 2")
    assert [n for n in range(12) if flt.matches({"n": n})] == list(range(2, 10))
    assert compile_filter('s between "z" and "a"').matches({"s": "m"})
    assert compile_filter("n between hi and lo").matches({"n": 5, "hi": 9, "lo": 2})
    assert not compile_filter("n between 9 and \"a\"").matches({"n": 5})  # bounds of different types
    built = Filter.from_builder(Condition("n", "between", 9, 2))
    assert built.text == "n between 9 and 2" and built.matches({"n": 5}) and not built.matches({"n": 10})
    assert TextQuery("9..2", mode="number").match_number("5")


# ==================================================================================
# requirements/F3, robustness/F5: streamed records with bounded memory
# ==================================================================================
def test_stream_values_capture_limit_and_first_only(tmp_path: Path, lang_en) -> None:
    items = ["value %d" % i for i in range(1000)]
    p = tmp_path / "c.json"
    p.write_bytes(b"[" + codec.dumps({"id": 1, "items": items, "obj": {"a": [1, 2]}}) + b"]")
    with Document.open(p) as doc:
        doc.build_index()
        span = doc.record_span(0)
        with pytest.raises(CaptureLimitExceeded) as info:
            stream_values(doc, span, ["items[*]"], max_bytes=10_000)
        assert info.value.key == "stream.error.capture_limit" and "10.0 KB" in info.value.message
        assert info.value.hint
        got = stream_values(doc, span, ["items[*]", "id", "obj"], max_bytes=10**7)
        assert got == {0: items, 1: [1], 2: [{"a": [1, 2]}]}
        # only the first value is kept (and only it, plus the keys of the matched path,
        # counts against the budget)
        assert stream_values(doc, span, ["items[*]", "obj.a[*]"], first_only=True, max_bytes=800) == \
            {0: ["value 0"], 1: [1]}
        with pytest.raises(CaptureLimitExceeded):
            stream_values(doc, span, ["obj"], max_bytes=50)  # a container is counted while it is built


def test_stream_values_keep_only_the_values(tmp_path: Path) -> None:
    n = 100_000
    p = tmp_path / "big.json"
    p.write_bytes(b'[{"items":[' + b",".join(b'{"k":%d,"v":"Toshkent"}' % i for i in range(n)) + b"]}]")
    with Document.open(p) as doc:
        doc.build_index()
        span = doc.record_span(0)
        got, peak = traced_peak(lambda: stream_values(doc, span, ["items[*].k"]))
        assert got[0] == list(range(n))
        # the ints and the list: ~4 MB; a path and a position tuple per value were ~20 MB more
        assert peak < 10 * MiB, peak
        got, peak = traced_peak(lambda: stream_values(doc, span, ["items[*].k"], first_only=True))
        assert got[0] == [0] and peak < 3 * MiB, peak


HUGE_ITEMS = 50_000


@pytest.fixture
def huge_doc(make_doc):
    raw = (b'[{"id":0,"items":[]},{"id":1,"items":['
           + b",".join(b'{"k":%d,"city":"Samarqand"}' % i for i in range(HUGE_ITEMS))
           + b'],"status":"ok"},{"id":2,"items":[{"k":7,"city":null}],"status":""}]')
    doc = make_doc(raw=raw, settings_=Settings(large_node_bytes=MiB))
    assert doc.is_huge(1) and not doc.is_huge(0)
    return doc


@pytest.mark.parametrize("expr, want", [
    ("items[*].k == 7", [1, 2]),
    ("items[*].k > 49998", [1]),
    ("len(items) > 5", [1]),
    ("len(items) == 0", [0]),
    ('items[*].city == "Toshkent"', []),
    ('items[*].city != "Samarqand"', [0, 2]),
    ("exists(items[*].city)", [1, 2]),
    ("items[*].city is null", [2]),
    ("items is empty", [0]),
    ("status is empty", [2]),
    ("items", [1, 2]),
    ('type(items[0]) == "object"', [1, 2]),
    ('lower(items[*].city) startswith "sam"', [1]),
    ("items[*].k between 50 and 40", [1]),
    ("items[*].k in [7, 49999]", [1, 2]),
    ('"Samarqand" in items[*].city', [1]),
    ('$ contains "status"', [1, 2]),
    ("len($) == 3", [1, 2]),
    ("7 == items[*].k and id > 0", [1, 2]),
    ("len(items)", [1, 2]),
    ("number(items[*].k) == 3", [1]),
])
def test_huge_record_conditions_are_decided_on_the_stream(huge_doc, expr: str, want: list[int]) -> None:
    flt = compile_filter(expr)
    (got, rep), peak = traced_peak(lambda: run_filter(huge_doc, flt))
    assert got == want == brute(huge_doc, flt)
    assert rep.too_large == 0 and rep.skipped_huge == 0 and rep.warnings == ()
    # nothing of the 50 000 elements is kept (capturing items[*].k alone was ~17 MB)
    assert peak < 4 * MiB, (expr, peak)


def test_huge_record_values_over_the_budget_are_skipped_with_a_warning(huge_doc, monkeypatch, lang_en) -> None:
    monkeypatch.setattr(stream_mod, "CAPTURE_MIN_BYTES", 0)
    huge_doc.settings = Settings(large_node_bytes=MiB, memory_limit_bytes=400_000)  # budget: 100 000 bytes
    flt = compile_filter("items[*].k == id")  # a dynamic comparison needs the values
    got, rep = run_filter(huge_doc, flt)
    assert got == [] and rep.too_large == 1 and rep.first_too_large == 1
    assert rep.warnings == ("recordset.warn.too_large",)
    text = rep.warning_texts()[0]
    assert "#1" in text and "too large to evaluate this expression in memory" in text and "100.0 KB" in text
    # within a bigger budget the same record is evaluated exactly
    monkeypatch.setattr(stream_mod, "CAPTURE_MIN_BYTES", 64 * MiB)
    got, rep = run_filter(huge_doc, flt)
    assert got == brute(huge_doc, flt) == [1] and rep.too_large == 0


STREAM_FILTERS = [
    'contact.address.city == "Toshkent"', "contact.address.city != \"Toshkent\"", "score > 0", "score <= 1.10",
    "score == 12345678901234567890", "orders[*].oid > 50", "orders[*].sum == 0", "len(orders) >= 2",
    "len(tags) == 0", 'tags contains "Toshkent"', 'tags[*] startswith "O"', 'name contains "osh"',
    'name matches "^[A-Z]"', 'lower(name) contains "toshkent"', 'upper(contact.address.city) == "TOSHKENT"',
    'type(score) == "number"', 'type(extra) == "object"', "active == true and score > 0", "active", "not active",
    "$index < 5 or $index >= 95", '$key startswith "id1"', "$key == name", 'contact.* == "+998901234567"',
    "extra.* is null", "extra[*][*] == 1", "number(contact.phone) > 998900000000", 'string(score) == "1.10"',
    "**.oid == 7", "$.id > 50", '$ contains "note"', "len($) > 6", "missing(contact.address.zip)",
    "empty(tags)", "extra is empty", "note is null", "note is not null", "exists(note)", "contact is missing",
    "score between 5 and -5", 'contact.address.city in ["Buxoro", "Тошкент"]', "id not in [1, 2, 3]",
    '"Toshkent" in tags', "5 > orders[*].oid", "orders[*].oid == id", "len(name) between 3 and 6",
    "len(contact) == 2", 'type(tags[*]) == "string"', "number(orders[*].sum) >= 0", "extra", "tags",
    'extra contains "a"', "trim(name) == name", "[score, id] contains 5",
]


@pytest.mark.parametrize("fmt", ["array", "keyed", "jsonl"])
def test_matches_stream_equals_matches(make_doc, fmt: str) -> None:
    doc = make_doc(jsongen.records(100, seed=13), fmt)
    member = doc.member_records
    parsed = records_of(doc)
    for text in STREAM_FILTERS:
        flt = compile_filter(text)
        for i, (key, value) in enumerate(parsed):
            want = flt.matches(value, key=key, index=i)
            got = flt.matches_stream(doc, doc.record_span(i), member, key=key, index=i, max_bytes=10 * MiB)
            assert got == want, (fmt, text, i)


def test_matches_stream_on_values_and_duplicate_keys(make_doc) -> None:
    dupes = [b'{"a":1,"tags":["x"],"a":2,"tags":"vip"}', b'{"a":{"b":1},"a":{"c":2}}', b'{"x":[1,{"y":2,"y":3}]}',
             b'{"k":"a","k":null}', b"[1,2,3]", b'"text"', b"5", b"null", b"[]", b"{}"]
    values = [codec.dumps(v) for v in jsongen.records(60, seed=4, kind="value")]
    doc = make_doc(raw=b"[" + b",".join(dupes + values) + b"]")
    texts = ["a == 2", "a == 1", 'tags contains "x"', 'tags == "vip"', "a.b == 1", "a.c == 2", "exists(a.b)",
             "len(a) == 1", "x[1].y == 3", "x[1].y == 2", "k is null", 'k == "a"', "len($) == 3", "$ > 4",
             '$ == "text"', "$ is empty", "$", '$[*] contains "a"', "$.* == null", "*.*.* != 5", 'type($) == "array"',
             "len(tags) == 3", "exists(**.y)", "$[0] > 0"]
    parsed = records_of(doc)
    for text in texts:
        flt = compile_filter(text)
        for i, (_, value) in enumerate(parsed):
            assert flt.matches_stream(doc, doc.record_span(i)) == flt.matches(value), (text, i)


_STREAM_DOCS: dict[int, tuple[Path, list[Any]]] = {}


def _stream_doc(tmp_path_factory: Any, seed: int) -> tuple[Path, list[Any]]:
    from test_query import sample_records

    got = _STREAM_DOCS.get(seed)
    if got is None or not got[0].exists():
        recs = sample_records(seed)
        p = tmp_path_factory.mktemp("stream") / f"s{seed}.json"
        p.write_bytes(b"[" + b",".join(codec.dumps(rec) for rec, _, _ in recs) + b"]")
        got = _STREAM_DOCS[seed] = (p, recs)
    return got


def _random_expressions() -> Any:
    from test_query import expr_texts

    return expr_texts(2)


@hyp_settings(max_examples=250, derandomize=True, database=None, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(text=_random_expressions(), seed=hyp_st.integers(0, 2))
def test_random_expressions_streamed_equal_parsed(tmp_path_factory, text: str, seed: int) -> None:
    # requirements/F3, robustness/F5: Filter.matches_stream (atoms decided on the event
    # stream + captured values) == Filter.matches on the parsed record. string() of numbers
    # is excluded: ijson normalizes exponent texts (a documented deviation).
    try:
        flt = compile_filter(text)
    except QuerySyntaxError:
        return
    if "string(" in text:
        return
    p, recs = _stream_doc(tmp_path_factory, seed)
    with Document.open(p) as doc:
        doc.build_index()
        for i, (rec, key, index) in enumerate(recs):
            want = flt.matches(codec.loads(codec.dumps(rec)), key=key, index=index)
            got = flt.matches_stream(doc, doc.record_span(i), key=key, index=index, max_bytes=10 * MiB)
            assert got == want, (text, i)


def test_stream_probe_reports_duplicate_keys_on_matched_paths(tmp_path: Path) -> None:
    p = tmp_path / "d.json"
    p.write_bytes(b'[{"a":1,"a":2,"z":{"q":1,"q":2}}]')
    with Document.open(p) as doc:
        doc.build_index()
        span = doc.record_span(0)

        class Count:
            n = 0

            def value(self, v: Any) -> None:
                self.n += 1

            def open(self, is_map: bool) -> bool:
                return False

        with pytest.raises(DuplicateKeys):
            stream_probe(doc, span, ["a"], {0: [Count()]})
        # a duplicate where no pattern can match does not matter
        probe = Count()
        assert stream_probe(doc, span, ["b"], {0: [probe]}, capture=[0]) == {0: []} and probe.n == 0
        # stream_values follows dict semantics (the last duplicate wins)
        assert stream_values(doc, span, ["a", "z.q"]) == {0: [2], 1: [2]}


def test_stream_text_prefix(tmp_path: Path) -> None:
    rec = {"a": {"b": [1, 'x"y', codec.loads(b"1.10"), None, True, {"c": "Тошкент"}]}, "s": "abcdef", "n": 5}
    p = tmp_path / "t.json"
    p.write_bytes(b"[" + codec.dumps(rec) + b"]")
    with Document.open(p) as doc:
        doc.build_index()
        span = doc.record_span(0)
        text = codec.dumps(rec["a"])
        assert stream_text_prefix(doc, span, "a", limit=1000) == ("object", text, True)
        assert stream_text_prefix(doc, span, "a", limit=10) == ("object", text[:10], False)
        assert stream_text_prefix(doc, span, "a.b", limit=1000) == ("array", codec.dumps(rec["a"]["b"]), True)
        assert stream_text_prefix(doc, span, "s", limit=3) == ("string", "abc", False)
        assert stream_text_prefix(doc, span, "n", limit=3) == ("scalar", 5, True)
        assert stream_text_prefix(doc, span, "a.b[*]", limit=3) == ("scalar", 1, True)
        assert stream_text_prefix(doc, span, "zz", limit=3) is None
        assert stream_text_prefix(doc, span, "$", limit=5000) == ("object", codec.dumps(rec), True)


def test_sort_by_a_value_too_large_to_capture_uses_its_beginning(make_doc, monkeypatch, lang_en) -> None:
    monkeypatch.setattr(stream_mod, "CAPTURE_MIN_BYTES", 0)
    monkeypatch.setattr(rsmod, "HUGE_KEY_PREFIX", 60)
    rng = random.Random(3)
    recs = []
    for i in range(24):
        first = "".join(rng.choice("abcdefgh") for _ in range(6))
        if i % 2:
            recs.append({"id": i, "items": [first] + ["filler %d" % j for j in range(80)], "s": first * 200})
        else:
            recs.append({"id": i, "items": [first], "s": first})
    settings = Settings(large_node_bytes=1000, memory_limit_bytes=4000)  # capture budget: 1000 bytes
    doc = make_doc(recs, settings_=settings)
    huge = [i for i in range(doc.record_count) if doc.is_huge(i)]
    assert len(huge) == 12
    for key in ("items", "-items", "s", "-s"):
        got, rep = run_sort(doc, [key])
        spec = SortKey.parse(key)
        want = [i for _, i in sorted((record_key(r, [spec]), i) for i, r in enumerate(recs))]
        assert got == want, key  # the values differ within their first 60 bytes / characters
        assert rep.approximate == 12 and rep.warnings == ("recordset.warn.approximate",), (key, rep)
        assert "#1" in rep.warning_texts()[0]


def test_sort_of_huge_records_keeps_only_first_values(make_doc) -> None:
    n = 100_000
    raw = (b'[{"id":2,"items":[{"k":5}]},{"id":1,"items":['
           + b",".join(b'{"k":%d}' % (i + 1) for i in range(n)) + b"]},{\"id\":0}]")
    doc = make_doc(raw=raw, settings_=Settings(large_node_bytes=MiB))
    assert doc.is_huge(1)
    (got, rep), peak = traced_peak(lambda: run_sort(doc, ["items[*].k"]))
    assert got == [1, 0, 2] and rep.approximate == 0 and rep.missing == (1,)
    assert peak < 4 * MiB, peak


# ==================================================================================
# robustness/F4: incremental search hits, bounded spans, huge strings
# ==================================================================================
def test_search_count_of_a_huge_record_keeps_no_hits(make_doc) -> None:
    n = 60_000
    raw = b'[{"id":1,"items":[' + b",".join(b'{"k":%d,"v":"Toshkent %d"}' % (i, i) for i in range(n)) + b"]}]"
    doc = make_doc(raw=raw, settings_=Settings(large_node_bytes=MiB))
    assert doc.is_huge(0)
    res, peak = traced_peak(lambda: search(doc, SearchOptions.build("Toshkent"), max_hits=0))
    assert res.hit_count == n and res.record_count == 1 and res.hits == [] and res.truncated
    res.close()
    assert peak < 8 * MiB, peak  # every raw hit of the record used to be kept (~25 MB)
    assert sum(1 for _ in iter_hits(doc, SearchOptions.build("Toshkent"))) == n


def test_search_count_of_a_big_parsed_record_produces_hits_incrementally(make_doc, monkeypatch) -> None:
    n = 40_000
    raw = b'[{"id":1,"items":[' + b",".join(b'{"k":%d,"v":"Toshkent %d"}' % (i, i) for i in range(n)) + b"]}]"
    doc = make_doc(raw=raw)
    assert not doc.is_huge(0) and doc.record_span(0)[1] - doc.record_span(0)[0] > S.INCREMENTAL_BYTES
    seen: list[int] = []
    real = S._Matcher.match_iter

    def spy(self: Any, value: Any, key: Any = None) -> Any:
        for batch in real(self, value, key):
            seen.append(len(batch))
            yield batch

    monkeypatch.setattr(S._Matcher, "match_iter", spy)
    res = search(doc, SearchOptions.build("Toshkent"), max_hits=5)
    assert res.hit_count == n and len(res.hits) == 5 and res.truncated
    assert len(seen) > 1 and max(seen) <= S.HIT_BATCH
    assert [h.path for h in res.hits] == [("items", i, "v") for i in range(5)]
    res.close()


@pytest.mark.parametrize("batch", [1, 2, 5])
def test_incremental_record_walk_equals_the_whole_walk(monkeypatch, batch: int) -> None:
    monkeypatch.setattr(S, "HIT_BATCH", batch)
    recs = jsongen.records(120, seed=4) + jsongen.records(80, seed=9, kind="value")
    for text, kw in (("a", {}), ("o", {"path": "**.city"}), ("1", {}), ("id", {"scope": "keys"}),
                     ("> 5", {"mode": "number"}), ("o", {"path": "orders[*]"}), ("Toshkent", {"translit": True})):
        matcher = S._Matcher(SearchOptions.build(text, **kw))
        for i, rec in enumerate(recs):
            key = f"k{i}" if i % 3 == 0 else None
            assert [h for b in matcher.match_iter(rec, key) for h in b] == matcher.match(rec, key), (text, kw, i)


def test_spans_are_capped_to_what_the_snippet_shows(make_doc) -> None:
    s = "ab " * 50_000
    q = TextQuery("ab")
    full = q.find_spans(s)
    assert len(full) == 50_000
    capped = q.find_spans(s, reach=10)
    assert capped == [sp for sp in full if sp[0] <= full[0][0] + 10] and len(capped) == 4
    assert TextQuery("a.", mode="regex").find_spans(s, reach=3) == [(0, 2), (3, 5)]
    doc = make_doc([{"t": s}])
    res = search(doc, SearchOptions.build("ab"))
    assert res.hits[0].text == make_snippet(s, full)[0] and res.hits[0].spans == make_snippet(s, full)[1]
    res.close()


def test_huge_strings_are_matched_window_by_window(monkeypatch) -> None:
    monkeypatch.setattr(tm, "HUGE_TEXT", 0)
    rng = random.Random(8)
    alphabet = "abSsHh'ʼ ц и ь ъ ў с ҳ е р Toshkent ТОШКЕНТ ß"
    for _ in range(1500):
        needle = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4))).strip() or "a"
        hay = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
        for kw in ({}, {"translit": True}, {"whole_word": True}, {"translit": True, "case_sensitive": True}):
            q = TextQuery(needle, **kw)
            monkeypatch.setattr(tm, "WINDOW_CHARS", 1 << 30)
            monkeypatch.setattr(tm, "HUGE_TEXT", 1 << 30)
            want = q.find_spans(hay)
            want_reach = q.find_spans(hay, reach=7)
            monkeypatch.setattr(tm, "HUGE_TEXT", 0)
            monkeypatch.setattr(tm, "WINDOW_CHARS", rng.randint(1, 25))
            assert q.find_spans(hay) == want, (needle, hay, kw)
            assert q.find_spans(hay, reach=7) == want_reach, (needle, hay, kw)


def test_a_huge_string_is_never_folded_whole(monkeypatch) -> None:
    monkeypatch.setattr(tm, "HUGE_TEXT", 1000)
    monkeypatch.setattr(tm, "WINDOW_CHARS", 500)
    lengths: list[int] = []
    real_text, real_map = tm._fold_text, tm._fold_map

    def fold_text(s: str, *a: Any) -> str:
        lengths.append(len(s))
        return real_text(s, *a)

    def fold_map(s: str, *a: Any) -> Any:
        lengths.append(len(s))
        return real_map(s, *a)

    monkeypatch.setattr(tm, "_fold_text", fold_text)
    monkeypatch.setattr(tm, "_fold_map", fold_map)
    hay = "Buxoro shahri " * 2000
    q = TextQuery("BUXORO")
    lengths.clear()
    spans = q.find_spans(hay, reach=150)
    assert spans[0] == (0, 6) and len(spans) == 11
    assert lengths and max(lengths) < 1000
    lengths.clear()
    assert len(q.find_spans(hay)) == 2000 and max(lengths) < 1000


def test_search_stores_hits_within_the_memory_budget(make_doc) -> None:
    recs = [{"t": "Toshkent shahri %d" % i} for i in range(3000)]
    doc = make_doc(recs, settings_=Settings(memory_limit_bytes=400_000))  # 100 000 bytes for stored hits
    res = search(doc, SearchOptions.build("Toshkent"), max_hits=None)
    assert res.hit_count == 3000 and res.record_count == 3000 and res.truncated
    assert 50 < len(res.hits) < 3000
    assert sum(S._hit_cost(h) for h in res.hits) <= 100_000
    assert [h.record for h in res.hits] == list(range(len(res.hits)))
    res.close()
    res = search(make_doc(recs), SearchOptions.build("Toshkent"), max_hits=None)  # default budget: all of them
    assert len(res.hits) == 3000 and not res.truncated
    res.close()


# ==================================================================================
# blackbox/F18: sort keys missing in (almost) every record
# ==================================================================================
def test_sort_by_a_misspelled_field_warns(make_doc, lang_en) -> None:
    doc = make_doc([{"name": "Zafar"}, {"name": "Abdulla"}, {"name": "Olim"}])
    got, rep = run_sort(doc, ["nmae"])
    assert got == [0, 1, 2] and rep.missing == (3,)
    assert rep.warnings == ("recordset.warn.sort_key_absent",)
    text = rep.warning_texts()[0]
    assert "nmae" in text and "3 records" in text and "spelling" in text
    got, rep = run_sort(doc, ["name", "$key", "$index"])
    assert got == [1, 2, 0] and rep.missing == (0, 3, 0)
    assert rep.warnings == ("recordset.warn.sort_key_absent",) and "$key" in rep.warning_texts()[0]
    got, rep = run_sort(doc, ["name"])
    assert rep.warnings == () and rep.missing == (0,)


def test_sort_key_present_in_very_few_records_warns(make_doc, lang_uz) -> None:
    recs: list[Any] = [{"id": i} for i in range(200)]
    recs[50]["score"] = 5
    doc = make_doc(recs, settings_=Settings(large_node_bytes=1 << 20))
    for limit in (None, 400):
        _, rep = run_sort(doc, ["score"], memory_limit=limit)
        assert rep.missing == (199,) and rep.warnings == ("recordset.warn.sort_key_rare",)
        assert "score" in rep.warning_texts()[0] and "200 ta yozuvdan faqat 1 tasida" in rep.warning_texts()[0]
    recs[51]["score"] = 1  # 2 of 200: exactly 99 % missing, no warning
    doc = make_doc(recs)
    assert run_sort(doc, ["score"])[1].warnings == ()


def test_missing_values_are_counted_in_streamed_records(make_doc) -> None:
    recs = jsongen.records(40, seed=6)
    doc = make_doc(recs, settings_=Settings(large_node_bytes=300))
    assert any(doc.is_huge(i) for i in range(doc.record_count))
    specs = [SortKey.parse("note"), SortKey.parse("contact.address.city"), SortKey.parse("$key")]
    _, rep = run_sort(doc, specs)
    want = tuple(sum(1 for r in recs if spec.extract(r) is rsmod.MISSING) for spec in specs)
    assert rep.missing == want and want[2] == 40


# ==================================================================================
# requirements/F6, blackbox/F12, codereview/CR-04, codereview/CR-08: memory budget,
# number of runs, run files in the cache folder
# ==================================================================================
def _sorted_reference(doc: Document, keys: list[str]) -> list[int]:
    specs = [SortKey.parse(k) for k in keys]
    keyed = [(record_key(v, specs, key=k, index=i), i) for i, (k, v) in enumerate(records_of(doc))]
    return [i for _, i in sorted(keyed)]


def test_sort_memory_is_capped_by_the_memory_limit_setting(make_doc) -> None:
    doc = make_doc(jsongen.records(1500, seed=1),
                   settings_=Settings(memory_limit_bytes=60_000, sort_memory_bytes=256 << 20))
    got, rep = run_sort(doc, ["name"])
    assert got == _sorted_reference(doc, ["name"])
    assert rep.memory_limit == 30_000 and rep.strategy == "external" and rep.runs > 1
    assert rep.peak_bytes_estimate <= 30_000 + 1000
    # an explicit limit is capped the same way
    _, rep = run_sort(doc, ["name"], memory_limit=1 << 30)
    assert rep.memory_limit == 30_000


def test_tiny_sort_memory_limit_does_not_write_thousands_of_runs(make_doc) -> None:
    n = 20_000
    doc = make_doc(raw=b"[" + b",".join(b'{"id":%d}' % ((i * 7919) % n) for i in range(n)) + b"]")
    got, rep = run_sort(doc, ["id"], memory_limit=512)
    assert got == sorted(range(n), key=lambda i: (i * 7919) % n)
    assert rep.strategy == "external" and rep.runs <= rsmod.TARGET_RUNS + 8, rep.runs  # ~2 500 runs before
    assert rep.memory_limit > 512
    # without knowing the number of records the run size grows as well
    got, rep = run_sort(doc, ["id"], source=iter(range(n)), memory_limit=512)
    assert len(got) == n and rep.runs < 2000


def test_sort_runs_are_written_to_the_cache_folder(make_doc, tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    seen: list[Path] = []
    real = rsmod._Sorter._run_path

    def spy(self: Any) -> Path:
        p = real(self)
        seen.append(p)
        return p

    monkeypatch.setattr(rsmod._Sorter, "_run_path", spy)
    doc = make_doc(jsongen.records(500, seed=2), settings_=Settings(cache_dir=str(cache)))
    got, rep = run_sort(doc, ["name"], memory_limit=3000)
    assert rep.runs > 1 and got == _sorted_reference(doc, ["name"])
    assert seen and all(p.parent.parent == work_dir(str(cache)) == cache / "tmp" for p in seen)
    assert not [n for n in os.listdir(cache / "tmp") if n.startswith("sort-")]  # removed afterwards


def test_stale_sort_runs_in_the_cache_folder_are_purged(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    from jsonreshape import settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: Settings(cache_dir=str(cache)))
    monkeypatch.setattr(rsmod, "get_settings", lambda: Settings(cache_dir=str(cache)))
    stale = work_dir(str(cache)) / "sort-999999999-abcd"
    stale.mkdir(parents=True)
    monkeypatch.setattr(rsmod, "_pid_alive", lambda pid: pid != 999999999)
    assert rsmod.purge_stale_files() >= 1 and not stale.exists()


# ==================================================================================
# requirements/F1: the scans check that the file did not change meanwhile
# ==================================================================================
def _change_after_last_block(doc: Document, monkeypatch) -> None:
    real = doc.iter_blocks

    def iter_blocks(*args: Any, **kwargs: Any) -> Any:
        yield from real(*args, **kwargs)
        st = os.stat(doc.source_path)  # another program touches the file after the last read
        os.utime(doc.source_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    monkeypatch.setattr(doc, "iter_blocks", iter_blocks)


@pytest.mark.parametrize("operation", ["filter", "sort", "search", "iter_hits", "schema"])
def test_scans_report_a_file_changed_meanwhile(make_doc, monkeypatch, operation: str) -> None:
    doc = make_doc(jsongen.records(300, seed=3))
    _change_after_last_block(doc, monkeypatch)
    with pytest.raises(FileChangedError):
        if operation == "filter":
            filter_records(doc, "id > 5")
        elif operation == "sort":
            sort_records(doc, ["name"], memory_limit=2000)
        elif operation == "search":
            search(doc, SearchOptions.build("a"))
        elif operation == "iter_hits":
            list(iter_hits(doc, SearchOptions.build("a")))
        else:
            infer_schema(doc, sample=None)
    assert os.listdir(recordsets_dir()) == []
    assert not [n for n in os.listdir(work_dir()) if n.startswith("sort-")]


# ==================================================================================
# blackbox/F10 (schema part): progress
# ==================================================================================
def test_schema_progress_never_exceeds_its_total(make_doc) -> None:
    doc = make_doc(jsongen.records(20_000, seed=5))
    for sample in (2000, 50, None):
        infos: list[Any] = []
        p = Progress(callback=infos.append, min_interval=0)
        infer_schema(doc, sample=sample, progress=p)
        assert infos and p.finished
        assert all(info.total is None or info.done <= info.total for info in infos), sample
        assert infos[-1].done == infos[-1].total


# ==================================================================================
# blackbox/F13, requirements/F19: transliteration
# ==================================================================================
@pytest.mark.parametrize("latin, cyrillic", [
    ("sirk", "цирк"), ("konsert", "концерт"), ("stansiya", "станция"), ("militsiya", "милиция"),
    ("Moʻjiza", "Мўъжиза"), ("Isʼhoq", "Исҳоқ"), ("Ishoq", "Исҳоқ"), ("Is'hoq", "ИСҲОҚ"), ("Asʼhob", "Асҳоб"),
])
def test_translit_official_spellings(latin: str, cyrillic: str) -> None:
    assert TextQuery(latin, translit=True).find_spans(cyrillic) == [(0, len(cyrillic))]
    assert TextQuery(cyrillic, translit=True).find_spans(latin) == [(0, len(latin))]
    assert TextQuery(latin).find_spans(cyrillic) == []


def test_translit_search_through_the_prefilter(make_doc) -> None:
    names = ["Is'hoq", "Исҳоқ", "Ishoq", "Iskandar", "IS`HOQ", "sirk", "tsirk", "цирк"]
    doc = make_doc([{"name": n} for n in names])
    for needle in ("Исҳоқ", "Isʼhoq", "ishoq", "цирк", "sirk"):
        q = TextQuery(needle, translit=True)
        want = [i for i, n in enumerate(names) if q.find_spans(n)]
        res = search(doc, SearchOptions.build(needle, translit=True))
        assert [h.record for h in res.hits] == want, needle
        assert res.hits == reference_search(doc, SearchOptions.build(needle, translit=True))
        res.close()
    assert tm.translit_fold_ascii("IS`HOQ s'h", casefold=True) == "ishoq sh"


# ==================================================================================
# requirements/F20: named filter presets
# ==================================================================================
def test_filter_presets(tmp_path: Path, lang_en) -> None:
    store = FilterPresets(tmp_path / "presets.json")
    assert store.list() == [] and store.names() == []
    store.save("Toshkent", 'city == "Toshkent"')
    store.save("vip", "contains(tags, 'vip')", lang="jmespath")
    got = store.get("toshkent")
    assert got["name"] == "Toshkent" and got["text"] == 'city == "Toshkent"' and got["lang"] == "expr"
    assert isinstance(got["saved_at"], float) and got["saved_at"] > 0
    assert store.names() == ["Toshkent", "vip"]
    store.save("  TOSHKENT ", "id > 5")  # the same name in another case replaces the preset
    assert store.names() == ["TOSHKENT", "vip"] and store.get("Toshkent")["text"] == "id > 5"
    reopened = FilterPresets(tmp_path / "presets.json")
    assert reopened.filter("vip").lang == "jmespath" and reopened.filter("toshkent").matches({"id": 6})
    assert reopened.delete("VIP") and not reopened.delete("vip") and reopened.names() == ["TOSHKENT"]
    with pytest.raises(UserInputError) as info:
        store.get("nope")
    assert info.value.key == "query.preset.not_found" and '"nope"' in info.value.message and info.value.hint
    for bad in ("", "   ", "a\x00b", "x" * 201, None, 5):
        with pytest.raises(UserInputError) as info:
            store.save(bad, "id > 1")  # type: ignore[arg-type]
        assert info.value.key == "query.preset.bad_name"
    with pytest.raises(UserInputError) as info:
        store.save("x", "id > 1", lang="sql")
    assert info.value.key == "query.preset.bad_lang"
    with pytest.raises(UserInputError) as info:
        store.save("x", "  ")
    assert info.value.key == "query.preset.empty"
    with pytest.raises(QuerySyntaxError):
        store.save("x", "id >")
    assert store.names() == ["TOSHKENT"]


def test_filter_presets_default_file_and_corrupt_contents() -> None:
    store = FilterPresets()
    assert store.path == app_home() / "filter_presets.json"
    store.path.write_bytes(b"{broken")
    assert store.list() == []
    store.save("a", "id > 1")
    data = json.loads(store.path.read_bytes().decode("utf-8"))
    assert data["version"] == 1 and [p["name"] for p in data["presets"]] == ["a"]
    store.path.write_bytes(json.dumps({"presets": [{"name": "b", "text": "x > 1"}, "junk", {"name": "", "text": "y"},
                                                    {"name": "B", "text": "dup"}, {"name": "c", "text": "z",
                                                                                   "lang": "sql"}]}).encode())
    assert [(p["name"], p["text"], p["lang"], p["saved_at"]) for p in store.list()] == [("b", "x > 1", "expr", 0.0)]
