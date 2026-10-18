"""AGENT_PROMPT §14 test 11: every ``(start, end)`` span parses to exactly the original
record, for every supported format (§3 formats 1-7), on every available core backend.

Each case writes a document with ``tests/jsongen.write_file`` and checks:

* the detected kind (and record path / encoding / compression);
* the record count and, for EVERY record, ``codec.dumps(doc.record(i)) ==
  codec.dumps(original)`` (+ ``record_key`` for keyed documents);
* a second open reuses the cached index (no rescan) with identical results;
* ``iter_blocks`` in ascending, descending and random order returns exactly the bytes of
  each record span.

The data of a case is generated from a seed that is a stable function of the case id
(:func:`_case_seed`, ``zlib.crc32``; Python's ``hash()`` of a str is salted per process,
so it would give different documents in every run). A failure names the case and the
seed, and the same test id replays the same documents.
"""

from __future__ import annotations

import gzip
import os
import random
import subprocess
import sys
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

import jsongen
from jsonreshape import codec, reader
from jsonreshape.reader import Document

N = 40


def _case_seed(case_id: str) -> int:
    """Data seed of a case: the same in every run (``hash()`` depends on PYTHONHASHSEED)."""
    return zlib.crc32(case_id.encode("utf-8")) % 1000


@contextmanager
def _replay_note(case_id: str, seed: int) -> Iterator[None]:
    """Name the case and its data seed in the message of any failure inside the block."""
    try:
        yield
    except Exception as exc:
        exc.add_note(f"replay: case {case_id!r}, data seed {seed} "
                     f"(jsongen.records({N}, seed={seed}); the seed is zlib.crc32 of the case id)")
        raise


@pytest.fixture
def backend(core_backend):
    """Run the test with the scanner of the parametrized core backend."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


def _records(seed: int, n: int = N) -> list[Any]:
    return jsongen.records(n, seed=seed)


def _keyed_keys(n: int) -> list[str]:
    base = ["id0", "Тошкент", "kalit bo'shliq bilan", "", "dot.key", "a/b", "x~y", "q\"uote", "back\\slash",
            "emoji \U0001f600", "tab\tkey"]
    return [base[i] if i < len(base) else f"id{i}" for i in range(n)]


# (case id, format, write_file kwargs, Document.open kwargs, expected kind, expected record path)
CASES: list[tuple[str, str, dict, dict, str, tuple | None]] = [
    ("array-compact", "array", {}, {}, "array", None),
    ("array-pretty2", "array", {"style": "pretty2"}, {}, "array", None),
    ("array-pretty4-crlf", "array", {"style": "pretty4", "newline": "\r\n"}, {}, "array", None),
    ("array-tab", "array", {"style": "tab"}, {}, "array", None),
    ("array-messy", "array", {"style": "messy"}, {}, "array", None),
    ("array-escaped", "array", {"style": "escaped"}, {}, "array", None),
    ("array-messy-escaped", "array", {"style": "messy_escaped"}, {}, "array", None),
    ("jsonl-lf", "jsonl", {}, {}, "jsonl", None),
    ("jsonl-crlf", "jsonl", {"newline": "\r\n"}, {}, "jsonl", None),
    ("jsonl-escaped", "jsonl", {"style": "escaped"}, {}, "jsonl", None),
    ("brace_list", "brace_list", {}, {}, "brace_list", None),
    ("brace_list-pretty", "brace_list", {"style": "pretty2"}, {}, "brace_list", None),
    ("wrapped-data", "wrapped", {}, {}, "wrapped", ("data",)),
    ("wrapped-data-pretty", "wrapped", {"style": "pretty2"}, {}, "wrapped", ("data",)),
    ("wrapped-nested", "wrapped", {"wrapper_path": ("result", "items")}, {}, "wrapped", ("result", "items")),
    ("wrapped-nested3-messy", "wrapped", {"wrapper_path": ("a", "b", "c"), "style": "messy"}, {}, "wrapped",
     ("a", "b", "c")),
    ("keyed", "keyed", {}, {}, "keyed", None),
    ("keyed-pretty", "keyed", {"style": "pretty2"}, {}, "keyed", None),
    ("single-forced", "single", {}, {"format": "single"}, "single", None),
    ("stream-pretty", "stream", {"style": "pretty2"}, {}, "stream", None),
    ("stream-compact-forced", "stream", {}, {"format": "stream"}, "stream", None),
    ("utf8-bom-array", "array", {"bom": True}, {}, "array", None),
    ("utf8-bom-jsonl", "jsonl", {"bom": True}, {}, "jsonl", None),
    ("utf16le-bom", "array", {"encoding": "utf-16-le", "bom": True}, {}, "array", None),
    ("utf16be-bom", "array", {"encoding": "utf-16-be", "bom": True}, {}, "array", None),
    ("utf16le-nobom", "array", {"encoding": "utf-16-le"}, {}, "array", None),
    ("utf16be-nobom-wrapped", "wrapped", {"encoding": "utf-16-be"}, {}, "wrapped", ("data",)),
    ("utf16le-jsonl", "jsonl", {"encoding": "utf-16-le", "bom": True}, {}, "jsonl", None),
    ("gzip-array", "array", {"gz": True}, {}, "array", None),
    ("gzip-jsonl", "jsonl", {"gz": True}, {}, "jsonl", None),
    ("gzip-wrapped", "wrapped", {"gz": True, "style": "pretty2"}, {}, "wrapped", ("data",)),
    ("gzip-keyed", "keyed", {"gz": True}, {}, "keyed", None),
    ("gzip-utf8-bom", "array", {"gz": True, "bom": True}, {}, "array", None),
    ("gzip-utf16le", "brace_list", {"gz": True, "encoding": "utf-16-le", "bom": True}, {}, "brace_list", None),
]


def _expected_encoding(kw: dict) -> str:
    enc = kw.get("encoding", "utf-8")
    if enc == "utf-8" and kw.get("bom"):
        return "utf-8-bom"
    return enc


def _check_records(doc: Document, recs: list[Any], fmt: str, keys: list[str] | None) -> None:
    if fmt == "single":
        assert doc.record_count == 1
        assert codec.dumps(doc.record(0)) == codec.dumps(recs)
        return
    assert doc.record_count == len(recs)
    for i, rec in enumerate(recs):
        assert codec.dumps(doc.record(i)) == codec.dumps(rec), f"record {i}"
        if keys is not None:
            assert doc.record_key(i) == keys[i]
        else:
            assert doc.record_key(i) is None


def _check_blocks(doc: Document, data: bytes) -> None:
    n = doc.record_count
    expected = {i: data[slice(*doc.record_span(i))] for i in range(n)}
    rng = random.Random(n)
    orders = {
        "none": None,
        "ascending": list(range(n)),
        "range": range(n),
        "descending": list(range(n - 1, -1, -1)),
        "random": rng.sample(range(n), n),
        "random-dupes": [rng.randrange(n) for _ in range(2 * n)],
    }
    for name, source in orders.items():
        for block_bytes in (1, 700, 1 << 20):
            got: list[int] = []
            for blk in doc.iter_blocks(source, block_bytes=block_bytes):
                assert not blk.huge
                assert len(blk.spans) == 16 * len(blk.indices)
                last_end = 0
                for k, i in enumerate(blk.indices):
                    s, e = blk.span(k)
                    assert s >= last_end, "spans sorted and non-overlapping"
                    last_end = e
                    assert blk.data[s:e] == expected[i], (name, block_bytes, i)
                if blk.contiguous:
                    assert data[blk.base:blk.base + len(blk.data)] == blk.data
                got.extend(blk.indices)
            want = list(range(n)) if source is None else list(source)
            assert got == want, (name, block_bytes)


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_format_roundtrip(tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch, case) -> None:
    seed = _case_seed(case[0])
    with _replay_note(case[0], seed):
        _check_roundtrip(tmp_path, backend, monkeypatch, case, seed)


def test_case_data_is_the_same_in_every_run() -> None:
    """Every run generates the same documents for the cases above, whatever the hash
    seed of the process is, so a failure can be replayed (str ``hash()`` is salted)."""
    code = ("import hashlib, test_reader_formats as m\n"
            "from jsonreshape import codec\n"
            "for c in m.CASES:\n"
            "    seed = m._case_seed(c[0])\n"
            "    print(c[0], seed, hashlib.sha256(codec.dumps(m._records(seed))).hexdigest())\n")
    outputs = []
    for hash_seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=hash_seed, PYTHONIOENCODING="utf-8")
        cp = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parent), env=env,
                            capture_output=True, timeout=120)
        assert cp.returncode == 0, cp.stderr.decode("utf-8", errors="replace")
        outputs.append(cp.stdout.decode("utf-8"))
    assert outputs[0] == outputs[1], outputs
    assert len(outputs[0].splitlines()) == len(CASES)
    # ... and the seeds of this process are the same as in the children
    mine = {c[0]: _case_seed(c[0]) for c in CASES}
    assert {line.split()[0]: int(line.split()[1]) for line in outputs[0].splitlines()} == mine


def _check_roundtrip(tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch, case, seed: int) -> None:
    case_id, fmt, write_kw, open_kw, kind, record_path = case
    recs = _records(seed)
    keys = _keyed_keys(len(recs)) if fmt == "keyed" else None
    name = "data.jsonl" if case_id == "stream-compact-forced" else "data.json"
    path = tmp_path / (name + (".gz" if write_kw.get("gz") else ""))
    jsongen.write_file(path, recs, fmt, keys=keys, seed=seed, **write_kw)

    with Document.open(path, **open_kw) as doc:
        assert doc.info.kind == kind
        assert doc.info.record_path == record_path
        assert doc.info.encoding == _expected_encoding(write_kw)
        assert doc.info.compression == ("gzip" if write_kw.get("gz") else None)
        assert doc.backend == backend
        assert not doc.index_complete and doc.record_count == 0
        doc.build_index()
        assert doc.index_complete and doc.error is None and not doc.partial
        assert doc.broken_count == 0 and doc.broken_lines == []
        assert doc.member_records == (fmt == "keyed")
        assert not doc.index_from_cache
        _check_records(doc, recs, fmt, keys)
        data = doc.data_path.read_bytes()
        if fmt != "single":
            _check_blocks(doc, data)
        spans = [doc.record_span(i) for i in range(doc.record_count)]
        container = doc.container_span
        flags = doc.flags

    # the second open reuses the cached index: building must not be needed
    def no_rescan(self, *a, **k):  # pragma: no cover - failure path
        raise AssertionError("index rebuilt instead of reused")

    monkeypatch.setattr(Document, "_build", no_rescan)
    with Document.open(path, **open_kw) as doc2:
        assert doc2.index_complete and doc2.index_from_cache
        assert doc2.info.kind == kind
        assert [doc2.record_span(i) for i in range(doc2.record_count)] == spans
        assert doc2.container_span == container and doc2.flags == flags
        _check_records(doc2, recs, fmt, keys)
        doc2.build_index()  # idempotent: no rebuild


@pytest.mark.parametrize("blank", [b"\n", b"\r\n", b"   \n", b"\t\r\n"])
def test_jsonl_with_blank_lines(tmp_path: Path, backend: str, blank: bytes) -> None:
    recs = _records(5, 25)
    lines = [codec.dumps(r) for r in recs]
    body = b"\n" + blank + b"\n".join(line + (b"\n" + blank if i % 3 == 0 else b"") for i, line in enumerate(lines))
    path = tmp_path / "blank.jsonl"
    path.write_bytes(body + b"\n\n")
    with Document.open(path) as doc:
        doc.build_index()
        assert doc.info.kind == "jsonl" and doc.error is None and doc.broken_count == 0
        _check_records(doc, recs, "jsonl", None)
        _check_blocks(doc, path.read_bytes())


@pytest.mark.parametrize("ext", [".jsonl", ".ndjson", ".jsonlines", ".JSONL", ".jsonl.gz"])
def test_jsonl_extension_wins(tmp_path: Path, backend: str, ext: str) -> None:
    recs = _records(9, 1)  # a single line: only the extension says JSON Lines
    path = tmp_path / ("one" + ext)
    data = codec.dumps(recs[0]) + b"\n"
    path.write_bytes(gzip.compress(data) if ext.endswith(".gz") else data)
    with Document.open(path) as doc:
        assert doc.info.kind == "jsonl"
        doc.build_index()
        _check_records(doc, recs, "jsonl", None)


def test_single_object_document(tmp_path: Path, backend: str) -> None:
    # no array with 2+ items, members not mostly objects: a single value
    value = {"meta": {"name": "x"}, "count": 1, "items": [1], "nested": {"a": {"b": None}}}
    path = tmp_path / "single.json"
    path.write_bytes(jsongen.encode(value, "pretty2"))
    with Document.open(path) as doc:
        assert doc.info.kind == "single"
        doc.build_index()
        assert doc.record_count == 1 and doc.record(0) == value


def test_wrapped_object_container_records_are_members(tmp_path: Path, backend: str) -> None:
    recs = _records(11, 12)
    keys = [f"k{i}" for i in range(len(recs))]
    inner = b"{" + b",".join(codec.dumps(k) + b":" + codec.dumps(r) for k, r in zip(keys, recs)) + b"}"
    path = tmp_path / "wobj.json"
    path.write_bytes(b'{"meta":{"v":1},"users":' + inner + b',"tail":[1,2]}')
    with Document.open(path, record_path="users") as doc:
        assert doc.info.kind == "wrapped" and doc.info.record_path == ("users",)
        doc.build_index()
        assert doc.member_records
        _check_records(doc, recs, "keyed", keys)
        _check_blocks(doc, path.read_bytes())
        assert doc.wrapper_parts() == (b'{"meta":{"v":1},"users":', b',"tail":[1,2]}')


def test_backends_produce_identical_indexes(tmp_path: Path) -> None:
    from jsonreshape import core

    if set(core.available_backends()) != {"rust", "python"}:
        pytest.skip("both core backends are required")
    recs = _records(77, 60)
    results = {}
    for name in ("rust", "python"):
        path = tmp_path / f"{name}.json"
        jsongen.write_file(path, recs, "wrapped", style="messy", wrapper_path=("x", "y"))
        old = reader.set_scanner_backend(name)
        try:
            with Document.open(path, use_cache=False) as doc:
                doc.build_index()
                results[name] = ([doc.record_span(i) for i in range(doc.record_count)], doc.container_span,
                                 doc.flags, doc.backend)
        finally:
            reader.set_scanner_backend(old)
    assert results["rust"][:3] == results["python"][:3]
    assert results["rust"][3] == "rust" and results["python"][3] == "python"
