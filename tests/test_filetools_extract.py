"""filetools.extract, resolve_node, parse_select (docs/ARCHITECTURE.md §14.4; s5/map_convert.md §5, §8).

* a record-relative pattern gathered from every record equals ``PathPattern.resolve`` over the parsed
  records (every kind, member records, both core backends, JSON and JSON Lines, ``**`` patterns,
  selections, sorted keys, edited documents);
* huge records: the node walk (keys / indices / ``[*]``) and ``stream_values`` (other patterns), a
  record over the capture budget skipped with a warning;
* one node (``#5.orders``, ``/data/5/orders``, ``data[5].orders``; JSON Lines records); the record path
  form (the records without the wrapper);
* refusals; ``parse_select`` (0-based ``N``, ``A:B``, ``A:``, ``:B``, comma lists, errors).
"""

from __future__ import annotations

from array import array
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec, filetools
from jsonreshape.errors import Cancelled, PathSyntaxError, UserInputError
from jsonreshape.filetools import extract, parse_select, resolve_node
from jsonreshape.paths import parse_pattern
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions
from overlay_helpers import make_edited, make_source

PATTERNS = ["orders[*]", "orders", "orders[*].oid", "contact.address", "tags[*]", "*", "$", "orders[0]",
            "contact.*", "extra..id", "**.oid", '["dot.key"]', "name"]


@pytest.fixture
def docs():
    opened: list[Document] = []
    yield opened
    for d in opened:
        d.close()


@pytest.fixture
def backend(core_backend, monkeypatch):
    from jsonreshape import writer

    monkeypatch.setattr(filetools, "core", core_backend)
    monkeypatch.setattr(writer, "core", core_backend)
    return core_backend


def reference(doc: Document, pattern: str, source: Any = None) -> list[bytes]:
    """The values of ``pattern`` in every record (parsed), compact JSON."""
    pat = parse_pattern(pattern)
    out = []
    positions = range(doc.record_count) if source is None else source
    for i in positions:
        out += [codec.dumps(v) for _p, v in pat.resolve(doc.record(i))]
    return out


def got_values(path: Path, fmt: str) -> list[bytes]:
    data = path.read_bytes()
    values = codec.loads(data) if fmt == "json" else [codec.loads(x) for x in data.splitlines()]
    return [codec.dumps(v) for v in values]


# ----------------------------------------------------------------------------------
# gather by a record-relative pattern
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped", "wrapped_obj", "stream", "brace_list"])
def test_gather_equals_the_reference(tmp_path, docs, backend, kind) -> None:
    doc = make_source(tmp_path / "s.json", kind, n=25, seed=7, style="pretty2")
    docs.append(doc)
    for pattern in PATTERNS:
        want = reference(doc, pattern)
        for fmt in ("jsonl", "json"):
            out = tmp_path / f"g.{fmt}"
            rep = extract(doc, out, ExportOptions(fmt), path=pattern)
            assert got_values(out, fmt) == want, (kind, pattern, fmt)
            assert rep.records == rep.rows == len(want) and rep.skipped == 0 and rep.warnings == []


def test_gather_copies_bytes(tmp_path, docs) -> None:
    src = tmp_path / "b.jsonl"
    src.write_bytes(b'{"o": [{"x": 1.10, "s": "\\u0041\xc3\xa9"}, {"x": 1e400}]}\n{"o": []}\n'
                    b'{"o": [12345678901234567890, -0.0, 0.1]}\n{"p": 1}\n')
    doc = Document.open(src)
    doc.build_index()
    docs.append(doc)
    extract(doc, tmp_path / "o.jsonl", ExportOptions("jsonl"), path="o[*]")
    assert (tmp_path / "o.jsonl").read_bytes() == (b'{"x":1.10,"s":"\\u0041\xc3\xa9"}\n{"x":1e400}\n'
                                                   b'12345678901234567890\n-0.0\n0.1\n')
    extract(doc, tmp_path / "o.json", ExportOptions("json", indent=None), path="o")
    assert (tmp_path / "o.json").read_bytes() == (b'[[{"x":1.10,"s":"\\u0041\xc3\xa9"},{"x":1e400}],[],'
                                                  b'[12345678901234567890,-0.0,0.1]]')
    extract(doc, tmp_path / "n.json", ExportOptions("json", indent=None), path="nothing")
    assert (tmp_path / "n.json").read_bytes() == b"[]"


def test_gather_selection_sort_keys_and_gzip(tmp_path, docs) -> None:
    doc = make_source(tmp_path / "s.json", "array", n=20, seed=3)
    docs.append(doc)
    sel = [19, 2, 2, 7]
    extract(doc, tmp_path / "s.jsonl", ExportOptions("jsonl"), path="contact", source=sel)
    assert got_values(tmp_path / "s.jsonl", "jsonl") == reference(doc, "contact", sel)
    extract(doc, tmp_path / "k.jsonl", ExportOptions("jsonl", sort_keys="codepoint"), path="$")
    assert (tmp_path / "k.jsonl").read_bytes().splitlines() == [
        codec.dumps(doc.record(i), sort_keys=True) for i in range(20)]
    extract(doc, tmp_path / "z.jsonl.gz", ExportOptions("jsonl"), path="orders[*]")
    import gzip

    data = gzip.decompress((tmp_path / "z.jsonl.gz").read_bytes())
    assert [codec.dumps(codec.loads(x)) for x in data.splitlines()] == reference(doc, "orders[*]")


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped_obj"])
def test_gather_on_an_edited_document(tmp_path, docs, kind) -> None:
    doc, _ob, exp = make_edited(tmp_path / "e", kind, "mixed", n=16, seed=4)
    docs += [doc, exp]
    for pattern in ("orders[*]", "*", "$", "contact.address.city"):
        extract(doc, tmp_path / "a.jsonl", ExportOptions("jsonl"), path=pattern)
        extract(exp, tmp_path / "b.jsonl", ExportOptions("jsonl"), path=pattern)
        assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes(), pattern


@pytest.mark.parametrize("kind", ["array", "jsonl", "keyed", "wrapped"])
def test_gather_from_huge_records(tmp_path, docs, backend, kind) -> None:
    small = make_source(tmp_path / "s.json", kind, n=20, seed=21, style="pretty2")
    huge = make_source(tmp_path / "h.json", kind, n=20, seed=21, style="pretty2",
                       settings=Settings(large_node_bytes=150))
    docs += [small, huge]
    assert any(huge.is_huge(i) for i in range(huge.record_count))
    for pattern in ("orders[*]", "orders[*].oid", "orders", "contact.address", "$", "*", "**.oid", "tags[1]"):
        for fmt in ("jsonl", "json"):
            extract(small, tmp_path / f"a.{fmt}", ExportOptions(fmt), path=pattern)
            rep = extract(huge, tmp_path / f"b.{fmt}", ExportOptions(fmt), path=pattern)
            assert got_values(tmp_path / f"b.{fmt}", fmt) == got_values(tmp_path / f"a.{fmt}", fmt), (pattern, fmt)
            assert rep.skipped == 0
    # the node walk copies bytes: identical output for walkable patterns
    for pattern in ("orders[*]", "contact", "tags"):
        extract(small, tmp_path / "a.jsonl", ExportOptions("jsonl"), path=pattern)
        extract(huge, tmp_path / "b.jsonl", ExportOptions("jsonl"), path=pattern)
        assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


def test_gather_over_the_capture_budget(tmp_path, docs, monkeypatch, lang_en) -> None:
    recs = [{"a": i, "big": "x" * 400} for i in range(5)]
    src = tmp_path / "c.json"
    src.write_bytes(codec.dumps(recs))
    doc = Document.open(src, settings=Settings(large_node_bytes=100))
    doc.build_index()
    docs.append(doc)
    monkeypatch.setattr(filetools, "capture_budget", lambda settings: 50)
    rep = extract(doc, tmp_path / "o.jsonl", ExportOptions("jsonl"), path="**")   # not walkable: streamed
    assert rep.skipped == 5 and rep.records == 0
    assert rep.warning_keys == ["filetools.warn.extract_too_large"]
    assert rep.warnings[0].startswith("The values of 5 large records did not fit") and "#0" in rep.warnings[0]
    for path in ("big", "*"):                                                         # walked: any size
        rep = extract(doc, tmp_path / "w.jsonl", ExportOptions("jsonl"), path=path)
        assert rep.skipped == 0 and rep.records == 5 * (1 if path == "big" else 2)


def test_walk_keeps_dict_semantics_in_huge_records(tmp_path, docs, monkeypatch) -> None:
    rec = b'{"a": 1, "b": {"x": 1, "y": 2, "x": 3}, "a": 4, "c": [5, {"a": 6}], "pad": "' + b"p" * 300 + b'"}'
    src = tmp_path / "d.jsonl"
    src.write_bytes(rec + b"\n" + rec + b"\n")
    small = Document.open(src)
    small.build_index()
    huge = Document.open(src, settings=Settings(large_node_bytes=50))
    huge.build_index()
    docs += [small, huge]
    assert huge.is_huge(0) and not small.is_huge(0)
    for path in ("*", "b.*", "a", "b.x", "c[*]", "c[1].a", "*.x", "c.1.a", "c[0]", "b[0]", "c.x"):
        extract(small, tmp_path / "a.jsonl", ExportOptions("jsonl"), path=path)
        extract(huge, tmp_path / "b.jsonl", ExportOptions("jsonl"), path=path)
        assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes(), path
        assert got_values(tmp_path / "a.jsonl", "jsonl") == reference(small, path)
    # an object bigger than the dict check: its members are written as they are
    monkeypatch.setattr(filetools, "WALK_MAX_MEMBERS", 2)
    extract(huge, tmp_path / "c.jsonl", ExportOptions("jsonl"), path="b.*")
    assert (tmp_path / "c.jsonl").read_bytes().splitlines()[:3] == [b"1", b"2", b"3"]


def test_gather_progress_and_cancel(tmp_path, docs) -> None:
    doc = make_source(tmp_path / "s.json", "array", n=200, seed=2)
    docs.append(doc)
    progress = Progress(min_interval=0)
    extract(doc, tmp_path / "o.jsonl", ExportOptions("jsonl"), path="orders[*]", progress=progress)
    assert progress.finished and progress.done == progress.total
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        extract(doc, tmp_path / "c.jsonl", ExportOptions("jsonl"), path="orders[*]", cancel=token)
    assert not (tmp_path / "c.jsonl").exists()


# ----------------------------------------------------------------------------------
# one node, the record path form, refusals
# ----------------------------------------------------------------------------------
def test_node_locations(tmp_path, docs) -> None:
    doc = make_source(tmp_path / "w.json", "wrapped", n=12, seed=5)
    docs.append(doc)
    rec = next(i for i in range(12) if "orders" in doc.record(i))
    a = resolve_node(doc, f"#{rec}.orders")
    b = resolve_node(doc, f"/data/{rec}/orders")
    c = resolve_node(doc, f"data[{rec}].orders")
    d = resolve_node(doc, ("data", rec, "orders"))
    assert a == b == c == d and resolve_node(doc, a) is a
    for fmt in ("json", "jsonl"):
        rep = extract(doc, tmp_path / f"n.{fmt}", ExportOptions(fmt), node=f"#{rec}.orders")
        want = doc.record(rec)["orders"]
        got = got_values(tmp_path / f"n.{fmt}", fmt) if fmt == "jsonl" else [
            codec.dumps(codec.loads((tmp_path / "n.json").read_bytes()))]
        assert got == ([codec.dumps(v) for v in want] if fmt == "jsonl" else [codec.dumps(want)])
        assert rep.records == (len(want) if fmt == "jsonl" else 1)
    for bad in (f"#{rec}.nothing", "#99", "/data/99", "nothing.at.all"):
        with pytest.raises(UserInputError) as ei:
            resolve_node(doc, bad)
        assert ei.value.key == "reader.path_not_found"
    with pytest.raises(PathSyntaxError):
        resolve_node(doc, "#x")


def test_node_of_a_json_lines_record(tmp_path, docs) -> None:
    src = tmp_path / "l.jsonl"
    src.write_bytes(b'{"a": 1}\n{"orders": [{"o": 1}, {"o": 2.50}]}\n')
    doc = Document.open(src)
    doc.build_index()
    docs.append(doc)
    extract(doc, tmp_path / "n.jsonl", ExportOptions("jsonl"), node="#1.orders")
    assert (tmp_path / "n.jsonl").read_bytes() == b'{"o":1}\n{"o":2.50}\n'
    extract(doc, tmp_path / "r.json", ExportOptions("json", indent=None), node="#1")
    assert (tmp_path / "r.json").read_bytes() == b'{"orders":[{"o":1},{"o":2.50}]}'


def test_record_path_form(tmp_path, docs) -> None:
    doc = make_source(tmp_path / "w.json", "wrapped", n=6, seed=1)
    docs.append(doc)
    for fmt in ("json", "jsonl", "csv"):
        rep = extract(doc, tmp_path / f"r.{fmt}", ExportOptions(fmt))
        assert rep.records == 6 and "writer.note.wrapper_not_written" not in rep.warning_keys
    assert codec.loads((tmp_path / "r.json").read_bytes()) == doc.root().value()["data"]


def test_refusals(tmp_path, docs, lang_en) -> None:
    doc = make_source(tmp_path / "s.json", "array", n=3)
    docs.append(doc)
    for fmt in ("csv", "xlsx", "sqlite"):
        for kw in ({"path": "a"}, {"node": "#0"}):
            with pytest.raises(UserInputError) as ei:
                extract(doc, tmp_path / f"x.{fmt}", ExportOptions(fmt), **kw)
            assert ei.value.key == "filetools.error.extract_format" and ei.value.hint
    with pytest.raises(UserInputError) as ei:
        extract(doc, tmp_path / "x.json", ExportOptions("json"), path="a", node="#0")
    assert ei.value.key == "filetools.error.extract_args"
    with pytest.raises(PathSyntaxError):
        extract(doc, tmp_path / "x.json", ExportOptions("json"), path="a[")
    with pytest.raises(UserInputError) as ei:
        extract(doc, doc.source_path, ExportOptions("json"), path="a")
    assert ei.value.key == "writer.error.same_file"
    assert not list(tmp_path.glob("x.*"))


# ----------------------------------------------------------------------------------
# parse_select
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("spec,count,want", [
    ("5", 10, range(5, 6)),
    ("#5", 10, range(5, 6)),
    ("2:7", 10, range(2, 7)),
    (" 2 : 7 ", 10, range(2, 7)),
    ("7:", 10, range(7, 10)),
    (":3", 10, range(0, 3)),
    (":", 10, range(0, 10)),
    ("0:1000", 10, range(0, 10)),           # the end is cut to the record count
    ("#2:#4", 10, range(2, 4)),
    ("1,5,8:", 10, array("Q", [1, 5, 8, 9])),
    ("9,0,0:2", 10, array("Q", [9, 0, 0, 1])),
    ("007", 10, range(7, 8)),
])
def test_parse_select(spec, count, want) -> None:
    got = parse_select(spec, count)
    assert type(got) is type(want) and list(got) == list(want)


@pytest.mark.parametrize("spec,key", [
    ("", "filetools.error.select_syntax"), (" ", "filetools.error.select_syntax"),
    ("1,,2", "filetools.error.select_syntax"), ("a", "filetools.error.select_syntax"),
    ("-1", "filetools.error.select_syntax"), ("1:2:3", "filetools.error.select_syntax"),
    ("1-3", "filetools.error.select_syntax"), ("1.5", "filetools.error.select_syntax"),
    ("10", "filetools.error.select_range"), ("12:", "filetools.error.select_range"),
    ("10:20", "filetools.error.select_range"), ("5:5", "filetools.error.select_empty"),
    ("7:3", "filetools.error.select_empty"),
])
def test_parse_select_errors(spec, key, lang_en) -> None:
    with pytest.raises(UserInputError) as ei:
        parse_select(spec, 10)
    assert ei.value.key == key
    assert "{" not in ei.value.message
    if key == "filetools.error.select_range":
        assert "#0 to #9" in ei.value.message
    if key == "filetools.error.select_syntax":
        assert "start at 0" in ei.value.hint


def test_parse_select_empty_document(lang_uz) -> None:
    with pytest.raises(UserInputError) as ei:
        parse_select(":", 0)
    assert ei.value.key == "filetools.error.select_range"
    assert parse_select(":3", 3) == range(0, 3)
