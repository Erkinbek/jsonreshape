"""Regression tests of the review fixes in the command line (phase 5a, workstream "cli").

One section per finding (lens/id in the comments): record paths that lead nowhere
(blackbox F7, codereview CR-01, requirements F4), a source changed while it is read
(requirements F1), several JSON values in one file (blackbox F2), ``--memory-limit``
(codereview CR-04, requirements F6), Windows-safe ``--help`` examples (codereview CR-05,
requirements F5), search following the index (requirements F11), wording (blackbox F16/F17,
codereview CR-09/CR-11/CR-14), JMESPath hints (requirements F17), the wrapper in ``inspect``
(requirements F22), OS errors (blackbox F14), filter presets (requirements F20) and
``--sort-keys alpha|codepoint`` (requirements F16).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import cli, codec, i18n
from jsonreshape.errors import FileChangedError, JsonDecodeError
from jsonreshape.i18n import MESSAGES, has, t

MiB = 1 << 20
OKINA = chr(0x02BB)


# ----------------------------------------------------------------------------------
# fixtures and helpers (as in test_cli.py)
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _cli_isolation(monkeypatch: pytest.MonkeyPatch):
    old_backend = os.environ.get("JSONRESHAPE_BACKEND")
    monkeypatch.setenv("JSONRESHAPE_LANG", "uz")
    monkeypatch.delenv("NO_COLOR", raising=False)
    old_lang = i18n.get_language()
    i18n.set_language("uz")   # whatever an earlier test left behind
    root = logging.getLogger()
    old_level = root.level
    old_sigint = signal.getsignal(signal.SIGINT)
    # the end-of-command memory check would measure this whole pytest process (it can pass
    # the 512 MB default and the 64 MB limits set below): off here; tested in test_fix2_cli.py
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
    def _run(*argv: Any) -> Result:
        code = cli.main([str(a) for a in argv])
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return _run


def people() -> list[dict[str, Any]]:
    return [
        {"id": 1, "name": "Anvar", "score": 10, "active": True, "tags": ["x"],
         "contact": {"address": {"city": "Toshkent"}}},
        {"id": 2, "name": "Bobur", "score": 25, "active": False, "contact": {"address": {"city": "Samarqand"}}},
        {"id": 3, "name": "Dilnoza", "score": 150, "active": True, "contact": {"address": {"city": "toshkent"}}},
    ]


@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "people.json"
    jsongen.write_file(p, people(), "array", style="pretty2")
    return p


def listing(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


# ----------------------------------------------------------------------------------
# blackbox F7, codereview CR-01, requirements F4: a record path that leads nowhere
# ----------------------------------------------------------------------------------
@pytest.fixture
def wrapped(tmp_path: Path) -> Path:
    p = tmp_path / "w.json"
    p.write_bytes(b'{"meta":{"n":2},"data":[{"id":1},{"id":2}],"more":[1,2,3]}')
    return p


def test_unknown_record_path_is_a_user_error_with_the_candidates(run, wrapped: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    r = run("convert", wrapped, out, "--record-path", "dat")
    assert r.code == 1, r.err
    lines = r.err.splitlines()
    assert lines[0] == "Xato: Yozuvlar yo'li topilmadi: dat"
    assert lines[1].startswith("Maslahat: Fayl boshida topilgan massivlar: ")
    assert "data (2 ta element)" in lines[1] and "more (3 ta element)" in lines[1]
    assert lines[1].endswith("Masalan: --record-path data")  # the closest candidate
    # not presented as a JSON error of the file, no --allow-partial / repair advice
    for wrong in ("JSON xatosi", "--allow-partial", "repair", "keyingi bosqich"):
        assert wrong not in r.err
    assert not out.exists()
    # --allow-partial never turns it into an empty success
    r = run("convert", wrapped, out, "--record-path", "dat", "--allow-partial")
    assert r.code == 1 and "Yozuvlar yo'li topilmadi: dat" in r.err
    assert not out.exists() and listing(tmp_path) == ["w.json"]


@pytest.mark.parametrize("argv", [
    ["inspect", "{src}"], ["inspect", "{src}", "--json"], ["inspect", "{src}", "--allow-partial"],
    ["search", "{src}", "1"], ["search", "{src}", "1", "--count"], ["search", "{src}", "1", "--export", "{tmp}/h.csv"],
    ["filter", "{src}", "{tmp}/o.json", "--where", "id > 0"], ["sort", "{src}", "{tmp}/o.json", "--by", "id"],
    ["convert", "{src}", "{tmp}/o.csv", "--allow-partial"],
])
def test_unknown_record_path_in_every_command(run, wrapped: Path, tmp_path: Path, argv: list[str]) -> None:
    args = [a.format(src=wrapped, tmp=tmp_path) for a in argv]
    r = run("--lang", "en", *args, "--record-path", "dat")
    assert r.code == 1, (argv, r.err)
    assert "Error: Record path not found: dat" in r.err
    assert "Hint: Arrays found at the start of the file: more (3 items), data (2 items)." in r.err
    assert "--allow-partial" not in r.err and r.out == ""
    assert listing(tmp_path) == ["w.json"]


def test_record_path_to_a_scalar_and_on_a_root_array(run, wrapped: Path, tmp_path: Path) -> None:
    r = run("--lang", "en", "convert", wrapped, tmp_path / "o.json", "--record-path", "meta.n")
    assert r.code == 1
    assert r.err.startswith("Error: The value at the record path is not an array or object: meta.n\n")
    assert "Arrays found at the start of the file" in r.err and "JSON error" not in r.err
    arr = tmp_path / "arr.json"
    arr.write_bytes(b'[{"a": 1}, {"a": 2}]')
    r = run("--lang", "en", "inspect", arr, "--record-path", "data")
    assert r.code == 1 and "Record path not found: data" in r.err
    assert "Hint: The file itself is an array of records: remove --record-path." in r.err
    single = tmp_path / "single.json"
    single.write_bytes(b'{"a": {"b": 1}}')
    r = run("inspect", single, "--record-path", "x.y")
    assert r.code == 1 and "Maslahat: Fayl boshida yozuvlar massivi topilmadi." in r.err


# ----------------------------------------------------------------------------------
# requirements F1 (CLI part): the source changed while it was read -> exit 2
# ----------------------------------------------------------------------------------
class _Changes:
    """Document.check_changed / ensure_unchanged driven by a flag (independent of how the
    reader detects changes)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jsonreshape.reader import Document

        self.changed = False

        def check_changed(doc: Any) -> bool:
            return self.changed

        def ensure_unchanged(doc: Any) -> None:
            if self.changed:
                raise FileChangedError(path=str(doc.source_path))

        monkeypatch.setattr(Document, "check_changed", check_changed)
        monkeypatch.setattr(Document, "ensure_unchanged", ensure_unchanged)


def _assert_changed(r: Result, path: Path) -> None:
    assert r.code == 2, r.err
    assert f"Xato: Fayl ish davomida boshqa dastur tomonidan o'zgartirildi: {path}" in r.err
    assert "Maslahat: Natija saqlanmadi." in r.err


def test_change_found_after_indexing_is_exit_2(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    changes = _Changes(monkeypatch)
    changes.changed = True
    for argv in (["inspect", src], ["search", src, "Anvar", "--count"], ["filter", src, tmp_path / "o.json", "--where",
                 "id > 0"], ["sort", src, tmp_path / "o.json", "--by", "id"], ["convert", src, tmp_path / "o.csv"]):
        r = run(*argv)
        _assert_changed(r, src)
        assert r.out == ""
    assert listing(tmp_path) == ["people.json"]


def test_change_during_inspect_schema_prints_no_report(run, src: Path, monkeypatch) -> None:
    from jsonreshape import schema

    changes = _Changes(monkeypatch)
    original = schema.infer_schema

    def infer_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        changes.changed = True
        return result

    monkeypatch.setattr(schema, "infer_schema", infer_then_change)
    r = run("inspect", src)
    _assert_changed(r, src)
    assert r.out == ""


def test_change_during_search_is_exit_2_without_summary(run, src: Path, monkeypatch) -> None:
    from jsonreshape import search as search_mod

    changes = _Changes(monkeypatch)
    original = search_mod.search

    def search_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        changes.changed = True
        return result

    monkeypatch.setattr(search_mod, "search", search_then_change)
    r = run("search", src, "Anvar")
    _assert_changed(r, src)
    assert "ta moslik" not in r.err


def test_change_during_search_export_commits_nothing(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import search as search_mod

    changes = _Changes(monkeypatch)
    original = search_mod.iter_hits

    def hits_then_change(*args, **kwargs):
        for hit in original(*args, **kwargs):
            yield hit
        changes.changed = True  # the scan finished; the export has not been committed yet

    monkeypatch.setattr(search_mod, "iter_hits", hits_then_change)
    r = run("search", src, "Toshkent", "--export", tmp_path / "hits.csv")
    _assert_changed(r, src)
    assert listing(tmp_path) == ["people.json"]


def test_change_during_sort_writes_no_output(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import recordset

    changes = _Changes(monkeypatch)
    original = recordset.sort_records

    def sort_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        changes.changed = True
        return result

    monkeypatch.setattr(recordset, "sort_records", sort_then_change)
    r = run("convert", src, tmp_path / "o.jsonl", "--sort-by", "-id")
    _assert_changed(r, src)
    assert listing(tmp_path) == ["people.json"]


def test_writer_file_changed_error_is_exit_2(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import writer

    def changed(doc, *args, **kwargs):
        raise FileChangedError(path=str(doc.source_path))

    monkeypatch.setattr(writer, "export", changed)
    r = run("--lang", "en", "convert", src, tmp_path / "o.json")
    assert r.code == 2
    assert f"Error: The file was changed by another program while it was being read: {src}" in r.err
    assert "Hint: Nothing was saved." in r.err


def test_garbled_data_of_a_changed_file_is_reported_as_the_change(run, src: Path, tmp_path: Path,
                                                                  monkeypatch) -> None:
    """The decode error caused by reading spans of a rewritten file is not the real cause."""
    from jsonreshape import writer

    changes = _Changes(monkeypatch)
    flip = {"on": True}

    def garbled(*args, **kwargs):
        changes.changed = flip["on"]
        raise JsonDecodeError("Extra data", 7)

    monkeypatch.setattr(writer, "export", garbled)
    r = run("convert", src, tmp_path / "o.csv")
    _assert_changed(r, src)
    assert "JSON matnini o'qib bo'lmadi" not in r.err
    flip["on"] = changes.changed = False  # an unchanged file keeps its real error
    r = run("convert", src, tmp_path / "o.csv")
    assert r.code == 1 and "JSON matnini o'qib bo'lmadi: Extra data" in r.err


def test_real_rewrite_during_sort_is_detected(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    """End to end with the real change detection: the source is rewritten in place (same
    size, other bytes) while the command sorts it."""
    from jsonreshape import recordset

    original = recordset.sort_records

    def sort_while_rewritten(*args, **kwargs):
        data = src.read_bytes()
        src.write_bytes(data.replace(b"Anvar", b"Zafar"))
        st = src.stat()
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        return original(*args, **kwargs)

    monkeypatch.setattr(recordset, "sort_records", sort_while_rewritten)
    r = run("sort", src, tmp_path / "o.json", "--by", "name")
    _assert_changed(r, src)
    assert not (tmp_path / "o.json").exists()


# ----------------------------------------------------------------------------------
# blackbox F2 (hint): several JSON values in one file
# ----------------------------------------------------------------------------------
def test_json_lines_read_as_one_value_suggest_format_jsonl(run, tmp_path: Path) -> None:
    p = tmp_path / "lines.json"
    p.write_bytes(b'{"a": 1}\n{"a": 2}\n{"a": 3}\n')
    out = tmp_path / "o.json"
    r = run("--lang", "en", "convert", p, out, "--format", "single")
    assert r.code == 2
    assert "Hint: The file seems to hold one JSON value per line (JSON Lines): run the command again with " \
           "--format jsonl." in r.err
    assert "--allow-partial" not in r.err and not out.exists()
    r = run("--lang", "en", "convert", p, out, "--format", "single", "--allow-partial")
    assert r.code == 0 and "only the 1 record before it will be used" in r.err and "--format jsonl" in r.err
    r = run("--lang", "en", "convert", p, out, "--format", "jsonl")
    assert r.code == 0 and "3 / 3 records" in r.err


def test_concatenated_values_suggest_format_stream(run, tmp_path: Path) -> None:
    pretty = tmp_path / "pretty.json"
    pretty.write_bytes(b'{\n  "a": 1\n}\n{\n  "a": 2\n}\n')
    same_line = tmp_path / "same.json"
    same_line.write_bytes(b'{"a": 1} {"a": 2}')
    for p in (pretty, same_line):
        r = run("--lang", "en", "inspect", p, "--format", "single")
        assert r.code == 2
        assert "Hint: The file seems to hold several JSON values one after another: run the command again with " \
               "--format stream." in r.err, r.err
        assert "--allow-partial" not in r.err
    # a broken document is still a JSON error with the --allow-partial advice
    broken = tmp_path / "broken.json"
    broken.write_bytes(b'[{"a": 1}, {"a": 2}]]')
    r = run("--lang", "en", "inspect", broken)
    assert r.code == 2 and "--allow-partial" in r.err and "--format" not in r.err


def test_record_path_on_json_lines_suggests_removing_it(run, tmp_path: Path) -> None:
    p = tmp_path / "rp.jsonl"
    p.write_bytes(b'{"data":[1,2]}\n{"data":[3]}\n')
    # detected as JSON Lines: refused before any work (exit 1, test_fix2_cli.py)
    r = run("--lang", "en", "convert", p, tmp_path / "o.json", "--record-path", "data")
    assert r.code == 1
    assert "only works inside a single JSON document" in r.err and "Remove --record-path" in r.err
    assert "--allow-partial" not in r.err
    # several values found only while reading (here: forced as one document): a JSON error
    r = run("--lang", "en", "convert", p, tmp_path / "o.json", "--record-path", "data", "--format", "wrapped")
    assert r.code == 2
    assert "--record-path only works inside a single JSON document: remove it" in r.err
    assert "--allow-partial" not in r.err


def test_big_first_line_with_a_small_detection_head(run, tmp_path: Path) -> None:
    """The realistic case: JSON Lines named .json whose first record does not fit into the
    head used for format detection (here head_bytes = 64)."""
    from jsonreshape import settings

    settings.set_settings(settings.Settings(head_bytes=64))
    p = tmp_path / "export.json"
    first = json.dumps({"id": 0, "text": "x" * 200})
    p.write_bytes((first + "\n" + "\n".join(json.dumps({"id": i}) for i in range(1, 6)) + "\n").encode())
    r = run("--lang", "en", "convert", p, tmp_path / "o.jsonl")
    if r.code == 0:  # the reader detected JSON Lines by itself
        assert "6 / 6 records" in r.err
        return
    assert r.code == 2
    assert "--format jsonl" in r.err and "--allow-partial" not in r.err


# ----------------------------------------------------------------------------------
# codereview CR-04, requirements F6 (CLI part): --memory-limit
# ----------------------------------------------------------------------------------
@pytest.fixture
def sort_memory(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from jsonreshape import recordset

    seen: list[Any] = []
    original = recordset.sort_records

    def spy(*args, **kwargs):
        seen.append(kwargs.get("memory_limit"))
        return original(*args, **kwargs)

    monkeypatch.setattr(recordset, "sort_records", spy)
    return seen


def test_memory_limit_without_unit_is_megabytes(run, src: Path, tmp_path: Path, sort_memory: list[Any]) -> None:
    for text, expected in (("64", 64 * MiB), ("20.5", int(20.5 * MiB)), ("100MB", 100 * MiB), ("128m", 128 * MiB)):
        r = run("sort", src, tmp_path / "o.json", "--by", "id", "--memory-limit", text)
        assert r.code == 0, r.err
        assert sort_memory[-1] == expected, text
    assert cli._memory_size_value("512") == 512 * MiB
    assert cli._memory_size_value("2GB") == 2 << 30
    with pytest.raises(Exception):
        cli._memory_size_value("0")


def test_memory_limit_below_the_minimum_is_raised_with_a_warning(run, src: Path, tmp_path: Path,
                                                                 sort_memory: list[Any]) -> None:
    r = run("sort", src, tmp_path / "o.json", "--by", "id", "--memory-limit", "1MB")
    assert r.code == 0, r.err
    assert sort_memory[-1] == cli.MIN_MEMORY_LIMIT == 16 * MiB
    assert "Ogohlantirish: --memory-limit 1 MB juda kichik: 16 MB ishlatiladi." in r.err
    r = run("--lang", "en", "sort", src, tmp_path / "o.json", "--by", "id", "--memory-limit", "65536B")
    assert sort_memory[-1] == 16 * MiB and "--memory-limit 64 KB is too small: 16 MB is used instead." in r.err


def test_sort_memory_follows_the_memory_limit_setting(run, src: Path, tmp_path: Path,
                                                      sort_memory: list[Any]) -> None:
    from jsonreshape import settings

    settings.set_settings(settings.Settings(memory_limit_bytes=64 * MiB, sort_memory_bytes=256 * MiB))
    r = run("sort", src, tmp_path / "o.json", "--by", "id")
    assert r.code == 0 and sort_memory[-1] == 32 * MiB and "Ogohlantirish" not in r.err
    r = run("--lang", "en", "convert", src, tmp_path / "o.json", "--sort-by", "id", "--memory-limit", "100")
    assert r.code == 0 and sort_memory[-1] == 32 * MiB
    assert ("--memory-limit 100 MB: the \"Memory limit\" setting (64 MB) allows at most 32 MB for sorting; "
            "32 MB is used.") in r.err
    settings.set_settings(settings.Settings(memory_limit_bytes=4 << 30, sort_memory_bytes=256 * MiB))
    r = run("sort", src, tmp_path / "o.json", "--by", "id")
    assert sort_memory[-1] == 256 * MiB
    r = run("sort", src, tmp_path / "o.json", "--by", "id", "--memory-limit", "1GB")
    assert sort_memory[-1] == 1 << 30 and "Ogohlantirish" not in r.err


# ----------------------------------------------------------------------------------
# codereview CR-05, requirements F5: --help examples work in cmd.exe and PowerShell
# ----------------------------------------------------------------------------------
_EPILOG_KEYS = ("cli.help.epilog", "cli.cmd.inspect.epilog", "cli.cmd.search.epilog", "cli.cmd.filter.epilog",
                "cli.cmd.sort.epilog", "cli.cmd.convert.epilog", "cli.cmd.presets.epilog")
# outside double quotes: cmd.exe / PowerShell operators, quoting, variables, array and
# sub-expression syntax; inside double quotes: PowerShell's escape character and variables
_UNQUOTED_BAD = frozenset("'`$&|<>^%;(){}[]@#,")


def _examples() -> list[str]:
    out = []
    for key in _EPILOG_KEYS:
        for lang in ("uz", "en"):
            for line in MESSAGES[key][lang].splitlines():
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


def test_help_examples_are_safe_in_windows_shells() -> None:
    examples = _examples()
    assert len(examples) >= 30
    for line in examples:
        assert not _shell_problems(line), (line, _shell_problems(line))


def test_help_examples_parse_and_their_filters_compile() -> None:
    from jsonreshape.query import compile_filter

    for line in _examples():
        argv = cli._join_dash_values(_win_split(line)[1:])
        with cli.localized_argparse():
            args = cli._parse(cli.build_parser(), argv)
        if getattr(args, "where", None) is not None:
            compile_filter(args.where, lang="expr")
        if getattr(args, "jmespath", None) is not None:
            compile_filter(args.jmespath, lang="jmespath")
    # the expressions the texts recommend
    compile_filter("contact.address.city == 'Toshkent' and len(children) > 2")
    compile_filter("contact.address.city == 'Toshkent' && length(children) > `2`", lang="jmespath")
    compile_filter("score > `100`", lang="jmespath")


def test_help_mentions_the_quoting_rule(run) -> None:
    r = run("filter", "--help")
    assert "Tirnoqlar:" in r.out and "--where \"contact.address.city == 'Toshkent'\"" in r.out
    assert "--jmespath 'score > `100`'" in r.out and "PowerShell" in r.out
    r = run("--lang", "en", "--help")
    assert "--where \"contact.address.city == 'Toshkent'\"" in r.out and "double quotes" in r.out
    assert "'contact.address.city == \"Toshkent\"'" not in r.out


def _windows_shells() -> list[str]:
    if sys.platform != "win32":
        return []
    return [name for name in ("powershell.exe", "pwsh.exe", "cmd.exe") if shutil.which(name)]


@pytest.mark.slow
@pytest.mark.parametrize("shell", _windows_shells() or ["none"])
def test_help_examples_pass_the_same_arguments_in_real_shells(tmp_path: Path, shell: str) -> None:
    if shell == "none":
        pytest.skip("no Windows shell here")
    examples = _examples()
    dump = tmp_path / "argv_dump.py"
    dump.write_bytes(b"import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    if shell == "cmd.exe":
        script = tmp_path / "examples.cmd"
        body = ["@echo off"] + [f'"{sys.executable}" "{dump}"' + line[len("jsonreshape"):] for line in examples]
        script.write_bytes(("\r\n".join(body) + "\r\n").encode("utf-8"))
        cmd = [shell, "/d", "/c", str(script)]
    else:
        script = tmp_path / "examples.ps1"
        body = [f"& '{sys.executable}' '{dump}'" + line[len("jsonreshape"):] for line in examples]
        script.write_bytes(("\ufeff" + "\r\n".join(body) + "\r\n").encode("utf-8"))
        cmd = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    proc = subprocess.run(cmd, capture_output=True, timeout=300, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    got = [json.loads(line) for line in proc.stdout.decode("utf-8", "replace").splitlines() if line.startswith("[")]
    assert got == [_win_split(line)[1:] for line in examples]


# ----------------------------------------------------------------------------------
# requirements F11: search prints hits while the index is still being built
# ----------------------------------------------------------------------------------
def test_search_does_not_build_the_index_first(run, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import search as search_mod

    p = tmp_path / "many.json"
    jsongen.write_file(p, [{"id": i, "name": f"Toshkent {i}"} for i in range(300)], "array")
    states: list[bool] = []
    original = search_mod.search

    def spy(doc, *args, **kwargs):
        states.append(bool(doc.index_complete))
        return original(doc, *args, **kwargs)

    monkeypatch.setattr(search_mod, "search", spy)
    r = run("search", p, "Toshkent", "--limit", "3")
    assert r.code == 0, r.err
    assert states == [False]  # search() itself starts and follows the index
    assert r.out.splitlines()[0].startswith("#0  name  [qiymat]")
    assert "300 ta moslik, 300 ta yozuvda" in r.err
    # --count (and a complete cached index) index first, as before
    r = run("search", p, "Toshkent", "--count", "--no-cache")
    assert r.out == "300 ta moslik, 300 ta yozuvda\n"


def test_search_reports_index_warnings_and_errors_after_the_hits(run, tmp_path: Path) -> None:
    lines = tmp_path / "broken.jsonl"
    lines.write_bytes(b'{bad\n{"a": "Toshkent"}\n')
    r = run("search", lines, "Toshkent")
    assert r.code == 0
    assert r.out.splitlines() == ["#0  a  [qiymat]  " + cli.MARK_OPEN + "Toshkent" + cli.MARK_CLOSE]
    assert "Ogohlantirish: 1 ta buzuq qator o'tkazib yuborildi (1-qator)." in r.err
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"a": "Toshkent"}, {"a": "x"} {"a": "Toshkent"}]')
    r = run("search", bad, "Toshkent")
    assert r.code == 2 and r.out.startswith("#0  a  [qiymat]")
    assert "Xato: JSON xatosi" in r.err and "--allow-partial" in r.err and "ta moslik" not in r.err
    r = run("search", bad, "Toshkent", "--allow-partial", "--no-cache")
    assert r.code == 0 and "Ogohlantirish: Faylda JSON xatosi bor" in r.err and "1 ta moslik" in r.err


# ----------------------------------------------------------------------------------
# blackbox F17, F16 and codereview CR-14 (b): search notes and English singulars
# ----------------------------------------------------------------------------------
@pytest.fixture
def sixty(tmp_path: Path) -> Path:
    p = tmp_path / "sixty.jsonl"
    p.write_bytes(("\n".join(json.dumps({"v": "hit"}) for _ in range(60)) + "\n").encode())
    return p


def test_more_note_has_no_export_advice_when_exporting(run, sixty: Path, tmp_path: Path) -> None:
    r = run("--lang", "en", "search", sixty, "hit", "--export", tmp_path / "hits.csv", "--limit", "3")
    assert r.code == 0
    more = [line for line in r.err.splitlines() if line.startswith("Only the first")]
    assert more == ["Only the first 3 hits are shown (of 60); use --limit N to show more."]
    assert "60 hits written to" in r.err
    r = run("--lang", "en", "search", sixty, "hit", "--limit", "5")
    assert "Only the first 5 hits are shown (of 60); use --limit N or --export FILE for more." in r.err
    r = run("--lang", "en", "search", sixty, "hit", "--limit", "10", "--max-hits", "2")
    assert "Only the first 2 hits are shown (of 60); use --max-hits N or --export FILE for more." in r.err
    r = run("search", sixty, "hit", "--limit", "1")
    assert "Faqat birinchi moslik ko'rsatildi (jami 60 ta); ko'proq uchun --limit N yoki --export FAYL." in r.err


def test_english_messages_use_singular_forms(run, tmp_path: Path) -> None:
    one = tmp_path / "one.jsonl"
    one.write_bytes(b'{bad\n{"a": "x"}\n')
    r = run("--lang", "en", "search", one, "x", "--count")
    assert r.out == "1 hit in 1 record\n"
    assert "Warning: 1 broken line was skipped (line 1)." in r.err and "1 hit in 1 record;" in r.err
    r = run("--lang", "en", "search", one, "x", "--export", tmp_path / "h.json")
    assert "1 hit written to" in r.err
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"a": 1} {"a": 2}]')
    r = run("--lang", "en", "convert", bad, tmp_path / "o.json")
    assert r.code == 2 and "Hint: Add --allow-partial to work with the 1 record before the error." in r.err
    r = run("--lang", "en", "convert", bad, tmp_path / "o.json", "--allow-partial")
    assert "only the 1 record before it will be used." in r.err
    r = run("--lang", "en", "inspect", bad)
    assert "1 record was read before the error." in r.out
    two = tmp_path / "two.jsonl"
    two.write_bytes(b'{bad\n{"a": "x"}\n{bad\n{"a": "x"}\n')
    r = run("--lang", "en", "search", two, "x")
    assert "2 broken lines were skipped (lines 1, 3)." in r.err and "2 hits in 2 records;" in r.err
    assert cli._tn("cli.summary.rows", 1, rows="1") == "1 row" and cli._tn("cli.summary.rows", 3, rows="3") == "3 rows"


def test_no_record_before_the_error_gets_no_useless_allow_partial_hint(run, tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_bytes(b'[}')
    r = run("--lang", "en", "convert", p, tmp_path / "o.json")
    assert r.code == 2 and "--allow-partial" not in r.err
    assert "Hint: Not a single record could be read before the error" in r.err


# ----------------------------------------------------------------------------------
# codereview CR-14 (a, c, d, f, g): wording
# ----------------------------------------------------------------------------------
_MY_SECTIONS = ("cli.", "error.", "json.", "common.")


def test_no_development_plan_promises_and_uzbek_terms() -> None:
    for key, entry in MESSAGES.items():
        if not key.startswith(_MY_SECTIONS):
            continue
        for lang in ("uz", "en"):
            text = entry[lang].lower()
            for promise in ("later stage", "keyingi bosqich", "repair buyrug", "coming in"):
                assert promise not in text, (key, lang)
        uz = re.sub(r"--[a-z-]+", "", entry["uz"])  # option names stay as code
        assert not re.search(r"(?i)\b(indent|escape|unicode escape|record path)\b", uz), (key, entry["uz"])
    assert "{offset}-bayt" in MESSAGES["error.json_syntax"]["uz"]
    assert "indent" not in MESSAGES["settings.field.export_indent"]["uz"].lower()


def test_filter_help_lists_every_function() -> None:
    from jsonreshape.query import FUNCTIONS

    for lang in ("uz", "en"):
        text = MESSAGES["cli.cmd.filter.epilog"][lang]
        listed = re.split(r"funksiyalar:|functions:", text)[1].replace("\n", " ").strip(" .").split()
        assert sorted(listed) == sorted(FUNCTIONS), lang


def _make_read_only(path: Path) -> None:
    path.write_bytes(b"old")
    os.chmod(path, stat.S_IREAD)


def test_read_only_output_gets_its_own_message(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "o.json"
    _make_read_only(out)
    try:
        r = run("convert", src, out)
        assert r.code == 1
        assert "boshqa dasturda" not in r.err and "Excel" not in r.err  # not "open in another program"
        if has("fileio.error.read_only"):
            assert r.err.startswith("Xato: " + t("fileio.error.read_only", path=str(out)))
        assert out.read_bytes() == b"old" and listing(tmp_path) == ["o.json", "people.json"]
        r = run("search", src, "Anvar", "--export", tmp_path / "o.json")  # search --export as well
        assert r.code == 1 and "Excel" not in r.err
    finally:
        os.chmod(out, stat.S_IREAD | stat.S_IWRITE)


# ----------------------------------------------------------------------------------
# codereview CR-09: broken JSON Lines lines in line-oriented words
# ----------------------------------------------------------------------------------
def test_broken_line_reasons_speak_of_the_line(run, tmp_path: Path) -> None:
    p = tmp_path / "b.jsonl"
    p.write_bytes(b'{"a":1}\n{"a":2\n{"a":3} {"a":4}\n{"a":5}\n')
    r = run("inspect", p)
    assert r.code == 0
    assert re.search(r"2-qator, \d+-ustun: qator kutilmaganda tugadi; ',' yoki '\}' kutilgan: \{\"a\":2\n", r.out), r.out
    assert re.search(r"3-qator, \d+-ustun: qiymatdan keyin ortiqcha ma'lumot; qator oxiri kutilgan \(bir qatorda bitta "
                     r"qiymat bo'ladi\)", r.out), r.out
    assert "fayl kutilmaganda tugadi" not in r.out and "fayl oxiri kutilgan" not in r.out
    data = json.loads(run("--lang", "en", "inspect", p, "--json").out)
    messages = [b["message"] for b in data["broken_lines"]["first"]]
    assert "the line ended unexpectedly" in messages[0]
    assert "end of the line was expected (one value per line)" in messages[1]
    # a whole document keeps the file-oriented wording
    doc = tmp_path / "cut.json"
    doc.write_bytes(b'[{"a": 1}, {"a": 2')
    r = run("inspect", doc)
    assert "fayl kutilmaganda tugadi" in r.out


# ----------------------------------------------------------------------------------
# requirements F17: JMESPath hints
# ----------------------------------------------------------------------------------
def test_jmespath_syntax_error_gets_a_jmespath_example(run, src: Path, tmp_path: Path) -> None:
    from jsonreshape.query import compile_filter

    r = run("filter", src, tmp_path / "o.json", "--jmespath", "contact.address.city ==")
    assert r.code == 1
    hint = [line for line in r.err.splitlines() if line.startswith("Maslahat: ")]
    assert hint == ["Maslahat: " + t("cli.hint.jmespath")]
    assert "JMESPath misoli: contact.address.city == 'Toshkent' && length(children) > `2`" in hint[0]
    example = hint[0].split("misoli: ", 1)[1].split(" (", 1)[0]
    compile_filter(example, lang="jmespath")  # the advice itself is valid JMESPath
    r = run("filter", src, tmp_path / "o.json", "--where", "id <")
    assert "Maslahat: Misol: contact.address.city == 'Toshkent' and len(children) > 2" in r.err
    compile_filter("contact.address.city == 'Toshkent' and len(children) > 2")


def test_jmespath_quoted_name_compared_warns(run, src: Path, tmp_path: Path) -> None:
    r = run("--lang", "en", "filter", src, tmp_path / "o.json", "--jmespath", 'contact.address.city == "Toshkent"')
    assert r.code == 0
    assert "Warning: In JMESPath \"Toshkent\" is read as a field name; for a text use single quotes: 'Toshkent'." \
        in r.err
    r = run("--lang", "en", "filter", src, tmp_path / "o.json", "--jmespath", "contact.address.city == 'Toshkent'")
    assert r.code == 0 and "Warning" not in r.err and "1 / 3 records" in r.err
    r = run("--lang", "en", "filter", src, tmp_path / "o.json", "--jmespath", "\"name\" == 'Anvar'")
    assert r.code == 0 and "Warning" not in r.err and "1 / 3 records" in r.err  # a quoted field on the left


# ----------------------------------------------------------------------------------
# requirements F22: inspect shows the outer wrapper
# ----------------------------------------------------------------------------------
def test_inspect_shows_the_wrapper(run, tmp_path: Path) -> None:
    p = tmp_path / "w.json"
    p.write_bytes(b'{"meta": {"n": 2, "source": "api"}, "data": [{"id": 1}, {"id": 2}], "next_page": "abc"}')
    r = run("inspect", p)
    assert r.code == 0, r.err
    assert re.search(r"Tashqi o'rama:\s+meta: \{\"n\":2,\"source\":\"api\"\}\n\s+data: \[yozuvlar: 2 ta\]\n"
                     r"\s+next_page: \"abc\"\n", r.out), r.out
    data = json.loads(run("inspect", p, "--json").out)
    assert data["wrapper"] == {
        "bytes_before": len(b'{"meta": {"n": 2, "source": "api"}, "data": '),
        "bytes_after": len(b', "next_page": "abc"}'),
        "members": [
            {"key": "meta", "records": False, "json": '{"n":2,"source":"api"}', "truncated": False},
            {"key": "data", "records": True, "json": None, "truncated": False},
            {"key": "next_page", "records": False, "json": '"abc"', "truncated": False},
        ],
    }
    nested = tmp_path / "nested.json"
    nested.write_bytes(b'{"result": {"total": 2, "items": [{"id": 1}, {"id": 2}]}, "ok": true}')
    r = run("--lang", "en", "inspect", nested, "--record-path", "result.items")
    assert re.search(r"Wrapper:\s+result: \{\"total\":2,\"items\":\[" + "\u2026" + r"\]\}\n\s+ok: true\n", r.out), r.out
    flat = tmp_path / "flat.json"
    jsongen.write_file(flat, people(), "array")
    assert json.loads(run("inspect", flat, "--json").out)["wrapper"] is None
    assert "Tashqi o'rama" not in run("inspect", flat).out


def test_inspect_wrapper_size_limit(run, tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "w.json"
    p.write_bytes(b'{"meta": {"note": "' + b"x" * 300 + b'"}, "data": [{"id": 1}, {"id": 2}]}')
    monkeypatch.setattr(cli, "WRAPPER_READ_MAX", 100)
    r = run("--lang", "en", "inspect", p)
    assert re.search(r"Wrapper:\s+\d+ B before the records, 1 B after them \(not shown\)\n", r.out), r.out
    assert json.loads(run("inspect", p, "--json").out)["wrapper"]["members"] is None
    monkeypatch.setattr(cli, "WRAPPER_READ_MAX", 1 << 20)
    monkeypatch.setattr(cli, "WRAPPER_TEXT_CHARS", 40)
    r = run("inspect", p)
    assert re.search(r"meta: \{\"note\":\"x{30,}" + "\u2026" + r"\n", r.out), r.out


# ----------------------------------------------------------------------------------
# codereview CR-11 (CLI part): candidate arrays of arrays
# ----------------------------------------------------------------------------------
def test_candidate_arrays_of_arrays_are_labelled_arrays(run, tmp_path: Path) -> None:
    assert cli._items_kind_label("arrays") == "massivlar"
    i18n.set_language("en")
    assert cli._items_kind_label("arrays") == "arrays"
    i18n.set_language("uz")
    from jsonreshape.reader import RecordPathCandidate

    probe = RecordPathCandidate(path=("data",), closed_in_head=True, items_in_head=2, objects_in_head=0)
    if not hasattr(probe, "arrays_in_head"):
        pytest.skip("the reader does not count arrays among the candidate items yet")
    p = tmp_path / "matrix.json"
    p.write_bytes(b'{"data": [[1, 2], [3, 4]], "name": "m"}')
    r = run("inspect", p)
    assert re.search(r"Nomzod massivlar:\s+data: 2 ta element \(massivlar\)", r.out), r.out


# ----------------------------------------------------------------------------------
# blackbox F14 (CLI part): OS errors in plain words
# ----------------------------------------------------------------------------------
def _os(winerror: int | None, err_no: int, path: str = "X:\\data\\out.json") -> OSError:
    if winerror is not None and sys.platform == "win32":
        return OSError(None, "raw OS text", path, winerror)
    return OSError(err_no, "raw OS text", path)


@pytest.mark.parametrize("winerror, err_no, key", [
    (2, errno.ENOENT, "cli.error.os.not_found"),
    (3, errno.ENOENT, "cli.error.os.not_found"),
    (5, errno.EACCES, "cli.error.os.access_denied"),
    (32, errno.EACCES, "fileio.error.in_use"),
    (33, errno.EACCES, "fileio.error.in_use"),
    (112, errno.ENOSPC, "fileio.error.disk_full"),
    (183, errno.EEXIST, "cli.error.os.exists"),
])
def test_os_errors_are_mapped_to_localized_messages(winerror: int, err_no: int, key: str) -> None:
    if winerror in (32, 33) and sys.platform != "win32":
        pytest.skip("sharing violations are Windows errors")
    err = cli._os_error(_os(winerror, err_no))
    assert err.key == key
    assert err.params["path"] == "X:\\data\\out.json"
    assert err.os_text == "raw OS text"  # only a detail line
    assert has(key) or key.startswith("fileio."), key


def test_os_error_on_a_read_only_file_and_without_a_path(tmp_path: Path) -> None:
    ro = tmp_path / "ro.json"
    _make_read_only(ro)
    try:
        err = cli._os_error(PermissionError(errno.EACCES, "Access is denied", str(ro)))
        assert err.key == "fileio.error.read_only"
    finally:
        os.chmod(ro, stat.S_IREAD | stat.S_IWRITE)
    err = cli._os_error(OSError(errno.EIO, "I/O error"))
    assert err.key == "cli.error.os" and err.params["detail"] == "I/O error"
    assert cli._display_path("\\\\?\\C:\\very\\long") == "C:\\very\\long"
    assert cli._display_path("\\\\?\\UNC\\server\\share\\x") == "\\\\server\\share\\x"


def test_os_error_message_through_the_command_line(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import writer

    def missing_drive(*args, **kwargs):
        raise _os(3, errno.ENOENT, "Z:\\")

    monkeypatch.setattr(writer, "export", missing_drive)
    r = run("convert", src, tmp_path / "o.json")
    assert r.code == 1
    lines = r.err.splitlines()
    assert lines[0] == "Xato: Fayl yoki papka topilmadi: Z:\\"
    assert lines[1] == "  tizim xabari: raw OS text"
    assert lines[2].startswith("Maslahat: Yo'lni tekshiring")
    r = run("--lang", "en", "convert", src, tmp_path / "o.json")
    assert r.err.splitlines()[:2] == ["Error: File or folder not found: Z:\\", "  system message: raw OS text"]

    def exists(*args, **kwargs):
        raise _os(183, errno.EEXIST, str(tmp_path / "home"))

    monkeypatch.setattr(writer, "export", exists)
    r = run("--lang", "en", "convert", src, tmp_path / "o.json")
    assert r.code == 1 and r.err.startswith(f"Error: A file or folder with this name already exists: {tmp_path / 'home'}")
    assert "JSONRESHAPE_HOME" in r.err


# ----------------------------------------------------------------------------------
# requirements F20 (CLI part): filter presets
# ----------------------------------------------------------------------------------
@pytest.fixture
def presets_available() -> None:
    from jsonreshape import query

    if not hasattr(query, "FilterPresets"):
        pytest.skip("query.FilterPresets is not available")


def test_presets_save_use_list_and_delete(run, src: Path, tmp_path: Path, presets_available) -> None:
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    r = run("filter", src, out1, "--where", "score > 20", "--save-preset", "Katta ball")
    assert r.code == 0, r.err
    assert "Filtr saqlandi: Katta ball" in r.err
    r = run("convert", src, out2, "--preset", "katta ball")
    assert r.code == 0, r.err
    assert codec.loads(out1.read_bytes()) == codec.loads(out2.read_bytes())
    assert [x["id"] for x in codec.loads(out2.read_bytes())] == [2, 3]
    r = run("filter", src, tmp_path / "c.json", "--jmespath", "active", "--save-preset", "faol")
    assert r.code == 0
    r = run("presets")
    assert r.code == 0
    assert r.out.splitlines() == ["faol        [jmespath]  active", "Katta ball  [expr]  score > 20"]
    listed = json.loads(run("presets", "--json").out)
    assert [(p["name"], p["lang"], p["text"]) for p in listed] == [("faol", "jmespath", "active"),
                                                                   ("Katta ball", "expr", "score > 20")]
    r = run("presets", "--delete", "FAOL")
    assert r.code == 0 and "Filtr o'chirildi: FAOL" in r.err
    r = run("--lang", "en", "presets", "--delete", "faol")
    assert r.code == 1 and "There is no saved filter named faol" in r.err and "jsonreshape presets" in r.err
    r = run("--lang", "en", "filter", src, tmp_path / "d.json", "--preset", "nope")
    assert r.code == 1 and not (tmp_path / "d.json").exists()
    assert "There is no saved filter named nope" in r.err and "Hint: List the saved filters with: jsonreshape " \
                                                                  "presets" in r.err


def test_presets_errors(run, src: Path, tmp_path: Path, presets_available) -> None:
    from jsonreshape.query import FilterPresets

    r = run("filter", src, tmp_path / "o.json", "--where", "id <", "--save-preset", "yomon")
    assert r.code == 1 and FilterPresets().list() == []  # an invalid filter is never saved
    r = run("convert", src, tmp_path / "o.json", "--save-preset", "x")
    assert r.code == 1 and "--save-preset uchun --where, --jmespath yoki --preset kerak." in r.err
    r = run("filter", src, tmp_path / "o.json", "--where", "id > 0", "--preset", "x")
    assert r.code == 1 and "bilan birga ishlatib bo'lmaydi" in r.err
    r = run("presets")
    assert r.code == 0 and r.out == "" and "Saqlangan filtrlar yo'q." in r.err
    r = run("filter", src, tmp_path / "o.json", "--jmespath", "contact.address.city ==", "--save-preset", "j")
    assert r.code == 1 and FilterPresets().list() == []


def test_presets_without_the_store(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import query

    monkeypatch.delattr(query, "FilterPresets", raising=False)
    r = run("presets")
    assert r.code == 1 and "Filtrlarni saqlash bu versiyada mavjud emas." in r.err
    r = run("filter", src, tmp_path / "o.json", "--preset", "x")
    assert r.code == 1


# ----------------------------------------------------------------------------------
# requirements F16 (CLI part): --sort-keys [alpha|codepoint]
# ----------------------------------------------------------------------------------
def _parse(*argv: str) -> Any:
    with cli.localized_argparse():
        return cli._parse(cli.build_parser(), cli._join_dash_values(list(argv)))


def test_sort_keys_option_values() -> None:
    assert _parse("convert", "a.json", "b.json", "--sort-keys").sort_keys == "alpha"
    assert _parse("convert", "a.json", "b.json", "--sort-keys", "codepoint").sort_keys == "codepoint"
    assert _parse("convert", "a.json", "b.json", "--sort-keys=codepoint").sort_keys == "codepoint"
    assert _parse("convert", "a.json", "b.json", "--sort-keys", "alpha", "--compact").sort_keys == "alpha"
    assert _parse("convert", "a.json", "b.json").sort_keys is None
    args = _parse("convert", "--sort-keys", "a.json", "b.json")  # the flag never swallows INPUT
    assert (args.sort_keys, args.input, args.output) == ("alpha", "a.json", "b.json")
    assert cli._join_dash_values(["convert", "a", "b", "--sort-keys", "--compact"]) == \
        ["convert", "a", "b", "--sort-keys", "--compact"]
    assert cli._join_dash_values(["convert", "--sort-keys", "a.json", "b.json"]) == \
        ["convert", "--sort-keys=alpha", "a.json", "b.json"]
    assert _parse("convert", "a.json", "b.json", "--sort-keys", "--compact").sort_keys == "alpha"


def test_sort_keys_reaches_the_writer(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import writer

    seen: list[Any] = []
    original = writer.export

    def spy(doc, out, opts, *args, **kwargs):
        seen.append(opts.sort_keys)
        return original(doc, out, opts, *args, **kwargs)

    monkeypatch.setattr(writer, "export", spy)
    for flag, accepted in ((["--sort-keys"], ("alpha", True)), (["--sort-keys", "codepoint"], ("codepoint", True)),
                           ([], (False,))):
        r = run("convert", src, tmp_path / "o.json", *flag)
        assert r.code == 0, r.err
        assert seen[-1] in accepted, (flag, seen[-1])


def test_sort_keys_alpha_is_alphabetical(run, tmp_path: Path) -> None:
    from jsonreshape.errors import UserInputError
    from jsonreshape.writer import ExportOptions

    try:
        ExportOptions("json", sort_keys="alpha")
    except (UserInputError, TypeError, ValueError):
        pytest.skip("the writer does not support key orders yet")
    keys = ["Banan", "Zebo", "apple", "item10", "item2", "olma"]
    p = tmp_path / "k.json"
    p.write_bytes(json.dumps([{k: i for i, k in enumerate(keys)}]).encode())
    r = run("convert", p, tmp_path / "alpha.json", "--sort-keys")
    assert r.code == 0, r.err
    assert list(json.loads((tmp_path / "alpha.json").read_bytes())[0]) == ["apple", "Banan", "item2", "item10",
                                                                           "olma", "Zebo"]
    r = run("convert", p, tmp_path / "cp.json", "--sort-keys", "codepoint")
    assert r.code == 0, r.err
    assert list(json.loads((tmp_path / "cp.json").read_bytes())[0]) == sorted(keys)


# ----------------------------------------------------------------------------------
# key coverage of the new texts
# ----------------------------------------------------------------------------------
def test_new_cli_keys_exist_with_matching_placeholders() -> None:
    import string

    def fields(text: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}

    keys = [k for k in MESSAGES if k.startswith(("cli.", "json.line_"))]
    for key in keys:
        assert fields(MESSAGES[key]["uz"]) == fields(MESSAGES[key]["en"]), key
        if key.endswith(".one"):
            base = MESSAGES[key[:-4]]
            assert fields(base["en"]) >= fields(MESSAGES[key]["en"]), key
    for key in ("cli.error.os.not_found", "cli.error.os.access_denied", "cli.error.os.exists"):
        assert has(key) and has(key + ".hint"), key
