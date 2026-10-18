"""Byte-mode search through ``MultiFinder.locate_tokens`` (jsonreshape/search.py "The
locator", docs/ARCHITECTURE.md §1.5).

The locator confirms candidate records from their located tokens instead of parsing them;
it must give exactly the hits of the parse-and-walk confirm step. Checked on every core
backend (``search.LOCATE`` is forced: by default only the Rust core uses the locator):

* directly -- ``search._byte_block`` against confirming every candidate and every record --
  on random records with duplicate keys, ``\\uXXXX`` / ``\\"`` / ``\\\\`` / ``\\/``
  escapes, Cyrillic, apostrophe variants, exact numbers (``1.10``, ``1e400``, ``-0``),
  member records and nested arrays, also with the big-candidate and long-record limits
  lowered so that the fallbacks interleave with located records;
* through ``search()`` / ``iter_hits()`` against ``reference_search()`` (corpora and
  queries of test_search.py, random documents, truncation inside a batch, first batch);
* the fallbacks one by one: duplicate keys, big candidates, numbers in long or deep
  records, tokens no decoder accepts alone.
"""

from __future__ import annotations

import random
from array import array
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

import jsongen
from jsonreshape import codec, core, search as search_mod
from jsonreshape.core.constants import FLAG_ESCAPES
from jsonreshape.errors import QuerySyntaxError, UserInputError
from jsonreshape.search import SearchOptions, iter_hits, reference_search, search
from jsonreshape.settings import Settings
from test_search import QUERIES, check_search, clean, corpus, make_doc, record_order  # noqa: F401  (make_doc: fixture)

HYP = dict(derandomize=True, database=None, deadline=None,
           suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow,
                                  HealthCheck.data_too_large])
BS = chr(92)  # a backslash (kept out of the string literals of this file)


@pytest.fixture(params=["rust", "python"])
def locate_core(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The search runs with this core backend's MultiFinder and with the locator on."""
    if request.param not in core.available_backends():
        pytest.skip(f"core backend {request.param} unavailable")
    backend = core.get_backend(request.param)
    monkeypatch.setattr(core, "MultiFinder", backend.MultiFinder)
    monkeypatch.setattr(search_mod, "LOCATE", True)
    return backend


def test_locator_is_the_default_with_the_rust_core() -> None:
    assert search_mod.LOCATE == (core.BACKEND == "rust")


# ----------------------------------------------------------------------------------
# random records, encoded by hand (duplicates, escapes, whitespace)
# ----------------------------------------------------------------------------------
INERT = chr(0xE9) + chr(0x20AC) + chr(0x1F600)   # é € 😀: never in a query, so \u-escaping them keeps the needles sound
QUERY_ALPHABET = "aAoOsShHtTkKeEnNiI'" + chr(0x2BB) + chr(0x2019) + "`ўЎшШтТеЕ 01.-" + chr(0xDF) + '"' + BS + "/"
TEXT_ALPHABET = QUERY_ALPHABET + INERT + "\x01\n\tжЖ:,{}[]"
NUMBERS = ["0", "-0", "1", "-1", "0.1", "1.10", "-0.0", "100.0", "1e400", "-1e400", "1E5", "2.5E+3",
           "12345678901234567890", "-9223372036854775809", "123456789.123456789", "0.00001", "1000", "1.1"]

_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(-10**6, 10**6),
    st.sampled_from(NUMBERS).map(codec.loads),
    st.text(TEXT_ALPHABET, max_size=10),
)
_values = st.recursive(
    _scalars,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(TEXT_ALPHABET, max_size=5), children,
                                                                         max_size=4),
    max_leaves=16,
)
_queries = st.builds(
    lambda txt, cs, ww, scope: (txt, {"case_sensitive": cs, "whole_word": ww, "scope": scope}),
    st.text(QUERY_ALPHABET, min_size=1, max_size=4), st.booleans(), st.booleans(),
    st.sampled_from(["both", "both", "keys", "values"]))
_paths = st.sampled_from([None, None, None, "a", "*", "**", "[*]", "**.a", "*.a", "[0]", "a[0]", "$", "**.'",
                          "**[1]"])


def enc_str(s: str, rng: random.Random) -> bytes:
    """A JSON string: mandatory escapes, ``\\/`` and ``\\n`` / ``\\t`` sometimes, the inert
    characters and control characters as ``\\uXXXX`` (surrogate pairs for 😀)."""
    out = ['"']
    for ch in s:
        if ch == '"' or ch == BS:
            out.append(BS + ch)
        elif ch == "/" and rng.random() < 0.3:
            out.append(BS + "/")
        elif ch in "\n\t" and rng.random() < 0.5:
            out.append(BS + ("n" if ch == "\n" else "t"))
        elif ord(ch) < 0x20 or (ch in INERT and rng.random() < 0.5):
            units = ch.encode("utf-16-be")
            out.extend(BS + "u%04x" % int.from_bytes(units[k:k + 2], "big") for k in range(0, len(units), 2))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out).encode("utf-8")


def _ws(rng: random.Random) -> bytes:
    return rng.choice([b"", b"", b"", b" ", b"\n", b"\t", b"\r\n "])


def enc_value(v: Any, rng: random.Random, dups: bool = True) -> bytes:
    """JSON text of ``v`` with random whitespace; objects repeat a key now and then."""
    if isinstance(v, dict):
        items = [(enc_str(k, rng), enc_value(x, rng, dups)) for k, x in v.items()]
        if dups and items and rng.random() < 0.2:  # a duplicate key, possibly spelled differently
            k = rng.choice(list(v))
            items.insert(rng.randint(0, len(items)), (enc_str(k, rng), enc_value(rng.choice([1, "x", None]), rng)))
        inner = b",".join(_ws(rng) + k + _ws(rng) + b":" + _ws(rng) + x + _ws(rng) for k, x in items)
        return b"{" + inner + b"}"
    if isinstance(v, list):
        return b"[" + b",".join(_ws(rng) + enc_value(x, rng, dups) + _ws(rng) for x in v) + b"]"
    if isinstance(v, str):
        return enc_str(v, rng)
    if v is None or v is True or v is False:
        return {None: b"null", True: b"true", False: b"false"}[v]
    return codec.number_text(v).encode()


def layout(encs: list[bytes], keys: list[bytes] | None) -> tuple[bytes, bytes]:
    """``(buf, spans)``: the records of an array, or the members of an object."""
    parts = []
    spans = []
    pos = 1
    for k, e in enumerate(encs):
        rec = e if keys is None else keys[k] + b" : " + e
        spans.append((pos, pos + len(rec)))
        parts.append(rec)
        pos += len(rec) + 2
    buf = (b"[" if keys is None else b"{") + b",\n".join(parts) + (b"]" if keys is None else b"}")
    return buf, array("Q", [x for p in spans for x in p]).tobytes()


class _NoDoc:
    """Stands in for the document: only a streamed record would need one."""

    def record_span(self, i: int) -> Any:  # pragma: no cover - records here are shallow
        raise AssertionError("streamed record")

    record_key = record_span


def flatten(items: Any) -> list[tuple[int, str | None, list]]:
    """``(record, key, raw hits)`` of the matching records among the items of ``_scan`` /
    ``_byte_block`` (a streamed or incremental record may turn out to have no hit)."""
    out = []
    for item in items:
        if item is None:
            continue
        if type(item) is search_mod._RecordBatch:
            assert all(item.raws) and item.hits == sum(map(len, item.raws))
            out.extend(zip(item.indices, item.keys, item.raws))
        else:
            i, key, raw = item
            hits = raw if type(raw) is list else [h for b in raw for h in b]
            if hits:
                out.append((i, key, hits))
    return out


def compare_paths(backend: Any, opts: SearchOptions, buf: bytes, spans: bytes, member: bool) -> list:
    """The locator path, confirming every candidate, and confirming every record: all
    three must give the same hits (the last one also proves the needles are sound for the
    generated data). Returns the hits."""
    needles = opts.query.prefilter_needles(FLAG_ESCAPES)  # \u escapes only hide inert characters
    if not needles:
        return []
    finder = backend.MultiFinder(needles)
    sp = array("Q")
    sp.frombytes(spans)
    n = len(sp) // 2
    indices = array("Q", range(n))

    def confirm(ks: Any) -> list:
        matcher = search_mod._Matcher(opts)
        loads = codec.loads if matcher.numbers_on else search_mod._loads_ignoring_numbers
        return flatten(item for k in ks for item in search_mod._confirm_record(
            _NoDoc(), matcher, k, buf[sp[2 * k]:sp[2 * k + 1]], member, loads, None))

    matcher = search_mod._Matcher(opts)
    loads = codec.loads if matcher.numbers_on else search_mod._loads_ignoring_numbers
    gen = search_mod._byte_block(_NoDoc(), matcher, finder, buf, spans, indices, member, loads, None)
    located = []
    while True:
        try:
            located.append(next(gen))
        except StopIteration as stop:
            parsed = stop.value
            break
    new = flatten(located)
    cand = array("Q")
    cand.frombytes(finder.find_in_spans(buf, spans))
    assert parsed == len(cand)
    old = confirm(cand)
    assert new == old, (buf, needles)
    assert old == confirm(range(n)), (buf, needles)
    return new


def _texts(v: Any) -> list[str]:
    """Keys, strings and number texts of a value."""
    if isinstance(v, dict):
        return [t for k, x in v.items() for t in [k, *_texts(x)]]
    if isinstance(v, list):
        return [t for x in v for t in _texts(x)]
    if isinstance(v, str):
        return [v]
    return [] if v is None or isinstance(v, bool) else [codec.number_text(v)]


def _query_text(text: str, recs: list, rng: random.Random) -> str:
    """Half of the time a piece of the data (so that there are hits), else ``text``."""
    pool = [t for v in recs for t in _texts(v) if t]
    if pool and rng.random() < 0.5:
        s = rng.choice(pool)
        a = rng.randrange(len(s))
        piece = "".join(c for c in s[a:a + rng.randint(1, 5)] if c in QUERY_ALPHABET)
        if piece:
            return piece
    return text


@hsettings(max_examples=120, **HYP)
@given(recs=st.lists(_values, min_size=1, max_size=8), query=_queries, path=_paths, member=st.booleans(),
       limits=st.sampled_from([None, None, "big", "numbers"]), seed=st.integers(0, 2**32 - 1))
def test_locator_equals_confirm_step(locate_core, monkeypatch, recs: list, query: tuple, path: str | None,
                                     member: bool, limits: str | None, seed: int) -> None:
    rng = random.Random(seed)
    text, kw = query
    try:
        opts = SearchOptions.build(_query_text(text, recs, rng), path=path, **kw)
    except (UserInputError, QuerySyntaxError):
        return
    encs = [enc_value(v, rng) for v in recs]
    keys = [enc_str(rng.choice(["a", "k" + INERT, "o'", "x" + str(i)]), rng) for i in range(len(encs))] \
        if member else None
    buf, spans = layout(encs, keys)
    # lowered limits: candidates beyond them are confirmed by parsing, in between located ones
    monkeypatch.setattr(search_mod, "INCREMENTAL_BYTES", 40 if limits == "big" else 1 << 20)
    monkeypatch.setattr(search_mod, "LOCATE_NUMBER_BYTES", 12 if limits == "numbers" else 1000)
    compare_paths(locate_core, opts, buf, spans, member)


def test_locator_fixed_examples(locate_core) -> None:
    """Hand-picked records through ``compare_paths`` (and the hits they give)."""
    rng = random.Random(1)
    e_acute = chr(0xE9)
    recs = [
        {"city": "Toshkent", "street": "Amir Temur ko'chasi", "n": codec.loads("1.10")},
        {"a": [["toshkent", {"toshkent": codec.loads("-0")}]], "TOSHKENT": None},
        {"esc": 'q"uote ' + BS + " toshkent/x", "k" + e_acute: ["ko" + chr(0x2BB) + "chasi", "Тошкент"]},
        [1, [2, [3, ["toshkentlik"]]]],
        "toshkent",
        codec.loads("1e400"),
    ]
    encs = [enc_value(v, rng, dups=False) for v in recs]
    encs.append(b'{"toshkent": 1, "' + BS.encode() + b'u0074oshkent": "x", "b": "Toshkent"}')  # duplicate key
    buf, spans = layout(encs, None)
    hits = compare_paths(locate_core, SearchOptions.build("toshkent"), buf, spans, False)
    assert [(i, [(p, w) for p, w, *_ in raw]) for i, _, raw in hits] == [
        (0, [(("city",), "value")]),
        (1, [(("a", 0, 0), "value"), (("a", 0, 1, "toshkent"), "key"), (("TOSHKENT",), "key")]),
        (2, [(("esc",), "value")]),
        (3, [((1, 1, 1, 0), "value")]),
        (4, [((), "value")]),
        (6, [(("toshkent",), "key"), (("b",), "value")]),  # dict semantics: one "toshkent" key
    ]
    for text, kw in [("ko'chasi", {}), ("KO'CHASI", {"case_sensitive": True}), ("1", {}), ("-0", {}),
                     ("e4", {}), ("null", {}), ("toshkent", {"scope": "keys"}), ("toshkent", {"path": "a[*]"}),
                     ("o", {"whole_word": True}), (BS, {}), ('"', {}), ("/x", {})]:
        compare_paths(locate_core, SearchOptions.build(text, **kw), buf, spans, False)
    keys = [enc_str(k, rng) for k in ("toshkent-1", "x", "Toshkent" + e_acute, "toshkent", "y", "z", "w")]
    buf, spans = layout(encs, keys)
    hits = compare_paths(locate_core, SearchOptions.build("toshkent"), buf, spans, True)
    assert [(i, k) for i, k, _ in hits] == [(0, "toshkent-1"), (1, "x"), (2, "Toshkent" + e_acute), (3, "toshkent"),
                                           (4, "y"), (6, "w")]
    assert hits[0][2][0] == ((), "key", "toshkent-1", ((0, 8),))


# ----------------------------------------------------------------------------------
# through search(): the corpora and queries of test_search.py, random documents
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["array", "jsonl", "keyed", "wrapped"])
@pytest.mark.parametrize("name", ["gen", "clean", "uz"])
def test_search_with_locator_equals_reference(locate_core, make_doc, name: str, fmt: str) -> None:
    doc = make_doc(corpus(name), fmt)
    for text, kw in QUERIES:
        check_search(doc, SearchOptions.build(text, **kw))


@hsettings(max_examples=40, **HYP)
@given(recs=st.lists(_values, min_size=1, max_size=10), query=_queries, path=_paths,
       fmt=st.sampled_from(["array", "jsonl", "keyed"]), seed=st.integers(0, 2**32 - 1))
def test_property_search_with_locator(locate_core, make_doc, recs: list, query: tuple, path: str | None, fmt: str,
                                      seed: int) -> None:
    rng = random.Random(seed)
    text, kw = query
    text = _query_text(text, recs, rng)
    try:
        opts = SearchOptions.build(text, path=path, **kw)
    except (UserInputError, QuerySyntaxError):
        return
    # no \u escapes in the file (byte mode); duplicate keys, other escapes and whitespace stay
    encs = [enc_value(_without_inert(v), rng) for v in recs]
    if fmt == "array":
        raw = b"[" + b",\n".join(encs) + b"]"
    elif fmt == "jsonl":
        raw = b"".join(e.replace(b"\n", b" ").replace(b"\r", b" ") + b"\n" for e in encs)
    else:
        raw = b"{" + b",\n".join(enc_str(f"k{i}-{text}", rng) + b": " + e for i, e in enumerate(encs)) + b"}"
    if BS.encode() + b"u" in raw:
        return  # a control character needed a \u escape: no byte mode for this file
    doc = make_doc([], raw=raw)
    res = check_search(doc, opts)
    assert res.mode_used == ("bytes" if opts.query.prefilter_needles(doc.flags) else "parse")
    assert [h for h in iter_hits(doc, opts)] == res.hits


def _without_inert(v: Any) -> Any:
    if isinstance(v, str):
        return "".join(c for c in v if c not in INERT and ord(c) >= 0x20 or c in "\n\t")
    if isinstance(v, dict):
        return {_without_inert(k): _without_inert(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_without_inert(x) for x in v]
    return v


def test_truncation_inside_a_batch_and_first_batch(locate_core, make_doc, monkeypatch) -> None:
    recs = jsongen.records(600, seed=5)
    for i in range(0, 600, 3):
        recs[i]["note"] = f"Toshkent {i}"
        recs[i]["tags"] = ["toshkent", "TOSHKENT"]
    doc = make_doc(recs, "array")
    opts = SearchOptions.build("toshkent")
    ref = reference_search(doc, opts)
    assert len(ref) > 500
    for max_hits in (0, 1, 7, 333, len(ref) - 1, None):
        res = search(doc, opts, max_hits=max_hits)
        stored = ref if max_hits is None else ref[:max_hits]
        assert res.hits == stored and res.hit_count == len(ref) and res.truncated == (len(stored) < len(ref))
        assert list(res.records) == record_order(ref) and res.record_count == len(record_order(ref))
    assert list(iter_hits(doc, opts)) == ref
    # the block's first matching record is handed over alone: its hits are shown at once
    batches: list = []
    monkeypatch.setattr(search_mod, "EMIT_INTERVAL", 3600.0)
    search(doc, opts, on_hits=batches.append)
    assert {h.record for h in batches[0]} == {ref[0].record}
    assert [h for b in batches for h in b] == ref


# ----------------------------------------------------------------------------------
# the fallbacks to the confirm step
# ----------------------------------------------------------------------------------
def test_duplicate_keys_are_confirmed_by_parsing(locate_core, make_doc) -> None:
    raw = (b'[{"a": "toshkent 1", "b": {"c": "x", "c": "Toshkent"}, "a": "toshkent 2"},'
           b' {"toshkent": 1, "TOSHKENT": 2, "toshkent": [3, "toshkent"]}, {"z": "toshkent"}]')
    doc = make_doc([], raw=raw)
    res = check_search(doc, SearchOptions.build("toshkent"))
    assert [(h.record, h.path, h.where) for h in res.hits] == [
        (0, ("a",), "value"), (0, ("b", "c"), "value"),
        (1, ("toshkent",), "key"), (1, ("toshkent", 1), "value"), (1, ("TOSHKENT",), "key"),
        (2, ("z",), "value")]
    assert res.hits[0].text == "toshkent 2" and res.records_parsed == 3


def test_big_candidates_between_located_ones(locate_core, make_doc, monkeypatch) -> None:
    recs = jsongen.records(120, seed=9)
    for i in range(0, 120, 4):
        recs[i]["note"] = "Toshkent " + "p" * (i * 7)
    doc = make_doc(recs, "array")
    monkeypatch.setattr(search_mod, "INCREMENTAL_BYTES", 400)
    sizes = [doc.record_span(i)[1] - doc.record_span(i)[0] for i in range(0, 120, 4)]
    assert min(sizes) < 400 < max(sizes)
    for text, kw in [("toshkent", {}), ("p", {"scope": "values"}), ("oid", {"scope": "keys"})]:
        check_search(doc, SearchOptions.build(text, **kw))


def test_numbers_in_long_or_deep_records(locate_core, make_doc, monkeypatch) -> None:
    """A located number in a record longer than LOCATE_NUMBER_BYTES is confirmed by parsing:
    such a record could be too deep for codec.loads, and the streaming fallback reads
    numbers differently (1e400 -> 1E+400)."""
    depth = 20000  # deeper than codec.loads accepts: streamed by search and reference alike
    deep = b"[" * depth + b'1e400, -0, "e4"' + b"]" * depth
    raw = b'[{"a": 1e400, "b": [-0, 1.10]}, ' + deep + b', {"c": "1e400"}]'
    doc = make_doc([], raw=raw)
    # record 1 is streamed: its "-0" reads as 0 and its 1e400 as 1E+400 (only the string "e4" matches)
    expected = {"e4": [0, 1, 2], "-0": [0], "1.1": [0], "e": [0, 1, 2]}
    for text, records in expected.items():
        assert list(check_search(doc, SearchOptions.build(text)).records) == records, text
    monkeypatch.setattr(search_mod, "LOCATE_NUMBER_BYTES", 10)  # record 0 is parsed as well
    for text, records in expected.items():
        assert list(check_search(doc, SearchOptions.build(text)).records) == records, text


def test_tokens_no_decoder_accepts_alone(locate_core, make_doc, monkeypatch) -> None:
    """A string token the decoders refuse sends its record to the confirm step (which
    streams it when the whole record is refused too), in order with the others."""
    raw = b'[{"a": "toshkent"}, {"refuse": ["Toshkent", {"toshkent": "refuse toshkent"}]}, {"b": ["toshkent"]}]'
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
    res = check_search(doc, SearchOptions.build("toshkent"))
    assert [(h.record, h.path) for h in res.hits] == [
        (0, ("a",)), (1, ("refuse", 0)), (1, ("refuse", 1, "toshkent")), (1, ("refuse", 1, "toshkent")),
        (2, ("b", 0))]


def test_refused_blocks_are_confirmed_by_parsing(locate_core, make_doc, monkeypatch) -> None:
    """``locate_tokens`` refusing a block (ValueError: a span that is no longer valid
    JSON) sends the block's candidates to the confirm step, in order."""
    real = locate_core.MultiFinder
    calls = []

    class Refusing:
        def __init__(self, needles: list[bytes]) -> None:
            self._m = real(needles)
            self.find_in_spans = self._m.find_in_spans
            self.find_first = self._m.find_first

        def locate_tokens(self, buf: bytes, spans: bytes, member: bool = False) -> list:
            calls.append(len(spans) // 16)
            if len(calls) % 2:
                raise ValueError("simulated: invalid JSON in a span")
            return self._m.locate_tokens(buf, spans, member)

    monkeypatch.setattr(core, "MultiFinder", Refusing)
    recs = clean(jsongen.records(200, seed=4))
    for i in range(0, 200, 3):
        recs[i]["note"] = f"Toshkent {i}"
    for fmt in ("array", "keyed"):
        doc = make_doc(recs, fmt)
        calls.clear()
        res = check_search(doc, SearchOptions.build("toshkent"))
        assert res.mode_used == "bytes" and res.hit_count > 60 and len(calls) >= 2


def test_member_records_and_huge_records(locate_core, make_doc) -> None:
    recs = [{"name": "Toshkent", "pad": "x" * (400 if i % 3 == 0 else 5)} for i in range(12)]
    keys = [f"Toshkent-{i}" if i % 2 else f"k{i}" for i in range(12)]
    doc = make_doc(recs, "keyed", keys=keys, settings=Settings(large_node_bytes=300))
    assert doc.member_records and any(doc.is_huge(i) for i in range(12))
    for text, kw in [("toshkent", {}), ("toshkent", {"scope": "keys"}), ("toshkent", {"path": "name"}),
                     ("x", {"path": "$"}), ("k1", {})]:
        check_search(doc, SearchOptions.build(text, **kw))
