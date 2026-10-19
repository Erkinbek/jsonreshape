"""jsonreshape.dupes: the duplicate finder (docs/ARCHITECTURE.md §12.6, ``dupes.py``).

* canonical equality: numbers by exact decimal value (any spelling, any size), strings
  exactly, literals distinct, objects without member order, arrays ordered;
* :func:`find_duplicates` against a naive reference (every record's value grouped by a
  tagged ``Decimal`` model) for all 7 document kinds, both core backends and both
  strategies, random records from ``tests/jsongen.py`` with forced duplicates in many
  spellings; ``keep="first"/"last"``; missing keys and ``null``; ``$key`` on keyed and
  wrapped-object documents (and on documents without record keys);
* sources (RecordSet subsets, unordered sets, repeated positions, ranges), the spill path
  (tiny memory limit, intermediate merges), huge records (streamed keys, keys over the
  capture budget), encodings (BOM, UTF-16, gzip, CRLF), cancellation and clean-up,
  progress, file changes, argument errors, ``gen`` / stale scopes, examples.
"""

from __future__ import annotations

import os
import random
from array import array
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pytest

import jsongen
from jsonreshape import codec, dupes, reader
from jsonreshape.appdirs import recordsets_dir, work_dir
from jsonreshape.codec import JsonFloat, JsonInt
from jsonreshape.dupes import (
    EXAMPLE_GROUPS,
    EXAMPLE_POSITIONS,
    EXAMPLE_TEXT_CHARS,
    DuplicateReport,
    canonical_bytes,
    canonical_token,
    find_duplicates,
)
from jsonreshape.errors import (
    Cancelled,
    EditError,
    FileChangedError,
    JsonReshapeError,
    PathSyntaxError,
    UserInputError,
)
from jsonreshape.i18n import has, t
from jsonreshape.paths import MISSING, get as path_get, parse_path
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet, filter_records, sort_records
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend, monkeypatch):
    """Documents use this core backend's Scanner and the "extract" strategy its
    PathExtractor (codec.loads follows the process-wide backend)."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    monkeypatch.setattr(dupes, "_EXTRACTOR", core_backend.PathExtractor)
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


def norm(v: Any) -> Any:
    """Independent model of canonical JSON equality: tagged, hashable, exact."""
    if v is None:
        return ("null",)
    if v is True or v is False:
        return ("bool", v)
    if isinstance(v, str):
        return ("str", str.__str__(v))
    if isinstance(v, (JsonFloat, JsonInt)):
        return ("num", Decimal(v.raw))
    if isinstance(v, int):
        return ("num", Decimal(int(v)))
    if isinstance(v, float):
        return ("num", Decimal(repr(float(v))))
    if isinstance(v, dict):
        return ("obj", frozenset((k, norm(x)) for k, x in v.items()))
    if isinstance(v, list):
        return ("arr", tuple(norm(x) for x in v))
    raise TypeError(type(v))


def text_of(value: Any) -> str:
    text = codec.dumps(value).decode("utf-8")
    if len(text) > EXAMPLE_TEXT_CHARS:
        text = text[:EXAMPLE_TEXT_CHARS - 1] + "…"
    return text


def reference(values: list[Any], positions: Iterable[int] | None, keep: str) -> dict[str, Any]:
    """The expected report: ``values[pos]`` is the key value of record ``pos`` (MISSING
    when absent); ``positions`` the source (None = all; may repeat / be unordered)."""
    uniq = range(len(values)) if positions is None else sorted(set(int(p) for p in positions))
    groups: dict[Any, list[int]] = {}
    missing = 0
    for pos in uniq:
        v = values[pos]
        if v is MISSING:
            missing += 1
            continue
        groups.setdefault(norm(v), []).append(pos)
    dup_groups = [g for g in groups.values() if len(g) > 1]
    dups = sorted(p for g in dup_groups for p in (g[1:] if keep == "first" else g[:-1]))
    first = sorted(dup_groups, key=lambda g: g[0])[:EXAMPLE_GROUPS]
    return {
        "groups": len(dup_groups),
        "duplicates": len(dups),
        "records": dups,
        "records_with_key": sum(len(g) for g in groups.values()),
        "missing": missing,
        "examples": [(text_of(values[g[0]]), g[:EXAMPLE_POSITIONS]) for g in first],
        "example_sizes": [len(g) for g in first],
    }


def check_report(rep: DuplicateReport, want: dict[str, Any], keep: str, doc: Document | None = None) -> None:
    """``rep`` equals the reference. With ``doc``: the key text of an example whose first
    record is huge comes from ijson, which spells numbers its own way (``1e0`` -> ``1``,
    ``0e5`` -> ``0E+5``; module docstring of jsonreshape.stream): compared by value."""
    examples = rep.examples
    if doc is not None:
        examples = []
        for (text, positions), (want_text, _) in zip(rep.examples, want["examples"]):
            if text != want_text and doc.is_huge(positions[0]):
                assert norm(codec.loads(text)) == norm(codec.loads(want_text)), (text, want_text)
                text = want_text
            examples.append((text, positions))
    got = {
        "groups": rep.groups,
        "duplicates": rep.duplicates,
        "records": list(rep.records),
        "records_with_key": rep.records_with_key,
        "missing": rep.missing,
        "examples": examples,
        "example_sizes": rep.example_sizes,
    }
    assert got == want
    assert rep.keep == keep
    assert len(rep.records) == rep.duplicates
    assert rep.records.complete and rep.records.kind == "duplicates"
    assert rep.scanned == rep.records_with_key + rep.missing + rep.skipped


def path_values(recs: list[Any], key: str) -> list[Any]:
    path = parse_path(key) if key not in ("$",) else ()
    return [path_get(r, path) for r in recs]


# values that must group across spellings (and some that must not)
POOL = [
    1, JsonFloat("1.0"), JsonFloat("1e0"), JsonFloat("10E-1"), JsonFloat("0.1e1"), JsonFloat("1.000"),
    JsonFloat("1.10"), 1.1, JsonFloat("1.100"), JsonFloat("11e-1"),
    JsonFloat("1e400"), JsonFloat("1E+400"), JsonFloat("10e399"), JsonFloat("2e400"),
    JsonInt("12345678901234567890"), JsonFloat("12345678901234567890.0"), JsonFloat("1.234567890123456789e19"),
    JsonInt("12345678901234567891"),
    JsonInt("-0"), -0.0, 0, JsonFloat("0e5"), JsonFloat("0.000"),
    0.1, JsonFloat("0.10"), JsonFloat("0.10000000000000001"), JsonFloat("1e-1"),
    -1, JsonFloat("-1.0"), 1000000, JsonFloat("1e6"),
    "Toshkent", "toshkent", "TOSHKENT", "Тошкент", "O'zbekiston", "Oʻzbekiston", "O‘zbekiston", "O’zbekiston",
    "1", "1.0", "", "null", "true", "emoji 😀", 'quote"in', "back\\slash", "slash/in", "nul\u0000x",
    "line sep", "tab\there",
    True, False, None,
    {"a": 1, "b": [1, 2]}, {"b": [1, JsonFloat("2.0")], "a": JsonFloat("1e0")}, {"a": 1, "b": [2, 1]},
    [1, 2], [2, 1], [JsonFloat("1.0"), JsonFloat("2e0")], [], {}, [[]], [{}],
    {"ism": "Ali", "shahar": {"nomi": "Toshkent", "kod": 100}},
    {"shahar": {"kod": JsonFloat("1e2"), "nomi": "Toshkent"}, "ism": "Ali"},
    {"shahar": {"kod": 100, "nomi": "Тошкент"}, "ism": "Ali"},
]


def dup_records(n: int, seed: int, field: str = "k") -> list[dict[str, Any]]:
    """Realistic records with a key field drawn from POOL (forced duplicates), random
    values and missing fields; the key field is sometimes the first member."""
    rng = random.Random(seed)
    recs = []
    for i in range(n):
        rec = jsongen.rand_record(rng, i)
        r = rng.random()
        if r < 0.8:
            value: Any = rng.choice(POOL)
        elif r < 0.9:
            value = jsongen.rand_value(rng, 1, 3)
        else:
            value = MISSING
        if value is not MISSING:
            if rng.random() < 0.3:
                rec = {field: value, **rec}
            else:
                rec[field] = value
        recs.append(rec)
    return recs


def no_temp_dirs() -> bool:
    return not any(name.startswith("sort-") for name in os.listdir(work_dir()))


# ----------------------------------------------------------------------------------
# canonical form
# ----------------------------------------------------------------------------------
EQUAL_NUMBERS = [
    ("1", "1.0"), ("1", "1e0"), ("1", "10e-1"), ("1", "0.1e1"), ("1", "1.000"), ("100", "1e2"), ("100", "1E+2"),
    ("-0", "0"), ("-0.0", "0"), ("0e10", "0"), ("-0e-10", "0.000"), ("1.10", "1.1"), ("1.1", "11e-1"),
    ("12345678901234567890", "1.2345678901234567890e19"), ("12345678901234567890", "12345678901234567890.000"),
    ("1e400", "1E+400"), ("1e400", "10e399"), ("-1e400", "-10E399"), ("5e-324", "0.5e-323"),
    ("0.00001", "1e-5"), ("123456789.123456789", "123456789123456789e-9"),
    ("1" + "0" * 5000, "1e5000"),
]
UNEQUAL_NUMBERS = [
    ("1e400", "2e400"), ("1", "1.0000000000000000000001"), ("12345678901234567890", "12345678901234567891"),
    ("0.1", "0.10000000000000001"), ("-1", "1"), ("5e-324", "0"), ("1e400", "1e401"), ("1e-400", "0"),
    ("9007199254740993", "9007199254740992"), ("1" + "0" * 5000, "1e4999"),
]


@pytest.mark.parametrize("a, b", EQUAL_NUMBERS)
def test_equal_numbers_have_equal_canonical_bytes(a: str, b: str) -> None:
    ca, cb = canonical_token(a.encode()), canonical_token(b.encode())
    assert ca == cb
    assert canonical_bytes(codec.loads(a)) == ca
    assert canonical_bytes(codec.loads(b)) == cb


@pytest.mark.parametrize("a, b", UNEQUAL_NUMBERS)
def test_unequal_numbers_differ(a: str, b: str) -> None:
    assert canonical_token(a.encode()) != canonical_token(b.encode())
    assert canonical_bytes(codec.loads(a)) != canonical_bytes(codec.loads(b))


def test_huge_exponent_texts_are_exact() -> None:
    e = "9" * 5000                      # an exponent beyond Python's int() digit limit
    e_minus_1 = "9" * 4999 + "8"
    a = ("1e" + e).encode()
    b = ("10e" + e_minus_1).encode()
    c = ("2e" + e).encode()
    assert canonical_token(a) == canonical_token(b) != canonical_token(c)
    assert canonical_token(("-1e-" + e).encode()) == canonical_token(("-0.1e-" + e_minus_1).encode())


def test_every_edge_number_token_round_trips() -> None:
    for tok in jsongen.EDGE_NUMBER_TOKENS:
        assert canonical_bytes(codec.loads(tok)) == canonical_token(tok.encode()), tok


def test_types_are_distinct() -> None:
    tokens = [b"1", b"true", b'"1"', b"null", b'"null"', b"[]", b"{}", b'""', b"0", b"false", b'"true"',
              b"[1]", b'{"1": 1}', b'["1"]', b"[null]", b'{"":null}', b'[[]]', b"[{}]", b'"[]"']
    canon = [canonical_token(x) for x in tokens]
    assert len(set(canon)) == len(canon)


def test_strings_are_exact() -> None:
    same = [
        (b'"A"', b'"\\u0041"'), (b'"\xd1\x91"', b'"\\u0451"'), (b'"a/b"', b'"a\\/b"'),
        ('"😀"'.encode(), b'"\\ud83d\\ude00"'), (b'"tab\\t"', b'"tab\\u0009"'), (b'"\\ud800"', b'"\\uD800"'),
    ]
    for a, b in same:
        assert canonical_token(a) == canonical_token(b), (a, b)
        assert canonical_token(a) == canonical_bytes(codec.loads(a))
    different = ["Toshkent", "toshkent", "Тошкент", "O'zbekiston", "Oʻzbekiston", "O‘zbekiston", "Straße",
                 "STRASSE", "é", "é", "", " ", "?", "\ud800", "\udc00", "𐀀"]
    canon = [canonical_bytes(s) for s in different]
    assert len(set(canon)) == len(canon)
    # a lone surrogate escape survives exactly (surrogatepass)
    assert canonical_token(b'"\\ud800"') == canonical_bytes("\ud800") != canonical_bytes("?")


def test_containers_canonical() -> None:
    equal = [
        (b'{"a": 1, "b": [1, 2]}', b'{"b":[1,2.0],"a":1e0}'),
        (b'{"a": {"x": null, "y": true}}', b'{ "a" : { "y" : true , "x" : null } }'),
        (b'{"a": 1, "a": 2}', b'{"a": 2}'),                      # dict semantics: the last wins
        (b'{"\\u0041": 1}', b'{"A": 1.0}'),
        (b'[{"b": 1, "a": 2}]', b'[{"a": 2, "b": 1}]'),
        (b"[]", b"[ ]"), (b"{}", b"{ }"),
    ]
    for a, b in equal:
        assert canonical_token(a) == canonical_token(b), (a, b)
        assert canonical_bytes(codec.loads(a)) == canonical_token(a)
    unequal = [(b"[1, 2]", b"[2, 1]"), (b'{"a": 1}', b'{"a": 1, "b": 1}'), (b'{"a": [1]}', b'{"a": 1}'),
               (b'{"a": "b"}', b'{"b": "a"}'), (b'[["a"], "b"]', b'["a", ["b"]]'), (b'{"ab": "c"}', b'{"a": "bc"}'),
               (b'["a", "b"]', b'["ab"]'), (b'[1, 2]', b'[12]'), (b'{"a": {}}', b'{"a": []}')]
    for a, b in unequal:
        assert canonical_token(a) != canonical_token(b), (a, b)


def test_random_values_token_and_value_paths_agree() -> None:
    rng = random.Random(7)
    for i in range(400):
        v = jsongen.rand_value(rng, 0, 4)
        want = canonical_bytes(v)
        for style in ("compact", "pretty2", "messy", "escaped", "messy_escaped"):
            assert canonical_token(jsongen.encode(v, style, seed=i)) == want


def test_deep_values_are_canonical_without_recursion() -> None:
    v: Any = 1
    w: Any = JsonFloat("1.0")
    for _ in range(100000):
        v = [v]
        w = [w]
    assert canonical_bytes(v) == canonical_bytes(w)
    o: Any = None
    for i in range(50000):
        o = {"k": o, "i": i % 3}
    assert canonical_bytes(o).startswith(b"{k1:i")


def test_invalid_tokens_raise() -> None:
    for bad in (b"", b"tru", b"01", b"1.", b".5", b"+1", b'"abc', b"   ", b"NaN", b"1e"):
        with pytest.raises((ValueError, codec.JsonDecodeError)):
            canonical_token(bad)
    with pytest.raises(TypeError):
        canonical_bytes(object())
    with pytest.raises(ValueError):
        canonical_bytes(float("nan"))


# ----------------------------------------------------------------------------------
# find_duplicates against the reference: every kind, both backends, both strategies
# ----------------------------------------------------------------------------------
KIND_CASES = [
    ("array", {}, {}),
    ("array", {"style": "pretty2", "newline": "\r\n"}, {}),
    ("array", {"style": "messy_escaped"}, {"format": "array"}),
    ("jsonl", {}, {"format": "jsonl"}),
    ("jsonl", {"style": "escaped", "newline": "\r\n"}, {"format": "jsonl"}),
    ("stream", {"style": "pretty4"}, {"format": "stream"}),
    ("brace_list", {"style": "messy"}, {"format": "brace_list"}),
    ("wrapped", {"style": "tab"}, {"record_path": "data"}),
    ("wrapped", {"wrapper_path": ("result", "items"), "style": "escaped"}, {"record_path": "result.items"}),
    ("keyed", {"style": "pretty2"}, {"format": "keyed"}),
]


@pytest.mark.parametrize("strategy", ["extract", "parse"])
@pytest.mark.parametrize("fmt, write_kw, open_kw", KIND_CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(KIND_CASES)])
def test_matches_reference(make_doc, backend, strategy, fmt, write_kw, open_kw) -> None:
    recs = dup_records(260, seed=sum(map(ord, fmt)) + len(write_kw))
    doc = make_doc(recs, fmt, **write_kw, **open_kw)
    assert doc.record_count == len(recs)
    values = path_values(recs, "k")
    parsed = [path_get(doc.record(i), ("k",)) for i in range(doc.record_count)]   # every record parsed
    for keep in ("first", "last"):
        want = reference(values, None, keep)
        assert reference(parsed, None, keep) == want          # the file holds what was generated
        rep = find_duplicates(doc, "k", keep=keep, strategy=strategy)
        check_report(rep, want, keep)
        assert rep.skipped == 0 and rep.runs == 0 and not rep.warnings
        rep.close()


@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_single_document(make_doc, backend, strategy) -> None:
    recs = dup_records(20, seed=3)
    doc = make_doc(recs, "single", format="single")
    assert doc.record_count == 1
    rep = find_duplicates(doc, "$", strategy=strategy)
    assert (rep.groups, rep.duplicates, rep.records_with_key, rep.missing) == (0, 0, 1, 0)
    rep = find_duplicates(doc, "0.k", strategy=strategy)          # a path into the one record
    assert rep.records_with_key == (0 if recs[0].get("k", MISSING) is MISSING else 1)
    rep = find_duplicates(doc, "k", strategy=strategy)            # "k" on an array: missing
    assert (rep.records_with_key, rep.missing) == (0, 1)
    assert rep.warnings == ("dupes.warn.key_absent",)


KEY_PATHS = ["contact.address.city", "contact.address", "orders[0].sum", "orders.0.oid", "tags[1]", "active",
             "note", "$", "extra", "score", '["k"]', "/k"]


@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_key_paths_match_reference(make_doc, backend, strategy) -> None:
    recs = dup_records(300, seed=11)
    doc = make_doc(recs, "jsonl", format="jsonl")
    for key in KEY_PATHS:
        values = path_values(recs, key if key not in ('["k"]', "/k") else "k")
        rep = find_duplicates(doc, key, strategy=strategy)
        check_report(rep, reference(values, None, "first"), "first")


def test_whole_record_key_and_exact_duplicates(make_doc) -> None:
    lines = [b'{"a": 1, "b": "x"}', b'{"b":"x","a":1.0}', b'{"a": 1, "b": "X"}', b"[1, 2]", b"[1, 2.00]",
             b"[2, 1]", b'"s"', b'"\\u0073"', b"null", b"null", b"1e2", b"100"]
    doc = make_doc(b"\n".join(lines) + b"\n", format="jsonl")
    rep = find_duplicates(doc, "$")
    assert rep.groups == 5 and list(rep.records) == [1, 4, 7, 9, 11]
    assert rep.examples == [('{"a":1,"b":"x"}', [0, 1]), ("[1,2]", [3, 4]), ('"s"', [6, 7]), ("null", [8, 9]),
                            ("1e2", [10, 11])]


def test_keep_last_and_missing_and_null(make_doc) -> None:
    data = (b'[{"k": 1}, {"x": 1}, {"k": null}, {"k": 1.0}, {"k": null}, {}, {"k": "1"}, {"k": 1e0}, '
            b'{"k": {"a": null}}, {"k": [null]}, {"k": null}]')
    doc = make_doc(data)
    first = find_duplicates(doc, "k")
    assert (first.groups, first.duplicates, first.records_with_key, first.missing) == (2, 4, 9, 2)
    assert list(first.records) == [3, 4, 7, 10]
    assert first.examples == [("1", [0, 3, 7]), ("null", [2, 4, 10])]
    assert first.example_sizes == [3, 3]
    last = find_duplicates(doc, "k", keep="LAST ")
    assert last.keep == "last" and list(last.records) == [0, 2, 3, 4]
    assert last.examples == first.examples
    # a null intermediate is not an object: the key below it is missing
    rep = find_duplicates(doc, "k.a")
    assert (rep.records_with_key, rep.missing, rep.groups) == (1, 10, 0)


def test_duplicate_member_keys_use_dict_semantics(make_doc, backend) -> None:
    data = b'[{"k": 1, "k": 2}, {"k": 2}, {"k": 1}, {"k": [1], "k": {"z": 1, "z": 1.0}}, {"k": {"z": 1e0}}]'
    doc = make_doc(data)
    for strategy in ("extract", "parse"):
        rep = find_duplicates(doc, "k", strategy=strategy)
        assert list(rep.records) == [1, 4], strategy
        assert rep.examples == [("2", [0, 1]), ('{"z":1.0}', [3, 4])]


# ----------------------------------------------------------------------------------
# $key
# ----------------------------------------------------------------------------------
def test_dollar_key_on_keyed_documents(make_doc, backend) -> None:
    rng = random.Random(5)
    names = ["a", "b", "Тошкент", "O'zbek", "Oʻzbek", "id", "ID", "x y", ""]
    keys = [rng.choice(names) for _ in range(150)]
    recs = [{"n": i, "k": rng.choice(POOL[:12])} for i in range(150)]
    doc = make_doc(recs, "keyed", format="keyed", keys=keys, style="pretty2")
    assert doc.member_records and doc.record_count == 150
    for keep in ("first", "last"):
        rep = find_duplicates(doc, "$key", keep=keep)
        check_report(rep, reference(keys, None, keep), keep)
        # paths apply to the member value
        rep = find_duplicates(doc, "k", keep=keep)
        check_report(rep, reference([r["k"] for r in recs], None, keep), keep)


def test_dollar_key_escaped_keys_and_wrapped_objects(make_doc, backend) -> None:
    data = (b'{"meta": {"v": 1}, "data": {"a": {"x": 1}, "\\u0061": {"x": 2}, "b": {"x": 1.0}, '
            b'"\\u0442\\u043e\\u0448": {"x": 3}, "\xd1\x82\xd0\xbe\xd1\x88": {"x": 4}, "c": {"y": 1}}, "tail": 2}')
    doc = make_doc(data, record_path="data")
    assert doc.kind == "wrapped" and doc.member_records and doc.record_count == 6
    rep = find_duplicates(doc, "$key")
    assert list(rep.records) == [1, 4]
    assert rep.examples == [('"a"', [0, 1]), ('"тош"', [3, 4])]
    rep = find_duplicates(doc, "x")
    assert list(rep.records) == [2] and rep.missing == 1
    assert rep.examples == [("1", [0, 2])]


def test_dollar_key_without_record_keys_reads_nothing(make_doc, monkeypatch, lang_en) -> None:
    doc = make_doc(b'[{"a": 1}, {"a": 1}, {"a": 1}]')

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no record data may be read")

    monkeypatch.setattr(Document, "iter_blocks", boom)
    rep = find_duplicates(doc, " $key ")
    assert (rep.groups, rep.duplicates, rep.records_with_key, rep.missing) == (0, 0, 0, 3)
    assert rep.warnings == ("dupes.warn.no_record_keys",)
    assert "3 records were counted as having no key" in rep.warning_texts()[0]
    rep = find_duplicates(doc, "$key", [2, 0, 2])
    assert rep.missing == 2 and rep.scanned == 2
    assert "1 record was counted" in find_duplicates(doc, "$key", [1]).warning_texts()[0]
    with pytest.raises(IndexError):
        find_duplicates(doc, "$key", [5])


# ----------------------------------------------------------------------------------
# sources
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_sources(make_doc, backend, strategy) -> None:
    recs = dup_records(400, seed=21)
    doc = make_doc(recs, "array", style="pretty2")
    values = path_values(recs, "k")
    rng = random.Random(1)
    subset = sorted(rng.sample(range(400), 150))
    shuffled = rng.sample(range(400), 200)
    repeated = [rng.randrange(400) for _ in range(300)]
    filtered = filter_records(doc, "active == true")
    by_name = sort_records(doc, ["-name"])
    sources: list[tuple[Any, list[int] | None]] = [
        (None, None),
        (RecordSet.base(doc), None),
        (range(50, 300), list(range(50, 300))),
        (range(399, -1, -2), list(range(399, -1, -2))),
        (subset, subset),
        (array("Q", shuffled), shuffled),
        (repeated, repeated),
        (tuple(repeated), repeated),
        (iter(repeated), repeated),
        (RecordSet.from_indices(shuffled), shuffled),
        (filtered, list(filtered)),
        (by_name, list(by_name)),
        ([], []),
    ]
    for src, positions in sources:
        for keep in ("first", "last"):
            if hasattr(src, "__next__"):
                src = iter(repeated)
            rep = find_duplicates(doc, "k", src, keep=keep, strategy=strategy)
            check_report(rep, reference(values, positions, keep), keep)
            assert rep.records.is_ascending() or len(rep.records) <= 1
    with pytest.raises(IndexError):
        find_duplicates(doc, "k", [3, 400], strategy=strategy)
    assert no_temp_dirs()


def test_source_order_does_not_change_keep(make_doc) -> None:
    doc = make_doc(b'[{"k": 1}, {"k": 2}, {"k": 1}, {"k": 2}, {"k": 1}]')
    rep = find_duplicates(doc, "k", [4, 3, 2, 1, 0])
    assert list(rep.records) == [2, 3, 4]               # the smallest position is kept
    rep = find_duplicates(doc, "k", [4, 3, 2, 1, 0], keep="last")
    assert list(rep.records) == [0, 1, 2]


# ----------------------------------------------------------------------------------
# the spill path
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["extract", "parse"])
def test_spill_path_equals_in_memory(make_doc, backend, strategy, monkeypatch) -> None:
    recs = dup_records(700, seed=31)
    doc = make_doc(recs, "jsonl", format="jsonl")
    values = path_values(recs, "k")
    in_memory = {keep: find_duplicates(doc, "k", keep=keep, strategy=strategy) for keep in ("first", "last")}
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 2)
    monkeypatch.setattr(dupes, "MAX_FANIN", 3)
    monkeypatch.setattr(dupes, "CHECK_RECORDS", 7)
    monkeypatch.setattr(dupes, "MERGE_BATCH", 50)
    for keep in ("first", "last"):
        rep = find_duplicates(doc, "k", keep=keep, memory_limit=dupes.ENTRY_BYTES * 9, strategy=strategy,
                              block_bytes=2000)
        check_report(rep, reference(values, None, keep), keep)
        assert rep.runs > 20                     # many runs, merged in passes (fan-in 3)
        assert list(rep.records) == list(in_memory[keep].records)
        assert rep.examples == in_memory[keep].examples
        assert no_temp_dirs()


def test_spill_with_a_source_and_huge_records(make_doc, monkeypatch) -> None:
    recs = dup_records(300, seed=41)
    doc = make_doc(recs, "array", settings=Settings(large_node_bytes=150))
    values = path_values(recs, "k")
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 3)
    src = list(range(299, -1, -1)) + [5, 6, 7]
    rep = find_duplicates(doc, "k", src, keep="last", memory_limit=1)
    check_report(rep, reference(values, src, "last"), "last", doc)
    assert rep.runs > 10 and no_temp_dirs()


def test_memory_limit_default_and_cap(make_doc, monkeypatch) -> None:
    doc = make_doc(b'[{"k": 1}, {"k": 1}]')
    seen: list[int] = []
    real = dupes._KeyReader.__init__

    def spy(self, *args: Any, **kw: Any) -> None:
        real(self, *args, **kw)
        seen.append(self.max_entries)

    monkeypatch.setattr(dupes._KeyReader, "__init__", spy)
    total = doc.settings.memory_limit_bytes
    find_duplicates(doc, "k")
    find_duplicates(doc, "k", memory_limit=10 * total)
    find_duplicates(doc, "k", memory_limit=dupes.ENTRY_BYTES * 10000)
    assert seen == [total // 4 // dupes.ENTRY_BYTES, total // 2 // dupes.ENTRY_BYTES, 10000]


# ----------------------------------------------------------------------------------
# huge records
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, open_kw", [("array", {}), ("jsonl", {"format": "jsonl"}),
                                          ("keyed", {"format": "keyed"}), ("stream", {"format": "stream"})])
def test_huge_records_are_streamed(make_doc, backend, fmt, open_kw) -> None:
    recs = dup_records(200, seed=51)
    big = make_doc(recs, fmt, **open_kw)
    small = make_doc(recs, fmt, settings=Settings(large_node_bytes=120), **open_kw)
    huge = sum(small.is_huge(i) for i in range(small.record_count))
    assert 0 < huge < small.record_count
    values = path_values(recs, "k")
    for keep in ("first", "last"):
        want = reference(values, None, keep)
        for doc in (big, small):
            rep = find_duplicates(doc, "k", keep=keep)
            check_report(rep, want, keep, doc)
            assert rep.skipped == 0
    if fmt == "keyed":
        keys = [f"id{i}" for i in range(len(recs))]
        rep = find_duplicates(small, "$key")
        check_report(rep, reference(keys, None, "first"), "first", small)


def test_huge_record_with_a_key_over_the_capture_budget_is_skipped(make_doc, lang_en) -> None:
    long = "x" * (1_200_000)
    lines = [b'{"k": "a"}', b'{"k": "' + long.encode() + b'", "n": 1}', b'{"k": "a"}',
             b'{"k": "' + long.encode() + b'", "n": 2}', b'{"n": 3}']
    doc = make_doc(b"\n".join(lines), format="jsonl", settings=Settings(large_node_bytes=1000))
    assert doc.is_huge(1) and doc.is_huge(3)
    rep = find_duplicates(doc, "k")
    assert (rep.groups, rep.duplicates, rep.records_with_key, rep.missing, rep.skipped) == (1, 1, 2, 1, 2)
    assert list(rep.records) == [2]
    assert rep.warnings == ("dupes.warn.too_large",)
    text = rep.warning_texts()[0]
    assert "2 records (the first is #1)" in text and "1.0 MB" in text
    # the same records read in memory compare fine
    plain = make_doc(b"\n".join(lines), format="jsonl")
    rep = find_duplicates(plain, "k")
    assert list(rep.records) == [2, 3] and rep.skipped == 0
    assert rep.examples[1] == (text_of(long), [1, 3])
    assert len(rep.examples[1][0]) == EXAMPLE_TEXT_CHARS and rep.examples[1][0].endswith("…")


def test_undecodable_keys_are_streamed_then_skipped(make_doc, monkeypatch, lang_en) -> None:
    deep = b"[" * 3000 + b"1" + b"]" * 3000
    deep_f = b"[" * 3000 + b"1.0" + b"]" * 3000
    lines = [b'{"k": ' + deep + b"}", b'{"k": ' + deep_f + b"}", b'{"k": [1]}', b'{"k": [1.0]}', b'{"k": "x"}']
    doc = make_doc(b"\n".join(lines) + b"\n", format="jsonl")
    for strategy in ("extract", "parse"):
        rep = find_duplicates(doc, "k", strategy=strategy)
        assert list(rep.records) == [1, 3] and rep.skipped == 0, strategy
        assert rep.examples[0][0].startswith("[[[[") and rep.examples[1] == ("[1]", [2, 3])
    # values the decoder refuses (e.g. nested deeper than it allows) are streamed instead
    real_loads = codec.loads
    refused: list[int] = []

    def picky(data: Any) -> Any:
        raw = data.encode() if isinstance(data, str) else bytes(data)
        if raw.count(b"[") >= 1000:
            refused.append(len(raw))
            raise codec.JsonDecodeError("too deep for this test")
        return real_loads(data)

    monkeypatch.setattr(codec, "loads", picky)
    for strategy in ("extract", "parse"):
        refused.clear()
        rep = find_duplicates(doc, "k", strategy=strategy)
        assert refused, strategy                               # the decoder was really refused
        assert list(rep.records) == [1, 3] and rep.skipped == 0, strategy
        assert rep.examples[0][0].startswith("[[[[") and rep.examples[1] == ("[1]", [2, 3])
    # a record that can be neither decoded nor streamed is skipped (never marked)
    from jsonreshape import stream

    real = stream._value_events

    def broken(doc_: Any, span: Any, member: bool) -> Any:
        if span == doc_.record_span(1):
            raise JsonReshapeError("stream.error.parse", detail="test")
        return real(doc_, span, member)

    monkeypatch.setattr(stream, "_value_events", broken)
    for strategy in ("extract", "parse"):
        rep = find_duplicates(doc, "k", strategy=strategy)
        assert list(rep.records) == [3] and rep.skipped == 1 and rep.records_with_key == 4, strategy
        assert rep.warnings == ("dupes.warn.unreadable",)
        assert "1 record (#1) could not be read" in rep.warning_texts()[0]
    # other errors while streaming are not a property of the record: they propagate
    def changed(doc_: Any, span: Any, member: bool) -> Any:
        raise FileChangedError(path="x")

    monkeypatch.setattr(stream, "_value_events", changed)
    with pytest.raises(FileChangedError):
        find_duplicates(doc, "k")
    assert no_temp_dirs()


def test_extractor_refusals_fall_back_to_single_records(make_doc, backend, core_backend, monkeypatch) -> None:
    recs = dup_records(120, seed=13)
    doc = make_doc(recs, "array", style="pretty2")
    bad = doc.record_span(7)
    calls = {"many": 0, "refused": 0}

    class Picky:
        """An extractor that refuses calls with several records and record #7 itself
        (as if their bytes were invalid JSON): the finder retries record by record and
        streams the refused record."""

        def __init__(self, paths: Any, member_spans: bool = False) -> None:
            self.inner = core_backend.PathExtractor(paths, member_spans=member_spans)

        def extract(self, buf: bytes, spans: bytes) -> bytes:
            pairs = array("Q")
            pairs.frombytes(spans)
            if len(pairs) > 2:
                calls["many"] += 1
                raise ValueError("refused (test)")
            if pairs[1] - pairs[0] == bad[1] - bad[0] and buf[pairs[0]:pairs[1]] == doc.record_bytes(7):
                calls["refused"] += 1
                raise ValueError("refused (test)")
            return self.inner.extract(buf, spans)

    monkeypatch.setattr(dupes, "_EXTRACTOR", Picky)
    rep = find_duplicates(doc, "k", strategy="extract")
    check_report(rep, reference(path_values(recs, "k"), None, "first"), "first")
    assert calls["many"] > 0 and calls["refused"] == 1


# ----------------------------------------------------------------------------------
# encodings
# ----------------------------------------------------------------------------------
ENCODINGS = [
    {"bom": True},
    {"encoding": "utf-16-le", "bom": True},
    {"encoding": "utf-16-be"},
    {"gz": True},
    {"gz": True, "encoding": "utf-16-le", "bom": True, "newline": "\r\n", "style": "pretty2"},
]


@pytest.mark.parametrize("kw", ENCODINGS)
def test_encodings(make_doc, kw) -> None:
    recs = dup_records(150, seed=61)
    doc = make_doc(recs, "array", **kw)
    rep = find_duplicates(doc, "k")
    check_report(rep, reference(path_values(recs, "k"), None, "first"), "first")


# ----------------------------------------------------------------------------------
# examples
# ----------------------------------------------------------------------------------
def test_examples_are_the_first_groups_with_at_most_20_positions(make_doc) -> None:
    recs = [{"k": i % 30} for i in range(30 * 25)] + [{"k": "solo"}]
    recs.reverse()
    doc = make_doc(recs, "jsonl", format="jsonl")
    rep = find_duplicates(doc, "k")
    assert rep.groups == 30 and rep.duplicates == 30 * 24
    assert len(rep.examples) == EXAMPLE_GROUPS and rep.example_sizes == [25] * EXAMPLE_GROUPS
    firsts = [positions[0] for _, positions in rep.examples]
    assert firsts == list(range(1, 21))
    for text, positions in rep.examples:
        assert len(positions) == EXAMPLE_POSITIONS and positions == sorted(positions)
        assert text == str(recs[positions[0]]["k"])
    assert rep.examples == reference([r["k"] for r in recs], None, "first")["examples"]


def test_partial_documents_and_broken_lines(make_doc) -> None:
    partial = make_doc(b'[{"k": 1}, {"k": 1.0}, {"k": 2}, {"k": 1e0}, {"k": oops')
    assert partial.error is not None and partial.record_count == 4
    rep = find_duplicates(partial, "k")
    assert list(rep.records) == [1, 3] and rep.scanned == 4
    broken = make_doc(b'{"k": "a"}\n{"k": \n{"k": "a"}\nnot json\n{"k": "\\u0061"}\n', format="jsonl")
    assert broken.broken_count == 2 and broken.record_count == 3
    rep = find_duplicates(broken, "k", keep="last")
    assert list(rep.records) == [0, 1] and rep.examples == [('"a"', [0, 1, 2])]


def test_concurrent_calls_agree(make_doc) -> None:
    import threading

    recs = dup_records(500, seed=17)
    doc = make_doc(recs, "array", settings=Settings(large_node_bytes=300))
    want = reference(path_values(recs, "k"), None, "first")
    results: list[Any] = []
    errors: list[BaseException] = []

    def run(strategy: str) -> None:
        try:
            rep = find_duplicates(doc, "k", strategy=strategy, block_bytes=5000)
            results.append((list(rep.records), rep.groups, rep.records_with_key))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,)) for s in ("extract", "parse") * 3]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert not errors
    assert results == [(want["records"], want["groups"], want["records_with_key"])] * len(threads)


def test_no_duplicates(make_doc) -> None:
    doc = make_doc(jsongen.records(100, seed=2), "array")
    rep = find_duplicates(doc, "id")
    assert (rep.groups, rep.duplicates, rep.records_with_key, rep.missing) == (0, 0, 100, 0)
    assert rep.examples == [] and rep.example_sizes == [] and list(rep.records) == []
    empty = make_doc(b"[]")
    rep = find_duplicates(empty, "id")
    assert (rep.groups, rep.scanned, rep.warnings) == (0, 0, ())


# ----------------------------------------------------------------------------------
# progress, cancellation, file changes
# ----------------------------------------------------------------------------------
def test_progress_in_bytes(make_doc, monkeypatch) -> None:
    recs = dup_records(400, seed=71)
    doc = make_doc(recs, "array", build=False)
    infos = []
    progress = Progress(callback=infos.append, min_interval=0)
    monkeypatch.setattr(dupes, "MERGE_BATCH", 10)
    rep = find_duplicates(doc, "k", progress=progress, block_bytes=4000)
    assert doc.index_complete           # built by find_duplicates (first part of the bar)
    data_bytes = doc.data_size
    assert progress.total == data_bytes and progress.unit == "bytes" and progress.finished
    done = [i.done for i in infos]
    assert done == sorted(done) and done[-1] == data_bytes
    assert len(set(done)) > 10
    messages = {i.message for i in infos}
    assert t("dupes.progress.merge", name=doc.source_path.name) in messages
    assert rep.records_with_key > 0
    # a caller's own total and message are kept
    p2 = Progress(1000, message="mine")
    find_duplicates(doc, "k", [1, 2, 3], progress=p2)
    assert p2.total == 1000 and p2.done == 1000


def test_cancel_before_and_during(make_doc, monkeypatch) -> None:
    recs = dup_records(600, seed=81)
    doc = make_doc(recs, "jsonl", format="jsonl")
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(Cancelled):
        find_duplicates(doc, "k", cancel=cancel)
    # during the scan
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 2)
    monkeypatch.setattr(dupes, "CHECK_RECORDS", 5)
    for phase in ("scan", "merge"):
        cancel = CancelToken()
        merge_message = t("dupes.progress.merge", name=doc.source_path.name)

        def on_progress(info: Any, phase: str = phase, cancel: CancelToken = cancel) -> None:
            if phase == "scan" and info.done > 0:
                cancel.cancel()
            if phase == "merge" and info.message == merge_message:
                cancel.cancel()

        progress = Progress(callback=on_progress, min_interval=0)
        with pytest.raises(Cancelled):
            find_duplicates(doc, "k", memory_limit=100, progress=progress, cancel=cancel, block_bytes=3000)
        assert cancel.cancelled
        assert no_temp_dirs()
        assert not [n for n in os.listdir(recordsets_dir()) if n.endswith(".u64")]


def test_file_changed_during_scan(make_doc) -> None:
    recs = dup_records(50, seed=91)
    doc = make_doc(recs, "array")
    p = doc.source_path
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    with pytest.raises(FileChangedError):
        find_duplicates(doc, "k")
    assert no_temp_dirs()


# ----------------------------------------------------------------------------------
# arguments, pinning, gen
# ----------------------------------------------------------------------------------
def test_bad_arguments(make_doc) -> None:
    doc = make_doc(b'[{"k": 1}]')
    for key in ("", "   ", None, 5, b"k"):
        with pytest.raises(UserInputError) as ei:
            find_duplicates(doc, key)  # type: ignore[arg-type]
        assert ei.value.key == "dupes.error.key" and ei.value.hint
    for key in ("a.*", "a[*]", "..k", "a.**.b", "*"):
        with pytest.raises(UserInputError) as ei:
            find_duplicates(doc, key)
        assert ei.value.key == "dupes.error.wildcard"
    with pytest.raises(PathSyntaxError):
        find_duplicates(doc, "a[")
    for keep in ("middle", "", None, 1):
        with pytest.raises(UserInputError) as ei:
            find_duplicates(doc, "k", keep=keep)  # type: ignore[arg-type]
        assert ei.value.key == "dupes.error.keep"
    for limit in (0, -5, True, 1.5, "64MB"):
        with pytest.raises(UserInputError) as ei:
            find_duplicates(doc, "k", memory_limit=limit)  # type: ignore[arg-type]
        assert ei.value.key == "dupes.error.memory_limit"
    with pytest.raises(ValueError):
        find_duplicates(doc, "k", strategy="fast")


def test_messages_exist_in_both_languages() -> None:
    keys = ["dupes.desc", "dupes.progress.scan", "dupes.progress.merge", "dupes.error.key", "dupes.error.key.hint",
            "dupes.error.wildcard", "dupes.error.keep", "dupes.error.memory_limit"]
    for w in ("too_large", "unreadable", "key_absent", "no_record_keys"):
        keys += [f"dupes.warn.{w}", f"dupes.warn.{w}.one"]
    for key in keys:
        assert has(key), key


def test_pinned_document_is_used_and_result_is_tagged(make_doc) -> None:
    doc = make_doc(b'[{"k": 1}, {"k": 1}, {"k": 2}]')

    class View:
        """A stand-in for the stage-3 pinned view: forwards everything, has a gen."""

        def __init__(self, origin: Document, gen: int) -> None:
            self.origin = origin
            self.gen = gen
            self.pins = 0

        def pinned(self) -> Any:
            self.pins += 1
            return Pinned(self.origin, self.gen)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.origin, name)

    class Pinned(View):
        def pinned(self) -> Any:  # a pinned view pins to itself
            return self

    live = View(doc, 7)
    rep = find_duplicates(live, "k")
    assert live.pins == 1 and rep.gen == 7
    assert getattr(rep.records, "gen", "unset") == 7
    assert list(rep.records) == [1]
    # a plain document (no edit support yet): gen None
    rep = find_duplicates(doc, "k")
    assert rep.gen == getattr(doc, "gen", None) == getattr(rep.records, "gen", "unset")
    # a source of another state is refused; a source without gen (or of this state) is fine
    stale = RecordSet.from_indices([0, 1])
    stale.gen = 6  # type: ignore[attr-defined]
    with pytest.raises(EditError) as ei:
        find_duplicates(live, "k", stale)
    assert ei.value.key == "edits.error.stale_scope" and ei.value.hint
    stale.gen = 7  # type: ignore[attr-defined]
    assert list(find_duplicates(live, "k", stale).records) == [1]
    assert list(find_duplicates(live, "k", RecordSet.base(doc)).records) == [1]


def test_record_set_description_is_localized(make_doc, lang_uz) -> None:
    doc = make_doc(b'[{"k": 1}, {"k": 1}]')
    rep = find_duplicates(doc, "k")
    assert rep.records.description == "Takroriy yozuvlar: k"


# ----------------------------------------------------------------------------------
# a larger count
# ----------------------------------------------------------------------------------
def test_many_records(make_doc, monkeypatch) -> None:
    n = 60000
    rng = random.Random(3)
    keys = [rng.randrange(1, 20001) for _ in range(n)]
    spell = ["{}", "{}.0", "{}e0", "{}0e-1", "{}.000"]
    lines = []
    for k in keys:
        lines.append(b'{"k": ' + rng.choice(spell).format(k).encode() + b', "pad": "x"}')
    doc = make_doc(b"\n".join(lines) + b"\n", format="jsonl")
    values = [k for k in keys]
    want = reference(values, None, "first")
    rep = find_duplicates(doc, "k")
    assert (rep.groups, rep.duplicates, rep.records_with_key) == (want["groups"], want["duplicates"], n)
    assert list(rep.records) == want["records"]
    assert rep.records.path is not None or len(rep.records) <= 8192   # big sets live in a file
    monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 1000)
    spilled = find_duplicates(doc, "k", memory_limit=dupes.ENTRY_BYTES * 7000)
    assert spilled.runs >= 5 and list(spilled.records) == want["records"]
    assert spilled.examples == rep.examples
