"""Stage-5 review "analysis", stats findings (R5A-1, R5A-2, R5A-5, R5A-6, R5A-7, R5A-11).

* R5A-1: the memory budget is checked inside a chunk (every FEED_SLICE values) and a record with
  millions of values is streamed: the exact dicts never grow past the budget by more than a slice;
  exact mode spills in the middle of a record and still counts every record once per value
  (markers); stored tokens are compact (orjson's results kept a 4 KiB buffer each).
* R5A-2: the token caches are shared by the fields and capped; the Misra-Gries summaries get fewer
  counters when many fields are sketched (their total stays near half the budget).
* R5A-5: ``_resolve`` reads a record's tokens once per field and honours cancel.
* R5A-6: HyperLogLog (Ertl's estimator) is unbiased above the old linear-counting switch.
* R5A-7: live snapshots of the exact mode do not collapse after a spill.
* R5A-11: numbers left out of the sum are reported as such; the mean is over the others.
"""

from __future__ import annotations

import random
import statistics
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import reader, stats
from jsonreshape.errors import Cancelled
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.stats import field_stats
from jsonreshape.tasks import CancelToken


@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []

    def make(data: bytes, name: str = "doc.json", settings: Settings | None = None) -> Document:
        p = tmp_path / f"{len(docs)}-{name}"
        p.write_bytes(data)
        doc = Document.open(p, settings=settings)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


@pytest.fixture
def backend(core_backend, monkeypatch):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    monkeypatch.setattr(stats, "_EXTRACTOR", core_backend.PathExtractor)
    yield name
    reader.set_scanner_backend(old)


def summary(fs: Any) -> tuple:
    return (fs.scanned, fs.values, fs.distinct, fs.distinct_exact, fs.types,
            [(v.text, v.count, v.records, v.first) for v in fs.top], fs.number and fs.number.to_dict())


# ==================================================================================
# R5A-1
# ==================================================================================
def test_budget_is_checked_inside_a_chunk(make_doc, backend, monkeypatch) -> None:
    """One record with 60 000 distinct values: the dicts are checked every FEED_SLICE values (they
    grew to the whole record before the first check)."""
    monkeypatch.setattr(stats, "FEED_SLICE", 1000)
    seen: list[int] = []
    real = stats._AccSet.check_budget

    def spy(self: Any, *args: Any) -> None:
        if not self.single:
            seen.append(self.dict_bytes())
        real(self, *args)

    monkeypatch.setattr(stats._AccSet, "check_budget", spy)
    rec = b'{"a": [' + b",".join(b"%d" % (10_000_000 + i) for i in range(60_000)) + b"]}"
    doc = make_doc(rec + b"\n" + b'{"a": [1, 2]}\n', "one.jsonl")
    budget = 50_000
    fs = field_stats(doc, "a[*]", memory_limit=budget, strategy="extract")[0]
    assert fs.values == 60_002 and not fs.distinct_exact
    # the first check came after the first slice (it came after the whole record: 7.7 MB of entries)
    slice_bytes = 1000 * (8 + stats.ENTRY_COST + stats.MULTI_EXTRA)
    assert seen and seen[0] <= budget + slice_bytes and max(seen) <= budget + slice_bytes


@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_exact_mode_spills_inside_a_record_and_counts_records_once(make_doc, backend, monkeypatch,
                                                                    strategy: str) -> None:
    """Values repeated inside one record across many spills: counts, records and the first
    occurrence equal those of an unbounded exact run (markers stand for the split record)."""
    monkeypatch.setattr(stats, "FEED_SLICE", 50)
    rng = random.Random(7)
    recs = []
    for r in range(6):
        vals = [rng.randrange(300) for _ in range(rng.randint(200, 900))]
        recs.append(b'{"a": [' + b",".join(b"%d" % v for v in vals) + b'], "s": "%d"}' % (r % 2))
    data = b"[" + b",".join(recs) + b"]"
    doc = make_doc(data)
    spills: list[int] = []
    real = stats._FieldAcc.spill

    def spy(self: Any, open_record: int = -1) -> None:
        spills.append(open_record)
        real(self, open_record)

    want = field_stats(doc, ["a[*]", "s"], exact=True, top=50, strategy=strategy)
    monkeypatch.setattr(stats._FieldAcc, "spill", spy)
    got = field_stats(doc, ["a[*]", "s"], exact=True, top=50, memory_limit=2000, strategy=strategy)
    if strategy == "extract":
        assert any(o >= 0 for o in spills), spills       # spilled in the middle of a record
    assert len(spills) > 5
    for g, w in zip(got, want):
        assert g.distinct_exact and g.top_exact
        assert summary(g) == summary(w)


def test_a_record_with_too_many_values_is_streamed(make_doc, backend, monkeypatch) -> None:
    """A record that may hold more values than memory_limit_bytes / 256 is streamed for the
    multi-valued fields (the extractor's output costs 32 bytes per value): same statistics."""
    vals = [10_000_000 + (i * 7919) % 20_000 for i in range(30_000)]
    rec = b'{"a": [' + b",".join(b"%d" % v for v in vals) + b'], "id": 5}'
    data = rec + b"\n" + b'{"a": [1, 2, 3], "id": 6}\n'
    plain = make_doc(data, "plain.jsonl")
    small = make_doc(data, "small.jsonl", settings=Settings(memory_limit_bytes=256 * 10_000))
    streamed: list[int] = []
    real = stats._Engine._big_record

    def spy(self: Any, blk: Any, k: int, o: int) -> None:
        streamed.append(int(blk.indices[k]))
        real(self, blk, k, o)

    want = field_stats(plain, ["a[*]", "id"], top=30, strategy="extract", memory_limit=64 << 20)
    monkeypatch.setattr(stats._Engine, "_big_record", spy)
    got = field_stats(small, ["a[*]", "id"], top=30, strategy="extract", memory_limit=64 << 20)
    assert streamed == [0]
    for g, w in zip(got, want):
        assert summary(g) == summary(w)


def test_stored_tokens_are_compact() -> None:
    """orjson keeps a 4 KiB buffer behind every small result: the tokens stats keeps are copies."""
    import tracemalloc

    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        toks = [stats._dump(10_000_000 + i) for i in range(2000)]
        per = (tracemalloc.get_traced_memory()[0] - base) / len(toks)
    finally:
        tracemalloc.stop()
    assert toks[5] == b"10000005" and per < 200


# ==================================================================================
# R5A-2
# ==================================================================================
def test_token_caches_are_shared_and_capped() -> None:
    accset = stats._AccSet(80_000, False, None, None, [])
    accs = [stats._FieldAcc(False, accset) for _ in range(5)]
    for acc in accs:
        acc.to_sketch()
    for i in range(3000):
        for acc in accs:
            acc.sketch_add(b'"%040d"' % i, i, 0)
    assert all(a.set is accset for a in accs)
    assert accset.cache_bytes <= accset.cache_cap + 2 * 42 + stats.CACHE_ENTRY_COST
    assert len(accset.scache) < 3000          # cleared at the cap (one cache, not five)


def test_misra_gries_counters_scale_with_the_sketched_fields() -> None:
    budget = 64 << 20
    accset = stats._AccSet(budget, False, None, None, [], top=20)
    assert accset.mg_counters(extra=1) == stats.MG_COUNTERS        # few fields: the full summary
    for _ in range(30):
        stats._FieldAcc(False, accset).to_sketch()
    k = accset.mg_counters()
    assert stats.MG_MIN <= k < stats.MG_COUNTERS
    assert 30 * 2 * k * stats.MG_ENTRY_COST <= budget // 2 + 30 * stats.MG_ENTRY_COST
    tiny = stats._AccSet(1000, False, None, None, [], top=900)
    assert tiny.mg_counters(extra=1) == max(stats.MG_MIN, 1800)   # never below max(MG_MIN, 2 * top)


def test_many_sketched_fields_stay_within_the_budget(make_doc, backend) -> None:
    rng = random.Random(3)
    nf = 12
    lines = [b"{" + b",".join(b'"f%d": %d' % (j, rng.randrange(10 ** 9)) for j in range(nf)) + b"}"
             for _ in range(8000)]
    doc = make_doc(b"\n".join(lines) + b"\n", "wide.jsonl")
    budget = 400_000
    sizes: list[int] = []
    real = stats._Engine.finish

    def spy(self: Any, progress: Any) -> Any:
        sizes.append(self.accset.sketch_bytes())
        sizes.append(min(a.mg.k for a in self.accs if a.mg is not None))
        return real(self, progress)

    import unittest.mock as um

    with um.patch.object(stats._Engine, "finish", spy):
        res = field_stats(doc, [f"f{j}" for j in range(nf)], memory_limit=budget)
    assert all(not fs.distinct_exact for fs in res)
    sketch_bytes, k = sizes
    # the summaries got MG_MIN counters (half the budget is far too small for MG_COUNTERS each)
    assert k == stats.MG_MIN
    assert sketch_bytes <= nf * (stats.HLL_BYTES + 2 * stats.MG_MIN * stats.MG_ENTRY_COST)
    for fs in res:
        assert fs.top_bound <= fs.values / (stats.MG_MIN + 1)
        assert abs(fs.distinct - 8000) <= 4 * stats.HLL_RSE * 8000


# ==================================================================================
# R5A-5
# ==================================================================================
def _deep_chain(depth: int) -> bytes:
    """An object chain: the outer ``..a`` values are over 4 KiB, so no spelling is kept for them
    and their texts are read again from the record (the review's case: depth 1000, 7 KB)."""
    rec = b"1"
    for i in range(depth):
        rec = b'{"a": %s, "n": %d, "p": "%s"}' % (rec, i, b"x" * 20)
    return rec


def test_resolve_reads_a_record_once_per_field(make_doc, backend, monkeypatch) -> None:
    doc = make_doc(_deep_chain(150) + b"\n", "deep.jsonl")
    calls: list[int] = []
    real = stats._Engine._tokens

    def spy(self: Any, f: Any, blk: Any, k: int, pos: int) -> Any:
        calls.append(pos)
        return real(self, f, blk, k, pos)

    monkeypatch.setattr(stats._Engine, "_tokens", spy)
    fs = field_stats(doc, "..a", top=20)[0]
    assert len(fs.top) == 20 and all(v.text != stats.ELLIPSIS for v in fs.top)
    assert calls == [0]          # once for the field and record (was once per top value)


def test_resolve_honours_cancel(make_doc, backend, monkeypatch) -> None:
    doc = make_doc(_deep_chain(150) + b"\n", "deep.jsonl")
    token = CancelToken()
    real = stats._Engine._tokens

    def spy(self: Any, *args: Any) -> Any:
        token.cancel()           # cancel while the top values are read again
        return real(self, *args)

    monkeypatch.setattr(stats._Engine, "_tokens", spy)
    with pytest.raises(Cancelled):
        field_stats(doc, "..a", top=20, cancel=token)


# ==================================================================================
# R5A-6
# ==================================================================================
def test_hyperloglog_is_unbiased_around_the_old_switch() -> None:
    """Fixed seeds: 20 sketches per cardinality. The classic estimator was biased by +1.2 .. +2.1 %
    between 40 000 and 50 000 (the linear-counting switch at 2.5 m = 40 960)."""
    for n in (1000, 20_000, 40_000, 42_000, 45_000, 50_000, 80_000):
        errs = []
        for seed in range(20):
            rng = random.Random(seed * 1_000_003 + n)
            h = stats._HLL()
            for _ in range(n):
                h.add(rng.getrandbits(64))
            errs.append((h.estimate() - n) / n)
        assert abs(statistics.mean(errs)) <= 0.006, (n, statistics.mean(errs))
        assert max(abs(e) for e in errs) <= 3 * stats.HLL_RSE, (n, max(errs))
    empty = stats._HLL()
    assert empty.estimate() == 0.0
    one = stats._HLL()
    one.add(12345)
    assert abs(one.estimate() - 1) < 0.01


# ==================================================================================
# R5A-7
# ==================================================================================
def test_exact_mode_snapshots_grow_after_spills(make_doc, backend, monkeypatch) -> None:
    monkeypatch.setattr(stats, "PARTIAL_SECONDS", 0.0)
    monkeypatch.setattr(stats, "PARTIAL_COST_FACTOR", 0.0)
    monkeypatch.setattr(stats, "CHUNK_RECORDS", 200)
    rng = random.Random(11)
    lines = [b'{"v": %d}' % (rng.randrange(20_000) if i % 3 else 7) for i in range(12_000)]
    doc = make_doc(b"\n".join(lines) + b"\n", "spill.jsonl")
    snaps: list[Any] = []
    final = field_stats(doc, "v", exact=True, memory_limit=20_000, on_partial=lambda fs: snaps.append(fs[0]))[0]
    assert final.distinct_exact and final.top[0].text == "7"
    assert len(snaps) > 10
    later = snaps[len(snaps) // 3:]          # after the first spills
    assert all(s.distinct > 0 and s.top and s.top[0].text == "7" for s in later)
    distinct = [s.distinct for s in snaps]
    assert distinct == sorted(distinct)                       # monotone
    assert abs(distinct[-1] - final.distinct) <= 4 * stats.HLL_RSE * final.distinct + 10
    assert all(s.top[0].count <= final.top[0].count for s in later)


def test_snapshots_count_values_not_spellings(make_doc, backend, monkeypatch) -> None:
    """1, 1.0 and 1e0 are one value: a snapshot showed 3 (the raw spellings) and the final 1."""
    monkeypatch.setattr(stats, "PARTIAL_SECONDS", 0.0)
    monkeypatch.setattr(stats, "PARTIAL_COST_FACTOR", 0.0)
    monkeypatch.setattr(stats, "CHUNK_RECORDS", 50)
    lines = [b'{"y": %s}' % [b"1", b"1.0", b"1e0", b"10E-1"][i % 4] for i in range(400)]
    doc = make_doc(b"\n".join(lines) + b"\n", "spell.jsonl")
    snaps: list[Any] = []
    final = field_stats(doc, "y", strategy="extract", on_partial=lambda fs: snaps.append(fs[0]))[0]
    assert final.distinct == 1 and snaps and all(s.distinct == 1 for s in snaps)


# ==================================================================================
# the sum's scale and the number texts: the same in every mode, strategy and backend
# ==================================================================================
@pytest.mark.parametrize("seed", [62, 63, 64, 65])
def test_number_stats_are_identical_in_every_mode(make_doc, backend, seed: int) -> None:
    """A multi-valued field's dict keys a value by its canonical token (1.100 -> 1.1) while the
    sketches add every spelling: the sum's text had the scale of whatever came after the switch
    to sketches (exact '...757.4', a tiny budget '...757.400' -- pure-Python core, parse strategy).
    Now the scale is the finest spelling's in every mode (1.10 + 2.20 = 3.30)."""
    from test_stats import FIELDS, document_bytes, stat_records

    data, _kw = document_bytes(stat_records(200, seed=seed), "array")
    doc = make_doc(data)
    want = None
    for strategy in ("extract", "parse"):
        runs = [field_stats(doc, FIELDS, strategy=strategy),                               # dicts
                field_stats(doc, FIELDS, memory_limit=1000, strategy=strategy),            # sketches
                field_stats(doc, FIELDS, exact=True, memory_limit=1000, strategy=strategy)]  # spilled runs
        for res in runs:
            got = [(fs.field, fs.number, fs.types, fs.values, fs.string, fs.arr_len, fs.obj_keys) for fs in res]
            if want is None:
                want = got
            assert got == want, strategy


# ==================================================================================
# R5A-11
# ==================================================================================
def test_numbers_left_out_of_the_sum_are_reported(make_doc, lang_en) -> None:
    doc = make_doc(b'[{"n": 1e99999999999999999999}, {"n": 5}, {"n": 7}, {"n": -1e-99999999999999999999}]')
    fs = field_stats(doc, "n")[0]
    nm = fs.number
    assert (nm.count, nm.sum_skipped, nm.sum_text, nm.mean_text, nm.exact_sum) == (4, 2, "12", "6", False)
    assert fs.warnings == ("stats.warn.sum_skipped",)
    assert "2 numbers were left out of the sum and the mean" in fs.warning_texts()[0]
    assert nm.to_dict()["sum_skipped"] == 2
    doc = make_doc(b'[{"n": 1e99999999999999999999}, {"n": 3}]')
    fs = field_stats(doc, "n")[0]
    assert (fs.number.sum_text, fs.number.mean_text) == ("3", "3")
    assert "1 number was left out" in fs.warning_texts()[0]
    doc = make_doc(b'[{"n": 1e99999999999999999999}]')
    nm = field_stats(doc, "n")[0].number
    assert (nm.sum_text, nm.mean_text, nm.sum_skipped) == ("0", "—", 1)


def test_left_out_numbers_do_not_make_the_rest_of_the_sum_approximate(make_doc, lang_en) -> None:
    big = "9" * 4500                     # a > 4 000-digit token: summed as a Decimal (exactly)
    doc = make_doc(('[{"n": %s}, {"n": 1}, {"n": 1e99999999999999999999}]' % big).encode())
    fs = field_stats(doc, "n")[0]
    assert fs.number.sum_text == "1" + "0" * 4500 and fs.number.sum_skipped == 1
    assert fs.warnings == ("stats.warn.sum_skipped",)          # not "approximate, 50 digits"
