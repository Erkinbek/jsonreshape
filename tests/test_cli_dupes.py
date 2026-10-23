"""``jsonreshape dupes INPUT --key KEY [--keep first|last|none] [--top N] [--examples N] [--memory-limit SIZE]
[-o FILE [output options]] [--json] [record scope]`` (docs/ARCHITECTURE.md §14.2, §14.8; AGENT_PROMPT §9).

Counts, example groups (0-based record numbers), the largest groups, the distinct-value count for every
``--keep``; canonical equality of the key (``1 == 1.0 == 1e0``, ``1e400 != 2e400``, ``1 != "1"``, ``-0 == 0``),
``$`` (whole record, member order ignored) and ``$key``; ``-o`` exports exactly the records marked as duplicates
(JSON / JSON Lines / CSV, output options, OUTPUT == INPUT refused, output options without ``-o`` warned); the
``--json`` report; the record scope; spilled key runs (tiny ``--memory-limit``) giving the same answer; gzip /
UTF-16 inputs; bad keys refused before INPUT is read; every core backend (subprocesses).
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


# ids: 1, 1.0 and 1e0 are one value; 1e400 and 2e400 differ; "1" is a string; -0 == 0
RECS = [
    {"id": 1, "name": "Ali"},                       # 0
    {"id": N("1.0"), "name": "Vali"},               # 1  dup of 0
    {"id": "1", "name": "G'ani"},                   # 2  a string: other value
    {"id": N("1e400"), "name": "Тошкент"},          # 3
    {"id": N("2e400"), "name": "O'zbekiston"},      # 4
    {"id": N("1e0"), "name": "Oʻzbekiston"},        # 5  dup of 0
    {"name": "kalitsiz"},                           # 6  no key
    {"id": N("-0"), "name": "nol"},                 # 7
    {"id": 0, "name": "nol2"},                      # 8  dup of 7
]


@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "people.json"
    jsongen.write_file(p, RECS, "array", style="pretty2")
    return p


def report(r: Any) -> dict[str, Any]:
    assert r.code == 0, r.err
    return json.loads(r.out)


# ----------------------------------------------------------------------------------
# counts, groups, keep
# ----------------------------------------------------------------------------------
def test_dupes_text_in_uzbek(run, src: Path) -> None:
    r = run("dupes", src, "--key", "id")
    assert r.code == 0, r.err
    lines = r.out.splitlines()
    assert lines[0] == "Kalit: id"
    assert lines[1] == "Guruhlar: 2; takroriy yozuvlar: 3 (har guruhning birinchisidan tashqari)"
    assert lines[2] == "Kaliti bor: 8; kalitsiz: 1; o'qib bo'lmagan: 0; noyob qiymatlar: 5"
    assert lines[3] == "Misollar (dastlabki 2 ta guruh):"
    assert lines[4] == "  1  (3 ta yozuv): #0, #1, #5"
    assert lines[5] == "  -0  (2 ta yozuv): #7, #8"
    assert "9 ta yozuv tekshirildi" in r.err


def test_dupes_text_in_english_with_top_and_examples(run, src: Path) -> None:
    r = run("--lang", "en", "dupes", src, "--key", "id", "--examples", "1", "--top", "2", "--keep", "last")
    assert r.code == 0, r.err
    lines = r.out.splitlines()
    assert lines[1] == "Groups: 2; duplicate records: 3 (all but the last of each group)"
    assert lines[3] == "Examples (the first 1 group):" and lines[4] == "  1  (3 records): #0, #1, #5"
    assert lines[5] == "The 2 largest groups:"
    assert lines[6] == "    1. 1  (3 records): #0, #1, #5" and lines[7] == "    2. -0  (2 records): #7, #8"
    r = run("dupes", src, "--key", "id", "--examples", "0")
    assert "Misollar" not in r.out and r.code == 0


@pytest.mark.parametrize("keep, duplicates, distinct", [("first", 3, 5), ("last", 3, 5), ("none", 5, 5)])
def test_dupes_json_for_every_keep(run, src: Path, keep: str, duplicates: int, distinct: int) -> None:
    data = report(run("dupes", src, "--key", "id", "--keep", keep, "--json"))
    assert data["keep"] == keep and data["groups"] == 2 and data["duplicates"] == duplicates
    assert data["distinct"] == distinct and data["records_with_key"] == 8 and data["missing"] == 1
    assert data["scanned"] == 9 and data["output"] is None
    assert data["examples"] == [{"key": "1", "size": 3, "positions": [0, 1, 5]},
                                {"key": "-0", "size": 2, "positions": [7, 8]}]


def test_dupes_without_duplicates(run, src: Path) -> None:
    r = run("dupes", src, "--key", "name")
    assert r.code == 0 and "Takroriy qiymat yo'q." in r.out and "Guruhlar: 0" in r.out


def test_dupes_whole_record_and_member_key(run, tmp_path: Path) -> None:
    p = tmp_path / "rec.jsonl"
    # member order does not matter for "$", numbers by value
    p.write_bytes(b'{"a":1,"b":"x"}\n{"b":"x","a":1.0}\n{"a":2}\n')
    data = report(run("dupes", p, "--key", "$", "--json"))
    assert data["groups"] == 1 and data["examples"][0]["positions"] == [0, 1]
    keyed = tmp_path / "keyed.json"
    jsongen.write_file(keyed, [{"v": 1}, {"v": 1}], "keyed", keys=["k1", "k2"])
    data = report(run("dupes", keyed, "--key", "$key", "--json"))
    assert data["groups"] == 0 and data["records_with_key"] == 2
    data = report(run("dupes", keyed, "--key", "v", "--json"))
    assert data["groups"] == 1 and data["duplicates"] == 1
    r = run("dupes", p, "--key", "$key")
    assert r.code == 0 and "Ogohlantirish" in r.err and "kalitsiz: 3" in r.out


def test_dupes_scope(run, src: Path) -> None:
    data = report(run("dupes", src, "--key", "id", "--select", "0:3", "--json"))
    assert data["scope"]["records"] == 3 and data["groups"] == 1 and data["examples"][0]["positions"] == [0, 1]
    data = report(run("dupes", src, "--key", "id", "--where", "name != 'Vali'", "--json"))
    assert data["examples"][0]["positions"] == [0, 5]


def test_dupes_spilled_runs_give_the_same_answer(run, tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "many.jsonl"
    jsongen.write_file(p, [{"id": i % 1000, "n": i} for i in range(5000)], "jsonl")
    plain = report(run("dupes", p, "--key", "id", "--json", "--top", "3"))
    monkeypatch.setattr(cli, "MIN_MEMORY_LIMIT", 1)
    spilled = report(run("dupes", p, "--key", "id", "--json", "--top", "3", "--memory-limit", "1KB"))
    for d in (plain, spilled):
        d.pop("elapsed")
    assert spilled == plain and plain["groups"] == 1000 and plain["duplicates"] == 4000


# ----------------------------------------------------------------------------------
# -o: the duplicates written to a file
# ----------------------------------------------------------------------------------
def test_dupes_output_file(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "dups.jsonl"
    r = run("dupes", src, "--key", "id", "-o", out)
    assert r.code == 0, r.err
    rows = [codec.loads(line) for line in out.read_bytes().splitlines()]
    assert [x["name"] for x in rows] == ["Vali", "Oʻzbekiston", "nol2"]
    assert b"1.0" in out.read_bytes() and b"1e0" in out.read_bytes()      # exact tokens
    assert "3 ta takroriy yozuv -> " in r.err
    out2 = tmp_path / "all.json"
    data = report(run("dupes", src, "--key", "id", "--keep", "none", "--output", out2, "--compact", "--json"))
    assert data["output"]["records"] == 5 and data["output"]["file"] == str(out2)
    assert out2.read_bytes().startswith(b'[{"id":1,')
    assert [x["name"] for x in codec.loads(out2.read_bytes())] == ["Ali", "Vali", "Oʻzbekiston", "nol", "nol2"]
    out3 = tmp_path / "dups.csv"
    r = run("dupes", src, "--key", "id", "--out", out3, "--columns", "id,name", "--no-bom")
    assert r.code == 0, r.err
    rows = list(csv.reader(io.StringIO(out3.read_text(encoding="utf-8"))))
    assert rows == [["id", "name"], ["1.0", "Vali"], ["1e0", "Oʻzbekiston"], ["0", "nol2"]]


def test_dupes_output_checks(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    before = src.read_bytes()
    monkeypatch.chdir(tmp_path)
    # another spelling of the same name only where the file system folds case (NTFS, APFS)
    upper = [str(src).upper(), "./sub/../PEOPLE.JSON"] if case_folds(tmp_path) else []
    for target in (src, "people.json", "./sub/../people.json", *upper):
        r = run("dupes", src, "--key", "id", "-o", target)
        assert r.code == 1 and "manba faylning o'ziga" in r.err, target
    assert src.read_bytes() == before
    r = run("dupes", src, "--key", "id", "--compact", "--to", "csv")
    assert r.code == 0 and "--compact, --to: faqat -o FAYL bilan ishlatiladi" in r.err
    r = run("dupes", src, "--key", "id", "-o", tmp_path / "x.json", "--columns", "id")
    assert r.code == 0 and "--columns: json formatida ishlatilmaydi" in r.err


# ----------------------------------------------------------------------------------
# inputs and errors
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["gzip", "utf16"])
def test_dupes_of_gzip_and_utf16_inputs(run, tmp_path: Path, src: Path, variant: str) -> None:
    base = report(run("dupes", src, "--key", "id", "--json"))
    p = tmp_path / ("p.json.gz" if variant == "gzip" else "p16.json")
    if variant == "gzip":
        jsongen.write_file(p, RECS, "array", gz=True)
    else:
        jsongen.write_file(p, RECS, "array", encoding="utf-16-le", bom=True)
    data = report(run("dupes", p, "--key", "id", "--json"))
    for d in (base, data):
        d.pop("elapsed")
        d.pop("file")
    assert data == base


def test_dupes_errors(run, src: Path, monkeypatch) -> None:
    from jsonreshape import reader

    opened: list[Any] = []
    original = reader.Document.open

    def spy(*args: Any, **kwargs: Any):
        opened.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(reader.Document, "open", spy)
    r = run("dupes", src, "--key", "orders[*]")
    assert r.code == 1 and "'*', '[*]' yoki '..'" in r.err
    r = run("dupes", src, "--key", "a[")
    assert r.code == 1 and "^" in r.err
    assert opened == []                                   # refused before INPUT was read
    r = run("dupes", src)
    assert r.code == 1 and "quyidagi argumentlar majburiy: --key" in r.err
    r = run("dupes", src, "--key", "id", "--keep", "all")
    assert r.code == 1 and "noto'g'ri tanlov" in r.err
    r = run("dupes", src, "--key", "id", "--top", "99999")
    assert r.code == 1


def test_dupes_json_error_exit_2(run, tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_bytes(b'[{"id": 1}, {"id": 1}, {"id": ')
    r = run("dupes", p, "--key", "id")
    assert r.code == 2 and r.out == ""
    data = report(run("dupes", p, "--key", "id", "--allow-partial", "--json"))
    assert data["groups"] == 1


@pytest.mark.slow
def test_dupes_with_every_core_backend(src: Path) -> None:
    from jsonreshape import core

    outputs = []
    for backend in core.available_backends():
        p = proc("--backend", backend, "dupes", src, "--key", "id", "--json", "--top", "5")
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        data = json.loads(p.stdout.decode("utf-8"))
        data.pop("elapsed")
        outputs.append(data)
    assert all(o == outputs[0] for o in outputs)
