"""filetools.split / plan_split (docs/ARCHITECTURE.md §14.0, §14.4; s5/map_convert.md §3, §8).

* every kind x mode (parts / size / records) x both core backends: parts reopen as the same kind,
  records byte-identical, JSON Lines / concatenated values laid end to end == the file (CRLF, blank
  lines, no final newline), names ``<stem>_<k><ext>`` zero-padded, no temporary file left;
* hypothesis: random documents, kinds, styles and part counts;
* encodings (BOM, UTF-16, gzip) kept; an oversized record; N > n, single, partial, broken lines,
  no records, bad names, the source as a part, read-only parts refused;
* edited (pinned) documents; ranges (engine A) and views (engine B, the document's own format or
  CSV with one shared header); cancellation and failed commits leave no temporary file and the old
  parts untouched; stale parts named; a part open in another Document replaced; progress.
"""

from __future__ import annotations

import gzip
import os
import random
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import jsongen
from jsonreshape import codec, editops, fileio, filetools, reader
from jsonreshape.errors import Cancelled, FileChangedError, JsonReshapeError, UserInputError
from jsonreshape.filetools import SplitOptions, plan_split, split
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions
from saver_helpers import decode_saved, edited, make_doc, oracle_text, source_traits

WINDOWS = sys.platform == "win32"
KINDS = ("array", "jsonl", "brace_list", "wrapped", "wrapped_obj", "keyed", "stream")


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


def raw_of(doc: Document) -> bytes:
    with doc.pinned(None).open_data() as f:
        return f.read()[:doc.data_size]


def reopen_part(path: Path, doc: Document, *, keep_wrapper: bool = True) -> Document:
    kind = "array" if doc.kind == "brace_list" else doc.kind
    record_path = doc.record_path
    if doc.kind == "wrapped" and not keep_wrapper:
        kind, record_path = ("keyed" if doc.member_records else "array"), None
    d = Document.open(path, format=kind, record_path=record_path)
    d.build_index()
    assert d.error is None, (path, d.error)
    return d


def part_records(rep: filetools.SplitReport, doc: Document, **kw: Any) -> list[bytes]:
    out: list[bytes] = []
    for part in rep.parts:
        d = reopen_part(part.path, doc, **kw)
        assert d.record_count == part.records
        out += [d.record_bytes(i) for i in range(d.record_count)]
        d.close()
    return out


def temps(folder: Path) -> list[str]:
    return sorted(n for n in os.listdir(folder) if n.endswith((filetools.PART_SUFFIX, ".tmp", fileio.ASIDE_SUFFIX)))


# ----------------------------------------------------------------------------------
# kinds x modes x backends
# ----------------------------------------------------------------------------------
MODES = [{"parts": 1}, {"parts": 3}, {"parts": 3, "balance": "records"}, {"records": 7}, {"size": 900},
         {"parts": 23}]


@pytest.mark.parametrize("mode", MODES, ids=lambda m: ",".join(f"{k}={v}" for k, v in m.items()))
@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_and_mode(tmp_path, docs, backend, kind, mode) -> None:
    doc = make_doc(tmp_path / "s", kind, "pretty2", n=23, seed=11)
    docs.append(doc)
    out = tmp_path / "out"
    out.mkdir()
    rep = split(doc, SplitOptions(out_dir=out, **mode))
    n = doc.record_count
    assert rep.engine == "same" and rep.records == n
    count = len(rep.parts)
    if "parts" in mode:
        assert count == mode["parts"]
    if "records" in mode:
        assert [p.records for p in rep.parts] == [7, 7, 7, 2]
    width = len(str(count))
    ext = ".json"
    assert [p.path.name for p in rep.parts] == [f"src_{str(k).zfill(width)}{ext}" for k in range(1, count + 1)]
    assert [p.first for p in rep.parts] == [sum(q.records for q in rep.parts[:k]) for k in range(count)]
    assert part_records(rep, doc) == [doc.record_bytes(i) for i in range(n)]
    assert rep.bytes_written == sum(p.path.stat().st_size for p in rep.parts) == sum(p.bytes for p in rep.parts)
    if kind in ("jsonl", "stream"):
        assert b"".join(p.path.read_bytes() for p in rep.parts) == raw_of(doc)[doc.info.data_start:]
    if "size" in mode:     # every part fits, unless it is one record bigger than the size
        for p in rep.parts:
            assert p.path.stat().st_size <= mode["size"] or p.records == 1
    assert temps(out) == []
    assert rep.warning_keys == (["filetools.warn.brace_list"] if kind == "brace_list" else [])


def test_plan_matches_the_split(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "array", "pretty2", n=40, seed=2)
    docs.append(doc)
    for opts in (SplitOptions(parts=4), SplitOptions(size=2000), SplitOptions(records=9),
                 SplitOptions(parts=5, balance="records")):
        plan = plan_split(doc, opts)
        assert plan[0][0] == 0 and plan[-1][1] == 40 and all(a < b for a, b in plan)
        assert all(plan[k][1] == plan[k + 1][0] for k in range(len(plan) - 1))
        out = tmp_path / f"o{len(plan)}{opts.records}{opts.size}"
        out.mkdir()
        rep = split(doc, dataclass_replace(opts, out_dir=out))
        assert [(p.first, p.first + p.records) for p in rep.parts] == plan


def dataclass_replace(opts: SplitOptions, **kw: Any) -> SplitOptions:
    import dataclasses

    return dataclasses.replace(opts, **kw)


def test_parts_balanced_by_bytes(tmp_path, docs) -> None:
    # records of very different sizes: a byte balance differs from a record balance
    recs = [{"i": i, "pad": "x" * (2000 if i < 5 else 10)} for i in range(60)]
    p = tmp_path / "b.json"
    p.write_bytes(codec.dumps(recs))
    doc = Document.open(p)
    doc.build_index()
    docs.append(doc)
    plan = plan_split(doc, SplitOptions(parts=4))
    sizes = [doc.record_span(b - 1)[1] - doc.record_span(a)[0] for a, b in plan]
    biggest = max(doc.record_span(i)[1] - doc.record_span(i)[0] for i in range(60))
    target = (doc.record_span(59)[1] - doc.record_span(0)[0]) / 4
    assert all(abs(s - target) <= biggest + 2 for s in sizes)
    assert plan != plan_split(doc, SplitOptions(parts=4, balance="records"))
    # the writer engine over the whole document / a range plans by the same byte offsets
    assert plan_split(doc, SplitOptions(parts=4, export=ExportOptions("jsonl"))) == plan
    sub = plan_split(doc, SplitOptions(parts=2, export=ExportOptions("csv")), source=range(10, 50))
    assert sub[0][0] == 0 and sub[-1][1] == 40
    assert plan_split(doc, SplitOptions(parts=60)) == [(i, i + 1) for i in range(60)]
    assert plan_split(doc, SplitOptions(parts=59))[-1][1] == 60 and len(plan_split(doc, SplitOptions(parts=59))) == 59


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture,
                                                                  HealthCheck.too_slow])
@given(seed=st.integers(0, 10_000), kind=st.sampled_from(["array", "jsonl", "keyed", "wrapped", "stream",
                                                          "brace_list"]),
       style=st.sampled_from(["compact", "pretty2", "tab", "messy"]), n=st.integers(1, 30),
       mode=st.sampled_from(["parts", "size", "records"]), data=st.data())
def test_random_documents(tmp_path_factory, seed, kind, style, n, mode, data) -> None:
    base = tmp_path_factory.mktemp("rnd")
    rng = random.Random(seed)
    recs = [jsongen.rand_value(rng, 0, 3, 4) for _ in range(n)]
    if kind in ("keyed", "wrapped", "brace_list"):
        recs = [r if isinstance(r, dict) else {"v": r} for r in recs]
    src = base / ("s.jsonl" if kind == "jsonl" else "s.json")
    jsongen.write_file(src, recs, kind, style="compact" if kind == "jsonl" else style, seed=seed)
    kw = {"format": kind}
    if kind == "wrapped":
        kw["record_path"] = "data"
    doc = Document.open(src, **kw)
    doc.build_index()
    try:
        assert doc.error is None and doc.record_count == n
        if mode == "parts":
            opts = SplitOptions(parts=data.draw(st.integers(1, n)), out_dir=base)
        elif mode == "records":
            opts = SplitOptions(records=data.draw(st.integers(1, n + 2)), out_dir=base)
        else:
            opts = SplitOptions(size=data.draw(st.integers(1, 3000)), out_dir=base)
        rep = split(doc, opts)
        if mode == "parts":
            assert len(rep.parts) == opts.parts
        assert part_records(rep, doc) == [doc.record_bytes(i) for i in range(n)]
        if kind in ("jsonl", "stream"):
            assert b"".join(p.path.read_bytes() for p in rep.parts) == src.read_bytes()
        assert temps(base) == []
    finally:
        doc.close()
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.parametrize("text", [
    b'{"a": 1}\r\n{"a": 2}\r\n\r\n{"a": 3}\r\n  \r\n{"a": 4}',          # CRLF, blank lines, no final newline
    b'\n\n{"a": 1}\n{"a": 2}\n{"a": 3}\n\n\n',                         # blank lines at both ends
    b'{"a":1}\n{"b":[1,2]}\n"s"\n12\n{"c":{}}\n',
])
def test_jsonl_parts_laid_end_to_end_are_the_file(tmp_path, docs, backend, text) -> None:
    src = tmp_path / "l.jsonl"
    src.write_bytes(text)
    doc = Document.open(src)
    doc.build_index()
    docs.append(doc)
    n = doc.record_count
    for parts in range(1, n + 1):
        out = tmp_path / f"o{parts}"
        out.mkdir()
        rep = split(doc, SplitOptions(parts=parts, out_dir=out))
        assert b"".join(p.path.read_bytes() for p in rep.parts) == text
        for p in rep.parts:
            d = Document.open(p.path, format="jsonl")
            d.build_index()
            assert d.error is None and d.broken_count == 0
            d.close()


# ----------------------------------------------------------------------------------
# encodings, names
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("encoding", ["utf-8-bom", "utf-16-le", "utf-16-le-bom", "utf-16-be-bom", "gzip",
                                      "gzip-bom"])
@pytest.mark.parametrize("kind", ["array", "jsonl", "wrapped"])
def test_encoding_and_compression_are_kept(tmp_path, docs, kind, encoding) -> None:
    doc = make_doc(tmp_path / "s", kind, "pretty2", n=10, seed=5, encoding=encoding)
    docs.append(doc)
    want_enc, want_bom, gz = source_traits(encoding)
    out = tmp_path / "out"
    out.mkdir()
    rep = split(doc, SplitOptions(parts=3, out_dir=out))
    for p in rep.parts:
        assert p.path.name.endswith(".json.gz" if gz else ".json")
        text, enc, bom, is_gz = decode_saved(p.path.read_bytes())
        assert (enc, bom, is_gz) == (want_enc, want_bom, gz), p.path.name
    assert part_records(rep, doc) == [doc.record_bytes(i) for i in range(doc.record_count)]


def test_names(tmp_path, docs) -> None:
    src = tmp_path / "data.jsonl.gz"
    src.write_bytes(gzip.compress(b"".join(b'{"i": %d}\n' % i for i in range(12))))
    doc = Document.open(src)
    doc.build_index()
    docs.append(doc)
    rep = split(doc, SplitOptions(parts=10))
    assert [p.path.name for p in rep.parts] == [f"data_{k:02d}.jsonl.gz" for k in range(1, 11)]
    assert all(p.path.parent == tmp_path for p in rep.parts)
    (tmp_path / "o").mkdir()
    rep = split(doc, SplitOptions(parts=3, name="{stem}-qism{n}{ext}", out_dir=tmp_path / "o"))
    assert [p.path.name for p in rep.parts] == ["data-qism1.jsonl.gz", "data-qism2.jsonl.gz", "data-qism3.jsonl.gz"]
    rep = split(doc, SplitOptions(parts=2, name="part{n}.ndjson.gz", out_dir=tmp_path / "o"))
    assert [p.path.name for p in rep.parts] == ["part1.ndjson.gz", "part2.ndjson.gz"]
    assert all(p.path.read_bytes()[:2] == b"\x1f\x8b" for p in rep.parts)
    rep = split(doc, SplitOptions(parts=2, name="plain{n}.jsonl", out_dir=tmp_path / "o"))
    assert all(p.path.read_bytes()[:2] != b"\x1f\x8b" for p in rep.parts)     # compression follows the name
    # a gzip input named without .gz: its parts get .gz
    bare = tmp_path / "z.json"
    bare.write_bytes(gzip.compress(b'[{"a": 1}, {"a": 2}]'))
    zdoc = Document.open(bare)
    zdoc.build_index()
    docs.append(zdoc)
    rep = split(zdoc, SplitOptions(parts=2, out_dir=tmp_path / "o"))
    assert [p.path.name for p in rep.parts] == ["z_1.json.gz", "z_2.json.gz"]
    # brace lists become arrays named .json
    brace = tmp_path / "b.txt"
    brace.write_bytes(b'{ {"a": 1}, {"a": 2} }')
    bdoc = Document.open(brace, format="brace_list")
    bdoc.build_index()
    docs.append(bdoc)
    rep = split(bdoc, SplitOptions(parts=2, out_dir=tmp_path / "o"))
    assert [p.path.name for p in rep.parts] == ["b_1.json", "b_2.json"]
    assert [p.path.read_bytes() for p in rep.parts] == [b'[ {"a": 1} ]', b'[ {"a": 2} ]']   # head + tail each


@pytest.mark.parametrize("pattern", ["{stem}{ext}", "{stem}_{x}{ext}", "{n:03}", "{n!r}", "a/{n}", "a\\{n}", "",
                                     "{stem", "{0}{n}", "a:{n}.json", "p{n}?.json", "x<{n}>", "t\t{n}"])
def test_bad_name_patterns(pattern, lang_en) -> None:
    with pytest.raises(UserInputError) as ei:
        SplitOptions(parts=2, name=pattern)
    assert ei.value.key == "filetools.error.name_pattern" and ei.value.hint


# ----------------------------------------------------------------------------------
# refusals
# ----------------------------------------------------------------------------------
def test_options_refused(lang_en) -> None:
    for kw in ({}, {"parts": 2, "size": 10}, {"parts": 2, "records": 1, "size": 4}):
        with pytest.raises(UserInputError) as ei:
            SplitOptions(**kw)
        assert ei.value.key == "filetools.error.split_mode" and ei.value.hint
    for kw in ({"parts": 0}, {"size": -1}, {"records": True}, {"parts": 2.5}, {"parts": 2, "balance": "rows"},
               {"parts": 2, "keep_wrapper": "yes"}):
        with pytest.raises(UserInputError) as ei:
            SplitOptions(**kw)
        assert ei.value.key == "filetools.error.option"


def test_document_refusals(tmp_path, docs, lang_en) -> None:
    doc = make_doc(tmp_path / "s", "array", n=5)
    docs.append(doc)
    with pytest.raises(UserInputError) as ei:
        split(doc, SplitOptions(parts=6))
    assert ei.value.key == "filetools.error.too_many_parts" and ei.value.params["count"] == 5
    single = make_doc(tmp_path / "1", "single", n=8)
    docs.append(single)
    with pytest.raises(UserInputError) as ei:
        split(single, SplitOptions(parts=2))
    assert ei.value.key == "filetools.error.single" and "--record-path" in ei.value.hint
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"a": 1}, {"a": 2}, {"a"')
    partial = Document.open(bad)
    partial.build_index()
    docs.append(partial)
    with pytest.raises(UserInputError) as ei:
        split(partial, SplitOptions(parts=2))
    assert ei.value.key == "filetools.error.partial" and "repair" in ei.value.hint
    empty = tmp_path / "e.json"
    empty.write_bytes(b"[]")
    edoc = Document.open(empty)
    edoc.build_index()
    docs.append(edoc)
    with pytest.raises(UserInputError) as ei:
        split(edoc, SplitOptions(records=5))
    assert ei.value.key == "filetools.error.no_records"
    with pytest.raises(UserInputError) as ei:
        split(doc, SplitOptions(parts=2, out_dir=tmp_path / "missing"))
    assert ei.value.key == "filetools.error.no_dir"
    assert not list((tmp_path / "s").glob("src_*"))


def test_too_many_files(tmp_path, docs, monkeypatch, lang_en) -> None:
    doc = make_doc(tmp_path / "s", "jsonl", n=30)
    docs.append(doc)
    monkeypatch.setattr(filetools, "MAX_PARTS", 10)
    for opts in (SplitOptions(records=2), SplitOptions(size=1), SplitOptions(parts=11)):
        with pytest.raises(UserInputError) as ei:
            split(doc, opts)
        assert ei.value.key == "filetools.error.too_many_files"


def test_broken_lines_refused_then_dropped(tmp_path, docs, lang_en) -> None:
    text = b'{"a": 1}\nbroken\n{"a": 2}\n{"a": 3}\noops {\n{"a": 4}\n'
    src = tmp_path / "b.jsonl"
    src.write_bytes(text)
    doc = Document.open(src)
    doc.build_index()
    docs.append(doc)
    assert doc.broken_count == 2
    with pytest.raises(UserInputError) as ei:
        split(doc, SplitOptions(parts=2))
    assert ei.value.key == "filetools.error.broken_lines" and "--drop-broken" in ei.value.hint
    rep = split(doc, SplitOptions(parts=2, drop_broken=True))
    assert b"".join(p.path.read_bytes() for p in rep.parts) == oracle_text(doc, None, drop_broken=True)
    assert "filetools.warn.broken_dropped" in rep.warning_keys
    # engine B refuses them too
    with pytest.raises(UserInputError):
        split(doc, SplitOptions(parts=2, export=ExportOptions("csv")))


def test_part_targets_refused(tmp_path, docs, lang_en) -> None:
    folder = tmp_path / "s"
    folder.mkdir()
    src = folder / "a_1.json"
    src.write_bytes(b'[{"a": 1}, {"a": 2}]')
    doc = Document.open(src)
    doc.build_index()
    docs.append(doc)
    with pytest.raises(UserInputError) as ei:
        split(doc, SplitOptions(parts=2, name="a_{n}.json"))        # part 1 would be the input itself
    assert ei.value.key == "writer.error.same_file"
    (folder / "x_2.json").mkdir()
    with pytest.raises(UserInputError) as ei:
        split(doc, SplitOptions(parts=2, name="x_{n}.json"))
    assert ei.value.key == "fileio.error.is_directory"
    ro = folder / "r_2.json"
    ro.write_bytes(b"[]")
    os.chmod(ro, 0o444)
    try:
        if fileio.is_read_only(ro):
            with pytest.raises(UserInputError) as ei:
                split(doc, SplitOptions(parts=2, name="r_{n}.json"))
            assert ei.value.key == "fileio.error.read_only" and ro.read_bytes() == b"[]"
            assert not (folder / "r_1.json").exists()
    finally:
        os.chmod(ro, 0o666)
    assert temps(folder) == []


def test_not_enough_disk_space(tmp_path, docs, monkeypatch, lang_en) -> None:
    doc = make_doc(tmp_path / "s", "array", n=10)
    docs.append(doc)
    monkeypatch.setattr(fileio, "free_space", lambda p: 1000)
    with pytest.raises(UserInputError) as ei:
        split(doc, SplitOptions(parts=2))
    assert ei.value.key == "filetools.error.disk_space" and ei.value.hint
    assert not list((tmp_path / "s").glob("src_*"))


# ----------------------------------------------------------------------------------
# oversized records, edited documents, ranges and views
# ----------------------------------------------------------------------------------
def test_oversized_record(tmp_path, docs, lang_en) -> None:
    recs = [{"i": 0}, {"i": 1, "big": "x" * 500}, {"i": 2}, {"i": 3}, {"i": 4, "big": "y" * 600}, {"i": 5}]
    src = tmp_path / "o.jsonl"
    src.write_bytes(b"".join(codec.dumps(r) + b"\n" for r in recs))
    doc = Document.open(src)
    doc.build_index()
    docs.append(doc)
    rep = split(doc, SplitOptions(size=100))
    assert [p.records for p in rep.parts] == [1, 1, 2, 1, 1]
    assert rep.warning_keys == ["filetools.warn.oversized"]
    assert rep.warnings[0] == ("2 records are larger than 100 B by themselves: each was written to a part of "
                               "its own (the first: record #1).")
    assert b"".join(p.path.read_bytes() for p in rep.parts) == src.read_bytes()


@pytest.mark.parametrize("pattern", ["point", "structure", "mixed", "wrapper", "delete", "insert_end"])
@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped", "stream"])
def test_edited_document(tmp_path, docs, kind, pattern) -> None:
    doc, ob = edited(tmp_path, docs, kind, pattern, style="pretty2" if kind != "jsonl" else "compact", n=20)
    assert ob is not None
    out = tmp_path / "out"
    out.mkdir()
    for opts in (SplitOptions(parts=3, out_dir=out), SplitOptions(size=1500, out_dir=out),
                 SplitOptions(parts=2, balance="records", out_dir=out)):
        rep = split(doc, opts)
        assert rep.engine == "same"
        pinned = doc.pinned()
        assert part_records(rep, pinned) == ob.records
        if kind == "jsonl":
            assert b"".join(p.path.read_bytes() for p in rep.parts) == oracle_text(pinned, ob)
        assert [(p.first, p.first + p.records) for p in rep.parts] == plan_split(doc, opts)


def test_range_selection_uses_the_byte_copy(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "jsonl", n=30, seed=3)
    docs.append(doc)
    out = tmp_path / "out"
    out.mkdir()
    rep = split(doc, SplitOptions(parts=3, balance="records", out_dir=out), source=range(10, 25))
    assert rep.engine == "same" and [p.first for p in rep.parts] == [10, 15, 20]
    spans = [doc.record_span(i) for i in range(30)]
    raw = raw_of(doc)
    assert b"".join(p.path.read_bytes() for p in rep.parts) == raw[spans[10][0]:spans[25][0]]


def test_view_parts_through_the_writer(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "array", "pretty2", n=30, seed=4)
    docs.append(doc)
    view = RecordSet.from_indices([29, 3, 17, 3, 0, 8, 12])
    out = tmp_path / "out"
    out.mkdir()
    rep = split(doc, SplitOptions(parts=3, out_dir=out), source=view)
    assert rep.engine == "export" and rep.fmt == "json"
    got = []
    for p in rep.parts:
        got += codec.loads(p.path.read_bytes())
        assert p.path.read_bytes().startswith(b"[\n  {")              # the document's indentation
    assert [codec.dumps(v) for v in got] == [codec.dumps(doc.record(i)) for i in view]
    lines = make_doc(tmp_path / "l", "jsonl", n=10, seed=4)
    docs.append(lines)
    rep = split(lines, SplitOptions(records=4, out_dir=out), source=[9, 8, 7, 6, 5])
    assert [p.path.name for p in rep.parts] == ["src_1.jsonl", "src_2.jsonl"]
    assert b"".join(p.path.read_bytes() for p in rep.parts) == b"".join(
        codec.dumps(lines.record(i)) + b"\n" for i in [9, 8, 7, 6, 5])


def test_part_warnings_add_up(tmp_path, docs, lang_en) -> None:
    # every 3rd record is huge: its keys cannot be sorted; the parts' counts are added up (2 + 1 + 1)
    recs = [{"b": i, "a": "x" * (300 if i % 3 == 0 else 1)} for i in range(12)]
    src = tmp_path / "h.json"
    src.write_bytes(codec.dumps(recs))
    doc = Document.open(src, settings=Settings(large_node_bytes=200))
    doc.build_index()
    docs.append(doc)
    assert [i for i in range(12) if doc.is_huge(i)] == [0, 3, 6, 9]
    rep = split(doc, SplitOptions(parts=3, balance="records", export=ExportOptions("jsonl", sort_keys=True)))
    assert [p.records for p in rep.parts] == [4, 4, 4]
    assert rep.warning_keys == ["writer.warn.huge_unsorted"]
    assert rep.warnings == ["The keys of 4 very large records were written unsorted."]
    # a warning about the document itself (every part gives it alike) is reported once
    wrapped = make_doc(tmp_path / "w", "wrapped", "pretty2", n=9, seed=2)
    docs.append(wrapped)
    rep = split(wrapped, SplitOptions(parts=3, export=ExportOptions("jsonl")))
    assert rep.warning_keys == ["writer.note.wrapper_not_written"] and len(rep.warnings) == 1
    rep = split(wrapped, SplitOptions(parts=3, export=ExportOptions("jsonl", keep_wrapper=False)))
    assert rep.warnings == []
    # explicit columns: only those that NO part found are named ("a" is in part 1 only, "b" in part 2)
    cols = tmp_path / "c.json"
    cols.write_bytes(codec.dumps([{"id": i, ("a" if i < 5 else "b"): i} for i in range(10)]))
    cdoc = Document.open(cols)
    cdoc.build_index()
    docs.append(cdoc)
    out = tmp_path / "cols"
    out.mkdir()
    rep = split(cdoc, SplitOptions(parts=2, balance="records", out_dir=out,
                                   export=ExportOptions("csv", columns=["id", "a", "zz", "b", "y.q"])))
    assert rep.warning_keys == ["writer.warn.columns_not_found"]
    assert rep.warnings == ["No exported record has a value for these columns (check the spelling of the paths): "
                            "zz, y.q."]
    rep = split(cdoc, SplitOptions(parts=2, balance="records", out_dir=out,
                                   export=ExportOptions("csv", columns=["id", "a", "b"])))
    assert rep.warnings == []


def test_csv_parts_share_one_header(tmp_path, docs) -> None:
    recs = [{"id": i, ("a" if i < 10 else "b"): i} for i in range(20)]
    src = tmp_path / "c.json"
    src.write_bytes(codec.dumps(recs))
    doc = Document.open(src)
    doc.build_index()
    docs.append(doc)
    rep = split(doc, SplitOptions(parts=2, export=ExportOptions("csv", csv_bom=False)))
    assert rep.engine == "export" and [p.path.name for p in rep.parts] == ["c_1.csv", "c_2.csv"]
    heads = [p.path.read_bytes().split(b"\r\n")[0] for p in rep.parts]
    assert heads == [b"id,a,b", b"id,a,b"] and rep.columns == ["id", "a", "b"]
    assert rep.rows == 20 and rep.warnings == []
    sql = split(doc, SplitOptions(records=15, export=ExportOptions("sqlite")))
    assert [p.records for p in sql.parts] == [15, 5] and sql.tables == ["records"]
    xl = split(doc, SplitOptions(parts=2, export=ExportOptions("xlsx", xlsx_split="files", xlsx_max_rows=4)))
    assert [p.path.name for p in xl.parts] == ["c_1.xlsx", "c_2.xlsx"]      # sheets inside a part
    report = xl.to_export_report()
    assert report.files == [p.path for p in xl.parts] and report.records == 20 and report.fmt == "xlsx"


# ----------------------------------------------------------------------------------
# cancellation, failed commits, stale parts, open targets, progress
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("engine", ["same", "export"])
def test_cancellation_leaves_no_temps_and_old_parts(tmp_path, docs, engine) -> None:
    doc = make_doc(tmp_path / "s", "array", "pretty2", n=200, seed=6)
    docs.append(doc)
    folder = tmp_path / "s"
    export = ExportOptions("json", indent=4) if engine == "export" else None
    split(doc, SplitOptions(parts=4, export=export))
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.name.startswith("src_")}
    assert len(before) == 4
    token = CancelToken()
    calls = []

    def cancel_in_part_two(info: Any) -> None:
        calls.append(info.message)
        if "2/4" in info.message:
            token.cancel()

    with pytest.raises(Cancelled):
        split(doc, SplitOptions(parts=4, export=ExportOptions("json", indent=2)),
              progress=Progress(callback=cancel_in_part_two, min_interval=0), cancel=token)
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.name.startswith("src_")} == before
    assert temps(folder) == []


def test_failed_commit_removes_the_temps(tmp_path, docs, monkeypatch) -> None:
    doc = make_doc(tmp_path / "s", "jsonl", n=20)
    docs.append(doc)

    def broken_commit(staged, finals, **kw):  # noqa: ANN001
        raise JsonReshapeError("fileio.error.replace", path=str(finals[0]))

    monkeypatch.setattr(fileio, "commit_files", broken_commit)
    with pytest.raises(JsonReshapeError):
        split(doc, SplitOptions(parts=3))
    assert temps(tmp_path / "s") == [] and not list((tmp_path / "s").glob("src_*"))


def test_source_changed_before_the_commit(tmp_path, docs, monkeypatch) -> None:
    doc = make_doc(tmp_path / "s", "jsonl", n=20)
    docs.append(doc)
    real = filetools._commit_check

    def change_then_check(d, warnings):  # noqa: ANN001
        with open(d.source_path, "r+b") as f:
            f.write(b"[")
        real(d, warnings)

    monkeypatch.setattr(filetools, "_commit_check", change_then_check)
    with pytest.raises(FileChangedError):
        split(doc, SplitOptions(parts=2))
    assert temps(tmp_path / "s") == [] and not list((tmp_path / "s").glob("src_*"))


def test_stale_parts_are_named_and_left_alone(tmp_path, docs, lang_en) -> None:
    doc = make_doc(tmp_path / "s", "jsonl", n=30)
    docs.append(doc)
    split(doc, SplitOptions(parts=12))
    (tmp_path / "s" / "unrelated.jsonl").write_bytes(b"{}\n")
    old = {p.name: p.read_bytes() for p in (tmp_path / "s").glob("src_*")}
    rep = split(doc, SplitOptions(parts=3))
    assert [p.path.name for p in rep.parts] == ["src_1.json", "src_2.json", "src_3.json"]
    assert [p.name for p in rep.stale] == [f"src_{k:02d}.json" for k in range(1, 13)]
    assert rep.warning_keys == ["filetools.warn.stale_parts"]
    assert "(12): src_01.json, src_02.json" in rep.warnings[0]
    assert all((tmp_path / "s" / n).read_bytes() == b for n, b in old.items())
    rep = split(doc, SplitOptions(parts=2))
    assert "src_3.json" in [p.name for p in rep.stale] and "src_1.json" not in [p.name for p in rep.stale]


@pytest.mark.skipif(not WINDOWS, reason="open files block replacing only on Windows")
def test_a_part_open_in_another_document_is_replaced(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "array", n=20, seed=1)
    docs.append(doc)
    rep = split(doc, SplitOptions(parts=2))
    shown = Document.open(rep.parts[1].path)          # another tab shows part 2
    shown.build_index()
    docs.append(shown)
    first = shown.record_bytes(0)
    rep2 = split(doc, SplitOptions(parts=2, balance="records", export=ExportOptions("json", indent=None)))
    assert rep2.parts[1].path == rep.parts[1].path
    assert codec.loads(rep2.parts[1].path.read_bytes())[0] == codec.loads(doc.record_bytes(10))
    assert shown.check_changed()                      # the tab sees the change (and reads its old snapshot)
    assert shown.pinned().record_bytes(0) == first


def test_progress_and_messages(tmp_path, docs, lang_en) -> None:
    doc = make_doc(tmp_path / "s", "array", "pretty2", n=100, seed=8)
    docs.append(doc)
    infos = []
    progress = Progress(callback=infos.append, min_interval=0)
    split(doc, SplitOptions(parts=3), progress=progress)
    assert progress.finished and progress.done == progress.total
    done = [i.done for i in infos if i.total]
    assert done == sorted(done) and all(i.done <= i.total for i in infos if i.total)
    assert {m for m in (i.message for i in infos) if m.startswith("Part")} >= {
        "Part 1/3 · src_1.json", "Part 2/3 · src_2.json", "Part 3/3 · src_3.json"}


def test_keep_wrapper_false(tmp_path, docs) -> None:
    doc = make_doc(tmp_path / "s", "wrapped", "pretty2", n=9, seed=2)
    docs.append(doc)
    rep = split(doc, SplitOptions(parts=3, keep_wrapper=False))
    for p in rep.parts:
        assert p.path.read_bytes().lstrip().startswith(b"[")
    assert part_records(rep, doc, keep_wrapper=False) == [doc.record_bytes(i) for i in range(9)]
    rep = split(doc, SplitOptions(parts=3))
    for p in rep.parts:
        assert codec.loads(p.path.read_bytes())["meta"] == doc.root().value()["meta"]   # the wrapper in every part
