"""saver: speed of a save (docs/ARCHITECTURE.md §12.7 "Speed", AGENT_PROMPT §12 "export with edits >= 50 MB/s").

A 200 MB array with 100 point edits: the saver's own work -- planning, reading the untouched runs in 1 MiB
reads, validating beside the writing (Rust core), writing the temp file, committing -- must run at
>= 200 MB/s. The final ``fsync`` (the disk writing 200 MB) is measured separately: on a quiet NVMe disk it
adds ~0.2 s, but on a busy disk it alone can take seconds, which says nothing about the saver; the raw
speed of the disk (writing + fsyncing 200 MB with plain Python) is measured for comparison. The generated
source is flushed to disk before the measurements. The numbers are attached to the test report
(``record_property``): ``save_mb_per_s`` (without the fsync), ``save_total_mb_per_s`` (with it),
``disk_mb_per_s``.
"""

from __future__ import annotations

import os
import random
import time
from unittest import mock

import pytest

from jsonreshape import core, saver
from jsonreshape.reader import Document
from overlay_helpers import OverlayBuilder

pytestmark = pytest.mark.slow

TARGET_MB_S = 200.0


def _big_array(path, megabytes: int) -> int:
    rec = (b'{"id":%d,"name":"Foydalanuvchi %d","email":"user%d@example.uz","city":"Toshkent",'
           b'"score":%d.5,"tags":["a","b","c"],"active":true,"note":null}')
    parts = []
    size = 0
    i = 0
    while size < megabytes * 1000 * 1000:
        r = rec % (i, i, i, i % 1000)
        parts.append(r)
        size += len(r) + 2
        i += 1
    data = b"[" + b",\n".join(parts) + b"]\n"
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return i


def _disk_rate(path, megabytes: int) -> float:
    blk = os.urandom(1 << 20)
    started = time.perf_counter()
    with open(path, "wb", buffering=1 << 20) as f:
        for _ in range(megabytes):
            f.write(blk)
        f.flush()
        os.fsync(f.fileno())
    rate = megabytes * (1 << 20) / (time.perf_counter() - started) / 1e6
    os.remove(path)
    return rate


@pytest.mark.skipif("rust" not in core.available_backends(), reason="the speed target is for the Rust core")
def test_200mb_with_100_point_edits(tmp_path, record_property) -> None:
    src = tmp_path / "big.json"
    count = _big_array(src, 200)
    disk = _disk_rate(tmp_path / "disk.bin", 200)
    doc = Document.open(src)
    try:
        doc.build_index()
        assert doc.record_count == count
        ob = OverlayBuilder(doc, tmp_path / "layer")
        rng = random.Random(1)
        for p in sorted(rng.sample(range(count), 100)):
            ob.set(p, b'{"id":%d,"edited":true}' % p)
        doc.set_overlay(ob.state())
        fsyncs: list[float] = []
        real_fsync = os.fsync

        def timed_fsync(fd):
            started = time.perf_counter()
            real_fsync(fd)
            fsyncs.append(time.perf_counter() - started)

        work, total = [], []
        with mock.patch.object(os, "fsync", timed_fsync):
            for run in range(3):
                out = tmp_path / f"out{run}.json"
                fsyncs.clear()
                started = time.perf_counter()
                report = saver.save(doc, out) if run < 2 else saver.save(doc)   # the last one in place
                elapsed = time.perf_counter() - started
                mb = report.bytes_written / 1e6
                total.append(mb / elapsed)
                work.append(mb / max(1e-9, elapsed - sum(fsyncs)))
                assert report.records == count and report.edited_records == 100
                if run < 2:
                    os.remove(out)
        record_property("save_mb_per_s", [round(r) for r in work])
        record_property("save_total_mb_per_s", [round(r) for r in total])
        record_property("disk_mb_per_s", round(disk))
        assert max(work) >= TARGET_MB_S, (
            f"save speed without the fsync {[round(r) for r in work]} MB/s < {TARGET_MB_S} "
            f"(with it {[round(r) for r in total]} MB/s; the disk alone {round(disk)} MB/s)")
        new = Document.open(src, **report.reopen_options)
        try:
            new.build_index()
            assert new.record_count == count and new.error is None
        finally:
            new.close()
    finally:
        doc.close()
