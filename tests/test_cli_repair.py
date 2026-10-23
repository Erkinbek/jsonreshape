"""``jsonreshape repair INPUT [OUTPUT | -o OUTPUT] [--check] [--nan null|string] [--jsonl] [--allow-partial]
[--report FILE] [--json] [--format F] [--record-path P]`` (docs/ARCHITECTURE.md §14.0, §14.5, §14.8;
s5/map_repair_diff.md §3.7; AGENT_PROMPT §10 "Ta'mirlash", §13).

The report (headline, one line per fix with its first place, the cut, the data-changing fixes as a warning)
in Uzbek and English; exact numbers kept; every exit code: 0 written (also "nothing to repair": an unchanged
copy), ``--check`` 0 / 2 and nothing written, 2 unrepairable (nothing written; the place with a caret and the
``--allow-partial`` advice), ``--allow-partial`` 0 with the problem and partial lines and never "no problems
found" (review R5F-6), 1 user errors (OUTPUT / ``--report`` naming the input in any spelling, a missing OUTPUT,
an option the input kind refuses), 2 an empty file, 3 cancelled, 4 a failed output check; ``--report``
CSV / JSON / JSON Lines; ``--json``; ``--nan string``; ``--jsonl``; JSON Lines lines; gzip / UTF-16 sources;
every core backend.
"""

from __future__ import annotations

import csv
import gzip
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

from jsonreshape import cli, codec, i18n
from jsonreshape.errors import JsonReshapeError
from jsonreshape.tasks import CancelToken

ROOT = Path(__file__).resolve().parents[1]


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


BROKEN = b'[{"a": 1,}, {"b": NaN}, // c\n {"c": \'Toshkent\'}, {"d": 2}'
FIXED = b'[{"a": 1}, {"b": null}, \n {"c": "Toshkent"}, {"d": 2}]'
NUMBERS = b'[{"v": 0.1}, {"v": 1e400,}, {"v": 12345678901234567890}, {"v": -0.0}, {"v": 1.10}, {"t": "\xd0\xa2"},]'
UNREPAIRABLE = b'[{"a": 1}, {"a": hello}, {"a": 3}]'
NONE_TEXTS = ("Muammo topilmadi", "No problems found")


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    return write(tmp_path / "broken.json", BROKEN)


# ----------------------------------------------------------------------------------
# repairs and their report
# ----------------------------------------------------------------------------------
def test_repair_writes_and_reports_every_fix(run, broken: Path, tmp_path: Path) -> None:
    out = tmp_path / "fixed.json"
    r = run("repair", broken, out)
    assert r.code == 0, r.err
    assert out.read_bytes() == FIXED and broken.read_bytes() == BROKEN
    lines = r.err.splitlines()
    assert lines[0].startswith("Ta'mirlandi: broken.json → fixed.json (") and "4 ta yozuv" in lines[0]
    assert "  ortiqcha (oxirgi) vergul olib tashlandi: 1 ta (birinchisi 1-qator, 9-ustun)" in lines
    assert "  NaN → null: 1 ta (birinchisi 1-qator, 19-ustun)" in lines
    assert "  izoh olib tashlandi: 1 ta (birinchisi 1-qator, 25-ustun)" in lines
    assert "  bitta tirnoq qo'sh tirnoqqa almashtirildi: 1 ta (birinchisi 2-qator, 8-ustun)" in lines
    assert any(line.startswith("Ogohlantirish: Ma'lumotni o'zgartirgan yoki tashlab yuborgan tuzatishlar: NaN")
               for line in lines)
    assert "Har bir tuzatish ro'yxati uchun --report FAYL.csv qo'shing." in lines
    assert not any(text in r.err for text in NONE_TEXTS) and r.out == ""


def test_repair_in_english_with_o_option(run, broken: Path, tmp_path: Path) -> None:
    out = tmp_path / "fixed.json"
    r = run("--lang", "en", "repair", broken, "-o", out)
    assert r.code == 0 and out.read_bytes() == FIXED
    assert "Repaired: broken.json → fixed.json" in r.err and "  trailing comma removed: 1 (first at line 1" in r.err


def test_repair_keeps_exact_numbers(run, tmp_path: Path) -> None:
    src = write(tmp_path / "n.json", NUMBERS)
    out = tmp_path / "n_fixed.json"
    r = run("repair", src, out)
    assert r.code == 0, r.err
    data = out.read_bytes()
    assert data == NUMBERS.replace(b"1e400,}", b"1e400}").replace(b"},]", b"}]")
    assert codec.loads(data)[1]["v"] == codec.loads(b"1e400")


def test_repair_of_a_valid_file_writes_a_copy(run, tmp_path: Path) -> None:
    src = write(tmp_path / "ok.json", '[{"a": 1}, {"b": "Тошкент"}]'.encode())
    out = tmp_path / "ok2.json"
    r = run("repair", src, out)
    assert r.code == 0 and out.read_bytes() == src.read_bytes()
    assert "Muammo topilmadi; o'zgarishsiz nusxa yozildi." in r.err
    r = run("repair", src, "--check")
    assert r.code == 0 and "Muammo topilmadi." in r.err


def test_repair_check(run, broken: Path, tmp_path: Path) -> None:
    r = run("repair", broken, "--check")
    assert r.code == 2 and r.err.startswith("Tekshirildi: broken.json") and "NaN → null" in r.err
    out = tmp_path / "never.json"
    r = run("repair", broken, out, "--check")
    assert r.code == 2 and "--check hech narsa yozmaydi" in r.err and not out.exists()
    r = run("repair", write(tmp_path / "bad.json", UNREPAIRABLE), "--check")
    assert r.code == 2 and "Ta'mirlab bo'lmaydigan joy" in r.err
    assert not any(text in r.err for text in NONE_TEXTS)


# ----------------------------------------------------------------------------------
# unrepairable files
# ----------------------------------------------------------------------------------
def test_unrepairable_writes_nothing(run, tmp_path: Path) -> None:
    src = write(tmp_path / "bad.json", UNREPAIRABLE)
    out = tmp_path / "out.json"
    r = run("repair", src, out)
    assert r.code == 2 and not out.exists()
    lines = r.err.splitlines()
    assert lines[0] == "Ta'mirlab bo'lmadi: bad.json (hech narsa yozilmadi)"
    error = lines.index(next(line for line in lines if line.startswith("Xato: JSON'ni ta'mirlab bo'lmadi")))
    assert lines[error + 1] == '  [{"a": 1}, {"a": hello}, {"a": 3}]'
    assert lines[error + 2] == "  " + " " * 17 + "^"
    assert lines[error + 3] == ("Maslahat: Muammodan oldingi 1 ta to'liq yozuvni saqlash uchun --allow-partial "
                                "qo'shing yoki shu joyni qo'lda tuzating.")
    assert sum("Ta'mirlab bo'lmaydigan joy" in line for line in lines) == 0     # said once, in the error
    assert not any(text in r.err for text in NONE_TEXTS)


def test_allow_partial_with_a_problem_never_says_no_problems(run, tmp_path: Path) -> None:
    src = write(tmp_path / "bad.json", UNREPAIRABLE)
    out = tmp_path / "out.json"
    report = tmp_path / "fixes.jsonl"
    r = run("repair", src, out, "--allow-partial", "--report", report)
    assert r.code == 0, r.err
    assert out.read_bytes() == b'[{"a": 1}]'
    assert "Ta'mirlab bo'lmaydigan joy: " in r.err and "(1-qator, 18-ustun)" in r.err
    assert "Qisman natija yozildi: muammodan oldingi 1 ta yozuv." in r.err
    assert ("Ogohlantirish: Fayl to'liq ta'mirlanmadi: 1-qator, 18-ustundagi muammodan keyingi ma'lumot natijaga "
            "yozilmadi.") in r.err
    assert not any(text in r.err for text in NONE_TEXTS)
    assert report.exists()
    r = run("--lang", "en", "repair", src, out, "--allow-partial")
    assert r.code == 0 and "Partial result written" in r.err and not any(text in r.err for text in NONE_TEXTS)


def test_a_cut_file_is_never_reported_as_clean(run, tmp_path: Path) -> None:
    src = write(tmp_path / "cut.json", b'[{"a": 1}, {"b": 2}, {"c": ')
    out = tmp_path / "out.json"
    r = run("repair", src, out)
    assert r.code == 0 and codec.loads(out.read_bytes()) == [{"a": 1}, {"b": 2}]
    assert "kesilgan joy:" in r.err and not any(text in r.err for text in NONE_TEXTS)
    r = run("repair", src, "--check")
    assert r.code == 2 and not any(text in r.err for text in NONE_TEXTS)


# ----------------------------------------------------------------------------------
# reports and options
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["csv", "json", "jsonl"])
def test_repair_report_file(run, broken: Path, tmp_path: Path, fmt: str) -> None:
    report = tmp_path / f"fixes.{fmt}"
    r = run("repair", broken, tmp_path / "fixed.json", "--report", report)
    assert r.code == 0, r.err
    data = report.read_bytes()
    if fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    elif fmt == "json":
        rows = json.loads(data)
    else:
        rows = [json.loads(line) for line in data.splitlines()]
    codes = [row["code"] for row in rows]
    assert codes[:4] == ["trailing_comma", "nan", "comment", "single_quote"] and "truncated" in codes
    assert "Tuzatishlar ro'yxati (5 ta) yozildi:" in r.err
    r = run("repair", broken, "--check", "--report", report)
    assert r.code == 2 and report.exists()


def test_repair_json_output(run, broken: Path, tmp_path: Path) -> None:
    r = run("repair", broken, tmp_path / "f.json", "--json")
    assert r.code == 0, r.err
    data = json.loads(r.out)
    assert data["written"] is True and data["total"] == 5 and data["counts"]["nan"] == 1
    assert data["kind"] == "array" and data["output_kind"] == "array" and data["records"] == 4
    assert data["problem"] is None and data["truncated"]["kept_records"] == 4


def test_repair_options(run, tmp_path: Path) -> None:
    src = write(tmp_path / "nan.json", b'[{"x": NaN}, {"y": -Infinity}]')
    out = tmp_path / "o.json"
    r = run("repair", src, out, "--nan", "string")
    assert r.code == 0 and out.read_bytes() == b'[{"x": "NaN"}, {"y": "-Infinity"}]'
    stream = write(tmp_path / "s.json", b'{"a": 1}\n{"a": 2}\n')
    r = run("repair", stream, tmp_path / "s.jsonl", "--jsonl", "--format", "stream")
    assert r.code == 0 and (tmp_path / "s.jsonl").read_bytes().splitlines() == [b'{"a": 1}', b'{"a": 2}']
    r = run("repair", src, tmp_path / "x.jsonl", "--jsonl")
    assert r.code == 1 and "JSON Lines" in r.err
    lines = write(tmp_path / "l.jsonl", b'{"a": 1}\n{"a": 2,}\n{"a": \n{"a": 4}\n')
    r = run("repair", lines, tmp_path / "l2.jsonl")
    assert r.code == 0 and (tmp_path / "l2.jsonl").read_bytes() == b'{"a": 1}\n{"a": 2}\n{"a": 4}\n'
    wrapped = write(tmp_path / "w.json", b'{"meta": 1, "rows": [{"a": 1,}, {"a": 2}]}')
    r = run("repair", wrapped, tmp_path / "w2.json", "--record-path", "rows", "--json")
    assert r.code == 0 and json.loads(r.out)["record_path"] == ["rows"]


def test_repair_gzip_and_utf16_sources(run, tmp_path: Path) -> None:
    gz = write(tmp_path / "b.json.gz", gzip.compress(BROKEN))
    out = tmp_path / "f.json.gz"
    r = run("repair", gz, out)
    assert r.code == 0, r.err
    assert gzip.decompress(out.read_bytes()) == FIXED
    u16 = write(tmp_path / "u.json", "﻿".encode("utf-16-le") + BROKEN.decode().encode("utf-16-le"))
    out2 = tmp_path / "u_fixed.json"
    r = run("repair", u16, out2)
    assert r.code == 0 and out2.read_bytes() == FIXED and "UTF-8" in r.err


# ----------------------------------------------------------------------------------
# user errors: never in place, outputs checked before the run
# ----------------------------------------------------------------------------------
def test_repair_never_writes_over_the_input(run, broken: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # another spelling of the same name only where the file system folds case (NTFS, APFS)
    upper = [str(broken).upper(), "./x/../BROKEN.JSON"] if case_folds(tmp_path) else []
    for target in (broken, "broken.json", "./x/../broken.json", *upper):
        r = run("repair", broken, target)
        assert r.code == 1 and "manba faylning o'ziga" in r.err, target
    assert broken.read_bytes() == BROKEN


def test_repair_report_never_overwrites_the_input(run, broken: Path, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import repair as repair_mod

    calls: list[Any] = []
    monkeypatch.setattr(repair_mod, "repair_file", lambda *a, **k: calls.append(a))
    monkeypatch.chdir(tmp_path)
    folds = case_folds(tmp_path)
    # another spelling of the same name only where the file system folds case (NTFS, APFS)
    upper = [str(broken).upper(), "./sub/../Broken.Json"] if folds else []
    for target in (broken, "broken.json", "./sub/../broken.json", *upper):
        r = run("repair", broken, "fixed.json", "--report", target)
        assert r.code == 1 and "manba faylning o'ziga" in r.err, target
        r = run("repair", broken, "--check", "--report", target)
        assert r.code == 1, target
    r = run("repair", broken, "fixed.json", "--report", "FIXED.JSON" if folds else "./fixed.json")
    assert r.code == 1 and "Hisobot fayli ta'mirlangan faylning o'zi" in r.err
    r = run("repair", broken, "fixed.json", "--report", "fixes.txt")
    assert r.code == 1 and ".csv, .json, .jsonl" in r.err
    assert calls == [] and broken.read_bytes() == BROKEN                  # refused before the repair ran


def test_repair_user_errors(run, broken: Path, tmp_path: Path) -> None:
    r = run("repair", broken)
    assert r.code == 1 and "--check" in r.err
    r = run("repair", broken, "a.json", "-o", "b.json")
    assert r.code == 1 and "ikki marta berilgan" in r.err
    r = run("repair", tmp_path / "missing.json", tmp_path / "o.json")
    assert r.code == 1 and "topilmadi" in r.err
    r = run("repair", broken, tmp_path / "o.json", "--nan", "zero")
    assert r.code == 1 and "noto'g'ri tanlov" in r.err
    empty = write(tmp_path / "empty.json", b"")
    r = run("repair", empty, tmp_path / "o.json")
    assert r.code == 2


def test_repair_cancelled_and_internal_errors(run, broken: Path, tmp_path: Path, monkeypatch) -> None:
    token = CancelToken()
    token.cancel()
    out = tmp_path / "o.json"
    r = run("repair", broken, out, cancel=token)
    assert r.code == 3 and not out.exists()
    from jsonreshape import repair as repair_mod

    def failing(*args: Any, **kwargs: Any):
        raise JsonReshapeError("repair.error.invalid_output", detail="test")

    monkeypatch.setattr(repair_mod, "repair_file", failing)
    r = run("repair", broken, out)
    assert r.code == 4 and "tekshiruvdan o'tmadi" in r.err


def test_repair_help(run) -> None:
    r = run("repair", "--help")
    assert r.code == 0 and "ta'mirlash parametrlari:" in r.out and "--no-cache" not in r.out
    r = run("--lang", "en", "repair", "--help")
    assert "repair options:" in r.out and "Exit codes:" in r.out


@pytest.mark.slow
def test_repair_with_every_core_backend(broken: Path, tmp_path: Path) -> None:
    from jsonreshape import core

    results = []
    for backend in core.available_backends():
        out = tmp_path / f"f_{backend}.json"
        p = proc("--backend", backend, "repair", broken, out, "--json")
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        data = json.loads(p.stdout.decode("utf-8"))
        results.append((out.read_bytes(), data["counts"], [f["offset"] for f in data["fixes"]]))
    assert all(x == results[0] for x in results) and results[0][0] == FIXED
