"""``jsonreshape merge INPUT... -o OUTPUT [--dedupe-by KEY [--keep first|last]] [--memory-limit SIZE]
[--to json|jsonl | --jsonl] [--compact | --indent N] [--sort-keys] [--no-wrapper] [input options]``
(docs/ARCHITECTURE.md §14.0, §14.4, §14.8; AGENT_PROMPT §10 "birlashtirish", §13).

Records in input order with exact numbers and Uzbek text, JSON / JSON Lines output (extension, ``--to``,
``--jsonl``; other formats refused with the advice), layouts, patterns expanded by the program itself in natural
order (``qism_2`` before ``qism_10``; a literal name with ``[`` is not a pattern; no match: exit 1; OUTPUT skipped
with a warning), ``--dedupe-by`` (first / last, ``$``, ``$key``) with the counts, keyed inputs -> one object,
the first input's wrapper (``--no-wrapper``), OUTPUT == an INPUT refused in any spelling, partial inputs
(exit 2 naming the file, ``--allow-partial``), gzip / UTF-16 inputs, every core backend.
"""

from __future__ import annotations

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


A = [{"id": 1, "name": "Toshkent", "v": N("0.1")}, {"id": 2, "name": "Тошкент", "v": N("1e400")}]
B = [{"id": 2, "name": "O'zbekiston", "v": N("12345678901234567890")}, {"id": 3, "name": "Oʻzbekiston",
                                                                         "v": N("-0.0")}]
C = [{"id": 4, "name": "G'ulom", "v": N("1.10")}]


def jsonl(p: Path) -> list[Any]:
    return [codec.loads(line) for line in p.read_bytes().splitlines() if line.strip()]


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    a, b, c = tmp_path / "a.json", tmp_path / "b.jsonl", tmp_path / "c.json"
    jsongen.write_file(a, A, "array", style="pretty2")
    jsongen.write_file(b, B, "jsonl")
    jsongen.write_file(c, C, "array")
    return a, b, c


# ----------------------------------------------------------------------------------
# records, formats, layouts
# ----------------------------------------------------------------------------------
def test_merge_to_json_and_jsonl(run, inputs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    a, b, c = inputs
    out = tmp_path / "all.json"
    r = run("merge", a, b, c, "-o", out)
    assert r.code == 0, r.err
    data = out.read_bytes()
    assert codec.loads(data) == A + B + C
    for token in (b"0.1", b"1e400", b"12345678901234567890", b"-0.0", b"1.10", "Тошкент".encode()):
        assert token in data, token
    assert "3 ta fayl -> " in r.err and "5 ta yozuv" in r.err and f"  {b}: 2 / 2 ta yozuv" in r.err
    out2 = tmp_path / "all.jsonl"
    r = run("merge", a, b, c, "--output", out2)
    assert r.code == 0 and jsonl(out2) == A + B + C and out2.read_bytes().count(b"\n") == 5
    out3 = tmp_path / "all.txt"
    r = run("merge", a, b, "--out", out3, "--jsonl")
    assert r.code == 0 and jsonl(out3) == A + B
    r = run("merge", a, b, "-o", tmp_path / "x.dat", "--to", "json", "--compact", "--sort-keys")
    assert r.code == 0 and (tmp_path / "x.dat").read_bytes().startswith(b'[{"id":1,"name":"Toshkent","v":0.1}')
    r = run("merge", a, b, "-o", tmp_path / "i.json", "--indent", "4")
    assert r.code == 0 and (tmp_path / "i.json").read_bytes().startswith(b'[\n    {\n        "id": 1')


def test_merge_refuses_other_formats_and_bad_options(run, inputs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    a, b, _c = inputs
    r = run("merge", a, b, "-o", tmp_path / "all.csv")
    assert r.code == 1 and "faqat JSON yoki JSON Lines" in r.err and "jsonreshape convert" in r.err
    r = run("merge", a, b, "-o", tmp_path / "all.unknown")
    assert r.code == 1 and "Natija formatini fayl nomidan aniqlab bo'lmadi" in r.err
    r = run("merge", a, b)
    assert r.code == 1 and "majburiy" in r.err
    r = run("merge", a, b, "-o", tmp_path / "x.json", "--dedupe-by", "tags[*]")
    assert r.code == 1 and not (tmp_path / "x.json").exists()
    r = run("merge", a, b, "-o", tmp_path / "x.json", "--keep", "last")
    assert r.code == 0 and "--keep faqat --dedupe-by bilan ishlaydi" in r.err
    r = run("merge", a, tmp_path / "missing.json", "-o", tmp_path / "y.json")
    assert r.code == 1 and "topilmadi" in r.err and not (tmp_path / "y.json").exists()


# ----------------------------------------------------------------------------------
# patterns
# ----------------------------------------------------------------------------------
def test_merge_expands_patterns_in_natural_order(run, tmp_path: Path, monkeypatch) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    for k in (1, 2, 10, 11):
        jsongen.write_file(parts / f"qism_{k}.json", [{"k": k}], "array")
    (parts / "notes.txt").write_text("x")
    monkeypatch.chdir(tmp_path)
    r = run("merge", "parts/qism_*.json", "-o", "all.json")
    assert r.code == 0, r.err
    assert [x["k"] for x in codec.loads((tmp_path / "all.json").read_bytes())] == [1, 2, 10, 11]
    # the output itself matches the pattern: skipped with a warning
    r = run("merge", "parts/qism_*.json", "-o", "parts/qism_99.json")
    assert r.code == 0 and "qism_99.json" in r.err
    r = run("merge", "parts/qism_*.json", "-o", "parts/qism_99.json")      # now it exists and matches
    assert r.code == 0 and "kirish fayli sifatida olinmadi" in r.err
    assert [x["k"] for x in codec.loads((parts / "qism_99.json").read_bytes())] == [1, 2, 10, 11]
    r = run("merge", "parts/qism_?.json", "parts/nothing_*.json", "-o", "x.json")
    assert r.code == 1 and "Andozaga mos fayl topilmadi: parts/nothing_*.json" in r.err


def test_a_literal_name_with_brackets_is_not_a_pattern(run, tmp_path: Path) -> None:
    odd = tmp_path / "data[1].json"
    jsongen.write_file(odd, [{"id": 1}], "array")
    jsongen.write_file(tmp_path / "data1.json", [{"id": "pattern"}], "array")
    r = run("merge", odd, "-o", tmp_path / "out.json")
    assert r.code == 0 and codec.loads((tmp_path / "out.json").read_bytes()) == [{"id": 1}]


# ----------------------------------------------------------------------------------
# dedupe, keyed, wrappers
# ----------------------------------------------------------------------------------
def test_merge_dedupe(run, inputs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    a, b, c = inputs
    out = tmp_path / "u.json"
    r = run("merge", a, b, c, "-o", out, "--dedupe-by", "id")
    assert r.code == 0, r.err
    names = [x["name"] for x in codec.loads(out.read_bytes())]
    assert names == ["Toshkent", "Тошкент", "Oʻzbekiston", "G'ulom"]
    assert "1 ta takroriy yozuv olib tashlandi (1 ta guruh)" in r.err and f"  {b}: 1 / 2 ta yozuv" in r.err
    r = run("merge", a, b, c, "-o", out, "--dedupe-by", "id", "--keep", "last")
    assert [x["name"] for x in codec.loads(out.read_bytes())] == ["Toshkent", "O'zbekiston", "Oʻzbekiston", "G'ulom"]
    same = tmp_path / "same.jsonl"
    same.write_bytes(b'{"a":1,"b":[1,2]}\n{"b":[1,2],"a":1.0}\n{"a":2}\n')
    r = run("--lang", "en", "merge", same, same, "-o", tmp_path / "w.jsonl", "--dedupe-by", "$")
    assert r.code == 0 and jsonl(tmp_path / "w.jsonl") == [{"a": 1, "b": [1, 2]}, {"a": 2}]
    assert "4 duplicate records dropped (2 groups)" in r.err


def test_merge_memory_limit_reaches_the_dedupe(run, inputs: tuple[Path, Path, Path], tmp_path: Path,
                                               monkeypatch) -> None:
    from jsonreshape import filetools

    a, b, _c = inputs
    seen: list[Any] = []
    original = filetools.merge

    def spy(docs: Any, out: Any, opts: Any, **kwargs: Any):
        seen.append(opts)
        return original(docs, out, opts, **kwargs)

    monkeypatch.setattr(filetools, "merge", spy)
    r = run("merge", a, b, "-o", tmp_path / "m.json", "--dedupe-by", "id", "--memory-limit", "24MB")
    assert r.code == 0, r.err
    assert seen[-1].memory_limit == 24 << 20 and seen[-1].dedupe_by == "id" and seen[-1].keep == "first"
    r = run("merge", a, b, "-o", tmp_path / "m.json")
    assert seen[-1].memory_limit is None and seen[-1].dedupe_by is None


def test_merge_keyed_and_wrapped_inputs(run, tmp_path: Path) -> None:
    k1, k2 = tmp_path / "k1.json", tmp_path / "k2.json"
    jsongen.write_file(k1, [{"v": 1}, {"v": 2}], "keyed", keys=["a", "b"])
    jsongen.write_file(k2, [{"v": 3}, {"v": 4}], "keyed", keys=["b", "Тошкент"])
    out = tmp_path / "k.json"
    r = run("merge", k1, k2, "-o", out, "--dedupe-by", "$key")
    assert r.code == 0, r.err
    assert codec.loads(out.read_bytes()) == {"a": {"v": 1}, "b": {"v": 2}, "Тошкент": {"v": 4}}
    w1, w2 = tmp_path / "w1.json", tmp_path / "w2.json"
    jsongen.write_file(w1, [{"id": 1}], "wrapped")
    jsongen.write_file(w2, [{"id": 2}], "wrapped")
    out = tmp_path / "w.json"
    r = run("merge", w1, w2, "-o", out, "--record-path", "data")      # the input options apply to every input
    assert r.code == 0, r.err
    value = codec.loads(out.read_bytes())
    assert list(value) == ["meta", "data", "tail"] and value["data"] == [{"id": 1}, {"id": 2}]
    r = run("merge", w1, w2, "-o", out, "--record-path", "data", "--no-wrapper")
    assert r.code == 0 and codec.loads(out.read_bytes()) == [{"id": 1}, {"id": 2}]


def test_merge_names_the_input_of_each_warning(run, tmp_path: Path) -> None:
    w1, w2 = tmp_path / "w1.json", tmp_path / "w2.json"
    jsongen.write_file(w1, [{"id": i} for i in range(5)], "wrapped")
    jsongen.write_file(w2, [{"id": i} for i in range(5, 9)], "wrapped")
    out = tmp_path / "w.json"
    r = run("merge", w1, w2, "-o", out)
    assert r.code == 0, r.err
    assert f"Ogohlantirish: {w1}: Yozuvlar yo'li avtomatik tanlandi: data" in r.err
    assert f"Ogohlantirish: {w2}: Yozuvlar yo'li avtomatik tanlandi: data" in r.err
    assert codec.loads(out.read_bytes())["data"] == [{"id": i} for i in range(9)]


# ----------------------------------------------------------------------------------
# outputs, inputs, errors
# ----------------------------------------------------------------------------------
def test_merge_never_overwrites_an_input(run, inputs: tuple[Path, Path, Path], tmp_path: Path, monkeypatch) -> None:
    a, b, _c = inputs
    before = a.read_bytes()
    monkeypatch.chdir(tmp_path)
    for target in (a, str(a).upper(), "a.json", "./x/../A.JSON"):
        r = run("merge", a, b, "-o", target)
        assert r.code == 1 and "manba faylning o'ziga" in r.err, target
    assert a.read_bytes() == before


def test_merge_partial_input(run, inputs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    a, _b, _c = inputs
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"id": 9}, {"id": ')
    out = tmp_path / "o.json"
    r = run("merge", a, bad, "-o", out)
    assert r.code == 2 and f"Fayl: {bad}" in r.err and not out.exists()
    r = run("merge", a, bad, "-o", out, "--allow-partial")
    assert r.code == 0 and codec.loads(out.read_bytes()) == A + [{"id": 9}]
    assert f"{bad}: Ogohlantirish" not in r.err and f"Ogohlantirish: {bad}: Faylda JSON xatosi bor" in r.err


def test_merge_gzip_and_utf16_inputs(run, tmp_path: Path) -> None:
    g, u = tmp_path / "g.json.gz", tmp_path / "u.json"
    jsongen.write_file(g, A, "array", gz=True)
    jsongen.write_file(u, B, "array", encoding="utf-16-le", bom=True)
    out = tmp_path / "o.jsonl.gz"
    r = run("merge", g, u, "-o", out)
    assert r.code == 0, r.err
    import gzip

    assert [codec.loads(line) for line in gzip.decompress(out.read_bytes()).splitlines()] == A + B


def test_merge_help(run) -> None:
    r = run("merge", "--help")
    assert r.code == 0 and "birlashtirish parametrlari:" in r.out and "--dedupe-by" in r.out
    r = run("--lang", "en", "merge", "--help")
    assert "merge options:" in r.out and "Examples:" in r.out


@pytest.mark.slow
def test_merge_with_every_core_backend(inputs: tuple[Path, Path, Path], tmp_path: Path) -> None:
    from jsonreshape import core

    a, b, c = inputs
    results = []
    for backend in core.available_backends():
        out = tmp_path / f"m_{backend}.json"
        p = proc("--backend", backend, "merge", a, b, c, "-o", out, "--dedupe-by", "id")
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        results.append(out.read_bytes())
    assert all(x == results[0] for x in results)
