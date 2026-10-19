"""jsonreshape.dupes, the stage-5 extensions (docs/ARCHITECTURE.md §14.2).

* ``keep="none"`` (every member of a group of two or more is marked) and ``top`` (the largest
  groups) against the naive reference of ``test_dupes`` -- every kind, both core backends,
  both strategies, sources, spilled runs;
* :func:`iter_groups` (the generic walk: groups, records vs occurrences, positions, marking by
  keep, progress, cancellation) on hand-made entry streams;
* the public helpers :class:`KeyHasher` / :class:`HashRuns` and their stage-3 aliases
  (``offset`` / shared ``entries``, 128-bit pair entries, the context manager);
* :func:`find_duplicates_many` against a reference over the concatenated inputs (mixed kinds,
  ``$``, ``$key``, keep first / last / none, spills, huge records, warnings naming the file,
  edited / pinned inputs), :class:`KeepSource` / ``duplicate_source`` as record sources (also
  for ``writer.export``), progress, cancellation and clean-up;
* the bulk ``dedupe`` rule still refuses ``keep="none"``.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import dupes
from jsonreshape.bulk import BulkSpec
from jsonreshape.codec import loads as json_loads
from jsonreshape.dupes import (
    EXAMPLE_GROUPS,
    EXAMPLE_POSITIONS,
    TOP_MAX,
    HashRuns,
    KeepSource,
    KeyHasher,
    MultiDuplicateReport,
    PositionSource,
    find_duplicates,
    find_duplicates_many,
    iter_groups,
    key_hash,
    parse_key,
)
from jsonreshape.errors import Cancelled, EditError, UserInputError
from jsonreshape.i18n import has, t
from jsonreshape.paths import MISSING
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export
from overlay_helpers import OverlayBuilder
from test_dupes import (  # noqa: F401 - fixtures used by name
    KIND_CASES,
    POOL,
    backend,
    dup_records,
    make_doc,
    no_temp_dirs,
    norm,
    path_values,
    text_of,
)


# ----------------------------------------------------------------------------------
# references
# ----------------------------------------------------------------------------------
def groups_of(values: list[Any], positions: list[int] | None = None) -> list[list[int]]:
    """Groups of equal values (positions ascending), in order of their first position."""
    uniq = range(len(values)) if positions is None else sorted(set(positions))
    groups: dict[Any, list[int]] = {}
    for pos in uniq:
        v = values[pos]
        if v is not MISSING:
            groups.setdefault(norm(v), []).append(pos)
    return sorted(groups.values(), key=lambda g: g[0])


def marked(groups: list[list[int]], keep: str) -> list[int]:
    out: list[int] = []
    for g in groups:
        if len(g) < 2:
            continue
        out += g[1:] if keep == "first" else g[:-1] if keep == "last" else g
    return sorted(out)


def top_reference(values: list[Any], groups: list[list[int]], top: int) -> tuple[list[tuple[str, list[int]]], list[int]]:
    dup = [g for g in groups if len(g) > 1]
    best = sorted(dup, key=lambda g: (-len(g), g[0]))[:top]
    return [(text_of(values[g[0]]), g[:EXAMPLE_POSITIONS]) for g in best], [len(g) for g in best]


# ----------------------------------------------------------------------------------
# keep="none"
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["extract", "parse"])
@pytest.mark.parametrize("fmt, write_kw, open_kw", KIND_CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(KIND_CASES)])
def test_keep_none_marks_every_member(make_doc, backend, strategy, fmt, write_kw, open_kw) -> None:
    recs = dup_records(240, seed=sum(map(ord, fmt)) + 7 * len(write_kw))
    doc = make_doc(recs, fmt, **write_kw, **open_kw)
    values = path_values(recs, "k")
    groups = groups_of(values)
    rep = find_duplicates(doc, "k", keep="none", strategy=strategy)
    want = marked(groups, "none")
    assert rep.keep == "none" and list(rep.records) == want and rep.duplicates == len(want)
    assert rep.groups == sum(1 for g in groups if len(g) > 1)
    # every record of a group with >= 2 members, so: duplicates == the other modes' + one per group
    first = find_duplicates(doc, "k", strategy=strategy)
    assert rep.duplicates == first.duplicates + first.groups
    assert set(rep.records) == set(first.records) | {g[0] for g in groups if len(g) > 1}
    assert rep.examples == first.examples and rep.example_sizes == first.example_sizes
    assert rep.records.kind == "duplicates" and rep.records.is_ascending()


@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_keep_none_with_sources_and_spills(make_doc, backend, strategy, monkeypatch) -> None:
    recs = dup_records(500, seed=17)
    doc = make_doc(recs, "jsonl", format="jsonl", settings=Settings(large_node_bytes=200))
    values = path_values(recs, "k")
    rng = random.Random(4)
    shuffled = rng.sample(range(500), 300)
    repeated = [rng.randrange(500) for _ in range(400)]
    for src in (None, shuffled, repeated, range(100, 400)):
        positions = None if src is None else list(src)
        want = marked(groups_of(values, positions), "none")
        assert list(find_duplicates(doc, "k", src, keep="none", strategy=strategy).records) == want
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 2)
    monkeypatch.setattr(dupes, "MAX_FANIN", 3)
    monkeypatch.setattr(dupes, "MERGE_BATCH", 37)
    rep = find_duplicates(doc, "k", repeated, keep="none", memory_limit=dupes.ENTRY_BYTES * 11, strategy=strategy)
    assert rep.runs > 10 and list(rep.records) == marked(groups_of(values, repeated), "none")
    assert no_temp_dirs()


def test_keep_none_small_example(make_doc, lang_en) -> None:
    doc = make_doc(b'[{"k": 1}, {"k": 2}, {"k": 1.0}, {"x": 0}, {"k": 2}, {"k": 3}, {"k": 1e0}]')
    rep = find_duplicates(doc, "k", keep="None ")
    assert (rep.groups, rep.duplicates, list(rep.records)) == (2, 5, [0, 1, 2, 4, 6])
    assert rep.examples == [("1", [0, 2, 6]), ("2", [1, 4])]
    with pytest.raises(UserInputError) as ei:
        find_duplicates(doc, "k", keep="middle")
    assert ei.value.key == "dupes.error.keep" and "'none'" in str(ei.value)


def test_bulk_dedupe_rule_still_refuses_keep_none(lang_en) -> None:
    for keep in ("none", "NONE"):
        with pytest.raises(UserInputError) as ei:
            BulkSpec("dedupe", {"key": "id", "keep": keep}).validate()
        assert ei.value.key == "dupes.error.keep"
        assert str(ei.value) == "The record to keep in each group of duplicates must be 'first' or 'last', got: " \
                                f"{keep!r}"
    BulkSpec("dedupe", {"key": "id", "keep": "last"}).validate()


# ----------------------------------------------------------------------------------
# top groups
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("keep", ["first", "last", "none"])
def test_top_groups_match_the_reference(make_doc, backend, keep, monkeypatch) -> None:
    rng = random.Random(9)
    recs = []
    for i in range(900):
        r = rng.random()
        k: Any = rng.choice(POOL[:30]) if r < 0.85 else (MISSING if r < 0.9 else f"solo{i}")
        rec = {"n": i}
        if k is not MISSING:
            rec["k"] = k
        recs.append(rec)
    doc = make_doc(recs, "array", style="pretty2")
    values = path_values(recs, "k")
    groups = groups_of(values)
    for top in (1, 3, 25, 10000):
        rep = find_duplicates(doc, "k", keep=keep, top=top)
        texts, sizes = top_reference(values, groups, top)
        assert (rep.top_groups, rep.top_sizes) == (texts, sizes), top
        assert all(len(p) <= EXAMPLE_POSITIONS for _, p in rep.top_groups)
        assert list(rep.records) == marked(groups, keep)
    assert find_duplicates(doc, "k").top_groups == [] and find_duplicates(doc, "k").top_sizes == []
    # the same through spilled runs
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 5)
    monkeypatch.setattr(dupes, "CHECK_RECORDS", 11)
    rep = find_duplicates(doc, "k", keep=keep, top=7, memory_limit=dupes.ENTRY_BYTES * 20, block_bytes=4000)
    assert rep.runs > 5
    assert (rep.top_groups, rep.top_sizes) == top_reference(values, groups, 7)
    assert no_temp_dirs()


def test_examples_count(make_doc, lang_en) -> None:
    recs = [{"k": i % 30} for i in range(120)]           # 30 groups of 4
    doc = make_doc(recs, "array")
    default = find_duplicates(doc, "k")
    assert len(default.examples) == EXAMPLE_GROUPS == 20
    many = find_duplicates(doc, "k", examples=25)
    assert [t for t, _ in many.examples] == [str(i) for i in range(25)] and many.example_sizes == [4] * 25
    assert many.examples[:20] == default.examples and many.records.to_list() == default.records.to_list()
    none = find_duplicates(doc, "k", examples=0, top=2)
    assert none.examples == [] and none.example_sizes == [] and none.groups == 30
    assert [t for t, _ in none.top_groups] == ["0", "1"]
    for bad in (-1, TOP_MAX + 1, True, "5"):
        with pytest.raises(UserInputError) as ei:
            find_duplicates(doc, "k", examples=bad)
        assert ei.value.key == "dupes.error.examples" and str(TOP_MAX) in str(ei.value)


def test_top_ties_prefer_the_earlier_group(make_doc) -> None:
    keys = [5, 3, 5, 9, 3, 9, 7, 1, 1, 7]          # four groups of two, one of ... all equal sizes
    doc = make_doc([{"k": k} for k in keys], "jsonl", format="jsonl")
    rep = find_duplicates(doc, "k", top=3)
    assert rep.top_groups == [("5", [0, 2]), ("3", [1, 4]), ("9", [3, 5])]
    assert rep.top_sizes == [2, 2, 2]
    keys = [1] * 3 + [2] * 30 + [3] * 2 + [4] * 30
    doc = make_doc([{"k": k} for k in keys], "jsonl", format="jsonl")
    rep = find_duplicates(doc, "k", top=2)
    assert rep.top_sizes == [30, 30] and [txt for txt, _ in rep.top_groups] == ["2", "4"]
    assert rep.top_groups[0][1] == list(range(3, 23))


def test_top_argument_is_checked(make_doc) -> None:
    doc = make_doc(b'[{"k": 1}]')
    for top in (-1, TOP_MAX + 1, True, 1.5, "3", None):
        with pytest.raises(UserInputError) as ei:
            find_duplicates(doc, "k", top=top)  # type: ignore[arg-type]
        assert ei.value.key == "dupes.error.top"
    assert find_duplicates(doc, "k", top=TOP_MAX).top_groups == []


# ----------------------------------------------------------------------------------
# iter_groups
# ----------------------------------------------------------------------------------
def entries_of(pairs: list[tuple[int, int]]) -> list[int]:
    return sorted((h << 64) | p for h, p in pairs)


def test_iter_groups_walks_groups() -> None:
    pairs = [(7, 0), (7, 4), (7, 4), (7, 9), (3, 2), (3, 2), (5, 1), (9, 3), (9, 8), (2**127, 6), (2**127, 7)]
    stream = entries_of(pairs)
    got = list(iter_groups(stream))
    assert got == [(3, 2, 1, 2, None), (5, 1, 1, 1, None), (7, 0, 3, 4, None), (9, 3, 2, 2, None),
                   (2**127, 6, 2, 2, None)]
    got = list(iter_groups(iter(stream), min_records=2, positions=2))
    assert got == [(7, 0, 3, 4, [0, 4]), (9, 3, 2, 2, [3, 8]), (2**127, 6, 2, 2, [6, 7])]
    assert list(iter_groups(stream, min_records=3, positions=10)) == [(7, 0, 3, 4, [0, 4, 9])]
    assert list(iter_groups(stream, positions=1))[1] == (5, 1, 1, 1, [1])
    assert list(iter_groups([])) == []
    with pytest.raises(ValueError):
        list(iter_groups(stream, min_records=0))
    with pytest.raises(ValueError):
        list(iter_groups(stream, bits=bytearray(2), keep="all"))


@pytest.mark.parametrize("keep", ["first", "last", "none"])
def test_iter_groups_marks_by_keep(keep: str) -> None:
    rng = random.Random(3)
    pairs = [(rng.randrange(40), p) for p in range(600)]
    pairs += [(h, p) for h, p in pairs[:50]]            # repeated positions inside groups
    by_hash: dict[int, list[int]] = {}
    for h, p in sorted(set(pairs)):
        by_hash.setdefault(h, []).append(p)
    want = marked(sorted(by_hash.values(), key=lambda g: g[0]), keep)
    bits = bytearray((600 + 7) // 8)
    out = list(iter_groups(entries_of(pairs), min_records=2, bits=bits, keep=keep))
    got = [p for p in range(600) if bits[p >> 3] >> (p & 7) & 1]
    assert got == want
    assert len(out) == sum(1 for g in by_hash.values() if len(g) > 1)
    assert sum(o[2] for o in out) == sum(len(g) for g in by_hash.values() if len(g) > 1)
    assert sum(o[3] for o in out) == sum(1 for h, _ in pairs if len(by_hash[h]) > 1)


def test_iter_groups_progress_and_cancel(monkeypatch) -> None:
    monkeypatch.setattr(dupes, "MERGE_BATCH", 10)
    stream = entries_of([(i % 7, i) for i in range(95)])
    seen: list[int] = []

    class P:
        def update(self, done: int) -> None:
            seen.append(done)

    assert len(list(iter_groups(stream, progress=P()))) == 7   # type: ignore[arg-type]
    assert seen == list(range(10, 100, 10)) + [95]
    cancel = CancelToken()
    it = iter_groups(stream, cancel=cancel)
    next(it)
    cancel.cancel()
    with pytest.raises(Cancelled):
        list(it)


# ----------------------------------------------------------------------------------
# the public helpers
# ----------------------------------------------------------------------------------
def test_public_helpers_and_stage3_aliases(tmp_path: Path) -> None:
    assert dupes._Runs is HashRuns and dupes._KeyReader is KeyHasher
    assert parse_key(" $key ") == ("$key", None) and parse_key("$") == ("$", ())
    assert parse_key("a.b[0]") == ("a.b[0]", ("a", "b", 0))
    with pytest.raises(UserInputError):
        parse_key("a[*]")
    # 128-bit pair entries (the diff's (a << 64) | b), spilled and merged
    rng = random.Random(1)
    values = [(rng.randrange(1 << 40) << 64) | rng.randrange(1 << 64) for _ in range(5000)]
    with HashRuns(tmp_path, None, fan_in=3) as runs:
        for k in range(0, 4000, 700):
            runs.spill(values[k:k + 700])
        assert runs.written >= 6 and runs.dir is not None
        stream = runs.merged(values[4200:])
        merged = list(stream)
        d = runs.dir
    assert merged == sorted(values[:4200] + values[4200:]) and runs.merge_passes >= 1
    assert not d.exists()                  # the context manager removed the directory


def test_key_hasher_offset_and_shared_entries(make_doc, tmp_path: Path) -> None:
    d1 = make_doc(b'[{"k": 1}, {"k": 2}, {"x": 3}]')
    d2 = make_doc(b'{"k": 2.0}\n{"k": "1"}\n', format="jsonl")
    settings = Settings()
    runs = HashRuns(tmp_path, None)
    entries: list[int] = []
    h1 = KeyHasher(d1, ("k",), "parse", settings, None, 1000, runs, offset=0, entries=entries)
    for blk in d1.iter_blocks():
        h1.block(blk)
    h2 = KeyHasher(d2, ("k",), "extract", settings, None, 1000, runs, offset=3, entries=h1.entries)
    for blk in d2.iter_blocks():
        h2.block(blk)
    assert h2.entries is entries and (h1.with_key, h1.missing, h2.with_key, h2.missing) == (2, 1, 2, 0)
    two = key_hash(dupes.canonical_token(b"2"))
    assert sorted(entries) == sorted([(key_hash(dupes.canonical_token(b"1")) << 64) | 0, (two << 64) | 1,
                                      (two << 64) | 3, (key_hash(dupes.canonical_token(b'"1"')) << 64) | 4])
    with pytest.raises(ValueError):
        KeyHasher(d1, ("k",), "auto", settings, None, 10, runs)
    runs.cleanup()


NUMBER_KEYS = ["1", "10", "100", "1.0", "1e1", "10.0", "0", "-0", "1e2", "100.0", "12345678901234567890",
               "12345678901234567890.0", "1234567890123456789e1", '"1"', '"10"', "1000000", "1E6", "-1", "9", "90",
               "900e-1", "0.0", "1" + "0" * 400, "1e400", "7" * 5000, "20", "2e1", '"20"', "true", "null"]


def test_key_hasher_entries_equal_between_strategies(make_doc, backend, tmp_path: Path) -> None:
    """The extract strategy's inline forms (plain strings, plain positive integers) give exactly the
    canonical hashes of the parse strategy."""
    raw = b"[" + b",".join(b'{"k": %s, "x": 1}' % k.encode() for k in NUMBER_KEYS) + b"]"
    doc = make_doc(raw)
    got = {}
    for strategy in ("extract", "parse"):
        with HashRuns(tmp_path, None) as runs:
            h = KeyHasher(doc, ("k",), strategy, Settings(), None, 10**6, runs)
            for blk in doc.iter_blocks():
                h.block(blk)
            got[strategy] = sorted(h.entries)
    assert got["extract"] == got["parse"]
    want = sorted((key_hash(dupes.canonical_bytes(json_loads(k.encode()))) << 64) | i
                  for i, k in enumerate(NUMBER_KEYS))
    assert got["extract"] == want
    rep = find_duplicates(doc, "k", keep="none")
    assert list(rep.records) == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 19, 20, 21, 22, 23, 25, 26]
    assert rep.groups == 9                     # 1, 10, 100, 0, 12345678901234567890, 1e6, 90, 1e400, 20


# ----------------------------------------------------------------------------------
# PositionSource / KeepSource
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(6))
def test_position_sources_select_like_a_list(seed: int) -> None:
    rng = random.Random(seed)
    total = rng.choice([0, 1, 7, 8, 9, 100, 20000, 70001])
    bits = bytearray(rng.getrandbits(8) & rng.getrandbits(8) for _ in range((total + 7) // 8 + 2))
    offset = rng.randrange(0, 13) if total else 0
    count = max(0, total - offset)
    gbits = [(bits[g >> 3] >> (g & 7)) & 1 for g in range(len(bits) * 8)]
    want_dup = [p for p in range(count) if gbits[offset + p]]
    want_keep = [p for p in range(count) if not gbits[offset + p]]
    for src, want in ((PositionSource(bits, offset, count, keep=False, gen=5), want_dup),
                      (KeepSource(bits, offset, count, gen=5), want_keep)):
        assert len(src) == len(want) and list(src) == want and src.gen == 5 and src.is_ascending()
        assert [x for c in src.iter_chunks(size=1000) for x in c] == want
        assert all(len(c) < 1000 + 64 for c in src.iter_chunks(size=1000))
        for k in sorted({0, len(want) // 2, len(want) - 1, *rng.sample(range(len(want)), min(30, len(want)))}):
            if 0 <= k < len(want):
                assert src[k] == want[k] and src[k - len(want)] == want[k]
        with pytest.raises(IndexError):
            src[len(want)]
        with pytest.raises(TypeError):
            src["1"]  # type: ignore[index]


# ----------------------------------------------------------------------------------
# find_duplicates_many
# ----------------------------------------------------------------------------------
def many_reference(values: list[list[Any]], keep: str) -> dict[str, Any]:
    flat: list[Any] = [v for vs in values for v in vs]
    offsets = []
    acc = 0
    for vs in values:
        offsets.append(acc)
        acc += len(vs)
    groups = groups_of(flat)
    marks = marked(groups, keep)

    def loc(g: int) -> tuple[int, int]:
        i = max(k for k, o in enumerate(offsets) if o <= g and (g - o < len(values[k]) or k == len(values) - 1))
        return i, g - offsets[i]

    dup = [g for g in groups if len(g) > 1]
    return {
        "groups": len(dup),
        "duplicates": len(marks),
        "marks": marks,
        "per_input": [sum(1 for g in marks if offsets[i] <= g < offsets[i] + len(values[i])) for i in range(len(values))],
        "examples": [(text_of(flat[g[0]]), [loc(p) for p in g[:EXAMPLE_POSITIONS]]) for g in dup[:EXAMPLE_GROUPS]],
        "example_sizes": [len(g) for g in dup[:EXAMPLE_GROUPS]],
        "with_key": sum(1 for v in flat if v is not MISSING),
        "missing": sum(1 for v in flat if v is MISSING),
    }


def check_many(rep: MultiDuplicateReport, want: dict[str, Any], counts: list[int]) -> None:
    total = sum(counts)
    got_marks = [g for g in range(total) if rep.bits[g >> 3] >> (g & 7) & 1]
    assert got_marks == want["marks"]
    assert (rep.groups, rep.duplicates, rep.per_input) == (want["groups"], want["duplicates"], want["per_input"])
    assert (rep.examples, rep.example_sizes) == (want["examples"], want["example_sizes"])
    assert (rep.records_with_key, rep.missing, rep.counts) == (want["with_key"], want["missing"], counts)
    assert rep.scanned == rep.records_with_key + rep.missing + rep.skipped
    for i, n in enumerate(counts):
        keep = rep.keep_source(i)
        dup = rep.duplicate_source(i)
        assert sorted(list(keep) + list(dup)) == list(range(n))
        assert len(dup) == rep.per_input[i] and all(rep.is_duplicate(i, p) for p in dup)
        assert keep.gen == rep.gens[i]


MANY_CASES = [
    [("array", {}, {}), ("jsonl", {}, {"format": "jsonl"})],
    [("stream", {"style": "pretty4"}, {"format": "stream"}), ("wrapped", {"style": "tab"}, {"record_path": "data"}),
     ("brace_list", {"style": "messy"}, {"format": "brace_list"})],
    [("keyed", {"style": "pretty2"}, {"format": "keyed"}), ("array", {"style": "messy_escaped"}, {})],
]


@pytest.mark.parametrize("strategy", ["extract", "parse"])
@pytest.mark.parametrize("case", range(len(MANY_CASES)))
def test_many_matches_the_reference(make_doc, backend, strategy, case) -> None:
    docs = []
    values = []
    for k, (fmt, write_kw, open_kw) in enumerate(MANY_CASES[case]):
        recs = dup_records(150 + 40 * k, seed=100 * case + k)
        docs.append(make_doc(recs, fmt, **write_kw, **open_kw))
        values.append(path_values(recs, "k"))
    counts = [d.record_count for d in docs]
    for keep in ("first", "last", "none"):
        rep = find_duplicates_many(docs, "k", keep=keep, strategy=strategy)
        assert rep.keep == keep and rep.key == "k" and rep.names == [d.source_path.name for d in docs]
        check_many(rep, many_reference(values, keep), counts)
        assert not rep.warnings and rep.runs == 0
    # the whole record as the key
    whole = [[d.record(i) for i in range(d.record_count)] for d in docs]
    rep = find_duplicates_many(docs, "$", strategy=strategy)
    check_many(rep, many_reference(whole, "first"), counts)


def test_many_single_input_equals_find_duplicates(make_doc, backend) -> None:
    recs = dup_records(400, seed=5)
    doc = make_doc(recs, "array")
    for keep in ("first", "last", "none"):
        one = find_duplicates(doc, "k", keep=keep)
        many = find_duplicates_many([doc], "k", keep=keep)
        assert list(many.duplicate_source(0)) == list(one.records)
        assert (many.groups, many.duplicates, many.per_input) == (one.groups, one.duplicates, [one.duplicates])
        assert many.examples == [(txt, [(0, p) for p in pos]) for txt, pos in one.examples]
        assert many.example_sizes == one.example_sizes


def test_many_keep_first_prefers_the_earlier_input(make_doc) -> None:
    a = make_doc(b'[{"id": 1, "v": "a1"}, {"id": 2, "v": "a2"}, {"id": 2, "v": "a2b"}]')
    b = make_doc(b'{"id": 3}\n{"id": 1.0}\n{"id": 2}\n', format="jsonl")
    empty = make_doc(b"[]")
    rep = find_duplicates_many([a, empty, b], "id")
    assert rep.offsets == [0, 3, 3] and rep.counts == [3, 0, 3]
    assert list(rep.keep_source(0)) == [0, 1] and list(rep.keep_source(1)) == [] and list(rep.keep_source(2)) == [0]
    assert rep.per_input == [1, 0, 2] and rep.locate(4) == (2, 1) and rep.locate(2) == (0, 2)
    assert rep.examples == [("1", [(0, 0), (2, 1)]), ("2", [(0, 1), (0, 2), (2, 2)])]
    last = find_duplicates_many([a, empty, b], "id", keep="last")
    assert list(last.keep_source(0)) == [] and list(last.keep_source(2)) == [0, 1, 2]
    with pytest.raises(IndexError):
        rep.locate(6)
    with pytest.raises(ValueError):
        find_duplicates_many([], "id")


def test_many_spills_like_in_memory(make_doc, backend, monkeypatch) -> None:
    docs = [make_doc(dup_records(300, seed=s), "jsonl", format="jsonl") for s in (1, 2, 3)]
    values = [path_values(dup_records(300, seed=s), "k") for s in (1, 2, 3)]
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 4)
    monkeypatch.setattr(dupes, "MAX_FANIN", 3)
    monkeypatch.setattr(dupes, "CHECK_RECORDS", 9)
    for keep in ("first", "none"):
        rep = find_duplicates_many(docs, "k", keep=keep, memory_limit=dupes.ENTRY_BYTES * 13, block_bytes=3000)
        assert rep.runs > 20
        check_many(rep, many_reference(values, keep), [300, 300, 300])
    assert no_temp_dirs()


def test_many_dollar_key_and_warnings(make_doc, lang_en) -> None:
    k1 = make_doc([{"v": i} for i in range(5)], "keyed", format="keyed", keys=["a", "b", "c", "d", "e"])
    k2 = make_doc(b'{"meta": 1, "data": {"c": {"v": 9}, "z": {}, "a": [1]}}', record_path="data")
    plain = make_doc(b'[{"v": 1}, {"v": 2}]', name="plain.json")
    rep = find_duplicates_many([k1, k2, plain], "$key")
    assert rep.examples == [('"a"', [(0, 0), (1, 2)]), ('"c"', [(0, 2), (1, 0)])]
    assert rep.per_input == [0, 2, 0] and rep.missing == 2
    assert rep.warnings == ("dupes.warn.input_no_record_keys",)
    assert rep.warning_texts() == ["plain.json: the records of this file have no keys ($key works only in keyed "
                                   "documents). 2 records were counted as having no key."]
    absent = find_duplicates_many([plain, k1], "nope")
    assert absent.warnings == ("dupes.warn.key_absent",) and absent.groups == 0


def test_many_huge_records_and_per_input_warnings(make_doc, lang_en) -> None:
    long = "x" * 1_200_000
    lines = [b'{"k": "a"}', b'{"k": "' + long.encode() + b'"}', b'{"k": 1}']
    small = Settings(large_node_bytes=1000)
    d1 = make_doc(b"\n".join(lines), format="jsonl", settings=small, name="one.jsonl")
    d2 = make_doc(b'[{"k": 1.0}, {"k": "a"}, {"k": "b"}]', settings=small, name="two.json")
    rep = find_duplicates_many([d1, d2], "k")
    assert rep.skipped == 1 and rep.per_input == [0, 2] and rep.groups == 2
    assert rep.warnings == ("dupes.warn.input_too_large",)
    assert rep.warning_texts()[0].startswith("one.jsonl: the key value of 1 record (#1) is too large")


def test_many_edited_documents_use_positions(make_doc, tmp_path: Path) -> None:
    doc = make_doc(b'[{"k": 1}, {"k": 2}, {"k": 3}, {"k": 4}]')
    other = make_doc(b'{"k": 9}\n{"k": 3}\n', format="jsonl")
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(0, b'{"k": 9}').delete(1).insert(0, b'{"k": 4}')        # positions: 4, 9, 3, 4
    doc.set_overlay(ob.state())
    pinned = doc.pinned()
    rep = find_duplicates_many([pinned, other], "k")
    assert rep.counts == [4, 2] and list(rep.duplicate_source(0)) == [3] and list(rep.duplicate_source(1)) == [0, 1]
    assert rep.gens[0] == pinned.gen != 0
    keep = rep.keep_source(0)
    out = tmp_path / "kept.jsonl"
    export(pinned, out, ExportOptions("jsonl"), keep)
    assert [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()] == [{"k": 4}, {"k": 9}, {"k": 3}]
    # a newer state: the positions of the source belong to the old one
    ob.set(2, b'{"k": 33}')
    doc.set_overlay(ob.state())
    with pytest.raises(EditError) as ei:
        export(doc, tmp_path / "stale.jsonl", ExportOptions("jsonl"), keep)
    assert ei.value.key == "edits.error.stale_scope"


def test_many_export_with_keep_sources_writes_the_deduplicated_records(make_doc, tmp_path: Path) -> None:
    recs_a = dup_records(200, seed=41)
    recs_b = dup_records(150, seed=42)
    da = make_doc(recs_a, "array", style="pretty2")
    db = make_doc(recs_b, "jsonl", format="jsonl")
    rep = find_duplicates_many([da, db], "k")
    seen: set[Any] = set()
    want = []
    for r in recs_a + recs_b:
        v = r.get("k", MISSING)
        if v is not MISSING:
            n = norm(v)
            if n in seen:
                continue
            seen.add(n)
        want.append(r)
    got = []
    for i, d in enumerate((da, db)):
        out = tmp_path / f"part{i}.jsonl"
        export(d, out, ExportOptions("jsonl"), rep.keep_source(i))
        got += [d.record(p) for p in rep.keep_source(i)]
        lines = out.read_bytes().split(b"\n")              # (not splitlines: U+2028 inside strings)
        assert lines[-1] == b"" and len(lines) - 1 == len(rep.keep_source(i))
        assert [norm(json_loads(x)) for x in lines[:-1]] == [norm(d.record(p)) for p in rep.keep_source(i)]
    assert [norm(r) for r in got] == [norm(r) for r in want]


def test_many_progress_and_cancel(make_doc, monkeypatch) -> None:
    docs = [make_doc(dup_records(300, seed=s), "array", build=False) for s in (7, 8)]
    infos: list[Any] = []
    progress = Progress(callback=infos.append, min_interval=0)
    monkeypatch.setattr(dupes, "MERGE_BATCH", 20)
    rep = find_duplicates_many(docs, "k", progress=progress, block_bytes=3000)
    assert rep.groups > 0 and all(d.index_complete for d in docs)
    done = [i.done for i in infos]
    assert done == sorted(done) and progress.finished and done[-1] == progress.total
    assert t("dupes.progress.merge_many", count=2) in {i.message for i in infos}
    cancel = CancelToken()
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 2)

    def on_progress(info: Any) -> None:
        if info.done > 0:
            cancel.cancel()

    with pytest.raises(Cancelled):
        find_duplicates_many(docs, "k", memory_limit=100, progress=Progress(callback=on_progress, min_interval=0),
                             cancel=cancel, block_bytes=2000)
    assert no_temp_dirs()


def test_messages_exist_in_both_languages() -> None:
    keys = ["dupes.keep.choices2", "dupes.keep.choices3", "dupes.error.top", "dupes.progress.scan_many",
            "dupes.progress.merge_many"]
    for w in ("input_no_record_keys", "input_too_large", "input_unreadable"):
        keys += [f"dupes.warn.{w}", f"dupes.warn.{w}.one"]
    for key in keys:
        assert has(key), key


def test_keep_error_texts(lang_uz) -> None:
    with pytest.raises(UserInputError) as ei:
        dupes._check_keep("x", allow_none=True)
    assert "'none' (hech biri" in str(ei.value)
    with pytest.raises(UserInputError) as ei:
        dupes._check_keep("none")
    assert str(ei.value) == ("Har bir takroriy guruhda qaysi yozuv qolishi 'first' (birinchisi) yoki 'last' "
                             "(oxirgisi) bo'lishi kerak, berilgan: 'none'")
    assert os.environ.get("JSONRESHAPE_HOME")       # the tests never write outside it
