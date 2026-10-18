"""Regression tests of the second review round in the command line (phase 5d, workstream "cli").

One section per finding (lens/id in the comments):

* blackbox F7: ``--record-path`` on an input *detected* as JSON Lines or as several JSON
  values is refused up front (exit 1), like ``--format jsonl --record-path``;
* blackbox F11: a record-path syntax error gets record-path examples as its hint (no
  ``#120``: record numbers are not paths); the other path options lose ``#120`` as well;
* requirements F6: a warning at the end of every command whose peak memory exceeded
  ``Settings.memory_limit_bytes`` (both numbers; the command is not aborted);
* N05 (CLI part): warnings the document gains during the command (e.g. "bytes appended
  while the file was read") are printed once, at the end;
* N11 (CLI part): a ``UserInputError`` of the filter runner (no record could be evaluated)
  exits 1 with its message and hint;
* N13 (CLI part): the progress line is labelled with the phase the operation announces
  (``Progress.info().message``: merging the sorted parts, finding the columns);
* blackbox F16, codereview CR-14: English texts with a count read correctly for 1.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import cli, i18n
from jsonreshape.errors import UserInputError
from jsonreshape.i18n import MESSAGES, has, t
from jsonreshape.tasks import ProgressInfo

MiB = 1 << 20
ROOT = Path(__file__).resolve().parents[1]
LQ, RQ = cli.MARK_OPEN, cli.MARK_CLOSE   # the Uzbek quotation marks « »
REAL_PEAK_MEMORY = cli._peak_memory      # before the fixtures replace it


# ----------------------------------------------------------------------------------
# fixtures and helpers (as in test_cli.py / test_fix_cli.py)
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
    # the end-of-command memory check measures this whole pytest process: off here, except
    # in the tests of that check (they replace cli._peak_memory themselves)
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
        {"id": 3, "name": "Dilnoza", "score": 150, "tags": ["a", "b"], "contact": {"address": {"city": "toshkent"}}},
        {"id": 1, "name": "Anvar", "score": 10, "tags": ["x"], "contact": {"address": {"city": "Toshkent"}}},
        {"id": 2, "name": "Bobur", "score": 25, "tags": [], "contact": {"address": {"city": "Samarqand"}}},
    ]


@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "people.json"
    jsongen.write_file(p, people(), "array", style="pretty2")
    return p


def listing(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


LINES = b'{"data":[1,2]}\n{"data":[3]}\n'


# ----------------------------------------------------------------------------------
# blackbox F7: --record-path on an input detected as JSON Lines / several values
# ----------------------------------------------------------------------------------
REFUSED = "only works inside a single JSON document, but this file was detected as "
REFUSED_HINT = ("Hint: Remove --record-path: each value is then a record. If the file is in fact a single JSON "
                "document, add --format wrapped.")


@pytest.mark.parametrize("name, data, label", [
    ("rp.jsonl", LINES, "JSON Lines"),                                # JSON Lines by its extension
    ("lines.json", LINES, "JSON Lines"),                              # JSON Lines by its content
    ("stream.json", b'{"data":[1,2]} {"data":[3]}', "Concatenated JSON values"),   # values one after another
    ("pretty.json", b'{\n  "data": [1]\n}\n{\n  "data": [2]\n}\n', "Concatenated JSON values"),
])
def test_record_path_on_detected_several_values_is_refused_up_front(run, tmp_path: Path, name: str, data: bytes,
                                                                     label: str) -> None:
    p = tmp_path / name
    p.write_bytes(data)
    out = tmp_path / "o.jsonl"
    for extra in ([], ["--allow-partial"]):
        r = run("--lang", "en", "convert", p, out, "--record-path", "data", *extra)
        assert r.code == 1, (extra, r.err)
        assert r.err.splitlines() == [f"Error: A record path (data) {REFUSED}{label} (several JSON values).",
                                      REFUSED_HINT]
        assert r.out == "" and listing(tmp_path) == [name]   # nothing written, also with --allow-partial


@pytest.mark.parametrize("argv", [
    ["inspect"], ["inspect", "--json"], ["search", "1"], ["search", "1", "--count"],
    ["search", "1", "--export", "{tmp}/h.csv"], ["filter", "{tmp}/o.json", "--where", "id > 0"],
    ["sort", "{tmp}/o.json", "--by", "id"], ["convert", "{tmp}/o.csv"],
])
def test_record_path_refusal_in_every_command(run, tmp_path: Path, argv: list[str]) -> None:
    p = tmp_path / "in.jsonl"
    p.write_bytes(LINES)
    args = [a.format(tmp=tmp_path) for a in argv]
    r = run("--lang", "en", args[0], p, *args[1:], "--record-path", "data")
    assert r.code == 1, (argv, r.err)
    assert REFUSED in r.err and "JSON error" not in r.err and r.out == ""
    assert listing(tmp_path) == ["in.jsonl"]


def test_record_path_refusal_in_uzbek_and_for_gzip_and_the_root(run, tmp_path: Path) -> None:
    p = tmp_path / "in.jsonl"
    p.write_bytes(LINES)
    r = run("inspect", p, "--record-path", "data")
    assert r.code == 1
    assert r.err.splitlines() == [
        "Xato: Yozuvlar yo'li (data) faqat bitta JSON hujjat ichida ishlaydi, bu fayl esa JSON Lines deb aniqlandi "
        "(bir nechta JSON qiymat).",
        "Maslahat: --record-path ni olib tashlang: har bir qiymat alohida yozuv bo'ladi. Fayl aslida bitta JSON "
        "hujjat bo'lsa, --format wrapped qo'shing.",
    ]
    gz = tmp_path / "in.jsonl.gz"
    gz.write_bytes(gzip.compress(LINES))
    r = run("--lang", "en", "convert", gz, tmp_path / "o.jsonl", "--record-path", "data")
    assert r.code == 1 and f"A record path (data) {REFUSED}JSON Lines" in r.err
    r = run("--lang", "en", "convert", p, tmp_path / "o.jsonl", "--record-path", "$")
    assert r.code == 1 and f"A record path ($) {REFUSED}JSON Lines" in r.err
    assert listing(tmp_path) == ["in.jsonl", "in.jsonl.gz"]


def test_record_path_still_works_inside_one_document(run, tmp_path: Path, monkeypatch) -> None:
    w = tmp_path / "w.json"
    w.write_bytes(b'{"meta":{"n":2},"data":[{"id":1},{"id":2}]}')
    out = tmp_path / "o.jsonl"
    r = run("--lang", "en", "convert", w, out, "--record-path", "data")
    assert r.code == 0 and r.err.startswith("2 / 2 records -> ")
    assert out.read_bytes().split() == [b'{"id":1}', b'{"id":2}']
    # one JSON document with a misleading name: refused, and --format wrapped is the way out
    misnamed = tmp_path / "single.jsonl"
    misnamed.write_bytes(b'{"meta": 1,\n "data": [{"id": 1}, {"id": 2}]}\n')
    r = run("--lang", "en", "convert", misnamed, out, "--record-path", "data")
    assert r.code == 1 and "add --format wrapped" in r.err
    r = run("--lang", "en", "convert", misnamed, out, "--record-path", "data", "--format", "wrapped")
    assert r.code == 0 and r.err.startswith("2 / 2 records -> ")
    # a forced --format jsonl is refused by the reader, as before
    lines = tmp_path / "in.jsonl"
    lines.write_bytes(LINES)
    r = run("--lang", "en", "convert", lines, out, "--record-path", "data", "--format", "jsonl")
    assert r.code == 1 and "The record path data cannot be combined with the jsonl format." in r.err
    # without --record-path the extra detection never runs
    from jsonreshape import reader

    def no_detection(*args, **kwargs):
        raise AssertionError("detect_format must not run without --record-path")

    monkeypatch.setattr(reader, "detect_format", no_detection)
    r = run("--lang", "en", "convert", lines, out)
    assert r.code == 0 and r.err.startswith("2 / 2 records -> ")
    r = run("--lang", "en", "convert", w, out, "--record-path", "data", "--format", "wrapped")
    assert r.code == 0


# ----------------------------------------------------------------------------------
# blackbox F11: record-path syntax errors get record-path examples (no "#120")
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("rp", ["data[", "$x", "data..", "/data/~2"])
def test_record_path_syntax_error_gets_record_path_examples(run, tmp_path: Path, rp: str) -> None:
    w = tmp_path / "w.json"
    w.write_bytes(b'{"data":[{"id":1},{"id":2}]}')
    r = run("--lang", "en", "convert", w, tmp_path / "o.json", "--record-path", rp)
    assert r.code == 1, r.err
    lines = r.err.splitlines()
    assert lines[0].startswith("Error: Invalid path at character ")
    assert lines[1] == "  " + rp and lines[2].endswith("^")   # the caret under the error
    assert lines[-1] == "Hint: Record path examples: data, result.items, /data/0, data[0] ($ = the root)."
    assert "#" not in r.err
    r = run("inspect", w, "--record-path", rp)
    assert r.code == 1
    assert r.err.splitlines()[-1] == "Maslahat: Yozuvlar yo'li misollari: data, result.items, /data/0, data[0] " \
                                     "($ - ildiz)."
    assert listing(tmp_path) == ["w.json"]


@pytest.mark.parametrize("argv", [
    ["search", "{src}", "1", "--path", "a["],
    ["sort", "{src}", "{tmp}/o.json", "--by", "a["],
    ["convert", "{src}", "{tmp}/o.json", "--sort-by", "contact[x"],
])
def test_other_path_syntax_errors_do_not_advertise_record_numbers(run, src: Path, tmp_path: Path,
                                                                  argv: list[str]) -> None:
    r = run("--lang", "en", *[a.format(src=src, tmp=tmp_path) for a in argv])
    assert r.code == 1, r.err
    assert r.err.splitlines()[-1] == "Hint: Examples: contact.address.city, data[120].contact.address, " \
                                     "$.data[120], /data/120"
    assert "#120" not in r.err


# ----------------------------------------------------------------------------------
# requirements F6: the peak memory of the process above Settings.memory_limit_bytes
# ----------------------------------------------------------------------------------
@pytest.fixture
def peaks(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """cli._peak_memory returns these values in turn (the last one repeats): the first is
    read when main() starts, the next one when the command has ended."""
    values: list[Any] = []

    def fake() -> Any:
        if len(values) > 1:
            return values.pop(0)
        return values[0] if values else None

    monkeypatch.setattr(cli, "_peak_memory", fake)
    return values


def _limit(n: int) -> None:
    from jsonreshape import settings

    settings.set_settings(settings.Settings(memory_limit_bytes=n))


MEM_EN = ('Warning: The program used up to 700 MB of memory, more than the "Memory limit" setting (512 MB). To use '
          'less, lower the "Large node threshold (size)" setting (50 MB), or raise "Memory limit".')
MEM_SORT_EN = ('Warning: The program used up to 700 MB of memory, more than the "Memory limit" setting (512 MB). To '
               'use less, lower --memory-limit or the "Large node threshold (size)" setting (50 MB), or raise '
               '"Memory limit".')


def test_memory_above_the_limit_gives_one_warning_at_the_end(run, src: Path, tmp_path: Path, peaks) -> None:
    _limit(512 * MiB)
    peaks[:] = [100 * MiB, 700 * MiB]
    r = run("--lang", "en", "convert", src, tmp_path / "o.jsonl")
    assert r.code == 0   # never aborted
    lines = r.err.splitlines()
    assert lines[0].startswith("3 / 3 records -> ") and lines[-1] == MEM_EN and r.err.count("Memory limit") == 2
    assert len(lines) == 2
    # a sort is told about --memory-limit; -q keeps the warning
    for argv in (["sort", src, tmp_path / "s.jsonl", "--by", "id"],
                 ["convert", src, tmp_path / "s.csv", "--sort-by", "-id"]):
        peaks[:] = [100 * MiB, 700 * MiB]
        r = run("--lang", "en", *argv, "-q")
        assert r.code == 0 and r.err.splitlines() == [MEM_SORT_EN]
    # Uzbek
    peaks[:] = [100 * MiB, 700 * MiB]
    r = run("search", src, "Toshkent", "--count")
    assert r.code == 0
    assert r.err.splitlines()[-1] == (
        f"Ogohlantirish: Dastur 700 MB gacha xotira ishlatdi: bu {LQ}Xotira chegarasi{RQ} sozlamasidan (512 MB) "
        f"ko'p. Kamroq xotira uchun {LQ}Katta tugun chegarasi (hajm){RQ} sozlamasini (50 MB) kamaytiring yoki "
        f"{LQ}Xotira chegarasi{RQ} qiymatini oshiring.")
    # every command, also one that failed (after its error)
    for argv in (["inspect", src], ["presets"], ["inspect", tmp_path / "missing.json"]):
        peaks[:] = [100 * MiB, 700 * MiB]
        r = run("--lang", "en", *argv)
        assert r.err.splitlines()[-1] == MEM_EN, argv


def test_no_memory_warning_below_the_limit_or_for_an_earlier_peak(run, src: Path, tmp_path: Path, peaks) -> None:
    _limit(512 * MiB)
    for values in ([100 * MiB, 400 * MiB],     # below the limit
                   [900 * MiB, 900 * MiB],     # an earlier peak of this (in-process) caller
                   [None]):                    # not measurable
        peaks[:] = values
        r = run("--lang", "en", "convert", src, tmp_path / "o.jsonl")
        assert r.code == 0 and "Warning" not in r.err, values
    # no command: --version, --help, a usage error
    for argv in (["--version"], ["--help"], ["inspect"], ["sort", src, tmp_path / "o.json"]):
        peaks[:] = [100 * MiB, 700 * MiB]
        r = run("--lang", "en", *argv)
        assert "The program used up to" not in r.err + r.out, argv


def test_peak_memory_is_measured() -> None:
    peak = REAL_PEAK_MEMORY()
    assert isinstance(peak, int) and peak > MiB


def test_memory_warning_in_a_real_process(tmp_path: Path) -> None:
    """End to end with the real measurement: every Python process needs more than 8 MB."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_bytes(json.dumps({"version": 1, "memory_limit_bytes": "8MB"}).encode())
    data = tmp_path / "d.json"
    data.write_bytes(json.dumps([{"id": i, "name": f"n{i}"} for i in range(50)]).encode())
    env = dict(os.environ, JSONRESHAPE_HOME=str(home), PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-m", "jsonreshape", "--lang", "en", "sort", str(data), str(tmp_path / "o.jsonl"),
           "--by", "name", "-q"]
    p = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, timeout=300)
    err = p.stderr.decode("utf-8", "replace")
    assert p.returncode == 0, err
    lines = err.splitlines()
    assert len(lines) == 1, err
    m = re.fullmatch(r'Warning: The program used up to ([\d.]+) MB of memory, more than the "Memory limit" setting '
                     r'\(8 MB\)\. To use less, lower --memory-limit or the "Large node threshold \(size\)" setting '
                     r'\(50 MB\), or raise "Memory limit"\.', lines[0])
    assert m is not None, lines[0]
    assert float(m.group(1)) > 8
    (home / "settings.json").write_bytes(json.dumps({"version": 1}).encode())   # the default limit: 512 MB
    p = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, timeout=300)
    assert p.returncode == 0 and p.stderr == b""


# ----------------------------------------------------------------------------------
# N05 (CLI part): warnings the document gains during the command, once at the end
# ----------------------------------------------------------------------------------
APPENDED_EN = "{count} bytes were appended while the file was read; they are not included."


@pytest.fixture
def appended(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Document.ensure_unchanged adds a warning to doc.info, again at every check with a
    growing count -- as the reader may do for an input that grows while it is read."""
    from jsonreshape.reader import Document

    monkeypatch.setitem(MESSAGES, "test.appended", {
        "uz": "Fayl o'qilayotganda unga {count} bayt qo'shildi; ular ishlatilmadi.", "en": APPENDED_EN})
    original = Document.ensure_unchanged
    calls: list[int] = []

    def ensure_unchanged(doc: Any) -> None:
        original(doc)
        calls.append(1)
        doc.info.add_warning("test.appended", count=10 * len(calls))

    monkeypatch.setattr(Document, "ensure_unchanged", ensure_unchanged)
    return calls


@pytest.mark.parametrize("argv", [
    ["convert", "{src}", "{tmp}/o.jsonl"], ["convert", "{src}", "{tmp}/o.csv"],
    ["sort", "{src}", "{tmp}/o.json", "--by", "name"], ["filter", "{src}", "{tmp}/o.json", "--where", "id > 1"],
    ["search", "{src}", "Toshkent"], ["search", "{src}", "Toshkent", "--count"],
    ["search", "{src}", "Toshkent", "--export", "{tmp}/h.csv"], ["search", "{src}", "Toshkent", "--no-cache"],
])
def test_warnings_gained_during_the_command_are_printed_once_at_the_end(run, src: Path, tmp_path: Path,
                                                                         appended: list[int], argv: list[str]) -> None:
    r = run("--lang", "en", *[a.format(src=src, tmp=tmp_path) for a in argv])
    assert r.code == 0, r.err
    assert len(appended) >= 2   # added more than once ...
    lines = r.err.splitlines()
    shown = [line for line in lines if "were appended" in line]
    assert shown == ["Warning: " + APPENDED_EN.format(count=10 * len(appended))]   # ... shown once, latest count
    assert lines[-1] == shown[0]   # after the command's own output


def test_late_warning_already_in_the_inspect_report_is_not_repeated(run, src: Path, appended: list[int]) -> None:
    r = run("--lang", "en", "inspect", src)
    assert r.code == 0
    assert r.out.count("were appended") == 1 and "were appended" not in r.err
    r = run("--lang", "en", "inspect", src, "--json")
    assert r.code == 0 and len(json.loads(r.out)["warnings"]) == 1 and "were appended" not in r.err


def test_late_warnings_are_identified_by_key_and_path(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    """Warnings known at the start are not repeated; distinct paths are distinct warnings."""
    from jsonreshape.reader import Document

    monkeypatch.setitem(MESSAGES, "test.folder", {"uz": "Papka: {path} ({n})", "en": "Folder: {path} ({n})"})
    original_build = Document.build_index
    original_check = Document.ensure_unchanged

    def build_index(doc: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_build(doc, *args, **kwargs)
        doc.info.add_warning("test.folder", path="A", n=1)   # before the start warnings
        return result

    def ensure_unchanged(doc: Any) -> None:
        original_check(doc)
        doc.info.add_warning("test.folder", path="A", n=2)   # the same warning again
        doc.info.add_warning("test.folder", path="B", n=3)   # another one

    monkeypatch.setattr(Document, "build_index", build_index)
    monkeypatch.setattr(Document, "ensure_unchanged", ensure_unchanged)
    r = run("--lang", "en", "convert", src, tmp_path / "o.jsonl", "--no-cache")
    assert r.code == 0, r.err
    lines = r.err.splitlines()
    assert lines[0] == "Warning: Folder: A (1)"
    assert lines[1].startswith("3 / 3 records -> ")
    assert lines[2:] == ["Warning: Folder: B (3)"]


def test_appended_data_note_is_shown_only_at_the_end_with_its_latest_size(run, src: Path, tmp_path: Path,
                                                                          monkeypatch) -> None:
    """The reader's note about data appended to the input (cli.END_WARNINGS) is never shown
    at the start or in the inspect report -- its size keeps changing -- but once at the end."""
    from jsonreshape.reader import Document

    key = cli.END_WARNINGS[0]
    original_build = Document.build_index
    original_check = Document.ensure_unchanged
    sizes = iter(f"{n} B" for n in range(10, 10000, 10))
    notes: dict[int, dict[str, Any]] = {}

    def note(doc: Any) -> None:   # like reader._note_appended: added once, its size updated in place
        params = notes.get(id(doc))
        if params is None:
            doc.info.add_warning(key, bytes=next(sizes))
            notes[id(doc)] = doc.info.warning_params[-1]
        else:
            params["bytes"] = next(sizes)

    def build_index(doc: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_build(doc, *args, **kwargs)
        note(doc)                  # growth seen while indexing: before the start warnings
        return result

    def ensure_unchanged(doc: Any) -> None:
        original_check(doc)
        note(doc)

    monkeypatch.setattr(Document, "build_index", build_index)
    monkeypatch.setattr(Document, "ensure_unchanged", ensure_unchanged)
    for argv in (["convert", src, tmp_path / "o.jsonl"], ["inspect", src], ["inspect", src, "--json"]):
        notes.clear()
        r = run("--lang", "en", *argv, "--no-cache")
        assert r.code == 0, r.err
        lines = r.err.splitlines()
        grew = [line for line in lines if "The file grew by" in line]
        assert len(grew) == 1 and lines[-1] == grew[0], argv
        latest = notes[next(iter(notes))]["bytes"]
        assert grew[0] == "Warning: " + t(key, bytes=latest), argv
        assert "grew by" not in r.out, argv   # not in the inspect report


def test_input_growing_during_the_command_is_used_as_its_snapshot(run, tmp_path: Path, monkeypatch) -> None:
    """End to end with the reader: a JSON Lines log another program appends to while the
    command runs -- the records of the version opened, and the note once at the end."""
    from jsonreshape import recordset

    log_file = tmp_path / "app.log.jsonl"
    log_file.write_bytes(b"".join(json.dumps({"ts": i, "level": "INFO"}).encode() + b"\n" for i in range(5)))
    original = recordset.sort_records

    def sort_while_appended(*args: Any, **kwargs: Any) -> Any:
        with open(log_file, "ab") as f:
            f.write(b'{"ts": 99, "level": "ERROR"}\n')
        return original(*args, **kwargs)

    monkeypatch.setattr(recordset, "sort_records", sort_while_appended)
    out = tmp_path / "sorted.jsonl"
    r = run("--lang", "en", "sort", log_file, out, "--by", "-ts")
    assert r.code == 0, r.err
    assert [json.loads(line)["ts"] for line in out.read_bytes().splitlines()] == [4, 3, 2, 1, 0]
    lines = r.err.splitlines()
    assert lines[0].startswith("5 / 5 records -> ")
    assert len(lines) == 2 and lines[1].startswith("Warning: The file grew by 29 B after it was opened"), r.err


def test_late_warnings_are_printed_when_the_command_fails(run, src: Path, tmp_path: Path, appended,
                                                          monkeypatch) -> None:
    from jsonreshape import writer

    def failing_export(doc: Any, *args: Any, **kwargs: Any) -> Any:
        doc.ensure_unchanged()
        raise UserInputError("cli.error.preset_not_found", name="x")

    monkeypatch.setattr(writer, "export", failing_export)
    r = run("--lang", "en", "convert", src, tmp_path / "o.jsonl")
    assert r.code == 1
    lines = r.err.splitlines()
    assert sum("were appended" in line for line in lines) == 1
    assert lines[-2:] == ["Error: There is no saved filter named x", "Hint: List the saved filters with: "
                                                                     "jsonreshape presets"]


# ----------------------------------------------------------------------------------
# N11 (CLI part): the filter runner's UserInputError (no record could be evaluated)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    ["filter", "{src}", "{tmp}/o.jsonl", "--jmespath", "abs(tags) > `1`"],
    ["convert", "{src}", "{tmp}/o.csv", "--where", "len(name) > 1", "--sort-by", "id"],
])
def test_filter_failing_on_every_record_exits_1_with_message_and_hint(run, src: Path, tmp_path: Path, monkeypatch,
                                                                      argv: list[str]) -> None:
    from jsonreshape import recordset

    monkeypatch.setitem(MESSAGES, "test.all_failed", {
        "uz": "Filtrni birorta ham yozuvda hisoblab bo'lmadi: {detail}",
        "en": "The filter could not be evaluated on any record: {detail}"})
    monkeypatch.setitem(MESSAGES, "test.all_failed.hint", {
        "uz": "Ifodadagi maydon nomlari va turlarini tekshiring.", "en": "Check the field names and types."})

    def all_failed(*args: Any, **kwargs: Any) -> Any:
        raise UserInputError("test.all_failed", detail="abs() got an array")

    monkeypatch.setattr(recordset, "filter_records", all_failed)
    r = run("--lang", "en", *[a.format(src=src, tmp=tmp_path) for a in argv])
    assert r.code == 1
    assert r.err.splitlines() == ["Error: The filter could not be evaluated on any record: abs() got an array",
                                  "Hint: Check the field names and types."]
    assert r.out == "" and listing(tmp_path) == ["people.json"]


# ----------------------------------------------------------------------------------
# N13 (CLI part): the progress line shows the phase the operation announces
# ----------------------------------------------------------------------------------
class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _frames(text: str) -> list[str]:
    return [f.strip() for f in text.replace("\n", "\r").split("\r") if f.strip()]


def test_progress_label_is_the_operations_message(monkeypatch) -> None:
    fake = _TTY()
    monkeypatch.setattr(sys, "stderr", fake)
    i18n.set_language("en")
    line = cli._ProgressLine(enabled=True)
    prog = line.stage("Sorting")
    assert prog is not None and prog.message == "Sorting"   # the CLI's label is the initial message
    prog.set_total(1000)
    prog.set_message("Sorting")
    assert _frames(fake.getvalue())[-1].startswith("Sorting: 0.0%")
    prog.update(1000)
    prog.set_message("Merging sorted parts of x.json")     # a phase of its own
    assert _frames(fake.getvalue())[-1].startswith("Merging sorted parts of x.json: 100.0%")
    prog.set_message("Finding the columns" + cli.ELLIPSIS)   # a trailing ellipsis is dropped
    assert _frames(fake.getvalue())[-1].startswith("Finding the columns: ")
    prog.set_message("Finishing...")
    assert _frames(fake.getvalue())[-1].startswith("Finishing: ")
    prog.set_message("")                                     # no message: the CLI's label
    assert _frames(fake.getvalue())[-1].startswith("Sorting: ")
    prog.finish()
    assert fake.getvalue().endswith("\r")


def test_long_progress_label_is_shortened_so_the_numbers_stay(monkeypatch) -> None:
    i18n.set_language("en")
    info = ProgressInfo(done=500, total=1000, fraction=0.5, rate=100.0, eta=5.0, elapsed=5.0,
                        message="Merging sorted parts of " + "x" * 80 + ".json", unit="bytes")
    line = cli._ProgressLine(enabled=True)
    full = line.format(info)
    assert full.startswith("Merging sorted parts of xxx") and full.endswith(" left")
    short = line.format(info, 70)
    assert len(short) <= 70 and short.endswith(" left") and (cli.ELLIPSIS + ": 50.0%") in short
    assert cli._stage_label("Ustunlar aniqlanmoqda" + cli.ELLIPSIS) == "Ustunlar aniqlanmoqda"
    assert cli._stage_label("Qidirilmoqda: a.json") == "Qidirilmoqda: a.json"
    assert cli._stage_label("a\nb") == "a\\nb" and cli._stage_label("") == ""


def test_sort_merge_and_column_scan_get_their_own_labels(src: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    fake = _TTY()
    monkeypatch.setattr(sys, "stderr", fake)
    monkeypatch.setattr(cli, "_terminal_columns", lambda: 200)   # no label shortened
    rc = cli.main(["--lang", "en", "convert", str(src), str(tmp_path / "o.csv"), "--sort-by", "name", "--no-cache"])
    assert rc == 0
    labels = [f.split(": ")[0] for f in _frames(fake.getvalue()) if "/s" in f]   # progress lines (a rate)
    assert "Indexing" in labels and "Sorting" in labels
    merging = t("recordset.progress.sort_merge", name=src.name)
    assert merging in labels                                  # not the "Sorting" label any more
    assert labels.index(merging) > labels.index("Sorting")
    assert cli._stage_label(t("writer.progress.columns")) in labels
    capsys.readouterr()


# ----------------------------------------------------------------------------------
# blackbox F16, codereview CR-14: English texts with a count read correctly for 1
# ----------------------------------------------------------------------------------
def test_summary_and_sizes_use_the_singular_for_one(run, tmp_path: Path) -> None:
    one = tmp_path / "one.json"
    one.write_bytes(b'[{"a": "x"}]')
    r = run("--lang", "en", "convert", one, tmp_path / "o.jsonl")
    assert r.code == 0 and r.err.startswith("1 / 1 record -> ")
    r = run("--lang", "en", "filter", one, tmp_path / "f.jsonl", "--where", "a == 'z'")
    assert r.code == 0 and r.err.startswith("0 / 1 record -> ")
    two = tmp_path / "two.json"
    two.write_bytes(b'[{"a": "x"}, {"a": "y"}]')
    r = run("--lang", "en", "filter", two, tmp_path / "f.jsonl", "--where", "a == 'x'")
    assert r.code == 0 and r.err.startswith("1 / 2 records -> ")
    r = run("convert", one, tmp_path / "o.jsonl")
    assert r.code == 0 and r.err.startswith("1 / 1 yozuv -> ")
    byte = tmp_path / "byte.json"
    byte.write_bytes(b"1")
    r = run("--lang", "en", "inspect", byte)
    assert r.code == 0 and re.search(r"^Size: +1 B \(1 byte\)$", r.out, re.M), r.out
    r = run("--lang", "en", "inspect", two)
    assert re.search(r"^Size: +\d+ B \(\d+ bytes\)$", r.out, re.M), r.out
    r = run("inspect", byte)
    assert re.search(r"^Hajm: +1 B \(1 bayt\)$", r.out, re.M), r.out


def test_other_count_helpers_use_the_singular(lang_en) -> None:
    assert cli._tn("cli.summary.files", 1, count="1") == "1 file"
    assert cli._tn("cli.summary.files", 3, count="3") == "3 files"
    assert cli._argparse_ngettext("expected %s argument", "expected %s arguments", 1) % 1 == "expected 1 argument"
    assert cli._argparse_ngettext("expected %s argument", "expected %s arguments", 3) % 3 == "expected 3 arguments"
    i18n.set_language("uz")
    assert cli._argparse_ngettext("expected %s argument", "expected %s arguments", 1) % 1 == "1 ta qiymat kutilgan"


_COUNT_RE = re.compile(r"(\{[a-z_]+\}|%s)\+?\s+(?:[a-z]+\s+)?([a-z]{2,}s)\b")
_NOT_NOUNS = {"is", "was", "has", "this", "as", "its", "takes", "less"}


def test_every_count_text_of_the_cli_sections_has_a_singular() -> None:
    """cli / common / errors / json texts: '<count> <plural noun>' needs '<key>.one'."""
    missing = []
    for key, entry in MESSAGES.items():
        if not key.startswith(("cli.", "common.", "error.", "json.")) or key.endswith(".one"):
            continue
        nouns = [m.group(0) for m in _COUNT_RE.finditer(entry["en"]) if m.group(2) not in _NOT_NOUNS]
        if nouns and not has(key + ".one"):
            missing.append(f"{key}: {nouns}")
    assert not missing, "\n".join(missing)


# ----------------------------------------------------------------------------------
# key coverage of the new texts
# ----------------------------------------------------------------------------------
def test_new_cli_keys_exist_with_matching_placeholders() -> None:
    keys = ["cli.error.record_path_values", "cli.error.record_path_values.hint", "cli.hint.record_path_syntax",
            "cli.hint.path_syntax", "cli.warn.memory_peak", "cli.warn.memory_peak_sort", "cli.summary.export.one",
            "cli.summary.files.one", "cli.inspect.size_value.one", "cli.argparse.expected_n.one"]
    fields = re.compile(r"\{([a-z_]+)\}|(%s)")
    for key in keys:
        entry = MESSAGES[key]
        assert entry["uz"].strip() and entry["en"].strip(), key
        assert sorted(fields.findall(entry["uz"])) == sorted(fields.findall(entry["en"])), key
        if key.endswith(".one"):
            base = MESSAGES[key[:-4]]
            assert sorted(fields.findall(base["en"])) == sorted(fields.findall(entry["en"])), key
    assert "#120" not in MESSAGES["cli.hint.record_path_syntax"]["en"] + MESSAGES["cli.hint.path_syntax"]["en"]


def test_end_warnings_name_the_readers_appended_note() -> None:
    """cli.END_WARNINGS repeats reader.APPENDED_WARNING (cli does not import the reader at
    start-up: --backend must be set first)."""
    from jsonreshape import reader

    assert reader.APPENDED_WARNING in cli.END_WARNINGS
    assert all(has(key) for key in cli.END_WARNINGS)
