r"""Benchmark of the stage-5 tools on the 2 GB synthetic file (AGENT_PROMPT §9-§10, §12 "Dasturning umumiy
xotirasi < 500 MB"; docs/ARCHITECTURE.md §14): statistics (one field, four fields, exact counting), duplicates,
JSON Schema (generation from every record, validation), split (byte copy of the same kind; to JSON Lines), merge
with dedupe, extract (gather), repair (a clean file; 1 % of the records broken) and diff (key / position / align).

Every command runs as the user runs it -- ``jsonreshape <command> ...`` through ``bench/ops.py cli`` (the CLI's
``main`` in the measured process: Document.open, the index, the operation, worker processes as the program
chooses them) -- in a fresh process with a fresh, empty ``JSONRESHAPE_HOME``, measured from outside with
``run_bench.run_child`` (wall time, the exact peak working set of every process of the tree, the sampled sum over
the tree = the whole program); best of ``--repeat`` runs (default 2; a run over ``--max-repeat-seconds`` is not
repeated), memory = the largest of all runs.

Inputs (``bench/data/syn_2g.json``; made by ``bench/make_synthetic.py`` when missing) and the inputs this script
prepares once, unmeasured, in the work folder (``--work-dir``, default ``bench/data/work/tools-<run id>``; removed at
the end unless ``--keep-work``):

* ``syn_2g_changed.json`` -- the "new version" of syn_2g.json: every 100th record's ``score`` changed, every 997th
  record removed, a new record inserted before every 1000th (diff by key, merge with dedupe);
* ``syn_2g_inplace.json`` -- every 100th record's ``score`` changed, nothing removed or inserted (diff by position);
* ``syn_2g_broken1.json`` -- 1 % of the records broken (every 100th, five kinds in turn: ``NaN``, a trailing comma,
  single quotes, an unquoted key, a ``//`` comment line) for repair;
* ``syn_190m_a.json`` / ``syn_190m_b.json`` -- the first ~190 MiB of syn_2g.json and its changed version (diff
  aligned by content needs both files under ``Settings.diff_align_max_bytes``, 200 MiB by default);
* ``syn_2g.schema.json`` -- the JSON Schema of syn_2g.json (``jsonreshape schema``) for the validation.

Usage (from the project root)::

    .venv/Scripts/python.exe bench/tools_bench.py --work-dir DIR            # everything (~30-40 minutes)
    .venv/Scripts/python.exe bench/tools_bench.py --only stats_1,diff_key --repeat 1
    .venv/Scripts/python.exe bench/tools_bench.py --smoke --data-dir D --results-dir R   # tiny files
    .venv/Scripts/python.exe bench/tools_bench.py --report bench/results/tools-<ts>.json   # the Markdown again
    .venv/Scripts/python.exe bench/tools_bench.py --list

Results: ``<results-dir>/tools-<YYYYMMDD-HHMMSS>.json`` / ``.md`` and, for a full run, ``tools_latest.md`` (Uzbek,
``--lang en``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
for _p in (str(ROOT), str(BENCH)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_bench as rb  # noqa: E402  (machine info, run_child, quiet wait, formatting, the table)

from jsonreshape import i18n  # noqa: E402
from jsonreshape.i18n import get_language, set_language, t  # noqa: E402

# ----------------------------------------------------------------------------------
# texts (Uzbek primary, English): a developer tool -- registered into i18n.MESSAGES at import like
# bench/run_bench.py does (a key jsonreshape/i18n.py defines itself wins)
# ----------------------------------------------------------------------------------
TOOLS_MESSAGES: dict[str, dict[str, str]] = {
    "bench.tools.log.prepare": {"uz": "Kirish fayli tayyorlanmoqda: {name}", "en": "Preparing the input {name}"},
    "bench.tools.log.run": {
        "uz": "[{n}/{total}] {name} {i}/{repeats}: {seconds} ({rate}), jami {total_s}, RAM {peak}, jarayon {wall}",
        "en": "[{n}/{total}] {name} {i}/{repeats}: {seconds} ({rate}), {total_s} in all, RAM {peak}, process {wall}",
    },
    "bench.tools.log.failed": {"uz": "[{n}/{total}] {name} {i}/{repeats}: XATO: {error}",
                               "en": "[{n}/{total}] {name} {i}/{repeats}: FAILED: {error}"},
    "bench.tools.error.unknown_op": {"uz": "Noma'lum o'lchov(lar): {names}. Mavjudlari: {known}",
                                     "en": "Unknown measurement(s): {names}. Known: {known}"},
    "bench.tools.md.title": {"uz": "jsonreshape: 5-bosqich asboblari benchmarki (AGENT_PROMPT §9–§10, §12)",
                             "en": "jsonreshape: benchmark of the stage-5 tools (AGENT_PROMPT §9–§10, §12)"},
    "bench.tools.md.main": {"uz": "Asboblar (syn_2g.json, 2.1 GB)", "en": "Tools (syn_2g.json, 2.1 GB)"},
    "bench.tools.md.legend": {
        "uz": "✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. Vaqt — eng "
              "yaxshi takror: buyruqning indeksdan keyingi qismi (tezlik = kirish fayl(lar)i baytlari / shu vaqt); "
              "«jami» — buyruq butunligicha, indeks bilan. RAM — barcha takrorlarning eng kattasi, butun dastur "
              "(ishchi jarayonlar bilan); MB = 10⁶ bayt.",
        "en": "✅ target met · ❌ missed (cause and options below) · — no target. Time: the best run, the command after "
              "its index (rate = bytes of the input file(s) / that time); \"in all\": the whole command, index "
              "included. RAM: the largest of all runs, the whole program (worker processes included); MB = 10⁶ bytes.",
    },
    "bench.tools.target": {"uz": "dastur < 500 MB (§12); tezlik — §12 da yo'q",
                           "en": "program < 500 MB (§12); speed — not in §12"},
    "bench.tools.res": {
        "uz": "{rate} ({seconds}; jami {total}, shundan indeks {index}); RAM {peak}{detail}",
        "en": "{rate} ({seconds}; {total} in all, {index} of it the index); RAM {peak}{detail}",
    },
    "bench.tools.res.no_index": {
        "uz": "{rate} ({seconds}; indeks kerak emas); RAM {peak}{detail}",
        "en": "{rate} ({seconds}; no index needed); RAM {peak}{detail}",
    },
    "bench.tools.failed": {"uz": "XATO: {error}", "en": "FAILED: {error}"},
    "bench.tools.single": {"uz": " — bir marta o'lchandi (juda sekin)", "en": " — measured once (very slow)"},
    # rows
    "bench.tools.row.stats_1": {"uz": "Statistika, 1 ta maydon: `stats --field contact.address.city`",
                                "en": "Statistics, 1 field: `stats --field contact.address.city`"},
    "bench.tools.row.stats_4": {
        "uz": "Statistika, 4 ta maydon: `stats --field name --field score --field contact.address.city --field "
              "\"orders[*].sum\"`",
        "en": "Statistics, 4 fields: `stats --field name --field score --field contact.address.city --field "
              "\"orders[*].sum\"`",
    },
    "bench.tools.row.stats_exact": {
        "uz": "Statistika, aniq hisoblash (deyarli takrorlanmaydigan maydon): `stats --field contact.phone --exact`",
        "en": "Statistics, exact counting (an almost unique field): `stats --field contact.phone --exact`",
    },
    "bench.tools.row.dupes": {"uz": "Dublikatlar: `dupes --key contact.phone`",
                              "en": "Duplicates: `dupes --key contact.phone`"},
    "bench.tools.row.schema_gen": {"uz": "JSON Schema yaratish, barcha yozuvlardan: `schema IN OUT`",
                                   "en": "JSON Schema generation from every record: `schema IN OUT`"},
    "bench.tools.row.validate": {"uz": "JSON Schema bo'yicha tekshirish: `validate --schema syn_2g.schema.json`",
                                 "en": "Validation against the JSON Schema: `validate --schema syn_2g.schema.json`"},
    "bench.tools.row.split_copy": {"uz": "Bo'lish, 4 qism, bayt nusxa (o'sha format): `split --parts 4`",
                                   "en": "Split into 4 parts, byte copy (same format): `split --parts 4`"},
    "bench.tools.row.split_jsonl": {"uz": "Bo'lish, 4 qism, JSON Lines'ga: `split --parts 4 --to jsonl`",
                                    "en": "Split into 4 parts, to JSON Lines: `split --parts 4 --to jsonl`"},
    "bench.tools.row.merge_dedupe": {
        "uz": "Birlashtirish, dublikatlarni tozalash bilan (syn_2g.json + uning yangi versiyasi, 4.3 GB): "
              "`merge A B -o OUT --dedupe-by id`",
        "en": "Merge with dedupe (syn_2g.json + its new version, 4.3 GB): `merge A B -o OUT --dedupe-by id`",
    },
    "bench.tools.row.extract": {"uz": "Ajratish, har yozuvdan yig'ish: `extract IN OUT.jsonl --gather \"orders[*]\"`",
                                "en": "Extract, gathered from every record: `extract IN OUT.jsonl --gather \"orders[*]\"`"},
    "bench.tools.row.repair_clean": {"uz": "Ta'mirlash, buzilmagan fayl: `repair IN OUT`",
                                     "en": "Repair of a clean file: `repair IN OUT`"},
    "bench.tools.row.repair_broken": {
        "uz": "Ta'mirlash, yozuvlarning 1 % i buzilgan (NaN, ortiqcha vergul, bitta tirnoq, tirnoqsiz kalit, izoh): "
              "`repair IN OUT`",
        "en": "Repair, 1 % of the records broken (NaN, trailing comma, single quotes, unquoted key, comment): "
              "`repair IN OUT`",
    },
    "bench.tools.row.diff_key": {
        "uz": "Solishtirish kalit bo'yicha (2.1 GB va uning yangi versiyasi): `diff A B --key id --summary`",
        "en": "Diff by key (2.1 GB and its new version): `diff A B --key id --summary`",
    },
    "bench.tools.row.diff_position": {
        "uz": "Solishtirish o'rin bo'yicha (2.1 GB, 1 % yozuv o'zgargan): `diff A B --by-position --summary`",
        "en": "Diff by position (2.1 GB, 1 % of the records changed): `diff A B --by-position --summary`",
    },
    "bench.tools.row.diff_align": {
        "uz": "Solishtirish mazmun bo'yicha tekislab (~190 MiB fayllar: kalitsiz tekislash chegarasi 200 MiB): "
              "`diff A B --align --summary`",
        "en": "Diff aligned by content (~190 MiB files: the limit for aligning without a key is 200 MiB): "
              "`diff A B --align --summary`",
    },
    # details of the rows
    "bench.tools.d.stats": {"uz": "{path}: {distinct} xil{approx}, {present} ta qiymat",
                            "en": "{path}: {distinct} distinct{approx}, {present} values"},
    "bench.tools.d.approx": {"uz": " (≈)", "en": " (≈)"},
    "bench.tools.d.dupes": {"uz": "{groups} ta guruh, {duplicates} ta dublikat",
                            "en": "{groups} groups, {duplicates} duplicates"},
    "bench.tools.d.validate": {"uz": "{scanned} ta yozuv tekshirildi, {issues} ta xato",
                               "en": "{scanned} records checked, {issues} issues"},
    "bench.tools.d.split": {"uz": "{parts} ta qism, {records} ta yozuv, {out}",
                            "en": "{parts} parts, {records} records, {out}"},
    "bench.tools.d.merge": {"uz": "{records} ta yozuv yozildi, {dupes} ta dublikat tashlandi, {out}",
                            "en": "{records} records written, {dupes} duplicates dropped, {out}"},
    "bench.tools.d.extract": {"uz": "{records} ta qiymat, {out}", "en": "{records} values, {out}"},
    "bench.tools.d.repair": {"uz": "{records} ta yozuv, {fixes} ta tuzatish, {out}",
                             "en": "{records} records, {fixes} fixes, {out}"},
    "bench.tools.d.diff": {
        "uz": "{equal} ta bir xil, {changed} ta o'zgargan, {added} ta qo'shilgan, {removed} ta o'chirilgan",
        "en": "{equal} equal, {changed} changed, {added} added, {removed} removed",
    },
    "bench.tools.d.schema": {"uz": "sxema {out}", "en": "schema {out}"},
    # misses
    "bench.tools.miss.memory.cause": {
        "uz": "butun dasturning eng katta xotirasi {peak} ({procs} ta jarayon).",
        "en": "the whole program's largest memory was {peak} ({procs} processes).",
    },
    "bench.tools.miss.memory.options": {
        "uz": "JSON natijalaridagi jarayonlar bo'yicha peak'larni ko'rib, xotirani ushlab turgan bosqichni topish; "
              "--memory-limit bilan byudjetni kamaytirish.",
        "en": "see the per-process peaks in the JSON results to find the phase holding the memory; lower the budget "
              "with --memory-limit.",
    },
    "bench.tools.miss.failed.cause": {"uz": "buyruq xato bilan tugadi: {error}", "en": "the command failed: {error}"},
    "bench.tools.miss.failed.options": {
        "uz": "JSON natijalaridagi xato matnini ko'rib, tuzatib, qayta o'lchash (--only {op}).",
        "en": "read the error in the JSON results, fix it and measure again (--only {op}).",
    },
    # conditions
    "bench.tools.cond.process": {
        "uz": "Har bir buyruq alohida yangi jarayonda, yangi bo'sh JSONRESHAPE_HOME bilan (kesh indeksi yo'q), "
              "foydalanuvchi ishga tushirgandek: `bench/ops.py cli` jarayon ichida `jsonreshape.cli.main` ni "
              "chaqiradi (Document.open, indeks, amal, xulosa; ishchi jarayonlarni dastur o'zi tanlaydi). Indeks "
              "vaqti `Document.build_index` dan o'lchanadi (diff va merge'da ikkala fayl).",
        "en": "Every command ran in a new process with a new empty JSONRESHAPE_HOME (no cached index), as the user "
              "runs it: `bench/ops.py cli` calls `jsonreshape.cli.main` in the process (Document.open, the index, "
              "the operation, the summary; the program chooses its worker processes). The index time is measured "
              "in `Document.build_index` (both files for diff and merge).",
    },
    "bench.tools.cond.memory": {
        "uz": "RAM — butun dastur: har jarayonning aniq peak ishchi to'plami va jarayonlar daraxtining bir paytdagi "
              "yig'indisi (har {poll} ms namuna; venv ishga tushiruvchisi kirmaydi) — qaysi biri katta bo'lsa "
              "(run_bench.program_peak). Jarayon tegib o'tgan mmap sahifalari (indeks, fayl) ham kiradi.",
        "en": "RAM: the whole program: the exact peak working set of every process and the sum over the process "
              "tree at one instant (sampled every {poll} ms; the venv launcher left out), whichever is larger "
              "(run_bench.program_peak). The mapped pages the process touched (index, file) count too.",
    },
    "bench.tools.cond.repeat": {
        "uz": "Takrorlar: {repeats} (bir martalik yugurish {slow} s dan oshsa — bir marta); eng yaxshisi olinadi.",
        "en": "Runs: {repeats} (a run over {slow} s is not repeated); the best counts.",
    },
    "bench.tools.cond.load": {
        "uz": "Har o'lchovdan oldin tizim CPU yuklamasi ≤ {limit}% bo'lguncha kutildi (ko'pi bilan {timeout} s); "
              "ko'rilgan yuklama: {min}–{max}%. O'lchovlar paytida kompyuterda boshqa og'ir ish yo'q edi.",
        "en": "Before every run the system CPU load had to drop to ≤ {limit}% (waiting at most {timeout} s); load "
              "seen: {min}–{max}%. Nothing else heavy ran on the machine during the runs.",
    },
    "bench.tools.cond.inputs": {
        "uz": "Tayyorlangan kirishlar (o'lchanmaydi, bir marta, ish papkasida): yangi versiya — har 100-yozuvning "
              "`score` qiymati o'zgargan, har 997-yozuv o'chirilgan, har 1000-yozuv oldiga yangi yozuv qo'shilgan; "
              "o'rin bo'yicha solishtirish uchun — faqat `score` o'zgargan; ta'mirlash uchun — har 100-yozuv buzilgan "
              "(5 xil buzilish navbat bilan); tekislab solishtirish uchun — syn_2g.json'ning birinchi ~190 MiB i va "
              "uning yangi versiyasi.",
        "en": "Prepared inputs (unmeasured, once, in the work folder): the new version — every 100th record's `score` "
              "changed, every 997th record removed, a new record inserted before every 1000th; for the diff by "
              "position — `score` changed only; for repair — every 100th record broken (5 kinds in turn); for the "
              "aligned diff — the first ~190 MiB of syn_2g.json and its new version.",
    },
    "bench.tools.cond.cache": {
        "uz": "Ma'lumot fayllari OS keshida (iliq kesh; {ram} RAM). Chiqishlar va tayyorlangan kirishlar — {work} "
              "diskida.",
        "en": "The data files are in the OS cache (warm; {ram} of RAM). Outputs and prepared inputs are on drive "
              "{work}.",
    },
    "bench.tools.nm.gui": {
        "uz": "GUI'dagi vaqtlar (asboblar oynalari shu funksiyalarni fon vazifalarida chaqiradi).",
        "en": "Times in the GUI (the tool windows call the same functions in background tasks).",
    },
    "bench.tools.md.rerun": {
        "uz": "Qayta o'lchash: `.venv/Scripts/python.exe bench/tools_bench.py --work-dir PAPKA` (to'liq) yoki "
              "`--only NOM` (`--list`); faqat hisobot: `--report NATIJA.json`.",
        "en": "Measure again: `.venv/Scripts/python.exe bench/tools_bench.py --work-dir DIR` (full) or `--only NAME` "
              "(`--list`); the report only: `--report RESULTS.json`.",
    },
}


def _register_messages() -> None:
    for key, entry in TOOLS_MESSAGES.items():
        i18n.MESSAGES.setdefault(key, entry)


_register_messages()

RESULTS_VERSION = 1
DATA_DIR = BENCH / "data"
RESULTS_DIR = BENCH / "results"
OPS_SCRIPT = BENCH / "ops.py"
PYTHON = sys.executable
T_TOTAL_RSS = rb.T_TOTAL_RSS
MAX_REPEAT_SECONDS = 400.0
ALIGN_PART_BYTES = 190 * (1 << 20)     # under Settings.diff_align_max_bytes (200 MiB)


# ----------------------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------------------
def _record_lines(src: Path) -> Iterator[bytes]:
    """The records of a make_synthetic record array (``[`` + one record per line joined by ``,\\n`` + ``]``)."""
    with open(src, "rb", buffering=16 << 20) as f:
        for line in f:
            line = line.rstrip(b"\r\n")
            if line in (b"[", b"]", b""):
                continue
            if line.endswith(b","):
                line = line[:-1]
            yield line


_SCORE = re.compile(rb'"score":(-?[0-9][0-9.eE+-]*)')
_NAME = re.compile(rb'"name":"([^"\\\']*)"')


def _changed_score(rec: bytes) -> bytes:
    return _SCORE.sub(b'"score":-1.25', rec, count=1)


def _broken(rec: bytes, i: int) -> bytes:
    """Record ``rec`` with one defect of kind ``(i // 100) % 5`` (all repairable)."""
    kind = (i // 100) % 5
    if kind == 0:
        return _SCORE.sub(b'"score":NaN', rec, count=1)
    if kind == 1:
        return rec[:-1] + b",}"                                     # a trailing comma
    if kind == 2:
        m = _NAME.search(rec)
        if m is not None:                                          # single quotes (a name without ' or \)
            return rec[:m.start()] + b"'name':'" + m.group(1) + b"'" + rec[m.end():]
        return rec[:-1] + b",}"
    if kind == 3:
        return rec.replace(b'"id":', b"id:", 1)                     # an unquoted key
    return b"// izoh " + str(i).encode() + b"\n" + rec              # a comment line before the record


def _new_record(i: int) -> bytes:
    return (b'{"id":' + str(100_000_000 + i).encode() + b',"name":"Yangi yozuv","active":false,"score":0.1,'
            b'"created_at":1700000000,"contact":{"phone":"+998900000000","address":{"city":"Toshkent",'
            b'"street":"Yangi ko\'cha","zip":100000}},"tags":["yangi"],"orders":[]}')


class _ArrayWriter:
    """A record array written one record per line (the make_synthetic layout)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tmp = path.with_name(path.name + ".part")
        self.f = open(self.tmp, "wb", buffering=16 << 20)
        self.f.write(b"[\n")
        self.n = 0
        self.size = 2

    def add(self, rec: bytes) -> None:
        piece = (b",\n" if self.n else b"") + rec
        self.f.write(piece)
        self.size += len(piece)
        self.n += 1

    def close(self) -> None:
        self.f.write(b"\n]\n")
        self.f.close()
        os.replace(self.tmp, self.path)


def prepare_variants(src: Path, work: Path, *, align_bytes: int = ALIGN_PART_BYTES) -> dict[str, Path]:
    """Write the prepared inputs (see the module docstring) in one pass over ``src``; existing ones are kept."""
    names = {"changed": "syn_2g_changed.json", "inplace": "syn_2g_inplace.json", "broken1": "syn_2g_broken1.json",
             "a190": "syn_190m_a.json", "b190": "syn_190m_b.json"}
    paths = {k: work / v for k, v in names.items()}
    if all(p.exists() for p in paths.values()):
        return paths
    work.mkdir(parents=True, exist_ok=True)
    for name in names.values():
        _log(t("bench.tools.log.prepare", name=name))
    w = {k: _ArrayWriter(p) for k, p in paths.items()}
    in_part = True
    for i, rec in enumerate(_record_lines(src)):
        changed = _changed_score(rec) if i % 100 == 7 else rec
        w["inplace"].add(changed)
        w["broken1"].add(_broken(rec, i) if i % 100 == 0 else rec)
        new = _new_record(i) if i % 1000 == 500 else None
        removed = i % 997 == 13
        if new is not None:
            w["changed"].add(new)
        if not removed:
            w["changed"].add(changed)
        if in_part:
            if w["a190"].size + len(rec) + 2 > align_bytes or w["b190"].size + len(rec) + 600 > align_bytes:
                in_part = False
            else:
                w["a190"].add(rec)
                if new is not None:
                    w["b190"].add(new)
                if not removed:
                    w["b190"].add(changed)
    for x in w.values():
        x.close()
    return paths


# ----------------------------------------------------------------------------------
# measurements
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolSpec:
    """One measured command: ``jsonreshape ARGS`` with ``{in}`` (the first input), ``{in2}`` (the second),
    ``{out}`` (a file of ``out_ext`` or, with ``out_ext="dir"``, a folder) and ``{schema}`` replaced."""

    name: str
    inputs: tuple[str, ...]          # data keys: "syn_2g" or a prepared input
    args: tuple[str, ...]
    out_ext: str | None = None
    expect: str = "0"
    capture: str = ""
    detail: str = ""                 # how the row describes what the command did
    repeats: int = 2
    groups: tuple[str, ...] = ("full",)


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("stats_1", ("syn_2g",), ("stats", "{in}", "--field", "contact.address.city", "-q"),
             capture="stats:field_stats", detail="stats"),
    ToolSpec("stats_4", ("syn_2g",), ("stats", "{in}", "--field", "name", "--field", "score", "--field",
                                      "contact.address.city", "--field", "orders[*].sum", "-q"),
             capture="stats:field_stats", detail="stats"),
    ToolSpec("stats_exact", ("syn_2g",), ("stats", "{in}", "--field", "contact.phone", "--exact", "-q"),
             capture="stats:field_stats", detail="stats"),
    ToolSpec("dupes", ("syn_2g",), ("dupes", "{in}", "--key", "contact.phone", "-q"),
             capture="dupes:find_duplicates", detail="dupes"),
    ToolSpec("schema_gen", ("syn_2g",), ("schema", "{in}", "{out}", "-q"), out_ext=".schema.json", detail="schema"),
    ToolSpec("validate", ("syn_2g",), ("validate", "{in}", "--schema", "{schema}", "--max-issues", "0", "-q"),
             capture="jschema:validate", detail="validate"),
    ToolSpec("split_copy", ("syn_2g",), ("split", "{in}", "--parts", "4", "-o", "{out}", "-q"), out_ext="dir",
             capture="filetools:split", detail="split"),
    ToolSpec("split_jsonl", ("syn_2g",), ("split", "{in}", "--parts", "4", "--to", "jsonl", "-o", "{out}", "-q"),
             out_ext="dir", capture="filetools:split", detail="split"),
    ToolSpec("merge_dedupe", ("syn_2g", "changed"), ("merge", "{in}", "{in2}", "-o", "{out}", "--dedupe-by", "id",
                                                    "-q"),
             out_ext=".json", capture="filetools:merge", detail="merge"),
    ToolSpec("extract", ("syn_2g",), ("extract", "{in}", "{out}", "--gather", "orders[*]", "-q"), out_ext=".jsonl",
             capture="filetools:extract", detail="extract"),
    ToolSpec("repair_clean", ("syn_2g",), ("repair", "{in}", "{out}", "-q"), out_ext=".json",
             capture="repair:repair_file", detail="repair"),
    ToolSpec("repair_broken", ("broken1",), ("repair", "{in}", "{out}", "-q"), out_ext=".json",
             capture="repair:repair_file", detail="repair"),
    ToolSpec("diff_key", ("syn_2g", "changed"), ("diff", "{in}", "{in2}", "--key", "id", "--summary", "-q"),
             capture="cli:_diff_summary@0", detail="diff"),
    ToolSpec("diff_position", ("syn_2g", "inplace"), ("diff", "{in}", "{in2}", "--by-position", "--summary", "-q"),
             capture="cli:_diff_summary@0", detail="diff"),
    ToolSpec("diff_align", ("a190", "b190"), ("diff", "{in}", "{in2}", "--align", "--summary", "-q"),
             capture="cli:_diff_summary@0", detail="diff"),
)
SPEC_BY_NAME = {s.name: s for s in SPECS}
NEEDS_VARIANTS = {"changed", "inplace", "broken1", "a190", "b190"}


def select_specs(only: list[str] | None = None) -> list[ToolSpec]:
    if not only:
        return list(SPECS)
    unknown = [n for n in only if n not in SPEC_BY_NAME]
    if unknown:
        raise SystemExit(t("bench.tools.error.unknown_op", names=", ".join(unknown), known=", ".join(SPEC_BY_NAME)))
    return [s for s in SPECS if s.name in only]


@dataclass
class ToolsConfig:
    only: list[str] | None = None
    smoke: bool = False
    data_dir: Path = DATA_DIR
    results_dir: Path = RESULTS_DIR
    work_dir: Path | None = None
    repeat: int | None = None
    max_repeat_seconds: float = MAX_REPEAT_SECONDS
    wait: bool = True
    quiet_load: float = rb.QUIET_LOAD
    quiet_timeout: float = rb.QUIET_TIMEOUT
    keep_work: bool = False
    lang: str = "uz"
    timeout: float = rb.OP_TIMEOUT
    write_latest: bool | None = None
    command: list[str] = field(default_factory=list)


def _log(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _prepare_schema(data: Path, target: Path, home: Path, timeout: float) -> None:
    """``jsonreshape schema DATA TARGET`` once (unmeasured), in a fresh home."""
    if target.exists():
        return
    _log(t("bench.tools.log.prepare", name=target.name))
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", JSONRESHAPE_HOME=str(home))
    res = rb.run_child([PYTHON, str(OPS_SCRIPT), "cli", str(data), str(target), "--keep", "--", "schema", "{in}",
                        "{out}", "-q"], env, timeout=timeout)
    shutil.rmtree(home, ignore_errors=True)
    if not res.get("ok") or not target.exists():
        raise SystemExit(f"could not prepare {target.name}: {res.get('error')}")


def _command(spec: ToolSpec, paths: dict[str, Path], out: Path, schema: Path | None) -> list[str]:
    main = paths[spec.inputs[0]]
    others = [str(paths[k]) for k in spec.inputs[1:]]
    args = []
    for a in spec.args:
        if "{in2}" in a:
            a = a.replace("{in2}", others[0])
        if "{schema}" in a and schema is not None:
            a = a.replace("{schema}", str(schema))
        args.append(a)
    cmd = [PYTHON, str(OPS_SCRIPT), "cli", str(main), str(out), "--expect", spec.expect]
    if others:
        cmd += ["--inputs", ",".join(others)]
    if spec.capture:
        cmd += ["--capture", spec.capture]
    if spec.out_ext == "dir":
        cmd.append("--mkdir")
    return cmd + ["--", *args]


def run(cfg: ToolsConfig) -> dict[str, Any]:
    old = get_language()
    set_language(cfg.lang)
    try:
        return _run(cfg)
    finally:
        set_language(old)


def _run(cfg: ToolsConfig) -> dict[str, Any]:
    started = datetime.now()
    run_id = started.strftime("%Y%m%d-%H%M%S")
    specs = select_specs(cfg.only)
    data_dir = Path(cfg.data_dir).resolve()
    results_dir = Path(cfg.results_dir).resolve()
    work = Path(cfg.work_dir).resolve() if cfg.work_dir else data_dir / "work" / f"tools-{run_id}"
    work.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"version": RESULTS_VERSION, "kind": "tools", "run_id": run_id,
                               "mode": "smoke" if cfg.smoke else ("only" if cfg.only else "full"), "only": cfg.only,
                               "started": started.isoformat(timespec="seconds"), "command": cfg.command,
                               "lang": cfg.lang}
    results["machine"] = rb.machine_info(data_dir)
    results["conditions"] = {"quiet_load_limit": cfg.quiet_load, "quiet_timeout": cfg.quiet_timeout,
                             "wait_for_quiet": cfg.wait, "repeat_override": cfg.repeat,
                             "max_repeat_seconds": cfg.max_repeat_seconds, "poll_seconds": rb.POLL_SECONDS,
                             "data_dir": str(data_dir), "work_dir": str(work), "fresh_home_per_run": True}
    data_info = rb.ensure_data(rb.DATA_SPECS["syn_2g"], data_dir, cfg.smoke)
    results["data"] = {"syn_2g": data_info}
    src = Path(data_info["path"])
    paths: dict[str, Path] = {"syn_2g": src}
    loads: list[dict[str, Any]] = []
    op_results: dict[str, Any] = {}
    try:
        needed = {k for s in specs for k in s.inputs}
        prepared = work / "inputs"
        if needed & NEEDS_VARIANTS:
            t0 = time.perf_counter()
            variants = prepare_variants(src, prepared,
                                        align_bytes=ALIGN_PART_BYTES if not cfg.smoke else 200_000)
            paths.update(variants)
            results["prepared"] = {k: {"path": str(p), "bytes": p.stat().st_size} for k, p in variants.items()}
            results["prepared_seconds"] = round(time.perf_counter() - t0, 1)
        schema: Path | None = None
        if any("{schema}" in a for s in specs for a in s.args):
            schema = prepared / "syn_2g.schema.json"
            prepared.mkdir(parents=True, exist_ok=True)
            _prepare_schema(src, schema, work / "homes" / "prepare-schema", cfg.timeout)
        total = len(specs)
        for n, spec in enumerate(specs, 1):
            repeats = max(1, cfg.repeat if cfg.repeat is not None else (1 if cfg.smoke else spec.repeats))
            runs: list[dict[str, Any]] = []
            for i in range(repeats):
                quiet = rb.wait_quiet(cfg.quiet_load, cfg.quiet_timeout) if cfg.wait else None
                if quiet is not None:
                    loads.append(quiet)
                home = work / "homes" / f"{spec.name}-{i + 1}"
                shutil.rmtree(home, ignore_errors=True)
                home.mkdir(parents=True, exist_ok=True)
                out = work / "out" / (f"{spec.name}-{i + 1}" if spec.out_ext in (None, "dir")
                                      else f"{spec.name}-{i + 1}{spec.out_ext}")
                if spec.out_ext is None:
                    out = work / "out" / f"{spec.name}-{i + 1}.unused"
                out.parent.mkdir(parents=True, exist_ok=True)
                env = dict(os.environ)
                env.update({"PYTHONIOENCODING": "utf-8", "JSONRESHAPE_LANG": cfg.lang, "JSONRESHAPE_HOME": str(home)})
                res = rb.run_child(_command(spec, paths, out, schema), env, timeout=cfg.timeout)
                res["quiet"] = quiet
                res["run"] = i + 1
                runs.append(res)
                _log(_progress_line(n, total, spec, i + 1, repeats, res))
                if not cfg.keep_work:
                    shutil.rmtree(home, ignore_errors=True)
                if not res.get("ok"):
                    break
                child = res.get("child") or {}
                if float(child.get("total_seconds") or 0.0) > cfg.max_repeat_seconds:
                    break
            op_results[spec.name] = summarize(spec, runs)
    finally:
        if not cfg.keep_work:
            shutil.rmtree(work, ignore_errors=True)
            try:
                (data_dir / "work").rmdir()
            except OSError:
                pass
    finished = datetime.now()
    results["ops"] = op_results
    results["finished"] = finished.isoformat(timespec="seconds")
    results["duration_seconds"] = round((finished - started).total_seconds(), 1)
    if loads:
        values = [x["load"] for x in loads]
        results["conditions"]["load_before"] = {"min": min(values), "max": max(values),
                                                "avg": round(sum(values) / len(values), 1)}
    rows = build_rows(results)
    results["rows"] = [asdict(r) for r in rows]
    results["all_ok"] = bool(op_results) and all(o["ok"] for o in op_results.values())
    results["files"] = write_results(results, rows, results_dir, cfg)
    return results


def summarize(spec: ToolSpec, runs: list[dict[str, Any]]) -> dict[str, Any]:
    good = [r for r in runs if r.get("ok") and isinstance((r.get("child") or {}).get("seconds"), (int, float))]
    best = min(good, key=lambda r: r["child"]["seconds"]) if good else None

    def most(key: str) -> Any:
        vals = [r[key] for r in runs if r.get(key)]
        return max(vals) if vals else None

    return {
        "spec": asdict(spec),
        "runs": runs,
        "ok": bool(runs) and all(r.get("ok") for r in runs),
        "error": next((r.get("error") for r in runs if not r.get("ok")), None),
        "best": None if best is None else best["child"],
        "best_wall_seconds": None if best is None else best["wall_seconds"],
        "repeats_done": len(runs),
        "peak_rss": most("peak_rss"),
        "peak_private": most("peak_private"),
        "peak_rss_sum": most("peak_rss_sum"),
        "peak_private_sum": most("peak_private_sum"),
        "processes_max": most("processes_max"),
    }


def _progress_line(n: int, total: int, spec: ToolSpec, i: int, repeats: int, res: dict[str, Any]) -> str:
    if not res.get("ok"):
        return t("bench.tools.log.failed", n=n, total=total, name=spec.name, i=i, repeats=repeats,
                 error=res.get("error"))
    child = res.get("child") or {}
    return t("bench.tools.log.run", n=n, total=total, name=spec.name, i=i, repeats=repeats,
             seconds=rb.fmt_s(child.get("seconds")), rate=rb.fmt_rate(child.get("mb_per_s")),
             total_s=rb.fmt_s(child.get("total_seconds")), peak=rb.fmt_mb(rb.program_peak(res)),
             wall=rb.fmt_s(res.get("wall_seconds")))


# ----------------------------------------------------------------------------------
# rows / report
# ----------------------------------------------------------------------------------
def _first(captured: dict[str, Any] | None, prefix: str) -> Any:
    for key, vals in (captured or {}).items():
        if key.startswith(prefix) and vals:
            return vals[-1]
    return None


def _detail(spec: ToolSpec, b: dict[str, Any]) -> str:
    """What the command did, from the captured report (empty when it is not known)."""
    cap = b.get("captured") or {}
    out = rb.fmt_mb(b.get("bytes_written"))
    try:
        if spec.detail == "stats":
            items = _first(cap, "stats:") or []
            parts = [t("bench.tools.d.stats", path=x.get("field") or x.get("path"), distinct=rb.group(x.get("distinct")),
                       approx="" if x.get("distinct_exact") else t("bench.tools.d.approx"),
                       present=rb.group(x.get("values") or x.get("present"))) for x in items if isinstance(x, dict)]
            return "; ".join(parts)
        if spec.detail == "dupes":
            x = _first(cap, "dupes:") or {}
            return t("bench.tools.d.dupes", groups=rb.group(x.get("groups")), duplicates=rb.group(x.get("duplicates")))
        if spec.detail == "validate":
            x = _first(cap, "jschema:") or {}
            return t("bench.tools.d.validate", scanned=rb.group(x.get("scanned")), issues=rb.group(x.get("issue_count")))
        if spec.detail == "split":
            x = _first(cap, "filetools:") or {}
            return t("bench.tools.d.split", parts=b.get("files") or "?", records=rb.group(x.get("records")), out=out)
        if spec.detail == "merge":
            x = _first(cap, "filetools:") or {}
            return t("bench.tools.d.merge", records=rb.group(x.get("records")), dupes=rb.group(x.get("duplicates")),
                     out=out)
        if spec.detail == "extract":
            x = _first(cap, "filetools:") or {}
            return t("bench.tools.d.extract", records=rb.group(x.get("records")), out=out)
        if spec.detail == "repair":
            x = _first(cap, "repair:") or {}
            return t("bench.tools.d.repair", records=rb.group(x.get("records")), fixes=rb.group(x.get("total")),
                     out=out)
        if spec.detail == "diff":
            x = _first(cap, "cli:") or {}
            return t("bench.tools.d.diff", equal=rb.group(x.get("equal")), changed=rb.group(x.get("changed")),
                     added=rb.group(x.get("added")), removed=rb.group(x.get("removed")))
        if spec.detail == "schema":
            return t("bench.tools.d.schema", out=out)
    except Exception:  # noqa: BLE001 - a detail never breaks the report
        return ""
    return ""


def build_rows(results: dict[str, Any]) -> list[rb.Row]:
    ops = results.get("ops", {})
    rows: list[rb.Row] = []
    for spec in SPECS:
        o = ops.get(spec.name)
        if o is None:
            continue
        action = t(f"bench.tools.row.{spec.name}")
        target = t("bench.tools.target")
        b = o.get("best")
        if b is None:
            rows.append(rb.Row(spec.name, "main", action, target, t("bench.tools.failed", error=o.get("error") or "?"),
                               rb._status(False), False, {"error": o.get("error"), "op": spec.name}))
            continue
        pk = rb.program_peak(o)
        procs = o.get("processes_max") or 1
        if procs > 1 and o.get("peak_rss"):
            mem = t("bench.mem.procs", peak=rb.fmt_mb(pk), n=procs, single=rb.fmt_mb(o.get("peak_rss")),
                    private=rb.fmt_mb(o.get("peak_private_sum")))
        else:
            mem = rb.fmt_mb(pk)
        detail = _detail(spec, b)
        detail = f"; {detail}" if detail else ""
        if o.get("repeats_done") == 1 and float(b.get("total_seconds") or 0) > MAX_REPEAT_SECONDS:
            detail += t("bench.tools.single")
        key = "bench.tools.res" if b.get("indexes") else "bench.tools.res.no_index"
        result = t(key, rate=rb.fmt_rate(b.get("mb_per_s")), seconds=rb.fmt_s(b.get("seconds")),
                   total=rb.fmt_s(b.get("total_seconds")), index=rb.fmt_s(b.get("index_seconds")), peak=mem,
                   detail=detail)
        ok = pk is not None and pk < T_TOTAL_RSS
        rows.append(rb.Row(spec.name, "main", action, target, result, rb._status(ok), ok,
                           {"op": spec.name, "peak_rss": pk, "processes": procs, "seconds": b.get("seconds"),
                            "total_seconds": b.get("total_seconds"), "index_seconds": b.get("index_seconds"),
                            "mb_per_s": b.get("mb_per_s"), "input_bytes": b.get("input_bytes"),
                            "bytes_written": b.get("bytes_written")}))
    return rows


def miss_texts(row: rb.Row, results: dict[str, Any]) -> tuple[str, str]:
    if row.details.get("error"):
        return (t("bench.tools.miss.failed.cause", error=row.details["error"]),
                t("bench.tools.miss.failed.options", op=row.id))
    return (t("bench.tools.miss.memory.cause", peak=rb.fmt_mb(row.details.get("peak_rss")),
              procs=row.details.get("processes") or 1),
            t("bench.tools.miss.memory.options"))


def render_markdown(results: dict[str, Any], rows: list[rb.Row], json_name: str | None = None) -> str:
    m = results.get("machine", {})
    cond = results.get("conditions", {})
    out: list[str] = [f"# {t('bench.tools.md.title')}", ""]
    out.append(t("bench.md.meta", date=str(results.get("started", "")).replace("T", " "),
                 duration=rb.fmt_s(results.get("duration_seconds")), mode=results.get("mode"), json=json_name or "?"))
    out.append("")
    if results.get("mode") != "full":
        out += ["> " + t("bench.md.partial", mode=results.get("mode")), ""]
    out += [f"## {t('bench.tools.md.main')}", ""] + rb._table(rows) + ["", t("bench.tools.md.legend"), ""]
    misses = [r for r in rows if r.ok is False]
    out += [f"## {t('bench.md.misses')}", ""]
    if not misses:
        out.append(t("bench.md.no_misses"))
    for r in misses:
        cause, options = miss_texts(r, results)
        out.append(f"- **{r.action}** — {r.result} ({t('bench.col.target').lower()}: {r.target}).")
        out.append(f"  - {t('bench.md.cause')}: {cause}")
        out.append(f"  - {t('bench.md.options')}: {options}")
    out.append("")
    out += [f"## {t('bench.md.data')}", ""]
    counts = [(o.get("best") or {}).get("record_count") for o in results.get("ops", {}).values()
              if (o.get("best") or {}).get("indexes") == 1 and list(o["spec"]["inputs"])[:1] == ["syn_2g"]]
    for key, info in (results.get("data") or {}).items():
        records = info.get("records") or next((c for c in counts if c), None)
        out.append("- " + t("bench.md.data_line", name=Path(info.get("path", key)).name, size=rb.fmt_mb(info.get("bytes")),
                             records=rb.group(records),
                             how=t("bench.md.generated") if info.get("generated") else t("bench.md.reused")))
    for key, info in (results.get("prepared") or {}).items():
        out.append(f"- `{Path(info['path']).name}`: {rb.fmt_mb(info.get('bytes'))}")
    out.append("")
    disk = m.get("disk") or {}
    split = t("bench.md.pe_split", p=m.get("p_cores"), e=m.get("e_cores")) if m.get("p_cores") else ""
    versions = m.get("versions") or {}
    out += [f"## {t('bench.md.machine')}", ""]
    out.append("- " + t("bench.md.cpu", cpu=m.get("cpu"), cores=m.get("cores_physical"), logical=m.get("cores_logical"),
                         split=split))
    out.append("- " + t("bench.md.ram", ram=rb.fmt_mb(m.get("ram_total"))))
    out.append("- " + t("bench.md.disk", model=disk.get("model") or "?", bus=disk.get("bus") or "?",
                         media=disk.get("media") or "?", drive=disk.get("drive") or "?", fs=disk.get("fs") or "?",
                         free=rb.fmt_mb(disk.get("free"))))
    out.append("- " + t("bench.md.os", os=m.get("os"), power=m.get("power_plan") or "?"))
    out.append("- " + t("bench.md.python", python=m.get("python"), backend=m.get("core_backend"),
                         orjson=versions.get("orjson") or "?", ijson=versions.get("ijson") or "?",
                         ijson_backend=m.get("ijson_backend") or "?", version=m.get("app_version") or "?"))
    out.append("")
    out += [f"## {t('bench.md.conditions')}", ""]
    out.append("- " + t("bench.tools.cond.process"))
    out.append("- " + t("bench.tools.cond.memory", poll=int(float(cond.get("poll_seconds") or rb.POLL_SECONDS) * 1000)))
    reps = sorted({o.get("repeats_done") or len(o.get("runs") or []) for o in results.get("ops", {}).values()})
    out.append("- " + t("bench.tools.cond.repeat", repeats="/".join(str(r) for r in reps) or "?",
                         slow=int(cond.get("max_repeat_seconds") or MAX_REPEAT_SECONDS)))
    lb = cond.get("load_before")
    if cond.get("wait_for_quiet") and lb:
        out.append("- " + t("bench.tools.cond.load", limit=cond.get("quiet_load_limit"),
                             timeout=cond.get("quiet_timeout"), min=lb.get("min"), max=lb.get("max")))
    elif not cond.get("wait_for_quiet"):
        out.append("- " + t("bench.cond.no_wait"))
    if results.get("prepared"):
        out.append("- " + t("bench.tools.cond.inputs"))
    work = str(cond.get("work_dir") or "?")
    out.append("- " + t("bench.tools.cond.cache", ram=rb.fmt_mb(m.get("ram_total")), work=work[:2]))
    out.append("")
    out += [f"## {t('bench.md.not_measured')}", ""]
    out.append("- " + t("bench.nm.laptop", cpu=m.get("cpu")))
    out.append("- " + t("bench.tools.nm.gui"))
    out += ["", t("bench.tools.md.rerun"), ""]
    return "\n".join(out)


def write_results(results: dict[str, Any], rows: list[rb.Row], results_dir: Path, cfg: ToolsConfig,
                  json_path: Path | None = None) -> dict[str, str]:
    results_dir.mkdir(parents=True, exist_ok=True)
    jpath = json_path or results_dir / f"tools-{results['run_id']}.json"
    mpath = jpath.with_suffix(".md")
    files = {"json": str(jpath), "md": str(mpath)}
    latest = cfg.write_latest if cfg.write_latest is not None else (results.get("mode") == "full")
    if latest:
        files["latest"] = str(results_dir / "tools_latest.md")
    results["files"] = files
    md = render_markdown(results, rows, jpath.name)
    if json_path is None:
        jpath.write_bytes((json.dumps(results, ensure_ascii=False, indent=1, default=str) + "\n").encode("utf-8"))
    mpath.write_bytes(md.encode("utf-8"))
    if latest:
        (results_dir / "tools_latest.md").write_bytes(md.encode("utf-8"))
    return files


# ----------------------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tools_bench.py", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--only", help="comma-separated measurement names (see --list)")
    ap.add_argument("--smoke", action="store_true", help="every command once on tiny files (machinery check)")
    ap.add_argument("--list", action="store_true", help="list the measurements and exit")
    ap.add_argument("--repeat", type=int, default=None, help="runs per measurement (default 2)")
    ap.add_argument("--max-repeat-seconds", type=float, default=MAX_REPEAT_SECONDS,
                    help="a run slower than this is not repeated")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--work-dir", type=Path, default=None)
    ap.add_argument("--quiet-load", type=float, default=rb.QUIET_LOAD, help="percent of all CPUs")
    ap.add_argument("--quiet-timeout", type=float, default=rb.QUIET_TIMEOUT, help="seconds")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for a quiet machine")
    ap.add_argument("--keep-work", action="store_true", help="keep the prepared inputs, homes and outputs")
    ap.add_argument("--timeout", type=float, default=rb.OP_TIMEOUT, help="seconds per run")
    ap.add_argument("--lang", choices=["uz", "en"], default="uz")
    ap.add_argument("--latest", action="store_true", help="also write tools_latest.md for a partial run")
    ap.add_argument("--report", type=Path, default=None, help="write the Markdown of an earlier results JSON again")
    return ap


def config_from_args(argv: list[str] | None = None) -> tuple[ToolsConfig, argparse.Namespace]:
    args = build_parser().parse_args(argv)
    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    cfg = ToolsConfig(only=only, smoke=args.smoke, data_dir=args.data_dir, results_dir=args.results_dir,
                      work_dir=args.work_dir, repeat=args.repeat, max_repeat_seconds=args.max_repeat_seconds,
                      wait=not (args.no_wait or args.smoke), quiet_load=args.quiet_load,
                      quiet_timeout=args.quiet_timeout, keep_work=args.keep_work, lang=args.lang,
                      timeout=args.timeout, write_latest=True if args.latest else None,
                      command=["tools_bench.py", *(argv if argv is not None else sys.argv[1:])])
    return cfg, args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg, args = config_from_args(argv)
    if args.list:
        for s in SPECS:
            sys.stdout.write(f"{s.name:15} {'+'.join(s.inputs):18} jsonreshape {' '.join(s.args)}\n")
        return 0
    if args.report is not None:
        set_language(cfg.lang)
        results = json.loads(Path(args.report).read_bytes().decode("utf-8"))
        rows = build_rows(results)
        files = write_results(results, rows, Path(cfg.results_dir), cfg, json_path=Path(args.report))
        sys.stdout.write(t("bench.log.written", json=files["json"], md=files["md"]) + "\n")
        return 0
    results = run(cfg)
    set_language(cfg.lang)
    for r in results["rows"]:
        sys.stdout.write(f"{r['status']} {r['action']}: {r['result']}\n")
    sys.stdout.write(t("bench.log.written", json=results["files"]["json"], md=results["files"]["md"]) + "\n")
    return 0 if results["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
