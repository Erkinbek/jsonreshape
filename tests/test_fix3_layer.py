"""Stage-3 fix round: lifetime and integrity of the edit layer (review3_concurrency_verified CL-1, CL-2, CL-10,
CL-12; review3_safety S4, S6, S7).

* CL-1: a layer made after "Discard all changes" never reuses the segment / layer-file names of the old layer in
  the same folder (a reader of an old pinned state that opens a segment lazily, in a new thread, must read the OLD
  bytes), and the node child caches are keyed by the edit layer, so two states with equal virtual offsets never
  share an entry.
* CL-2 / CL-10: files that alive states still read (redo truncation, discard, save, a head-0 close) are deleted
  only when the last such state is gone -- then every reader handle on them is closed and the files are deleted.
* CL-12: ``EditSession.interrupt()`` / ``close()`` from another thread stop a command that waits for the
  record-key index.
* S4: the lock is taken on the PHYSICAL file too: the same file opened through a directory junction cannot be
  edited twice.
* S6: a failure while deleting the layer after the file was saved is a warning of the returned report.
* S7: the bytes of every step (and of the steps folded into the checkpoint) are checksummed and verified when a
  layer is loaded.
"""

from __future__ import annotations

import gc
import json
import os
import random
import sqlite3
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable

import pytest

from jsonreshape import editstore
from jsonreshape.bulk import BulkSpec
from jsonreshape.edits import EditSession, KeyIndex, Ref
from jsonreshape.editstore import EditStore, LayerDamagedError, LayerLock, crc32_combine, file_lock_key, layer_key
from jsonreshape.errors import Cancelled, EditError
from jsonreshape.i18n import t
from jsonreshape.overlay import EDIT_BASE, readers_of, split_edit_offset
from jsonreshape.reader import Document

from overlay_helpers import OverlayBuilder


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def closing():
    objs: list[Any] = []
    yield objs
    for obj in reversed(objs):
        try:
            obj.close()
        except Exception:
            pass


def write_array(path: Path, n: int = 40, *, indent: int | None = 2) -> Path:
    path.write_text(json.dumps([{"id": i, "v": f"val{i:04d}", "o": {"a": i, "b": "x"}} for i in range(n)],
                               indent=indent), encoding="utf-8")
    return path


def open_doc(path: Path, closing: list, **kw: Any) -> Document:
    doc = Document.open(path, **kw)
    closing.append(doc)
    doc.build_index()
    return doc


def in_thread(fn: Callable[[], Any]) -> tuple[str, Any]:
    """Run ``fn`` in a NEW thread (a reader that opens its segment handles now) and return its outcome."""
    out: list[tuple[str, Any]] = []

    def run() -> None:
        try:
            out.append(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            out.append(("error", exc))

    th = threading.Thread(target=run)
    th.start()
    th.join(60)
    assert not th.is_alive()
    return out[0]


def wait_until(cond: Callable[[], bool], timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def files_of(folder: Path) -> set[str]:
    return set(os.listdir(folder)) if folder.exists() else set()


def collect() -> None:
    """Drop unreachable states and delete the retained files whose readers are gone (deterministic, no reaper)."""
    gc.collect()
    editstore._sweep_retained()


# ----------------------------------------------------------------------------------
# CL-1: names never reused, child caches keyed by layer
# ----------------------------------------------------------------------------------
def test_discard_then_new_layer_never_reads_the_new_bytes(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "a.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    es.set_value(Ref(5, ("v",)), '"OLD-AAAA"')
    old = doc.pinned()                                   # e.g. an export queued now
    old_dir = es.store.dir
    old_point = es.store.point_segment
    es.discard()
    es.set_value(Ref(7, ("v",)), '"NEW-BBBB"')           # same unit length as the old record 5
    assert es.store.dir == old_dir
    assert es.store.point_segment != old_point           # a new name in the same folder
    status, data = in_thread(lambda: old.record_bytes(5))
    assert status == "ok" and b"OLD-AAAA" in data and b'"id": 5' in data
    assert in_thread(lambda: old.record(5))[1]["v"] == "OLD-AAAA"
    assert b"NEW-BBBB" in in_thread(lambda: doc.record_bytes(7))[1]
    # the virtual offsets of the two layers differ too (§12.0: unique offsets may key caches)
    assert split_edit_offset(old.record_span(5)[0])[0] != split_edit_offset(doc.record_span(7)[0])[0]


def test_new_segments_continue_the_numbers_of_the_folder(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "a.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    es.set_value(Ref(1, ("v",)), '"x"')
    first = es.store.new_segment()
    es.store.drop_segment(first)
    es.discard()
    es.set_value(Ref(2, ("v",)), '"y"')
    store = es.store
    assert store.point_segment > first
    assert store.new_segment() > store.point_segment
    # the number survives a reopen of the layer (meta point_seg)
    es.close()
    es2 = EditSession.open(doc)
    closing.append(es2)
    assert es2.store.point_segment == store.point_segment
    es2.set_value(Ref(3, ("v",)), '"z"')
    assert split_edit_offset(doc.record_span(3)[0])[0] == store.point_segment
    assert doc.record(2)["v"] == "y" and doc.record(3)["v"] == "z"


def test_child_caches_of_two_layers_with_equal_offsets_never_mix(tmp_path, closing) -> None:
    src = tmp_path / "c.json"
    src.write_text(json.dumps([{"id": i, "o": {"a": i, "b": "x"}} for i in range(12)]), encoding="utf-8")
    doc = open_doc(src, closing)
    one = OverlayBuilder(doc, tmp_path / "layer1")
    two = OverlayBuilder(doc, tmp_path / "layer2")
    closing.extend([one, two])
    one.set(5, b'{"id": 5, "o": {"OLDKEY1": 1, "OLDKEY2": 2}}')
    two.set(5, b'{"id": 5, "o": {"N": 1, "Q": [1, 2, 3], "R": 3, "S": 4}}')
    v1, v2 = doc.pinned(one.state()), doc.pinned(two.state())
    n1, n2 = v1.record_node(5).find_child("o"), v2.record_node(5).find_child("o")
    assert n1.start == n2.start and n1.start >= EDIT_BASE         # the same virtual offset in both layers
    assert [c.path[-1] for c in n1.children()] == ["OLDKEY1", "OLDKEY2"]
    assert [c.path[-1] for c in n2.children()] == ["N", "Q", "R", "S"]
    assert n2.value() == {"N": 1, "Q": [1, 2, 3], "R": 3, "S": 4}
    assert v1._children_key(n1.start) != v2._children_key(n2.start)
    assert v1._children_key(100) == 100                           # base offsets keep their key


# ----------------------------------------------------------------------------------
# CL-2 / CL-10: deferred deletion while states can read the files
# ----------------------------------------------------------------------------------
def _bulk(es: EditSession) -> None:
    pv = es.bulk_preview(BulkSpec("replace", {"find": "val", "replacement": "BULK", "mode": "text"}))
    es.bulk_apply(pv)


def test_redo_truncation_keeps_the_files_a_pinned_state_reads(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "t.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    _bulk(es)
    pinned = doc.pinned()                           # an export of the bulk result, still queued
    bulk_seg = split_edit_offset(pinned.record_span(3)[0])[0]
    folder = es.store.dir
    es.undo()
    es.set_value(Ref(1, ("v",)), '"after-undo"')   # truncates the bulk step
    seg_name = f"seg-{bulk_seg:05d}.dat"
    assert seg_name in files_of(folder)
    status, data = in_thread(lambda: pinned.record_bytes(3))
    assert status == "ok" and b"BULK0003" in data, data
    assert in_thread(lambda: [r["v"] for _i, r in pinned.iter_records()])[1][:3] == ["BULK0000", "BULK0001", "BULK0002"]
    # the last reader of the truncated step goes: the file and every handle on it go too
    del pinned, status, data
    collect()
    assert seg_name not in files_of(folder) and not any(n.startswith("layer-") for n in files_of(folder))
    for r in readers_of(folder):
        assert all(bulk_seg not in handles for _ref, handles in r._registry)
        assert r.published(bulk_seg) == 0
    assert doc.record(1)["v"] == "after-undo" and doc.record(3)["v"] == "val0003"


def test_truncated_files_go_at_once_when_nothing_reads_them(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "t.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    _bulk(es)
    folder = es.store.dir
    reader = es.store.reader
    doc.record_bytes(3)                             # this thread opens a handle on the bulk segment
    bulk = {n for n in files_of(folder) if n.startswith(("seg-", "layer-")) and n != "seg-00000.dat"}
    assert bulk
    es.undo()
    es.set_value(Ref(4, ("id",)), "44")
    assert not bulk & files_of(folder)              # deleted right after the commit (CL-10)
    assert editstore.retained_files() == []
    assert all(not any(int(n[4:9]) in handles for n in bulk if n.startswith("seg-"))
               for _ref, handles in reader._registry)


def test_discard_keeps_the_layer_for_a_pinned_state_until_it_is_released(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "d.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    _bulk(es)
    es.set_value(Ref(2, ("v",)), '"point"')
    pinned = doc.pinned()
    folder = es.store.dir
    es.discard()
    assert doc.record(2)["v"] == "val0002"
    status, data = in_thread(lambda: (pinned.record(2), pinned.record(9)))
    assert status == "ok" and data == ({"id": 2, "v": "point", "o": {"a": 2, "b": "x"}},
                                       {"id": 9, "v": "BULK0009", "o": {"a": 9, "b": "x"}}), data
    assert not (folder / editstore.DB_NAME).exists()      # tombstoned and gone: never offered for recovery
    assert editstore.retained_files()
    del pinned, status, data
    # the reaper deletes the files when the last state is gone (no explicit sweep here)
    gc.collect()
    assert wait_until(lambda: not editstore.retained_files())
    assert wait_until(lambda: not folder.exists() or not files_of(folder))


def test_discard_without_readers_deletes_at_once(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "d.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    es.set_value(Ref(2, ("v",)), '"point"')
    folder = es.store.dir
    es.discard()
    gc.collect()
    assert wait_until(lambda: not folder.exists())


def test_save_as_keeps_the_layer_while_the_document_still_shows_it(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "s.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    _bulk(es)
    es.set_value(Ref(4, ("v",)), '"saved"')
    pinned = doc.pinned()
    folder = es.store.dir
    report = es.save(tmp_path / "copy.json")
    assert es.closed and report.records == 40
    status, data = in_thread(lambda: [r["v"] for _i, r in pinned.iter_records()])
    assert status == "ok" and data[4] == "saved" and data[5] == "BULK0005", data
    del pinned, status, data
    doc.close()                                     # the document's readers close: nothing reads the layer now
    collect()
    assert not folder.exists() or not files_of(folder)


def test_in_place_save_keeps_the_segments_for_a_pinned_state(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "s.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    es.set_value(Ref(4, ("v",)), '"saved"')
    pinned = doc.pinned()
    es.save()
    status, data = in_thread(lambda: pinned.record_bytes(4))     # an edited record: read from the segment
    assert status == "ok" and b'"saved"' in data, data


def test_head0_close_keeps_the_layer_for_a_pinned_state(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "h.json"), closing)
    es = EditSession.open(doc)
    closing.append(es)
    es.set_value(Ref(6, ("v",)), '"undone"')
    pinned = doc.pinned()
    es.undo()                                       # head 0: close() deletes the layer
    folder = es.store.dir
    es.close()
    status, data = in_thread(lambda: pinned.record_bytes(6))
    assert status == "ok" and b'"undone"' in data
    assert not (folder / editstore.DB_NAME).exists()
    del pinned, status, data
    collect()
    assert not folder.exists() or not files_of(folder)


def test_a_retained_file_replaced_by_another_layer_is_left_alone(tmp_path, closing) -> None:
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "seg-00003.dat").write_bytes(b"old bytes")
    doc = open_doc(write_array(tmp_path / "r.json"), closing)
    ob = OverlayBuilder(doc, folder)
    closing.append(ob)
    st = ob.state()
    assert editstore._defer_delete(folder, ["seg-00003.dat"], [st])
    (folder / "seg-00003.dat").unlink()
    (folder / "seg-00003.dat").write_bytes(b"a new layer's segment")      # another process's new file
    del st
    collect()
    assert (folder / "seg-00003.dat").read_bytes() == b"a new layer's segment"


# ----------------------------------------------------------------------------------
# CL-12: interrupting a command that waits for the key index
# ----------------------------------------------------------------------------------
def _blocking_build(started: threading.Event) -> Callable[..., Any]:
    def build(self: KeyIndex, s0: Any, *, progress: Any, cancel: Any) -> Any:
        started.set()
        cancel.wait()               # until the build is cancelled
        cancel.check()
        raise AssertionError("unreachable")
    return build


@pytest.mark.parametrize("how", ["interrupt", "close"])
def test_a_key_command_waiting_for_the_index_is_interrupted(tmp_path, closing, monkeypatch, how) -> None:
    src = tmp_path / "k.json"
    src.write_text(json.dumps({f"k{i}": {"v": i} for i in range(30)}, indent=1), encoding="utf-8")
    doc = open_doc(src, closing)
    assert doc.kind == "keyed"
    started = threading.Event()
    monkeypatch.setattr(KeyIndex, "_build", _blocking_build(started))
    es = EditSession.open(doc)
    closing.append(es)
    outcome: list[BaseException | None] = []

    def command() -> None:
        try:
            es.rename_key(Ref(0, ()), "renamed")
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 - checked below
            outcome.append(exc)

    th = threading.Thread(target=command)
    th.start()
    assert started.wait(20)
    if how == "interrupt":
        es.interrupt()              # e.g. the GUI's detach, from the GUI thread: never blocks
    else:
        closer = threading.Thread(target=es.close)
        closer.start()
        closer.join(20)
        assert not closer.is_alive() and es.closed
    th.join(20)
    assert not th.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], Cancelled)
    assert doc.record_key(0) == "k0"


# ----------------------------------------------------------------------------------
# S4: the physical file is locked under every spelling
# ----------------------------------------------------------------------------------
def _junction(target: Path, link: Path) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError):
        return False
    return link.exists()


def test_the_same_file_under_two_spellings_is_edited_once(tmp_path, closing) -> None:
    real = tmp_path / "real"
    real.mkdir()
    write_array(real / "data.json")
    link = tmp_path / "link"
    if not _junction(real, link):
        pytest.skip("directory junctions are not available")
    try:
        d1 = open_doc(real / "data.json", closing)
        d2 = open_doc(link / "data.json", closing)
        assert layer_key(d1) != layer_key(d2)            # layer keys (and folders) are unchanged
        assert file_lock_key(d1) == file_lock_key(d2) is not None
        assert file_lock_key(d1) != file_lock_key(write_array(tmp_path / "other.json"))
        es = EditSession.open(d1)
        closing.append(es)
        other = LayerLock.for_doc(d2)
        assert other.key == layer_key(d2)
        assert not other.try_acquire() and not other.held
        with pytest.raises(EditError) as exc:
            EditSession.open(d2)
        assert exc.value.key == "edits.error.locked"
        es.close()
        assert other.try_acquire() and other.held
        assert not LayerLock.for_doc(d1).try_acquire()   # and the other way round
        other.release()
        es2 = EditSession.open(d2)
        closing.append(es2)
        es2.set_value(Ref(0, ("v",)), '"via the junction"')
        assert d2.record(0)["v"] == "via the junction"
    finally:
        for obj in reversed(closing):
            try:
                obj.close()
            except Exception:
                pass
        closing.clear()
        os.rmdir(link)


def test_a_lock_without_a_file_key_is_unchanged(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "p.json"), closing)
    plain = LayerLock(layer_key(doc))
    assert plain.file_key is None and plain.try_acquire()
    whole = LayerLock.for_doc(doc)
    assert not whole.try_acquire()                  # its layer key is held
    plain.release()
    assert whole.try_acquire() and whole.held
    assert LayerLock.is_locked(whole.file_key) and LayerLock.is_locked(whole.key)
    whole.release()
    assert not LayerLock.is_locked(whole.file_key) and not LayerLock.is_locked(whole.key)


# ----------------------------------------------------------------------------------
# S6: a failure after the file was saved is a warning
# ----------------------------------------------------------------------------------
def test_cleanup_failure_after_saving_is_a_warning(tmp_path, closing, monkeypatch) -> None:
    src = write_array(tmp_path / "w.json")
    doc = open_doc(src, closing)
    es = EditSession.open(doc)
    closing.append(es)
    es.set_value(Ref(2, ("v",)), '"saved edit"')
    folder = es.store.dir

    def fail(self: EditStore) -> None:
        raise EditError("editstore.error.disk_full", path=str(self.dir))

    monkeypatch.setattr(EditStore, "mark_discarded", fail)
    report = es.save()
    assert b'"saved edit"' in src.read_bytes()
    assert "edits.warn.layer_left" in report.warning_keys
    detail = EditError("editstore.error.disk_full", path=str(folder)).message
    assert report.warnings[-1] == t("edits.warn.layer_left", detail=detail)
    assert es.closed and es.store is None
    with pytest.raises(EditError) as exc:
        es.set_value(Ref(2, ("v",)), '"again"')
    assert exc.value.key == "edits.error.closed"
    # the store is closed (its database released) and the lock is free
    monkeypatch.undo()
    assert not LayerLock.is_locked(layer_key(doc))
    con = sqlite3.connect(str(folder / editstore.DB_NAME), timeout=0.1)
    try:
        con.execute("PRAGMA locking_mode=EXCLUSIVE")
        assert con.execute("SELECT value FROM meta WHERE key = 'state'").fetchone()[0] == "live"
    finally:
        con.close()


# ----------------------------------------------------------------------------------
# S7: checksums of the layer's bytes
# ----------------------------------------------------------------------------------
def test_crc32_combine_matches_zlib() -> None:
    rng = random.Random(7)
    for _ in range(40):
        a = rng.randbytes(rng.randint(0, 3000))
        b = rng.randbytes(rng.randint(0, 3000))
        assert crc32_combine(zlib.crc32(a), zlib.crc32(b), len(b)) == zlib.crc32(a + b)
    big = b"\x00" * (1 << 20) + b"tail"
    assert crc32_combine(zlib.crc32(b"head"), zlib.crc32(big), len(big)) == zlib.crc32(b"head" + big)


def _garble(path: Path, at: int, data: bytes) -> None:
    with open(path, "r+b") as f:
        f.seek(at)
        f.write(data)


def _span_in_file(doc: Document, pos: int) -> tuple[str, int, int]:
    s, e = doc.record_span(pos)
    seg, off = split_edit_offset(s)
    return f"seg-{seg:05d}.dat", off, off + (e - s)


def test_a_garbled_unit_is_reported_as_a_damaged_step(tmp_path, closing) -> None:
    src = write_array(tmp_path / "g.json")
    doc = open_doc(src, closing)
    es = EditSession.open(doc)
    for i in range(5):
        es.set_value(Ref(i, ("v",)), f'"good step {i} with a longer text"')
    name, s, _e = _span_in_file(doc, 4)
    folder = es.store.dir
    es.close()
    _garble(folder / name, s + 12, b"XXXXXXXXXX")     # the size is unchanged (only a checksum sees it)
    with pytest.raises(LayerDamagedError) as exc:
        EditSession.open(doc)
    assert exc.value.seq == 5 and exc.value.last_good == 4
    es2 = EditSession.open(doc, upto=4)                # the steps before it can be restored
    closing.append(es2)
    assert [doc.record(i)["v"] for i in range(4)] == [f"good step {i} with a longer text" for i in range(4)]
    assert doc.record(4)["v"] == "val0004"


def test_a_garbled_bulk_segment_is_reported(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "b.json"), closing)
    es = EditSession.open(doc)
    es.set_value(Ref(0, ("v",)), '"first"')
    _bulk(es)
    name, s, _e = _span_in_file(doc, 20)
    folder = es.store.dir
    es.close()
    _garble(folder / name, s + 3, b"Z")
    store = EditStore.open(doc)
    try:
        with pytest.raises(LayerDamagedError) as exc:
            store.load_state()
        assert exc.value.seq == 2 and exc.value.last_good == 1
        assert store.load_state(upto=1).version == 1
    finally:
        store.close()


def test_folded_steps_keep_their_checksums_in_the_checkpoint(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "c.json", 80), closing)
    es = EditSession.open(doc)
    for i in range(70):
        es.set_value(Ref(i, ("v",)), f'"step {i:03d}"')
    es.undo()
    es.undo()
    es.set_value(Ref(1, ("v",)), '"after a truncation"')       # a gap of truncated bytes in the segment
    store = es.store
    store.maybe_trim(0)                                         # fold everything into the checkpoint
    assert store.checkpoint_seq == 64
    name, s, _e = _span_in_file(doc, 3)                         # a unit of a folded step
    folder = store.dir
    es.close()
    cp = sqlite3.connect(str(folder / editstore.DB_NAME))
    try:
        data = json.loads(cp.execute("SELECT data FROM checkpoint WHERE id = 1").fetchone()[0])
    finally:
        cp.close()
    assert data["crc"] and len(data["crc"]) <= 2
    es_ok = EditSession.open(doc)                               # intact: loads
    es_ok.close()
    _garble(folder / name, s + 8, b"Q")
    with pytest.raises(LayerDamagedError) as exc:
        EditSession.open(doc)
    assert exc.value.last_good is None                          # the checkpoint itself is damaged


def test_rows_without_checksums_still_load(tmp_path, closing) -> None:
    doc = open_doc(write_array(tmp_path / "o.json"), closing)
    es = EditSession.open(doc)
    for i in range(3):
        es.set_value(Ref(i, ("v",)), f'"v{i}"')
    folder = es.store.dir
    es.close()
    con = sqlite3.connect(str(folder / editstore.DB_NAME))
    try:
        rows = con.execute("SELECT seq, data FROM steps").fetchall()
        assert all("crc" in json.loads(d) for _s, d in rows)
        for seq, d in rows:
            obj = json.loads(d)
            del obj["crc"]
            con.execute("UPDATE steps SET data = ? WHERE seq = ?", (json.dumps(obj), seq))
        con.commit()
    finally:
        con.close()
    es2 = EditSession.open(doc)
    closing.append(es2)
    assert [doc.record(i)["v"] for i in range(3)] == ["v0", "v1", "v2"]
