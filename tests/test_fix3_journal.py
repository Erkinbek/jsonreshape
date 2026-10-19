"""Stage-3 fix round, saver journal (review3_safety S1, S2; ARCHITECTURE §12.7).

S1 -- "Finish the save" must never take an interrupted save for a completed one: the content hashes of a
file identity cover only ~2 MiB, so a same-size edit outside them in a bigger file makes the old and the new
content look equal. Every test here uses such a file (> 2 MiB, one record changed with the same length
outside the hashed windows) and decides only by what the journal recorded (file ids, modification times):

* real crashes (a subprocess dies with ``os._exit``) at every step of the commit sequence, backup on / off,
  with / without a previous ``.bak``, POSIX renames and the two-rename fallback, journals of version 2 and the
  same journals downgraded to version 1, and a file system without stable file ids whose two versions even
  share their modification time -> the expected action and the files afterwards; "completed" never while the
  source still holds the original;
* the reviewer's scenarios with a real edit layer: a crash before ``replace_posix`` (``rj_partial_hash``,
  with and without a previous ``.bak``) and a save that failed at the replace and could not undo its ``.bak``
  link (``rj_failed_save``): the roll goes "back" (or, for the version-1 journal, "conflict" touching
  nothing), the layer stays and restores the edit;
* a damaged journal naming files outside the source's folder is "invalid" and touches nothing.

S2 -- an interrupted fallback save leaves the file missing: :func:`saver.find_journals` finds the journal
from the path alone and :func:`saver.roll_journal_at` rolls it forward / back under the layer lock and clears
it through the layer's store (the reviewer's ``rj_missing_source``, backup on and off; a version-1 journal).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jsonreshape import fileio, saver
from jsonreshape.editstore import EditStore, LayerLock, find_pending
from jsonreshape.edits import EditSession, Ref
from jsonreshape.reader import Document

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
N_RECORDS = 60000          # ~4 MB compact: > 2 MiB, so most records lie outside the hashed windows


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def big_array(path: Path, n: int = N_RECORDS) -> list[tuple[int, int]]:
    """A compact array of ``n`` records; returns every record's byte span."""
    spans = []
    out = bytearray(b"[")
    for i in range(n):
        rec = json.dumps({"id": i, "status": "open", "name": f"user {i:07d}", "tags": ["a", "b"]},
                         separators=(",", ":")).encode()
        spans.append((len(out), len(out) + len(rec)))
        out += rec
        if i < n - 1:
            out += b","
    out += b"]"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return spans


def unhashed_record(size: int, spans: list[tuple[int, int]]) -> int:
    """A record outside every range the identity hashes cover (head, tail, sampled pages)."""
    ranges = [(0, fileio.HEAD_HASH_BYTES), (size - fileio.TAIL_HASH_BYTES, size)] + fileio.sample_ranges(size)
    for i in range(len(spans) // 2, len(spans)):
        a, b = spans[i]
        if not any(lo < b and a < hi for lo, hi in ranges):
            return i
    raise AssertionError("no record outside the hashed windows")


def bak_of(p: Path) -> Path:
    return p.with_name(p.name + ".bak")


def leftovers(folder: Path) -> list[str]:
    return sorted(n for n in os.listdir(folder) if n.endswith((saver.TEMP_SUFFIX, ".old")))


def downgrade(journal: dict) -> dict:
    """The same journal as the previous version wrote it (version 1: no ids, no time of the new file)."""
    j = {k: v for k, v in journal.items() if k not in ("old_file", "ids", "layer", "bak_read_only")}
    j["v"] = 1
    j["new"] = {"size": journal["new"]["size"], "hashes": list(journal["new"]["hashes"])}
    return j


def snapshot(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


# ----------------------------------------------------------------------------------
# real crashes at every step of the commit (journal written to a file by FakeStore)
# ----------------------------------------------------------------------------------
CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r}); sys.path.insert(0, {tests!r})
    from jsonreshape import fileio, saver
    from jsonreshape.reader import Document
    from overlay_helpers import OverlayBuilder
    from saver_helpers import FakeStore
    src, layer, journal, point, backup, pos, mode = sys.argv[1:8]
    doc = Document.open(src); doc.build_index()
    ob = OverlayBuilder(doc, layer)
    rec = doc.record_bytes(int(pos))
    ob.set(int(pos), rec.replace(b'"open"', b'"done"'))          # the same length: equal size and hashes
    doc.set_overlay(ob.state())
    def die(*a, **k):
        os._exit(17)
    def unsupported(a, b):
        raise fileio.ReplaceUnsupported(None, "x", str(b), 50)
    class Store(FakeStore):
        def journal(self, data):
            if data is None and point == "before_clear":
                die()
            super().journal(data)
            if data is not None and point == "journal":
                die()
    if mode == "noids":         # a file system without stable ids whose versions share their time (FAT: 2 s)
        fileio.stable_file_ids = lambda p: False
        real_copy = saver._copy_attributes
        def same_time(source, tmp):
            real_copy(source, tmp)
            st = os.stat(source)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        saver._copy_attributes = same_time
    if mode == "fallback":
        fileio.replace_posix = unsupported
        fileio.hard_link = lambda a, b: False
    if point == "bak_moved":
        fileio.hard_link = die
    elif point == "before_replace":
        fileio.replace_posix = die
    elif point == "after_replace":
        real_replace = fileio.replace_posix
        def replace_then_die(a, b):
            real_replace(a, b)
            die()
        fileio.replace_posix = replace_then_die
    elif point == "between_renames":
        fileio.replace_file = die
    elif point == "after_fallback":
        real_rf = fileio.replace_file
        def rename_then_die(*a, **k):
            real_rf(*a, **k)
            die()
        fileio.replace_file = rename_then_die
    elif point == "cleanup":
        fileio.remove_quiet = die
    saver.save(doc, backup=backup == "1", store=Store(journal))
    os._exit(0)
""")

# (point, backup, previous .bak, mode, expected action of a version-2 journal)
CRASHES = [
    ("journal", True, True, "posix", "back"),
    ("bak_moved", True, True, "posix", "back"),
    ("before_replace", True, True, "posix", "back"),
    ("before_replace", True, False, "posix", "back"),
    ("before_replace", False, True, "posix", "back"),
    ("after_replace", True, True, "posix", "completed"),
    ("after_replace", False, False, "posix", "completed"),
    ("between_renames", True, True, "fallback", "forward"),
    ("between_renames", False, False, "fallback", "forward"),
    ("after_fallback", True, True, "fallback", "completed"),
    ("after_fallback", False, False, "fallback", "completed"),
    ("cleanup", True, True, "posix", "completed"),
    ("before_clear", True, True, "posix", "completed"),
    # no stable ids and the same modification time: whatever the evidence cannot tell apart is a conflict
    ("before_replace", True, True, "noids", "back"),
    ("after_replace", True, True, "noids", "conflict"),
]


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> tuple[bytes, int]:
    path = tmp_path_factory.mktemp("s1_template") / "data.json"
    spans = big_array(path)
    data = path.read_bytes()
    assert len(data) > fileio.HEAD_HASH_BYTES + fileio.TAIL_HASH_BYTES + (1 << 20)
    return data, unhashed_record(len(data), spans)


def crash(tmp_path: Path, template: tuple[bytes, int], point: str, backup: bool, had_bak: bool,
          mode: str) -> tuple[Path, dict, bytes, bytes]:
    """Run a save of ``data.json`` that dies at ``point``; returns (source, journal, original, edited)."""
    data, pos = template
    src = tmp_path / "data.json"
    src.write_bytes(data)
    if had_bak:
        bak_of(src).write_bytes(b'["the previous backup"]')
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=str(ROOT), tests=str(TESTS)), encoding="utf-8")
    journal_path = tmp_path / "journal.json"
    proc = subprocess.run([sys.executable, str(script), str(src), str(tmp_path / "layer"), str(journal_path), point,
                           "1" if backup else "0", str(pos), mode], env=dict(os.environ), capture_output=True,
                          timeout=300)
    assert proc.returncode == 17, proc.stderr.decode(errors="replace")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal is not None and journal["v"] == 2
    edited = data.replace(b'{"id":%d,"status":"open"' % pos, b'{"id":%d,"status":"done"' % pos, 1)
    assert len(edited) == len(data) and edited != data
    return src, journal, data, edited


def check_rolled(src: Path, action: str, original: bytes, edited: bytes, backup: bool, had_bak: bool) -> None:
    previous = b'["the previous backup"]'
    if action == "back":
        assert src.read_bytes() == original, "the file is the original again"
        if had_bak:
            assert bak_of(src).read_bytes() == previous, "the previous backup is back"
        else:
            assert not bak_of(src).exists(), "our backup link is gone"
    else:
        assert src.read_bytes() == edited, "the save is complete"
        if backup:
            assert bak_of(src).read_bytes() == original
        elif had_bak:
            assert bak_of(src).read_bytes() == previous
        else:
            assert not bak_of(src).exists()
    assert leftovers(src.parent) == []


@pytest.mark.parametrize("point, backup, had_bak, mode, action", CRASHES)
def test_s1_crash_at_every_step_is_rolled_by_evidence(tmp_path, template, point, backup, had_bak, mode,
                                                      action) -> None:
    src, journal, original, edited = crash(tmp_path, template, point, backup, had_bak, mode)
    if mode != "noids" and sys.platform == "win32":
        assert journal["ids"] is True
    before = snapshot(tmp_path)
    result = saver.roll_journal(journal)
    assert result.action == action, str(result)
    if action == "conflict":
        assert snapshot(tmp_path) == before, "a conflict touches nothing"
        assert str(src) in result and str(bak_of(src)) in result     # the files involved are named
        return
    check_rolled(src, action, original, edited, backup, had_bak)


@pytest.mark.parametrize("point, backup, had_bak, mode, action", [
    ("journal", True, True, "posix", "back"),
    ("before_replace", True, True, "posix", "back"),
    ("after_replace", True, True, "posix", "completed"),
    ("between_renames", False, False, "fallback", "forward"),
])
def test_s1_version_1_journals_are_still_rolled_safely(tmp_path, template, point, backup, had_bak, mode,
                                                       action) -> None:
    """Version 1 (no ids, no time of the new file): the temp file still there means the commit did not
    happen, and the modification times of the two versions still differ on NTFS."""
    src, journal, original, edited = crash(tmp_path, template, point, backup, had_bak, mode)
    result = saver.roll_journal(downgrade(journal))
    assert result.action == action, str(result)
    check_rolled(src, action, original, edited, backup, had_bak)


# ----------------------------------------------------------------------------------
# the reviewer's scenarios with a real edit layer
# ----------------------------------------------------------------------------------
REAL_CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r})
    from jsonreshape import fileio
    from jsonreshape.reader import Document
    from jsonreshape.edits import EditSession, Ref
    src, pos, how = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    doc = Document.open(src); doc.build_index()
    es = EditSession.open(doc)
    es.set_value(Ref(pos, ("status",)), '"done"')               # the same length as "open"
    def die(*a, **k):
        os._exit(17)
    if how == "before_replace":                                 # rj_partial_hash: power cut before the replace
        fileio.replace_posix = die
    else:                                                       # rj_missing_source: fallback, between renames
        def unsupported(a, b):
            raise fileio.ReplaceUnsupported(None, "x", str(b), 50)
        fileio.replace_posix = unsupported
        fileio.hard_link = lambda a, b: False
        fileio.replace_file = die
    es.save(backup=how != "between_renames_nobak")
    os._exit(0)
""")


def real_crash(tmp_path: Path, template: tuple[bytes, int], how: str, *, had_bak: bool = False) -> tuple[Path, int]:
    data, pos = template
    src = tmp_path / "data.json"
    src.write_bytes(data)
    if had_bak:
        bak_of(src).write_bytes(b'["the previous backup"]')
    script = tmp_path / "child.py"
    script.write_text(REAL_CHILD.format(root=str(ROOT)), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), str(src), str(pos), how], env=dict(os.environ),
                          capture_output=True, timeout=300)
    assert proc.returncode == 17, proc.stderr.decode(errors="replace")
    return src, pos


def open_doc(path: Path) -> Document:
    doc = Document.open(path)
    doc.build_index()
    return doc


def roll_like_the_gui(doc: Document, info) -> saver.RollResult:
    """What ``gui/editing.py`` does for "Finish the save": the layer's store under the document's lock."""
    lock = LayerLock.for_doc(doc)
    assert lock.try_acquire()
    try:
        store = EditStore.open(doc, dir=info.dir, lock=lock) if info.matches else \
            EditStore.open(doc, dir=info.dir, lock=lock, read_only=True, allow_stale=True)
        try:
            result = saver.roll_journal(info.saving, store=store)
            assert (store.saving is None) == (result.action != "conflict")
        finally:
            store.close()
        return result
    finally:
        lock.release()


def restored_status(doc: Document, pos: int) -> str:
    es = EditSession.open(doc, restore=True)
    try:
        return es.doc.pinned(es.state).record(pos)["status"]
    finally:
        es.close()


@pytest.mark.parametrize("had_bak", [False, True])
def test_s1_rj_partial_hash_crash_before_replace(tmp_path, template, had_bak) -> None:
    src, pos = real_crash(tmp_path, template, "before_replace", had_bak=had_bak)
    original = template[0]
    assert src.read_bytes() == original and len(leftovers(tmp_path)) == 1 + had_bak
    doc = open_doc(src)
    try:
        infos = find_pending(doc)
        assert len(infos) == 1 and infos[0].matches and infos[0].saving and infos[0].saving["v"] == 2
        assert infos[0].saving["layer"]["key"] == infos[0].key
        result = roll_like_the_gui(doc, infos[0])
        assert result.action == "back", str(result)
        assert src.read_bytes() == original and leftovers(tmp_path) == []
        if had_bak:
            assert bak_of(src).read_bytes() == b'["the previous backup"]'
        else:
            assert not bak_of(src).exists()
        infos = find_pending(doc)                       # the layer is still there, without its journal
        assert len(infos) == 1 and infos[0].matches and infos[0].saving is None and infos[0].steps == 1
        assert restored_status(doc, pos) == "done", "the edit can still be restored"
    finally:
        doc.close()


def test_s1_rj_failed_save_then_finish(tmp_path, template, monkeypatch) -> None:
    """The replace fails (the file is held by another program) and removing the new ``.bak`` link fails
    too: the journal stays, the temp file is gone. "Finish" later must keep the layer."""
    data, pos = template
    src = tmp_path / "data.json"
    src.write_bytes(data)
    doc = open_doc(src)
    es = EditSession.open(doc)
    es.set_value(Ref(pos, ("status",)), '"done"')
    real_remove = fileio.remove_quiet

    def busy(a, b):
        raise OSError(None, "in use", str(b), 32)

    with monkeypatch.context() as m:       # (never monkeypatch.undo(): it would drop JSONRESHAPE_HOME too)
        m.setattr(fileio, "replace_posix", busy)
        m.setattr(fileio, "remove_quiet", lambda p: False if str(p).endswith(".bak") else real_remove(p))
        with pytest.raises(Exception) as ei:
            es.save()
    assert getattr(ei.value, "key", "") == "fileio.error.in_use"
    journal = es.store.saving
    assert journal is not None and journal["v"] == 2 and not Path(journal["temp"]).exists()
    es.close()
    doc.close()
    doc = open_doc(src)
    try:
        info = find_pending(doc)[0]
        assert info.matches and info.saving == journal
        # the same journal as the previous version wrote it: the evidence cannot tell -> conflict, untouched
        lock = LayerLock.for_doc(doc)
        assert lock.try_acquire()
        store = EditStore.open(doc, dir=info.dir, lock=lock)
        try:
            before = snapshot(tmp_path)
            v1 = saver.roll_journal(downgrade(journal), store=store)
            assert v1.action == "conflict" and snapshot(tmp_path) == before and store.saving == journal
        finally:
            store.close()
            lock.release()
        result = roll_like_the_gui(doc, info)
        assert result.action == "back", str(result)
        assert src.read_bytes() == data and not bak_of(src).exists() and leftovers(tmp_path) == []
        assert restored_status(doc, pos) == "done"
    finally:
        doc.close()


def test_s1_back_never_overwrites_a_backup_it_cannot_account_for(tmp_path, template) -> None:
    """Rolling back puts the previous ``.bak`` over OUR link of the original -- never over a ``.bak``
    that is something else now (another program wrote it): a conflict that touches nothing."""
    src, journal, original, _edited = crash(tmp_path, template, "before_replace", True, True, "posix")
    os.remove(bak_of(src))
    bak_of(src).write_bytes(b'["written by another program"]')
    before = snapshot(tmp_path)
    result = saver.roll_journal(journal)
    assert result.action == "conflict" and snapshot(tmp_path) == before
    assert str(bak_of(src)) in result and src.read_bytes() == original


def test_s1_journal_naming_foreign_files_is_invalid(tmp_path, template) -> None:
    src, journal, _original, _edited = crash(tmp_path, template, "before_replace", True, True, "posix")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / f".{src.name}.0123abcd{saver.TEMP_SUFFIX}"
    victim.write_bytes(b"not ours")
    for key, value in (("temp", str(victim)), ("old_backup", str(tmp_path / "precious.txt")),
                       ("backup", str(tmp_path / "other.bak"))):
        before = snapshot(tmp_path)
        result = saver.roll_journal({**journal, key: value})
        assert result.action == "invalid" and snapshot(tmp_path) == before and victim.exists()


# ----------------------------------------------------------------------------------
# S2: the source is missing (the fallback stopped between its two renames)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("backup", [True, False])
def test_s2_missing_source_is_rolled_forward_from_the_path(tmp_path, template, backup) -> None:
    data, pos = template
    src, _pos = real_crash(tmp_path, template, "between_renames" if backup else "between_renames_nobak")
    assert not src.exists()
    infos = saver.find_journals(src)
    assert len(infos) == 1 and infos[0].saving["source"] == str(src) and not infos[0].matches
    result = saver.roll_journal_at(infos[0])
    assert result.action == "forward", str(result)
    edited = data.replace(b'{"id":%d,"status":"open"' % pos, b'{"id":%d,"status":"done"' % pos, 1)
    assert src.read_bytes() == edited and leftovers(tmp_path) == []
    if backup:
        assert bak_of(src).read_bytes() == data
    else:
        assert not bak_of(src).exists()
    assert saver.find_journals(src) == []
    doc = open_doc(src)
    try:
        assert find_pending(doc) == [], "the save went through: its layer is gone"
    finally:
        doc.close()


def test_s2_missing_source_rolled_back_keeps_the_layer(tmp_path, template) -> None:
    data, pos = template
    src, _pos = real_crash(tmp_path, template, "between_renames")
    info = saver.find_journals(src)[0]
    Path(info.saving["temp"]).write_bytes(b"[")                 # the new version is damaged: go back
    result = saver.roll_journal_at(info)
    assert result.action == "back", str(result)
    assert src.read_bytes() == data and not bak_of(src).exists() and leftovers(tmp_path) == []
    doc = open_doc(src)
    try:
        infos = find_pending(doc)
        assert len(infos) == 1 and infos[0].matches and infos[0].saving is None
        assert restored_status(doc, pos) == "done"
    finally:
        doc.close()


def test_s2_version_1_journal_and_locked_layer(tmp_path, template) -> None:
    data, pos = template
    src, _pos = real_crash(tmp_path, template, "between_renames")
    info = saver.find_journals(src)[0]
    # the layer is held by another tab / program: nothing happens
    held = LayerLock(info.key)
    assert held.try_acquire()
    try:
        with pytest.raises(Exception) as ei:
            saver.roll_journal_at(info)
        assert getattr(ei.value, "key", "") == "edits.error.locked" and not src.exists()
    finally:
        held.release()
    # rewrite the journal as the previous version wrote it (no "layer" to open the layer by)
    doc = saver._LayerDoc.for_layer(info)
    store = EditStore.open(doc, dir=info.dir)
    try:
        store.journal(downgrade(info.saving))
    finally:
        store.close()
    info = saver.find_journals(src)[0]
    assert info.saving["v"] == 1
    result = saver.roll_journal_at(info)
    assert result.action == "forward", str(result)
    edited = data.replace(b'{"id":%d,"status":"open"' % pos, b'{"id":%d,"status":"done"' % pos, 1)
    assert src.read_bytes() == edited and bak_of(src).read_bytes() == data and leftovers(tmp_path) == []
    assert saver.find_journals(src) == []
