"""sort_records (docs/ARCHITECTURE.md §6; AGENT_PROMPT §6.4, §12, §14 test 17).

The reference sorts ``(sortkeys.record_key(codec.loads(record), specs), position)`` --
keys from parsed records, positions for stability. The runner extracts the field values
with core.PathExtractor (never parsing whole records), streams huge records and may sort
externally; its result must equal the reference on every format, for every key type and
option, in memory and with a tiny ``memory_limit`` (many runs, multi-pass merges). Hand
written expectations cover stability, type order, null placement, natural order, exact
numbers and the Uzbek collation.
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec
from jsonreshape import recordset as rsmod
from jsonreshape.appdirs import recordsets_dir, tmp_dir
from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.i18n import t
from jsonreshape.reader import Document, split_member
from jsonreshape.recordset import RecordSet, SortReport, filter_records, sort_records
from jsonreshape.settings import Settings
from jsonreshape.sortkeys import SortKey, record_key
from jsonreshape.tasks import BackgroundTask, CancelToken, Progress

HYP = settings(derandomize=True, database=None, deadline=None, max_examples=40,
               suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])

TINY = 3000  # bytes: a few dozen keys per run


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


def array_of(values: list[Any], field: str | None = "v") -> bytes:
    """A JSON array document; ``field=None`` -> the values are the records; the
    sentinel ``MISSING_V`` leaves the field out."""
    parts = []
    for i, v in enumerate(values):
        if field is None:
            parts.append(codec.dumps(v))
        elif v is MISSING_V:
            parts.append(b'{"i":%d}' % i)
        else:
            parts.append(b'{"i":%d,"%s":%s}' % (i, field.encode(), codec.dumps(v) if not isinstance(v, Raw) else v.raw))
    return b"[" + b",".join(parts) + b"]"


class Raw:
    """A number token written verbatim."""

    def __init__(self, raw: str) -> None:
        self.raw = raw.encode()


MISSING_V = object()


def specs_of(*texts: str, **opts: Any) -> list[SortKey]:
    return [SortKey.parse(x, **opts) for x in texts]


def reference(doc: Document, specs: list[SortKey], source: Any = None) -> list[int]:
    indices = range(doc.record_count) if source is None else list(source)
    keyed = []
    with open(doc.data_path, "rb") as fh:
        for pos, i in enumerate(indices):
            s, e = doc.record_span(i)
            fh.seek(s)
            buf = fh.read(e - s)
            key = None
            if doc.member_records:
                key, vo = split_member(buf)
                value = codec.loads(buf[vo:])
            else:
                value = codec.loads(buf)
            keyed.append((record_key(value, specs, key=key, index=i), pos, i))
    keyed.sort()
    return [i for _, _, i in keyed]


def run(doc: Document, specs: Any, **kw) -> tuple[list[int], SortReport]:
    rs = sort_records(doc, specs, **kw)
    try:
        assert rs.complete and rs.kind == "sort" and len(rs) == rs.report.records
        return rs.to_list(), rs.report
    finally:
        rs.close()


def no_temp_files() -> bool:
    return os.listdir(recordsets_dir()) == [] and not [n for n in os.listdir(tmp_dir()) if n.startswith("sort-")]


KEY_SETS: list[list[SortKey]] = [
    specs_of("id"),
    specs_of("-id"),
    specs_of("name"),
    specs_of("-name"),
    specs_of("name", natural=False, collation="simple"),
    specs_of("name", case_sensitive=True, collation="unicode"),
    specs_of("contact.address.city"),
    specs_of("contact.address.city", nulls="first"),
    specs_of("-contact.address.city", nulls="first"),
    specs_of("contact.address.city", "-id"),
    specs_of("score"),
    specs_of("-score"),
    specs_of("orders[*].sum"),
    specs_of("-orders[0].oid", "name"),
    specs_of("tags[*]"),
    specs_of("tags[1]", nulls="first"),
    specs_of("extra"),
    specs_of("$"),
    specs_of("-$"),
    specs_of("active", "-score"),
    specs_of("note", "id"),
    specs_of("contact"),
    specs_of("contact.*"),
    specs_of("$key"),
    specs_of("-$key", "id"),
    specs_of("-$index"),
    specs_of("contact.address.zip", "$index", "name"),
]


# ----------------------------------------------------------------------------------
# reference equality
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, style", [
    ("array", "compact"),
    ("array", "messy_escaped"),
    ("jsonl", "compact"),
    ("keyed", "pretty2"),
    ("wrapped", "compact"),
    ("brace_list", "messy"),
    ("stream", "tab"),
])
def test_sort_equals_reference_on_every_format(make_doc, fmt: str, style: str) -> None:
    doc = make_doc(jsongen.records(90, seed=12), fmt, style=style)
    for specs in KEY_SETS:
        expected = reference(doc, specs)
        for limit in (None, TINY):
            got, rep = run(doc, specs, memory_limit=limit)
            assert got == expected, (fmt, [s.to_text() for s in specs], limit)
            assert rep.records == 90 and rep.approximate == 0
            assert rep.strategy == ("memory" if limit is None else "external")


@pytest.mark.parametrize("fmt", ["array", "jsonl", "keyed", "single"])
def test_sort_arbitrary_values(make_doc, fmt: str) -> None:
    recs = jsongen.records(70, seed=9, kind="value")
    if fmt == "keyed":
        recs = [r if isinstance(r, dict) else {"v": r} for r in recs]
    doc = make_doc(recs, fmt)
    for specs in (specs_of("$"), specs_of("-$", nulls="first"), specs_of("[0]"), specs_of("*"), specs_of("v"),
                  specs_of("[*][*]", "-$index")):
        expected = reference(doc, specs)
        for limit in (None, 500):
            got, _ = run(doc, specs, memory_limit=limit)
            assert got == expected, (fmt, [s.to_text() for s in specs], limit)


# ----------------------------------------------------------------------------------
# hand-written expectations
# ----------------------------------------------------------------------------------
def test_stability_equal_keys_keep_source_order(make_doc) -> None:
    rng = random.Random(3)
    groups = [rng.choice(["a", "b", "c", None]) for _ in range(300)]
    doc = make_doc(raw=array_of(groups, "g"))
    for desc in (False, True):
        for limit in (None, 1000):
            got, _ = run(doc, [SortKey("g", descending=desc)], memory_limit=limit)
            order = ["c", "b", "a"] if desc else ["a", "b", "c"]
            expected = [i for g in order + [None] for i, x in enumerate(groups) if x == g]
            assert got == expected, (desc, limit)


def test_type_order_and_null_placement(make_doc) -> None:
    values = [3, "b", True, [1], {"a": 1}, None, MISSING_V, "a", 1.5, False, [], {}, -2, None, MISSING_V]
    doc = make_doc(raw=array_of(values))
    asc = [12, 8, 0, 7, 1, 9, 2, 3, 10, 4, 11, 5, 13, 6, 14]
    desc = [11, 4, 10, 3, 2, 9, 1, 7, 0, 8, 12, 5, 13, 6, 14]
    asc_first = [6, 14, 5, 13, 12, 8, 0, 7, 1, 9, 2, 3, 10, 4, 11]
    desc_first = [6, 14, 5, 13, 11, 4, 10, 3, 2, 9, 1, 7, 0, 8, 12]
    for limit in (None, 200):
        assert run(doc, [SortKey("v")], memory_limit=limit)[0] == asc
        assert run(doc, [SortKey("v", descending=True)], memory_limit=limit)[0] == desc
        assert run(doc, [SortKey("v", nulls="first")], memory_limit=limit)[0] == asc_first
        assert run(doc, [SortKey("v", descending=True, nulls="first")], memory_limit=limit)[0] == desc_first


def test_natural_order(make_doc) -> None:
    names = ["child10", "child2", "child1", "child20", "Child3", "child02", "v1.10", "v1.9"]
    doc = make_doc(raw=array_of(names, "name"))
    # uz collation: "ch" is one letter, after z (so every "child..." follows "v...")
    got, _ = run(doc, specs_of("name"))
    assert [names[i] for i in got] == ["v1.9", "v1.10", "child1", "child2", "child02", "Child3", "child10", "child20"]
    got, _ = run(doc, specs_of("name", natural=False))
    assert [names[i] for i in got] == ["v1.10", "v1.9", "child02", "child1", "child10", "child2", "child20", "Child3"]
    got, _ = run(doc, specs_of("-name"))
    assert [names[i] for i in got] == ["child20", "child10", "Child3", "child02", "child2", "child1", "v1.10", "v1.9"]
    got, _ = run(doc, specs_of("name", collation="simple"), memory_limit=100)
    assert [names[i] for i in got] == ["child1", "child2", "child02", "Child3", "child10", "child20", "v1.9", "v1.10"]


def test_multi_key(make_doc) -> None:
    rows = [("Toshkent", 1), ("Buxoro", 2), ("Toshkent", 3), (None, 4), ("Buxoro", 5), ("Andijon", 6)]
    raw = b"[" + b",".join(codec.dumps({"city": c, "id": i}) for c, i in rows) + b"]"
    doc = make_doc(raw=raw)
    got, _ = run(doc, specs_of("city", "-id"))
    assert [rows[i][1] for i in got] == [6, 5, 2, 3, 1, 4]
    got, _ = run(doc, specs_of("-city", "id"))
    assert [rows[i][1] for i in got] == [1, 3, 2, 5, 6, 4]


def test_member_key_and_record_index(make_doc) -> None:
    keys = ["b", "a10", "a2", "Ўзбек", "c", "A1", "oʻrik", "zebra"]
    doc = make_doc([{"n": i} for i in range(len(keys))], "keyed", keys=keys)
    got, _ = run(doc, specs_of("$key"))
    assert [keys[i] for i in got] == ["A1", "a2", "a10", "b", "c", "zebra", "oʻrik", "Ўзбек"]
    got, _ = run(doc, specs_of("-$key"))
    assert [keys[i] for i in got] == ["Ўзбек", "oʻrik", "zebra", "c", "b", "a10", "a2", "A1"]
    got, _ = run(doc, specs_of("-$index"))
    assert got == list(range(len(keys)))[::-1]
    arr = make_doc(raw=array_of([5, 3, 4]))
    assert run(arr, specs_of("$key"))[0] == [0, 1, 2]       # no member keys: all missing, stable
    assert run(arr, specs_of("-$key", "v"))[0] == [1, 2, 0]


def test_number_exactness(make_doc) -> None:
    toks = ["1.10", "1.1", "1.100", "1", "1.0", "12345678901234567891", "12345678901234567890", "1e400", "-1e400",
            "0.1", "-0.0", "0", "-0", "1E5", "100000", "2.5E+3", "9007199254740993", "9007199254740992"]
    doc = make_doc(raw=array_of([Raw(x) for x in toks]))
    expected_groups = [["-1e400"], ["-0.0", "0", "-0"], ["0.1"], ["1", "1.0"], ["1.10", "1.1", "1.100"], ["2.5E+3"],
                       ["1E5", "100000"], ["9007199254740992"], ["9007199254740993"], ["12345678901234567890"],
                       ["12345678901234567891"], ["1e400"]]
    expected = [toks.index(x) for group in expected_groups for x in group]
    for limit in (None, 300):
        got, _ = run(doc, specs_of("v"), memory_limit=limit)
        assert got == expected
        got, _ = run(doc, specs_of("-v"), memory_limit=limit)
        assert got == [toks.index(x) for group in reversed(expected_groups) for x in group]  # ties stay in order


def test_uzbek_collation_through_the_runner(make_doc) -> None:
    latin = ["Shoira", "Sanjar", "Chori", "Oʻtkir", "Olim", "Gʻani", "Gulnora", "Zafar", "Nodira", "Ngoz",
             "Ulugʻbek", "O'ktam", "Xurshid", "Hilola", "Tohir", "Abbos"]
    doc = make_doc(raw=array_of(latin, "ism"))
    got, _ = run(doc, specs_of("ism"))
    assert [latin[i] for i in got] == ["Abbos", "Gulnora", "Hilola", "Nodira", "Olim", "Sanjar", "Tohir", "Ulugʻbek",
                                       "Xurshid", "Zafar", "O'ktam", "Oʻtkir", "Gʻani", "Shoira", "Chori", "Ngoz"]
    got, _ = run(doc, specs_of("ism", collation="simple"))
    assert [latin[i] for i in got][:4] == ["Abbos", "Chori", "Gʻani", "Gulnora"]
    cyr = ["Тошкент", "Андижон", "Ўзбекистон", "Қарши", "Бухоро", "Ғулом", "Ҳамид", "Яккабоғ"]
    doc2 = make_doc(raw=array_of(cyr, "shahar"))
    got, _ = run(doc2, specs_of("shahar"), memory_limit=150)
    assert [cyr[i] for i in got] == ["Андижон", "Бухоро", "Тошкент", "Яккабоғ", "Ўзбекистон", "Қарши", "Ғулом", "Ҳамид"]


# ----------------------------------------------------------------------------------
# external sort (§14 test 17)
# ----------------------------------------------------------------------------------
def test_external_sort_equals_in_memory(make_doc, monkeypatch) -> None:
    monkeypatch.setattr(rsmod, "MAX_FANIN", 3)  # force intermediate merge passes
    doc = make_doc(jsongen.records(2500, seed=17))
    for specs in (specs_of("name"), specs_of("-score", "id"), specs_of("contact.address.city", "-$index"),
                  specs_of("$"), specs_of("orders[*].sum", nulls="first")):
        memory, rep_mem = run(doc, specs)
        assert rep_mem.runs == 0 and rep_mem.strategy == "memory" and rep_mem.merge_passes == 0
        external, rep_ext = run(doc, specs, memory_limit=2000, block_bytes=16384)
        assert external == memory == reference(doc, specs)
        assert rep_ext.strategy == "external" and rep_ext.runs > 50 and rep_ext.merge_passes > 10
        if specs[0].path != "$":  # a whole-record key may alone exceed 2000 bytes (a run of its own)
            assert rep_ext.peak_bytes_estimate <= 2000
        assert no_temp_files()


def test_sorted_pieces_equal_one_sort() -> None:
    """P-F5: keys are sorted in bounded slices and merged batch by batch; the result is
    exactly sorted() (also for presorted / reversed input, duplicates and tiny slices)."""
    import itertools
    import struct

    rng = random.Random(11)
    for n, size in [(0, 4), (1, 4), (4, 4), (5, 4), (17, 4), (1000, 7), (1000, 64), (20000, 1000), (20000, 2)]:
        keys = [bytes([rng.randrange(3)]) * rng.randrange(4) + struct.pack(">Q", i) for i in range(n)]
        want = sorted(keys)
        bound = rsmod.SORT_MERGE_FANIN * max(2, size)   # a batch: the merge of a few slices
        for variant in (list(keys), sorted(keys), sorted(keys, reverse=True), [b"a", b"b", b"a"] * (n // 3)):
            unique = len(set(variant)) == len(variant)
            expected = sorted(variant)
            pieces = list(rsmod._sorted_pieces(variant, size))
            assert list(itertools.chain.from_iterable(pieces)) == expected
            assert variant == []                                      # consumed
            assert all(len(piece) > 0 for piece in pieces)
            if unique:                                                # (sort keys always are)
                assert all(len(piece) <= bound for piece in pieces)
                # every batch but a few run tails has a full slice: no flood of tiny batches
                assert len(pieces) <= n // max(2, size) + rsmod.SORT_MERGE_FANIN + 1
        assert want == sorted(keys)
    # few distinct values in file order (the slices cover separate key ranges): still
    # about n / size batches
    keys = [b"name%d" % (i % 3 if i < 30000 else 7) + struct.pack(">Q", i) for i in range(40000)]
    pieces = list(rsmod._sorted_pieces(list(keys), 100))
    assert list(itertools.chain.from_iterable(pieces)) == sorted(keys)
    assert len(pieces) <= 40000 // 100 + rsmod.SORT_MERGE_FANIN + 1


def test_sort_never_sorts_more_than_a_slice_at_once(make_doc, monkeypatch) -> None:
    """P-F5: the in-memory part and every spilled run go through _sorted_pieces (bounded
    list.sort calls, the GIL is released between them); same result as before."""
    pieces: list[int] = []
    real = rsmod._sorted_pieces

    def spy(entries, slice_size=None, cancel=None):
        for piece in real(entries, slice_size, cancel):
            pieces.append(len(piece))
            yield piece

    monkeypatch.setattr(rsmod, "SORT_SLICE", 64)       # 3000 keys: 47 slices, 2 merge levels
    bound = rsmod.SORT_MERGE_FANIN * 64
    monkeypatch.setattr(rsmod, "_sorted_pieces", spy)
    doc = make_doc(jsongen.records(3000, seed=23))
    for specs in (specs_of("name"), specs_of("-score", "id")):
        pieces.clear()
        memory, rep_mem = run(doc, specs)
        assert rep_mem.strategy == "memory" and pieces and max(pieces) <= bound and sum(pieces) == 3000
        pieces.clear()
        external, rep_ext = run(doc, specs, memory_limit=60_000, block_bytes=16384)
        assert rep_ext.strategy == "external" and rep_ext.runs >= 2
        assert max(pieces) <= bound and sum(pieces) == 3000
        assert external == memory == reference(doc, specs)


def test_one_key_per_run(make_doc) -> None:
    doc = make_doc(raw=array_of(list(range(40, 0, -1))))
    got, rep = run(doc, specs_of("v"), memory_limit=1)
    # every key is larger than the limit: 39 runs on disk + the last key still in memory
    assert got == list(range(39, -1, -1)) and rep.runs == 39


# ----------------------------------------------------------------------------------
# sources
# ----------------------------------------------------------------------------------
def test_sort_of_a_filtered_source_and_other_sources(make_doc) -> None:
    doc = make_doc(jsongen.records(200, seed=5))
    specs = specs_of("name", "-id")
    flt = filter_records(doc, "score > 0")
    for limit in (None, TINY):
        got, rep = run(doc, specs, source=flt, memory_limit=limit)
        assert got == reference(doc, specs, flt) and rep.records == len(flt)
    flt.close()
    cases: list[Any] = [range(50, 150), range(0, 200, 7), range(199, 0, -2), [150, 3, 3, 77, 10, 199, 0, 3],
                        (5, 4, 3), range(0)]
    for source in cases:
        for limit in (None, 500):
            got, _ = run(doc, specs, source=source, memory_limit=limit)
            assert got == reference(doc, specs, source), (source, limit)
    got, _ = run(doc, specs, source=(i for i in range(10, 0, -1)))
    assert got == reference(doc, specs, range(10, 0, -1))
    with RecordSet.from_indices(range(199, -1, -1)) as rev:
        got, _ = run(doc, specs_of("id"), source=rev)
        assert got == list(range(200))
    got, _ = run(doc, specs, source=RecordSet.base(doc))
    assert got == reference(doc, specs)
    once = sort_records(doc, specs_of("-score"))
    got, _ = run(doc, specs_of("id"), source=once)  # re-sorting a sorted set
    assert got == list(range(200))
    once.close()
    assert no_temp_files()


def test_invalid_sources_and_keys(make_doc) -> None:
    doc = make_doc(raw=array_of([1, 2, 3]))
    with pytest.raises(IndexError):
        run(doc, specs_of("v"), source=[0, 9])
    with pytest.raises(UserInputError):
        sort_records(doc, [])
    for bad in (0, -5, True, 1.5):
        with pytest.raises(UserInputError):
            sort_records(doc, ["v"], memory_limit=bad)
    with pytest.raises(TypeError):
        sort_records(doc, [42])  # type: ignore[list-item]
    assert run(doc, "-v")[0] == [2, 1, 0]
    assert run(doc, ["-v", "$index"])[0] == [2, 1, 0]
    assert no_temp_files()


# ----------------------------------------------------------------------------------
# huge records
# ----------------------------------------------------------------------------------
def huge_records() -> list[dict]:
    recs = jsongen.records(45, seed=22)
    for i in range(0, 45, 3):
        recs[i]["blob"] = ["Toshkent ko'chasi " * 4] * 30
        recs[i]["nested"] = {"k": (i * 7) % 11, "list": list(range(i % 5))}
    return recs


@pytest.mark.parametrize("fmt", ["array", "keyed", "wrapped", "jsonl"])
def test_huge_records_are_streamed(make_doc, fmt: str) -> None:
    doc = make_doc(huge_records(), fmt, settings_=Settings(large_node_bytes=1500))
    assert sum(doc.is_huge(i) for i in range(doc.record_count)) == 15
    for specs in (specs_of("name"), specs_of("-score"), specs_of("nested.k", "-id"), specs_of("nested.list[*]"),
                  specs_of("blob[0]", nulls="first"), specs_of("contact"), specs_of("$key", "-$index"),
                  specs_of("orders[*].oid", "contact.address.city")):
        expected = reference(doc, specs)
        for limit in (None, TINY):
            got, rep = run(doc, specs, memory_limit=limit)
            assert got == expected, (fmt, [s.to_text() for s in specs], limit)
            assert rep.approximate == 0


def test_whole_record_key_of_huge_records(make_doc, monkeypatch, lang_en) -> None:
    rng = random.Random(4)
    recs: list[Any] = []
    for i in range(30):
        if i % 3 == 0:
            recs.append({"k": "".join(rng.choice("abcxyz") for _ in range(8)), "pad": [i] * 600})  # huge object
        elif i % 3 == 1:
            recs.append([rng.randrange(1000)] + ["x" * 30] * 60)  # huge array
        else:
            recs.append({"k": "small%d" % i})
    recs.append("s" * 3000)  # a huge string: exact key
    p_settings = Settings(large_node_bytes=1000)
    doc = make_doc(recs, "array", settings_=p_settings)
    huge = [i for i in range(doc.record_count) if doc.is_huge(i)]
    assert len(huge) == 21
    expected = reference(doc, specs_of("$"))  # the records differ within the prefix: same order
    for limit in (None, 400):
        got, rep = run(doc, specs_of("$"), memory_limit=limit)
        assert got == expected and rep.approximate == 0  # compact texts below HUGE_KEY_PREFIX (64 KiB)
    monkeypatch.setattr(rsmod, "HUGE_KEY_PREFIX", 40)
    got, rep = run(doc, specs_of("-$"))
    assert got == reference(doc, specs_of("-$"))
    assert rep.approximate == 20  # objects and arrays; the string is exact
    assert rep.warnings == ("recordset.warn.approximate",) and "20" in rep.warning_texts()[0]


def test_padded_huge_record_with_short_compact_text_is_exact(make_doc, monkeypatch) -> None:
    pad = b" " * 3000
    raw = b'[{"a":2},' + b"{" + pad + b'"a"' + pad + b":" + pad + b"1" + pad + b"}" + b',{"a":0}]'
    doc = make_doc(raw=raw, settings_=Settings(large_node_bytes=500))
    assert doc.is_huge(1)
    monkeypatch.setattr(rsmod, "HUGE_KEY_PREFIX", 100)
    got, rep = run(doc, specs_of("$"))
    assert got == [2, 1, 0] and rep.approximate == 0


# ----------------------------------------------------------------------------------
# progress, cancellation, errors
# ----------------------------------------------------------------------------------
def test_report_and_progress(make_doc) -> None:
    doc = make_doc(jsongen.records(800, seed=7))
    infos = []
    p = Progress(callback=infos.append, min_interval=0)
    got, rep = run(doc, specs_of("name"), progress=p, memory_limit=TINY, block_bytes=8192)
    data_bytes = doc.data_size - doc.info.data_start
    assert rep.records == 800 and rep.runs > 1 and rep.strategy == "external"
    assert rep.key_bytes > 800 * 8 and rep.bytes_read <= data_bytes and rep.elapsed >= 0
    # the total holds the record bytes plus the projected key bytes from the first block on
    # and then stays fixed: the bar never goes back (fix2 N13)
    assert p.finished and p.done == p.total and p.total > data_bytes
    assert p.message == t("recordset.progress.sort_merge", name=doc.source_path.name)
    assert infos[0].message == t("recordset.progress.sort_keys", name=doc.source_path.name)
    dones = [i.done for i in infos]
    assert dones == sorted(dones)
    shown = [i for i in infos if i.done]
    assert all(i.total == p.total and i.done <= i.total for i in shown)
    reading = [i for i in shown if i.message == t("recordset.progress.sort_keys", name=doc.source_path.name)]
    assert reading and all(i.done < i.total and i.eta for i in reading)   # never "100%, 0.0 s" before merging
    p2 = Progress(total=7, message="mine")
    run(doc, specs_of("id"), progress=p2)
    assert p2.total == 7


@pytest.mark.parametrize("phase", ["keys", "merge"])
def test_cancel_midway_leaves_no_temp_files(make_doc, phase: str) -> None:
    doc = make_doc(jsongen.records(3000, seed=8))
    token = CancelToken()
    merge_msg = t("recordset.progress.sort_merge", name=doc.source_path.name)
    seen = []

    def callback(info) -> None:
        seen.append(info.message)
        if phase == "keys" and info.done > 50_000:
            token.cancel()
        if phase == "merge" and info.message == merge_msg:
            token.cancel()

    with pytest.raises(Cancelled):
        sort_records(doc, specs_of("name"), memory_limit=TINY, block_bytes=8192, cancel=token,
                     progress=Progress(callback=callback, min_interval=0))
    assert (merge_msg in seen) == (phase == "merge")
    assert no_temp_files()
    early = CancelToken()
    early.cancel()
    with pytest.raises(Cancelled):
        sort_records(doc, specs_of("id"), cancel=early)
    assert no_temp_files()


def test_damaged_record_fails_the_sort_cleanly(make_doc) -> None:
    doc = make_doc(raw=array_of(["a", "b", "c", "d"]))
    s, e = doc.record_span(2)
    raw = doc.data_path.read_bytes()
    pos = raw.index(b'"c"', s)
    with open(doc.data_path, "r+b") as fh:
        fh.seek(pos)
        fh.write(b"[c]")
    with pytest.raises(JsonReshapeError):
        sort_records(doc, specs_of("v"), memory_limit=TINY)
    assert no_temp_files()


def test_values_the_decoder_rejects_are_keyed_by_streaming(make_doc, monkeypatch) -> None:
    values = [{"m": 2}, {"marker": [3, 1]}, {"m": 1}, [5], "x"]
    doc = make_doc(raw=array_of(values, None))
    expected = reference(doc, specs_of("$"))
    expected_desc = reference(doc, specs_of("-$", "$index"))
    real = codec.loads

    def fake(data: Any) -> Any:
        b = data.encode() if isinstance(data, str) else bytes(data)
        if b'"marker"' in b:
            raise codec.JsonDecodeError("simulated: too deep", 0)
        return real(data)

    monkeypatch.setattr(codec, "loads", fake)
    assert run(doc, specs_of("$"))[0] == expected
    assert run(doc, specs_of("-$", "$index"), memory_limit=100)[0] == expected_desc


def test_sort_follows_a_growing_index(make_doc) -> None:
    doc = make_doc(jsongen.records(15000, seed=3), index=False)
    doc.start_index()
    got, rep = run(doc, specs_of("-score", "id"))
    assert doc.index_complete and rep.records == 15000
    assert got == reference(doc, specs_of("-score", "id"))


def test_runs_as_background_task(make_doc) -> None:
    doc = make_doc(jsongen.records(300, seed=6))
    task = BackgroundTask(sort_records, doc, specs_of("contact.address.city"), name="sort").start()
    rs = task.result(60)
    assert rs.to_list() == reference(doc, specs_of("contact.address.city")) and task.progress.finished
    assert rs.description == t("recordset.desc.sort", keys=SortKey("contact.address.city").describe())
    rs.close()


def test_empty_document(make_doc) -> None:
    doc = make_doc(raw=b"[]")
    got, rep = run(doc, specs_of("id"), memory_limit=10)
    assert got == [] and rep.records == 0 and rep.runs == 0 and rep.strategy == "memory"


# ----------------------------------------------------------------------------------
# property-based
# ----------------------------------------------------------------------------------
json_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(-10**20, 10**20),
    st.sampled_from([Raw(x) for x in ("1.10", "1e400", "-0.0", "0.1", "2.50", "-1E-3")]),
    st.text(alphabet="aAbB01 ʻ'oOgGsShHcCnNЎўқ-", max_size=8),
)
json_values = st.recursive(json_scalars, lambda c: st.lists(c, max_size=3) | st.dictionaries(
    st.sampled_from(["a", "b", "c"]), c, max_size=3), max_leaves=6)


def _plain(v: Any) -> Any:
    if isinstance(v, Raw):
        return codec.loads(v.raw)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


@HYP
@given(values=st.lists(st.one_of(json_values, st.just(MISSING_V)), max_size=25),
       desc=st.booleans(), nulls=st.sampled_from(["last", "first"]), natural=st.booleans(),
       cs=st.booleans(), collation=st.sampled_from(["uz", "simple", "unicode"]),
       limit=st.sampled_from([None, 1, 120]))
def test_random_values_equal_reference(tmp_path_factory, values, desc, nulls, natural, cs, collation, limit) -> None:
    p = tmp_path_factory.mktemp("hyp") / "v.json"
    p.write_bytes(array_of([v if v is MISSING_V else _plain(v) for v in values]))
    with Document.open(p) as doc:
        doc.build_index()
        specs = [SortKey("v", descending=desc, nulls=nulls, natural=natural, case_sensitive=cs, collation=collation)]
        got, _ = run(doc, specs, memory_limit=limit)
        assert got == reference(doc, specs)
    assert no_temp_files()


@pytest.mark.slow
def test_larger_external_sort(make_doc) -> None:
    doc = make_doc(jsongen.records(40000, seed=19))
    specs = specs_of("contact.address.city", "-score")
    memory, _ = run(doc, specs)
    external, rep = run(doc, specs, memory_limit=200_000)
    assert external == memory == reference(doc, specs)
    assert rep.runs > 5 and rep.peak_bytes_estimate <= 200_000 + 1000


# ----------------------------------------------------------------------------------
# R2-5: cancellation inside the in-memory sort / merges; key lists released at once
# ----------------------------------------------------------------------------------
def _big_lists_in_traceback(exc: BaseException, limit: int = 500) -> list[str]:
    """Names of the locals of the traceback's frames holding a list of more than
    ``limit`` items (those stay alive as long as the exception does)."""
    out = []
    tb = exc.__traceback__
    while tb is not None:
        for name, value in tb.tb_frame.f_locals.items():
            if isinstance(value, list) and len(value) > limit:
                out.append(f"{tb.tb_frame.f_code.co_name}.{name}={len(value)}")
            if isinstance(value, list) and value and isinstance(value[0], list):
                big = sum(len(x) for x in value if isinstance(x, list))
                if big > limit:
                    out.append(f"{tb.tb_frame.f_code.co_name}.{name}=lists of {big}")
        tb = tb.tb_next
    return out


@pytest.mark.parametrize("where", ["slices", "levels", "last_level"])
def test_sorted_pieces_checks_cancel_and_empties_its_lists(where: str) -> None:
    """Every slice sort and merge batch checks the token; a cancelled (or closed)
    generator empties every list it holds -- the keys do not wait for the traceback."""
    rnd = random.Random(5)
    entries = [bytes([rnd.randrange(256) for _ in range(6)]) for _ in range(20_000)]
    token = CancelToken()
    real_merge = rsmod._merge_runs
    calls = {"merge": 0, "batches": 0}

    def merge_spy(runs, size, release=False, cancel=None):
        calls["merge"] += 1
        last = release
        for batch in real_merge(runs, size, release, cancel):
            calls["batches"] += 1
            if (where == "levels" and not last and calls["batches"] == 3) or (where == "last_level" and last):
                token.cancel()
            yield batch

    real_check = rsmod.check
    checks = [0]

    def check_spy(cancel):
        checks[0] += 1
        if where == "slices" and checks[0] == 10:
            token.cancel()
        real_check(cancel)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rsmod, "_merge_runs", merge_spy)
        mp.setattr(rsmod, "check", check_spy)
        gen = rsmod._sorted_pieces(entries, slice_size=64, cancel=token)
        got = []
        with pytest.raises(Cancelled) as info:
            for piece in gen:
                got.append(piece)
    assert entries == []                                # consumed / emptied
    assert not _big_lists_in_traceback(info.value), _big_lists_in_traceback(info.value)
    if where == "slices":
        assert checks[0] == 10 and calls["merge"] == 0  # stopped during the slicing
    elif where == "levels":
        assert calls["batches"] == 3 and not got        # stopped inside an intermediate level
    else:
        assert len(got) <= 1                            # stopped at the first batch of the last level


def test_sorted_pieces_closed_early_empties_its_lists() -> None:
    entries = [b"%06d" % ((i * 7919) % 100_000) for i in range(10_000)]
    gen = rsmod._sorted_pieces(entries, slice_size=50)
    first = next(gen)
    assert first == sorted(first) and entries == []
    frame = gen.gi_frame
    gen.close()
    assert gen.gi_frame is None and frame is not None   # finished: the frame let go of its lists


def test_cancel_during_the_in_memory_sort_is_prompt(make_doc, monkeypatch) -> None:
    """A cancel during the merge phase of a big in-memory sort stops it within a batch
    (structurally: no merge batch after the cancel; and timed, generously); the temporary
    set is gone, the key lists are not kept by the exception and the encoders' memoised
    keys are dropped."""
    import time as _time

    doc = make_doc(raw=array_of([(i * 7919) % 50_000 for i in range(60_000)]))
    monkeypatch.setattr(rsmod, "SORT_SLICE", 32)        # a long merge: many small steps
    token = CancelToken()
    real_merge = rsmod._merge_runs
    batches = {"final": 0, "after_cancel": 0}
    cancelled_at: list[float] = []

    def merge_spy(runs, size, release=False, cancel=None):
        for batch in real_merge(runs, size, release, cancel):
            if token.cancelled:
                batches["after_cancel"] += 1
            elif release:
                batches["final"] += 1
                if batches["final"] == 20:             # well inside the last merge level
                    cancelled_at.append(_time.perf_counter())
                    token.cancel()
            yield batch

    monkeypatch.setattr(rsmod, "_merge_runs", merge_spy)
    cleared = []
    real_clear = rsmod.clear_caches
    monkeypatch.setattr(rsmod, "clear_caches", lambda: cleared.append(1) or real_clear())
    with pytest.raises(Cancelled) as info:
        sort_records(doc, specs_of("v"), cancel=token)
    ended = _time.perf_counter()
    assert cancelled_at and ended - cancelled_at[0] < 0.5, ended - cancelled_at[0]
    assert batches["after_cancel"] == 0
    assert not _big_lists_in_traceback(info.value), _big_lists_in_traceback(info.value)
    assert cleared and no_temp_files()
    # completed sorts clear the memoised keys as well
    cleared.clear()
    monkeypatch.setattr(rsmod, "_merge_runs", real_merge)
    got, _rep = run(doc, specs_of("v"))
    assert cleared and len(got) == 60_000


def test_cancel_during_run_writes_and_reduction(make_doc, monkeypatch) -> None:
    """External sort: writing a run and merging runs into a bigger one check the token
    every RUN_WRITE_ITEMS keys."""
    doc = make_doc(raw=array_of([(i * 7919) % 50_000 for i in range(20_000)]))
    monkeypatch.setattr(rsmod, "RUN_WRITE_ITEMS", 64)
    monkeypatch.setattr(rsmod, "MAX_FANIN", 4)          # the runs are merged into bigger ones first
    real_write_run = rsmod._write_run
    real_reduce = rsmod._Sorter._reduce_runs
    for target in ("spill", "reduce"):
        token = CancelToken()
        written = [0]

        def write_spy(f, entries, cancel=None, target=target):
            def counting():
                for k in entries:
                    written[0] += 1
                    if written[0] == 300 and (target == "spill" or reducing[0]):
                        token.cancel()
                    yield k
            return real_write_run(f, counting(), cancel)

        reducing = [False]

        def reduce_spy(self):
            reducing[0] = True
            written[0] = 0
            return real_reduce(self)

        monkeypatch.setattr(rsmod, "_write_run", write_spy)
        monkeypatch.setattr(rsmod._Sorter, "_reduce_runs", reduce_spy)
        with pytest.raises(Cancelled):
            sort_records(doc, specs_of("v"), memory_limit=40_000, cancel=token)
        assert 300 <= written[0] <= 300 + 64            # stopped within one write chunk
        assert no_temp_files()


def test_release_of_big_key_lists_is_sliced_and_in_the_background(make_doc, monkeypatch) -> None:
    """R2-5: the keys of a sort that ended early are freed RELEASE_SLICE at a time -- small
    amounts at once, big ones by a short daemon thread, so the cancelled task ends at once
    and no single call frees millions of keys while holding the GIL."""
    monkeypatch.setattr(rsmod, "RELEASE_SLICE", 100)
    small = [b"k%d" % i for i in range(60)]
    assert rsmod._release(small, []) is None and small == []
    a = [b"a%d" % i for i in range(1000)]
    b = [b"b%d" % i for i in range(500)]
    steps = []
    real_free = rsmod._free_lists

    def free_spy(lists):
        for lst in lists:
            while lst:
                before = len(lst)
                del lst[-rsmod.RELEASE_SLICE:]
                steps.append(before - len(lst))
        real_free(lists)

    monkeypatch.setattr(rsmod, "_free_lists", free_spy)
    thread = rsmod._release(a, b)
    assert thread is not None and thread.daemon and thread.name == "jsonreshape-release"
    thread.join(5)
    assert a == [] and b == [] and steps and max(steps) <= 100 and sum(steps) == 1500
    # a cancelled sort hands its big lists over: the task ends before they are freed
    monkeypatch.setattr(rsmod, "_free_lists", real_free)
    released: list = []
    real_release = rsmod._release
    monkeypatch.setattr(rsmod, "_release", lambda *ls: released.append(real_release(*ls)) or released[-1])
    doc = make_doc(raw=array_of([(i * 7919) % 50_000 for i in range(20_000)]))
    monkeypatch.setattr(rsmod, "SORT_SLICE", 32)
    token = CancelToken()
    real_merge = rsmod._merge_runs

    def merge_spy(runs, size, release=False, cancel=None):
        for n, batch in enumerate(real_merge(runs, size, release, cancel)):
            if release and n == 10:
                token.cancel()
            yield batch

    monkeypatch.setattr(rsmod, "_merge_runs", merge_spy)
    with pytest.raises(Cancelled) as info:
        sort_records(doc, specs_of("v"), cancel=token)
    threads = [t for t in released if t is not None]
    assert threads
    for t in threads:
        t.join(5)
    assert not _big_lists_in_traceback(info.value, limit=100)
    assert no_temp_files()
