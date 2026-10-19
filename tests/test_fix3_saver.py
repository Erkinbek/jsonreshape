"""Stage-3 fix round, saver (review3_safety S5, S8, S9; ARCHITECTURE §12.7).

* S8 -- :func:`saver.sweep_leftovers`: leftovers of interrupted saves next to a file (``.<name>.<hex>.jrsave``
  temp files, ``.old`` copies) that nothing needs are removed -- not the young ones, not those in use, not
  while another tab / program holds the file or one of its layers, an ``.old`` copy only when ``<file>.bak`` is
  at least as new, nothing next to a missing file, and for names of 48+ characters (whose prefix other files
  share) no ``.old`` copy and nothing at all while another file of the prefix looks interrupted. A save
  sweeps first and says how much it freed.
* S5 -- no sweep (also the one at the start of a save) touches a file that a live journal of any layer of
  the file names, however old it is.
* S9 -- an in-place save keeps the source's hidden / system / not-content-indexed attributes and sets the
  archive bit, keeps NTFS compression (a failure to compress or to encrypt like the source is a warning),
  and puts a read-only previous ``.bak`` back read-only (undo and roll "back"). :func:`saver.save_questions`
  asks about gzip data after the stream that an in-place save without a backup would lose (and the
  brace_list / broken lines questions); ``save`` itself keeps its behaviour.
* R5F-4 (stage-5 files review) -- a disk full while the temp file is written is reported as the localized
  ``fileio.error.disk_full`` (save, Save As, ``write_part``), not as the raw ``OSError`` of the second
  flush when the file object is closed.
"""

from __future__ import annotations

import errno
import gzip
import io
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from jsonreshape import fileio, saver
from jsonreshape.editstore import LayerLock, file_lock_key, find_pending
from jsonreshape.edits import EditSession, Ref
from jsonreshape.errors import JsonReshapeError, UserInputError
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import format_bytes
from overlay_helpers import OverlayBuilder
from saver_helpers import FakeStore, apply_pattern, make_doc, oracle_text

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows file attributes")
ROOT = Path(__file__).resolve().parents[1]
HIDDEN, SYSTEM, ARCHIVE, COMPRESSED, NOT_INDEXED, ENCRYPTED = 0x2, 0x4, 0x20, 0x800, 0x2000, 0x4000


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


def edited(directory: Path, docs: list, kind: str = "array", **kw):
    doc = make_doc(directory / "src", kind, kw.pop("style", "pretty2"), n=kw.pop("n", 20), **kw)
    docs.append(doc)
    ob = OverlayBuilder(doc, directory / "layer")
    apply_pattern(ob, doc, "mixed")
    doc.set_overlay(ob.state())
    return doc, ob


def leftover(src: Path, hexpart: str, suffix: str = saver.TEMP_SUFFIX) -> Path:
    return src.with_name(f".{src.name[:48]}.{hexpart}{suffix}")


def make_old(p: Path, seconds: float) -> None:
    past = time.time() - seconds
    os.utime(p, (past, past))


def bak_of(p: Path) -> Path:
    return p.with_name(p.name + ".bak")


# ----------------------------------------------------------------------------------
# S8: sweep_leftovers
# ----------------------------------------------------------------------------------
def test_s8_orphan_temp_files_are_removed_when_old_enough(tmp_path, lang_en) -> None:
    src = tmp_path / "data.json"
    src.write_text('[{"a": 1}]', encoding="utf-8")
    old = leftover(src, "0123abcd")
    old.write_bytes(b"x" * 5000)
    make_old(old, 3600)
    young = leftover(src, "89abcdef")
    young.write_bytes(b"y")
    other = tmp_path / f".other.json.0123abcd{saver.TEMP_SUFFIX}"
    other.write_bytes(b"z")
    make_old(other, 3600)
    result = saver.sweep_leftovers(src)
    assert result.removed == [old] and result.freed == 5000 and result.skipped == ""
    assert not old.exists() and young.exists() and other.exists()
    assert result.message() == t_en("saver.sweep.removed.one", count=1, size=format_bytes(5000))
    later = saver.sweep_leftovers(src, now=time.time() + saver.SWEEP_MIN_AGE + 60)
    assert later.removed == [young] and other.exists()
    assert saver.sweep_leftovers(src).removed == [] and saver.sweep_leftovers(src).message() == ""


def t_en(key: str, **params) -> str:
    from jsonreshape.i18n import t

    return t(key, **params)


@pytest.mark.skipif(not WINDOWS, reason="Windows sharing violations")
def test_s8_a_file_in_use_is_kept(tmp_path) -> None:
    src = tmp_path / "data.json"
    src.write_text("[1]", encoding="utf-8")
    temp = leftover(src, "0123abcd")
    temp.write_bytes(b"partial")
    make_old(temp, 3600)
    with open(temp, "rb"):                       # another program writes it: no delete sharing
        assert saver.sweep_leftovers(src).removed == [] and temp.exists()
    assert saver.sweep_leftovers(src).removed == [temp]


def test_s8_nothing_is_removed_while_someone_else_holds_the_file(tmp_path) -> None:
    src = tmp_path / "data.json"
    src.write_text("[1]", encoding="utf-8")
    temp = leftover(src, "0123abcd")
    temp.write_bytes(b"partial")
    make_old(temp, 3600)
    held = LayerLock(file_lock_key(src))         # an edit session of another tab / program
    assert held.try_acquire()
    try:
        result = saver.sweep_leftovers(src)
        assert result.skipped == "locked" and temp.exists()
        assert saver.sweep_leftovers(src, own_file_lock=True).removed == [temp]   # the caller's own lock
    finally:
        held.release()


def test_s8_old_copies_need_a_newer_backup(tmp_path) -> None:
    src = tmp_path / "data.json"
    src.write_text("[2]", encoding="utf-8")
    copy = leftover(src, "0123abcd", ".old")
    copy.write_text("[1]", encoding="utf-8")
    make_old(copy, 7200)
    later = time.time() + saver.SWEEP_MIN_AGE + 60          # past the copy's change time (it was just made)
    assert saver.sweep_leftovers(src, now=later).removed == []            # no .bak: maybe the only copy
    bak_of(src).write_text("[0]", encoding="utf-8")
    make_old(bak_of(src), 9000)
    assert saver.sweep_leftovers(src, now=later).removed == []            # the .bak is older than it
    make_old(bak_of(src), 3600)
    assert saver.sweep_leftovers(src).removed == []                       # renamed just now (change time)
    assert saver.sweep_leftovers(src, now=later).removed == [copy] and bak_of(src).exists()


def test_s8_missing_file_and_long_names(tmp_path) -> None:
    gone = tmp_path / "gone.json"
    temp = leftover(gone, "0123abcd")
    temp.write_bytes(b"the new version of a file that is missing")
    make_old(temp, 3600)
    assert saver.sweep_leftovers(gone).skipped == "missing" and temp.exists()
    # 48+ characters: the 48-character prefix of the leftovers' names is shared with other files
    name = "customer_export_2026-10-19_full_dataset_version2_part1.json"
    assert len(name) > saver.NAME_PREFIX
    src = tmp_path / name
    src.write_text("[1]", encoding="utf-8")
    temp = leftover(src, "0123abcd")
    temp.write_bytes(b"x")
    make_old(temp, 3600)
    copy = leftover(src, "89abcdef", ".old")
    copy.write_text("[0]", encoding="utf-8")
    bak_of(src).write_text("[0]", encoding="utf-8")
    later = time.time() + 3600
    assert saver.sweep_leftovers(src, now=later).skipped == "shared"       # an .old copy of the prefix
    assert temp.exists() and copy.exists()
    copy.unlink()
    sibling = tmp_path / (name[:saver.NAME_PREFIX] + "_part2.json")      # gone, its .bak left: interrupted?
    bak_of(sibling).write_text("[0]", encoding="utf-8")
    assert saver.sweep_leftovers(src, now=later).skipped == "shared" and temp.exists()
    bak_of(sibling).unlink()
    assert saver.sweep_leftovers(src, now=later).removed == [temp]


def _layer_with_journal(src: Path, journal_files: dict) -> dict:
    """A real edit layer of ``src`` (one step) holding a save journal that names ``journal_files``."""
    doc = Document.open(src)
    doc.build_index()
    try:
        es = EditSession.open(doc)
        es.set_value(Ref(0, ("id",)), "999")
        journal = {"v": 2, "op": "save", "source": str(src), "had_backup": True, "backup": str(bak_of(src)),
                   **{k: str(v) for k, v in journal_files.items()}}
        es.store.journal(journal)
        es.close()                                   # the layer stays: one step + the journal
        return journal
    finally:
        doc.close()


def test_s5_files_of_a_live_journal_are_never_swept(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "array")
    src = doc.source_path
    want = oracle_text(doc, ob)
    temp = leftover(src, "0123abcd")
    temp.write_bytes(b"the complete new version")
    copy = leftover(src, "89abcdef", ".old")
    copy.write_bytes(b"the previous backup")
    bak_of(src).write_bytes(b"the backup")
    for p in (temp, copy):
        make_old(p, 3 * saver.STALE_TEMP_SECONDS)
    _layer_with_journal(src, {"temp": temp, "old_backup": copy, "aside": leftover(src, "22222222", ".old")})
    later = time.time() + 10 * saver.STALE_TEMP_SECONDS
    assert saver.sweep_leftovers(src, now=later).removed == []
    assert temp.exists() and copy.exists()
    # the sweep at the start of a save (it used to remove day-old temp files whatever referenced them)
    report = saver.save(doc)
    assert src.read_bytes() == want and temp.exists() and copy.exists()
    assert "saver.sweep.removed" not in report.warning_keys
    # the file is another version now: the layer is stale, its journal still protects the files
    new_doc = Document.open(src)
    docs.append(new_doc)
    new_doc.build_index()
    infos = find_pending(new_doc)
    assert len(infos) == 1 and not infos[0].matches and infos[0].saving
    assert saver.sweep_leftovers(src, now=later).removed == [] and temp.exists() and copy.exists()


def test_s5_a_layer_held_elsewhere_stops_the_sweep(tmp_path) -> None:
    src = tmp_path / "data.json"
    src.write_text('[{"id": 1}]', encoding="utf-8")
    temp = leftover(src, "0123abcd")
    temp.write_bytes(b"?")
    make_old(temp, 3600)
    _layer_with_journal(src, {})
    doc = Document.open(src)
    doc.build_index()
    try:
        key = find_pending(doc)[0].key
    finally:
        doc.close()
    held = LayerLock(key)                         # another program has the layer open: its journal is unknown
    assert held.try_acquire()
    try:
        assert saver.sweep_leftovers(src).skipped == "locked" and temp.exists()
        assert saver.sweep_leftovers(src, own_key=key).removed == [temp]      # unless it is the caller's
    finally:
        held.release()


def test_s8_a_save_sweeps_first_and_says_so(tmp_path, docs, lang_en) -> None:
    doc, ob = edited(tmp_path, docs, "array")
    src = doc.source_path
    temp = leftover(src, "0123abcd")
    temp.write_bytes(b"x" * 1234)
    make_old(temp, 3600)
    report = saver.save(doc)
    assert not temp.exists() and "saver.sweep.removed" in report.warning_keys
    assert t_en("saver.sweep.removed.one", count=1, size=format_bytes(1234)) in report.warnings


# ----------------------------------------------------------------------------------
# S9: attributes, read-only backups
# ----------------------------------------------------------------------------------
@windows_only
def test_s9_hidden_system_and_not_indexed_are_kept(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "array")
    src = doc.source_path
    want = oracle_text(doc, ob)
    assert fileio.set_file_attributes(src, HIDDEN | SYSTEM | NOT_INDEXED)        # archive bit cleared
    report = saver.save(doc)
    assert src.read_bytes() == want and report.backup == bak_of(src)
    attrs = fileio.file_attributes(src)
    assert attrs & (HIDDEN | SYSTEM | NOT_INDEXED) == HIDDEN | SYSTEM | NOT_INDEXED
    assert attrs & ARCHIVE, "the content changed: backup programs must see it"
    assert fileio.file_attributes(bak_of(src)) & HIDDEN       # the backup is the original file
    fileio.set_file_attributes(src, ARCHIVE)
    fileio.set_file_attributes(bak_of(src), ARCHIVE)


@windows_only
def test_s9_compressed_source_stays_compressed(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "array")
    src = doc.source_path
    if not fileio.set_compressed(src):
        pytest.skip("this volume cannot compress files")
    assert fileio.file_attributes(src) & COMPRESSED
    want = oracle_text(doc, ob)
    report = saver.save(doc)
    assert src.read_bytes() == want and "saver.warn.compression_lost" not in report.warning_keys
    assert fileio.file_attributes(src) & COMPRESSED


def test_s9_lost_compression_or_encryption_is_a_warning(tmp_path, docs, monkeypatch) -> None:
    doc, ob = edited(tmp_path, docs, "array")
    src = doc.source_path
    want = oracle_text(doc, ob)
    real_attributes = fileio.file_attributes
    calls: list[tuple[str, bool]] = []

    def attributes(p):
        value = real_attributes(p) or 0
        return value | COMPRESSED | ENCRYPTED if Path(p) == src else value

    def encrypt(p):
        try:                                     # EncryptFileW needs the file closed: we can rename it
            os.replace(p, str(p) + ".probe")
            os.replace(str(p) + ".probe", p)
            closed = True
        except OSError:
            closed = False
        calls.append((Path(p).name, closed))
        return outcome[0]

    outcome = [False]
    monkeypatch.setattr(fileio, "file_attributes", attributes)
    monkeypatch.setattr(fileio, "set_compressed", lambda p: False)
    monkeypatch.setattr(fileio, "encrypt_file", encrypt)
    report = saver.save(doc, backup=False)
    assert src.read_bytes() == want
    assert {"saver.warn.compression_lost", "saver.warn.encryption_lost"} <= set(report.warning_keys)
    assert len(calls) == 1 and calls[0][0].endswith(saver.TEMP_SUFFIX) and calls[0][1]
    # encrypted: no warning
    doc2 = Document.open(src)
    docs.append(doc2)
    doc2.build_index()
    ob2 = OverlayBuilder(doc2, tmp_path / "layer2")
    ob2.set(0, b'{"again": true}')
    doc2.set_overlay(ob2.state())
    outcome[0] = True
    report = saver.save(doc2, backup=False)
    assert "saver.warn.encryption_lost" not in report.warning_keys and b'{"again": true}' in src.read_bytes()


@windows_only
def test_s9_read_only_backup_is_put_back_read_only(tmp_path, docs, monkeypatch, lang_en) -> None:
    doc, ob = edited(tmp_path, docs, "array")
    src = doc.source_path
    before = src.read_bytes()
    bak_of(src).write_bytes(b"previous backup")
    fileio.set_read_only(bak_of(src))

    def in_use(a, b):
        raise OSError(None, "in use", str(b), 32)

    monkeypatch.setattr(fileio, "replace_posix", in_use)
    store = FakeStore()
    try:
        with pytest.raises(UserInputError):
            saver.save(doc, store=store)
        assert src.read_bytes() == before and bak_of(src).read_bytes() == b"previous backup"
        assert fileio.is_read_only(bak_of(src)), "the undo put the backup back with its read-only bit"
        assert store.calls[0]["bak_read_only"] is True and store.calls[-1] is None
    finally:
        fileio.clear_read_only(bak_of(src))


CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r}); sys.path.insert(0, {tests!r})
    from jsonreshape import fileio, saver
    from jsonreshape.reader import Document
    from overlay_helpers import OverlayBuilder
    from saver_helpers import FakeStore, apply_pattern
    src, layer, journal = sys.argv[1:4]
    doc = Document.open(src); doc.build_index()
    ob = OverlayBuilder(doc, layer); apply_pattern(ob, doc, "mixed"); doc.set_overlay(ob.state())
    def die(*a, **k):
        os._exit(17)
    fileio.replace_posix = die
    saver.save(doc, store=FakeStore(journal))
""")


@windows_only
def test_s9_roll_back_puts_a_read_only_backup_back_read_only(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "array")
    src = doc.source_path
    original = src.read_bytes()
    doc.close()
    bak_of(src).write_bytes(b"previous backup")
    fileio.set_read_only(bak_of(src))
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=str(ROOT), tests=str(ROOT / "tests")), encoding="utf-8")
    journal_path = tmp_path / "journal.json"
    proc = subprocess.run([sys.executable, str(script), str(src), str(tmp_path / "layer2"), str(journal_path)],
                          env=dict(os.environ), capture_output=True, timeout=120)
    assert proc.returncode == 17, proc.stderr.decode(errors="replace")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["bak_read_only"] is True and not fileio.is_read_only(Path(journal["old_backup"]))
    try:
        result = saver.roll_journal(journal)
        assert result.action == "back"
        assert src.read_bytes() == original and bak_of(src).read_bytes() == b"previous backup"
        assert fileio.is_read_only(bak_of(src))
    finally:
        fileio.clear_read_only(bak_of(src))


# ----------------------------------------------------------------------------------
# R5F-4 (stage-5 files review): a disk full while writing is reported localized
# ----------------------------------------------------------------------------------
class _FullDisk(io.RawIOBase):
    """A temp file on a disk that is full after ``limit`` bytes (every later write fails, also the one of
    the second flush when the file object is closed)."""

    def __init__(self, fd: int, limit: int) -> None:
        self.fd = fd
        self.limit = limit
        self.n = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        if self.n + len(b) > self.limit:
            raise OSError(errno.ENOSPC, "No space left on device (simulated)")
        self.n += len(b)
        return os.write(self.fd, b)

    def fileno(self) -> int:
        return self.fd

    def close(self) -> None:
        if not self.closed:
            os.close(self.fd)
        super().close()


def _full_disk(m, limit: int) -> None:
    real = os.fdopen

    def fdopen(fd, mode="r", buffering=-1, **kw):
        if mode == "wb" and buffering == saver.WRITE_BUFFER:          # the saver's temp file only
            return io.BufferedWriter(_FullDisk(fd, limit), buffer_size=buffering)
        return real(fd, mode, buffering, **kw)

    m.setattr(saver.os, "fdopen", fdopen)


@pytest.mark.parametrize("limit", [0, 100, 3000])
def test_r5f4_disk_full_is_reported_localized(tmp_path, docs, monkeypatch, lang_en, limit) -> None:
    doc, ob = edited(tmp_path, docs, "array", n=60)
    src = doc.source_path
    before = src.read_bytes()
    for target in (None, tmp_path / "copy.json"):
        with monkeypatch.context() as m:
            _full_disk(m, limit)
            with pytest.raises(JsonReshapeError) as ei:     # never the raw OSError of the close
                saver.save(doc, target, backup=True)
        assert ei.value.key == "fileio.error.disk_full", repr(ei.value)
        assert src.read_bytes() == before and not (tmp_path / "copy.json").exists()
        assert [n for n in os.listdir(src.parent) if n.endswith((saver.TEMP_SUFFIX, ".old"))] == []
    final = tmp_path / "part1.json"
    fd, tmp = fileio.create_temp(final, ".part")
    with monkeypatch.context() as m:
        _full_disk(m, limit)
        with pytest.raises(JsonReshapeError) as ei:
            saver.write_part(doc.pinned(), fd, final, 0, doc.record_count)
    assert ei.value.key == "fileio.error.disk_full", repr(ei.value)
    os.remove(tmp)                                        # write_part closed the descriptor


# ----------------------------------------------------------------------------------
# S9: questions before a save (gzip data after the stream)
# ----------------------------------------------------------------------------------
def test_s9_gzip_trailing_data_is_a_question(tmp_path, docs, lang_en) -> None:
    src = tmp_path / "data.json.gz"
    src.write_bytes(gzip.compress(json.dumps([{"id": i} for i in range(5)]).encode(), mtime=0)
                    + b"\x00not gzip: a signature someone appended")
    doc = Document.open(src)
    docs.append(doc)
    doc.build_index()
    assert "fileio.warning.gzip_trailing" in doc.prepared.warnings
    no_backup = Settings(backup_on_save=False)
    qs = saver.save_questions(doc, backup=False)
    assert [q.key for q in qs] == ["gzip_trailing"] and "data.json.gz" in qs[0].text
    assert qs[0].text == t_en("saver.ask.gzip_trailing", name="data.json.gz")
    assert saver.save_questions(doc, settings=no_backup)[0].key == "gzip_trailing"   # backup None: the setting
    assert saver.save_questions(doc, backup=True) == []                              # the .bak keeps it
    assert saver.save_questions(doc, tmp_path / "copy.json.gz", backup=False) == []  # Save As: source kept
    assert saver.save_questions(doc, src, backup=False)[0].key == "gzip_trailing"    # the source's own name
    # nobody asks: the old behaviour (written with a warning)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(0, b'{"id": "zero"}')
    doc.set_overlay(ob.state())
    report = saver.save(doc, backup=False)
    assert "saver.warn.gzip_trailing" in report.warning_keys
    assert json.loads(gzip.decompress(src.read_bytes()))[0] == {"id": "zero"}


def test_s9_questions_for_brace_list_and_broken_lines(tmp_path, docs, lang_en) -> None:
    brace = make_doc(tmp_path / "b", "brace_list", "compact", n=5)
    docs.append(brace)
    qs = saver.save_questions(brace)
    assert [q.key for q in qs] == ["brace_list"] and "{" in qs[0].params["example"]
    lines = tmp_path / "l" / "lines.jsonl"
    lines.parent.mkdir()
    lines.write_text('{"id": 1}\n{"id": \n{"id": 3}\nnot json\n', encoding="utf-8")
    doc = Document.open(lines, format="jsonl")
    docs.append(doc)
    doc.build_index()
    qs = saver.save_questions(doc)
    assert [q.key for q in qs] == ["broken_lines"] and qs[0].params == {"count": 2}
    assert qs[0].text == t_en("saver.ask.broken_lines", count=2)
    plain = make_doc(tmp_path / "p", "array", "compact", n=3)
    docs.append(plain)
    assert saver.save_questions(plain) == []
