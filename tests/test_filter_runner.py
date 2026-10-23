"""filter_records (docs/ARCHITECTURE.md §6, §9; AGENT_PROMPT §6.3, §14 test 18).

The reference ("brute force") reads every record, decodes it with ``codec.loads``
(member records: ``split_member``) and calls ``Filter.matches(value, key=, index=)``.
Both evaluation strategies (``extract``: core.PathExtractor + ``matches_values``;
``parse``: whole records) must give exactly the reference result on every format,
including huge records (streamed with ijson), builder filters and their ``to_text()``
expressions, exact numbers, other sources, progress, ``on_batch`` and cancellation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec, core
from jsonreshape import recordset as rsmod
from jsonreshape.appdirs import recordsets_dir
from jsonreshape.codec import JsonFloat
from jsonreshape.errors import Cancelled, QuerySyntaxError, UserInputError
from jsonreshape.query import Condition, Filter, Group, compile_filter
from jsonreshape.reader import Document, split_member
from jsonreshape.recordset import FilterReport, RecordSet, filter_records
from jsonreshape.settings import Settings
from jsonreshape.tasks import BackgroundTask, CancelToken, Progress

HYP = settings(derandomize=True, database=None, deadline=None, max_examples=40,
               suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])

STRATEGIES = ("extract", "parse")

#: filters over jsongen.rand_record records (both strategies must agree with the reference)
FILTERS = [
    'contact.address.city == "Toshkent"',
    'contact.address.city != "Toshkent"',
    'contact.address.city in ["Samarqand", "Buxoro", "Тошкент"]',
    'contact.address.city is null',
    "contact.address is missing",
    "exists(note)",
    "note is null",
    "score > 0",
    "score >= 1.10",
    "score < -1000 or score > 1e300",
    "score == 12345678901234567890",
    "score == 0",
    "score between -5 and 5",
    "id < 10 or id > 90",
    "not (id > 50)",
    "orders[*].oid > 50",
    "orders[0].sum > 0",
    "orders.1.oid < 30",
    "orders[*].sum == 0",
    "len(orders) >= 2",
    "len(tags) == 0",
    'tags contains "Toshkent"',
    'tags[*] startswith "O"',
    'name contains "osh"',
    'name matches "^[A-Z]"',
    'lower(name) contains "toshkent"',
    'upper(contact.address.city) == "TOSHKENT"',
    'type(score) == "number"',
    'type(extra) == "object"',
    "active == true and score > 0",
    "active",
    "$index < 5 or $index >= 95",
    '$key startswith "id1"',
    "$key == name",
    'contact.* == "+998901234567" or contact.*.city == "Buxoro"',
    "extra.* is null",
    "extra[*][*] == 1",
    'number(contact.phone) > 998900000000',
    'string(score) == "1.10"',
    'orders[*].sum > 100 and contact.address.city == "Тошкент"',
    "**.oid == 7",
    "$.id > 50",
    '$ contains "note"',
    "len($) > 6",
    "missing(contact.address.zip)",
    "empty(tags)",
    "extra is empty",
    "contact.address.zip > 500000",
    "true",
    "false",
    "name == $index",
]

#: filters over arbitrary JSON values (jsongen.records(kind="value"))
VALUE_FILTERS = [
    "$ == 0",
    '$ contains "a"',
    'type($) == "array"',
    "$[0] > 0",
    "$[*] == true",
    "$.* == null",
    "len($) > 2",
    "$ is empty",
    'exists($["id"])',
    "*.*.* != 5",
    '$[*][*] contains "o"',
]

JMESPATH = [
    "contact.address.city == 'Toshkent'",
    "length(orders || `[]`) > `1`",
    "score > `100`",
    "active",
    "contains(tags || `[]`, 'Toshkent')",
]


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []

    def make(recs: list[Any] | None = None, fmt: str = "array", *, style: str = "compact",
             settings_: Settings | None = None, raw: bytes | None = None, index: bool = True, **kw) -> Document:
        p = tmp_path / f"doc{len(docs)}-{fmt}.json"
        if raw is not None:
            p.write_bytes(raw)
        else:
            jsongen.write_file(p, recs or [], fmt, style=style, **kw)
        forced = None if raw is not None else fmt
        rp = ("data",) if forced == "wrapped" else None
        doc = Document.open(p, format=forced, record_path=rp, settings=settings_)
        if index:
            doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


def read_record(doc: Document, fh, i: int) -> tuple[str | None, Any]:
    s, e = doc.record_span(i)
    fh.seek(s)
    buf = fh.read(e - s)
    if doc.member_records:
        key, vo = split_member(buf)
        return key, codec.loads(buf[vo:])
    return None, codec.loads(buf)


def brute(doc: Document, flt: Filter, source: Any = None) -> list[int]:
    """Reference result: parse every record of ``source`` and call Filter.matches."""
    indices = range(doc.record_count) if source is None else list(source)
    out = []
    with open(doc.data_path, "rb") as fh:
        for i in indices:
            key, value = read_record(doc, fh, i)
            if flt.matches(value, key=key, index=i):
                out.append(i)
    return out


def run(doc: Document, flt: Filter | str, **kw) -> tuple[list[int], FilterReport]:
    rs = filter_records(doc, flt, **kw)
    try:
        assert rs.complete and rs.kind == "filter"
        return rs.to_list(), rs.report
    finally:
        rs.close()


def check_all(doc: Document, texts: list[str], lang: str = "expr", *, strategies=STRATEGIES) -> None:
    for text in texts:
        flt = compile_filter(text, lang)
        expected = brute(doc, flt)
        for strategy in strategies:
            got, rep = run(doc, flt, strategy=strategy)
            assert got == expected, (doc.kind, text, strategy, got[:8], expected[:8])
            assert rep.scanned == doc.record_count and rep.matched == len(expected)
            assert rep.skipped_huge == 0 and rep.unparseable == 0 and rep.warnings == ()


# ----------------------------------------------------------------------------------
# reference equality on every format
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, style", [
    ("array", "compact"),
    ("array", "messy_escaped"),
    ("jsonl", "compact"),
    ("keyed", "compact"),
    ("keyed", "pretty2"),
    ("wrapped", "pretty4"),
    ("brace_list", "messy"),
    ("stream", "tab"),
])
def test_filters_equal_reference_on_every_format(make_doc, fmt: str, style: str) -> None:
    doc = make_doc(jsongen.records(100, seed=11), fmt, style=style)
    assert doc.record_count == 100 and doc.kind == fmt
    check_all(doc, FILTERS)


@pytest.mark.parametrize("fmt", ["array", "jsonl", "keyed", "single"])
def test_value_records(make_doc, fmt: str) -> None:
    recs = jsongen.records(80, seed=5, kind="value")
    if fmt == "keyed":
        recs = [r if isinstance(r, dict) else {"v": r} for r in recs]
    doc = make_doc(recs, fmt)
    check_all(doc, VALUE_FILTERS)


def test_keyed_records_with_special_keys(make_doc) -> None:
    keys = ["id0", "kalit bo'shliq", "Тошкент", "dot.key", "", "0", "a/b", "x~y", 'q"uote', "tab\tkey",
            "😀", "id1"]
    recs = [{"id": i, "name": k} for i, k in enumerate(keys)]
    doc = make_doc(recs, "keyed", keys=keys, style="escaped")
    check_all(doc, ['$key startswith "id"', '$key == "Тошкент"', "$key == name", '$key contains "/"',
                    'len($key) == 1', '$key matches "^[0-9]+$"', "id > 3"])


def test_jmespath_filters(make_doc) -> None:
    for fmt in ("array", "keyed"):
        doc = make_doc(jsongen.records(60, seed=8), fmt)
        for text in JMESPATH:
            flt = compile_filter(text, "jmespath")
            expected = brute(doc, flt)
            for strategy in ("auto", "extract", "parse"):
                got, rep = run(doc, flt, strategy=strategy)
                assert got == expected and rep.strategy == "parse", (fmt, text)


def test_jmespath_runtime_errors_do_not_abort_the_filter(make_doc, lang_en) -> None:
    # length() of a number fails on all records but one: they do not match, nothing aborts,
    # and the report names the first record with a localized reason
    recs = jsongen.records(20, seed=8)
    recs[5]["score"] = "abc"
    doc = make_doc(recs)
    got, rep = run(doc, compile_filter("length(score) > `1`", "jmespath"))
    assert got == [5] and rep.eval_errors == 19 and rep.first_error_record == 0
    assert rep.warnings == ("recordset.warn.eval_errors",)
    text = rep.warning_texts()[0]
    assert "19" in text and "#0" in text and "length()" in text and "number" in text
    assert os.listdir(recordsets_dir()) == []
    # failing on every record means the expression is wrong: the filter stops (fix2 N11)
    doc = make_doc(jsongen.records(20, seed=8))
    with pytest.raises(UserInputError) as info:
        run(doc, compile_filter("length(score) > `1`", "jmespath"))
    assert info.value.key == "recordset.error.eval_failed"
    assert "#0" in info.value.message and "length()" in info.value.message and "20" in info.value.message
    assert os.listdir(recordsets_dir()) == []


# ----------------------------------------------------------------------------------
# huge records
# ----------------------------------------------------------------------------------
def huge_records() -> list[dict]:
    recs = jsongen.records(48, seed=21)
    for i in range(0, 48, 3):
        recs[i]["blob"] = ["Toshkent ko'chasi " * 4] * 30
        recs[i]["nested"] = {"deep": {"k": i, "list": list(range(i))}}
    return recs


HUGE_FILTERS = [
    'contact.address.city == "Toshkent"',
    "score > 0",
    "len(blob) == 30",
    "exists(blob)",
    "nested.deep.k > 20",
    "nested.deep.list[*] == 5",
    "**.k == 9",
    "orders[*].oid > 50",
    "$index > 10",
    '$key startswith "id1"',
    "true",
    "tags contains \"Toshkent\" or name contains \"osh\"",
]


@pytest.mark.parametrize("fmt", ["array", "keyed", "wrapped", "jsonl"])
def test_huge_records_are_streamed(make_doc, fmt: str) -> None:
    doc = make_doc(huge_records(), fmt, settings_=Settings(large_node_bytes=1500))
    huge = [i for i in range(doc.record_count) if doc.is_huge(i)]
    assert len(huge) == 16
    for text in HUGE_FILTERS:
        flt = compile_filter(text)
        expected = brute(doc, flt)
        for strategy in STRATEGIES:
            got, rep = run(doc, flt, strategy=strategy)
            assert got == expected, (fmt, text, strategy)
            assert rep.skipped_huge == 0 and rep.scanned == doc.record_count


@pytest.mark.parametrize("text, lang", [("string($) contains \"blob\"", "expr"), ('$ == {"id": 1}', "expr"),
                                        ("score > `0`", "jmespath")])
def test_whole_record_filters_skip_huge_records(make_doc, text: str, lang: str, lang_en) -> None:
    # the whole record as a value (JMESPath, string($), $ == {...}) cannot be evaluated
    # on a record that is never loaded
    doc = make_doc(huge_records(), "array", settings_=Settings(large_node_bytes=1500))
    huge = {i for i in range(doc.record_count) if doc.is_huge(i)}
    flt = compile_filter(text, lang)
    expected = [i for i in brute(doc, flt) if i not in huge]
    got, rep = run(doc, flt)
    assert got == expected
    assert rep.skipped_huge == len(huge) == 16 and rep.strategy == "parse"
    assert rep.warnings == ("recordset.warn.skipped_huge",)
    text_en = rep.warning_texts()[0]
    assert "16" in text_en and "1.5 KB" in text_en


@pytest.mark.parametrize("text", ['$ contains "blob"', "len($) > 3", "len($) == 9", "$ is empty", "exists($)",
                                  "type($) == \"object\""])
def test_whole_record_conditions_are_streamed_on_huge_records(make_doc, text: str) -> None:
    # len($), key tests and types of the record itself are decided on the event stream
    doc = make_doc(huge_records(), "array", settings_=Settings(large_node_bytes=1500))
    flt = compile_filter(text)
    got, rep = run(doc, flt)
    assert got == brute(doc, flt) and rep.skipped_huge == 0 and rep.warnings == ()


# ----------------------------------------------------------------------------------
# builder model (§14 test 18, runner side)
# ----------------------------------------------------------------------------------
BUILDERS = [
    Group(items=[Condition("contact.address.city", "eq", "Toshkent")]),
    Group(op="or", items=[Condition("score", "gt", 1000), Condition("score", "lt", -1000)]),
    Group(items=[Condition("tags", "len_gt", 1),
                 Group(op="or", items=[Condition("active", "eq", True), Condition("note", "is_null")])]),
    Group(items=[Condition("name", "contains", "osh"), Condition("orders[*].oid", "between", 10, 40)], negate=True),
    Group(items=[Condition("contact.address.city", "in", ["Buxoro", "Тошкент"])]),
    Group(items=[Condition("note", "exists")]),
    Group(items=[Condition("contact", "not_exists")]),
    Group(items=[Condition("tags", "is_empty")]),
    Group(items=[Condition("name", "regex", "^[TO]")]),
    Group(items=[Condition("name", "startswith", "O'z")]),
    Group(items=[Condition("$index", "lt", 30), Condition("score", "ge", 0)]),
    Group(items=[Condition("$key", "startswith", "id2")]),
    Group(items=[Condition("score", "eq", JsonFloat("1.10"))]),
    Group(items=[Condition("score", "le", JsonFloat("1e400")), Condition("score", "ne", 0)]),
    Group(items=[Condition("orders[*].sum", "eq", 12345678901234567890)]),
    Group(items=[Condition("", "is_empty")]),
    Group(items=[Condition("orders", "len_ge", 2), Condition("orders[*].sum", "gt", 0)]),
    Group(),
    Group(negate=True, op="or", items=[Group(), Condition("id", "ne", 5)]),
    Condition("id", "ne", 5),
]


@pytest.mark.parametrize("fmt", ["array", "keyed"])
def test_builder_filters_equal_their_text(make_doc, fmt: str) -> None:
    doc = make_doc(jsongen.records(120, seed=31), fmt)
    for model in BUILDERS:
        built = Filter.from_builder(model)
        text = model.to_text()
        parsed = compile_filter(text)
        expected = brute(doc, built)
        assert brute(doc, parsed) == expected, text
        for strategy in STRATEGIES:
            got_built, _ = run(doc, built, strategy=strategy)
            got_text, _ = run(doc, parsed, strategy=strategy)
            assert got_built == got_text == expected, (text, strategy)


# ----------------------------------------------------------------------------------
# strategies, numbers, decoding
# ----------------------------------------------------------------------------------
def test_strategy_selection(make_doc) -> None:
    doc = make_doc(jsongen.records(30, seed=2))
    # auto: extract when possible -- with the Rust core (the pure-Python extractor is
    # slower than parsing whole records)
    rust = core.BACKEND == "rust"
    cases = {"id > 5": True, "**.oid == 7": False, '$ contains "id"': False, "len($) > 3": False,
             "$index < 3": True, "true": True, "$key == 1": True}
    for text, extractable in cases.items():
        _, rep = run(doc, text)
        assert rep.strategy == ("extract" if extractable and rust else "parse"), text
        _, rep = run(doc, text, strategy="extract")
        assert rep.strategy == ("extract" if extractable else "parse"), text  # falls back when impossible
        _, rep = run(doc, text, strategy="parse")
        assert rep.strategy == "parse"
    with pytest.raises(ValueError):
        run(doc, "id > 5", strategy="fast")


NUMBER_TOKENS = ["0.1", "1e400", "-1e400", "12345678901234567890", "12345678901234567891", "-0.0", "1.10", "1.1",
                 "-0", "0", "1E5", "100000", "123456789012345678901234567890", "5e-324", "0.00001", "2.5E+3",
                 "18446744073709551616", "-9223372036854775809", "9007199254740993", "1000000000000000.0"]


def test_exact_numbers(make_doc) -> None:
    raw = b"[" + b",".join(b'{"n":' + t.encode() + b',"t":"' + t.encode() + b'","a":[' + t.encode() + b"]}"
                            for t in NUMBER_TOKENS) + b"]"
    doc = make_doc(raw=raw)
    texts = ["n == 1.1", "n == 1.10", "n == 1e400", "n > 1e399", "n < -1e399", "n == 12345678901234567890",
             "n > 12345678901234567890", "n == 0", "n == -0", "n == 100000", "n < 0.2 and n > 0", 'string(n) == "1.10"',
             "string(n) == t", "n == number(t)", "a contains 1.1", "a[0] == 9007199254740993",
             "n == 9007199254740992", "n between 0.1 and 1.1", "n >= 5e-324 and n <= 0.00001"]
    check_all(doc, texts)
    got, _ = run(doc, "n == 1.1")
    assert got == [NUMBER_TOKENS.index("1.10"), NUMBER_TOKENS.index("1.1")]
    got, _ = run(doc, "n == 0")
    assert got == [NUMBER_TOKENS.index(t) for t in ("-0.0", "-0", "0")]


def same(a: Any, b: Any) -> bool:
    """Equal values with equal types (and equal raw text for exact numbers)."""
    if type(a) is not type(b):
        return False
    if isinstance(b, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in b)
    if isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(b, float) and b != b:
        return a != a
    return a == b and repr(a) == repr(b)


@pytest.mark.parametrize("tok", jsongen.EDGE_NUMBER_TOKENS + NUMBER_TOKENS + [
    '"plain"', '"esc\\n\\u00e9\\ud83d\\ude00"', '"\\/"', '""', '"Тошкент"', "true", "false", "null", "[]", "{}",
    '[1.10, {"a": -0}]', '{"x": 1e400}', "-12", "123456789012345678", "1234567890123456789", "-123456789012345678",
    "1" * 4500, "1." + "5" * 30])
def test_decode_value_equals_codec_loads(tok: str) -> None:
    raw = tok.encode()
    assert same(rsmod._decode_value(raw), codec.loads(raw)), tok


@HYP
@given(seed=st.integers(0, 10**6), style=st.sampled_from(["compact", "escaped"]))
def test_decode_value_random_values(seed: int, style: str) -> None:
    import random

    value = jsongen.rand_value(random.Random(seed), 0, 3)
    raw = jsongen.encode(value, style, seed)
    assert same(rsmod._decode_value(raw), codec.loads(raw))


# ----------------------------------------------------------------------------------
# sources
# ----------------------------------------------------------------------------------
def test_sources(make_doc) -> None:
    doc = make_doc(jsongen.records(200, seed=4))
    flt = compile_filter("score > 0 or name contains \"o\"")
    full = set(brute(doc, flt))
    cases: list[tuple[Any, list[int]]] = [
        (range(50, 150), list(range(50, 150))),
        (range(0, 200, 3), list(range(0, 200, 3))),
        (range(199, -1, -1), list(range(199, -1, -1))),
        ([150, 3, 3, 77, 10, 199, 0], [150, 3, 3, 77, 10, 199, 0]),
        ((5, 4, 3), [5, 4, 3]),
        (iter([9, 1, 8]), [9, 1, 8]),
        (range(0), []),
    ]
    for source, indices in cases:
        expected = [i for i in indices if i in full]
        for strategy in STRATEGIES:
            got, rep = run(doc, flt, source=source if not hasattr(source, "__next__") else iter(indices),
                           strategy=strategy)
            assert got == expected and rep.scanned == len(indices), (indices[:5], strategy)
    with RecordSet.from_indices([7, 100, 3]) as manual:
        got, _ = run(doc, flt, source=manual)
        assert got == [i for i in (7, 100, 3) if i in full]
    base = RecordSet.base(doc)
    got, _ = run(doc, flt, source=base)
    assert got == sorted(full)
    first = filter_records(doc, "id < 100")
    got, rep = run(doc, flt, source=first)
    assert got == [i for i in first if i in full] and rep.scanned == len(first)
    first.close()


def test_invalid_source_index(make_doc) -> None:
    doc = make_doc(jsongen.records(10, seed=4))
    with pytest.raises(IndexError):
        run(doc, "id > 0", source=[1, 50])
    assert os.listdir(recordsets_dir()) == []


def test_filter_text_argument_and_errors(make_doc) -> None:
    doc = make_doc(jsongen.records(20, seed=4))
    got, _ = run(doc, "id >= 15")
    assert got == [15, 16, 17, 18, 19]
    with pytest.raises(TypeError):
        filter_records(doc, 123)  # type: ignore[arg-type]
    with pytest.raises(QuerySyntaxError):
        filter_records(doc, "id >")


# ----------------------------------------------------------------------------------
# growth, progress, cancellation
# ----------------------------------------------------------------------------------
def test_on_batch_reports_the_growing_set(make_doc) -> None:
    doc = make_doc(jsongen.records(3000, seed=6))
    calls: list[tuple[RecordSet, int, bool]] = []
    rs = filter_records(doc, "id >= 0", on_batch=lambda r: calls.append((r, len(r), r.complete)),
                        block_bytes=4096, batch_interval=0)
    assert len(calls) > 3
    assert all(c[0] is rs for c in calls)  # the very object that is returned
    lengths = [c[1] for c in calls]
    assert lengths == sorted(lengths) and len(set(lengths)) == len(lengths)
    assert not any(c[2] for c in calls)
    assert rs.complete and len(rs) == 3000 and rs.to_list() == list(range(3000))
    rs.close()
    quiet: list[Any] = []
    rs2 = filter_records(doc, "id >= 0", on_batch=quiet.append, block_bytes=4096, batch_interval=3600)
    assert quiet == [] and len(rs2) == 3000
    rs2.close()


def test_progress_in_bytes(make_doc) -> None:
    doc = make_doc(jsongen.records(500, seed=6))
    infos = []
    p = Progress(callback=infos.append, min_interval=0)
    got, rep = run(doc, "id > 3", progress=p, block_bytes=2048)
    total = doc.data_size - doc.info.data_start
    assert p.total == total and p.finished and p.done == total and p.unit == "bytes"
    assert p.message and len(infos) > 5
    dones = [i.done for i in infos]
    assert dones == sorted(dones)
    assert 0 < rep.bytes_read <= total and rep.elapsed >= 0
    p2 = Progress(total=123, message="o'zim")
    run(doc, "id > 3", progress=p2)
    assert p2.total == 123 and p2.message == "o'zim"
    p3 = Progress()
    run(doc, "id > 3", progress=p3, source=range(0, 250))
    assert p3.total is not None and p3.finished


def test_cancel_leaves_nothing_behind(make_doc, monkeypatch) -> None:
    monkeypatch.setattr(rsmod, "FLUSH_BYTES", 64)  # the growing result is on disk at once
    doc = make_doc(jsongen.records(3000, seed=6))
    token = CancelToken()
    paths: list[Path | None] = []

    def on_batch(rs: RecordSet) -> None:
        paths.append(rs.path)
        token.cancel()

    with pytest.raises(Cancelled):
        filter_records(doc, "id >= 0", cancel=token, on_batch=on_batch, block_bytes=4096, batch_interval=0)
    assert paths and paths[0] is not None and not paths[0].exists()
    assert os.listdir(recordsets_dir()) == []
    early = CancelToken()
    early.cancel()
    with pytest.raises(Cancelled):
        filter_records(doc, "id > 1", cancel=early)
    assert os.listdir(recordsets_dir()) == []


def test_runs_as_background_task(make_doc) -> None:
    doc = make_doc(jsongen.records(400, seed=6))
    flt = compile_filter('contact.address.city == "Samarqand"')
    task = BackgroundTask(filter_records, doc, flt, name="filter").start()
    rs = task.result(60)
    assert rs.to_list() == brute(doc, flt) and task.progress.finished
    rs.close()


# ----------------------------------------------------------------------------------
# damaged / unusual records and documents
# ----------------------------------------------------------------------------------
def test_unparseable_records_are_skipped_and_counted(make_doc, monkeypatch, lang_en) -> None:
    # record 7 is valid JSON that neither codec.loads nor ijson can decode (simulated; a
    # file changed after indexing is reported as FileChangedError instead, see
    # test_fix_query.py)
    from jsonreshape import stream

    recs = [b'{"id":%d,"v":{"x":"abc"}}' % i for i in range(20)]
    recs[7] = b'{"id":7,"v":{"x":"BAD"}}'
    doc = make_doc(raw=b"[" + b",".join(recs) + b"]")
    bad_span = doc.record_span(7)
    real_loads, real_events = codec.loads, stream._value_events

    def loads(data: Any) -> Any:
        if b"BAD" in (data.encode() if isinstance(data, str) else bytes(data)):
            raise codec.JsonDecodeError("simulated", 0)
        return real_loads(data)

    def events(doc_: Any, span: Any, member: bool) -> Any:
        if tuple(span) == tuple(bad_span):
            raise rsmod.JsonReshapeError("stream.error.parse", detail="simulated")
        return real_events(doc_, span, member)

    monkeypatch.setattr(codec, "loads", loads)
    monkeypatch.setattr(stream, "_value_events", events)
    for text in ('v == {"x": "abc"}', "exists(v)", "$index >= 0"):
        flt = compile_filter(text)
        for strategy in STRATEGIES:
            got, rep = run(doc, flt, strategy=strategy)
            if text == "$index >= 0":  # needs no record data: nothing is decoded
                assert got == list(range(20)) and rep.unparseable == 0
                continue
            want = [i for i in range(20) if i != 7]
            assert got == want and rep.unparseable == 1, (text, strategy)
            assert rep.warnings == ("recordset.warn.unparseable",) and "1" in rep.warning_texts()[0]


def test_records_the_decoder_rejects_are_streamed(make_doc, monkeypatch) -> None:
    """A record (or extracted value) codec.loads cannot decode -- e.g. nested deeper than
    the C stack allows -- is evaluated by streaming it with ijson instead."""
    raw = b'[{"id":0,"x":"a"},{"id":1,"x":"b","deep":{"marker":[1,2]}},{"id":2,"x":"b"}]'
    doc = make_doc(raw=raw)
    real = codec.loads

    def fake(data: Any) -> Any:
        b = data.encode() if isinstance(data, str) else bytes(data)
        if b'"marker"' in b:
            raise codec.JsonDecodeError("simulated: too deep", 0)
        return real(data)

    monkeypatch.setattr(codec, "loads", fake)
    for text, want in (('x == "b"', [1, 2]), ('$ contains "deep"', [1]), ("exists(deep)", [1]),
                       ("deep.marker[1] == 2", [1]), ("id >= 0", [0, 1, 2])):
        for strategy in STRATEGIES:
            got, rep = run(doc, text, strategy=strategy)
            assert got == want and rep.unparseable == 0, (text, strategy)
    got, rep = run(doc, compile_filter("x == 'b'", "jmespath"))
    assert got == [1, 2] and rep.unparseable == 0


@pytest.mark.slow
def test_record_too_deep_for_the_json_parser(make_doc) -> None:
    deep = b"[" * jsongen.TOO_DEEP + b"1" + b"]" * jsongen.TOO_DEEP
    raw = b'[{"id":0,"x":"a"},{"id":1,"x":"b","deep":' + deep + b'},{"id":2,"x":"b"}]'
    doc = make_doc(raw=raw)
    with pytest.raises(codec.JsonDecodeError):
        doc.record(1)
    got, rep = run(doc, 'x == "b"', strategy="extract")  # the deep value is never decoded
    assert got == [1, 2] and rep.unparseable == 0
    got, rep = run(doc, 'x == "b"', strategy="parse")    # record 1 is streamed
    assert got == [1, 2] and rep.unparseable == 0


def test_empty_and_partial_documents(make_doc) -> None:
    empty = make_doc(raw=b"[]")
    got, rep = run(empty, "id > 0")
    assert got == [] and rep.scanned == 0 and rep.matched == 0
    partial = make_doc(raw=b'[{"id":1},{"id":2},{"id":3')
    assert partial.partial and partial.record_count == 2
    for strategy in STRATEGIES:
        got, rep = run(partial, "id > 0", strategy=strategy)
        assert got == [0, 1] and rep.scanned == 2


def test_filter_follows_a_growing_index(make_doc) -> None:
    doc = make_doc(jsongen.records(20000, seed=3), index=False)
    doc.start_index()
    flt = compile_filter("score > 0 and active")
    got, rep = run(doc, flt)
    assert doc.index_complete and rep.scanned == 20000
    assert got == brute(doc, flt)


def test_report_dataclass() -> None:
    rep = FilterReport(1, 2, 3, 4, "parse", 0.5)
    assert (rep.scanned, rep.matched, rep.skipped_huge, rep.unparseable, rep.strategy, rep.elapsed) == \
        (1, 2, 3, 4, "parse", 0.5)
    assert rep.warnings == () and rep.warning_texts() == [] and rep.bytes_read == 0
