"""``jsonreshape stats INPUT [--field F ...] [--top N] [--exact] [--sample N] [--memory-limit SIZE] [--json]
[--histogram] [--bins N] [--where/--jmespath/--preset] [--select SPEC]`` (docs/ARCHITECTURE.md §14.1, §14.8; AGENT_PROMPT §9, §13).

In-process (``cli.main``) and as subprocesses (every core backend): the text of :func:`stats.format_stats`
(Uzbek and English) and the ``--json`` report (``FieldStats.to_dict()``, equal to calling the core directly),
exact numbers (``0.1``, ``1e400``, ``12345678901234567890``, ``-0.0``, ``1.10``), Uzbek Latin/Cyrillic text and
apostrophes, presence (null / missing), the default fields (``schema.default_columns``), the record scope
(``--where``, ``--select``), ``$key``, ``--top`` / ``--sample`` / ``--exact`` with approximate counting forced by a
tiny ``--memory-limit`` (and the limit's minimum / cap warnings), gzip / UTF-16 / BOM / CRLF inputs, JSON errors
(exit 2, ``--allow-partial``), bad arguments (exit 1), cancellation (exit 3) and the histograms (``--histogram``,
``--bins``: text blocks per field, ``--json`` entries with exact edges).
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
MiB = 1 << 20


# ----------------------------------------------------------------------------------
# harness
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _cli_isolation(monkeypatch: pytest.MonkeyPatch):
    """main() changes process state on purpose (language, JSONRESHAPE_BACKEND, logging, signal handlers):
    put everything back after each test; the memory check of the pytest process is off."""
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


def set_settings(**kw: Any) -> None:
    from jsonreshape import settings

    settings.set_settings(settings.Settings(**kw))


RECS = [
    {"id": 1, "name": "Toshkent", "score": N("0.1"), "city": "Toshkent"},
    {"id": 2, "name": "Тошкент", "score": N("1e400"), "city": "Toshkent"},
    {"id": 3, "name": "O'zbekiston", "score": N("12345678901234567890"), "city": None},
    {"id": 4, "name": "Oʻzbekiston", "score": N("-0.0"), "city": "Samarqand"},
    {"id": 5, "name": "Toshkent", "score": N("1.10")},
]


@pytest.fixture
def src(tmp_path: Path) -> Path:
    p = tmp_path / "people.json"
    jsongen.write_file(p, RECS, "array", style="pretty2")
    return p


def fields_of(out: str) -> list[dict[str, Any]]:
    """The fields of a ``--json`` report without the run-dependent values."""
    data = json.loads(out)
    for f in data["fields"]:
        f.pop("elapsed")
        f.pop("gen")
    return data["fields"]


# ----------------------------------------------------------------------------------
# text and JSON output
# ----------------------------------------------------------------------------------
def test_stats_text_in_uzbek(run, src: Path) -> None:
    r = run("stats", src, "--field", "score", "--field", "name", "--field", "city")
    assert r.code == 0, r.err
    out = r.out
    assert out.startswith("Maydon: score    5 ta yozuv")
    # exact numbers: the spelling of the extremes, the integral ones counted (1e400, the big integer, -0.0)
    assert "Sonlar: 5 ta (butun: 3) · min -0.0 · max 1e400" in out
    assert "Noyob qiymatlar: 4 (aniq)" in out          # name: no case / apostrophe folding, Cyrillic apart
    assert '"Toshkent"' in out and '"Тошкент"' in out and "\"O'zbekiston\"" in out and '"Oʻzbekiston"' in out
    lines = out.splitlines()
    top = [line for line in lines if line.strip().startswith("1. ")]
    assert any('"Toshkent"' in line and " 2 " in line for line in top)   # the most frequent name (2 of 5)
    # presence: null is present, a missing key is missing
    assert "Mavjud: 4 (80%)    null: 1 (25%)    yo'q: 1 (20%)" in out
    assert "Statistika: 3 ta maydon, 5 ta yozuv" in r.err


def test_stats_text_in_english(run, src: Path) -> None:
    r = run("--lang", "en", "stats", src, "--field", "score", "--top", "2")
    assert r.code == 0, r.err
    assert r.out.startswith("Field: score    5 records")
    assert "Numbers: 5 (integers: 3) · min -0.0 · max 1e400" in r.out
    assert "The 2 most frequent values:" in r.out
    assert "Statistics: 1 field, 5 records" in r.err


def test_stats_json_equals_the_core(run, src: Path) -> None:
    from jsonreshape.reader import Document
    from jsonreshape.stats import field_stats

    r = run("stats", src, "--field", "score", "--field", "name", "--field", "city", "--json")
    assert r.code == 0, r.err
    data = json.loads(r.out)
    assert data["records"] == 5 and data["scope"] == {"records": 5, "select": None, "filter": None, "sample": None}
    assert Path(data["file"]) == src
    with Document.open(src) as doc:
        doc.build_index()
        expected = [fs.to_dict() for fs in field_stats(doc, ["score", "name", "city"])]
    for f in expected:
        f.pop("elapsed")
        f.pop("gen")
    assert fields_of(r.out) == expected
    score = data["fields"][0]
    assert score["number"]["min"] == "-0.0" and score["number"]["max"] == "1e400"
    assert score["number"]["sum"].startswith("1") and "12345678901234567891.2" in score["number"]["sum"].replace(
        "E+400", "")  # exact sum of the five numbers (no float on the way)
    assert {v["json"] for v in score["top"]} == {"0.1", "1e400", "12345678901234567890", "-0.0", "1.10"}


def test_stats_default_fields_come_from_the_schema(run, src: Path) -> None:
    r = run("stats", src, "--json")
    assert r.code == 0, r.err
    assert [f["field"] for f in json.loads(r.out)["fields"]] == ["id", "name", "score", "city"]
    assert "--field berilmadi: tuzilmadagi 4 ta asosiy maydon olindi." in r.err


def test_stats_scope_where_and_select(run, src: Path) -> None:
    r = run("stats", src, "--field", "id", "--where", "id > 2", "--json")
    assert r.code == 0, r.err
    data = json.loads(r.out)
    assert data["scope"]["records"] == 3 and data["scope"]["filter"] == {"text": "id > 2", "lang": "expr"}
    assert data["fields"][0]["scanned"] == 3 and data["fields"][0]["number"]["min"] == "3"
    r = run("stats", src, "--field", "id", "--select", "0,4", "--json")
    data = json.loads(r.out)
    assert data["scope"]["select"] == "0,4" and data["fields"][0]["scanned"] == 2
    assert {v["json"] for v in data["fields"][0]["top"]} == {"1", "5"}
    # --select and --where together: the filter runs over the selection
    r = run("stats", src, "--field", "id", "--select", "1:", "--where", "id < 4", "--json")
    assert json.loads(r.out)["fields"][0]["scanned"] == 2
    r = run("stats", src, "--field", "id", "--select", "9")
    assert r.code == 1 and "faylda yo'q" in r.err


def test_stats_member_key_and_a_document_without_keys(run, tmp_path: Path, src: Path) -> None:
    keyed = tmp_path / "keyed.json"
    jsongen.write_file(keyed, [{"v": 1}, {"v": 2}, {"v": 2}], "keyed", keys=["a", "b", "Тошкент"])
    r = run("stats", keyed, "--field", "$key", "--field", "v", "--json")
    assert r.code == 0, r.err
    key, v = json.loads(r.out)["fields"]
    assert key["key"] is True and key["distinct"] == 3
    assert {x["json"] for x in key["top"]} == {'"a"', '"b"', '"Тошкент"'}
    assert v["top"][0]["json"] == "2" and v["top"][0]["count"] == 2
    r = run("stats", src, "--field", "$key")
    assert r.code == 0 and "! Bu hujjatda yozuvlarning kaliti yo'q" in r.out


def test_stats_top_sample_and_exact_counting(run, tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "many.jsonl"
    jsongen.write_file(p, [{"id": i, "g": i % 3} for i in range(3000)], "jsonl")
    r = run("stats", p, "--field", "g", "--top", "0", "--json")
    g = json.loads(r.out)["fields"][0]
    assert g["top"] == [] and g["distinct"] == 3 and g["distinct_exact"] is True
    r = run("stats", p, "--field", "id", "--sample", "10", "--json")
    assert json.loads(r.out)["fields"][0]["scanned"] == 10
    # a tiny counting budget: approximate distinct counts ("≈"), exact again with --exact
    monkeypatch.setattr(cli, "MIN_MEMORY_LIMIT", 1)
    r = run("stats", p, "--field", "id", "--memory-limit", "1KB")
    assert r.code == 0, r.err
    assert "Noyob qiymatlar: ≈" in r.out and "HyperLogLog" in r.out
    r = run("stats", p, "--field", "id", "--memory-limit", "1KB", "--exact", "--json")
    ident = json.loads(r.out)["fields"][0]
    assert ident["distinct"] == 3000 and ident["distinct_exact"] is True and ident["top_exact"] is True


def test_stats_memory_limit_minimum_and_cap(run, src: Path, monkeypatch) -> None:
    from jsonreshape import stats as stats_mod

    seen: list[Any] = []
    original = stats_mod.field_stats

    def spy(*args: Any, **kwargs: Any):
        seen.append(kwargs.get("memory_limit"))
        return original(*args, **kwargs)

    monkeypatch.setattr(stats_mod, "field_stats", spy)
    set_settings(memory_limit_bytes=64 * MiB)
    r = run("stats", src, "--field", "id")
    assert r.code == 0 and seen[-1] is None and "Ogohlantirish" not in r.err      # the core's own default
    r = run("stats", src, "--field", "id", "--memory-limit", "1")
    assert seen[-1] == 16 * MiB and "--memory-limit 1 MB juda kichik: 16 MB ishlatiladi." in r.err
    r = run("--lang", "en", "stats", src, "--field", "id", "--memory-limit", "100")
    assert seen[-1] == 32 * MiB
    assert ("--memory-limit 100 MB: the \"Memory limit\" setting (64 MB) allows at most 32 MB here; 32 MB is "
            "used.") in r.err
    r = run("stats", src, "--field", "id", "--memory-limit", "20MB")
    assert seen[-1] == 20 * MiB and "Ogohlantirish" not in r.err


# ----------------------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["gzip", "utf16", "bom_crlf", "jsonl"])
def test_stats_of_every_input_encoding_agree(run, tmp_path: Path, src: Path, variant: str) -> None:
    base = run("stats", src, "--field", "name", "--field", "score", "--json")
    if variant == "gzip":
        p = tmp_path / "people.json.gz"
        jsongen.write_file(p, RECS, "array", style="pretty2", gz=True)
    elif variant == "utf16":
        p = tmp_path / "people16.json"
        jsongen.write_file(p, RECS, "array", style="pretty2", encoding="utf-16-le", bom=True)
    elif variant == "bom_crlf":
        p = tmp_path / "people_bom.json"
        jsongen.write_file(p, RECS, "array", style="pretty2", bom=True, newline="\r\n")
    else:
        p = tmp_path / "people.jsonl"
        jsongen.write_file(p, RECS, "jsonl")
    r = run("stats", p, "--field", "name", "--field", "score", "--json")
    assert r.code == 0, r.err
    assert fields_of(r.out) == fields_of(base.out)


# ----------------------------------------------------------------------------------
# errors and exit codes
# ----------------------------------------------------------------------------------
def test_stats_errors(run, tmp_path: Path, src: Path) -> None:
    r = run("stats", tmp_path / "nope.json", "--field", "id")
    assert r.code == 1 and "topilmadi" in r.err
    r = run("stats", src, "--field", "a[")
    assert r.code == 1 and "Yo'l noto'g'ri yozilgan" in r.err and "^" in r.err
    r = run("stats", src, "--field", "id", "--top", "20000")
    assert r.code == 1
    r = run("stats", src, "--top", "-1")
    assert r.code == 1 and "0 yoki undan katta butun son" in r.err
    r = run("stats", src, "--where", "id >")
    assert r.code == 1 and "^" in r.err
    assert r.out == ""


def test_stats_json_error_and_allow_partial(run, tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_bytes(b'[{"id": 1}, {"id": 2}, {"id": ]')
    r = run("stats", p, "--field", "id")
    assert r.code == 2 and "--allow-partial" in r.err and r.out == ""
    r = run("stats", p, "--field", "id", "--allow-partial", "--json")
    assert r.code == 0 and json.loads(r.out)["fields"][0]["scanned"] == 2
    assert "faqat xatogacha bo'lgan 2 ta yozuv" in r.err


def test_stats_cancelled(run, src: Path) -> None:
    token = CancelToken()
    token.cancel()
    r = run("stats", src, "--field", "id", cancel=token)
    assert r.code == 3 and "bekor qilindi" in r.err


def test_stats_help_in_both_languages(run) -> None:
    r = run("stats", "--help")
    assert r.code == 0 and r.out.startswith("foydalanish: jsonreshape stats")
    assert "statistika parametrlari:" in r.out and "--exact" in r.out and "--select SPEC" in r.out
    r = run("stats", "--help", "--lang", "en")
    assert "statistics options:" in r.out and "Examples:" in r.out


# ----------------------------------------------------------------------------------
# every core backend (subprocesses)
# ----------------------------------------------------------------------------------
@pytest.mark.slow
def test_stats_with_every_core_backend(src: Path) -> None:
    from jsonreshape import core

    outputs = []
    for backend in core.available_backends():
        p = proc("--backend", backend, "stats", src, "--field", "score", "--field", "name", "--field", "city",
                 "--json")
        assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
        outputs.append(fields_of(p.stdout.decode("utf-8")))
    assert all(o == outputs[0] for o in outputs)


# ----------------------------------------------------------------------------------
# --histogram / --bins (stage 7)
# ----------------------------------------------------------------------------------
@pytest.fixture
def spread(tmp_path: Path) -> Path:
    p = tmp_path / "spread.jsonl"
    p.write_text("".join(json.dumps({"v": i, "s": "x" * (i % 4 + 1)}) + "\n" for i in range(100)), encoding="utf-8")
    return p


def test_stats_histogram_text(run, spread: Path) -> None:
    r = run("stats", spread, "--field", "v", "--field", "s", "--top", "0", "--histogram")
    assert r.code == 0, r.err
    v_block, s_block = r.out.split("\n\n")
    assert "  Sonlar gistogrammasi:\n    Taqsimot: 20 ta oraliq, 100 ta qiymat (aniq):\n        0 – 4 " in v_block
    assert "Satr uzunliklari" not in v_block and "Sonlar gistogrammasi" not in s_block
    assert "  Satr uzunliklari gistogrammasi (belgilar):\n    Taqsimot: 4 ta oraliq, 100 ta qiymat (aniq):" in s_block
    assert [line.split()[:2] for line in s_block.splitlines()[-4:]] == [["1", "25"], ["2", "25"], ["3", "25"],
                                                                       ["4", "25"]]
    r = run("--lang", "en", "stats", spread, "--field", "v", "--top", "0", "--bins", "4")     # --bins implies it
    assert r.code == 0, r.err
    assert "  Histogram of the numbers:\n    Distribution: 4 bins, 100 values (exact):\n        0 – 24 " in r.out
    assert run("stats", spread, "--field", "v", "--top", "0").out.count("gistogramma") == 0   # off by default


def test_stats_histogram_json(run, spread: Path, src: Path) -> None:
    r = run("stats", spread, "--field", "v", "--field", "s", "--bins", "4", "--json", "-q")
    assert r.code == 0, r.err
    v, s = fields_of(r.out)
    assert v["histogram"] == {"bins": 4, "edges": [0, 25, 50, 75, 100], "counts": [25, 25, 25, 25], "exact": True,
                              "sample_size": 100}
    assert v["length_histogram"] is None and s["histogram"] is None
    assert s["length_histogram"] == {"bins": 4, "edges": [1, 2, 3, 4, 5], "counts": [25, 25, 25, 25], "exact": True,
                                     "sample_size": 100}
    assert "histogram" not in fields_of(run("stats", spread, "--field", "v", "--json", "-q").out)[0]
    # exact edges: 1e400 stays a JSON number (no float), the counts are exact
    r = run("stats", src, "--field", "score", "--histogram", "--bins", "3", "--json", "-q")
    assert r.code == 0, r.err
    hist = codec.loads(r.out)["fields"][0]["histogram"]
    assert '"edges": [\n          0,\n          5E+399,\n          1E+400\n        ]' in r.out
    assert hist["counts"] == [4, 1] and hist["exact"] is True


@pytest.mark.parametrize("value", ["0", "4097", "abc", "-1"])
def test_stats_bins_out_of_range(run, src: Path, value: str) -> None:
    r = run("stats", src, "--bins", value)
    assert r.code == 1
    assert i18n.t("cli.argerr.hist_bins", value=value, maximum=cli.HIST_BINS_MAX) in r.err


def test_stats_histogram_constants_follow_the_core() -> None:
    from jsonreshape import stats

    assert (cli.HIST_BINS, cli.HIST_BINS_MAX) == (stats.HIST_BINS, stats.HIST_BINS_MAX)
