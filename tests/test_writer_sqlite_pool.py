"""Stage 7: the pooled SQLite export (``jsonreshape.writer_sqlite``) writes EXACTLY the database of the one-thread
exporter of stages 1-6 (``writer._export_sqlite_onethread``, kept as the reference): the same tables in the same
order, the same ``CREATE`` statements (columns, names, ``REFERENCES``), the same indexes, every row with every value
and its storage class (INTEGER / REAL / TEXT / NULL; ``-0.0`` included), and the same report (records, rows,
skipped, tables, warnings with their parameters).

* Every document kind, styles, both core backends, edited documents, selections (an order other than the file's:
  ``_ord``; a record selected twice), tiny jobs and segments, tiny insert batches (local tables that get columns
  after their first rows), jobs a worker "could not do", workers that break midway, ``workers=1``.
* The optimistic single pass and its map guard: maps found early and late, inside arrays and records that are
  arrays; the attempt is dropped and redone with the scan exactly (progress grows by the two passes).
* The limits: the overflow column ``_extra`` (``SQLITE_MAX_COLUMNS``), arrays as JSON text beyond
  ``SQLITE_MAX_TABLES``, values deeper than ``MAX_DEPTH``, records left to the calling thread (many brackets),
  records that cannot be decoded, huge records, exact numbers, lone surrogates, name clashes.
* The job's walk (``writer_sqlite._LocalRows``) counts exactly the keys ``writer._SqliteScan.add`` walks
  (``_KeyStats``, the calling thread's walk, is compared on random values).
* Real spawned worker processes: the same database, cancellation (no output, no temporary file).
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, writer, writer_sqlite
from jsonreshape.errors import Cancelled
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export
from overlay_helpers import make_edited
from test_perf6_csv_pool import (  # noqa: F401 - fixtures
    KINDS,
    FakePool,
    backend,
    docs,
    fake_pool,
    files_in,
    mixed_array,
    open_kind,
    open_raw,
)


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def dump(path: Path) -> tuple[list[tuple], dict[str, list[tuple]]]:
    """The schema (``sqlite_master`` in creation order) and every row of every table (values with their storage
    class: Python's sqlite3 maps INTEGER / REAL / TEXT / BLOB / NULL to int / float / str / bytes / None)."""
    con = sqlite3.connect(path)
    try:
        objs = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid").fetchall()
        data = {}
        for typ, name, _tbl, _sql in objs:
            if typ == "table":
                rows = con.execute(f'SELECT * FROM "{name.replace(chr(34), chr(34) * 2)}" ORDER BY rowid').fetchall()
                data[name] = [tuple((type(v).__name__, repr(v)) for v in r) for r in rows]
        return objs, data
    finally:
        con.close()


def view(rep: writer.ExportReport) -> tuple:
    return (rep.records, rep.rows, rep.skipped, rep.tables, rep.columns, rep.warning_keys, rep.warning_params,
            rep.warnings, len(rep.files))


def same_db(doc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: int = 3, name: str = "o",
            **kw: Any) -> tuple[writer.ExportReport, writer.ExportReport]:
    """The pooled export (``workers``) equals the one-thread reference export: database and report."""
    out = tmp_path / "db"
    out.mkdir(exist_ok=True)
    with monkeypatch.context() as m:
        m.setitem(writer._EXPORTERS, "sqlite", writer._export_sqlite_onethread)
        ref = export(doc, out / f"{name}_ref.sqlite", ExportOptions("sqlite"), workers=1, **kw)
    rep = export(doc, out / f"{name}_pool.sqlite", ExportOptions("sqlite"), workers=workers, **kw)
    assert dump(out / f"{name}_pool.sqlite") == dump(out / f"{name}_ref.sqlite")
    assert view(rep) == view(ref)
    return ref, rep


def map_records(n: int, seed: int, late: int = 0) -> list[Any]:
    """Records with map-like objects: ``attrs`` (rare keys from record ``late`` on), ``orders[*].by_day`` (a map
    inside array elements) and ordinary fields."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        rec: dict[str, Any] = {"id": i, "name": f"n{i}", "attrs": {"color": "red", "size": i % 7}}
        if i >= late:
            for j in range(3):
                rec["attrs"][f"k{rng.randrange(10 ** 6)}"] = [j, {"x": j}] if j == 1 else j
        rec["orders"] = [{"oid": k, "by_day": {f"2024-01-{rng.randrange(1, 10 ** 4)}": k}} for k in range(2)]
        out.append(rec)
    return out


def special_sqlite_records() -> list[bytes]:
    """Raw records for the SQLite rules: exact numbers, surrogates, name clashes, deep values, many brackets, a
    record too deep to decode."""
    deep_obj = '{"d":' * 70 + "1" + "}" * 70
    deep_arr = "[" * 70 + "2" + "]" * 70
    many = "[" + ",".join(["[]"] * (writer.POOL_SAFE_BRACKETS + 10)) + "]"
    raws = [
        '{"id": 1e400, "big": 123456789012345678901234567890, "f": 1.10, "z": -0, "nz": -0.0, "e": 1E3,'
        ' "x": 0.1, "pi": 3.141592653589793, "inexact": 123456789.123456789, "tiny": 1e-400, "b": true}',
        '{"_id": "clash", "_extra": 5, "A": 1, "a": 2, "_jsonreshape": [1, 2], "sqlite_x": [3], "value": 0,'
        ' "s": "lone \\ud800 pair \\ud83d\\ude00", "\\udc00key": "k"}',
        '{"id": 3, "deep": ' + deep_obj + ', "deeparr": ' + deep_arr + ', "empty": {}, "e2": []}',
        '{"id": 4, "brackets": ' + many + ', "orders": [{"oid": 4}]}',
        '{"id": 5, "toodeep": ' + "[" * 20000 + "0" + "]" * 20000 + "}",
        '{"id": 6, "orders": [[1, [2, {"q": [3]}]], {"oid": "x", "items": [{"sku": 1}, {"sku": 2, "n": null}]}]}',
        '[1, [2, 3], {"in": "array record"}]',
        '"a scalar record"',
        "null",
    ]
    return [r.encode("utf-8") for r in raws]


def special_doc(tmp_path: Path, docs: list, seed: int, n: int = 50, name: str = "sp.json") -> Any:
    recs = [jsongen.encode(r, "compact", seed + i) for i, r in enumerate(jsongen.records(n, seed=seed))]
    recs += [jsongen.encode(v, "compact", seed) for v in jsongen.records(15, seed=seed, kind="value")]
    rng = random.Random(seed)
    for raw in special_sqlite_records():
        recs.insert(rng.randrange(len(recs) + 1), raw)
    return open_raw(tmp_path, docs, b"[" + b",\n".join(recs) + b"]", name)


# ----------------------------------------------------------------------------------
# the pooled code in this process (FakePool): equal to the one-thread export
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_every_kind(tmp_path, docs, fake_pool, backend, monkeypatch, kind) -> None:
    rng = random.Random(KINDS.index(kind) + 100)
    recs = jsongen.records(40, seed=KINDS.index(kind)) + jsongen.records(10, seed=7, kind="value")
    if kind in ("keyed", "wrapped"):
        recs = jsongen.records(45, seed=KINDS.index(kind))
    doc = open_kind(tmp_path, docs, kind, recs, seed=3, style=rng.choice(["compact", "pretty2", "messy"]))
    for i, block in enumerate((1, 300, 2000, 1 << 20)):
        monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", block)
        ref, rep = same_db(doc, tmp_path, monkeypatch, name=f"{kind}{i}")
        assert rep.workers == 3 and ref.workers == 1
        if kind == "wrapped":
            assert "_wrapper" not in ref.tables      # (the wrapper table is not a data table)
    assert fake_pool.made and all(p.closed for p in fake_pool.made)


def test_special_records_both_backends(tmp_path, docs, fake_pool, backend, monkeypatch) -> None:
    doc = special_doc(tmp_path, docs, 11)
    for block in (1, 700, 1 << 20):
        monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", block)
        ref, _ = same_db(doc, tmp_path, monkeypatch, name=f"b{block}")
    keys = set(ref.warning_keys)
    assert {"writer.warn.sqlite_number_text", "writer.warn.sqlite_deep", "writer.warn.deep_skipped_sqlite"} <= keys
    # workers=1: the same jobs in this thread
    same_db(doc, tmp_path, monkeypatch, workers=1, name="w1")


def test_tiny_batches_and_commits(tmp_path, docs, fake_pool, monkeypatch) -> None:
    """Insert batches of a few rows: a job's local tables get columns after their first rows (ALTER TABLE)."""
    doc = special_doc(tmp_path, docs, 12, n=80)
    monkeypatch.setattr(writer, "SQLITE_BATCH", 3)
    monkeypatch.setattr(writer, "SQLITE_COMMIT_BYTES", 2000)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 5000)
    same_db(doc, tmp_path, monkeypatch)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1 << 20)
    same_db(doc, tmp_path, monkeypatch, name="one")


def test_segments_with_other_table_layouts(tmp_path, docs, fake_pool, monkeypatch) -> None:
    """Consecutive job images whose tables sit on other root pages (created in another order): SQLite must not
    reuse the parsed schema of the previous image (a new schema cookie per image)."""
    recs = []
    for i in range(240):
        if (i // 12) % 2:
            recs.append({"id": i, "orders": [{"oid": k} for k in range(1 + i % 7)], "tags": ["x"]})
        else:
            recs.append({"id": i, "tags": ["a"] * (1 + i % 9), "orders": [{"oid": 1, "n": "q" * (i % 50)}]})
    doc = open_kind(tmp_path, docs, "array", recs)
    monkeypatch.setattr(writer, "SQLITE_BATCH", 3)                 # (tables created as their rows come)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 600)
    same_db(doc, tmp_path, monkeypatch)
    loads: list[int] = []
    real = writer_sqlite._SqliteTarget._load

    def spy(self: Any, blob: bytes) -> None:
        loads.append(int.from_bytes(blob[40:44], "big"))
        real(self, blob)

    monkeypatch.setattr(writer_sqlite._SqliteTarget, "_load", spy)
    monkeypatch.setattr(writer, "SQLITE_BATCH", 4096)
    same_db(doc, tmp_path, monkeypatch, name="b")
    assert len(loads) > 10 and len(set(loads)) == 1                # (every job image has the same cookie)


def test_column_and_table_limits(tmp_path, docs, fake_pool, monkeypatch) -> None:
    recs = []
    for i in range(60):
        rec: dict[str, Any] = {"id": i, f"f{i % 13}": i, "nested": {f"g{i % 5}": [i, {"h": i}]}}
        rec[f"arr{i % 9}"] = [{"v": i, f"w{i % 4}": [i]}]
        if i % 10 == 0:
            rec["_extra"] = "taken"
        recs.append(rec)
    doc = open_kind(tmp_path, docs, "array", recs)
    monkeypatch.setattr(writer, "SQLITE_MAX_COLUMNS", 10)       # max_columns = 10: the overflow column
    monkeypatch.setattr(writer, "SQLITE_MAX_TABLES", 6)         # arrays beyond 6 child tables: JSON text
    for block in (1, 400, 1 << 20):
        monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", block)
        ref, _ = same_db(doc, tmp_path, monkeypatch, name=f"l{block}")
    assert {"writer.warn.sqlite_extra", "writer.warn.sqlite_tables"} <= set(ref.warning_keys)


@pytest.mark.parametrize("late", [0, 150])
def test_maps_restart_exactly(tmp_path, docs, fake_pool, monkeypatch, late) -> None:
    doc = open_kind(tmp_path, docs, "array", map_records(300, seed=late, late=late))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 3000)
    attempts: list[Any] = []
    real = writer_sqlite._attempt

    def spy(ctx: Any, out: Path, plans: Any, wrapper: Any, ordered: bool) -> None:
        attempts.append(plans)
        real(ctx, out, plans, wrapper, ordered)

    monkeypatch.setattr(writer_sqlite, "_attempt", spy)
    ref, rep = same_db(doc, tmp_path, monkeypatch)
    assert "writer.warn.map_columns" in ref.warning_keys
    assert len(attempts) == 2 and attempts[0] is None and attempts[1]    # the optimistic pass, then the exact one
    infos: list[Any] = []
    export(doc, tmp_path / "p.sqlite", ExportOptions("sqlite"), workers=3,
           progress=Progress(callback=infos.append, min_interval=0))
    dones = [i.done for i in infos if i.total]
    assert dones == sorted(dones) and infos[-1].finished and infos[-1].done == infos[-1].total
    # array records with a map in their elements; a map that is the record itself
    recs = [[{"id": i, f"k{i * 7 % 1000}": i}] for i in range(250)] + [{f"r{i}": i} for i in range(250)]
    doc2 = open_kind(tmp_path, docs, "array", recs, name="maps2.json")
    same_db(doc2, tmp_path, monkeypatch, name="m2")


def test_no_map_no_second_pass(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = open_kind(tmp_path, docs, "array", jsongen.records(120, seed=9))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 2000)
    scans: list[int] = []
    real = writer._sqlite_scan

    def spy(ctx: Any) -> Any:
        scans.append(1)
        return real(ctx)

    monkeypatch.setattr(writer_sqlite, "_sqlite_scan", spy)
    infos: list[Any] = []
    rep = export(doc, tmp_path / "x.sqlite", ExportOptions("sqlite"), workers=3,
                 progress=Progress(callback=infos.append, min_interval=0))
    assert not scans and rep.records == 120
    assert {i.total for i in infos if i.total} == {infos[-1].total}      # one pass planned, one pass made


def test_wide_records_restart_and_stay_exact(tmp_path, docs, fake_pool, monkeypatch) -> None:
    """More keys at one path than a map needs (a conservative restart: the scan finds no map) and records wider
    than a table (every block goes to the calling thread)."""
    recs = [{f"c{j}": i * j for j in range(130)} for i in range(30)]
    doc = open_kind(tmp_path, docs, "array", recs)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1500)
    ref, _ = same_db(doc, tmp_path, monkeypatch)
    assert "writer.warn.map_columns" not in ref.warning_keys
    monkeypatch.setattr(writer, "SQLITE_MAX_COLUMNS", 40)
    ref, _ = same_db(doc, tmp_path, monkeypatch, name="wide")
    assert "writer.warn.sqlite_extra" in ref.warning_keys


def test_redo_and_broken_workers(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = special_doc(tmp_path, docs, 13, n=90)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1500)
    fake_pool.redo = {2, 5}
    same_db(doc, tmp_path, monkeypatch)
    fake_pool.redo = set()
    fake_pool.break_at = 4
    same_db(doc, tmp_path, monkeypatch, name="broken")


@pytest.mark.parametrize("kind,scenario", [("array", "structure"), ("keyed", "mixed"), ("wrapped", "wrapper"),
                                           ("jsonl", "every3"), ("keyed", "keys")])
def test_edited_documents(tmp_path, fake_pool, monkeypatch, kind, scenario) -> None:
    doc, _ob, exp = make_edited(tmp_path / "e", kind, scenario, n=40, seed=5)
    try:
        monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 600)
        same_db(doc, tmp_path, monkeypatch, name="ed")
        plain = export(exp, tmp_path / "db" / "exp.sqlite", ExportOptions("sqlite"), workers=1)
        assert dump(tmp_path / "db" / "exp.sqlite")[1] == dump(tmp_path / "db" / "ed_pool.sqlite")[1]
        assert plain.records == exp.record_count
    finally:
        doc.close()
        exp.close()


def test_selections(tmp_path, docs, fake_pool, monkeypatch) -> None:
    doc = special_doc(tmp_path, docs, 14, n=70)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 900)
    n = doc.record_count
    for i, source in enumerate(([5, 3, 3, 40, 0, 41, 5], range(10, n, 7), list(range(n - 1, -1, -1)),
                                [n - 1, 0, n - 1])):
        ref, _ = same_db(doc, tmp_path, monkeypatch, name=f"s{i}", source=source)
        if i in (0, 3):
            assert "writer.warn.duplicates" in ref.warning_keys


def test_huge_records_skipped(tmp_path, docs, fake_pool, monkeypatch) -> None:
    from jsonreshape.settings import Settings

    recs = jsongen.records(30, seed=4)
    for i in (0, 7, 29):
        recs[i]["orders"] = [{"oid": k, "sum": k / 10} for k in range(300)]
    doc = open_kind(tmp_path, docs, "array", recs, settings=Settings(large_node_bytes=5000))
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 1000)
    ref, _ = same_db(doc, tmp_path, monkeypatch)
    assert ref.skipped >= 3 and "writer.warn.huge_skipped_sqlite" in ref.warning_keys


# ----------------------------------------------------------------------------------
# the key statistics of the map guard
# ----------------------------------------------------------------------------------
def deep_value(rng: random.Random, depth: int) -> Any:
    if depth <= 0:
        return {"leaf": 1}
    if rng.random() < 0.5:
        return [deep_value(rng, depth - 1), 1]
    return {f"k{rng.randrange(3)}": deep_value(rng, depth - 1), "s": "x", "e": {}}


def small_keys_value(rng: random.Random, depth: int = 0) -> Any:
    """A random value whose objects use at most 12 different keys (no path can become a map)."""
    r = rng.random()
    if depth >= 5 or r < 0.3:
        return rng.choice([1, "s", None, True, 2.5])
    if r < 0.6:
        return [small_keys_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {f"k{rng.randrange(12)}": small_keys_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def test_job_walk_counts_the_scan_keys(monkeypatch) -> None:
    monkeypatch.setattr(writer, "SQLITE_MAX_TABLES", 10 ** 6)      # (the deep values make many tables)
    rng = random.Random(5)
    values = [small_keys_value(rng) for _ in range(300)] + jsongen.records(50, seed=4)
    values += [deep_value(rng, d) for d in (60, 62, 63, 64, 65, 66, 70) for _ in range(4)]
    values += [[[{"a": 1}, []], {"b": [{"c": {}}]}], {"x": [[[{"y": 2}]]]}, 5, "s", None, [], {}]
    rng.shuffle(values)
    stats = writer_sqlite._KeyStats()
    engine_stats: dict[tuple, list] = {}
    engine = writer_sqlite._LocalRows(False, True, 1 << 20, None, False, 1 << 30, engine_stats)
    scan = writer._SqliteScan()
    try:
        for i, v in enumerate(values):
            stats.add(v)
            engine.add(i, None, v, i)
            scan.add(v)
    finally:
        engine.close()
    mine = {p: (set(keys), widest) for p, keys, widest in stats.summary()}
    walked = {p: (node[0], node[1]) for p, node in engine_stats.items()}
    assert walked == mine
    assert not scan.maps and set(mine) == set(scan.nodes)     # the paths the one-thread scan counts ...
    assert all(mine[p] == (scan.nodes[p].distinct, scan.nodes[p].widest) for p in mine)   # ... and their keys


# ----------------------------------------------------------------------------------
# real worker processes
# ----------------------------------------------------------------------------------
def test_real_workers(tmp_path, docs, monkeypatch) -> None:
    doc = special_doc(tmp_path, docs, 15, n=120)
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 4000)
    ref, rep = same_db(doc, tmp_path, monkeypatch, workers=2)
    assert rep.workers == 2
    doc2 = open_kind(tmp_path, docs, "array", map_records(200, seed=2), name="maps.json")
    same_db(doc2, tmp_path, monkeypatch, workers=2, name="maps")


def test_real_workers_cancel(tmp_path, docs, monkeypatch) -> None:
    doc = open_raw(tmp_path, docs, mixed_array(21, 600), "m.json")
    monkeypatch.setattr(writer, "POOL_BLOCK_BYTES", 3000)
    out = tmp_path / "c"
    out.mkdir()
    (out / "o.sqlite").write_bytes(b"OLD")
    token = CancelToken()

    def cb(info: Any) -> None:
        if info.total and info.done > info.total // 3:
            token.cancel()

    with pytest.raises(Cancelled):
        export(doc, out / "o.sqlite", ExportOptions("sqlite"), workers=2, cancel=token,
               progress=Progress(callback=cb, min_interval=0))
    assert files_in(out) == ["o.sqlite"] and (out / "o.sqlite").read_bytes() == b"OLD"
    assert codec is not None
