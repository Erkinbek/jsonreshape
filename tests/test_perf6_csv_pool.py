"""Stage-6 performance work P6 on the CSV export (docs/ARCHITECTURE.md §8, §12.12; AGENT_PROMPT §12 "eksport
>= 50 MB/s"): the rows of a CSV export and the column scan of automatic CSV/XLSX columns in worker processes give
EXACTLY the one-thread results -- the same bytes (also gzip-compressed), the same report.

* An in-process stand-in for ``writer._Pool`` (a job runs when it is submitted; its arguments and its result are
  pickled as on their way to and from a worker) drives the pooled code over random documents of every kind, both
  core backends, edited documents, every column mode, tiny jobs, jobs cut by the text limit, jobs a worker
  "could not do" and workers that break midway.
* Records a worker leaves to the calling thread (cannot be decoded in memory, more than POOL_SAFE_BRACKETS
  brackets), huge records, exact numbers, lone surrogates, Unicode, explicit columns never found.
* :class:`writer._ScanMerge` against the one-thread :class:`writer._ColumnScan` on random value sequences cut into
  random runs (exact whenever it says so; it says so for ordinary data and refuses maps / too many paths).
* XLSX: the sheet XML deflated by workers gives the same workbooks as one thread (every zip member but the creation
  time; sheets and ``files`` parts; numbers kept as text, truncated texts, lone surrogates) -- stage 7,
  ``writer_xlsx`` (``tests/test_writer_xlsx_fast.py`` has the rest).
* Real spawned worker processes: every kind, edited documents, cancellation (no output, no temporary file, the
  workers gone), progress, the fallback when the workers cannot run, the in-flight bound, the automatic number
  of workers, the column scan.
* gzip output does not depend on the sizes of the writes (``writer._Sink``).
"""

from __future__ import annotations

import gzip
import io
import pickle
import random
import zipfile
import zlib
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import psutil
import pytest

import jsongen
from jsonreshape import bulk, codec, writer
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress, check
from jsonreshape.writer import ExportOptions, discover_columns, export
from overlay_helpers import make_edited

SCALARS = ["id", "name", "score", "active", "contact.phone", "contact.address.city", "contact.address.zip"]
SPECIAL = ["$index", "$key", "orders[*].oid", "tags", "contact", "note", "nope.never", "extra.**", "orders[0].sum"]
KINDS = ("array", "jsonl", "brace_list", "wrapped", "keyed", "single", "stream")


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
class FakePool:
    """``writer._Pool`` without processes: a job runs when it is submitted (arguments and result pickled as
    for a worker). ``redo``: job numbers (1-based, per pool) answered ``("redo",)``; ``break_at``: from this job
    number on the workers are broken (``submit`` returns ``None``)."""

    made: list[FakePool] = []
    redo: set[int] = set()
    break_at: int | None = None

    def __init__(self, workers: int, init: Any, initargs: tuple, cancel: Any, what: str) -> None:
        self.workers = workers
        self.cancel = cancel
        self.what = what
        self.broken = False
        self.jobs = 0
        self.closed = False
        init(*pickle.loads(pickle.dumps(initargs, protocol=pickle.HIGHEST_PROTOCOL)))
        FakePool.made.append(self)

    def submit(self, fn: Any, *args: Any) -> Any:
        if self.broken:
            return None
        self.jobs += 1
        if FakePool.break_at is not None and self.jobs >= FakePool.break_at:
            self.broken = True
            return None
        res = fn(*pickle.loads(pickle.dumps(args, protocol=pickle.HIGHEST_PROTOCOL)))
        if self.jobs in FakePool.redo:
            res = ("redo",)
        return pickle.loads(pickle.dumps(res, protocol=pickle.HIGHEST_PROTOCOL))

    def result(self, fut: Any) -> Any:
        check(self.cancel)
        if fut is None or fut[0] == "redo":
            return None
        return fut

    def close(self, futures: Any = ()) -> None:
        for _ in futures:
            pass
        self.closed = True


@pytest.fixture
def fake_pool(monkeypatch):
    monkeypatch.setattr(writer, "_Pool", FakePool)
    monkeypatch.setattr(writer, "_pool_init_codec", lambda fast: None)   # (the decoding of this process)
    monkeypatch.setattr(writer, "_POOL_STATE", {})
    FakePool.made = []
    FakePool.redo = set()
    FakePool.break_at = None
    yield FakePool
    FakePool.made = []
    FakePool.redo = set()
    FakePool.break_at = None


@pytest.fixture
def backend(core_backend, monkeypatch):
    """The core backend of the test for the decoding (codec's fast path) and the writer."""
    rust = core_backend.__name__.endswith("_rscore")
    codec._init_fast()
    monkeypatch.setattr(codec, "_fast_checker", core_backend.numbers_plain if rust else False)
    monkeypatch.setattr(writer, "core", core_backend)
    return core_backend


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


def open_raw(tmp_path: Path, docs: list, data: bytes, name: str = "s.json", **kw: Any) -> Document:
    p = tmp_path / "src" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    doc = Document.open(p, **kw)
    doc.build_index()
    docs.append(doc)
    return doc


def open_kind(tmp_path: Path, docs: list, kind: str, recs: list, seed: int = 0, style: str = "compact",
              settings: Settings | None = None, name: str | None = None) -> Document:
    data = jsongen.build_document(recs, kind, style=style, seed=seed)
    fmt = {"array": None, "jsonl": "jsonl", "stream": "stream", "brace_list": "brace_list", "keyed": "keyed",
           "single": "single", "wrapped": "wrapped"}[kind]
    kw: dict[str, Any] = {"settings": settings}
    if fmt is not None:
        kw["format"] = fmt
    if kind == "wrapped":
        kw["record_path"] = "data"
    return open_raw(tmp_path, docs, data, name or f"{kind}_{seed}.{'jsonl' if kind == 'jsonl' else 'json'}", **kw)


def files_in(directory: Path) -> list[str]:
    """The files directly in ``directory`` (temporary outputs are named ``.<name>.<hex>.tmp``)."""
    return sorted(p.name for p in directory.iterdir() if p.is_file())


def run(doc: Document, opts: ExportOptions, out: Path, **kw: Any) -> tuple[bytes, writer.ExportReport]:
    rep = export(doc, out, opts, **kw)
    return out.read_bytes(), rep


def report_view(rep: writer.ExportReport) -> tuple:
    return (rep.fmt, rep.records, rep.rows, rep.skipped, rep.columns, rep.warnings, rep.warning_keys,
            rep.warning_params, rep.bytes_written)


def same(doc: Document, opts: ExportOptions, tmp_path: Path, workers: int = 3, name: str = "o.csv",
         **kw: Any) -> tuple[writer.ExportReport, writer.ExportReport]:
    """The export with ``workers`` equals the one-thread export (bytes and report)."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    one, r1 = run(doc, opts, out_dir / ("one_" + name), workers=1, **kw)
    many, r2 = run(doc, opts, out_dir / ("many_" + name), workers=workers, **kw)
    assert r1.workers == 1
    assert many == one
    assert report_view(r2) == report_view(r1)
    return r1, r2


def special_records() -> list[bytes]:
    """Raw records: exact numbers, escapes and lone surrogates, a shallow record with many brackets (left to
    the calling thread), a record too deep for the json module (cannot be decoded in memory)."""
    many = b"[" + b",".join([b"[]"] * (writer.POOL_SAFE_BRACKETS + 10)) + b"]"
    deep = b"[" * 20000 + b"1" + b"]" * 20000
    return [
        ('{"id": 1e400, "name": "Ўзбекистон o\'zbek oʻzbek", "score": 12345678901234567890, "active": -0.0,'
         ' "contact": {"phone": 1.10, "address": {"city": 0.1, "zip": -1e-400}}, "tags": ["a;b", "c,d"]}'
         ).encode("utf-8"),
        b'{"id": 2, "name": "lone \\ud800 and pair \\ud83d\\ude00", "note": "q\\"uote, comma\\r\\nCRLF\\ttab",'
        b' "tags": ["\\u2028", "x\\u0000y"], "orders": [{"oid": 1, "sum": 0.1}, {"oid": 2}]}',
        b'{"id": 3, "name": "brackets", "extra": ' + many + b', "orders": [{"oid": 3, "sum": 5}]}',
        b'{"id": 4, "name": "deep", "extra": ' + deep + b', "contact": {"phone": "+998"}}',
        b'{"id": 5, "orders": [], "tags": [], "contact": {}, "score": null}',
    ]


def mixed_array(seed: int, n: int = 60) -> bytes:
    recs = [jsongen.encode(r, "compact", seed + i) for i, r in enumerate(jsongen.records(n, seed=seed))]
    rng = random.Random(seed)
    for raw in special_records():
        recs.insert(rng.randrange(len(recs) + 1), raw)
    return b"[" + b",\n".join(recs) + b"]"


OPTIONS = [
    lambda: ExportOptions("csv", columns=SCALARS),
    lambda: ExportOptions("csv", columns=SCALARS + SPECIAL),
    lambda: ExportOptions("csv"),
    lambda: ExportOptions("csv", array_mode="rows"),
    lambda: ExportOptions("csv", explode="orders"),
    lambda: ExportOptions("csv", columns=["id", "orders[*].oid", "orders[*].sum", "name"], array_mode="rows"),
    lambda: ExportOptions("csv", columns=SCALARS, csv_delimiter=";", csv_bom=False, join_sep=" | "),
]


# ----------------------------------------------------------------------------------
# the pooled code in this process (FakePool): equal to the one-thread export
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_and_column_mode(tmp_path, docs, fake_pool, backend, monkeypatch, kind) -> None:
    rng = random.Random(KINDS.index(kind))
    doc = open_kind(tmp_path, docs, kind, jsongen.records(40, seed=KINDS.index(kind)), seed=3,
                    style=rng.choice(["compact", "pretty2", "messy"]))
    for i, make in enumerate(OPTIONS):
        monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", rng.choice([1, 300, 2000, 1 << 20]))
        r1, r2 = same(doc, make(), tmp_path, name=f"{kind}{i}.csv")
        assert r2.workers == 3 and r2.rows >= r1.records
    assert fake_pool.made and all(p.closed for p in fake_pool.made)


def test_special_records_both_backends(tmp_path, docs, fake_pool, backend, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(7), "mixed.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 900)
    for make in OPTIONS:
        r1, _ = same(doc, make(), tmp_path)
        if make().columns is None:
            assert "writer.warn.deep_skipped" in r1.warning_keys    # (the record of 20 000 levels)
    # gzip: the same compressed bytes
    same(doc, ExportOptions("csv", columns=SCALARS + SPECIAL), tmp_path, name="o.csv.gz")
    same(doc, ExportOptions("csv"), tmp_path, name="a.csv.gz")


def test_records_left_to_the_calling_thread(tmp_path, docs, fake_pool, monkeypatch) -> None:
    """A job names the bracket-heavy and the undecodable records; the calling thread does them (decoded, streamed
    or skipped exactly as without workers)."""
    doc = open_raw(tmp_path, docs, mixed_array(11, 20), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1 << 20)          # one job: every record
    seen: list[tuple] = []
    real = writer._RowsJob.run

    def spy(self, *a: Any) -> tuple:
        res = real(self, *a)
        seen.append(res)
        return res

    monkeypatch.setattr(writer._RowsJob, "run", spy)
    r1, r2 = same(doc, ExportOptions("csv", columns=SCALARS), tmp_path)
    left = [p for res in seen for p in res[1] if type(p) is int]
    assert len(left) == 2          # the record of 4 106 brackets and the one of 20 000 levels
    assert r1.records == doc.record_count and r1.rows == doc.record_count


@pytest.mark.parametrize("kind", ["array", "keyed", "wrapped_obj", "jsonl"])
@pytest.mark.parametrize("scenario", ["structure", "mixed", "keys"])
def test_edited_documents(tmp_path, fake_pool, monkeypatch, kind, scenario) -> None:
    doc, _ob, exp = make_edited(tmp_path / "e", kind, scenario, n=40, seed=5)
    try:
        monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 500)
        for make in OPTIONS[:5]:
            r1, _ = same(doc, make(), tmp_path)
            plain, _ = run(exp, make(), tmp_path / "out" / "exp.csv", workers=1)
            assert (tmp_path / "out" / "one_o.csv").read_bytes() == plain    # the edits are exported
    finally:
        doc.close()
        exp.close()


def test_selections_keep_their_order(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(80, seed=2))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 400)
    for source in ([5, 3, 3, 70, 0, 41], range(10, 60, 7), list(range(79, -1, -1))):
        same(doc, ExportOptions("csv", columns=SCALARS + ["$index"]), tmp_path, source=source)
        same(doc, ExportOptions("csv"), tmp_path, source=source)


def test_huge_records_streamed_or_skipped(tmp_path, docs, fake_pool, monkeypatch) -> None:
    recs = jsongen.records(30, seed=4)
    for i in (0, 7, 29):
        recs[i]["orders"] = [{"oid": k, "sum": k / 10} for k in range(40)]
        recs[i]["tags"] = ["Тошкент oʻzbek"] * 30
    doc = open_kind(tmp_path, docs, "array", recs, settings=Settings(large_node_bytes=700))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 600)
    r1, _ = same(doc, ExportOptions("csv", columns=SCALARS + ["orders[*].oid", "tags"]), tmp_path)
    assert r1.records == 30
    r1, _ = same(doc, ExportOptions("csv", columns=["id", "orders[*].oid", "orders[*].sum"], explode="orders"),
                 tmp_path)
    assert r1.rows > 120
    r1, _ = same(doc, ExportOptions("csv"), tmp_path)
    assert r1.skipped >= 3 and "writer.warn.huge_skipped" in r1.warning_keys


def test_jobs_cut_by_the_text_limit_and_rows_mode(tmp_path, docs, fake_pool, monkeypatch) -> None:
    recs = jsongen.records(50, seed=9)
    for i, r in enumerate(recs):
        r["orders"] = [{"oid": k, "sum": k * 1.5, "note": "Ўзбек " * (k % 3)} for k in range(i % 13)]
    doc = open_kind(tmp_path, docs, "array", recs)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 5000)
    monkeypatch.setattr(writer, "POOL_JOB_TEXT", 150)            # a job stops after a few rows
    monkeypatch.setattr(writer, "ROW_BATCH", 3)
    starts: list[int] = []
    real = writer._RowsJob.run

    def spy(self, data: bytes, spans: bytes, indices: Any, start: int, pending: tuple) -> tuple:
        starts.append(start)
        return real(self, data, spans, indices, start, pending)

    monkeypatch.setattr(writer._RowsJob, "run", spy)
    same(doc, ExportOptions("csv", explode="orders"), tmp_path)
    same(doc, ExportOptions("csv", columns=["id", "orders[*].note", "name"], array_mode="rows"), tmp_path)
    assert any(s > 0 for s in starts)                             # jobs went on where the last one stopped


def test_jobs_encode_their_text_in_pieces(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(23, 70), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 6000)
    monkeypatch.setattr(writer, "POOL_JOB_ROWS", 2)                 # rows built two at a time ...
    monkeypatch.setattr(writer, "POOL_PIECE_CHARS", 10)             # ... their text encoded right away
    pieces: list[int] = []
    real = writer._RowsJob.run

    def spy(self, *a: Any) -> tuple:
        res = real(self, *a)
        pieces.append(sum(type(p) is bytes for p in res[1]))
        return res

    monkeypatch.setattr(writer._RowsJob, "run", spy)
    same(doc, ExportOptions("csv", columns=SCALARS + SPECIAL), tmp_path)
    same(doc, ExportOptions("csv", explode="orders"), tmp_path, name="p.csv.gz")
    assert max(pieces) > 5


def test_redo_and_broken_workers(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(21, 80), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 700)
    fake_pool.redo = {1, 3, 4, 9}
    _, r2 = same(doc, ExportOptions("csv", columns=SCALARS + SPECIAL), tmp_path)
    assert r2.workers == 3
    fake_pool.redo = set()
    fake_pool.break_at = 4
    _, r2 = same(doc, ExportOptions("csv"), tmp_path)
    assert r2.workers == 1                                         # the workers broke: the rest here
    fake_pool.break_at = 1
    _, r2 = same(doc, ExportOptions("csv", explode="orders"), tmp_path)
    assert r2.workers == 1


def test_a_job_error_is_raised_as_without_workers(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(30, seed=1))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 400)
    real = writer._RowBuilder.join_row

    def boom(self, value: Any, key: Any, index: int) -> list:
        if index == 17:
            raise ValueError("record 17")
        return real(self, value, key, index)

    monkeypatch.setattr(writer._RowBuilder, "join_row", boom)
    out = tmp_path / "x.csv"
    for workers in (1, 3):
        with pytest.raises(ValueError, match="record 17"):
            export(doc, out, ExportOptions("csv", columns=SCALARS), workers=workers)
        assert not files_in(tmp_path)              # no output, no temporary file


def test_unfound_columns_and_report(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "keyed", jsongen.records(30, seed=6))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 300)
    cols = ["$key", "$index", "id", "missing.a", "contact.address.city", "nope[*].x", "orders[*].sum"]
    r1, r2 = same(doc, ExportOptions("csv", columns=cols), tmp_path)
    assert "writer.warn.columns_not_found" in r1.warning_keys
    params = r1.warning_params[r1.warning_keys.index("writer.warn.columns_not_found")]
    assert params["names"] == ["missing.a", "nope[*].x"]
    assert r2.to_dict()["workers"] == 3 and r1.to_dict()["workers"] == 1


def test_progress_is_monotone_and_complete(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(3, 120), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 600)
    for opts in (ExportOptions("csv", columns=SCALARS), ExportOptions("csv")):
        infos: list[Any] = []
        prog = Progress(callback=infos.append, min_interval=0.0)
        export(doc, tmp_path / "p.csv", opts, progress=prog, workers=2)
        dones = [i.done for i in infos if i.total]
        assert dones == sorted(dones) and prog.finished and prog.done == prog.total > 0


def test_cancel_with_the_pool(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(200, seed=8))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 500)
    tok = CancelToken()
    real = FakePool.submit

    def submit(self: FakePool, fn: Any, *a: Any) -> Any:
        if self.jobs == 5:
            tok.cancel()
        return real(self, fn, *a)

    monkeypatch.setattr(FakePool, "submit", submit)
    out = tmp_path / "c.csv"
    with pytest.raises(Cancelled):
        export(doc, out, ExportOptions("csv", columns=SCALARS), cancel=tok, workers=2)
    assert not files_in(tmp_path) and all(p.closed for p in fake_pool.made)


# ----------------------------------------------------------------------------------
# the column scan: workers' scans merged in order == one scan
# ----------------------------------------------------------------------------------
def _scan_once(values: list[Any], rows: bool, explode: tuple | None) -> writer._ColumnScan:
    scan = writer._ColumnScan(rows, explode)
    for v in values:
        scan.add(v)
    return scan


def _merged(values: list[Any], rows: bool, explode: tuple | None, cuts: list[int]) -> writer._ScanMerge:
    merge = writer._ScanMerge(rows, explode)
    for a, b in zip([0] + cuts, cuts + [len(values)]):
        merge.add(pickle.loads(pickle.dumps(writer._scan_summary(_scan_once(values[a:b], rows, explode)))))
    return merge


def _scan_view(scan: writer._ColumnScan) -> tuple:
    paths, explode = scan.columns()
    return paths, explode, sorted(map(repr, scan.deep)), scan.capped, scan.records, scan.undecodable, scan.plans()


def deep_value(depth: int, leaf: Any = 1) -> Any:
    v = leaf
    for i in range(depth):
        v = {"k%d" % (i % 3): v, "x": i} if i % 2 else [v]
    return v


def random_values(seed: int, n: int) -> list[Any]:
    """Records of many shapes: jsongen records (random ``extra`` values), objects nested beyond MAX_DEPTH,
    arrays of objects with empty objects, scalars, arrays and odd keys as records."""
    rng = random.Random(seed)
    out: list[Any] = []
    for i in range(n):
        r = rng.random()
        if r < 0.7:
            out.append(jsongen.rand_record(rng, i))
        elif r < 0.8:
            rec = jsongen.rand_record(rng, i)
            rec["deep"] = deep_value(rng.randrange(60, 140))
            out.append(rec)
        elif r < 0.9:
            out.append({"id": i, "orders": [{"oid": k, "x": {} if k % 2 else {"y": [k]}} for k in range(3)],
                        "e": {} if rng.random() < 0.5 else {"z": None}})
        else:
            out.append(rng.choice([None, 1, "s", [], {}, [1, {"a": 2}], {"": {"": 0}}, [[{"q": 1}]]]))
    return out


def _nodes(scan: writer._ColumnScan) -> list[Any]:
    stack = [scan.main.root] + ([scan.elements.root] if scan.elements is not None else [])
    out = []
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.kids.values())
    return out


@pytest.mark.parametrize("seed", range(12))
def test_scan_merge_equals_one_scan(seed) -> None:
    """Whenever the merge says it is exact, it is (columns, expanded array, deep paths, cap, counts, maps);
    for these records it says so in most cuts (a merge that may meet a map-like path is refused, never wrong)."""
    rng = random.Random(seed)
    values = random_values(seed, rng.randrange(40, 160))
    exact = 0
    for rows, explode in ((False, None), (True, ("orders",)), (True, ("extra",)), (True, ())):
        whole = _scan_once(values, rows, explode)
        for _ in range(4):
            cuts = sorted(rng.sample(range(1, len(values)), rng.randrange(0, min(12, len(values) - 1))))
            merge = _merged(values, rows, explode, cuts)
            if merge.ok:
                exact += 1
                assert _scan_view(merge.scan) == _scan_view(whole), (seed, rows, explode, cuts)
    assert exact >= 12


def test_scan_merge_refuses_maps_and_too_many_paths(monkeypatch) -> None:
    ids = [{"id": i, "by_day": {f"2026-01-{i % 300:03d}": i}} for i in range(400)]   # a map of dates
    assert _scan_once(ids, False, None).map_nodes
    assert not _merged(ids, False, None, [100, 200, 300]).ok
    wide = [{f"f{k}": k for k in range(150)} for _ in range(30)]                      # a wide table: no map
    merge = _merged(wide, False, None, [7, 15])
    assert merge.ok and _scan_view(merge.scan) == _scan_view(_scan_once(wide, False, None))
    sparse = [{"id": i, f"k{i}": i} for i in range(300)]                              # sparse: map-like
    assert not _merged(sparse, False, None, [50, 150]).ok
    monkeypatch.setattr(writer, "SCAN_MAX_PATHS", 30)
    many = [{f"p{i}": {"a": 1, "b": 2}} for i in range(20)]
    assert not _merged(many, False, None, [5, 10]).ok


@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl", "wrapped"])
def test_discover_and_export_columns_with_the_scan_pool(tmp_path, docs, fake_pool, backend, monkeypatch,
                                                        kind) -> None:
    if kind == "array":
        doc = open_raw(tmp_path, docs, mixed_array(5, 70), "m.json")
    elif kind == "jsonl":
        doc = open_kind(tmp_path, docs, "jsonl", random_values(4, 70))
    else:
        doc = open_kind(tmp_path, docs, kind, jsongen.records(70, seed=4), seed=2)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 800)
    for kw in ({}, {"explode": "orders"}, {"array_mode": "rows"}):
        cols1 = discover_columns(doc, workers=1, **kw)
        cols3 = discover_columns(doc, workers=3, **kw)
        assert cols3 == cols1, kw
    scans = [p for p in fake_pool.made if p.what == "column scan"]
    assert len(scans) == 2                       # (inferred rows mode scans in one thread)
    r1, r2 = same(doc, ExportOptions("csv"), tmp_path)
    assert r2.workers == 3


def test_a_sampled_scan_stays_in_this_thread(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(90, seed=12))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 500)
    assert discover_columns(doc, sample=10, workers=3) == discover_columns(doc, sample=10, workers=1)
    assert not fake_pool.made                                   # (a sample reads a few records)
    assert discover_columns(doc, sample=500, workers=3) == discover_columns(doc, workers=1)
    assert [p.what for p in fake_pool.made] == ["column scan"]  # (a sample of every record is a full scan)


def test_scan_pool_falls_back_for_maps_with_monotone_progress(tmp_path, docs, fake_pool, monkeypatch) -> None:
    recs = [{"id": i, "name": f"n{i}", "by_day": {f"d{i % 500}": i, "fixed": True}} for i in range(1200)]
    doc = open_kind(tmp_path, docs, "array", recs)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 2000)
    infos: list[Any] = []
    prog = Progress(callback=infos.append, min_interval=0.0)
    cols3 = discover_columns(doc, workers=3, progress=prog)
    assert cols3 == discover_columns(doc, workers=1) and "by_day.*" in cols3
    dones = [i.done for i in infos if i.total]
    assert dones == sorted(dones) and prog.done == prog.total
    same(doc, ExportOptions("csv"), tmp_path)


def xlsx_parts(paths: list[Path]) -> list[dict[str, bytes]]:
    """The members of XLSX files, without ``docProps/core.xml`` (it holds the creation time)."""
    out = []
    for p in paths:
        with zipfile.ZipFile(p) as z:
            out.append({n: z.read(n) for n in z.namelist() if n != "docProps/core.xml"})
    return out


def same_xlsx(doc: Document, opts: ExportOptions, tmp_path: Path, workers: int = 3,
              **kw: Any) -> tuple[writer.ExportReport, writer.ExportReport]:
    """The XLSX export with ``workers`` writes the same workbooks (every sheet, every part) as one thread."""
    reps, parts = [], []
    for w, name in ((1, "one"), (workers, "many")):
        d = tmp_path / f"x_{name}"
        d.mkdir(exist_ok=True)
        rep = export(doc, d / "book.xlsx", opts, workers=w, **kw)
        reps.append(rep)
        parts.append(xlsx_parts(rep.files))
    assert parts[1] == parts[0]
    assert [p.name for p in reps[1].files] == [p.name for p in reps[0].files]
    assert report_view(reps[1])[:-1] == report_view(reps[0])[:-1]       # (bytes: the creation time differs)
    return reps[0], reps[1]


def test_xlsx_rows_and_columns_with_the_pool(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(13, 50), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 700)
    r1, r2 = same_xlsx(doc, ExportOptions("xlsx"), tmp_path)
    assert r2.workers == 3 and r1.workers == 1
    r1, r2 = same_xlsx(doc, ExportOptions("xlsx", columns=SCALARS + SPECIAL), tmp_path)
    assert r2.workers == 3                                        # (stage 7: every worker builds sheet XML)
    assert "writer.warn.xlsx_number_text" in r1.warning_keys          # 1e400, 12345678901234567890 as text
    monkeypatch.setattr(writer, "POOL_JOB_CELLS", 40)               # jobs cut every few rows
    monkeypatch.setattr(writer, "ROW_BATCH", 3)
    starts: list[int] = []
    real = writer._RowsJob.run

    def spy(self, data: bytes, spans: bytes, indices: Any, start: int, pending: tuple) -> tuple:
        starts.append(start)
        return real(self, data, spans, indices, start, pending)

    monkeypatch.setattr(writer._RowsJob, "run", spy)
    same_xlsx(doc, ExportOptions("xlsx", explode="orders", xlsx_max_rows=30), tmp_path)
    assert any(s > 0 for s in starts)
    r1, _ = same_xlsx(doc, ExportOptions("xlsx", columns=["id", "orders[*].oid", "name"], array_mode="rows",
                                         xlsx_max_rows=25, xlsx_split="files"), tmp_path)
    assert len(r1.files) > 2 and "writer.warn.xlsx_files" in r1.warning_keys
    fake_pool.redo = {2, 5}
    fake_pool.break_at = 7
    same_xlsx(doc, ExportOptions("xlsx", columns=SCALARS), tmp_path)


def test_xlsx_long_texts_and_surrogates_with_the_pool(tmp_path, docs, fake_pool, monkeypatch) -> None:
    recs =[{"id": i, "s": ("Ўз" * 20000) if i % 7 == 0 else f"q{i}", "e": "x\ud800y" if i % 5 == 0 else "ok"}
            for i in range(60)]
    data = codec.dumps(recs)
    doc = open_raw(tmp_path, docs, data, "t.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 30000)
    r1, _ = same_xlsx(doc, ExportOptions("xlsx"), tmp_path)
    assert "writer.warn.xlsx_truncated" in r1.warning_keys


# ----------------------------------------------------------------------------------
# options, automatic workers, gzip
# ----------------------------------------------------------------------------------
def test_workers_option_is_checked(tmp_path, docs) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(5))
    for bad in (0, -1, True, 1.5, "2"):
        with pytest.raises(UserInputError) as ei:
            export(doc, tmp_path / "x.csv", ExportOptions("csv"), workers=bad)
        assert ei.value.key == "writer.invalid_option"
        with pytest.raises(UserInputError):
            discover_columns(doc, workers=bad)
    assert not list(tmp_path.glob("x.csv*"))


def test_automatic_workers(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(40, seed=3))
    rep = export(doc, tmp_path / "a.csv", ExportOptions("csv"))
    assert rep.workers == 1                                        # far below bulk.POOL_MIN_BYTES
    monkeypatch.setattr(bulk, "POOL_MIN_BYTES", 0)
    monkeypatch.setattr(bulk, "_spawn_safe", lambda: False)        # a plain script as __main__
    assert export(doc, tmp_path / "b.csv", ExportOptions("csv")).workers == 1
    monkeypatch.setattr(bulk, "_spawn_safe", lambda: True)
    monkeypatch.setattr(bulk, "auto_workers", lambda nbytes, settings: 2)
    rep = export(doc, tmp_path / "c.csv", ExportOptions("csv"))
    assert rep.workers == 2 and (tmp_path / "c.csv").read_bytes() == (tmp_path / "a.csv").read_bytes()
    assert writer._auto_workers(None, 0, 1 << 40, doc.settings) == 1        # nothing selected
    assert writer._auto_workers(5, 0, 0, doc.settings) == 5                 # asked for
    assert writer._auto_workers(500, 9, 0, doc.settings) == writer.POOL_MAX_PROCESSES == 61


def test_gzip_bytes_do_not_depend_on_the_writes() -> None:
    rng = random.Random(4)
    data = b"".join(b"%d,%s,%r\r\n" % (i, ("x" * rng.randrange(40)).encode(), rng.random()) for i in range(60000))
    outs = []
    for _ in range(4):
        f = io.BytesIO()
        sink = writer._Sink(f, gzip=True)
        pos = 0
        while pos < len(data):
            step = rng.choice([1, 7, 1000, 70000, writer.GZIP_FEED, 3 * writer.GZIP_FEED + 5])
            sink.write(data[pos:pos + step])
            pos += step
        sink.write(b"")
        sink.finish()
        outs.append(f.getvalue())
    assert len(set(outs)) == 1 and gzip.decompress(outs[0]) == data
    # feeding zlib one compress() per write (as before) depends on the writes
    z1, z2 = (zlib.compressobj(writer.GZIP_LEVEL, zlib.DEFLATED, 31) for _ in range(2))
    a = z1.compress(data) + z1.flush()
    b = b"".join(z2.compress(data[i:i + 4099]) for i in range(0, len(data), 4099)) + z2.flush()
    assert a != b and gzip.decompress(a) == gzip.decompress(b) == data
    plain = io.BytesIO()
    writer._Sink(plain).write(b"abc")
    assert plain.getvalue() == b"abc"


# ----------------------------------------------------------------------------------
# real worker processes (spawned)
# ----------------------------------------------------------------------------------
def _children() -> set[int]:
    return {p.pid for p in psutil.Process().children(recursive=True)}


@pytest.mark.slow
@pytest.mark.parametrize("kind", KINDS)
def test_real_workers_every_kind(tmp_path, docs, backend, monkeypatch, kind) -> None:
    doc = open_kind(tmp_path, docs, kind, jsongen.records(50, seed=30 + KINDS.index(kind)), seed=1,
                    style="pretty2" if kind in ("keyed", "stream") else "compact")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 700)
    _, r2 = same(doc, ExportOptions("csv", columns=SCALARS + SPECIAL), tmp_path, workers=2)
    assert r2.workers == 2
    _, r2 = same(doc, ExportOptions("csv", explode="orders"), tmp_path, workers=2, name="r.csv.gz")
    assert r2.workers == 2


def test_real_workers_special_and_edited(tmp_path, docs, backend, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(41, 90), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1500)
    _, r2 = same(doc, ExportOptions("csv", columns=SCALARS + SPECIAL), tmp_path, workers=2)
    assert r2.workers == 2
    _, r2 = same(doc, ExportOptions("csv"), tmp_path, workers=2)
    assert r2.workers == 2
    ed, _ob, exp = make_edited(tmp_path / "e", "keyed", "mixed", n=60, seed=8)
    try:
        _, r2 = same(ed, ExportOptions("csv"), tmp_path, workers=2, name="k.csv")
        assert r2.workers == 2
    finally:
        ed.close()
        exp.close()


def test_real_workers_cancel_leaves_nothing(tmp_path, docs, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(1500, seed=2))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 2000)
    before = _children()
    tok = CancelToken()
    real = ProcessPoolExecutor.submit
    count = {"n": 0}

    def submit(self, fn, *a, **kw):
        count["n"] += 1
        if count["n"] == 6:
            holder["tok"].cancel()          # cancelled while results are awaited
        return real(self, fn, *a, **kw)

    monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)
    out = tmp_path / "c.csv"
    holder = {"tok": tok}
    with pytest.raises(Cancelled):
        export(doc, out, ExportOptions("csv", columns=SCALARS), cancel=tok, workers=2)
    assert not files_in(tmp_path)
    assert not (_children() - before)                              # the workers were shut down
    holder["tok"] = tok = CancelToken()
    count["n"] = 0
    with pytest.raises(Cancelled):                                 # cancelled in the column scan
        export(doc, out, ExportOptions("csv"), cancel=tok, workers=2)
    assert not files_in(tmp_path) and not (_children() - before)


def test_real_workers_broken_pool_falls_back(tmp_path, docs, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(5, 120), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1200)
    real = ProcessPoolExecutor.submit
    count = {"n": 0}

    def submit(self, fn, *a, **kw):
        count["n"] += 1
        if count["n"] > 3:
            raise BrokenProcessPool("test: the workers are gone")
        return real(self, fn, *a, **kw)

    monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)
    _, r2 = same(doc, ExportOptions("csv", columns=SCALARS + SPECIAL), tmp_path, workers=3)
    assert r2.workers == 1
    count["n"] = 0
    _, r2 = same(doc, ExportOptions("csv"), tmp_path, workers=3)    # scan and rows both fall back
    assert r2.workers == 1


def test_real_workers_jobs_in_flight_are_bounded(tmp_path, docs, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(800, seed=3))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1500)
    real_submit = ProcessPoolExecutor.submit
    real_result = writer._Pool.result
    count = {"submitted": 0, "taken": 0, "peak": 0}

    def submit(self, fn, *a, **kw):
        count["submitted"] += 1
        count["peak"] = max(count["peak"], count["submitted"] - count["taken"])
        return real_submit(self, fn, *a, **kw)

    def result(self, fut):
        res = real_result(self, fut)
        count["taken"] += 1
        return res

    monkeypatch.setattr(ProcessPoolExecutor, "submit", submit)
    monkeypatch.setattr(writer._Pool, "result", result)
    _, r2 = same(doc, ExportOptions("csv", columns=SCALARS), tmp_path, workers=2)
    assert r2.workers == 2 and count["submitted"] > 20 and count["taken"] == count["submitted"]
    assert count["peak"] <= 2 * 2


def test_real_workers_column_scan(tmp_path, docs, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "jsonl", random_values(77, 300))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 3000)
    for kw in ({}, {"explode": "orders"}):
        assert discover_columns(doc, workers=2, **kw) == discover_columns(doc, workers=1, **kw)


def test_real_workers_xlsx(tmp_path, docs, backend, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(17, 80), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 2000)
    _, r2 = same_xlsx(doc, ExportOptions("xlsx", columns=SCALARS + SPECIAL, xlsx_max_rows=40), tmp_path, workers=4)
    assert r2.workers == 4
