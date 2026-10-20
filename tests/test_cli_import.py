"""``jsonreshape import INPUT [OUTPUT | -o OUTPUT] [--from csv|yaml|xml] [--to json|jsonl | --jsonl] [--compact |
--indent N] [--types auto|string] [--empty null|string] [--delimiter D] [--no-header] [--no-nest] [--encoding ENC]
[--skip-bad] [--record-tag TAG | --record-path P] [--json]`` (docs/ARCHITECTURE.md §17; core: importers.py).

The summary line and the notes in Uzbek and English, ``--json``, every option, exact numbers, the exit codes: 0
written, 1 user errors (OUTPUT naming the input in any spelling, a missing or doubled OUTPUT, an unknown extension
without ``--from``, a missing pyyaml), 2 a malformed input (CSV row, YAML, XML) with the line, 3 cancelled (nothing
written), the help in both languages, and one run as a real subprocess.
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

from jsonreshape import cli, codec, i18n
from jsonreshape.codec import JsonFloat, JsonInt
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


CSV = "id,name,price,contact.city,active,note\n1,Olma,1.10,Toshkent,true,\n007,Anor,12345678901234567890,Samarqand,false,x\n"
RECORDS = [
    {"id": 1, "name": "Olma", "price": JsonFloat("1.10"), "contact": {"city": "Toshkent"}, "active": True, "note": None},
    {"id": "007", "name": "Anor", "price": JsonInt("12345678901234567890"), "contact": {"city": "Samarqand"},
     "active": False, "note": "x"},
]


def write(path: Path, data: str | bytes, encoding: str = "utf-8") -> Path:
    path.write_bytes(data.encode(encoding) if isinstance(data, str) else data)
    return path


def lines(path: Path) -> list[Any]:
    return [codec.loads(line) for line in path.read_bytes().splitlines()]


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    return write(tmp_path / "data.csv", CSV)


# ----------------------------------------------------------------------------------
# the command and its report
# ----------------------------------------------------------------------------------
def test_csv_to_json_lines_with_summary(run, csv_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "data.jsonl"
    r = run("import", csv_file, out)
    assert r.code == 0, r.err
    assert lines(out) == RECORDS and r.out == ""
    text = out.read_text(encoding="utf-8")
    assert '"price":1.10' in text and '"price":12345678901234567890' in text and '"id":"007"' in text
    err = r.err.splitlines()
    assert err[0].startswith("2 ta yozuv: ") and "data.csv (CSV / TSV) -> " in err[0] and "data.jsonl (" in err[0]
    assert err[1:] == ["3 ta katak son bo'ldi.", "2 ta katak true / false / null bo'ldi.", "1 ta bo'sh katak null bo'ldi."]
    assert csv_file.read_text(encoding="utf-8") == CSV


def test_english_summary(run, csv_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "data.jsonl"
    r = run("import", csv_file, out, "--lang", "en")
    assert r.code == 0
    err = r.err.splitlines()
    assert err[0].startswith("2 records: ") and err[1] == "3 cells became numbers."
    r = run("import", write(tmp_path / "one.csv", "a\n1\n"), tmp_path / "one.jsonl", "--lang", "en")
    assert r.err.splitlines() == [r.err.splitlines()[0], "1 cell became a number."] and r.err.startswith("1 record: ")


def test_json_report(run, csv_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "data.json"
    r = run("import", csv_file, out, "--json", "-q")
    assert r.code == 0 and r.err == ""
    data = json.loads(r.out)
    assert data["records"] == 2 and data["format"] == "csv" and data["output_format"] == "json"
    assert data["columns"] == ["id", "name", "price", "contact.city", "active", "note"]
    assert data["numbers"] == 3 and data["literals"] == 2 and data["empty_cells"] == 1 and data["skipped"] == 0
    assert data["encoding"] == "utf-8" and data["delimiter"] == "," and data["bytes_written"] == out.stat().st_size
    assert data["note_keys"] == ["importers.note.numbers", "importers.note.literals", "importers.note.empty_null"]
    assert codec.loads(out.read_bytes()) == RECORDS


def test_quiet_hides_the_summary_but_not_warnings(run, tmp_path: Path) -> None:
    src = write(tmp_path / "w.csv", "a,b\n1,2\n3\n")
    r = run("import", src, tmp_path / "o.jsonl", "--skip-bad", "-q")
    assert r.code == 0 and r.err.splitlines() == [
        "Ogohlantirish: 1 ta buzuq qator o'tkazib yuborildi (3-qator)."]


# ----------------------------------------------------------------------------------
# output options
# ----------------------------------------------------------------------------------
def test_json_array_layouts(run, csv_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "data.json"
    assert run("import", csv_file, out).code == 0
    assert out.read_bytes() == codec.dumps(RECORDS, indent=2)          # the export_indent setting (2)
    assert run("import", csv_file, out, "--indent", "4").code == 0
    assert out.read_bytes() == codec.dumps(RECORDS, indent=4)
    assert run("import", csv_file, out, "--indent", "tab").code == 0
    assert out.read_bytes() == codec.dumps(RECORDS, indent="\t")
    assert run("import", csv_file, out, "--compact").code == 0
    assert out.read_bytes() == codec.dumps(RECORDS)
    r = run("import", csv_file, out, "--compact", "--indent", "4")
    assert r.code == 1 and "--compact" in r.err


def test_to_and_jsonl_options(run, csv_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "data.txt"
    assert run("import", csv_file, out).code == 0 and lines(out) == RECORDS       # jsonl by default
    assert run("import", csv_file, out, "--to", "json").code == 0
    assert codec.loads(out.read_bytes()) == RECORDS
    out2 = tmp_path / "data.json"
    assert run("import", csv_file, out2, "--jsonl").code == 0 and lines(out2) == RECORDS
    assert run("import", csv_file, "-o", tmp_path / "o.ndjson").code == 0 and lines(tmp_path / "o.ndjson") == RECORDS


def test_output_is_required_once(run, csv_file: Path, tmp_path: Path) -> None:
    r = run("import", csv_file)
    assert r.code == 1 and "Natija fayli ko'rsatilmagan" in r.err
    r = run("import", csv_file, tmp_path / "a.jsonl", "-o", tmp_path / "b.jsonl")
    assert r.code == 1 and "ikki marta" in r.err
    assert not (tmp_path / "a.jsonl").exists() and not (tmp_path / "b.jsonl").exists()


# ----------------------------------------------------------------------------------
# CSV options
# ----------------------------------------------------------------------------------
def test_csv_options(run, tmp_path: Path) -> None:
    src = write(tmp_path / "t.txt", "a.b\tc\n1\t\n2\t\"x\ty\"\n")
    out = tmp_path / "o.jsonl"
    r = run("import", src, out)
    assert r.code == 1 and "Kirish faylining turi kengaytmasidan aniqlanmadi" in r.err and "--from csv|yaml|xml" in r.err
    assert run("import", src, out, "--from", "csv", "--delimiter", "tab").code == 0
    assert lines(out) == [{"a": {"b": 1}, "c": None}, {"a": {"b": 2}, "c": "x\ty"}]
    assert run("import", src, out, "--format", "csv", "--delimiter", "\t", "--no-nest", "--types", "string",
               "--empty", "string").code == 0
    assert lines(out) == [{"a.b": "1", "c": ""}, {"a.b": "2", "c": "x\ty"}]
    assert run("import", src, out, "--from", "csv", "--no-header").code == 0
    assert lines(out) == [{"c1": "a.b", "c2": "c"}, {"c1": 1, "c2": None}, {"c1": 2, "c2": "x\ty"}]


def test_encoding_option(run, tmp_path: Path) -> None:
    src = write(tmp_path / "c.csv", "ism;shahar\nАли;Тошкент\n".encode("cp1251"))
    out = tmp_path / "o.jsonl"
    r = run("import", src, out)
    assert r.code == 2 and "utf-8 kodirovkasida o'qib bo'lmadi (2-qator atrofida)" in r.err and "--encoding" in r.err
    assert not out.exists()
    assert run("import", src, out, "--encoding", "cp1251").code == 0
    assert lines(out) == [{"ism": "Али", "shahar": "Тошкент"}]
    r = run("import", src, out, "--encoding", "nope")
    assert r.code == 1 and "Noma'lum kodirovka: nope" in r.err


def test_bad_rows_exit_2_or_skip(run, tmp_path: Path) -> None:
    src = write(tmp_path / "w.csv", "a,b\n1,2\n3\n4,5\n")
    out = tmp_path / "o.jsonl"
    r = run("import", src, out)
    assert r.code == 2 and not out.exists()
    assert r.err.splitlines()[0] == "Xato: 3-qator: 1 ta maydon, sarlavhada esa 2 ta."
    assert "--skip-bad" in r.err
    r = run("import", src, out, "--skip-bad")
    assert r.code == 0 and lines(out) == [{"a": 1, "b": 2}, {"a": 4, "b": 5}]
    assert "Ogohlantirish: 1 ta buzuq qator o'tkazib yuborildi (3-qator)." in r.err.splitlines()
    src = write(tmp_path / "d.csv", "a,b,a\n1,2,3\n")
    r = run("import", src, out)
    assert r.code == 2 and "«a» nomi takrorlangan (1, 3-ustunlar)" in r.err and "--no-header" in r.err


# ----------------------------------------------------------------------------------
# YAML and XML
# ----------------------------------------------------------------------------------
def test_yaml(run, tmp_path: Path) -> None:
    src = write(tmp_path / "c.yaml", "name: Olma\nprice: 1.10\nhex: 0x1F\nwhen: 2001-12-14\ntags: [a, b]\n")
    out = tmp_path / "c.json"
    r = run("import", src, out)
    assert r.code == 0 and r.err.splitlines()[0].startswith("1 ta yozuv: ") and "(YAML)" in r.err
    assert out.read_bytes() == b'{\n  "name": "Olma",\n  "price": 1.10,\n  "hex": 31,\n  "when": "2001-12-14",\n  "tags": [\n    "a",\n    "b"\n  ]\n}'
    assert "1 ta YAML son (0x1F, 017, 1_000, +1, 1:30, .5 ko'rinishidagi) qiymati bo'yicha yozildi." in r.err
    src = write(tmp_path / "s.yml", "---\na: 1\n---\nb: 2\n")
    r = run("import", src, tmp_path / "s.out")
    assert r.code == 0 and lines(tmp_path / "s.out") == [{"a": 1}, {"b": 2}]
    r = run("import", write(tmp_path / "bad.yaml", "a: [1,\nb: 2\n"), tmp_path / "bad.json")
    assert r.code == 2 and r.err.startswith("Xato: YAML xatosi (") and not (tmp_path / "bad.json").exists()
    r = run("import", write(tmp_path / "rec.yaml", "a: &a [*a]\n"), tmp_path / "rec.json")
    assert r.code == 1 and "rekursiv" in r.err


def test_yaml_without_pyyaml(run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yaml", None)
    r = run("import", write(tmp_path / "a.yaml", "a: 1\n"), tmp_path / "a.json")
    assert r.code == 1 and "pyyaml" in r.err and "pip install pyyaml" in r.err


def test_xml(run, tmp_path: Path) -> None:
    src = write(tmp_path / "c.xml", '<catalog><book id="1"><t>A</t><t>B</t></book><book id="2">text</book></catalog>')
    out = tmp_path / "c.jsonl"
    r = run("import", src, out)
    assert r.code == 0 and lines(out) == [{"@id": 1, "t": ["A", "B"]}, {"@id": 2, "#text": "text"}]
    assert run("import", src, out, "--record-tag", "t").code == 0 and lines(out) == ["A", "B"]
    assert run("import", src, out, "--record-path", "catalog/book/t").code == 0 and lines(out) == ["A", "B"]
    r = run("import", src, out, "--record-path", "nope")
    assert r.code == 0 and out.read_bytes() == b"" and "ildiz elementi: <catalog>" in r.err and "--record-tag" in r.err
    r = run("import", src, out, "--record-tag", "t", "--record-path", "x")
    assert r.code == 1
    r = run("import", write(tmp_path / "bad.xml", "<a><b></a>"), out)
    assert r.code == 2 and r.err.startswith("Xato: XML xatosi (1-qator, ") and "mismatched tag" in r.err
    evil = '<!DOCTYPE x [<!ENTITY e "x">]><a>&e;</a>'
    r = run("import", write(tmp_path / "evil.xml", evil), tmp_path / "evil.jsonl")
    assert r.code == 1 and "ENTITY" in r.err and not (tmp_path / "evil.jsonl").exists()


# ----------------------------------------------------------------------------------
# refusals, cancellation, help, a real process
# ----------------------------------------------------------------------------------
def test_output_may_not_be_the_input(run, csv_file: Path, tmp_path: Path) -> None:
    r = run("import", csv_file, csv_file)
    assert r.code == 1 and "manba faylning o'ziga yozilmaydi" in r.err
    other = tmp_path / "DATA.CSV" if os.name == "nt" else csv_file
    r = run("import", csv_file, other)
    assert r.code == 1
    r = run("import", csv_file, tmp_path)
    assert r.code == 1
    r = run("import", csv_file, tmp_path / "nowhere" / "o.jsonl")
    assert r.code == 1 and "Natija papkasi topilmadi" in r.err
    r = run("import", tmp_path / "missing.csv", tmp_path / "o.jsonl")
    assert r.code == 1 and "topilmadi" in r.err
    assert csv_file.read_text(encoding="utf-8") == CSV


def test_cancelled_writes_nothing(run, csv_file: Path, tmp_path: Path) -> None:
    token = CancelToken()
    token.cancel()
    out = tmp_path / "o.jsonl"
    r = run("import", csv_file, out, cancel=token)
    assert r.code == 3 and not out.exists() and "Bekor qilindi" in r.err or r.code == 3


def test_help_in_both_languages(run) -> None:
    r = run("import", "--help")
    assert r.code == 0 and r.out.startswith("foydalanish: jsonreshape import")
    for text in ("--from", "--record-tag", "--skip-bad", "CSV / TSV parametrlari:", "XML parametrlari:", "Misollar:",
                 "jsonreshape import data.csv data.jsonl", "Chiqish kodlari"):
        assert text in r.out, text
    r = run("import", "--help", "--lang", "en")
    assert r.code == 0 and r.out.startswith("usage: jsonreshape import")
    assert "CSV / TSV options:" in r.out and "Exit codes" in r.out and "a.b.c becomes nested objects" in r.out
    r = run("--help")
    assert "import" in r.out


def test_runs_as_a_process(csv_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "data.jsonl"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-m", "jsonreshape", "import", str(csv_file), str(out), "--lang", "en"],
                          cwd=ROOT, env=env, capture_output=True, timeout=240)
    assert proc.returncode == 0, proc.stderr
    assert lines(out) == RECORDS and proc.stderr.decode("utf-8").startswith("2 records: ")
