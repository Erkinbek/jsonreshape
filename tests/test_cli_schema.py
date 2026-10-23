"""``jsonreshape schema`` and ``jsonreshape validate`` (docs/ARCHITECTURE.md §14.3, §14.8; AGENT_PROMPT §9 [K]).

* ``schema INPUT [OUTPUT | -o OUTPUT] [--sample N | --full] [--strict] [--record | --document] [--title T]
  [--compact | --indent N]``: a draft 2020-12 schema written atomically (or to stdout), exact numbers in it
  (``1e400``, ``12345678901234567890``, ``0.1``), every document kind, a full scan by default (the schema always
  accepts its own file, also ``--strict``), the sample warning, ``.gz`` output, OUTPUT checks;
* ``validate INPUT --schema S [--scope] [--formats] [--max-issues N] [--max-per-record N] [--export FILE] [--json]``:
  ``#record [key] path: message (keyword)`` lines (``(hujjat)`` for document issues), exit 5 with issues and 0
  without, exact number comparisons (``0.10000000000000000001 > 0.1``, ``1e400`` is an integer), the JSON report
  equal to the core's, every issue exported (JSON Lines / JSON / CSV) in the same pass, schema errors (exit 1,
  before INPUT is read), the record scope, gzip / UTF-16 inputs, English texts, every core backend.
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
    {"id": 1, "name": "Ali", "score": N("0.1"), "when": "2026-10-19"},
    {"id": 0, "name": "B", "score": N("0.10000000000000000001")},
    {"name": "Vali", "when": "2026-13-45"},
    {"id": N("1e400"), "name": "Тошкент"},
    {"id": N("12345678901234567890"), "name": "O'zbekiston", "score": N("-0.0")},
]
RECORD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["id", "name"],
    "properties": {
        "id": {"type": "integer", "minimum": 1},
        "name": {"type": "string", "minLength": 2},
        "score": {"type": "number", "maximum": N("0.1")},
        "when": {"type": "string", "format": "date"},
    },
}


@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "people.json"
    jsongen.write_file(p, RECS, "array", style="pretty2")
    return p


@pytest.fixture
def schema_file(tmp_path: Path) -> Path:
    p = tmp_path / "people.schema.json"
    p.write_bytes(codec.dumps(RECORD_SCHEMA, indent=2))
    return p


# ----------------------------------------------------------------------------------
# schema
# ----------------------------------------------------------------------------------
def test_schema_to_a_file_validates_its_own_file(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "gen.schema.json"
    r = run("schema", src, out)
    assert r.code == 0, r.err
    assert r.out == "" and "JSON Schema -> " in r.err and "5 ta yozuv bo'yicha" in r.err
    data = out.read_bytes()
    value = codec.loads(data)
    assert value["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert value["title"] == "people.json" and value["type"] == "array"
    assert value["$comment"] == "jsonreshape:scope=document"
    assert b"1e400" in data and b"12345678901234567890" in data and b"0.10000000000000000001" in data
    assert "Тошкент".encode() in data                                   # non-ASCII unescaped
    r = run("validate", src, "--schema", out)
    assert r.code == 0 and r.out == "" and "Schema'ga mos: 5 ta yozuv tekshirildi, xato yo'q" in r.err


def test_schema_to_stdout_and_layouts(run, src: Path, tmp_path: Path) -> None:
    r = run("schema", src, "--record", "--title", "Mijozlar ro'yxati", "--compact")
    assert r.code == 0, r.err
    assert "\n" not in r.out.rstrip("\n")
    value = codec.loads(r.out)
    assert value["title"] == "Mijozlar ro'yxati" and value["type"] == "object"
    assert value["$comment"] == "jsonreshape:scope=record" and "id" in value["properties"]
    r = run("schema", src, "--indent", "4", "--document")
    assert r.out.startswith('{\n    "$schema"')
    r = run("schema", src, "-o", tmp_path / "s.json", "--full")
    assert r.code == 0 and codec.loads((tmp_path / "s.json").read_bytes())["type"] == "array"


def test_schema_strict_still_accepts_its_own_file(run, src: Path, tmp_path: Path) -> None:
    out = tmp_path / "strict.json"
    r = run("schema", src, out, "--strict")
    assert r.code == 0, r.err
    value = codec.loads(out.read_bytes())
    items = value["items"]
    assert items.get("additionalProperties") is False
    assert run("validate", src, "--schema", out).code == 0
    other = tmp_path / "other.json"
    jsongen.write_file(other, RECS + [{"id": 2, "name": "Ali", "extra": True}], "array")
    r = run("validate", other, "--schema", out)
    assert r.code == 5 and "#5 " in r.out and "(additionalProperties)" in r.out


def test_schema_from_a_sample_warns(run, tmp_path: Path) -> None:
    p = tmp_path / "many.jsonl"
    jsongen.write_file(p, [{"id": i, **({"x": 1} if i == 999 else {})} for i in range(1000)], "jsonl")
    r = run("schema", p, "--sample", "10")
    assert r.code == 0, r.err
    assert "Schema 1 000 ta yozuvdan 10 tasi (namuna) bo'yicha yaratildi" in r.err
    full = run("schema", p)
    assert "namuna" not in full.err and "x" in codec.loads(full.out)["properties"]
    r = run("schema", p, "--sample", "10", "--full")
    assert r.code == 1 and "birga ishlatib bo'lmaydi" in r.err


@pytest.mark.parametrize("kind", ["keyed", "wrapped", "jsonl"])
def test_schema_of_other_document_kinds(run, tmp_path: Path, kind: str) -> None:
    p = tmp_path / f"d_{kind}.json"
    recs = [{"v": 1, "s": "a"}, {"v": N("2.50"), "s": "Тошкент"}]
    jsongen.write_file(p, recs, kind, keys=["k1", "k2"] if kind == "keyed" else None)
    out = tmp_path / f"d_{kind}.schema.json"
    r = run("schema", p, out)
    assert r.code == 0, r.err
    value = codec.loads(out.read_bytes())
    if kind == "keyed":
        assert value["type"] == "object" and "additionalProperties" in value
    elif kind == "wrapped":
        assert "data" in value["properties"] and "meta" in value["properties"]
    else:
        assert value["type"] == "object" and value["$comment"] == "jsonreshape:scope=record"
    assert run("validate", p, "--schema", out).code == 0


def test_schema_output_checks_and_gzip(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    before = src.read_bytes()
    monkeypatch.chdir(tmp_path)
    # another spelling of the same name only where the file system folds case (NTFS, APFS)
    upper = [str(src).upper()] if case_folds(tmp_path) else []
    for target in (src, "people.json", *upper):
        r = run("schema", src, target)
        assert r.code == 1 and "manba faylning o'ziga" in r.err
    assert src.read_bytes() == before
    r = run("schema", src, "a.json", "-o", "b.json")
    assert r.code == 1 and "ikki marta berilgan" in r.err
    r = run("schema", src, tmp_path / "missing" / "s.json")
    assert r.code == 1
    gz = tmp_path / "s.json.gz"
    r = run("schema", src, gz)
    assert r.code == 0 and codec.loads(gzip.decompress(gz.read_bytes()))["title"] == "people.json"


@pytest.mark.parametrize("variant", ["gzip", "utf16"])
def test_schema_of_gzip_and_utf16_inputs(run, src: Path, tmp_path: Path, variant: str) -> None:
    base = codec.loads(run("schema", src).out)
    p = tmp_path / ("p.json.gz" if variant == "gzip" else "p16.json")
    if variant == "gzip":
        jsongen.write_file(p, RECS, "array", gz=True)
    else:
        jsongen.write_file(p, RECS, "array", encoding="utf-16-le", bom=True)
    value = codec.loads(run("schema", p).out)
    assert value["items"] == base["items"]


# ----------------------------------------------------------------------------------
# validate
# ----------------------------------------------------------------------------------
def test_validate_lines_and_exit_5(run, src: Path, schema_file: Path) -> None:
    r = run("validate", src, "--schema", schema_file)
    assert r.code == 5, r.err
    assert r.out.splitlines() == [
        "#1 id: Son juda kichik: 0. Eng kichik ruxsat etilgan qiymat: 1. (minimum)",
        "#1 name: Satr juda qisqa: 1 ta belgi, kamida 2 ta belgi kerak. (minLength)",
        "#1 score: Son juda katta: 0.10000000000000000001. Eng katta ruxsat etilgan qiymat: 0.1. (maximum)",
        "#2 $: Majburiy maydon yo'q: \"id\". (required)",
    ]
    assert "Schema'ga mos emas: 5 ta yozuvdan 2 tasida 4 ta xato" in r.err
    assert "Kalit so'zlar bo'yicha:" in r.err and "required 1" in r.err
    # formats are asserted only with --formats
    r = run("validate", src, "--schema", schema_file, "--formats")
    assert r.code == 5 and r.out.splitlines()[-1] == "#2 when: Satr date formatiga mos emas. (format)"


def test_validate_in_english_and_ok(run, src: Path, schema_file: Path, tmp_path: Path) -> None:
    r = run("--lang", "en", "validate", src, "--schema", schema_file)
    assert r.code == 5 and "Not valid: 4 issues in 2 of 5 records" in r.err and "By keyword:" in r.err
    good = tmp_path / "good.json"
    jsongen.write_file(good, [RECS[0], RECS[3], RECS[4]], "array")
    r = run("--lang", "en", "validate", good, "--schema", schema_file)
    assert r.code == 0 and r.out == "" and "Valid: 3 records checked, no issues" in r.err


def test_validate_json_equals_the_core(run, src: Path, schema_file: Path) -> None:
    from jsonreshape.jschema import validate
    from jsonreshape.reader import Document

    r = run("validate", src, "--schema", schema_file, "--json")
    assert r.code == 5, r.err
    data = json.loads(r.out)
    assert data["ok"] is False and data["scope"] == "record" and data["issue_count"] == 4
    assert data["invalid"] == 2 and data["scanned"] == 5 and data["exported"] is None
    assert data["counts"] == {"minimum": 1, "minLength": 1, "maximum": 1, "required": 1}
    with Document.open(src) as doc:
        doc.build_index()
        rep = validate(doc, RECORD_SCHEMA)
        expected = [json.loads(codec.dumps(i.to_dict())) for i in rep.issues]
        rep.close()
    assert data["issues"] == expected


def test_validate_document_scope_and_keyed_records(run, src: Path, tmp_path: Path) -> None:
    doc_schema = tmp_path / "doc.schema.json"
    doc_schema.write_bytes(codec.dumps({"type": "array", "maxItems": 2, "items": {"type": "object"}}))
    r = run("validate", src, "--schema", doc_schema)
    assert r.code == 5 and r.out.splitlines() == [
        "(hujjat) $: Hujjatda 5 ta yozuv bor, ko'pi bilan 2 ta bo'lishi mumkin. (maxItems)"]
    r = run("validate", src, "--schema", doc_schema, "--scope", "record")
    assert r.code == 5 and all(line.startswith("#") for line in r.out.splitlines())   # each record is no array
    keyed = tmp_path / "keyed.json"
    jsongen.write_file(keyed, [{"v": 1}, {"v": "x"}], "keyed", keys=["a", "b"])
    rec_schema = tmp_path / "rec.schema.json"
    rec_schema.write_bytes(codec.dumps({"type": "object", "properties": {"v": {"type": "integer"}}}))
    r = run("validate", keyed, "--schema", rec_schema, "--scope", "record")
    assert r.code == 5 and r.out.startswith("#1 [b] v: ") and r.out.rstrip().endswith("(type)")


def test_validate_limits(run, src: Path, schema_file: Path) -> None:
    r = run("validate", src, "--schema", schema_file, "--max-issues", "1")
    assert r.code == 5 and len(r.out.splitlines()) == 1
    assert "Faqat birinchi xato ko'rsatildi (jami 4 ta)" in r.err
    r = run("validate", src, "--schema", schema_file, "--max-issues", "0")
    assert r.code == 5 and r.out == "" and "Faqat" not in r.err and "4 ta xato" in r.err
    r = run("validate", src, "--schema", schema_file, "--max-per-record", "1")
    assert r.code == 5 and len(r.out.splitlines()) == 2
    assert "2 ta yozuvda xatolar ko'p" in r.err
    r = run("validate", src, "--schema", schema_file, "--max-per-record", "0", "--json")
    assert json.loads(r.out)["issue_count"] == 4


@pytest.mark.parametrize("fmt", ["jsonl", "json", "csv"])
def test_validate_export_every_issue(run, src: Path, schema_file: Path, tmp_path: Path, fmt: str) -> None:
    out = tmp_path / f"issues.{fmt}"
    r = run("validate", src, "--schema", schema_file, "--export", out, "--max-issues", "1")
    assert r.code == 5, r.err
    assert "4 ta xato yozildi" in r.err and len(r.out.splitlines()) == 1     # every issue, not only the printed
    data = out.read_bytes()
    if fmt == "jsonl":
        rows = [json.loads(line) for line in data.splitlines()]
    elif fmt == "json":
        rows = json.loads(data)
    else:
        table = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        assert table[0] == ["record", "key", "path", "keyword", "schema_path", "message", "params"]
        rows = [dict(zip(table[0], row)) for row in table[1:]]
    assert [str(x["record"]) for x in rows] == ["1", "1", "1", "2"]
    assert [x["keyword"] for x in rows] == ["minimum", "minLength", "maximum", "required"]
    assert [x["path"] for x in rows] == ["id", "name", "score", "$"]


def test_validate_scope_options(run, src: Path, schema_file: Path) -> None:
    r = run("validate", src, "--schema", schema_file, "--select", "2:")
    assert r.code == 5 and r.out.splitlines() == ["#2 $: Majburiy maydon yo'q: \"id\". (required)"]
    r = run("validate", src, "--schema", schema_file, "--where", "name != 'B'", "--json")
    assert json.loads(r.out)["issue_count"] == 1
    r = run("validate", src, "--schema", schema_file, "--select", "3,4")
    assert r.code == 0 and "2 ta yozuv tekshirildi" in r.err


def test_validate_schema_errors_before_the_input_is_read(run, src: Path, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape import reader

    opened: list[Any] = []
    original = reader.Document.open

    def spy(*args: Any, **kwargs: Any):
        opened.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(reader.Document, "open", spy)
    r = run("validate", src, "--schema", tmp_path / "none.json")
    assert r.code == 1 and "topilmadi" in r.err
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"type": ')
    r = run("validate", src, "--schema", bad)
    assert r.code == 1
    remote = tmp_path / "remote.json"
    remote.write_bytes(b'{"$ref": "https://example.com/schema.json"}')
    r = run("validate", src, "--schema", remote)
    assert r.code == 1
    wrong = tmp_path / "wrong.json"
    wrong.write_bytes(b'{"minimum": "x"}')
    r = run("validate", src, "--schema", wrong)
    assert r.code == 1
    assert opened == []
    r = run("validate", src)
    assert r.code == 1 and "--schema" in r.err


def test_validate_export_checks(run, src: Path, schema_file: Path, tmp_path: Path, monkeypatch) -> None:
    before = src.read_bytes(), schema_file.read_bytes()
    monkeypatch.chdir(tmp_path)
    # another spelling of the same name only where the file system folds case (NTFS, APFS)
    folds = case_folds(tmp_path)
    r = run("validate", src, "--schema", schema_file, "--export", str(schema_file).upper() if folds else schema_file)
    assert r.code == 1 and "schema faylining o'zi" in r.err
    for target in (src, "PEOPLE.JSON" if folds else "people.json", "./people.json"):
        r = run("validate", src, "--schema", schema_file, "--export", target)
        assert r.code == 1 and "manba faylning o'ziga" in r.err
    r = run("validate", src, "--schema", schema_file, "--export", "issues.txt")
    assert r.code == 1 and ".jsonl, .json, .csv" in r.err
    assert (src.read_bytes(), schema_file.read_bytes()) == before


@pytest.mark.parametrize("variant", ["gzip", "utf16"])
def test_validate_gzip_and_utf16_inputs(run, schema_file: Path, tmp_path: Path, variant: str) -> None:
    p = tmp_path / ("p.json.gz" if variant == "gzip" else "p16.json")
    if variant == "gzip":
        jsongen.write_file(p, RECS, "array", gz=True)
    else:
        jsongen.write_file(p, RECS, "array", encoding="utf-16-le", bom=True)
    r = run("validate", p, "--schema", schema_file)
    assert r.code == 5 and len(r.out.splitlines()) == 4


def test_validate_json_error_exit_2(run, schema_file: Path, tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_bytes(b'[{"id": 1, "name": "Ali"}, {"id": ')
    r = run("validate", p, "--schema", schema_file)
    assert r.code == 2 and r.out == ""
    r = run("validate", p, "--schema", schema_file, "--allow-partial")
    assert r.code == 0 and "1 ta yozuv tekshirildi" in r.err


def test_schema_and_validate_help(run) -> None:
    for command, needle in (("schema", "--strict"), ("validate", "--max-per-record")):
        r = run(command, "--help")
        assert r.code == 0 and r.out.startswith(f"foydalanish: jsonreshape {command}") and needle in r.out
        r = run("--lang", "en", command, "--help")
        assert r.code == 0 and "Examples:" in r.out


@pytest.mark.slow
def test_validate_with_every_core_backend(src: Path, schema_file: Path) -> None:
    from jsonreshape import core

    outputs = []
    for backend in core.available_backends():
        p = proc("--backend", backend, "validate", src, "--schema", schema_file, "--json")
        assert p.returncode == 5, p.stderr.decode("utf-8", "replace")
        data = json.loads(p.stdout.decode("utf-8"))
        data.pop("elapsed")
        outputs.append(data)
    assert all(o == outputs[0] for o in outputs)
