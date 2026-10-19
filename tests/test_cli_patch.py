"""``jsonreshape patch INPUT PATCH (OUTPUT | --in-place [--no-backup])`` (docs/ARCHITECTURE.md §12.8, §10).

In-process (``cli.main``) and as a subprocess: OUTPUT in the same kind and encoding (gzip, UTF-16 with BOM),
``--in-place`` with ``INPUT.bak``, ``--no-backup``, a patch that changes nothing (INPUT untouched), every
exit code (1: patch problems and a failed ``test`` -- nothing written --, bad option combinations, a file with
pending changes or edited elsewhere; 2: a JSON error in INPUT; 3: cancelled), JSON Lines with broken lines
(``--allow-partial`` drops them), keyed / wrapped / record-path inputs, English messages, the temporary edit
layer removed afterwards.
"""

from __future__ import annotations

import gzip
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import cli, codec, i18n
from jsonreshape.tasks import CancelToken

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _cli_isolation(monkeypatch: pytest.MonkeyPatch):
    old_backend = os.environ.get("JSONRESHAPE_BACKEND")
    monkeypatch.setenv("JSONRESHAPE_LANG", "uz")
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


RECORDS = [{"id": 1, "name": "Anvar", "n": codec.loads("1.10")}, {"id": 2, "name": "Тошкент", "n": codec.loads("1e400")},
           {"id": 3, "name": "O'zbekiston", "n": codec.loads("12345678901234567890")}]
PATCH = [{"op": "test", "path": "/0/id", "value": 1}, {"op": "replace", "path": "/0/name", "value": "Ali"},
         {"op": "remove", "path": "/1"}, {"op": "add", "path": "/-", "value": {"id": 4, "n": codec.loads("-0.0")}},
         {"op": "move", "from": "/0", "path": "/-"}]
EXPECTED = [RECORDS[2], {"id": 4, "n": codec.loads("-0.0")}, {**RECORDS[0], "name": "Ali"}]


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def no_layer_left() -> None:
    from jsonreshape.appdirs import work_dir

    assert not [p for p in work_dir().iterdir() if p.name.startswith("patch-")]


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    src = write(tmp_path / "in.json", codec.dumps(RECORDS, indent=2))
    patch = write(tmp_path / "fix.json", codec.dumps(PATCH))
    return src, patch


# ----------------------------------------------------------------------------------
# in-process
# ----------------------------------------------------------------------------------
def test_patch_to_output(run, files: tuple[Path, Path], tmp_path: Path) -> None:
    src, patch = files
    before = src.read_bytes()
    r = run("patch", src, patch, tmp_path / "out.json")
    assert r.code == 0, r.err
    assert "5 ta amal qo'llandi" in r.err and str(tmp_path / "out.json") in r.err
    out = (tmp_path / "out.json").read_bytes()
    assert codec.loads(out) == EXPECTED and b"1e400" not in out and b"12345678901234567890" in out
    assert b'"n": -0.0' in out and b"\n  {\n" in out                 # exact numbers, the input's pretty style
    assert src.read_bytes() == before and not (tmp_path / "in.json.bak").exists()
    no_layer_left()


def test_patch_in_place_and_backup(run, files: tuple[Path, Path], tmp_path: Path) -> None:
    src, patch = files
    before = src.read_bytes()
    r = run("patch", src, patch, "--in-place", "-q")
    assert r.code == 0 and r.err == "" and r.out == ""
    assert codec.loads(src.read_bytes()) == EXPECTED and (tmp_path / "in.json.bak").read_bytes() == before
    write(src, before)
    (tmp_path / "in.json.bak").unlink()
    r = run("patch", src, patch, "--in-place", "--no-backup", "--lang", "en")
    assert r.code == 0 and "5 operations applied" in r.err and "Backup" not in r.err
    assert codec.loads(src.read_bytes()) == EXPECTED and not (tmp_path / "in.json.bak").exists()
    no_layer_left()


def test_patch_that_changes_nothing(run, files: tuple[Path, Path], tmp_path: Path) -> None:
    src, _patch = files
    before = src.read_bytes()
    mtime = src.stat().st_mtime_ns
    tests = write(tmp_path / "t.json", codec.dumps([{"op": "test", "path": "/1/name", "value": "Тошкент"}]))
    r = run("patch", src, tests, "--in-place")
    assert r.code == 0 and "hech narsani o'zgartirmadi" in r.err
    assert src.read_bytes() == before and src.stat().st_mtime_ns == mtime and not (tmp_path / "in.json.bak").exists()
    r = run("patch", src, tests, tmp_path / "same.json")
    assert r.code == 0 and codec.loads((tmp_path / "same.json").read_bytes()) == RECORDS


@pytest.mark.parametrize("patch, key", [
    ([{"op": "test", "path": "/0/id", "value": 2}], "kutilganiga teng emas"),
    ([{"op": "remove", "path": "/9"}], "9-o'rin yo'q"),
    ([{"op": "replace", "path": "/0/x", "value": 1}], "mavjud emas"),
    ([{"op": "frob", "path": "/0"}], "noma'lum"),
])
def test_failed_patch_writes_nothing(run, files: tuple[Path, Path], tmp_path: Path, patch: list, key: str) -> None:
    src, _p = files
    before = src.read_bytes()
    bad = write(tmp_path / "bad.json", codec.dumps(patch))
    for argv in ((tmp_path / "out.json",), ("--in-place",)):
        r = run("patch", src, bad, *argv)
        assert r.code == 1 and key in r.err and "Xato:" in r.err
        assert src.read_bytes() == before and not (tmp_path / "out.json").exists()
        assert not (tmp_path / "in.json.bak").exists()
    no_layer_left()


def test_usage_and_user_errors(run, files: tuple[Path, Path], tmp_path: Path) -> None:
    src, patch = files
    r = run("patch", src, patch)
    assert r.code == 1 and "--in-place" in r.err
    r = run("patch", src, patch, tmp_path / "o.json", "--in-place")
    assert r.code == 1
    r = run("patch", src, patch, tmp_path / "o.json", "--no-backup")
    assert r.code == 1 and "--no-backup" in r.err
    r = run("patch", src, tmp_path / "missing.json", tmp_path / "o.json")
    assert r.code == 1
    r = run("patch", tmp_path / "missing.json", patch, tmp_path / "o.json")
    assert r.code == 1
    r = run("patch", src, patch, src)
    assert r.code == 1
    write(tmp_path / "notjson.json", b'[{"op": "remove", "path": "/0"},')
    r = run("patch", src, tmp_path / "notjson.json", tmp_path / "o.json")
    assert r.code == 1 and "JSON Patch fayli to'g'ri JSON emas" in r.err and "Maslahat" in r.err
    r = run("patch", "--lang", "en", src, tmp_path / "notjson.json", tmp_path / "o.json")
    assert r.code == 1 and "is not valid JSON" in r.err
    assert not (tmp_path / "o.json").exists()


def test_json_error_in_input_is_exit_2(run, tmp_path: Path) -> None:
    src = write(tmp_path / "bad.json", b'[{"a": 1}, {"a": ]')
    patch = write(tmp_path / "p.json", b"[]")
    r = run("patch", src, patch, tmp_path / "o.json")
    assert r.code == 2 and not (tmp_path / "o.json").exists()


def test_cancelled(run, files: tuple[Path, Path], tmp_path: Path) -> None:
    src, patch = files
    token = CancelToken()
    token.cancel()
    r = run("patch", src, patch, tmp_path / "o.json", cancel=token)
    assert r.code == 3 and not (tmp_path / "o.json").exists()
    no_layer_left()


@pytest.mark.parametrize("variant", ["gzip", "utf-16-le-bom", "utf-8-bom"])
def test_encodings_are_kept(run, tmp_path: Path, variant: str) -> None:
    text = codec.dumps(RECORDS)
    patch = write(tmp_path / "p.json", codec.dumps(PATCH))
    if variant == "gzip":
        src = write(tmp_path / "in.json.gz", gzip.compress(text))
        out = tmp_path / "out.json.gz"
    elif variant == "utf-16-le-bom":
        src = write(tmp_path / "in.json", b"\xff\xfe" + text.decode("utf-8").encode("utf-16-le"))
        out = tmp_path / "out.json"
    else:
        src = write(tmp_path / "in.json", b"\xef\xbb\xbf" + text)
        out = tmp_path / "out.json"
    r = run("patch", src, patch, out)
    assert r.code == 0, r.err
    raw = out.read_bytes()
    if variant == "gzip":
        raw = gzip.decompress(raw)
    elif variant == "utf-16-le-bom":
        assert raw[:2] == b"\xff\xfe"
        raw = raw[2:].decode("utf-16-le").encode("utf-8")
    else:
        assert raw[:3] == b"\xef\xbb\xbf"
        raw = raw[3:]
    assert codec.loads(raw) == EXPECTED
    r = run("patch", src, patch, "--in-place")
    assert r.code == 0 and (tmp_path / (src.name + ".bak")).exists()


def test_jsonl_with_broken_lines(run, tmp_path: Path) -> None:
    src = write(tmp_path / "in.jsonl", b'{"id": 1}\n{broken\n{"id": 2}\n')
    patch = write(tmp_path / "p.json", codec.dumps([{"op": "add", "path": "/-", "value": {"id": 3}}]))
    r = run("patch", src, patch, tmp_path / "o.jsonl")
    assert r.code == 1 and "--allow-partial" in r.err and not (tmp_path / "o.jsonl").exists()
    r = run("patch", src, patch, tmp_path / "o.jsonl", "--allow-partial")
    assert r.code == 0, r.err
    assert (tmp_path / "o.jsonl").read_bytes() == b'{"id": 1}\n{"id": 2}\n{"id": 3}\n'   # the file's ": " style


def test_keyed_wrapped_and_record_path(run, tmp_path: Path) -> None:
    keyed = write(tmp_path / "k.json", b'{"a": {"v": 1}, "b": {"v": 2}, "a/b": {"v": 3}}')
    patch = write(tmp_path / "p.json", codec.dumps([{"op": "move", "from": "/a", "path": "/z"},
                                                    {"op": "replace", "path": "/a~1b/v", "value": 30}]))
    r = run("patch", keyed, patch, tmp_path / "ko.json", "--format", "keyed")
    assert r.code == 0, r.err
    assert (tmp_path / "ko.json").read_bytes() == b'{"z": {"v": 1}, "b": {"v": 2}, "a/b": {"v": 30}}'
    wrapped = write(tmp_path / "w.json", b'{"meta": {"n": 2}, "result": {"items": [{"id": 1}, {"id": 2}]}}')
    patch = write(tmp_path / "pw.json", codec.dumps([{"op": "replace", "path": "/meta/n", "value": 1},
                                                     {"op": "remove", "path": "/result/items/0"}]))
    r = run("patch", wrapped, patch, tmp_path / "wo.json", "--record-path", "result.items")
    assert r.code == 0, r.err
    assert codec.loads((tmp_path / "wo.json").read_bytes()) == {"meta": {"n": 1}, "result": {"items": [{"id": 2}]}}
    lock = write(tmp_path / "pl.json", codec.dumps([{"op": "remove", "path": "/result/items"}]))
    r = run("patch", wrapped, lock, tmp_path / "wo2.json", "--record-path", "result.items")
    assert r.code == 1 and "konteyneri" in r.err


def test_pending_layer_and_locked_file(run, files: tuple[Path, Path], tmp_path: Path) -> None:
    from jsonreshape.edits import EditSession, Ref
    from jsonreshape.reader import Document

    src, patch = files
    doc = Document.open(src)
    doc.build_index()
    s = EditSession.open(doc)
    try:
        s.set_value(Ref(0, ("id",)), "100")
        r = run("patch", src, patch, tmp_path / "o.json")
        assert r.code == 1 and "boshqa oynada" in r.err                  # edited elsewhere: locked
    finally:
        s.close()                                                       # unsaved changes are kept (recovery)
        doc.close()
    r = run("patch", src, patch, tmp_path / "o.json")
    assert r.code == 1 and "saqlanmagan o'zgarishlar" in r.err          # the pending layer is not touched
    assert not (tmp_path / "o.json").exists()


# ----------------------------------------------------------------------------------
# subprocess
# ----------------------------------------------------------------------------------
def test_subprocess_end_to_end(tmp_path: Path, files: tuple[Path, Path]) -> None:
    src, patch = files
    env = dict(os.environ, JSONRESHAPE_LANG="en", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-m", "jsonreshape", "patch", str(src), str(patch), "--in-place"],
                          cwd=ROOT, env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"5 operations applied" in proc.stderr and b"Backup:" in proc.stderr
    assert codec.loads(src.read_bytes()) == EXPECTED
    bad = write(tmp_path / "t.json", codec.dumps([{"op": "test", "path": "/0/id", "value": 1}]))
    proc = subprocess.run([sys.executable, "-m", "jsonreshape", "patch", str(src), str(bad), str(tmp_path / "o.json"),
                           "--backend", "python"], cwd=ROOT, env=env, capture_output=True, timeout=120)
    assert proc.returncode == 1 and b"differs from the expected one" in proc.stderr
    assert not (tmp_path / "o.json").exists()


def test_partial_input_cannot_be_patched(run, tmp_path: Path) -> None:
    src = write(tmp_path / "bad.json", b'[{"a": 1}, {"a": ]')
    patch = write(tmp_path / "p.json", b"[]")
    r = run("patch", src, patch, tmp_path / "o.json", "--allow-partial")
    assert r.code == 2 and "tahrirlab bo'lmaydi" in r.err and not (tmp_path / "o.json").exists()
