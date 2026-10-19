"""filetools.merge (docs/ARCHITECTURE.md §14.0, §14.4; s5/map_convert.md §4, §8).

* merge(split(x)) gives the records of x for every kind and both core backends (byte-equal for
  compact JSON Lines; the JSON / JSON Lines export of x; a wrapped document data-equal with its
  wrapper);
* root / wrapper rules through merge, mixed kinds, repeated member keys counted;
* deduplication across inputs (keep first / last, ``$``, ``$key``, a path) against an independent
  reference built from ``dupes.canonical_bytes``, also with the key runs spilled to disk, both
  backends; the deduplication's warnings of every input;
* edited inputs (pinned snapshots) are merged -- and deduplicated -- as they are shown;
* exact numbers (``0.1``, ``1e400``, ``12345678901234567890``, ``-0.0``, ``1.10``), no ``\\u`` escaping;
* refusals (format, partial inputs, the output is an input, options), cancellation, progress.
"""

from __future__ import annotations

import random
import zlib
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, dupes, editops, filetools, reader, writer
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.filetools import MergeOptions, SplitOptions, merge, split
from jsonreshape.paths import MISSING, get, parse_path
from jsonreshape.reader import Document
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export
from overlay_helpers import make_edited, make_source
from saver_helpers import make_doc

KINDS = ("array", "jsonl", "brace_list", "wrapped", "wrapped_obj", "keyed", "stream")


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


@pytest.fixture
def backend(core_backend, monkeypatch):
    """Every core user of a merge on one backend: indexing (documents opened in the test), the
    writer's Reformatter, the deduplication's PathExtractor."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    monkeypatch.setattr(writer, "core", core_backend)
    monkeypatch.setattr(dupes, "_EXTRACTOR", core_backend.PathExtractor)   # (as tests/test_dupes.py)
    monkeypatch.setattr(filetools, "core", core_backend)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)


def open_path(path: Path, docs: list, **kw: Any) -> Document:
    d = Document.open(path, **kw)
    d.build_index()
    docs.append(d)
    return d


def reopen_parts(rep: filetools.SplitReport, doc: Document, docs: list) -> list[Document]:
    kind = "array" if doc.kind == "brace_list" else doc.kind
    return [open_path(p.path, docs, format=kind, record_path=doc.record_path) for p in rep.parts]


def lines(path: Path) -> list[Any]:
    return [codec.loads(x) for x in path.read_bytes().splitlines() if x.strip()]


# ----------------------------------------------------------------------------------
# merge(split(x)) == x
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_merge_of_a_split_gives_the_records_back(tmp_path, docs, backend, kind) -> None:
    doc = make_doc(tmp_path / "s", kind, "pretty2", n=25, seed=12)
    docs.append(doc)
    rep = split(doc, SplitOptions(parts=4))
    parts = reopen_parts(rep, doc, docs)
    out = tmp_path / "out"
    out.mkdir()
    for opts in (ExportOptions("jsonl"), ExportOptions("json", indent=2), ExportOptions("json", indent=None)):
        m = merge(parts, out / f"m.{opts.fmt}", MergeOptions(opts))
        export(doc, out / f"x.{opts.fmt}", opts)
        assert m.records == 25 and [i[3] for i in m.inputs] == [p.records for p in rep.parts]
        assert [i[0] for i in m.inputs] == [p.path for p in rep.parts]
        if kind.startswith("wrapped") and opts.fmt == "json":
            # the first part's wrapper is kept (the same as the document's); the others are dropped
            assert codec.loads((out / "m.json").read_bytes()) == codec.loads((out / "x.json").read_bytes())
            assert m.warning_keys == ["writer.warn.wrappers_dropped"]
        else:
            assert (out / f"m.{opts.fmt}").read_bytes() == (out / f"x.{opts.fmt}").read_bytes(), opts
        if kind.startswith("wrapped") and opts.fmt == "jsonl":
            assert m.warning_keys == ["writer.note.wrapper_not_written"]


def test_compact_jsonl_round_trip_is_byte_equal(tmp_path, docs) -> None:
    src = tmp_path / "c.jsonl"
    recs = jsongen.records(40, seed=3)
    src.write_bytes(b"".join(codec.dumps(r) + b"\n" for r in recs))
    doc = open_path(src, docs)
    rep = split(doc, SplitOptions(size=2500))
    parts = reopen_parts(rep, doc, docs)
    merge(parts, tmp_path / "back.jsonl", MergeOptions(ExportOptions("jsonl")))
    assert (tmp_path / "back.jsonl").read_bytes() == src.read_bytes()


def test_exact_numbers_and_unicode(tmp_path, docs) -> None:
    text = ('[{"n": 0.1}, {"n": 1e400}, {"n": 12345678901234567890}, {"n": -0.0}, {"n": 1.10}, '
            '{"t": "Тошкент Oʻzbekiston G\'ulom \\u0041 😀"}]').encode()
    a = tmp_path / "a.json"
    a.write_bytes(text)
    b = tmp_path / "b.jsonl"
    b.write_bytes(b'{"n": 1E5}\n{"n": -0}\n')
    m = merge([open_path(a, docs), open_path(b, docs)], tmp_path / "m.json",
              MergeOptions(ExportOptions("json", indent=None)))
    data = (tmp_path / "m.json").read_bytes()
    assert data == ('[{"n":0.1},{"n":1e400},{"n":12345678901234567890},{"n":-0.0},{"n":1.10},'
                    '{"t":"Тошкент Oʻzbekiston G\'ulom \\u0041 😀"},{"n":1E5},{"n":-0}]').encode()
    assert m.records == 8


def test_mixed_kinds_and_repeated_keys(tmp_path, docs, lang_en) -> None:
    arr = open_path(_write(tmp_path / "a.json", b'[{"x": 1}]'), docs)
    keyed = open_path(_write(tmp_path / "k.json", b'{"a": {"v": 1}, "b": {"v": 2}}'), docs, format="keyed")
    keyed2 = open_path(_write(tmp_path / "k2.json", b'{"b": {"v": 3}}'), docs, format="keyed")
    m = merge([arr, keyed], tmp_path / "m.json", MergeOptions(ExportOptions("json", indent=None)))
    assert codec.loads((tmp_path / "m.json").read_bytes()) == [{"x": 1}, {"a": {"v": 1}}, {"b": {"v": 2}}]
    assert m.warning_keys == ["writer.warn.members_as_objects"]
    m = merge([keyed, keyed2], tmp_path / "kk.json", MergeOptions(ExportOptions("json", indent=None)))
    assert (tmp_path / "kk.json").read_bytes() == b'{"a":{"v":1},"b":{"v":2},"b":{"v":3}}'
    assert m.warning_keys == ["writer.warn.merge_duplicate_keys"] and "$key" in m.warnings[0]
    # ... deduplicated by the member key: one record per key
    m = merge([keyed, keyed2], tmp_path / "d.json", MergeOptions(ExportOptions("json", indent=None),
                                                                 dedupe_by="$key"))
    assert (tmp_path / "d.json").read_bytes() == b'{"a":{"v":1},"b":{"v":2}}'
    assert (m.duplicates, m.groups) == (1, 1) and m.warnings == []
    m = merge([keyed, keyed2], tmp_path / "e.json", MergeOptions(ExportOptions("json", indent=None),
                                                                 dedupe_by="$key", keep="last"))
    assert (tmp_path / "e.json").read_bytes() == b'{"a":{"v":1},"b":{"v":3}}'


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# ----------------------------------------------------------------------------------
# deduplication against a reference
# ----------------------------------------------------------------------------------
def reference(inputs: list[list[tuple[str | None, Any]]], key: str, keep: str) -> list[Any]:
    """The records a merge with deduplication keeps: ``inputs`` = per input ``(member key, value)``
    in order; equal canonical key values form a group; keep the first / last (global order)."""
    flat = [(k, v) for recs in inputs for k, v in recs]
    groups: dict[bytes, list[int]] = {}
    for g, (k, v) in enumerate(flat):
        if key == "$":
            kv = v
        elif key == "$key":
            if k is None:
                continue
            kv = k
        else:
            kv = get(v, parse_path(key))
            if kv is MISSING:
                continue
        groups.setdefault(dupes.canonical_bytes(kv), []).append(g)
    drop = set()
    for members in groups.values():
        if len(members) > 1:
            kept = members[0] if keep == "first" else members[-1]
            drop.update(m for m in members if m != kept)
    return [flat[g] for g in range(len(flat)) if g not in drop]


def random_inputs(tmp_path: Path, docs: list, seed: int) -> tuple[list[Document], list[list[tuple[str | None, Any]]]]:
    rng = random.Random(seed)
    pool = [{"id": rng.randint(0, 12), "v": rng.choice([1, 1.0, "1", None, [1, 2], {"a": 1}]),
             "w": rng.randint(0, 3)} for _ in range(30)]
    out_docs, out_recs = [], []
    for i, kind in enumerate(("array", "jsonl", "keyed")):
        recs = [rng.choice(pool) for _ in range(rng.randint(5, 25))]
        path = tmp_path / f"in{i}.{'jsonl' if kind == 'jsonl' else 'json'}"
        keys = [f"k{rng.randint(0, 9)}" for _ in recs]
        jsongen.write_file(path, recs, kind, keys=keys)
        out_docs.append(open_path(path, docs, format=kind))
        out_recs.append([(keys[j] if kind == "keyed" else None, r) for j, r in enumerate(recs)])
    return out_docs, out_recs


@pytest.mark.parametrize("spill", [False, True])
@pytest.mark.parametrize("key", ["id", "$", "v", "$key", "missing.path"])
@pytest.mark.parametrize("keep", ["first", "last"])
def test_dedupe_matches_the_reference(tmp_path, docs, monkeypatch, backend, key, keep, spill) -> None:
    ins, recs = random_inputs(tmp_path, docs, seed=zlib.crc32(f"{key}|{keep}".encode()) % 1000)   # deterministic
    limit = None
    if spill:
        monkeypatch.setattr(dupes, "MIN_RUN_ENTRIES", 2)
        limit = dupes.ENTRY_BYTES * 3
    m = merge(ins, tmp_path / "m.jsonl", MergeOptions(ExportOptions("jsonl"), dedupe_by=key, keep=keep,
                                                       memory_limit=limit))
    want = reference(recs, key, keep)
    got = lines(tmp_path / "m.jsonl")
    expect = [({k: v} if k is not None else v) for k, v in want]     # member records are {"key": value} lines
    assert [codec.dumps(g) for g in got] == [codec.dumps(e) for e in expect]
    total = sum(len(r) for r in recs)
    assert m.duplicates == total - len(want) and m.records == len(want)
    assert sum(i[3] for i in m.inputs) == len(want)


def test_single_input_dedupe_is_a_dedupe_tool(tmp_path, docs) -> None:
    src = _write(tmp_path / "d.json", codec.dumps([{"id": i % 4, "n": i} for i in range(12)]))
    m = merge([open_path(src, docs)], tmp_path / "u.json", MergeOptions(ExportOptions("json", indent=None),
                                                                        dedupe_by="id"))
    assert codec.loads((tmp_path / "u.json").read_bytes()) == [{"id": i, "n": i} for i in range(4)]
    assert (m.duplicates, m.groups) == (8, 4)


def test_dedupe_warnings_of_every_input(tmp_path, docs, lang_en) -> None:
    # "$key" over inputs without member keys: the deduplication warns once per input -- every one
    # of them reaches the merge report (not only the first)
    a = open_path(_write(tmp_path / "a.json", b'[{"x": 1}, {"x": 1}]'), docs)
    b = open_path(_write(tmp_path / "b.jsonl", b'{"x": 1}\n'), docs)
    m = merge([a, b], tmp_path / "m.jsonl", MergeOptions(ExportOptions("jsonl"), dedupe_by="$key"))
    assert m.records == 3 and m.duplicates == 0
    assert m.warning_keys == ["dupes.warn.input_no_record_keys"] * 2
    assert [w.split(":")[0] for w in m.warnings] == ["a.json", "b.jsonl"]
    assert "2 records were counted as having no key" in m.warnings[0]


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped"])
def test_edited_inputs_are_merged_as_shown(tmp_path, docs, kind) -> None:
    doc, _ob, exp = make_edited(tmp_path / "e", kind, "mixed", n=16, seed=4)
    other = make_source(tmp_path / "o.json", kind, n=6, seed=9)
    docs += [doc, exp, other]
    assert doc.edited
    for opts, dedupe in ((ExportOptions("jsonl"), None), (ExportOptions("json", indent=2), None),
                         (ExportOptions("jsonl"), "$"), (ExportOptions("json", indent=None), "$")):
        got = merge([doc, other], tmp_path / f"a.{opts.fmt}", MergeOptions(opts, dedupe_by=dedupe))
        want = merge([exp, other], tmp_path / f"b.{opts.fmt}", MergeOptions(opts, dedupe_by=dedupe))
        assert (tmp_path / f"a.{opts.fmt}").read_bytes() == (tmp_path / f"b.{opts.fmt}").read_bytes(), (opts, dedupe)
        assert (got.records, got.duplicates, got.groups) == (want.records, want.duplicates, want.groups)
        assert got.inputs[0][2] == exp.record_count             # the records shown (edits included)


# ----------------------------------------------------------------------------------
# refusals, cancellation, progress
# ----------------------------------------------------------------------------------
def test_refusals(tmp_path, docs, lang_en) -> None:
    a = open_path(_write(tmp_path / "a.json", b'[{"a": 1}]'), docs)
    for fmt in ("csv", "xlsx", "sqlite"):
        with pytest.raises(UserInputError) as ei:
            MergeOptions(ExportOptions(fmt))
        assert ei.value.key == "filetools.error.merge_format" and "jsonl" in ei.value.hint
    for kw in ({"keep": "none"}, {"dedupe_by": ""}, {"allow_partial": 1}, {"memory_limit": 0}):
        with pytest.raises(UserInputError) as ei:
            MergeOptions(ExportOptions("json"), **kw)
        assert ei.value.key == "filetools.error.option"
    with pytest.raises(UserInputError) as ei:
        merge([], tmp_path / "x.json", MergeOptions(ExportOptions("json")))
    assert ei.value.key == "writer.error.no_inputs"
    with pytest.raises(UserInputError) as ei:
        merge([a], a.source_path, MergeOptions(ExportOptions("json")))
    assert ei.value.key == "writer.error.same_file"
    bad = open_path(_write(tmp_path / "p.json", b'[{"a": 1}, {"a": 2}, {"a"'), docs)
    with pytest.raises(UserInputError) as ei:
        merge([a, bad], tmp_path / "m.json", MergeOptions(ExportOptions("json")))
    assert ei.value.key == "filetools.error.partial_input" and "p.json" in ei.value.message
    assert "--allow-partial" in ei.value.hint
    assert not (tmp_path / "m.json").exists()
    m = merge([a, bad], tmp_path / "m.json", MergeOptions(ExportOptions("json", indent=None), allow_partial=True))
    assert codec.loads((tmp_path / "m.json").read_bytes()) == [{"a": 1}, {"a": 1}, {"a": 2}]
    assert m.warning_keys == ["writer.warn.partial"]
    with pytest.raises(UserInputError) as ei:
        merge([a], tmp_path / "w.json", MergeOptions(ExportOptions("json"), dedupe_by="a[*]"))
    assert ei.value.key.startswith("dupes.error")


@pytest.mark.parametrize("dedupe", [None, "id"])
def test_cancellation_and_progress(tmp_path, docs, dedupe) -> None:
    ins = [open_path(_write(tmp_path / f"i{k}.json", codec.dumps([{"id": i % 7, "k": k} for i in range(300)])), docs)
           for k in range(3)]
    infos: list[Any] = []
    progress = Progress(callback=infos.append, min_interval=0)
    m = merge(ins, tmp_path / "ok.jsonl", MergeOptions(ExportOptions("jsonl"), dedupe_by=dedupe), progress=progress)
    assert progress.finished and progress.done == progress.total
    done = [i.done for i in infos if i.total]
    assert done == sorted(done)
    assert m.records == (7 if dedupe else 900)
    (tmp_path / "c.jsonl").write_bytes(b"old")
    token = CancelToken()

    def cancel_soon(info: Any) -> None:
        if info.done:
            token.cancel()

    with pytest.raises(Cancelled):
        merge(ins, tmp_path / "c.jsonl", MergeOptions(ExportOptions("jsonl"), dedupe_by=dedupe),
              progress=Progress(callback=cancel_soon, min_interval=0), cancel=token)
    assert (tmp_path / "c.jsonl").read_bytes() == b"old"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
