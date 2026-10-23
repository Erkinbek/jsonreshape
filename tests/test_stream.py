"""jsonreshape.stream: ijson-based access to huge records (docs/ARCHITECTURE.md §5.3).

Results must equal parsing the same record with ``codec.loads`` and applying
``PathPattern.values`` / a pre-order walk (documented ijson deviations: ``-0`` arrives as
``0``; ``1e400``-style exponents may be normalized to ``1E+400`` -- equal values).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

import jsongen
from jsonreshape import codec
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.errors import JsonReshapeError
from jsonreshape.paths import parse_pattern
from jsonreshape.query import compile_filter
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.stream import iter_events, stream_values, stream_walk

PATTERNS = [
    "$", "id", "name", "contact.address.city", "contact.*", "orders[*].sum", "orders[0].oid", "orders.0.oid",
    "orders[*]", "tags[*]", "*", "**", "..city", "..oid", "extra.**", "extra[*][*]", "**.sum", "missing.path",
    "[*]", "[1]", "*.*",
]


def same(a: Any, b: Any) -> bool:
    """Deep equality with equal types (``a`` streamed, ``b`` from codec.loads); the only
    tolerated difference is ijson's ``-0`` -> ``0``."""
    if isinstance(b, dict):
        return isinstance(a, dict) and list(a) == list(b) and all(same(a[k], b[k]) for k in b)
    if isinstance(b, list):
        return isinstance(a, list) and len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(b, JsonInt) and b.raw == "-0":
        return type(a) is int and a == 0
    return type(a) is type(b) and a == b


def ref_walk(v: Any, path: tuple = ()) -> Iterator[tuple[tuple, str, Any]]:
    if isinstance(v, dict):
        for k, x in v.items():
            yield path + (k,), "key", k
            yield from ref_walk(x, path + (k,))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from ref_walk(x, path + (i,))
    else:
        yield path, "value", v


def walk_same(got: list, want: list) -> bool:
    return len(got) == len(want) and all(g[0] == w[0] and g[1] == w[1] and same(g[2], w[2]) for g, w in zip(got, want))


@pytest.fixture
def records_doc(tmp_path: Path):
    def make(fmt: str, n: int = 30, seed: int = 1, kind: str = "record", **kw) -> tuple[Document, list]:
        recs = jsongen.records(n, seed=seed, kind=kind)
        p = tmp_path / f"{fmt}-{seed}-{kind}.json"
        jsongen.write_file(p, recs, fmt, **kw)
        doc = Document.open(p)
        doc.build_index()
        return doc, recs

    docs: list[Document] = []

    def factory(*a, **k):
        doc, recs = make(*a, **k)
        docs.append(doc)
        return doc, recs

    yield factory
    for d in docs:
        d.close()


@pytest.mark.parametrize("fmt, kind, style", [("array", "record", "compact"), ("jsonl", "value", "compact"),
                                              ("keyed", "record", "pretty2"), ("array", "value", "messy_escaped")])
def test_stream_values_equal_pattern_values(records_doc, fmt: str, kind: str, style: str) -> None:
    doc, recs = records_doc(fmt, n=40, seed=7, kind=kind, style=style)
    patterns = [parse_pattern(p) for p in PATTERNS]
    for i in range(doc.record_count):
        rec = doc.record(i)
        got = stream_values(doc, doc.record_span(i), patterns, member=doc.member_records)
        assert set(got) == set(range(len(patterns)))
        for k, pat in enumerate(patterns):
            assert same(got[k], pat.values(rec)), (i, pat.text, got[k], pat.values(rec))


def test_stream_values_accepts_pattern_text(records_doc) -> None:
    doc, _ = records_doc("array", n=3)
    got = stream_values(doc, doc.record_span(1), ["name", "contact.address.city"])
    rec = doc.record(1)
    assert got[0] == [rec["name"]] and got[1] == parse_pattern("contact.address.city").values(rec)


DUPES = (b'{"a": 1, "b": {"x": 1, "x": 2}, "a": {"y": [1, 2]}, "c": [{"k": 1, "k": 2}],'
         b' "b": {"x": 3, "z": {"x": 4}}, "d": {"e": {"f": 1}, "e": {"g": 2}}}')


def test_stream_values_dict_semantics_with_duplicate_keys(tmp_path: Path) -> None:
    p = tmp_path / "dupes.json"
    p.write_bytes(b"[" + DUPES + b"," + DUPES + b"]")
    with Document.open(p) as doc:
        doc.build_index()
        rec = codec.loads(DUPES)
        pats = [parse_pattern(t) for t in ("a", "a.y[*]", "b.x", "b", "*", "**", "c[*].k", "..x", "d.e.f", "d.e.g",
                                           "d.*.*", "$")]
        for i in range(2):
            got = stream_values(doc, doc.record_span(i), pats)
            for k, pat in enumerate(pats):
                assert same(got[k], pat.values(rec)), (pat.text, got[k], pat.values(rec))


def test_stream_values_numbers_match_codec_types(tmp_path: Path) -> None:
    text = (b'{"f": 1.10, "e": 1e400, "big": 123456789012345678901234567890, "neg0": -0, "negf": -0.0, '
            b'"i": 7, "pf": 0.5, "exp": 1E5, "u64": 18446744073709551615, "i64": -9223372036854775808, '
            b'"tiny": 5e-324, "frac": 123456789.123456789, "arr": [1.10, 2]}')
    p = tmp_path / "n.json"
    p.write_bytes(b"[" + text + b"]")
    with Document.open(p) as doc:
        doc.build_index()
        rec = doc.record(0)
        got = stream_values(doc, doc.record_span(0), ["*", "$"])
        assert same(got[0], list(rec.values())) and same(got[1], [rec])
        f, e = got[0][0], got[0][1]
        assert isinstance(f, JsonFloat) and codec.dumps(f) == b"1.10"
        assert isinstance(e, JsonFloat) and e == float("inf")
        assert isinstance(got[0][2], JsonInt) and got[0][2] == 123456789012345678901234567890
        assert type(got[0][5]) is int and type(got[0][6]) is float


def test_stream_walk_equals_preorder_walk(records_doc) -> None:
    for fmt, kind in (("array", "record"), ("keyed", "record"), ("jsonl", "value")):
        doc, _ = records_doc(fmt, n=25, seed=3, kind=kind)
        for i in range(doc.record_count):
            got = list(stream_walk(doc, doc.record_span(i), member=doc.member_records))
            want = list(ref_walk(doc.record(i)))
            assert walk_same(got, want), (fmt, i)


def test_stream_walk_reports_every_duplicate(tmp_path: Path) -> None:
    p = tmp_path / "d.json"
    p.write_bytes(b'[{"a": 1, "a": [2, {"a": 3}]}]')
    with Document.open(p) as doc:
        doc.build_index()
        got = list(stream_walk(doc, doc.record_span(0)))
        assert got == [(("a",), "key", "a"), (("a",), "value", 1), (("a",), "key", "a"),
                       (("a", 0), "value", 2), (("a", 1, "a"), "key", "a"), (("a", 1, "a"), "value", 3)]


def test_stream_walk_memory_does_not_grow_with_the_square_of_the_depth(tmp_path: Path) -> None:
    import tracemalloc

    depth = 20_000
    p = tmp_path / "deep.json"
    p.write_bytes(b'[{"a": 1}, ' + b"[" * depth + b'{"k": "v"}' + b"]" * depth + b"]")
    with Document.open(p) as doc:
        doc.build_index()
        tracemalloc.start()
        try:
            got = list(stream_walk(doc, doc.record_span(1)))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert got == [((0,) * depth + ("k",), "key", "k"), ((0,) * depth + ("k",), "value", "v")]
    assert peak < 32 << 20, peak         # was ~1.6 GB: a path tuple per open container


def test_iter_events_and_member_wrapping(tmp_path: Path) -> None:
    p = tmp_path / "k.json"
    p.write_bytes(b'{"k1": {"a": [1, "x"]}, "k2": 2.5}')
    with Document.open(p, format="keyed") as doc:
        doc.build_index()
        assert doc.member_records
        events = list(iter_events(doc, doc.record_span(0), member=True))
        assert [e for e, _ in events] == ["start_map", "map_key", "start_map", "map_key", "start_array", "number",
                                          "string", "end_array", "end_map", "end_map"]
        assert events[1] == ("map_key", "k1")
        assert list(stream_walk(doc, doc.record_span(1), member=True)) == [((), "value", 2.5)]
        assert stream_values(doc, doc.record_span(0), ["a[1]", "$"], member=True) == {0: ["x"], 1: [{"a": [1, "x"]}]}
        events = list(iter_events(doc, doc.record_value_span(1)))
        assert events == [("number", codec.loads("2.5"))] or events[0][0] == "number"


def test_invalid_span_raises_localized_error(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_bytes(b'[{"a": 1}, {"b": 2}]')
    with Document.open(p) as doc:
        with pytest.raises(JsonReshapeError) as ei:
            list(iter_events(doc, (2, 12)))  # cuts through two records
        assert ei.value.key == "stream.error.parse"
        with pytest.raises(ValueError):
            list(iter_events(doc, (5, 3)))


def test_huge_records_are_streamed_and_filtered(tmp_path: Path) -> None:
    recs = jsongen.records(30, seed=21)
    for r in recs[::3]:
        r["blob"] = ["x" * 40 for _ in range(30)]  # ~1.3 KB: "huge" under the small limit below
    p = tmp_path / "h.json"
    jsongen.write_file(p, recs, "keyed")
    flt = compile_filter('blob[0] == "' + "x" * 40 + '" or contact.address.city == "Toshkent" or len(orders) > 1')
    patterns = flt.required_patterns
    assert patterns
    with Document.open(p, settings=Settings(large_node_bytes=1000)) as doc:
        doc.build_index()
        data = doc.data_path.read_bytes()
        huge = [blk for blk in doc.iter_blocks() if blk.huge]
        assert len(huge) == 10
        for blk in huge:
            i = int(blk.indices[0])
            span = blk.span(0)
            assert span == doc.record_span(i) and blk.data == b""
            raw = data[span[0]:span[1]]
            key, vo = Document.split_member(raw)
            rec = codec.loads(raw[vo:])
            assert key == f"id{i}" and codec.dumps(rec) == codec.dumps(recs[i])
            values = stream_values(doc, span, patterns, member=True)
            for k, pat in enumerate(patterns):
                assert same(values[k], pat.values(rec)), pat.text
            assert flt.matches_values(values, key=doc.record_key(i), index=i) == flt.matches(rec, key=f"id{i}", index=i)
            walked = list(stream_walk(doc, span, member=True))
            assert walk_same(walked, list(ref_walk(rec)))
