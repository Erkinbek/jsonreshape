"""Stage-5 options of the existing export commands and the CLI as a whole (docs/ARCHITECTURE.md §14.4, §14.8).

* ``--select SPEC`` on ``filter`` / ``sort`` / ``convert``: 0-based ``N``, ``A:B`` (``A`` .. ``B-1``), ``A:``,
  ``:B``, comma lists kept in the given order, ``#N``; out of range / empty / syntax errors (exit 1, nothing
  written); the selection taken first, then the filter (in the selection's order), then the sort;
* ``--no-wrapper`` applies to every output format (JSON Lines, CSV, XLSX, SQLite: no "ignored" warning, no
  "wrapper not written" note, no ``_wrapper`` table) -- the filetools agent's request;
* the help of every new command in Uzbek and English (localized argparse phrases), the examples of their
  ``--help`` safe in cmd.exe / PowerShell / bash and parsed by the real parser (their filters compile), the
  command list and exit code 5 in the main help, ``EXIT_DIFFERENT``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import signal
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import cli, codec, i18n
from jsonreshape.i18n import MESSAGES
from jsonreshape.tasks import CancelToken

N = codec.loads
NEW_COMMANDS = ("stats", "dupes", "schema", "validate", "split", "merge", "extract", "repair", "diff")


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


RECS = [{"id": i, "name": ["Toshkent", "Тошкент", "O'zbekiston", "Oʻzbekiston"][i % 4],
         "v": [N("0.1"), N("1e400"), N("12345678901234567890"), N("-0.0"), N("1.10")][i % 5]} for i in range(10)]


@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "people.json"
    jsongen.write_file(p, RECS, "array", style="pretty2")
    return p


def ids(p: Path) -> list[int]:
    data = p.read_bytes()
    if p.suffix == ".jsonl":
        return [codec.loads(line)["id"] for line in data.splitlines()]
    return [r["id"] for r in codec.loads(data)]


# ----------------------------------------------------------------------------------
# --select
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("spec, expected", [
    ("3", [3]),
    ("#3", [3]),
    ("2:5", [2, 3, 4]),
    ("7:", [7, 8, 9]),
    (":2", [0, 1]),
    ("8,1,4:6", [8, 1, 4, 5]),
    ("5:99", [5, 6, 7, 8, 9]),
    ("0:", list(range(10))),
])
def test_select_forms(run, src: Path, tmp_path: Path, spec: str, expected: list[int]) -> None:
    out = tmp_path / "o.json"
    r = run("convert", src, out, "--select", spec)
    assert r.code == 0, r.err
    assert ids(out) == expected
    assert f"{len(expected)} / 10 yozuv -> " in r.err
    data = out.read_bytes()
    for rec in expected:   # exact numbers and Uzbek text untouched
        assert codec.dumps(RECS[rec]["v"]) in data


@pytest.mark.parametrize("spec, message", [
    ("10", "faylda yo'q: faylda 10 ta yozuv bor (#0 dan #9 gacha)"),
    ("12:", "faylda yo'q"),
    ("5:5", "birorta ham yozuv yo'q"),
    ("a:b", "noto'g'ri yozilgan"),
    ("-1", "noto'g'ri yozilgan"),
    ("1,,2", "noto'g'ri yozilgan"),
])
def test_select_errors_write_nothing(run, src: Path, tmp_path: Path, spec: str, message: str) -> None:
    out = tmp_path / "o.json"
    r = run("convert", src, out, f"--select={spec}")
    assert r.code == 1 and message in r.err, r.err
    assert not out.exists()


@pytest.mark.parametrize("command", ["convert", "stats", "dupes", "split", "extract", "validate"])
def test_select_syntax_is_checked_before_the_input_is_read(run, src: Path, tmp_path: Path, monkeypatch,
                                                           command: str) -> None:
    from jsonreshape import reader

    opened: list[Any] = []
    original = reader.Document.open
    monkeypatch.setattr(reader.Document, "open", lambda *a, **k: opened.append(a) or original(*a, **k))
    schema = tmp_path / "s.json"
    schema.write_bytes(b"{}")
    extra = {"convert": [tmp_path / "o.json"], "stats": [], "dupes": ["--key", "id"], "split": ["--parts", "2"],
             "extract": [tmp_path / "o.json"], "validate": ["--schema", schema]}[command]
    for spec in ("a:b", "1,,2", "5:5", "-1", " "):
        r = run(command, src, *extra, f"--select={spec}")
        assert r.code == 1 and "tanlov" in r.err.lower(), (spec, r.err)
    assert opened == []
    r = run(command, src, *extra, "--select", "10")          # beyond the end: known once the file is indexed
    assert r.code == 1 and "faylda yo'q" in r.err and opened


def test_select_then_filter_then_sort(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    r = run("filter", src, out, "--select", "9,2,5,0,7", "--where", "id > 1")
    assert r.code == 0 and ids(out) == [9, 2, 5, 7] and "4 / 10 yozuv" in r.err
    r = run("sort", src, out, "--by", "-id", "--select", "2:6")
    assert r.code == 0 and ids(out) == [5, 4, 3, 2]
    r = run("convert", src, out, "--select", "1,3,5,7", "--where", "id != 3", "--sort-by", "-id")
    assert r.code == 0 and ids(out) == [7, 5, 1]
    r = run("--lang", "en", "convert", src, out, "--select", "0", "--jsonl")
    assert r.code == 0 and "1 / 10 records -> " in r.err


def test_select_on_member_records(run, tmp_path: Path) -> None:
    p = tmp_path / "keyed.json"
    jsongen.write_file(p, [{"v": i} for i in range(5)], "keyed", keys=["a", "b", "c", "d", "e"])
    out = tmp_path / "o.json"
    r = run("convert", p, out, "--select", "4,0")
    assert r.code == 0 and list(codec.loads(out.read_bytes())) == ["e", "a"]


# ----------------------------------------------------------------------------------
# --no-wrapper for every format
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv", "xlsx", "sqlite"])
def test_no_wrapper_applies_to_every_format(run, tmp_path: Path, fmt: str) -> None:
    w = tmp_path / "w.json"
    jsongen.write_file(w, [{"id": i} for i in range(3)], "wrapped")
    out = tmp_path / f"o.{fmt}"
    r = run("convert", w, out, "--record-path", "data", "--no-wrapper")
    assert r.code == 0, r.err
    assert "e'tiborga olinmadi" not in r.err and "yozilmadi" not in r.err and "Ogohlantirish" not in r.err
    if fmt == "json":
        assert codec.loads(out.read_bytes()) == [{"id": 0}, {"id": 1}, {"id": 2}]
    elif fmt == "jsonl":
        assert ids(out) == [0, 1, 2]
    elif fmt == "csv":
        assert list(csv.reader(io.StringIO(out.read_text(encoding="utf-8-sig")))) == [["id"], ["0"], ["1"], ["2"]]
    elif fmt == "sqlite":
        con = sqlite3.connect(out)
        try:
            tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        finally:
            con.close()
        assert "_wrapper" not in tables
    # without --no-wrapper a JSON Lines / CSV / XLSX output names the part it cannot hold
    out2 = tmp_path / f"keep.{fmt}"
    r = run("convert", w, out2, "--record-path", "data")
    assert r.code == 0
    assert ("yozilmadi" in r.err) == (fmt in ("jsonl", "csv", "xlsx"))


# ----------------------------------------------------------------------------------
# help texts and examples of the new commands
# ----------------------------------------------------------------------------------
def test_new_commands_in_the_main_help_and_usage_error(run) -> None:
    r = run("--help")
    assert r.code == 0
    for command in NEW_COMMANDS:
        assert f"    {command}" in r.out, command
    assert "5 - ma'lumot mos emas" in r.out
    r = run()
    assert r.code == 1 and "stats, dupes, schema, validate, split, merge, extract, repair, diff" in r.err
    assert cli.EXIT_DIFFERENT == 5 and "EXIT_DIFFERENT" in cli.__all__


@pytest.mark.parametrize("command", NEW_COMMANDS)
def test_help_of_every_new_command(run, command: str) -> None:
    r = run(command, "--help")
    assert r.code == 0, r.err
    assert r.out.startswith(f"foydalanish: jsonreshape {command}")
    assert "parametrlar:" in r.out and "Misollar:" in r.out and "umumiy parametrlar:" in r.out
    for english in ("usage:", "show this help", "options:", "Examples:"):
        assert english not in r.out, english
    r = run("--lang", "en", command, "--help")
    assert r.code == 0 and r.out.startswith(f"usage: jsonreshape {command}") and "Examples:" in r.out
    assert "general options:" in r.out


_UNQUOTED_BAD = frozenset("'`$&|<>^%;(){}[]@#,")


def _examples() -> list[str]:
    out = []
    for command in NEW_COMMANDS:
        for lang in ("uz", "en"):
            for line in MESSAGES[f"cli.cmd.{command}.epilog"][lang].splitlines():
                if line.startswith("  jsonreshape "):
                    out.append(line.strip())
    return out


def _win_split(line: str) -> list[str]:
    """Arguments as the Windows C runtime splits a command line without backslashes."""
    out: list[str] = []
    buf: list[str] = []
    quoted = seen = False
    for ch in line:
        if ch == '"':
            quoted = not quoted
            seen = True
        elif ch == " " and not quoted:
            if buf or seen:
                out.append("".join(buf))
            buf, seen = [], False
        else:
            buf.append(ch)
    assert not quoted, line
    if buf or seen:
        out.append("".join(buf))
    return out


def _shell_problems(line: str) -> list[str]:
    problems = []
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif quoted:
            nxt = line[i + 1:i + 2]
            if ch in "`%" or (ch == "$" and (nxt.isalnum() or nxt in "_{(:")):
                problems.append(f"{ch!r} inside double quotes at {i}")
        elif ch in _UNQUOTED_BAD:
            problems.append(f"{ch!r} outside double quotes at {i}")
    return problems


def test_examples_of_the_new_commands_are_safe_and_parse() -> None:
    from jsonreshape.query import compile_filter

    examples = _examples()
    assert len(examples) >= 70
    for line in examples:
        assert not _shell_problems(line), (line, _shell_problems(line))
        argv = cli._join_dash_values(_win_split(line)[1:])
        with cli.localized_argparse():
            args = cli._parse(cli.build_parser(), argv)
        assert args.command in NEW_COMMANDS, line
        if getattr(args, "where", None) is not None:
            compile_filter(args.where, lang="expr")
        if getattr(args, "select", None) is not None:
            from jsonreshape.filetools import parse_select

            parse_select(args.select, 10_000_000)


def test_main_epilog_examples_parse() -> None:
    for lang in ("uz", "en"):
        for line in MESSAGES["cli.help.epilog"][lang].splitlines():
            if line.startswith("  jsonreshape "):
                assert not _shell_problems(line), line
                with cli.localized_argparse():
                    cli._parse(cli.build_parser(), cli._join_dash_values(_win_split(line.strip())[1:]))


class _CountdownEvent(threading.Event):
    """An Event that reports "set" from the n-th ``is_set()`` call on (a cancellation in the middle of a run)."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.calls = 0
        self.n = n

    def is_set(self) -> bool:  # noqa: D401
        self.calls += 1
        return self.calls >= self.n or super().is_set()


@pytest.mark.parametrize("command", ["split", "merge", "validate", "diff", "dupes"])
def test_cancelled_in_the_middle_leaves_nothing_behind(run, tmp_path: Path, monkeypatch, command: str) -> None:
    from jsonreshape import diff as diff_mod
    from jsonreshape import dupes as dupes_mod
    from jsonreshape import filetools, jschema

    src = tmp_path / "in" / "many.jsonl"
    src.parent.mkdir()
    jsongen.write_file(src, [{"id": i % 7000, "name": f"Toshkent {i}"} for i in range(20000)], "jsonl")
    other = tmp_path / "in" / "other.jsonl"
    jsongen.write_file(other, [{"id": i, "name": "x"} for i in range(20000)], "jsonl")
    schema = tmp_path / "in" / "s.json"
    schema.write_bytes(b'{"type": "object", "properties": {"id": {"maximum": 10}}}')
    out = tmp_path / "out"
    out.mkdir()
    token = CancelToken()
    seen: dict[str, Any] = {}
    target, name, n, argv = {
        "split": (filetools, "split", 5, ["split", src, "--records", "500", "-o", out]),
        "merge": (filetools, "merge", 3, ["merge", src, other, "-o", out / "m.jsonl"]),
        "validate": (jschema, "validate", 3, ["validate", src, "--schema", schema, "--export", out / "i.jsonl"]),
        "diff": (diff_mod, "iter_record_diffs", 3, ["diff", src, other, "--key", "id", "-o", out / "d.jsonl"]),
        "dupes": (dupes_mod, "find_duplicates", 3, ["dupes", src, "--key", "id", "-o", out / "d.jsonl"]),
    }[command]
    original = getattr(target, name)

    def cancel_inside(*args: Any, **kwargs: Any) -> Any:
        token._event = seen["event"] = _CountdownEvent(n)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, cancel_inside)
    r = run(*argv, cancel=token)
    assert r.code == 3 and r.err.strip().endswith("Amal bekor qilindi."), r.err
    assert seen["event"].calls >= n                                   # it was cancelled inside the run
    assert list(out.iterdir()) == []                                  # no output, no temporary file


def test_json_reports_use_exact_numbers(run, tmp_path: Path) -> None:
    """Every ``--json`` report of the new commands is codec JSON: big and long numbers stay exact."""
    p = tmp_path / "n.json"
    jsongen.write_file(p, [{"v": N("1e400")}, {"v": N("12345678901234567890.000000000000000001")}], "array")
    r = run("stats", p, "--field", "v", "--json")
    data = json.loads(r.out)["fields"][0]
    assert data["number"]["min"] == "12345678901234567890.000000000000000001" and data["number"]["max"] == "1e400"
    assert [v["json"] for v in data["top"]] == ["1e400", "12345678901234567890.000000000000000001"]
    schema = tmp_path / "s.json"
    schema.write_bytes(b'{"type": "object", "properties": {"v": {"maximum": 1e300}}}')
    r = run("validate", p, "--schema", schema, "--json")
    assert r.code == 5
    issue = codec.loads(r.out.encode())["issues"][0]
    assert issue["record"] == 0 and issue["keyword"] == "maximum"
