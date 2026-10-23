"""Fix round R5 (stage 5, review lens "files"): regression tests of the findings R5F-1..R5F-3, R5F-5..R5F-9 and
the index cache scan (scratchpad ``s5/review5_files.md``, ``s5/task_R5_fix_files.md``).

* R5F-1 ``RepairReport.write`` refuses the repaired source (any spelling, a hard link, the prepared copy, an
  open gzip / UTF-16 source) and the repaired output;
* R5F-2 ``diff.write_diff`` refuses the compared documents (sources and data copies; documents open or closed;
  a generator of :func:`iter_record_diffs`, a report, ``sources=``, a wrapping generator);
* R5F-3 split / merge / extract never replace a file that has unsaved edits (a live or pending edit layer);
* R5F-5 ``commit_files`` names every failed restore and sweeps old ``.jrold`` copies of its names;
* R5F-6 an ``allow_partial`` result lists the dropped tail as a counted fix (``dropped_tail``);
* R5F-7 the ``trailing_data`` preview of a JSON Lines line stops at the line end;
* R5F-8 a UTF-8 file starting with a NUL byte is not taken for UTF-16;
* R5F-9 stale parts: named with their count in a warning that says what to do; ``remove_stale``;
* the cache scan: indexing does not list the whole cache every time, and a scan does no per-entry path work.
"""

from __future__ import annotations

import errno
import gzip
import os
import sys
import time
from pathlib import Path

import pytest
from fs_helpers import case_folds

from jsonreshape import diff, fileio, filetools, index as index_mod, repair
from jsonreshape.edits import EditSession
from jsonreshape.errors import JsonReshapeError, UserInputError
from jsonreshape.filetools import MergeOptions, SplitOptions, split
from jsonreshape.reader import Document
from jsonreshape.repair import RepairOptions, repair_file
from jsonreshape.settings import Settings
from jsonreshape.writer import ExportOptions
from repair_helpers import repair_bytes
from test_repair_props import check_coverage

WINDOWS = sys.platform == "win32"
BROKEN = b'[{"id": 1, "v": NaN}, {"id": 2, "v": "x",}, {"id": 3 // cut here'


@pytest.fixture
def docs():
    opened: list = []
    yield opened
    for d in reversed(opened):
        try:
            d.close()
        except Exception:  # noqa: BLE001 - best effort
            pass


def _open(path: Path, docs: list, **kw) -> Document:
    d = Document.open(path, **kw)
    d.build_index()
    docs.append(d)
    return d


# ==================================================================================================
# R5F-1: RepairReport.write never replaces the source, its prepared copy or the repaired output
# ==================================================================================================
def test_report_write_refuses_the_source_and_the_output(tmp_path: Path, monkeypatch, lang_en) -> None:
    src = tmp_path / "broken.json"
    src.write_bytes(BROKEN)
    dst = tmp_path / "fixed.json"
    rep = repair_file(src, dst, RepairOptions())
    fixed = dst.read_bytes()
    monkeypatch.chdir(tmp_path)
    try:
        spellings = [src, str(src).replace("\\", "/"), str(tmp_path / "sub" / ".." / "broken.json"), "broken.json"]
        # another spelling of the same name only where the file system folds case (NTFS, APFS)
        if case_folds(tmp_path):
            spellings += [str(src).upper(), "./BROKEN.json"]
        for spelling in spellings:
            with pytest.raises(UserInputError) as ei:
                rep.write(spelling, "csv")
            assert ei.value.key == "writer.error.same_file", spelling
        link = tmp_path / "hard_link.json"
        os.link(src, link)                                   # the same file under another name
        with pytest.raises(UserInputError) as ei:
            rep.write(link, "json")
        assert ei.value.key == "writer.error.same_file"
        with pytest.raises(UserInputError) as ei:                                # the repaired output
            rep.write(str(dst).upper() if case_folds(tmp_path) else str(dst), "jsonl")
        assert ei.value.key == "repair.error.report_target"
        assert "repaired file" in str(ei.value) and "fixed.json" in str(ei.value).lower()
        assert src.read_bytes() == BROKEN and dst.read_bytes() == fixed
        rep.write(tmp_path / "report.csv")                    # any other name works
        assert (tmp_path / "report.csv").read_bytes().startswith(b"\xef\xbb\xbfcode,name,category")
    finally:
        rep.close()
    chk = repair_file(src, None, check_only=True)             # a check-only report knows the source too
    try:
        with pytest.raises(UserInputError):
            chk.write(src, "json")
    finally:
        chk.close()
    assert src.read_bytes() == BROKEN
    assert sorted(p.name for p in tmp_path.iterdir()) == ["broken.json", "fixed.json", "hard_link.json", "report.csv"]


@pytest.mark.parametrize("kind", ["gzip", "utf16"])
def test_report_write_refuses_an_open_converted_source_and_its_copy(tmp_path: Path, docs, kind: str) -> None:
    """A Document of a gzip / UTF-16 source holds only the prepared copy: nothing else blocks the replace."""
    if kind == "gzip":
        src = tmp_path / "broken.json.gz"
        raw = gzip.compress(b'[{"id": 1, "v": NaN}, {"id": 2}]')
    else:
        src = tmp_path / "u16.json"
        raw = '[{"id": 1, "ism": "Oʻzbekiston", "v": NaN}]'.encode("utf-16")
    src.write_bytes(raw)
    doc = _open(src, docs)                                    # the tab that offered "Ta'mirlash..."
    rep = repair_file(src, tmp_path / "fixed.json")
    try:
        assert rep.data_path is not None and rep.data_path != src
        for target in (src, rep.data_path, doc.data_path):
            with pytest.raises(UserInputError) as ei:
                rep.write(target, "csv")
            assert ei.value.key == "writer.error.same_file"
    finally:
        rep.close()
    assert src.read_bytes() == raw


# ==================================================================================================
# R5F-2: write_diff never replaces a compared document
# ==================================================================================================
A_TEXT = b'[{"id": 1, "v": "original A data"}, {"id": 2, "v": 2}]'
B_TEXT = b'[{"id": 1, "v": "changed"}, {"id": 2, "v": 2}]'


def _ab(tmp_path: Path, gz: bool = False) -> tuple[Path, Path, bytes]:
    suffix = ".json.gz" if gz else ".json"
    a, b = tmp_path / f"a{suffix}", tmp_path / f"b{suffix}"
    a_raw = gzip.compress(A_TEXT) if gz else A_TEXT
    a.write_bytes(a_raw)
    b.write_bytes(gzip.compress(B_TEXT) if gz else B_TEXT)
    return a, b, a_raw


@pytest.mark.parametrize("gz", [False, True])
def test_write_diff_refuses_an_open_compared_document(tmp_path: Path, docs, gz: bool) -> None:
    a, b, a_raw = _ab(tmp_path, gz)
    da, db = _open(a, docs), _open(b, docs)
    targets = [a, b, da.data_path, db.data_path] + ([str(a).upper()] if case_folds(tmp_path) else [])
    for target in targets:
        gen = diff.iter_record_diffs(da, db, key="id")     # the CLI's streaming --out
        with pytest.raises(UserInputError) as ei:
            diff.write_diff(gen, target, "json")
        assert ei.value.key == "writer.error.same_file", target
        assert gen.gi_frame is None                          # the generator was closed
    assert a.read_bytes() == a_raw
    n = diff.write_diff(diff.iter_record_diffs(da, db, key="id"), tmp_path / "d.json", "json")
    assert n == 1


def test_write_diff_refuses_closed_documents_through_the_report(tmp_path: Path) -> None:
    a, b, a_raw = _ab(tmp_path)
    diffs: list = []
    with Document.open(a) as da, Document.open(b) as db:
        rep = diff.diff_files(da, db, key="id", on_record=diffs.append)
    assert {fileio.normalize_path(p) for p in rep.sources} >= {fileio.normalize_path(a), fileio.normalize_path(b)}
    for target in (str(a).upper() if case_folds(tmp_path) else a, b, tmp_path / "x" / ".." / "a.json"):
        with pytest.raises(UserInputError) as ei:
            diff.write_diff(diffs, target, "jsonl", report=rep)
        assert ei.value.key == "writer.error.same_file"
    assert a.read_bytes() == a_raw
    # no report: the caller names the sources (a list, or one path)
    with pytest.raises(UserInputError):
        diff.write_diff(diffs, a, "csv", sources=[a, b])
    with pytest.raises(UserInputError):
        diff.write_diff(diffs, b, "csv", sources=str(b))
    assert a.read_bytes() == a_raw
    assert diff.write_diff(diffs, tmp_path / "d.jsonl", "jsonl", report=rep) == 1


def test_write_diff_checks_the_report_of_a_wrapping_generator_before_the_commit(tmp_path: Path) -> None:
    """A generator that is not the one iter_record_diffs returned: its report (the return value) is only known at
    the end -- checked before the result replaces anything."""
    a, b, a_raw = _ab(tmp_path)
    with Document.open(a) as da, Document.open(b) as db:
        def wrapper():
            rep = yield from diff.iter_record_diffs(da, db, key="id")
            return rep

        with pytest.raises(UserInputError) as ei:
            diff.write_diff(wrapper(), str(a).lower(), "json")
        assert ei.value.key == "writer.error.same_file"
    assert a.read_bytes() == a_raw
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json", "b.json"]     # no temporary file left


def test_align_and_its_op_codes_are_public_and_the_private_names_stay() -> None:
    """Request of the compare-GUI round: gui/diffview.py aligns text lines with ``diff._align`` and the
    ``_EQ`` / ``_PAIRS`` / ``_DEL`` / ``_INS`` codes; the public names are the same objects."""
    assert diff.align is diff._align
    public = (diff.OP_EQ, diff.OP_PAIRS, diff.OP_DEL, diff.OP_INS)
    private = (diff._EQ, diff._PAIRS, diff._DEL, diff._INS)
    assert all(p is q for p, q in zip(public, private)) and public == (0, 1, 2, 3)
    assert {"align", "OP_EQ", "OP_PAIRS", "OP_DEL", "OP_INS"} <= set(diff.__all__)
    assert diff.align(["a", "b", "c"], ["a", "x", "c", "d"]) == [
        (diff.OP_EQ, 0, 0, 1), (diff.OP_PAIRS, 1, 1, 1), (diff.OP_EQ, 2, 2, 1), (diff.OP_INS, 3, 1)]
    assert diff.align([b"1", b"2"], [b"0", b"1", b"2"]) == [(diff.OP_INS, 0, 1), (diff.OP_EQ, 0, 1, 2)]
    assert diff.align(["a", "b"], ["a"]) == [(diff.OP_EQ, 0, 0, 1), (diff.OP_DEL, 1, 1)]


# ==================================================================================================
# R5F-3: a file with unsaved edits is never replaced (split / merge / extract)
# ==================================================================================================
def _source(tmp_path: Path, n: int = 10) -> Path:
    src = tmp_path / "data.json"
    src.write_bytes(b"[" + b",".join(b'{"id": %d, "v": "orig"}' % i for i in range(n)) + b"]")
    return src


def test_split_refuses_to_replace_a_part_with_unsaved_edits(tmp_path: Path, docs, lang_en) -> None:
    doc = _open(_source(tmp_path), docs)
    split(doc, SplitOptions(parts=2))
    part1 = tmp_path / "data_1.json"
    before = {p.name: p.read_bytes() for p in tmp_path.glob("data_*.json")}
    tab = _open(part1, docs)                                 # another tab shows part 1 ...
    sess = EditSession.open(tab)
    try:
        sess.set_value((0, ("v",)), '"MY UNSAVED EDIT"')     # ... with an unsaved edit
        for opts in (SplitOptions(parts=2), SplitOptions(parts=2, export=ExportOptions("json", indent=None))):
            with pytest.raises(UserInputError) as ei:
                split(doc, opts)
            assert ei.value.key == "filetools.error.part_open_edited"
            assert "data_1.json" in str(ei.value)
        assert {p.name: p.read_bytes() for p in tmp_path.glob("data_*.json")} == before
        assert not tab.check_changed()                       # the edits can still be saved
        assert not [p for p in tmp_path.iterdir() if p.name.endswith((".jrpart", ".jrold"))]
    finally:
        sess.close()                                         # the layer stays for recovery (pending)
    with pytest.raises(UserInputError) as ei:
        split(doc, SplitOptions(parts=2))
    assert ei.value.key == "filetools.error.part_open_edited"
    sess = EditSession.open(tab)                             # restores the pending layer
    sess.discard()
    sess.close()
    rep = split(doc, SplitOptions(parts=2, export=ExportOptions("json", indent=None)))   # no edits any more
    assert [p.path.name for p in rep.parts] == ["data_1.json", "data_2.json"]


def test_a_part_shown_without_edits_is_still_replaced(tmp_path: Path, docs) -> None:
    doc = _open(_source(tmp_path), docs)
    split(doc, SplitOptions(parts=2))
    tab = _open(tmp_path / "data_2.json", docs)
    sess = EditSession.open(tab)                             # an edit session without edits holds the lock only
    try:
        rep = split(doc, SplitOptions(parts=2, export=ExportOptions("json", indent=None)))
        assert [p.path.name for p in rep.parts] == ["data_1.json", "data_2.json"]
    finally:
        sess.close()


def test_merge_and_extract_refuse_an_output_with_unsaved_edits(tmp_path: Path, docs, lang_en) -> None:
    doc = _open(_source(tmp_path), docs)
    out = tmp_path / "out.json"
    out.write_bytes(b'[{"x": 1}]')
    tab = _open(out, docs)
    sess = EditSession.open(tab)
    try:
        sess.set_value((0, ("x",)), "2")
        with pytest.raises(UserInputError) as ei:
            filetools.merge([doc], out, MergeOptions(ExportOptions("json")))
        assert ei.value.key == "filetools.error.part_open_edited" and "out.json" in str(ei.value)
        with pytest.raises(UserInputError) as ei:
            filetools.extract(doc, out, ExportOptions("json"), path="v")
        assert ei.value.key == "filetools.error.part_open_edited"
        assert out.read_bytes() == b'[{"x": 1}]'
    finally:
        sess.discard()
        sess.close()
    tab.close()                       # (Windows: the writer's os.replace cannot replace a file a reader holds)
    assert filetools.merge([doc], out, MergeOptions(ExportOptions("json"))).records == 10


# ==================================================================================================
# R5F-5: commit_files names failed restores; old asides are swept
# ==================================================================================================
def _stage(folder: Path, contents: list[bytes]) -> list[Path]:
    staged = []
    for i, data in enumerate(contents):
        p = folder / f".staged{i}.jrpart"
        p.write_bytes(data)
        staged.append(p)
    return staged


def test_commit_files_names_every_failed_restore(tmp_path: Path, monkeypatch, lang_en) -> None:
    out = tmp_path / "o"
    out.mkdir()
    finals = [out / f"p_{k}.json" for k in (1, 2, 3, 4)]
    for k, p in enumerate(finals):
        p.write_bytes(b"old %d" % k)
    staged = _stage(out, [b"n1", b"n2", b"n3", b"n4"])
    real = fileio.move_replace

    def move(a: Path, b: Path) -> None:                      # placing p_3 fails ...
        if a.name.startswith(".staged") and b.name == "p_3.json":
            raise OSError(errno.EIO, "I/O error", str(b))
        real(a, b)

    def undo(a, b, **kw):                                    # ... and so do the restores of p_2 and p_4
        if str(a).endswith(fileio.ASIDE_SUFFIX) and Path(b).name in ("p_2.json", "p_4.json"):
            raise PermissionError(errno.EACCES, "held by a scanner", str(b))
        return real(a, b, **kw)

    monkeypatch.setattr(fileio, "move_replace", undo)
    with pytest.raises(JsonReshapeError) as ei:
        fileio.commit_files(staged, finals, move=move)
    exc = ei.value
    assert exc.key == "fileio.error.restore_failed"
    asides = sorted(p for p in out.iterdir() if p.name.endswith(fileio.ASIDE_SUFFIX))
    assert [p.read_bytes() for p in asides] in ([b"old 1", b"old 3"], [b"old 3", b"old 1"])
    text = str(exc)
    for p in asides:
        assert str(p) in text                                # where the old content now is ...
    assert str(finals[1]) in text and str(finals[3]) in text    # ... and which file it belongs to
    assert "p_3.json" in text                                # the original error is told too
    assert isinstance(exc.__cause__, JsonReshapeError) and "p_3.json" in str(exc.__cause__)
    assert finals[0].read_bytes() == b"old 0" and finals[2].read_bytes() == b"old 2"
    assert not finals[1].exists() and not finals[3].exists()


def _age(monkeypatch, paths: list[Path], seconds: float) -> None:
    """Make ``paths`` look renamed ``seconds`` ago (the sweep ages a copy by its change time -- a rename keeps
    the modification time -- and setting the times with os.utime counts as a change)."""
    then = time.time() - seconds
    for p in paths:
        os.utime(p, (then, then))
    names = {p.name for p in paths}
    real = fileio.change_time
    monkeypatch.setattr(fileio, "change_time", lambda p: then if Path(p).name in names else real(p))


def test_commit_files_sweeps_old_asides_of_its_names(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "o"
    out.mkdir()
    final = out / "a.json"
    final.write_bytes(b"old")
    long_final = out / ("L" * 60 + ".json")
    long_final.write_bytes(b"old long")
    stale = out / ".a.json.0badf00d.jrold"                    # a failed restore (or a crash) an hour ago
    fresh = out / ".a.json.1badf00d.jrold"                    # maybe another commit in progress right now
    other = out / ".b.json.2badf00d.jrold"                    # another file's copy
    long_stale = out / f".{long_final.name[:48]}.3badf00d.jrold"   # the 48-character prefix is ambiguous
    for p in (stale, fresh, other, long_stale):
        p.write_bytes(b"copy")
    _age(monkeypatch, [stale, other, long_stale], 3600)
    old_mtime = out / ".a.json.4badf00d.jrold"                # renamed just now, an old modification time
    old_mtime.write_bytes(b"copy")
    then = time.time() - 3600
    os.utime(old_mtime, (then, then))
    left = fileio.commit_files(_stage(out, [b"new", b"new long"]), [final, long_final])
    assert left == []
    assert final.read_bytes() == b"new" and long_final.read_bytes() == b"new long"
    assert not stale.exists()
    assert fresh.exists() and other.exists() and long_stale.exists() and old_mtime.exists()


@pytest.mark.skipif(not WINDOWS, reason="an open file blocks deleting it on Windows only")
def test_commit_files_keeps_an_old_aside_that_is_open(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "o"
    out.mkdir()
    final = out / "a.json"
    stale = out / ".a.json.0badf00d.jrold"
    stale.write_bytes(b"the old content, open in an editor")
    _age(monkeypatch, [stale], 3600)
    with open(stale, "rb"):
        fileio.commit_files(_stage(out, [b"new"]), [final])
        assert stale.exists()
    assert final.read_bytes() == b"new"
    fileio.commit_files(_stage(out, [b"newer"]), [final])      # closed now: removed by the next commit
    assert not stale.exists() and final.read_bytes() == b"newer"


# ==================================================================================================
# R5F-6: the dropped tail of a partial result is a counted fix
# ==================================================================================================
def test_partial_result_lists_the_dropped_tail(tmp_path: Path, lang_en) -> None:
    data = b'[{"a": 1}, {"b": hello}, {"c": 3}]'
    out, rep = repair_bytes(tmp_path, data, windows=(None, 1, 7), allow_partial=True)
    assert out == b'[{"a": 1}]'
    assert rep.problem is not None and rep.problem.code == "unquoted_value" and rep.written
    assert rep.total == 1 and rep.counts == {"dropped_tail": 1}
    (fx,) = rep.fixes
    assert (fx.code, fx.category, fx.offset, fx.line, fx.column) == ("dropped_tail", "dropped", 9, 1, 10)
    assert (fx.length, fx.out_offset, fx.out_length, fx.record) == (25, 9, 1, None)
    assert (fx.before, fx.after, fx.reason) == (', {"b": hello}, {"c": 3}]', "]", "unquoted_value")
    assert rep.truncated is not None and rep.truncated.dropped_bytes == 25
    assert rep.data_changes_line() is not None and "dropped" in rep.data_changes_line()
    assert any(fx.name in line for line in rep.summary_lines())
    check_coverage(data, out, rep)
    report = tmp_path / "rep.csv"
    rep2 = repair_file(tmp_path / "in.json", tmp_path / "again.json", RepairOptions(allow_partial=True))
    try:
        rep2.write(report)
    finally:
        rep2.close()
    rows = report.read_bytes().decode("utf-8-sig").strip().splitlines()
    assert len(rows) == 2 and rows[1].startswith("dropped_tail,")
    # without allow_partial nothing is written; the report tells what the partial result would drop
    out, rep = repair_bytes(tmp_path, data, windows=(None, 7))
    assert out is None and rep.counts == {"dropped_tail": 1} and not rep.written


def test_dropped_tail_covers_padding_and_lines(tmp_path: Path) -> None:
    # NUL padding after an unrepairable place belongs to the dropped tail
    data = b'[{"a": 1}, {"b": hello}]\x00\x00\x00'
    out, rep = repair_bytes(tmp_path, data, windows=(None, 7), allow_partial=True)
    assert out == b'[{"a": 1}]' and rep.counts == {"dropped_tail": 1}
    assert rep.fixes[0].length == len(data) - 9
    check_coverage(data, out, rep)
    # JSON Lines: a line that stops the repair (line_dropped turned off) drops everything from its start
    data = b'{"a": 1}\n{"b": hello}\n{"c": 3}\n'
    fixes = frozenset(repair.FIX_CODES) - {"line_dropped"}
    out, rep = repair_bytes(tmp_path, data, "in.jsonl", windows=(None, 7), out_name="o.jsonl", allow_partial=True,
                            fixes=fixes)
    assert out == b'{"a": 1}\n' and rep.counts == {"dropped_tail": 1} and rep.problem.code == "unquoted_value"
    fx = rep.fixes[0]
    assert (fx.offset, fx.length, fx.out_offset, fx.out_length, fx.reason) == (9, len(data) - 9, 9, 0,
                                                                               "unquoted_value")
    check_coverage(data, out, rep)
    # trailing data that may not be removed (trailing_data turned off) is dropped as the partial tail
    data = b'[{"a": 1}]\x00\x00'
    fixes = frozenset(repair.FIX_CODES) - {"trailing_data"}
    out, rep = repair_bytes(tmp_path, data, windows=(None, 7), allow_partial=True, fixes=fixes)
    assert out == b'[{"a": 1}]' and rep.counts == {"dropped_tail": 1} and rep.problem.code == "disabled"
    assert (rep.fixes[0].offset, rep.fixes[0].length) == (10, 2)
    check_coverage(data, out, rep)


# ==================================================================================================
# R5F-7: the trailing_data preview of a JSON Lines line ends with the line
# ==================================================================================================
def test_jsonl_trailing_data_preview_stays_on_its_line(tmp_path: Path) -> None:
    data = b'{"a": 1} garbage\n{"b": 2}\n{"c": 3}\n'
    out, rep = repair_bytes(tmp_path, data, "in.jsonl", windows=(None, 1, 7), out_name="o.jsonl")
    assert out == b'{"a": 1} \n{"b": 2}\n{"c": 3}\n'
    (fx,) = rep.fixes
    assert (fx.code, fx.offset, fx.length, fx.before, fx.after) == ("trailing_data", 9, 7, "garbage", "")


# ==================================================================================================
# R5F-8: a NUL byte before UTF-8 JSON is not UTF-16
# ==================================================================================================
@pytest.mark.parametrize("head, encoding", [
    (b'\x00{"a": 1}\n{"b": 2}\n', "utf-8"),          # the fuzzer's file (seed 2121)
    (b'\x00[1, 2]', "utf-8"),
    (b'{\x00"a": 1}', "utf-8"),                       # "{" NUL then UTF-8: not UTF-16 LE either
    (b'1\x00re', "utf-8"),
    ('[{"a": 1}]'.encode("utf-16-be"), "utf-16-be"),  # real BOM-less UTF-16 still is
    ('[{"a": 1}]'.encode("utf-16-le"), "utf-16-le"),
    ('"Ўзбекистон"'.encode("utf-16-be"), "utf-16-be"),
    ('"Ўзбекистон"'.encode("utf-16-le"), "utf-16-le"),
    (b"\x00[", "utf-16-be"),                          # too short to tell: the first code unit decides
])
def test_sniff_needs_two_utf16_code_units(head: bytes, encoding: str) -> None:
    assert fileio.sniff(head).encoding == encoding


def test_repair_and_reader_handle_a_leading_nul(tmp_path: Path) -> None:
    src = tmp_path / "c.jsonl"
    src.write_bytes(b'\x00{"a": 1}\n{"b": 2}\n')
    rep = repair_file(src, None, check_only=True)              # before: fileio.error.decode (UTF-16 BE)
    try:
        assert rep.encoding == "utf-8"
        assert rep.problem is not None or rep.total > 0
        first = rep.problem.offset if rep.problem is not None else rep.fixes[0].offset
        assert first == 0
    finally:
        rep.close()
    with Document.open(src) as doc:                            # JSON Lines: the first line is a broken line
        doc.build_index()
        assert doc.info.encoding == "utf-8"
        assert doc.error is None and doc.record_count == 1 and doc.broken_count == 1
    arr = tmp_path / "a.json"
    arr.write_bytes(b'\x00[1, 2]')
    with Document.open(arr) as doc:                            # JSON: the error names the NUL byte
        doc.build_index()
        assert doc.info.encoding == "utf-8"
        assert doc.error is not None and doc.error.offset == 0


# ==================================================================================================
# R5F-9: stale parts
# ==================================================================================================
def _x_source(tmp_path: Path, n: int = 120) -> Path:
    src = tmp_path / "x.json"
    src.write_bytes(b"[" + b",".join(b'{"id": %d}' % i for i in range(n)) + b"]")
    return src


def test_stale_parts_warning_names_them_and_says_what_to_do(tmp_path: Path, docs, lang_en) -> None:
    doc = _open(_x_source(tmp_path), docs)
    split(doc, SplitOptions(parts=12))
    rep = split(doc, SplitOptions(parts=10))
    assert [p.name for p in rep.stale] == ["x_11.json", "x_12.json"]
    (text,) = rep.warnings
    assert "(2): x_11.json, x_12.json" in text and "twice" in text
    split(doc, SplitOptions(parts=40))
    rep = split(doc, SplitOptions(parts=3))
    assert len(rep.stale) == 40
    (text,) = rep.warnings
    assert "(40): x_01.json, x_02.json" in text and text.count(".json") == 7 and "x_40.json" in text


def test_remove_stale_parts(tmp_path: Path, docs, lang_en) -> None:
    doc = _open(_x_source(tmp_path), docs)
    with pytest.raises(UserInputError):
        SplitOptions(parts=2, remove_stale="yes")              # type: ignore[arg-type]
    split(doc, SplitOptions(parts=12))
    (tmp_path / "x_2023.json").write_bytes(b'[{"mine": true}]')   # the user's own file of the same pattern
    rep = split(doc, SplitOptions(parts=10, remove_stale=True))
    assert [p.name for p in rep.removed] == ["x_11.json", "x_12.json"]
    assert [p.name for p in rep.stale] == ["x_2023.json"]          # not a part of an earlier split: kept
    assert "filetools.warn.stale_removed" in rep.warning_keys and "filetools.warn.stale_parts" in rep.warning_keys
    names = sorted(p.name for p in tmp_path.glob("x_*.json"))
    assert names == [f"x_{k:02d}.json" for k in range(1, 11)] + ["x_2023.json"]
    assert (tmp_path / "x_2023.json").read_bytes() == b'[{"mine": true}]'
    parts = [Document.open(tmp_path / f"x_{k:02d}.json") for k in range(1, 11)]
    try:
        merged = filetools.merge(parts, tmp_path / "merged.json", MergeOptions(ExportOptions("json")))
    finally:
        for p in parts:
            p.close()
    assert merged.records == 120
    # another padding: the 1-digit parts of a split into 9 are an earlier set too
    split(doc, SplitOptions(parts=9))
    rep = split(doc, SplitOptions(parts=10, remove_stale=True))
    assert [p.name for p in rep.removed] == [f"x_{k}.json" for k in range(1, 10)]
    assert not [p for p in tmp_path.iterdir() if p.name.endswith((".jrpart", ".jrold"))]


def test_remove_stale_never_touches_a_part_with_unsaved_edits(tmp_path: Path, docs, lang_en) -> None:
    doc = _open(_x_source(tmp_path), docs)
    split(doc, SplitOptions(parts=12))
    tab = _open(tmp_path / "x_12.json", docs)
    sess = EditSession.open(tab)
    try:
        sess.set_value((0, ("id",)), "999")
        with pytest.raises(UserInputError) as ei:
            split(doc, SplitOptions(parts=10, remove_stale=True))
        assert ei.value.key == "filetools.error.part_open_edited" and "x_12.json" in str(ei.value)
        assert (tmp_path / "x_11.json").exists() and not tab.check_changed()
    finally:
        sess.discard()
        sess.close()


# ==================================================================================================
# part_names: the files split writes, known before (request of the CLI and split-GUI rounds)
# ==================================================================================================
def test_part_names_are_the_files_split_writes(tmp_path: Path, docs) -> None:
    from jsonreshape.recordset import RecordSet
    from saver_helpers import make_doc

    arr = make_doc(tmp_path / "a", "array", n=30, seed=1)
    gz = make_doc(tmp_path / "g", "array", n=12, seed=2, encoding="gzip")              # src.json.gz
    gz_plain_name = make_doc(tmp_path / "h", "array", n=12, seed=2, encoding="gzip", name="data.json")
    jl_gz = make_doc(tmp_path / "j", "jsonl", n=25, seed=3, encoding="gzip", name="x.jsonl.gz")
    brace = make_doc(tmp_path / "b", "brace_list", n=11, seed=4, name="list.txt")      # parts: list_k.json
    docs.extend([arr, gz, gz_plain_name, jl_gz, brace])
    other = tmp_path / "other"
    other.mkdir()
    cases = [
        (arr, SplitOptions(parts=3), None),
        (arr, SplitOptions(records=7), None),
        (arr, SplitOptions(size=600), None),                                             # 10+ parts: _01 ..
        (arr, SplitOptions(parts=4, name="{stem}-qism{n}{ext}", out_dir=other), None),
        (arr, SplitOptions(parts=2), range(5, 20)),                                      # a range: byte copy
        (arr, SplitOptions(parts=2), RecordSet.base(arr)),                               # all records
        (arr, SplitOptions(parts=3), RecordSet.from_indices([29, 3, 17, 0, 8])),         # a view: the writer
        (arr, SplitOptions(parts=2, export=ExportOptions("csv")), None),
        (arr, SplitOptions(parts=2, export=ExportOptions("xlsx")), None),
        (arr, SplitOptions(parts=2, export=ExportOptions("sqlite")), None),
        (gz, SplitOptions(parts=3), None),
        (gz, SplitOptions(parts=2, export=ExportOptions("jsonl")), None),
        (gz, SplitOptions(parts=2, export=ExportOptions("xlsx")), None),
        (gz_plain_name, SplitOptions(records=5), None),                                  # .gz added
        (jl_gz, SplitOptions(parts=11), None),                                           # double extension
        (jl_gz, SplitOptions(parts=2), [24, 0, 3]),                                      # a view of lines
        (brace, SplitOptions(parts=2), None),                                            # brace list -> .json
    ]
    for doc, opts, source in cases:
        plan = filetools.plan_split(doc, opts, source)
        names = filetools.part_names(doc, opts, source, count=len(plan))
        rep = split(doc, opts, source)
        assert [p.path for p in rep.parts] == names, (doc.source_path.name, opts, source)
        assert all(p.exists() for p in names)
    assert [p.name for p in filetools.part_names(arr, SplitOptions(size=600), count=12)][:2] == ["src_01.json",
                                                                                              "src_02.json"]
    assert [p.name for p in filetools.part_names(gz_plain_name, SplitOptions(parts=2), count=2)] == [
        "data_1.json.gz", "data_2.json.gz"]
    assert [p.name for p in filetools.part_names(brace, SplitOptions(parts=2), count=2)] == ["list_1.json",
                                                                                            "list_2.json"]
    assert [p.name for p in filetools.part_names(jl_gz, SplitOptions(parts=2), [3, 1], count=2)] == [
        "x_1.jsonl.gz", "x_2.jsonl.gz"]
    with pytest.raises(UserInputError):
        filetools.part_names(arr, SplitOptions(parts=2), count=0)


# ==================================================================================================
# the index cache scan
# ==================================================================================================
def _fill_cache(cache: Path, n_index: int, n_prepared: int) -> None:
    pdir = cache / "tmp" / "prepared"
    pdir.mkdir(parents=True, exist_ok=True)
    old = time.time() - 7200
    for k in range(n_index):
        p = cache / f"{k:064x}.jri"
        p.write_bytes(b"x" * 100)
        os.utime(p, (old, old))
    for k in range(n_prepared):
        (pdir / f"{k:064x}.json").write_bytes(b"[1]")
        m = pdir / f"{k:064x}.meta"
        m.write_bytes(b"{}")
        os.utime(m, (old, old))


def test_indexing_does_not_rescan_the_cache_each_time(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    _fill_cache(cache, 300, 100)
    settings = Settings(cache_dir=str(cache))
    scans: list[Path] = []
    real = index_mod._index_entries

    def counting(base, *args, **kw):
        scans.append(Path(base))
        return real(base, *args, **kw)

    monkeypatch.setattr(index_mod, "_index_entries", counting)
    src = tmp_path / "small.json"
    for k in range(5):
        src.write_bytes(b"[" + b",".join(b'{"id": %d}' % i for i in range(50 + k)) + b"]")   # a new version
        with Document.open(src, settings=settings) as doc:
            doc.build_index()
    assert len(scans) == 1                                     # the first eviction of the process only
    # over the budget: the next index build scans again and evicts the least recently used files
    small = Settings(cache_dir=str(cache), cache_limit_bytes=5000)
    src.write_bytes(b"[1, 2, 3]")
    with Document.open(src, settings=small) as doc:
        doc.build_index()
        mine = doc.index_path
    assert len(scans) == 2 and mine.exists()
    assert len(list(cache.glob("*.jri"))) < 300


def _prepared_folder(folder: Path) -> Path:
    folder.mkdir(parents=True)
    now = time.time()
    files = {
        "a.json": (b"[1, 2, 3]", now - 7200), "a.meta": (b"{}", now - 7200),          # old: removable
        "b.json": (b"[1]", now - 60), "b.meta": (b"{}", now - 60),                    # used a minute ago
        "c.json": (b"[]", now - 7200), "c.meta": (b"{}", now - 7200),                # kept by the caller
        "d.json": (b"[4]", now - 7200), "d.meta": (b"{}", now - 7200),               # read by an open document
        "e.meta": (b"{}", now - 7200),                                                # its data file is gone
        "orphan_old.json": (b"x", now - 7200), "orphan_new.json": (b"y", now - 60),  # data without metadata
        "x.part": (b"p", now - 3 * 86400), "y.part": (b"q", now - 60),             # copies being written
        "other.txt": (b"t", now - 7200),
    }
    for name, (data, when) in files.items():
        (folder / name).write_bytes(data)
        os.utime(folder / name, (when, when))
    return folder


def test_the_fast_prepared_scan_equals_the_old_one(tmp_path: Path) -> None:
    results = []
    for k, scan in enumerate((fileio.prepared_cache_entries, fileio.scan_prepared_entries)):
        folder = _prepared_folder(tmp_path / f"p{k}")
        fileio.retain_prepared(folder / "d.json")
        try:
            entries = scan(folder, keep=[str(folder / ("C.JSON" if case_folds(tmp_path) else "c.json"))])
        finally:
            fileio.release_prepared(folder / "d.json")
        age = time.time()
        rows = sorted((tuple(p.name for p in e.paths), e.size, e.protected, round((age - e.last_used) / 60))
                      for e in entries)                       # (the two folders were made moments apart)
        results.append((rows, sorted(os.listdir(folder))))
    assert results[0] == results[1]
    rows, left = results[1]
    assert [r[0][0] for r in rows if r[2]] == ["b.json", "c.json", "d.json"]
    assert [r[0][0] for r in rows] == ["a.json", "b.json", "c.json", "d.json", "e.json"]
    assert "orphan_old.json" not in left and "x.part" not in left and {"orphan_new.json", "y.part"} <= set(left)


def test_a_cache_scan_does_no_per_entry_path_work(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    _fill_cache(cache, 400, 150)
    calls = [0]
    real = fileio.long_path

    def counting(p):
        calls[0] += 1
        return real(p)

    monkeypatch.setattr(index_mod, "long_path", counting)
    monkeypatch.setattr(fileio, "long_path", counting)
    freed = index_mod.evict(1 << 40, directory=cache, prepared_dir=cache / "tmp" / "prepared")
    assert freed == 0 and calls[0] <= 4
    # the protections still hold: a kept file and recently used copies stay, the rest goes LRU first
    keep = cache / f"{0:064x}.jri"
    recent = cache / "tmp" / "prepared" / f"{0:064x}.meta"
    os.utime(recent, None)
    freed = index_mod.evict(0, keep=[keep], directory=cache, prepared_dir=cache / "tmp" / "prepared")
    assert freed == 399 * 100 + 149 * 3
    assert keep.exists() and recent.exists() and recent.with_suffix(".json").exists()
    assert len(list(cache.glob("*.jri"))) == 1
