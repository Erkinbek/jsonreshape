"""A 200 MB JSON Patch is streamed with bounded memory (docs/ARCHITECTURE.md §12.8; AGENT_PROMPT §1.1).

The patch (20 000 ``replace`` operations of ~10 KB strings, more than ``KEEP_OPS_BYTES``: read twice, never
kept) is applied to a 30 000-record JSON Lines file with ``memory_limit_bytes`` = 64 MiB (a 16 MiB record
cache): the Python allocations (tracemalloc) stay far below the patch size, the changed records go to ONE
array layer (> 10 000), the result is exact, undo restores the file's records. Then the net effect is exported
(a ~200 MB patch written as a stream) with the same bound.
"""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest

from jsonreshape import codec, jsonpatch
from jsonreshape.edits import EditSession
from jsonreshape.overlay import ArrayLayer
from jsonreshape.reader import Document
from jsonreshape.settings import Settings

pytestmark = pytest.mark.slow

RECORDS = 30_000
CHANGED = 20_000
VALUE = 10_000


def write_patch(path: Path) -> int:
    with open(path, "wb") as f:
        f.write(b"[")
        for i in range(CHANGED):
            text = (f"r{i:05d}-" * (VALUE // 7 + 1))[:VALUE]
            f.write((b",\n" if i else b"\n") + b'{"op": "replace", "path": "/' + str(i * 3 // 2).encode()
                    + b'/s", "value": "' + text.encode("ascii") + b'"}')
        f.write(b'\n,{"op": "test", "path": "/0/id", "value": 0}]\n')
    return path.stat().st_size


def test_200_mb_patch_with_bounded_memory(tmp_path: Path) -> None:
    src = tmp_path / "big.jsonl"
    with open(src, "wb") as f:
        for i in range(RECORDS):
            f.write(b'{"id":' + str(i).encode() + b',"s":"","n":1.10}\n')
    patch = tmp_path / "patch.json"
    size = write_patch(patch)
    assert size > 200_000_000
    settings = Settings(memory_limit_bytes=64 << 20)
    doc = Document.open(src, settings=settings)
    doc.build_index()
    s = EditSession.open(doc, settings=settings)
    try:
        tracemalloc.start()
        try:
            cs = s.import_patch(patch)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 120_000_000, f"peak Python memory {peak / 1e6:.1f} MB for a {size / 1e6:.0f} MB patch"
        assert cs.records is None and isinstance(s.state.layers[-2], ArrayLayer)
        v = doc.pinned()
        # operation i replaced position i * 3 // 2: positions p with p % 3 != 2, by operation (2p + 1) // 3
        for p in (0, 1, 2, 1500, 14_998, 29_998, 29_999):
            rec = codec.loads(v.record_bytes(p))
            assert rec["id"] == p and codec.number_text(rec["n"]) == "1.10"
            if p % 3 != 2:
                assert rec["s"].startswith(f"r{(2 * p + 1) // 3:05d}-") and len(rec["s"]) == VALUE, p
            else:
                assert rec["s"] == "", p
        out = tmp_path / "net.json"
        tracemalloc.start()
        try:
            n = jsonpatch.export_patch(s, out)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert n == CHANGED and out.stat().st_size > 200_000_000
        assert peak < 120_000_000, f"export peak {peak / 1e6:.1f} MB"
        s.undo()
        assert doc.pinned().record_bytes(0) == b'{"id":0,"s":"","n":1.10}'
    finally:
        s.close()
        doc.close()
