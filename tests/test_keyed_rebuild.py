r"""``writer.ExportOptions.as_keyed`` / ``--as-keyed``: rebuild the keyed object from the JSON
Lines form of a keyed document (``{"key": value}`` per line; user decision 2026-10-17; writer
docstring "Keyed object from one-member records", docs/ARCHITECTURE.md §8, §10).

* round trips keyed JSON -> JSONL -> JSON(as_keyed) == keyed JSON -> JSON, byte for byte, for
  indent 2 / 4 / tab / compact and sort_keys alpha / codepoint, with exact numbers and
  Uzbek / Cyrillic / escaped keys and values (``"\u0041"`` stays as written);
* every rejected record shape: the 0-based record number, a localized description, nothing
  saved (no output, no temporary file, an existing output unchanged) -- in any block, in huge
  records, in selections (the first offender in selection order);
* the one-step JSON Lines join and the general join give the same bytes (``\r\n``, blank and
  broken lines, whitespace around and inside records); the threaded (Rust) check, the plain
  check and the pure-Python core give the same results;
* repeated member keys: written as they are, counted (escapes decoded), bounded memory;
* wrapped / keyed / filtered / sorted / empty / partial sources; the CLI option, its help and
  its rejection for non-JSON outputs (exit 1).
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import jsongen
from jsonreshape import cli, codec, core, i18n, writer
from jsonreshape.errors import Cancelled, ExportError, UserInputError
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress
from jsonreshape.writer import ExportOptions, export

INDENTS = [2, 4, "\t", None]
SORTS = [False, "alpha", "codepoint"]

#: a keyed document with exact number tokens and keys/values that must stay as written
EXACT_KEYED = (
    '{"\\u0041": {"n": [0.1, 1e400, 12345678901234567890, -0.0, 1.10], "s": "\\u0041\\n"},\n'
    ' "Oʻzbekiston" : {"shahar": "Toshkent", "kirill": "Тошкент", "emoji": "😀", "esc": "\\ud83d\\ude00"},\n'
    '  "Тошкент": [1E5, -0, 2.5E+3, 123456789012345678901234567890, -1e-400],\n'
    ' "\\ud800": "lone", "\\/x": {"b": {"a": {"z": null, "y": true}}}, "": false, "z": {},\n'
    ' "G\'ulom": {"Zebo": 1, "apple": 2, "Banan": 3, "oʻrik": 4, "olma": 5, "item10": 6, "item2": 7}}'
).encode("utf-8")
EXACT_TOKENS = [b"0.1", b"1e400", b"12345678901234567890", b"-0.0", b"1.10", b"1E5", b"-0", b"2.5E+3",
                b"123456789012345678901234567890", b"-1e-400"]


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def make_doc(tmp_path: Path):
    """``make_doc(data, name=..., settings=..., format=..., record_path=...)``: an indexed
    Document of ``data`` (a ``.jsonl`` file unless ``name`` says otherwise)."""
    docs: list[Document] = []
    src = tmp_path / "src"
    src.mkdir()

    def make(data: bytes, name: str | None = None, *, settings: Settings | None = None, format: str | None = None,
             record_path: Any = None) -> Document:
        p = src / (name or f"s{len(docs)}.jsonl")
        p.write_bytes(data)
        doc = Document.open(p, settings=settings, format=format, record_path=record_path)
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


def _namespace(mod: Any, name: str) -> SimpleNamespace:
    return SimpleNamespace(Scanner=mod.Scanner, Reformatter=mod.Reformatter, BACKEND=name)


@pytest.fixture(params=["active", "plain", "python"])
def check_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """The writer's core as loaded (Rust: the check runs in a helper thread), the same core
    without the thread ("plain"), and the pure-Python core (fed in 1 MiB pieces)."""
    if request.param == "plain":
        monkeypatch.setattr(writer, "core", _namespace(core, "python"))
    elif request.param == "python":
        monkeypatch.setattr(writer, "core", _namespace(core.get_backend("python"), "python"))
    return request.param


def out_bytes(doc: Document, out_dir: Path, name: str = "o.json", source: Any = None, **opts: Any) -> bytes:
    path = out_dir / name
    export(doc, path, ExportOptions("json", **opts), source=source)
    return path.read_bytes()


def to_jsonl(doc: Document, out_dir: Path, make_doc: Any, name: str = "k.jsonl") -> Document:
    """The JSON Lines export of ``doc`` opened as a new document."""
    path = out_dir / name
    export(doc, path, ExportOptions("jsonl"))
    return make_doc(path.read_bytes(), name=name)


def keyed_source(n: int, seed: int, style: str) -> bytes:
    recs = jsongen.records(n, seed=seed)
    keys = [f"id{i}" for i in range(n)]
    for i, k in enumerate(["Oʻzbekiston", "Тошкент", 'quote"in', "back\\slash", "tab\there", "", "😀 emoji",
                           "a" * 300, "G'ulom"]):
        if i < n:
            keys[i * 3 % n] = k
    return jsongen.build_document(recs, "keyed", style=style, keys=keys, seed=seed)


def lines(*records: bytes, sep: bytes = b"\n") -> bytes:
    return sep.join(records) + sep


def good_lines(n: int, prefix: str = "k") -> list[bytes]:
    return [b'{"%s%d": {"v": %d, "s": "x%d"}}' % (prefix.encode(), i, i, i) for i in range(n)]


def files_in(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


# ----------------------------------------------------------------------------------
# round trips
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("style", ["compact", "pretty2", "messy", "escaped"])
def test_round_trip_is_byte_identical_for_every_indent_and_key_order(make_doc, out_dir: Path, style: str) -> None:
    src = make_doc(keyed_source(40, seed=7, style=style), name="keyed.json")
    assert src.info.kind == "keyed" and src.record_count == 40
    jl = to_jsonl(src, out_dir, make_doc)
    assert jl.info.kind == "jsonl" and jl.record_count == 40
    for indent in INDENTS:
        for sort in SORTS:
            want = out_bytes(src, out_dir, "want.json", indent=indent, sort_keys=sort)
            got = out_bytes(jl, out_dir, "got.json", indent=indent, sort_keys=sort, as_keyed=True)
            assert got == want, (indent, sort)
    # the result is the keyed object of the source, members in order
    assert list(codec.loads(got)) == [src.record_key(i) for i in range(40)]


def test_round_trip_keeps_numbers_keys_and_escapes_exactly(make_doc, out_dir: Path, check_path: str) -> None:
    src = make_doc(EXACT_KEYED, name="exact.json", format="keyed")
    jl = to_jsonl(src, out_dir, make_doc)
    for indent in INDENTS:
        for sort in SORTS:
            want = out_bytes(src, out_dir, "want.json", indent=indent, sort_keys=sort)
            got = out_bytes(jl, out_dir, "got.json", indent=indent, sort_keys=sort, as_keyed=True)
            assert got == want, (check_path, indent, sort)
    compact = out_bytes(jl, out_dir, "c.json", indent=None, as_keyed=True)
    for tok in EXACT_TOKENS:
        assert tok in compact, tok
    # keys and values are never re-encoded: escapes stay, non-ASCII is never \u-escaped
    for raw in (b'"\\u0041":', b'"\\u0041\\n"', b'"\\ud800":', b'"\\/x":', b'"\\ud83d\\ude00"',
                "\"Oʻzbekiston\":".encode(), "\"Тошкент\":".encode(), "😀".encode()):
        assert raw in compact, raw
    assert compact.count(b"\\u") == EXACT_KEYED.count(b"\\u")
    assert compact.startswith(b'{"\\u0041":{"n":[0.1,1e400,12345678901234567890,-0.0,1.10]')
    # sort_keys sorts inside the values; the member keys keep their order
    alpha = codec.loads(out_bytes(jl, out_dir, "a.json", sort_keys="alpha", as_keyed=True))
    assert list(alpha) == ["A", "Oʻzbekiston", "Тошкент", "\ud800", "/x", "", "z", "G'ulom"]
    assert list(alpha["G'ulom"]) == ["apple", "Banan", "item2", "item10", "olma", "Zebo", "oʻrik"]
    point = codec.loads(out_bytes(jl, out_dir, "p.json", sort_keys="codepoint", as_keyed=True))
    assert list(point["G'ulom"]) == ["Banan", "Zebo", "apple", "item10", "item2", "olma", "oʻrik"]
    assert list(point["/x"]["b"]["a"]) == ["y", "z"]


def test_lines_written_by_hand_give_the_expected_text(make_doc, out_dir: Path) -> None:
    doc = make_doc(lines(b'{"a": {"x": 0.1, "y": [1e400]}}', b'{ "b" : -0.0 }', b'{"\\u0041":"\\u0041"}',
                         b'{"c":{}}', b'{"d":[]}'))
    assert out_bytes(doc, out_dir, indent=None, as_keyed=True) == \
        b'{"a":{"x":0.1,"y":[1e400]},"b":-0.0,"\\u0041":"\\u0041","c":{},"d":[]}'
    assert out_bytes(doc, out_dir, indent=2, as_keyed=True) == (
        b'{\n  "a": {\n    "x": 0.1,\n    "y": [\n      1e400\n    ]\n  },\n  "b": -0.0,\n'
        b'  "\\u0041": "\\u0041",\n  "c": {},\n  "d": []\n}')
    assert out_bytes(doc, out_dir, indent="\t", as_keyed=True).startswith(b'{\n\t"a": {\n\t\t"x": 0.1,')
    rep = export(doc, out_dir / "r.json", ExportOptions("json", as_keyed=True))
    assert (rep.records, rep.rows, rep.warnings, rep.files) == (5, 5, [], [out_dir / "r.json"])


@pytest.mark.parametrize("indent", INDENTS)
def test_no_records_give_an_empty_object(make_doc, out_dir: Path, indent: Any) -> None:
    doc = make_doc(lines(*good_lines(3)))
    assert out_bytes(doc, out_dir, source=[], indent=indent, as_keyed=True) == b"{}"
    empty_array = make_doc(b"[]", name="empty.json")
    assert empty_array.record_count == 0
    assert out_bytes(empty_array, out_dir, indent=indent, as_keyed=True) == b"{}"
    assert out_bytes(empty_array, out_dir, indent=indent, sort_keys=True, as_keyed=True) == b"{}"


# ----------------------------------------------------------------------------------
# rejected records
# ----------------------------------------------------------------------------------
BAD_RECORDS = [
    (b"[1, 2]", "array", None, "massiv", "an array"),
    (b"[]", "array", None, "massiv", "an array"),
    (b'"text"', "string", None, "matn (satr)", "a string"),
    (b'"}"', "string", None, "matn (satr)", "a string"),
    (b"5", "number", None, "son", "a number"),
    (b"-0.5e3", "number", None, "son", "a number"),
    (b"true", "bool", None, "mantiqiy qiymat (true/false)", "a boolean (true/false)"),
    (b"false", "bool", None, "mantiqiy qiymat (true/false)", "a boolean (true/false)"),
    (b"null", "null", None, "null", "null"),
    (b"{}", "object", 0, "bo'sh obyekt", "an empty object"),
    (b"{  \t }", "object", 0, "bo'sh obyekt", "an empty object"),
    (b'{"a": 1, "b": 2}', "object", 2, "2 a'zoli obyekt", "an object with 2 members"),
    (b'{"a": 1, "a": 2}', "object", 2, "2 a'zoli obyekt", "an object with 2 members"),
    (b'{"a": {"x": 1}, "b": [], "c": 3}', "object", 3, "3 a'zoli obyekt", "an object with 3 members"),
]


@pytest.mark.parametrize("bad, kind, members, uz, en", BAD_RECORDS)
@pytest.mark.parametrize("position", [0, 3, 7])
def test_rejected_record_shapes_save_nothing(make_doc, out_dir: Path, lang_uz, bad: bytes, kind: str,
                                             members: int | None, uz: str, en: str, position: int) -> None:
    recs = good_lines(8)
    recs[position] = bad
    doc = make_doc(lines(*recs))
    out = out_dir / "r.json"
    with pytest.raises(ExportError) as ei:
        export(doc, out, ExportOptions("json", as_keyed=True))
    e = ei.value
    assert e.key == "writer.error.not_single_member"
    assert (e.params["index"], e.params["kind"], e.params["members"], e.params["what"]) == (position, kind, members, uz)
    assert f"#{position} yozuv" in e.message and uz in e.message and "Hech narsa saqlanmadi" in e.message
    assert "--as-keyed" in e.hint and '{"kalit": qiymat}' in e.hint
    assert files_in(out_dir) == []
    # an existing output stays as it was
    out.write_bytes(b"old")
    with pytest.raises(ExportError):
        export(doc, out, ExportOptions("json", indent=None, sort_keys="alpha", as_keyed=True))
    assert out.read_bytes() == b"old" and files_in(out_dir) == ["r.json"]
    # English
    i18n.set_language("en")
    with pytest.raises(ExportError) as ei:
        export(doc, out, ExportOptions("json", as_keyed=True))
    assert f"record #{position} is not an object with exactly one member, it is {en}." in ei.value.message
    assert "Nothing was saved" in ei.value.message and '{"key": value}' in ei.value.hint


def test_rejection_with_every_check_path(make_doc, out_dir: Path, check_path: str) -> None:
    for bad, kind, members, _uz, _en in BAD_RECORDS:
        recs = good_lines(6)
        recs[4] = bad
        doc = make_doc(lines(*recs))
        with pytest.raises(ExportError) as ei:
            export(doc, out_dir / "r.json", ExportOptions("json", as_keyed=True))
        assert (ei.value.params["index"], ei.value.params["kind"], ei.value.params["members"]) == (4, kind, members)
        assert files_in(out_dir) == []


def test_first_offender_in_selection_order_and_record_numbers_of_the_file(make_doc, out_dir: Path) -> None:
    recs = good_lines(10)
    recs[2] = b"[2]"
    recs[7] = b'{"a": 1, "b": 2}'
    doc = make_doc(lines(*recs))
    with pytest.raises(ExportError) as ei:
        export(doc, out_dir / "r.json", ExportOptions("json", as_keyed=True), source=[9, 7, 5, 2])
    assert (ei.value.params["index"], ei.value.params["kind"]) == (7, "object")
    with pytest.raises(ExportError) as ei:
        export(doc, out_dir / "r.json", ExportOptions("json", as_keyed=True), source=range(1, 9))
    assert ei.value.params["index"] == 2
    # a selection without the bad records is fine
    got = codec.loads(out_bytes(doc, out_dir, source=[9, 5, 0], as_keyed=True))
    assert list(got) == ["k9", "k5", "k0"]
    assert files_in(out_dir) == ["o.json"]


def test_a_bad_record_in_a_later_block_saves_nothing(make_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                     check_path: str) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 96)   # a few records per block
    recs = good_lines(40)
    doc = make_doc(lines(*recs))
    want = b"{" + b",".join(r[1:-1] for r in recs) + b"}"
    assert out_bytes(doc, out_dir, indent=None, as_keyed=True) == want.replace(b" ", b"")
    recs[33] = b'{"k33": 1, "extra": 2}'
    doc = make_doc(lines(*recs))
    out = out_dir / "o.json"
    out.write_bytes(b"previous result")
    with pytest.raises(ExportError) as ei:
        export(doc, out, ExportOptions("json", as_keyed=True))
    assert (ei.value.params["index"], ei.value.params["members"]) == (33, 2)
    assert out.read_bytes() == b"previous result" and files_in(out_dir) == ["o.json"]


# ----------------------------------------------------------------------------------
# the one-step JSON Lines join and the general join
# ----------------------------------------------------------------------------------
def test_line_layouts_give_the_same_result(make_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                           check_path: str) -> None:
    recs = [b'{"a":{"x":0.1,"t":"x}\\n{y"}}', b'{"b":[1e400,{"c":"}"}]}', b'{"\\u0041":"\\u0041"}',
            b'{"Tosh\\"kent":-0.0}', '{"e":"Тошкент"}'.encode()]
    used: list[bool] = []
    real = writer._joined_members

    def spy(ctx: Any, blk: Any) -> Any:
        body, pieces = real(ctx, blk)
        used.append(pieces is None)
        return body, pieces

    monkeypatch.setattr(writer, "_joined_members", spy)
    clean = make_doc(lines(*recs))
    want = out_bytes(clean, out_dir, indent=2, as_keyed=True)
    assert used == [True]   # one-step join
    spaced = [r.replace(b":", b" : ", 1) for r in recs]
    variants = {
        "crlf": (lines(*recs, sep=b"\r\n"), False),
        "blank lines": (b"\n\n".join(recs) + b"\n\n\n", False),
        "spaces around": (b"\n".join(b"  " + r + b" \t" for r in recs) + b"\n", False),
        "spaces inside": (lines(*[b"{ " + r[1:-1] + b" }" for r in spaced]), True),
        "no last newline": (b"\n".join(recs), True),
        "broken lines": (b"\n".join([recs[0], b'{"broken": ', recs[1], recs[2], b"[1, 2", recs[3], recs[4]]) + b"\n",
                         False),
    }
    for name, (data, fast) in variants.items():
        used.clear()
        doc = make_doc(data)
        rep = export(doc, out_dir / "v.json", ExportOptions("json", indent=2, as_keyed=True))
        assert (out_dir / "v.json").read_bytes() == want, name
        assert rep.records == 5
        assert used == [fast], name
        if name == "broken lines":
            assert rep.warning_keys == ["writer.warn.broken_lines"]


def test_one_step_join_never_hides_a_bad_record(make_doc, out_dir: Path) -> None:
    """Records whose braces could pair up with neighbours after the join."""
    cases = [
        ([b'{"a":1}', b'{"b":2,"c":3}', b"{}"], 1, 2),
        ([b'{"a":1,"b":2}', b"{}"], 0, 2),
        ([b"{}", b'{"a":1,"b":2}'], 0, 0),
        ([b'{"a":1,"b":2}', b'{"c":3}', b"[]"], 0, 2),
        ([b'{"a":1}', b'"}"', b'{"b":2}'], 1, None),
        ([b'{"a":1}', b'"x"', b'{"b":2}'], 1, None),
        ([b'{"a":{"b":1}}', b'{"c":2}', b'{"d":{}}', b'{}'], 3, 0),
        ([b'{"a":1}', b"{}", b'{"b":2}'], 1, 0),
        ([b"{}"], 0, 0),
        ([b"[{}]"], 0, None),
    ]
    for recs, index, members in cases:
        doc = make_doc(lines(*recs))
        with pytest.raises(ExportError) as ei:
            export(doc, out_dir / "x.json", ExportOptions("json", as_keyed=True))
        assert (ei.value.params["index"], ei.value.params["members"]) == (index, members), recs
        assert files_in(out_dir) == []


# ----------------------------------------------------------------------------------
# huge records (larger than large_node_bytes: streamed)
# ----------------------------------------------------------------------------------
def _huge_lines() -> list[bytes]:
    return [b'{"small0": 1}', b'{"big1": {"text": "' + b"x" * 400 + b'", "n": [0.1, 1e400, -0.0]}}',
            b'{"small2": [1.10]}', b'{"big3": "' + "Тошкент ".encode() * 60 + b'"}', b'{"small4": null}']


@pytest.mark.parametrize("chunk", [7, 64, 1 << 20])
def test_huge_records_are_streamed_without_their_braces(make_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                        chunk: int) -> None:
    monkeypatch.setattr(writer, "CHUNK_BYTES", chunk)
    settings = Settings(large_node_bytes=200)
    recs = _huge_lines()
    jl = make_doc(lines(*recs), settings=settings)
    keyed = make_doc(b"{" + b",\n".join(r[1:-1] for r in recs) + b"}", name="k.json", settings=settings,
                     format="keyed")
    for indent in INDENTS:
        got = out_bytes(jl, out_dir, "got.json", indent=indent, as_keyed=True)
        assert got == out_bytes(keyed, out_dir, "want.json", indent=indent), (chunk, indent)
    rep = export(jl, out_dir / "s.json", ExportOptions("json", sort_keys=True, as_keyed=True))
    assert rep.warning_keys == ["writer.warn.huge_unsorted"] and rep.records == 5
    assert list(codec.loads((out_dir / "s.json").read_bytes())["big1"]) == ["text", "n"]   # unsorted


@pytest.mark.parametrize("bad, kind, members", [
    (b"[" + b"1, " * 100 + b"1]", "array", None),
    (b'"' + b"y" * 300 + b'"', "string", None),
    (b"{" + b" " * 300 + b"}", "object", 0),
    (b'{"a": "' + b"z" * 300 + b'", "b": 1}', "object", 2),
    (b'{"a": 1, "b": "' + b"z" * 300 + b'", "c": 2}', "object", 3),
    (b"{" + b",".join(b'"k%d": %d' % (i, i) for i in range(60)) + b"}", "object", 60),
])
@pytest.mark.parametrize("chunk", [7, 1 << 20])
def test_huge_bad_records_save_nothing(make_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, bad: bytes,
                                       kind: str, members: int | None, chunk: int) -> None:
    monkeypatch.setattr(writer, "CHUNK_BYTES", chunk)
    recs = _huge_lines()
    recs[2] = bad
    doc = make_doc(lines(*recs), settings=Settings(large_node_bytes=200))
    out = out_dir / "h.json"
    with pytest.raises(ExportError) as ei:
        export(doc, out, ExportOptions("json", as_keyed=True))
    assert (ei.value.params["index"], ei.value.params["kind"], ei.value.params["members"]) == (2, kind, members)
    assert files_in(out_dir) == []


# ----------------------------------------------------------------------------------
# repeated member keys
# ----------------------------------------------------------------------------------
def test_repeated_keys_are_written_and_counted(make_doc, out_dir: Path, lang_uz, check_path: str) -> None:
    doc = make_doc(lines(b'{"a": 1}', b'{"b": 2}', b'{"a": 3}', b'{"\\u0061": 4}', b'{"c": 5}', b'{"b": 6}',
                         b'{"\\"q": 7}', b'{"\\"q": 8}'))
    out = out_dir / "d.json"
    rep = export(doc, out, ExportOptions("json", indent=None, as_keyed=True))
    assert out.read_bytes() == b'{"a":1,"b":2,"a":3,"\\u0061":4,"c":5,"b":6,"\\"q":7,"\\"q":8}'
    assert rep.warning_keys == ["writer.warn.duplicate_keys"]
    text = rep.warnings[0]
    assert text.startswith("4 ta yozuvning kaliti") and '#2 yozuv, kalit "a"' in text
    assert "faqat birinchi" not in text


def test_repeated_key_warning_in_english_and_across_blocks(make_doc, out_dir: Path, lang_en,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 64)
    recs = good_lines(30)
    recs[25] = b'{"k3": "again"}'
    doc = make_doc(lines(*recs))
    rep = export(doc, out_dir / "e.json", ExportOptions("json", as_keyed=True))
    assert rep.warnings == ['1 record repeats the key of an earlier record (record #25, key "k3"): the result '
                            'object has a repeated key. It was written unchanged — JSON allows this, but many '
                            'programs keep only the last value of a repeated key.']
    recs[27] = b'{"k26": 0}'
    recs[28] = b'{"k0": 0}'
    rep = export(make_doc(lines(*recs)), out_dir / "e.json", ExportOptions("json", as_keyed=True))
    assert rep.warnings[0].startswith("3 records repeat the key of an earlier record (the first: record #25, key ")


def test_repeated_key_check_is_bounded(make_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, lang_en,
                                       check_path: str) -> None:
    monkeypatch.setattr(writer, "DUP_KEYS_MAX", 4)
    # a repeat after the first 4 keys is not seen: no warning
    doc = make_doc(lines(b'{"a":1}', b'{"b":1}', b'{"c":1}', b'{"d":1}', b'{"a":2}'))
    rep = export(doc, out_dir / "b.json", ExportOptions("json", as_keyed=True))
    assert rep.warnings == []
    # one inside them: counted, and the warning says that only 4 keys were checked
    doc = make_doc(lines(b'{"a":1}', b'{"a":2}', b'{"c":1}', b'{"d":1}', b'{"e":1}', b'{"a":3}'))
    rep = export(doc, out_dir / "b.json", ExportOptions("json", as_keyed=True))
    assert rep.warnings[0].startswith("1 record repeats") and rep.warnings[0].endswith(
        " To save memory, only the first 4 keys were checked.")
    # exactly 4 keys: everything was checked
    doc = make_doc(lines(b'{"a":1}', b'{"a":2}', b'{"c":1}', b'{"d":1}'))
    assert not export(doc, out_dir / "b.json", ExportOptions("json", as_keyed=True)).warnings[0].endswith("checked.")


def test_repeated_key_memory_budget(make_doc, out_dir: Path, lang_en) -> None:
    # memory_limit_bytes // 4 bytes for the remembered keys: room for 3 keys of DUP_KEY_OVERHEAD bytes
    settings = Settings(memory_limit_bytes=writer.DUP_MEMORY_DIVISOR * writer.DUP_KEY_OVERHEAD * 3)
    doc = make_doc(lines(b'{"x":1}', b'{"x":2}', b'{"y":1}', b'{"z":1}', b'{"x":3}'), settings=settings)
    rep = export(doc, out_dir / "m.json", ExportOptions("json", as_keyed=True))
    assert rep.warnings[0].startswith("1 record repeats") and "only the first 3 keys were checked" in rep.warnings[0]
    keys = writer._DupKeys(doc)
    assert keys.budget == writer.DUP_KEY_OVERHEAD * 3


def test_repeated_keys_of_huge_records(make_doc, out_dir: Path, monkeypatch: pytest.MonkeyPatch, lang_en) -> None:
    settings = Settings(large_node_bytes=100)
    big = b'"' + b"v" * 200 + b'"'
    doc = make_doc(lines(b'{"dup": 1}', b'{"dup": ' + big + b"}", b'{"x": 2}', b'{"x": ' + big + b"}"),
                   settings=settings)
    rep = export(doc, out_dir / "h.json", ExportOptions("json", as_keyed=True))
    assert rep.warnings[0].startswith('2 records repeat the key of an earlier record (the first: record #1, key "dup")')
    # a key that does not end in the first piece of a huge record is not checked (said in the warning)
    monkeypatch.setattr(writer, "CHUNK_BYTES", 7)
    doc = make_doc(lines(b'{"q":1}', b'{"q":2}', b'{"long-key-name": ' + big + b"}"), settings=settings)
    rep = export(doc, out_dir / "h.json", ExportOptions("json", as_keyed=True))
    assert rep.warnings[0].endswith("only the first 2 keys were checked.")


def test_keyed_source_is_a_normal_export(make_doc, out_dir: Path) -> None:
    """as_keyed on a keyed document changes nothing (no check, no repeated-key count)."""
    data = '{"a": {"x": [1, 0.1]}, "b": 1e400, "a": {"y": "Тошкент"}, "c": {"B": 1, "A": 2}}'.encode()
    doc = make_doc(data, name="k.json", format="keyed")
    for indent in INDENTS:
        for sort in SORTS:
            rep = export(doc, out_dir / "a.json", ExportOptions("json", indent=indent, sort_keys=sort, as_keyed=True))
            want = out_bytes(doc, out_dir, "n.json", indent=indent, sort_keys=sort)
            assert (out_dir / "a.json").read_bytes() == want
            assert rep.warnings == []
    huge = make_doc(keyed_source(12, seed=3, style="pretty2"), name="h.json", settings=Settings(large_node_bytes=150))
    for indent in INDENTS:
        assert out_bytes(huge, out_dir, "a.json", indent=indent, as_keyed=True) == \
            out_bytes(huge, out_dir, "n.json", indent=indent)


# ----------------------------------------------------------------------------------
# wrapped, filtered, sorted, partial sources
# ----------------------------------------------------------------------------------
def test_wrapped_sources_drop_the_wrapper(make_doc, out_dir: Path) -> None:
    arr = ('{"meta": {"v": 1}, "data": [{"k1": {"x": 0.1}}, {"k2": [1e400]}, {"\\u0041": "Тошкент"}],'
           ' "tail": {"n": 3}}').encode()
    doc = make_doc(arr, name="w.json", format="wrapped", record_path="data")
    assert doc.info.kind == "wrapped" and doc.record_count == 3
    for keep in (True, False):
        rep = export(doc, out_dir / "w.json", ExportOptions("json", indent=None, keep_wrapper=keep, as_keyed=True))
        assert (out_dir / "w.json").read_bytes() == '{"k1":{"x":0.1},"k2":[1e400],"\\u0041":"Тошкент"}'.encode()
        assert rep.warnings == []
    assert out_bytes(doc, out_dir, indent=None).startswith(b'{"meta":{"v":1},"data":[{"k1"')
    bad = make_doc(arr.replace(b'{"k2": [1e400]}', b"[1e400]"), name="wb.json", format="wrapped", record_path="data")
    with pytest.raises(ExportError) as ei:
        export(bad, out_dir / "x.json", ExportOptions("json", as_keyed=True))
    assert (ei.value.params["index"], ei.value.params["kind"]) == (1, "array")
    # an object as the record container: its members are the records already
    obj = b'{"meta": {"v": 1}, "data": {"k1": {"x": 0.1}, "k2": [1e400]}, "tail": 2}'
    doc = make_doc(obj, name="wo.json", format="wrapped", record_path="data")
    assert doc.member_records
    for indent in INDENTS:
        assert out_bytes(doc, out_dir, "a.json", indent=indent, as_keyed=True) == \
            out_bytes(doc, out_dir, "n.json", indent=indent, keep_wrapper=False)
    assert out_bytes(doc, out_dir, indent=None, as_keyed=True) == b'{"k1":{"x":0.1},"k2":[1e400]}'


def test_other_value_sources(make_doc, out_dir: Path) -> None:
    recs = [b'{"a": 1}', b'{"b": [0.1]}', b'{"c": {"d": null}}']
    want = b'{"a":1,"b":[0.1],"c":{"d":null}}'
    sources = {
        "array": (b"[" + b",\n ".join(recs) + b"]", "array"),
        "stream": (b"\n".join(recs[:1] + [b'{\n  "b": [0.1]\n}', recs[2]]), "stream"),
        "brace_list": (b"{" + b", ".join(recs) + b"}", "brace_list"),
    }
    for name, (data, fmt) in sources.items():
        doc = make_doc(data, name=f"{name}.json", format=fmt)
        assert doc.record_count == 3, name
        assert out_bytes(doc, out_dir, indent=None, as_keyed=True) == want, name
    single = make_doc(b'{"only": [1, 2]}', name="single.json", format="single")
    assert out_bytes(single, out_dir, indent=None, as_keyed=True) == b'{"only":[1,2]}'
    two = make_doc(b'{"a": 1, "b": 2}', name="single2.json", format="single")
    with pytest.raises(ExportError) as ei:
        export(two, out_dir / "x.json", ExportOptions("json", as_keyed=True))
    assert (ei.value.params["index"], ei.value.params["members"]) == (0, 2)


def test_partial_document_exports_the_records_before_the_error(make_doc, out_dir: Path) -> None:
    doc = make_doc(b'[{"a": 1}, {"b": 2}, {"c": ', name="cut.json", format="array")
    assert doc.error is not None and doc.record_count == 2
    rep = export(doc, out_dir / "p.json", ExportOptions("json", indent=None, as_keyed=True))
    assert (out_dir / "p.json").read_bytes() == b'{"a":1,"b":2}'
    assert rep.warning_keys == ["writer.warn.partial"]


def test_cancel_leaves_nothing_and_stops_the_helper_thread(make_doc, out_dir: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "BLOCK_BYTES", 128)
    doc = make_doc(lines(*good_lines(200)))
    token = CancelToken()

    def on_progress(info: Any) -> None:
        if info.done > 0:
            token.cancel()

    before = threading.active_count()
    with pytest.raises(Cancelled):
        export(doc, out_dir / "c.json", ExportOptions("json", as_keyed=True),
               progress=Progress(callback=on_progress, min_interval=0.0), cancel=token)
    assert files_in(out_dir) == []
    assert threading.active_count() == before


# ----------------------------------------------------------------------------------
# options
# ----------------------------------------------------------------------------------
def test_option_validation(lang_en) -> None:
    assert ExportOptions("json").as_keyed is False
    assert ExportOptions("JSON", as_keyed=True).as_keyed is True
    assert ExportOptions.from_settings("json", Settings(), as_keyed=True).as_keyed is True
    for fmt in ("jsonl", "csv", "xlsx", "sqlite"):
        with pytest.raises(UserInputError) as ei:
            ExportOptions(fmt, as_keyed=True)
        assert ei.value.key == "writer.invalid_option" and ei.value.params["name"] == "as_keyed"
        assert f"works only for the JSON format (the chosen format: {fmt})" in ei.value.message
        assert ExportOptions(fmt, as_keyed=False).as_keyed is False
    for value in ("yes", 1, None):
        with pytest.raises(UserInputError) as ei:
            ExportOptions("json", as_keyed=value)
        assert ei.value.params["name"] == "as_keyed"
    opts = ExportOptions("json", as_keyed=True)
    opts.fmt = "jsonl"
    with pytest.raises(UserInputError):
        opts.validate()


def test_export_validates_the_option_again(make_doc, out_dir: Path) -> None:
    doc = make_doc(lines(*good_lines(2)))
    opts = ExportOptions("jsonl")
    opts.as_keyed = True
    with pytest.raises(UserInputError):
        export(doc, out_dir / "x.jsonl", opts)
    assert files_in(out_dir) == []


# ----------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------
@pytest.fixture
def run(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JSONRESHAPE_LANG", "uz")
    monkeypatch.setattr(cli, "_peak_memory", lambda: None)
    old = i18n.get_language()

    def _run(*argv: Any) -> SimpleNamespace:
        code = cli.main([str(a) for a in argv])
        captured = capsys.readouterr()
        return SimpleNamespace(code=code, out=captured.out, err=captured.err)

    yield _run
    i18n.set_language(old)
    from jsonreshape import log as applog

    applog.shutdown_logging()


@pytest.fixture
def keyed_files(tmp_path: Path) -> tuple[Path, Path]:
    """(keyed.json, keyed.jsonl): a keyed document and its JSON Lines form."""
    k = tmp_path / "keyed.json"
    recs = [{"id": i, "name": f"n{i}", "v": codec.loads(t)} for i, t in
            enumerate(["0.1", "1e400", "12345678901234567890", "-0.0", "1.10", "7", "8", "9"])]
    keys = [f"u{i}" for i in range(len(recs))]
    keys[1] = "Тошкент"
    keys[2] = "Oʻzbekiston"
    jsongen.write_file(k, recs, "keyed", style="pretty2", keys=keys)
    jl = tmp_path / "keyed.jsonl"
    with Document.open(k) as doc:
        export(doc, jl, ExportOptions("jsonl"))
    return k, jl


def test_cli_convert_rebuilds_the_keyed_object(run, tmp_path: Path, keyed_files: tuple[Path, Path]) -> None:
    k, jl = keyed_files
    for extra in ([], ["--compact"], ["--indent", "4"], ["--indent", "tab"], ["--sort-keys", "codepoint"],
                  ["--no-wrapper"]):
        want, got = tmp_path / "want.json", tmp_path / "got.json"
        assert run("convert", k, want, *extra).code == 0
        r = run("convert", jl, got, "--as-keyed", *extra)
        assert r.code == 0, r.err
        assert got.read_bytes() == want.read_bytes(), extra
        assert "e'tiborga olinmadi" not in r.err   # --as-keyed is used, never ignored
    assert b"12345678901234567890" in got.read_bytes() and "Тошкент".encode() in got.read_bytes()


def test_cli_filter_and_sort_with_as_keyed(run, tmp_path: Path, keyed_files: tuple[Path, Path]) -> None:
    _, jl = keyed_files
    out = tmp_path / "f.json"
    r = run("filter", jl, out, "--where", "*.id >= 5 or *.name == 'n1'", "--as-keyed", "--compact")
    assert r.code == 0, r.err
    assert list(codec.loads(out.read_bytes())) == ["Тошкент", "u5", "u6", "u7"]
    r = run("sort", jl, out, "--by=-$index", "--as-keyed")
    assert r.code == 0, r.err
    data = codec.loads(out.read_bytes())
    assert list(data) == ["u7", "u6", "u5", "u4", "u3", "Oʻzbekiston", "Тошкент", "u0"]
    assert out.read_bytes().startswith(b'{\n  "u7": {\n    "id": 7,')
    r = run("convert", jl, out, "--where", "*.id < 3", "--sort-by=-$index", "--as-keyed", "--compact")
    assert r.code == 0, r.err
    assert out.read_bytes() == '{"Oʻzbekiston":{"id":2,"name":"n2","v":12345678901234567890},' \
                               '"Тошкент":{"id":1,"name":"n1","v":1e400},"u0":{"id":0,"name":"n0","v":0.1}}'.encode()
    r = run("filter", jl, out, "--where", "*.id > 100", "--as-keyed")
    assert r.code == 0, r.err
    assert out.read_bytes() == b"{}"


@pytest.mark.parametrize("args", [
    ["out.csv"], ["out.jsonl"], ["out.json", "--jsonl"], ["out.json", "--to", "jsonl"], ["out.xlsx"],
    ["out.sqlite"], ["out.json", "--to", "csv"],
])
def test_cli_rejects_as_keyed_for_other_formats(run, tmp_path: Path, keyed_files: tuple[Path, Path],
                                                args: list[str]) -> None:
    _, jl = keyed_files
    out = tmp_path / args[0]
    r = run("convert", jl, out, *args[1:], "--as-keyed")
    assert r.code == cli.EXIT_USER
    assert "--as-keyed faqat JSON natija uchun ishlaydi" in r.err
    assert not out.exists()
    r = run("--lang", "en", "filter", jl, out, "--where", "*.id > 1", *args[1:], "--as-keyed")
    assert r.code == cli.EXIT_USER and "--as-keyed works only for JSON output" in r.err
    assert not out.exists()


def test_cli_reports_a_record_that_is_not_a_one_member_object(run, tmp_path: Path) -> None:
    src = tmp_path / "mixed.jsonl"
    src.write_bytes(lines(b'{"a": 1}', b'{"b": 2}', b'{"c": 3, "d": 4}', b"[5]"))
    out = tmp_path / "m.json"
    out.write_bytes(b"old")
    r = run("convert", src, out, "--as-keyed")
    assert r.code == cli.EXIT_USER
    assert "#2 yozuv bitta a'zoli obyekt emas, u — 2 a'zoli obyekt" in r.err
    assert "--as-keyed" in r.err and out.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json", "mixed.jsonl"]
    # without the option the records are an array
    assert run("convert", src, out, "--compact").code == 0
    assert out.read_bytes() == b'[{"a":1},{"b":2},{"c":3,"d":4},[5]]'


def test_cli_prints_the_repeated_key_warning(run, tmp_path: Path) -> None:
    src = tmp_path / "dup.jsonl"
    src.write_bytes(lines(b'{"a": 1}', b'{"b": 2}', b'{"a": 3}'))
    out = tmp_path / "d.json"
    r = run("--lang", "en", "convert", src, out, "--as-keyed", "--compact")
    assert r.code == 0, r.err
    assert out.read_bytes() == b'{"a":1,"b":2,"a":3}'
    assert 'record #2, key "a"' in r.err


@pytest.mark.parametrize("command", ["filter", "sort", "convert"])
def test_cli_help_lists_the_option(run, command: str) -> None:
    r = run(command, "--help")
    assert r.code == 0
    assert "--as-keyed" in r.out and "bitta kalitli obyektni qayta yig'ish" in " ".join(r.out.split())
    r = run("--lang", "en", command, "--help")
    assert r.code == 0
    text = " ".join(r.out.split())
    assert "--as-keyed" in text and "rebuild one keyed object from records that are all one-member objects" in text
    assert "{{" not in text


def test_readme_and_architecture_describe_the_option() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    arch = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "--as-keyed" in readme and "jsonreshape convert keyed.jsonl keyed.json --as-keyed" in readme
    assert "opsiyasi kerakmi?" not in readme
    assert "as_keyed" in arch and "[--as-keyed]" in arch
