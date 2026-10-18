"""Regression tests of the query workstream's second fix round (phase 5d findings).

* robustness/N14 -- :mod:`jsonreshape.stream` feeds ijson in adaptive chunks: a long
  string is read in growing chunks (no quadratic re-lexing), the chunk that completes it
  is cut right after it (bounded events per chunk), and the events equal ijson's own;
* requirements/N06 -- ``--translit`` keeps both Latin spellings of ``ц``: Latin ``ts``
  folds to ``s`` (exact index maps; a needle ending in ``t`` still finds ``ts`` words);
* requirements/N07 -- filter presets saved by several processes at once are all kept
  (inter-process lock); robustness/N15 -- presets and search history create their folder
  with ``make_dirs`` (long paths) and raise localized errors;
* blackbox/N11 -- a filter that fails on every record stops with a localized error;
* blackbox/N13 -- sort progress never goes back, never exceeds its total and never shows
  "100 %, 0.0 s left" before the merge;
* codereview/N03, blackbox/F16 -- English warnings and summaries with a count of 1;
* blackbox/F14 -- localized JMESPath lexer errors (the English text only as a detail).
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import ijson
import pytest

import jsongen
from jsonreshape import cli, i18n
from jsonreshape import recordset as rsmod
from jsonreshape import stream as stream_mod
from jsonreshape import textmatch as tm
from jsonreshape.appdirs import recordsets_dir
from jsonreshape.errors import JsonReshapeError, QuerySyntaxError, UserInputError
from jsonreshape.i18n import t
from jsonreshape.query import Filter, FilterPresets, compile_filter, file_lock
from jsonreshape.reader import Document
from jsonreshape.recordset import filter_records, sort_records
from jsonreshape.schema import SchemaBuilder
from jsonreshape.search import SearchHistory, SearchOptions, reference_search, search
from jsonreshape.tasks import Progress
from jsonreshape.textmatch import APOSTROPHES, TextQuery, fold

ROOT = Path(__file__).resolve().parents[1]
MiB = 1 << 20


@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []

    def make(recs: list[Any] | None = None, fmt: str = "array", *, raw: bytes | None = None, **kw: Any) -> Document:
        p = tmp_path / f"doc{len(docs)}.json"
        if raw is not None:
            p.write_bytes(raw)
        else:
            jsongen.write_file(p, recs or [], fmt, **kw)
        doc = Document.open(p, format=None if raw is not None else fmt)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


# ==================================================================================
# robustness/N14: adaptive ijson chunks
# ==================================================================================
def _span_bytes(doc: Document, i: int) -> bytes:
    s, e = doc.record_span(i)
    with open(doc.data_path, "rb") as fh:
        fh.seek(s)
        return fh.read(e - s)


@pytest.mark.parametrize("piece", ["Toshkent Samarqand ", "1 2 3 4 5 ", "1234567890", "1,22,333:", 'a\\"b\\\\'])
def test_n14_a_long_string_is_read_in_growing_chunks(make_doc, monkeypatch, piece: str) -> None:
    text = piece * (4 * MiB // len(piece))
    doc = make_doc(raw=json.dumps([{"id": 1, "text": text}, {"id": 2, "text": "end"}]).encode())
    sizes: list[int] = []
    real_read = stream_mod._RangeReader.read

    def read(self: Any, n: int = -1) -> bytes:
        data = real_read(self, n)
        sizes.append(len(data))
        return data

    monkeypatch.setattr(stream_mod._RangeReader, "read", read)
    got = list(stream_mod.iter_events(doc, doc.record_span(0)))
    assert got == list(ijson.basic_parse(io.BytesIO(_span_bytes(doc, 0)), use_float=False))
    # 64 KiB, then doubling while the string is open: a handful of reads, not one per
    # 64 KiB (each of which made yajl re-lex the whole string read so far)
    assert len(sizes) <= 12 and max(sizes) <= stream_mod.BUF_MAX, sizes


def test_n14_the_chunk_that_ends_a_long_string_holds_nothing_after_it(make_doc, monkeypatch) -> None:
    n = 300_000
    raw = b'[{"t":"' + b"x" * (3 * MiB) + b'","n":[' + b",".join([b"1"] * n) + b"]}]"
    doc = make_doc(raw=raw)
    per_send: list[tuple[int, int]] = []   # (chunk bytes, events it produced)
    real = ijson.basic_parse_coro

    def spy(target: Any, **kw: Any) -> Any:
        coro = real(target, **kw)

        class Proxy:
            def send(self, chunk: Any) -> None:
                before = len(target)
                coro.send(chunk)
                per_send.append((len(chunk), len(target) - before))

            def close(self) -> None:
                coro.close()

        return Proxy()

    monkeypatch.setattr(stream_mod.ijson, "basic_parse_coro", spy)
    assert sum(1 for _ in stream_mod.iter_events(doc, doc.record_span(0))) == n + 7
    big = [ev for size, ev in per_send if size > stream_mod.BUF_SIZE]
    assert big and max(big) <= 1, per_send   # a grown chunk only completes the string
    # never more events at once than a 64 KiB chunk holds (the memory bound of the
    # earlier fix: ijson turns a whole chunk into event tuples at once)
    assert max(ev for _, ev in per_send) <= stream_mod.BUF_SIZE // 2


def test_n14_member_records_with_a_long_string(make_doc) -> None:
    long_text = "a\\\"b" * 300_000          # escaped quotes all along the string
    raw = ('{"k1": {"t": "' + long_text + '", "n": 1}, "k2": {"t": "x"}}').encode()
    doc = make_doc(raw=raw)
    assert doc.member_records
    got = list(stream_mod.iter_events(doc, doc.record_span(0), member=True))
    member = _span_bytes(doc, 0)
    want = list(ijson.basic_parse(io.BytesIO(b"{" + member + b"}"), use_float=False))
    assert got == want


_CHARS = ['a', ' ', '"', '\\', '/', '\n', '1', 'e', '{', ',', ':', chr(0x410), chr(0xE9), chr(0x1F600)]


def _rand_string(rng: random.Random) -> str:
    n = rng.choice([0, 1, 5, 40, 300, 2000])
    return "".join(rng.choice(_CHARS) for _ in range(n))


def _rand_value(rng: random.Random, depth: int = 0) -> Any:
    r = rng.random()
    if depth > 3 or r < 0.3:
        k = rng.random()
        if k < 0.45:
            return _rand_string(rng)
        if k < 0.7:
            return rng.choice([0, -1, 12345678901234567890, 1.5, -2.25e-7, 10 ** 30, 7])
        return rng.choice([True, False, None])
    if r < 0.65:
        return [_rand_value(rng, depth + 1) for _ in range(rng.randint(0, 6))]
    return {_rand_string(rng)[:20]: _rand_value(rng, depth + 1) for _ in range(rng.randint(0, 6))}


def _events_or_error(make: Any) -> tuple[str, Any]:
    try:
        return "ok", list(make())
    except Exception as exc:  # noqa: BLE001 - compared by type
        return "error", type(exc).__name__


@pytest.mark.parametrize("seed", range(3))
def test_n14_adaptive_chunks_give_ijsons_events(seed: int, monkeypatch) -> None:
    rng = random.Random(seed)
    for _ in range(300):
        text = json.dumps(_rand_value(rng), ensure_ascii=rng.random() < 0.3, indent=rng.choice([None, None, 1, 30]))
        if rng.random() < 0.2:
            text = " " * rng.randint(0, 200) + text + " " * rng.randint(0, 200)
        data = text.encode("utf-8")
        if rng.random() < 0.1 and len(data) > 2:
            data = data[: rng.randint(1, len(data) - 1)]   # truncated: both fail alike
        size = rng.choice([1, 2, 3, 7, 64])
        monkeypatch.setattr(stream_mod, "BUF_SIZE", size)
        monkeypatch.setattr(stream_mod, "BUF_MAX", size * rng.choice([1, 2, 64, 1024]))
        want = _events_or_error(lambda: ijson.basic_parse(io.BytesIO(data), use_float=False, buf_size=1 << 20))
        got = _events_or_error(lambda: stream_mod._parse(stream_mod._RangeReader(io.BytesIO(data), 0, len(data))))
        assert got == want, text[:200]


# ==================================================================================
# requirements/N06: both Latin spellings of ц
# ==================================================================================
TS_PAIRS = [
    ("sirk", "цирк"), ("tsirk", "цирк"), ("konsert", "концерт"), ("kontsert", "концерт"),
    ("stansiya", "станция"), ("stantsiya", "станция"), ("militsiya", "милиция"), ("milisiya", "милиция"),
    ("sex", "цех"), ("tsex", "цех"), ("funksiya", "функция"), ("funktsiya", "функция"),
    ("revolyutsiya", "революция"), ("revolyusiya", "революция"),
]


@pytest.mark.parametrize("latin, cyrillic", TS_PAIRS)
def test_n06_both_spellings_match_both_ways(latin: str, cyrillic: str) -> None:
    for needle, hay in ((latin, cyrillic), (cyrillic, latin)):
        for variant in (hay, hay.upper(), hay.title()):
            assert TextQuery(needle, translit=True).find_spans(f"« {variant} »") == [(2, 2 + len(variant))]
    other = latin.replace("ts", "s") if "ts" in latin else None
    if other is not None:  # the two Latin spellings find each other as well
        assert TextQuery(latin, translit=True).find_spans(other) == [(0, len(other))]
        assert TextQuery(other, translit=True).find_spans(latin) == [(0, len(latin))]
    assert TextQuery(latin).find_spans(cyrillic) == []   # translit off: scripts stay apart


def test_n06_merged_ts_keeps_exact_index_maps_and_spans() -> None:
    assert fold("kontsert", translit=True) == ("konsert", [0, 1, 2, 3, 5, 6, 7, 8])
    assert fold("Tsirk", translit=True) == ("sirk", [0, 2, 3, 4, 5])
    assert fold("отцы", translit=True)[0] == "osi"

    def spans(needle: str, hay: str, **kw: Any) -> list[tuple[int, int]]:
        return TextQuery(needle, translit=True, **kw).find_spans(hay)

    assert spans("sirk", "atsirk") == [(1, 6)]          # the match covers both letters
    assert spans("kons", "kontsert") == [(0, 5)]
    assert spans("nse", "kontsert") == [(2, 6)]
    # a needle ending in "t" still finds the t of a merged pair; the match ends after it
    assert spans("kont", "kontsert") == [(0, 4)]
    assert spans("Toshkent", "Toshkentskaya ko'chasi") == [(0, 8)]
    assert spans("student", "students") == [(0, 7)]
    assert spans("student", "students", whole_word=True) == []
    assert spans("t", "ts t") == [(0, 1), (3, 4)]
    assert spans("ts", "tts") == [(1, 3)]
    assert spans("kont", "концерт") == []                # ц is one letter: no t to end on


def test_n06_case_sensitive_merges_only_pairs_of_one_case() -> None:
    def spans(needle: str, hay: str) -> list[tuple[int, int]]:
        return TextQuery(needle, translit=True, case_sensitive=True).find_spans(hay)

    assert spans("TSIRK", "ЦИРК") == [(0, 4)]
    assert spans("ЦИРК", "TSIRK") == [(0, 5)]
    assert spans("Sirk", "Цирк") == [(0, 4)]
    assert spans("tsirk", "цирк") == [(0, 4)]
    assert spans("sirk", "Tsirk") == [(1, 5)]            # a mixed pair stays: the literal "sirk" is found
    assert spans("Tsirk", "Цирк") == []                  # documented


TS_ALPHABET = "tsTSтсТСцЦa оеьъ'ʻhҳ1"
VANISH = "ьЬъЪ" + APOSTROPHES


def _needle_and_hay(rng: random.Random) -> tuple[str, str]:
    needle = "".join(rng.choice(TS_ALPHABET) for _ in range(rng.randint(1, 4)))
    hay = "".join(rng.choice(TS_ALPHABET) for _ in range(rng.randint(0, 8)))
    return needle, hay + needle + "".join(rng.choice(TS_ALPHABET) for _ in range(rng.randint(0, 8)))


def test_n06_literal_occurrences_are_found_and_spans_are_aligned() -> None:
    rng = random.Random(11)
    for _ in range(1500):
        needle, hay = _needle_and_hay(rng)
        if all(c in VANISH for c in needle):
            continue  # documented: a needle of characters that may fold to nothing
        for kw in ({"translit": True}, {"translit": True, "case_sensitive": True}):
            found = TextQuery(needle, **kw).find_spans(hay)
            assert found, (needle, hay, kw)
            last = 0
            for a, b in found:
                assert last <= a < b <= len(hay), (needle, hay, kw, found)
                last = b


def test_n06_huge_strings_are_matched_window_by_window_alike(monkeypatch) -> None:
    rng = random.Random(5)
    for _ in range(500):
        needle, hay = _needle_and_hay(rng)
        hay = hay * rng.randint(1, 4)
        for kw in ({"translit": True}, {"translit": True, "case_sensitive": True},
                   {"translit": True, "whole_word": True}):
            q = TextQuery(needle, **kw)
            monkeypatch.setattr(tm, "HUGE_TEXT", 1 << 30)
            monkeypatch.setattr(tm, "WINDOW_CHARS", 1 << 30)
            want = q.find_spans(hay)
            monkeypatch.setattr(tm, "HUGE_TEXT", 0)
            monkeypatch.setattr(tm, "WINDOW_CHARS", rng.randint(1, 20))
            assert q.find_spans(hay) == want, (needle, hay, kw)


def test_n06_ascii_fast_path_folds_alike() -> None:
    rng = random.Random(3)
    samples = ["tsirk", "TSIRK", "Tsirk", "tts", "ts's'h", "a`ts", "s'hts", "Botswana", ""]
    samples += ["".join(rng.choice("tsTSa'`hH ") for _ in range(rng.randint(0, 12))) for _ in range(500)]
    for s in samples:
        for casefold in (True, False):
            assert tm.translit_fold_ascii(s, casefold) == fold(s, casefold=casefold, translit=True)[0], s


def test_n06_search_with_translit_finds_both_spellings(make_doc) -> None:
    names = ["цирк", "tsirk", "sirk", "kontsert", "концерт", "Toshkentskaya", "Toshkent", "students"]
    doc = make_doc([{"name": n} for n in names])
    for needle in ("tsirk", "sirk", "kont", "konsert", "Toshkent", "student"):
        q = TextQuery(needle, translit=True)
        want = [i for i, n in enumerate(names) if q.find_spans(n)]
        res = search(doc, SearchOptions.build(needle, translit=True))
        assert [h.record for h in res.hits] == want, needle
        assert res.hits == reference_search(doc, SearchOptions.build(needle, translit=True))
        res.close()
    q = TextQuery("tsirk", translit=True)
    assert [n for n in names if q.find_spans(n)] == ["цирк", "tsirk", "sirk"]


# ==================================================================================
# requirements/N07, robustness/N15: presets and search history
# ==================================================================================
_SAVER = """
import sys
sys.path.insert(0, sys.argv[1])
from jsonreshape.query import FilterPresets
from jsonreshape.search import SearchHistory
k, n = int(sys.argv[4]), int(sys.argv[5])
presets, history = FilterPresets(sys.argv[2]), SearchHistory(sys.argv[3])
for j in range(n):
    presets.save(f"p{k}_{j}", f"id == {k * 100 + j}")
    history.add(f"q{k}_{j}")
"""


def test_n07_presets_saved_by_several_processes_are_all_kept(tmp_path: Path) -> None:
    script = tmp_path / "saver.py"
    script.write_bytes(_SAVER.encode("utf-8"))
    presets = tmp_path / "home" / "filter_presets.json"
    history = tmp_path / "home" / "search_history.json"
    n_proc, n_each = 6, 8
    procs = [subprocess.Popen([sys.executable, str(script), str(ROOT), str(presets), str(history), str(k),
                               str(n_each)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for k in range(n_proc)]
    for p in procs:
        _, err = p.communicate(timeout=180)
        assert p.returncode == 0, err.decode("utf-8", "replace")
    want = sorted(f"p{k}_{j}" for k in range(n_proc) for j in range(n_each))
    assert sorted(FilterPresets(presets).names()) == want
    assert sorted(SearchHistory(history).texts()) == sorted(w.replace("p", "q") for w in want)
    assert (tmp_path / "home" / "filter_presets.json.lock").exists()   # the lock file stays


def test_n07_file_lock_waits_for_the_holder_and_times_out(tmp_path: Path, lang_en) -> None:
    path = tmp_path / "x.json"
    order: list[str] = []

    def other() -> None:
        with file_lock(path):
            order.append("thread")

    with file_lock(path):
        started = time.monotonic()
        with pytest.raises(JsonReshapeError) as info:
            with file_lock(path, timeout=0.3):
                pass
        assert info.value.key == "query.preset.busy" and time.monotonic() - started >= 0.25
        assert str(path) in info.value.message
        th = threading.Thread(target=other)
        th.start()
        time.sleep(0.2)
        order.append("main")
    th.join(10)
    assert order == ["main", "thread"]
    store = FilterPresets(path)
    store.save("a", "id > 1")
    assert store.delete("a") and not store.delete("a") and store.names() == []


def _long_dir(base: Path) -> Path:
    d = base
    while len(str(d)) < 300:
        d = d / ("d" * 40)
    return d


def test_n15_presets_and_history_create_long_folders(tmp_path: Path, monkeypatch) -> None:
    home = _long_dir(tmp_path) / "home"   # does not exist yet
    real_mkdir = os.mkdir

    def mkdir(p: Any, *args: Any, **kwargs: Any) -> None:
        # a process that is not long-path aware (the Windows default): a plain path over
        # MAX_PATH fails even when the folder exists; the \\?\ form works
        s = os.fspath(p)
        if len(s) >= 260 and not s.startswith("\\\\?\\"):
            raise FileNotFoundError(2, "The system cannot find the path specified", s)
        real_mkdir(p, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir)
    FilterPresets(home / "filter_presets.json").save("big", "id > 3")
    assert FilterPresets(home / "filter_presets.json").names() == ["big"]
    history = SearchHistory(home / "search_history.json")
    history.add("Toshkent")
    assert SearchHistory(home / "search_history.json").texts() == ["Toshkent"]


def test_n15_folder_errors_are_localized(tmp_path: Path, lang_en) -> None:
    blocker = tmp_path / "a_file"
    blocker.write_bytes(b"x")
    with pytest.raises(JsonReshapeError) as info:
        FilterPresets(blocker / "sub" / "filter_presets.json").save("a", "id > 1")
    assert "a_file" in info.value.message
    with pytest.raises(JsonReshapeError):
        SearchHistory(blocker / "sub" / "search_history.json").add("x")


# ==================================================================================
# blackbox/N11: a filter that fails on every record
# ==================================================================================
def test_n11_a_filter_failing_on_every_record_stops_with_an_error(make_doc, lang_en) -> None:
    doc = make_doc([{"id": i, "tags": ["a"]} for i in range(5)])
    with pytest.raises(UserInputError) as info:
        filter_records(doc, compile_filter("abs(tags) > `1`", "jmespath"))
    err = info.value
    assert err.key == "recordset.error.eval_failed"
    assert "records tried: 5" in err.message and "#0" in err.message and "abs()" in err.message
    assert "In function abs()" not in err.message and "In function abs()" in (err.hint or "")
    assert os.listdir(recordsets_dir()) == []


def test_n11_the_scan_stops_after_the_first_failures(make_doc, monkeypatch, lang_uz) -> None:
    recs: list[Any] = [{"id": i, "tags": ["a"]} for i in range(3000)]
    for r in recs[1500:]:
        r["tags"] = 5   # later records would work: the first 1000 decide
    doc = make_doc(recs, "jsonl")
    calls: list[int] = []
    real = Filter._jmes_matches

    def counting(self: Filter, value: Any) -> bool:
        calls.append(1)
        return real(self, value)

    monkeypatch.setattr(Filter, "_jmes_matches", counting)
    with pytest.raises(UserInputError) as info:
        filter_records(doc, compile_filter("abs(tags) > `1`", "jmespath"), block_bytes=4096)
    assert len(calls) == rsmod.ALL_FAILED_LIMIT == 1000
    assert "tekshirilgan yozuvlar: 1 000" in info.value.message


def test_n11_one_success_keeps_the_filter_running(make_doc, lang_en) -> None:
    recs: list[Any] = [{"id": i, "tags": ["a"]} for i in range(1500)]
    recs[10]["tags"] = 7
    doc = make_doc(recs, "jsonl")
    rs = filter_records(doc, compile_filter("abs(tags) > `1`", "jmespath"))
    try:
        assert rs.to_list() == [10] and rs.report.eval_errors == 1499
        assert "1,499 records" in rs.report.warning_texts()[0]
    finally:
        rs.close()


def test_n11_cli_exits_1_and_writes_nothing(tmp_path: Path, capsys, lang_en) -> None:
    src = tmp_path / "in.jsonl"
    src.write_bytes(b"".join(b'{"id":%d,"tags":["a"]}\n' % i for i in range(50)))
    out = tmp_path / "out.jsonl"
    rc = cli.main(["--lang", "en", "filter", str(src), str(out), "--jmespath", "abs(tags) > `1`"])
    err = capsys.readouterr().err
    assert rc == 1 and not out.exists()
    assert "could not be evaluated on any record (records tried: 50)" in err and "#0" in err


# ==================================================================================
# blackbox/N13: sort progress
# ==================================================================================
@pytest.mark.parametrize("limit", [None, 20_000])
def test_n13_sort_progress_never_goes_back(make_doc, limit: int | None) -> None:
    doc = make_doc(jsongen.records(3000, seed=4))
    infos: list[Any] = []
    p = Progress(callback=infos.append, min_interval=0)
    rs = sort_records(doc, ["name"], memory_limit=limit, progress=p, block_bytes=16384)
    rs.close()
    keys_msg = t("recordset.progress.sort_keys", name=doc.source_path.name)
    merge_msg = t("recordset.progress.sort_merge", name=doc.source_path.name)
    fractions = [i.fraction for i in infos if i.fraction is not None]
    assert fractions == sorted(fractions)
    assert all(i.done <= i.total for i in infos if i.total is not None)
    reading = [i for i in infos if i.message == keys_msg and i.done]
    merging = [i for i in infos if i.message == merge_msg and not i.finished]
    assert reading and merging
    # the reading phase never shows "100 %, 0.0 s left" while the merge is still to come
    assert all(i.done < i.total and i.eta for i in reading)
    assert max(i.fraction for i in reading) <= min(i.fraction for i in merging)
    assert infos[-1].finished and infos[-1].done == infos[-1].total == p.total
    # a caller's own total is filled in proportion, never exceeded
    infos.clear()
    p2 = Progress(total=1000, callback=infos.append, min_interval=0)
    sort_records(doc, ["name"], memory_limit=limit, progress=p2, block_bytes=16384).close()
    assert p2.total == 1000 and all(i.done <= 1000 for i in infos)
    assert [i.done for i in infos] == sorted(i.done for i in infos) and infos[-1].done == 1000


# ==================================================================================
# codereview/N03, blackbox/F16: counts of one in English
# ==================================================================================
_ONE_PLURAL = re.compile(r"\b1 (?:[a-z]+ ){0,2}(?:records|matches|values)\b")


def test_n03_recordset_warnings_read_correctly_for_one(lang_en) -> None:
    keys = ["recordset.warn.eval_errors", "recordset.warn.too_large", "recordset.warn.approximate",
            "recordset.warn.unparseable", "recordset.warn.skipped_huge", "recordset.warn.sort_key_absent"]
    params = [{"count": 1, "record": 7, "error": "E"}, {"count": 1, "record": 7, "size": "1.0 MB"},
              {"count": 1, "record": 7, "size": "1.0 MB"}, {"count": 1}, {"count": 1, "size": "1.0 MB"},
              {"field": "nmae", "total": 1}]
    texts = rsmod._warning_texts(keys, params)
    for text in texts:
        assert not _ONE_PLURAL.search(text), text
    assert texts[0] == "The filter could not be evaluated on 1 record (#7); it is not in the result: E"
    assert "1 record (#7) is too large" in texts[1] and "1 very large record " in texts[2]
    assert texts[3].startswith("1 record could not be read") and "in the 1 record," in texts[5]
    many = rsmod._warning_texts(keys[:1], [{"count": 12345, "record": 7, "error": "E"}])[0]
    assert "on 12,345 records; they are" in many
    i18n.set_language("uz")
    assert rsmod._warning_texts(keys[:1], [{"count": 12345, "record": 7, "error": "E"}])[0].startswith(
        "12 345 ta yozuvda")


def test_n03_one_failing_record_gives_a_singular_warning(make_doc, lang_en) -> None:
    recs: list[Any] = [{"id": i, "tags": ["v"]} for i in range(10)]
    recs[4]["tags"] = None
    doc = make_doc(recs)
    rs = filter_records(doc, compile_filter("contains(tags, 'v')", "jmespath"))
    try:
        assert "could not be evaluated on 1 record (#4); it is not in the result" in rs.report.warning_texts()[0]
    finally:
        rs.close()


def test_f16_schema_search_and_query_texts_for_one(make_doc, lang_en) -> None:
    b = SchemaBuilder()
    b.add({"a": 1})
    assert "1 record could not be analysed" in b.build(skipped=1).summary()
    assert "3 records could not be analysed" in b.build(skipped=3).summary()
    doc = make_doc([{"t": "Toshkent"}, {"t": "Toshkent shahri", "u": "Toshkent"}])
    res = search(doc, SearchOptions.build("Toshkent"), max_hits=1)
    text = res.summary()
    res.close()
    assert text.startswith("Matches: 3, records: 2;") and "Only the first match is listed (1 of 3)." in text
    assert not _ONE_PLURAL.search(text)
    with pytest.raises(QuerySyntaxError) as info:
        compile_filter("len(a, b) > 1")
    assert "arguments given: 2" in info.value.message
    with pytest.raises(QuerySyntaxError) as info:
        compile_filter("length(a, b) > `1`", "jmespath")
    assert "arguments given: 2" in info.value.message


# ==================================================================================
# blackbox/F14: JMESPath lexer errors in Uzbek
# ==================================================================================
@pytest.mark.parametrize("expr, pos, fragment", [
    ("a == 'x", 5, "matn yopilmagan: oxiridagi ' yetishmayapti"),
    ('a == "x', 5, "qo'shtirnoqli maydon nomi yopilmagan: oxiridagi \" yetishmayapti"),
    ("a[?b == `x]", 8, "` bilan boshlangan qiymat yopilmagan: oxiridagi ` yetishmayapti"),
    ('a == `"x`', 5, "` ` orasidagi qiymat to'g'ri JSON emas: \"x"),
    ("a ^ b", 2, "kutilmagan belgi '^'"),
    ("a[1:2:3:4]", 7, "kutilmagan ':'"),
    ('"f"(a)', 0, "funksiya nomi qo'shtirnoqsiz yoziladi"),
    ('a."\\q"', 2, "qo'shtirnoqli maydon nomi noto'g'ri"),
])
def test_f14_jmespath_syntax_errors_are_localized(expr: str, pos: int, fragment: str, lang_uz) -> None:
    with pytest.raises(QuerySyntaxError) as info:
        compile_filter(expr, "jmespath")
    err = info.value
    assert err.pos == pos and fragment in err.message, err.message
    for english in ("Unclosed", "Bad token", "Unknown token", "delimiter", "syntax error", "JMESPath xatosi"):
        assert english not in err.message
    assert err.params.get("raw")   # JMESPath's own text is kept as a detail
