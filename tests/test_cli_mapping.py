"""``jsonreshape apply`` and ``jsonreshape flatten`` (docs/ARCHITECTURE.md §13.4; AGENT_PROMPT §13, §14 test 2).

In-process (``cli.main``) and as subprocesses (``python -m jsonreshape``, every core backend):

* ``apply`` over every document kind (array, jsonl, stream, brace_list, wrapped, keyed, single) gives exactly the
  bytes of the in-memory oracle (``CompiledMapping.transform_value`` of the whole value in the engine's layout),
  JSON and JSON Lines (``--jsonl`` or an OUTPUT named ``.jsonl`` / ``.ndjson``), every indent option and the
  "export indent" setting; exact numbers (``0.1``, ``1e400``, ``12345678901234567890``, ``-0.0``, ``1.10``) and
  Unicode through UTF-8 / BOM / CRLF / UTF-16 / gzip inputs; ``--skip-huge`` with a tiny ``large_node_bytes``;
  every validation error of a mapping file (exit 1 with the JSON pointer of the bad node, nothing written); kind
  mismatches (with the ``--format`` hint); the record path stored in a mapping (used, overridden, missing);
  OUTPUT checks (INPUT, the mapping itself, other formats, ``.gz``); JSON errors (exit 2, ``--allow-partial``);
  cancellation (exit 3, nothing left behind) and ``--workers``;
* ``flatten`` in both modes, AGENT_PROMPT test 2 (``children`` -> child1, child1.2, child1.2.1, child1.2.3, child2
  with ``_depth`` 1, 2, 3, 3, 1 and no ``children`` left inside), every document kind, ``--depth-field`` (rename;
  a name the data uses gets ``_2`` with a warning, the default too), a wrong ``--children-key`` (exit 1 with the
  reason), competing recursive arrays in ``--mode records``, ``--sample N`` vs ``--full-scan``, exact numbers,
  Uzbek/Cyrillic keys, empty inputs, cancellation, progress and the help texts (Uzbek and English).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

import jsongen
from jsonreshape import cli, codec, i18n
from jsonreshape.engine import MISSING, CompiledMapping
from jsonreshape.mapping import Designer, MappingTree, infer_tree, make_input_info, save_mapping
from jsonreshape.reader import Document
from jsonreshape.tasks import CancelToken

ROOT = Path(__file__).resolve().parents[1]
N = codec.loads                      # exact number values (the codec keeps their tokens)
RECORD_KINDS = ("array", "jsonl", "stream", "brace_list")
KINDS = RECORD_KINDS + ("wrapped", "keyed", "single")
EXACT_TOKENS = (b"0.1", b"1e400", b"12345678901234567890", b"-0.0", b"1.10")
OKINA = chr(0x02BB)                  # the Uzbek apostrophe (o'g'il -> oʻgʻil)
CYR = "".join(map(chr, (0x0431, 0x043E, 0x043B, 0x0430, 0x043B, 0x0430, 0x0440)))   # "bolalar" in Cyrillic


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _cli_isolation(monkeypatch: pytest.MonkeyPatch):
    """main() changes process state on purpose (language, JSONRESHAPE_BACKEND, logging, signal handlers): put
    everything back after each test; the memory check of a whole pytest process is off here."""
    old_backend = os.environ.get("JSONRESHAPE_BACKEND")
    monkeypatch.setenv("JSONRESHAPE_LANG", "uz")
    monkeypatch.delenv("NO_COLOR", raising=False)
    old_lang = i18n.get_language()
    i18n.set_language("uz")
    root = logging.getLogger()
    old_level = root.level
    old_sigint = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(cli, "_peak_memory", lambda: None)
    yield
    if old_backend is None:
        os.environ.pop("JSONRESHAPE_BACKEND", None)
    else:
        os.environ["JSONRESHAPE_BACKEND"] = old_backend
    i18n.set_language(old_lang)
    from jsonreshape import log as applog

    applog.shutdown_logging()
    root.setLevel(old_level)
    assert signal.getsignal(signal.SIGINT) == old_sigint


@dataclass
class Result:
    code: int
    out: str
    err: str


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]):
    def _run(*argv: Any, cancel: CancelToken | None = None) -> Result:
        code = cli.main([str(a) for a in argv], cancel=cancel)
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return _run


@pytest.fixture
def opened():
    """Documents opened by a test (closed at the end)."""
    docs: list[Document] = []

    def _open(path: Path, **kw: Any) -> Document:
        doc = Document.open(path, **kw)
        doc.build_index()
        docs.append(doc)
        return doc

    yield _open
    for d in docs:
        d.close()


def proc(*argv: Any, timeout: float = 240) -> subprocess.CompletedProcess:
    """``python -m jsonreshape ARGV`` (JSONRESHAPE_HOME and the language of this test are inherited)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "jsonreshape", *map(str, argv)], cwd=ROOT, env=env,
                          capture_output=True, timeout=timeout)


def listing(p: Path) -> list[str]:
    return sorted(x.name for x in p.iterdir())


def set_settings(**kw: Any) -> None:
    from jsonreshape import settings

    settings.set_settings(settings.Settings(**kw))


def people() -> list[dict[str, Any]]:
    """Records with nested objects, arrays, exact numbers, Uzbek Latin/Cyrillic text and escapes."""
    return [
        {"id": 1, "name": "Anvar", "score": N("1.10"), "contact": {"phone": "+998901",
                                                                   "address": {"city": "Toshkent", "zip": 100000}},
         "orders": [{"oid": 7, "sum": N("0.1")}, {"oid": 8, "sum": N("1e400")}]},
        {"id": 2, "name": "Тошкент", "score": N("-0.0"), "contact": {"phone": "+998902",
                                                                     "address": {"city": "Samarqand"}},
         "orders": []},
        {"id": 3, "name": "O'zbekiston", "score": N("12345678901234567890")},
        {"id": 4, "name": "G" + OKINA + "ulom \"qo'shtirnoq\" \\   \u0001", "contact": {"address": {"city": None}},
         "orders": [{"oid": 9, "sum": 5}]},
    ]


META = {"version": 3, "source": "test"}


def write_input(tmp_path: Path, kind: str, recs: list, *, name: str | None = None, **kw: Any) -> Path:
    """A document of ``kind`` holding ``recs`` (single: ``{"meta": ..., "items": recs}``, read with
    ``--format single``; wrapped: jsongen's ``{"meta", "data": [...], "tail"}``)."""
    p = tmp_path / (name or f"in_{kind}.{'jsonl' if kind == 'jsonl' else 'json'}")
    if kind == "single":
        p.write_bytes(codec.dumps({"meta": META, "items": recs}, indent=2))
    else:
        jsongen.write_file(p, recs, kind, style="compact" if kind == "jsonl" else "pretty2", **kw)
    return p


def open_args(kind: str) -> list[str]:
    return ["--format", "single"] if kind == "single" else []


def values_of(doc: Document) -> list:
    return [doc.record(i) for i in range(doc.record_count)]


def whole_of(doc: Document) -> Any:
    return codec.loads(Path(doc.source_path).read_bytes())


def restructured(doc: Document) -> MappingTree:
    """A mapping with a move, a gather, a rename (and a broadcast of a wrapper/document field) for any kind."""
    vals = values_of(doc)
    kind = doc.kind
    if kind == "wrapped":
        rp = tuple(doc.record_path)
        d = Designer(infer_tree(vals, root_kind="wrapped", wrapper=whole_of(doc), record_path=rp),
                     population=lambda: iter(vals), wrapper=whole_of(doc), record_path=rp, undo_limit=500)
        p = "data[]."
    elif kind == "keyed":
        d = Designer(infer_tree(vals, root_kind="keyed"), population=lambda: iter(vals), undo_limit=500)
        p = ""
    elif kind == "single":
        d = Designer(infer_tree(vals, root_kind="value"), population=lambda: iter(vals), undo_limit=500)
        p = "items[]."
    else:
        d = Designer(infer_tree(vals, root_kind="records"), population=lambda: iter(vals), undo_limit=500)
        p = "[]."
    t = d.tree
    assert d.move(t.find(p + "contact.address.city"), t.find(p + "name"), "before").applied
    assert d.move(t.find(p + "orders[].oid"), t.find(p + "name"), "after").applied           # gather
    assert d.rename(t.find(p + "score"), "ball").applied
    if kind in ("wrapped", "single"):
        assert d.move(t.find("meta.version"), t.find(p[:-1]), "inside").applied            # broadcast
    return d.tree


def save(tmp_path: Path, tree: MappingTree, doc: Document | None = None, name: str = "map.json",
         **info: Any) -> Path:
    p = tmp_path / name
    save_mapping(tree, p, input_info=dict(make_input_info(doc) if doc is not None else {}, **info))
    return p


def _reformat(compact: bytes, indent: Any) -> bytes:
    from jsonreshape import core

    rf = core.Reformatter(indent)
    return rf.feed(compact) + rf.finish()


def oracle(doc: Document, tree: MappingTree) -> Any:
    """The mapped document value: the transform of the whole value (keyed: member by member)."""
    cm = CompiledMapping(tree)
    if doc.kind in RECORD_KINDS:
        return cm.transform_value(values_of(doc))
    if doc.kind == "keyed":
        out = {}
        for i in range(doc.record_count):
            v = cm.transform_value(doc.record(i))
            if v is not MISSING and v is not None:
                out[doc.record_key(i)] = v
        return out
    return cm.transform_value(whole_of(doc))


def expected(doc: Document, tree: MappingTree, *, indent: Any = 2, jsonl: bool = False) -> bytes:
    """The bytes apply must write (the layout of one Reformatter over the compact text, as every JSON export)."""
    value = oracle(doc, tree)
    if doc.kind == "keyed":
        pairs = [(codec.dumps(k), v) for k, v in value.items()]
        if jsonl:
            return b"".join(b"{" + k + b":" + codec.dumps(v) + b"}\n" for k, v in pairs)
        return _reformat(b"{" + b",".join(k + b":" + codec.dumps(v) for k, v in pairs) + b"}", indent)
    if jsonl:
        items = value["data"] if doc.kind == "wrapped" else value if isinstance(value, list) else [value]
        return b"".join(codec.dumps(x) + b"\n" for x in items)
    return codec.dumps(value, indent=indent)


class _CountdownEvent(threading.Event):
    """An Event that reports "set" from the n-th ``is_set()`` call on (a cancellation in the middle of a run)."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.calls = 0
        self.n = n

    def is_set(self) -> bool:  # noqa: D401
        self.calls += 1
        return self.calls >= self.n or super().is_set()


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


CHILDREN = [{"name": "p1", "children": [
    {"name": "child1", "children": [{"name": "child1.2", "children": [{"name": "child1.2.1", "children": []},
                                                                    {"name": "child1.2.3", "children": []}]}]},
    {"name": "child2", "children": []}]}]
TEST2 = [{"name": "p1", "children": [{"_depth": 1, "name": "child1"}, {"_depth": 2, "name": "child1.2"},
                                     {"_depth": 3, "name": "child1.2.1"}, {"_depth": 3, "name": "child1.2.3"},
                                     {"_depth": 1, "name": "child2"}]}]

FAMILY = [
    {"id": 1, "name": "p1", "children": [{"name": "c1", "children": [{"name": "g1", "children": []}]},
                                         {"name": "c2", "children": []}]},
    {"id": 2, "name": "p2", "children": []},
    {"id": 3, "name": "p3", "children": [{"name": "c3", "children": [{"name": "g3", "children": [
        {"name": "gg3", "children": []}]}]}]},
]
FAMILY_NESTED = [
    {"id": 1, "name": "p1", "children": [{"_depth": 1, "name": "c1"}, {"_depth": 2, "name": "g1"},
                                         {"_depth": 1, "name": "c2"}]},
    {"id": 2, "name": "p2", "children": []},
    {"id": 3, "name": "p3", "children": [{"_depth": 1, "name": "c3"}, {"_depth": 2, "name": "g3"},
                                         {"_depth": 3, "name": "gg3"}]},
]
FAMILY_RECORDS = [
    {"_depth": 1, "id": 1, "name": "p1"}, {"_depth": 2, "name": "c1"}, {"_depth": 3, "name": "g1"},
    {"_depth": 2, "name": "c2"}, {"_depth": 1, "id": 2, "name": "p2"}, {"_depth": 1, "id": 3, "name": "p3"},
    {"_depth": 2, "name": "c3"}, {"_depth": 3, "name": "g3"}, {"_depth": 4, "name": "gg3"},
]
JSONGEN_META = {"version": 1, "source": "test", "list": [1, 2, 3]}


# ----------------------------------------------------------------------------------
# apply: every document kind, layouts, exact numbers and encodings
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_apply_every_document_kind(run, opened, tmp_path: Path, kind: str) -> None:
    src = write_input(tmp_path, kind, people())
    doc = opened(src, format="single" if kind == "single" else None)
    assert doc.kind == kind
    tree = restructured(doc)
    mp = save(tmp_path, tree, doc)
    before = src.read_bytes()
    out = tmp_path / "out.json"
    r = run("apply", mp, src, out, *open_args(kind))
    assert r.code == 0, r.err
    assert out.read_bytes() == expected(doc, tree)
    assert src.read_bytes() == before
    n = 4 if kind != "single" else 1
    assert f"{n} ta yozuv -> " in r.err and "1 ta o'tish, 1 ta jarayon" in r.err and str(out) in r.err
    out2 = tmp_path / "out.jsonl"
    r = run("apply", mp, src, out2, *open_args(kind), "-q")
    assert r.code == 0 and r.out == ""
    assert out2.read_bytes() == expected(doc, tree, jsonl=True)
    if kind == "wrapped":         # JSON Lines of a wrapped document: the record array, with a warning
        assert "o'rovchi obyektning boshqa maydonlari yozilmadi" in r.err
    elif kind == "brace_list":    # the document's own note only
        assert r.err.startswith("Ogohlantirish: Fayl standart JSON emas") and r.err.count("\n") == 1
    else:
        assert r.err == ""


@pytest.mark.parametrize("options, indent, jsonl", [
    ([], 2, False), (["--compact"], None, False), (["--indent", "4"], 4, False), (["--indent", "tab"], "\t", False),
    (["--indent", "0"], 0, False), (["--jsonl"], None, True)])
def test_apply_layouts(run, opened, tmp_path: Path, options: list, indent: Any, jsonl: bool) -> None:
    src = write_input(tmp_path, "array", people())
    doc = opened(src)
    tree = restructured(doc)
    mp = save(tmp_path, tree, doc)
    out = tmp_path / "out.json"
    r = run("apply", mp, src, out, *options)
    assert r.code == 0, r.err
    assert out.read_bytes() == expected(doc, tree, indent=indent, jsonl=jsonl)
    assert "Ogohlantirish" not in r.err


def test_apply_output_names_and_the_indent_setting(run, opened, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", people())
    doc = opened(src)
    tree = restructured(doc)
    mp = save(tmp_path, tree, doc)
    for name in ("out.jsonl", "out.NDJSON"):                       # JSON Lines from the name
        r = run("apply", mp, src, tmp_path / name)
        assert r.code == 0 and (tmp_path / name).read_bytes() == expected(doc, tree, jsonl=True)
    r = run("apply", mp, src, tmp_path / "x.jsonl", "--indent", "4", "--lang", "en")
    assert r.code == 0 and "Warning: --indent: not used by the jsonl format, ignored." in r.err
    assert (tmp_path / "x.jsonl").read_bytes() == expected(doc, tree, jsonl=True)
    r = run("apply", mp, src, tmp_path / "x.json", "--jsonl", "--compact")      # --jsonl wins over the name
    assert r.code == 0 and "--compact: jsonl formatida ishlatilmaydi" in r.err
    assert (tmp_path / "x.json").read_bytes() == expected(doc, tree, jsonl=True)
    r = run("apply", mp, src, tmp_path / "noext")                   # an unknown name: JSON
    assert r.code == 0 and (tmp_path / "noext").read_bytes() == expected(doc, tree)
    set_settings(export_indent=4)                                   # the "export indent" setting is the default
    r = run("apply", mp, src, tmp_path / "s.json")
    assert r.code == 0 and (tmp_path / "s.json").read_bytes() == expected(doc, tree, indent=4)
    set_settings(export_indent="tab")
    r = run("apply", mp, src, tmp_path / "s.json", "--indent", "2")
    assert r.code == 0 and (tmp_path / "s.json").read_bytes() == expected(doc, tree, indent=2)


@pytest.mark.parametrize("encoding, bom, gz, newline", [
    ("utf-8", False, False, "\n"), ("utf-8", True, False, "\r\n"), ("utf-16-le", True, False, "\n"),
    ("utf-16-be", True, False, "\r\n"), ("utf-8", False, True, "\n")],
    ids=["utf8", "bom-crlf", "utf16le", "utf16be-crlf", "gzip"])
@pytest.mark.parametrize("style", ["pretty2", "escaped"])
def test_apply_identity_keeps_exact_numbers_and_unicode(run, opened, tmp_path: Path, encoding: str, bom: bool,
                                                        gz: bool, newline: str, style: str) -> None:
    recs = people() + [{"id": 5, "n": [N("0.1"), N("1e400"), N("12345678901234567890"), N("-0.0"), N("1.10"),
                                       N("-0"), N("5e-324")], "ism": "Oʻzbekiston Тошкент 😀", "k": {"10": 1, "2": 2}}]
    src = tmp_path / ("in.json.gz" if gz else "in.json")
    jsongen.write_file(src, recs, "array", style=style, encoding=encoding, bom=bom, gz=gz, newline=newline)
    doc = opened(src)
    tree = infer_tree(values_of(doc), root_kind="records")          # identity
    mp = save(tmp_path, tree, doc)
    for options, want in (([], codec.dumps(recs, indent=2)), (["--compact"], codec.dumps(recs)),
                          (["--jsonl"], b"".join(codec.dumps(r) + b"\n" for r in recs))):
        out = tmp_path / "out.json"
        r = run("apply", mp, src, out, *options)
        assert r.code == 0, r.err
        data = out.read_bytes()
        assert data == want
        for token in EXACT_TOKENS:
            assert token in data
        assert "Тошкент".encode() in data and b"\\u0001" in data and b'"1.10"' not in data


@pytest.mark.slow
def test_apply_subprocess_end_to_end(opened, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", people())
    doc = opened(src)
    tree = restructured(doc)
    mp = save(tmp_path, tree, doc)
    out = tmp_path / "out.json"
    p = proc("apply", mp, src, out, "--lang", "en")
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert out.read_bytes() == expected(doc, tree)
    err = p.stderr.decode("utf-8")
    assert "4 records -> 4 items -> " in err and "(1 pass, 1 process)" not in err and "1 pass, 1 process)" in err
    p = proc("apply", mp, src, tmp_path / "o.csv")
    assert p.returncode == 1 and "jsonreshape convert" in p.stderr.decode("utf-8")
    assert not (tmp_path / "o.csv").exists()


@pytest.mark.slow
@pytest.mark.parametrize("kind", KINDS)
def test_subprocess_every_document_kind(opened, tmp_path: Path, kind: str) -> None:
    """``python -m jsonreshape apply`` and ``flatten`` give the in-process results for every document kind."""
    src = write_input(tmp_path, kind, people())
    doc = opened(src, format="single" if kind == "single" else None)
    tree = restructured(doc)
    mp = save(tmp_path, tree, doc)
    out = tmp_path / "out.json"
    p = proc("apply", mp, src, out, "--indent", "tab", *open_args(kind))
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert out.read_bytes() == expected(doc, tree, indent="\t")
    if kind == "single":
        fam = tmp_path / "fam.json"
        fam.write_bytes(codec.dumps({"title": "T", "children": FAMILY}))
        want: Any = {"title": "T", "children": FAMILY_RECORDS}
    else:
        fam = write_input(tmp_path, kind, FAMILY, name=f"fam.{'jsonl' if kind == 'jsonl' else 'json'}")
        want = {"wrapped": {"meta": JSONGEN_META, "data": FAMILY_NESTED, "tail": {"count": 3, "ok": True}},
                "keyed": {f"id{i}": rec for i, rec in enumerate(FAMILY_NESTED)}}.get(kind, FAMILY_NESTED)
    p = proc("flatten", fam, out, "--children-key", "children", "--compact", *open_args(kind))
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert out.read_bytes() == codec.dumps(want)


@pytest.mark.slow
def test_apply_with_every_core_backend(opened, tmp_path: Path) -> None:
    from jsonreshape import core

    src = write_input(tmp_path, "jsonl", people() * 3)
    doc = opened(src)
    tree = restructured(doc)
    mp = save(tmp_path, tree, doc)
    want = expected(doc, tree, indent=4)
    for backend in core.available_backends():
        out = tmp_path / f"out_{backend}.json"
        p = proc("apply", mp, src, out, "--indent", "4", "--backend", backend, "--no-cache", "-q")
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        assert out.read_bytes() == want and p.stderr == b""


def test_apply_summary_counts_passes(run, opened, tmp_path: Path) -> None:
    """An array merged into the root array: element-node-major output, two passes, said in the summary."""
    recs = [{"n": 1, "k": [{"m": "a"}]}, {"n": 2, "k": [{"m": "b"}, {"m": "c"}]}]
    src = write_input(tmp_path, "array", recs)
    doc = opened(src)
    d = Designer(infer_tree(recs, root_kind="records"), population=lambda: iter(recs), undo_limit=500)
    assert d.merge(d.tree.find("[].k"), d.tree.find("")).applied
    mp = save(tmp_path, d.tree, doc)
    out = tmp_path / "out.json"
    r = run("apply", mp, src, out, "--compact", "--lang", "en")
    assert r.code == 0, r.err
    assert out.read_bytes() == b'[{"n":1},{"n":2},{"m":"a"},{"m":"b"},{"m":"c"}]'
    assert "The records were read 2 times" in r.err
    assert "2 records -> 5 items -> " in r.err and "2 passes, 1 process)" in r.err


# ----------------------------------------------------------------------------------
# apply: mapping files with problems, kinds, outputs
# ----------------------------------------------------------------------------------
def tree_doc(*fields: Any, **root: Any) -> dict:
    """A records tree in the mapping-file form: root array -> element object -> ``fields``."""
    return dict({"key": "", "kind": "array", "src": [], "is_root": True, "children": [
        {"key": "", "kind": "object", "src": [None], "is_elem": True, "children": list(fields)}]}, **root)


def field(key: str, **kw: Any) -> dict:
    return dict({"key": key, "kind": "value", "src": [None, key]}, **kw)


def mapping_bytes(tree: Any = None, **top: Any) -> bytes:
    doc: dict[str, Any] = {"version": 1, "input": {"kind": "array"},
                           "tree": tree if tree is not None else tree_doc(field("id"))}
    doc.update(top)
    return json.dumps(doc).encode()


OBJECT_ROOT = {"key": "", "kind": "object", "src": [], "is_root": True, "children": [field("a", src=["a"])]}
FIELD0 = "/tree/children/0/children/0"
BAD_MAPPINGS = [
    (b"{not json", "The mapping file is not valid JSON"),
    (b"[1, 2]", "A mapping file must be a JSON object"),
    (mapping_bytes(version=2), "Unsupported mapping file version: 2 (expected 1)"),
    (json.dumps({"version": 1, "input": {}}).encode(), "The mapping file has no «tree»"),
    (mapping_bytes(input=[1]), "«input» in the mapping file must be an object"),
    (mapping_bytes(tree_doc(is_root=False)), "Only the top node is the root, and it is not an element (/tree)"),
    (mapping_bytes(tree_doc(src=["x"])), "The source of the root node must be an empty list (/tree)"),
    (mapping_bytes(tree_doc(5)), f"A structure node must be a JSON object ({FIELD0})"),
    (mapping_bytes(tree_doc(field("a", kind="list"))), f'Unknown node kind "list" ({FIELD0})'),
    (mapping_bytes(tree_doc(field("a", kind="array", children=[]))),
     f"An array node must hold at least one element node and nothing else ({FIELD0})"),
    (mapping_bytes(tree_doc(field("a", src=[None, "a", None], is_elem=True))),
     f"An element node can only be inside an array node ({FIELD0})"),
    (mapping_bytes(tree_doc(field("a", src="a"))), f"Invalid source path (src) ({FIELD0})"),
    (mapping_bytes(tree_doc(field("a", src=[None, None]))), f"The source of a field node must end with a key ({FIELD0})"),
    (mapping_bytes(tree_doc(field("_depth", src=None, computed="depth"))), f"Invalid computed node ({FIELD0})"),
    (mapping_bytes(tree_doc(field("a", rec_key="children"))),
     f"rec_key is allowed only on element nodes of kind object ({FIELD0})"),
    (mapping_bytes(tree_doc(field("a", children=[field("b")]))), f"A value node cannot hold other nodes ({FIELD0})"),
    (mapping_bytes(tree_doc(field("g", kind="object", synthetic=True))),
     f"New groups and computed nodes must have the source null ({FIELD0})"),
    (mapping_bytes(tree_doc(field("a", types=["text"]))), f"The «types» field of the node is invalid ({FIELD0})"),
    (mapping_bytes(tree_doc(field("a", count=-1))), f"The «count» field of the node is invalid ({FIELD0})"),
    (mapping_bytes({"key": "", "kind": "array", "src": [], "is_root": True, "children": [
        {"key": "", "kind": "object", "src": ["x"], "is_elem": True, "children": []}]}),
     "The source of an element node must end with an array step (null) (/tree/children/0)"),
    (mapping_bytes(root_kind="list"), 'Unknown root kind "list"'),
    (mapping_bytes(OBJECT_ROOT, root_kind="records"), "The root kind does not fit (/tree)"),
    (mapping_bytes(known_paths=[5]), "Invalid known_paths (/known_paths/0)"),
    (mapping_bytes(designer=[1]), "Invalid designer section (/designer)"),
    (mapping_bytes(designer={"orig": tree_doc(5)}),
     "A structure node must be a JSON object (/designer/orig/children/0/children/0)"),
    (mapping_bytes(OBJECT_ROOT, root_kind="wrapped", input={"kind": "wrapped", "record_path": "data"}),
     "Invalid input.record_path"),
]


@pytest.mark.parametrize("data, message", BAD_MAPPINGS, ids=range(len(BAD_MAPPINGS)))
def test_apply_bad_mapping_files(run, tmp_path: Path, data: bytes, message: str) -> None:
    src = write_input(tmp_path, "array", people())
    mp = tmp_path / "map.json"
    mp.write_bytes(data)
    r = run("--lang", "en", "apply", mp, src, tmp_path / "out.json")
    assert r.code == 1, r.err
    assert r.err.startswith("Error: ") and message in r.err, r.err
    assert listing(tmp_path) == ["in_array.json", "map.json"]


def test_apply_bad_mapping_in_uzbek_names_the_pointer(run, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", people())
    mp = tmp_path / "map.json"
    mp.write_bytes(mapping_bytes(tree_doc(field("a", kind="list"))))
    r = run("apply", mp, src, tmp_path / "out.json")
    assert r.code == 1 and r.err.startswith("Xato: Tugun turi noma'lum: \"list\" (" + FIELD0 + ")")


def test_apply_missing_or_unusable_mapping(run, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", people())
    r = run("apply", tmp_path / "none.json", src, tmp_path / "out.json")
    assert r.code == 1 and "Fayl topilmadi" in r.err and "none.json" in r.err
    (tmp_path / "dir").mkdir()
    r = run("apply", tmp_path / "dir", src, tmp_path / "out.json")
    assert r.code == 1 and "Xato:" in r.err
    mp = save(tmp_path, infer_tree(people(), root_kind="records"))
    r = run("apply", mp, tmp_path / "missing.json", tmp_path / "out.json")
    assert r.code == 1 and "Fayl topilmadi" in r.err and "missing.json" in r.err
    assert not (tmp_path / "out.json").exists()


def test_apply_kind_mismatch(run, opened, tmp_path: Path) -> None:
    recs = people()
    keyed_src = write_input(tmp_path, "keyed", recs)
    array_src = write_input(tmp_path, "array", recs)
    records_map = save(tmp_path, infer_tree(recs, root_kind="records"), opened(array_src), name="records.json")
    keyed_map = save(tmp_path, restructured(opened(keyed_src)), opened(keyed_src), name="keyed.json")
    r = run("--lang", "en", "apply", records_map, keyed_src, tmp_path / "out.json")
    assert r.code == 1
    assert "This mapping was made for an array of records, but the input is: Keyed object." in r.err
    assert "Hint: Build the mapping from this file again" in r.err
    # a mapping made from a keyed file: the hint names the --format that reads INPUT the same way
    r = run("--lang", "en", "apply", keyed_map, array_src, tmp_path / "out.json")
    assert r.code == 1 and "This mapping was made for a keyed object, but the input is: JSON array." in r.err
    assert "read the input the same way with --format keyed" in r.err
    single = save(tmp_path, infer_tree([recs], root_kind="value"), name="single.json", kind="single")
    r = run("apply", single, array_src, tmp_path / "out.json")
    assert r.code == 1 and "--format single" in r.err
    r = run("apply", single, array_src, tmp_path / "out.json", "--format", "single", "--compact")
    assert r.code == 0 and codec.loads((tmp_path / "out.json").read_bytes()) == recs
    assert listing(tmp_path) == ["in_array.json", "in_keyed.json", "keyed.json", "out.json", "records.json",
                                 "single.json"]


def test_apply_output_checks(run, opened, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", people())
    mp = save(tmp_path, restructured(opened(src)), opened(src))
    before = sorted(listing(tmp_path))
    cases = [
        (src, "Natija manba faylning o'ziga yozilmaydi"),
        (mp, "Natija fayli mapping faylining o'zi"),
        (tmp_path / "o.csv", "nom esa csv formatini bildiradi"),
        (tmp_path / "o.xlsx", "nom esa xlsx formatini bildiradi"),
        (tmp_path / "o.sqlite", "nom esa sqlite formatini bildiradi"),
        (tmp_path / "o.json.gz", "nom esa .gz bilan tugaydi"),
        (tmp_path / "no_dir" / "o.json", "no_dir"),
        (tmp_path, "papka"),
    ]
    for target, text in cases:
        r = run("apply", mp, src, target)
        assert r.code == 1 and text in r.err, (target, r.err)
    assert listing(tmp_path) == before
    assert codec.loads(mp.read_bytes())["version"] == 1                      # the mapping is untouched


def test_apply_read_only_output(run, opened, tmp_path: Path) -> None:
    import stat

    src = write_input(tmp_path, "array", people())
    mp = save(tmp_path, restructured(opened(src)), opened(src))
    out = tmp_path / "ro.json"
    out.write_bytes(b"old")
    os.chmod(out, stat.S_IREAD)
    try:
        r = run("apply", mp, src, out)
        assert r.code == 1 and out.read_bytes() == b"old"
    finally:
        os.chmod(out, stat.S_IREAD | stat.S_IWRITE)


def test_apply_skip_huge_with_a_tiny_large_node_bytes(run, opened, tmp_path: Path) -> None:
    recs = [{"i": 0, "s": "a"}, {"i": 1, "pad": "x" * 400}, {"i": 2, "s": "b"}, {"i": 3, "pad": "y" * 400}]
    src = write_input(tmp_path, "array", recs)
    mp = save(tmp_path, infer_tree([recs[0], recs[2]], root_kind="records"), opened(src))
    set_settings(large_node_bytes=200)
    out = tmp_path / "out.json"
    out.write_bytes(b"old")
    r = run("apply", mp, src, out)
    assert r.code == 1 and "#1 yozuv juda katta" in r.err and "--skip-huge" in r.err
    assert out.read_bytes() == b"old"                                   # nothing replaced
    r = run("--lang", "en", "apply", mp, src, out, "--skip-huge", "--compact")
    assert r.code == 0, r.err
    assert out.read_bytes() == b'[{"i":0,"s":"a"},{"i":2,"s":"b"}]'
    assert "Warning: 2 very large records (over 200 B) were skipped." in r.err
    assert "2 records -> 2 items" not in r.err and "4 records -> 2 items -> " in r.err


@pytest.mark.slow
def test_apply_skip_huge_subprocess_reads_the_settings_file(opened, tmp_path: Path) -> None:
    from jsonreshape.appdirs import settings_file

    recs = [{"i": 0}, {"i": 1, "pad": "x" * 400}, {"i": 2}]
    src = write_input(tmp_path, "jsonl", recs)
    mp = save(tmp_path, infer_tree([recs[0]], root_kind="records"), opened(src))
    settings_file().write_text(json.dumps({"version": 1, "large_node_bytes": 200}), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    p = proc("apply", mp, src, out)
    assert p.returncode == 1 and b"--skip-huge" in p.stderr and not out.exists()
    p = proc("apply", mp, src, out, "--skip-huge", "--lang", "en")
    assert p.returncode == 0 and out.read_bytes() == b'{"i":0}\n{"i":2}\n'
    assert b"1 very large record (over 200 B) was skipped." in p.stderr


def test_apply_record_path_of_the_mapping(run, opened, tmp_path: Path) -> None:
    """A wrapped mapping stores its record path: INPUT is opened with it (auto detection would pick ``a``);
    ``--record-path`` overrides it (the engine warns); a stored path INPUT lacks is exit 1."""
    data = {"meta": {"v": N("1.10")}, "a": [{"x": 1}, {"x": 2}],
            "b": [{"y": "Oʻg'il", "z": [1, 2]}, {"y": "Тошкент"}]}
    src = tmp_path / "w.json"
    src.write_bytes(codec.dumps(data, indent=2))
    assert opened(src).record_path == ("a",)                            # what auto detection chooses
    doc = opened(src, record_path="b")
    vals = values_of(doc)
    d = Designer(infer_tree(vals, root_kind="wrapped", wrapper=data, record_path=("b",)),
                 population=lambda: iter(vals), wrapper=data, record_path=("b",), undo_limit=500)
    assert d.move(d.tree.find("meta.v"), d.tree.find("b[]"), "inside").applied           # broadcast
    mp = save(tmp_path, d.tree, doc)
    assert codec.loads(mp.read_bytes())["input"]["record_path"] == ["b"]
    out = tmp_path / "out.json"
    r = run("apply", mp, src, out, "--compact")
    assert r.code == 0, r.err
    want = CompiledMapping(d.tree).transform_value(data)
    assert out.read_bytes() == codec.dumps(want)
    assert codec.loads(out.read_bytes())["b"][0] == {"y": "Oʻg'il", "z": [1, 2], "v": N("1.10")}
    assert b'"v":1.10' in out.read_bytes()
    # --record-path overrides the stored one: the records of "a" are read, the engine says so
    r = run("--lang", "en", "apply", mp, src, out, "--compact", "--record-path", "a")
    assert r.code == 0 and "The mapping was made for records at b, but the records of this file are at a" in r.err
    # an input without "b": exit 1, the stored path and the arrays found are named
    other = tmp_path / "other.json"
    other.write_bytes(b'{"meta": {"v": 1}, "a": [{"x": 1}, {"x": 2}], "c": [{"q": 1}, {"q": 2}]}')
    r = run("--lang", "en", "apply", mp, other, tmp_path / "o2.json")
    assert r.code == 1
    assert "The record path stored in the mapping file is not in this file or is not an array: b" in r.err
    assert "--record-path" in r.err and "a" in r.err and not (tmp_path / "o2.json").exists()
    # JSON Lines input: no record path is forced on it; the kinds do not fit
    lines = write_input(tmp_path, "jsonl", [{"y": 1}])
    r = run("--lang", "en", "apply", mp, lines, tmp_path / "o3.json")
    assert r.code == 1 and "an array inside an object, but the input is: JSON Lines" in r.err
    assert "read the input the same way with --format wrapped --record-path b" in r.err


def test_apply_json_errors_and_allow_partial(run, opened, tmp_path: Path) -> None:
    good = write_input(tmp_path, "array", people())
    mp = save(tmp_path, restructured(opened(good)), opened(good))
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": ]')
    r = run("apply", mp, bad, tmp_path / "out.json")
    assert r.code == 2 and "--allow-partial" in r.err and not (tmp_path / "out.json").exists()
    r = run("--lang", "en", "apply", mp, bad, tmp_path / "out.json", "--allow-partial", "--compact")
    assert r.code == 0, r.err
    assert codec.loads((tmp_path / "out.json").read_bytes()) == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert r.err.count("JSON error") + r.err.count("the records before the error") >= 1
    assert "were restructured" not in r.err                          # the engine's own partial note: given once


def test_apply_jsonl_with_broken_lines(run, opened, tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    src.write_bytes(b'{"id": 1, "v": 1.10}\n{"id": \n{"id": 3, "v": -0.0}\n')
    mp = save(tmp_path, infer_tree([{"id": 1, "v": 1}], root_kind="records"), kind="jsonl")
    r = run("--lang", "en", "apply", mp, src, tmp_path / "out.jsonl")
    assert r.code == 0, r.err
    assert (tmp_path / "out.jsonl").read_bytes() == b'{"id":1,"v":1.10}\n{"id":3,"v":-0.0}\n'
    assert r.err.count("broken line") == 1                          # said once (not again by the engine)


def test_apply_unmapped_fields_are_reported(run, opened, tmp_path: Path) -> None:
    recs = [{"a": 1}, {"a": 2, "new": True}, {"a": 3, "deep": {"x": 1}}]
    src = write_input(tmp_path, "array", recs)
    mp = save(tmp_path, infer_tree([recs[0]], root_kind="records"), opened(src))
    r = run("--lang", "en", "apply", mp, src, tmp_path / "out.json", "--compact")
    assert r.code == 0 and (tmp_path / "out.json").read_bytes() == b'[{"a":1},{"a":2},{"a":3}]'
    assert "2 records had fields the mapping does not know" in r.err
    assert "--full-scan" not in r.err                               # flatten's hint only


# ----------------------------------------------------------------------------------
# apply: cancellation and worker processes
# ----------------------------------------------------------------------------------
def test_apply_cancelled_before_start(run, opened, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", people())
    mp = save(tmp_path, restructured(opened(src)), opened(src))
    token = CancelToken()
    token.cancel()
    before = listing(tmp_path)
    r = run("apply", mp, src, tmp_path / "out.json", cancel=token)
    assert r.code == 3 and r.err.strip() == "Amal bekor qilindi."
    r = run("flatten", src, tmp_path / "out.json", "--children-key", "orders", cancel=token)
    assert r.code == 3
    assert listing(tmp_path) == before


def _big_input(tmp_path: Path, count: int = 6000) -> Path:
    p = tmp_path / "big.json"
    base = people()
    jsongen.write_file(p, [dict(base[i % len(base)], id=i, children=[{"n": i, "children": [{"n": -i}]}])
                           for i in range(count)], "array")
    return p


def test_apply_cancelled_while_writing_leaves_nothing(run, opened, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from jsonreshape import engine, fileio

    src = _big_input(tmp_path)
    mp = save(tmp_path, infer_tree(people(), root_kind="records"), opened(src))
    before = listing(tmp_path)
    token = CancelToken()
    original = engine.apply
    original_temp = fileio._create_temp
    temps: list[Path] = []

    def apply_then_cancel(*args: Any, **kwargs: Any) -> Any:
        token._event = _CountdownEvent(4)
        return original(*args, **kwargs)

    def spy_temp(*args: Any, **kwargs: Any) -> Any:
        fd, path = original_temp(*args, **kwargs)
        temps.append(Path(path))
        return fd, path

    monkeypatch.setattr(engine, "apply", apply_then_cancel)
    monkeypatch.setattr(fileio, "_create_temp", spy_temp)
    r = run("apply", mp, src, tmp_path / "out.json", cancel=token)
    assert r.code == 3 and r.err.strip().endswith("Amal bekor qilindi.")
    assert temps and token._event.calls >= 4                          # it had started writing
    assert listing(tmp_path) == before


def test_apply_ctrl_c(run, opened, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if threading.current_thread() is not threading.main_thread():  # pragma: no cover
        pytest.skip("signal handlers need the main thread")
    from jsonreshape import engine

    src = _big_input(tmp_path, 3000)
    mp = save(tmp_path, infer_tree(people(), root_kind="records"), opened(src))
    original = engine.apply
    presses = {"n": 1}

    def apply_with_ctrl_c(*args: Any, **kwargs: Any) -> Any:
        for _ in range(presses["n"]):
            signal.raise_signal(signal.SIGINT)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "apply", apply_with_ctrl_c)
    for n in (1, 2):                                   # the second Ctrl+C raises KeyboardInterrupt at once
        presses["n"] = n
        r = run("apply", mp, src, tmp_path / "out.json")
        assert r.code == 3 and "Amal bekor qilindi." in r.err
        assert not (tmp_path / "out.json").exists()


@pytest.mark.slow
def test_apply_with_worker_processes(opened, tmp_path: Path) -> None:
    src = _big_input(tmp_path, 4000)
    doc = opened(src)
    tree = restructured_children(doc)
    mp = save(tmp_path, tree, doc)
    one, two = tmp_path / "one.json", tmp_path / "two.json"
    p1 = proc("apply", mp, src, one, "--workers", "1", "--lang", "en")
    p2 = proc("apply", mp, src, two, "--workers", "2", "--lang", "en")
    assert p1.returncode == 0 and p2.returncode == 0, p2.stderr.decode("utf-8", "replace")
    assert one.read_bytes() == two.read_bytes() == expected(doc, tree)
    assert b"1 pass, 1 process)" in p1.stderr and b"1 pass, 2 processes)" in p2.stderr


def restructured_children(doc: Document) -> MappingTree:
    vals = values_of(doc)
    d = Designer(infer_tree(vals, root_kind="records"), population=lambda: iter(vals), undo_limit=500)
    assert d.move(d.tree.find("[].contact.address.city"), d.tree.find("[].name"), "before").applied
    assert d.flatten(d.tree.find("[].children")).applied
    return d.tree


def test_workers_option_is_validated(run, opened, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", people())
    mp = save(tmp_path, infer_tree(people(), root_kind="records"), opened(src))
    for bad in ("0", "-2", "x"):
        r = run("apply", mp, src, tmp_path / "out.json", "--workers", bad)
        assert r.code == 1 and "--workers" in r.err
    r = run("apply", mp, src, tmp_path / "out.json", "--workers", "1", "--compact")
    assert r.code == 0 and codec.loads((tmp_path / "out.json").read_bytes()) == people()


# ----------------------------------------------------------------------------------
# flatten: AGENT_PROMPT test 2, both modes, every document kind
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("options, layout", [([], 2), (["--compact"], None), (["--jsonl"], "jsonl")])
def test_flatten_agent_prompt_test_2(run, tmp_path: Path, options: list, layout: Any) -> None:
    src = write_input(tmp_path, "array", CHILDREN)
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "children", *options)
    assert r.code == 0, r.err
    data = out.read_bytes()
    if layout == "jsonl":
        assert data == b"".join(codec.dumps(x) + b"\n" for x in TEST2)
    else:
        assert data == codec.dumps(TEST2, indent=layout)
    items = codec.loads(data.splitlines()[0] if layout == "jsonl" else data)
    items = items["children"] if isinstance(items, dict) else items[0]["children"]
    assert [x["name"] for x in items] == ["child1", "child1.2", "child1.2.1", "child1.2.3", "child2"]
    assert [x["_depth"] for x in items] == [1, 2, 3, 3, 1]
    assert all("children" not in x for x in items)
    assert "«children» rekursiv tekislandi (daraja raqami: «_depth»)" in r.err
    assert "1 ta yozuv -> 1 ta element -> " in r.err


@pytest.mark.slow
def test_flatten_test_2_subprocess(tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", CHILDREN)
    out = tmp_path / "flat.json"
    p = proc("flatten", src, out, "--children-key", "children", "--lang", "en")
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert out.read_bytes() == codec.dumps(TEST2, indent=2)
    err = p.stderr.decode("utf-8")
    assert "«children» was flattened recursively (level number: «_depth»)" in err
    assert "1 record -> 1 item -> " in err
    p = proc("flatten", src, tmp_path / "x.json", "--children-key", "kids")
    assert p.returncode == 1 and "«kids» maydoni topilmadi" in p.stderr.decode("utf-8")


@pytest.mark.parametrize("kind", KINDS)
def test_flatten_every_document_kind(run, tmp_path: Path, kind: str) -> None:
    if kind == "single":
        src = tmp_path / "single.json"
        src.write_bytes(codec.dumps({"title": "T", "children": FAMILY}, indent=2))
        want: Any = {"title": "T", "children": FAMILY_RECORDS}
    else:
        src = write_input(tmp_path, kind, FAMILY)
        want = {"array": FAMILY_NESTED, "jsonl": FAMILY_NESTED, "stream": FAMILY_NESTED, "brace_list": FAMILY_NESTED,
                "wrapped": {"meta": JSONGEN_META, "data": FAMILY_NESTED, "tail": {"count": 3, "ok": True}},
                "keyed": {f"id{i}": rec for i, rec in enumerate(FAMILY_NESTED)}}[kind]
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "children", "--compact", *open_args(kind))
    assert r.code == 0, r.err
    assert out.read_bytes() == codec.dumps(want)


@pytest.mark.parametrize("kind", ["array", "jsonl", "wrapped"])
def test_flatten_records_mode(run, tmp_path: Path, kind: str) -> None:
    src = write_input(tmp_path, kind, FAMILY)
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "children", "--mode", "records", "--compact")
    assert r.code == 0, r.err
    data = codec.loads(out.read_bytes())
    assert (data["data"] if kind == "wrapped" else data) == FAMILY_RECORDS


def test_flatten_records_mode_engine_example_18_and_single_arrays(run, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", [{"n": "r1", "children": [{"n": "c1", "children": [{"n": "g1"}]}]},
                                          {"n": "r2"}])
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "children", "--mode", "records", "--compact")
    assert r.code == 0 and out.read_bytes() == (b'[{"_depth":1,"n":"r1"},{"_depth":2,"n":"c1"},{"_depth":3,"n":"g1"},'
                                                b'{"_depth":1,"n":"r2"}]')
    # a single document that is an array (forced --format single): the same
    r = run("flatten", src, out, "--children-key", "children", "--mode", "records", "--compact", "--format", "single")
    assert r.code == 0 and codec.loads(out.read_bytes())[2] == {"_depth": 3, "n": "g1"}


def test_flatten_records_mode_picks_the_given_key(run, tmp_path: Path) -> None:
    """The reference findRec would take the FIRST recursive array of the record (tags); KEY is used instead and
    the other recursive array is kept (the flatten re-infers it)."""
    recs = [{"n": "r1", "tags": [{"t": 1, "tags": [{"t": 2}]}], "children": [{"n": "c1", "children": [{"n": "g1"}]}]},
            {"n": "r2", "tags": []}]
    src = write_input(tmp_path, "array", recs)
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "children", "--mode", "records", "--compact")
    assert r.code == 0, r.err
    assert codec.loads(out.read_bytes()) == [
        {"_depth": 1, "n": "r1", "tags": [{"t": 1, "tags": [{"t": 2}]}]}, {"_depth": 2, "n": "c1"},
        {"_depth": 3, "n": "g1"}, {"_depth": 1, "n": "r2", "tags": []}]
    r = run("flatten", src, out, "--children-key", "tags", "--mode", "records", "--compact")
    assert r.code == 0, r.err
    assert codec.loads(out.read_bytes()) == [
        {"_depth": 1, "n": "r1", "children": [{"n": "c1", "children": [{"n": "g1"}]}]}, {"_depth": 2, "t": 1},
        {"_depth": 3, "t": 2}, {"_depth": 1, "n": "r2"}]


def test_flatten_records_mode_needs_a_record_array(run, tmp_path: Path) -> None:
    src = write_input(tmp_path, "keyed", FAMILY)
    r = run("--lang", "en", "flatten", src, tmp_path / "flat.json", "--children-key", "children", "--mode", "records")
    assert r.code == 1 and "--mode records does not work for this file" in r.err and "Keyed object" in r.err
    assert "Hint: Use --mode nested" in r.err
    single = tmp_path / "single.json"
    single.write_bytes(codec.dumps({"title": "T", "children": FAMILY}))
    r = run("flatten", single, tmp_path / "flat.json", "--children-key", "children", "--mode", "records",
            "--format", "single")
    assert r.code == 1 and "--mode records bu faylda ishlamaydi" in r.err
    assert not (tmp_path / "flat.json").exists()


# ----------------------------------------------------------------------------------
# flatten: --depth-field, wrong keys, samples
# ----------------------------------------------------------------------------------
def test_flatten_depth_field(run, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", CHILDREN)
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "children", "--depth-field", " daraja ", "--compact")
    assert r.code == 0 and "Ogohlantirish" not in r.err and "(daraja raqami: «daraja»)" in r.err
    items = codec.loads(out.read_bytes())[0]["children"]
    assert [x["daraja"] for x in items] == [1, 2, 3, 3, 1] and all("_depth" not in x for x in items)
    # a name that starts with "-" is a value, not an option
    r = run("flatten", src, out, "--children-key", "children", "--depth-field", "-lvl", "--compact")
    assert r.code == 0 and codec.loads(out.read_bytes())[0]["children"][0] == {"-lvl": 1, "name": "child1"}
    for empty in ("", "  　"):
        r = run("flatten", src, out, "--children-key", "children", "--depth-field", empty)
        assert r.code == 1 and "--depth-field qiymati bo'sh bo'lmasligi kerak" in r.err


def test_flatten_depth_field_collisions(run, tmp_path: Path) -> None:
    recs = [{"name": "p", "children": [{"name": "a", "level": "x", "_depth": "d",
                                        "children": [{"name": "b", "level": "y"}]}]}]
    src = write_input(tmp_path, "array", recs)
    out = tmp_path / "flat.json"
    r = run("--lang", "en", "flatten", src, out, "--children-key", "children", "--depth-field", "level", "--compact")
    assert r.code == 0, r.err
    assert "Warning: The name «level» is taken in the data: the level number is written to «level_2»." in r.err
    assert out.read_bytes() == (b'[{"name":"p","children":[{"level_2":1,"name":"a","level":"x","_depth":"d"},'
                                b'{"level_2":2,"name":"b","level":"y"}]}]')
    # the default name too (_depth is a data field here)
    r = run("flatten", src, out, "--children-key", "children", "--compact")
    assert r.code == 0 and "«_depth» nomi ma'lumotda band: daraja raqami «_depth_2» maydoniga yoziladi" in r.err
    assert codec.loads(out.read_bytes())[0]["children"][1] == {"_depth_2": 2, "name": "b", "level": "y"}


@pytest.mark.parametrize("recs, key, message", [
    (CHILDREN, "kids", "Error: The records have no «kids» field.\nHint: Array fields of the records: children."),
    ([{"a": 1}], "kids", "Hint: The records have no array fields."),
    ([[1, 2], [3]], "kids", "Hint: The records are not objects."),
    (CHILDREN, "name", "«name» is not a recursive array of objects here: the field is not an array (type: text)."),
    ([{"c": [1, "x"]}, {"c": [2]}], "c", "«c» is not a recursive array of objects here: its items are not objects "
                                         "(type: text, number)."),
    ([{"c": []}, {"c": []}], "c", "«c» is not a recursive array of objects here: the array is always empty."),
    ([{"c": [{"n": 1}, {"n": 2}]}], "c", "«c» is not a recursive array of objects here: its items hold no nested "
                                         "«c» array of objects."),
    ([{"c": [{"n": 1, "items": [{"n": 2}]}]}], "c", "its items hold no nested «c» array of objects."),
    ([{"c": [{"n": 1, "c": ["x", "y"]}]}], "c", "its items hold no nested «c» array of objects."),
])
def test_flatten_wrong_children_key(run, tmp_path: Path, recs: list, key: str, message: str) -> None:
    src = write_input(tmp_path, "array", recs)
    r = run("--lang", "en", "flatten", src, tmp_path / "flat.json", "--children-key", key)
    assert r.code == 1 and message in r.err, r.err
    assert not (tmp_path / "flat.json").exists()


def test_flatten_records_mode_wrong_key(run, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", [{"n": 1, "tags": ["a"], "children": [{"n": 2}]}, {"n": 3}])
    r = run("--lang", "en", "flatten", src, tmp_path / "flat.json", "--children-key", "tags", "--mode", "records")
    assert r.code == 1 and "«tags» is not a recursive array of objects here: its items are not objects" in r.err
    r = run("--lang", "en", "flatten", src, tmp_path / "flat.json", "--children-key", "nope", "--mode", "records")
    assert r.code == 1 and "The records have no «nope» field." in r.err and "children, tags" not in r.err
    assert "Array fields of the records: tags, children." in r.err


def _deep_only_in(count: int, where: int) -> list[dict[str, Any]]:
    """``count`` records whose children have one level, except record ``where``: two levels and a field ``x``."""
    recs = [{"id": i, "children": [{"n": i}]} for i in range(count)]
    recs[where] = {"id": where, "children": [{"n": where, "children": [{"n": -1, "x": N("1e400")}]}]}
    return recs


def test_flatten_sample_and_full_scan(run, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", _deep_only_in(50, 25))
    out = tmp_path / "flat.json"
    # the sample (records #0 and #49) has no second level: KEY is not recursive there
    r = run("--lang", "en", "flatten", src, out, "--children-key", "children", "--sample", "2")
    assert r.code == 1 and "its items hold no nested «children» array of objects" in r.err
    assert "a sample of 2 of 50 records" in r.err and "--full-scan" in r.err and not out.exists()
    r = run("--lang", "en", "flatten", src, out, "--children-key", "children", "--full-scan", "--compact")
    assert r.code == 0, r.err
    assert "the structure was inferred from every record (50)" in r.err and "Warning" not in r.err
    data = codec.loads(out.read_bytes())
    assert data[25] == {"id": 25, "children": [{"_depth": 1, "n": 25}, {"_depth": 2, "n": -1, "x": N("1e400")}]}
    assert data[0] == {"id": 0, "children": [{"_depth": 1, "n": 0}]}
    assert b'"x":1e400' in out.read_bytes()
    # a sample that has the level but not the field x: the field is dropped, counted, and --full-scan advised
    recs = _deep_only_in(50, 25)
    recs[0] = {"id": 0, "children": [{"n": 0, "children": [{"n": -2}]}]}
    write_input(tmp_path, "array", recs, name="in_array.json")
    r = run("--lang", "en", "flatten", src, out, "--children-key", "children", "--sample", "2", "--compact")
    assert r.code == 0, r.err
    assert "the structure was inferred from a sample of 2 of 50 records" in r.err
    assert "Warning: 1 record had fields the mapping does not know" in r.err
    assert "Hint: Add --full-scan to infer the structure from every record." in r.err
    assert codec.loads(out.read_bytes())[25]["children"][1] == {"_depth": 2, "n": -1}
    # the sample size setting is the default sample
    set_settings(sample_size=2)
    r = run("--lang", "en", "flatten", src, out, "--children-key", "children", "--compact")
    assert r.code == 0 and "a sample of 2 of 50 records" in r.err
    set_settings(sample_size=50)
    r = run("--lang", "en", "flatten", src, out, "--children-key", "children", "--compact")
    assert r.code == 0 and "inferred from every record (50)" in r.err and "Warning" not in r.err


def test_flatten_large_count_with_the_default_sample(run, tmp_path: Path) -> None:
    n = 25000
    src = tmp_path / "big.jsonl"
    src.write_bytes(b"".join(codec.dumps({"id": i, "children": [{"n": i, "children": [{"n": -i, "s": N("1.10")}]}]})
                             + b"\n" for i in range(n)))
    out = tmp_path / "flat.jsonl"
    r = run("flatten", src, out, "--children-key", "children")
    assert r.code == 0, r.err
    assert "tuzilma 25 000 ta yozuvdan 10 000 tasi (namuna) bo'yicha aniqlandi" in r.err
    assert "Ogohlantirish" not in r.err and "25 000 ta yozuv -> 25 000 ta element -> " in r.err
    lines = out.read_bytes().splitlines()
    assert len(lines) == n
    assert lines[12345] == b'{"id":12345,"children":[{"_depth":1,"n":12345},{"_depth":2,"n":-12345,"s":1.10}]}'


def test_flatten_json_error_and_allow_partial(run, tmp_path: Path) -> None:
    src = tmp_path / "bad.json"
    src.write_bytes(b'[{"children": [{"n": 1, "children": [{"n": 2}]}]}, {"children": [{"n": 3}]}, {"children": ]')
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "children")
    assert r.code == 2 and "--allow-partial" in r.err and not out.exists()
    r = run("flatten", src, out, "--children-key", "children", "--allow-partial", "--compact")
    assert r.code == 0, r.err
    assert out.read_bytes() == b'[{"children":[{"_depth":1,"n":1},{"_depth":2,"n":2}]},{"children":[{"_depth":1,"n":3}]}]'


def test_flatten_exact_numbers_unicode_keys_and_layouts(run, tmp_path: Path) -> None:
    key = CYR
    recs = [{"ism": "Oʻgʻil", key: [{"v": N("0.1"), key: [{"v": N("12345678901234567890"), "s": "Тошкент\n"},
                                                         {"v": N("-0.0")}]}, {"v": N("1.10"), key: []}]},
            {"ism": "G'ulom", "son": N("1e400")}]
    src = tmp_path / "in.json"
    jsongen.write_file(src, recs, "array", style="escaped")
    out = tmp_path / "flat.json"
    want = [{"ism": "Oʻgʻil", key: [{"_depth": 1, "v": N("0.1")}, {"_depth": 2, "v": N("12345678901234567890"),
                                                                  "s": "Тошкент\n"},
                                    {"_depth": 2, "v": N("-0.0")}, {"_depth": 1, "v": N("1.10")}]},
            {"ism": "G'ulom", "son": N("1e400")}]
    for options, data in ((["--indent", "4"], codec.dumps(want, indent=4)), (["--indent", "tab"], codec.dumps(want, indent="\t")),
                          (["--jsonl"], b"".join(codec.dumps(x) + b"\n" for x in want))):
        r = run("flatten", src, out, "--children-key", key, *options)
        assert r.code == 0, r.err
        assert out.read_bytes() == data
        for token in EXACT_TOKENS:
            assert token in data
    r = run("flatten", src, out, "--children-key", key, "--depth-field", "daraja_oʻ", "--compact")
    assert r.code == 0 and codec.loads(out.read_bytes())[0][key][1]["daraja_oʻ"] == 2


@pytest.mark.slow
def test_flatten_keyed_and_wrapped_subprocess(tmp_path: Path) -> None:
    keyed = write_input(tmp_path, "keyed", FAMILY, keys=["Toshkent", "Тошкент", "O'zbekiston"])
    out = tmp_path / "flat.json"
    p = proc("flatten", keyed, out, "--children-key", "children", "--compact")
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert codec.loads(out.read_bytes()) == dict(zip(["Toshkent", "Тошкент", "O'zbekiston"], FAMILY_NESTED))
    wrapped = write_input(tmp_path, "wrapped", FAMILY, wrapper_path=("result", "items"))
    p = proc("flatten", wrapped, out, "--children-key", "children", "--record-path", "result.items", "--jsonl")
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert out.read_bytes() == b"".join(codec.dumps(x) + b"\n" for x in FAMILY_NESTED)
    assert "JSON Lines faqat yozuvlarni saqlaydi" in p.stderr.decode("utf-8")


def test_flatten_empty_inputs(run, tmp_path: Path) -> None:
    src = tmp_path / "e.json"
    src.write_bytes(b"[]")
    for name, want in (("o.json", b"[]"), ("o.jsonl", b"")):
        r = run("flatten", src, tmp_path / name, "--children-key", "children")
        assert r.code == 0, r.err
        assert (tmp_path / name).read_bytes() == want and "Faylda yozuv yo'q: tekislanadigan narsa yo'q." in r.err
    src.write_bytes(b"{}")                                   # one empty object (a single value): no such field
    r = run("flatten", src, tmp_path / "o2.json", "--children-key", "children")
    assert r.code == 1 and "Yozuvlarda «children» maydoni topilmadi." in r.err


def test_flatten_huge_records(run, tmp_path: Path) -> None:
    recs = [{"id": 0, "children": [{"n": 1, "children": [{"n": 2}]}]}, {"id": 1, "pad": "x" * 500},
            {"id": 2, "children": []}]
    src = write_input(tmp_path, "array", recs)
    set_settings(large_node_bytes=300)
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "children")
    assert r.code == 1 and "#1 yozuv juda katta" in r.err and not out.exists()
    r = run("flatten", src, out, "--children-key", "children", "--skip-huge", "--compact")
    assert r.code == 0, r.err
    assert codec.loads(out.read_bytes()) == [{"id": 0, "children": [{"_depth": 1, "n": 1}, {"_depth": 2, "n": 2}]},
                                             {"id": 2, "children": []}]
    # a single document over the limit is refused before any inference
    single = tmp_path / "s.json"
    single.write_bytes(codec.dumps({"children": [{"pad": "y" * 500}]}))
    r = run("--lang", "en", "flatten", single, out, "--children-key", "children", "--format", "single")
    assert r.code == 1 and "too large to restructure as one value" in r.err


def test_flatten_member_records_and_partial_wrapper_are_refused(run, tmp_path: Path) -> None:
    src = tmp_path / "w.json"
    src.write_bytes(b'{"meta": 1, "users": {"a": {"children": []}, "b": {"children": []}, "c": {"children": []}}}')
    r = run("--lang", "en", "flatten", src, tmp_path / "flat.json", "--children-key", "children",
            "--record-path", "users")
    assert r.code == 1 and "The records are members of an object inside the wrapper" in r.err
    partial = tmp_path / "p.json"
    partial.write_bytes(b'{"meta": 1, "data": [{"children": [{"children": []}]}, {"children": [')
    r = run("--lang", "en", "flatten", partial, tmp_path / "flat.json", "--children-key", "children",
            "--record-path", "data", "--allow-partial")
    assert r.code == 1 and "The record array is never closed" in r.err
    assert not (tmp_path / "flat.json").exists()


def test_flatten_usage_errors(run, tmp_path: Path) -> None:
    src = write_input(tmp_path, "array", CHILDREN)
    out = tmp_path / "flat.json"
    r = run("flatten", src, out)
    assert r.code == 1 and "--children-key" in r.err and "majburiy" in r.err
    r = run("flatten", src, out, "--children-key", "children", "--sample", "5", "--full-scan")
    assert r.code == 1 and "--full-scan" in r.err
    r = run("flatten", src, out, "--children-key", "children", "--sample", "0")
    assert r.code == 1
    r = run("flatten", src, out, "--children-key", "children", "--mode", "deep")
    assert r.code == 1 and "nested" in r.err
    r = run("flatten", src, src, "--children-key", "children")
    assert r.code == 1 and "Natija manba faylning o'ziga yozilmaydi" in r.err
    r = run("flatten", src, tmp_path / "o.xlsx", "--children-key", "children")
    assert r.code == 1 and "xlsx" in r.err
    assert listing(tmp_path) == ["in_array.json"]


def test_flatten_children_key_starting_with_a_dash(run, tmp_path: Path) -> None:
    recs = [{"-c": [{"n": 1, "-c": [{"n": 2}]}]}]
    src = write_input(tmp_path, "array", recs)
    out = tmp_path / "flat.json"
    r = run("flatten", src, out, "--children-key", "-c", "--compact")
    assert r.code == 0, r.err
    assert out.read_bytes() == b'[{"-c":[{"_depth":1,"n":1},{"_depth":2,"n":2}]}]'


def test_flatten_cancelled_while_reading_the_levels(run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jsonreshape import mapping

    src = _big_input(tmp_path, 3000)
    token = CancelToken()
    original = mapping.Designer.plan_flatten
    seen: dict[str, Any] = {}

    def plan_then_cancel(self: Any, *args: Any, **kwargs: Any) -> Any:
        token._event = seen["event"] = _CountdownEvent(3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(mapping.Designer, "plan_flatten", plan_then_cancel)
    before = listing(tmp_path)
    r = run("flatten", src, tmp_path / "flat.json", "--children-key", "children", "--full-scan", cancel=token)
    assert r.code == 3 and "Amal bekor qilindi." in r.err
    assert seen["event"].calls >= 3 and listing(tmp_path) == before


def test_progress_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    src = write_input(tmp_path, "array", FAMILY * 20)
    fake = _TTY()
    monkeypatch.setattr(sys, "stderr", fake)
    assert cli.main(["flatten", str(src), str(tmp_path / "flat.json"), "--children-key", "children",
                     "--no-cache"]) == 0
    text = fake.getvalue()
    for label in ("\rIndekslanmoqda: ", "\rTuzilma aniqlanmoqda: ", "\rIchki darajalar o'qilmoqda: ",
                  "\rTuzilma qo'llanmoqda: flat.json: "):
        assert label in text, (label, text)
    tail = text.rsplit("\r", 1)[-1].splitlines()           # every progress line is cleared before the summary
    assert tail[0].startswith("«children» rekursiv tekislandi") and tail[1].startswith("60 ta yozuv -> 60 ta element")
    fake = _TTY()
    monkeypatch.setattr(sys, "stderr", fake)
    assert cli.main(["flatten", str(src), str(tmp_path / "flat.json"), "--children-key", "children", "-q"]) == 0
    assert fake.getvalue() == ""


# ----------------------------------------------------------------------------------
# help texts
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("lang, needles", [
    ("uz", ("foydalanish: jsonreshape apply", "Misollar:", "--skip-huge", "--workers N", "mapping fayli",
            "--compact", "--indent N", "--record-path P")),
    ("en", ("usage: jsonreshape apply", "Examples:", "--skip-huge", "--workers N", "mapping file saved by the "
            "structure builder", "the record path stored in the mapping file is used")),
])
def test_apply_help(run, lang: str, needles: tuple) -> None:
    r = run("--lang", lang, "apply", "--help")
    assert r.code == 0
    for needle in needles:
        assert needle in r.out, needle


@pytest.mark.parametrize("lang, needles", [
    ("uz", ("foydalanish: jsonreshape flatten", "--children-key KEY", "--depth-field NAME", "--mode {nested,records}",
            "--full-scan", "child1, child1.2, child1.2.1, child1.2.3, child2", "tekislash parametrlari")),
    ("en", ("usage: jsonreshape flatten", "--children-key KEY", "--sample N", "flatten options",
            "_depth = 1, 2,", "jsonreshape flatten data.json flat.json --children-key children")),
])
def test_flatten_help(run, lang: str, needles: tuple) -> None:
    r = run("--lang", lang, "flatten", "--help")
    assert r.code == 0
    for needle in needles:
        assert needle in r.out, needle


def test_main_help_lists_the_commands(run) -> None:
    r = run("--lang", "en", "--help")
    assert r.code == 0 and "apply" in r.out and "flatten" in r.out
    assert "apply a mapping (structure) file to a whole file" in r.out
