"""jsonreshape.schema: structure inference for ``inspect`` and the schema view (AGENT_PROMPT
§4.4, §9 field statistics, §13 ``jsonreshape inspect input.json [--sample 10000]``).

Every statistic is checked against an independent brute-force oracle
(:func:`reference_schema`: a plain recursive walk that keeps every value, record id and
context unit in sets/lists) on generated data in every file format, on samples, on
member records (``$key``) and on huge / big records (tiny ``large_node_bytes`` and
``STREAM_RECORD_BYTES`` -> the walking path must give exactly the same schema; more in
``tests/test_schema_walk.py``). Plus: presence under arrays, mixed types,
exact number ranges, limits, sampling determinism/spread, table rendering (uz/en, wide
characters, truncation), ``to_dict`` round trip, progress and cancellation.
"""

from __future__ import annotations

import io
import json
import random
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any

import ijson
import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec, i18n, reader
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.errors import Cancelled, PathSyntaxError, UserInputError
from jsonreshape.paths import Wild
from jsonreshape.reader import Document
from jsonreshape.schema import (
    EXAMPLE_CHARS,
    MAX_EXAMPLES,
    RECORD_KEY,
    TYPE_NAMES,
    FieldInfo,
    Schema,
    SchemaBuilder,
    example_text,
    infer_schema,
    sample_positions,
)
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

ANY = Wild.ANY_INDEX
KEYP = ("<record key>",)        # oracle name of the $key pseudo-field
BS = "\\"
ELL = "\N{HORIZONTAL ELLIPSIS}"
HYP = hsettings(derandomize=True, database=None, deadline=None, max_examples=60,
                suppress_health_check=[HealthCheck.too_slow])


# ----------------------------------------------------------------------------------
# oracle
# ----------------------------------------------------------------------------------
def _dec(v: Any) -> Decimal:
    return Decimal(codec.number_text(v))


def reference_schema(records: list[Any], keys: list[str] | None = None) -> dict[str, Any]:
    """Brute-force statistics: {"order": pre-order patterns, "fields": {pattern: stats},
    "scanned": n}. A pattern is the path with array indices replaced by ``ANY``; the
    context unit of a value is the concrete path of its nearest array-element ancestor
    (itself excluded), or the record."""
    stats: dict[tuple, dict[str, Any]] = {}
    seen: list[tuple] = []

    def entry(pattern: tuple) -> dict[str, Any]:
        e = stats.get(pattern)
        if e is None:
            e = stats[pattern] = {"count": 0, "records": set(), "units": set(), "types": {}, "examples": [],
                                  "str": [], "num": [], "arr": [], "obj": []}
            seen.append(pattern)
        return e

    def visit(v: Any, pattern: tuple, concrete: tuple, rec: int, own: Any, kid: Any) -> None:
        e = entry(pattern)
        e["count"] += 1
        e["records"].add(rec)
        e["units"].add(own)
        tp = codec.json_type(v)
        e["types"][tp] = e["types"].get(tp, 0) + 1
        if tp == "object":
            e["obj"].append(len(v))
            for k, x in v.items():
                visit(x, pattern + (k,), concrete + (k,), rec, kid, kid)
        elif tp == "array":
            e["arr"].append(len(v))
            for i, x in enumerate(v):
                visit(x, pattern + (ANY,), concrete + (i,), rec, kid, (rec, concrete + (i,)))
        else:
            text = example_text(v)
            if text not in e["examples"] and len(e["examples"]) < MAX_EXAMPLES:
                e["examples"].append(text)
            if tp == "string":
                e["str"].append(len(v))
            elif tp == "number":
                e["num"].append(v)

    for r, v in enumerate(records):
        unit = ("record", r)
        if keys is not None:
            visit(keys[r], KEYP, KEYP, r, unit, unit)
        visit(v, (), (), r, unit, unit)
    scanned = len(records)

    def base_of(pattern: tuple) -> int:
        if pattern == KEYP:
            return scanned
        for cut in range(len(pattern) - 1, 0, -1):
            if pattern[cut - 1] is ANY:
                return stats[pattern[:cut]]["count"]
        return scanned

    fields: dict[tuple, dict[str, Any]] = {}
    for pattern, e in stats.items():
        lo = hi = None
        for v in e["num"]:
            if lo is None:
                lo = hi = v
            elif _dec(v) < _dec(lo):
                lo = v
            elif _dec(v) > _dec(hi):
                hi = v
        base = base_of(pattern)
        present = len(e["units"])
        order = {name: i for i, name in enumerate(TYPE_NAMES)}
        types = dict(sorted(e["types"].items(), key=lambda kv: (-kv[1], order[kv[0]])))
        fields[pattern] = {
            "count": e["count"], "records": len(e["records"]), "present": present, "base": base,
            "presence": present / base if base else 0.0, "types": types, "examples": e["examples"],
            "str_len": [min(e["str"]), max(e["str"])] if e["str"] else None,
            "num_range": [codec.number_text(lo), codec.number_text(hi)] if lo is not None else None,
            "arr_len": [min(e["arr"]), max(e["arr"])] if e["arr"] else None,
            "obj_keys": [min(e["obj"]), max(e["obj"])] if e["obj"] else None,
        }
    # pre-order with children in first-seen order
    children: dict[tuple, list[tuple]] = {}
    for pattern in seen:
        if pattern == ():
            continue
        parent = () if pattern == KEYP else pattern[:-1]
        children.setdefault(parent, []).append(pattern)
    order_list: list[tuple] = []
    stack = list(reversed(children.get((), [])))
    while stack:
        p = stack.pop()
        order_list.append(p)
        stack.extend(reversed(children.get(p, [])))
    return {"order": order_list, "fields": fields, "scanned": scanned}


STAT_KEYS = ("count", "records", "present", "base", "presence", "types", "examples", "str_len", "num_range",
             "arr_len", "obj_keys")


def _pattern(fi: FieldInfo) -> tuple:
    return KEYP if fi.is_key else fi.path


def assert_matches(schema: Schema, ref: dict[str, Any]) -> None:
    assert schema.scanned == ref["scanned"]
    got_order = [_pattern(fi) for fi in schema.fields()]
    assert got_order == ref["order"]
    for fi in [schema.root] + schema.fields():
        want = ref["fields"][_pattern(fi)]
        got = fi.to_dict()
        for k in STAT_KEYS:
            assert got[k] == want[k], (fi.text, k, got[k], want[k])
        assert list(got["types"]) == list(want["types"]), fi.text   # most frequent first


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend):
    """Documents opened in the test use this core backend's Scanner (codec.loads follows
    the process-wide backend: run the suite with JSONRESHAPE_BACKEND=python as well)."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


@pytest.fixture
def open_doc(tmp_path: Path):
    docs: list[Document] = []
    counter = [0]

    def make(data: bytes | list, fmt: str | None = None, *, settings: Settings | None = None,
             record_path: Any = None, format: str | None = None, name: str | None = None,
             build: bool = True, **write_kw: Any) -> Document:
        counter[0] += 1
        p = tmp_path / (name or f"doc{counter[0]}.json")
        if isinstance(data, (bytes, bytearray)):
            p.write_bytes(bytes(data))
        else:
            jsongen.write_file(p, data, fmt, **write_kw)
        doc = Document.open(p, settings=settings, record_path=record_path, format=format)
        docs.append(doc)
        if build:
            doc.build_index()
        return doc

    yield make
    for d in docs:
        d.close()


def jsonl(*values: Any) -> bytes:
    return b"".join(codec.dumps(v) + b"\n" for v in values)


# ----------------------------------------------------------------------------------
# oracle comparisons on generated data, every format
# ----------------------------------------------------------------------------------
FORMAT_CASES = [
    ("array", {}, {}),
    ("array", {"style": "pretty2"}, {}),
    ("array", {"style": "messy_escaped"}, {}),
    ("jsonl", {}, {"format": "jsonl"}),
    ("stream", {"style": "pretty2"}, {"format": "stream"}),
    ("brace_list", {}, {"format": "brace_list"}),
    ("wrapped", {"style": "pretty4"}, {"record_path": "data"}),
    ("wrapped", {"wrapper_path": ("result", "items")}, {"record_path": "result.items"}),
]


@pytest.mark.parametrize("fmt, write_kw, open_kw", FORMAT_CASES)
def test_full_scan_matches_oracle(open_doc, backend, fmt, write_kw, open_kw) -> None:
    recs = jsongen.records(120, seed=11)
    doc = open_doc(recs, fmt, **write_kw, **open_kw)
    assert doc.record_count == len(recs)
    schema = infer_schema(doc)
    assert schema.complete and schema.total_records == len(recs) and schema.skipped == 0
    assert schema.sample == 10000
    assert_matches(schema, reference_schema(recs))


def test_random_values_match_oracle(open_doc) -> None:
    recs = jsongen.records(150, seed=5, kind="value")   # scalars, arrays and objects as records
    doc = open_doc(recs, "jsonl", format="jsonl")
    schema = infer_schema(doc, sample=None)
    assert schema.complete
    assert_matches(schema, reference_schema(recs))
    assert schema.root.types.keys() >= {"object", "array"}


def test_single_document_is_one_record(open_doc) -> None:
    recs = jsongen.records(30, seed=2)
    doc = open_doc(recs, "single", format="single")
    schema = infer_schema(doc)
    assert schema.scanned == 1 and schema.root.types == {"array": 1}
    assert_matches(schema, reference_schema([recs]))
    assert schema.find("[*].id").count == 30


def test_keyed_document_has_key_field(open_doc, backend) -> None:
    recs = jsongen.records(60, seed=4)
    keys = [f"user-{i:03d}" for i in range(60)]
    doc = open_doc(recs, "keyed", keys=keys, format="keyed")
    assert doc.member_records
    schema = infer_schema(doc)
    assert_matches(schema, reference_schema(recs, keys))
    key = schema.fields()[0]
    assert key.is_key and key.text == "$key" and key.path == ("$key",) and key.depth == 1
    assert schema.root.children[RECORD_KEY] is key
    assert key.presence == 1.0 and key.types == {"string": 60}
    assert key.examples == ['"user-000"', '"user-001"', '"user-002"', '"user-003"', '"user-004"']
    assert key.str_len == (8, 8)
    assert schema.find("$key") is key


def test_real_member_named_key_stays_separate(open_doc) -> None:
    data = b'{"a": {"$key": 1, "x": 2}, "b": {"$key": "s"}}'
    doc = open_doc(data, format="keyed")
    schema = infer_schema(doc)
    pseudo, real = schema.find("$key"), schema.find('["$key"]')
    assert pseudo.is_key and pseudo.examples == ['"a"', '"b"']
    assert not real.is_key and real.text == '["$key"]' and real.path == ("$key",)
    assert real.types == {"number": 1, "string": 1}
    assert schema.find('$["$key"]') is real
    assert [fi.text for fi in schema.fields()] == ["$key", '["$key"]', "x"]


def test_wrapped_object_container_gives_member_records(open_doc) -> None:
    data = b'{"meta": {"v": 1}, "data": {"k1": {"n": 1}, "k2": {"n": "two"}, "k3": 7}}'
    doc = open_doc(data, record_path="data")
    assert doc.member_records
    schema = infer_schema(doc)
    assert_matches(schema, reference_schema([{"n": 1}, {"n": "two"}, 7], ["k1", "k2", "k3"]))
    assert schema.root.types == {"object": 2, "number": 1}
    assert schema.find("n").presence == pytest.approx(2 / 3)


# ----------------------------------------------------------------------------------
# presence rules, types, numbers
# ----------------------------------------------------------------------------------
def test_presence_under_arrays(open_doc) -> None:
    recs = [
        {"orders": [{"oid": 1, "sum": 5}, {"oid": 2}]},
        {"orders": []},
        {"x": 1},
        {"orders": [{"oid": 3, "sum": None, "items": [{"sku": "a"}, {}]}]},
    ]
    schema = infer_schema(open_doc(jsonl(*recs), format="jsonl"))
    assert_matches(schema, reference_schema(recs))
    f = schema.find
    assert (f("orders").present, f("orders").base) == (3, 4)                    # records
    assert (f("orders[*]").count, f("orders[*]").present, f("orders[*]").base) == (3, 2, 4)
    assert f("orders[*]").presence == 0.5                                      # records with >= 1 order
    assert f("orders[*].oid").presence == 1.0                                  # of the 3 order elements
    assert (f("orders[*].sum").present, f("orders[*].sum").base) == (2, 3)     # null is present
    assert f("orders[*].sum").types == {"number": 1, "null": 1}
    assert f("orders[*].sum").missing == 1 and f("orders[*].sum").nulls == 1
    assert (f("orders[*].items[*]").present, f("orders[*].items[*]").base) == (1, 3)
    assert (f("orders[*].items[*].sku").present, f("orders[*].items[*].sku").base) == (1, 2)
    assert f("orders").arr_len == (0, 2) and f("orders[*]").obj_keys == (1, 3)
    assert f("x").presence == 0.25 and f("x").records == 1


def test_arrays_of_arrays_and_array_records(open_doc) -> None:
    recs = [{"m": [[1, 2], [], [3]]}, [[1], "s"], {"m": []}]
    schema = infer_schema(open_doc(jsonl(*recs), format="jsonl"))
    assert_matches(schema, reference_schema(recs))
    m2 = schema.find("m[*][*]")
    assert (m2.count, m2.present, m2.base) == (3, 2, 3)
    assert schema.find("[*]").text == "[*]" and schema.find("[*][*]").count == 1
    assert schema.root.types == {"object": 2, "array": 1}


def test_mixed_types_nulls_and_exact_number_range(open_doc) -> None:
    tokens = [b"1", b"1.5", b'"x"', b"null", b"true", b'{"a": 1}', b"[1, 2]", b"12345678901234567890",
              b"1e400", b"-1e400", b"0.1", b"1.10", b"-0.0", b"-0", b"false", b"0.10000000000000000001",
              b"9007199254740993", b"9007199254740992.0", b"1.1", b"1.100"]
    data = b"".join(b'{"v": ' + tok + b"}\n" for tok in tokens)
    schema = infer_schema(open_doc(data, format="jsonl"))
    v = schema.find("v")
    assert v.types == {"number": 14, "bool": 2, "string": 1, "null": 1, "object": 1, "array": 1}
    assert v.num_range == ("-1e400", "1e400")
    assert v.examples == ["1", "1.5", '"x"', "null", "true"]
    assert v.str_len == (1, 1) and v.obj_keys == (1, 1) and v.arr_len == (2, 2)
    assert v.presence == 1.0 and v.main_type == "number"
    assert v.type_fraction("number") == pytest.approx(14 / 20)
    recs = [codec.loads(b'{"v": ' + tok + b"}") for tok in tokens]
    assert_matches(schema, reference_schema(recs))


@pytest.mark.parametrize("tokens, expected", [
    ([b"0.1", b"0.10000000000000000001", b"0.09999999999999999999"], ("0.09999999999999999999",
                                                                       "0.10000000000000000001")),
    ([b"9007199254740992.0", b"9007199254740993", b"9007199254740992"], ("9007199254740992.0",
                                                                         "9007199254740993")),
    ([b"1.10", b"1.1", b"1.100"], ("1.10", "1.10")),                     # ties keep the first text
    ([b"-0", b"0", b"-0.0", b"0.0"], ("-0", "-0")),
    ([b"1e-400", b"0", b"5e-324"], ("0", "5e-324")),
    # the double of 1.8446744073709551615e19 is 2**64: only an exact comparison sees 2**64 above it
    ([b"1.8446744073709551615e19", b"18446744073709551616", b"18446744073709551615"],
     ("1.8446744073709551615e19", "18446744073709551616")),
    ([b"1.8446744073709552e19", b"18446744073709551616"], ("18446744073709551616", "1.8446744073709552e19")),
    ([b"-1E5", b"-100000.5", b"-99999"], ("-100000.5", "-99999")),
])
def test_number_range_is_exact(tokens, expected) -> None:
    b = SchemaBuilder()
    for tok in tokens:
        b.add({"n": codec.loads(tok)})
    assert b.build().find("n").num_range == expected


def test_strings_lengths_in_characters_and_examples() -> None:
    b = SchemaBuilder()
    for s in ["", "a", "😀", "Тошкент", "x" * 100, "a", "b", "c"]:
        b.add(s)
    root = b.build().root
    assert root.str_len == (0, 100)
    assert root.examples == ['""', '"a"', '"😀"', '"Тошкент"', '"' + "x" * 58 + ELL]


# ----------------------------------------------------------------------------------
# example texts
# ----------------------------------------------------------------------------------
def test_example_text_rules() -> None:
    assert example_text("Toshkent") == '"Toshkent"'
    assert example_text('a"b' + BS + "c") == '"a' + BS + '"b' + BS + BS + 'c"'
    assert example_text("tab\tnl\n") == '"tab' + BS + "tnl" + BS + 'n"'
    assert example_text("\x00") == '"' + BS + 'u0000"'
    for cp in (0x7F, 0x85, 0x2028, 0x2029, 0x202E, 0x2066, 0xFEFF, 0xD800):
        assert example_text("a" + chr(cp)) == '"a' + BS + "u%04x" % cp + '"', hex(cp)
    long = example_text("y" * 200)
    assert len(long) == EXAMPLE_CHARS and long.startswith('"yyy') and long.endswith(ELL)
    # never cut inside an escape sequence
    nl = example_text("\n" * 40)
    assert nl == '"' + (BS + "n") * 29 + ELL and len(nl) == EXAMPLE_CHARS
    assert example_text(chr(1) * 30).endswith(BS + "u0001" + ELL)
    assert example_text(12345678901234567890) == "12345678901234567890"
    assert example_text(JsonFloat("1e400")) == "1e400"
    assert example_text(JsonInt("-0")) == "-0"
    assert example_text(codec.loads(b"1.10")) == "1.10"
    assert example_text(10 ** 80).endswith(ELL) and len(example_text(10 ** 80)) == EXAMPLE_CHARS
    assert example_text(True) == "true" and example_text(False) == "false" and example_text(None) == "null"
    assert example_text(Decimal("2.50")) == "2.50"
    assert example_text({}) is None and example_text([1]) is None
    assert example_text("abcdef", limit=4) == '"ab' + ELL


def test_examples_first_five_distinct_in_order() -> None:
    b = SchemaBuilder()
    for v in [3, 3, "3", None, None, True, 3.0, False, 7, 8]:
        b.add({"f": v})
    assert b.build().find("f").examples == ["3", '"3"', "null", "true", "3.0"]
    b = SchemaBuilder()
    for i in range(300):   # many long strings sharing a 64-character prefix: one example text
        b.add("p" * 70 + str(i))
    b.add("q")
    assert b.build().root.examples == ['"' + "p" * 58 + ELL, '"q"']


# ----------------------------------------------------------------------------------
# sampling
# ----------------------------------------------------------------------------------
def test_sample_positions() -> None:
    assert sample_positions(1000, 10) == [0, 111, 222, 333, 444, 555, 666, 777, 888, 999]
    assert sample_positions(10, 10) == list(range(10))
    assert sample_positions(5, 10) == list(range(5))
    assert sample_positions(0, 10) == []
    assert sample_positions(1000, 1) == [0]
    assert sample_positions(1000, 2) == [0, 999]
    assert sample_positions(7, None) == list(range(7))
    for n, k in [(11, 10), (10001, 10000), (12345, 77), (2, 2), (3, 2), (99, 98)]:
        pos = sample_positions(n, k)
        assert len(pos) == k and pos[0] == 0 and pos[-1] == n - 1
        assert all(b > a for a, b in zip(pos, pos[1:]))
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        assert max(gaps) - min(gaps) <= 1   # evenly spread


def test_sampled_records_and_determinism(open_doc, backend) -> None:
    recs = jsongen.records(500, seed=21)
    doc = open_doc(recs, "array")
    a = infer_schema(doc, sample=37)
    b = infer_schema(doc, sample=37)
    assert not a.complete and a.scanned == 37 and a.total_records == 500 and a.sample == 37
    picked = [recs[p] for p in sample_positions(500, 37)]
    assert_matches(a, reference_schema(picked))
    da, db = a.to_dict(), b.to_dict()
    da.pop("elapsed"), db.pop("elapsed")
    assert da == db
    ids = a.find("id")
    assert ids.num_range == ("0", "499")          # first and last record included
    assert ids.examples == ["0", "13", "27", "41", "55"]
    assert infer_schema(doc, sample=500).complete and infer_schema(doc, sample=10 ** 9).scanned == 500


def test_sources_range_list_recordset_and_iterable(open_doc) -> None:
    recs = [{"id": i, "half": i % 2} for i in range(1000)]
    doc = open_doc(jsonl(*recs), format="jsonl")
    s = infer_schema(doc, range(100, 200), sample=None)
    assert s.complete and s.total_records == 100 and s.find("id").num_range == ("100", "199")

    order = list(range(999, -1, -3))           # a "sorted" subset, not in file order
    s = infer_schema(doc, order, sample=None)
    assert s.scanned == len(order) and s.find("id").examples[:3] == ["999", "996", "993"]

    class FakeRecordSet:
        def __init__(self, idx: list[int]) -> None:
            self.idx = idx

        def __len__(self) -> int:
            return len(self.idx)

        def __getitem__(self, i: int) -> int:
            return self.idx[i]

        def iter_chunks(self, size: int = 65536):
            for i in range(0, len(self.idx), size):
                yield self.idx[i:i + size]

    rs = FakeRecordSet(order)
    s = infer_schema(doc, rs, sample=5)
    want = [order[p] for p in sample_positions(len(order), 5)]
    assert s.scanned == 5 and s.find("id").examples == [str(i) for i in want]

    gen = infer_schema(doc, (i for i in range(1000) if i % 2 == 0), sample=50)
    assert gen.scanned == 50 and gen.total_records == 500 and not gen.complete
    assert gen.find("id").num_range == ("0", "998") and gen.find("half").types == {"number": 50}
    small = infer_schema(doc, iter([5, 6, 7]), sample=50)
    assert small.complete and small.total_records == 3
    every = infer_schema(doc, iter(range(10)), sample=None)
    assert every.complete and every.scanned == every.total_records == 10


@pytest.mark.parametrize("n, k", [(1, 1), (5, 3), (100, 10), (1000, 7), (12345, 100), (4097, 1)])
def test_iterable_thinning_is_spread_and_bounded(n, k) -> None:
    from jsonreshape.schema import _sample_iterable

    chosen, total = _sample_iterable(iter(range(n)), k, None)
    assert total == n and len(chosen) == min(n, k)
    assert chosen[0] == 0 and chosen[-1] == n - 1 or len(chosen) == 1
    assert chosen == sorted(set(chosen))
    if n > k > 1:
        ideal = (n - 1) / (k - 1)
        gaps = [b - a for a, b in zip(chosen, chosen[1:])]
        assert max(gaps) <= 2 * ideal + 1 and min(gaps) >= 1


@pytest.fixture
def small_read_ahead(monkeypatch):
    """Make the read-ahead path (normally >= 1024 sampled records) run on small samples."""
    from jsonreshape import schema as schema_mod

    monkeypatch.setattr(schema_mod, "PREFETCH_MIN_RECORDS", 10)
    monkeypatch.setattr(schema_mod, "PREFETCH_CHUNK_RECORDS", 7)
    return schema_mod


def test_read_ahead_matches_oracle_in_source_order(open_doc, small_read_ahead) -> None:
    recs = jsongen.records(600, seed=31)
    order = list(range(599, -1, -1))                    # e.g. a RecordSet sorted descending
    for settings in (None, Settings(large_node_bytes=150)):   # with and without huge records
        doc = open_doc(recs, "array", settings=settings)
        s = infer_schema(doc, order, sample=250)
        assert s.scanned == 250 and s.skipped == 0
        assert_matches(s, reference_schema([recs[order[p]] for p in sample_positions(600, 250)]))


def test_read_ahead_cancel_and_errors(open_doc, small_read_ahead) -> None:
    doc = open_doc(jsonl(*[{"i": i} for i in range(3000)]), format="jsonl")
    token = CancelToken()
    prog = Progress(callback=lambda info: token.cancel() if info.done else None, min_interval=0)
    with pytest.raises(Cancelled):
        infer_schema(doc, sample=500, progress=prog, cancel=token)
    with pytest.raises(IndexError):
        infer_schema(doc, list(range(2990, 3010)), sample=15)
    assert infer_schema(doc, sample=500).scanned == 500     # the shared pool still works


def test_sampling_reads_only_the_sampled_records(open_doc) -> None:
    recs = [{"id": i, "pad": "z" * 200} for i in range(4000)]
    doc = open_doc(jsonl(*recs), format="jsonl")
    prog = Progress(min_interval=0)
    s = infer_schema(doc, sample=20, progress=prog)
    assert s.scanned == 20
    record_bytes = len(codec.dumps(recs[0])) + 4
    assert prog.info().done <= 20 * record_bytes   # far-apart records: no gap bytes read
    assert prog.finished


# ----------------------------------------------------------------------------------
# huge / big records (walking path)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("walk_limit", [None, 64])
@pytest.mark.parametrize("fmt, open_kw", [("array", {}), ("jsonl", {"format": "jsonl"}),
                                          ("keyed", {"format": "keyed"})])
def test_huge_records_stream_to_the_same_schema(open_doc, monkeypatch, fmt, open_kw, walk_limit) -> None:
    from jsonreshape import schema as schema_mod

    recs = jsongen.records(80, seed=8)
    keys = [f"k{i}" for i in range(len(recs))] if fmt == "keyed" else None
    normal = infer_schema(open_doc(recs, fmt, keys=keys, **open_kw))
    if walk_limit is not None:     # values above 64 bytes are walked child by child
        monkeypatch.setattr(schema_mod, "STREAM_RECORD_BYTES", walk_limit)
    huge_doc = open_doc(recs, fmt, keys=keys, settings=Settings(large_node_bytes=120), **open_kw)
    assert any(huge_doc.is_huge(i) for i in range(len(recs)))
    assert not all(huge_doc.is_huge(i) for i in range(len(recs)))   # a mix of both paths
    streamed = infer_schema(huge_doc)
    assert streamed.skipped == 0 and streamed.complete
    d1, d2 = normal.to_dict(), streamed.to_dict()
    d1.pop("elapsed"), d2.pop("elapsed")
    assert d1 == d2
    assert_matches(streamed, reference_schema(recs, keys))


@pytest.mark.parametrize("walk_limit", [None, 4])
def test_huge_record_walk_has_no_stream_deviations(open_doc, monkeypatch, walk_limit) -> None:
    """R2-3 / R2-11: a huge record is analysed exactly like a parsed one -- number texts
    (``1e400``, ``-0``, ``0.0000001``), lone surrogate escapes and duplicate keys (the last
    value at the first key's position) included. (The former ijson streaming path deviated
    in all of these -- the old test asserted those deviations.)"""
    from jsonreshape import schema as schema_mod

    data = (b'[{"n": 1e400, "z": -0, "d": {"a": 1, "b": [1], "a": 2}, "s": "x\\udc00y", "t": "\\ud800\\ud800"},'
            b' {"n": 5, "e": 0.0000001}]')
    normal = infer_schema(open_doc(data))
    if walk_limit is not None:
        monkeypatch.setattr(schema_mod, "STREAM_RECORD_BYTES", walk_limit)
    doc = open_doc(data, settings=Settings(large_node_bytes=10))
    s = infer_schema(doc)
    assert s.scanned == 2 and s.skipped == 0
    assert s.find("n").num_range == ("5", "1e400")
    assert s.find("z").examples == ["-0"] and s.find("e").examples == ["0.0000001"]
    assert s.find("d.a").count == 1 and s.find("d.a").examples == ["2"] and s.find("d").obj_keys == (2, 2)
    assert [fi.text for fi in s.fields()][:5] == ["n", "z", "d", "d.a", "d.b"]
    assert s.find("s").examples == ['"x\\udc00y"'] and s.find("t").examples == ['"\\ud800\\ud800"']
    d1, d2 = normal.to_dict(), s.to_dict()
    d1.pop("elapsed"), d2.pop("elapsed")
    assert d1 == d2


def test_deeply_nested_record_is_streamed_and_truncated(open_doc, monkeypatch) -> None:
    from jsonreshape import schema as schema_mod

    streamed = []
    real = schema_mod._stream_into
    monkeypatch.setattr(schema_mod, "_stream_into", lambda *a: streamed.append(a[2]) or real(*a))
    depth = jsongen.TOO_DEEP      # below the scanner's depth limit, beyond what codec.loads decodes
    data = b'[{"a": 1}, ' + b"[" * depth + b"1" + b"]" * depth + b"]"
    doc = open_doc(data)
    s = infer_schema(doc)
    assert streamed == [doc.record_span(1)]       # decoding failed: the record was streamed
    assert s.scanned == 2 and s.skipped == 0 and s.truncated
    assert max(fi.depth for fi in s.fields()) == 128
    assert s.root.types == {"object": 1, "array": 1}
    assert "[*]" in s.format_table(width=60)


@pytest.mark.parametrize("fmt, open_kw", [("array", {}), ("jsonl", {"format": "jsonl"}),
                                          ("keyed", {"format": "keyed"})])
def test_big_records_are_streamed_not_parsed(open_doc, monkeypatch, fmt, open_kw) -> None:
    """P-F2: a record larger than STREAM_RECORD_BYTES (but below large_node_bytes) is
    streamed (codec.loads of a big record needs ~25x its size) -- same schema."""
    from jsonreshape import schema as schema_mod

    recs = jsongen.records(60, seed=12)
    keys = [f"k{i}" for i in range(len(recs))] if fmt == "keyed" else None
    normal = infer_schema(open_doc(recs, fmt, keys=keys, **open_kw))
    doc = open_doc(recs, fmt, keys=keys, **open_kw)
    assert not any(doc.is_huge(i) for i in range(len(recs)))
    sizes = [doc.record_span(i)[1] - doc.record_span(i)[0] for i in range(len(recs))]
    limit = sorted(sizes)[len(sizes) // 2]
    monkeypatch.setattr(schema_mod, "STREAM_RECORD_BYTES", limit)
    streamed: list = []
    real = schema_mod._stream_into
    monkeypatch.setattr(schema_mod, "_stream_into", lambda *a: streamed.append(a[2]) or real(*a))
    loaded: list[int] = []
    real_loads = codec.loads
    monkeypatch.setattr(codec, "loads", lambda data, *a, **k: loaded.append(len(data)) or real_loads(data, *a, **k))
    s = infer_schema(doc)
    big = [doc.record_span(i) for i in range(len(recs)) if sizes[i] > limit]
    assert big and streamed == big
    assert loaded and max(loaded) <= limit + 2       # nothing big went through codec.loads
    d1, d2 = normal.to_dict(), s.to_dict()
    d1.pop("elapsed"), d2.pop("elapsed")
    assert d1 == d2
    assert_matches(s, reference_schema(recs, keys))


def test_big_single_document_schema_memory(open_doc) -> None:
    """P-F2: the schema of a ~6 MB single document (one big record) costs a small multiple
    of its size in Python memory (parsing it took ~25x: a 45 MB document +1.1 GB)."""
    import tracemalloc

    n = 150_000
    data = b'{"a":[' + b",".join(b'{"k":%d,"s":"x"}' % (i % 10) for i in range(n)) + b'],"b":{"c":[1,2]}}'
    doc = open_doc(data, format="single")
    tracemalloc.start()
    try:
        s = infer_schema(doc)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert s.scanned == 1 and s.find("a[*].k").count == n and s.find("b.c[*]").count == 2
    assert peak < 3 * len(data), (peak, len(data))


def test_failed_huge_record_is_skipped_without_partial_counts(open_doc, monkeypatch, caplog) -> None:
    """A huge record that cannot be analysed after all leaves nothing behind. (A 5000-digit
    integer -- what ijson refused -- is decoded like the parsed path does it now:
    ``codec.loads`` keeps its text; the failure is injected instead.)"""
    from jsonreshape import schema as schema_mod

    big = b"1" * 5000
    data = b'[{"a": 1}, {"a": 2, "b": ' + big + b'}, {"a": 3}]'
    doc = open_doc(data, settings=Settings(large_node_bytes=100))
    s = infer_schema(doc)
    assert s.scanned == 3 and s.skipped == 0
    assert s.find("b").num_range == ("1" * 5000, "1" * 5000)
    assert infer_schema(open_doc(data)).find("b").num_range == s.find("b").num_range
    # a record that fails half-way: "a", "c", "c.x" were counted when "BOOM" fails to decode
    # (8 bytes: the 6-byte token "BOOM" is decoded whole -- a longer string would be
    # decoded piece by piece, and its pieces do not contain the marker)
    monkeypatch.setattr(schema_mod, "STREAM_RECORD_BYTES", 8)
    real_loads = codec.loads

    def loads(d: Any, *a: Any, **k: Any) -> Any:
        if b"BOOM" in bytes(d):
            raise codec.JsonDecodeError("boom")
        return real_loads(d, *a, **k)

    monkeypatch.setattr(codec, "loads", loads)
    data = b'[{"a": 1}, {"a": "two", "c": {"x": [1, 2], "y": "BOOM"}, "z": 1}, {"a": 3}]'
    doc = open_doc(data, settings=Settings(large_node_bytes=10))
    s = infer_schema(doc)
    assert s.scanned == 2 and s.skipped == 1 and not s.complete and s.total_records == 3
    assert s.find("a").count == 2 and s.find("a").types == {"number": 2} and s.find("a").presence == 1.0
    assert s.find("c") is None and s.find("z") is None and [fi.text for fi in s.fields()] == ["a"]
    assert s.find("a").examples == ["1", "3"] and s.find("a").num_range == ("1", "3")
    assert "skipped" in caplog.text


# ----------------------------------------------------------------------------------
# streaming builder == in-memory builder (property)
# ----------------------------------------------------------------------------------
_keys = st.sampled_from(["a", "b", "c", "id", "x y", "Тошкент", "", "$key", "*", "0"])
_scalars = st.one_of(st.none(), st.booleans(), st.integers(-10 ** 25, 10 ** 25), st.text(max_size=12),
                     st.floats(allow_nan=False, allow_infinity=False).map(lambda f: round(f, 4)))
_values = st.recursive(_scalars, lambda inner: st.one_of(st.lists(inner, max_size=4),
                                                         st.dictionaries(_keys, inner, max_size=4)),
                       max_leaves=25)


def _events(value: Any, key: str | None = None) -> list:
    data = codec.dumps(value)
    if key is not None:
        data = b"{" + codec.dumps(key) + b":" + data + b"}"
    return list(ijson.basic_parse(io.BytesIO(data), use_float=False))


def _norm(d: dict) -> dict:
    """Number texts compared by value (Decimal writes exponents differently)."""
    def fix(text: str) -> Any:
        try:
            return ("num", Decimal(text))
        except ArithmeticError:
            return text
    for f in [d["root"]] + d["fields"]:
        if f["num_range"]:
            f["num_range"] = [fix(x) for x in f["num_range"]]
        f["examples"] = [fix(x) for x in f["examples"]]
    d.pop("elapsed")
    return d


@HYP
@given(st.lists(_values, min_size=1, max_size=6), st.booleans())
def test_events_and_values_give_the_same_schema(values, member) -> None:
    values = [codec.loads(codec.dumps(v)) for v in values]   # exactly what a file would give
    a, b = SchemaBuilder(), SchemaBuilder()
    for i, v in enumerate(values):
        key = f"k{i}" if member else None
        a.add(v, key)
        b.add_events(_events(v, key), member=member)
    assert _norm(a.build().to_dict()) == _norm(b.build().to_dict())


@HYP
@given(st.lists(_values, min_size=1, max_size=8), st.integers(0, 8))
def test_merge_equals_sequential_adds(values, cut) -> None:
    whole, first, second = SchemaBuilder(), SchemaBuilder(), SchemaBuilder()
    for i, v in enumerate(values):
        whole.add(v)
        (first if i < cut else second).add(v)
    first.merge(second)
    d1, d2 = whole.build().to_dict(), first.build().to_dict()
    assert d1 == d2


# ----------------------------------------------------------------------------------
# limits
# ----------------------------------------------------------------------------------
def test_key_pool_for_many_distinct_keys() -> None:
    b = SchemaBuilder(max_keys=3)
    for r in range(4):
        b.add({"m": {f"k{r}{j}": j for j in range(5)}, "z": 1})
    s = b.build()
    texts = [fi.text for fi in s.fields()]
    assert texts == ["m", "m.k00", "m.k01", "m.k02", "m.*", "z"]
    pool = s.find("m.*")
    assert pool.path == ("m", Wild.ANY_KEY) and pool.count == 2 + 3 * 5 and pool.present == 4
    assert not s.truncated


def test_field_and_depth_limits_set_truncated() -> None:
    b = SchemaBuilder(max_fields=3)
    b.add({"a": 1, "b": {"c": 2, "d": 3}, "e": [1]})
    s = b.build()
    assert s.truncated and [fi.text for fi in s.fields()] == ["a", "b", "b.c"]
    assert s.find("b").obj_keys == (2, 2)
    b = SchemaBuilder(max_depth=2)
    b.add({"a": {"b": {"c": 1}}, "l": [[1]]})
    s = b.build()
    assert s.truncated and max(fi.depth for fi in s.fields()) == 2
    assert s.find("a.b").obj_keys == (1, 1) and s.find("l[*]").arr_len == (1, 1)
    with pytest.raises(ValueError):
        SchemaBuilder(max_fields=0)


def test_builder_accepts_python_values() -> None:
    from collections import OrderedDict

    b = SchemaBuilder()
    b.add(OrderedDict([("t", (1, 2)), ("d", Decimal("1.50")), (5, "five")]))
    s = b.build()
    assert s.find("t").types == {"array": 1} and s.find("t[*]").num_range == ("1", "2")
    assert s.find("d").num_range == ("1.50", "1.50") and s.find('["5"]').examples == ['"five"']
    with pytest.raises(TypeError):
        SchemaBuilder().add({"x": object()})


# ----------------------------------------------------------------------------------
# find, to_dict
# ----------------------------------------------------------------------------------
def test_find_accepts_path_syntaxes() -> None:
    b = SchemaBuilder()
    b.add({"orders": [{"oid": 1}], "a b": {"c": True}, "*": 1})
    s = b.build()
    oid = s.find("orders[*].oid")
    for text in ["orders[0].oid", "orders[].oid", "$.orders[*].oid", "/orders/0/oid", "orders.0.oid",
                 " orders[*].oid "]:
        assert s.find(text) is oid, text
    assert s.find('["a b"].c').text == '["a b"].c' and s.find("/a b/c") is s.find('["a b"].c')
    assert s.find('["*"]').path == ("*",) and s.find("*") is None
    assert s.find("$") is s.root and s.find("") is s.root
    assert s.find("missing") is None and s.find("orders[*].oid.deeper") is None
    assert s.find("..oid") is None
    with pytest.raises(PathSyntaxError):
        s.find("orders[")


def test_to_dict_round_trip(open_doc) -> None:
    recs = jsongen.records(80, seed=13)
    keys = [f"id{i}" for i in range(80)]
    doc = open_doc(recs, "keyed", keys=keys, format="keyed")
    b = SchemaBuilder(max_keys=4)
    b.add({"m": {f"k{j}": j for j in range(9)}})
    for schema in (infer_schema(doc, sample=50), b.build()):
        d = schema.to_dict()
        text = json.dumps(d, ensure_ascii=False)
        back = Schema.from_dict(json.loads(text))
        assert back.to_dict() == d
        assert [fi.text for fi in back.fields()] == [fi.text for fi in schema.fields()]
        assert [fi.path for fi in back.fields()] == [fi.path for fi in schema.fields()]
        for fi in schema.fields():
            assert back.find(fi.text).to_dict() == fi.to_dict()
        assert back.format_table() == schema.format_table()
    # numbers of the data appear only as exact texts
    nr = Schema.from_dict(json.loads(json.dumps(infer_schema(
        open_doc(b'[{"v": 1e400}, {"v": 12345678901234567890}]')).to_dict())))
    assert nr.find("v").num_range == ("12345678901234567890", "1e400")
    with pytest.raises(ValueError):
        Schema.from_dict({**d, "version": 99})


# ----------------------------------------------------------------------------------
# table
# ----------------------------------------------------------------------------------
def _cells(s: str) -> int:
    """Independent terminal-width count."""
    n = 0
    for ch in s:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _table_schema() -> Schema:
    b = SchemaBuilder()
    b.add({"name": "Ali", "名前": "山田", "emoji😀": 1, "city": "Toshkent", "n": None, "e": ""})
    b.add({"name": "Vali", "名前": "太郎", "city": None, "n": 2.5, "e": "x" * 80})
    return b.build()


def test_table_uz_and_en(lang_uz) -> None:
    s = _table_schema()
    uz = s.format_table()
    head = uz.splitlines()[0]
    for word in ("Yo'l", "Turlar", "Mavjudlik", "Misollar"):
        assert word in head
    assert "satr 100%" in uz and "son 50%, null 50%" in uz and "satr 50%, null 50%" in uz
    i18n.set_language("en")
    en = s.format_table()
    for word in ("Path", "Types", "Presence", "Examples"):
        assert word in en.splitlines()[0]
    assert "string 100%" in en and "number 50%, null 50%" in en
    assert '"Ali", "Vali"' in en


def _char_at(line: str, col: int) -> str:
    """The character drawn at terminal column ``col`` ("" past the end)."""
    pos = 0
    for ch in line:
        w = _cells(ch)
        if w and pos <= col < pos + w:
            return ch
        pos += w
    return ""


def test_table_alignment_with_wide_characters(lang_en) -> None:
    s = _table_schema()
    rows_by_text = {fi.text: fi for fi in s.fields()}
    assert {"名前", "emoji😀"} <= set(rows_by_text)
    for width in (None, 200, 70, 40):
        lines = s.format_table(width=width).splitlines()
        header, rule, rows = lines[0], lines[1], lines[2:]
        assert len(rows) == len(s.fields())
        # column starts/ends in terminal cells, taken from the "----" rule line
        runs, pos = [], 0
        for part in rule.split("  "):
            runs.append((pos, pos + len(part)))
            pos += len(part) + 2
        (p0, p1), (t0, t1), (r0, r1), (e0, _) = runs
        assert _cells(header[:header.index("Presence") + len("Presence")]) == r1   # right-aligned header
        for line, fi in zip(rows, s.fields()):
            assert _char_at(line, 0) != " " and _char_at(line, t0) != " ", line
            assert _char_at(line, t0 - 1) == " " and _char_at(line, t0 - 2) == " ", line
            assert _char_at(line, r1 - 1) == "%", line          # presence ends at the column end
            assert _char_at(line, r1) in ("", " "), line
            if fi.examples:
                assert _char_at(line, e0) not in ("", " "), line
        if width is not None:
            assert all(_cells(line) <= width for line in lines), width
    # a wide path is padded by its terminal width, not by its length
    line = next(ln for ln in s.format_table().splitlines() if ln.startswith("名前"))
    assert _char_at(line, 4) == " "


def test_table_truncates_with_ellipsis_and_limits_examples(lang_en) -> None:
    b = SchemaBuilder()
    for i in range(6):
        b.add({"averyveryveryveryveryverylongfieldname": f"value number {i} " + "w" * 30})
    s = b.build()
    wide = s.format_table(max_examples=2)
    assert wide.count('"value number') == 2
    narrow = s.format_table(width=50, max_examples=5)
    for line in narrow.splitlines():
        assert _cells(line) <= 50
    assert ELL in narrow
    none = s.format_table(max_examples=0)
    assert '"value' not in none


def test_table_shows_control_and_bidi_characters_escaped(lang_en) -> None:
    b = SchemaBuilder()
    b.add({"a" + chr(1) + "b": "x" + chr(0x202E) + "y", "c" + chr(0x9B): None})
    s = b.build()
    table = s.format_table()
    assert "a" + BS + "u0001b" in table and "c" + BS + "u009b" in table
    assert '"x' + BS + 'u202ey"' in table
    assert not any(ch in table for ch in (chr(1), chr(0x9B), chr(0x202E)))
    assert s.find("a" + chr(1) + "b") is not None      # FieldInfo.text itself is unchanged


def test_table_root_row_and_empty_documents(open_doc, lang_en) -> None:
    s = infer_schema(open_doc(jsonl(1, "a", [1]), format="jsonl"))
    lines = s.format_table().splitlines()
    assert lines[2].startswith("$ ") and "number" in lines[2] and lines[3].startswith("[*]")
    empty = infer_schema(open_doc(b"[]"))
    assert empty.scanned == 0 and empty.total_records == 0 and empty.complete and empty.fields() == []
    assert empty.format_table().splitlines()[-1] == "No records were analysed."
    objs = infer_schema(open_doc(b"[{}, {}]"))
    assert objs.format_table().splitlines()[-1] == "The records have no fields."
    assert objs.root.obj_keys == (0, 0)


def test_summary_texts(open_doc, lang_en) -> None:
    doc = open_doc(jsonl(*[{"i": i} for i in range(30)]), format="jsonl")
    assert infer_schema(doc).summary().startswith("All records were analysed: 30 (")
    assert infer_schema(doc, sample=7).summary().startswith("Sample: 7 of 30 records analysed (")
    i18n.set_language("uz")
    assert "30 ta yozuvdan 7 tasi" in infer_schema(doc, sample=7).summary()
    i18n.set_language("en")
    b = SchemaBuilder(max_fields=1)
    b.add({"a": 1, "b": 2})
    text = b.build(skipped=2).summary()
    assert text.startswith("Sample: 1 of 3 records analysed (")
    assert "2 records could not be analysed" in text and "fields beyond 1" in text


# ----------------------------------------------------------------------------------
# document handling, progress, cancellation, errors
# ----------------------------------------------------------------------------------
def test_index_is_built_when_needed_with_progress(open_doc) -> None:
    recs = [{"i": i, "s": "q" * (i % 7)} for i in range(3000)]
    doc = open_doc(jsonl(*recs), format="jsonl", build=False)
    assert not doc.index_complete
    infos = []
    prog = Progress(callback=infos.append, min_interval=0)
    s = infer_schema(doc, sample=100, progress=prog)
    assert doc.index_complete and s.scanned == 100 and s.total_records == 3000
    fractions = [i.fraction for i in infos if i.fraction is not None]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert prog.finished and prog.message


@pytest.mark.parametrize("write_kw", [{"gz": True}, {"encoding": "utf-16-le", "bom": True}, {"bom": True}])
def test_prepared_inputs_give_the_same_schema(open_doc, write_kw) -> None:
    recs = jsongen.records(40, seed=17)
    plain = infer_schema(open_doc(recs, "array")).to_dict()
    name = "doc.json.gz" if write_kw.get("gz") else None
    other = infer_schema(open_doc(recs, "array", name=name, **write_kw)).to_dict()
    plain.pop("elapsed"), other.pop("elapsed")
    assert plain == other


def test_jsonl_broken_lines_are_not_records(open_doc) -> None:
    doc = open_doc(b'{"a": 1}\n{"a": \n{"a": "x", "b": [1]}\nnot json\n', format="jsonl")
    assert doc.broken_count == 2
    s = infer_schema(doc)
    assert s.scanned == 2 and s.complete and s.find("a").types == {"number": 1, "string": 1}
    assert s.find("b").presence == 0.5


def test_runs_as_background_task(open_doc) -> None:
    from jsonreshape.tasks import BackgroundTask

    doc = open_doc(jsonl(*[{"i": i, "t": [i]} for i in range(2000)]), format="jsonl")
    task = BackgroundTask(infer_schema, doc, name="schema", sample=100).start()
    s = task.result(timeout=60)
    assert s.scanned == 100 and s.find("t[*]").count == 100
    assert task.progress.finished and task.progress.info().fraction == 1.0


def test_partial_document_uses_records_before_the_error(open_doc) -> None:
    doc = open_doc(b'[{"a": 1}, {"a": 2}, {"a": ')
    s = infer_schema(doc)
    assert doc.partial and s.scanned == 2 and s.find("a").count == 2


def test_cancel(open_doc) -> None:
    doc = open_doc(jsonl(*[{"i": i} for i in range(5000)]), format="jsonl")
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        infer_schema(doc, cancel=token)
    token = CancelToken()
    prog = Progress(callback=lambda info: token.cancel() if info.done else None, min_interval=0)
    with pytest.raises(Cancelled):
        infer_schema(doc, sample=None, progress=prog, cancel=token)


@pytest.mark.parametrize("bad", [0, -5, True, 2.5, "10"])
def test_bad_sample(open_doc, bad) -> None:
    doc = open_doc(b"[1]")
    with pytest.raises(UserInputError) as exc:
        infer_schema(doc, sample=bad)
    assert exc.value.key == "schema.bad_sample"
    assert exc.value.hint


def test_large_random_sample_against_oracle(open_doc) -> None:
    rng = random.Random(3)
    recs = [jsongen.rand_record(rng, i) for i in range(2000)]
    doc = open_doc(recs, "array", style="pretty2")
    s = infer_schema(doc, sample=333)
    assert_matches(s, reference_schema([recs[p] for p in sample_positions(2000, 333)]))


@pytest.mark.slow
def test_sampling_a_larger_file_is_fast(open_doc, backend) -> None:
    import time

    rng = random.Random(9)
    recs = [jsongen.rand_record(rng, i) for i in range(60000)]
    doc = open_doc(recs, "array")
    t0 = time.perf_counter()
    s = infer_schema(doc, sample=10000)
    elapsed = time.perf_counter() - t0
    assert s.scanned == 10000 and len(s.fields()) > 10
    assert elapsed < 10   # ~0.2 s measured; generous bound for slow CI machines


# ----------------------------------------------------------------------------------
# stage 5 (docs/ARCHITECTURE.md §14.1): the integer counter, the core field lists
# ----------------------------------------------------------------------------------
INTEGRAL_TOKENS = ["0", "-0", "1", "-17", "12345678901234567890", "1.0", "100.0", "-0.0", "1e400", "1E5", "1.000",
                   "2.5e1", "1e-0", "9" * 5000, "18446744073709551616"]
FRACTIONAL_TOKENS = ["0.1", "1.10", "0.10000000000000000001", "1e-400", "2.5", "-3.75e1", "5e-324", "1.0000000001"]


def _integral_oracle(v: Any) -> bool:
    if isinstance(v, bool) or codec.json_type(v) != "number":
        return False
    d = Decimal(codec.number_text(v))
    return d == d.to_integral_value()


def test_integer_counter_counts_exact_integral_values(open_doc) -> None:
    from jsonreshape.schema import is_integral

    toks = INTEGRAL_TOKENS + FRACTIONAL_TOKENS
    for tok in toks:
        v = codec.loads(tok.encode())
        assert is_integral(v) == (tok in INTEGRAL_TOKENS) == _integral_oracle(v), tok
    assert not is_integral(True) and not is_integral("1") and not is_integral(None)
    assert not is_integral(float("nan")) and not is_integral(float("inf")) and not is_integral(Decimal("Infinity"))
    data = ("[" + ",".join('{"n": %s, "a": [%s, "x"]}' % (tok, tok) for tok in toks) + "]").encode()
    want = len(INTEGRAL_TOKENS)
    for settings in (None, Settings(large_node_bytes=60)):      # parsed, and walked as big records
        doc = open_doc(data, settings=settings)
        s = infer_schema(doc, sample=None)
        assert s.find("n").integers == want and s.find("a[*]").integers == want
        assert s.find("n").types == {"number": len(toks)} and s.find("a").integers == 0
    b = SchemaBuilder()
    b.add({"d": Decimal("2.000"), "e": Decimal("2.5"), "f": 3.0, "g": JsonInt("-0"), "h": JsonFloat("1e400")})
    s = b.build()
    assert [s.find(k).integers for k in "defgh"] == [1, 0, 1, 1, 1]


def test_integer_counter_matches_an_oracle_on_random_records(open_doc) -> None:
    recs = jsongen.records(300, seed=77)
    doc = open_doc(recs, "array")
    s = infer_schema(doc, sample=None)
    counts: dict[tuple, int] = {}

    def walk(v: Any, path: tuple) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, path + (k,))
        elif isinstance(v, list):
            for x in v:
                walk(x, path + (ANY,))
        elif _integral_oracle(v):
            counts[path] = counts.get(path, 0) + 1

    for r in [doc.record(i) for i in range(doc.record_count)]:
        walk(r, ())
    checked = 0
    for fi in [s.root] + s.fields():
        assert fi.integers == counts.get(fi.path, 0), fi.text
        checked += 1
    assert checked > 20 and s.find("id").integers == 300


def test_integers_in_to_dict_and_files_without_it() -> None:
    b = SchemaBuilder()
    b.add({"n": 1, "m": 1.5})
    b.add({"n": 2.0, "m": None})
    s = b.build()
    d = s.to_dict()
    assert [f["integers"] for f in d["fields"]] == [2, 0]
    for f in d["fields"]:
        del f["integers"]          # a schema file written before stage 5
    del d["root"]["integers"]
    back = Schema.from_dict(d)
    assert back.find("n").integers == 0 and back.find("n").types == {"number": 2}
    first, second = SchemaBuilder(), SchemaBuilder()
    first.add({"n": 1})
    second.add({"n": 2.5})
    first.merge(second)
    assert first.build().find("n").integers == 1


def test_core_field_lists() -> None:
    from jsonreshape.schema import default_columns, field_paths, is_container, under_array

    b = SchemaBuilder(max_keys=6)
    b.add({"id": 1, "name": "a", "contact": {"phone": "1", "address": {"city": "T", "geo": {"lat": 1}}},
           "orders": [{"oid": 1, "sum": 2}], "tags": ["x"], "m": {f"k{i}": i for i in range(1, 9)}})
    s = b.build()
    assert s.find("m.*") is not None
    assert default_columns(s) == ["id", "name", "contact.phone", "contact.address.city", "orders", "tags", "m.k1",
                                  "m.k2", "m.k3", "m.k4", "m.k5", "m.k6"]
    assert default_columns(s, limit=2) == ["id", "name"]
    assert default_columns(s, max_depth=1) == ["id", "name", "orders", "tags"]
    paths = field_paths(s)
    assert "orders[*].oid" in paths and "m.*" not in paths and "contact" in paths
    assert "orders[*].oid" not in field_paths(s, inside_arrays=False)
    assert "contact" not in field_paths(s, containers=False)
    assert all(p.count(".") < 2 for p in field_paths(s, max_depth=2))
    assert is_container(s.find("contact")) and not is_container(s.find("id"))
    assert under_array(s.find("orders[*].oid")) and not under_array(s.find("orders"))
    keyed = SchemaBuilder()
    keyed.add({"x": 1}, "k1")
    ks = keyed.build()
    assert default_columns(ks) == ["$key", "x"] and field_paths(ks) == ["$key", "x"]
    assert field_paths(ks, key_field=False) == ["x"]
    scalars = SchemaBuilder()
    scalars.add(5)
    scalars.add("x")
    assert default_columns(scalars.build()) == ["$"] and default_columns(None) == ["$"] and field_paths(None) == []
