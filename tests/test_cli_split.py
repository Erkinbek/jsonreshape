"""``jsonreshape split INPUT (--parts N | --records N | --size SIZE) [--balance bytes|records] [-o DIR]
[--name PATTERN] [--force] [--drop-broken] [--no-wrapper] [output options] [record scope]``
(docs/ARCHITECTURE.md §14.0, §14.4, §14.8; AGENT_PROMPT §10 "Bo'lish", §13).

Every mode and the part names (``<stem>_<k><ext>``, zero-padded), parts that are byte copies of the source
(JSON Lines parts laid end to end ARE the file; arrays keep their head/tail, wrapped documents their wrapper,
``--no-wrapper`` drops it), exact numbers and Uzbek text untouched, writer parts (``--to``, ``--compact``, a
filter, a ``--select`` list), existing parts refused without ``--force`` (nothing written) and replaced with it,
stale parts named, the name pattern, the output folder, gzip / UTF-16 sources, JSON errors (exit 2 with the
repair advice; no ``--allow-partial``), JSON Lines broken lines (``--drop-broken``), bad options (exit 1),
the parts listed on stdout, every core backend.
"""

from __future__ import annotations

import gzip
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

import jsongen
from jsonreshape import cli, codec, i18n
from jsonreshape.tasks import CancelToken

ROOT = Path(__file__).resolve().parents[1]
N = codec.loads
MiB = 1 << 20


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


EXACT = [N("0.1"), N("1e400"), N("12345678901234567890"), N("-0.0"), N("1.10")]


def records(n: int) -> list[dict[str, Any]]:
    names = ["Toshkent", "Тошкент", "O'zbekiston", "Oʻzbekiston", "G'ulom"]
    return [{"id": i, "name": names[i % 5], "v": EXACT[i % 5]} for i in range(n)]


def listing(p: Path) -> list[str]:
    return sorted(x.name for x in p.iterdir())


def values(p: Path) -> list[Any]:
    data = p.read_bytes()
    if p.suffix == ".gz":
        data = gzip.decompress(data)
    if ".jsonl" in p.name:
        return [codec.loads(line) for line in data.splitlines() if line.strip()]
    return codec.loads(data)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    d = tmp_path / "parts"
    d.mkdir()
    return d


# ----------------------------------------------------------------------------------
# modes, names, byte copies
# ----------------------------------------------------------------------------------
def test_split_parts_of_an_array(run, tmp_path: Path, folder: Path) -> None:
    src = tmp_path / "people.json"
    jsongen.write_file(src, records(30), "array", style="pretty2")
    r = run("split", src, "--parts", "3", "-o", folder)
    assert r.code == 0, r.err
    names = [f"people_{k}.json" for k in (1, 2, 3)]
    assert listing(folder) == names
    assert r.out.splitlines() == [str(folder / n) for n in names]
    assert "30 ta yozuv -> 3 ta qism" in r.err
    got = [rec for n in names for rec in values(folder / n)]
    assert got == records(30)
    head = src.read_bytes()[:3]
    joined = b""
    for n in names:   # each part keeps the document's own layout (a byte copy of the records)
        data = (folder / n).read_bytes()
        assert data.startswith(head)
        joined += data
    for token in (b"0.1", b"1e400", b"12345678901234567890", b"-0.0", b"1.10", "Тошкент".encode()):
        assert token in joined, token


def test_split_records_and_padding(run, tmp_path: Path, folder: Path) -> None:
    src = tmp_path / "lines.jsonl"
    raw = jsongen.write_file(src, records(10), "jsonl")
    r = run("split", src, "--records", "1", "-o", folder)
    assert r.code == 0, r.err
    assert listing(folder) == [f"lines_{k:02d}.jsonl" for k in range(1, 11)]
    joined = b"".join((folder / f"lines_{k:02d}.jsonl").read_bytes() for k in range(1, 11))
    assert joined == raw == src.read_bytes()                     # JSON Lines parts laid end to end ARE the file


def test_split_by_size(run, tmp_path: Path, folder: Path) -> None:
    src = tmp_path / "big.jsonl"
    recs = [{"id": i, "pad": "x" * 200, "v": EXACT[i % 5]} for i in range(12000)]   # about 2.7 MB
    jsongen.write_file(src, recs, "jsonl")
    r = run("split", src, "--size", "1", "-o", folder)                           # 1 = 1 MB (binary)
    assert r.code == 0, r.err
    parts = sorted(folder.iterdir())
    assert len(parts) == 3 and all(p.stat().st_size <= MiB for p in parts)
    assert b"".join(p.read_bytes() for p in parts) == src.read_bytes()
    r = run("split", src, "--size", "500KB")
    assert r.code == 1 and "qism hajmi kamida 1 MB bo'lishi kerak" in r.err


def test_split_balance_by_bytes_or_records(run, tmp_path: Path, folder: Path) -> None:
    src = tmp_path / "skew.jsonl"
    recs = [{"id": 0, "pad": "x" * 5000}] + [{"id": i} for i in range(1, 10)]   # one big record first
    jsongen.write_file(src, recs, "jsonl")
    r = run("split", src, "--parts", "2", "-o", folder)
    assert r.code == 0, r.err
    by_bytes = [len(values(folder / n)) for n in listing(folder)]
    assert by_bytes == [1, 9]
    other = tmp_path / "rec"
    other.mkdir()
    r = run("split", src, "--parts", "2", "-o", other, "--balance", "records")
    assert r.code == 0 and "--balance" not in r.err
    assert [len(values(other / n)) for n in listing(other)] == [5, 5]


def test_split_keeps_or_drops_the_wrapper(run, tmp_path: Path, folder: Path) -> None:
    src = tmp_path / "w.json"
    jsongen.write_file(src, records(6), "wrapped", style="pretty2")
    r = run("split", src, "--parts", "2", "-o", folder)
    assert r.code == 0, r.err
    for k in (1, 2):
        part = values(folder / f"w_{k}.json")
        assert list(part) == ["meta", "data", "tail"] and len(part["data"]) == 3
    other = tmp_path / "bare"
    other.mkdir()
    r = run("split", src, "--parts", "2", "-o", other, "--no-wrapper")
    assert r.code == 0 and "e'tiborga olinmadi" not in r.err
    assert values(other / "w_1.json") == records(6)[:3]


def test_split_with_the_writer(run, tmp_path: Path, folder: Path) -> None:
    src = tmp_path / "people.json"
    jsongen.write_file(src, records(9), "array", style="pretty2")
    r = run("split", src, "--records", "5", "-o", folder, "--to", "jsonl")
    assert r.code == 0, r.err
    assert listing(folder) == ["people_1.jsonl", "people_2.jsonl"]
    assert values(folder / "people_1.jsonl") + values(folder / "people_2.jsonl") == records(9)
    assert b"1e400" in (folder / "people_1.jsonl").read_bytes()                    # exact tokens
    compact = tmp_path / "compact"
    compact.mkdir()
    r = run("split", src, "--parts", "2", "-o", compact, "--compact")
    assert r.code == 0 and (compact / "people_1.json").read_bytes().startswith(b'[{"id":0,')
    r = run("split", src, "--parts", "2", "-o", compact, "--columns", "id", "--force")
    assert r.code == 0 and "--columns: json formatida ishlatilmaydi" in r.err


def test_split_a_scope(run, tmp_path: Path, folder: Path) -> None:
    src = tmp_path / "people.json"
    jsongen.write_file(src, records(20), "array", style="pretty2")
    r = run("split", src, "--parts", "2", "-o", folder, "--where", "id >= 10")
    assert r.code == 0, r.err
    assert [x["id"] for n in ("people_1.json", "people_2.json") for x in values(folder / n)] == list(range(10, 20))
    sel = tmp_path / "sel"
    sel.mkdir()
    r = run("split", src, "--records", "2", "-o", sel, "--select", "5:8")    # a range: byte copies
    assert r.code == 0 and listing(sel) == ["people_1.json", "people_2.json"]
    assert [x["id"] for n in listing(sel) for x in values(sel / n)] == [5, 6, 7]
    lst = tmp_path / "list"
    lst.mkdir()
    r = run("split", src, "--records", "2", "-o", lst, "--select", "9,1,4")  # a list, in the given order
    assert r.code == 0 and [x["id"] for n in listing(lst) for x in values(lst / n)] == [9, 1, 4]


# ----------------------------------------------------------------------------------
# existing parts, names, folders
# ----------------------------------------------------------------------------------
def test_existing_parts_need_force(run, tmp_path: Path, folder: Path) -> None:
    src = tmp_path / "people.json"
    jsongen.write_file(src, records(6), "array")
    mine = folder / "people_2.json"
    mine.write_bytes(b"my own file")
    r = run("split", src, "--parts", "3", "-o", folder)
    assert r.code == 1 and "Qism fayllar allaqachon bor (1 ta): people_2.json" in r.err and "--force" in r.err
    assert listing(folder) == ["people_2.json"] and mine.read_bytes() == b"my own file"
    r = run("split", src, "--parts", "3", "-o", folder, "--force")
    assert r.code == 0 and listing(folder) == ["people_1.json", "people_2.json", "people_3.json"]
    assert mine.read_bytes() != b"my own file"
    assert [x for n in listing(folder) for x in values(folder / n)] == records(6)
    # an earlier split into more parts: its other files are left alone and named
    r = run("split", src, "--parts", "2", "-o", folder, "--force")
    assert r.code == 0 and "people_3.json" in r.err and (folder / "people_3.json").exists()


def test_name_pattern_and_folder_errors(run, tmp_path: Path, folder: Path, monkeypatch) -> None:
    src = tmp_path / "people.json"
    jsongen.write_file(src, records(4), "array")
    r = run("split", src, "--parts", "2", "-o", folder, "--name", "{stem}_qism{n}{ext}")
    assert r.code == 0 and listing(folder) == ["people_qism1.json", "people_qism2.json"]
    from jsonreshape import reader

    opened: list[Any] = []
    original = reader.Document.open
    monkeypatch.setattr(reader.Document, "open", lambda *a, **k: opened.append(a) or original(*a, **k))
    r = run("split", src, "--parts", "2", "-o", folder, "--name", "{stem}")
    assert r.code == 1 and "andozasi noto'g'ri" in r.err
    r = run("split", src, "--parts", "2", "-o", tmp_path / "missing")
    assert r.code == 1 and "Bunday papka yo'q" in r.err
    r = run("split", src, "--parts", "2", "-o", src)
    assert r.code == 1 and "-o papka bo'lishi kerak" in r.err
    assert opened == []                                      # refused before INPUT was read
    r = run("split", src)
    assert r.code == 1 and "--parts --records --size" in r.err
    r = run("split", src, "--parts", "2", "--records", "2")
    assert r.code == 1
    r = run("split", src, "--parts", "9", "-o", folder)
    assert r.code == 1 and "bor-yo'g'i 4 ta yozuv" in r.err


def test_split_next_to_the_input_and_the_balance_warning(run, tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "people.json"
    jsongen.write_file(src, records(4), "array")
    monkeypatch.chdir(tmp_path)
    r = run("split", "people.json", "--records", "2", "--balance", "records")
    assert r.code == 0 and "--balance faqat --parts bilan ishlaydi" in r.err
    assert r.out.splitlines() == ["people_1.json", "people_2.json"]
    assert listing(tmp_path) == ["people.json", "people_1.json", "people_2.json"]


# ----------------------------------------------------------------------------------
# sources
# ----------------------------------------------------------------------------------
def test_split_gzip_and_utf16_sources(run, tmp_path: Path, folder: Path) -> None:
    gz = tmp_path / "people.json.gz"
    jsongen.write_file(gz, records(6), "array", gz=True)
    r = run("split", gz, "--parts", "2", "-o", folder)
    assert r.code == 0, r.err
    assert listing(folder) == ["people_1.json.gz", "people_2.json.gz"]
    assert values(folder / "people_1.json.gz") + values(folder / "people_2.json.gz") == records(6)
    u16 = tmp_path / "u16.json"
    jsongen.write_file(u16, records(6), "array", encoding="utf-16-le", bom=True)
    other = tmp_path / "u16parts"
    other.mkdir()
    r = run("split", u16, "--parts", "2", "-o", other)
    assert r.code == 0, r.err
    part = (other / "u16_1.json").read_bytes()
    assert part.startswith(b"\xff\xfe") and codec.loads(part.decode("utf-16")) == records(6)[:3]


def test_split_json_errors_and_broken_lines(run, tmp_path: Path, folder: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"id": 1}, {"id": 2}, {"id": ')
    r = run("split", bad, "--parts", "2", "-o", folder)
    assert r.code == 2 and "Avval faylni ta'mirlang (jsonreshape repair)" in r.err
    r = run("split", bad, "--parts", "2", "-o", folder, "--allow-partial")
    assert r.code == 1 and "tanilmagan argumentlar: --allow-partial" in r.err
    lines = tmp_path / "lines.jsonl"
    lines.write_bytes(b'{"id": 1}\n{"id": \n{"id": 3}\n{"id": 4}\n')
    r = run("split", lines, "--parts", "2", "-o", folder)
    assert r.code == 1 and "--drop-broken" in r.err and listing(folder) == []
    r = run("split", lines, "--parts", "2", "-o", folder, "--drop-broken")
    assert r.code == 0, r.err
    assert "1 ta buzuq qator qismlarga yozilmadi" in r.err
    assert [x["id"] for n in listing(folder) for x in values(folder / n)] == [1, 3, 4]
    single = tmp_path / "single.json"
    single.write_bytes(b'{"a": 1}')
    r = run("split", single, "--parts", "2", "-o", folder, "--format", "single")
    assert r.code == 1


def test_split_help(run) -> None:
    r = run("split", "--help")
    assert r.code == 0 and "bo'lish parametrlari:" in r.out and "--force" in r.out and "--allow-partial" not in r.out
    r = run("--lang", "en", "split", "--help")
    assert "split options:" in r.out and "Examples:" in r.out


@pytest.mark.slow
def test_split_with_every_core_backend(tmp_path: Path) -> None:
    from jsonreshape import core

    src = tmp_path / "people.json"
    jsongen.write_file(src, records(12), "array", style="pretty2")
    contents = []
    for backend in core.available_backends():
        out = tmp_path / f"out_{backend}"
        out.mkdir()
        p = proc("--backend", backend, "split", src, "--parts", "3", "-o", out)
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        contents.append([(f.name, f.read_bytes()) for f in sorted(out.iterdir())])
    assert all(c == contents[0] for c in contents) and len(contents[0]) == 3
