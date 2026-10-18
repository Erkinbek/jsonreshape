"""jsonreshape.schema: big records are *walked* (round-2 fix R2-3 / R2-11) -- the schema
must equal the one of the parsed records exactly.

The property test writes generated JSON *text* (duplicate keys, keys that only decode to the
same text, lone surrogate escapes, ``-0``, exponents, huge integers, nested arrays, maps with
more keys than ``MAX_KEYS``) as array / JSON Lines / keyed documents and compares
``infer_schema`` with every record parsed whole (``STREAM_RECORD_BYTES`` huge) against the
walking path (tiny ``STREAM_RECORD_BYTES`` / ``PARSE_BATCH_BYTES`` / ``FEED_BYTES``, records
in memory or -- tiny ``large_node_bytes`` -- read from the file; long strings decoded piece
by piece), under the default limits and
under tiny ones (key pooling, field budget exhausted, depth limit). Plus: all-or-nothing on
a record that fails half-way (property), cancellation inside a walked record, the plan path
for repeated keys and for equal hashes of different keys, the compact key-hash table, the
original repro numbers, memory of a big map, and ``SchemaBuilder.merge`` of pooled keys.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any, Iterator

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from jsonreshape import codec
from jsonreshape import schema as sm
from jsonreshape.errors import Cancelled
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken

HYP = hsettings(derandomize=True, database=None, deadline=None, max_examples=150,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

# ----------------------------------------------------------------------------------
# JSON text generator
# ----------------------------------------------------------------------------------
# key / string *texts* (inside the quotes): "a" decodes to "a" (a duplicate that
# differs as text), lone surrogates, a line separator, quotes and backslashes
KEYS = ["a", "b", "k0", "k1", "k2", "k3", "k4", "k5", "\\u0061", "x y", "Тош", "", "$key",
        "*", "0", "\\udc00", "q\\\""]
STRINGS = ["", "x", "Toshkent", "a\\nb", "\\udc00", "x\\udc00y", "\\ud800\\ud800", "\\ud83d\\ude00",
           "\\u2028", "q\\\"uote", "éé", "z" * 70, "\\\\"]
NUMBERS = ["0", "-0", "1", "-17", "1e400", "1E5", "0.0000001", "1.10", "2.5", "-1.5e-10", "1e-05",
           "123456789012345678901234567890", "18446744073709551615", "18446744073709551616",
           "-9223372036854775809", "0.1", "1" * 25]

_scalar = st.one_of(
    st.sampled_from(NUMBERS),
    st.integers(-10 ** 6, 10 ** 6).map(str),
    st.sampled_from(STRINGS).map(lambda s: '"' + s + '"'),
    st.sampled_from(["true", "false", "null"]),
)
_sep = st.sampled_from([",", ", ", " ,  "])


def _containers(inner: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    arr = st.tuples(st.lists(inner, max_size=6), _sep).map(lambda t: "[" + t[1].join(t[0]) + "]")
    obj = st.tuples(st.lists(st.tuples(st.sampled_from(KEYS), inner), max_size=7), _sep).map(
        lambda t: "{" + t[1].join(f'"{k}": {v}' for k, v in t[0]) + "}")
    return st.one_of(arr, obj)


_value = st.recursive(_scalar, _containers, max_leaves=40)
_record = st.one_of(_value, _containers(_value))   # mostly containers

LIMITS = [(sm.MAX_FIELDS, sm.MAX_KEYS, sm.MAX_DEPTH), (12, 3, 3), (5, 2, 2), (40, 4, 6), (8, 1000, 1)]


@contextlib.contextmanager
def patched(**values: Any) -> Iterator[None]:
    """Set module constants of jsonreshape.schema (hypothesis cannot use monkeypatch)."""
    old = {k: getattr(sm, k) for k in values}
    try:
        for k, v in values.items():
            setattr(sm, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(sm, k, v)


def write_doc(folder: Path, name: str, records: list[str], fmt: str) -> Path:
    p = folder / name
    if fmt == "jsonl":
        text = "\n".join(records) + "\n"
    elif fmt == "keyed":
        text = "{" + ", ".join(f'"r{i}": {r}' for i, r in enumerate(records)) + "}"
    else:
        text = "[" + ",\n".join(records) + "]"
    p.write_bytes(text.encode("utf-8"))
    return p


def schema_dict(path: Path, fmt: str, *, huge: bool = False) -> dict[str, Any]:
    # huge: records above 48 bytes are huge blocks (read from the file, values parsed only
    # below 48 bytes); else every record is in memory
    settings = Settings(large_node_bytes=48) if huge else None
    with Document.open(path, format=fmt, settings=settings) as doc:
        doc.build_index()
        d = sm.infer_schema(doc, sample=None).to_dict()
    d.pop("elapsed")
    return d


def limits_kw(limits: tuple[int, int, int]) -> dict[str, int]:
    return {"MAX_FIELDS": limits[0], "MAX_KEYS": limits[1], "MAX_DEPTH": limits[2]}


# ----------------------------------------------------------------------------------
# the property: walked == parsed
# ----------------------------------------------------------------------------------
@HYP
@given(records=st.lists(_record, min_size=1, max_size=5), fmt=st.sampled_from(["array", "jsonl", "keyed"]),
       huge=st.booleans(), limit=st.sampled_from([2, 8, 24, 100]), batch=st.sampled_from([1, 10, 40, 1 << 15]),
       feed=st.sampled_from([1, 7, 64, 1 << 18]), limits=st.sampled_from(LIMITS))
def test_walked_schema_equals_parsed(records, fmt, huge, limit, batch, feed, limits) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_doc(Path(tmp), "doc.json", records, fmt)
        with patched(STREAM_RECORD_BYTES=1 << 40, **limits_kw(limits)):
            parsed = schema_dict(path, fmt)
        with patched(STREAM_RECORD_BYTES=limit, PARSE_BATCH_BYTES=batch, FEED_BYTES=feed, **limits_kw(limits)):
            walked = schema_dict(path, fmt, huge=huge)
    assert parsed["skipped"] == 0 and walked["skipped"] == 0
    assert walked == parsed


@hsettings(HYP, max_examples=80)
@given(records=st.lists(_record, min_size=0, max_size=4), bad=_value, pos=st.integers(0, 4),
       where=st.sampled_from(["first", "middle", "last"]), huge=st.booleans(), limits=st.sampled_from(LIMITS))
def test_failing_record_leaves_nothing_behind(records, bad, pos, where, huge, limits) -> None:
    """A record that fails half-way through the walk is undone completely: the schema
    equals the one of the other records (fields, order, statistics, examples). (Under
    tight limits the walk may never decode the failing value -- a field beyond the depth
    limit or the field budget is not analysed -- and then the record counts like parsed.)"""
    marker = '"BOOM"'
    parts = {"first": f'{{"boom": {marker}, "v": {bad}}}', "middle": f'{{"v": {bad}, "w": [{bad}, {marker}], "z": 1}}',
             "last": f'[{bad}, {{"k0": {bad}, "k1": [1, {marker}]}}, {marker}]'}
    failing = parts[where]
    pos = min(pos, len(records))
    real_loads = codec.loads

    def loads(data: Any, *a: Any, **k: Any) -> Any:
        if b'"BOOM"' in bytes(data):
            raise codec.JsonDecodeError("injected")
        return real_loads(data, *a, **k)

    with tempfile.TemporaryDirectory() as tmp:
        good = write_doc(Path(tmp), "good.json", records, "array")
        mixed = write_doc(Path(tmp), "mixed.json", records[:pos] + [failing] + records[pos:], "array")
        with patched(STREAM_RECORD_BYTES=1 << 40, **limits_kw(limits)):
            want_good = schema_dict(good, "array")
            want_mixed = schema_dict(mixed, "array")
        # (8 bytes: the token "BOOM" is decoded whole, not piece by piece like longer strings)
        with patched(STREAM_RECORD_BYTES=8, PARSE_BATCH_BYTES=16, FEED_BYTES=32, **limits_kw(limits)):
            codec.loads = loads
            try:
                got = schema_dict(mixed, "array", huge=huge)
            finally:
                codec.loads = real_loads
    if not got["skipped"]:     # the failing value was never decoded (limits): counted like parsed
        assert limits != LIMITS[0] and got == want_mixed
        return
    assert got["skipped"] == 1 and got["total_records"] == len(records) + 1 and not got["complete"]
    for k in ("skipped", "total_records", "complete"):
        got.pop(k)
        want_good.pop(k)
    assert got == want_good


# ----------------------------------------------------------------------------------
# targeted cases
# ----------------------------------------------------------------------------------
def _open(tmp_path: Path, data: bytes, name: str = "d.json", **kw: Any) -> Document:
    p = tmp_path / name
    p.write_bytes(data)
    doc = Document.open(p, **kw)
    doc.build_index()
    return doc


def _schema(doc: Document) -> dict[str, Any]:
    d = sm.infer_schema(doc, sample=None).to_dict()
    d.pop("elapsed")
    return d


def test_r2_3_pooled_keys_of_big_records_counted_once(tmp_path) -> None:
    """The verifier's repro (default settings): two 1.4 MB records with 700 distinct keys
    each -> 1400 > MAX_KEYS keys under ``scores``; the streamed path counted the pooled
    field once per pooled key (presence 20000 %)."""
    pad = "x" * 2000
    recs = [{"id": r, "scores": {f"k{r}_{i}": pad for i in range(700)}} for r in range(2)]
    data = json.dumps(recs).encode()
    doc = _open(tmp_path, data)
    assert all(e - s > sm.STREAM_RECORD_BYTES for s, e in (doc.record_span(i) for i in range(2)))
    s = sm.infer_schema(doc, sample=None)
    pool = s.find("scores.*")
    assert (pool.count, pool.records, pool.present, pool.base) == (400, 1, 1, 2) and pool.presence == 0.5
    assert s.find("scores").obj_keys == (700, 700) and len(s.find("scores").children) == 1001
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "STREAM_RECORD_BYTES", 1 << 40)
        parsed = _schema(doc)
    walked = s.to_dict()
    walked.pop("elapsed")
    assert walked == parsed


def test_r2_11_lone_low_surrogate_in_a_big_record(tmp_path) -> None:
    """A lone low surrogate escape (ijson refused it: the record was skipped) and two high
    surrogates (ijson made U+10000 of them) in a record above STREAM_RECORD_BYTES."""
    pad = "p" * (sm.STREAM_RECORD_BYTES + 1000)
    data = ('[{"id":1,"s":"a\\udc00b","t":"\\ud800\\ud800","pad":"' + pad + '"},{"id":2,"s":"ok","t":"x"}]').encode()
    doc = _open(tmp_path, data)
    s = sm.infer_schema(doc, sample=None)
    assert s.scanned == 2 and s.skipped == 0 and s.complete
    assert s.find("s").examples == ['"a\\udc00b"', '"ok"'] and s.find("t").examples == ['"\\ud800\\ud800"', '"x"']
    assert s.find("pad").str_len == (len(pad), len(pad))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "STREAM_RECORD_BYTES", 1 << 40)
        parsed = _schema(doc)
    walked = s.to_dict()
    walked.pop("elapsed")
    assert walked == parsed


@pytest.mark.parametrize("huge", [False, True])
def test_repeated_keys_across_batches_use_the_plan(tmp_path, monkeypatch, huge) -> None:
    """A key whose first occurrence is in one parse batch and the last one is a big member
    of the same big object: the record is undone and walked again with a plan (the last
    value, at the first key's position)."""
    big = "[" + ", ".join(f'{{"i": {i}, "s": "{"y" * 20}"}}' for i in range(200)) + "]"
    rec = '{"a": 1, "b": {"x": 1}, "c": [true], "a": ' + big + ', "d": "e", "b": null}'
    data = ("[" + rec + ', {"a": "z", "d": 5}]').encode()
    doc = _open(tmp_path, data, settings=Settings(large_node_bytes=1) if huge else None)
    monkeypatch.setattr(sm, "STREAM_RECORD_BYTES", 600)
    monkeypatch.setattr(sm, "PARSE_BATCH_BYTES", 64)
    plans: list[Any] = []
    real_plan = sm._RecordWalker.object_plan
    monkeypatch.setattr(sm._RecordWalker, "object_plan", lambda self, s, e: plans.append(real := real_plan(self, s, e))
                        or real)
    s = sm.infer_schema(doc, sample=None)
    assert plans and any(p is not None and set(p) == {"a", "b"} for p in plans)
    assert [fi.text for fi in s.fields()] == ["a", "a[*]", "a[*].i", "a[*].s", "b", "c", "c[*]", "d"]
    assert s.find("a").types == {"array": 1, "string": 1} and s.find("a[*]").count == 200
    assert s.find("b").types == {"null": 1} and s.find("b.x") is None
    assert s.find("$").obj_keys == (2, 4)
    monkeypatch.setattr(sm, "STREAM_RECORD_BYTES", 1 << 40)
    parsed = _schema(doc)
    walked = s.to_dict()
    walked.pop("elapsed")
    assert walked == parsed


def test_equal_hashes_of_different_keys_are_not_duplicates(tmp_path, monkeypatch) -> None:
    """Every key hash colliding: the plan pass finds no real duplicate and the object is
    walked normally (no endless re-walk, nothing merged)."""
    rec = "{" + ", ".join(f'"k{i}": {{"v": {i}, "w": [{i}]}}' for i in range(40)) + ', "k3": "last"}'
    data = ("[" + rec + "]").encode()
    doc = _open(tmp_path, data)
    monkeypatch.setattr(sm, "STREAM_RECORD_BYTES", 1 << 40)
    parsed = _schema(doc)
    monkeypatch.setattr(sm, "STREAM_RECORD_BYTES", 30)
    monkeypatch.setattr(sm, "PARSE_BATCH_BYTES", 50)
    monkeypatch.setattr(sm, "hash", lambda _k: 7, raising=False)
    walked = _schema(doc)
    assert walked == parsed
    keys = [f["text"] for f in walked["fields"] if f["depth"] == 1]
    assert keys == [f"k{i}" for i in range(40)]


def test_deep_big_object_counts_distinct_keys(tmp_path, monkeypatch) -> None:
    """A big object at the depth limit is not walked, but its distinct keys are counted
    (with dict semantics) -- also when keys repeat."""
    inner = "{" + ", ".join(f'"k{i % 7}": {i}' for i in range(30)) + "}"
    data = ('[{"a": {"b": ' + inner + '}}]').encode()
    doc = _open(tmp_path, data)
    with patched(MAX_DEPTH=2, STREAM_RECORD_BYTES=1 << 40):
        parsed = _schema(doc)
    with patched(MAX_DEPTH=2, STREAM_RECORD_BYTES=16, PARSE_BATCH_BYTES=16):
        walked = _schema(doc)
    assert walked == parsed and walked["truncated"]
    assert [f["obj_keys"] for f in walked["fields"] if f["text"] == "a.b"] == [[7, 7]]


def test_cancel_inside_a_walked_record_undoes_it(tmp_path, monkeypatch) -> None:
    """Cancellation is checked inside a walked record (every scanner chunk and parse
    batch); the builder is left exactly as it was before the record."""
    recs = [{"x": 1, "y": "a"}, {"x": [{"z": i} for i in range(500)], "w": "b" * 50}]
    data = json.dumps(recs).encode()
    doc = _open(tmp_path, data)
    monkeypatch.setattr(sm, "STREAM_RECORD_BYTES", 64)
    monkeypatch.setattr(sm, "PARSE_BATCH_BYTES", 128)
    b = sm.SchemaBuilder()
    b.add({"x": 1, "y": "a"})
    before = b.build().to_dict()
    token = CancelToken()
    calls = [0]
    real_check = sm.check

    def check(cancel: Any) -> None:
        calls[0] += 1
        if calls[0] == 5:
            token.cancel()
        real_check(cancel)

    monkeypatch.setattr(sm, "check", check)
    with pytest.raises(Cancelled):
        sm._stream_into(b, doc, doc.record_span(1), False, token)
    assert calls[0] == 5
    after = b.build().to_dict()
    assert after == before
    assert sm._stream_into(b, doc, doc.record_span(1), False, None)    # the builder is still usable
    assert b.build().find("x[*].z").count == 500


def test_big_map_memory_and_pooling(tmp_path) -> None:
    """A ~2.3 MB single document holding one map of 200 000 tiny members: walked with a
    compact key-hash table (pooled into ``m.*`` beyond MAX_KEYS) -- Python memory a small
    multiple of the bytes (the block itself, ~16-32 bytes of hash table per key) and well
    below parsing it; equal to the parsed schema."""
    import tracemalloc

    n = 200_000
    data = b'{"m": {' + b",".join(b'"k%d":%d' % (i, i % 10) for i in range(n)) + b'}, "z": 1}'
    doc = _open(tmp_path, data, format="single")
    tracemalloc.start()
    try:
        s = sm.infer_schema(doc, sample=None)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    pool = s.find("m.*")
    assert pool.count == n - sm.MAX_KEYS and pool.records == 1 and pool.presence == 1.0
    assert s.find("m").obj_keys == (n, n)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "STREAM_RECORD_BYTES", 1 << 40)
        tracemalloc.start()
        try:
            parsed = _schema(doc)
            _cur, parsed_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak < 5 * len(data) and peak < parsed_peak / 2, (peak, parsed_peak, len(data))
    walked = s.to_dict()
    walked.pop("elapsed")
    assert walked == parsed


def test_key_hashes_set_and_table() -> None:
    """The key-hash set switches to the open-addressing table beyond KEYSET_MAX and
    answers the same (zero, negative and colliding slots included)."""
    with patched(KEYSET_MAX=8):
        kh = sm._KeyHashes()
        seen: set[int] = set()
        values = [0, -1, 1, 2 ** 63, -(2 ** 63), 5, 5 + 2 ** 40, 7] + [i * 1024 for i in range(3000)] + \
            [hash(f"key{i}") for i in range(3000)]
        for v in values + values[::7]:
            norm = v & 0xFFFFFFFFFFFFFFFF or 1          # how the table stores it
            again = kh.add(v)
            if v in seen:
                assert again, v                          # never a missed repeat
            elif again:
                assert norm in {x & 0xFFFFFFFFFFFFFFFF or 1 for x in seen}, v   # only equal stored hashes
            seen.add(v)
        assert kh._set is None and 2 * kh._n <= len(kh._table) <= 8 * kh._n
        assert not kh.add(123456789) and kh.add(123456789)


def test_merge_of_pooled_keys_is_capped_by_the_parent() -> None:
    """``SchemaBuilder.merge``: fields of the other builder that land in one pooled field
    here no longer add one record per pooled key (exact for a one-record builder; capped
    upper bound otherwise)."""
    pad = "x"
    recs = [{"id": r, "scores": {f"k{r}_{i}": pad for i in range(7)}, "l": [{f"q{r}{i}": 1 for i in range(4)}]}
            for r in range(3)]
    whole = sm.SchemaBuilder(max_keys=5)
    for r in recs:
        whole.add(r)
    merged = sm.SchemaBuilder(max_keys=5)
    for r in recs:
        one = sm.SchemaBuilder(max_keys=5)
        one.add(r)
        merged.merge(one)
    assert merged.build().to_dict() == whole.build().to_dict()
    # several records at once: an upper bound, never above the parent's
    two = sm.SchemaBuilder(max_keys=5)
    for r in recs[1:]:
        two.add(r)
    first = sm.SchemaBuilder(max_keys=5)
    first.add(recs[0])
    first.merge(two)
    s = first.build()
    for fi in s.fields():
        parent = s.root if len(fi.path) == 1 else s.find(sm.format_path(fi.path[:-1]))
        assert fi.records <= parent.records and fi.present <= fi.base
    assert s.find("scores.*").records == 3 and s.find("l[*].*").present <= s.find("l[*]").count


@pytest.mark.parametrize("large", [4096, 300])
def test_huge_records_are_never_parsed_whole(tmp_path, monkeypatch, large) -> None:
    """No ``codec.loads`` input reaches the huge-record size (``large_node_bytes``), even
    when it is below STREAM_RECORD_BYTES -- big strings included: they are decoded piece
    by piece (surrogate pairs, escapes and multi-byte characters across piece borders)."""
    big = list(range(1500))
    s_long = "x" * 9000
    tricky = ("\U0001F600\u00e9\\\"a" * 700) + "\udc00" + "\ud83d"   # pairs, escapes, lone surrogates
    recs = [{"id": 0, "big": [1, 2]}, {"id": 1, "big": big, "s": s_long, "t": tricky, "w": [{"x": 1}, {"x": "t"}]}]
    body = ",".join(json.dumps(r) for r in recs)       # ensure_ascii: \ud83d\ude00 escapes
    raw = ("[" + body + ',{"name":"first","pad":' + json.dumps(big) + ',"name":"last","id":2}]').encode()
    doc = _open(tmp_path, raw, settings=Settings(large_node_bytes=large))
    assert doc.is_huge(1) and doc.is_huge(2) and not doc.is_huge(0)
    sizes: list[int] = []
    real = codec.loads

    def spy(data: Any, *a: Any, **k: Any) -> Any:
        sizes.append(len(data))
        return real(data, *a, **k)

    monkeypatch.setattr(codec, "loads", spy)
    s = sm.infer_schema(doc, sample=None)
    monkeypatch.setattr(codec, "loads", real)
    assert sizes and max(sizes) < large, max(sizes)
    assert s.find("s").str_len == (9000, 9000) and s.find("t").str_len == (len(tricky), len(tricky))
    assert s.find("name").examples == ['"last"'] and s.find("big[*]").count == 1502
    monkeypatch.setattr(sm, "STREAM_RECORD_BYTES", 1 << 40)
    parsed = sm.infer_schema(_open(tmp_path, raw, name="p.json"), sample=None).to_dict()
    walked = s.to_dict()
    parsed.pop("elapsed"), walked.pop("elapsed")
    assert walked == parsed
