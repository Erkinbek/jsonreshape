"""writer.export_many: several documents into one JSON / JSON Lines file (the merge of
docs/ARCHITECTURE.md §14.4, s5/map_convert.md §4.1-§4.2).

* bytes equal to the export of ONE document holding all the records (array root; JSON Lines);
* object root only when every input has member records; member records in an array root become
  ``{"key": value}`` objects (warning); repeated member keys counted (output position named);
* wrapper rules: the first input's wrapper kept when its container is of the root's kind, any
  other dropped (warning); JSON Lines: the wrapper notice;
* per-input selections, sort_keys, huge records streamed, exact numbers and no ``\\u`` escaping,
  gzip outputs, refusals, cancellation (no output, no temporary file), progress, both core backends.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, writer
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export, export_many

NUMBERS = ('[{"n": 0.1}, {"n": 1e400}, {"n": 12345678901234567890}, {"n": -0.0}, {"n": 1.10}, '
           '{"s": "Ўзбекистон Oʻzbekiston \\u0041"}]').encode("utf-8")


@pytest.fixture
def backend(core_backend, monkeypatch):
    monkeypatch.setattr(writer, "core", core_backend)
    return core_backend


@pytest.fixture
def open_doc(tmp_path: Path):
    docs: list[Document] = []
    src = tmp_path / "src"
    src.mkdir()

    def make(data: bytes | None = None, *, recs: list | None = None, fmt: str = "array", name: str | None = None,
             settings: Settings | None = None, **kw: Any) -> Document:
        p = src / (name or f"in{len(docs)}.{'jsonl' if fmt == 'jsonl' else 'json'}")
        if data is None:
            jsongen.write_file(p, recs, fmt, **kw)
        else:
            p.write_bytes(data)
        open_kw: dict[str, Any] = {}
        if fmt == "wrapped":
            open_kw = {"format": "wrapped", "record_path": kw.get("wrapper_path", ("data",))[0]}
        elif fmt in ("keyed", "stream", "brace_list"):
            open_kw = {"format": fmt}
        doc = Document.open(p, settings=settings, **open_kw)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "out"
    d.mkdir()
    return d


def parsed(path: Path) -> Any:
    return codec.loads(path.read_bytes())


# ----------------------------------------------------------------------------------
# bytes = the export of one document of all the records
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kinds", [("array", "array"), ("jsonl", "array", "stream"), ("brace_list", "jsonl"),
                                   ("array", "wrapped")])
def test_value_inputs_equal_one_document(open_doc, out_dir, backend, kinds) -> None:
    recs = jsongen.records(30, seed=4)
    chunks = [recs[0:9], recs[9:21], recs[21:30]][:len(kinds)]
    if len(kinds) == 2:
        chunks = [recs[:13], recs[13:]]
    docs = [open_doc(recs=c, fmt=k, style="pretty2" if k != "jsonl" else "compact") for c, k in zip(chunks, kinds)]
    whole = open_doc(recs=[r for c in chunks for r in c], fmt="array")
    for opts in (ExportOptions("json", indent=2), ExportOptions("json", indent=None), ExportOptions("json", indent="\t"),
                 ExportOptions("jsonl"), ExportOptions("json", indent=4, sort_keys=True),
                 ExportOptions("jsonl", sort_keys="codepoint"), ExportOptions("json", keep_wrapper=False)):
        rep = export_many([(d, None) for d in docs], out_dir / "m.out", opts)
        export(whole, out_dir / "w.out", opts)
        assert (out_dir / "m.out").read_bytes() == (out_dir / "w.out").read_bytes(), opts
        assert rep.records == rep.rows == len(recs) and rep.per_input == [len(c) for c in chunks]
        assert rep.files == [out_dir / "m.out"] and rep.bytes_written == (out_dir / "m.out").stat().st_size


def test_selections_per_input(open_doc, out_dir) -> None:
    a = open_doc(recs=[{"a": i} for i in range(10)])
    b = open_doc(recs=[{"b": i} for i in range(10)], fmt="jsonl")
    rep = export_many([(a, range(2, 5)), (b, [9, 0, 9])], out_dir / "s.json", ExportOptions("json", indent=None))
    assert parsed(out_dir / "s.json") == [{"a": 2}, {"a": 3}, {"a": 4}, {"b": 9}, {"b": 0}, {"b": 9}]
    assert rep.per_input == [3, 3]
    rep = export_many([(a, []), (b, range(1))], out_dir / "e.jsonl", ExportOptions("jsonl"))
    assert (out_dir / "e.jsonl").read_bytes() == b'{"b":0}\n' and rep.per_input == [0, 1]
    rep = export_many([(a, []), (b, [])], out_dir / "z.json", ExportOptions("json"))
    assert parsed(out_dir / "z.json") == [] and rep.records == 0


def test_exact_numbers_and_no_escaping(open_doc, out_dir, backend) -> None:
    a = open_doc(NUMBERS)
    b = open_doc(NUMBERS, name="b.json")
    export_many([(a, None), (b, None)], out_dir / "n.json", ExportOptions("json", indent=None))
    data = (out_dir / "n.json").read_bytes()
    body = NUMBERS[1:-1].replace(b", ", b",").replace(b": ", b":")
    assert data == b"[" + body + b"," + body + b"]"
    assert b"\\u" not in data.replace(b"\\u0041", b"")       # nothing escaped by the merge itself


# ----------------------------------------------------------------------------------
# roots, member records, wrappers
# ----------------------------------------------------------------------------------
def test_member_inputs_make_an_object_root(open_doc, out_dir, lang_en) -> None:
    k1 = open_doc(recs=[{"v": 1}, {"v": 2}], fmt="keyed", keys=["a", "b"])
    k2 = open_doc(b'{"meta": 1, "data": {"c": {"v": 3}, "a": {"v": 4}}, "tail": 2}', name="wo.json")
    k2.close()
    k2 = Document.open(k2.source_path, format="wrapped", record_path="data")
    k2.build_index()
    try:
        assert k1.member_records and k2.member_records
        rep = export_many([(k1, None), (k2, None)], out_dir / "o.json", ExportOptions("json", indent=None))
        assert (out_dir / "o.json").read_bytes() == b'{"a":{"v":1},"b":{"v":2},"c":{"v":3},"a":{"v":4}}'
        # the key "a" repeats: written as it is, counted with the output position; the wrapper of the
        # second (wrapped) input has no place in the result
        assert rep.warning_keys == ["writer.warn.wrappers_dropped", "writer.warn.merge_duplicate_keys"]
        assert "wo.json" in rep.warnings[0]
        assert rep.warnings[1].startswith('1 record repeats the key of an earlier record (#3 of the result, key "a")')
        # the wrapped-object input first: its wrapper is kept around the object of all members
        rep = export_many([(k2, None), (k1, None)], out_dir / "w.json", ExportOptions("json", indent=None))
        assert (out_dir / "w.json").read_bytes() == (b'{"meta":1,"data":{"c":{"v":3},"a":{"v":4},"a":{"v":1},'
                                                     b'"b":{"v":2}},"tail":2}')
        assert rep.warning_keys == ["writer.warn.merge_duplicate_keys"]
        # JSON Lines: {"key":value} lines, no warning about members
        rep = export_many([(k1, None), (k2, None)], out_dir / "o.jsonl", ExportOptions("jsonl"))
        assert (out_dir / "o.jsonl").read_bytes() == b'{"a":{"v":1}}\n{"b":{"v":2}}\n{"c":{"v":3}}\n{"a":{"v":4}}\n'
        assert rep.warning_keys == ["writer.note.wrapper_not_written"]
    finally:
        k2.close()


def test_mixed_inputs_make_an_array_root(open_doc, out_dir, lang_en) -> None:
    arr = open_doc(recs=[{"x": 1}], fmt="array")
    keyed = open_doc(recs=[{"v": 1}, {"v": 2}], fmt="keyed", keys=["a", "b"])
    for opts in (ExportOptions("json", indent=None), ExportOptions("json", indent=None, sort_keys=True)):
        rep = export_many([(arr, None), (keyed, None)], out_dir / "m.json", opts)
        assert (out_dir / "m.json").read_bytes() == b'[{"x":1},{"a":{"v":1}},{"b":{"v":2}}]'
        assert rep.warning_keys == ["writer.warn.members_as_objects"]
        assert rep.warnings[0].startswith("The records of 1 input were")


def test_wrapper_rules(open_doc, out_dir, lang_en) -> None:
    w1 = open_doc(recs=[{"r": 1}], fmt="wrapped")
    w2 = open_doc(recs=[{"r": 2}], fmt="wrapped", name="w2.json")
    arr = open_doc(recs=[{"r": 3}])
    rep = export_many([(w1, None), (arr, None), (w2, None)], out_dir / "w.json", ExportOptions("json", indent=None))
    got = parsed(out_dir / "w.json")
    assert got["data"] == [{"r": 1}, {"r": 3}, {"r": 2}] and got["meta"]["source"] == "test"   # w1's wrapper
    assert rep.warning_keys == ["writer.warn.wrappers_dropped"] and "w2.json" in rep.warnings[0]
    # keep_wrapper=False: the records alone, no warning
    rep = export_many([(w1, None), (w2, None)], out_dir / "n.json", ExportOptions("json", indent=None,
                                                                                   keep_wrapper=False))
    assert parsed(out_dir / "n.json") == [{"r": 1}, {"r": 2}] and rep.warnings == []
    # a wrapped-object first input in an array root: its wrapper is dropped as well
    wo = open_doc(b'{"m": 1, "data": {"k": {"r": 0}}}', name="wo.json")
    wo.close()
    wo = Document.open(wo.source_path, format="wrapped", record_path="data")
    wo.build_index()
    try:
        rep = export_many([(wo, None), (arr, None)], out_dir / "x.json", ExportOptions("json", indent=None))
        assert parsed(out_dir / "x.json") == [{"k": {"r": 0}}, {"r": 3}]
        assert rep.warning_keys == ["writer.warn.members_as_objects", "writer.warn.wrappers_dropped"]
    finally:
        wo.close()


# ----------------------------------------------------------------------------------
# huge records, gzip, progress, refusals, cancellation
# ----------------------------------------------------------------------------------
def test_huge_records_are_streamed(open_doc, out_dir, backend) -> None:
    recs = jsongen.records(20, seed=8)
    small = [open_doc(recs=recs[:10], fmt="keyed", style="pretty2", keys=[f"k{i}" for i in range(10)]),
             open_doc(recs=recs[10:], fmt="keyed", style="pretty2", keys=[f"k{i}" for i in range(10, 20)])]
    tiny = Settings(large_node_bytes=120)
    huge = [open_doc(recs=recs[:10], fmt="keyed", style="pretty2", keys=[f"k{i}" for i in range(10)], settings=tiny,
                     name="h1.json"),
            open_doc(recs=recs[10:], fmt="keyed", style="pretty2", keys=[f"k{i}" for i in range(10, 20)],
                     settings=tiny, name="h2.json")]
    assert any(huge[0].is_huge(i) for i in range(10))
    for opts in (ExportOptions("json", indent=2), ExportOptions("jsonl")):
        export_many([(d, None) for d in small], out_dir / "a.out", opts)
        export_many([(d, None) for d in huge], out_dir / "b.out", opts)
        assert (out_dir / "a.out").read_bytes() == (out_dir / "b.out").read_bytes()


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_gzip_output(open_doc, out_dir, fmt) -> None:
    a = open_doc(recs=jsongen.records(15, seed=2))
    b = open_doc(recs=jsongen.records(5, seed=3), fmt="jsonl")
    export_many([(a, None), (b, None)], out_dir / f"m.{fmt}", ExportOptions(fmt))
    export_many([(a, None), (b, None)], out_dir / f"m.{fmt}.gz", ExportOptions(fmt))
    raw = (out_dir / f"m.{fmt}.gz").read_bytes()
    assert raw[:2] == b"\x1f\x8b" and gzip.decompress(raw) == (out_dir / f"m.{fmt}").read_bytes()
    back = Document.open(out_dir / f"m.{fmt}.gz")
    back.build_index()
    assert back.record_count == 20
    back.close()


def test_progress_covers_all_inputs(open_doc, out_dir) -> None:
    docs = [open_doc(recs=jsongen.records(40, seed=s)) for s in range(3)]
    infos = []
    progress = Progress(callback=infos.append, min_interval=0)
    export_many([(d, None) for d in docs], out_dir / "p.json", ExportOptions("json"), progress=progress)
    assert progress.finished and progress.total and progress.done == progress.total
    done = [i.done for i in infos if i.total]
    assert done == sorted(done) and all(i.done <= i.total for i in infos if i.total)


def test_refusals(open_doc, out_dir, lang_en) -> None:
    a = open_doc(recs=[{"a": 1}])
    for fmt in ("csv", "xlsx", "sqlite"):
        with pytest.raises(UserInputError) as ei:
            export_many([(a, None)], out_dir / f"x.{fmt}", ExportOptions(fmt))
        assert ei.value.key == "writer.error.many_format" and ei.value.hint
    with pytest.raises(UserInputError) as ei:
        export_many([(a, None)], out_dir / "x.json", ExportOptions("json", as_keyed=True))
    assert ei.value.key == "writer.invalid_option"
    with pytest.raises(UserInputError) as ei:
        export_many([], out_dir / "x.json", ExportOptions("json"))
    assert ei.value.key == "writer.error.no_inputs"
    with pytest.raises(UserInputError) as ei:
        export_many([(a, None)], a.source_path, ExportOptions("json"))
    assert ei.value.key == "writer.error.same_file"
    assert not list(out_dir.iterdir())


def test_partial_and_broken_inputs_are_reported(open_doc, out_dir, lang_en) -> None:
    good = open_doc(recs=[{"a": 1}])
    partial = open_doc(b'[{"a": 2}, {"a": 3}, {"a"', name="p.json")
    broken = open_doc(b'{"b": 1}\nnot json\n{"b": 2}\n', name="b.jsonl", fmt="jsonl")
    rep = export_many([(good, None), (partial, None), (broken, None)], out_dir / "r.json",
                      ExportOptions("json", indent=None))
    assert parsed(out_dir / "r.json") == [{"a": 1}, {"a": 2}, {"a": 3}, {"b": 1}, {"b": 2}]
    assert rep.warning_keys == ["writer.warn.partial", "writer.warn.broken_lines"]


def test_cancellation_leaves_nothing(open_doc, out_dir) -> None:
    docs = [open_doc(recs=jsongen.records(50, seed=s)) for s in range(2)]
    (out_dir / "c.json").write_bytes(b"old")
    token = CancelToken()
    calls = []

    def cancel_later(info: Any) -> None:
        calls.append(info)
        if len(calls) > 2:
            token.cancel()

    with pytest.raises(Cancelled):
        export_many([(d, None) for d in docs], out_dir / "c.json", ExportOptions("json"),
                    progress=Progress(callback=cancel_later, min_interval=0), cancel=token)
    assert [p.name for p in out_dir.iterdir()] == ["c.json"] and (out_dir / "c.json").read_bytes() == b"old"
