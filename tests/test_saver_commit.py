"""saver: the commit of a save (docs/ARCHITECTURE.md §12.7 "Commit", AGENT_PROMPT §1.4 "atomik almashtiradi
(ixtiyoriy .bak zaxira bilan)", §14 test 21 for an interrupted save).

In place while our own Document handles, a pinned view in the middle of an iteration and a second
document of the same file are open (Windows: POSIX-semantics rename + hard-link ``.bak``); a read-only
source; an existing ``.bak`` that is open and read-only, or a folder; growth of the source during the save
(refused in place, a warning for Save As); POSIX renames or hard links unsupported -> the two-rename
fallback, its undo, and the error naming the original when even the undo fails; Save As onto targets that
are open, locked or read-only; the save journal (written before the first rename, cleared at the end,
kept when the original could not be put back) with a real EditStore; :func:`saver.roll_journal` for
every state an interrupted save can leave (direct states and real crashes of a subprocess between the
steps); leftover temp files of crashed saves.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest import mock

import pytest

from jsonreshape import fileio, saver
from jsonreshape.errors import EditError, FileChangedError, JsonReshapeError, UserInputError
from jsonreshape.fileio import FileIdentity
from jsonreshape.reader import Document
from overlay_helpers import OverlayBuilder
from saver_helpers import FakeStore, apply_pattern, make_doc, oracle_text

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows-specific behaviour")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


def edited(directory: Path, docs: list, kind: str = "array", pattern: str = "mixed", **kw):
    doc = make_doc(directory / "src", kind, kw.pop("style", "pretty2"), n=kw.pop("n", 20), **kw)
    docs.append(doc)
    ob = OverlayBuilder(doc, directory / "layer")
    apply_pattern(ob, doc, pattern)
    doc.set_overlay(ob.state())
    return doc, ob


def bak_of(p: Path) -> Path:
    return p.with_name(p.name + ".bak")


def leftovers(folder: Path) -> list[str]:
    return sorted(n for n in os.listdir(folder) if n.endswith((saver.TEMP_SUFFIX, ".old")))


def set_read_only(p: Path, on: bool = True) -> None:
    mode = os.stat(p).st_mode
    os.chmod(p, (mode & ~stat.S_IWRITE) if on else (mode | stat.S_IWRITE))


# ----------------------------------------------------------------------------------
# in place while the file is in use
# ----------------------------------------------------------------------------------
@windows_only
@pytest.mark.parametrize("backup", [True, False])
def test_in_place_while_documents_and_a_pinned_view_read_it(tmp_path, docs, backup) -> None:
    doc, ob = edited(tmp_path, docs, "array", "every3", n=3000, style="compact")
    src = doc.source_path
    before = src.read_bytes()
    want = oracle_text(doc, ob)
    base = doc.pinned(None)
    first = base.record_bytes(5)                       # our per-thread handle on the file
    view = doc.pinned()
    it = view.iter_record_bytes(block_bytes=4096)      # a pinned view in the middle of an iteration
    got = [next(it) for _ in range(100)]
    other = Document.open(src)                          # a second tab on the same file
    docs.append(other)
    other.build_index()
    other_first = other.record_bytes(0)
    report = saver.save(doc, backup=backup)
    assert src.read_bytes() == want
    assert (report.backup is not None) == backup and bak_of(src).exists() == backup
    if backup:
        assert bak_of(src).read_bytes() == before
    assert leftovers(src.parent) == []
    # the old readers keep reading the version they opened
    got += list(it)
    assert [b for _i, b in got] == list(ob.records)
    assert base.record_bytes(5) == first and other.record_bytes(0) == other_first
    assert doc.check_changed() and other.check_changed()


def test_read_only_source_is_refused(tmp_path, docs, lang_en) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    before = src.read_bytes()
    set_read_only(src)
    try:
        with pytest.raises(UserInputError) as ei:
            saver.save(doc)
        assert ei.value.key == "fileio.error.read_only" and str(src) in str(ei.value)
        assert src.read_bytes() == before and not bak_of(src).exists() and leftovers(src.parent) == []
        report = saver.save(doc, tmp_path / "copy.json")          # Save As still works
        assert (tmp_path / "copy.json").read_bytes() == oracle_text(doc, ob) and not report.in_place
        assert fileio.is_read_only(src)
    finally:
        set_read_only(src, False)


def test_existing_open_read_only_backup(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    v0 = src.read_bytes()
    saver.save(doc)
    v1 = src.read_bytes()
    assert bak_of(src).read_bytes() == v0
    old_bak = Document.open(bak_of(src), format="array")    # the previous version open in another tab
    docs.append(old_bak)
    old_bak.build_index()
    old_first = old_bak.record_bytes(0)
    set_read_only(bak_of(src))
    doc2 = Document.open(src)
    docs.append(doc2)
    doc2.build_index()
    ob2 = OverlayBuilder(doc2, tmp_path / "layer2")
    ob2.set(0, b'{"second": 2}')
    doc2.set_overlay(ob2.state())
    report = saver.save(doc2)
    assert report.backup == bak_of(src) and bak_of(src).read_bytes() == v1
    assert src.read_bytes().startswith(b'[{"second": 2}') or b'{"second": 2}' in src.read_bytes()
    assert leftovers(src.parent) == []
    assert old_bak.record_bytes(0) == old_first          # its handle still reads the old .bak


def test_backup_that_is_a_folder_is_refused(tmp_path, docs, lang_en) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    before = src.read_bytes()
    bak_of(src).mkdir()
    with pytest.raises(UserInputError) as ei:
        saver.save(doc)
    assert ei.value.key == "fileio.error.is_directory"
    assert src.read_bytes() == before and leftovers(src.parent) == []
    report = saver.save(doc, backup=False)
    assert report.backup is None and bak_of(src).is_dir()


# ----------------------------------------------------------------------------------
# growth and changes of the source
# ----------------------------------------------------------------------------------
def test_growth_during_an_in_place_save_is_refused(tmp_path, docs, monkeypatch, lang_en) -> None:
    doc, ob = edited(tmp_path, docs, "jsonl", "point", n=10)
    src = doc.source_path
    before = src.read_bytes()
    real_finish = saver._Validator.finish

    def append_then_finish(self):
        with open(src, "ab") as f:                     # an active log gets a line meanwhile
            f.write(b'{"late": true}\n')
        return real_finish(self)

    monkeypatch.setattr(saver._Validator, "finish", append_then_finish)
    store = FakeStore()
    with pytest.raises(EditError) as ei:
        saver.save(doc, store=store)
    assert ei.value.key == "saver.error.grown" and "15 B" in str(ei.value) and str(src) in str(ei.value)
    assert src.read_bytes() == before + b'{"late": true}\n'
    assert not bak_of(src).exists() and leftovers(src.parent) == [] and store.calls == []
    monkeypatch.setattr(saver._Validator, "finish", real_finish)
    # grown before the save even starts: refused at once
    with pytest.raises(EditError) as ei:
        saver.save(doc)
    assert ei.value.key == "saver.error.grown"
    # Save As writes the snapshot opened, with a warning
    report = saver.save(doc, tmp_path / "copy.jsonl")
    assert "reader.warn.appended" in report.warning_keys
    assert (tmp_path / "copy.jsonl").read_bytes() == oracle_text(doc, ob)


def test_changed_source_fails_save_as(tmp_path, docs, monkeypatch) -> None:
    doc, ob = edited(tmp_path, docs, "array", "point", n=10)
    src = doc.source_path
    real_finish = saver._Validator.finish

    def rewrite_then_finish(self):
        data = src.read_bytes()
        src.write_bytes(data.replace(b"[", b" ", 1)[:-1] + b" ")    # same size, other content
        return real_finish(self)

    monkeypatch.setattr(saver._Validator, "finish", rewrite_then_finish)
    with pytest.raises(FileChangedError):
        saver.save(doc, tmp_path / "copy.json")
    assert not (tmp_path / "copy.json").exists() and leftovers(tmp_path) == []


# ----------------------------------------------------------------------------------
# fallbacks
# ----------------------------------------------------------------------------------
def _unsupported(src, dst):
    raise fileio.ReplaceUnsupported(None, "not supported", str(dst), 50)


@pytest.mark.parametrize("links", [True, False])
@pytest.mark.parametrize("backup", [True, False])
def test_fallback_without_posix_renames(tmp_path, docs, monkeypatch, backup, links) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    before = src.read_bytes()
    want = oracle_text(doc, ob)
    doc.pinned(None).record_bytes(0)                     # our handle is open: the rename-away still works
    bak_of(src).write_bytes(b"previous backup")
    monkeypatch.setattr(fileio, "replace_posix", _unsupported)
    if not links:
        monkeypatch.setattr(fileio, "hard_link", lambda a, b: False)
    store = FakeStore()
    report = saver.save(doc, backup=backup, store=store)
    assert src.read_bytes() == want
    if backup:
        assert report.backup == bak_of(src) and bak_of(src).read_bytes() == before
    else:
        assert bak_of(src).read_bytes() == b"previous backup"   # untouched without a backup
    assert leftovers(src.parent) == []
    assert len(store.calls) == 2 and store.calls[0]["source"] == str(src) and store.calls[1] is None


def test_fallback_failure_is_undone(tmp_path, docs, monkeypatch, lang_en) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    before = src.read_bytes()
    bak_of(src).write_bytes(b"previous backup")
    monkeypatch.setattr(fileio, "replace_posix", _unsupported)
    monkeypatch.setattr(fileio, "replace_file", _refuse)
    store = FakeStore()
    with pytest.raises(UserInputError) as ei:
        saver.save(doc, store=store)
    assert ei.value.key == "fileio.error.in_use"
    assert src.read_bytes() == before and bak_of(src).read_bytes() == b"previous backup"
    assert leftovers(src.parent) == [] and store.calls[-1] is None


def _refuse(a, b, **kw):
    raise UserInputError("fileio.error.in_use", path=str(b))


def _undo_fails(src: Path):
    """``os.replace`` that refuses to rename anything back to ``src`` (the undo of the fallback)."""
    real_replace = os.replace

    def fake_replace(a, b):
        if Path(fileio._strip_long_prefix(str(b))) == src:
            raise PermissionError(13, "denied", str(b))
        return real_replace(a, b)

    return mock.patch.object(saver.os, "replace", fake_replace)


def _fallback_patches():
    return (mock.patch.object(fileio, "replace_posix", _unsupported),
            mock.patch.object(fileio, "hard_link", lambda a, b: False),
            mock.patch.object(fileio, "replace_file", _refuse))


def test_fallback_undo_failure_names_the_original(tmp_path, docs, lang_en) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    before = src.read_bytes()
    want = oracle_text(doc, ob)
    store = FakeStore()
    p1, p2, p3 = _fallback_patches()
    with p1, p2, p3, _undo_fails(src):
        with pytest.raises(JsonReshapeError) as ei:
            saver.save(doc, backup=False, store=store)
    assert ei.value.key == "saver.error.original_moved"
    journal = store.calls[-1]
    assert journal is not None, "the journal is kept"
    aside = Path(journal["aside"])
    assert str(aside) in str(ei.value) and str(src) in (ei.value.hint or "")
    assert not src.exists() and aside.read_bytes() == before and Path(journal["temp"]).exists()
    result = saver.roll_journal(journal, store=store)
    assert result.action == "forward" and str(src) in result
    assert src.read_bytes() == want and not aside.exists() and not Path(journal["temp"]).exists()
    assert store.calls[-1] is None


def test_target_in_use_undoes_the_backup(tmp_path, docs, monkeypatch, lang_en) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    before = src.read_bytes()
    bak_of(src).write_bytes(b"previous backup")

    def locked(a, b):
        raise OSError(None, "in use", str(b), 32)

    monkeypatch.setattr(fileio, "replace_posix", locked)
    store = FakeStore()
    with pytest.raises(UserInputError) as ei:
        saver.save(doc, store=store)
    assert ei.value.key == "fileio.error.in_use"
    assert src.read_bytes() == before and bak_of(src).read_bytes() == b"previous backup"
    assert leftovers(src.parent) == [] and store.calls[-1] is None


@windows_only
def test_source_locked_by_another_program(tmp_path, docs, lang_en) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    before = src.read_bytes()
    with open(src, "rb"):                                   # plain open(): no delete sharing
        with pytest.raises(UserInputError) as ei:
            saver.save(doc)
    assert ei.value.key == "fileio.error.in_use"
    assert src.read_bytes() == before and not bak_of(src).exists() and leftovers(src.parent) == []


# ----------------------------------------------------------------------------------
# Save As targets
# ----------------------------------------------------------------------------------
@windows_only
def test_save_as_onto_targets_in_use(tmp_path, docs, lang_en) -> None:
    doc, ob = edited(tmp_path, docs)
    want = oracle_text(doc, ob)
    target = tmp_path / "target.json"
    target.write_bytes(b'[{"old": 1}]')
    other = Document.open(target)                 # shown in another tab: replaced all the same
    docs.append(other)
    other.build_index()
    other.record_bytes(0)
    saver.save(doc, target)
    assert target.read_bytes() == want and other.check_changed()
    with open(target, "rb"):                      # locked by another program
        with pytest.raises(UserInputError) as ei:
            saver.save(doc, target)
        assert ei.value.key == "fileio.error.in_use"
    set_read_only(target)
    try:
        with pytest.raises(UserInputError) as ei:
            saver.save(doc, target)
        assert ei.value.key == "fileio.error.read_only"
    finally:
        set_read_only(target, False)
    assert target.read_bytes() == want and leftovers(tmp_path) == []


def test_save_as_without_posix_renames(tmp_path, docs, monkeypatch) -> None:
    doc, ob = edited(tmp_path, docs)
    monkeypatch.setattr(fileio, "replace_posix", _unsupported)
    target = tmp_path / "t.json"
    target.write_bytes(b"[]")
    saver.save(doc, target)
    assert target.read_bytes() == oracle_text(doc, ob)


def test_stale_temp_files_are_swept(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    old = src.with_name(f".{src.name}.0123abcd{saver.TEMP_SUFFIX}")
    fresh = src.with_name(f".{src.name}.89abcdef{saver.TEMP_SUFFIX}")
    other = src.with_name(f".other.json.0123abcd{saver.TEMP_SUFFIX}")
    for p in (old, fresh, other):
        p.write_bytes(b"partial")
    past = time.time() - 2 * saver.STALE_TEMP_SECONDS
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    saver.save(doc)
    assert not old.exists() and fresh.exists() and other.exists()


def test_symlinked_source_saves_into_the_target(tmp_path, docs) -> None:
    real_dir = tmp_path / "real"
    doc0 = make_doc(real_dir, "array", "compact", n=5)
    real = doc0.source_path
    doc0.close()
    link = tmp_path / "link.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created here")
    doc = Document.open(link)
    docs.append(doc)
    doc.build_index()
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(0, b'{"via": "link"}')
    doc.set_overlay(ob.state())
    want = oracle_text(doc, ob)
    report = saver.save(doc)
    assert os.path.islink(link) and real.read_bytes() == want and report.target == link
    assert bak_of(real).exists() and not bak_of(link).exists()


# ----------------------------------------------------------------------------------
# the journal
# ----------------------------------------------------------------------------------
def test_journal_is_written_before_the_first_rename(tmp_path, docs, monkeypatch) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    bak_of(src).write_bytes(b"previous")
    store = FakeStore()
    seen = []
    real_link = fileio.hard_link

    def spy(a, b):
        seen.append(store.saving)
        assert Path(store.saving["old_backup"]).exists(), "the previous .bak was moved aside first"
        return real_link(a, b)

    monkeypatch.setattr(fileio, "hard_link", spy)
    saver.save(doc, store=store)
    j = seen[0]
    # version 2 (review S1): the journal also records the file ids and the new file's modification time
    assert j["v"] == saver.JOURNAL_VERSION == 2
    assert j["op"] == "save" and j["source"] == str(src) and j["backup"] == str(bak_of(src))
    assert j["had_backup"] is True and Path(j["temp"]).name.endswith(saver.TEMP_SUFFIX)
    assert Path(j["aside"]).name.endswith(".old") and Path(j["old_backup"]).name.endswith(".old")
    assert FileIdentity.from_dict(j["old_identity"]).matches(doc.identity)
    st = src.stat()                                    # the temp file became the source: same id and time
    assert j["new"]["size"] == st.st_size and j["new"]["mtime_ns"] == st.st_mtime_ns
    assert j["new"]["ino"] == str(st.st_ino) and j["old_file"]["ino"] != j["new"]["ino"]
    assert j["ids"] is fileio.stable_file_ids(src)        # NTFS: True
    assert j["bak_read_only"] is False and "layer" not in j          # a FakeStore has no layer
    assert store.calls[-1] is None and leftovers(src.parent) == []


def test_journal_with_a_real_edit_store(tmp_path, docs, monkeypatch) -> None:
    from jsonreshape.editstore import EditStore, find_pending

    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    store = EditStore.open(doc)
    try:
        calls = []
        real = store.journal

        def spy(data):
            calls.append(data)
            return real(data)

        monkeypatch.setattr(store, "journal", spy)
        saver.save(doc, store=store)
        assert calls[0]["source"] == str(src) and calls[-1] is None and store.saving is None
        # a save that stops with the original moved away keeps the journal for the next open
        doc2, ob2 = edited(tmp_path / "b", docs)
        store2 = EditStore.open(doc2)
        p1, p2, p3 = _fallback_patches()
        with p1, p2, p3, _undo_fails(doc2.source_path):
            with pytest.raises(JsonReshapeError):
                saver.save(doc2, store=store2)
        journal = store2.saving
        assert journal is not None and journal["source"] == str(doc2.source_path)
        store2.close()
        infos = find_pending(doc2)
        assert len(infos) == 1 and infos[0].saving == journal
        store2 = EditStore.open(doc2, allow_stale=True)
        result = saver.roll_journal(infos[0].saving, store=store2)
        assert result.action == "forward" and store2.saving is None
        store2.close()
    finally:
        store.close()


# ----------------------------------------------------------------------------------
# roll_journal: every state an interrupted save can leave
# ----------------------------------------------------------------------------------
def _state(tmp_path: Path, *, had_backup: bool = True, backup: bool = True) -> dict:
    """Files of an in-place save at the moment its journal was written, and that journal."""
    src = tmp_path / "data.json"
    src.write_bytes(b'[{"v": "old"}]')
    temp = tmp_path / f".data.json.0123abcd{saver.TEMP_SUFFIX}"
    temp.write_bytes(b'[{"v": "new version"}]')
    bak = bak_of(src)
    if had_backup:
        bak.write_bytes(b"previous backup")
    old_identity = fileio.file_identity(src)
    new = fileio.file_identity(temp)
    return {"v": 1, "op": "save", "source": str(src), "temp": str(temp), "backup": str(bak) if backup else None,
            "had_backup": had_backup, "old_backup": str(tmp_path / ".data.json.11111111.old") if had_backup else None,
            "aside": str(tmp_path / ".data.json.22222222.old"), "old_identity": old_identity.to_dict(),
            "new": {"size": new.size, "hashes": list(new.hashes)}, "time": time.time()}


def test_roll_back_before_any_rename(tmp_path, lang_en) -> None:
    j = _state(tmp_path)
    store = FakeStore()
    result = saver.roll_journal(j, store=store)
    assert isinstance(result, str) and result.action == "back"
    assert "put back" in result and j["source"] in result
    assert Path(j["source"]).read_bytes() == b'[{"v": "old"}]' and not Path(j["temp"]).exists()
    assert Path(j["backup"]).read_bytes() == b"previous backup" and store.calls == [None]


def test_roll_back_after_the_backup_link(tmp_path) -> None:
    j = _state(tmp_path)
    os.replace(j["backup"], j["old_backup"])          # step 3: the previous .bak moved aside ...
    os.link(j["source"], j["backup"])                  # ... and the hard link made
    assert saver.roll_journal(j).action == "back"
    assert Path(j["backup"]).read_bytes() == b"previous backup" and not Path(j["old_backup"]).exists()
    assert not Path(j["temp"]).exists() and leftovers(tmp_path) == []
    # without a previous backup: our link goes
    for p in tmp_path.iterdir():
        p.unlink()
    j = _state(tmp_path, had_backup=False)
    os.link(j["source"], j["backup"])
    assert saver.roll_journal(j).action == "back"
    assert not Path(j["backup"]).exists() and Path(j["source"]).read_bytes() == b'[{"v": "old"}]'


def test_roll_completed_after_the_replace(tmp_path, lang_en) -> None:
    j = _state(tmp_path)
    os.replace(j["backup"], j["old_backup"])
    os.link(j["source"], j["backup"])
    os.replace(j["temp"], j["source"])                 # step 4 done; the cleanup did not run
    result = saver.roll_journal(j)
    assert result.action == "completed" and "finished" in result
    assert not Path(j["old_backup"]).exists() and Path(j["backup"]).read_bytes() == b'[{"v": "old"}]'
    assert Path(j["source"]).read_bytes() == b'[{"v": "new version"}]'
    # a save without backup whose original copy (aside) was left
    for p in tmp_path.iterdir():
        p.unlink()
    j = _state(tmp_path, had_backup=False, backup=False)
    os.replace(j["source"], j["aside"])
    os.replace(j["temp"], j["source"])
    assert saver.roll_journal(j).action == "completed" and not Path(j["aside"]).exists()


def test_roll_forward_when_the_source_is_missing(tmp_path, lang_en) -> None:
    j = _state(tmp_path)
    os.replace(j["backup"], j["old_backup"])
    os.replace(j["source"], j["backup"])               # fallback: original renamed to .bak, crash
    result = saver.roll_journal(j)
    assert result.action == "forward" and "completed" in result
    assert Path(j["source"]).read_bytes() == b'[{"v": "new version"}]'
    assert Path(j["backup"]).read_bytes() == b'[{"v": "old"}]' and not Path(j["old_backup"]).exists()
    # backup off: the original was renamed aside
    for p in tmp_path.iterdir():
        p.unlink()
    j = _state(tmp_path, had_backup=False, backup=False)
    os.replace(j["source"], j["aside"])
    assert saver.roll_journal(j).action == "forward"
    assert Path(j["source"]).read_bytes() == b'[{"v": "new version"}]' and not Path(j["aside"]).exists()


def test_roll_back_when_the_temp_file_is_incomplete(tmp_path) -> None:
    j = _state(tmp_path)
    os.replace(j["backup"], j["old_backup"])
    os.replace(j["source"], j["backup"])
    Path(j["temp"]).write_bytes(b'[{"v": "new')       # cut
    assert saver.roll_journal(j).action == "back"
    assert Path(j["source"]).read_bytes() == b'[{"v": "old"}]'
    assert Path(j["backup"]).read_bytes() == b"previous backup" and not Path(j["temp"]).exists()
    for p in tmp_path.iterdir():
        p.unlink()
    j = _state(tmp_path, had_backup=False, backup=False)
    os.replace(j["source"], j["aside"])
    os.remove(j["temp"])
    assert saver.roll_journal(j).action == "back"
    assert Path(j["source"]).read_bytes() == b'[{"v": "old"}]' and not Path(j["aside"]).exists()


def test_roll_conflict_and_invalid(tmp_path, lang_en) -> None:
    j = _state(tmp_path)
    Path(j["source"]).write_bytes(b'[{"v": "edited by someone else"}]')
    store = FakeStore()
    result = saver.roll_journal(j, store=store)
    assert result.action == "conflict" and j["temp"] in result and store.calls == []
    assert Path(j["temp"]).exists() and Path(j["backup"]).exists()
    os.remove(j["source"])
    os.remove(j["temp"])
    assert saver.roll_journal(j).action == "conflict"      # nothing left to restore from: untouched
    for bad in ({}, {"source": "x"}, {**j, "new": {"size": "?", "hashes": []}}):
        result = saver.roll_journal(bad, store=store)
        assert result.action == "invalid" and "damaged" in result
    assert store.calls[-1] is None


# ----------------------------------------------------------------------------------
# real crashes between the steps (a subprocess dies with os._exit)
# ----------------------------------------------------------------------------------
CHILD = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, {root!r}); sys.path.insert(0, {tests!r})
    from pathlib import Path
    from jsonreshape import fileio, saver
    from jsonreshape.reader import Document
    from overlay_helpers import OverlayBuilder
    from saver_helpers import FakeStore, apply_pattern
    src, layer, journal, point, backup = sys.argv[1:6]
    doc = Document.open(src); doc.build_index()
    ob = OverlayBuilder(doc, layer); apply_pattern(ob, doc, "mixed"); doc.set_overlay(ob.state())
    Path(journal + ".want").write_bytes(b"")
    def die(*a, **k):
        os._exit(17)
    if point == "before_replace":
        fileio.replace_posix = die
    elif point == "between_renames":
        def unsupported(a, b):
            raise fileio.ReplaceUnsupported(None, "x", str(b), 50)
        fileio.replace_posix = unsupported
        fileio.replace_file = die
    elif point == "cleanup":
        fileio.remove_quiet = die
    saver.save(doc, backup=backup == "1", store=FakeStore(journal))
    os._exit(0)
""")


@pytest.mark.parametrize("point, backup, action", [
    ("before_replace", True, "back"),
    ("between_renames", True, "forward"),
    ("between_renames", False, "forward"),
    ("cleanup", True, "completed"),
])
def test_crash_between_the_steps(tmp_path, docs, point, backup, action) -> None:
    doc, ob = edited(tmp_path, docs)
    src = doc.source_path
    original = src.read_bytes()
    want = oracle_text(doc, ob)
    doc.close()
    bak_of(src).write_bytes(b"previous backup")
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=str(ROOT), tests=str(ROOT / "tests")), encoding="utf-8")
    journal_path = tmp_path / "journal.json"
    proc = subprocess.run([sys.executable, str(script), str(src), str(tmp_path / "layer2"), str(journal_path), point,
                           "1" if backup else "0"], env=dict(os.environ), capture_output=True, timeout=120)
    assert proc.returncode == 17, proc.stderr.decode(errors="replace")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal is not None and journal["source"] == str(src)
    result = saver.roll_journal(journal)
    assert result.action == action
    if action == "back":
        assert src.read_bytes() == original and bak_of(src).read_bytes() == b"previous backup"
    else:
        assert src.read_bytes() == want
        if backup:
            assert bak_of(src).read_bytes() == original
        else:
            assert bak_of(src).read_bytes() == b"previous backup"
    assert leftovers(src.parent) == []
