"""saver.save (docs/ARCHITECTURE.md §12.7): Save / Save As of edited documents in their own kind.

AGENT_PROMPT §1.4 (the original is replaced only by Save, atomically, with a .bak), §8.3 (saving is
streamed and the result is validated), §14 test 19 (save with edits, reopen: the expected result; the
original stays unchanged). For every document kind (the 7 kinds + a wrapped document with an object
container) x edit pattern (none, point edits, inserts at the start / middle / end, deletes, deleting
every record, moves, an array layer over every 3rd record, a mix with two array layers, renamed keys, an
edited wrapper prefix and suffix), on both core backends (styles: ``test_saver_styles.py``): (1) the saved file reopens (with ``reopen_options``) to exactly the expected records, (2) its
bytes are exactly those of an independent record-by-record oracle of the §12.7 rules (so untouched
records and their gaps are byte-identical to the original), (3) Save As leaves the original unchanged and
an in-place save leaves it as ``.bak``; (4) a corrupted unit or a wrong record count fails the
validation and nothing is replaced. Encodings, the commit sequence, JSON Lines broken lines and the
performance target have their own modules (``test_saver_*.py``).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from jsonreshape import editops, reader, saver
from jsonreshape.errors import Cancelled, ExportError, UserInputError
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from overlay_helpers import KINDS, OverlayBuilder, new_value
from saver_helpers import PATTERNS, check_saved, edited, make_doc, oracle_text, temp_leftovers


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


# ----------------------------------------------------------------------------------
# (1)-(3): kinds x patterns (both backends), Save As and in place
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("kind", KINDS)
def test_save_as_every_kind_and_pattern(tmp_path, docs, backend, kind, pattern) -> None:
    doc, ob = edited(tmp_path, docs, kind, pattern)
    src = doc.source_path
    before = src.read_bytes()
    out = tmp_path / "out" / "saved.json"
    out.parent.mkdir()
    report = saver.save(doc, out)
    assert src.read_bytes() == before, "Save As never touches the original"
    assert report.target == out and not report.in_place and report.backup is None
    assert report.kind == ("array" if kind == "brace_list" else doc.kind)
    assert report.reopen_options == {"format": report.kind,
                                     "record_path": tuple(doc.record_path) if doc.record_path else None}
    assert report.bytes_written == out.stat().st_size
    assert report.records == doc.record_count
    check_saved(doc, ob, out, report)
    assert temp_leftovers(out.parent) == [] and temp_leftovers(src.parent) == []


@pytest.mark.parametrize("pattern", ["none", "point", "structure", "every3", "wrapper", "delete_all"])
@pytest.mark.parametrize("kind", KINDS)
def test_save_in_place_keeps_a_backup(tmp_path, docs, kind, pattern) -> None:
    doc, ob = edited(tmp_path, docs, kind, pattern)
    src = doc.source_path
    before = src.read_bytes()
    want = oracle_text(doc, ob)
    first = doc.pinned(None).record_bytes(0) if doc.base_record_count else None   # opens our read handle
    report = saver.save(doc)
    assert report.in_place and report.target == src
    bak = src.with_name(src.name + ".bak")
    assert report.backup == bak and bak.read_bytes() == before
    assert src.read_bytes() == want
    check_saved(doc, ob, src, report, want=want)
    assert temp_leftovers(src.parent) == []
    # the old document still reads the version it opened (now the .bak) through its open handle,
    # and knows the file changed
    if first is not None:
        assert doc.pinned(None).record_bytes(0) == first == before[slice(*doc.pinned(None).record_span(0))]
    assert doc.check_changed()


def test_untouched_neighbours_keep_their_gaps(tmp_path, docs) -> None:
    """Irregular separators of the file survive an edit of the record between them (§12.7: the original
    gap between consecutive records of the file, also when one of them is edited)."""
    text = b'[ {"a": 1} ,\n\n  {"a": 2},{"a": 3}   ,\t{"a": 4} ]'
    doc = make_doc(tmp_path / "s", "array", text=text, open_kw={"format": "array"})
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(1, b'{"a": 20}')
    ob.set(2, b'{"a": 30}')
    doc.set_overlay(ob.state())
    out = tmp_path / "o.json"
    saver.save(doc, out)
    assert out.read_bytes() == b'[ {"a": 1} ,\n\n  {"a": 20},{"a": 30}   ,\t{"a": 4} ]'
    # a moved record gets the document's separator on both sides
    ob.move([3], 0)
    doc.set_overlay(ob.state())
    saver.save(doc, out)
    sep = editops.detect_style(doc).record_sep
    assert out.read_bytes() == b'[ {"a": 4}' + sep + b'{"a": 1} ,\n\n  {"a": 20},{"a": 30} ]'


def test_save_unedited_document_is_byte_identical(tmp_path, docs) -> None:
    for kind in KINDS:
        if kind == "brace_list":
            continue
        doc = make_doc(tmp_path / kind, kind, "messy", n=10, seed=5)
        docs.append(doc)
        out = tmp_path / f"{kind}.out.json"
        report = saver.save(doc, out)
        assert out.read_bytes() == doc.source_path.read_bytes(), kind
        assert report.edited_records == 0 and report.copied_bytes == doc.data_size - doc.info.data_start


def test_brace_list_becomes_an_array(tmp_path, docs, lang_en) -> None:
    text = b'  {\n  {"a": 1},\n  {"b": [2, {"c": 3}]}\n}  \n'
    doc = make_doc(tmp_path / "s", "brace_list", text=text, open_kw={"format": "brace_list"})
    docs.append(doc)
    out = tmp_path / "o.json"
    report = saver.save(doc, out)
    assert out.read_bytes() == b'  [\n  {"a": 1},\n  {"b": [2, {"c": 3}]}\n]  \n'
    assert report.kind == "array" and report.reopen_options["format"] == "array"
    assert report.warning_keys == ["saver.warn.brace_list"]
    assert "{ {...}, {...} }" in report.warnings[0]
    # and with every record deleted: "[]"
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.delete(0, 1)
    doc.set_overlay(ob.state())
    saver.save(doc, out)
    assert out.read_bytes() == b"  []  \n"


def test_exact_numbers_unicode_and_escapes_survive(tmp_path, docs, backend) -> None:
    import random

    rng = random.Random(4)
    for kind in ("array", "jsonl", "keyed", "wrapped"):
        doc = make_doc(tmp_path / kind, kind, "pretty2", n=6, seed=2)
        docs.append(doc)
        ob = OverlayBuilder(doc, tmp_path / kind / "layer")
        for p in range(len(ob)):
            value = new_value(rng, 3 * p)          # \" \n U+2028, Uzbek Latin/Cyrillic, 0.1 1e400 ... 1.10
            ob.set(p, (editops.encode_key(f"k'{p} o‘zbek ўзбек") + b": " + value) if doc.member_records else value)
        doc.set_overlay(ob.state())
        out = tmp_path / f"{kind}.saved"
        report = saver.save(doc, out)
        text = check_saved(doc, ob, out, report)
        for token in (b"1e400", b"12345678901234567890", b"-0.0", b"1.10", "Ўзбек o‘zbek".encode()):
            assert token in text


# ----------------------------------------------------------------------------------
# (4) validation
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped", "stream", "single"])
def test_corrupted_unit_fails_validation_nothing_replaced(tmp_path, docs, backend, kind, lang_en) -> None:
    doc = make_doc(tmp_path / "s", kind, "compact", n=8)
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    bad = b'"k": {"x": tru}' if doc.member_records else b'{"x": tru}'
    rid = ob.rids[0]
    ob.top[rid] = ob.raw_unit(bad)              # not validated: a damaged unit
    doc.set_overlay(ob.state())
    src = doc.source_path
    before = src.read_bytes()
    for target in (None, tmp_path / "out.json"):
        with pytest.raises(ExportError) as ei:
            saver.save(doc, target)
        assert ei.value.key == "saver.error.invalid_output"
        assert "not valid JSON" in str(ei.value)
        assert ("line 1 is broken" if kind == "jsonl" else "byte") in str(ei.value)
        assert src.read_bytes() == before
        assert not (tmp_path / "out.json").exists()
        assert not src.with_name(src.name + ".bak").exists()
        assert temp_leftovers(src.parent) == [] and temp_leftovers(tmp_path) == []


def test_corrupted_read_and_wrong_count_fail_validation(tmp_path, docs, monkeypatch, lang_en) -> None:
    doc, ob = edited(tmp_path, docs, "array", "point")
    src = doc.source_path
    before = src.read_bytes()
    real_read = saver._Executor._read

    def flip(self, kind, a, b):
        data = real_read(self, kind, a, b)
        return data.replace(b",", b";", 1)

    monkeypatch.setattr(saver._Executor, "_read", flip)
    with pytest.raises(ExportError, match="not valid JSON"):
        saver.save(doc)
    monkeypatch.setattr(saver._Executor, "_read", real_read)
    assert src.read_bytes() == before and temp_leftovers(src.parent) == []

    real_init = saver._Validator.__init__

    def off_by_one(self, scanner_cls, mode, target, expected, cancel):
        real_init(self, scanner_cls, mode, target, expected + 1, cancel)

    monkeypatch.setattr(saver._Validator, "__init__", off_by_one)
    with pytest.raises(ExportError) as ei:
        saver.save(doc, tmp_path / "x.json")
    assert ei.value.key == "saver.error.invalid_output"
    assert f"{doc.record_count} records instead of {doc.record_count + 1}" in str(ei.value)
    assert not (tmp_path / "x.json").exists() and temp_leftovers(tmp_path) == []


def test_jsonl_broken_line_in_output_fails(tmp_path, docs, lang_en) -> None:
    """MODE_LINES reports a broken line through take_broken() only: it must still fail the check."""
    doc = make_doc(tmp_path / "s", "jsonl", n=5)
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.top[ob.rids[2]] = ob.raw_unit(b'{"a":1}\n{"b":')     # a unit spanning a broken second line
    doc.set_overlay(ob.state())
    with pytest.raises(ExportError, match="line 4 is broken"):
        saver.save(doc, tmp_path / "o.jsonl")
    assert not (tmp_path / "o.jsonl").exists()


# ----------------------------------------------------------------------------------
# preconditions, targets, report
# ----------------------------------------------------------------------------------
def test_partial_document_is_refused(tmp_path, docs, lang_en) -> None:
    p = tmp_path / "bad.json"
    p.write_bytes(b'[{"a": 1}, {"a": 2}, {"a": ]')
    doc = Document.open(p)
    docs.append(doc)
    doc.build_index()
    assert doc.error is not None
    with pytest.raises(UserInputError) as ei:
        saver.save(doc, tmp_path / "o.json")
    assert ei.value.key == "saver.error.partial" and "line 1" in str(ei.value)


def test_index_is_built_when_incomplete(tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_bytes(b'[1, 2, 3]')
    doc = Document.open(p, use_cache=False)
    try:
        assert not doc.index_complete
        report = saver.save(doc, tmp_path / "b.json")
        assert report.records == 3 and (tmp_path / "b.json").read_bytes() == b"[1, 2, 3]"
    finally:
        doc.close()


def test_targets(tmp_path, docs, lang_en) -> None:
    doc = make_doc(tmp_path / "s", "array", n=4)
    docs.append(doc)
    with pytest.raises(UserInputError) as ei:
        saver.save(doc, tmp_path / "missing" / "x.json")
    assert ei.value.key == "saver.error.no_dir"
    (tmp_path / "adir").mkdir()
    with pytest.raises(UserInputError) as ei:
        saver.save(doc, tmp_path / "adir")
    assert ei.value.key == "fileio.error.is_directory"
    # the same file under another spelling -> in place
    other = Path(str(doc.source_path).upper()) if os.name == "nt" else doc.source_path
    report = saver.save(doc, other)
    assert report.in_place and report.target == doc.source_path
    assert doc.source_path.with_name(doc.source_path.name + ".bak").exists()


def test_save_as_onto_a_hard_link_of_the_source_is_in_place(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "array", "point")
    link = tmp_path / "link.json"
    os.link(doc.source_path, link)
    report = saver.save(doc, link)
    assert report.in_place and report.target == doc.source_path


def test_backup_follows_settings(tmp_path, docs) -> None:
    settings = Settings(backup_on_save=False)
    doc, ob = edited(tmp_path, docs, "array", "point", settings=settings)
    report = saver.save(doc)
    assert report.backup is None
    assert not doc.source_path.with_name(doc.source_path.name + ".bak").exists()
    assert temp_leftovers(doc.source_path.parent) == []
    doc2, ob2 = edited(tmp_path / "b", docs, "array", "point", settings=settings)
    report = saver.save(doc2, backup=True)
    assert report.backup is not None and report.backup.exists()


def test_report_counts_and_progress(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "array", "mixed", n=40)
    seen = []
    prog = Progress(callback=seen.append, min_interval=0)
    report = saver.save(doc, tmp_path / "o.json", progress=prog)
    impure = sum(1 for r in ob.rids if r >= doc.base_record_count or ob.content(r) is not None)
    assert report.edited_records == impure
    assert 0 < report.copied_bytes < report.bytes_written
    assert seen and seen[-1].finished and seen[-1].done == seen[-1].total
    assert all(i.unit == "bytes" for i in seen)
    assert prog.message.startswith("o.json") or "o.json" in prog.message
    assert report.mb_per_s > 0 and report.elapsed > 0


def test_cancel_leaves_nothing(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "array", "every3", n=200)
    cancel = CancelToken()
    cancel.cancel()
    src = doc.source_path
    before = src.read_bytes()
    with pytest.raises(Cancelled):
        saver.save(doc, cancel=cancel)
    assert src.read_bytes() == before and temp_leftovers(src.parent) == []

    calls = [0]
    real = saver._Validator.feed

    def feed_then_cancel(self, chunk):
        calls[0] += 1
        cancel2.cancel()
        return real(self, chunk)

    cancel2 = CancelToken()
    import unittest.mock as mock

    with mock.patch.object(saver._Validator, "feed", feed_then_cancel):
        with pytest.raises(Cancelled):
            saver.save(doc, tmp_path / "o.json", cancel=cancel2)
    assert calls[0] >= 1 and not (tmp_path / "o.json").exists() and temp_leftovers(tmp_path) == []
    assert threading.active_count() < 50       # the validator thread ended


def test_pinned_state_is_written(tmp_path, docs) -> None:
    """save pins the document: a state set on the live document while saving is not mixed in."""
    doc, ob = edited(tmp_path, docs, "array", "point")
    first = doc.overlay
    want = oracle_text(doc, ob)
    ob2 = OverlayBuilder(doc, tmp_path / "layer2")
    ob2.delete(0, 1, 2)
    real = saver._Planner._body

    def swap_then_body(self):
        doc.set_overlay(ob2.state())        # the live document changes while the save runs
        yield from real(self)

    import unittest.mock as mock

    with mock.patch.object(saver._Planner, "_body", swap_then_body):
        saver.save(doc.pinned(first), tmp_path / "o.json")
    assert (tmp_path / "o.json").read_bytes() == want


@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl", "stream", "wrapped_obj"])
def test_runs_of_edited_records_are_assembled_at_once(tmp_path, docs, monkeypatch, kind) -> None:
    """Long runs of edited records of the file (bulk operations) take the RUN path: units read with one
    read, gaps with one read around them; small limits force the sub-runs, the read-size cut and the
    fallback for spread-out units."""
    import random

    rng = random.Random(5)
    doc = make_doc(tmp_path / "s", kind, "pretty2", n=300, seed=4)
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    member = doc.member_records
    value = lambda p: (editops.encode_key(f"b{p}") + b": " + new_value(rng, p)) if member else new_value(rng, p)  # noqa
    ob.bulk({p: value(p) for p in range(0, 120)})                  # one array layer, units in order
    for p in range(200, 140, -1):                                   # point edits in reverse unit order
        ob.set(p, value(1000 + p))
    ob.move([250, 251, 252], 10)
    doc.set_overlay(ob.state())
    calls = []
    real = saver._Executor._run_piece

    def spy(self, p):
        calls.append(len(p[1]))
        return real(self, p)

    monkeypatch.setattr(saver._Executor, "_run_piece", spy)
    for run_sub, read_bytes in ((saver.RUN_SUB, saver.READ_BYTES), (7, saver.READ_BYTES), (16, 2000)):
        monkeypatch.setattr(saver, "RUN_SUB", run_sub)
        monkeypatch.setattr(saver, "READ_BYTES", read_bytes)
        calls.clear()
        out = tmp_path / f"o{run_sub}.json"
        report = saver.save(doc, out)
        check_saved(doc, ob, out, report)
        assert calls and max(calls) >= saver.RUN_MIN


def test_stream_runs_with_touching_values_go_piece_by_piece(tmp_path, docs) -> None:
    text = b"".join(b'{"i":%d}' % i for i in range(40)) + b"\n"
    doc = make_doc(tmp_path / "s", "stream", text=text, open_kw={"format": "stream"})
    docs.append(doc)
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.bulk({p: b"%d" % p for p in range(5, 30)})                  # numbers would merge without a separator
    doc.set_overlay(ob.state())
    out = tmp_path / "o.json"
    report = saver.save(doc, out)
    check_saved(doc, ob, out, report)
    assert b"5\n6\n7" in out.read_bytes()


def test_huge_records_are_streamed(tmp_path, docs) -> None:
    """Records over ``large_node_bytes`` (here 300 bytes) are never read whole: a huge single document,
    huge records of an array, a duplicate sharing a huge record's file span."""
    settings = Settings(large_node_bytes=300)
    for kind in ("array", "single", "wrapped", "jsonl"):
        doc, ob = edited(tmp_path / kind, docs, kind, "none", n=12, settings=settings, style="pretty4")
        if kind != "single":
            ob.duplicate(1)
            ob.set(3, ob.records[0])
        doc.set_overlay(ob.state())
        assert any(doc.is_huge(i) for i in range(doc.record_count))
        out = tmp_path / f"{kind}.out"
        report = saver.save(doc, out)
        check_saved(doc, ob, out, report)


@pytest.mark.parametrize("record_path, text", [
    (("result", "items"),
     b'{\n  "meta": {"v": 1},\n  "result": {\n    "count": 3,\n    "items": [\n      {"a": 1},\n      {"a": 2},'
     b'\n      {"a": 3}\n    ],\n    "more": null\n  },\n  "tail": "x"\n}\n'),
    (("a", 1, "items"), b'{"a": [{"items": [0]}, {"items": [{"a": 1}, {"a": 2}, {"a": 3}], "z": 1}], "b": 2}'),
])
def test_nested_record_paths_with_an_edited_wrapper(tmp_path, docs, record_path, text) -> None:
    doc = make_doc(tmp_path / "s", "wrapped", text=text, open_kw={"format": "wrapped", "record_path": record_path})
    docs.append(doc)
    assert doc.record_count == 3 and tuple(doc.record_path) == record_path
    ob = OverlayBuilder(doc, tmp_path / "layer")
    if record_path[0] == "result":
        ob.set_wrapper(b'{"meta": {"v": 2, "ok": "ha"}, "result": {"count": 4, "items": [], "more": [1]}, '
                       b'"tail": "y"}')
    else:
        ob.set_wrapper(b'{"a": [{"items": [0, 1]}, {"items": [], "z": 2}], "b": 3, "c": "new"}')
    ob.insert(1, b'{"a": 1.5}')
    ob.set(3, b'{"a": "three"}')
    doc.set_overlay(ob.state())
    out = tmp_path / "o.json"
    report = saver.save(doc, out)
    assert report.reopen_options["record_path"] == record_path
    check_saved(doc, ob, out, report)


def test_empty_brace_list_gets_records(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "brace_list", text=b"{ }", open_kw={"format": "brace_list"})
    docs.append(doc)
    assert doc.record_count == 0
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.insert(0, b'{"a": 1}')
    ob.insert(1, b"[2]")
    doc.set_overlay(ob.state())
    out = tmp_path / "o.json"
    report = saver.save(doc, out)
    assert out.read_bytes() == b'[{"a": 1},[2]]' and report.kind == "array"
    check_saved(doc, ob, out, report)


def test_base_view_of_an_edited_document_saves_the_file(tmp_path, docs) -> None:
    doc, ob = edited(tmp_path, docs, "keyed", "structure")
    out = tmp_path / "o.json"
    report = saver.save(doc.pinned(None), out)
    assert out.read_bytes() == doc.source_path.read_bytes()
    assert report.edited_records == 0 and report.records == doc.base_record_count


def test_large_document_streams_in_chunks(tmp_path, docs) -> None:
    """A document of several MiB with scattered edits: pieces are batched and read coalesced."""
    doc, ob = edited(tmp_path, docs, "array", "none", n=20000, seed=9)
    import random

    rng = random.Random(3)
    ob.bulk_every(7, lambda p, old: new_value(rng, p))
    for p in range(0, len(ob), 997):
        ob.set(p, new_value(rng, p + 1))
    ob.move(list(range(100, 200)), 5000)
    doc.set_overlay(ob.state())
    out = tmp_path / "o.json"
    report = saver.save(doc, out)
    check_saved(doc, ob, out, report)
