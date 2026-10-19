"""Fix round L1 (stage-5 leftovers for the CLI and the core modules; scratchpad ``s5/task_L1_core_cli.md``):

1. ``diff -o``: a target that is one of the compared files (their data copies included) is refused BEFORE the
   diff runs (the CLI passes ``sources=`` to ``write_diff``; its printer's generator hid the run);
2. ``split``: the part names are the core ``filetools.part_names``; ``--remove-stale`` (every removed file named on
   stderr, also with ``-q``); a part with unsaved edits is refused (exit 1) before the "parts exist" question;
   the ``--remove-stale`` hint;
3. ``editstore.layer_sources``: the layer index read once with a buffer of its size (no 16 MiB read);
   ``filetools._edited_files`` asks ``find_pending`` only about the files it lists;
4. the writer (``export`` / ``export_node`` / ``export_many``, filetools' gather) and every CLI output never
   replace a file with unsaved edits (a tab or a pending layer): before the work and again right before the commit;
5. ``fileio``: ``_utf16_len`` in C (same results, astral characters and lone surrogates), ``long_path`` decisions
   unchanged, ``prepared_cache_entries`` = ``scan_prepared_entries``, a UTF-16 file cut inside a character is read
   (the tail left out with ``fileio.warning.utf16_truncated``) and repaired;
6. public ``TextQuery.regex`` / ``regex_postfilter``, ``textmatch.is_whole_word`` and ``query.json_equal`` (the
   private names stay as aliases);
7. ``validate``: records that could not be validated are "not shown to match" -- a clear summary, exit 5,
   ``--json`` ``ok`` false.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import cli, codec, editstore, fileio, filetools, i18n, query, textmatch, writer
from jsonreshape import diff as diff_mod
from jsonreshape.edits import EditSession
from jsonreshape.errors import FileFormatError, UserInputError
from jsonreshape.fileio import normalize_path, prepare_data_file
from jsonreshape.reader import Document
from jsonreshape.repair import repair_file
from jsonreshape.tasks import CancelToken
from jsonreshape.textmatch import TextQuery
from jsonreshape.writer import ExportOptions

WINDOWS = sys.platform == "win32"
N = codec.loads
EDITED_UZ = "saqlanmagan o'zgarishlar bor"
EDITED_EN = "has unsaved changes in this program"


@pytest.fixture(autouse=True)
def _cli_isolation(monkeypatch: pytest.MonkeyPatch):
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


def records(n: int) -> list[dict[str, Any]]:
    names = ["Toshkent", "Тошкент", "O'zbekiston", "Oʻzbekiston", "G'ulom"]
    exact = [N("0.1"), N("1e400"), N("12345678901234567890"), N("-0.0"), N("1.10")]
    return [{"id": i, "v": names[i % 5], "x": exact[i % 5]} for i in range(n)]


def listing(p: Path) -> list[str]:
    return sorted(x.name for x in p.iterdir())


def _edit(path: Path) -> tuple[Document, EditSession]:
    """``path`` opened as in a tab, with an unsaved edit of record 0."""
    doc = Document.open(path)
    doc.build_index()
    sess = EditSession.open(doc)
    sess.set_value((0, ("v",)), '"UNSAVED EDIT"')
    return doc, sess


def _pending(path: Path) -> None:
    """``path`` gets an edit layer kept for recovery (the tab closed with unsaved edits)."""
    doc, sess = _edit(path)
    sess.close()
    doc.close()


def _discard(path: Path) -> None:
    with Document.open(path) as doc:
        doc.build_index()
        sess = EditSession.open(doc)              # restores the pending layer ...
        sess.discard()                            # ... and deletes it
        sess.close()


def _temps(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.endswith((".tmp", ".part", ".jrpart", ".jrold"))]


# ==================================================================================================
# 1. diff -o: the compared files are refused before the diff runs
# ==================================================================================================
@pytest.mark.parametrize("variant", ["gzip", "utf16"])
def test_diff_output_naming_a_data_copy_is_refused_before_the_diff_runs(run, tmp_path: Path, monkeypatch,
                                                                        variant: str) -> None:
    a = tmp_path / ("a.json.gz" if variant == "gzip" else "a.json")
    if variant == "gzip":
        jsongen.write_file(a, records(6), "array", gz=True)
    else:
        jsongen.write_file(a, records(6), "array", encoding="utf-16-le", bom=True)
    b = tmp_path / "b.json"
    jsongen.write_file(b, records(5) + [{"id": 99}], "array")
    with Document.open(a) as doc:
        copy = Path(doc.data_path)                # the prepared UTF-8 copy the diff reads
    assert normalize_path(copy) != normalize_path(a) and copy.suffix == ".json"
    before = copy.read_bytes()
    started: list[int] = []
    real = diff_mod._Run._prepare
    monkeypatch.setattr(diff_mod._Run, "_prepare", lambda self: started.append(1) or real(self))
    r = run("diff", a, b, "-o", copy)
    assert r.code == 1, r.err
    assert "Natija manba faylning o'ziga yozilmaydi" in r.err
    assert started == []                          # refused before the diff ran (it used to run it whole first)
    assert copy.read_bytes() == before and _temps(copy.parent) == []
    out = tmp_path / "res.jsonl"                  # (an ordinary target still works and runs the diff once)
    r = run("diff", a, b, "-o", out)
    assert r.code == 0, r.err
    assert started == [1] and out.exists()


# ==================================================================================================
# 2. split: the core part names, --remove-stale, parts with unsaved edits
# ==================================================================================================
def test_split_takes_its_part_names_from_the_core(run, tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "people.json.gz"
    jsongen.write_file(src, records(7), "array", gz=True)
    out = tmp_path / "parts"
    out.mkdir()
    calls: list[tuple[int, list[str]]] = []
    real = filetools.part_names

    def spy(doc: Any, opts: Any, source: Any = None, *, count: int) -> list[Path]:
        names = real(doc, opts, source, count=count)
        calls.append((count, [p.name for p in names]))
        return names

    monkeypatch.setattr(filetools, "part_names", spy)
    r = run("split", src, "--records", "3", "-o", out, "--to", "jsonl")
    assert r.code == 0, r.err
    expected = ["people_1.jsonl.gz", "people_2.jsonl.gz", "people_3.jsonl.gz"]
    assert calls == [(3, expected)] and listing(out) == expected
    assert r.out.splitlines() == [str(out / n) for n in expected]
    r = run("split", src, "--records", "3", "-o", out, "--to", "jsonl")       # the same files exist now
    assert r.code == 1 and "Qism fayllar allaqachon bor (3 ta): " + ", ".join(expected) in r.err
    assert not hasattr(cli, "_split_part_names")                             # no copy of the naming rule


def test_split_remove_stale(run, tmp_path: Path) -> None:
    src = tmp_path / "x.json"
    jsongen.write_file(src, records(12), "array")
    out = tmp_path / "parts"
    out.mkdir()
    r = run("split", src, "--records", "1", "-o", out)
    assert r.code == 0 and listing(out) == [f"x_{k:02d}.json" for k in range(1, 13)]
    mine = out / "x_2023.json"                    # matches the pattern but is no part of a split: never removed
    mine.write_bytes(b"[]")
    r = run("split", src, "--parts", "10", "-o", out, "--force")
    assert r.code == 0, r.err
    assert "x_11.json, x_12.json, x_2023.json" in r.err                     # named in the stale warning ...
    assert "--force --remove-stale" in r.err                                # ... with the hint
    assert (out / "x_11.json").exists() and (out / "x_12.json").exists()
    r = run("-q", "split", src, "--parts", "10", "-o", out, "--force", "--remove-stale")
    assert r.code == 0, r.err
    removed = [line for line in r.err.splitlines() if line.startswith("O'chirildi")]
    assert removed == [f"O'chirildi (oldingi bo'lishdan qolgan qism): {out / n}" for n in ("x_11.json", "x_12.json")]
    assert "eski qismlar o'chirildi" not in r.err                           # (named once, above)
    assert "x_2023.json" in r.err and "--remove-stale" not in r.err         # still named; no hint now
    assert listing(out) == [f"x_{k:02d}.json" for k in range(1, 11)] + ["x_2023.json"]
    assert r.out.splitlines() == [str(out / f"x_{k:02d}.json") for k in range(1, 11)]
    assert [rec for k in range(1, 11) for rec in codec.loads((out / f"x_{k:02d}.json").read_bytes())] == records(12)
    assert mine.read_bytes() == b"[]" and _temps(out) == []
    r = run("--lang", "en", "split", "--help")
    assert "--remove-stale" in r.out and "data_11" in r.out


def test_split_never_replaces_a_part_with_unsaved_edits(run, tmp_path: Path) -> None:
    src = tmp_path / "data.json"
    jsongen.write_file(src, records(6), "array")
    out = tmp_path / "parts"
    out.mkdir()
    assert run("split", src, "--parts", "2", "-o", out).code == 0
    part = out / "data_1.json"
    before = {n: (out / n).read_bytes() for n in listing(out)}
    tab, sess = _edit(part)                       # another tab edits part 1
    try:
        for extra in ((), ("--force",)):
            r = run("split", src, "--parts", "2", "-o", out, *extra)
            assert r.code == 1, r.err
            assert EDITED_UZ in r.err and str(part) in r.err and "Maslahat:" in r.err
            assert "allaqachon bor" not in r.err  # not "add --force": --force cannot replace it either
        assert {n: (out / n).read_bytes() for n in listing(out)} == before
        assert not tab.check_changed()            # the edits can still be saved
    finally:
        sess.close()
        tab.close()
    r = run("split", src, "--parts", "2", "-o", out, "--force")             # kept for recovery: still refused
    assert r.code == 1 and EDITED_UZ in r.err
    _discard(part)
    r = run("split", src, "--parts", "2", "-o", out, "--force")
    assert r.code == 0, r.err
    assert _temps(out) == []


# ==================================================================================================
# 3. editstore.layer_sources
# ==================================================================================================
def test_layer_sources_lists_live_and_pending_layers(tmp_path: Path) -> None:
    src = tmp_path / "d.json"
    src.write_bytes(b'[{"v": 1}, {"v": 2}]')
    assert editstore.layer_sources() == {}
    doc = Document.open(src)
    doc.build_index()
    try:
        sess = EditSession.open(doc)
        try:
            assert normalize_path(src) not in editstore.layer_sources()     # no edit yet: no layer
            sess.set_value((0, ("v",)), "10")
            live = editstore.layer_sources()[normalize_path(src)]
            assert [e["key"] for e in live] == [editstore.layer_key(doc)]
            assert live[0]["location"] == "appdata" and Path(live[0]["dir"]).is_dir()
            assert normalize_path(live[0]["display"]) == normalize_path(src)
            live[0]["key"] = "changed"            # copies: the next call reads the index again
            assert editstore.layer_sources()[normalize_path(src)][0]["key"] == editstore.layer_key(doc)
        finally:
            sess.close()                          # pending: kept for recovery
        pending = editstore.layer_sources()[normalize_path(src)]
        assert len(pending) == 1 and pending[0]["steps"] == 1
        sess = EditSession.open(doc)
        sess.discard()
        sess.close()
    finally:
        doc.close()
    assert normalize_path(src) not in editstore.layer_sources()


class _SpyFile:
    def __init__(self, f: Any, reads: list[tuple]) -> None:
        self.f = f
        self.reads = reads

    def __enter__(self) -> _SpyFile:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.f.close()

    def fileno(self) -> int:
        return self.f.fileno()

    def read(self, *args: Any) -> bytes:
        self.reads.append(args)
        return self.f.read(*args)


def test_layer_sources_reads_the_index_once_with_a_small_buffer(tmp_path: Path, monkeypatch) -> None:
    root = editstore._edits_root()
    a, b = normalize_path(tmp_path / "a.json"), normalize_path(tmp_path / "Тошкент.json")
    entries = [
        {"key": "1" * 32, "dir": str(root / ("1" * 32)), "source": a, "updated": 10.0, "steps": 2},
        {"key": "2" * 32, "dir": str(root / ("2" * 32)), "source": b, "updated": 5.0},
        {"key": "3" * 32, "dir": str(tmp_path / "a.json.jredit"), "source": a, "updated": 20.0, "location": "beside"},
        {"key": 4, "dir": "x", "source": a},                     # not a valid entry: skipped
        {"key": "5" * 32, "dir": None, "source": a},
        {"key": "6" * 32, "dir": "x"},
        "not an object",
    ]
    index = root / "index.json"
    index.write_bytes(json.dumps({"version": 1, "layers": entries}, ensure_ascii=False).encode("utf-8"))
    reads: list[tuple] = []
    real_open = editstore.open_shared
    monkeypatch.setattr(editstore, "open_shared", lambda p, *a, **k: _SpyFile(real_open(p, *a, **k), reads))
    monkeypatch.setattr(editstore, "_index_load", lambda: pytest.fail("the 16 MiB private reader was used"))
    got = editstore.layer_sources()
    assert len(reads) == 1 and (reads[0] == () or reads[0][0] <= index.stat().st_size + 1)
    assert list(got) == [a, b]
    assert [e["key"] for e in got[a]] == ["3" * 32, "1" * 32]   # most recently updated first
    assert got[b] == [entries[1]]


def test_layer_sources_is_best_effort(tmp_path: Path, monkeypatch, caplog) -> None:
    index = editstore._edits_root() / "index.json"
    assert editstore.layer_sources() == {}                       # no index yet
    for raw in (b"{not json", b'["a list"]', b'{"layers": {"k": 1}}', b"\xff\xfe\x00", b"[" * 100_000 + b"]" * 100_000):
        index.write_bytes(raw)
        assert editstore.layer_sources() == {}
    index.write_bytes(json.dumps({"layers": [{"key": "k", "dir": "d", "source": "s"}]}).encode())
    assert editstore.layer_sources() == {"s": [{"key": "k", "dir": "d", "source": "s"}]}
    monkeypatch.setattr(editstore, "_INDEX_MAX_BYTES", 10)
    with caplog.at_level(logging.WARNING, logger="jsonreshape.editstore"):
        assert editstore.layer_sources() == {}
    assert "too large" in caplog.text


def test_edited_files_ask_find_pending_only_about_listed_files(tmp_path: Path, monkeypatch) -> None:
    files = [tmp_path / f"f{i}.json" for i in range(30)]
    for p in files:
        p.write_bytes(b'[{"v": 1}, {"v": 2}]')
    _pending(files[7])
    calls: dict[str, Any] = {"index": 0, "pending": []}
    real_load, real_find = editstore._index_load, editstore.find_pending

    def load() -> list:
        calls["index"] += 1
        return real_load()

    def find(doc: Any, **kw: Any) -> list:
        calls["pending"].append(Path(doc.source_path))
        return real_find(doc, **kw)

    monkeypatch.setattr(editstore, "_index_load", load)
    monkeypatch.setattr(editstore, "find_pending", find)
    assert filetools._edited_files(files) == [files[7]]
    assert calls["pending"] == [files[7]]
    assert calls["index"] == 1                    # find_pending's own read only (the listing used to read it too)
    calls["pending"].clear()
    assert writer.edited_targets([tmp_path / "missing.json", *files[:5]]) == []
    assert calls["pending"] == []
    _discard(files[7])
    assert filetools._edited_files(files) == []


# ==================================================================================================
# 4. writer outputs and CLI outputs never replace a file with unsaved edits
# ==================================================================================================
@pytest.mark.parametrize("how", ["tab", "pending"])
def test_writer_outputs_never_replace_a_file_with_unsaved_edits(tmp_path: Path, how: str, lang_en) -> None:
    src = tmp_path / "data.json"
    jsongen.write_file(src, records(5), "array")
    out = tmp_path / "out.json"
    mine = b'[{"v": "mine"}, {"v": "also mine"}]'
    out.write_bytes(mine)
    tab = sess = None
    if how == "tab":
        tab, sess = _edit(out)
    else:
        _pending(out)
    try:
        with Document.open(src) as doc:
            doc.build_index()
            node = doc.record_node(1)
            attempts = {
                "export json": lambda: writer.export(doc, out, ExportOptions("json")),
                "export csv": lambda: writer.export(doc, out, ExportOptions("csv")),
                "export_node": lambda: writer.export_node(doc, node, out),
                "export_many": lambda: writer.export_many([(doc, None)], out, ExportOptions("jsonl")),
                "extract gather": lambda: filetools.extract(doc, out, ExportOptions("json"), path="v"),
            }
            for name, attempt in attempts.items():
                with pytest.raises(UserInputError) as ei:
                    attempt()
                assert ei.value.key == "filetools.error.part_open_edited", name
                assert "out.json" in str(ei.value) and EDITED_EN in str(ei.value), name
        assert out.read_bytes() == mine and _temps(tmp_path) == []
    finally:
        if sess is not None:
            sess.close()
        if tab is not None:
            tab.close()
    _discard(out)
    with Document.open(src) as doc:
        doc.build_index()
        assert writer.export(doc, out, ExportOptions("json")).records == 5


@pytest.mark.parametrize("what", ["json", "jsonl", "csv", "xlsx", "sqlite", "export_node", "export_many", "gather"])
def test_writer_checks_the_target_again_right_before_the_commit(tmp_path: Path, monkeypatch, what: str) -> None:
    src = tmp_path / "data.json"
    jsongen.write_file(src, records(5), "array")
    out = tmp_path / "out.json"
    mine = b'[{"v": "mine"}]'
    out.write_bytes(mine)
    _pending(out)
    real = editstore.layer_sources
    seen: list[int] = []

    def late() -> dict:                           # the edit "begins" after the first check
        seen.append(1)
        return {} if len(seen) == 1 else real()

    monkeypatch.setattr(editstore, "layer_sources", late)
    with Document.open(src) as doc:
        doc.build_index()
        with pytest.raises(UserInputError) as ei:
            if what == "export_node":
                writer.export_node(doc, doc.record_node(2), out)
            elif what == "export_many":
                writer.export_many([(doc, None), (doc, range(1, 3))], out, ExportOptions("json"))
            elif what == "gather":
                filetools.extract(doc, out, ExportOptions("jsonl"), path="x")
            else:
                writer.export(doc, out, ExportOptions(what))
    assert ei.value.key == "filetools.error.part_open_edited"
    assert len(seen) >= 2                         # before the work, and right before the commit
    assert out.read_bytes() == mine and _temps(tmp_path) == []
    monkeypatch.setattr(editstore, "layer_sources", real)
    _discard(out)


_CLI_OUTPUTS = {
    "convert": ("convert", "{src}", "{out}"),
    "filter": ("filter", "{src}", "{out}", "--where", "id > 1"),
    "sort": ("sort", "{src}", "{out}", "--by", "id"),
    "extract": ("extract", "{src}", "{out}", "--gather", "v"),
    "schema": ("schema", "{src}", "{out}"),
    "repair": ("repair", "{src}", "{out}"),
    "dupes": ("dupes", "{src}", "--key", "v", "-o", "{out}"),
    "merge": ("merge", "{src}", "{src2}", "-o", "{out}"),
    "diff": ("diff", "{src}", "{src2}", "-o", "{out}"),
    "validate": ("validate", "{src}", "--schema", "{schema}", "--export", "{out}"),
    "search": ("search", "{src}", "Toshkent", "--export", "{out}"),
    "patch": ("patch", "{src}", "{patch}", "{out}"),
}


@pytest.mark.parametrize("command", sorted(_CLI_OUTPUTS))
def test_cli_outputs_with_unsaved_edits_are_refused_before_any_work(run, tmp_path: Path, monkeypatch,
                                                                    command: str) -> None:
    from jsonreshape import reader, repair

    src = tmp_path / "data.json"
    jsongen.write_file(src, records(6), "array")
    src2 = tmp_path / "data2.json"
    jsongen.write_file(src2, records(4), "array")
    schema = tmp_path / "s.schema.json"
    schema.write_bytes(b'{"type": "object"}')
    patch = tmp_path / "p.json"
    patch.write_bytes(b'[{"op": "replace", "path": "/0/v", "value": "x"}]')
    out = tmp_path / "out.json"
    mine = b'[{"v": "mine"}]'
    out.write_bytes(mine)
    _pending(out)
    work: list[str] = []
    real_open, real_repair = reader.Document.open, repair.repair_file
    monkeypatch.setattr(reader.Document, "open", lambda *a, **k: work.append("open") or real_open(*a, **k))
    monkeypatch.setattr(repair, "repair_file", lambda *a, **k: work.append("repair") or real_repair(*a, **k))
    argv = [part.format(src=src, src2=src2, out=out, schema=schema, patch=patch) for part in _CLI_OUTPUTS[command]]
    r = run(*argv)
    assert r.code == 1, r.err
    assert EDITED_UZ in r.err and str(out) in r.err and "Maslahat: Avval o'sha fayldagi" in r.err
    assert work == []                             # refused before any input was read
    assert out.read_bytes() == mine and _temps(tmp_path) == []
    monkeypatch.setattr(reader.Document, "open", real_open)
    _discard(out)
    r = run(*argv)
    assert r.code in (0, 5), r.err                # (validate / search: 5 / 0 by their own rules)
    assert out.read_bytes() != mine


# ==================================================================================================
# 5. fileio
# ==================================================================================================
def _old_utf16_len(s: str) -> int:
    """The rule before fix round L1 (a Python generator per character)."""
    return len(s) + sum(1 for ch in s if ord(ch) > 0xFFFF)


@pytest.mark.parametrize("text", [
    "", "abc", "Тошкент O'zbekiston ʻ ʼ ’", "😀", "a😀b𝔘c", "\ud800", "\udc00", "\udc00\ud800", "😀",
    "x" * 1000 + "😀" * 300 + "\udfff", "\U0010ffff\U00010000",
])
def test_utf16_len_is_unchanged(text: str) -> None:
    assert fileio._utf16_len(text) == _old_utf16_len(text)


@pytest.mark.skipif(not WINDOWS, reason="the long-path prefix is added on Windows only")
def test_long_path_decisions_are_unchanged(tmp_path: Path) -> None:
    def old_long_path(p: str) -> str:
        full = os.path.abspath(p)
        if _old_utf16_len(full) < fileio.LONG_PATH_THRESHOLD:
            return p
        return "\\\\?\\" + full

    base = str(tmp_path)
    checked = 0
    for k in range(0, 260, 3):
        for m in range(0, 70, 7):
            for tail in ("a" * k + "😀" * m, "ʻ" * k + "\ud800" * (m % 3)):
                p = base + "\\" + tail
                assert fileio.long_path(p) == old_long_path(p), (k, m)
                checked += 1
    assert checked > 1000


def test_prepared_cache_entries_is_the_fast_scan(tmp_path: Path, monkeypatch) -> None:
    seen: list[tuple] = []
    entries: list = []

    def scan(directory: Any = None, *, keep: Any = (), min_age: float = fileio.PREPARED_MIN_AGE) -> list:
        seen.append((directory, list(keep), min_age))
        return entries

    monkeypatch.setattr(fileio, "scan_prepared_entries", scan)
    assert fileio.prepared_cache_entries(tmp_path, keep=[tmp_path / "a.json"], min_age=5.0) is entries
    assert fileio.prune_prepared(1 << 40, keep=[tmp_path / "b.json"], directory=tmp_path) == 0
    assert seen == [(tmp_path, [tmp_path / "a.json"], 5.0),
                    (tmp_path, [tmp_path / "b.json"], fileio.PREPARED_MIN_AGE)]


UTF16_TEXT = '[{"id": 1, "v": "Тошкент"}, {"id": 2, "v": "😀 O\'zbekiston"}, {"id": 3}]'
UTF16_RECORDS = [{"id": 1, "v": "Тошкент"}, {"id": 2, "v": "😀 O'zbekiston"}, {"id": 3}]


def _cut_cases() -> list[tuple[str, bool, str, int, str]]:
    """(encoding, bom, case, cut byte, decoded text kept)."""
    out = []
    for enc, bom in (("utf-16-le", True), ("utf-16-be", True), ("utf-16-le", False)):
        data = ("﻿" if bom else "").encode(enc) + UTF16_TEXT.encode(enc)
        emoji = data.index("😀".encode(enc))
        kept_before = UTF16_TEXT[:UTF16_TEXT.index("😀")]
        out += [
            (enc, bom, "last byte", len(data) - 1, UTF16_TEXT[:-1]),
            (enc, bom, "odd byte in a pair", emoji + 1, kept_before),
            (enc, bom, "between the halves", emoji + 2, kept_before),
            (enc, bom, "inside the low half", emoji + 3, kept_before),
        ]
    return out


@pytest.mark.parametrize("enc,bom,case,cut,kept", _cut_cases())
def test_a_utf16_file_cut_inside_a_character_is_read_and_repaired(tmp_path: Path, enc: str, bom: bool, case: str,
                                                                  cut: int, kept: str) -> None:
    data = ("﻿" if bom else "").encode(enc) + UTF16_TEXT.encode(enc)
    src = tmp_path / "cut.json"
    src.write_bytes(data[:cut])
    pf = prepare_data_file(src)                   # (used to raise fileio.error.decode: nothing could run)
    assert pf.encoding == enc and pf.warnings == ("fileio.warning.utf16_truncated",)
    assert Path(pf.data_path).read_bytes() == kept.encode("utf-8")
    assert not any(n.endswith(".part") for n in os.listdir(Path(pf.data_path).parent))
    with Document.open(src) as doc:
        doc.build_index()
        assert doc.error is not None              # a truncated document: the records before the cut
        assert "fileio.warning.utf16_truncated" in doc.info.warnings
        whole = 3 if case == "last byte" else 1   # (only the "]" is cut / record 2 is cut inside its string)
        assert doc.record_count == whole
        assert [codec.loads(doc.record_bytes(i)) for i in range(whole)] == UTF16_RECORDS[:whole]
    dst = tmp_path / "fixed.json"
    rep = repair_file(src, dst)
    try:
        assert codec.loads(dst.read_bytes()) == UTF16_RECORDS[:whole]
        assert "fileio.warning.utf16_truncated" in rep.warnings
    finally:
        rep.close()


def test_utf16_cut_inside_a_complete_gzip_stream_and_invalid_data(tmp_path: Path, lang_en) -> None:
    data = "﻿".encode("utf-16-le") + UTF16_TEXT.encode("utf-16-le")
    gz = tmp_path / "cut.json.gz"
    gz.write_bytes(gzip.compress(data[:-1]))
    pf = prepare_data_file(gz)
    assert pf.compression == "gzip" and pf.warnings == ("fileio.warning.utf16_truncated",)
    assert Path(pf.data_path).read_bytes() == UTF16_TEXT[:-1].encode("utf-8")
    whole = tmp_path / "whole.json"               # a complete file: no warning
    whole.write_bytes(data)
    assert prepare_data_file(whole).warnings == ()
    bad = tmp_path / "bad.json"                   # invalid data INSIDE the file is still an error
    bad.write_bytes(data[:10] + "\udc00".encode("utf-16-le", "surrogatepass") + data[10:])
    with pytest.raises(FileFormatError) as ei:
        prepare_data_file(bad)
    assert ei.value.key == "fileio.error.decode" and ei.value.params["offset"] == 10


def test_cli_repairs_a_cut_utf16_file(run, tmp_path: Path) -> None:
    data = "﻿".encode("utf-16-be") + UTF16_TEXT.encode("utf-16-be")
    src = tmp_path / "cut.json"
    src.write_bytes(data[:data.index("😀".encode("utf-16-be")) + 2])
    dst = tmp_path / "fixed.json"
    r = run("repair", src, dst)
    assert r.code == 0, r.err
    assert "UTF-16 fayl belgining o'rtasida tugaydi" in r.err
    assert codec.loads(dst.read_bytes()) == UTF16_RECORDS[:1]


# ==================================================================================================
# 6. public accessors (textmatch, query)
# ==================================================================================================
def test_textquery_regex_accessors() -> None:
    q = TextQuery("o'z", "regex")
    assert q.regex is q._regex and q.regex.flags & re.IGNORECASE and q.regex_postfilter is False
    assert not TextQuery("abc", "regex", case_sensitive=True).regex.flags & re.IGNORECASE
    w = TextQuery("Toshkent", "regex", whole_word=True)
    assert w.regex.pattern != "Toshkent" and w.regex_postfilter is False    # the guards are in the pattern
    s = "Toshkentda 'Toshkent' ʻToshkentʼ"
    assert [m.span() for m in w.regex.finditer(s)] == w.find_spans(s) == [(12, 20), (23, 31)]
    assert TextQuery("Toshkent").regex is None and TextQuery("> 5", "number").regex is None
    with pytest.raises(AttributeError):
        w.regex = None                            # read-only


def test_regex_postfilter_when_the_guards_cannot_be_compiled(monkeypatch) -> None:
    monkeypatch.setattr(textmatch, "_whole_word_pattern", lambda pattern, flags: "(")
    q = TextQuery("Toshkent", "regex", whole_word=True)
    assert q.regex_postfilter is True and q.regex.pattern == "Toshkent"
    s = "Toshkentda Toshkent O'Toshkent"
    spans = [m.span() for m in q.regex.finditer(s) if textmatch.is_whole_word(s, *m.span())]
    assert spans == q.find_spans(s) == [(11, 19)]


def test_is_whole_word_is_the_rule_of_the_search() -> None:
    assert textmatch.is_whole_word is textmatch._is_whole_word
    assert not textmatch.is_whole_word("O'zbekiston", 0, 1)
    assert textmatch.is_whole_word("'Toshkent'", 1, 9) and textmatch.is_whole_word("ʻToshkentʼ", 1, 9)
    assert not textmatch.is_whole_word("Toshkentda", 0, 8) and textmatch.is_whole_word("Toshkent", 0, 8)


def test_json_equal_is_public() -> None:
    from jsonreshape import bulk, jsonpatch

    assert query.json_equal is query._eq and "json_equal" in query.__all__
    # bulk and jsonpatch compare with the query's equality (imported under the public name since the switch to
    # the public accessors; the private alias of the time before is accepted too)
    for mod in (bulk, jsonpatch):
        assert (getattr(mod, "json_equal", None) or getattr(mod, "_json_equal", None)) is query.json_equal, mod
    equal = [
        (N("1.10"), N("1.1")), (N("-0.0"), 0), (N("1e400"), N("10e399")), (N("0.1"), N("0.10")),
        (N("12345678901234567890"), N("12345678901234567890")), ("Тошкент", "Тошкент"), (None, None),
        ({"a": 1, "b": [1, {"c": None}]}, {"b": [1, {"c": None}], "a": N("1.0")}), ([], []), ({}, {}),
    ]
    different = [
        (1, "1"), (True, 1), (None, False), ([1, 2], [2, 1]), ({"a": 1}, {"a": 1, "b": 2}), ("O'z", "Oʻz"),
        (N("1e400"), N("1e401")), ([1], {"0": 1}), ("", None), (N("12345678901234567890"), N("12345678901234567891")),
    ]
    for a, b in equal:
        assert query.json_equal(a, b) and query.json_equal(b, a), (a, b)
    for a, b in different:
        assert not query.json_equal(a, b) and not query.json_equal(b, a), (a, b)
    deep_a: list = []
    deep_b: list = []
    cur_a, cur_b = deep_a, deep_b
    for _ in range(20_000):                       # any depth (iterative)
        cur_a.append([])
        cur_b.append([])
        cur_a, cur_b = cur_a[0], cur_b[0]
    cur_b.append(1)
    assert query.json_equal(deep_a, deep_a) and not query.json_equal(deep_a, deep_b)


# ==================================================================================================
# 7. validate: records that could not be validated
# ==================================================================================================
DEPTH = 20_000          # deeper than the decoder can parse: the record is "unreadable" (not validated)


@pytest.fixture
def deep_file(tmp_path: Path) -> Path:
    p = tmp_path / "deep.json"
    p.write_bytes(b'[{"id": 1}, {"id": 2, "deep": ' + b"[" * DEPTH + b"]" * DEPTH + b'}, {"id": 3}]')
    return p


def test_validate_records_not_checked_are_not_shown_to_match(run, tmp_path: Path, deep_file: Path) -> None:
    schema = tmp_path / "s.schema.json"
    schema.write_bytes(b'{"type": "object", "required": ["id"]}')
    r = run("validate", deep_file, "--schema", schema)
    assert r.code == 5, r.err                     # used to be 0: "valid" although a record was never checked
    assert r.out == ""
    assert "1 ta yozuvni o'qib bo'lmadi va u tekshirilmadi: #1." in r.err
    assert "Mosligi tasdiqlanmadi: 2 ta yozuv tekshirildi va xato topilmadi, lekin 1 ta yozuv tekshirilmadi" in r.err
    assert "Schema'ga mos:" not in r.err
    r = run("--lang", "en", "validate", deep_file, "--schema", schema)
    assert r.code == 5 and "Not confirmed: 2 records checked without issues, but 1 record could not be checked" in r.err
    r = run("-q", "validate", deep_file, "--schema", schema)
    assert r.code == 5 and "tekshirilmadi: #1." in r.err and "Mosligi" not in r.err     # the warning stays
    r = run("validate", deep_file, "--schema", schema, "--json")
    data = json.loads(r.out)
    assert r.code == 5 and data["ok"] is False and data["issue_count"] == 0
    assert data["skipped"] == 1 and data["unreadable"] == 1 and data["scanned"] == 2
    good = tmp_path / "good.json"                 # every record checked, no issue: 0 (and "ok" true)
    good.write_bytes(b'[{"id": 1}, {"id": 3}]')
    r = run("validate", good, "--schema", schema, "--json")
    assert r.code == 0 and json.loads(r.out)["ok"] is True


def test_validate_issues_and_records_not_checked(run, tmp_path: Path, deep_file: Path) -> None:
    schema = tmp_path / "s.schema.json"
    schema.write_bytes(b'{"type": "object", "required": ["id", "name"]}')
    r = run("validate", deep_file, "--schema", schema)
    assert r.code == 5, r.err
    assert len(r.out.splitlines()) == 2 and "(required)" in r.out
    assert "Schema'ga mos emas: 2 ta yozuvdan 2 tasida 2 ta xato" in r.err
    assert "Bundan tashqari 1 ta yozuv tekshirilmadi" in r.err
    r = run("--lang", "en", "validate", deep_file, "--schema", schema)
    assert "Besides, 1 record could not be checked" in r.err


def test_validate_help_names_the_exit_codes(run) -> None:
    r = run("validate", "--help")
    assert r.code == 0 and "ba'zi yozuvlarni tekshirib bo'lmadi" in r.out
    r = run("--lang", "en", "validate", "--help")
    assert "some records could not be checked" in r.out
