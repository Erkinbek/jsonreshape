"""jsonreshape.jsonpatch: reading patches and applying them (docs/ARCHITECTURE.md §12.8, import side).

* ``paths.parse_pointer`` (strict RFC 6901) and ``format_pointer``;
* ``iter_patch``: every source (path, bytes, file object, list), encodings (UTF-8 + BOM, UTF-16 / UTF-32 with and
  without BOM, gzip), RFC 6902 §4 validation with the operation number, exact numbers, duplicate keys
  (appendix A.13), unknown members (A.11), JSON errors with line/column;
* RFC 6902 appendix A (A.1-A.16) adapted to EVERY document kind (the target value is a record / the root),
  on both core backends; the rest of the document keeps every byte;
* record-level semantics (positions after every operation, ``-``, copies sharing their content, moves between
  records and levels), keyed records via the KeyIndex (rename in place, add onto an existing key, escaped
  keys ``~`` ``/`` ``""``), the wrapper and its protections, single documents, the record container;
* atomicity (a failing operation changes nothing and leaves no segment), ChangeSet / undo / redo, duplicate
  keys, huge records, the record cache spilling to the segment, array layers (> 10 000 records), progress
  and cancellation, exact numbers and Unicode.
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest

from edits_helpers import effective
from jsonpatch_helpers import canon, doc_value
from jsonreshape import codec, editops, jsonpatch, reader
from jsonreshape.edits import EditSession, Ref
from jsonreshape.errors import Cancelled, EditError, PathSyntaxError
from jsonreshape.jsonpatch import NO_VALUE, PatchError, apply_patch, iter_patch
from jsonreshape.overlay import ArrayLayer
from jsonreshape.paths import format_pointer, parse_pointer
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

KINDS = ("array", "jsonl", "stream", "brace_list", "keyed", "wrapped", "wrapped_obj", "single")


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    old_jp = jsonpatch.set_core_backend(name)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)
    jsonpatch.set_core_backend(old_jp)


@pytest.fixture
def closing():
    objs: list[Any] = []
    yield objs
    for obj in reversed(objs):
        try:
            obj.close()
        except Exception:
            pass


R0 = {"x": 0, "name": "Ali"}
R2 = {"x": 2, "name": "Тошкент"}


def build(kind: str, target: Any, *, pretty: bool = False) -> tuple[bytes, dict, tuple]:
    """``(file bytes, Document.open kwargs, pointer prefix of the target)`` of a document whose record (key
    ``t``, position 1) -- or root, for single documents -- is ``target``."""
    ind = 2 if pretty else None

    def enc(v: Any) -> bytes:
        return codec.dumps(v, indent=ind)

    recs = [enc(R0), enc(target), enc(R2)]
    if kind == "array":
        return b"[" + b",".join(recs) + b"]", {"format": "array"}, ("1",)
    if kind == "jsonl":
        return b"".join(codec.dumps(v) + b"\n" for v in (R0, target, R2)), {"format": "jsonl"}, ("1",)
    if kind == "stream":
        return b"\n".join(recs) + b"\n", {"format": "stream"}, ("1",)
    if kind == "brace_list":
        return b"{" + b",".join(recs) + b"}", {"format": "brace_list"}, ("1",)
    if kind == "keyed":
        return (b'{"r0":' + recs[0] + b',"t":' + recs[1] + b',"r2":' + recs[2] + b"}", {"format": "keyed"}, ("t",))
    if kind == "wrapped":
        return (b'{"meta":{"m":1},"data":[' + b",".join(recs) + b'],"tail":[1,2]}',
                {"format": "wrapped", "record_path": "data"}, ("data", "1"))
    if kind == "wrapped_obj":
        return (b'{"meta":{"m":1},"data":{"r0":' + recs[0] + b',"t":' + recs[1] + b',"r2":' + recs[2]
                + b'},"tail":[1,2]}', {"format": "wrapped", "record_path": "data"}, ("data", "t"))
    if kind == "single":
        return enc(target), {"format": "single"}, ()
    raise ValueError(kind)


def expected_full(kind: str, target: Any) -> Any:
    if kind in ("array", "jsonl", "stream", "brace_list"):
        return [R0, target, R2]
    if kind == "keyed":
        return {"r0": R0, "t": target, "r2": R2}
    if kind == "wrapped":
        return {"meta": {"m": 1}, "data": [R0, target, R2], "tail": [1, 2]}
    if kind == "wrapped_obj":
        return {"meta": {"m": 1}, "data": {"r0": R0, "t": target, "r2": R2}, "tail": [1, 2]}
    return target


def open_kind(tmp_path: Path, closing: list, kind: str, target: Any, *, pretty: bool = False,
              settings: Settings | None = None, name: str = "src.json") -> tuple[Document, EditSession, tuple]:
    data, kw, prefix = build(kind, target, pretty=pretty)
    path = tmp_path / name
    path.write_bytes(data)
    doc = Document.open(path, settings=settings, **kw)
    doc.build_index()
    assert doc.error is None
    closing.append(doc)
    s = EditSession.open(doc, settings=settings)
    closing.append(s)
    return doc, s, prefix


def ptr(prefix: tuple, pointer: str) -> str:
    return format_pointer(prefix + parse_pointer(pointer))


def prefixed(ops: list[dict], prefix: tuple) -> list[dict]:
    out = []
    for op in ops:
        op = dict(op)
        if "path" in op:
            op["path"] = ptr(prefix, op["path"])
        if "from" in op:
            op["from"] = ptr(prefix, op["from"])
        out.append(op)
    return out


def patch_bytes(ops: list[dict]) -> bytes:
    return codec.dumps(ops)


def fails(s: EditSession, doc: Document, source: Any, key: str) -> PatchError:
    before = effective(doc)
    gen = s.state.gen
    store = s.store
    files = sorted(p.name for p in store.dir.iterdir()) if store is not None and store.dir.exists() else []
    with pytest.raises(PatchError) as exc:
        s.import_patch(source)
    assert exc.value.key == key, (exc.value.key, exc.value.params)
    assert s.state.gen == gen and effective(doc) == before
    if s.store is not None and s.store.dir.exists():
        assert sorted(p.name for p in s.store.dir.iterdir() if p.name.startswith("seg-")) == \
            [n for n in files if n.startswith("seg-")]
    return exc.value


# ----------------------------------------------------------------------------------
# pointers (RFC 6901)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("text, segs", [
    ("", ()), ("/", ("",)), ("//", ("", "")), ("/foo", ("foo",)), ("/foo/0", ("foo", "0")),
    ("/a~1b", ("a/b",)), ("/m~0n", ("m~n",)), ("/~01", ("~1",)), ("/~10", ("/0",)), ("/ ", (" ",)),
    ("/c%d", ("c%d",)), ("/e^f", ("e^f",)), ("/k\"l", ("k\"l",)), ("/ a/b ", (" a", "b ")),
    ("/Тошкент/O'zbek", ("Тошкент", "O'zbek")),
])
def test_parse_pointer_rfc6901(text: str, segs: tuple) -> None:
    assert parse_pointer(text) == segs
    assert format_pointer(segs) == text


@pytest.mark.parametrize("text", ["a", " /a", "a/b", "/~", "/~2", "/a~", "#/a", "/a~1~"])
def test_parse_pointer_is_strict(text: str) -> None:
    with pytest.raises(PathSyntaxError):
        parse_pointer(text)
    with pytest.raises(TypeError):
        parse_pointer(5)   # type: ignore[arg-type]


def test_format_pointer_of_ints_and_lenient_parser_unchanged() -> None:
    from jsonreshape.paths import parse_path

    assert format_pointer(("data", 3, "a/b", "~")) == "/data/3/a~1b/~0"
    assert parse_path("  /a/b") == ("a", "b")          # the lenient parser still strips leading whitespace


# ----------------------------------------------------------------------------------
# iter_patch
# ----------------------------------------------------------------------------------
OPS = [{"op": "add", "path": "/a", "value": {"n": [0.1, 1e400]}}, {"op": "remove", "path": "/b"},
       {"op": "move", "from": "/c", "path": "/d"}, {"op": "test", "path": "/e", "value": None}]


def test_iter_patch_sources(tmp_path: Path) -> None:
    text = b'[{"op":"add","path":"/a","value":{"n":[0.1,1e400,12345678901234567890,-0.0,1.10,-0]}},\n' \
           b' {"op":"remove","path":"/b"}, {"op":"move","from":"/c","path":"/d"},{"op":"test","path":"/e","value":null}]'
    f = tmp_path / "p.json"
    f.write_bytes(text)
    for src in (text, f, str(f), io.BytesIO(text)):
        ops = list(iter_patch(src))
        assert [o.op for o in ops] == ["add", "remove", "move", "test"]
        assert [o.index for o in ops] == [0, 1, 2, 3]
        assert ops[0].segments == ("a",) and ops[2].from_segments == ("c",) and ops[2].from_ == "/c"
        assert ops[1].value is NO_VALUE and ops[3].value is None
        assert codec.dumps(ops[0].value) == b'{"n":[0.1,1e400,12345678901234567890,-0.0,1.10,-0]}'
        assert ops[0].size > 0
    listed = list(iter_patch([{"op": "replace", "path": "", "value": 1}]))
    assert listed[0].segments == () and listed[0].value == 1


@pytest.mark.parametrize("variant", ["bom", "utf-16-le", "utf-16-be", "utf-16-bom", "utf-32-le", "gzip", "gzip-utf16"])
def test_iter_patch_encodings(tmp_path: Path, variant: str) -> None:
    text = '[{"op": "add", "path": "/Тошкент~1x", "value": "O\'zbekiston ўзбек 😀"}]'
    if variant == "bom":
        raw = b"\xef\xbb\xbf" + text.encode("utf-8")
    elif variant == "utf-16-bom":
        raw = "﻿".encode("utf-16-le") + text.encode("utf-16-le")
    elif variant == "gzip":
        raw = gzip.compress(text.encode("utf-8"))
    elif variant == "gzip-utf16":
        raw = gzip.compress(b"\xff\xfe" + text.encode("utf-16-le"))
    else:
        raw = text.encode(variant)
    (tmp_path / "p.json").write_bytes(raw)
    ops = list(iter_patch(tmp_path / "p.json"))
    assert ops[0].segments == ("Тошкент/x",) and ops[0].value == "O'zbekiston ўзбек 😀"


def test_iter_patch_reads_big_files_in_pieces(tmp_path: Path) -> None:
    ops = [{"op": "test", "path": f"/{i}/name", "value": "x" * 300} for i in range(9000)]
    (tmp_path / "p.json").write_bytes(codec.dumps(ops, indent=2))
    assert (tmp_path / "p.json").stat().st_size > 3 * jsonpatch.READ_CHUNK
    p = Progress()
    got = list(iter_patch(tmp_path / "p.json", progress=p))
    assert len(got) == 9000 and got[-1].index == 8999 and got[-1].path == "/8999/name"
    assert p.done == (tmp_path / "p.json").stat().st_size


@pytest.mark.parametrize("text, key, params", [
    (b'{"op": "add"}', "patch.error.not_array", {}),
    (b'5', "patch.error.not_array", {}),
    (b'  \n ', "patch.error.empty", {}),
    (b'', "patch.error.empty", {}),
    (b'[1]', "patch.error.op_not_object", {"index": 0}),
    (b'[{"op":"test","path":"","value":1}, []]', "patch.error.op_not_object", {"index": 1}),
    (b'[{"path": "/a"}]', "patch.error.member_missing", {"index": 0, "member": "op"}),
    (b'[{"op": 1, "path": "/a"}]', "patch.error.member_type", {"member": "op"}),
    (b'[{"op": "frob", "path": "/a"}]', "patch.error.op_unknown", {"op": "frob"}),
    (b'[{"op": "remove"}]', "patch.error.member_missing", {"member": "path"}),
    (b'[{"op": "remove", "path": 5}]', "patch.error.member_type", {"member": "path"}),
    (b'[{"op": "remove", "path": "a"}]', "patch.error.pointer", {"pointer": "a", "member": "path"}),
    (b'[{"op": "remove", "path": "/a~2"}]', "patch.error.pointer", {"member": "path"}),
    (b'[{"op": "move", "path": "/a"}]', "patch.error.member_missing", {"member": "from"}),
    (b'[{"op": "copy", "path": "/a", "from": "x"}]', "patch.error.pointer", {"member": "from"}),
    (b'[{"op": "add", "path": "/a"}]', "patch.error.member_missing", {"member": "value"}),
    (b'[{"op": "test", "path": "/a"}]', "patch.error.member_missing", {"member": "value"}),
    (b'[{"op": "move", "from": "/a", "path": "/a/b"}]', "patch.error.move_into_self", {"pointer": "/a"}),
    (b'[{"op": "add", "path": "/baz", "value": "qux", "op": "remove"}]', "patch.error.op_duplicate_key",
     {"key": "op"}),
    (b'[{"op": "add", "path": "/baz", "value": {"a": 1, "a": 2}}]', "patch.error.op_duplicate_key", {"key": "a"}),
    (b'[{"op": "add", "path": "/a", "value": 1},\n {"op": "add", "path": "/b" "value": 2}]', "patch.error.json",
     {"line": 2, "column": 29}),
    (b'[{"op": "add", "path": "/a", "value": 1}] x', "patch.error.json", {"line": 1}),
    (b'[{"op": "add", "path": "/a", "value": 1}', "patch.error.json", {}),
    (b'[{"op": "add", "path": "/a", "value": NaN}]', "patch.error.json", {}),
])
def test_iter_patch_validation(text: bytes, key: str, params: dict) -> None:
    with pytest.raises(PatchError) as exc:
        list(iter_patch(text))
    assert exc.value.key == key
    for k, v in params.items():
        assert exc.value.params[k] == v, (k, exc.value.params)
    assert exc.value.message and "{" not in exc.value.message


def test_iter_patch_ignores_unknown_members_and_allows_from_equal_path() -> None:
    (op,) = iter_patch(b'[{"op": "add", "path": "/baz", "value": "qux", "xyz": 123, "from": 5}]')
    assert op.from_ is None and op.value == "qux"                 # A.11; "from" is not an add member
    (mv,) = iter_patch(b'[{"op": "move", "from": "/a/b", "path": "/a/b"}]')
    assert mv.from_segments == mv.segments == ("a", "b")


def test_iter_patch_yields_operations_before_an_error() -> None:
    it = iter_patch(b'[{"op":"remove","path":"/a"},{"op":"remove","path":"/b"},7]')
    assert next(it).path == "/a" and next(it).path == "/b"
    with pytest.raises(PatchError):
        next(it)


def test_iter_patch_encoding_errors(tmp_path: Path) -> None:
    (tmp_path / "bad16.json").write_bytes(b"\xff\xfe" + "[]".encode("utf-16-le") + b"\x00\xd8")
    with pytest.raises(PatchError) as exc:
        list(iter_patch(tmp_path / "bad16.json"))
    assert exc.value.key == "patch.error.encoding"
    (tmp_path / "bad.gz").write_bytes(gzip.compress(b'[{"op":"remove","path":"/a"}]' * 20)[:30])
    with pytest.raises(PatchError) as exc:
        list(iter_patch(tmp_path / "bad.gz"))
    assert exc.value.key == "patch.error.encoding"
    with pytest.raises(PatchError) as exc:
        list(iter_patch(b'[{"op": "add", "path": "/a", "value": "\xff"}]'))
    assert exc.value.key == "patch.error.json"


def test_missing_patch_file(tmp_path: Path) -> None:
    from jsonreshape.errors import JsonReshapeError

    with pytest.raises(JsonReshapeError) as exc:
        list(iter_patch(tmp_path / "nope.json"))
    assert exc.value.key == "error.file_not_found"
    with pytest.raises(TypeError):
        list(iter_patch(5))


# ----------------------------------------------------------------------------------
# RFC 6902 appendix A on every kind
# ----------------------------------------------------------------------------------
APPENDIX_A = [
    ("A.1", {"foo": "bar"}, [{"op": "add", "path": "/baz", "value": "qux"}], {"baz": "qux", "foo": "bar"}),
    ("A.2", {"foo": ["bar", "baz"]}, [{"op": "add", "path": "/foo/1", "value": "qux"}],
     {"foo": ["bar", "qux", "baz"]}),
    ("A.3", {"baz": "qux", "foo": "bar"}, [{"op": "remove", "path": "/baz"}], {"foo": "bar"}),
    ("A.4", {"foo": ["bar", "qux", "baz"]}, [{"op": "remove", "path": "/foo/1"}], {"foo": ["bar", "baz"]}),
    ("A.5", {"baz": "qux", "foo": "bar"}, [{"op": "replace", "path": "/baz", "value": "boo"}],
     {"baz": "boo", "foo": "bar"}),
    ("A.6", {"foo": {"bar": "baz", "waldo": "fred"}, "qux": {"corge": "grault"}},
     [{"op": "move", "from": "/foo/waldo", "path": "/qux/thud"}],
     {"foo": {"bar": "baz"}, "qux": {"corge": "grault", "thud": "fred"}}),
    ("A.7", {"foo": ["all", "grass", "cows", "eat"]}, [{"op": "move", "from": "/foo/1", "path": "/foo/3"}],
     {"foo": ["all", "cows", "eat", "grass"]}),
    ("A.8", {"baz": "qux", "foo": ["a", 2, "c"]},
     [{"op": "test", "path": "/baz", "value": "qux"}, {"op": "test", "path": "/foo/1", "value": 2}],
     {"baz": "qux", "foo": ["a", 2, "c"]}),
    ("A.10", {"foo": "bar"}, [{"op": "add", "path": "/child", "value": {"grandchild": {}}}],
     {"foo": "bar", "child": {"grandchild": {}}}),
    ("A.11", {"foo": "bar"}, [{"op": "add", "path": "/baz", "value": "qux", "xyz": 123}],
     {"foo": "bar", "baz": "qux"}),
    ("A.14", {"/": 9, "~1": 10}, [{"op": "test", "path": "/~01", "value": 10}], {"/": 9, "~1": 10}),
    ("A.16", {"foo": ["bar"]}, [{"op": "add", "path": "/foo/-", "value": ["abc", "def"]}],
     {"foo": ["bar", ["abc", "def"]]}),
]
APPENDIX_ERRORS = [
    ("A.9", {"baz": "qux"}, [{"op": "test", "path": "/baz", "value": "bar"}], "patch.error.test_failed"),
    ("A.12", {"foo": "bar"}, [{"op": "add", "path": "/baz/bat", "value": "qux"}], "patch.error.path_not_found"),
    ("A.15", {"/": 9, "~1": 10}, [{"op": "test", "path": "/~01", "value": "10"}], "patch.error.test_failed"),
]


@pytest.mark.parametrize("kind", KINDS)
def test_rfc6902_appendix_a(tmp_path: Path, closing: list, backend: str, kind: str) -> None:
    for n, (name, target, ops, want) in enumerate(APPENDIX_A):
        doc, s, prefix = open_kind(tmp_path, closing, kind, target, pretty=n % 2 == 1, name=f"{name}.json")
        before = dict(enumerate(effective(doc)[0]))
        cs = s.import_patch(patch_bytes(prefixed(ops, prefix)))
        assert canon(doc_value(doc)) == canon(expected_full(kind, want)), name
        after = effective(doc)[0]
        if kind != "single":                 # the other records keep every byte
            assert after[0] == before[0] and after[-1] == before[2]
        if name in ("A.8", "A.14"):
            assert cs.state is s.state and not s.dirty and cs.records == frozenset()
        else:
            assert cs.kind == "patch" and s.can_undo
            s.undo()
            assert canon(doc_value(doc)) == canon(expected_full(kind, target))
            s.redo()
            assert canon(doc_value(doc)) == canon(expected_full(kind, want))
    for name, target, ops, key in APPENDIX_ERRORS:
        doc, s, prefix = open_kind(tmp_path, closing, kind, target, name=f"{name}.json")
        fails(s, doc, patch_bytes(prefixed(ops, prefix)), key)
    doc, s, prefix = open_kind(tmp_path, closing, kind, {"foo": "bar"}, name="A13.json")
    p = prefix_text = format_pointer(prefix + ("baz",))
    fails(s, doc, b'[{"op": "add", "path": "' + p.encode() + b'", "value": "qux", "op": "remove"}]',
          "patch.error.op_duplicate_key")
    assert prefix_text


# ----------------------------------------------------------------------------------
# value documents: the record level
# ----------------------------------------------------------------------------------
def records_of(doc: Document) -> list[Any]:
    return [codec.loads(b) for _p, b in doc.pinned().iter_record_bytes()]


def write_array(tmp_path: Path, closing: list, values: list, *, kind: str = "array", name: str = "a.json",
                settings: Settings | None = None, record_path: str | None = None) -> tuple[Document, EditSession]:
    path = tmp_path / name
    if kind == "jsonl":
        path.write_bytes(b"".join(codec.dumps(v) + b"\n" for v in values))
        kw: dict = {"format": "jsonl"}
    elif kind == "wrapped":
        path.write_bytes(b'{"w": {"pre": [0, 1], "data": ' + codec.dumps(values) + b', "post": true}}')
        kw = {"format": "wrapped", "record_path": record_path or "w.data"}
    else:
        path.write_bytes(codec.dumps(values, indent=2))
        kw = {"format": "array"}
    doc = Document.open(path, settings=settings, **kw)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc, settings=settings)
    closing.append(s)
    return doc, s


def test_record_level_positions_follow_every_operation(tmp_path: Path, closing: list, backend: str) -> None:
    doc, s = write_array(tmp_path, closing, [{"id": i} for i in range(6)])
    ops = [
        {"op": "remove", "path": "/0"},                             # 1 2 3 4 5
        {"op": "add", "path": "/0", "value": {"id": "a"}},          # a 1 2 3 4 5
        {"op": "add", "path": "/-", "value": {"id": "z"}},          # a 1 2 3 4 5 z
        {"op": "move", "from": "/1", "path": "/5"},                  # a 2 3 4 5 1 z
        {"op": "copy", "from": "/2", "path": "/0"},                  # 3 a 2 3 4 5 1 z
        {"op": "replace", "path": "/0/id", "value": "c"},           # c a 2 3 4 5 1 z (the copy changed only)
        {"op": "move", "from": "/7", "path": "/-"},                  # unchanged ("z" stays last)
        {"op": "move", "from": "/2", "path": "/0/inner"},            # c{inner:2} a 3 4 5 1 z
        {"op": "move", "from": "/2/id", "path": "/-"},               # c a {} 4 5 1 z 3
        {"op": "test", "path": "/7", "value": 3},
        {"op": "test", "path": "/0", "value": {"id": "c", "inner": {"id": 2}}},
    ]
    cs = s.import_patch(patch_bytes(ops))
    assert records_of(doc) == [{"id": "c", "inner": {"id": 2}}, {"id": "a"}, {}, {"id": 4}, {"id": 5},
                               {"id": 1}, {"id": "z"}, 3]
    assert cs.remap is not None and cs.label.endswith("(11 ta amal)")
    # the untouched records keep their bytes; the copy of #3 is a record of its own
    assert doc.pinned().record_bytes(3) == doc.pinned(None).record_bytes(4)
    s.undo()
    assert records_of(doc) == [{"id": i} for i in range(6)]


def test_record_level_errors(tmp_path: Path, closing: list) -> None:
    doc, s = write_array(tmp_path, closing, [{"id": i} for i in range(3)])
    for op, key in [
        ({"op": "remove", "path": "/3"}, "patch.error.index_range"),
        ({"op": "add", "path": "/4", "value": 1}, "patch.error.index_range"),
        ({"op": "remove", "path": "/01"}, "patch.error.bad_index"),
        ({"op": "remove", "path": "/-1"}, "patch.error.bad_index"),
        ({"op": "remove", "path": "/x"}, "patch.error.bad_index"),
        ({"op": "remove", "path": "/-"}, "patch.error.dash"),
        ({"op": "replace", "path": "/-", "value": 1}, "patch.error.dash"),
        ({"op": "test", "path": "/-", "value": 1}, "patch.error.dash"),
        ({"op": "copy", "from": "/-", "path": "/0"}, "patch.error.dash"),
        ({"op": "move", "from": "/0", "path": "/3"}, "patch.error.index_range"),   # 3 > count - 1 after removal
        ({"op": "add", "path": "/0/id/x", "value": 1}, "patch.error.not_container"),
        ({"op": "add", "path": "/-/x", "value": 1}, "patch.error.dash"),
        ({"op": "add", "path": "/0/-/x", "value": 1}, "patch.error.path_not_found"),   # "-" is a key of an object
        ({"op": "remove", "path": ""}, "patch.error.root"),
        ({"op": "move", "from": "", "path": "/a"}, "patch.error.move_into_self"),
        ({"op": "replace", "path": "", "value": {"not": "an array"}}, "patch.error.container_type"),
        ({"op": "remove", "path": "/0/nope"}, "patch.error.path_not_found"),
    ]:
        fails(s, doc, patch_bytes([{"op": "test", "path": "/0/id", "value": 0}, op]), key)
    err = fails(s, doc, patch_bytes([{"op": "test", "path": "/0/id", "value": 0},
                                     {"op": "remove", "path": "/2/id/q"}]), "patch.error.path_not_found")
    assert err.params["index"] == 1 and err.params["pointer"] == "/2/id/q" and err.params["op"] == "remove"
    assert "#1" in err.message and err.hint


def test_root_of_array_documents(tmp_path: Path, closing: list) -> None:
    doc, s = write_array(tmp_path, closing, [{"id": i} for i in range(3)], kind="jsonl")
    s.import_patch(patch_bytes([{"op": "test", "path": "", "value": [{"id": 0}, {"id": 1.0}, {"id": 2}]},
                                {"op": "replace", "path": "", "value": [1, {"b": 2}]},
                                {"op": "add", "path": "/-", "value": "x"}]))
    assert records_of(doc) == [1, {"b": 2}, "x"]
    fails(s, doc, patch_bytes([{"op": "test", "path": "", "value": [1, {"b": 2}]}]), "patch.error.test_failed")
    s.import_patch(patch_bytes([{"op": "add", "path": "", "value": []}]))
    assert doc.record_count == 0
    s.undo()
    s.undo()
    assert records_of(doc) == [{"id": i} for i in range(3)]


def test_copies_share_their_content_and_change_on_their_own(tmp_path: Path, closing: list) -> None:
    doc, s = write_array(tmp_path, closing, [{"id": i, "v": [i]} for i in range(4)])
    s.import_patch(patch_bytes([{"op": "copy", "from": "/1", "path": "/-"}, {"op": "copy", "from": "/1", "path": "/0"}]))
    v = doc.pinned()
    assert v.record_span(0) == v.record_span(2) == doc.pinned(None).record_span(1)   # no new bytes
    s.import_patch(patch_bytes([{"op": "add", "path": "/0/v/-", "value": 9},
                                {"op": "copy", "from": "/0", "path": "/1"}]))
    assert records_of(doc) == [{"id": 1, "v": [1, 9]}, {"id": 1, "v": [1, 9]}, {"id": 0, "v": [0]},
                               {"id": 1, "v": [1]}, {"id": 2, "v": [2]}, {"id": 3, "v": [3]}, {"id": 1, "v": [1]}]


# ----------------------------------------------------------------------------------
# member records (keyed) through the KeyIndex
# ----------------------------------------------------------------------------------
def write_keyed(tmp_path: Path, closing: list, members: list[tuple[str, Any]], *, name: str = "k.json",
                settings: Settings | None = None, pretty: bool = False) -> tuple[Document, EditSession]:
    path = tmp_path / name
    sep = b",\n  " if pretty else b","
    path.write_bytes(b"{" + sep.join(codec.dumps(k) + (b": " if pretty else b":") + codec.dumps(v, indent=None)
                                      for k, v in members) + b"}")
    doc = Document.open(path, settings=settings, format="keyed")
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc, settings=settings)
    closing.append(s)
    return doc, s


def members_of(doc: Document) -> list[tuple[str, Any]]:
    out = []
    for _p, b in doc.pinned().iter_record_bytes():
        k, vo = reader.split_member(b)
        out.append((k, codec.loads(b[vo:])))
    return out


def test_keyed_records_by_key(tmp_path: Path, closing: list, backend: str) -> None:
    keys = ["a", "a/b", "m~n", "", "-", "Тошкент", "x"]
    doc, s = write_keyed(tmp_path, closing, [(k, {"i": i}) for i, k in enumerate(keys)], pretty=True)
    ops = [
        {"op": "replace", "path": "/a~1b/i", "value": 10},
        {"op": "add", "path": "/m~0n", "value": {"new": True}},     # add onto an existing record: replaced in place
        {"op": "remove", "path": "/"},                               # the empty key
        {"op": "add", "path": "/-/i", "value": 40},                  # "-" is an ordinary key of an object
        {"op": "move", "from": "/a", "path": "/renamed"},            # rename in place (the record keeps its place)
        {"op": "add", "path": "/zz", "value": [1, 2]},               # a new record: appended
        {"op": "move", "from": "/x", "path": "/Тошкент"},             # onto an existing key: x removed, value moves
        {"op": "copy", "from": "/zz", "path": "/zz2"},
        {"op": "test", "path": "/renamed", "value": {"i": 0}},
    ]
    cs = s.import_patch(patch_bytes(ops))
    assert members_of(doc) == [("renamed", {"i": 0}), ("a/b", {"i": 10}), ("m~n", {"new": True}), ("-", {"i": 40}),
                               ("Тошкент", {"i": 6}), ("zz", [1, 2]), ("zz2", [1, 2])]
    raw = [b for _p, b in doc.pinned().iter_record_bytes()]
    assert raw[0].startswith(b'"renamed": {') and raw[2].startswith(b'"m~n": {')   # the original heads' spacing
    step = s.store.step(s.store.head)
    assert sorted(step.keys) == sorted([(0, "a", "renamed"), (3, "", None), (6, "x", None), (7, None, "zz"),
                                        (8, None, "zz2")])
    assert cs.records == frozenset({0, 1, 2, 3, 4})
    # the KeyIndex follows the step: record-key commands and later patches find the new keys
    assert s.key_index.find("renamed") == 0 and s.key_index.find("a") is None and s.key_index.find("zz2") == 6
    s.import_patch(patch_bytes([{"op": "remove", "path": "/zz2"}, {"op": "test", "path": "/zz", "value": [1, 2]}]))
    assert doc.record_count == 6
    s.undo()
    s.undo()
    assert members_of(doc) == [(k, {"i": i}) for i, k in enumerate(keys)]


def test_keyed_errors_and_duplicate_record_keys(tmp_path: Path, closing: list) -> None:
    doc, s = write_keyed(tmp_path, closing, [("a", 1), ("b", {"c": 2}), ("dup", 1), ("dup", 2)])
    fails(s, doc, patch_bytes([{"op": "remove", "path": "/nope"}]), "patch.error.path_not_found")
    fails(s, doc, patch_bytes([{"op": "replace", "path": "/nope", "value": 1}]), "patch.error.path_not_found")
    fails(s, doc, patch_bytes([{"op": "move", "from": "/nope", "path": "/x"}]), "patch.error.path_not_found")
    err = fails(s, doc, patch_bytes([{"op": "replace", "path": "/dup", "value": 3}]), "patch.error.duplicate_keys")
    assert err.params["key"] == "dup"
    fails(s, doc, patch_bytes([{"op": "remove", "path": ""}]), "patch.error.root")
    fails(s, doc, patch_bytes([{"op": "add", "path": "", "value": [1]}]), "patch.error.container_type")
    # records elsewhere still work; the untouched duplicates stay as they are
    s.import_patch(patch_bytes([{"op": "add", "path": "/b/d", "value": 3}]))
    assert members_of(doc) == [("a", 1), ("b", {"c": 2, "d": 3}), ("dup", 1), ("dup", 2)]
    s.import_patch(patch_bytes([{"op": "test", "path": "", "value": {"a": 1, "b": {"c": 2, "d": 3}, "dup": 2}},
                                {"op": "replace", "path": "", "value": {"n1": 1, "n2": [2]}},
                                {"op": "move", "from": "/n1", "path": "/n0"}]))
    assert members_of(doc) == [("n2", [2]), ("n0", 1)] or members_of(doc) == [("n0", 1), ("n2", [2])]
    assert s.store.step(s.store.head).keys is None          # the container was replaced: the KeyIndex rebuilds
    assert s.key_index.find("n2") is not None and s.key_index.find("a") is None


def test_keyed_many_keys_use_the_key_index(tmp_path: Path, closing: list) -> None:
    n = 3000
    doc, s = write_keyed(tmp_path, closing, [(f"id{i}", {"v": i}) for i in range(n)])
    ops = [{"op": "replace", "path": f"/id{i}/v", "value": -i} for i in range(0, n, 7)]
    ops += [{"op": "move", "from": f"/id{i}", "path": f"/r{i}"} for i in range(1, n, 101)]
    s.import_patch(patch_bytes(ops))
    assert s.key_index_ready
    got = dict(members_of(doc))
    assert got["id7"] == {"v": -7} and got["r1"] == {"v": 1} and "id1" not in got and len(got) == n
    assert [k for k, _v in members_of(doc)][:3] == ["id0", "r1", "id2"]


# ----------------------------------------------------------------------------------
# wrapped documents: the wrapper and the record container
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("member", [False, True])
def test_wrapper_edits_and_protections(tmp_path: Path, closing: list, member: bool) -> None:
    target = {"t": 1}
    doc, s, prefix = open_kind(tmp_path, closing, "wrapped_obj" if member else "wrapped", target, pretty=True)
    head = "/data/t" if member else "/data/1"
    cs = s.import_patch(patch_bytes([
        {"op": "add", "path": "/meta/new", "value": [codec.loads("1.10"), "ўзбек"]},
        {"op": "move", "from": "/meta/m", "path": "/meta/m2"},
        {"op": "copy", "from": head, "path": "/tail/-"},
        {"op": "remove", "path": "/tail/0"},
        {"op": "add", "path": "/extra", "value": None},
        {"op": "test", "path": "/meta", "value": {"m2": 1, "new": [1.1, "ўзбек"]}},
    ]))
    assert cs.wrapper and s.state.wrapper is not None
    full = doc_value(doc)
    assert full["meta"] == {"m2": 1, "new": [codec.loads("1.10"), "ўзбек"]} and full["tail"] == [2, {"t": 1}]
    assert full["extra"] is None and list(full)[:2] == ["meta", "data"]
    assert codec.dumps(full["meta"]["new"]) == b'[1.10,"\xd1\x9e\xd0\xb7\xd0\xb1\xd0\xb5\xd0\xba"]'
    for op in [{"op": "remove", "path": "/data"}, {"op": "move", "from": "/data", "path": "/d2"},
               {"op": "move", "from": "/meta", "path": "/data"} if False else {"op": "remove", "path": ""},
               {"op": "replace", "path": "", "value": {}}, {"op": "add", "path": "", "value": {}}]:
        fails(s, doc, patch_bytes([op]), "patch.error.container_locked")
    fails(s, doc, patch_bytes([{"op": "replace", "path": "/data", "value": [] if member else {}}]),
          "patch.error.container_type")
    # the container itself: tested record by record, replaced as a whole
    want = dict(doc_value(doc))
    fails(s, doc, patch_bytes([{"op": "test", "path": "", "value": {**want, "extra": 0}}]), "patch.error.test_failed")
    s.import_patch(patch_bytes([{"op": "test", "path": "", "value": want},
                                {"op": "test", "path": "/data", "value": want["data"]}]))
    new = {"k1": {"a": 1}} if member else [{"a": 1}]
    s.import_patch(patch_bytes([{"op": "replace", "path": "/data", "value": new},
                                {"op": "copy", "from": "/data", "path": "/meta/snapshot"}]))
    assert doc_value(doc)["data"] == new and doc_value(doc)["meta"]["snapshot"] == new
    while s.can_undo:
        s.undo()
    assert canon(doc_value(doc)) == canon(expected_full("wrapped_obj" if member else "wrapped", target))


def test_wrapper_record_path_through_an_array(tmp_path: Path, closing: list) -> None:
    path = tmp_path / "w.json"
    path.write_bytes(b'{"list": [{"a": 0}, {"data": [{"id": 1}, {"id": 2}]}, {"z": 9}], "k": 1}')
    doc = Document.open(path, format="wrapped", record_path="/list/1/data")
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    assert doc.record_path == ("list", 1, "data")
    for op in [{"op": "add", "path": "/list/0", "value": 1}, {"op": "add", "path": "/list/1", "value": 1},
               {"op": "remove", "path": "/list/0"}, {"op": "remove", "path": "/list/1"},
               {"op": "move", "from": "/list/2", "path": "/list/0"}, {"op": "replace", "path": "/list/1", "value": 1}]:
        fails(s, doc, patch_bytes([op]), "patch.error.container_locked")
    s.import_patch(patch_bytes([{"op": "replace", "path": "/list/0", "value": {"a": 5}},
                                {"op": "add", "path": "/list/2", "value": "after"},
                                {"op": "remove", "path": "/list/3"},
                                {"op": "add", "path": "/list/-", "value": "end"},
                                {"op": "add", "path": "/list/1/data/0/x", "value": True},
                                {"op": "add", "path": "/list/1/other", "value": 1}]))
    assert doc_value(doc) == {"list": [{"a": 5}, {"data": [{"id": 1, "x": True}, {"id": 2}], "other": 1}, "after",
                                       "end"], "k": 1}


# ----------------------------------------------------------------------------------
# single documents
# ----------------------------------------------------------------------------------
def test_single_documents(tmp_path: Path, closing: list, backend: str) -> None:
    doc, s, _p = open_kind(tmp_path, closing, "single", {"a": {"b": 1, "c": [1, 2]}, "d": "x"}, pretty=True)
    s.import_patch(patch_bytes([{"op": "move", "from": "/a/b", "path": "/a/b2"},
                                {"op": "move", "from": "/d", "path": "/a/c/0"},
                                {"op": "test", "path": "", "value": {"a": {"b2": 1, "c": ["x", 1, 2]}}}]))
    assert list(doc_value(doc)["a"]) == ["b2", "c"]          # renamed in place
    s.import_patch(patch_bytes([{"op": "replace", "path": "", "value": [1, 2]}, {"op": "add", "path": "/-", "value": 3}]))
    assert doc_value(doc) == [1, 2, 3]
    s.import_patch(patch_bytes([{"op": "add", "path": "", "value": "scalar"}]))
    assert doc_value(doc) == "scalar"
    fails(s, doc, patch_bytes([{"op": "remove", "path": ""}]), "patch.error.root")
    fails(s, doc, patch_bytes([{"op": "add", "path": "/x", "value": 1}]), "patch.error.not_container")
    s.import_patch(patch_bytes([{"op": "copy", "from": "", "path": ""}]))
    assert doc_value(doc) == "scalar"


# ----------------------------------------------------------------------------------
# exact numbers, Unicode, escapes
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "keyed", "jsonl"])
def test_exact_numbers_and_unicode(tmp_path: Path, closing: list, kind: str) -> None:
    doc, s, prefix = open_kind(tmp_path, closing, kind, {"n": 1})
    text = ('[{"op": "add", "path": "' + ptr(prefix, "/vals") + '", "value": [0.1, 1e400, 12345678901234567890, '
            '-0.0, 1.10, -0, 1E5, "O\'zbekiston", "Oʻzbekiston", "Ўзбекистон", "\\ud800", "a\\"b\\\\c\\n", '
            '"\\u00e9"]}, {"op": "test", "path": "' + ptr(prefix, "/vals/1") + '", "value": 1E+400}, '
            '{"op": "test", "path": "' + ptr(prefix, "/vals/4") + '", "value": 1.1}, '
            '{"op": "test", "path": "' + ptr(prefix, "/vals/0") + '", "value": 0.10}]')
    s.import_patch(text.encode("utf-8"))
    raw = doc.pinned().record_bytes(1)
    for tok in (b"0.1", b"1e400", b"12345678901234567890", b"-0.0", b"1.10", b"-0", b"1E5"):
        assert tok in raw
    vals = doc_value(doc)[("t" if kind == "keyed" else 1)]["vals"]
    assert [codec.number_text(v) for v in vals[:7]] == ["0.1", "1e400", "12345678901234567890", "-0.0", "1.10", "-0",
                                                        "1E5"]
    assert vals[7:] == ["O'zbekiston", "Oʻzbekiston", "Ўзбекистон", "\ud800", 'a"b\\c\n', "é"]
    fails(s, doc, patch_bytes([{"op": "test", "path": ptr(prefix, "/vals/0"), "value": 0.11}]),
          "patch.error.test_failed")
    fails(s, doc, patch_bytes([{"op": "test", "path": ptr(prefix, "/vals/7"), "value": "O’zbekiston"}]),
          "patch.error.test_failed")


# ----------------------------------------------------------------------------------
# atomicity, ChangeSets, duplicate keys, huge records
# ----------------------------------------------------------------------------------
def test_a_failing_operation_changes_nothing(tmp_path: Path, closing: list) -> None:
    doc, s = write_array(tmp_path, closing, [{"id": i, "s": "x" * 50} for i in range(50)])
    s.set_value(Ref(0, ("id",)), "100")                      # a layer exists already
    history = s.history()
    ops = [{"op": "replace", "path": f"/{i}/s", "value": "y" * 100} for i in range(50)]
    ops += [{"op": "move", "from": "/0", "path": "/-"}, {"op": "test", "path": "/0/id", "value": "nope"}]
    fails(s, doc, patch_bytes(ops), "patch.error.test_failed")
    assert s.history() == history and s.undo_label
    assert sorted(p.name for p in s.store.dir.iterdir() if p.name.startswith("seg-")) == ["seg-00000.dat"]


def test_changeset_undo_redo(tmp_path: Path, closing: list) -> None:
    doc, s = write_array(tmp_path, closing, [{"id": i} for i in range(5)])
    before = effective(doc)
    cs = s.import_patch(patch_bytes([{"op": "replace", "path": "/3/id", "value": 30},
                                     {"op": "remove", "path": "/0"}, {"op": "add", "path": "/1", "value": {"n": 1}}]))
    assert cs.kind == "patch" and cs.records == frozenset({3}) and cs.select is None and cs.nodes == ()
    assert [cs.remap.map(p) for p in range(5)] == [None, 0, 2, 3, 4]
    after = effective(doc)
    u = s.undo()
    assert u.kind == "undo" and effective(doc) == before and u.records == frozenset({3})
    assert [u.remap.map(p) for p in range(5)] == [1, None, 2, 3, 4]
    r = s.redo()
    assert effective(doc) == after and r.records == cs.records
    assert s.store.step(s.store.head).label_params == {"count": 3}


def test_duplicate_keys_inside_records(tmp_path: Path, closing: list) -> None:
    path = tmp_path / "d.json"
    path.write_bytes(b'[{"a": 1, "a": 2}, {"b": {"x": 1, "\\u0078": 2}}, {"ok": 1}]')
    doc = Document.open(path)
    doc.build_index()
    closing.append(doc)
    s = EditSession.open(doc)
    closing.append(s)
    err = fails(s, doc, patch_bytes([{"op": "replace", "path": "/0/a", "value": 3}]), "patch.error.duplicate_keys")
    assert err.params["key"] == "a" and err.params["pointer"] == "/0"
    err = fails(s, doc, patch_bytes([{"op": "test", "path": "/1/b", "value": {"x": 2}}]), "patch.error.duplicate_keys")
    assert err.params["key"] == "x"
    s.import_patch(patch_bytes([{"op": "add", "path": "/2/n", "value": 1}, {"op": "move", "from": "/0", "path": "/-"},
                                {"op": "copy", "from": "/0", "path": "/0"}, {"op": "remove", "path": "/1"}]))
    raw = [b for _p, b in doc.pinned().iter_record_bytes()]
    assert raw == [b'{"b": {"x": 1, "\\u0078": 2}}', b'{"ok": 1, "n": 1}', b'{"a": 1, "a": 2}']


def test_huge_records(tmp_path: Path, closing: list) -> None:
    settings = Settings(large_node_bytes=200)
    doc, s = write_array(tmp_path, closing, [{"big": "x" * 300}, {"small": 1}, {"big": "y" * 300}], settings=settings)
    for op in [{"op": "replace", "path": "/0/big", "value": 1}, {"op": "test", "path": "/0", "value": 1},
               {"op": "add", "path": "/1/s", "value": "z" * 300}]:
        err = fails(s, doc, patch_bytes([op]), "patch.error.huge_record")
        assert err.params["limit"]
    s.import_patch(patch_bytes([{"op": "move", "from": "/0", "path": "/-"}, {"op": "copy", "from": "/1", "path": "/0"},
                                {"op": "remove", "path": "/2"}, {"op": "replace", "path": "/1", "value": {"n": 1}}]))
    got = [b for _p, b in doc.pinned().iter_record_bytes(include_huge=True)]
    assert got[0] is None and got[2] is None and codec.loads(got[1]) == {"n": 1}
    v = doc.pinned()
    assert v.record_span(0) == doc.pinned(None).record_span(2) and v.record_span(2) == doc.pinned(None).record_span(0)


def test_record_cache_spills_to_the_segment(tmp_path: Path, closing: list, monkeypatch, backend: str) -> None:
    monkeypatch.setattr(jsonpatch, "LRU_MIN", 0)
    settings = Settings(memory_limit_bytes=40_000)              # a record cache of ~10 KB: a few records
    doc, s = write_array(tmp_path, closing, [{"id": i, "s": "v" * 100} for i in range(300)], settings=settings)
    ops = []
    for rnd in range(3):                                       # the same records three times
        ops += [{"op": "add", "path": f"/{i}/r{rnd}", "value": [rnd, i]} for i in range(0, 300, 3)]
    ops.append({"op": "move", "from": "/0", "path": "/-"})
    ops.append({"op": "test", "path": "/299", "value": {"id": 0, "s": "v" * 100, "r0": [0, 0], "r1": [1, 0],
                                                         "r2": [2, 0]}})
    writes: list[int] = []
    real = jsonpatch._Applier._write_rec

    def spy(self: Any, rid: int, r: Any) -> None:
        writes.append(rid)
        real(self, rid, r)

    monkeypatch.setattr(jsonpatch._Applier, "_write_rec", spy)
    s.import_patch(patch_bytes(ops))
    assert len(writes) > len(set(writes)) == 100     # records left the cache and were re-read from the segment
    model = [{"id": i, "s": "v" * 100} for i in range(300)]
    for rnd in range(3):
        for i in range(0, 300, 3):
            model[i][f"r{rnd}"] = [rnd, i]
    model.append(model.pop(0))
    assert records_of(doc) == model


def test_many_changed_records_make_an_array_layer(tmp_path: Path, closing: list, monkeypatch) -> None:
    monkeypatch.setattr(jsonpatch, "RECORDS_MAX", 20)
    doc, s = write_array(tmp_path, closing, [{"id": i} for i in range(60)], kind="jsonl")
    cs = s.import_patch(patch_bytes([{"op": "replace", "path": f"/{i}/id", "value": i * 10} for i in range(0, 60, 2)]
                                    + [{"op": "copy", "from": "/1", "path": "/-"}]))
    assert isinstance(s.state.layers[-2], ArrayLayer) and cs.records is None
    assert records_of(doc)[:4] == [{"id": 0}, {"id": 1}, {"id": 20}, {"id": 3}] and records_of(doc)[-1] == {"id": 1}
    s.undo()
    assert records_of(doc) == [{"id": i} for i in range(60)] and len(s.state.layers) == 1
    s.redo()
    assert records_of(doc)[2] == {"id": 20}


def test_progress_and_cancel(tmp_path: Path, closing: list) -> None:
    doc, s = write_array(tmp_path, closing, [{"id": i} for i in range(20)])
    text = patch_bytes([{"op": "replace", "path": f"/{i}/id", "value": -i} for i in range(20)])
    p = Progress()
    s.import_patch(text, progress=p)
    assert p.total == 2 * len(text) and p.finished
    s.undo()
    token = CancelToken()
    token.cancel()
    before = effective(doc)
    with pytest.raises(Cancelled):
        s.import_patch(text, cancel=token)
    assert effective(doc) == before
    with pytest.raises(Cancelled):
        apply_patch(s, [{"op": "replace", "path": "/0/id", "value": 1}], cancel=token)


def test_stream_sources(tmp_path: Path, closing: list, monkeypatch) -> None:
    class Stream(io.RawIOBase):
        """A non-seekable binary stream."""

        def __init__(self, data: bytes) -> None:
            self.buf = io.BytesIO(data)

        def readable(self) -> bool:
            return True

        def seekable(self) -> bool:
            return False

        def readinto(self, b: Any) -> int:
            chunk = self.buf.read(len(b))
            b[:len(chunk)] = chunk
            return len(chunk)

    monkeypatch.setattr(jsonpatch, "KEEP_OPS_BYTES", 0)      # never kept because of the size
    doc, s = write_array(tmp_path, closing, [{"id": i} for i in range(3)])
    s.import_patch(Stream(patch_bytes([{"op": "remove", "path": "/0"}])))          # kept: read only once
    head = io.BytesIO(b"junk" + patch_bytes([{"op": "replace", "path": "/0/id", "value": 7}]))
    head.seek(4)
    s.import_patch(head)                                       # read twice from its starting position
    assert records_of(doc) == [{"id": 7}, {"id": 2}]


def test_list_sources_and_stats(tmp_path: Path, closing: list) -> None:
    doc, s = write_array(tmp_path, closing, [{"id": i} for i in range(3)])
    stats: dict = {}
    apply_patch(s, [{"op": "replace", "path": "/0/id", "value": 0.5}, {"op": "copy", "from": "/1", "path": "/-"}],
                stats=stats)
    assert stats == {"ops": 2, "records": 2, "changed": True}
    stats.clear()
    apply_patch(s, [{"op": "test", "path": "/0/id", "value": 0.5}], stats=stats)
    assert stats == {"ops": 1, "records": 0, "changed": False}


def test_closed_session_and_lock(tmp_path: Path, closing: list) -> None:
    doc, s = write_array(tmp_path, closing, [1, 2])
    s.close()
    with pytest.raises(EditError) as exc:
        apply_patch(s, [{"op": "remove", "path": "/0"}])
    assert exc.value.key == "edits.error.closed"


def test_working_order_against_a_list() -> None:
    """The applier's mutable record order (blocks of rid runs + a Fenwick tree) behaves like a list."""
    import random

    from jsonreshape.overlay import PieceTable

    for seed in range(40):
        rng = random.Random(seed)
        n = rng.choice([0, 1, 5, 300, 2000])
        base = PieceTable.identity(n)
        if n > 10 and seed % 2:
            base = base.move([1, 2, 7], 0).delete(4, 3)
        model = [base.rid_at(i) for i in range(base.count)]
        order = jsonpatch._Order.of(base)
        nxt = n + 100
        for step in range(600):
            kind = rng.random()
            if kind < 0.45 or not model:
                pos = rng.choice([0, len(model), rng.randint(0, len(model))])
                order.insert(pos, nxt)
                model.insert(pos, nxt)
                nxt += rng.choice([1, 1, 1, 2])
            elif kind < 0.8:
                pos = rng.randrange(len(model))
                assert order.delete(pos) == model.pop(pos)
            else:
                i = rng.randrange(len(model))
                j = rng.randint(0, len(model) - 1)
                order.insert(j, order.delete(i))
                model.insert(j, model.pop(i))
            assert order.count == len(model)
            if model and step % 37 == 0:
                for pos in {0, len(model) - 1, rng.randrange(len(model))}:
                    assert order.rid_at(pos) == model[pos]
        assert [r + k for r, c in order.runs() for k in range(c)] == model
        table = order.table()
        assert table.count == len(model) and [table.rid_at(i) for i in range(table.count)] == model
