"""Stage-6 performance work P6 on restoring an edit layer (docs/ARCHITECTURE.md §12.2, "Checksums"): a big
checksummed range -- the segment of a bulk step -- is read and checksummed in pieces by several threads at once
(``EditStore._crc_range``, joined with ``crc32_combine``). The values are zlib's crc32 of the whole range, a
damaged byte anywhere in it is still found before the state is returned, cancellation still works."""

from __future__ import annotations

import random
import threading
import zlib
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import editstore
from jsonreshape.bulk import BulkSpec
from jsonreshape.edits import EditSession, Ref
from jsonreshape.editstore import EditStore, LayerDamagedError
from jsonreshape.errors import Cancelled
from jsonreshape.overlay import split_edit_offset
from jsonreshape.reader import Document
from jsonreshape.tasks import CancelToken


@pytest.fixture
def closing():
    objs: list[Any] = []
    yield objs
    for obj in reversed(objs):
        try:
            obj.close()
        except Exception:
            pass


@pytest.fixture
def small_ranges(monkeypatch):
    """Ranges of 4 KiB or more are checked by threads, read in pieces of 512 bytes."""
    monkeypatch.setattr(editstore, "_CRC_PARALLEL_MIN", 4096)
    monkeypatch.setattr(editstore, "_CRC_READ", 512)
    monkeypatch.setattr(editstore, "_CRC_THREADS", 4)
    monkeypatch.setattr(editstore.os, "cpu_count", lambda: 8)


def layer_with_bulk(tmp_path: Path, closing: list, n: int = 400) -> tuple[Document, Path, str, int, int]:
    """A document with a point edit and a bulk step (every record changed): ``(doc, layer folder, bulk segment
    file name, start, end of its bytes)``."""
    src = tmp_path / "d.json"
    recs = ['{"id": %d, "v": "val%04d Ўзбек oʻzbek", "n": [0.1, 1e400, -0.0, 12345678901234567890]}' % (i, i)
            for i in range(n)]
    src.write_text("[\n" + ",\n".join(recs) + "\n]\n", encoding="utf-8")
    doc = Document.open(src)
    closing.append(doc)
    doc.build_index()
    es = EditSession.open(doc)
    es.set_value(Ref(0, ("v",)), '"first"')
    es.bulk_apply(es.bulk_preview(BulkSpec("replace", {"find": "val", "replacement": "BULK", "mode": "text"})))
    starts, ends = [], []
    seg = None
    for pos in range(1, n):
        s, e = doc.record_span(pos)
        sg, off = split_edit_offset(s)
        seg = sg if seg is None else seg
        assert sg == seg
        starts.append(off)
        ends.append(off + (e - s))
    folder = es.store.dir
    es.close()
    return doc, folder, f"seg-{seg:05d}.dat", min(starts), max(ends)


def test_the_threads_give_zlibs_crc32(tmp_path, closing, small_ranges, monkeypatch) -> None:
    doc, folder, name, s0, e0 = layer_with_bulk(tmp_path, closing)
    raw = (folder / name).read_bytes()
    seg = int(name[4:9])
    store = EditStore.open(doc)
    closing.append(store)
    store.load_state()
    seen: set[int] = set()
    real = EditStore._crc_extend

    def spy(self, sg: int, start: int, end: int, crc: int = 0, cancel: Any = None) -> int:
        seen.add(threading.get_ident())
        return real(self, sg, start, end, crc, cancel)

    monkeypatch.setattr(EditStore, "_crc_extend", spy)
    rng = random.Random(3)
    cases = [(s0, e0), (s0, s0 + 4096), (s0 + 1, e0 - 1), (s0, s0 + 4095)]
    cases += [tuple(sorted(rng.sample(range(s0, e0 + 1), 2))) for _ in range(20)]
    for a, b in cases:
        assert store._crc_range(seg, a, b) == zlib.crc32(raw[a:b]), (a, b)
    assert len(seen - {threading.get_ident()}) >= 2            # several threads read the big ranges
    assert e0 - s0 > 4 * 4096


@pytest.mark.parametrize("where", [0.0, 0.37, 0.74, 1.0])
def test_a_damaged_byte_anywhere_in_a_big_range_is_found(tmp_path, closing, small_ranges, where) -> None:
    doc, folder, name, s0, e0 = layer_with_bulk(tmp_path, closing)
    es = EditSession.open(doc)                                  # intact: restores
    assert es.state.version == 2
    es.close()
    path = folder / name
    data = bytearray(path.read_bytes())
    at = min(e0 - 1, s0 + int((e0 - s0) * where))
    data[at] ^= 0x01                                            # the size is unchanged: only a checksum sees it
    path.write_bytes(bytes(data))
    store = EditStore.open(doc)
    closing.append(store)
    with pytest.raises(LayerDamagedError) as exc:
        store.load_state()
    assert exc.value.seq == 2 and exc.value.last_good == 1
    assert store.load_state(upto=1).version == 1


def test_cancel_while_the_threads_check(tmp_path, closing, small_ranges) -> None:
    doc, _folder, _name, _s0, _e0 = layer_with_bulk(tmp_path, closing)
    store = EditStore.open(doc)
    closing.append(store)
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        store.load_state(cancel=tok)
    assert store.load_state().version == 2


def test_small_ranges_and_one_cpu_stay_in_this_thread(tmp_path, closing, monkeypatch) -> None:
    doc, folder, name, s0, e0 = layer_with_bulk(tmp_path, closing, n=60)
    raw = (folder / name).read_bytes()
    store = EditStore.open(doc)
    closing.append(store)
    calls: list[int] = []
    real = EditStore._crc_extend

    def spy(self, sg: int, start: int, end: int, crc: int = 0, cancel: Any = None) -> int:
        calls.append(threading.get_ident())
        return real(self, sg, start, end, crc, cancel)

    monkeypatch.setattr(EditStore, "_crc_extend", spy)
    seg = int(name[4:9])
    assert store._crc_range(seg, s0, e0) == zlib.crc32(raw[s0:e0])       # far below _CRC_PARALLEL_MIN
    monkeypatch.setattr(editstore, "_CRC_PARALLEL_MIN", 1)
    monkeypatch.setattr(editstore.os, "cpu_count", lambda: 1)
    assert store._crc_range(seg, s0, e0) == zlib.crc32(raw[s0:e0])
    assert set(calls) == {threading.get_ident()} and len(calls) == 2
