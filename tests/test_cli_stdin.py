"""Standard input as INPUT (``-``) on the command line (cli docstring "Standard input"; :mod:`jsonreshape.stdin_input`).

In-process runs replace ``sys.stdin`` with a ``TextIOWrapper`` over the bytes (``cli.main`` reads its binary
``buffer``); real pipes go through ``python -m jsonreshape`` subprocesses. Covered: ``convert -`` byte-identical with
the file input (subprocess and in-process), ``inspect -`` JSON, gzip bytes on stdin, ``--format jsonl``, a terminal
as stdin refused, ``-`` as an output refused (positional OUTPUT, ``-o``, ``--export``), the spool file removed after
success, after a JSON error and after a cancellation, ``merge a.json -``, ``diff a.json -``, two ``-`` refused, the
non-INPUT files (``--schema``, PATCH, MAPPING) refused, ``patch --in-place -``, ``split -`` without ``-o``,
``import -`` without ``--from`` (and with it), the free-space rule, ``-q`` silence, the help text, and a 50 MB
pipe that keeps the child's peak RSS under 150 MB.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import cli, codec, i18n, stdin_input
from jsonreshape.errors import JsonReshapeError
from jsonreshape.tasks import CancelToken

ROOT = Path(__file__).resolve().parents[1]
MiB = 1 << 20
SPOOL_RE = re.compile(r"^stdin-\d+-.+\.json$")


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _cli_isolation(monkeypatch: pytest.MonkeyPatch):
    """main() changes process state on purpose (language, logging, signal handlers): put it back."""
    old_backend = os.environ.get("JSONRESHAPE_BACKEND")
    monkeypatch.setenv("JSONRESHAPE_LANG", "uz")
    monkeypatch.delenv("NO_COLOR", raising=False)
    old_lang = i18n.get_language()
    i18n.set_language("uz")
    root = logging.getLogger()
    old_level = root.level
    old_sigint = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(cli, "_peak_memory", lambda: None)   # the memory check would measure pytest itself
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


class _Tty:
    """A ``sys.stdin`` that is a terminal: reading it would be a bug."""

    def isatty(self) -> bool:
        return True

    @property
    def buffer(self) -> Any:
        raise AssertionError("standard input must not be read when it is a terminal")


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch):
    """``feed(data)``: the bytes the command finds on standard input (a pipe, not a terminal)."""
    def _feed(data: bytes) -> None:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))

    return _feed


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _Tty())


@pytest.fixture
def spooled(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The spool files the commands of the test created (each existed right after spooling, with mode 0600 on POSIX)."""
    paths: list[Path] = []
    original = stdin_input.spool_stdin

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        spool = original(*args, **kwargs)
        assert spool.path.is_file() and spool.path.stat().st_size == spool.size
        if os.name != "nt":  # pragma: no cover - POSIX only
            assert stat.S_IMODE(spool.path.stat().st_mode) == 0o600
        paths.append(Path(spool.path))
        return spool

    monkeypatch.setattr(stdin_input, "spool_stdin", wrapper)
    return paths


def tmp_folder() -> Path:
    return Path(os.environ["JSONRESHAPE_HOME"]) / "tmp"


def spool_files() -> list[str]:
    folder = tmp_folder()
    return sorted(name for name in os.listdir(folder) if SPOOL_RE.match(name)) if folder.is_dir() else []


def people() -> list[dict[str, Any]]:
    return [{"id": i, "name": f"n{i}", "score": codec.loads("1.10") if i % 2 else codec.loads("1e400"),
             "city": "Toshkent" if i % 3 == 0 else "Buxoro"} for i in range(8)]


def array_bytes(recs: list[Any] | None = None) -> bytes:
    return jsongen.build_document(recs if recs is not None else people(), "array", style="pretty2")


def jsonl_bytes(recs: list[Any] | None = None) -> bytes:
    return jsongen.build_document(recs if recs is not None else people(), "jsonl")


def read_jsonl(path: Path) -> list[Any]:
    return [codec.loads(line) for line in path.read_bytes().split(b"\n") if line.strip()]


# ----------------------------------------------------------------------------------
# the data flows: identical with a file input
# ----------------------------------------------------------------------------------
def test_convert_from_a_real_pipe_matches_the_file_input(tmp_path: Path) -> None:
    data = jsongen.build_document(jsongen.records(60, seed=11), "array", style="pretty2")
    src = tmp_path / "in.json"
    src.write_bytes(data)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    base = [sys.executable, "-m", "jsonreshape", "--lang", "en", "convert"]
    p = subprocess.run([*base, str(src), str(tmp_path / "file.jsonl")], cwd=str(ROOT), env=env, capture_output=True,
                       timeout=300)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    p = subprocess.run([*base, "-", str(tmp_path / "pipe.jsonl")], cwd=str(ROOT), env=env, input=data,
                       capture_output=True, timeout=300)
    err = p.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
    assert p.returncode == 0, err
    assert (tmp_path / "pipe.jsonl").read_bytes() == (tmp_path / "file.jsonl").read_bytes()
    assert re.search(r"^[\d.]+ KB read from standard input \(temporary file .*stdin-\d+-.*\.json, removed when the "
                     r"command ends\)$", err, flags=re.M), err
    assert "60 / 60 records" in err
    assert spool_files() == []


def test_convert_from_stdin_in_process(run, feed, spooled, tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(array_bytes())
    assert run("--lang", "en", "convert", src, tmp_path / "file.jsonl").code == 0
    feed(array_bytes())
    r = run("--lang", "en", "convert", "-", tmp_path / "pipe.jsonl")
    assert r.code == 0, r.err
    assert (tmp_path / "pipe.jsonl").read_bytes() == (tmp_path / "file.jsonl").read_bytes()
    assert len(spooled) == 1 and spooled[0].parent == tmp_folder() and SPOOL_RE.match(spooled[0].name)
    assert spooled[0].name.startswith(f"stdin-{os.getpid()}-")
    assert not spooled[0].exists() and spool_files() == []
    # the exact number tokens survive the detour through the spool file as they do from a file
    recs = read_jsonl(tmp_path / "pipe.jsonl")
    assert len(recs) == 8 and codec.dumps(recs[0]["score"]) == b"1e400" and codec.dumps(recs[1]["score"]) == b"1.10"


def test_inspect_stdin_json(run, feed, spooled, tmp_path: Path) -> None:
    feed(array_bytes())
    r = run("--lang", "en", "inspect", "-", "--json")
    assert r.code == 0, r.err
    data = json.loads(r.out)
    assert data["records"] == 8 and data["format"]["kind"] == "array" and data["compression"] is None
    assert Path(data["file"]) == spooled[0] and data["size"] == len(array_bytes())
    assert not spooled[0].exists()
    assert "read from standard input" in r.err


def test_gzip_bytes_on_stdin_are_detected(run, feed, tmp_path: Path) -> None:
    feed(array_bytes())
    assert run("--lang", "en", "convert", "-", tmp_path / "plain.jsonl").code == 0
    feed(gzip.compress(array_bytes()))
    r = run("--lang", "en", "convert", "-", tmp_path / "gz.jsonl")
    assert r.code == 0, r.err
    assert (tmp_path / "gz.jsonl").read_bytes() == (tmp_path / "plain.jsonl").read_bytes()
    feed(gzip.compress(array_bytes()))
    r = run("--lang", "en", "inspect", "-", "--json")
    assert r.code == 0 and json.loads(r.out)["compression"] == "gzip"
    assert spool_files() == []


def test_format_jsonl_on_stdin(run, feed, tmp_path: Path) -> None:
    feed(jsonl_bytes())
    r = run("--lang", "en", "convert", "-", tmp_path / "out.json", "--format", "jsonl", "--compact")
    assert r.code == 0, r.err
    assert codec.loads((tmp_path / "out.json").read_bytes()) == people()
    feed(jsonl_bytes())
    r = run("--lang", "en", "inspect", "-", "--json")   # detected without --format as well
    assert r.code == 0 and json.loads(r.out)["format"]["kind"] == "jsonl"
    feed(jsonl_bytes())
    r = run("--lang", "en", "convert", "-", tmp_path / "o2.json", "--format", "array")   # a forced format that does not match
    assert r.code == 2 and spool_files() == []


def test_record_path_and_filter_on_stdin(run, feed, tmp_path: Path) -> None:
    feed(jsongen.build_document(people(), "wrapped", wrapper_path=("result", "items")))
    r = run("--lang", "en", "filter", "-", tmp_path / "t.jsonl", "--record-path", "result.items",
            "--where", "city == 'Toshkent'")
    assert r.code == 0, r.err
    assert [rec["id"] for rec in read_jsonl(tmp_path / "t.jsonl")] == [0, 3, 6]
    feed(array_bytes())
    r = run("--lang", "en", "search", "-", "Toshkent", "--count")
    assert r.code == 0 and r.out.strip().startswith("3 ")
    assert spool_files() == []


# ----------------------------------------------------------------------------------
# refusals (nothing is read)
# ----------------------------------------------------------------------------------
def test_a_terminal_as_stdin_is_refused(run, tty, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    r = run("--lang", "en", "inspect", "-")
    assert r.code == 1 and "Error: Standard input is the terminal: nothing is piped to -." in r.err
    assert "Hint: Pipe the data in (for example curl ... | jsonreshape inspect -) or give a file name." in r.err
    r = run("--lang", "uz", "convert", "-", tmp_path / "o.jsonl")
    assert r.code == 1 and "Standart kirish terminal" in r.err and "quvur" in r.err
    monkeypatch.setattr(sys, "stdin", None)      # no standard input at all (pythonw)
    r = run("--lang", "en", "inspect", "-")
    assert r.code == 1 and "Standard input is the terminal" in r.err
    assert spool_files() == []


def test_dash_as_an_output_is_refused(run, tty, tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(array_bytes())
    message = "Error: The output cannot be - (standard output): results are written to files, give a file name."
    for argv in (("convert", src, "-"), ("filter", src, "-", "--where", "id > 1"), ("merge", src, "-o", "-"),
                 ("search", src, "x", "--export", "-"), ("schema", src, "-o", "-"), ("schema", src, "-"),
                 ("split", src, "--parts", "2", "-o", "-"), ("diff", src, src, "-o", "-"),
                 ("repair", src, "-"), ("repair", src, "--report", "-", "-o", str(tmp_path / "r.json")),
                 ("extract", src, "-"), ("dupes", src, "--key", "id", "-o", "-"),
                 ("import", src, "-", "--from", "csv"), ("convert", "-", "-")):    # the output is checked first
        r = run("--lang", "en", *argv)
        assert r.code == 1 and message in r.err, (argv, r.err)
    assert spool_files() == []


def test_two_dashes_are_refused(run, tty, tmp_path: Path) -> None:
    for argv in (("merge", "-", "-", "-o", tmp_path / "m.json"), ("diff", "-", "-"),
                 ("merge", str(tmp_path / "a.json"), "-", "-", "-o", tmp_path / "m.json")):
        r = run("--lang", "en", *argv)
        assert r.code == 1 and "Error: Only one INPUT may be - (standard input)." in r.err, argv
    assert spool_files() == []


def test_dash_for_a_file_that_is_not_an_input_is_refused(run, tty, tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_bytes(array_bytes())
    message = "Error: Only the INPUT of a command may be - (standard input); the other files are given by name."
    for argv in (("validate", src, "--schema", "-"), ("patch", src, "-", tmp_path / "o.json"),
                 ("apply", "-", src, tmp_path / "o.json")):
        r = run("--lang", "en", *argv)
        assert r.code == 1 and message in r.err, (argv, r.err)


def test_patch_in_place_from_stdin_is_refused(run, tty, feed, tmp_path: Path) -> None:
    patch = tmp_path / "p.json"
    patch.write_bytes(b'[{"op": "replace", "path": "/0/name", "value": "Zed"}]')
    r = run("--lang", "en", "patch", "-", patch, "--in-place")
    assert r.code == 1 and "Error: patch --in-place cannot work on standard input (-): give an OUTPUT file." in r.err
    feed(array_bytes())                                   # with an OUTPUT the patch works
    r = run("--lang", "en", "patch", "-", patch, tmp_path / "o.json")
    assert r.code == 0, r.err
    out = codec.loads((tmp_path / "o.json").read_bytes())
    assert out[0]["name"] == "Zed" and out[1] == people()[1]
    assert spool_files() == []


def test_split_from_stdin_needs_a_folder(run, tty, feed, tmp_path: Path) -> None:
    r = run("--lang", "en", "split", "-", "--parts", "2")
    assert r.code == 1 and "Error: split from standard input (-) needs -o DIR (the folder of the parts)." in r.err
    assert "Hint: Name the parts with --name PATTERN" in r.err
    out_dir = tmp_path / "parts"
    out_dir.mkdir()
    feed(array_bytes())
    r = run("--lang", "en", "split", "-", "--parts", "2", "-o", out_dir, "--name", "part_{n}.json")
    assert r.code == 0, r.err
    assert sorted(p.name for p in out_dir.iterdir()) == ["part_1.json", "part_2.json"]
    assert sum(len(codec.loads(p.read_bytes())) for p in out_dir.iterdir()) == 8
    assert spool_files() == []


def test_import_from_stdin_needs_from(run, tty, feed, tmp_path: Path) -> None:
    r = run("--lang", "en", "import", "-", tmp_path / "o.json")
    assert r.code == 1
    assert "Error: import from standard input (-) needs --from csv|yaml|xml: the type cannot be told from a file name." in r.err
    feed("id,name\n1,Anvar\n2,Bobur\n".encode("utf-8"))
    r = run("--lang", "en", "import", "-", tmp_path / "o.json", "--from", "csv")
    assert r.code == 0, r.err
    assert codec.loads((tmp_path / "o.json").read_bytes()) == [{"id": 1, "name": "Anvar"}, {"id": 2, "name": "Bobur"}]
    assert "read from standard input" in r.err and spool_files() == []


# ----------------------------------------------------------------------------------
# the spool file: removed in every case; the free-space rule; cancellation
# ----------------------------------------------------------------------------------
def test_spool_file_is_removed_after_a_json_error(run, feed, spooled, tmp_path: Path) -> None:
    broken = b'[{"id": 1}, {"id": '
    feed(broken)
    r = run("--lang", "en", "convert", "-", tmp_path / "o.jsonl")
    assert r.code == 2 and "JSON error" in r.err and "^" in r.err
    assert f"{len(broken)} B read from standard input" in r.err
    assert len(spooled) == 1 and not spooled[0].exists() and spool_files() == []
    assert not (tmp_path / "o.jsonl").exists()
    feed(b"")                                           # an empty pipe: not usable at all, still cleaned up
    r = run("--lang", "en", "inspect", "-")
    assert r.code == 2 and spool_files() == []


def test_spool_file_is_removed_after_a_user_error_in_the_command(run, feed, spooled, tmp_path: Path) -> None:
    feed(array_bytes())
    r = run("--lang", "en", "filter", "-", tmp_path / "o.jsonl", "--where", "id >")
    assert r.code == 1 and len(spooled) == 1 and not spooled[0].exists()
    feed(array_bytes())
    r = run("--lang", "en", "sort", "-", tmp_path / "o2.jsonl", "--by", "nosuch.field..")
    assert r.code == 1 and spool_files() == []


class _CancellingStream:
    """A pipe whose producer cancels the command after the first piece was read."""

    def __init__(self, data: bytes, token: CancelToken) -> None:
        self._data = data
        self._token = token
        self.reads = 0

    def isatty(self) -> bool:
        return False

    @property
    def buffer(self) -> _CancellingStream:
        return self

    def read1(self, n: int) -> bytes:
        self.reads += 1
        piece, self._data = self._data[:n], self._data[n:]
        self._token.cancel()
        return piece


def test_cancellation_while_reading_removes_the_spool_file(run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = CancelToken()
    stream = _CancellingStream(array_bytes() * 400, token)
    monkeypatch.setattr(sys, "stdin", stream)
    r = run("--lang", "en", "convert", "-", tmp_path / "o.jsonl", cancel=token)
    assert r.code == 3 and stream.reads == 1
    assert "read from standard input" not in r.err
    assert spool_files() == [] and not (tmp_path / "o.jsonl").exists()


def test_free_space_rule(run, feed, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # before the first piece: less than 64 MiB free -> refused, nothing written
    monkeypatch.setattr(stdin_input, "free_space", lambda folder: 10 * MiB)
    feed(array_bytes())
    r = run("--lang", "en", "convert", "-", tmp_path / "o.jsonl")
    assert r.code == 1 and "Error: Not enough free disk space:" in r.err and str(tmp_folder()) in r.err
    assert "Hint: Free up some disk space and try again." in r.err
    assert spool_files() == [] and not (tmp_path / "o.jsonl").exists()
    # while reading: checked at every FREE_SPACE_CHECK_EVERY bytes against bytes read + 64 MiB
    calls: list[int] = []

    def tight(folder: Any) -> int:
        calls.append(1)
        # the start (0 bytes read + 64 MiB needed) passes; once anything was read, exactly 64 MiB is too little
        return 100 * MiB if len(calls) == 1 else 64 * MiB

    monkeypatch.setattr(stdin_input, "free_space", tight)
    monkeypatch.setattr(stdin_input, "FREE_SPACE_CHECK_EVERY", 4096)
    feed(array_bytes() * 50)
    r = run("--lang", "en", "convert", "-", tmp_path / "o.jsonl")
    assert r.code == 1 and "Not enough free disk space" in r.err and len(calls) == 2
    assert spool_files() == []
    # enough space at every check: the data is read whole (3 checks for ~48 KB in 4 KiB steps -> many)
    monkeypatch.setattr(stdin_input, "free_space", lambda folder: 10 * (1 << 30))
    feed(array_bytes() * 50)
    r = run("--lang", "en", "inspect", "-", "--json", "--format", "stream")
    assert r.code == 0, r.err
    assert json.loads(r.out)["records"] == 50 and spool_files() == []


def test_unknown_free_space_does_not_block(run, feed, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(stdin_input, "free_space", lambda folder: None)
    feed(array_bytes())
    r = run("--lang", "en", "convert", "-", tmp_path / "o.jsonl")
    assert r.code == 0 and len(read_jsonl(tmp_path / "o.jsonl")) == 8


def test_spool_stdin_directly(tmp_path: Path) -> None:
    """The module alone: progress in bytes without a total, the pieces, the result."""
    from jsonreshape.tasks import Progress

    data = bytes(range(256)) * 20000           # 5 MB of binary data: no newline translation anywhere
    seen: list[tuple[int, int | None]] = []
    progress = Progress(callback=lambda info: seen.append((info.done, info.total)), min_interval=0.0)
    spool = stdin_input.spool_stdin(tmp_path, stream=io.TextIOWrapper(io.BytesIO(data)), progress=progress)
    try:
        assert spool.size == len(data) and spool.path.parent == tmp_path and SPOOL_RE.match(spool.path.name)
        assert spool.path.read_bytes() == data
        assert seen and seen[-1] == (len(data), None) and all(total is None for _, total in seen)
        assert progress.finished
    finally:
        assert spool.remove() and not spool.path.exists()
    assert spool.remove()                      # gone already: still True
    assert stdin_input.stdin_is_tty(_Tty()) is True and stdin_input.stdin_is_tty(io.BytesIO(b"x")) is False
    with pytest.raises(JsonReshapeError):      # a folder that does not exist: a localized error, nothing left behind
        stdin_input.spool_stdin(tmp_path / "missing", stream=io.BytesIO(b"[]"))
    assert not (tmp_path / "missing").exists()


# ----------------------------------------------------------------------------------
# several inputs: merge / diff
# ----------------------------------------------------------------------------------
def test_merge_with_stdin(run, feed, tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    a.write_bytes(array_bytes([{"id": 100}, {"id": 101}, {"id": 102}]))
    feed(array_bytes())
    r = run("--lang", "en", "merge", a, "-", "-o", tmp_path / "m.jsonl")
    assert r.code == 0, r.err
    assert [rec["id"] for rec in read_jsonl(tmp_path / "m.jsonl")] == [100, 101, 102, *range(8)]
    feed(array_bytes())
    r = run("--lang", "en", "merge", "-", a, "-o", tmp_path / "m2.jsonl", "--dedupe-by", "id")
    assert r.code == 0, r.err
    assert [rec["id"] for rec in read_jsonl(tmp_path / "m2.jsonl")] == [*range(8), 100, 101, 102]
    assert spool_files() == []


def test_diff_with_stdin(run, feed, tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    a.write_bytes(array_bytes())
    changed = people()
    changed[2]["name"] = "changed"
    feed(array_bytes(changed))
    r = run("--lang", "en", "diff", a, "-", "--key", "id", "--json", "--exit-code")
    assert r.code == 5, r.err
    lines = [json.loads(line) for line in r.out.splitlines() if line.strip()]
    assert lines[-1]["summary"]["changed"] == 1
    feed(array_bytes())
    r = run("--lang", "en", "diff", "-", a, "--key", "id", "--summary", "--exit-code")
    assert r.code == 0, r.err
    assert spool_files() == []


# ----------------------------------------------------------------------------------
# console
# ----------------------------------------------------------------------------------
def test_quiet_hides_the_stdin_summary(run, feed, tmp_path: Path) -> None:
    feed(array_bytes())
    r = run("--lang", "en", "-q", "convert", "-", tmp_path / "o.jsonl")
    assert r.code == 0 and r.err == "" and len(read_jsonl(tmp_path / "o.jsonl")) == 8


def test_stdin_summary_in_uzbek(run, feed, tmp_path: Path) -> None:
    feed(array_bytes())
    r = run("--lang", "uz", "convert", "-", tmp_path / "o.jsonl")
    assert r.code == 0
    assert re.search(r"^Standart kirishdan \d+ B o'qildi \(vaqtinchalik fayl .*stdin-\d+-.*\.json, buyruq "
                     r"tugagach o'chiriladi\)$", r.err, flags=re.M), r.err


def test_help_mentions_standard_input(run) -> None:
    r = run("--lang", "en", "convert", "--help")
    assert r.code == 0 and "or - for standard input" in " ".join(r.out.split())   # (argparse wraps the help)
    r = run("--lang", "en", "import", "--help")
    assert r.code == 0 and "for - standard input" in " ".join(r.out.split())


# ----------------------------------------------------------------------------------
# memory: a big pipe is spooled in pieces
# ----------------------------------------------------------------------------------
@pytest.mark.slow
def test_a_50_mb_pipe_keeps_the_memory_flat(tmp_path: Path) -> None:
    import psutil

    rec = b'{"id": %d, "name": "name%07d", "city": "Toshkent", "tags": ["a", "b"], "score": 12.5}'
    parts = []
    size = 1
    i = 0
    while size < 50 * MiB:
        piece = rec % (i, i)
        parts.append(piece)
        size += len(piece) + 1
        i += 1
    data = b"[" + b",".join(parts) + b"]"
    del parts
    assert len(data) >= 50 * MiB
    src = tmp_path / "big.json"
    src.write_bytes(data)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    base = [sys.executable, "-m", "jsonreshape", "--lang", "en", "-q", "convert"]
    p = subprocess.run([*base, str(src), str(tmp_path / "file.jsonl")], cwd=str(ROOT), env=env, capture_output=True,
                       timeout=600)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    proc = subprocess.Popen([*base, "-", str(tmp_path / "pipe.jsonl")], cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None

    def produce() -> None:
        try:
            for start in range(0, len(data), MiB):
                proc.stdin.write(data[start:start + MiB])
        finally:
            proc.stdin.close()

    writer = threading.Thread(target=produce, daemon=True)
    writer.start()
    # the whole process tree: on Windows the venv's python.exe is a launcher that runs the real interpreter as
    # its child. Polled RSS of the tree every 10 ms, and the OS's own peak working set where it exists (no spike
    # is missed there).
    root = psutil.Process(proc.pid)
    peak = 0
    peak_wset = 0
    while proc.poll() is None:
        total = 0
        try:
            procs = [root, *root.children(recursive=True)]
        except psutil.Error:  # the child ended between poll() and the measurement
            break
        for p in procs:
            try:
                info = p.memory_info()
            except psutil.Error:
                continue
            total += info.rss
            peak_wset = max(peak_wset, getattr(info, "peak_wset", 0))
        peak = max(peak, total)
        time.sleep(0.01)
    writer.join(timeout=60)
    proc.stdin = None                  # closed by the writer: communicate() on POSIX would flush it again
    out, err = proc.communicate(timeout=600)
    assert proc.returncode == 0, err.decode("utf-8", "replace")
    assert (tmp_path / "pipe.jsonl").read_bytes() == (tmp_path / "file.jsonl").read_bytes()
    assert peak > 20 * MiB, "the measurement saw the interpreter"
    assert max(peak, peak_wset) < 150 * MiB, f"peak RSS of the child: {max(peak, peak_wset) / MiB:.0f} MB"
    assert spool_files() == []
