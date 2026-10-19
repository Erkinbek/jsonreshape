"""jsonreshape.stats sketches (docs/ARCHITECTURE.md §14.0/§14.1; s5/map_stats.md §4.4):

* HyperLogLog p = 14: relative error within 4 sigma (4 x 0.81 %) on 100 000 .. 1 000 000
  distinct values, linear counting for small cardinalities, idempotence, union by register max;
* Misra-Gries with batch decrement: every kept count ``c`` satisfies ``c <= true <= c + D`` (records
  too), untracked values occur at most ``D`` times, ``D <= n / (k + 1)``; merges keep the bound;
* end to end: a tiny budget gives the approximate flags, estimates within the bounds of the exact
  run and the warning; the results are identical across processes with different
  ``PYTHONHASHSEED`` (blake2b, never Python's ``hash()``).
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from jsonreshape import stats
from jsonreshape.dupes import key_hash
from jsonreshape.reader import Document
from jsonreshape.stats import HLL_RSE, field_stats

ROOT = Path(__file__).resolve().parents[1]


def h128(i: int, prefix: str = "v") -> int:
    return key_hash(b"s" + f"{prefix}{i}".encode())


def hll_of(n: int, prefix: str = "v") -> stats._HLL:
    hll = stats._HLL()
    for i in range(n):
        hll.add(h128(i, prefix) & ((1 << 64) - 1))
    return hll


# ----------------------------------------------------------------------------------
# HyperLogLog
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("n", [100_000, 250_000])
def test_hll_error_within_four_sigma(n: int) -> None:
    est = hll_of(n).estimate()
    assert abs(est - n) / n <= 4 * HLL_RSE, (n, est)


def test_hll_error_within_four_sigma_at_a_million() -> None:
    n = 1_000_000
    est = hll_of(n, "m").estimate()
    assert abs(est - n) / n <= 4 * HLL_RSE, est


@pytest.mark.parametrize("n", [0, 1, 10, 1000, 20000])
def test_hll_small_cardinalities_use_linear_counting(n: int) -> None:
    est = hll_of(n, "s").estimate()
    assert abs(est - n) <= max(1.0, 0.02 * n), (n, est)


def test_hll_is_idempotent_and_merges_by_register_max() -> None:
    a = hll_of(5000, "a")
    before = bytes(a.reg)
    for i in range(5000):
        a.add(h128(i, "a") & ((1 << 64) - 1))
    assert bytes(a.reg) == before
    b = hll_of(7000, "b")
    union = stats._HLL()
    for i in range(5000):
        union.add(h128(i, "a") & ((1 << 64) - 1))
    for i in range(7000):
        union.add(h128(i, "b") & ((1 << 64) - 1))
    a.merge(b)
    assert bytes(a.reg) == bytes(union.reg)
    assert abs(a.estimate() - 12000) / 12000 < 4 * HLL_RSE


# ----------------------------------------------------------------------------------
# Misra-Gries
# ----------------------------------------------------------------------------------
def zipf_stream(n: int, seed: int, universe: int = 5000) -> list[int]:
    rng = random.Random(seed)
    weights = [1.0 / (r + 1) ** 1.1 for r in range(universe)]
    return rng.choices(range(universe), weights=weights, k=n)


def check_mg(mg: stats._MG, truth: Counter, n: int, records: dict[int, int] | None = None) -> None:
    d = mg.d
    assert d <= n / (mg.k + 1)
    assert len(mg.c) <= 2 * mg.k
    for item, e in mg.c.items():
        true = truth[item]
        assert e[0] <= true <= e[0] + d, (item, e[0], true, d)
        if records is not None:
            assert e[1] <= records[item] <= e[1] + d
    for item, true in truth.items():
        if item not in mg.c:
            assert true <= d, (item, true, d)


@pytest.mark.parametrize("k", [10, 100, 1000])
def test_misra_gries_bounds(k: int) -> None:
    items = zipf_stream(100_000, seed=k)
    mg = stats._MG(k)
    for pos, item in enumerate(items):
        mg.add(item, pos, 0, None)
    check_mg(mg, Counter(items), len(items), records={i: c for i, c in Counter(items).items()})
    # the heavy hitters are kept, in the right order
    top = sorted(mg.c.items(), key=lambda kv: -kv[1][0])[:3]
    assert [item for item, _ in top] == [0, 1, 2]


def test_misra_gries_records_and_weighted_seeding() -> None:
    rng = random.Random(3)
    # records: each record holds a few values (a value counted once per record)
    truth: Counter = Counter()
    recs: Counter = Counter()
    mg = stats._MG(50)
    for o in range(20_000):
        vals = [rng.choice(range(400)) if rng.random() < 0.5 else rng.randrange(20) for _ in range(rng.randint(1, 4))]
        for v in vals:
            truth[v] += 1
            mg.add(v, o, 0, None)
        for v in set(vals):
            recs[v] += 1
    check_mg(mg, truth, sum(truth.values()), records=recs)
    # seeding from exact counts (the overflow of the exact phase) keeps the bound
    seeded = stats._MG(50)
    for v, c in truth.items():
        seeded.add_weighted(v, c, recs[v], 0, 0, 0, None)
    seeded.prune()
    check_mg(seeded, truth, sum(truth.values()), records=recs)


def test_misra_gries_merge_keeps_the_bound() -> None:
    a_items = zipf_stream(30_000, seed=1)
    b_items = zipf_stream(40_000, seed=2)
    a, b = stats._MG(64), stats._MG(64)
    for i, x in enumerate(a_items):
        a.add(x, i, 0, None)
    for i, x in enumerate(b_items):
        b.add(x, 100_000 + i, 0, None)
    a.merge(b)
    total = len(a_items) + len(b_items)
    check_mg(a, Counter(a_items) + Counter(b_items), total)


# ----------------------------------------------------------------------------------
# end to end
# ----------------------------------------------------------------------------------
@pytest.fixture
def skewed_doc(tmp_path: Path):
    rng = random.Random(9)
    lines = []
    for i in range(60_000):
        hot = rng.choice(["Toshkent", "Xiva", "Nukus", "Buxoro"]) if rng.random() < 0.3 else f"u{rng.randrange(40000)}"
        lines.append(json.dumps({"id": i, "c": hot, "t": [f"t{rng.randrange(30000)}", "x"]}, ensure_ascii=False))
    p = tmp_path / "skewed.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    doc = Document.open(p)
    doc.build_index()
    yield doc
    doc.close()


def test_sketch_mode_end_to_end(skewed_doc, lang_en) -> None:
    exact = {fs.field: fs for fs in field_stats(skewed_doc, ["id", "c", "t[*]"])}
    approx = {fs.field: fs for fs in field_stats(skewed_doc, ["id", "c", "t[*]"], memory_limit=300_000)}
    for f, fs in approx.items():
        ex = exact[f]
        assert ex.distinct_exact and ex.top_exact
        assert not fs.distinct_exact and not fs.top_exact and fs.distinct_rse == HLL_RSE, f
        assert abs(fs.distinct - ex.distinct) / ex.distinct <= 4 * HLL_RSE, (f, fs.distinct, ex.distinct)
        # D <= values / (k + 1): k = MG_COUNTERS, fewer when the sketches share the budget (here
        # 300 kB for 3 fields: MG_MIN; review R5A-2), never below MG_MIN
        assert fs.top_bound <= fs.values / (stats.MG_MIN + 1)
        truth = {v.text: (v.count, v.records) for v in field_stats(skewed_doc, f, top=stats.TOP_MAX)[0].top}
        for v in fs.top:
            want = truth.get(v.text)
            if want is not None:
                assert v.count <= want[0] <= v.count + v.error and v.records <= want[1] <= v.records + v.error
        # everything else stays exact
        assert (fs.values, fs.types, fs.base, fs.present, fs.string) == (ex.values, ex.types, ex.base, ex.present,
                                                                         ex.string)
        assert fs.number == ex.number
        assert "stats.warn.approximate" in fs.warnings
        assert "HyperLogLog (±0.8 %)" in " ".join(fs.warning_texts())
    # the frequent values are found with their exact first records
    c = approx["c"]
    assert [v.text for v in c.top[:4]] == [v.text for v in exact["c"].top[:4]]
    assert [v.first for v in c.top[:4]] == [v.first for v in exact["c"].top[:4]]
    tx = approx["t[*]"]
    assert tx.top[0].text == '"x"' and tx.top[0].records <= 60000 <= tx.top[0].records + tx.top[0].error


SCRIPT = """
import json, sys
from jsonreshape.reader import Document
from jsonreshape.stats import field_stats
doc = Document.open(sys.argv[1]); doc.build_index()
res = field_stats(doc, ["id", "c", "t[*]"], memory_limit=200_000)
out = []
for fs in res:
    d = fs.to_dict()
    d.pop("elapsed")
    out.append(d)
print(json.dumps(out, sort_keys=True))
doc.close()
"""


def test_results_do_not_depend_on_the_hash_seed(skewed_doc) -> None:
    outputs = []
    for seed in ("0", "12345", "random"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        res = subprocess.run([sys.executable, "-c", SCRIPT, str(skewed_doc.source_path)], cwd=str(ROOT), env=env,
                             capture_output=True, text=True, timeout=300, check=True)
        outputs.append(res.stdout.strip().splitlines()[-1])
    assert outputs[0] == outputs[1] == outputs[2]
    data = json.loads(outputs[0])
    assert not data[0]["distinct_exact"]
