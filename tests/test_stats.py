"""jsonreshape.stats: per-field statistics (docs/ARCHITECTURE.md §14.1, AGENT_PROMPT §9).

* :func:`field_stats` against a brute-force reference (every record parsed, ``PathPattern.values``,
  ``dupes.canonical_bytes`` groups, exact ``Decimal`` sums) for all 7 kinds (+ wrapped objects),
  both core backends, both strategies, fields at the root, under ``[*]``, pooled maps ``*``,
  ``..``/``**`` patterns, ``$``, ``$key``, concrete indices, missing fields;
* scopes: RecordSets (sorted / filtered / unsorted), repeated positions, ranges, samples, the
  base set; "first" in scope order;
* edited and pinned documents (``tests/overlay_helpers.py``): the same statistics as a plain
  file of the expected content; stale scopes;
* huge records (tiny ``large_node_bytes``): streamed with the same results (ijson number
  spellings compared by value), containers over the capture budget, duplicate keys;
* exact numbers (``0.1``, ``1e400``, ``12345678901234567890``, ``-0.0``, ``1.10``, ``-0``, ``1E5``,
  ``0.10000000000000000001``, a 5 000-digit integer), strings with escapes / lone surrogates /
  non-BMP characters, containers with different key orders, several spellings in one record;
* ``exact=True`` with spilled runs == the reference (temporary files removed), cancellation,
  progress, file changes, partial snapshots, argument errors, texts (uz / en), JSON form.

Sketches (HyperLogLog, Misra-Gries) are tested in ``tests/test_stats_sketch.py``.
"""

from __future__ import annotations

import json
import os
import random
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, reader, stats
from jsonreshape.appdirs import work_dir
from jsonreshape.codec import JsonFloat
from jsonreshape.dupes import canonical_bytes
from jsonreshape.errors import Cancelled, EditError, FileChangedError, JsonReshapeError, PathSyntaxError, UserInputError
from jsonreshape.i18n import has
from jsonreshape.paths import MISSING, PathPattern, Wild, parse_pattern
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet, filter_records, sort_records
from jsonreshape.schema import TYPE_NAMES, is_integral
from jsonreshape.settings import Settings
from jsonreshape.stats import FieldStats, field_stats
from jsonreshape.tasks import CancelToken, Progress

EXACT = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)
MEAN = Context(prec=34, Emax=MAX_EMAX, Emin=MIN_EMIN)
ROUND15 = Context(prec=15, Emax=MAX_EMAX, Emin=MIN_EMIN)


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend, monkeypatch):
    """Documents use this core backend's Scanner, the "extract" strategy its PathExtractor."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    monkeypatch.setattr(stats, "_EXTRACTOR", core_backend.PathExtractor)
    yield name
    reader.set_scanner_backend(old)


@pytest.fixture
def make_doc(tmp_path: Path):
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


def no_temp_dirs() -> bool:
    return not any(name.startswith("sort-") for name in os.listdir(work_dir()))


# ----------------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------------
# values that must group across spellings (and some that must not)
POOL_TOKENS = [
    "1", "1.0", "1e0", "10E-1", "0.1e1", "1.000", "1.10", "1.1", "1.100", "11e-1", "1e400", "1E+400", "10e399",
    "2e400", "12345678901234567890", "12345678901234567890.0", "1.234567890123456789e19", "12345678901234567891",
    "-0", "-0.0", "0", "0e5", "0.000", "0.1", "0.10", "0.10000000000000000001", "1e-1", "-1", "-1.0", "1000000",
    "1e6", "1E5", "100000", "282.0", "282", "5e-324", "-9223372036854775809", "1" + "0" * 70, "1e70",
    '"Toshkent"', '"toshkent"', '"\\u0054oshkent"', '"Тошкент"', '"\\u0422\\u043e\\u0448\\u043a\\u0435\\u043d\\u0442"',
    "\"O'zbekiston\"", '"Oʻzbekiston"', '"O‘zbekiston"', '""', '"null"', '"1"', '"1.0"', '"emoji 😀"',
    '"\\ud83d\\ude00"', '"😀"', '"\\ud800"', '"quote\\"in"', '"back\\\\slash"', '"tab\\there"', '"tab\\u0009here"',
    '"' + "long " * 20 + '"', '"' + "\\u006cong " * 20 + '"',
    "true", "false", "null",
    '{"a": 1, "b": [1, 2]}', '{"b": [1, 2.0], "a": 1e0}', '{"a": 1, "b": [2, 1]}', "[1, 2]", "[2, 1]",
    "[1.0, 2e0]", "[]", "{}", "[[]]", "[{}]", '{"x": {"y": [1, {"z": null}]}}', '{"x":{"y":[1.0,{"z":null}]}}',
]


def pool_value(rng: random.Random) -> bytes:
    return rng.choice(POOL_TOKENS).encode("utf-8")


def stat_record(rng: random.Random, i: int) -> bytes:
    """One record as JSON text: jsongen's realistic record plus POOL values in several
    spellings (``k``; ``arr`` -- a multi-valued field holding equal values in different
    spellings, also inside one record), a map ``m``, nested arrays ``grid`` and ``objs``."""
    rec = jsongen.rand_record(rng, i)
    parts = [jsongen.encode(rec, "compact", seed=i)[1:-1]]
    if rng.random() < 0.85:
        parts.append(b'"k": ' + pool_value(rng))
    if rng.random() < 0.7:
        parts.append(b'"arr": [' + b", ".join(pool_value(rng) for _ in range(rng.randint(0, 5))) + b"]")
    if rng.random() < 0.6:
        members = [b'"%s": %s' % (rng.choice([b"a", b"b", b"c", b"d"]), pool_value(rng)) for _ in range(rng.randint(0, 3))]
        parts.append(b'"m": {' + b", ".join(members) + b"}")
    if rng.random() < 0.6:
        rows = [b"[" + b", ".join(pool_value(rng) for _ in range(rng.randint(0, 3))) + b"]"
                for _ in range(rng.randint(0, 3))]
        parts.append(b'"grid": [' + b", ".join(rows) + b"]")
    if rng.random() < 0.6:
        objs = []
        for _ in range(rng.randint(0, 3)):
            y = b", ".join(b'"%d": %s' % (rng.randint(0, 3), pool_value(rng)) for _ in range(rng.randint(0, 2)))
            o = b'"y": {' + y + b"}" if rng.random() < 0.8 else b'"y": 5'
            if rng.random() < 0.7:
                o += b', "x": ' + pool_value(rng)
            objs.append(b"{" + o + b"}")
        parts.append(b'"objs": [' + b", ".join(objs) + b"]")
    return b"{" + b", ".join(p for p in parts if p) + b"}"


def stat_records(n: int, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    return [stat_record(rng, i) for i in range(n)]


def document_bytes(recs: list[bytes], kind: str, keys: list[str] | None = None) -> tuple[bytes, dict[str, Any]]:
    """(file bytes, Document.open keyword arguments) of ``recs`` in ``kind``."""
    if kind == "array":
        return b"[\n" + b",\n".join(recs) + b"\n]", {}
    if kind == "jsonl":
        return b"".join(r + b"\n" for r in recs), {"format": "jsonl"}
    if kind == "stream":
        return b"\n\n".join(recs) + b"\n", {"format": "stream"}
    if kind == "brace_list":
        return b"{" + b",\n".join(recs) + b"}", {"format": "brace_list"}
    if kind == "wrapped":
        return b'{"meta": {"v": 1}, "data": [' + b",".join(recs) + b'], "tail": 1}', {"record_path": "data"}
    if kind in ("keyed", "wrapped_obj"):
        keys = keys or [f"k{i}" for i in range(len(recs))]
        members = b",\n".join(codec.dumps(k) + b": " + r for k, r in zip(keys, recs))
        if kind == "keyed":
            return b"{" + members + b"}", {"format": "keyed"}
        return b'{"meta": [1], "data": {' + members + b'}}', {"record_path": "data"}
    if kind == "single":
        return b"[" + b",".join(recs) + b"]", {"format": "single"}
    raise ValueError(kind)


FIELDS = [
    "k", "arr[*]", "id", "name", "contact.address.city", "contact.address", "contact.*", "orders[*].sum",
    "orders[*]", "orders[*].oid", "orders[0].sum", "tags[*]", "tags", "score", "note", "extra", "..sum", "..oid",
    "$", "m.*", "grid[*][*]", "grid[*]", "objs[*].y.*", "objs[*].x", "objs[*].y", "missing.x", "**.city",
    'objs[*].y["1"]', "arr[0]", "orders[*].*",
]
SINGLE_FIELDS = ["$", "[*]", "[*].k", "[*].arr[*]", "[*].orders[*].sum", "[*].m.*", "..k", "[*].objs[*].y.*",
                 "[0].k"]


# ----------------------------------------------------------------------------------
# the reference
# ----------------------------------------------------------------------------------
def context_of(pattern: PathPattern) -> tuple[PathPattern | None, PathPattern | None]:
    segs = pattern.segments
    if any(s is Wild.DESCENDANTS for s in segs):
        return None, None
    for i in range(len(segs) - 2, -1, -1):
        if segs[i] is Wild.ANY_INDEX:
            return PathPattern(segs[:i + 1]), PathPattern(segs[i + 1:])
    return None, None


def number_dec(v: Any) -> Decimal:
    return Decimal(codec.number_text(v))


def mean_value(total: Decimal, count: int) -> Decimal:
    return ROUND15.plus(MEAN.divide(total, Decimal(count)))


def reference(values_of: list[Any], keys: list[str] | None, field: str, positions: list[int], top: int,
              streamed: bool = False) -> dict[str, Any]:
    """The expected statistics: ``values_of[pos]`` = the parsed record value at a position,
    ``keys`` the member keys (None: no member records), ``positions`` the scope in order (a
    repeated position is examined once). Number texts compare by value when ``streamed``."""
    pat = None if field == "$key" else parse_pattern(field)
    cpat, rest = context_of(pat) if pat is not None else (None, None)
    seen: set[int] = set()
    groups: dict[bytes, list[Any]] = {}
    base = present = nvalues = scanned = 0
    types = {name: 0 for name in TYPE_NAMES}
    nums: list[tuple[Decimal, tuple[int, int], Any]] = []
    integers = 0
    strs: list[int] = []
    empty = 0
    arrs: list[int] = []
    objs: list[int] = []
    for o, pos in enumerate(positions):
        if pos in seen:
            continue
        seen.add(pos)
        scanned += 1
        rec = values_of[pos]
        if pat is None:
            vals = [keys[pos]] if keys is not None else []
            base += 1
            present += 1 if vals else 0
        elif cpat is None:
            vals = pat.values(rec)
            base += 1
            present += 1 if vals else 0
        else:
            insts = cpat.values(rec)
            base += len(insts)
            vals = []
            for inst in insts:
                rv = rest.values(inst)
                if rv:
                    present += 1
                    vals.extend(rv)
        nvalues += len(vals)
        for i, v in enumerate(vals):
            tp = codec.json_type(v)
            types[tp] += 1
            cb = canonical_bytes(v)
            g = groups.get(cb)
            if g is None:
                g = groups[cb] = [0, set(), (o, i), v]
            g[0] += 1
            g[1].add(pos)
            if tp == "number":
                nums.append((number_dec(v), (o, i), v))
                if is_integral(v):
                    integers += 1
            elif tp == "string":
                strs.append(len(v))
                if v == "":
                    empty += 1
            elif tp == "array":
                arrs.append(len(v))
            elif tp == "object":
                objs.append(len(v))
    order = {name: i for i, name in enumerate(TYPE_NAMES)}
    best = sorted(groups.values(), key=lambda g: (-g[0], g[2]))[:top]
    number = None
    if nums:
        mn = min(nums, key=lambda x: (x[0], x[1]))
        mx = min(nums, key=lambda x: (-x[0], x[1]))
        total = Decimal(0)
        for d, _, _ in nums:
            total = EXACT.add(total, d)
        mn_text, mx_text = codec.number_text(mn[2]), codec.number_text(mx[2])
        if streamed:
            mn_text, mx_text = Decimal(mn_text), Decimal(mx_text)
        number = (len(nums), integers, mn_text, mx_text, positions[mn[1][0]], positions[mx[1][0]], total,
                  mean_value(total, len(nums)), True)
    return {
        "scanned": scanned, "skipped": 0, "base": base, "present": present, "missing": base - present,
        "values": nvalues, "nulls": types["null"],
        "types": [(k, n) for k, n in sorted(types.items(), key=lambda kv: (-kv[1], order[kv[0]])) if n],
        "distinct": len(groups), "distinct_exact": True, "top_exact": True, "top_bound": 0,
        "top": [(top_text(g[3], streamed), g[0], len(g[1]), positions[g[2][0]], 0, top_json(g[3], streamed))
                for g in best],
        "number": number,
        "string": (min(strs), max(strs), empty) if strs else None,
        "arr_len": (min(arrs), max(arrs)) if arrs else None,
        "obj_keys": (min(objs), max(objs)) if objs else None,
        "uncounted": 0,
    }


def top_text(v: Any, streamed: bool) -> Any:
    if streamed and codec.json_type(v) == "number":
        return ("number", number_dec(v))
    if streamed and codec.json_type(v) in ("object", "array"):
        return ("container", canonical_bytes(v))
    return stats._value_text(v)


def top_json(v: Any, streamed: bool) -> Any:
    if streamed:
        return canonical_bytes(v)
    return codec.dumps(v)


def summary(fs: FieldStats, streamed: bool = False) -> dict[str, Any]:
    """The comparable part of a result (shape of :func:`reference`)."""
    number = None
    if fs.number is not None:
        n = fs.number
        mn: Any = n.min_text
        mx: Any = n.max_text
        if streamed:
            mn, mx = Decimal(mn), Decimal(mx)
        number = (n.count, n.integers, mn, mx, n.min_first, n.max_first, Decimal(n.sum_text), Decimal(n.mean_text),
                  n.exact_sum)
    tops = []
    for v in fs.top:
        assert v.value is not MISSING, v
        text: Any = v.text
        if streamed and codec.json_type(v.value) == "number":
            text = ("number", number_dec(v.value))
        elif streamed and codec.json_type(v.value) in ("object", "array"):
            text = ("container", canonical_bytes(v.value))
        tops.append((text, v.count, v.records, v.first, v.error,
                     canonical_bytes(v.value) if streamed else codec.dumps(v.value)))
    return {
        "scanned": fs.scanned, "skipped": fs.skipped, "base": fs.base, "present": fs.present, "missing": fs.missing,
        "values": fs.values, "nulls": fs.nulls, "types": list(fs.types.items()), "distinct": fs.distinct,
        "distinct_exact": fs.distinct_exact, "top_exact": fs.top_exact, "top_bound": fs.top_bound, "top": tops,
        "number": number,
        "string": None if fs.string is None else (fs.string.min_len, fs.string.max_len, fs.string.empty),
        "arr_len": fs.arr_len, "obj_keys": fs.obj_keys, "uncounted": fs.uncounted,
    }


def assert_same(got: dict[str, Any], want: dict[str, Any], field: str) -> None:
    if got != want:
        diff = {k: (got.get(k), want.get(k)) for k in set(got) | set(want) if got.get(k) != want.get(k)}
        raise AssertionError(f"{field}: {diff}")


def values_of(doc: Document) -> tuple[list[Any], list[str] | None]:
    """The parsed record values (member values) and member keys of a document."""
    vals = [doc.record(i) for i in range(doc.record_count)]
    keys = [doc.record_key(i) for i in range(doc.record_count)] if doc.member_records else None
    return vals, keys


def check(doc: Document, fields: list[str], source: Any = None, *, top: int = 20, ref_doc: Document | None = None,
          streamed: bool = False, **kw: Any) -> list[FieldStats]:
    vals, keys = values_of(ref_doc or doc)
    if source is None:
        positions = list(range(len(vals)))
    elif isinstance(source, RecordSet):
        positions = list(source)
    else:
        positions = [int(p) for p in source]
        if iter(source) is source:      # a one-shot iterator: the engine gets a fresh one
            source = iter(positions)
    if kw.get("sample") is not None and len(positions) > kw["sample"]:
        from jsonreshape.schema import sample_positions

        positions = [positions[p] for p in sample_positions(len(positions), kw["sample"])]
    res = field_stats(doc, fields, source, top=top, **kw)
    assert len(res) == len(fields)
    for f, fs in zip(fields, res):
        assert_same(summary(fs, streamed), reference(vals, keys, f, positions, top, streamed), f)
        assert fs.missing == fs.base - fs.present and fs.values == sum(fs.types.values())
        assert fs.nulls == fs.types.get("null", 0)
    return res


# ----------------------------------------------------------------------------------
# every kind, both backends, both strategies
# ----------------------------------------------------------------------------------
KINDS = ["array", "jsonl", "stream", "brace_list", "wrapped", "keyed", "wrapped_obj"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_matches_reference(make_doc, backend, kind, strategy) -> None:
    recs = stat_records(160, seed=11)
    data, kw = document_bytes(recs, kind)
    doc = make_doc(data, **kw)
    assert doc.kind == ("wrapped" if kind == "wrapped_obj" else kind)
    fields = FIELDS + (["$key"] if kind in ("keyed", "wrapped_obj") else [])
    check(doc, fields, strategy=strategy)


@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_single_document(make_doc, backend, strategy) -> None:
    recs = stat_records(40, seed=12)
    data, kw = document_bytes(recs, "single")
    doc = make_doc(data, **kw)
    assert doc.kind == "single" and doc.record_count == 1
    check(doc, SINGLE_FIELDS, strategy=strategy)


def test_styles_and_encodings_give_the_same_results(make_doc) -> None:
    # (jsongen cannot write a lone surrogate unescaped: those records are left out)
    recs = [json.loads(r) for r in stat_records(60, seed=13) if b"\\ud800" not in r]
    want = None
    for kw in ({"style": "pretty2"}, {"style": "messy_escaped"}, {"gz": True, "encoding": "utf-16-le", "bom": True,
                                                                   "newline": "\r\n", "style": "pretty4"}):
        doc = make_doc(recs, "array", **kw)
        res = field_stats(doc, ["name", "contact.address.city", "orders[*].sum", "tags[*]"])
        got = [(fs.distinct, fs.values, fs.present, [(v.count, v.first) for v in fs.top]) for fs in res]
        assert want is None or got == want
        want = got


# ----------------------------------------------------------------------------------
# scopes
# ----------------------------------------------------------------------------------
def test_scopes(make_doc, backend) -> None:
    recs = stat_records(200, seed=21)
    data, kw = document_bytes(recs, "jsonl")
    doc = make_doc(data, **kw)
    fields = ["k", "arr[*]", "orders[*].sum", "contact.address.city", "objs[*].y.*", "..oid"]
    # a sorted view: "first" is the first record in the view's order
    srt = sort_records(doc, ["-score"])
    check(doc, fields, srt)
    flt = filter_records(doc, "exists(k)")
    check(doc, fields, flt)
    # unsorted positions with repeats (each examined once), a range, a sample
    rng = random.Random(5)
    pos = [rng.randrange(200) for _ in range(150)]
    check(doc, fields, pos)
    check(doc, fields, range(20, 140))
    check(doc, fields, iter(pos))
    check(doc, fields, srt, sample=37)
    check(doc, fields, None, sample=50)
    # the base set means every record
    base = check(doc, fields, RecordSet.base(doc))
    assert base[0].scanned == 200
    # an empty scope
    for fs in field_stats(doc, fields, []):
        assert (fs.scanned, fs.values, fs.distinct, fs.top, fs.number) == (0, 0, 0, [], None)


def test_partial_documents_and_broken_lines(make_doc) -> None:
    doc = make_doc(b'[{"a": 1}, {"a": 2}, {"a": 2}, {"a": ')
    assert doc.error is not None and doc.record_count == 3
    fs = field_stats(doc, "a")[0]
    assert (fs.scanned, fs.distinct, fs.top[0].text, fs.top[0].count, fs.number.sum_text) == (3, 2, "2", 2, "5")
    doc = make_doc(b'{"a": 1}\n{"a": oops}\n{"a": 3}\n', format="jsonl")
    assert doc.broken_count == 1
    fs = field_stats(doc, "a")[0]
    assert (fs.scanned, fs.values, fs.number.sum_text) == (2, 2, "4")


def test_first_follows_scope_order(make_doc) -> None:
    doc = make_doc(b'[{"v": "a"}, {"v": "b"}, {"v": "a"}, {"v": "b"}, {"v": "c"}]')
    fs = field_stats(doc, "v")[0]
    assert [(v.text, v.count, v.first) for v in fs.top] == [('"a"', 2, 0), ('"b"', 2, 1), ('"c"', 1, 4)]
    fs = field_stats(doc, "v", [4, 3, 2, 1, 0])[0]
    assert [(v.text, v.count, v.first) for v in fs.top] == [('"b"', 2, 3), ('"a"', 2, 2), ('"c"', 1, 4)]


# ----------------------------------------------------------------------------------
# semantics in detail
# ----------------------------------------------------------------------------------
def test_presence_contexts(make_doc) -> None:
    doc = make_doc(b"\n".join([
        b'{"orders": [{"sum": 1}, {"sum": null}, {"x": 1}, 7], "tags": ["a", "b", "a"], "m": {"a": 1, "b": 2}}',
        b'{"orders": [], "tags": [], "m": {}}',
        b'{"orders": [{"items": [1, 2]}, {"items": []}, {"items": "x"}], "m": {"c": [3]}}',
        b'{"note": null}',
    ]) + b"\n", format="jsonl")
    s = {fs.field: fs for fs in field_stats(doc, ["orders[*].sum", "tags[*]", "orders[*].items[*]", "m.*", "note",
                                                  "orders[*]", "orders[*].*"])}
    o = s["orders[*].sum"]
    assert (o.context, o.base, o.present, o.missing, o.values, o.nulls) == ("orders[*]", 7, 2, 5, 2, 1)
    tg = s["tags[*]"]
    assert (tg.context, tg.base, tg.present, tg.missing, tg.values, tg.multi) == ("$", 4, 1, 3, 3, True)
    assert [(v.text, v.count, v.records) for v in tg.top] == [('"a"', 2, 1), ('"b"', 1, 1)]
    it = s["orders[*].items[*]"]
    assert (it.context, it.base, it.present, it.values) == ("orders[*]", 7, 1, 2)
    m = s["m.*"]
    assert (m.base, m.present, m.values, m.types) == (4, 2, 3, {"number": 2, "array": 1})
    nt = s["note"]
    assert (nt.base, nt.present, nt.missing, nt.nulls, nt.presence) == (4, 1, 3, 1, 0.25)
    od = s["orders[*]"]
    assert (od.context, od.base, od.present, od.values) == ("$", 4, 2, 7)
    star = s["orders[*].*"]
    assert (star.context, star.base, star.present, star.values) == ("orders[*]", 7, 6, 6)


def test_numbers_are_exact(make_doc) -> None:
    big = "9" * 5000
    toks = ["0.1", "1e400", "12345678901234567890", "-0.0", "1.10", "-0", "1E5", "0.10000000000000000001", big,
            "1.1", "0", "100000", "-1e400"]
    doc = make_doc(("[" + ",".join('{"n": %s}' % t for t in toks) + "]").encode())
    fs = check(doc, ["n"])[0]
    n = fs.number
    assert n is not None
    assert (n.min_text, n.max_text, n.min_first, n.max_first) == ("-1e400", big, 12, 8)
    assert n.count == 13 and n.integers == 9 and n.exact_sum      # 1e400, ints, -0, -0.0, 1E5, 0, 100000, -1e400
    want = Decimal(0)
    for tok in toks:
        want = EXACT.add(want, Decimal(tok))
    assert Decimal(n.sum_text) == want
    assert n.sum_text == "1" + "0" * 4980 + "12345678901234767891.40000000000000000001"
    # equal values are one value; the spelling shown is the first one
    top = {v.text: (v.count, v.first) for v in fs.top}
    assert top["1.10"] == (2, 4) and "1.1" not in top
    assert top["-0.0"] == (3, 3) and "-0" not in top and "0" not in top
    assert top["1E5"] == (2, 6) and "100000" not in top
    assert fs.distinct == 9
    # exact texts never become strings or floats
    v = {x.text: x.value for x in fs.top}
    assert isinstance(v["1e400"], JsonFloat) and v["1e400"].raw == "1e400"
    assert v["12345678901234567890"] == 12345678901234567890 and isinstance(v["12345678901234567890"], int)
    assert isinstance(v["-0.0"], float) and codec.dumps(v["-0.0"]) == b"-0.0"
    assert codec.dumps(v["0.10000000000000000001"]) == b"0.10000000000000000001"


def test_mean_and_sum_texts(make_doc) -> None:
    doc = make_doc(b'[{"n": 1}, {"n": 2}, {"n": 2}]')
    n = field_stats(doc, "n")[0].number
    assert (n.sum_text, n.mean_text, n.integers) == ("5", "1.66666666666667", 3)
    doc = make_doc(b'[{"n": 1.10}, {"n": 2.20}]')
    n = field_stats(doc, "n")[0].number
    assert (n.sum_text, n.mean_text, n.integers) == ("3.30", "1.65", 0)
    doc = make_doc(b'[{"n": 1e400}, {"n": 1e400}]')
    n = field_stats(doc, "n")[0].number
    assert (Decimal(n.sum_text), n.mean_text) == (Decimal("2e400"), "1E+400")
    doc = make_doc(b'[{"n": -0.0}, {"n": 0}]')
    n = field_stats(doc, "n")[0].number
    assert (n.sum_text, n.mean_text, n.min_text, n.max_text) == ("0", "0", "-0.0", "-0.0")
    doc = make_doc(b'[{"n": 0.1}, {"n": 0.2}]')
    n = field_stats(doc, "n")[0].number
    assert (n.sum_text, n.mean_text) == ("0.3", "0.15")
    doc = make_doc(b'[{"n": 1e-30}, {"n": 3e-30}]')
    n = field_stats(doc, "n")[0].number
    assert Decimal(n.sum_text) == Decimal("4e-30") and Decimal(n.mean_text) == Decimal("2e-30")


def test_sum_guard_switches_to_50_digits(make_doc, lang_en) -> None:
    doc = make_doc(b'[{"n": 1e999999}, {"n": 1}, {"n": 0.5}]')
    fs = field_stats(doc, "n")[0]
    n = fs.number
    assert not n.exact_sum and "stats.warn.sum_approximate" in fs.warnings
    assert Decimal(n.sum_text) == Decimal("1e999999")
    assert "50 significant digits" in " ".join(fs.warning_texts())
    # numbers beyond the Decimal range are compared and shown, but not summed exactly
    doc = make_doc(b'[{"n": 1e99999999999999999999}, {"n": 2}, {"n": -1e99999999999999999999}]')
    fs = field_stats(doc, "n")[0]
    n = fs.number
    assert (n.min_text, n.max_text, n.count, n.exact_sum) == ("-1e99999999999999999999", "1e99999999999999999999",
                                                              3, False)
    assert fs.distinct == 3


def test_strings_escapes_surrogates_and_non_bmp(make_doc) -> None:
    doc = make_doc('[{"s": "\\u0041"}, {"s": "A"}, {"s": "😀"}, {"s": "\\ud83d\\ude00"}, {"s": "\\ud800"}, '
                   '{"s": ""}, {"s": "tab\\there"}, {"s": "\\u202e"}, {"s": "x\\u0000y"}]'.encode())
    fs = check(doc, ["s"])[0]
    top = {v.text: (v.count, v.first) for v in fs.top}
    assert top['"A"'] == (2, 0) and top['"😀"'] == (2, 2)
    assert top['"\\ud800"'] == (1, 4)
    assert top['"\\u202e"'] == (1, 7)               # bidi controls shown escaped
    assert top['"tab\\there"'] == (1, 6) and top['"x\\u0000y"'] == (1, 8)
    assert (fs.string.min_len, fs.string.max_len, fs.string.empty) == (0, 8, 1)
    assert fs.distinct == 7


def test_containers_ignore_member_order(make_doc) -> None:
    doc = make_doc(b'[{"c": {"a": 1, "b": [1, 2.0]}}, {"c": {"b": [1, 2], "a": 1.0}}, {"c": {"a": 1, "b": [2, 1]}},'
                   b' {"c": []}, {"c": {}}]')
    fs = check(doc, ["c"])[0]
    assert fs.distinct == 4 and fs.top[0].count == 2 and fs.top[0].text == '{"a":1,"b":[1,2.0]}'
    assert (fs.arr_len, fs.obj_keys) == ((0, 0), (0, 2))


def test_spellings_in_one_record_count_the_record_once(make_doc) -> None:
    doc = make_doc(b'[{"a": [1, 1.0, 1e0, "x", "\\u0078"]}, {"a": [1.00]}, {"a": ["x", 2]}]')
    fs = check(doc, ["a[*]"])[0]
    top = [(v.text, v.count, v.records, v.first) for v in fs.top]
    assert top == [("1", 4, 2, 0), ('"x"', 3, 2, 0), ("2", 1, 1, 2)]


def test_long_values_are_hashed_and_read_again(make_doc) -> None:
    long1 = "x" * 300
    escaped = "".join("\\u%04x" % ord(c) for c in "yz" * 40)
    doc = make_doc(json.dumps([{"s": long1}, {"s": "yz" * 40}, {"s": long1}, {"t": [long1]}]).encode()[:-1]
                   + b', {"s": "' + escaped.encode() + b'"}]')
    fs = check(doc, ["s", "t", "t[*]", "$"])
    top = fs[0].top
    assert top[0].count == 2 and top[0].value == long1 and top[0].text.endswith("…") and len(top[0].text) == 120
    assert top[1].count == 2 and top[1].value == "yz" * 40 and top[1].first == 1


def test_key_field(make_doc, backend, lang_en) -> None:
    recs = stat_records(30, seed=31)
    keys = [f"k{i % 7}" for i in range(30)] + []
    data, kw = document_bytes(recs, "keyed", keys=[f"id{i % 25}" if i % 3 else f"Т{i}" for i in range(30)])
    doc = make_doc(data, **kw)
    check(doc, ["$key", "k"])
    # documents without member keys: every record counts as missing, with a warning
    data, kw = document_bytes(recs, "jsonl")
    doc = make_doc(data, **kw)
    fs = field_stats(doc, "$key")[0]
    assert (fs.scanned, fs.base, fs.present, fs.missing, fs.values, fs.is_key) == (30, 30, 0, 30, 0, True)
    assert fs.warnings == ("stats.warn.no_record_keys",) and "30 records" in fs.warning_texts()[0]
    assert keys  # (unused list kept readable)


def test_missing_field_warns(make_doc, lang_en) -> None:
    doc = make_doc(b'[{"a": 1}]')
    fs = field_stats(doc, "b")[0]
    assert (fs.present, fs.missing, fs.values, fs.distinct, fs.top) == (0, 1, 0, 0, [])
    assert fs.warnings == ("stats.warn.field_absent",)
    assert fs.warning_texts() == ["The field b was not found in the 1 record checked. Check the spelling of the "
                                  "field name."]


def test_top_order_ties_and_limits(make_doc) -> None:
    doc = make_doc(b'[{"t": ["b", "a"]}, {"t": ["a", "b", "c"]}, {"t": ["c"]}]')
    fs = field_stats(doc, "t[*]", top=2)[0]
    assert [(v.text, v.count, v.first) for v in fs.top] == [('"b"', 2, 0), ('"a"', 2, 0)]
    assert fs.distinct == 3
    assert field_stats(doc, "t[*]", top=0)[0].top == []
    assert len(field_stats(doc, "t[*]", top=1000)[0].top) == 3


def test_fields_named_twice_and_order(make_doc) -> None:
    doc = make_doc(b'[{"a": 1, "b": 2}]')
    res = field_stats(doc, ["b", "a", "$.b", "b"])
    assert [fs.field for fs in res] == ["b", "a", "b", "b"]
    assert res[0] is res[2] is res[3]


# ----------------------------------------------------------------------------------
# huge records (streamed)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "single"])
def test_huge_records_are_streamed(make_doc, backend, kind) -> None:
    recs = stat_records(50, seed=41)
    # lone surrogates arrive as "?" from ijson (documented): not in these records
    recs = [r.replace(b'"\\ud800"', b'"x"') for r in recs]
    data, kw = document_bytes(recs, kind)
    plain = make_doc(data, name=f"plain-{kind}.json", **kw)
    huge = make_doc(data, name=f"huge-{kind}.json", settings=Settings(large_node_bytes=200), **kw)
    assert any(huge.is_huge(i) for i in range(huge.record_count))
    fields = (SINGLE_FIELDS if kind == "single" else FIELDS) + (["$key"] if kind == "keyed" else [])
    check(huge, fields, ref_doc=plain, streamed=True)
    check(huge, fields, ref_doc=plain, streamed=True, exact=True, memory_limit=500)


def test_huge_single_record_spilling_inside_the_record(make_doc, monkeypatch) -> None:
    """A single-record accumulator spills several epochs that all hold its one record: each value
    still counts that record once (exact mode), and the sketches merge it (approximate mode)."""
    monkeypatch.setattr(stats, "TICK_VALUES", 5)
    recs = stat_records(80, seed=42)
    recs = [r.replace(b'"\\ud800"', b'"x"') for r in recs]
    data, kw = document_bytes(recs, "single")
    plain = make_doc(data, name="plain-single.json", **kw)
    huge = make_doc(data, name="huge-single.json", settings=Settings(large_node_bytes=200), **kw)
    assert huge.is_huge(0)
    spills: list[bool] = []
    real = stats._FieldAcc.spill

    def spy(self: Any, *args: Any) -> None:   # (args: the record a spill happens inside, R5A-1)
        spills.append(self.set.single)
        real(self, *args)

    monkeypatch.setattr(stats._FieldAcc, "spill", spy)
    fields = SINGLE_FIELDS + ["[*].orders[*].sum", "[*].tags[*]", "..oid"]   # (no containers: no second pass)
    check(huge, fields, ref_doc=plain, streamed=True, exact=True, memory_limit=300)
    assert spills.count(True) >= 10        # spilled inside the record
    res = field_stats(huge, ["[*].arr[*]", "[*].k"], memory_limit=300)
    ref = check(plain, ["[*].arr[*]", "[*].k"])
    for fs, ex in zip(res, ref):
        assert not fs.distinct_exact and fs.values == ex.values and fs.types == ex.types
        truth = {v.text: (v.count, v.records) for v in ex.top}
        for v in fs.top:
            if v.text in truth:
                assert v.count <= truth[v.text][0] <= v.count + v.error and v.records == 1
    assert no_temp_dirs()


def test_huge_record_containers_over_the_capture_budget(make_doc, lang_en) -> None:
    big = [{"i": i, "pad": "x" * 2000} for i in range(800)]
    rec = json.dumps({"data": big, "n": [1, 2, 3]}).encode()
    doc = make_doc(rec + b"\n" + b'{"data": [{"i": 1}], "n": []}\n', format="jsonl",
                   settings=Settings(large_node_bytes=10_000, memory_limit_bytes=4 << 20))
    assert doc.is_huge(0)
    fs = field_stats(doc, ["data[*]", "data[*].i", "data", "n[*]"])
    d = fs[0]
    assert d.uncounted == 800 and d.values == 801 and d.types == {"object": 801}
    assert d.obj_keys == (1, 2) and d.distinct == 1 and d.top[0].count == 1
    assert d.warnings == ("stats.warn.uncounted",)
    assert "800 object or array values (the first in record #0) are too large" in d.warning_texts()[0]
    i = fs[1]
    assert (i.values, i.distinct, i.top[0].text, i.top[0].count, i.uncounted) == (801, 800, "1", 2, 0)
    whole = fs[2]
    assert whole.uncounted == 1 and whole.arr_len == (1, 800)
    assert fs[3].distinct == 3 and fs[3].base == 2 and fs[3].present == 1


def test_huge_record_with_duplicate_keys_uses_dict_semantics(make_doc) -> None:
    rec = b'{"a": [{"x": 1, "x": 2}, {"x": 3}], "b": {"y": 1, "y": "z"}, "pad": "' + b"p" * 500 + b'"}'
    doc = make_doc(b"[" + rec + b", " + rec + b"]", settings=Settings(large_node_bytes=100))
    plain = make_doc(b"[" + rec + b", " + rec + b"]")
    assert doc.is_huge(0)
    check(doc, ["a[*].x", "b.y", "a[*]", "b", "..x"], ref_doc=plain, streamed=True)


def test_huge_record_values_over_the_budget_are_skipped(make_doc, lang_en) -> None:
    long = "y" * 1_100_000
    rec = b'{"a": [{"x": "' + long.encode() + b'"}, {"x": "' + long.encode() + b'", "x": 1}]}'
    doc = make_doc(rec + b"\n" + b'{"a": [{"x": 2}]}\n', format="jsonl", settings=Settings(large_node_bytes=1000))
    fs = field_stats(doc, "a[*].x")[0]
    assert fs.skipped == 1 and fs.scanned == 1 and fs.values == 1
    assert fs.warnings == ("stats.warn.too_large",)
    assert "#0" in fs.warning_texts()[0]


def test_unreadable_streamed_record_is_skipped(make_doc, monkeypatch, lang_en) -> None:
    from jsonreshape import stream

    doc = make_doc(b'[{"a": 1}, {"a": 2, "pad": "' + b"q" * 300 + b'"}, {"a": 3}]',
                   settings=Settings(large_node_bytes=100))
    real = stream._value_events

    def broken(doc_: Any, span: Any, member: bool) -> Any:
        if span == doc_.record_span(1):
            raise JsonReshapeError("stream.error.parse", detail="test")
        return real(doc_, span, member)

    monkeypatch.setattr(stream, "_value_events", broken)
    fs = field_stats(doc, "a")[0]
    assert (fs.scanned, fs.skipped, fs.values, fs.warnings) == (2, 1, 2, ("stats.warn.unreadable",))
    assert "1 record (#1) could not be read" in fs.warning_texts()[0]


def test_extractor_refusals_fall_back_to_parsing(make_doc, backend, core_backend, monkeypatch) -> None:
    recs = stat_records(60, seed=51)
    data, kw = document_bytes(recs, "array")
    doc = make_doc(data, **kw)
    bad = doc.record_bytes(7)
    calls = {"many": 0, "refused": 0}

    class Picky:
        def __init__(self, paths: Any, member_spans: bool = False) -> None:
            self.inner = core_backend.PathExtractor(paths, member_spans=member_spans)

        def extract(self, buf: bytes, spans: bytes) -> bytes:
            from array import array

            pairs = array("Q")
            pairs.frombytes(spans)
            if len(pairs) > 2:
                calls["many"] += 1
                raise ValueError("refused (test)")
            if buf[pairs[0]:pairs[1]] == bad:
                calls["refused"] += 1
                raise ValueError("refused (test)")
            return self.inner.extract(buf, spans)

    monkeypatch.setattr(stats, "_EXTRACTOR", Picky)
    check(doc, FIELDS, strategy="extract")
    assert calls["many"] > 0 and calls["refused"] >= 1


# ----------------------------------------------------------------------------------
# exact mode (spilled runs) and budgets
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "keyed"])
def test_exact_mode_with_spilled_runs(make_doc, backend, kind, monkeypatch) -> None:
    recs = stat_records(250, seed=61)
    data, kw = document_bytes(recs, kind)
    doc = make_doc(data, **kw)
    fields = FIELDS + (["$key"] if kind == "keyed" else [])
    spills: list[int] = []
    real = stats._FieldAcc.spill

    def spy(self: Any, *args: Any) -> None:   # (args: the record a spill happens inside, R5A-1)
        spills.append(len(self.d))
        real(self, *args)

    monkeypatch.setattr(stats._FieldAcc, "spill", spy)
    res = check(doc, fields, exact=True, memory_limit=3000)
    assert len(spills) > 10
    assert all(fs.distinct_exact and fs.top_exact for fs in res)
    assert no_temp_dirs()


def test_tiny_budget_switches_to_sketches(make_doc, lang_en) -> None:
    recs = stat_records(200, seed=62)
    data, kw = document_bytes(recs, "array")
    doc = make_doc(data, **kw)
    exact = {fs.field: fs for fs in field_stats(doc, FIELDS)}
    approx = {fs.field: fs for fs in field_stats(doc, FIELDS, memory_limit=1000)}
    switched = [f for f, fs in approx.items() if not fs.distinct_exact]
    assert "id" in switched and "$" in switched
    for f, fs in approx.items():
        ex = exact[f]
        # everything but distinct / top stays exact
        assert (fs.base, fs.present, fs.values, fs.types, fs.number, fs.string, fs.arr_len, fs.obj_keys) == \
               (ex.base, ex.present, ex.values, ex.types, ex.number, ex.string, ex.arr_len, ex.obj_keys), f
        if not fs.distinct_exact:
            assert fs.distinct_rse == stats.HLL_RSE and not fs.top_exact
            assert abs(fs.distinct - ex.distinct) <= max(3, 0.05 * ex.distinct), f
            assert "stats.warn.approximate" in fs.warnings
            truth = {v.text: v.count for v in ex.top}
            for v in fs.top:
                assert v.error == fs.top_bound
                if v.text in truth:
                    assert v.count <= truth[v.text] <= v.count + v.error
        else:
            assert summary(fs) == summary(ex), f


def test_budget_goes_to_the_largest_field_first(make_doc) -> None:
    recs = [{"id": i, "c": i % 3} for i in range(3000)]
    doc = make_doc(recs, "array")
    res = field_stats(doc, ["id", "c"], memory_limit=200_000)
    assert not res[0].distinct_exact and res[1].distinct_exact and res[1].distinct == 3


def test_bad_arguments(make_doc) -> None:
    doc = make_doc(b'[{"a": 1}]')
    for fields in ([], "", "  ", [""], [5], None, 5):
        with pytest.raises(UserInputError) as ei:
            field_stats(doc, fields)  # type: ignore[arg-type]
        assert ei.value.key.startswith("stats.error.")
    with pytest.raises(PathSyntaxError):
        field_stats(doc, "a[")
    for top in (-1, True, 1.5, stats.TOP_MAX + 1):
        with pytest.raises(UserInputError) as ei:
            field_stats(doc, "a", top=top)  # type: ignore[arg-type]
        assert ei.value.key == "stats.error.top"
    for sample in (0, -2, True, "10"):
        with pytest.raises(UserInputError) as ei:
            field_stats(doc, "a", sample=sample)  # type: ignore[arg-type]
        assert ei.value.key == "stats.error.sample"
    for limit in (0, -5, True, 1.5, "64MB"):
        with pytest.raises(UserInputError) as ei:
            field_stats(doc, "a", memory_limit=limit)  # type: ignore[arg-type]
        assert ei.value.key == "stats.error.memory_limit"
    with pytest.raises(ValueError):
        field_stats(doc, "a", strategy="fast")


# ----------------------------------------------------------------------------------
# edited and pinned documents
# ----------------------------------------------------------------------------------
EDIT_FIELDS = ["id", "name", "contact.address.city", "orders[*].sum", "tags[*]", "score", "..oid", "$", "esc",
               "nums[*]"]


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped", "wrapped_obj", "single"])
@pytest.mark.parametrize("scenario", ["point", "structure", "mixed", "keys"])
def test_edited_documents_equal_the_expected_file(tmp_path, kind, scenario) -> None:
    from overlay_helpers import make_edited

    doc, ob, exp = make_edited(tmp_path / f"{kind}-{scenario}", kind, scenario, n=30, seed=3)
    try:
        fields = EDIT_FIELDS + (["$key"] if doc.member_records else [])
        if kind == "single":
            fields = ["[*].id", "[*].orders[*].sum", "..oid", "$", "[*].nums[*]"]
        got = field_stats(doc, fields)
        want = field_stats(exp, fields)
        for g, w in zip(got, want):
            assert summary(g) == summary(w), g.field
            assert g.gen == doc.gen and g.gen != 0
        # and the reference on the expected content
        check(doc, fields, ref_doc=exp)
        # a scope of the edited state; a stale one is refused
        view = filter_records(doc, "exists(id) or exists(name)") if kind != "single" else None
        if view is not None:
            assert view.gen == doc.gen
            check(doc, fields, view, ref_doc=exp)
            ob.set(0, ob.records[0])
            doc.set_overlay(ob.state())
            with pytest.raises(EditError) as ei:
                field_stats(doc, fields, view)
            assert ei.value.key == "edits.error.stale_scope"
    finally:
        doc.close()
        exp.close()


def test_pinned_document_is_read(make_doc, tmp_path) -> None:
    from overlay_helpers import OverlayBuilder

    doc = make_doc(b'[{"a": 1}, {"a": 2}, {"a": 3}]')
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(1, b'{"a": 20}')
    doc.set_overlay(ob.state())
    pinned = doc.pinned()
    gen = pinned.gen
    ob.set(2, b'{"a": 30}')
    doc.set_overlay(ob.state())
    fs = field_stats(pinned, "a")[0]
    assert fs.gen == gen and fs.number.sum_text == "24"
    assert field_stats(doc, "a")[0].number.sum_text == "51"


# ----------------------------------------------------------------------------------
# cancellation, progress, changes
# ----------------------------------------------------------------------------------
def test_cancel_before_and_during(make_doc, monkeypatch) -> None:
    recs = stat_records(400, seed=71)
    data, kw = document_bytes(recs, "jsonl")
    doc = make_doc(data, **kw)
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(Cancelled):
        field_stats(doc, "k", cancel=cancel)
    monkeypatch.setattr(stats, "CHUNK_RECORDS", 7)
    for exact in (False, True):
        cancel = CancelToken()

        def on_progress(info: Any, cancel: CancelToken = cancel) -> None:
            if info.done > 0:
                cancel.cancel()

        with pytest.raises(Cancelled):
            field_stats(doc, FIELDS, exact=exact, memory_limit=1000, cancel=cancel,
                        progress=Progress(callback=on_progress, min_interval=0))
        assert no_temp_dirs()
    # cancelled in the final merge of the spilled runs
    cancel = CancelToken()
    msg = stats.t("stats.progress.finish", name=doc.source_path.name)

    def at_finish(info: Any) -> None:
        if info.message == msg:
            cancel.cancel()

    with pytest.raises(Cancelled):
        field_stats(doc, FIELDS, exact=True, memory_limit=1000, cancel=cancel,
                    progress=Progress(callback=at_finish, min_interval=0))
    assert no_temp_dirs()


def test_progress_in_bytes(make_doc) -> None:
    recs = stat_records(300, seed=72)
    data, kw = document_bytes(recs, "array")
    doc = make_doc(data, build=False, **kw)
    infos: list[Any] = []
    progress = Progress(callback=infos.append, min_interval=0)
    field_stats(doc, ["k", "arr[*]"], exact=True, memory_limit=2000, progress=progress)
    assert doc.index_complete
    assert progress.total == doc.data_size and progress.unit == "bytes" and progress.finished
    done = [i.done for i in infos]
    assert done == sorted(done) and done[-1] == doc.data_size and len(set(done)) > 5
    assert stats.t("stats.progress", name=doc.source_path.name) in {i.message for i in infos}


def test_file_changed(make_doc) -> None:
    doc = make_doc(b'[{"a": 1}, {"a": 2}]')
    p = doc.source_path
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    with pytest.raises(FileChangedError):
        field_stats(doc, "a")
    assert no_temp_dirs()


def test_partial_snapshots(make_doc, monkeypatch) -> None:
    recs = stat_records(300, seed=73)
    data, kw = document_bytes(recs, "jsonl")
    doc = make_doc(data, **kw)
    monkeypatch.setattr(stats, "CHUNK_RECORDS", 16)
    monkeypatch.setattr(stats, "PARTIAL_SECONDS", 0.0)
    monkeypatch.setattr(stats, "PARTIAL_COST_FACTOR", 0.0)
    snaps: list[list[FieldStats]] = []
    final = field_stats(doc, ["k", "arr[*]", "k"], on_partial=snaps.append)
    assert len(snaps) > 5
    for snap in snaps:
        assert [fs.field for fs in snap] == ["k", "arr[*]", "k"] and all(fs.partial for fs in snap)
        assert snap[0].number is None and not snap[0].distinct_exact
        assert snap[0].scanned <= final[0].scanned
    scanned = [snap[0].scanned for snap in snaps]
    assert scanned == sorted(scanned) and scanned[-1] > scanned[0]
    assert not final[0].partial
    # a failing callback does not stop the computation
    field_stats(doc, "k", on_partial=lambda s: 1 / 0)


# ----------------------------------------------------------------------------------
# texts and JSON
# ----------------------------------------------------------------------------------
def test_value_texts() -> None:
    vt = stats._value_text
    assert vt("Toshkent") == '"Toshkent"' and vt(None) == "null" and vt(True) == "true"
    assert vt(codec.loads(b"1e400")) == "1e400" and vt(codec.loads(b"1.10")) == "1.10"
    assert vt({"b": [1, "‮"], "a": None}) == '{"b":[1,"\\u202e"],"a":null}'
    long = vt(list(range(100)))
    assert len(long) == 120 and long.endswith("…")
    assert vt("x" * 200).endswith("…") and len(vt("x" * 200)) == 120


def test_format_and_json(make_doc, lang_uz) -> None:
    doc = make_doc(b'[{"c": "Toshkent", "n": 1.5, "t": ["a", "b"]}, {"c": "Xiva", "n": null, "t": ["a"]},'
                   b' {"c": "Toshkent"}]')
    res = field_stats(doc, ["c", "n", "t[*]"])
    text = stats.format_stats(res)
    assert "Maydon: c" in text and "Noyob qiymatlar: 2 (aniq)" in text and "Eng ko'p uchragan 2 ta qiymat:" in text
    assert "yo'q: 1 (33,3%)" in text and '"Toshkent"' in text and "(2 ta yozuvda)" in text
    assert "Sonlar: 1 ta (butun: 0)" in text
    from jsonreshape.i18n import set_language

    set_language("en")
    text = res[0].format_text()
    assert "Field: c" in text and "Distinct values: 2 (exact)" in text and "The 2 most frequent values:" in text
    text = stats.format_stats(res[2:])
    assert "(in 2 records)" in text and "(in 1 record)" in text and "Field: t[*]    3 records" in text
    one = field_stats(doc, "c", [0], top=1)[0].format_text()
    assert "Field: c    1 record ·" in one and "The most frequent value:" in one
    d = res[1].to_dict()
    json.dumps(d)
    assert d["number"]["sum"] == "1.5" and d["types"] == {"number": 1, "null": 1}
    assert d["top"][0]["json"] == "1.5" and d["path"] == ["n"]
    assert res[2].to_dict()["path"] == ["t", "[*]"]


def test_messages_exist_in_both_languages() -> None:
    keys = ["stats.progress", "stats.progress.finish", "stats.error.fields", "stats.error.no_fields",
            "stats.error.field", "stats.error.top", "stats.error.sample", "stats.error.memory_limit",
            "stats.warn.approximate", "stats.warn.sum_approximate"]
    for w in ("no_record_keys", "field_absent", "too_large", "unreadable", "uncounted"):
        keys += [f"stats.warn.{w}", f"stats.warn.{w}.one"]
    for key in keys:
        assert has(key), key


# ----------------------------------------------------------------------------------
# bigger data
# ----------------------------------------------------------------------------------
@pytest.mark.slow
def test_many_records_exact_and_spilled_agree(make_doc) -> None:
    rng = random.Random(81)
    lines = [b'{"id": %d, "c": "%s", "v": [%d, %d.5]}' % (i, rng.choice(["Toshkent", "Xiva", "Nukus"]).encode(),
                                                            rng.randrange(5000), rng.randrange(100))
             for i in range(60000)]
    doc = make_doc(b"\n".join(lines) + b"\n", format="jsonl")
    a = field_stats(doc, ["id", "c", "v[*]"])
    b = field_stats(doc, ["id", "c", "v[*]"], exact=True, memory_limit=200_000)
    for x, y in zip(a, b):
        assert summary(x) == summary(y)
    assert a[0].distinct == 60000 and a[1].distinct == 3
    assert no_temp_dirs()


@pytest.mark.slow
def test_performance_smoke_on_bench_data(capsys) -> None:
    """A smoke on the benchmark files when present (MB/s reported, not asserted tightly)."""
    import time

    root = Path(__file__).resolve().parents[1] / "bench" / "data"
    path = root / "syn_2g.json"
    if not path.exists():
        path = root / "_smoke.json"
    if not path.exists():
        pytest.skip("no benchmark data (bench/data)")
    doc = Document.open(path)
    try:
        doc.build_index()
        t0 = time.perf_counter()
        fs = field_stats(doc, ["contact.address.city"])[0]
        dt = time.perf_counter() - t0
        rate = doc.data_size / dt / 1e6
        with capsys.disabled():
            print(f"\nstats contact.address.city on {path.name}: {dt:.2f} s, {rate:.0f} MB/s, distinct {fs.distinct}")
        assert fs.distinct_exact and fs.scanned == doc.record_count
    finally:
        doc.close()
