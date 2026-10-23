"""``jsonreshape extract INPUT [OUTPUT | -o OUTPUT] [--path RECORD_PATH | --node LOCATION | --gather PATTERN]
[output options] [record scope]`` and ``convert --node`` (docs/ARCHITECTURE.md §14.4, §14.8; AGENT_PROMPT §10
"yo'ldagi massivni alohida faylga ajratish", "faqat tanlangan qismni eksport qilish ... bitta tugun").

The record array at a path without its wrapper (every format: JSON, JSON Lines, CSV with ``--columns``; same as
``--record-path``), one node by a location (``#2.contact``, ``/1/orders``, ``data[1]``; JSON re-indented,
JSON Lines one line per element), values gathered from every record (``orders[*]``; ``--sort-keys`` honoured,
layout options that do not apply named), exact numbers and Uzbek text byte for byte, the record scope with
``--gather``, refusals before INPUT is read (another format for a node / gathered values, a node with a
filter), OUTPUT checks, gzip / UTF-16 inputs, every core backend.
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


RECS = [
    {"id": 0, "name": "Toshkent", "contact": {"city": "Тошкент"}, "orders": [{"oid": 1, "sum": N("0.1")}]},
    {"id": 1, "name": "O'zbekiston", "orders": [{"sum": N("1e400"), "oid": 2}, {"oid": 3, "sum": N("-0.0")}]},
    {"id": 2, "name": "Oʻzbekiston", "contact": {"city": "Samarqand"}, "orders": []},
    {"id": 3, "name": "G'ulom", "orders": [{"oid": 4, "sum": N("12345678901234567890")},
                                           {"oid": 5, "sum": N("1.10")}]},
]
ORDERS = [o for r in RECS for o in r["orders"]]


def jsonl(p: Path) -> list[Any]:
    return [codec.loads(line) for line in p.read_bytes().splitlines() if line.strip()]


@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "people.json"
    jsongen.write_file(p, RECS, "array", style="pretty2")
    return p


@pytest.fixture
def wrapped(tmp_path: Path) -> Path:
    p = tmp_path / "big.json"
    jsongen.write_file(p, RECS, "wrapped", style="pretty2", wrapper_path=("data", "items"))
    return p


def no_open(monkeypatch) -> list[Any]:
    from jsonreshape import reader

    opened: list[Any] = []
    original = reader.Document.open
    monkeypatch.setattr(reader.Document, "open", lambda *a, **k: opened.append(a) or original(*a, **k))
    return opened


# ----------------------------------------------------------------------------------
# --path: the records of the container at a path
# ----------------------------------------------------------------------------------
def test_extract_the_array_at_a_path(run, wrapped: Path, tmp_path: Path) -> None:
    out = tmp_path / "items.json"
    r = run("extract", wrapped, out, "--path", "data.items")
    assert r.code == 0, r.err
    data = out.read_bytes()
    assert codec.loads(data) == RECS and data.startswith(b"[")
    for token in (b"0.1", b"1e400", b"-0.0", b"12345678901234567890", b"1.10", "Тошкент".encode()):
        assert token in data, token
    assert "4 / 4 yozuv -> " in r.err and "Ogohlantirish" not in r.err       # no "wrapper not written" note
    r = run("extract", wrapped, "-o", tmp_path / "items2.json", "--record-path", "data.items")
    assert r.code == 0 and codec.loads((tmp_path / "items2.json").read_bytes()) == RECS
    r = run("extract", wrapped, tmp_path / "x.json", "--path", "data.items", "--record-path", "meta.list")
    assert r.code == 1 and "har xil" in r.err


def test_extract_a_path_to_csv_and_jsonl(run, wrapped: Path, tmp_path: Path) -> None:
    out = tmp_path / "items.csv"
    r = run("extract", wrapped, out, "--path", "data.items", "--columns", "id,name", "--no-bom")
    assert r.code == 0, r.err
    rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert rows == [["id", "name"], ["0", "Toshkent"], ["1", "O'zbekiston"], ["2", "Oʻzbekiston"], ["3", "G'ulom"]]
    out2 = tmp_path / "items.jsonl"
    r = run("extract", wrapped, out2, "--path", "data.items", "--select", "1:3")
    assert r.code == 0 and jsonl(out2) == RECS[1:3] and "2 / 4 yozuv" in r.err


def test_extract_without_a_form_writes_the_records(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "all.jsonl"
    r = run("extract", src, out)
    assert r.code == 0 and jsonl(out) == RECS


# ----------------------------------------------------------------------------------
# --gather: values of a pattern from every record
# ----------------------------------------------------------------------------------
def test_extract_gather(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "orders.jsonl"
    r = run("extract", src, out, "--gather", "orders[*]")
    assert r.code == 0, r.err
    assert jsonl(out) == ORDERS and "5 ta qiymat -> " in r.err
    assert out.read_bytes().splitlines()[1] == b'{"sum":1e400,"oid":2}'               # byte copies, exact
    out2 = tmp_path / "orders.json"
    r = run("--lang", "en", "extract", src, out2, "--gather", "orders[*]", "--sort-keys", "--compact")
    assert r.code == 0 and out2.read_bytes().startswith(b'[{"oid":1,"sum":0.1},{"oid":2,"sum":1e400}')
    assert "5 values -> " in r.err
    r = run("extract", src, out, "--gather", "orders[*]", "--indent", "4", "--columns", "a")
    assert r.code == 0 and "--indent, --columns: --gather (jsonl) uchun ishlatilmaydi" in r.err
    r = run("extract", src, out, "--gather", "contact.city", "--select", "0,2")
    assert r.code == 0 and jsonl(out) == ["Тошкент", "Samarqand"]
    r = run("extract", src, out, "--gather", "orders[*]", "--where", "id >= 3")
    assert r.code == 0 and jsonl(out) == RECS[3]["orders"]


def test_extract_gather_refusals(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    opened = no_open(monkeypatch)
    r = run("extract", src, tmp_path / "o.csv", "--gather", "orders[*]")
    assert r.code == 1 and "faqat JSON yoki JSON Lines" in r.err
    r = run("extract", src, tmp_path / "o.json", "--gather", "orders[*]", "--node", "#1")
    assert r.code == 1 and "birga ishlatib bo'lmaydi" in r.err
    assert opened == []
    r = run("extract", src, tmp_path / "o.json", "--gather", "orders[")
    assert r.code == 1 and "^" in r.err


# ----------------------------------------------------------------------------------
# --node and convert --node
# ----------------------------------------------------------------------------------
def test_extract_a_node(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "contact.json"
    r = run("extract", src, out, "--node", "#0.contact")
    assert r.code == 0, r.err
    assert out.read_bytes() == b'{\n  "city": "\xd0\xa2\xd0\xbe\xd1\x88\xd0\xba\xd0\xb5\xd0\xbd\xd1\x82"\n}'
    assert "#0.contact -> " in r.err
    out2 = tmp_path / "orders.jsonl"
    r = run("extract", src, out2, "--node", "/1/orders")
    assert r.code == 0 and out2.read_bytes() == b'{"sum":1e400,"oid":2}\n{"oid":3,"sum":-0.0}\n'
    out3 = tmp_path / "rec.json"
    r = run("extract", src, out3, "--node", "[3]", "--compact", "--sort-keys")
    assert r.code == 0 and "--sort-keys: --node (json) uchun ishlatilmaydi" in r.err
    assert codec.loads(out3.read_bytes()) == RECS[3] and b"\n" not in out3.read_bytes()
    r = run("extract", src, out3, "--node", "#99")
    assert r.code == 1 and "#99" in r.err


def test_extract_a_node_of_json_lines(run, tmp_path: Path) -> None:
    p = tmp_path / "people.jsonl"
    jsongen.write_file(p, RECS, "jsonl")
    out = tmp_path / "o.json"
    r = run("extract", p, out, "--node", "#3.orders", "--indent", "4")
    assert r.code == 0, r.err
    assert codec.loads(out.read_bytes()) == RECS[3]["orders"] and out.read_bytes().startswith(b"[\n    {")
    r = run("extract", p, out, "--path", "data")
    assert r.code == 1 and "bitta JSON hujjat ichida" in r.err


def test_convert_node(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "o.jsonl"
    r = run("convert", src, out, "--node", "#1.orders")
    assert r.code == 0 and jsonl(out) == RECS[1]["orders"]
    opened = no_open(monkeypatch)
    r = run("convert", src, out, "--node", "#1.orders", "--where", "id > 1", "--sort-by", "id")
    assert r.code == 1 and "--node bitta tugunni yozadi: uni --where, --sort-by bilan birga ishlatib bo'lmaydi" in r.err
    r = run("convert", src, tmp_path / "o.xlsx", "--node", "#1")
    assert r.code == 1 and "Excel (XLSX) emas" in r.err
    r = run("extract", src, out, "--node", "#1", "--select", "1")
    assert r.code == 1
    assert opened == []


# ----------------------------------------------------------------------------------
# outputs and inputs
# ----------------------------------------------------------------------------------
def test_extract_output_checks(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    before = src.read_bytes()
    monkeypatch.chdir(tmp_path)
    # another spelling of the same name only where the file system folds case (NTFS, APFS)
    upper = [str(src).upper()] if case_folds(tmp_path) else []
    for target in (src, "people.json", *upper):
        r = run("extract", src, target, "--gather", "orders[*]")
        assert r.code == 1 and "manba faylning o'ziga" in r.err
    assert src.read_bytes() == before
    r = run("extract", src, "a.json", "-o", "b.json", "--gather", "orders[*]")
    assert r.code == 1 and "ikki marta berilgan" in r.err
    r = run("extract", src, "--gather", "orders[*]")
    assert r.code == 1 and "Natija fayli ko'rsatilmagan" in r.err


@pytest.mark.parametrize("variant", ["gzip", "utf16"])
def test_extract_gzip_and_utf16_inputs(run, tmp_path: Path, variant: str) -> None:
    p = tmp_path / ("p.json.gz" if variant == "gzip" else "p16.json")
    if variant == "gzip":
        jsongen.write_file(p, RECS, "array", gz=True)
    else:
        jsongen.write_file(p, RECS, "array", encoding="utf-16-le", bom=True)
    out = tmp_path / "o.jsonl"
    r = run("extract", p, out, "--gather", "orders[*]")
    assert r.code == 0 and jsonl(out) == ORDERS
    r = run("extract", p, out, "--node", "#0.contact")
    assert r.code == 0 and jsonl(out) == [{"city": "Тошкент"}]


def test_extract_help(run) -> None:
    r = run("extract", "--help")
    assert r.code == 0 and "--gather PATTERN" in r.out and "ajratish parametrlari:" in r.out
    r = run("--lang", "en", "extract", "--help")
    assert "extract options:" in r.out and "Examples:" in r.out


@pytest.mark.slow
def test_extract_with_every_core_backend(src: Path, tmp_path: Path) -> None:
    from jsonreshape import core

    results = []
    for backend in core.available_backends():
        out = tmp_path / f"o_{backend}.json"
        p = proc("--backend", backend, "extract", src, out, "--gather", "orders[*]")
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        results.append(out.read_bytes())
    assert all(x == results[0] for x in results) and codec.loads(results[0]) == ORDERS
