"""jsonreshape.stats histograms (stage 7; stats.py module docstring, "Histograms").

* nice bin edges (steps 1, 2, 2.5, 5 x 10^k; integer edges with half-open bins for integral data; one bin for a
  single value; at most ``bins`` bins; exact Decimal / int edges for ``1e400`` and 5 000-digit integers);
* the exact histogram of a field against a brute-force reference (every record parsed, exact Decimal values,
  the same edges), single- and multi-valued fields, numbers beyond the Decimal range left out;
* the reservoir sample: exact up to ``k`` distinct values, then uniform (per-item and weighted insertions,
  merges of every combination) within statistical tolerance; sampled histograms scaled to the total within
  tolerance in the dict mode (many distinct numbers) and in sketch mode (a tiny budget);
* reproducible results (the sample is seeded from the field), memory accounting (``_hist_k``, ``hist_bytes``
  inside ``total()`` and outside the sketch bytes), string-length histograms (exact, then grouped), live
  snapshots carrying the material, ``to_dict()`` / the text output unchanged, the text block of
  :func:`format_histogram` in both languages.
"""

from __future__ import annotations

import bisect
import math
import random
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec, stats
from jsonreshape.i18n import t
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.stats import (
    HIST_MIN,
    HIST_SAMPLE,
    Histogram,
    bin_label,
    field_stats,
    format_histogram,
    nice_edges,
    number_text,
)

NICE = {Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5)}
TOKENS = ["0.1", "1e400", "12345678901234567890", "-0.0", "1.10", "7", "-3", "2.50", "1E5", "0", "99.99",
          "1e99999999999999999999", "5e-324", "-0", "282"]


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []
    n = [0]

    def make(lines: list[str], *, settings: Settings | None = None) -> Document:
        n[0] += 1
        p = tmp_path / f"doc{n[0]}.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        doc = Document.open(p, settings=settings)
        docs.append(doc)
        doc.build_index()
        return doc

    yield make
    for d in docs:
        d.close()


def dec(token: str) -> Decimal | None:
    """The exact value of a number token; ``None`` beyond the Decimal range (left out of histograms)."""
    try:
        d = Decimal(token)
    except ArithmeticError:
        return None
    return d if d.is_finite() else None


def brute_counts(values: list[Decimal], edges: list) -> list[int]:
    """Reference bin counts: bin ``i`` holds ``edges[i] <= v < edges[i + 1]``, the last bin also ``v == last``."""
    counts = [0] * (len(edges) - 1)
    last = len(counts) - 1
    for v in values:
        i = bisect.bisect_right(edges, v) - 1
        counts[max(0, min(last, i))] += 1
    return counts


def step_of(edges: list) -> Decimal:
    return Decimal(edges[1]) - Decimal(edges[0])


def is_nice(step: Decimal) -> bool:
    s = step.normalize()
    return any(s == n.scaleb(k) for n in NICE for k in range(-30, 40))


# ----------------------------------------------------------------------------------
# nice edges, number texts, labels
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("lo, hi, bins, integral, expected", [
    (0, 100, 32, True, list(range(0, 106, 5))),                      # step 5: 21 bins, the last beyond the maximum
    (0, 59, 32, True, list(range(0, 61, 2))),                        # step 2: 30 bins
    (0, 3, 32, True, [0, 1, 2, 3, 4]),                               # every integer its own bin
    (5, 5, 32, True, [5, 5]),                                        # one distinct value: one bin
    (-7, 7, 4, True, [-10, -5, 0, 5, 10]),                           # step 5 (2.5 is no integer): aligned edges
    (Decimal("0.1"), Decimal("1.10"), 32, False,
     [Decimal(k) * Decimal("0.05") for k in range(2, 23)]),          # step 0.05: 20 bins
    (Decimal("0"), Decimal("1"), 1, False, [Decimal(0), Decimal(1)]),
])
def test_nice_edges_cases(lo: Any, hi: Any, bins: int, integral: bool, expected: list) -> None:
    edges = nice_edges(lo, hi, bins, integral)
    assert edges == expected
    if integral:
        assert all(type(e) is int for e in edges)
    else:
        assert all(isinstance(e, Decimal) for e in edges)


def test_nice_edges_properties() -> None:
    rng = random.Random(7)
    for _ in range(300):
        bins = rng.choice([1, 2, 5, 10, 32, 100])
        integral = rng.random() < 0.5
        if integral:
            lo = rng.randint(-10 ** 6, 10 ** 6)
            hi = lo + rng.randint(0, 10 ** rng.randint(0, 7))
        else:
            lo = Decimal(rng.randint(-10 ** 9, 10 ** 9)).scaleb(rng.randint(-6, 3))
            hi = lo + Decimal(rng.randint(0, 10 ** 6)).scaleb(rng.randint(-6, 3))
        edges = nice_edges(lo, hi, bins, integral)
        assert 2 <= len(edges) <= bins + 1
        assert edges == sorted(edges) and edges[0] <= lo
        if lo == hi:
            assert edges == [lo, hi]
            continue
        if bins == 1 and lo < 0 and (hi > 0 or (integral and hi >= 0)):
            assert edges == ([lo, hi + 1] if integral else [lo, hi])   # one bin with 0 inside: the exact extremes
            continue
        assert edges[-1] > hi if integral else edges[-1] >= hi
        step = step_of(edges)
        assert is_nice(step), (lo, hi, bins, step)
        assert all(Decimal(b) - Decimal(a) == step for a, b in zip(edges, edges[1:]))
        assert Decimal(edges[0]) % step == 0
        if integral:
            assert all(type(e) is int for e in edges) and step >= 1


def test_nice_edges_exact_beyond_floats() -> None:
    edges = nice_edges(Decimal("-0.0"), Decimal("1E+400"), 32, False)
    assert len(edges) == 21 and edges[-1] == Decimal("1E+400") and step_of(edges) == Decimal("5E+398")
    assert number_text(edges[1]) == "5E+398" and number_text(edges[0]) == "0"
    big = 10 ** 5000
    edges = nice_edges(0, big, 10, True)
    assert edges[-1] > big and len(edges) <= 11 and all(type(e) is int for e in edges)
    assert number_text(edges[-1]).startswith("1.2E+5000")
    with pytest.raises(ValueError):
        nice_edges(0, 1, 0)
    with pytest.raises(ValueError):
        nice_edges(0, 1, stats.HIST_BINS_MAX + 1)
    with pytest.raises(ValueError):
        nice_edges(Decimal("Infinity"), 1, 4)


def test_number_text_is_exact_json() -> None:
    assert number_text(5) == "5" and number_text(-12345678901234567890) == "-12345678901234567890"
    assert number_text(Decimal("2.50")) == "2.5" and number_text(Decimal("1E+2")) == "100"
    assert number_text(Decimal("-0.0")) == "0" and number_text(Decimal("5E-324")) == "5E-324"
    assert number_text(Decimal("1E+400")) == "1E+400"
    for text in ("5", "2.5", "100", "5E-324", "1E+400", "-12345678901234567890"):
        assert codec.loads(text.encode()) is not None                  # valid JSON number texts
    assert number_text(10 ** 5000) == "1E+5000"                        # beyond Python's str() digit limit


@pytest.mark.parametrize("lang", ["uz", "en"])
def test_bin_labels_and_text_block(lang: str) -> None:
    from jsonreshape import i18n

    old = i18n.get_language()
    i18n.set_language(lang)
    try:
        edges = [0, 5, 10, 15]
        assert bin_label(edges, 0) == "0 – 4" and bin_label(edges, 2) == "10 – 14"
        assert bin_label([3, 4], 0) == "3" and bin_label([7, 7], 0) == "7"
        dedges = [Decimal("0.1"), Decimal("0.15"), Decimal("0.2")]
        assert bin_label(dedges, 0) == "0.1 ≤ x < 0.15" and bin_label(dedges, 1) == "0.15 ≤ x ≤ 0.2"
        text = format_histogram(Histogram(edges, [10, 0, 30], True, 40))
        lines = text.splitlines()
        assert lines[0] == t("stats.hist.title", bins=3, count="40")
        assert "0 – 4" in lines[1] and lines[1].rstrip().endswith("#" * 10) and "10 – 14" in lines[3]
        assert lines[3].rstrip().endswith("#" * 30) and lines[2].rstrip().endswith("0%")
        sampled = format_histogram(Histogram([0, 1], [1000], False, 100))
        assert sampled.splitlines()[0] == t("stats.hist.title_sampled.one", bins=1, count="1 000" if lang == "uz" else "1,000",
                                            sample="100")
    finally:
        i18n.set_language(old)


# ----------------------------------------------------------------------------------
# the exact histogram against a brute-force reference
# ----------------------------------------------------------------------------------
def test_exact_histogram_matches_brute_force(make_doc) -> None:
    rng = random.Random(11)
    lines = []
    values: list[Decimal] = []
    arr_values: list[Decimal] = []
    skipped = 0
    for i in range(2000):
        tok = rng.choice(TOKENS)
        d = dec(tok)
        if d is None:
            skipped += 1
        else:
            values.append(d)
        arr = [rng.choice(TOKENS) for _ in range(rng.randint(0, 4))]
        for a in arr:
            da = dec(a)
            if da is not None:
                arr_values.append(da)
        lines.append('{"id": %d, "v": %s, "arr": [%s], "s": "%s"}' % (i, tok, ", ".join(arr), "x" * (i % 9)))
    doc = make_doc(lines)
    fs_id, fs_v, fs_arr, fs_s = field_stats(doc, ["id", "v", "arr[*]", "s"])
    for bins in (1, 5, 32, 200):
        h = fs_v.histogram(bins)
        assert h is not None and h.exact and h.sample_size == len(values) and h.bins <= bins
        assert h.counts == brute_counts(values, h.edges) and h.total == len(values)
        assert h.edges[0] <= min(values) and h.edges[-1] >= max(values)
        assert all(isinstance(e, Decimal) for e in h.edges)         # 0.1 / 1.10: not integral
        ha = fs_arr.histogram(bins)
        assert ha is not None and ha.exact and ha.counts == brute_counts(arr_values, ha.edges)
    assert fs_v.number_hist.skipped == skipped and fs_v.number is not None
    assert fs_v.number.count == len(values) + skipped                  # NumberStats unchanged: every number counted
    h = fs_id.histogram(32)
    assert h.exact and all(type(e) is int for e in h.edges) and h.edges[-1] > 1999
    assert h.counts == brute_counts([Decimal(i) for i in range(2000)], h.edges) and h.total == 2000
    assert fs_s.histogram() is None and fs_id.length_histogram() is None
    lh = fs_s.length_histogram(32)
    assert lh is not None and lh.exact and lh.edges == list(range(10)) and lh.total == 2000
    assert lh.counts == [len([i for i in range(2000) if i % 9 == n]) for n in range(9)]
    assert fs_s.string is not None and (fs_s.string.min_len, fs_s.string.max_len) == (0, 8)


def test_no_numbers_and_only_non_finite_numbers(make_doc) -> None:
    doc = make_doc(['{"a": "x", "b": 1e99999999999999999999}', '{"a": "y", "b": -1e99999999999999999999}'])
    fa, fb = field_stats(doc, ["a", "b"])
    assert fa.histogram() is None and fa.number_hist is None
    assert fb.histogram() is None and fb.number_hist is not None and fb.number_hist.skipped == 2
    assert fb.number is not None and fb.number.count == 2                # still counted as numbers


def test_streamed_huge_records_feed_the_same_histogram(make_doc) -> None:
    rng = random.Random(5)
    lines = []
    for i in range(300):
        arr = [str(rng.randint(-50, 50)) for _ in range(rng.randint(0, 40))]
        lines.append('{"id": %d, "arr": [%s], "pad": "%s"}' % (i, ", ".join(arr), "p" * (i % 50)))
    plain = make_doc(lines)
    huge = make_doc(lines, settings=Settings(large_node_bytes=120))   # every record streamed (ijson)
    for field in ("id", "arr[*]", "pad"):
        a = field_stats(plain, field)[0]
        b = field_stats(huge, field)[0]
        assert a.histogram(16) == b.histogram(16) and a.length_histogram(16) == b.length_histogram(16), field
        if a.number_hist is not None:
            assert (a.number_hist.n, a.number_hist.lo, a.number_hist.hi) == (b.number_hist.n, b.number_hist.lo, b.number_hist.hi)


# ----------------------------------------------------------------------------------
# the reservoir sample
# ----------------------------------------------------------------------------------
def shares(items: list[Any]) -> dict[Any, float]:
    c = Counter(items)
    return {k: v / len(items) for k, v in c.items()}


def test_sample_is_exact_until_k_then_a_reservoir() -> None:
    s = stats._Sample(4, "f")
    for v, c in ((1, 3), (Decimal("2.5"), 2), (3, 1), (4, 4)):
        s.add(v, c, isinstance(v, int))
    assert s.exact and s.n == 10 and s.size == 4 and (s.lo, s.hi) == (1, 4) and not s.integral
    s.add(Decimal("1.0"), 2)                                            # 1 == 1.0: the same key
    assert s.exact and s.size == 4 and s.n == 12 and s.d[1] == 5
    s.add(Decimal("Infinity"), 3)
    assert s.skipped == 3 and s.n == 12
    h = s.histogram(32)
    assert h.exact and h.sample_size == 12 and sum(h.counts) == 12 and h.edges[0] <= 1 and h.edges[-1] >= 4
    s.add(9, 1)                                                          # a 5th distinct value: the reservoir
    assert not s.exact and s.size == 4 and s.n == 13 and s.hi == 9
    for i in range(1000):
        s.add(100 + i, 1)
    assert s.size == 4 and s.n == 1013 and s.hi == 1099
    h = s.histogram(10)
    assert not h.exact and h.sample_size == 4 and 1000 <= sum(h.counts) <= 1026


def test_reservoir_is_uniform_for_items_weights_and_merges() -> None:
    k = 2000
    sigma = math.sqrt(0.1 * 0.9 / k)
    s = stats._Sample(k, "u")
    for i in range(200_000):
        s.add(i, 1)                                                      # 200 000 distinct values, 10 classes
    assert not s.exact and s.size == k and s.n == 200_000 and (s.lo, s.hi) == (0, 199_999)
    for v, share in shares([v % 10 for v in s.items]).items():
        assert abs(share - 0.1) <= 5 * sigma, (v, share)
    # weighted insertions: one value with as many occurrences as everything before gets about half
    s.add(1_000_000, 200_000)
    assert s.n == 400_000 and s.size == k and s.hi == 1_000_000
    assert abs(shares(s.items).get(1_000_000, 0.0) - 0.5) <= 5 * math.sqrt(0.25 / k)
    # exact + exact (fits), exact + exact (overflows), exact + reservoir, reservoir + exact, reservoir + reservoir
    a, b = stats._Sample(100, "a"), stats._Sample(100, "b")
    for i in range(30):
        a.add(i, 1)
    for i in range(30, 60):
        b.add(i, 1)
    a.merge(b)
    assert a.exact and a.n == 60 and a.size == 60 and (a.lo, a.hi) == (0, 59)
    c = stats._Sample(100, "c")
    for i in range(1000, 1200):
        c.add(i, 1)
    a.merge(c)                                                           # 260 distinct: a reservoir of 100
    assert not a.exact and a.size == 100 and a.n == 260 and a.hi == 1199
    frac_c = sum(1 for v in a.items if v >= 1000) / 100
    assert abs(frac_c - 200 / 260) <= 5 * math.sqrt(0.77 * 0.23 / 100)
    e = stats._Sample(100, "e")
    for i in range(50):
        e.add(-i, 1)                                                     # exact, 50 occurrences
    r = stats._Sample(100, "r")
    for i in range(950):
        r.add(5000 + i, 1)                                               # a reservoir of 950 occurrences
    e.merge(r)                                                           # exact + reservoir
    assert not e.exact and e.n == 1000 and e.size == 100 and e.lo == -49 and e.hi == 5949
    assert abs(sum(1 for v in e.items if v < 0) / 100 - 0.05) <= 5 * math.sqrt(0.05 * 0.95 / 100)
    r2 = stats._Sample(100, "r2")
    for i in range(950):
        r2.add(5000 + i, 1)
    e2 = stats._Sample(100, "e2")
    for i in range(50):
        e2.add(-i, 1)
    r2.merge(e2)                                                         # reservoir + exact (streamed in)
    assert not r2.exact and r2.n == 1000 and r2.size == 100
    assert abs(sum(1 for v in r2.items if v < 0) / 100 - 0.05) <= 5 * math.sqrt(0.05 * 0.95 / 100)
    r3 = stats._Sample(100, "r3")
    for i in range(3000):
        r3.add(i, 1)
    r2.merge(r3)                                                         # reservoir + reservoir
    assert r2.n == 4000 and r2.size == 100
    assert abs(sum(1 for v in r2.items if 0 <= v < 3000) / 100 - 0.75) <= 5 * math.sqrt(0.75 * 0.25 / 100)
    # the sample keeps going after a merge (the threshold was re-seated)
    for i in range(4000):
        r2.add(-1, 1)
    assert r2.n == 8000 and abs(sum(1 for v in r2.items if v == -1) / 100 - 0.5) <= 5 * math.sqrt(0.25 / 100)
    # an empty sample takes the other's state
    z = stats._Sample(100, "z")
    z.merge(r2)
    assert z.n == r2.n and z.size == r2.size and not z.exact
    z2 = stats._Sample(100, "z2")
    z2.merge(e2)
    assert z2.exact and z2.n == 50 and z2.size == 50
    # copies are independent
    cp = r2.copy()
    cp.add(7, 10_000)
    assert cp.n == r2.n + 10_000 and r2.size == cp.size == 100


def test_sampled_histogram_scaled_within_tolerance(make_doc) -> None:
    n = 90_000
    vals = [(i * 7919) % 100_003 for i in range(n)]                      # > HIST_SAMPLE distinct values
    assert len(set(vals)) > HIST_SAMPLE
    doc = make_doc(['{"v": %d}' % v for v in vals])
    fs = field_stats(doc, "v")[0]
    assert fs.distinct_exact and fs.number is not None and fs.number.count == n
    h = fs.histogram(10)
    assert h is not None and not h.exact and h.sample_size == HIST_SAMPLE and h.bins <= 10
    assert abs(h.total - n) <= h.bins                                  # scaled counts (rounding per bin)
    truth = brute_counts([Decimal(v) for v in vals], h.edges)
    for est, true in zip(h.counts, truth):
        p = true / n
        sigma = n * math.sqrt(p * (1 - p) / HIST_SAMPLE) + 1
        assert abs(est - true) <= 5 * sigma, (est, true)
    assert h.edges[0] <= min(vals) < max(vals) < h.edges[-1]           # the exact extremes, not the sample's
    # the same document again: the same sample (seeded from the field), another field another sample
    again = field_stats(doc, "v")[0]
    assert again.histogram(10) == h and again.number_hist.items == fs.number_hist.items
    assert stats._seed_of("v") != stats._seed_of("w")


def test_sketch_mode_keeps_a_small_sample(make_doc) -> None:
    n = 40_000
    vals = [(i * 7919) % 100_003 for i in range(n)]
    doc = make_doc(['{"v": %d, "s": "%s"}' % (v, "s" * (v % 20)) for v in vals])
    fs, fss = field_stats(doc, ["v", "s"], memory_limit=300_000)         # a tiny budget: sketches
    assert not fs.distinct_exact
    h = fs.histogram(8)
    assert h is not None and not h.exact and h.sample_size == HIST_MIN
    truth = brute_counts([Decimal(v) for v in vals], h.edges)
    for est, true in zip(h.counts, truth):
        p = true / n
        assert abs(est - true) <= 6 * (n * math.sqrt(p * (1 - p) / HIST_MIN) + 1), (est, true)
    exact = field_stats(doc, ["v", "s"])
    assert fs.number == exact[0].number and fss.string == exact[1].string   # everything else unchanged
    assert fss.length_histogram(32) == exact[1].length_histogram(32)         # lengths stay exact in every mode


# ----------------------------------------------------------------------------------
# memory accounting
# ----------------------------------------------------------------------------------
def test_hist_k_follows_the_budget() -> None:
    assert stats._hist_k(1500, 1) == HIST_MIN
    assert stats._hist_k(64 << 20, 1) == HIST_SAMPLE and stats._hist_k(128 << 20, 2) == HIST_SAMPLE
    assert stats._hist_k(64 << 20, 2) == (64 << 20) // stats.HIST_SHARE // (2 * stats.HIST_ENTRY_COST) == 41943
    mid = stats._hist_k(4 << 20, 1)
    assert mid == (4 << 20) // stats.HIST_SHARE // stats.HIST_ENTRY_COST and HIST_MIN < mid < HIST_SAMPLE
    assert stats._hist_k(4 << 20, 4) == mid // 4
    accset = stats._AccSet(10_000, False, None, None, [], hist_k=16)
    acc = stats._FieldAcc(False, accset, "f")
    assert acc.agg.hist.k == 16 and accset.total() == 0
    for i in range(500):
        acc.agg.add(stats.T_NUMBER, stats._number_info(str(i).encode()), 1, 0, 0, str(i).encode())
        acc.agg.add(stats.T_STRING, i % 7, 1, 0, 0, b'"x"')
    assert acc.agg.hist.size == 16 and acc.agg.hist.nbytes == 16 * stats.HIST_ENTRY_COST
    assert acc.agg.lens.nbytes == 7 * stats.ENTRY_COST
    assert accset.hist_bytes() == acc.agg.nbytes and accset.total() == accset.hist_bytes()
    assert acc.sketch_memory() == 0 and accset.sketch_bytes() == 0        # not sketch bytes: MG sizing unchanged
    assert acc.agg.hist.nbytes <= 16 * stats.HIST_ENTRY_COST


def test_length_counts_exact_then_grouped(make_doc, monkeypatch) -> None:
    doc = make_doc(['{"s": "%s"}' % ("a" * (i % 13)) for i in range(260)])
    fs = field_stats(doc, "s")[0]
    lh = fs.length_histogram(32)
    assert lh.exact and lh.edges == list(range(14)) and lh.counts == [20] * 13 and fs.length_hist.width == 1
    monkeypatch.setattr(stats, "LEN_COUNTS", 8)
    fs = field_stats(doc, "s")[0]
    assert fs.length_hist.width == 2 and len(fs.length_hist.d) == 7
    lh = fs.length_histogram(32)
    assert not lh.exact and lh.total == 260 and lh.sample_size == 260
    assert sum(lh.counts) == 260 and lh.edges[0] == 0 and lh.edges[-1] >= 13
    # merging counts of different widths
    a = stats._LenCounts()
    b = stats._LenCounts()
    for i in range(20):
        a.add(i, 1)                                                      # > 8 distinct: width 2, then 4
    for i in range(3):
        b.add(i, 5)
    assert a.width == 4 and a.n == 20
    b.merge(a)
    assert b.width == 4 and b.n == 35 and sum(b.d.values()) == 35
    a2 = stats._LenCounts()
    a2.add(100, 2)
    a2.merge(b)
    assert a2.width == 4 and a2.n == 37


def test_to_dict_text_and_live_snapshots(make_doc, monkeypatch) -> None:
    doc = make_doc(['{"id": %d, "s": "%s"}' % (i, "q" * (i % 5)) for i in range(400)])
    fs_id, fs_s = field_stats(doc, ["id", "s"])
    assert "number_hist" not in fs_id.to_dict() and "length_hist" not in fs_s.to_dict()   # --json unchanged
    assert "hist" not in " ".join(fs_id.to_dict()) and t("stats.hist.title", bins=1, count="1")[:8] not in fs_id.format_text()
    monkeypatch.setattr(stats, "PARTIAL_SECONDS", 0.0)
    monkeypatch.setattr(stats, "CHUNK_RECORDS", 64)
    snaps: list[list[stats.FieldStats]] = []
    field_stats(doc, ["id", "s"], on_partial=snaps.append)
    assert snaps
    for a, b in snaps:
        assert a.partial and a.number_hist is not None and a.histogram(8) is not None and a.histogram(8).exact
        assert a.histogram(8).total == a.values and a.length_histogram() is None
        assert b.length_histogram(8) is not None and b.length_histogram(8).total == b.values
        assert b.length_histogram(8).edges == [0, 1, 2, 3, 4, 5]
    with pytest.raises(ValueError):
        fs_id.histogram(0)
