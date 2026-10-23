"""``jsonreshape diff A B [--key KEY | --by-position | --align] [--array-key [PATH=]FIELD ...]
[--numbers value|exact|text] [--ignore-order] [--max-changes N] [--limit N] [--summary] [--json] [-o FILE]
[--exit-code] [--memory-limit SIZE] [input options]`` (docs/ARCHITECTURE.md §14.0, §14.6, §14.8;
s5/map_repair_diff.md §4.6; AGENT_PROMPT §4.5, §13).

The text output (``~ + - >`` markers, the key and both record numbers, values as compact JSON, colours only on
a terminal), every mode (align, key, position, document; the wrapper), canonical number equality (``1 == 1.0``,
``1.10 == 1.1``; ``--numbers text`` reports them), array element moves by key, ``--ignore-order``, ``--limit`` /
``--summary`` / ``--max-changes``, JSON Lines on stdout, every difference written to JSON / JSON Lines / CSV in
the same pass, ``--exit-code`` (5 / 0), outputs that are an input refused in any spelling before anything is
read, the size limit without a key (exit 1 with the advice), JSON errors naming the file (exit 2), gzip /
UTF-16 inputs, every core backend.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fs_helpers import case_folds

import jsongen
from jsonreshape import cli, codec, i18n
from jsonreshape.tasks import CancelToken

ROOT = Path(__file__).resolve().parents[1]
N = codec.loads


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


def proc(*argv: Any, timeout: float = 240) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "jsonreshape", *map(str, argv)], cwd=ROOT, env=env,
                          capture_output=True, timeout=timeout)


A_TEXT = ('[{"id": 1, "n": 1, "tags": ["a", "b"], "items": [{"k": 1, "v": "x"}, {"k": 2, "v": "y"}]},\n'
          ' {"id": 2, "n": 1.10, "s": "Тошкент"},\n {"id": 3, "n": 2}]')
B_TEXT = ('[{"id": 1, "n": 1.0, "tags": ["b", "a"], "items": [{"k": 2, "v": "y"}, {"k": 1, "v": "z"}]},\n'
          ' {"id": 2, "n": 1.1, "s": "Тошкент"},\n {"id": 4, "n": 1e400}]')


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "eski.json", tmp_path / "yangi.json"
    a.write_text(A_TEXT, encoding="utf-8")
    b.write_text(B_TEXT, encoding="utf-8")
    return a, b


TAG_AND_ITEM_LINES = ['    + tags[0]: "b"', '    - tags[1]: "b"', '    - items[0]: {"k":1,"v":"x"}',
                      '    + items[1]: {"k":1,"v":"z"}']


# ----------------------------------------------------------------------------------
# modes and the text output
# ----------------------------------------------------------------------------------
def test_diff_align_text(run, pair: tuple[Path, Path]) -> None:
    a, b = pair
    r = run("diff", a, b)
    assert r.code == 0, r.err
    assert r.out.splitlines() == ["~ (A #0, B #0)", *TAG_AND_ITEM_LINES,
                                  "~ (A #2, B #2)", "    ~ id: 3 → 4", "    ~ n: 2 → 1e400"]
    assert ("Farqli yozuvlar: o'zgargan 2, o'chirilgan 0, qo'shilgan 0; bir xil 1 (mazmun bo'yicha tekislab; "
            "yozuvlar: A 3, B 3;") in r.err


def test_diff_by_key_and_exit_code(run, pair: tuple[Path, Path]) -> None:
    a, b = pair
    r = run("diff", a, b, "--key", "id", "--exit-code")
    assert r.code == 5, r.err
    assert r.out.splitlines() == ["~ id=1  (A #0, B #0)", *TAG_AND_ITEM_LINES, "- id=3  (A #2)", "+ id=4  (B #2)"]
    assert "(kalit bo'yicha: id;" in r.err
    r = run("--lang", "en", "diff", a, b, "--key", "id")
    assert r.code == 0 and "Differing records: changed 1, removed 1, added 1; equal 1 (by key: id;" in r.err
    r = run("diff", a, a, "--exit-code")
    assert r.code == 0 and r.out == "" and "Farq yo'q: A va B bir xil" in r.err


def test_diff_numbers_text_and_by_position(run, pair: tuple[Path, Path]) -> None:
    a, b = pair
    r = run("diff", a, b, "--key", "id", "--numbers", "text")
    lines = r.out.splitlines()
    assert lines[1] == "    ~ n: 1 → 1.0" and "~ id=2  (A #1, B #1)" in lines and "    ~ n: 1.10 → 1.1" in lines
    r = run("diff", a, b, "--by-position")
    assert r.code == 0 and "(o'rni bo'yicha;" in r.err and r.out.splitlines()[0] == "~ (A #0, B #0)"
    r = run("diff", a, b, "--key", "id", "--by-position")
    assert r.code == 1 and "birga ishlatib bo'lmaydi" in r.err


def test_diff_explicit_align_numbers_exact_and_memory_limit(run, pair: tuple[Path, Path], monkeypatch) -> None:
    from jsonreshape import diff as diff_mod

    a, b = pair
    seen: list[dict[str, Any]] = []
    original = diff_mod.iter_record_diffs

    def spy(*args: Any, **kwargs: Any):
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(diff_mod, "iter_record_diffs", spy)
    auto = run("diff", a, b)
    r = run("diff", a, b, "--align", "--numbers", "exact")
    assert r.code == 0 and r.out == auto.out and seen[-1]["mode"] == "align"
    assert seen[-1]["opts"].numbers == "value" and seen[-2]["mode"] == "auto"
    r = run("diff", a, b, "--key", "id", "--memory-limit", "20MB", "--summary")
    assert r.code == 0 and seen[-1]["memory_limit"] == 20 << 20 and seen[-1]["max_details"] == 0


def test_diff_array_key_moves(run, pair: tuple[Path, Path]) -> None:
    a, b = pair
    r = run("diff", a, b, "--key", "id", "--array-key", "items=k")
    assert r.code == 0, r.err
    lines = r.out.splitlines()
    assert "    > items[0] → items[1]" in lines and '    ~ items[0].v: "x" → "z"' in lines
    assert "1 ta element joyi o'zgargan" in r.err


def test_diff_ignore_order(run, tmp_path: Path) -> None:
    a, b, c = tmp_path / "a.jsonl", tmp_path / "b.jsonl", tmp_path / "c.jsonl"
    a.write_bytes(b'{"id":1,"t":[1,2,3]}\n{"id":2,"t":["x"]}\n')
    b.write_bytes(b'{"id":2,"t":["x"]}\n{"t":[1,2,3],"id":1.0}\n')     # records reordered, members too, 1 == 1.0
    c.write_bytes(b'{"id":2,"t":["x"]}\n{"t":[3,2,1],"id":1}\n')       # an array reordered inside a record
    r = run("diff", a, b, "--ignore-order", "--exit-code")
    assert r.code == 0 and r.out == "" and "(tartibsiz, butun yozuv bo'yicha;" in r.err
    r = run("diff", a, b, "--exit-code")
    assert r.code == 5
    # records are matched by their whole value (arrays in order); paired by --key, arrays compare as multisets
    r = run("diff", a, c, "--ignore-order", "--exit-code")
    assert r.code == 5 and r.out.splitlines() == ["- (A #0)", "+ (B #1)"]
    r = run("diff", a, c, "--key", "id", "--ignore-order", "--exit-code")
    assert r.code == 0


def test_diff_wrapper_and_single_documents(run, tmp_path: Path) -> None:
    w1, w2 = tmp_path / "w1.json", tmp_path / "w2.json"
    recs = [{"id": i} for i in range(5)]
    raw = jsongen.build_document(recs, "wrapped")
    w1.write_bytes(raw)
    w2.write_bytes(raw.replace(b'"version":1', b'"version":2'))
    r = run("diff", w1, w2, "--record-path", "data")
    assert r.code == 0, r.err
    assert r.out.splitlines() == ["~ (tashqi o'rama)", "    ~ meta.version: 1 → 2"]
    assert "tashqi o'ramalar farq qiladi" in r.err
    s1, s2 = tmp_path / "s1.json", tmp_path / "s2.json"
    s1.write_bytes(b'{"a": {"b": 1, "c": [1, 2]}}')
    s2.write_bytes(b'{"a": {"b": 2, "c": [1, 2, 3]}}')
    r = run("diff", s1, s2, "--format", "single", "--exit-code")
    assert r.code == 5 and r.out.splitlines() == ["~ a.b: 1 → 2", "+ a.c[2]: 3"]
    assert "(bitta hujjat sifatida;" in r.err


# ----------------------------------------------------------------------------------
# limits, JSON Lines, files
# ----------------------------------------------------------------------------------
def test_diff_limits(run, pair: tuple[Path, Path]) -> None:
    a, b = pair
    r = run("diff", a, b, "--key", "id", "--limit", "1")
    assert r.out.splitlines() == ["~ id=1  (A #0, B #0)", *TAG_AND_ITEM_LINES]
    assert "Faqat birinchi farqli yozuv ko'rsatildi (jami 3 ta)" in r.err
    r = run("diff", a, b, "--key", "id", "--limit", "0")
    assert len(r.out.splitlines()) == 7 and "Faqat" not in r.err
    r = run("diff", a, b, "--key", "id", "--summary")
    assert r.out == "" and "o'zgargan 1, o'chirilgan 1, qo'shilgan 1" in r.err
    r = run("diff", a, b, "--key", "id", "--max-changes", "1", "--limit", "1")
    assert r.out.splitlines() == ["~ id=1  (A #0, B #0)", '    + tags[0]: "b"',
                                  "    … yana 3 ta o'zgarish (hammasi uchun --max-changes N yoki -o FAYL)"]


def test_diff_json_lines(run, pair: tuple[Path, Path]) -> None:
    a, b = pair
    r = run("diff", a, b, "--key", "id", "--json", "--limit", "1")
    assert r.code == 0, r.err
    rows = [json.loads(line) for line in r.out.splitlines()]
    assert [row.get("status") for row in rows[:-1]] == ["changed", "removed", "added"]   # --limit is for text
    assert rows[0]["key"] == 1 and rows[0]["a"] == 0 and rows[0]["b"] == 0
    assert rows[0]["changes"][0] == {"op": "added", "path": "tags[0]", "pointer": "/tags/0", "b": "b"}
    summary = rows[-1]["summary"]
    assert summary["mode"] == "key" and summary["changed"] == 1 and summary["equal"] == 1
    added = codec.loads(r.out.splitlines()[2])
    assert added["changes"][0]["b"]["n"] == N("1e400")                              # exact in the JSON text


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv"])
def test_diff_output_file(run, pair: tuple[Path, Path], tmp_path: Path, fmt: str) -> None:
    a, b = pair
    out = tmp_path / f"farqlar.{fmt}"
    r = run("diff", a, b, "--key", "id", "-o", out, "--limit", "1")
    assert r.code == 0, r.err
    assert len(r.out.splitlines()) == 5 and "3 ta farqli yozuv yozildi" in r.err   # the file has every record
    data = out.read_bytes()
    if fmt == "json":
        value = codec.loads(data)
        assert [x["status"] for x in value["records"]] == ["changed", "removed", "added"]
        assert value["summary"]["changed"] == 1
    elif fmt == "jsonl":
        rows = [codec.loads(line) for line in data.splitlines()]
        assert [x.get("status") for x in rows] == ["changed", "removed", "added", None] and "summary" in rows[-1]
    else:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        assert rows[0] == ["status", "key", "a_index", "b_index", "op", "path", "a_value", "b_value"]
        assert rows[1][:6] == ["changed", "1", "0", "0", "added", "tags[0]"]
    assert b"1e400" in data


def test_diff_outputs_are_never_an_input(run, pair: tuple[Path, Path], tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import reader

    a, b = pair
    before = a.read_bytes(), b.read_bytes()
    opened: list[Any] = []
    original = reader.Document.open
    monkeypatch.setattr(reader.Document, "open", lambda *x, **k: opened.append(x) or original(*x, **k))
    monkeypatch.chdir(tmp_path)
    # another spelling of the same name only where the file system folds case (NTFS, APFS)
    upper = [str(a).upper(), "./sub/../ESKI.JSON"] if case_folds(tmp_path) else []
    for target in (a, b, "yangi.json", "./sub/../eski.json", *upper):
        r = run("diff", a, b, "-o", target)
        assert r.code == 1 and "manba faylning o'ziga" in r.err, target
    r = run("diff", a, b, "--out", "x.txt")
    assert r.code == 1 and ".json, .jsonl, .csv" in r.err
    r = run("diff", a, b, "--key", "items[*]")
    assert r.code == 1
    assert opened == [] and (a.read_bytes(), b.read_bytes()) == before


# ----------------------------------------------------------------------------------
# sizes, errors, inputs
# ----------------------------------------------------------------------------------
def test_diff_too_big_without_a_key(run, pair: tuple[Path, Path]) -> None:
    from jsonreshape import settings

    a, b = pair
    settings.set_settings(settings.Settings(diff_align_max_bytes=16))
    r = run("diff", a, b)
    assert r.code == 1 and "--key" in r.err and "--by-position" in r.err
    r = run("diff", a, b, "--key", "id", "--summary")
    assert r.code == 0


def test_diff_json_error_names_the_file(run, pair: tuple[Path, Path], tmp_path: Path) -> None:
    a, _b = pair
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"id": 1}, {"id": ')
    r = run("diff", a, bad, "--key", "id")
    assert r.code == 2 and f"Fayl: {bad}" in r.err and r.out == ""
    r = run("diff", a, bad, "--key", "id", "--allow-partial", "--summary")
    assert r.code == 0 and f"Ogohlantirish: {bad}: Faylda JSON xatosi bor" in r.err
    assert r.err.count("Faylda JSON xatosi bor") == 1                       # the tool's own note is not repeated


def test_diff_gzip_and_utf16_inputs(run, pair: tuple[Path, Path], tmp_path: Path) -> None:
    import gzip

    a, b = pair
    base = run("diff", a, b, "--key", "id", "--json").out
    gz = tmp_path / "eski.json.gz"
    gz.write_bytes(gzip.compress(a.read_bytes()))
    u16 = tmp_path / "yangi16.json"
    u16.write_bytes("﻿".encode("utf-16-le") + B_TEXT.encode("utf-16-le"))
    r = run("diff", gz, u16, "--key", "id", "--json")
    assert r.code == 0, r.err
    strip = [json.loads(line) for line in r.out.splitlines()]
    expected = [json.loads(line) for line in base.splitlines()]
    for rows in (strip, expected):
        rows[-1]["summary"].pop("elapsed")
    assert strip == expected


def test_diff_colours_only_on_a_terminal(pair: tuple[Path, Path]) -> None:
    from jsonreshape.diff import Change

    class _Console:
        color = True

    class _Ctx:
        console = _Console()

    printer = cli._DiffPrinter(_Ctx(), "id", limit=0, json_lines=False, quiet=False)   # type: ignore[arg-type]
    line = printer.change_line(Change("changed", ("n",), ("n",), 1, 2), "    ")
    assert line == "    \x1b[33m~ n: 1 → 2\x1b[0m"
    _Ctx.console.color = False
    assert cli._DiffPrinter(_Ctx(), "id", limit=0, json_lines=False, quiet=False).change_line(  # type: ignore[arg-type]
        Change("added", None, ("t", 0), b="x"), "") == '+ t[0]: "x"'


def test_diff_help(run) -> None:
    r = run("diff", "--help")
    assert r.code == 0 and "solishtirish parametrlari:" in r.out and "--exit-code" in r.out
    r = run("--lang", "en", "diff", "--help")
    assert "comparison options:" in r.out and "Exit codes:" in r.out


@pytest.mark.slow
def test_diff_with_every_core_backend(pair: tuple[Path, Path]) -> None:
    from jsonreshape import core

    a, b = pair
    outputs = []
    for backend in core.available_backends():
        p = proc("--backend", backend, "diff", a, b, "--json")
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        rows = [json.loads(line) for line in p.stdout.decode("utf-8").splitlines()]
        rows[-1]["summary"].pop("elapsed")
        outputs.append(rows)
    assert all(o == outputs[0] for o in outputs)
