r"""Big-file benchmark runner (AGENT_PROMPT §12 table, §14 test 25).

Generates the data files when they are missing (``bench/make_synthetic.py``, deterministic):

* ``syn_2g.json``    -- ~2 GB record array (~6.5 million records of ~330 bytes);
* ``syn_10m.jsonl``  -- 10 million records, JSON Lines (~3.3 GB);
* ``node_1m.json``   -- one object whose ``"items"`` array has 1 000 000 elements;
* ``syn_5g.json``    -- ~5 GB record array: §12 states its targets for a 5 GB array file, so
  open, index and search are also measured on it (extra table);

then runs every operation of ``bench/ops.py`` in a fresh process with a fresh, empty
``JSONRESHAPE_HOME`` (no cached index unless the operation is "cached reopen"), measures
the wall time and the PEAK RSS of that process from outside, repeats timing-sensitive
operations (best of N) after waiting for a quiet machine, and writes

* ``bench/results/<YYYYMMDD-HHMMSS>.json`` -- machine, conditions, every run of every op;
* ``bench/results/<YYYYMMDD-HHMMSS>.md`` and, for a full run, ``bench/results/latest.md``
  -- the "Amal | Maqsad | Natija | Holat" table of AGENT_PROMPT §12 for the README (Uzbek
  by default, ``--lang en``), the extra measurements, every missed target with its cause
  and the options to fix it, the machine and the measurement conditions.

Usage (from the project root)::

    .venv/Scripts/python.exe bench/run_bench.py                 # full benchmark (~20 minutes)
    .venv/Scripts/python.exe bench/run_bench.py --subset 2g     # index/search/sort/export of the 2 GB file
    .venv/Scripts/python.exe bench/run_bench.py --subset 5g     # open/index/search of the 5 GB file
    .venv/Scripts/python.exe bench/run_bench.py --only index_2g,search_rare --repeat 1
    .venv/Scripts/python.exe bench/run_bench.py --smoke --data-dir D --results-dir R   # tiny files
    .venv/Scripts/python.exe bench/run_bench.py --list

Measurement
===========
* **Peak RSS** = the peak working set of the child process (Windows ``peak_wset``): the
  runner polls ``psutil`` every 20 ms until the process exits and finally reads the exact
  ``PeakWorkingSetSize`` from the still-open process handle; the child also reports its own
  peak at the end (POSIX: ``ru_maxrss``). The largest of these is used. Pages of the
  memory-mapped index that the process touched count, as they do in Task Manager. The peak
  private bytes (commit) are recorded too.
* **Time**: each operation reports its own internal timings (``bench/ops.py``); the table
  uses the operation's time (the index that search/filter/sort/export/schema build first in
  the same process is timed separately); the runner adds the process wall time.
* **What the user gets** (end to end): the §12 sort row is ``sort+export`` --
  ``Document.open`` + index + ``sort_records`` + the export of the sorted RecordSet to
  JSONL, until the output file is written (``total_seconds``), like ``jsonreshape sort``
  (AGENT_PROMPT §6.4: the sort result is exported in sorted order). That export reads the
  records in key order, i.e. from random positions of the file, which neither the plain
  ``sort`` (``sort_records`` only, now an extra "one phase" row) nor the file-order
  ``export`` shows. ``filter+export`` measures ``jsonreshape filter`` the same way (extra
  rows). ``tests/test_bench.py`` holds regression thresholds for both on the 2 GB file.
  Operations are repeated (``OpSpec.repeats``, ``--repeat``) and the best run counts; the
  memory of an operation is the largest peak over all its runs.
* **Quiet machine**: before every run the system CPU load is sampled with
  ``psutil.cpu_percent`` until it is at most ``--quiet-load`` percent (default 35) or
  ``--quiet-timeout`` seconds (default 60) have passed; the load seen before and during
  every run (the other processes' share, in cores) is recorded.
* **OS file cache**: the data files are normally in the OS cache (warm). ``cold_2g`` copies
  the 2 GB file with ``FILE_FLAG_NO_BUFFERING`` (the copy never enters the cache) and opens
  and indexes the copy; the read counter of the physical disk shows the bytes that really
  came from the disk. ``coldread_2g`` measures the raw unbuffered disk speed.

Big files and work files live under ``--data-dir`` (default ``bench/data``, gitignored):
per-run homes, export outputs and cold copies go to ``<data-dir>/work/<run id>`` and are
removed at the end (``--keep-work`` keeps them).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
for _p in (str(ROOT), str(BENCH)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import psutil  # noqa: E402

import ops  # noqa: E402  (bench/ops.py: cold_copy, disk_key)
from jsonreshape import i18n  # noqa: E402
from jsonreshape.i18n import get_language, has, set_language, t  # noqa: E402
from jsonreshape.tasks import format_bytes  # noqa: E402

# ----------------------------------------------------------------------------------
# texts (Uzbek primary, English fallback; every text goes through jsonreshape.i18n.t).
# They belong to this developer tool, not to the program, so they are registered into
# i18n.MESSAGES at import instead of living in the shipped dictionary; a key that
# jsonreshape/i18n.py defines itself wins.
# ----------------------------------------------------------------------------------
BENCH_MESSAGES: dict[str, dict[str, str]] = {
    # console
    "bench.log.machine": {"uz": "Kompyuter haqida ma'lumot yig'ilmoqda…", "en": "Collecting machine information…"},
    "bench.log.generate": {"uz": "Ma'lumot fayli yaratilmoqda: {name} ({args})",
                           "en": "Generating the data file {name} ({args})"},
    "bench.log.run": {
        "uz": "[{n}/{total}] {name} {i}/{repeats}: {metric} = {value}, {rate}, peak RSS {peak}, jarayon {wall} "
              "(yuklama: oldin {load}, davomida {during})",
        "en": "[{n}/{total}] {name} {i}/{repeats}: {metric} = {value}, {rate}, peak RSS {peak}, process {wall} "
              "(load: before {load}, during {during})",
    },
    "bench.log.failed": {"uz": "[{n}/{total}] {name} {i}/{repeats}: XATO: {error}",
                         "en": "[{n}/{total}] {name} {i}/{repeats}: FAILED: {error}"},
    "bench.log.written": {"uz": "Natijalar yozildi: {json}, {md}", "en": "Results written: {json}, {md}"},
    "bench.error.unknown_op": {"uz": "Noma'lum amal(lar): {names}. Mavjudlari: {known}",
                               "en": "Unknown operation(s): {names}. Known: {known}"},
    "bench.error.generate": {"uz": "{name} faylini yaratib bo'lmadi: {detail}",
                             "en": "Could not generate {name}: {detail}"},
    "bench.unit.minutes": {"uz": "{m} daq {s} s", "en": "{m} min {s} s"},
    "bench.strategy.memory": {"uz": "xotirada", "en": "in memory"},
    "bench.strategy.external": {"uz": "tashqi, diskda", "en": "external, on disk"},
    # table
    "bench.col.action": {"uz": "Amal", "en": "Operation"},
    "bench.col.target": {"uz": "Maqsad", "en": "Target"},
    "bench.col.result": {"uz": "Natija", "en": "Result"},
    "bench.col.status": {"uz": "Holat", "en": "Status"},
    "bench.target.open": {"uz": "< 1 s", "en": "< 1 s"},
    "bench.target.index": {"uz": "≥ 150 MB/s", "en": "≥ 150 MB/s"},
    "bench.target.node": {"uz": "< 200 ms (virtual)", "en": "< 200 ms (virtual)"},
    "bench.target.search": {"uz": "≥ 100 MB/s, natijalar darhol chiqa boshlaydi (birinchi natija < 1 s)",
                            "en": "≥ 100 MB/s, results start at once (first hit < 1 s)"},
    "bench.target.first_hit": {"uz": "birinchi natija < 1 s", "en": "first hit < 1 s"},
    "bench.target.sort": {"uz": "< 2 daqiqa, RAM < 1 GB", "en": "< 2 minutes, RAM < 1 GB"},
    "bench.target.export": {"uz": "≥ 50 MB/s, RAM < 300 MB", "en": "≥ 50 MB/s, RAM < 300 MB"},
    "bench.target.memory": {"uz": "< 500 MB", "en": "< 500 MB"},
    "bench.target.index_python": {"uz": "≥ 15 MB/s (ARCHITECTURE §1)", "en": "≥ 15 MB/s (ARCHITECTURE §1)"},
    "bench.target.none": {"uz": "— (§12 da yo'q)", "en": "— (not in §12)"},
    "bench.target.part": {"uz": "— (amalning bir bosqichi; §12 maqsadi butun amal uchun)",
                          "en": "— (one phase of the operation; the §12 target is for the whole operation)"},
    "bench.row.open": {"uz": "Ochish, birinchi {n} ta yozuv ko'rinishi ({file}, {size})",
                       "en": "Open, first {n} records visible ({file}, {size})"},
    "bench.row.index": {"uz": "To'liq indeks, Rust yadro ({file})", "en": "Full index, Rust core ({file})"},
    "bench.row.node": {"uz": "{count} bolali tugunni ochish ({file}, ichki massiv)",
                       "en": "Opening a node with {count} children ({file}, inner array)"},
    "bench.row.search_rare": {
        "uz": "To'liq matn qidiruvi, bayt rejimi: kam uchraydigan so'z «{text}» ({share}% yozuvda)",
        "en": "Full-text search, byte mode: rare word “{text}” (in {share}% of the records)",
    },
    "bench.row.search_common": {
        "uz": "To'liq matn qidiruvi, bayt rejimi: ko'p uchraydigan so'z «{text}» ({share}% yozuvda)",
        "en": "Full-text search, byte mode: common word “{text}” (in {share}% of the records)",
    },
    "bench.row.sort_export": {
        "uz": "{records} ta yozuvni bitta maydon (`{key}`, o'zbekcha tartib) bo'yicha tartiblash va natijani {fmt} "
              "faylga yozish ({file}): ochishdan natija fayli to'liq yozilguncha, `jsonreshape sort` kabi",
        "en": "Sorting {records} records by one field (`{key}`, Uzbek collation) and writing the sorted output to "
              "{fmt} ({file}): from opening until the output is written, like `jsonreshape sort`",
    },
    "bench.row.sort_only": {
        "uz": "Faqat tartiblash bosqichi (sort_records, natija yozilmaydi): {records} ta yozuv, `{key}` bo'yicha "
              "({file})",
        "en": "The sort phase only (sort_records, the output is not written): {records} records by `{key}` ({file})",
    },
    "bench.row.filter_export": {
        "uz": "Filtr `{expr}` va mos yozuvlarni {fmt} faylga yozish ({file}): ochishdan natija fayli to'liq "
              "yozilguncha, `jsonreshape filter` kabi",
        "en": "Filter `{expr}` and writing the matching records to {fmt} ({file}): from opening until the output is "
              "written, like `jsonreshape filter`",
    },
    "bench.row.export_jsonl": {"uz": "Eksport JSON → JSONL ({file}; mapping/tahrirlarsiz)",
                               "en": "Export JSON → JSONL ({file}; without mapping/edits)"},
    "bench.row.export_json2": {"uz": "Eksport JSON → JSON, indent 2 ({file}; mapping/tahrirlarsiz)",
                               "en": "Export JSON → JSON, indent 2 ({file}; without mapping/edits)"},
    "bench.row.memory": {"uz": "Dasturning umumiy xotirasi (tartiblashdan boshqa amallarning eng katta peak RSS)",
                         "en": "Overall program memory (largest peak RSS of the non-sort operations)"},
    "bench.row.reopen": {"uz": "Keshdagi indeks bilan qayta ochish, birinchi {n} ta yozuv",
                         "en": "Reopen with the cached index, first {n} records"},
    "bench.row.node_auto": {
        "uz": "Xuddi shu fayl avtomatik format bilan (items = yozuvlar yo'li): ochishdan tugun bolalari ko'ringuncha",
        "en": "The same file with automatic format detection (items = record path): from opening until the "
              "node's children are visible",
    },
    "bench.row.search_medium": {
        "uz": "Qidiruv, bayt rejimi: o'rtacha uchraydigan so'z «{text}» ({share}% yozuvda)",
        "en": "Search, byte mode: medium-frequency word “{text}” (in {share}% of the records)",
    },
    "bench.row.search_live": {"uz": "Fayl ochilishi bilan darhol qidiruv «{text}» (indeks fonda quriladi)",
                              "en": "Search right after opening, “{text}” (the index is built in the background)"},
    "bench.row.sort_unique": {
        "uz": "{records} ta yozuvni deyarli takrorlanmaydigan satr maydoni (`{key}`) bo'yicha tartiblash (faqat "
              "sort_records, natija yozilmaydi)",
        "en": "Sorting {records} records by an almost unique string field (`{key}`) (sort_records only, the output "
              "is not written)",
    },
    "bench.row.sort_num": {"uz": "{records} ta yozuvni son maydon (`{key}`) bo'yicha tartiblash (faqat sort_records, "
                                 "natija yozilmaydi)",
                           "en": "Sorting {records} records by a number field (`{key}`) (sort_records only, the "
                                 "output is not written)"},
    "bench.row.index_jsonl": {"uz": "To'liq indeks, Rust yadro, JSON Lines ({file})",
                              "en": "Full index, Rust core, JSON Lines ({file})"},
    "bench.row.filter": {"uz": "Filtr `{expr}` (strategiya: {strategy})", "en": "Filter `{expr}` (strategy: {strategy})"},
    "bench.row.export_csv": {"uz": "Eksport JSON → CSV, avtomatik ustunlar (massivlar katakda JSON matni)",
                             "en": "Export JSON → CSV, automatic columns (arrays as JSON text in the cells)"},
    "bench.row.export_csv_scalar": {"uz": "Eksport JSON → CSV, faqat {n} ta skalyar ustun",
                                    "en": "Export JSON → CSV, {n} scalar columns only"},
    "bench.row.index_python": {"uz": "To'liq indeks, sof Python yadro (zaxira)",
                               "en": "Full index, pure-Python core (fallback)"},
    "bench.row.schema": {"uz": "Tuzilma (schema), {n} ta yozuvli namuna", "en": "Structure (schema) from a {n}-record sample"},
    "bench.row.cold_open": {"uz": "Sovuq fayl (OS keshida yo'q): ochish, birinchi {n} ta yozuv",
                            "en": "Cold file (not in the OS cache): open, first {n} records"},
    "bench.row.cold_index": {"uz": "Sovuq fayl: to'liq indeks (Rust)", "en": "Cold file: full index (Rust)"},
    "bench.row.search_file": {"uz": "To'liq matn qidiruvi, bayt rejimi, {file}: «{text}» ({share}% yozuvda)",
                              "en": "Full-text search, byte mode, {file}: “{text}” (in {share}% of the records)"},
    "bench.row.coldread": {"uz": "Disk: buferlanmagan ketma-ket o'qish / o'qish + Scanner",
                           "en": "Disk: unbuffered sequential read / read + Scanner"},
    "bench.res.error": {"uz": "xato: {error}", "en": "error: {error}"},
    "bench.res.open": {"uz": "{first} (Document.open {open}; jarayon ishga tushganidan {start})",
                       "en": "{first} (Document.open {open}; {start} from the process start)"},
    "bench.res.index": {"uz": "{rate} ({size}, {seconds}, {records} ta yozuv; birinchi yozuv {first}; yadro: {backend})",
                        "en": "{rate} ({size}, {seconds}, {records} records; first record {first}; core: {backend})"},
    "bench.res.node": {
        "uz": "{node} (bolalar skaneri {scan}; fayl ochilishidan {total}; oxirgi sahifa {tail}; "
              "qayta ochish keshdan {reopen})",
        "en": "{node} (child scan {scan}; {total} from opening the file; last page {tail}; "
              "reopen from the cache {reopen})",
    },
    "bench.res.search": {
        "uz": "{rate} ({seconds}; {hits} ta moslik, {records} ta yozuvda; birinchi natija {first}; rejim: {mode})",
        "en": "{rate} ({seconds}; {hits} hits in {records} records; first hit {first}; mode: {mode})",
    },
    "bench.res.sort": {"uz": "{seconds}, peak RSS {peak} ({rate}; {strategy}, {runs} ta run)",
                       "en": "{seconds}, peak RSS {peak} ({rate}; {strategy}, {runs} runs)"},
    "bench.res.sort_export": {
        "uz": "{total}, peak RSS {peak} (indeks {index}; tartiblash {sort} — {strategy}, {runs} ta run; tartiblangan "
              "eksport {export} — {rate})",
        "en": "{total}, peak RSS {peak} (index {index}; sort {sort} — {strategy}, {runs} runs; sorted export "
              "{export} — {rate})",
    },
    "bench.res.filter_export": {
        "uz": "{total}, kirish bo'yicha {rate}, peak RSS {peak} (indeks {index}; filtr {filter} — {strategy}; eksport "
              "{export} — {matched} / {scanned} ta yozuv, chiqish {out})",
        "en": "{total}, {rate} over the input, peak RSS {peak} (index {index}; filter {filter} — {strategy}; export "
              "{export} — {matched} of {scanned} records, output {out})",
    },
    "bench.res.export": {"uz": "{rate}, peak RSS {peak} ({seconds}; chiqish {out})",
                         "en": "{rate}, peak RSS {peak} ({seconds}; output {out})"},
    "bench.res.memory": {"uz": "{peak} (eng kattasi: {op}; {n} ta amal)", "en": "{peak} (largest: {op}; {n} operations)"},
    "bench.res.reopen": {"uz": "{first} (Document.open {open}; {records} ta yozuv keshdan: {cached})",
                         "en": "{first} (Document.open {open}; {records} records from the cache: {cached})"},
    "bench.res.node_auto": {
        "uz": "{total} (Document.open {open} — bosh qism ijson bilan tahlil qilinadi; ildiz {root} — asosiy "
              "indeks bilan; tugun {node})",
        "en": "{total} (Document.open {open} — the head is analysed with ijson; root {root} — with the main "
              "index; node {node})",
    },
    "bench.res.search_live": {
        "uz": "birinchi natija {first} (ochishdan); hammasi {total}; rejim: {mode}; parse qilingan yozuvlar: {parsed}",
        "en": "first hit {first} (from opening); total {total}; mode: {mode}; records parsed: {parsed}",
    },
    "bench.res.filter": {"uz": "{rate} ({seconds}; {matched} / {scanned} ta yozuv)",
                         "en": "{rate} ({seconds}; {matched} of {scanned} records)"},
    "bench.res.schema": {"uz": "{seconds} ({records} / {total} ta yozuv, {fields} ta maydon)",
                         "en": "{seconds} ({records} of {total} records, {fields} fields)"},
    "bench.res.cold_open": {"uz": "{first} (Document.open {open}); ochish + indeks paytida diskdan o'qildi: {disk}",
                            "en": "{first} (Document.open {open}); read from the disk while opening + indexing: {disk}"},
    "bench.res.cold_index": {"uz": "{rate} ({size}, {seconds}); diskdan o'qildi: {disk}",
                             "en": "{rate} ({size}, {seconds}); read from the disk: {disk}"},
    "bench.res.coldread": {"uz": "{read} xom o'qish; {scan} o'qish + Scanner (diskdan {disk})",
                           "en": "{read} raw read; {scan} read + Scanner ({disk} from the disk)"},
    # misses
    "bench.miss.failed.cause": {"uz": "amal xato bilan tugadi: {error}", "en": "the operation failed: {error}"},
    "bench.miss.failed.options": {
        "uz": "JSON natijalaridagi stderr va traceback'ni ko'rib, xatoni tuzatish va qayta o'lchash.",
        "en": "read stderr and the traceback in the JSON results, fix the error and measure again.",
    },
    "bench.miss.search.cause": {
        "uz": "bayt filtrining o'zi tez (kam uchraydigan so'zda butun yo'l {rare}), lekin bu so'zda {scanned} ta "
              "yozuvdan {parsed} tasi ({share}%) nomzod bo'ldi va har biri Python'da parse qilinib, butun daraxti "
              "aylanib chiqildi (moslikni tasdiqlash, yo'l va parcha uchun): ~{per_s} ta yozuv/s. Bu GIL ostidagi "
              "CPU ishi, disk emas.",
        "en": "the byte prefilter itself is fast (the whole pass runs at {rare} for the rare word), but here {parsed} "
              "of {scanned} records ({share}%) were candidates and each one was parsed and walked completely in "
              "Python (to confirm the hit and build its path and snippet): ~{per_s} records/s. This is CPU work "
              "under the GIL, not I/O.",
    },
    "bench.miss.search.options": {
        "uz": "1) tasdiqlashni Rust yadroga o'tkazish: yozuv baytlaridan ignaga mos satr va kalit tokenlarini "
              "yo'li bilan qaytaradigan funksiya (Python obyektlari faqat haqiqiy natijalar uchun yaratiladi) — eng "
              "katta yutuq; 2) tasdiqlashni bloklar bo'yicha bir nechta jarayonga bo'lish (fayl umumiy, GIL "
              "to'smaydi); 3) UI'da nomzod yozuvlar sonini bayt tezligida darhol ko'rsatib, tasdiqlashni ko'rinib "
              "turgan natijalar uchun dangasa (lazy) bajarish.",
        "en": "1) move the confirmation into the Rust core: a function returning the string and key tokens that "
              "match the needles, with their paths, straight from the record bytes (Python objects only for real "
              "hits) — the biggest gain; 2) split the confirmation by blocks over several processes (shared file, "
              "no GIL contention); 3) in the UI, show the number of candidate records at byte speed at once and "
              "confirm lazily for the visible results.",
    },
    "bench.miss.search_parse.cause": {"uz": "bayt rejimi ishlatilmadi (rejim: {mode}): har bir yozuv parse qilindi.",
                                      "en": "byte mode was not used (mode: {mode}): every record was parsed."},
    "bench.miss.search_parse.options": {
        "uz": "indeks to'liq tugaganini va faylda \\uXXXX escape'lar yo'qligini tekshirish (ular bo'lsa bayt "
              "filtri ishlatilmaydi).",
        "en": "check that the index was complete and that the file has no \\uXXXX escapes (they disable the "
              "byte prefilter).",
    },
    "bench.miss.export_csv.cause": {
        "uz": "CSV eksport har bir yozuvni parse qiladi (codec.loads) va har bir katakni Python'da hosil qiladi; "
              "avtomatik ustunlarda orders/children kabi obyektlar massivi har katakda ixcham JSON matniga qayta "
              "yoziladi, so'ng csv moduli yozadi (cProfile: vaqtning katta qismi katak qurishda, keyin "
              "csv.writerows va parse). Faqat skalyar ustunlar bilan: {scalar}.",
        "en": "the CSV export parses every record (codec.loads) and builds every cell in Python; with automatic "
              "columns the arrays of objects (orders, children) are re-serialized to compact JSON in each cell, "
              "then the csv module writes the rows (cProfile: most of the time goes to building the cells, then to "
              "csv.writerows and parsing). Scalar columns only: {scalar}.",
    },
    "bench.miss.export_csv.options": {
        "uz": "1) faqat kerakli skalyar ustunlarni tanlash ({scalar}, o'lchandi); 2) qator qurishni Rust yadroga "
              "o'tkazish (PathExtractor spanlari + xom tokenlarni nusxalash, konteynerlar uchun Reformatter) — "
              "Python obyektlari yaratilmaydi; 3) bloklarni parallel jarayonlarda tayyorlab, tartib bilan yozish.",
        "en": "1) choose only the scalar columns that are needed ({scalar}, measured); 2) move row building into "
              "the Rust core (PathExtractor spans + copying raw tokens, Reformatter for containers) — no Python "
              "objects; 3) prepare blocks in parallel processes and write them in order.",
    },
    "bench.miss.sort_export.cause": {
        "uz": "foydalanuvchi {total} kutdi: indeks {index}, tartiblash {sort}, tartiblangan eksport {export} ({rate}; "
              "butun vaqtning {share}%); peak RSS {peak}. Tartiblangan eksport yozuvlarni kalit tartibida — manba "
              "faylning tasodifiy joylaridan — o'qiydi, fayl tartibidagi eksport esa ketma-ket o'qiydi ({plain}).",
        "en": "the user waited {total}: index {index}, sort {sort}, sorted export {export} ({rate}; {share}% of the "
              "whole time); peak RSS {peak}. The sorted export reads the records in key order — from random "
              "positions of the source file — while the export in file order reads them sequentially ({plain}).",
    },
    "bench.miss.sort_export.options": {
        "uz": "1) eng sekin bosqichni profil qilish (`bench/ops.py sort+export`, cProfile); 2) tartiblangan eksport "
              "uchun yozuvlarni kalit bilan birga run fayllariga yozish (butun yozuvlarni tashqi tartiblash: faqat "
              "ketma-ket o'qish, vaqtinchalik disk ≈ fayl hajmi); 3) keyingi bloklarni alohida oqimda oldindan "
              "o'qish (read-ahead), yozish bilan parallel; 4) xotira oshsa: --memory-limit yoki "
              "Settings.sort_memory_bytes ni kamaytirish (ko'proq run, kamroq RAM).",
        "en": "1) profile the slowest phase (`bench/ops.py sort+export`, cProfile); 2) for the sorted export, write "
              "the whole records into the sorted runs next to their keys (external sort of whole records: "
              "sequential reads only, temporary disk ≈ the file size); 3) read the next blocks ahead in a separate "
              "thread, in parallel with writing; 4) when memory is the problem: lower --memory-limit or "
              "Settings.sort_memory_bytes (more runs, less RAM).",
    },
    "bench.miss.generic.cause": {"uz": "sababi avtomatik tahlil qilinmagan; JSON natijalaridagi ichki vaqtlarni ko'ring.",
                                 "en": "the cause is not analysed automatically; see the internal timings in the "
                                       "JSON results."},
    "bench.miss.generic.options": {
        "uz": "tinch kompyuterda qayta o'lchash (--only {op}); profil olib (cProfile), eng sekin bosqichni yadroga "
              "yoki parallel ishga o'tkazish.",
        "en": "measure again on a quiet machine (--only {op}); profile it (cProfile) and move the slowest step into "
              "the core or into parallel work.",
    },
    # report
    "bench.md.title": {"uz": "jsonreshape: katta fayl benchmarki (AGENT_PROMPT §12)",
                       "en": "jsonreshape: big-file benchmark (AGENT_PROMPT §12)"},
    "bench.md.meta": {"uz": "Sana: {date} · davomiyligi: {duration} · rejim: {mode} · to'liq natijalar: `{json}`",
                      "en": "Date: {date} · duration: {duration} · mode: {mode} · full results: `{json}`"},
    "bench.md.partial": {"uz": "Qisman o'lchov ({mode}): jadvalda faqat tanlangan amallar bor.",
                         "en": "Partial run ({mode}): only the selected operations are in the table."},
    "bench.md.main": {"uz": "§12 maqsadlari", "en": "§12 targets"},
    "bench.md.legend": {
        "uz": "✅ maqsadga yetildi · ❌ yetilmadi (sababi va variantlari pastda) · — maqsad belgilanmagan. "
              "Vaqt — eng yaxshi takror, peak RSS — barcha takrorlarning eng kattasi; MB = 10⁶ bayt.",
        "en": "✅ target met · ❌ missed (cause and options below) · — no target. Time: best run; peak RSS: "
              "largest of all runs; MB = 10⁶ bytes.",
    },
    "bench.md.extra": {"uz": "Qo'shimcha o'lchovlar", "en": "Extra measurements"},
    "bench.md.misses": {"uz": "Maqsadga yetilmagan joylar", "en": "Missed targets"},
    "bench.md.no_misses": {"uz": "Barcha maqsadlarga yetildi.", "en": "Every target was met."},
    "bench.md.cause": {"uz": "Sabab", "en": "Cause"},
    "bench.md.options": {"uz": "Variantlar", "en": "Options"},
    "bench.md.data": {"uz": "Ma'lumot fayllari (bench/make_synthetic.py, deterministik)",
                      "en": "Data files (bench/make_synthetic.py, deterministic)"},
    "bench.md.data_line": {"uz": "`{name}`: {size}, {records} ta yozuv ({how})",
                           "en": "`{name}`: {size}, {records} records ({how})"},
    "bench.md.generated": {"uz": "shu o'lchovda yaratildi", "en": "generated by this run"},
    "bench.md.reused": {"uz": "avval yaratilgan", "en": "generated earlier"},
    "bench.md.machine": {"uz": "Kompyuter", "en": "Machine"},
    "bench.md.cpu": {"uz": "Protsessor: {cpu}, {cores} yadro / {logical} mantiqiy protsessor{split}",
                     "en": "CPU: {cpu}, {cores} cores / {logical} logical processors{split}"},
    "bench.md.pe_split": {"uz": " ({p} ta P + {e} ta E yadro)", "en": " ({p} P + {e} E cores)"},
    "bench.md.ram": {"uz": "RAM: {ram}", "en": "RAM: {ram}"},
    "bench.md.disk": {"uz": "Ma'lumotlar diski: {model} ({bus}, {media}), {drive} {fs}, bo'sh joy {free}",
                      "en": "Data disk: {model} ({bus}, {media}), {drive} {fs}, {free} free"},
    "bench.md.os": {"uz": "OS: {os}; quvvat rejasi: {power}", "en": "OS: {os}; power plan: {power}"},
    "bench.md.python": {"uz": "Python {python}; jsonreshape {version}, yadro: {backend}; orjson {orjson}, "
                              "ijson {ijson} ({ijson_backend})",
                        "en": "Python {python}; jsonreshape {version}, core: {backend}; orjson {orjson}, "
                              "ijson {ijson} ({ijson_backend})"},
    "bench.md.conditions": {"uz": "O'lchash shartlari", "en": "Measurement conditions"},
    "bench.md.not_measured": {"uz": "Nimani o'lchab bo'lmadi", "en": "What could not be measured"},
    "bench.md.rerun": {
        "uz": "Qayta o'lchash: `.venv/Scripts/python.exe bench/run_bench.py` (to'liq) yoki `--subset 2g` "
              "(§14 25-test to'plami); bitta amal: `--only NOM` (`--list`).",
        "en": "Measure again: `.venv/Scripts/python.exe bench/run_bench.py` (full) or `--subset 2g` (the set of "
              "§14 test 25); one operation: `--only NAME` (`--list`).",
    },
    "bench.cond.process": {
        "uz": "Har bir amal alohida yangi jarayonda, yangi bo'sh JSONRESHAPE_HOME bilan bajarildi (kesh indeksi yo'q; "
              "faqat «qayta ochish» oldingi indeksni ishlatadi). Qidiruv, filtr, tartiblash, eksport va tuzilma o'sha "
              "jarayonda avval indeks quradi; jadvaldagi vaqt — faqat amalning o'zi (indeks vaqti JSON'da alohida).",
        "en": "Every operation ran in a new process with a new, empty JSONRESHAPE_HOME (no cached index; only "
              "\"reopen\" uses the index of the previous run). Search, filter, sort, export and schema build the "
              "index first in the same process; the table shows the operation alone (the index time is in the JSON).",
    },
    "bench.cond.end_to_end": {
        "uz": "Istisno — foydalanuvchi oladigan natijani o'lchaydigan qatorlar: §12 dagi tartiblash qatori va «filtr "
              "… va yozish» qatorlari `jsonreshape sort` / `jsonreshape filter` kabi butun yo'lni o'lchaydi — "
              "Document.open, indeks, sort_records / filter_records va natija RecordSet'ini JSONL faylga yozish, "
              "fayl to'liq yozilguncha (AGENT_PROMPT §6.4: tartiblash natijasi shu tartibda eksport qilinadi). "
              "Tartiblangan eksport yozuvlarni kalit tartibida, ya'ni faylning tasodifiy joylaridan o'qiydi, shuning "
              "uchun fayl tartibidagi eksport uning o'rnini bosa olmaydi; faqat sort_records vaqti qo'shimcha "
              "jadvalda.",
        "en": "The exceptions are the rows that measure what the user gets: the §12 sort row and the \"filter … and "
              "writing\" rows measure the whole path like `jsonreshape sort` / `jsonreshape filter` — Document.open, "
              "index, sort_records / filter_records and writing the resulting RecordSet to a JSONL file, until the "
              "file is written (AGENT_PROMPT §6.4: the sort result is exported in that order). The sorted export "
              "reads the records in key order, i.e. from random positions of the file, so the export in file order "
              "cannot stand in for it; the sort_records time alone is in the extra table.",
    },
    "bench.cond.memory": {
        "uz": "Peak RSS — jarayonning eng katta ishchi to'plami (Windows peak_wset). Windows'da venv'ning "
              "python.exe fayli faqat ishga tushiruvchi, haqiqiy interpretator uning bola jarayoni, shuning uchun "
              "butun jarayonlar daraxti psutil bilan har 20 ms kuzatildi va har jarayonning handle'i ochiq tutilib, "
              "tugagach aniq qiymat olindi; amal jarayoni ham o'z peak'ini xabar qiladi. Ikki mustaqil o'lchov "
              "farqi: ko'pi bilan {diff}%. Jarayon tegib o'tgan mmap indeks sahifalari ham kiradi "
              "(Vazifalar dispetcheridagidek).",
        "en": "Peak RSS is the process's peak working set (Windows peak_wset). On Windows the python.exe of a venv "
              "is only a launcher and the real interpreter is its child process, so the whole process tree was "
              "polled with psutil every 20 ms and a handle to every process was kept open to read the exact value "
              "after it exited; the operation's process also reports its own peak. The two independent "
              "measurements differ by at most {diff}%. Index pages the process touched through mmap count too "
              "(as in Task Manager).",
    },
    "bench.cond.repeat": {"uz": "Takrorlar: amalga qarab {repeats} marta; jadvalda eng yaxshi takror.",
                          "en": "Runs per operation: {repeats} (depending on the operation); the table shows the best."},
    "bench.cond.load": {
        "uz": "Har o'lchovdan oldin tizim CPU yuklamasi tekshirildi: ≤ {limit}% bo'lguncha, ko'pi bilan {timeout} s "
              "kutildi. Ko'rilgan yuklama: {min}–{max}% (o'rtacha {avg}%); {waited} marta kutish muddati tugab, "
              "o'lchov baribir bajarildi; jami kutish {seconds} s.",
        "en": "Before every run the system CPU load was checked: waited until ≤ {limit}%, at most {timeout} s. "
              "Load seen: {min}–{max}% (average {avg}%); {waited} times the wait timed out and the run went ahead; "
              "{seconds} s waited in total.",
    },
    "bench.cond.no_wait": {"uz": "Tinch kompyuterni kutish o'chirilgan (--no-wait yoki --smoke).",
                           "en": "Waiting for a quiet machine was off (--no-wait or --smoke)."},
    "bench.cond.others": {
        "uz": "O'lchovlar paytida boshqa jarayonlar band qilgan protsessor: o'rtacha {avg}, ko'pi bilan {max} yadro "
              "({logical} tadan).",
        "en": "CPU used by other processes during the runs: {avg} cores on average, at most {max} (of {logical}).",
    },
    "bench.cond.cache": {
        "uz": "OS fayl keshi: kompyuterda {ram} RAM bor, ma'lumot fayllari keshda turadi — jadvaldagi natijalar "
              "iliq keshda olingan (faylni o'qish diskka bormaydi).",
        "en": "OS file cache: the machine has {ram} of RAM, so the data files stay cached — the results are warm-cache "
              "results (reading the file does not touch the disk).",
    },
    "bench.cond.cold": {
        "uz": "Bitta sovuq o'qish o'lchandi: fayl FILE_FLAG_NO_BUFFERING bilan nusxalandi (nusxa OS keshiga "
              "tushmaydi), shu nusxa ochildi va indekslandi; disk hisoblagichi {disk} o'qilganini ko'rsatdi "
              "(fayl {size}) — ma'lumot haqiqatan diskdan kelgan.",
        "en": "One cold read was measured: the file was copied with FILE_FLAG_NO_BUFFERING (the copy never enters "
              "the OS cache) and the copy was opened and indexed; the disk counter shows {disk} read (file {size}) — "
              "the data really came from the disk.",
    },
    "bench.cond.size": {
        "uz": "§12 maqsadlari 5 GB massiv fayl uchun; bu yerda {size2g} massiv ({records} ta yozuv) va {records10m} ta "
              "yozuvli {size10m} JSONL ishlatildi. Tezlik (MB/s) streaming tufayli fayl hajmiga bog'liq emas, ochish "
              "faqat faylning boshini o'qiydi, xotira esa hajmga deyarli bog'liq emas (indeks diskda, mmap).",
        "en": "The §12 targets are for a 5 GB array file; this run used a {size2g} array ({records} records) and a "
              "{size10m} JSON Lines file with {records10m} records. Speeds (MB/s) do not depend on the file size "
              "(streaming), opening reads only the head of the file, and memory hardly depends on the size (the "
              "index is on disk, mmap).",
    },
    "bench.cond.size5g": {
        "uz": "§12 dagi 5 GB holatni to'g'ridan-to'g'ri tekshirish uchun qo'shimcha jadvalda {size5g} massivda "
              "({records5g} ta yozuv) ochish, indeks va qidiruv ham o'lchandi.",
        "en": "To check the 5 GB case of §12 directly, opening, indexing and searching a {size5g} array "
              "({records5g} records) are in the extra table too.",
    },
    "bench.cond.cardinality": {
        "uz": "Tartiblash: `name` maydonida atigi ~286 xil qiymat bor (make_synthetic: 26 ism × 11 familiya), "
              "shuning uchun kalitlarni kodlash keshi ko'p ishlaydi; deyarli takrorlanmaydigan kalitlar uchun "
              "qo'shimcha jadvaldagi `created_at` (son) va `contact.phone` (satr) qatorlariga qarang.",
        "en": "Sorting: the `name` field has only ~286 distinct values (make_synthetic: 26 first names × 11 last "
              "names), so the sort-key cache hits most of the time; for almost unique keys see the `created_at` "
              "(number) and `contact.phone` (string) rows of the extra table.",
    },
    "bench.cond.export": {
        "uz": "§12 dagi eksport maqsadi mapping yoki tahrirlar bilan eksport uchun; ular keyingi bosqichlarda "
              "qo'shiladi, shuning uchun hozir oddiy JSON → JSONL va JSON → JSON (indent 2) o'lchandi.",
        "en": "The §12 export target is for exports with a mapping or edits; those come in later stages, so plain "
              "JSON → JSONL and JSON → JSON (indent 2) were measured.",
    },
    "bench.cond.hybrid": {
        "uz": "Gibrid protsessor ({p} ta P + {e} ta E yadro): affinity o'rnatilmadi, yadroni Windows tanlaydi; "
              "E yadroga tushgan takror sekinroq bo'ladi, shuning uchun eng yaxshi takror olinadi.",
        "en": "Hybrid CPU ({p} P + {e} E cores): no affinity was set, Windows picks the core; a run that lands on an "
              "E core is slower, which is one reason the best run is taken.",
    },
    "bench.nm.laptop": {
        "uz": "§12 «oddiy noutbuk» uchun yozilgan; o'lchov ish stoli protsessorida ({cpu}) bajarildi — noutbukda "
              "CPU'ga bog'liq amallar sekinroq bo'lishi kutiladi.",
        "en": "§12 is written for \"an ordinary laptop\"; this run used a desktop CPU ({cpu}) — CPU-bound "
              "operations are expected to be slower on a laptop.",
    },
    "bench.nm.cold_all": {
        "uz": "Hamma amallarni sovuq keshda o'lchab bo'lmadi: butun tizim keshini tozalash administrator huquqini "
              "talab qiladi va boshqa ishlayotgan dasturlarga ta'sir qiladi; faqat bitta sovuq ochish va indekslash "
              "o'lchandi (yuqorida).",
        "en": "Not every operation could be measured with a cold cache: flushing the system cache needs "
              "administrator rights and disturbs the other running programs; one cold open + index was measured "
              "(above).",
    },
    "bench.nm.gui": {
        "uz": "GUI hali yo'q (2-bosqich): «birinchi yozuvlar ko'rinishi» kutubxona darajasida o'lchandi "
              "(Document.open + daraxt tugunlari + qiymatlar), Qt chizish vaqti kirmaydi.",
        "en": "There is no GUI yet (stage 2): \"first records visible\" was measured at the library level "
              "(Document.open + tree nodes + values); Qt painting is not included.",
    },
    "bench.nm.mapping": {
        "uz": "Mapping yoki tahrirlar bilan eksport (§12 dagi 6-qator) hali o'lchab bo'lmaydi: bu imkoniyatlar 3- "
              "va 4-bosqichlarda.",
        "en": "Export with a mapping or edits (row 6 of §12) cannot be measured yet: those features come in stages "
              "3 and 4.",
    },
}


def _register_messages() -> None:
    for key, entry in BENCH_MESSAGES.items():
        i18n.MESSAGES.setdefault(key, entry)


_register_messages()

RESULTS_VERSION = 1
DATA_DIR = BENCH / "data"
RESULTS_DIR = BENCH / "results"
OPS_SCRIPT = BENCH / "ops.py"
GENERATOR = BENCH / "make_synthetic.py"
PYTHON = sys.executable

POLL_SECONDS = 0.02
QUIET_LOAD = 35.0           # percent of all CPUs
QUIET_TIMEOUT = 60.0        # seconds
QUIET_WINDOW = 1.0          # seconds per load sample
OP_TIMEOUT = 3600.0         # seconds per run

# §12 targets (decimal units: 1 MB = 10**6 bytes, the strict reading)
T_OPEN_S = 1.0
T_INDEX_MBPS = 150.0
T_NODE_S = 0.2
T_SEARCH_MBPS = 100.0
T_FIRST_HIT_S = 1.0         # "natijalar darhol chiqa boshlaydi": the first hit within the open target
T_SORT_S = 120.0
T_SORT_RSS = 1_000_000_000
T_SORT_RECORDS = 10_000_000  # "10 million yozuvni ... tartiblash": a smaller file's time is projected to this
T_EXPORT_MBPS = 50.0
T_EXPORT_RSS = 300_000_000
T_TOTAL_RSS = 500_000_000
T_PY_INDEX_MBPS = 15.0      # docs/ARCHITECTURE.md §1: Python reference Scanner aim

RARE_TEXT = "Qoraqalpog'iston"   # make_synthetic marker: every 100 000th record
COMMON_TEXT = "ko'chasi"          # street names: ~60 % of the records
MEDIUM_TEXT = "Toshkent"          # city: ~6 % of the records
FILTER_EXPR = 'contact.address.city == "Toshkent"'
SCALAR_COLUMNS = ("id,name,active,score,created_at,contact.phone,contact.address.city,"
                  "contact.address.street,contact.address.zip")


# ----------------------------------------------------------------------------------
# data files and operations
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class DataSpec:
    key: str
    filename: str
    args: tuple[str, ...]
    smoke_args: tuple[str, ...]


DATA_SPECS: dict[str, DataSpec] = {
    "syn_2g": DataSpec("syn_2g", "syn_2g.json", ("--size", "2GB", "--format", "array"),
                       ("--records", "3000", "--format", "array")),
    "syn_10m": DataSpec("syn_10m", "syn_10m.jsonl", ("--records", "10000000", "--format", "jsonl"),
                        ("--records", "5000", "--format", "jsonl")),
    "node_1m": DataSpec("node_1m", "node_1m.json", ("--node-items", "1000000"), ("--node-items", "20000")),
    "syn_5g": DataSpec("syn_5g", "syn_5g.json", ("--size", "5GB", "--format", "array"),
                       ("--records", "4000", "--format", "array")),
}


@dataclass(frozen=True)
class OpSpec:
    """One benchmark operation: ``bench/ops.py <op> <data file> <args>``."""

    name: str
    op: str
    data: str
    args: tuple[str, ...] = ()
    repeats: int = 3
    metric: str = "seconds"            # the best run has the smallest value of this result field
    env: tuple[tuple[str, str], ...] = ()
    keep_home: bool = False            # keep the home of the last run (a later op reuses its index)
    home_from: str | None = None       # run in the kept home of that op (cached reopen)
    out_ext: str | None = None         # export: output file in the work dir
    cold: bool = False                 # run on an unbuffered (uncached) copy of the data file
    windows_only: bool = False
    is_sort: bool = False              # excluded from the "overall memory" row
    groups: tuple[str, ...] = ("full",)


SPECS: tuple[OpSpec, ...] = (
    OpSpec("open_2g", "open", "syn_2g", repeats=5, metric="first_records_seconds"),
    OpSpec("index_2g", "index", "syn_2g", keep_home=True, groups=("full", "2g")),
    OpSpec("reopen_2g", "reopen", "syn_2g", home_from="index_2g", metric="first_records_seconds"),
    OpSpec("node_1m", "node", "node_1m", ("--format", "single"), metric="node_open_seconds"),
    OpSpec("node_1m_auto", "node", "node_1m", ("--format", "auto"), metric="open_to_node_seconds"),
    OpSpec("search_rare", "search", "syn_2g", (RARE_TEXT,), groups=("full", "2g")),
    OpSpec("search_common", "search", "syn_2g", (COMMON_TEXT,), groups=("full", "2g")),
    OpSpec("search_medium", "search", "syn_2g", (MEDIUM_TEXT,)),
    OpSpec("search_live", "search", "syn_2g", (RARE_TEXT, "--no-prebuild"), metric="total_seconds"),
    OpSpec("sort_10m", "sort", "syn_10m", ("name",), is_sort=True),
    OpSpec("sort_10m_num", "sort", "syn_10m", ("created_at",), repeats=2, is_sort=True),
    OpSpec("sort_10m_unique", "sort", "syn_10m", ("contact.phone",), repeats=2, is_sort=True),
    OpSpec("sort_2g", "sort", "syn_2g", ("name",), is_sort=True, groups=("2g",)),
    # what the user gets from `jsonreshape sort`: the sorted output file (the §12 sort row)
    OpSpec("sort_export_10m", "sort+export", "syn_10m", ("name",), repeats=2, metric="total_seconds",
           out_ext=".jsonl", is_sort=True),
    OpSpec("sort_export_2g", "sort+export", "syn_2g", ("name",), repeats=2, metric="total_seconds",
           out_ext=".jsonl", is_sort=True, groups=("full", "2g")),
    OpSpec("export_jsonl", "export", "syn_2g", ("--fmt", "jsonl"), out_ext=".jsonl", groups=("full", "2g")),
    OpSpec("export_json2", "export", "syn_2g", ("--fmt", "json", "--indent", "2"), out_ext=".json",
           groups=("full", "2g")),
    OpSpec("export_csv", "export", "syn_2g", ("--fmt", "csv"), out_ext=".csv", repeats=2),
    OpSpec("export_csv_scalar", "export", "syn_2g", ("--fmt", "csv", "--columns", SCALAR_COLUMNS), out_ext=".csv",
           repeats=2),
    OpSpec("filter_extract", "filter", "syn_2g", (FILTER_EXPR,)),
    OpSpec("filter_parse", "filter", "syn_2g", (FILTER_EXPR, "--strategy", "parse"), repeats=1),
    # what the user gets from `jsonreshape filter`: the matching records written to a file
    OpSpec("filter_export_2g", "filter+export", "syn_2g", (FILTER_EXPR,), repeats=2, metric="total_seconds",
           out_ext=".jsonl", groups=("full", "2g")),
    OpSpec("filter_export_10m", "filter+export", "syn_10m", (FILTER_EXPR,), repeats=2, metric="total_seconds",
           out_ext=".jsonl"),
    OpSpec("schema_2g", "schema", "syn_2g", ("--sample", "10000")),
    OpSpec("index_python", "index", "syn_2g", repeats=2, env=(("JSONRESHAPE_BACKEND", "python"),)),
    OpSpec("coldread_2g", "coldread", "syn_2g", repeats=2, metric="read_seconds", windows_only=True),
    OpSpec("cold_2g", "cold", "syn_2g", repeats=2, metric="first_records_seconds", cold=True, windows_only=True),
    OpSpec("open_5g", "open", "syn_5g", repeats=3, metric="first_records_seconds", groups=("full", "5g")),
    OpSpec("index_5g", "index", "syn_5g", repeats=2, groups=("full", "5g")),
    OpSpec("search_rare_5g", "search", "syn_5g", (RARE_TEXT,), repeats=2, groups=("full", "5g")),
)
SPEC_BY_NAME = {s.name: s for s in SPECS}
# the §12 sort row: the first of these that was run (end to end: sort + export of the sorted set)
SORT_MAIN_OPS = ("sort_export_10m", "sort_export_2g")


def select_specs(mode: str, only: list[str] | None = None) -> list[OpSpec]:
    """The operations of a run: ``full``, ``2g`` (the subset of §14 test 25), ``smoke``
    (everything, on tiny files) or an explicit ``only`` list (in ``SPECS`` order)."""
    if only:
        unknown = [n for n in only if n not in SPEC_BY_NAME]
        if unknown:
            raise SystemExit(t("bench.error.unknown_op", names=", ".join(unknown),
                               known=", ".join(SPEC_BY_NAME)))
        chosen = [s for s in SPECS if s.name in only]
        # a cached reopen needs the index op that leaves its home behind
        for s in list(chosen):
            if s.home_from and s.home_from not in only:
                chosen.insert(0, SPEC_BY_NAME[s.home_from])
        chosen = [s for s in SPECS if s in chosen]
    elif mode == "smoke":
        chosen = list(SPECS)
    else:
        chosen = [s for s in SPECS if mode in s.groups]
    if sys.platform != "win32":
        chosen = [s for s in chosen if not s.windows_only]
    return chosen


# ----------------------------------------------------------------------------------
# small formatting helpers
# ----------------------------------------------------------------------------------
def fmt_s(x: float | None) -> str:
    """Seconds for humans: ``8.4 ms``, ``0.21 s``, ``17.2 s``, ``2 min 5 s``."""
    if x is None:
        return "?"
    x = float(x)
    if x < 0.01:
        return f"{x * 1000:.1f} ms"
    if x < 1:
        return f"{x * 1000:.0f} ms"
    if x < 10:
        return f"{x:.2f} s"
    if x < 120:
        return f"{x:.1f} s"
    m, s = divmod(int(round(x)), 60)
    return t("bench.unit.minutes", m=m, s=s)


def fmt_rate(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:,.0f} MB/s".replace(",", " ") if x >= 100 else f"{x:.1f} MB/s"


def fmt_mb(n: int | float | None) -> str:
    return "?" if n is None else format_bytes(n)


def group(n: int | None) -> str:
    return "?" if n is None else f"{int(n):,}".replace(",", " ")


def _num(d: dict[str, Any] | None, key: str) -> float | None:
    if not d:
        return None
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pct(x: Any) -> str:
    return f"{x}%" if isinstance(x, (int, float)) and not isinstance(x, bool) else "-"


def _mode_text(mode: Any) -> str:
    """Search mode in the UI language (the search module's own texts)."""
    key = f"search.mode.{mode}"
    return t(key) if has(key) else str(mode)


def _strategy_text(strategy: Any) -> str:
    key = f"bench.strategy.{strategy}"
    return t(key) if has(key) else str(strategy)


# ----------------------------------------------------------------------------------
# machine info and load
# ----------------------------------------------------------------------------------
def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0")
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "?"


def _efficiency_classes() -> dict[int, int]:
    """Windows hybrid CPUs: {efficiency class: logical processors} (higher = faster cores)."""
    if sys.platform != "win32":
        return {}
    import ctypes
    import struct
    from ctypes import wintypes as wt

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        size = wt.ULONG(0)
        k32.GetSystemCpuSetInformation(None, 0, ctypes.byref(size), None, 0)
        if not size.value:
            return {}
        buf = (ctypes.c_ubyte * size.value)()
        if not k32.GetSystemCpuSetInformation(buf, size, ctypes.byref(size), None, 0):
            return {}
        raw = bytes(buf)
        out: dict[int, int] = {}
        off = 0
        while off + 8 <= len(raw):
            entry, _type = struct.unpack_from("<II", raw, off)
            if entry <= 0:
                break
            # SYSTEM_CPU_SET_INFORMATION.CpuSet: Id, Group, LogicalProcessorIndex, CoreIndex,
            # LastLevelCacheIndex, NumaNodeIndex, EfficiencyClass, ...
            eff = struct.unpack_from("<IHBBBBB", raw, off + 8)[6]
            out[eff] = out.get(eff, 0) + 1
            off += entry
        return out
    except Exception:
        return {}


def _disk_model(path: Path) -> dict[str, Any]:
    """Model / bus / media of the disk holding ``path`` (Windows: PowerShell storage cmdlets)."""
    info: dict[str, Any] = {}
    try:
        usage = psutil.disk_usage(str(path))
        info["free"] = usage.free
        info["total"] = usage.total
    except OSError:
        pass
    drive = os.path.splitdrive(str(path.resolve()))[0]
    info["drive"] = drive or None
    for part in psutil.disk_partitions(all=False):
        if drive and part.mountpoint.upper().startswith(drive.upper()):
            info["fs"] = part.fstype
    if sys.platform == "win32" and len(drive) == 2:
        script = (
            f"$d = Get-Partition -DriveLetter {drive[0]} | Get-Disk; "
            "$p = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq [string]$d.Number }; "
            "[pscustomobject]@{Model=$d.FriendlyName; Bus=[string]$d.BusType; Media=[string]$p.MediaType; "
            "Number=$d.Number} | ConvertTo-Json -Compress"
        )
        try:
            cp = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, timeout=60)
            text = cp.stdout.decode("utf-8", errors="replace").strip()
            if cp.returncode == 0 and text.startswith("{"):
                data = json.loads(text)
                info.update({"model": data.get("Model"), "bus": data.get("Bus"), "media": data.get("Media"),
                             "number": data.get("Number")})
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    info["io_key"] = ops.disk_key(path)
    return info


_POWER_PLANS = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Balanced",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "High performance",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "Power saver",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "Ultimate Performance",
}


def _power_plan() -> str | None:
    """Active Windows power plan: the well-known GUIDs by name, others as reported."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import re

        cp = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, timeout=30)
        raw = cp.stdout
        try:  # console tools write in the OEM code page when redirected
            text = raw.decode(f"cp{ctypes.windll.kernel32.GetOEMCP()}", errors="replace").strip()
        except LookupError:
            text = raw.decode(errors="replace").strip()
        m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text)
        if m and m.group(0).lower() in _POWER_PLANS:
            return _POWER_PLANS[m.group(0).lower()]
        if "(" in text and text.endswith(")"):
            return text[text.rfind("(") + 1:-1]
        return text or None
    except (OSError, subprocess.SubprocessError, AttributeError):
        return None


def machine_info(data_dir: Path) -> dict[str, Any]:
    from jsonreshape.env import check_environment

    env = check_environment(log_result=False)
    eff = _efficiency_classes()
    vm = psutil.virtual_memory()
    info: dict[str, Any] = {
        "cpu": _cpu_name(),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True),
        "efficiency_classes": {str(k): v for k, v in sorted(eff.items())},
        "p_cores": eff.get(max(eff), 0) if len(eff) > 1 else None,
        "e_cores": sum(v for k, v in eff.items() if k != max(eff)) if len(eff) > 1 else None,
        "ram_total": vm.total,
        "ram_available_at_start": vm.available,
        "disk": _disk_model(data_dir),
        "os": platform.platform(),
        "python": platform.python_version(),
        "python_impl": platform.python_implementation(),
        "power_plan": _power_plan(),
        "core_backend": env.get("core_backend"),
        "core_backends": env.get("core_backends"),
        "ijson_backend": env.get("ijson_backend"),
        "versions": env.get("versions"),
        "app_version": env.get("app_version"),
    }
    return info


def current_load(window: float = QUIET_WINDOW) -> float:
    """System CPU load in percent of all logical CPUs, averaged over ``window`` seconds."""
    return float(psutil.cpu_percent(interval=window))


def wait_quiet(limit: float, timeout: float, *, load_fn: Callable[[], float] | None = None,
               clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Sample the system load until it is at most ``limit`` percent or ``timeout``
    seconds have passed. Returns ``{"load", "waited", "quiet", "samples", "max"}``."""
    load_fn = load_fn or current_load
    t0 = clock()
    seen: list[float] = []
    while True:
        load = float(load_fn())
        seen.append(load)
        waited = clock() - t0
        if load <= limit or waited >= timeout:
            return {"load": round(load, 1), "waited": round(waited, 2), "quiet": load <= limit,
                    "samples": len(seen), "max": round(max(seen), 1)}


# ----------------------------------------------------------------------------------
# running one child process
# ----------------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as _wt

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", _wt.DWORD), ("PageFaultCount", _wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.OpenProcess.restype = _wt.HANDLE
    _K32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
    _K32.K32GetProcessMemoryInfo.restype = _wt.BOOL
    _K32.K32GetProcessMemoryInfo.argtypes = [_wt.HANDLE, ctypes.c_void_p, _wt.DWORD]
    _K32.GetProcessTimes.restype = _wt.BOOL
    _K32.GetProcessTimes.argtypes = [_wt.HANDLE] + [ctypes.POINTER(_wt.FILETIME)] * 4
    _K32.CloseHandle.restype = _wt.BOOL
    _K32.CloseHandle.argtypes = [_wt.HANDLE]
    _PROCESS_QUERY = 0x1000 | 0x0010   # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ

    def _win_open(pid: int) -> Any:
        return _K32.OpenProcess(_PROCESS_QUERY, False, pid) or None

    def _win_stats(handle: Any) -> dict[str, Any]:
        """Peak working set / peak private bytes / CPU seconds of a process handle; works
        after the process has exited as long as the handle is open."""
        out: dict[str, Any] = {}
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        if _K32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            out["peak_wset"] = int(pmc.PeakWorkingSetSize)
            out["peak_private"] = int(pmc.PeakPagefileUsage)
        ft = [_wt.FILETIME() for _ in range(4)]
        if _K32.GetProcessTimes(handle, *(ctypes.byref(x) for x in ft)):
            out["cpu"] = sum(((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7 for f in ft[2:])
        return out


class TreeWatcher:
    """Peak memory and CPU time of a process and all its descendants.

    On Windows the ``python.exe`` of a virtual environment is a launcher that runs the
    real interpreter as a child process, so the process started by ``Popen`` is not the
    one doing the work. Every process of the tree is found with ``psutil`` and (Windows) a
    handle to it is kept open, so its exact ``PeakWorkingSetSize`` and CPU times can still
    be read after it has exited. Elsewhere ``psutil`` RSS samples are used.
    """

    def __init__(self, pid: int) -> None:
        self.procs: dict[int, dict[str, Any]] = {}
        self._next_scan = 0.0
        try:
            self.root: psutil.Process | None = psutil.Process(pid)
        except psutil.Error:
            self.root = None

    def _scan(self) -> None:
        if self.root is None:
            return
        try:
            found = [self.root, *self.root.children(recursive=True)]
        except psutil.Error:
            found = [self.root]
        for p in found:
            if p.pid in self.procs:
                continue
            entry: dict[str, Any] = {"pid": p.pid, "name": None, "proc": p, "handle": None,
                                     "peak": 0, "private": 0, "cpu": None}
            try:
                entry["name"] = p.name()
            except psutil.Error:
                pass
            if sys.platform == "win32":
                entry["handle"] = _win_open(p.pid)
            self.procs[p.pid] = entry

    def _update(self, e: dict[str, Any]) -> None:
        if e["handle"] is not None:
            st = _win_stats(e["handle"])
            e["peak"] = max(e["peak"], st.get("peak_wset", 0))
            e["private"] = max(e["private"], st.get("peak_private", 0))
            if "cpu" in st:
                e["cpu"] = st["cpu"]
            return
        try:
            mi = e["proc"].memory_info()
            e["peak"] = max(e["peak"], int(getattr(mi, "peak_wset", 0) or mi.rss))
            ct = e["proc"].cpu_times()
            e["cpu"] = ct.user + ct.system
        except psutil.Error:
            pass

    def poll(self) -> None:
        now = time.monotonic()
        if now >= self._next_scan:
            self._scan()
            # the real interpreter appears right after the start; later a slow rescan is enough
            self._next_scan = now + (POLL_SECONDS if len(self.procs) < 2 else 0.5)
        for e in self.procs.values():
            self._update(e)

    def kill(self) -> None:
        for e in self.procs.values():
            try:
                e["proc"].kill()
            except psutil.Error:
                pass

    def finish(self) -> list[dict[str, Any]]:
        """Final values (read from the kept handles of the exited processes); closes them."""
        out = []
        for e in self.procs.values():
            self._update(e)
            if e["handle"] is not None:
                _K32.CloseHandle(e["handle"])
                e["handle"] = None
            out.append({"pid": e["pid"], "name": e["name"], "peak_rss": e["peak"] or None,
                        "peak_private": e["private"] or None,
                        "cpu_seconds": None if e["cpu"] is None else round(e["cpu"], 3)})
        return out


def run_child(cmd: list[str], env: dict[str, str], *, timeout: float = OP_TIMEOUT,
              cwd: Path | None = None) -> dict[str, Any]:
    """Run ``cmd`` and watch the memory of its process tree until it exits. Returns the
    child's JSON result (``child``), the wall time, the peak RSS (the largest of: every
    process of the tree seen from outside, and the child's own report), the CPU time of
    the tree and the CPU load of the machine during the run."""
    psutil.cpu_percent(None)  # start a load window for this run
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, env=env, cwd=str(cwd or ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out: dict[str, bytes] = {}

    def drain(name: str, stream: Any) -> None:
        out[name] = stream.read()

    readers = [threading.Thread(target=drain, args=(n, s), daemon=True)
               for n, s in (("stdout", proc.stdout), ("stderr", proc.stderr))]
    for th in readers:
        th.start()
    watcher = TreeWatcher(proc.pid)
    timed_out = False
    while proc.poll() is None:
        if time.perf_counter() - t0 > timeout:
            timed_out = True
            watcher.kill()
            proc.kill()
            break
        watcher.poll()
        time.sleep(POLL_SECONDS)
    proc.wait()
    wall = time.perf_counter() - t0
    load_during = float(psutil.cpu_percent(None))
    for th in readers:
        th.join(30)
    tree = watcher.finish()
    stdout = out.get("stdout", b"").decode("utf-8", errors="replace")
    stderr = out.get("stderr", b"").decode("utf-8", errors="replace")
    child: dict[str, Any] | None = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                child = json.loads(line)
                break
            except ValueError:
                continue
    tree_peak = max((p["peak_rss"] or 0 for p in tree), default=0)
    tree_private = max((p["peak_private"] or 0 for p in tree), default=0)
    peaks = [p for p in (tree_peak, (child or {}).get("peak_rss")) if p]
    privates = [p for p in (tree_private, (child or {}).get("peak_private")) if p]
    cpus = [p["cpu_seconds"] for p in tree if p["cpu_seconds"] is not None]
    cpu: float | None = sum(cpus) if cpus else None
    if not cpu and child and child.get("cpu_user") is not None:
        cpu = float(child["cpu_user"]) + float(child.get("cpu_system") or 0.0)
    ncpu = psutil.cpu_count() or 1
    others = None
    if cpu is not None and wall > 0:
        others = round(max(0.0, load_during / 100.0 * ncpu - cpu / wall), 2)
    ok = bool(child and child.get("ok")) and proc.returncode == 0 and not timed_out
    error = None
    if not ok:
        if timed_out:
            error = f"timeout after {timeout:.0f} s"
        elif child and child.get("error"):
            error = str(child["error"])
        else:
            error = f"exit code {proc.returncode}: " + (stderr.strip().splitlines() or ["no output"])[-1]
    return {
        "ok": ok,
        "error": error,
        "returncode": proc.returncode,
        "wall_seconds": round(wall, 4),
        "peak_rss": max(peaks) if peaks else None,
        "peak_rss_tree": tree_peak or None,
        "peak_rss_child": (child or {}).get("peak_rss"),
        "peak_private": max(privates) if privates else None,
        "processes": tree,
        "cpu_seconds": None if cpu is None else round(cpu, 3),
        "load_during": round(load_during, 1),
        "others_cores_during": others,
        "child": child,
        "stderr_tail": "\n".join(stderr.strip().splitlines()[-15:]) if (stderr.strip() and not ok) else None,
    }


# ----------------------------------------------------------------------------------
# the run
# ----------------------------------------------------------------------------------
@dataclass
class RunConfig:
    mode: str = "full"                    # full | 2g | smoke | only
    only: list[str] | None = None
    data_dir: Path = DATA_DIR
    results_dir: Path = RESULTS_DIR
    work_dir: Path | None = None
    repeat: int | None = None
    quiet_load: float = QUIET_LOAD
    quiet_timeout: float = QUIET_TIMEOUT
    wait: bool = True
    keep_work: bool = False
    lang: str = "uz"
    timeout: float = OP_TIMEOUT
    write_latest: bool | None = None      # default: full runs only
    command: list[str] = field(default_factory=list)


def _log(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def ensure_data(spec: DataSpec, data_dir: Path, smoke: bool) -> dict[str, Any]:
    """Generate the data file when it is missing (never measured)."""
    path = data_dir / spec.filename
    info: dict[str, Any] = {"path": str(path), "generated": False}
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        args = spec.smoke_args if smoke else spec.args
        _log(t("bench.log.generate", name=spec.filename, args=" ".join(args)))
        cmd = [PYTHON, str(GENERATOR), "--out", str(path), *args, "-q"]
        cp = subprocess.run(cmd, cwd=str(ROOT), capture_output=True)
        if cp.returncode != 0 or not path.exists():
            raise SystemExit(t("bench.error.generate", name=spec.filename,
                               detail=cp.stderr.decode(errors="replace").strip()[-500:]))
        try:
            gen = json.loads(cp.stdout.decode(errors="replace").strip().splitlines()[-1])
            info.update({"records": gen.get("records"), "gen_seconds": gen.get("seconds")})
        except (ValueError, IndexError):
            pass
        info["generated"] = True
        info["generator_args"] = list(args)
    info["bytes"] = path.stat().st_size
    return info


def _best_run(runs: list[dict[str, Any]], metric: str) -> dict[str, Any] | None:
    good = [r for r in runs if r.get("ok") and isinstance((r.get("child") or {}).get(metric), (int, float))]
    if not good:
        return None
    return min(good, key=lambda r: r["child"][metric])


def summarize_op(spec: OpSpec, runs: list[dict[str, Any]]) -> dict[str, Any]:
    best = _best_run(runs, spec.metric)
    peaks = [r["peak_rss"] for r in runs if r.get("peak_rss")]
    privs = [r["peak_private"] for r in runs if r.get("peak_private")]
    values = [r["child"][spec.metric] for r in runs
              if r.get("ok") and isinstance((r.get("child") or {}).get(spec.metric), (int, float))]
    return {
        "spec": asdict(spec),
        "runs": runs,
        "ok": bool(runs) and all(r.get("ok") for r in runs),
        "error": next((r.get("error") for r in runs if not r.get("ok")), None),
        "best": None if best is None else best["child"],
        "best_wall_seconds": None if best is None else best["wall_seconds"],
        "metric": spec.metric,
        "metric_values": values,
        "peak_rss": max(peaks) if peaks else None,
        "peak_private": max(privs) if privs else None,
    }


def run(cfg: RunConfig) -> dict[str, Any]:
    """Run the benchmark described by ``cfg``; returns the results dict (also written to
    ``cfg.results_dir``; the paths are in ``results["files"]``)."""
    old_lang = get_language()
    set_language(cfg.lang)
    try:
        return _run(cfg)
    finally:
        set_language(old_lang)


def _run(cfg: RunConfig) -> dict[str, Any]:
    started = datetime.now()
    run_id = started.strftime("%Y%m%d-%H%M%S")
    smoke = cfg.mode == "smoke"
    specs = select_specs(cfg.mode, cfg.only)
    data_dir = Path(cfg.data_dir).resolve()
    results_dir = Path(cfg.results_dir).resolve()
    work = Path(cfg.work_dir).resolve() if cfg.work_dir else data_dir / "work" / run_id
    work.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {
        "version": RESULTS_VERSION,
        "run_id": run_id,
        "mode": cfg.mode if not cfg.only else "only",
        "only": cfg.only,
        "started": started.isoformat(timespec="seconds"),
        "command": cfg.command,
        "lang": cfg.lang,
    }
    _log(t("bench.log.machine"))
    results["machine"] = machine_info(data_dir)
    results["conditions"] = {
        "quiet_load_limit": cfg.quiet_load,
        "quiet_timeout": cfg.quiet_timeout,
        "wait_for_quiet": cfg.wait,
        "repeat_override": cfg.repeat,
        "poll_seconds": POLL_SECONDS,
        "fresh_home_per_run": True,
        "data_dir": str(data_dir),
        "work_dir": str(work),
        "backend_env": os.environ.get("JSONRESHAPE_BACKEND") or "auto",
        "smoke": smoke,
    }
    needed = sorted({s.data for s in specs})
    results["data"] = {key: ensure_data(DATA_SPECS[key], data_dir, smoke) for key in needed}
    op_results: dict[str, Any] = {}
    homes: dict[str, Path] = {}
    total = len(specs)
    loads: list[dict[str, Any]] = []
    try:
        for n, spec in enumerate(specs, 1):
            repeats = max(1, cfg.repeat if cfg.repeat is not None else (1 if smoke else spec.repeats))
            data_path = data_dir / DATA_SPECS[spec.data].filename
            runs: list[dict[str, Any]] = []
            for i in range(repeats):
                quiet = wait_quiet(cfg.quiet_load, cfg.quiet_timeout) if cfg.wait else None
                if quiet is not None:
                    loads.append(quiet)
                env = dict(os.environ)
                env.update({"PYTHONIOENCODING": "utf-8", "JSONRESHAPE_LANG": cfg.lang})
                env.update(dict(spec.env))
                if spec.home_from:
                    home = homes.get(spec.home_from)
                    if home is None:
                        runs.append({"ok": False, "error": f"no index home from {spec.home_from}"})
                        break
                else:
                    home = work / "homes" / f"{spec.name}-{i + 1}"
                    shutil.rmtree(home, ignore_errors=True)
                    home.mkdir(parents=True, exist_ok=True)
                env["JSONRESHAPE_HOME"] = str(home)
                target = data_path
                cold_copy_s = None
                if spec.cold:
                    target = work / f"cold-{spec.name}-{i + 1}{data_path.suffix}"
                    cold_copy_s = ops.cold_copy(data_path, target)
                cmd = [PYTHON, str(OPS_SCRIPT), spec.op, str(target)]
                if spec.out_ext:
                    cmd.append(str(work / "out" / f"{spec.name}-{i + 1}{spec.out_ext}"))
                cmd.extend(spec.args)
                res = run_child(cmd, env, timeout=cfg.timeout)
                res["quiet"] = quiet
                res["run"] = i + 1
                if cold_copy_s is not None:
                    res["cold_copy_seconds"] = round(cold_copy_s, 3)
                    try:
                        target.unlink()
                    except OSError:
                        pass
                runs.append(res)
                _log(_progress_line(n, total, spec, i + 1, repeats, res))
                if not spec.home_from:
                    if spec.keep_home and i == repeats - 1 and res.get("ok"):
                        homes[spec.name] = home
                    elif not cfg.keep_work:
                        shutil.rmtree(home, ignore_errors=True)
                if not res.get("ok"):
                    break
            op_results[spec.name] = summarize_op(spec, runs)
            # a kept home is removed once every op that needs it has run
            for name, home in list(homes.items()):
                if not any(s.home_from == name for s in specs[n:]):
                    if not cfg.keep_work:
                        shutil.rmtree(home, ignore_errors=True)
                    del homes[name]
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
    results["conditions"]["load_samples"] = _load_summary(loads, op_results)
    for key, info in results["data"].items():
        if info.get("records") is None:
            info["records"] = _records_of(op_results, key)
    rows = build_rows(results)
    results["rows"] = [asdict(r) for r in rows]
    results["all_ok"] = all(o["ok"] for o in op_results.values())
    results["files"] = write_results(results, rows, results_dir, cfg)
    return results


def _progress_line(n: int, total: int, spec: OpSpec, i: int, repeats: int, res: dict[str, Any]) -> str:
    child = res.get("child") or {}
    metric = child.get(spec.metric)
    rate = child.get("mb_per_s") or child.get("read_mb_per_s")
    if not res.get("ok"):
        return t("bench.log.failed", n=n, total=total, name=spec.name, i=i, repeats=repeats,
                 error=res.get("error"))
    quiet = res.get("quiet") or {}
    return t("bench.log.run", n=n, total=total, name=spec.name, i=i, repeats=repeats,
             metric=spec.metric, value=fmt_s(metric) if isinstance(metric, (int, float)) else "?",
             rate=fmt_rate(rate) if isinstance(rate, (int, float)) else "-", peak=fmt_mb(res.get("peak_rss")),
             wall=fmt_s(res.get("wall_seconds")), load=_pct(quiet.get("load")), during=_pct(res.get("load_during")))


def _peak_agreement(results: dict[str, Any]) -> str:
    """Largest relative difference (percent) between the peak RSS seen from outside (the
    process tree) and the peak the operation's process reported itself."""
    worst = None
    for o in results.get("ops", {}).values():
        for r in o.get("runs", []):
            a, b = r.get("peak_rss_tree"), r.get("peak_rss_child")
            if a and b:
                d = abs(a - b) / max(a, b) * 100.0
                worst = d if worst is None else max(worst, d)
    return "?" if worst is None else f"{worst:.1f}"


def _load_summary(loads: list[dict[str, Any]], op_results: dict[str, Any]) -> dict[str, Any]:
    before = [x["load"] for x in loads]
    during = [r.get("load_during") for o in op_results.values() for r in o["runs"]
              if isinstance(r.get("load_during"), (int, float))]
    others = [r.get("others_cores_during") for o in op_results.values() for r in o["runs"]
              if isinstance(r.get("others_cores_during"), (int, float))]
    out: dict[str, Any] = {"runs_waited_out": sum(1 for x in loads if not x["quiet"]),
                           "wait_seconds": round(sum(x["waited"] for x in loads), 1)}
    for name, vals in (("before", before), ("during", during), ("others_cores", others)):
        if vals:
            out[name] = {"min": round(min(vals), 1), "max": round(max(vals), 1),
                         "avg": round(sum(vals) / len(vals), 1)}
    return out


def _records_of(op_results: dict[str, Any], data_key: str) -> int | None:
    for o in op_results.values():
        if o["spec"]["data"] != data_key or not o.get("best"):
            continue
        b = o["best"]
        for key in ("record_count", "child_count"):
            v = b.get(key)
            if isinstance(v, int) and v > 0 and not (key == "record_count" and o["spec"]["op"] == "node"):
                return v
    return None


# ----------------------------------------------------------------------------------
# table rows
# ----------------------------------------------------------------------------------
OK_MARK = "\u2705"
MISS_MARK = "\u274c"
NO_TARGET = "\u2014"


@dataclass
class Row:
    id: str
    section: str        # main | extra
    action: str
    target: str
    result: str
    status: str
    ok: bool | None     # None: no target
    details: dict[str, Any] = field(default_factory=dict)


def _status(ok: bool | None) -> str:
    return NO_TARGET if ok is None else (OK_MARK if ok else MISS_MARK)


def build_rows(results: dict[str, Any]) -> list[Row]:
    ops_ = results.get("ops", {})
    data = results.get("data", {})
    rows: list[Row] = []

    def best(name: str) -> dict[str, Any] | None:
        o = ops_.get(name)
        return o.get("best") if o and o.get("best") else None

    def peak(name: str) -> int | None:
        o = ops_.get(name)
        return o.get("peak_rss") if o else None

    def size_of(key: str) -> str:
        info = data.get(key) or {}
        return fmt_mb(info.get("bytes"))

    def failed(name: str, section: str, action: str, target: str) -> None:
        o = ops_.get(name)
        if o is None:
            return
        rows.append(Row(name, section, action, target, t("bench.res.error", error=o.get("error") or "?"),
                        _status(False), False, {"error": o.get("error")}))

    def add(name: str, section: str, action: str, target: str, fn: Callable[[dict[str, Any]], tuple[str, bool | None]],
            details: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        b = best(name)
        if b is None:
            failed(name, section, action, target)
            return
        result, ok = fn(b)
        d = {"peak_rss": peak(name), "op": name}
        if details is not None:
            d.update(details(b))
        rows.append(Row(name, section, action, target, result, _status(ok), ok, d))

    file_2g = DATA_SPECS["syn_2g"].filename
    # -- (1) open + first records -----------------------------------------------------------
    add("open_2g", "main", t("bench.row.open", n=50, file=file_2g, size=size_of("syn_2g")),
        t("bench.target.open"),
        lambda b: (t("bench.res.open", first=fmt_s(b["first_records_seconds"]), open=fmt_s(b.get("open_seconds")),
                     start=fmt_s(b.get("since_process_start_seconds"))),
                   b["first_records_seconds"] < T_OPEN_S))
    # -- (2) full index (Rust) -----------------------------------------------------------------
    add("index_2g", "main", t("bench.row.index", file=file_2g), t("bench.target.index"),
        lambda b: (t("bench.res.index", rate=fmt_rate(b["mb_per_s"]), size=fmt_mb(b.get("data_size")),
                     seconds=fmt_s(b["seconds"]), records=group(b.get("record_count")),
                     first=fmt_s(b.get("first_record_seconds")), backend=b.get("reader_backend")),
                   b["mb_per_s"] >= T_INDEX_MBPS and b.get("reader_backend") == "rust"))
    # -- (3) node with 1 million children ---------------------------------------------------------
    add("node_1m", "main", t("bench.row.node", count=group((best("node_1m") or {}).get("child_count")),
                             file=DATA_SPECS["node_1m"].filename),
        t("bench.target.node"),
        lambda b: (t("bench.res.node", node=fmt_s(b["node_open_seconds"]), scan=fmt_mb(b.get("node_scan_bytes")),
                     total=fmt_s(b.get("open_to_node_seconds")), tail=fmt_s(b.get("node_tail_seconds")),
                     reopen=fmt_s(b.get("node_reopen_seconds"))),
                   b["node_open_seconds"] < T_NODE_S))

    # -- (4) full-text search, byte mode ---------------------------------------------------------
    def search_res(b: dict[str, Any]) -> tuple[str, bool]:
        ok = (b["mb_per_s"] >= T_SEARCH_MBPS and b.get("mode_used") == "bytes"
              and (b.get("first_hit_seconds") is not None and b["first_hit_seconds"] < T_FIRST_HIT_S))
        return (t("bench.res.search", rate=fmt_rate(b["mb_per_s"]), seconds=fmt_s(b["seconds"]),
                  hits=group(b.get("hit_count")), records=group(b.get("matching_records")),
                  first=fmt_s(b.get("first_hit_seconds")), mode=_mode_text(b.get("mode_used"))), ok)

    def search_details(b: dict[str, Any]) -> dict[str, Any]:
        return {k: b.get(k) for k in ("records_scanned", "records_parsed", "seconds", "matching_records",
                                      "hit_count", "mb_per_s")}

    for name, text, key in (("search_rare", RARE_TEXT, "bench.row.search_rare"),
                            ("search_common", COMMON_TEXT, "bench.row.search_common")):
        b0 = best(name)
        share = None
        if b0 and b0.get("records_scanned"):
            share = 100.0 * (b0.get("matching_records") or 0) / b0["records_scanned"]
        add(name, "main", t(key, text=text, share=f"{share:.3g}" if share is not None else "?"),
            t("bench.target.search"), search_res, search_details)
    # -- (5) sort: what the user gets -----------------------------------------------------------
    # AGENT_PROMPT §6.4: the sort result is exported in that order, so the §12 row is `sort+export`:
    # Document.open, index, sort_records and the export of the sorted set until the output file is
    # written (like `jsonreshape sort`). The sorted export reads the records in key order, i.e. from
    # random positions of the file; sort_records alone (op `sort`) cannot show its cost and is only
    # an extra row ("one phase").
    def op_records(name: str) -> str:
        n = (best(name) or {}).get("records")
        if n is None and name in ops_:
            n = (data.get(ops_[name]["spec"]["data"]) or {}).get("records")
        return group(n)

    def op_key(name: str) -> str:
        return str(((best(name) or {}).get("keys") or ["?"])[0])

    def op_file(name: str) -> str:
        return DATA_SPECS[ops_[name]["spec"]["data"]].filename

    def op_fmt(name: str) -> str:
        return str((best(name) or {}).get("fmt") or "jsonl").upper()

    def phase_details(b: dict[str, Any]) -> dict[str, Any]:
        return {k: b.get(k) for k in ("total_seconds", "index_seconds", "sort_seconds", "filter_seconds",
                                      "export_seconds", "export_mb_per_s", "records", "exported", "matched",
                                      "bytes_written") if k in b}

    def sort_export_res(name: str) -> Callable[[dict[str, Any]], tuple[str, bool]]:
        def fn(b: dict[str, Any]) -> tuple[str, bool]:
            pk = peak(name)
            ok = b["total_seconds"] < T_SORT_S and pk is not None and pk < T_SORT_RSS
            return (t("bench.res.sort_export", total=fmt_s(b["total_seconds"]), peak=fmt_mb(pk),
                      index=fmt_s(b.get("index_seconds")), sort=fmt_s(b.get("sort_seconds")),
                      strategy=_strategy_text(b.get("strategy")), runs=b.get("runs"),
                      export=fmt_s(b.get("export_seconds")), rate=fmt_rate(b.get("export_mb_per_s"))), ok)
        return fn

    def sort_export_row(name: str, section: str) -> None:
        add(name, section, t("bench.row.sort_export", records=op_records(name), key=op_key(name), fmt=op_fmt(name),
                             file=op_file(name)),
            t("bench.target.sort"), sort_export_res(name), phase_details)

    sort_main = next((name for name in SORT_MAIN_OPS if name in ops_), None)
    if sort_main is not None:
        sort_export_row(sort_main, "main")

    # -- (6) export --------------------------------------------------------------------------------
    def export_res(name: str) -> Callable[[dict[str, Any]], tuple[str, bool]]:
        def fn(b: dict[str, Any]) -> tuple[str, bool]:
            pk = peak(name)
            ok = b["mb_per_s"] >= T_EXPORT_MBPS and pk is not None and pk < T_EXPORT_RSS
            return (t("bench.res.export", rate=fmt_rate(b["mb_per_s"]), seconds=fmt_s(b["seconds"]), peak=fmt_mb(pk),
                      out=fmt_mb(b.get("bytes_written"))), ok)
        return fn

    add("export_jsonl", "main", t("bench.row.export_jsonl", file=file_2g), t("bench.target.export"),
        export_res("export_jsonl"))
    add("export_json2", "main", t("bench.row.export_json2", file=file_2g), t("bench.target.export"),
        export_res("export_json2"))
    # -- (7) overall memory -------------------------------------------------------------------------
    non_sort = [(o.get("peak_rss"), name) for name, o in ops_.items()
                if not o["spec"].get("is_sort") and o.get("peak_rss")]
    if non_sort:
        pk, who = max(non_sort)
        ok = pk < T_TOTAL_RSS
        rows.append(Row("memory", "main", t("bench.row.memory"), t("bench.target.memory"),
                        t("bench.res.memory", peak=fmt_mb(pk), op=who, n=len(non_sort)), _status(ok), ok,
                        {"peak_rss": pk, "op": who}))

    # -- extra measurements -----------------------------------------------------------------------
    # the 5 GB array of the §12 heading: the same operations on a file 2.5 times larger
    file_5g = DATA_SPECS["syn_5g"].filename
    add("open_5g", "extra", t("bench.row.open", n=50, file=file_5g, size=size_of("syn_5g")), t("bench.target.open"),
        lambda b: (t("bench.res.open", first=fmt_s(b["first_records_seconds"]), open=fmt_s(b.get("open_seconds")),
                     start=fmt_s(b.get("since_process_start_seconds"))),
                   b["first_records_seconds"] < T_OPEN_S))
    add("index_5g", "extra", t("bench.row.index", file=file_5g), t("bench.target.index"),
        lambda b: (t("bench.res.index", rate=fmt_rate(b["mb_per_s"]), size=fmt_mb(b.get("data_size")),
                     seconds=fmt_s(b["seconds"]), records=group(b.get("record_count")),
                     first=fmt_s(b.get("first_record_seconds")), backend=b.get("reader_backend")),
                   b["mb_per_s"] >= T_INDEX_MBPS and b.get("reader_backend") == "rust"))
    b5 = best("search_rare_5g")
    share5 = None
    if b5 and b5.get("records_scanned"):
        share5 = 100.0 * (b5.get("matching_records") or 0) / b5["records_scanned"]
    add("search_rare_5g", "extra", t("bench.row.search_file", file=file_5g, text=RARE_TEXT,
                                     share=f"{share5:.3g}" if share5 is not None else "?"),
        t("bench.target.search"), search_res, search_details)
    add("reopen_2g", "extra", t("bench.row.reopen", n=50), t("bench.target.open"),
        lambda b: (t("bench.res.reopen", first=fmt_s(b["first_records_seconds"]), open=fmt_s(b.get("open_seconds")),
                     records=group(b.get("record_count")), cached=t("common.yes") if b.get("index_from_cache")
                     else t("common.no")),
                   b["first_records_seconds"] < T_OPEN_S and bool(b.get("index_from_cache"))))
    add("node_1m_auto", "extra", t("bench.row.node_auto"), t("bench.target.open"),
        lambda b: (t("bench.res.node_auto", total=fmt_s(b["open_to_node_seconds"]), open=fmt_s(b.get("open_seconds")),
                     root=fmt_s(b.get("root_children_seconds")), node=fmt_s(b.get("node_open_seconds"))),
                   b["open_to_node_seconds"] < T_OPEN_S))
    b_med = best("search_medium")
    share_med = None
    if b_med and b_med.get("records_scanned"):
        share_med = 100.0 * (b_med.get("matching_records") or 0) / b_med["records_scanned"]
    add("search_medium", "extra", t("bench.row.search_medium", text=MEDIUM_TEXT,
                                    share=f"{share_med:.3g}" if share_med is not None else "?"),
        t("bench.target.search"), search_res, search_details)
    add("search_live", "extra", t("bench.row.search_live", text=RARE_TEXT), t("bench.target.first_hit"),
        lambda b: (t("bench.res.search_live", first=fmt_s(b.get("first_hit_seconds")), total=fmt_s(b["total_seconds"]),
                     mode=_mode_text(b.get("mode_used")), parsed=group(b.get("records_parsed"))),
                   b.get("first_hit_seconds") is not None and b["first_hit_seconds"] < T_FIRST_HIT_S))
    # sorting: the other end-to-end run, then sort_records alone (one phase, no target of its own)
    for name in SORT_MAIN_OPS:
        if name in ops_ and name != sort_main:
            sort_export_row(name, "extra")

    def sort_res(name: str) -> Callable[[dict[str, Any]], tuple[str, None]]:
        def fn(b: dict[str, Any]) -> tuple[str, None]:
            return (t("bench.res.sort", seconds=fmt_s(b["seconds"]), peak=fmt_mb(peak(name)),
                      strategy=_strategy_text(b.get("strategy")), runs=b.get("runs"),
                      rate=fmt_rate(b.get("mb_per_s"))), None)
        return fn

    for name, key in (("sort_10m", "bench.row.sort_only"), ("sort_2g", "bench.row.sort_only"),
                      ("sort_10m_num", "bench.row.sort_num"), ("sort_10m_unique", "bench.row.sort_unique")):
        if name in ops_:
            add(name, "extra", t(key, records=op_records(name), key=op_key(name), file=op_file(name)),
                t("bench.target.part"), sort_res(name))
    # JSON Lines indexing speed, measured by the operations on it before their own work
    for name in ("sort_10m", "sort_10m_num", "sort_export_10m", "filter_export_10m"):
        o = ops_.get(name)
        if o and o.get("runs"):
            idx = [r["child"] for r in o["runs"] if r.get("ok") and (r.get("child") or {}).get("index_mb_per_s")]
            if idx:
                b = max(idx, key=lambda c: c["index_mb_per_s"])
                ok = b["index_mb_per_s"] >= T_INDEX_MBPS and b.get("reader_backend") == "rust"
                rows.append(Row("index_jsonl", "extra", t("bench.row.index_jsonl",
                                                          file=DATA_SPECS["syn_10m"].filename),
                                t("bench.target.index"),
                                t("bench.res.index", rate=fmt_rate(b["index_mb_per_s"]), size=fmt_mb(b.get("data_size")),
                                  seconds=fmt_s(b["index_seconds"]), records=group(b.get("record_count")),
                                  first=fmt_s((b.get("index_stats") or {}).get("first_record_seconds")),
                                  backend=b.get("reader_backend")),
                                _status(ok), ok, {"op": name}))
                break
    for name, key in (("filter_extract", "bench.row.filter"), ("filter_parse", "bench.row.filter")):
        b0 = best(name) or {}
        add(name, "extra", t(key, expr=FILTER_EXPR, strategy=b0.get("strategy", "?")), t("bench.target.none"),
            lambda b: (t("bench.res.filter", rate=fmt_rate(b["mb_per_s"]), seconds=fmt_s(b["seconds"]),
                         matched=group(b.get("matched")), scanned=group(b.get("scanned"))), None))

    def filter_export_res(name: str) -> Callable[[dict[str, Any]], tuple[str, None]]:
        def fn(b: dict[str, Any]) -> tuple[str, None]:
            return (t("bench.res.filter_export", total=fmt_s(b["total_seconds"]), rate=fmt_rate(b.get("mb_per_s")),
                      peak=fmt_mb(peak(name)), index=fmt_s(b.get("index_seconds")),
                      filter=fmt_s(b.get("filter_seconds")), strategy=_strategy_text(b.get("strategy")),
                      export=fmt_s(b.get("export_seconds")), matched=group(b.get("matched")),
                      scanned=group(b.get("scanned")), out=fmt_mb(b.get("bytes_written"))), None)
        return fn

    for name in ("filter_export_2g", "filter_export_10m"):
        if name in ops_:
            add(name, "extra", t("bench.row.filter_export", expr=FILTER_EXPR, fmt=op_fmt(name), file=op_file(name)),
                t("bench.target.none"), filter_export_res(name), phase_details)
    add("export_csv", "extra", t("bench.row.export_csv"), t("bench.target.export"), export_res("export_csv"))
    add("export_csv_scalar", "extra", t("bench.row.export_csv_scalar", n=len(SCALAR_COLUMNS.split(","))),
        t("bench.target.export"), export_res("export_csv_scalar"))
    add("index_python", "extra", t("bench.row.index_python"), t("bench.target.index_python"),
        lambda b: (t("bench.res.index", rate=fmt_rate(b["mb_per_s"]), size=fmt_mb(b.get("data_size")),
                     seconds=fmt_s(b["seconds"]), records=group(b.get("record_count")),
                     first=fmt_s(b.get("first_record_seconds")), backend=b.get("reader_backend")),
                   b["mb_per_s"] >= T_PY_INDEX_MBPS and b.get("reader_backend") == "python"))
    add("schema_2g", "extra", t("bench.row.schema", n=group(10000)), t("bench.target.none"),
        lambda b: (t("bench.res.schema", seconds=fmt_s(b["seconds"]), fields=b.get("fields"),
                     records=group(b.get("records_analysed")), total=group(b.get("total_records"))), None))
    b_cold = best("cold_2g")
    if b_cold is not None:
        disk_read = b_cold.get("disk_read_bytes")
        add("cold_2g", "extra", t("bench.row.cold_open", n=50), t("bench.target.open"),
            lambda b: (t("bench.res.cold_open", first=fmt_s(b["first_records_seconds"]), open=fmt_s(b.get("open_seconds")),
                         disk=fmt_mb(disk_read)), b["first_records_seconds"] < T_OPEN_S))
        rate = _num(b_cold, "mb_per_s")
        ok = rate is not None and rate >= T_INDEX_MBPS
        rows.append(Row("cold_index", "extra", t("bench.row.cold_index"), t("bench.target.index"),
                        t("bench.res.cold_index", rate=fmt_rate(rate), seconds=fmt_s((b_cold.get("index_stats") or {})
                                                                                  .get("seconds")),
                          disk=fmt_mb(disk_read), size=fmt_mb(b_cold.get("data_size"))),
                        _status(ok), ok, {"op": "cold_2g", "disk_read_bytes": disk_read}))
    elif "cold_2g" in ops_:
        failed("cold_2g", "extra", t("bench.row.cold_open", n=50), t("bench.target.open"))
    add("coldread_2g", "extra", t("bench.row.coldread"), t("bench.target.none"),
        lambda b: (t("bench.res.coldread", read=fmt_rate(b.get("read_mb_per_s")), scan=fmt_rate(b.get("scan_mb_per_s")),
                     disk=fmt_mb(b.get("disk_read_bytes"))), None))
    return rows


# ----------------------------------------------------------------------------------
# misses: cause and options
# ----------------------------------------------------------------------------------
def miss_texts(row: Row, results: dict[str, Any]) -> tuple[str, str]:
    """(cause, options) of a missed target, with the measured numbers filled in."""
    ops_ = results.get("ops", {})
    if row.details.get("error"):
        return (t("bench.miss.failed.cause", error=row.details["error"]), t("bench.miss.failed.options"))
    params: dict[str, Any] = {"op": row.details.get("op") or row.id}
    key = row.id
    if row.id.startswith("search_") and row.id != "search_live":
        key = "search"
        b = (ops_.get(row.id) or {}).get("best") or {}
        scanned = b.get("records_scanned") or 0
        parsed = b.get("records_parsed") or 0
        secs = b.get("seconds") or 0
        rare = (ops_.get("search_rare") or {}).get("best") or {}
        params.update({"parsed": group(parsed), "scanned": group(scanned),
                       "share": f"{100.0 * parsed / scanned:.0f}" if scanned else "?",
                       "per_s": group(int(parsed / secs)) if secs else "?",
                       "mode": b.get("mode_used"), "rare": fmt_rate(rare.get("mb_per_s")) if rare else "?"})
        if b.get("mode_used") != "bytes":
            key = "search_parse"
    elif row.id in ("export_csv", "export_csv_scalar"):
        key = "export_csv"
        sc = (ops_.get("export_csv_scalar") or {}).get("best") or {}
        params["scalar"] = fmt_rate(sc.get("mb_per_s")) if sc else "?"
    elif row.id in SORT_MAIN_OPS:
        key = "sort_export"
        b = (ops_.get(row.id) or {}).get("best") or {}
        total, export_s = _num(b, "total_seconds"), _num(b, "export_seconds")
        plain = (ops_.get("export_jsonl") or {}).get("best") or {}
        params.update({"total": fmt_s(total), "index": fmt_s(_num(b, "index_seconds")),
                       "sort": fmt_s(_num(b, "sort_seconds")), "export": fmt_s(export_s),
                       "rate": fmt_rate(_num(b, "export_mb_per_s")),
                       "share": f"{100.0 * export_s / total:.0f}" if total and export_s is not None else "?",
                       "peak": fmt_mb(row.details.get("peak_rss")),
                       "plain": fmt_rate(_num(plain, "mb_per_s")) if plain else "?"})
    cause_key, options_key = f"bench.miss.{key}.cause", f"bench.miss.{key}.options"
    if has(cause_key) and has(options_key):
        return t(cause_key, **params), t(options_key, **params)
    return t("bench.miss.generic.cause", **params), t("bench.miss.generic.options", **params)


# ----------------------------------------------------------------------------------
# output files
# ----------------------------------------------------------------------------------
def _table(rows: list[Row]) -> list[str]:
    lines = [f"| {t('bench.col.action')} | {t('bench.col.target')} | {t('bench.col.result')} | {t('bench.col.status')} |",
             "|---|---|---|:---:|"]
    for r in rows:
        cells = [c.replace("|", "\\|").replace("\n", " ") for c in (r.action, r.target, r.result, r.status)]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_markdown(results: dict[str, Any], rows: list[Row], json_name: str | None = None) -> str:
    m = results.get("machine", {})
    cond = results.get("conditions", {})
    loads = cond.get("load_samples", {})
    data = results.get("data", {})
    out: list[str] = [f"# {t('bench.md.title')}", ""]
    out.append(t("bench.md.meta", date=str(results.get("started", "")).replace("T", " "),
                 duration=fmt_s(results.get("duration_seconds")), mode=results.get("mode"),
                 json=json_name or "?"))
    out.append("")
    if results.get("mode") not in ("full",):
        out.append("> " + t("bench.md.partial", mode=results.get("mode")))
        out.append("")
    main = [r for r in rows if r.section == "main"]
    extra = [r for r in rows if r.section == "extra"]
    out += [f"## {t('bench.md.main')}", ""] + _table(main) + [""]
    out.append(t("bench.md.legend"))
    out.append("")
    if extra:
        out += [f"## {t('bench.md.extra')}", ""] + _table(extra) + [""]
    misses = [r for r in rows if r.ok is False]
    out += [f"## {t('bench.md.misses')}", ""]
    if not misses:
        out.append(t("bench.md.no_misses"))
    for r in misses:
        cause, options = miss_texts(r, results)
        out.append(f"- **{r.action}** \u2014 {r.result} ({t('bench.col.target').lower()}: {r.target}).")
        out.append(f"  - {t('bench.md.cause')}: {cause}")
        out.append(f"  - {t('bench.md.options')}: {options}")
    out.append("")
    # data files
    out += [f"## {t('bench.md.data')}", ""]
    for key, info in data.items():
        out.append("- " + t("bench.md.data_line", name=DATA_SPECS[key].filename, size=fmt_mb(info.get("bytes")),
                             records=group(info.get("records")),
                             how=t("bench.md.generated") if info.get("generated") else t("bench.md.reused")))
    out.append("")
    # machine
    disk = m.get("disk") or {}
    split = ""
    if m.get("p_cores"):
        split = t("bench.md.pe_split", p=m.get("p_cores"), e=m.get("e_cores"))
    versions = m.get("versions") or {}
    out += [f"## {t('bench.md.machine')}", ""]
    out.append("- " + t("bench.md.cpu", cpu=m.get("cpu"), cores=m.get("cores_physical"),
                         logical=m.get("cores_logical"), split=split))
    out.append("- " + t("bench.md.ram", ram=fmt_mb(m.get("ram_total"))))
    out.append("- " + t("bench.md.disk", model=disk.get("model") or "?", bus=disk.get("bus") or "?",
                         media=disk.get("media") or "?", drive=disk.get("drive") or "?", fs=disk.get("fs") or "?",
                         free=fmt_mb(disk.get("free"))))
    out.append("- " + t("bench.md.os", os=m.get("os"), power=m.get("power_plan") or "?"))
    out.append("- " + t("bench.md.python", python=m.get("python"), backend=m.get("core_backend"),
                         orjson=versions.get("orjson") or "?", ijson=versions.get("ijson") or "?",
                         ijson_backend=m.get("ijson_backend") or "?", version=m.get("app_version") or "?"))
    out.append("")
    # conditions
    out += [f"## {t('bench.md.conditions')}", ""]
    reps = sorted({len(o["runs"]) for o in results.get("ops", {}).values() if o.get("runs")})
    out.append("- " + t("bench.cond.process"))
    if any(o["spec"].get("op") in ("sort+export", "filter+export") for o in results.get("ops", {}).values()):
        out.append("- " + t("bench.cond.end_to_end"))
    out.append("- " + t("bench.cond.memory", diff=_peak_agreement(results)))
    out.append("- " + t("bench.cond.repeat", repeats="/".join(str(r) for r in reps) or "?"))
    if cond.get("wait_for_quiet"):
        bl = loads.get("before") or {}
        out.append("- " + t("bench.cond.load", limit=cond.get("quiet_load_limit"), timeout=cond.get("quiet_timeout"),
                             min=bl.get("min", "?"), max=bl.get("max", "?"), avg=bl.get("avg", "?"),
                             waited=loads.get("runs_waited_out", 0), seconds=loads.get("wait_seconds", 0)))
    else:
        out.append("- " + t("bench.cond.no_wait"))
    oc = loads.get("others_cores") or {}
    if oc:
        out.append("- " + t("bench.cond.others", avg=oc.get("avg"), max=oc.get("max"),
                             logical=m.get("cores_logical")))
    cold = (results.get("ops", {}).get("cold_2g") or {}).get("best") or {}
    out.append("- " + t("bench.cond.cache", ram=fmt_mb(m.get("ram_total"))))
    if cold:
        out.append("- " + t("bench.cond.cold", disk=fmt_mb(cold.get("disk_read_bytes")),
                             size=fmt_mb(cold.get("data_size"))))
    out.append("- " + t("bench.cond.size", records=group((data.get("syn_2g") or {}).get("records")),
                         size2g=fmt_mb((data.get("syn_2g") or {}).get("bytes")),
                         size10m=fmt_mb((data.get("syn_10m") or {}).get("bytes")),
                         records10m=group((data.get("syn_10m") or {}).get("records"))))
    if data.get("syn_5g"):
        out.append("- " + t("bench.cond.size5g", size5g=fmt_mb(data["syn_5g"].get("bytes")),
                             records5g=group(data["syn_5g"].get("records"))))
    if any(n.startswith("sort_") for n in results.get("ops", {})):
        out.append("- " + t("bench.cond.cardinality"))
    out.append("- " + t("bench.cond.export"))
    if m.get("p_cores"):
        out.append("- " + t("bench.cond.hybrid", p=m.get("p_cores"), e=m.get("e_cores")))
    out.append("")
    out += [f"## {t('bench.md.not_measured')}", ""]
    for key in ("bench.nm.laptop", "bench.nm.cold_all", "bench.nm.gui", "bench.nm.mapping"):
        out.append("- " + t(key, cpu=m.get("cpu")))
    out.append("")
    out.append(t("bench.md.rerun"))
    out.append("")
    return "\n".join(out)


def write_results(results: dict[str, Any], rows: list[Row], results_dir: Path, cfg: RunConfig) -> dict[str, str]:
    results_dir.mkdir(parents=True, exist_ok=True)
    run_id = results["run_id"]
    jpath = results_dir / f"{run_id}.json"
    mpath = results_dir / f"{run_id}.md"
    files = {"json": str(jpath), "md": str(mpath)}
    results["files"] = files
    latest = cfg.write_latest if cfg.write_latest is not None else (results.get("mode") == "full")
    if latest:
        files["latest"] = str(results_dir / "latest.md")
    md = render_markdown(results, rows, jpath.name)
    jpath.write_bytes((json.dumps(results, ensure_ascii=False, indent=1, default=str) + "\n").encode("utf-8"))
    mpath.write_bytes(md.encode("utf-8"))
    if latest:
        (results_dir / "latest.md").write_bytes(md.encode("utf-8"))
    return files


# ----------------------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run_bench.py", description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--subset", choices=["2g", "5g"],
                   help="2g: index, search, sort, export, sort+export and filter+export of the 2 GB file; "
                        "5g: open, index, search of 5 GB")
    g.add_argument("--only", help="comma-separated operation names (see --list)")
    g.add_argument("--smoke", action="store_true", help="every operation once on tiny files (machinery check)")
    ap.add_argument("--list", action="store_true", help="list the operations and exit")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--work-dir", type=Path, default=None)
    ap.add_argument("--repeat", type=int, default=None, help="runs per operation (default: per operation)")
    ap.add_argument("--quiet-load", type=float, default=QUIET_LOAD, help="percent of all CPUs")
    ap.add_argument("--quiet-timeout", type=float, default=QUIET_TIMEOUT, help="seconds")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for a quiet machine")
    ap.add_argument("--keep-work", action="store_true", help="keep homes, outputs and cold copies")
    ap.add_argument("--timeout", type=float, default=OP_TIMEOUT, help="seconds per run")
    ap.add_argument("--lang", choices=["uz", "en"], default="uz")
    ap.add_argument("--latest", action="store_true", help="also write latest.md for a partial run")
    return ap


def config_from_args(argv: list[str] | None = None) -> tuple[RunConfig, argparse.Namespace]:
    args = build_parser().parse_args(argv)
    mode = "smoke" if args.smoke else (args.subset or "full")
    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    cfg = RunConfig(mode=mode, only=only, data_dir=args.data_dir, results_dir=args.results_dir,
                    work_dir=args.work_dir, repeat=args.repeat, quiet_load=args.quiet_load,
                    quiet_timeout=args.quiet_timeout, wait=not (args.no_wait or args.smoke),
                    keep_work=args.keep_work, lang=args.lang, timeout=args.timeout,
                    write_latest=True if args.latest else None,
                    command=["run_bench.py", *(argv if argv is not None else sys.argv[1:])])
    return cfg, args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a cp1251/cp866 console cannot print every character
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg, args = config_from_args(argv)
    if args.list:
        for s in SPECS:
            sys.stdout.write(f"{s.name:18} {s.op:13} {DATA_SPECS[s.data].filename:14} {' '.join(s.args)}"
                             f"  [{', '.join(s.groups)}]\n")
        return 0
    results = run(cfg)
    set_language(cfg.lang)
    rows = [Row(**r) for r in results["rows"]]
    for r in rows:
        sys.stdout.write(f"{r.status} {r.action}: {r.result}\n")
    sys.stdout.write(t("bench.log.written", json=results["files"]["json"], md=results["files"]["md"]) + "\n")
    return 0 if results["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
