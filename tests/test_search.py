"""jsonreshape.search: the full-file search runner (docs/ARCHITECTURE.md §7).

AGENT_PROMPT §6.2 (scopes, path restriction, every mode, Uzbek conveniences, results as a
RecordSet, CSV/JSON export, history), §12 (byte-mode prefilter, results stream at once),
§14 test 15 (``search()`` equals the brute-force reference, every mode) and test 16
(apostrophe variants and Latin <-> Cyrillic at the search level).

The suite runs on whichever core backend is active; run it twice (default Rust and
``JSONRESHAPE_BACKEND=python``).
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
from array import array
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

import jsongen
from jsonreshape import codec, search as search_mod
from jsonreshape.core.constants import FLAG_ESCAPES, FLAG_U_ESCAPES
from jsonreshape.errors import Cancelled, PathSyntaxError, QuerySyntaxError, UserInputError
from jsonreshape.i18n import t
from jsonreshape.paths import Wild, parse_path, parse_pattern
from jsonreshape.reader import Document
from jsonreshape.search import (
    HISTORY_MAX,
    HitBatch,
    SearchHistory,
    SearchHit,
    SearchOptions,
    SearchResult,
    export_hits,
    make_snippet,
    reference_search,
    search,
)
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

HYP = dict(derandomize=True, database=None, deadline=None,
           suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])


# ----------------------------------------------------------------------------------
# corpora
# ----------------------------------------------------------------------------------
def _clean_str(s: str) -> str:
    """Replace control characters JSON can only write as ``\\u`` escapes."""
    return "".join(c if (ord(c) >= 0x20 or c in "\b\f\n\r\t") else "?" for c in s)


def clean(v: Any) -> Any:
    """A value whose compact JSON needs no ``\\u`` escape (byte mode stays possible)."""
    if isinstance(v, str):
        return _clean_str(v)
    if isinstance(v, dict):
        return {_clean_str(k): clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


UZ_WORDS = [
    "Toshkent", "Тошкент", "TOSHKENT", "toshkentlik", "Toshkent-2", "Yangi Toshkent shahri",
    "O'zbekiston", "Oʻzbekiston", "O‘zbekiston", "O’zbekiston", "Ўзбекистон", "ЎЗБЕКИСТОН",
    "G'ulom", "Gʻulom", "Ғулом", "Shahrisabz", "Шаҳрисабз", "Farg'ona", "Фарғона",
    "Amir Temur ko'chasi", "Мустақиллик кўчаси", "Yer", "Ер", "ер", "Straße", "STRASSE", "İstanbul",
    "ﬁle", "emoji 😀 test", "a/b", 'quote"inside', "back\\slash", "tab\tx", "line\nbreak", "null", "true",
    "123", "1.10", "child2", "child10", "", "x" * 140 + "toshkent" + "y" * 60,
]
UZ_KEYS = ["name", "ism", "city", "shahar", "Тошкент", "O'zbekiston", "kalit bo'shliq bilan", "dot.key", "a/b",
           "", "0", "toshkent_id"]
NUMBER_TOKENS = ["0", "-0", "1", "-1", "0.1", "1.10", "-0.0", "100.0", "1e400", "-1e400", "1E5", "2.5E+3",
                 "12345678901234567890", "-9223372036854775809", "123456789.123456789", "0.00001", "1000",
                 "1001", "999.5", "1.1"]


def uz_records(n: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    recs: list[dict[str, Any]] = []
    for i in range(n):
        rec: dict[str, Any] = {
            "id": i,
            "name": rng.choice(UZ_WORDS),
            "score": codec.loads(rng.choice(NUMBER_TOKENS)),
            "active": rng.choice([True, False, None]),
            "contact": {"address": {"city": rng.choice(UZ_WORDS), "zip": rng.randint(100000, 999999)},
                        "phone": f"+99890{rng.randint(1000000, 9999999)}"},
            "tags": [rng.choice(UZ_WORDS) for _ in range(rng.randint(0, 3))],
            "orders": [{"oid": rng.randint(1, 50), "sum": codec.loads(rng.choice(NUMBER_TOKENS))}
                       for _ in range(rng.randint(0, 3))],
        }
        for _ in range(rng.randint(0, 2)):
            rec[rng.choice(UZ_KEYS)] = rng.choice(UZ_WORDS + [None, 7, codec.loads("1.10")])
        if rng.random() < 0.2:
            rec["children"] = [{"name": f"child{k}", "children": [{"name": rng.choice(UZ_WORDS)}]}
                               for k in range(rng.randint(1, 3))]
        recs.append(rec)
    return recs


def corpus(name: str, n: int = 60, seed: int = 7) -> list[Any]:
    if name == "gen":
        return jsongen.records(n, seed=seed)
    if name == "clean":
        return clean(jsongen.records(n, seed=seed))
    if name == "uz":
        return uz_records(n, seed)
    raise ValueError(name)


def keyed_keys(n: int, seed: int) -> list[str]:
    rng = random.Random(seed + 1)
    return [f"{rng.choice(UZ_WORDS[:20])}-{i}" for i in range(n)]


@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []

    def make(recs: list[Any], fmt: str = "array", style: str = "compact", *, settings: Settings | None = None,
             build: bool = True, raw: bytes | None = None, name: str | None = None, record_path: Any = None,
             **kw: Any) -> Document:
        p = tmp_path / (name or f"doc{len(docs)}-{fmt}-{style}.json")
        if raw is not None:
            p.write_bytes(raw)
        else:
            if fmt == "keyed" and "keys" not in kw:
                kw["keys"] = keyed_keys(len(recs), 0)
            jsongen.write_file(p, recs, fmt, style=style, **kw)
        doc = Document.open(p, settings=settings, record_path=record_path, format=None if raw is not None or fmt in (
            "array", "jsonl", "keyed", "wrapped") else fmt)
        docs.append(doc)
        if build:
            doc.build_index()
        return doc

    yield make
    for d in docs:
        d.close()


# ----------------------------------------------------------------------------------
# an independent oracle (straight from the spec text, recursive, no shared code)
# ----------------------------------------------------------------------------------
def naive_hits(doc: Document, opts: SearchOptions, source: Any = None) -> list[SearchHit]:
    q = opts.query
    pat = parse_pattern(opts.path) if opts.path else None

    def passes(p: tuple) -> bool:
        return pat is None or pat.matches_prefix_of(p)

    keys_on = opts.scope in ("both", "keys") and q.mode != "number"
    values_on = opts.scope in ("both", "values")
    out: list[SearchHit] = []
    indices = range(doc.record_count) if source is None else source
    for i in indices:
        value = doc.record(i)
        rkey = doc.record_key(i)

        def emit(path: tuple, where: str, text: str) -> None:
            spans = q.find_spans(text)
            if spans:
                snippet, rel = make_snippet(text, spans)
                out.append(SearchHit(i, path, where, snippet, rel, rkey))

        def walk(v: Any, path: tuple) -> None:
            if isinstance(v, dict):
                for k, x in v.items():
                    p = path + (k,)
                    if keys_on and passes(p):
                        emit(p, "key", k)
                    walk(x, p)
            elif isinstance(v, list):
                for j, x in enumerate(v):
                    walk(x, path + (j,))
            elif values_on and passes(path):
                if q.mode == "number":
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        emit(path, "value", codec.number_text(v))
                else:
                    emit(path, "value", codec.value_text(v))

        if rkey is not None and keys_on and passes(()):
            emit((), "key", rkey)
        walk(value, ())
    return out


def record_order(hits: list[SearchHit]) -> list[int]:
    out: list[int] = []
    for h in hits:
        if not out or out[-1] != h.record:
            out.append(h.record)
    return out


def check_search(doc: Document, opts: SearchOptions, source: Any = None, **kw: Any) -> SearchResult:
    """search() == reference_search() exactly; records/count consistent; prefilter used
    exactly when the query has byte needles for this file."""
    res = search(doc, opts, source, **kw)
    ref = reference_search(doc, opts, source)
    assert res.hits == ref, (opts.query, opts.scope, opts.path)
    assert res.hit_count == len(ref)
    assert list(res.records) == record_order(ref)
    assert res.record_count == len(record_order(ref))
    assert not res.truncated
    if res.records_scanned:
        needles = opts.query.prefilter_needles(doc.flags)
        assert res.mode_used == ("bytes" if needles else "parse")
    if res.mode_used == "parse":
        assert res.records_parsed == res.records_scanned
    else:
        assert res.records_parsed <= res.records_scanned
    return res


# ----------------------------------------------------------------------------------
# §14 test 15: search() == reference_search(), every mode / scope / path restriction
# ----------------------------------------------------------------------------------
QUERIES: list[tuple[str, dict[str, Any]]] = [
    ("toshkent", {}),
    ("Toshkent", {"case_sensitive": True}),
    ("TOSHKENT", {"case_sensitive": True}),
    ("O'zbekiston", {}),
    ("oʻzbekiston", {}),
    ("O’zbekiston", {"case_sensitive": True}),
    ("'", {}),
    ("ko'chasi", {"scope": "values"}),
    ("toshkent", {"whole_word": True}),
    ("O", {"whole_word": True, "case_sensitive": True}),
    ("1", {"whole_word": True}),
    ("1.10", {}),
    ("1e400", {}),
    ("-0", {}),
    ("null", {}),
    ("true", {"scope": "values"}),
    ("strasse", {}),
    ("ss", {}),
    ("İstanbul", {}),
    ("file", {}),
    ("😀", {}),
    ('quote"', {}),
    ("back\\slash", {}),
    ("a/b", {}),
    ("tab\tx", {}),
    ("e", {}),
    ("name", {"scope": "keys"}),
    ("Тошкент", {"scope": "keys"}),
    ("city", {}),
    ("toshkent", {"path": "contact"}),
    ("toshkent", {"path": "**.city"}),
    ("toshkent", {"path": "..city", "scope": "values"}),
    ("toshkent", {"path": "tags[*]"}),
    ("1", {"path": "orders[*].sum"}),
    ("a", {"path": "$"}),
    ("a", {"path": "**", "scope": "keys"}),
    ("i", {"path": "children[0]"}),
    ("id", {"path": "nonexistent"}),
    (r"^tosh", {"mode": "regex"}),
    (r"\d{3}", {"mode": "regex"}),
    (r"ko.chasi", {"mode": "regex", "case_sensitive": True}),
    (r"x*", {"mode": "regex", "path": "contact"}),
    (r"o'z|ўз", {"mode": "regex"}),
    (r"Toshkent", {"mode": "regex", "whole_word": True}),
    ("> 1000", {"mode": "number"}),
    ("< 0", {"mode": "number"}),
    ("= 1.1", {"mode": "number"}),
    ("0", {"mode": "number"}),
    ("1..10", {"mode": "number", "path": "orders"}),
    (">= 1e300", {"mode": "number"}),
    ("> 5", {"mode": "number", "scope": "keys"}),
    ("Toshkent", {"translit": True}),
    ("Тошкент", {"translit": True}),
    ("O'zbekiston", {"translit": True}),
    ("Shahrisabz", {"translit": True, "case_sensitive": True}),
    ("ер", {"translit": True}),
    ("g'ulom", {"translit": True, "whole_word": True}),
]


@pytest.mark.parametrize("style", ["compact", "escaped"])
@pytest.mark.parametrize("fmt", ["array", "jsonl", "keyed", "wrapped"])
@pytest.mark.parametrize("name", ["gen", "clean", "uz"])
def test_search_equals_reference(make_doc, name: str, fmt: str, style: str) -> None:
    doc = make_doc(corpus(name), fmt, style)
    if name == "uz":
        assert bool(doc.flags & FLAG_U_ESCAPES) == (style == "escaped")
    if name == "clean" and style == "compact":
        assert not doc.flags & FLAG_U_ESCAPES  # byte mode possible
    modes = set()
    for text, kw in QUERIES:
        opts = SearchOptions.build(text, **kw)
        modes.add(check_search(doc, opts).mode_used)
    if doc.flags & FLAG_U_ESCAPES:
        assert modes == {"parse"}
    else:
        assert modes == {"bytes", "parse"}  # \\u escapes in the file: no byte prefilter


@pytest.mark.parametrize("locate", [True, False])
@pytest.mark.parametrize("fmt", ["array", "keyed"])
def test_both_byte_mode_confirm_paths(make_doc, monkeypatch: pytest.MonkeyPatch, fmt: str, locate: bool) -> None:
    """Byte mode confirms candidates from their located tokens (``search.LOCATE``, the
    default with the Rust core) or by parsing them: the same hits on the active core."""
    monkeypatch.setattr(search_mod, "LOCATE", locate)
    doc = make_doc(corpus("uz", 80, seed=3), fmt)
    for text, kw in QUERIES:
        check_search(doc, SearchOptions.build(text, **kw))


@pytest.mark.parametrize("fmt", ["single", "stream", "brace_list"])
def test_search_other_formats(make_doc, fmt: str) -> None:
    doc = make_doc(corpus("uz", 30), fmt)
    assert doc.kind == fmt
    for text, kw in QUERIES[::3]:
        check_search(doc, SearchOptions.build(text, **kw))


@pytest.mark.parametrize("name", ["gen", "uz"])
@pytest.mark.parametrize("fmt", ["array", "keyed"])
def test_reference_equals_naive_oracle(make_doc, name: str, fmt: str) -> None:
    """The shared confirm function follows the spec: compare with a naive recursive walk."""
    doc = make_doc(corpus(name, 40), fmt)
    for text, kw in QUERIES:
        opts = SearchOptions.build(text, **kw)
        assert reference_search(doc, opts) == naive_hits(doc, opts), (text, kw)


def test_rare_needle_uses_byte_prefilter(make_doc) -> None:
    recs = clean(jsongen.records(300, seed=3))
    recs[17]["note"] = "Qoraqalpog'iston-maxsus"
    recs[250]["contact"] = {"address": {"street": "QORAQALPOGʻISTON-MAXSUS ko'chasi"}}
    doc = make_doc(recs, "array")
    res = check_search(doc, SearchOptions.build("qoraqalpog'iston-maxsus"))
    assert res.mode_used == "bytes"
    assert res.records_scanned == 300
    # only candidates are parsed (the needles also hold exotic case variants such as "ß",
    # which the corpus has in "Straße": a few false candidates, never a missed record)
    assert 2 <= res.records_parsed < 50
    assert [h.record for h in res.hits] == [17, 250]
    assert res.hits[1].path == ("contact", "address", "street")
    assert res.bytes_scanned > 0 and res.elapsed > 0


def test_parse_mode_for_regex_number_translit_and_u_escapes(make_doc) -> None:
    doc = make_doc(corpus("uz", 20), "array")
    assert check_search(doc, SearchOptions.build("toshkent")).mode_used == "bytes"
    for text, kw in [("tosh", {"mode": "regex"}), ("> 1", {"mode": "number"}), ("tosh", {"translit": True})]:
        assert check_search(doc, SearchOptions.build(text, **kw)).mode_used == "parse"
    escaped = make_doc(corpus("uz", 20), "array", "escaped")
    assert check_search(escaped, SearchOptions.build("toshkent")).mode_used == "parse"


# ----------------------------------------------------------------------------------
# hypothesis: random documents x random queries
# ----------------------------------------------------------------------------------
ALPHABET = "aAoOsShHtTkKeEnNiI'ʻ’`ўЎшШтТеЕ 01.-ßİ\"\\/"
_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(-10**20, 10**20),
    st.sampled_from(NUMBER_TOKENS).map(codec.loads),
    st.text(ALPHABET, max_size=8),
)
_values = st.recursive(
    _scalars,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(ALPHABET, max_size=5), children,
                                                                         max_size=4),
    max_leaves=14,
)
_queries = st.one_of(
    st.builds(lambda txt, cs, ww, tr, scope: (txt, {"case_sensitive": cs, "whole_word": ww, "translit": tr,
                                                    "scope": scope}),
              st.text(ALPHABET, min_size=1, max_size=4), st.booleans(), st.booleans(), st.booleans(),
              st.sampled_from(["both", "keys", "values"])),
    st.builds(lambda txt, cs: (txt, {"mode": "regex", "case_sensitive": cs}),
              st.sampled_from(["a", "^t", "o.z", "[0-9]+", "s$", "'", "x*", r"\w+"]), st.booleans()),
    st.builds(lambda txt: (txt, {"mode": "number"}),
              st.sampled_from(["> 0", "< 1", "= 1.1", "0", "-1..1", ">= 1e300", "!= 1"])),
)
_paths = st.sampled_from([None, None, None, "a", "*", "**", "[*]", "**.a", "*.a", "[0]", "a[0]", "$", "**.'"])


@hsettings(max_examples=40, **HYP)
@given(recs=st.lists(_values, min_size=1, max_size=6), query=_queries, path=_paths,
       fmt=st.sampled_from(["array", "jsonl", "single"]), style=st.sampled_from(["compact", "escaped", "pretty2"]))
def test_property_search_equals_reference(make_doc, recs: list[Any], query: tuple[str, dict], path: str | None,
                                          fmt: str, style: str) -> None:
    text, kw = query
    try:
        opts = SearchOptions.build(text, path=path, **kw)
    except (UserInputError, QuerySyntaxError):
        return
    doc = make_doc(recs, fmt, style)
    res = check_search(doc, opts)
    if fmt != "single":
        assert reference_search(doc, opts) == naive_hits(doc, opts)
    del res


# ----------------------------------------------------------------------------------
# §14 test 16 at the search level
# ----------------------------------------------------------------------------------
UZ_CITIES = [
    {"city": "Тошкент"}, {"city": "Toshkent"}, {"city": "TOSHKENT shahri"}, {"country": "O'zbekiston"},
    {"country": "Oʻzbekiston"}, {"country": "O‘zbekiston"}, {"country": "O’zbekiston"}, {"country": "Ўзбекистон"},
    {"country": "Ozbekiston"}, {"city": "Samarqand"},
]


@pytest.mark.parametrize("style", ["compact", "escaped"])
def test_latin_cyrillic_and_apostrophes(make_doc, style: str) -> None:
    doc = make_doc(UZ_CITIES, "array", style)

    def records(text: str, **kw: Any) -> list[int]:
        res = check_search(doc, SearchOptions.build(text, **kw))
        return [h.record for h in res.hits]

    assert records("Toshkent", translit=True) == [0, 1, 2]
    assert records("Тошкент", translit=True) == [0, 1, 2]
    assert records("Toshkent") == [1, 2]
    assert records("Тошкент") == [0]
    for variant in ("O'zbekiston", "Oʻzbekiston", "O‘zbekiston", "O’zbekiston", "O`zbekiston", "O´zbekiston"):
        assert records(variant) == [3, 4, 5, 6], variant
        assert records(variant.lower()) == [3, 4, 5, 6], variant
        assert records(variant, case_sensitive=True) == [3, 4, 5, 6], variant
        assert records(variant.lower(), case_sensitive=True) == [], variant
        assert records(variant, translit=True) == [3, 4, 5, 6, 7], variant
    assert records("Ўзбекистон", translit=True) == [3, 4, 5, 6, 7]
    assert records("ozbekiston") == [8]
    res = search(doc, SearchOptions.build("toshkent", translit=True))
    assert [(h.text, h.spans) for h in res.hits] == [("Тошкент", ((0, 7),)), ("Toshkent", ((0, 8),)),
                                                      ("TOSHKENT shahri", ((0, 8),))]
    assert all(h.path == ("city",) and h.where == "value" and h.key is None for h in res.hits)


# ----------------------------------------------------------------------------------
# hits: keys, paths, record keys, number mode, snippets
# ----------------------------------------------------------------------------------
def test_hit_order_paths_and_keys(make_doc) -> None:
    recs = [{"Toshkent": {"name": "Toshkent", "list": ["x", "toshkent", {"toshkent": 1}]}, "n": 5},
            ["toshkent", ["TOSHKENT"]], "toshkentlik", 12]
    doc = make_doc(recs, "jsonl")
    res = search(doc, SearchOptions.build("toshkent"))
    assert [(h.record, h.path, h.where, h.text) for h in res.hits] == [
        (0, ("Toshkent",), "key", "Toshkent"),
        (0, ("Toshkent", "name"), "value", "Toshkent"),
        (0, ("Toshkent", "list", 1), "value", "toshkent"),
        (0, ("Toshkent", "list", 2, "toshkent"), "key", "toshkent"),
        (1, (0,), "value", "toshkent"),
        (1, (1, 0), "value", "TOSHKENT"),
        (2, (), "value", "toshkentlik"),
    ]
    assert list(res.records) == [0, 1, 2]
    keys = search(doc, SearchOptions.build("toshkent", scope="keys"))
    assert [h.path for h in keys.hits] == [("Toshkent",), ("Toshkent", "list", 2, "toshkent")]
    values = search(doc, SearchOptions.build("toshkent", scope="values"))
    assert len(values.hits) == 5 and all(h.where == "value" for h in values.hits)
    assert search(doc, SearchOptions.build("12")).hits[0].path == ()
    hit = res.hits[0]
    assert hit.path_text == "Toshkent" and res.hits[6].path_text == "$"
    assert hit.to_dict() == {"record": 0, "key": None, "path": "Toshkent", "where": "key", "text": "Toshkent",
                             "spans": [[0, 8]]}
    assert hash(hit) == hash(SearchHit(0, ("Toshkent",), "key", "Toshkent", ((0, 8),)))


def test_duplicate_keys_follow_dict_semantics(make_doc) -> None:
    raw = b'[{"a": "toshkent 1", "b": "x", "a": "toshkent 2"}]'
    doc = make_doc([], raw=raw)
    res = check_search(doc, SearchOptions.build("toshkent"))
    assert [(h.path, h.text) for h in res.hits] == [(("a",), "toshkent 2")]


def test_keyed_record_keys(make_doc) -> None:
    recs = [{"name": "Toshkent"}, {"name": "Samarqand"}, {"name": "Buxoro"}]
    doc = make_doc(recs, "keyed", keys=["Toshkent-1", "tosh", "Toshkent"])
    assert doc.member_records
    res = check_search(doc, SearchOptions.build("toshkent"))
    assert [(h.record, h.path, h.where, h.key) for h in res.hits] == [
        (0, (), "key", "Toshkent-1"), (0, ("name",), "value", "Toshkent-1"), (2, (), "key", "Toshkent")]
    # a path filter excludes the record key unless it matches the empty path
    named = check_search(doc, SearchOptions.build("toshkent", path="name"))
    assert [(h.record, h.path) for h in named.hits] == [(0, ("name",))]
    everything = check_search(doc, SearchOptions.build("toshkent", path="**"))
    assert everything.hits == res.hits
    assert [h.where for h in check_search(doc, SearchOptions.build("tosh", scope="keys")).hits] == ["key"] * 3


def test_wrapped_records_only(make_doc) -> None:
    recs = [{"name": "Toshkent"}, {"name": "test"}]
    doc = make_doc(recs, "wrapped", record_path="data")
    assert doc.kind == "wrapped"
    res = check_search(doc, SearchOptions.build("test"))
    # the wrapper ({"meta": {"source": "test"}, ...}) is not a record and is not searched
    assert [(h.record, h.path) for h in res.hits] == [(1, ("name",))]


def test_number_mode(make_doc) -> None:
    raw = (b'[{"a": 1.10, "b": "1.1", "1.1": true}, {"a": -0, "b": 0.0, "c": null}, '
           b'{"a": 1e400, "b": 12345678901234567890, "c": [5, 1000.5]}]')
    doc = make_doc([], raw=raw)

    def hits(text: str, **kw: Any) -> list[tuple[int, tuple, str]]:
        res = check_search(doc, SearchOptions.build(text, mode="number", **kw))
        return [(h.record, h.path, h.text) for h in res.hits]

    assert hits("= 1.1") == [(0, ("a",), "1.10")]  # the key "1.1" and the string "1.1" are not numbers
    assert hits("0") == [(1, ("a",), "-0"), (1, ("b",), "0.0")]
    assert hits("> 1e300") == [(2, ("a",), "1e400")]
    assert hits("= 12345678901234567890") == [(2, ("b",), "12345678901234567890")]
    assert hits("5..1000.5", path="c") == [(2, ("c", 0), "5"), (2, ("c", 1), "1000.5")]
    assert hits("> 0", scope="keys") == []
    res = search(doc, SearchOptions.build("> 1000", mode="number"))
    assert [h.spans for h in res.hits] == [((0, 5),), ((0, 20),), ((0, 6),)]


def test_plain_mode_numbers_and_literals(make_doc) -> None:
    raw = b'[{"a": 1.10, "b": 1e400, "c": -0, "d": true, "e": null, "f": [false]}]'
    doc = make_doc([], raw=raw)
    texts = [h.text for h in check_search(doc, SearchOptions.build("1")).hits]
    assert texts == ["1.10", "1e400"]
    assert [h.text for h in check_search(doc, SearchOptions.build("-0")).hits] == ["-0"]
    assert [h.path for h in check_search(doc, SearchOptions.build("e")).hits] == [
        ("b",), ("d",), ("e",), ("f", 0)]
    assert [h.path for h in check_search(doc, SearchOptions.build("null")).hits] == [("e",)]


def test_snippets() -> None:
    assert make_snippet("Toshkent", [(0, 4)]) == ("Toshkent", ((0, 4),))
    text = "a" * 100 + "Toshkent" + "b" * 100
    snip, spans = make_snippet(text, [(100, 108)])
    assert snip.startswith("\N{HORIZONTAL ELLIPSIS}") and snip.endswith("\N{HORIZONTAL ELLIPSIS}")
    (s, e), = spans
    assert snip[s:e] == "Toshkent"
    assert len(snip) == 1 + 50 + 8 + 50 + 1
    start = make_snippet("Toshkent" + "x" * 200, [(0, 8)])
    assert not start[0].startswith("\N{HORIZONTAL ELLIPSIS}") and start[1] == ((0, 8),)
    end = make_snippet("x" * 200 + "Toshkent", [(200, 208)])
    assert end[0].endswith("Toshkent") and end[0][end[1][0][0]:end[1][0][1]] == "Toshkent"
    # several matches: the ones inside the window are kept (clipped), the rest dropped
    many = "t" * 300
    snip, spans = make_snippet(many, [(120, 121), (140, 160), (165, 175), (290, 291)])
    assert spans[0] == (51, 52) and spans[1] == (71, 91) and spans[2] == (96, 102)
    assert len(spans) == 3 and len(snip) == 1 + 101 + 1
    # a snippet ends at most SNIPPET_REACH (150) characters after the start of the first match
    long, spans = make_snippet("q" * 1000, [(10, 900)])
    assert spans == ((10, 160),) and len(long) == 10 + 150 + 1
    # an empty regex match at the end of a long string
    assert make_snippet("z" * 130, [(130, 130)])[1] == ((51, 51),)


def test_long_values_get_snippets_in_hits(make_doc) -> None:
    recs = corpus("uz", 60)
    recs[5]["name"] = "x" * 140 + "Toshkent" + "y" * 60
    doc = make_doc(recs, "array")
    res = check_search(doc, SearchOptions.build("toshkent", path="name"))
    long_hits = [h for h in res.hits if h.text.startswith("\N{HORIZONTAL ELLIPSIS}")]
    assert long_hits and long_hits[0].record == 5 and len(long_hits[0].text) == 110
    for h in long_hits:
        assert all(h.text[a:b].casefold() == "toshkent" for a, b in h.spans)


# ----------------------------------------------------------------------------------
# path filter: incremental NFA == PathPattern.matches_prefix_of
# ----------------------------------------------------------------------------------
_seg = st.sampled_from(["a", "b", "0", "1", "x y", "'"]) | st.integers(0, 2)
_pattern_texts = st.sampled_from([
    "a", "a.b", "*", "[*]", "**", "**.a", "a.**", "a[0]", "a.0", "$", "[1]", "*.b", "a..b", "..0", "[*].a",
    "**.*", "*.*.*", "**[1]", "a.*.b", '["x y"]', "/a/0", "**.**.b", "$..a[*]",
])


@hsettings(max_examples=300, **HYP)
@given(text=_pattern_texts, path=st.lists(_seg, max_size=5).map(tuple))
def test_path_filter_matches_prefix_of(text: str, path: tuple) -> None:
    pattern = parse_pattern(text)
    filt = search_mod._PathFilter(pattern)
    assert (filt.state_for(path) is None) == pattern.matches_prefix_of(path)


def test_path_filter_dead_subtrees_are_skipped(make_doc) -> None:
    doc = make_doc([{"a": {"name": "Toshkent"}, "name": "Toshkent", "b": [{"name": "toshkent"}]}], "array")
    res = check_search(doc, SearchOptions.build("toshkent", path="b[*].name"))
    assert [h.path for h in res.hits] == [("b", 0, "name")]
    res = check_search(doc, SearchOptions.build("name", path="**.name", scope="keys"))
    assert [h.path for h in res.hits] == [("a", "name"), ("name",), ("b", 0, "name")]
    assert parse_pattern("**.name").segments[0] is Wild.DESCENDANTS


# ----------------------------------------------------------------------------------
# truncation, streaming of results, progress, cancellation
# ----------------------------------------------------------------------------------
def test_truncation_counts_everything(make_doc) -> None:
    doc = make_doc(corpus("uz", 80), "array")
    opts = SearchOptions.build("o")
    ref = reference_search(doc, opts)
    assert len(ref) > 20
    res = search(doc, opts, max_hits=7)
    assert res.truncated and res.hits == ref[:7] and res.hit_count == len(ref)
    assert list(res.records) == record_order(ref) and res.record_count == len(record_order(ref))
    assert "7" in res.summary() and str(len(ref)) in res.summary()
    none = search(doc, opts, max_hits=0)
    assert none.hits == [] and none.truncated and none.hit_count == len(ref)
    unlimited = search(doc, opts, max_hits=None)
    assert unlimited.hits == ref and not unlimited.truncated
    exact = search(doc, opts, max_hits=len(ref))
    assert exact.hits == ref and not exact.truncated
    with pytest.raises(ValueError):
        search(doc, opts, max_hits=-1)


def test_on_hits_streams_in_order(make_doc) -> None:
    recs = clean(jsongen.records(400, seed=11))
    for i in range(0, 400, 7):
        recs[i]["note"] = f"Toshkent {i}"
    doc = make_doc(recs, "array")
    batches: list[HitBatch] = []
    seen_done: list[int] = []
    progress = Progress()

    def on_hits(batch: HitBatch) -> None:
        assert isinstance(batch, HitBatch) and isinstance(batch, list)
        batches.append(batch)
        seen_done.append(progress.done)

    res = search(doc, SearchOptions.build("toshkent"), progress=progress, on_hits=on_hits, block_bytes=2048)
    assert res.mode_used == "bytes"
    assert [h for b in batches for h in b] == res.hits == reference_search(doc, SearchOptions.build("toshkent"))
    assert batches[0] and seen_done[0] < progress.total / 4  # the first hits arrive long before the end
    assert batches[-1].hit_count == res.hit_count and batches[-1].record_count == res.record_count
    counts = [b.hit_count for b in batches]
    assert counts == sorted(counts)
    assert all(b.bytes_scanned <= res.bytes_scanned for b in batches)


def test_on_hits_after_truncation_carries_totals(make_doc, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_mod, "EMIT_INTERVAL", 0.0)
    doc = make_doc(corpus("uz", 60), "array")
    batches: list[HitBatch] = []
    res = search(doc, SearchOptions.build("a"), max_hits=3, on_hits=batches.append, block_bytes=512)
    assert sum(len(b) for b in batches) == 3
    assert [h for b in batches for h in b] == res.hits
    assert any(len(b) == 0 and b.truncated for b in batches)
    assert batches[-1].hit_count == res.hit_count > 3


def test_progress_in_bytes(make_doc) -> None:
    doc = make_doc(corpus("uz", 50), "array")
    infos = []
    progress = Progress(callback=infos.append, min_interval=0)
    res = search(doc, SearchOptions.build("toshkent"), progress=progress, block_bytes=1024)
    info = progress.info()
    assert info.unit == "bytes" and info.finished and info.done == info.total == doc.data_size - doc.info.data_start
    assert progress.message == t("search.progress.search", name=doc.source_path.name)
    assert len(infos) > 2 and res.bytes_scanned > 0
    dones = [i.done for i in infos]
    assert dones == sorted(dones)


def test_cancel(make_doc) -> None:
    doc = make_doc(corpus("uz", 50), "array")
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        search(doc, SearchOptions.build("toshkent"), cancel=token)


def test_cancel_from_on_hits_removes_partial_recordset(make_doc, monkeypatch: pytest.MonkeyPatch) -> None:
    from jsonreshape import recordset
    from jsonreshape.appdirs import recordsets_dir

    def tiny_builder(kind: str, description: str = "") -> recordset.RecordSetBuilder:
        return recordset.RecordSetBuilder(kind, description, flush_bytes=16, memory_max=16)

    monkeypatch.setattr(search_mod, "_RecordSetBuilder", tiny_builder)
    monkeypatch.setattr(search_mod, "EMIT_INTERVAL", 0.0)
    doc = make_doc(corpus("uz", 200), "array")
    token = CancelToken()
    calls = []

    def on_hits(batch: HitBatch) -> None:
        calls.append(len(batch))
        if batch.record_count > 40:
            token.cancel()

    with pytest.raises(Cancelled):
        search(doc, SearchOptions.build("a"), cancel=token, on_hits=on_hits, block_bytes=1024)
    assert calls
    assert list(recordsets_dir().glob("*")) == []
    # without cancelling, the same search gives a file-backed set
    res = search(doc, SearchOptions.build("a"), block_bytes=1024)
    assert res.records.path is not None and len(res.records) == res.record_count
    res.close()
    assert list(recordsets_dir().glob("*.u64")) == []


def test_records_is_a_recordset(make_doc) -> None:
    from jsonreshape.recordset import RecordSet

    doc = make_doc(corpus("uz", 40), "array")
    res = search(doc, SearchOptions.build("toshkent"))
    assert isinstance(res.records, RecordSet) and res.records.kind == "search"
    assert res.records.complete and res.records.is_ascending()
    # the result feeds other record-set operations, e.g. a search within the results
    again = check_search(doc, SearchOptions.build("tosh"), source=res.records)
    assert again.records_scanned == len(res.records) and set(again.records) <= set(res.records)
    res.close()
    assert res.records.closed and len(res.hits) > 0


# ----------------------------------------------------------------------------------
# sources, partial documents, background index
# ----------------------------------------------------------------------------------
def test_sources_keep_their_order(make_doc) -> None:
    from jsonreshape.recordset import RecordSet

    doc = make_doc(corpus("uz", 50), "array")
    opts = SearchOptions.build("toshkent")
    for source in (range(10, 30), [41, 3, 3, 27, 0], array("Q", [5, 4, 3]), RecordSet.from_indices([49, 1, 30])):
        res = check_search(doc, opts, source=source)
        indices = list(source)
        assert all(h.record in indices for h in res.hits)
        positions = [indices.index(r) for r in res.records]
        assert positions == sorted(positions)
    gen = search(doc, opts, source=(i for i in (7, 2, 9)))
    assert gen.hits == reference_search(doc, opts, [7, 2, 9]) and gen.records_scanned == 3
    with pytest.raises(IndexError):
        search(doc, opts, source=[999])


def test_partial_document_and_broken_lines(make_doc) -> None:
    doc = make_doc([], raw=b'[{"a": "toshkent"}, {"a": "Toshkent"}, {"a": tosh}, {"a": "toshkent"}]')
    assert doc.partial and doc.record_count == 2
    res = check_search(doc, SearchOptions.build("toshkent"))
    assert [h.record for h in res.hits] == [0, 1]
    jl = make_doc([], raw=b'{"a": "toshkent"}\n{"a": oops toshkent}\n{"a": "TOSHKENT"}\n', name="broken.jsonl")
    assert jl.kind == "jsonl" and jl.broken_count == 1
    res = check_search(jl, SearchOptions.build("toshkent"))
    assert [h.record for h in res.hits] == [0, 1] and res.hits[1].text == "TOSHKENT"


def test_search_while_the_index_is_built(make_doc) -> None:
    recs = clean(jsongen.records(300, seed=5))
    doc = make_doc(recs, "array", build=False)
    assert not doc.index_complete
    opts = SearchOptions.build("toshkent")
    res = search(doc, opts)  # starts the background index and follows it
    assert doc.index_complete
    assert res.hits == reference_search(doc, opts)
    assert res.records_scanned == 300
    other = make_doc(recs, "array", build=False)
    sub = search(other, opts, source=range(100, 200))  # waits for (builds) the index first
    assert other.index_complete and sub.mode_used == "bytes"
    assert sub.hits == reference_search(other, opts, range(100, 200))


class _GrowingDoc:
    """Just enough of a Document whose index is still being built."""

    def __init__(self, n: int) -> None:
        self.index_complete = False
        self.record_count = n
        self.source_path = Path("growing.json")
        self.sources: list[Any] = []

    def iter_blocks(self, source: Any, *, block_bytes: int, cancel: Any) -> Any:
        self.sources.append(source)
        for i in (range(self.record_count) if source is None else source):
            yield type("Blk", (), {"indices": array("Q", [i])})()

    def build_index(self, cancel: Any = None) -> None:
        self.index_complete = True


def test_search_during_indexing_waits_for_the_index_then_resumes() -> None:
    doc = _GrowingDoc(10)
    progress = Progress(message="searching")
    messages: list[str] = []
    progress.callback = lambda info: messages.append(info.message)
    blocks = list(search_mod._record_blocks(doc, None, 1024, progress, None, 0.0))
    assert [int(b.indices[0]) for b in blocks] == list(range(10))  # nothing lost or repeated
    assert doc.sources == [None, range(1, 10)] and doc.index_complete
    assert progress.message == "searching" and any("index" in m.lower() or "indeks" in m for m in messages)
    plain = _GrowingDoc(4)
    assert len(list(search_mod._record_blocks(plain, None, 1024, None, None, None))) == 4
    assert plain.sources == [None] and not plain.index_complete  # parse-only queries keep following


# ----------------------------------------------------------------------------------
# huge records (streamed) and records too deep to parse in memory
# ----------------------------------------------------------------------------------
def huge_corpus(n: int, seed: int) -> list[dict[str, Any]]:
    """Records the streaming path reads exactly like codec.loads (no duplicate keys, no
    -0, numbers whose Decimal text is the token)."""
    rng = random.Random(seed)
    words = [w for w in UZ_WORDS if w not in ("",)]
    out = []
    for i in range(n):
        rec = {"id": i, "name": rng.choice(words), "score": rng.choice([0.5, 12.25, 100, -7, 1.1]),
               "flag": rng.choice([True, False, None]),
               "items": [{"city": rng.choice(words), "n": rng.randint(0, 3000)} for _ in range(rng.randint(0, 6))]}
        if i % 5 == 0:
            rec["pad"] = "p" * rng.randint(100, 600)
        out.append(rec)
    return out


@pytest.mark.parametrize("fmt", ["array", "keyed", "jsonl"])
def test_huge_records_are_streamed(make_doc, fmt: str) -> None:
    doc = make_doc(huge_corpus(40, 1), fmt, settings=Settings(large_node_bytes=300))
    huge = [i for i in range(doc.record_count) if doc.is_huge(i)]
    assert 0 < len(huge) < doc.record_count
    for text, kw in [("toshkent", {}), ("Toshkent", {"translit": True}), ("> 2000", {"mode": "number"}),
                     ("city", {"scope": "keys"}), ("1", {"path": "items[*].n"}), ("o'z", {"path": "**.city"}),
                     ("1.1", {}), ("null", {}), (r"^p+$", {"mode": "regex"}), ("toshkent", {"path": "$"})]:
        check_search(doc, SearchOptions.build(text, **kw))


def test_huge_records_byte_prefilter(make_doc) -> None:
    recs = huge_corpus(30, 2)
    for r in recs:
        r["pad"] = "q" * 400
    recs[13]["marker"] = "Qoraqalpog'iston"
    doc = make_doc(recs, "array", settings=Settings(large_node_bytes=200))
    assert all(doc.is_huge(i) for i in range(doc.record_count))
    res = check_search(doc, SearchOptions.build("qoraqalpogʻiston"))
    assert res.mode_used == "bytes" and res.records_parsed == 1
    assert [(h.record, h.path) for h in res.hits] == [(13, ("marker",))]


def test_huge_record_duplicate_keys_reported_per_occurrence(make_doc) -> None:
    """Documented deviation of the streaming path: every occurrence of a duplicate key."""
    raw = b'[{"a": "toshkent", "pad": "' + b"x" * 300 + b'", "a": "TOSHKENT"}]'
    doc = make_doc([], raw=raw, settings=Settings(large_node_bytes=100))
    res = search(doc, SearchOptions.build("toshkent"))
    assert [(h.path, h.text) for h in res.hits] == [(("a",), "toshkent"), (("a",), "TOSHKENT")]
    assert [(h.path, h.text) for h in reference_search(doc, SearchOptions.build("toshkent"))] == [
        (("a",), "TOSHKENT")]


def test_reference_refuses_records_beyond_the_memory_limit(make_doc) -> None:
    doc = make_doc([{"pad": "x" * 500}], "array", settings=Settings(large_node_bytes=100, memory_limit_bytes=200))
    from jsonreshape.errors import TooLargeError

    with pytest.raises(TooLargeError):
        reference_search(doc, SearchOptions.build("x"))
    assert search(doc, SearchOptions.build("x")).hit_count == 1


def test_undecodable_records_are_streamed(make_doc, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid record the in-memory decoders refuse (e.g. nested deeper than their
    recursion limit) is confirmed with the streaming walk, by search and reference alike."""
    raw = b'[{"a": "toshkent"}, {"refuse": ["Toshkent", {"toshkent": 1.10}]}, {"b": ["toshkent"]}]'
    doc = make_doc([], raw=raw)
    real_loads = codec.loads
    real_orjson = search_mod.orjson.loads

    def refused(data: Any) -> bool:
        return b"refuse" in (data.encode("utf-8", "surrogatepass") if isinstance(data, str) else bytes(data))

    def refuse(data: Any) -> Any:
        if refused(data):
            raise codec.JsonDecodeError("simulated: too deep")
        return real_loads(data)

    def refuse_orjson(data: Any) -> Any:
        if refused(data):
            raise search_mod.orjson.JSONDecodeError("simulated", "", 0)
        return real_orjson(data)

    monkeypatch.setattr(codec, "loads", refuse)
    monkeypatch.setattr(search_mod.orjson, "loads", refuse_orjson)
    for opts in (SearchOptions.build("toshkent"), SearchOptions.build("1.1", mode="number"),
                 SearchOptions.build("tosh", scope="keys", path="**")):
        res = check_search(doc, opts)
        assert 1 in list(res.records)
    res = search(doc, SearchOptions.build("toshkent"))
    assert [(h.record, h.path) for h in res.hits] == [(0, ("a",)), (1, ("refuse", 0)), (1, ("refuse", 1, "toshkent")),
                                                     (2, ("b", 0))]


@pytest.mark.slow
def test_records_too_deep_for_the_decoder_are_streamed(make_doc) -> None:
    depth = jsongen.TOO_DEEP
    deep = b"[" * depth + b'"Toshkent"' + b"]" * depth
    raw = b'[{"a": "toshkent"}, ' + deep + b', {"b": ["toshkent"]}]'
    doc = make_doc([], raw=raw)
    assert doc.record_count == 3
    res = check_search(doc, SearchOptions.build("toshkent"))
    assert [(h.record, len(h.path)) for h in res.hits] == [(0, 1), (1, depth), (2, 2)]
    assert res.hits[1].text == "Toshkent" and set(res.hits[1].path) == {0}


# ----------------------------------------------------------------------------------
# options
# ----------------------------------------------------------------------------------
def test_search_options() -> None:
    opts = SearchOptions.build("Toshkent", mode="plain", case_sensitive=True, whole_word=True, translit=True,
                               scope="keys", path="contact.city")
    assert opts.pattern == parse_pattern("contact.city")
    again = SearchOptions.from_dict(opts.to_dict())
    assert again.to_dict() == opts.to_dict()
    assert json.loads(json.dumps(opts.to_dict())) == opts.to_dict()
    assert SearchOptions.build("x", path="   ").path is None
    assert SearchOptions.build("x", path="  name ").pattern == parse_pattern("name")
    with pytest.raises(ValueError):
        SearchOptions.build("x", scope="everything")
    with pytest.raises(PathSyntaxError):
        SearchOptions.build("x", path="a[")
    with pytest.raises(UserInputError):
        SearchOptions.build("")
    with pytest.raises(QuerySyntaxError):
        SearchOptions.build("(", mode="regex")
    with pytest.raises(TypeError):
        SearchOptions("x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        search(None, "x")  # type: ignore[arg-type]


def test_summary_is_localized(make_doc, lang_en) -> None:
    doc = make_doc(corpus("uz", 20), "array")
    res = search(doc, SearchOptions.build("toshkent"))
    text = res.summary()
    assert "{" not in text and "byte mode" in text and str(res.hit_count) in text
    from jsonreshape.i18n import set_language

    set_language("uz")
    assert "bayt rejimi" in res.summary()
    for key in ("search.where.key", "search.where.value", "search.scope.both", "search.scope.keys",
                "search.scope.values", "search.mode.parse"):
        assert t(key) != key


# ----------------------------------------------------------------------------------
# export
# ----------------------------------------------------------------------------------
def _sample_hits() -> list[SearchHit]:
    return [
        SearchHit(0, ("contact", "address", "city"), "value", "Toshkent", ((0, 8),)),
        SearchHit(3, (), "key", "Toshkent-1", ((0, 8),), "Toshkent-1"),
        SearchHit(7, ("tags", 2), "value", 'multi\nline, "quoted";', ((0, 5),)),
        SearchHit(9, ("a b", "0", 1), "value", "lone \ud800 surrogate", ((0, 4),)),
        SearchHit(12, ("Тошкент",), "key", "Тошкент", ((0, 7),)),
    ]


def test_export_csv(tmp_path: Path) -> None:
    hits = _sample_hits()
    out = tmp_path / "hits.csv"
    assert export_hits(iter(hits), out) == len(hits)
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\r\n") >= len(hits) + 1
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    assert rows[0] == ["record", "key", "path", "where", "text"]
    assert rows[1] == ["0", "", "contact.address.city", "value", "Toshkent"]
    assert rows[2] == ["3", "Toshkent-1", "$", "key", "Toshkent-1"]
    assert rows[3] == ["7", "", "tags[2]", "value", 'multi\nline, "quoted";']
    assert rows[4] == ["9", "", '["a b"]["0"][1]', "value", "lone \\ud800 surrogate"]
    assert rows[5] == ["12", "", "Тошкент", "key", "Тошкент"]
    assert parse_path(rows[4][2]) == ("a b", "0", 1)
    plain = tmp_path / "plain.csv"
    export_hits(hits[:2], plain, "CSV", bom=False, delimiter=";")
    data = plain.read_bytes()
    assert data.startswith(b"record;key;path;where;text\r\n")


def test_export_json_round_trip(tmp_path: Path) -> None:
    hits = _sample_hits()
    out = tmp_path / "hits.json"
    progress = Progress()
    assert export_hits(hits, out, "json", progress=progress) == len(hits)
    assert progress.finished and progress.done == out.stat().st_size
    data = json.loads(out.read_bytes().decode("utf-8"))
    assert data == [h.to_dict() for h in hits]
    back = [SearchHit(d["record"], parse_path(d["path"]) if d["path"] != "$" else (), d["where"], d["text"],
                      tuple(tuple(s) for s in d["spans"]), d["key"]) for d in data]
    assert back == hits
    empty = tmp_path / "empty.json"
    export_hits([], empty, ".json")
    assert json.loads(empty.read_bytes()) == []


def test_export_search_results(make_doc, tmp_path: Path) -> None:
    doc = make_doc(corpus("uz", 40), "keyed")
    res = search(doc, SearchOptions.build("toshkent"))
    out = tmp_path / "res.json"
    export_hits(res.hits, out, "json")
    data = json.loads(out.read_bytes())
    assert len(data) == len(res.hits) and data[0]["key"] == res.hits[0].key is not None


def test_iter_hits_streams_every_hit(make_doc, tmp_path: Path) -> None:
    doc = make_doc(corpus("uz", 60), "keyed")
    opts = SearchOptions.build("o")
    ref = reference_search(doc, opts)
    progress = Progress()
    it = search_mod.iter_hits(doc, opts, progress=progress, block_bytes=1024)
    assert list(it) == ref and progress.finished
    assert search(doc, opts, max_hits=5).hits == ref[:5]
    out = tmp_path / "all.csv"
    assert export_hits(search_mod.iter_hits(doc, opts), out) == len(ref)
    rows = list(csv.reader(io.StringIO(out.read_bytes().decode("utf-8-sig"), newline="")))
    assert [r[0] for r in rows[1:]] == [str(h.record) for h in ref]
    assert list(search_mod.iter_hits(doc, opts, source=[3, 1])) == reference_search(doc, opts, [3, 1])
    with pytest.raises(TypeError):
        search_mod.iter_hits(doc, "o")  # type: ignore[arg-type]  # checked before iterating
    token = CancelToken()
    gen = search_mod.iter_hits(doc, opts, cancel=token)
    next(gen)
    token.cancel()
    with pytest.raises(Cancelled):
        list(gen)


def test_export_errors(tmp_path: Path) -> None:
    with pytest.raises(UserInputError) as info:
        export_hits([], tmp_path / "x.xlsx", "xlsx")
    assert info.value.key == "search.export.bad_format"
    target = tmp_path / "keep.csv"
    target.write_bytes(b"old")
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        export_hits(_sample_hits(), target, cancel=token)
    assert target.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.csv"]


# ----------------------------------------------------------------------------------
# history
# ----------------------------------------------------------------------------------
def test_history_basics(_isolated_app_home: Path) -> None:
    hist = SearchHistory()
    assert hist.path == _isolated_app_home / "search_history.json"
    assert hist.list() == []
    hist.add("Toshkent", {"mode": "plain"})
    hist.add("O'zbekiston")
    hist.add("> 1000", SearchOptions.build("> 1000", mode="number").to_dict())
    assert hist.texts() == ["> 1000", "O'zbekiston", "Toshkent"]
    hist.add("Toshkent", {"mode": "plain", "translit": True})
    entries = SearchHistory().list()  # persisted
    assert [e["text"] for e in entries] == ["Toshkent", "> 1000", "O'zbekiston"]
    assert entries[0]["options"] == {"mode": "plain", "translit": True}
    assert entries[0]["time"] >= entries[1]["time"]
    assert SearchOptions.from_dict(entries[1]["options"]).query.mode == "number"
    hist.add("   ")
    assert len(hist.list()) == 3
    assert hist.remove("> 1000") and not hist.remove("> 1000")
    hist.clear()
    assert hist.list() == [] and hist.path.exists()
    with pytest.raises(TypeError):
        hist.add("x", {"bad": object()})
    with pytest.raises(TypeError):
        hist.add(5)  # type: ignore[arg-type]


def test_history_cap_and_corrupt_file(tmp_path: Path) -> None:
    assert HISTORY_MAX == 200
    p = tmp_path / "h.json"
    hist = SearchHistory(p, max_entries=5)
    for i in range(8):
        hist.add(f"q{i}")
    assert hist.texts() == ["q7", "q6", "q5", "q4", "q3"]
    p.write_bytes(b"{not json")
    assert hist.list() == []
    hist.add("again")
    assert hist.texts() == ["again"]
    p.write_bytes(json.dumps({"version": 1, "entries": [{"text": "a"}, "junk", {"text": ""}, {"text": "a"},
                                                         {"text": "b", "options": [], "time": "x"}]}).encode())
    assert hist.list() == [{"text": "a", "options": {}, "time": 0.0}, {"text": "b", "options": {}, "time": 0.0}]
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"entries": [{"text": "\ud800x"}]}).encode())
    assert hist.texts() == ["\ud800x"]
    hist.add("\udc00y")
    assert SearchHistory(p).texts() == ["\udc00y", "\ud800x"]


# ----------------------------------------------------------------------------------
# benchmark (excluded by default: pytest -m bench)
# ----------------------------------------------------------------------------------
@pytest.mark.bench
def test_bench_byte_mode_throughput(tmp_path: Path) -> None:
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    out = tmp_path / "syn.json"
    subprocess.run([sys.executable, str(root / "bench" / "make_synthetic.py"), "--size", "200MB", "--out", str(out),
                    "-q"], check=True)
    with Document.open(out) as doc:
        doc.build_index()
        first: list[float] = []
        import time

        t0 = time.perf_counter()
        res = search(doc, SearchOptions.build("Qoraqalpog'iston-maxsus"),
                     on_hits=lambda b: first.append(time.perf_counter() - t0) if b and not first else None)
        assert res.mode_used == "bytes" and res.hit_count >= 1
        assert res.mb_per_s >= 100, res.summary()
        assert first and first[0] < 1.0
        res.close()


def test_module_has_no_print_or_eval() -> None:
    src = Path(search_mod.__file__).read_text(encoding="utf-8")
    assert "print(" not in src and "eval(" not in src and "exec(" not in src
    assert os.path.basename(search_mod.__file__) == "search.py"
    assert FLAG_ESCAPES == 1
