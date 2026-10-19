"""Performance smoke test of the bulk operations (docs/ARCHITECTURE.md §12.6) on 1,000,000 records.

A JSON Lines file of 1,000,000 records (~80 MB): a prefiltered text replace (10 % of the records change), a
``set_field`` that changes EVERY record (1,000,000 units written to the preview's segment, the pending
array layer written through the spill path) and a ``dedupe``; the speeds are printed (``pytest -s``) and
logged. The applied ``set_field`` step is checked on a sample of records and by one full read of the edited
document (its speed is reported too). Marked ``slow``.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path

import pytest

from jsonreshape import codec
from jsonreshape.bulk import BulkSpec, make_step, preview
from jsonreshape.editstore import EditStore, apply_step
from jsonreshape.reader import Document

pytestmark = pytest.mark.slow

log = logging.getLogger(__name__)

N = 1_000_000
CITIES = [b"Toshkent", b"Samarqand", b"Buxoro", b"Namangan", b"Andijon", b"Xiva", b"Qarshi", b"Navoiy", b"Termiz",
          b"Urganch"]


def report(what: str, size: int, seconds: float) -> float:
    rate = size / 1e6 / max(seconds, 1e-9)
    line = f"bulk perf: {what}: {seconds:.2f} s, {rate:.1f} MB/s ({size / 1e6:.1f} MB)"
    print(line)
    log.info(line)
    return rate


def test_one_million_records(tmp_path: Path) -> None:
    path = tmp_path / "big.jsonl"
    with open(path, "wb") as f:
        f.write(b"".join(b'{"id":%d,"name":"user %d","city":"%s","score":%d.5,"tags":["a","b"]}\n'
                         % (i, i, CITIES[i % 10], i % 1000) for i in range(N)))
    size = path.stat().st_size
    doc = Document.open(path)
    store = None
    try:
        doc.build_index()
        assert doc.record_count == N
        store = EditStore.open(doc, dir=tmp_path / "layer")
        state = store.load_state()

        t0 = time.perf_counter()
        pv = preview(doc.pinned(), state, store, BulkSpec("replace", {"find": "toshkent", "replacement": "Tashkent"}),
                     None)
        report("replace (prefiltered, 10% changed)", size, time.perf_counter() - t0)
        assert (pv.scanned, pv.records_changed, pv.values_changed) == (N, N // 10, N // 10)
        pv.discard()

        t0 = time.perf_counter()
        pv = preview(doc.pinned(), state, store, BulkSpec("set_field", {"path": "country", "value": '"UZ"'}), None)
        report("set_field (every record changed)", size, time.perf_counter() - t0)
        assert pv.records_changed == N and pv.values_changed == N and pv.records is None
        assert pv.bytes_new == size + N * len(b',"country":"UZ"') - N   # each unit: the record + the member

        t0 = time.perf_counter()
        dd = preview(doc.pinned(), state, store, BulkSpec("dedupe", {"key": "city"}), None)
        report("dedupe by city", size, time.perf_counter() - t0)
        assert dd.records_changed == N - 10
        dd.discard()

        step = make_step(pv)
        store.commit(step, state=state)
        state = apply_step(state, step, layers=store)
        doc.set_overlay(state)
        view = doc.pinned()
        rng = random.Random(1)
        for pos in [0, N - 1] + rng.sample(range(N), 200):
            rec = codec.loads(view.record_bytes(pos))
            assert rec["id"] == pos and rec["country"] == "UZ" and list(rec)[-1] == "country"
        t0 = time.perf_counter()
        total = 0
        count = 0
        for blk in view.iter_blocks():
            total += blk.selected_bytes
            count += len(blk.indices)
        report("reading the edited document", total, time.perf_counter() - t0)
        assert count == N
        pv.discard()
    finally:
        if store is not None:
            store.close()
        doc.close()
