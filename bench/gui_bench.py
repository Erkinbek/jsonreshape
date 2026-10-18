r"""GUI benchmark of the stage-2 viewer: the AGENT_PROMPT §12 targets measured in the real GUI
(``jsonreshape.gui.app.main``), next to the library-level numbers of ``bench/run_bench.py``.

Every measurement runs in a fresh child process (``gui_bench.py --child <scenario>``, Qt
``offscreen`` platform with ``QT_QPA_FONTDIR=C:/Windows/Fonts``, a fresh empty
``JSONRESHAPE_HOME``), which starts the viewer exactly like a user does and reports one JSON
line; the runner watches the memory of the child's process tree (``run_bench.run_child``).

Scenarios (one child each, repeated ``--repeat`` times -- default 2 -- the best timing of
every value is kept, the memory is the largest peak of all runs):

* ``open_index`` (2 GB / 5 GB arrays, 10 M-record JSON Lines): process launched ->
  main window -> first records painted in the tree (§12 "< 1 s"); the index speed inside
  the GUI (§12 "≥ 150 MB/s") while a 5 ms heartbeat timer of the GUI thread measures how
  responsive the UI stays (gaps p95 / max); then the scrollbar jumps to the end and the time
  until every visible row is painted.
* ``node``: the "items" array with 1 000 000 elements (opened as a single document):
  expanded with the Right key the first time (its children are indexed in a task) and again
  after a collapse (cached) -- time until the visible children are painted (§12 "< 200 ms");
  the jump to the last child.
* ``search`` (2 GB): the search panel (query + Enter): time to the first hit painted in the
  results grid, MB/s (§12 "≥ 100 MB/s, natijalar darhol"), heartbeat.
* ``filter`` (10 M JSON Lines): the filter panel's Apply: time to the first partial rows
  painted in the tree, total time, MB/s, heartbeat.
* ``sort_export`` (10 M JSON Lines): ``session.set_sort([SortKey("name")])`` -- time,
  peak RSS (§12 "< 2 daqiqa, RAM < 1 GB"), heartbeat -- then the export of the sorted view
  to JSON Lines through the export dialog's code path (``ExportDialog.start_export``):
  MB/s and the RSS of that phase.
* ``session`` (2 GB): a whole session without a sort (open, index, scroll, expand, table
  view, search, filter, schema view, text view, export of the filtered view): the peak RSS
  of the process (§12 "< 500 MB") and the RSS after closing the tab.

Painting is measured by wrapping ``VirtualGrid._paint`` (the cells it painted vs. the
placeholders of rows not loaded yet); "painted" means a Qt paint event of the grid ended
with real cells (offscreen: into the backing-store image, not onto a physical screen).

Usage (from the project root)::

    .venv/Scripts/python.exe bench/gui_bench.py               # full: bench/data big files (~10 minutes)
    .venv/Scripts/python.exe bench/gui_bench.py --quick       # generated ~200 MB files, ~1-2 minutes
    .venv/Scripts/python.exe bench/gui_bench.py --only node_1m,search_2g --repeat 1
    .venv/Scripts/python.exe bench/gui_bench.py --list

Results: ``bench/results/gui_<YYYYMMDD-HHMMSS>.json`` / ``.md`` and, for a full run (or with
``--latest``), ``bench/results/gui_latest.md`` (the Uzbek "Amal | Maqsad | Natija | Holat"
table, ``--lang en`` for English). Generated quick files, homes and export outputs live in
the work folder (``--work-dir``, default ``<data dir>/work/gui-<run id>``), removed at the end
(``--keep-work`` keeps them).
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import shutil
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
for _p in (str(ROOT), str(BENCH)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from jsonreshape import i18n  # noqa: E402
from jsonreshape.i18n import get_language, set_language, t  # noqa: E402

# ----------------------------------------------------------------------------------
# texts (Uzbek primary, English): a developer tool -- registered into i18n.MESSAGES at
# import like bench/run_bench.py does (a key jsonreshape/i18n.py defines itself wins)
# ----------------------------------------------------------------------------------
GUI_MESSAGES: dict[str, dict[str, str]] = {
    "bench.gui.log.run": {
        "uz": "[{n}/{total}] {name} {i}/{repeats}: {summary}, peak RSS {peak}, jarayon {wall}",
        "en": "[{n}/{total}] {name} {i}/{repeats}: {summary}, peak RSS {peak}, process {wall}",
    },
    "bench.gui.log.failed": {"uz": "[{n}/{total}] {name} {i}/{repeats}: XATO: {error}",
                             "en": "[{n}/{total}] {name} {i}/{repeats}: FAILED: {error}"},
    "bench.gui.log.generate": {"uz": "Tezkor rejim fayli yaratilmoqda: {name} ({args})",
                               "en": "Generating the quick-mode file {name} ({args})"},
    "bench.gui.log.written": {"uz": "Natijalar yozildi: {json}, {md}", "en": "Results written: {json}, {md}"},
    "bench.gui.error.unknown_op": {"uz": "Noma'lum o'lchov(lar): {names}. Mavjudlari: {known}",
                                   "en": "Unknown measurement(s): {names}. Known: {known}"},
    "bench.gui.error.generate": {"uz": "{name} faylini yaratib bo'lmadi: {detail}",
                                 "en": "Could not generate {name}: {detail}"},
    "bench.gui.md.title": {"uz": "jsonreshape: GUI benchmarki (2-bosqich, AGENT_PROMPT §12)",
                           "en": "jsonreshape: GUI benchmark (stage 2, AGENT_PROMPT §12)"},
    "bench.gui.md.meta": {
        "uz": "Sana: {date} · davomiyligi: {duration} · rejim: {mode} · to'liq natijalar: `{json}`",
        "en": "Date: {date} · duration: {duration} · mode: {mode} · full results: `{json}`",
    },
    "bench.gui.md.quick": {
        "uz": "> Tezkor rejim: kichik yaratilgan fayllar ({files}); raqamlar mexanizm tekshiruvi uchun, "
              "§12 dagi 5 GB holat uchun to'liq rejimni ishlating.",
        "en": "> Quick mode: small generated files ({files}); the numbers check the machinery -- run the full "
              "mode for the 5 GB case of §12.",
    },
    "bench.gui.md.main": {"uz": "§12 maqsadlari GUI ichida", "en": "§12 targets inside the GUI"},
    "bench.gui.md.extra": {"uz": "Qo'shimcha o'lchovlar", "en": "Additional measurements"},
    "bench.gui.md.ui": {"uz": "UI javob berishi (GUI oqimidagi 5 ms taymer)",
                        "en": "UI responsiveness (5 ms timer in the GUI thread)"},
    "bench.gui.md.ui_head": {
        "uz": "| Bosqich | Davomiyligi | Oraliqlar p50 / p95 / p99 / max | > 100 ms | > 250 ms |",
        "en": "| Phase | Duration | Gaps p50 / p95 / p99 / max | > 100 ms | > 250 ms |",
    },
    "bench.gui.md.legend": {
        "uz": "✅ maqsadga yetildi · ❌ yetilmadi (pastda) · — maqsad belgilanmagan. Vaqt — eng yaxshi takror, "
              "peak RSS — barcha takrorlarning eng kattasi; MB = 10⁶ bayt.",
        "en": "✅ target met · ❌ missed (below) · — no target. Time: best run; peak RSS: the largest of all runs; "
              "MB = 10⁶ bytes.",
    },
    "bench.gui.md.misses": {"uz": "Maqsadga yetilmagan joylar", "en": "Missed targets"},
    "bench.gui.md.no_misses": {"uz": "Yo'q — barcha maqsadlarga yetildi.", "en": "None -- every target was met."},
    "bench.gui.md.data": {"uz": "Ma'lumot fayllari", "en": "Data files"},
    "bench.gui.md.data_line": {"uz": "`{name}`: {size}, {records} ta yozuv ({how})",
                               "en": "`{name}`: {size}, {records} records ({how})"},
    "bench.gui.md.generated": {"uz": "shu ishga tushirishda yaratildi", "en": "generated by this run"},
    "bench.gui.md.reused": {"uz": "avval yaratilgan", "en": "generated before"},
    "bench.gui.md.machine": {"uz": "Kompyuter", "en": "Machine"},
    "bench.gui.md.conditions": {"uz": "O'lchash shartlari", "en": "Measurement conditions"},
    "bench.gui.md.not_measured": {"uz": "Nimani o'lchab bo'lmadi", "en": "What could not be measured"},
    "bench.gui.md.rerun": {
        "uz": "Qayta o'lchash: `.venv/Scripts/python.exe bench/gui_bench.py` (to'liq) yoki `--quick` (tezkor); "
              "bitta o'lchov: `--only NOM` (`--list`). Kutubxona darajasidagi raqamlar: `bench/results/latest.md`.",
        "en": "Re-run: `.venv/Scripts/python.exe bench/gui_bench.py` (full) or `--quick`; one measurement: "
              "`--only NAME` (`--list`). Library-level numbers: `bench/results/latest.md`.",
    },
    "bench.gui.cond.process": {
        "uz": "Har bir o'lchov alohida yangi jarayonda (`gui_bench.py --child`), yangi bo'sh JSONRESHAPE_HOME bilan "
              "(kesh indeksi yo'q): ko'ruvchi foydalanuvchi ishga tushirgandek `jsonreshape.gui.app.main([fayl])` "
              "orqali ochiladi (haqiqiy MainWindow, ko'rinishlar, panellar, GC siyosati).",
        "en": "Every measurement runs in its own new process (`gui_bench.py --child`) with a new empty "
              "JSONRESHAPE_HOME (no cached index): the viewer starts like a user starts it, "
              "`jsonreshape.gui.app.main([file])` (the real MainWindow, views, panels, GC policy).",
    },
    "bench.gui.cond.offscreen": {
        "uz": "Qt `offscreen` platformasi (`QT_QPA_FONTDIR=C:/Windows/Fonts`, haqiqiy shriftlar), oyna 1440×900. "
              "«Chizildi» — VirtualGrid paint hodisasi haqiqiy kataklar bilan tugadi (yuklanmagan qatorlar "
              "o'rnidagi bo'sh joylar sanaladi); «to'liq» — ko'rinadigan barcha kataklar yuklangan.",
        "en": "Qt `offscreen` platform (`QT_QPA_FONTDIR=C:/Windows/Fonts`, real fonts), window 1440×900. "
              "\"Painted\": a VirtualGrid paint event ended with real cells (placeholders of rows not loaded yet "
              "are counted); \"complete\": every visible cell was loaded.",
    },
    "bench.gui.cond.start": {
        "uz": "«Jarayon ishga tushganidan» — runner jarayonni yaratgan paytdan (venv python.exe ishga tushiruvchisi "
              "va importlar ichida); interpretator jarayoni yaratilganidan va `main()` chaqirilganidan vaqtlar ham "
              "JSON'da.",
        "en": "\"From the process start\": from the moment the runner created the process (venv python.exe launcher "
              "and the imports included); the times from the interpreter's creation and from the `main()` call are "
              "in the JSON too.",
    },
    "bench.gui.cond.heartbeat": {
        "uz": "UI javob berishi: GUI oqimida 5 ms li aniq (PreciseTimer) taymer; ketma-ket ikki urish orasidagi "
              "vaqt — foydalanuvchi kiritishi kutadigan eng uzoq vaqt. 250 ms dan uzun oraliq — sezilarli qotish "
              "(o'z chegaramiz; §12 da raqam yo'q).",
        "en": "UI responsiveness: a 5 ms precise timer in the GUI thread; the time between two ticks is the longest "
              "time user input waits. A gap above 250 ms is a noticeable freeze (our own limit; §12 has no number).",
    },
    "bench.gui.cond.memory": {
        "uz": "Peak RSS — jarayonlar daraxtining eng katta ishchi to'plami (run_bench.TreeWatcher, Windows "
              "peak_wset; venv ishga tushiruvchisi va haqiqiy interpretator); bosqich RSS'i — jarayon ichida har 50 ms "
              "olingan namunalarning eng kattasi.",
        "en": "Peak RSS: the largest working set of the process tree (run_bench.TreeWatcher, Windows peak_wset; the "
              "venv launcher and the real interpreter); a phase's RSS: the largest of the samples taken every 50 ms "
              "inside the process.",
    },
    "bench.gui.cond.repeat": {
        "uz": "Takrorlar: {repeats}; har bir qiymatning eng yaxshisi olinadi (kompyuter umumiy).",
        "en": "Runs: {repeats}; the best of every value is kept (shared machine).",
    },
    "bench.gui.cond.load": {
        "uz": "Har o'lchovdan oldin tizim CPU yuklamasi ≤ {limit}% bo'lguncha kutildi (ko'pi bilan {timeout} s); "
              "ko'rilgan yuklama: {min}–{max}%.",
        "en": "Before every run the system CPU load was awaited until ≤ {limit}% (at most {timeout} s); "
              "load seen: {min}–{max}%.",
    },
    "bench.gui.cond.no_wait": {"uz": "Yuklama kutilmadi (--no-wait).", "en": "The load was not awaited (--no-wait)."},
    "bench.gui.cond.cache": {
        "uz": "OS fayl keshi iliq (ma'lumot fayllari RAM'da); sovuq disk o'lchovlari `bench/results/latest.md` da.",
        "en": "The OS file cache is warm (the data files are in RAM); cold-disk numbers: `bench/results/latest.md`.",
    },
    "bench.gui.nm.screen": {
        "uz": "Haqiqiy ekranga chiqish (GPU, kompozitor, monitor yangilanishi) o'lchanmadi: offscreen platforma "
              "faqat Qt'ning o'z chizishini o'lchaydi.",
        "en": "Getting onto a real screen (GPU, compositor, monitor refresh) was not measured: the offscreen platform "
              "measures Qt's own painting only.",
    },
    "bench.gui.nm.laptop": {
        "uz": "§12 «oddiy noutbuk» uchun yozilgan; o'lchov {cpu} da bajarildi.",
        "en": "§12 is written for \"an ordinary laptop\"; this ran on {cpu}.",
    },
    "bench.gui.nm.edit": {
        "uz": "Tahrir, undo, mapping bilan eksport — 3- va 4-bosqichlarda.",
        "en": "Editing, undo, export with a mapping -- stages 3 and 4.",
    },
    # rows
    "bench.gui.row.open": {
        "uz": "Ochish: jarayon ishga tushganidan daraxtda birinchi yozuvlar chizilguncha ({file}, {size})",
        "en": "Open: from the process start until the first records are painted in the tree ({file}, {size})",
    },
    "bench.gui.row.open.result": {
        "uz": "{first} (`main()` chaqirilganidan {from_main}; interpretator yaratilganidan {from_proc}; "
              "ko'rinadigan qatorlar to'liq: {full})",
        "en": "{first} ({from_main} from the `main()` call; {from_proc} from the interpreter's creation; "
              "visible rows complete: {full})",
    },
    "bench.gui.row.index": {
        "uz": "To'liq indeks GUI ichida, UI javob berib turadi ({file})",
        "en": "Full index inside the GUI while the UI stays responsive ({file})",
    },
    "bench.gui.row.index.target": {
        "uz": "≥ 150 MB/s; 5 ms taymer oralig'i max < 250 ms",
        "en": "≥ 150 MB/s; 5 ms timer gap max < 250 ms",
    },
    "bench.gui.row.index.result": {
        "uz": "{rate} ({size}, {seconds}, {records} ta yozuv); taymer oraliqlari p95 {p95}, max {max}",
        "en": "{rate} ({size}, {seconds}, {records} records); timer gaps p95 {p95}, max {max}",
    },
    "bench.gui.row.scroll": {
        "uz": "Skrollbarni oxiriga sakratish: ko'rinadigan qatorlar to'liq chizilguncha ({file}, {records} ta yozuv)",
        "en": "Scrollbar jump to the end: until the visible rows are completely painted ({file}, {records} records)",
    },
    "bench.gui.row.scroll.result": {"uz": "{ms} (o'rtaga sakrash {mid})", "en": "{ms} (jump to the middle {mid})"},
    "bench.gui.row.node": {
        "uz": "1 000 000 bolali tugunni ochish ({file}): bolalari indekslangandan keyin chizilguncha",
        "en": "Expanding the node with 1 000 000 children ({file}): until painted once its children are indexed",
    },
    "bench.gui.row.node.result": {
        "uz": "qayta ochish {cached}; birinchi marta: bolalar indeksi {index} + chizish {after} = {total}; "
              "oxirgi bolaga sakrash {last}; {children} ta bola",
        "en": "re-expand {cached}; first time: children index {index} + painting {after} = {total}; jump to the "
              "last child {last}; {children} children",
    },
    "bench.gui.row.search": {
        "uz": "To'liq qidiruv «{query}» ({file}), qidiruv panelidan: birinchi natija chizilguncha va tezlik",
        "en": "Full search \"{query}\" ({file}) from the search panel: until the first hit is painted, and speed",
    },
    "bench.gui.row.search.result": {
        "uz": "{rate} ({seconds}; {hits} ta moslik); birinchi natija chizildi: {first}; taymer p95 {p95}, max {max}",
        "en": "{rate} ({seconds}; {hits} hits); first hit painted: {first}; timer p95 {p95}, max {max}",
    },
    "bench.gui.row.filter": {
        "uz": "Filtr `{expr}` filtr panelidan ({file}, {records} ta yozuv): qisman natijalar",
        "en": "Filter `{expr}` from the filter panel ({file}, {records} records): partial results",
    },
    "bench.gui.row.filter.result": {
        "uz": "birinchi qisman qatorlar chizildi: {first}; hammasi {total} ({rate}; {matched} ta mos yozuv); "
              "taymer p95 {p95}, max {max}",
        "en": "first partial rows painted: {first}; all {total} ({rate}; {matched} matching records); "
              "timer p95 {p95}, max {max}",
    },
    "bench.gui.row.sort": {
        "uz": "{records} ta yozuvni `{key}` bo'yicha tartiblash, sessiya orqali ({file})",
        "en": "Sorting {records} records by `{key}` through the session ({file})",
    },
    "bench.gui.row.sort.result": {
        "uz": "{seconds}, peak RSS {peak} (tartiblash bosqichida {phase}); taymer p95 {p95}, max {max}; "
              "tartiblangan qatorlar chizildi {painted} keyin",
        "en": "{seconds}, peak RSS {peak} ({phase} during the sort); timer p95 {p95}, max {max}; sorted rows "
              "painted {painted} later",
    },
    "bench.gui.row.export": {
        "uz": "Tartiblangan ko'rinishni JSONL ga eksport, eksport oynasi orqali ({file})",
        "en": "Export of the sorted view to JSONL through the export dialog ({file})",
    },
    "bench.gui.row.export.target": {"uz": "≥ 50 MB/s, RAM < 300 MB", "en": "≥ 50 MB/s, RAM < 300 MB"},
    "bench.gui.row.export.result": {
        "uz": "{rate} ({seconds}; {records} ta yozuv, chiqish {size}); eksport bosqichida RSS {phase} "
              "(bosqich boshida {start}); taymer max {max}",
        "en": "{rate} ({seconds}; {records} records, output {size}); RSS during the export {phase} ({start} at its "
              "start); timer max {max}",
    },
    "bench.gui.row.session": {
        "uz": "Butun GUI sessiyasining xotirasi, tartiblashsiz ({file}: ochish, indeks, skroll, jadval, qidiruv, "
              "filtr, tuzilma, matn, eksport)",
        "en": "Memory of a whole GUI session without a sort ({file}: open, index, scroll, table, search, filter, "
              "schema, text, export)",
    },
    "bench.gui.row.session.result": {
        "uz": "peak RSS {peak} (tartiblashsiz barcha o'lchovlarning eng kattasi: {max_all} — {op})",
        "en": "peak RSS {peak} (largest of every measurement without a sort: {max_all} -- {op})",
    },
    "bench.gui.row.close": {
        "uz": "Tab yopilgandan keyin RSS ({file})",
        "en": "RSS after closing the tab ({file})",
    },
    "bench.gui.row.close.result": {
        "uz": "{after} (fayl ochilishidan oldin {base}; sessiya davomida eng ko'pi {peak})",
        "en": "{after} ({base} before the file was opened; at most {peak} during the session)",
    },
    "bench.gui.no_target": {"uz": "— (§12 da yo'q)", "en": "— (not in §12)"},
    "bench.gui.target.open": {"uz": "< 1 s", "en": "< 1 s"},
    "bench.gui.target.node": {"uz": "< 200 ms (virtual)", "en": "< 200 ms (virtual)"},
    "bench.gui.target.search": {"uz": "≥ 100 MB/s, birinchi natija < 1 s", "en": "≥ 100 MB/s, first hit < 1 s"},
    "bench.gui.target.sort": {"uz": "< 2 daqiqa, RAM < 1 GB", "en": "< 2 minutes, RAM < 1 GB"},
    "bench.gui.target.memory": {"uz": "< 500 MB", "en": "< 500 MB"},
    "bench.gui.failed": {"uz": "XATO: {error}", "en": "FAILED: {error}"},
    "bench.gui.miss.line": {"uz": "**{action}** — {result} (maqsad: {target}).",
                            "en": "**{action}** -- {result} (target: {target})."},
    "bench.gui.miss.detail": {
        "uz": "Bosqichlar va har bir takror JSON faylda (`{json}`, `ops.{op}`); sababini tahlil qilish kerak.",
        "en": "Phases and every run are in the JSON file (`{json}`, `ops.{op}`); the cause needs an analysis.",
    },
    "bench.gui.miss.memory": {
        "uz": "Xotira bosqich oxirida: RSS {rss}, xususiy (private) {private}; ishchi to'plamdagi eng katta fayl "
              "xaritalari: {maps}.",
        "en": "Memory at the end of the phase: RSS {rss}, private {private}; largest file mappings in the working "
              "set: {maps}.",
    },
    "bench.gui.row.node_select": {
        "uz": "1 000 000 elementli tugunni tanlash ({file}): matn paneli uni formatlab ko'rsatguncha",
        "en": "Selecting the node with 1 000 000 elements ({file}): until the text pane has formatted it",
    },
    "bench.gui.row.node_select.result": {
        "uz": "{seconds}; RSS {before} → {after}; taymer p95 {p95}, max {max}",
        "en": "{seconds}; RSS {before} → {after}; timer p95 {p95}, max {max}",
    },
    "bench.gui.phase.select": {"uz": "tugunni tanlash (matn paneli)", "en": "selecting the node (text pane)"},
    "bench.gui.phase.open": {"uz": "ochish (birinchi chizishgacha)", "en": "open (until the first paint)"},
    "bench.gui.phase.index": {"uz": "indeks", "en": "index"},
    "bench.gui.phase.scroll": {"uz": "skroll", "en": "scroll"},
    "bench.gui.phase.expand1": {"uz": "tugunni birinchi ochish", "en": "first expand"},
    "bench.gui.phase.expand2": {"uz": "tugunni qayta ochish", "en": "re-expand"},
    "bench.gui.phase.search": {"uz": "qidiruv «{query}»", "en": "search \"{query}\""},
    "bench.gui.phase.filter": {"uz": "filtr", "en": "filter"},
    "bench.gui.phase.sort": {"uz": "tartiblash", "en": "sort"},
    "bench.gui.phase.export": {"uz": "eksport", "en": "export"},
}


def _register_messages() -> None:
    for key, entry in GUI_MESSAGES.items():
        i18n.MESSAGES.setdefault(key, entry)


_register_messages()

RESULTS_VERSION = 1
DATA_DIR = BENCH / "data"
RESULTS_DIR = BENCH / "results"
SCRIPT = Path(__file__).resolve()
GENERATOR = BENCH / "make_synthetic.py"
PYTHON = sys.executable
CHILD_TIMEOUT = 3600.0
FONT_DIR = "C:/Windows/Fonts"

# targets (decimal units)
T_OPEN_S = 1.0
T_INDEX_MBPS = 150.0
T_GAP_MAX_S = 0.25          # our own responsiveness limit: no freeze longer than this
T_NODE_S = 0.2
T_SEARCH_MBPS = 100.0
T_FIRST_HIT_S = 1.0
T_SORT_S = 120.0
T_SORT_RSS = 1_000_000_000
T_EXPORT_MBPS = 50.0
T_EXPORT_RSS = 300_000_000
T_TOTAL_RSS = 500_000_000

RARE_TEXT = "Qoraqalpog'iston"     # make_synthetic: every 100 000th record
MEDIUM_TEXT = "Toshkent"            # ~6 % of the records
FILTER_EXPR = 'contact.address.city == "Toshkent"'
SORT_KEY = "name"
HEARTBEAT_MS = 5
RSS_SAMPLE_S = 0.05


# ==================================================================================
# child side: one scenario in this process
# ==================================================================================
class Probe:
    """Heartbeat (5 ms precise timer in the GUI thread), RSS sampler (50 ms, per phase),
    paint records of every VirtualGrid, event-loop helpers."""

    def __init__(self) -> None:
        import psutil

        self.t0 = time.perf_counter()
        self.wall0 = time.time()
        self.proc = psutil.Process()
        self.phase = "start"
        self.phases: dict[str, tuple[float, float | None]] = {}
        self.ticks: list[tuple[float, str]] = []
        self.paints: list[tuple[float, int, int, int, Any]] = []   # (t_end, grid id, real, none, tag)
        self.paint_tag: Callable[[], Any] | None = None
        self.rss_peak: dict[str, int] = {}
        self.rss_start: dict[str, int] = {}
        self._cnt = [0, 0]
        self._timer: Any = None
        threading.Thread(target=self._rss_loop, name="gui-bench-rss", daemon=True).start()

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def rss(self) -> int:
        try:
            return int(self.proc.memory_info().rss)
        except Exception:
            return 0

    def peak_wset(self) -> int | None:
        try:
            mi = self.proc.memory_info()
            return int(getattr(mi, "peak_wset", 0) or 0) or None
        except Exception:
            return None

    def memory(self) -> dict[str, Any]:
        """RSS, private bytes and the largest file mappings in the working set now."""
        out: dict[str, Any] = {"rss": self.rss()}
        try:
            out["private"] = int(getattr(self.proc.memory_info(), "private", 0) or 0) or None
            maps = sorted(self.proc.memory_maps(grouped=True), key=lambda m: -m.rss)[:5]
            out["maps"] = [[os.path.basename(m.path) or m.path, int(m.rss)] for m in maps]
        except Exception:
            pass
        return out

    def mark(self, phase: str) -> float:
        now = self.now()
        old = self.phase
        if old in self.phases and self.phases[old][1] is None:
            self.phases[old] = (self.phases[old][0], now)
        self.phase = phase
        self.phases[phase] = (now, None)
        self.rss_start[phase] = self.rss()
        return now

    def _rss_loop(self) -> None:
        while True:
            rss = self.rss()
            ph = self.phase
            if rss > self.rss_peak.get(ph, 0):
                self.rss_peak[ph] = rss
            time.sleep(RSS_SAMPLE_S)

    # -- Qt instrumentation ---------------------------------------------------------------
    def patch_paint(self) -> None:
        """Wrap VirtualGrid._paint / _fetch: per paint, the cells painted and the
        placeholders (rows not loaded yet)."""
        from jsonreshape.gui import vgrid

        cls = vgrid.VirtualGrid
        if getattr(cls, "_gui_bench_patched", False):
            return
        orig_paint, orig_fetch = cls._paint, cls._fetch
        probe = self

        def _fetch(grid: Any, row: int, column: Any) -> Any:
            cell = orig_fetch(grid, row, column)
            probe._cnt[0 if cell is not None else 1] += 1
            return cell

        def _paint(grid: Any, p: Any, clip: Any, w: int, h: int) -> Any:
            probe._cnt[0] = probe._cnt[1] = 0
            try:
                return orig_paint(grid, p, clip, w, h)
            finally:
                tag = None
                if probe.paint_tag is not None:
                    try:
                        tag = probe.paint_tag()
                    except Exception:
                        tag = None
                probe.paints.append((probe.now(), id(grid), probe._cnt[0], probe._cnt[1], tag))

        cls._fetch = _fetch
        cls._paint = _paint
        cls._gui_bench_patched = True

    def install(self, app: Any) -> None:
        from PySide6.QtCore import Qt, QTimer

        timer = QTimer(app)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.setInterval(HEARTBEAT_MS)
        timer.timeout.connect(lambda: self.ticks.append((self.now(), self.phase)))
        timer.start()
        self._timer = timer

    def run_until(self, pred: Callable[[], Any], timeout: float, poll_ms: int = HEARTBEAT_MS) -> bool:
        from PySide6.QtCore import QEventLoop, Qt, QTimer

        if pred():
            return True
        loop = QEventLoop()
        deadline = time.perf_counter() + timeout
        result = [False]

        def check() -> None:
            try:
                ok = bool(pred())
            except Exception:
                traceback.print_exc()
                ok = True
            if ok:
                result[0] = True
                loop.quit()
            elif time.perf_counter() >= deadline:
                loop.quit()

        timer = QTimer()
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.setInterval(poll_ms)
        timer.timeout.connect(check)
        timer.start()
        loop.exec()
        timer.stop()
        return result[0]

    def run_for(self, seconds: float) -> None:
        self.run_until(lambda: False, seconds, poll_ms=20)

    # -- analysis ------------------------------------------------------------------------
    def gaps(self, t_from: float | None = None, t_to: float | None = None) -> dict[str, Any]:
        """Heartbeat gaps of ``[t_from, t_to]`` -- with the last tick before and the first
        tick after the window, so a freeze that began before it or outlasted it counts."""
        ts = [x[0] for x in list(self.ticks)]
        i0 = 0 if t_from is None else max(0, bisect.bisect_left(ts, t_from) - 1)
        i1 = len(ts) if t_to is None else min(len(ts), bisect.bisect_right(ts, t_to) + 1)
        sel = ts[i0:i1]
        diffs = sorted(b - a for a, b in zip(sel, sel[1:]))
        if not diffs:
            return {"n": 0}
        n = len(diffs)

        def q(f: float) -> float:
            return round(diffs[min(n - 1, int(n * f))] * 1000, 1)

        return {"n": n, "p50_ms": q(0.5), "p95_ms": q(0.95), "p99_ms": q(0.99), "max_ms": round(diffs[-1] * 1000, 1),
                "over100": sum(1 for d in diffs if d > 0.1), "over250": sum(1 for d in diffs if d > 0.25),
                "seconds": round(sel[-1] - sel[0], 3)}

    def phase_gaps(self, phase: str) -> dict[str, Any]:
        start, end = self.phases.get(phase, (None, None))
        if start is None:
            return {"n": 0}
        return self.gaps(start, end)

    def first_paint(self, grid: Any, t_from: float, *, full: bool = False,
                    tag: Callable[[Any], bool] | None = None) -> float | None:
        gid = id(grid)
        for t_end, g, real, none, tg in self.paints:
            if g != gid or t_end < t_from or real <= 0:
                continue
            if full and none:
                continue
            if tag is not None and not tag(tg):
                continue
            return t_end
        return None

    def settle(self, grid: Any, t_from: float, *, quiet_ms: int = 300, timeout: float = 60.0) -> float | None:
        """Wait until ``grid`` stopped painting for ``quiet_ms``; the time of the first paint
        after ``t_from`` from which on every paint was complete (``None``: never)."""
        gid = id(grid)

        def quiet() -> bool:
            last = max((p[0] for p in self.paints if p[1] == gid), default=0.0)
            return self.now() - max(last, t_from) > quiet_ms / 1000.0 and \
                any(p[1] == gid and p[0] >= t_from for p in self.paints)

        self.run_until(quiet, timeout, poll_ms=10)
        mine = [p for p in self.paints if p[1] == gid and p[0] >= t_from]
        if not mine or mine[-1][3] or mine[-1][2] <= 0:
            return None
        k = len(mine) - 1
        while k > 0 and mine[k - 1][3] == 0 and mine[k - 1][2] > 0:
            k -= 1
        return mine[k][0]


class _Ctx:
    """What every scenario shares: the probe, the started window, collected errors."""

    def __init__(self, probe: Probe, params: dict[str, Any]) -> None:
        self.probe = probe
        self.params = params
        self.win: Any = None
        self.app: Any = None
        self.errors: list[str] = []
        self.out: dict[str, Any] = {}

    def start(self, argv: list[str]) -> Any:
        from PySide6.QtWidgets import QApplication

        from jsonreshape.gui.app import main

        p = self.probe
        p.patch_paint()
        self.out["main_called"] = p.now()
        win = main(argv, run=False)
        self.out["main_returned"] = p.now()
        p.install(QApplication.instance())
        win.app.headless = True
        win.app.error_reported.connect(lambda exc: self.errors.append(f"{type(exc).__name__}: {exc}"))
        win.resize(1440, 900)
        self.win = win
        self.app = win.app
        return win

    def ready(self, session: Any, timeout: float = 1800.0) -> None:
        if not self.probe.run_until(lambda: session.state in ("ready", "failed"), timeout):
            raise TimeoutError(f"{session.name} not ready after {timeout} s")
        if session.state != "ready":
            raise RuntimeError(f"opening {session.name} failed: {session.open_error!r}")

    def quiet_tasks(self, timeout: float = 120.0) -> None:
        """Wait until no background task runs (schema samples, helpers)."""
        self.probe.run_until(lambda: not self.app.tasks.tasks(include_silent=True), timeout, poll_ms=20)

    def key(self, widget: Any, key: Any) -> None:
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QApplication

        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(widget, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier))

    def finish(self) -> None:
        win = self.win
        if win is not None and not win._closing:
            win.close()
        if self.app is not None:
            self.app.tasks.wait_idle(60)


def _gap_fields(prefix: str, gaps: dict[str, Any], out: dict[str, Any]) -> None:
    out[f"{prefix}_gaps"] = gaps
    out[f"{prefix}_gap_p95_s"] = gaps.get("p95_ms", 0.0) / 1000.0 if gaps.get("n") else None
    out[f"{prefix}_gap_max_s"] = gaps.get("max_ms", 0.0) / 1000.0 if gaps.get("n") else None


def scenario_open_index(ctx: _Ctx) -> None:
    p = ctx.probe
    path = ctx.params["path"]
    size = os.path.getsize(path)
    out = ctx.out
    p.mark("open")
    win = ctx.start([path])
    tab = win.current_tab()
    s = tab.session
    tree = tab.active_view()
    grid = tree.grid
    ev: dict[str, float] = {}
    s.opened.connect(lambda: ev.setdefault("opened", p.now()))
    s.index_finished.connect(lambda: ev.setdefault("indexed", p.now()))
    if not p.run_until(lambda: p.first_paint(grid, 0.0) is not None, 120):
        raise TimeoutError("no tree rows were painted")
    t_first = p.first_paint(grid, 0.0)
    p.run_until(lambda: p.first_paint(grid, 0.0, full=True) is not None, 60)
    t_full = p.first_paint(grid, 0.0, full=True)
    launch = ctx.params.get("launch_wall")
    wall_first = p.wall0 + t_first
    out.update({
        "data_size": size,
        "first_paint_seconds": round(wall_first - launch, 4) if launch else None,
        "first_paint_proc_seconds": round(wall_first - p.proc.create_time(), 4),
        "first_paint_main_seconds": round(t_first - out["main_called"], 4),
        "first_paint_script_seconds": round(t_first, 4),
        "first_full_paint_main_seconds": round(t_full - out["main_called"], 4) if t_full is not None else None,
        "main_seconds": round(out["main_returned"] - out["main_called"], 4),
        "opened_main_seconds": round(ev["opened"] - out["main_called"], 4) if "opened" in ev else None,
        "first_rows": [tree.cell_text(r, "key") for r in range(min(3, tree.row_count()))],
    })
    _gap_fields("open", p.gaps(None, t_first), out)
    p.mark("index")
    ctx.ready(s)
    t_ready = p.now()
    opened = ev.get("opened", out["main_returned"])
    indexed = ev.get("indexed", t_ready)
    index_s = max(1e-9, indexed - opened)
    out.update({"record_count": int(s.doc.record_count), "index_seconds": round(index_s, 4),
                "index_mb_per_s": round(size / 1e6 / index_s, 1), "kind": s.doc.kind,
                "rss_after_index": p.rss()})
    _gap_fields("index", p.gaps(opened, indexed), out)
    # the scrollbar jumps to the end, then to the middle
    p.mark("idle")
    p.run_for(0.5)
    bar = grid.verticalScrollBar()
    p.mark("scroll")
    t_a = p.now()
    bar.setValue(bar.maximum())
    t_done = p.settle(grid, t_a)
    out["scroll_end_seconds"] = round(t_done - t_a, 4) if t_done is not None else None
    out["scroll_end_top_row"] = grid.top_row()
    t_a = p.now()
    bar.setValue(bar.maximum() // 2)
    t_done = p.settle(grid, t_a)
    out["scroll_mid_seconds"] = round(t_done - t_a, 4) if t_done is not None else None
    _gap_fields("scroll", p.phase_gaps("scroll"), out)
    p.mark("close")
    win.close_tab(0)
    p.run_for(1.0)
    out["rss_after_close"] = p.rss()


def scenario_node(ctx: _Ctx) -> None:
    from PySide6.QtCore import Qt

    p = ctx.probe
    path = ctx.params["path"]
    out = ctx.out
    out["data_size"] = os.path.getsize(path)
    p.mark("open")
    win = ctx.start([])
    tab = win.open_path(path, {"format": "single"})
    s = tab.session
    tree = tab.active_view()
    grid = tree.grid
    ctx.ready(s)
    row = None

    def find_items() -> bool:
        nonlocal row
        for r in range(min(tree.row_count(), 20)):
            if tree.cell_text(r, "key") == "items" and tree.info_at(r) is not None:
                row = r
                return True
        return False

    if not p.run_until(find_items, 120):
        raise TimeoutError("the 'items' row did not appear")
    ctx.quiet_tasks()
    kp = tree.row_keypath(row)
    rows_before = tree.row_count()
    grid.scroll_to(row, "top")
    # selecting the node: the text pane formats it (up to its 16 MiB cap) -- measured
    # separately, the expansion below starts once that is done
    p.mark("select")
    out["rss_before_select"] = p.rss()
    t_a = p.now()
    tree.select_row(row, scroll="none")
    text = tab.panes[1].current_view() if tab.panes[1].current_kind() == "text" else None
    p.run_for(0.2)
    if text is not None:
        p.run_until(lambda: not text.is_loading(), 120)
    ctx.quiet_tasks()
    out["text_pane_seconds"] = round(p.now() - t_a, 4) if text is not None else None
    out["rss_after_select"] = p.rss()
    _gap_fields("select", p.gaps(t_a, p.now()), out)
    p.run_for(0.3)
    changes: list[float] = []
    tree.structure_changed.connect(lambda: changes.append(p.now()))
    # first time: the children are indexed in a task
    p.mark("expand1")
    t_a = p.now()
    ctx.key(grid, Qt.Key.Key_Right)
    if not p.run_until(lambda: tree.flat.is_expanded(kp), 300):
        raise TimeoutError("the node was not expanded")
    t_exp = next((c for c in changes if c >= t_a), p.now())
    t_paint = p.settle(grid, t_exp)
    children = tree.row_count() - rows_before
    out["memory_after_expand"] = p.memory()
    out.update({"child_count": children,
                "first_index_seconds": round(t_exp - t_a, 4),
                "first_after_index_seconds": round(t_paint - t_exp, 4) if t_paint is not None else None,
                "first_total_seconds": round(t_paint - t_a, 4) if t_paint is not None else None})
    _gap_fields("expand1", p.gaps(t_a, t_paint), out)
    # collapse, then expand again (children known)
    p.mark("collapse")
    ctx.key(grid, Qt.Key.Key_Left)
    p.run_until(lambda: not tree.flat.is_expanded(kp), 30)
    p.run_for(0.4)
    p.mark("expand2")
    t_a = p.now()
    ctx.key(grid, Qt.Key.Key_Right)
    p.run_until(lambda: tree.flat.is_expanded(kp), 30)
    t_paint = p.settle(grid, t_a)
    out["cached_seconds"] = round(t_paint - t_a, 4) if t_paint is not None else None
    _gap_fields("expand2", p.gaps(t_a, t_paint), out)
    # the last child
    p.mark("last")
    bar = grid.verticalScrollBar()
    t_a = p.now()
    bar.setValue(bar.maximum())
    t_paint = p.settle(grid, t_a)
    out["last_child_seconds"] = round(t_paint - t_a, 4) if t_paint is not None else None
    last_row = tree.row_count() - 2       # the "tail" member follows the children
    out["last_child_key"] = tree.cell_text(last_row, "key")


def scenario_search(ctx: _Ctx) -> None:
    from PySide6.QtCore import Qt

    p = ctx.probe
    path = ctx.params["path"]
    size = os.path.getsize(path)
    out = ctx.out
    out["data_size"] = size
    p.mark("open")
    win = ctx.start([path])
    s = win.current_tab().session
    ctx.ready(s)
    ctx.quiet_tasks()
    out["record_count"] = int(s.doc.record_count)
    panel = win.show_search("")
    p.run_for(0.5)
    edit = panel.query_combo.lineEdit()
    results = []
    for query in ctx.params["queries"]:
        panel.set_query_text(query)
        p.mark(f"search:{query}")
        t_a = p.now()
        ctx.key(edit, Qt.Key.Key_Return)        # Enter in the query field runs the search
        if not p.run_until(lambda: panel.state(s) is not None and panel.state(s).status in ("done", "failed",
                                                                                              "cancelled"), 1800):
            raise TimeoutError(f"search {query!r} did not end")
        t_done = p.now()
        st = panel.state(s)
        if st.status != "done":
            raise RuntimeError(f"search {query!r}: {st.status} {st.error!r}")
        res = st.result
        first = p.first_paint(panel.grid, t_a)
        p.run_for(0.2)
        results.append({
            "query": query, "seconds": round(t_done - t_a, 4), "mb_per_s": round(size / 1e6 / (t_done - t_a), 1),
            "core_seconds": round(res.elapsed, 4), "core_mb_per_s": round(res.mb_per_s, 1),
            "hit_count": int(res.hit_count), "record_count": int(res.record_count), "mode_used": res.mode_used,
            "first_hit_painted_seconds": round(first - t_a, 4) if first is not None else None,
            "gaps": p.gaps(t_a, t_done),
        })
    out["searches"] = results


def scenario_filter(ctx: _Ctx) -> None:
    p = ctx.probe
    path = ctx.params["path"]
    size = os.path.getsize(path)
    out = ctx.out
    out["data_size"] = size
    p.mark("open")
    win = ctx.start([path])
    tab = win.current_tab()
    s = tab.session
    tree = tab.active_view()
    ctx.ready(s)
    ctx.quiet_tasks()
    out["record_count"] = int(s.doc.record_count)
    panel = win.show_filter()
    ed = panel.current_editor()
    ed.set_text(ctx.params["expr"])
    p.run_for(0.5)
    p.paint_tag = lambda: (not s.view_is_base) and s.filter is not None
    p.mark("filter")
    t_a = p.now()
    panel.apply_button.click()
    if not p.run_until(lambda: s.filter is not None and not s.view_busy, 3600):
        raise TimeoutError("the filter did not end")
    t_done = p.now()
    first = p.first_paint(tree.grid, t_a, tag=bool)
    p.paint_tag = None
    out.update({"seconds": round(t_done - t_a, 4), "mb_per_s": round(size / 1e6 / (t_done - t_a), 1),
                "matched": int(s.view_len()),
                "first_rows_painted_seconds": round(first - t_a, 4) if first is not None else None,
                "first_rows_before_end": first is not None and first < t_done})
    _gap_fields("filter", p.gaps(t_a, t_done), out)


def scenario_sort_export(ctx: _Ctx) -> None:
    from jsonreshape.gui.exportdialog import ExportDialog
    from jsonreshape.sortkeys import SortKey

    p = ctx.probe
    path = ctx.params["path"]
    size = os.path.getsize(path)
    out = ctx.out
    out["data_size"] = size
    p.mark("open")
    win = ctx.start([path])
    tab = win.current_tab()
    s = tab.session
    tree = tab.active_view()
    ctx.ready(s)
    ctx.quiet_tasks()
    out["record_count"] = int(s.doc.record_count)
    p.run_for(0.5)
    out["rss_before_sort"] = p.rss()
    p.mark("sort")
    t_a = p.now()
    s.set_sort([SortKey(ctx.params["key"])])
    if not p.run_until(lambda: not s.view_busy, 3600):
        raise TimeoutError("the sort did not end")
    t_done = p.now()
    out.update({"sort_seconds": round(t_done - t_a, 4), "sorted_records": int(s.view_len()),
                "sort_keys": [k.path for k in s.sort_keys], "peak_wset_after_sort": p.peak_wset()})
    _gap_fields("sort", p.gaps(t_a, t_done), out)
    painted = p.settle(tree.grid, t_done)
    out["sorted_rows_painted_seconds"] = round(painted - t_done, 4) if painted is not None else None
    out["sort_phase_rss"] = p.rss_peak.get("sort")
    out["memory_after_sort"] = p.memory()
    # export of the sorted view through the export dialog
    target = Path(ctx.params["out"])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    dialog = ExportDialog(s, win, scope="view")
    dialog.set_format("jsonl")
    dialog.set_output_path(str(target))
    dialog.show()
    p.run_for(0.3)
    p.mark("export")
    t_a = p.now()
    task = dialog.start_export()
    if task is None:
        raise RuntimeError(f"the export did not start: {dialog.error_text()}")
    if not p.run_until(lambda: dialog.outcome is not None or dialog.error is not None, 3600):
        raise TimeoutError("the export did not end")
    t_done = p.now()
    if dialog.error is not None:
        raise RuntimeError(f"export failed: {dialog.error!r}")
    rep = dialog.outcome.report
    out["memory_after_export"] = p.memory()
    out.update({"export_seconds": round(t_done - t_a, 4), "export_core_seconds": round(rep.elapsed, 4),
                "export_records": int(rep.records), "export_bytes": int(rep.bytes_written),
                "export_mb_per_s": round(rep.bytes_written / 1e6 / (t_done - t_a), 1),
                "export_phase_rss": p.rss_peak.get("export"), "export_start_rss": p.rss_start.get("export")})
    _gap_fields("export", p.gaps(t_a, t_done), out)
    p.mark("cleanup")
    try:
        if dialog.report_dialog is not None:
            dialog.report_dialog.close()
        dialog.close()
    finally:
        try:
            target.unlink()
        except OSError:
            pass


def scenario_session(ctx: _Ctx) -> None:
    from PySide6.QtCore import Qt

    from jsonreshape.gui.exportdialog import ExportDialog
    from jsonreshape.gui.session import FilterSpec

    p = ctx.probe
    path = ctx.params["path"]
    out = ctx.out
    out["data_size"] = os.path.getsize(path)
    p.mark("start")
    win = ctx.start([])
    p.run_for(1.0)
    out["rss_before_open"] = p.rss()
    p.mark("open")
    tab = win.open_path(path)
    s = tab.session
    tree = tab.active_view()
    grid = tree.grid
    ctx.ready(s)
    out["record_count"] = int(s.doc.record_count)
    p.mark("scroll")
    bar = grid.verticalScrollBar()
    bar.setValue(bar.maximum())
    p.settle(grid, p.now())
    bar.setValue(0)
    p.settle(grid, p.now())
    tree.select_row(0)
    ctx.key(grid, Qt.Key.Key_Right)
    p.run_until(lambda: tree.flat.is_expanded(tree.row_keypath(0)), 30)
    p.mark("table")
    table = tab.panes[1].set_view("table")
    p.run_until(lambda: len(table.visible_column_paths()) > 3 and table.cell_text(0, table.visible_column_paths()[1])
                is not None, 120)
    tbar = table.grid.verticalScrollBar()
    tbar.setValue(tbar.maximum() // 3)
    p.settle(table.grid, p.now())
    out["table_columns"] = len(table.visible_column_paths())
    p.mark("search")
    panel = win.show_search(RARE_TEXT)
    panel.set_query_text(ctx.params["query"])
    panel.run_search()
    p.run_until(lambda: panel.state(s) is not None and not panel.state(s).running, 1800)
    out["search_hits"] = int(panel.state(s).hit_count)
    p.mark("filter")
    s.set_filter(FilterSpec(ctx.params["expr"]))
    p.run_until(lambda: not s.view_busy, 3600)
    out["filtered"] = int(s.view_len())
    p.mark("schema")
    schema_view = tab.panes[1].set_view("schema")
    p.run_for(1.0)
    ctx.quiet_tasks(300)
    out["schema_view"] = type(schema_view).__name__
    p.mark("text")
    tab.panes[1].set_view("text")
    tree.select_row(0)
    p.run_for(1.0)
    p.mark("export")
    target = Path(ctx.params["out"])
    target.parent.mkdir(parents=True, exist_ok=True)
    dialog = ExportDialog(s, win, scope="view")
    dialog.set_format("jsonl")
    dialog.set_output_path(str(target))
    if dialog.start_export() is None:
        raise RuntimeError(f"the export did not start: {dialog.error_text()}")
    p.run_until(lambda: dialog.outcome is not None or dialog.error is not None, 3600)
    out["export_records"] = int(dialog.outcome.report.records) if dialog.outcome is not None else None
    if dialog.report_dialog is not None:
        dialog.report_dialog.close()
    dialog.close()
    try:
        target.unlink()
    except OSError:
        pass
    out["rss_before_close"] = p.rss()
    out["peak_wset_session"] = p.peak_wset()
    p.mark("close")
    win.close_tab(0)
    p.run_for(3.0)                  # deferred deletes, closing record sets, GUI-thread GC
    out["rss_after_close"] = p.rss()


SCENARIOS: dict[str, Callable[[_Ctx], None]] = {
    "open_index": scenario_open_index,
    "node": scenario_node,
    "search": scenario_search,
    "filter": scenario_filter,
    "sort_export": scenario_sort_export,
    "session": scenario_session,
}


def child_main(scenario: str) -> int:
    probe = Probe()
    params = json.loads(os.environ.get("GUI_BENCH_PARAMS") or "{}")
    launch = os.environ.get("GUI_BENCH_LAUNCH")
    if launch:
        params["launch_wall"] = float(launch)
    ctx = _Ctx(probe, params)
    result: dict[str, Any] = {"scenario": scenario, "ok": False}
    code = 1
    try:
        SCENARIOS[scenario](ctx)
        result["ok"] = not ctx.errors
        if ctx.errors:
            result["error"] = "; ".join(ctx.errors[:5])
        code = 0 if result["ok"] else 1
    except BaseException as exc:  # noqa: BLE001 - reported to the runner
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-4000:]
    try:
        ctx.finish()
    except Exception as exc:  # noqa: BLE001
        result.setdefault("error", f"closing failed: {exc!r}")
        result["ok"] = False
        code = 1
    result.update(ctx.out)
    result["errors"] = ctx.errors
    result["phase_rss_peak"] = dict(probe.rss_peak)
    result["phase_gaps"] = {ph: probe.phase_gaps(ph) for ph in probe.phases}
    result["peak_rss"] = probe.peak_wset()
    try:
        ct = probe.proc.cpu_times()
        result["cpu_user"], result["cpu_system"] = round(ct.user, 3), round(ct.system, 3)
    except Exception:
        pass
    sys.stdout.write(json.dumps(result, ensure_ascii=True, default=str) + "\n")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)      # no interpreter teardown with Qt objects and worker threads around


# ==================================================================================
# runner side
# ==================================================================================
@dataclass(frozen=True)
class DataFile:
    key: str
    filename: str
    gen_args: tuple[str, ...]          # make_synthetic arguments
    quick: bool                        # generated in the work folder by --quick


FULL_DATA: dict[str, DataFile] = {
    "syn_2g": DataFile("syn_2g", "syn_2g.json", ("--size", "2GB", "--format", "array"), False),
    "syn_5g": DataFile("syn_5g", "syn_5g.json", ("--size", "5GB", "--format", "array"), False),
    "syn_10m": DataFile("syn_10m", "syn_10m.jsonl", ("--records", "10000000", "--format", "jsonl"), False),
    "node_1m": DataFile("node_1m", "node_1m.json", ("--node-items", "1000000"), False),
}
QUICK_DATA: dict[str, DataFile] = {
    # at most ~200 MB each (decimal): 190 MiB = 199.2 MB
    "syn_2g": DataFile("syn_2g", "gui_quick.json", ("--size", "190MB", "--format", "array"), True),
    "syn_10m": DataFile("syn_10m", "gui_quick.jsonl", ("--records", "400000", "--format", "jsonl"), True),
    "node_1m": DataFile("node_1m", "gui_quick_node.json", ("--node-items", "1000000"), True),
}


@dataclass(frozen=True)
class GuiOp:
    name: str
    scenario: str
    data: str
    params: tuple[tuple[str, Any], ...] = ()
    is_sort: bool = False
    quick: bool = True                 # also in --quick
    out_ext: str | None = None


OPS: tuple[GuiOp, ...] = (
    GuiOp("open_2g", "open_index", "syn_2g"),
    GuiOp("open_5g", "open_index", "syn_5g", quick=False),
    GuiOp("open_10m", "open_index", "syn_10m"),
    GuiOp("node_1m", "node", "node_1m"),
    GuiOp("search_2g", "search", "syn_2g", (("queries", (RARE_TEXT, MEDIUM_TEXT)),)),
    GuiOp("filter_10m", "filter", "syn_10m", (("expr", FILTER_EXPR),)),
    GuiOp("sort_export_10m", "sort_export", "syn_10m", (("key", SORT_KEY),), is_sort=True, out_ext=".jsonl"),
    GuiOp("session_2g", "session", "syn_2g", (("query", RARE_TEXT), ("expr", FILTER_EXPR)), out_ext=".jsonl"),
)
OP_BY_NAME = {op.name: op for op in OPS}


def select_ops(quick: bool, only: list[str] | None = None) -> list[GuiOp]:
    if only:
        unknown = [n for n in only if n not in OP_BY_NAME]
        if unknown:
            raise SystemExit(t("bench.gui.error.unknown_op", names=", ".join(unknown), known=", ".join(OP_BY_NAME)))
        chosen = [op for op in OPS if op.name in only]
    else:
        chosen = list(OPS)
    if quick:
        chosen = [op for op in chosen if op.quick]
    return chosen


@dataclass
class GuiConfig:
    quick: bool = False
    only: list[str] | None = None
    repeat: int = 2
    data_dir: Path = DATA_DIR
    results_dir: Path = RESULTS_DIR
    work_dir: Path | None = None
    wait: bool = True
    quiet_load: float = 35.0
    quiet_timeout: float = 60.0
    keep_work: bool = False
    lang: str = "uz"
    timeout: float = CHILD_TIMEOUT
    write_latest: bool | None = None
    command: list[str] = field(default_factory=list)


def _log(text: str) -> None:
    stream = sys.stdout
    if stream is not None:
        stream.write(text + "\n")
        stream.flush()


def ensure_file(spec: DataFile, folder: Path) -> dict[str, Any]:
    """The data file (generated with make_synthetic.py when missing)."""
    import subprocess

    path = folder / spec.filename
    info: dict[str, Any] = {"path": str(path), "file": spec.filename, "generated": False}
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        _log(t("bench.gui.log.generate", name=spec.filename, args=" ".join(spec.gen_args)))
        cp = subprocess.run([PYTHON, str(GENERATOR), "--out", str(path), *spec.gen_args, "-q"], cwd=str(ROOT),
                            capture_output=True)
        if cp.returncode != 0 or not path.exists():
            raise SystemExit(t("bench.gui.error.generate", name=spec.filename,
                               detail=cp.stderr.decode(errors="replace").strip()[-500:]))
        try:
            gen = json.loads(cp.stdout.decode(errors="replace").strip().splitlines()[-1])
            info["records"] = gen.get("records")
        except (ValueError, IndexError):
            pass
        info["generated"] = True
    info["bytes"] = path.stat().st_size
    return info


def _best(values: list[Any], *, larger: bool = False) -> Any:
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return None
    return max(nums) if larger else min(nums)


# per scenario: (field, larger is better) of the values whose best run is kept
_BEST_FIELDS: dict[str, tuple[tuple[str, bool], ...]] = {
    "open_index": (("first_paint_seconds", False), ("first_paint_proc_seconds", False),
                   ("first_paint_main_seconds", False), ("first_full_paint_main_seconds", False),
                   ("main_seconds", False), ("index_seconds", False), ("index_mb_per_s", True),
                   ("index_gap_p95_s", False), ("index_gap_max_s", False), ("open_gap_max_s", False),
                   ("scroll_end_seconds", False), ("scroll_mid_seconds", False), ("scroll_gap_max_s", False)),
    "node": (("first_index_seconds", False), ("first_after_index_seconds", False), ("first_total_seconds", False),
             ("cached_seconds", False), ("last_child_seconds", False), ("expand1_gap_max_s", False),
             ("text_pane_seconds", False), ("select_gap_max_s", False)),
    "search": (),
    "filter": (("seconds", False), ("mb_per_s", True), ("first_rows_painted_seconds", False),
               ("filter_gap_p95_s", False), ("filter_gap_max_s", False)),
    "sort_export": (("sort_seconds", False), ("sort_gap_p95_s", False), ("sort_gap_max_s", False),
                    ("sorted_rows_painted_seconds", False), ("export_seconds", False), ("export_mb_per_s", True),
                    ("export_gap_max_s", False)),
    "session": (("rss_after_close", False),),
}
# memory: the largest of all runs
_MAX_FIELDS = ("sort_phase_rss", "export_phase_rss", "export_start_rss", "peak_wset_session", "rss_before_open",
               "rss_after_index", "rss_after_select")


def summarize(op: GuiOp, runs: list[dict[str, Any]]) -> dict[str, Any]:
    good = [r["child"] for r in runs if r.get("ok") and r.get("child")]
    best: dict[str, Any] | None = None
    if good:
        best = dict(good[0])
        for name, larger in _BEST_FIELDS.get(op.scenario, ()):
            best[name] = _best([g.get(name) for g in good], larger=larger)
        for name in _MAX_FIELDS:
            v = _best([g.get(name) for g in good], larger=True)
            if v is not None:
                best[name] = v
        if op.scenario == "search":
            merged = []
            for k, first in enumerate(good[0].get("searches") or []):
                same = [g["searches"][k] for g in good if len(g.get("searches") or []) > k]
                item = dict(first)
                item["seconds"] = _best([x.get("seconds") for x in same])
                item["mb_per_s"] = _best([x.get("mb_per_s") for x in same], larger=True)
                item["first_hit_painted_seconds"] = _best([x.get("first_hit_painted_seconds") for x in same])
                item["gap_p95_s"] = _best([(x.get("gaps") or {}).get("p95_ms", 0) / 1000 for x in same])
                item["gap_max_s"] = _best([(x.get("gaps") or {}).get("max_ms", 0) / 1000 for x in same])
                merged.append(item)
            best["searches"] = merged
    peaks = [r.get("peak_rss") for r in runs if r.get("peak_rss")]
    return {
        "spec": asdict(op),
        "runs": runs,
        "ok": bool(runs) and all(r.get("ok") for r in runs),
        "error": next((r.get("error") for r in runs if not r.get("ok")), None),
        "best": best,
        "peak_rss": max(peaks) if peaks else None,
    }


def _records_of(op_results: dict[str, Any], data_key: str) -> int | None:
    """Records of a data file seen by a measurement (the 1 M node file: its children)."""
    for name, o in op_results.items():
        b = o.get("best") or {}
        if OP_BY_NAME[name].data != data_key:
            continue
        v = b.get("child_count" if OP_BY_NAME[name].scenario == "node" else "record_count")
        if isinstance(v, int) and v > 0:
            return v
    return None


def _run_summary(op: GuiOp, child: dict[str, Any]) -> str:
    rb = _rb()
    f = rb.fmt_s
    sc = op.scenario
    if sc == "open_index":
        return (f"first paint {f(child.get('first_paint_seconds'))}, index {child.get('index_mb_per_s')} MB/s, "
                f"gap max {f(child.get('index_gap_max_s'))}, scroll end {f(child.get('scroll_end_seconds'))}")
    if sc == "node":
        return (f"first {f(child.get('first_total_seconds'))} (index {f(child.get('first_index_seconds'))}), "
                f"cached {f(child.get('cached_seconds'))}, gap max {f(child.get('expand1_gap_max_s'))}")
    if sc == "search":
        return ", ".join(f"{x['query']}: {x['mb_per_s']} MB/s, first {f(x.get('first_hit_painted_seconds'))}"
                         for x in child.get("searches") or [])
    if sc == "filter":
        return (f"first rows {f(child.get('first_rows_painted_seconds'))}, all {f(child.get('seconds'))}, "
                f"{child.get('mb_per_s')} MB/s")
    if sc == "sort_export":
        return (f"sort {f(child.get('sort_seconds'))}, export {child.get('export_mb_per_s')} MB/s, "
                f"sort RSS {rb.fmt_mb(child.get('sort_phase_rss'))}")
    if sc == "session":
        return f"after close {rb.fmt_mb(child.get('rss_after_close'))}"
    return ""


def _rb() -> Any:
    import run_bench

    return run_bench


def run(cfg: GuiConfig) -> dict[str, Any]:
    old = get_language()
    set_language(cfg.lang)
    try:
        return _run(cfg)
    finally:
        set_language(old)


def _run(cfg: GuiConfig) -> dict[str, Any]:
    rb = _rb()
    started = datetime.now()
    run_id = started.strftime("%Y%m%d-%H%M%S")
    data_dir = Path(cfg.data_dir).resolve()
    results_dir = Path(cfg.results_dir).resolve()
    work = Path(cfg.work_dir).resolve() if cfg.work_dir else data_dir / "work" / f"gui-{run_id}"
    work.mkdir(parents=True, exist_ok=True)
    ops = select_ops(cfg.quick, cfg.only)
    mode = "quick" if cfg.quick else "full"
    results: dict[str, Any] = {"version": RESULTS_VERSION, "kind": "gui", "run_id": run_id,
                               "mode": mode if not cfg.only else f"{mode}/only", "only": cfg.only,
                               "started": started.isoformat(timespec="seconds"), "command": cfg.command,
                               "lang": cfg.lang}
    results["machine"] = rb.machine_info(data_dir)
    results["conditions"] = {"quiet_load_limit": cfg.quiet_load, "quiet_timeout": cfg.quiet_timeout,
                             "wait_for_quiet": cfg.wait, "repeat": cfg.repeat, "qt_platform": "offscreen",
                             "font_dir": FONT_DIR, "heartbeat_ms": HEARTBEAT_MS, "rss_sample_s": RSS_SAMPLE_S,
                             "window": [1440, 900], "data_dir": str(data_dir), "work_dir": str(work),
                             "fresh_home_per_run": True}
    specs = QUICK_DATA if cfg.quick else FULL_DATA
    data_folder = work / "data" if cfg.quick else data_dir
    needed = sorted({op.data for op in ops})
    results["data"] = {key: ensure_file(specs[key], data_folder) for key in needed}
    loads: list[dict[str, Any]] = []
    op_results: dict[str, Any] = {}
    try:
        for n, op in enumerate(ops, 1):
            runs: list[dict[str, Any]] = []
            data_path = Path(results["data"][op.data]["path"])
            for i in range(max(1, cfg.repeat)):
                quiet = rb.wait_quiet(cfg.quiet_load, cfg.quiet_timeout) if cfg.wait else None
                if quiet is not None:
                    loads.append(quiet)
                home = work / "homes" / f"{op.name}-{i + 1}"
                shutil.rmtree(home, ignore_errors=True)
                home.mkdir(parents=True, exist_ok=True)
                params: dict[str, Any] = {"path": str(data_path)}
                params.update({k: (list(v) if isinstance(v, tuple) else v) for k, v in op.params})
                if op.out_ext:
                    params["out"] = str(work / "out" / f"{op.name}-{i + 1}{op.out_ext}")
                env = dict(os.environ)
                env.update({"PYTHONIOENCODING": "utf-8", "JSONRESHAPE_LANG": cfg.lang, "JSONRESHAPE_HOME": str(home),
                            "QT_QPA_PLATFORM": "offscreen", "QT_QPA_FONTDIR": FONT_DIR,
                            "QT_FORCE_STDERR_LOGGING": "1",     # Qt messages (a qFatal) reach stderr
                            "GUI_BENCH_PARAMS": json.dumps(params, ensure_ascii=True)})
                env.pop("PYTHONFAULTHANDLER", None)
                cmd = [PYTHON, "-X", "faulthandler", str(SCRIPT), "--child", op.scenario]
                env["GUI_BENCH_LAUNCH"] = repr(time.time())
                res = rb.run_child(cmd, env, timeout=cfg.timeout)
                res["quiet"] = quiet
                res["run"] = i + 1
                runs.append(res)
                if res.get("ok"):
                    _log(t("bench.gui.log.run", n=n, total=len(ops), name=op.name, i=i + 1, repeats=cfg.repeat,
                           summary=_run_summary(op, res["child"]), peak=rb.fmt_mb(res.get("peak_rss")),
                           wall=rb.fmt_s(res.get("wall_seconds"))))
                else:
                    detail = res.get("error") or ""
                    child = res.get("child") or {}
                    if child.get("traceback"):
                        detail += "\n" + child["traceback"]
                    elif res.get("stderr_tail"):
                        detail += "\n" + res["stderr_tail"]
                    _log(t("bench.gui.log.failed", n=n, total=len(ops), name=op.name, i=i + 1, repeats=cfg.repeat,
                           error=detail))
                if not cfg.keep_work:
                    shutil.rmtree(home, ignore_errors=True)
                if not res.get("ok"):
                    break
            op_results[op.name] = summarize(op, runs)
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
    for key, info in results["data"].items():
        if not info.get("records"):
            info["records"] = _records_of(op_results, key)
    rows = build_rows(results)
    results["rows"] = [asdict(r) for r in rows]
    results["all_ok"] = bool(op_results) and all(o["ok"] for o in op_results.values())
    results["files"] = write_results(results, rows, results_dir, cfg)
    return results


# ----------------------------------------------------------------------------------
# rows / report
# ----------------------------------------------------------------------------------
@dataclass
class Row:
    id: str
    section: str          # main | extra
    action: str
    target: str
    result: str
    ok: bool | None
    status: str


def _status(ok: bool | None) -> str:
    rb = _rb()
    return rb.NO_TARGET if ok is None else (rb.OK_MARK if ok else rb.MISS_MARK)


def _ms(x: Any) -> str:
    return _rb().fmt_s(x) if isinstance(x, (int, float)) else "?"


def _le(x: Any, limit: float) -> bool | None:
    return None if not isinstance(x, (int, float)) else x < limit


def _all(*parts: bool | None) -> bool | None:
    vals = [p for p in parts if p is not None]
    if not vals:
        return None
    return all(vals) if len(vals) == len(parts) else False


def build_rows(results: dict[str, Any]) -> list[Row]:
    rb = _rb()
    rows: list[Row] = []
    data = results.get("data", {})
    ops = results.get("ops", {})

    def file_of(key: str) -> str:
        return (data.get(key) or {}).get("file", key)

    def failed(name: str, action: str, target: str, section: str = "main") -> None:
        o = ops[name]
        rows.append(Row(name, section, action, target, t("bench.gui.failed", error=o.get("error")), False,
                        _status(False)))

    for name in ("open_2g", "open_5g", "open_10m"):
        if name not in ops:
            continue
        o = ops[name]
        op = OP_BY_NAME[name]
        size = rb.fmt_mb((data.get(op.data) or {}).get("bytes"))
        act_open = t("bench.gui.row.open", file=file_of(op.data), size=size)
        act_index = t("bench.gui.row.index", file=file_of(op.data))
        b = o.get("best")
        section = "main" if name in ("open_2g", "open_5g") else "extra"
        if b is None:
            failed(name, act_open, t("bench.gui.target.open"), section)
            continue
        first = b.get("first_paint_seconds")
        rows.append(Row(f"{name}_first", section, act_open, t("bench.gui.target.open"),
                        t("bench.gui.row.open.result", first=_ms(first), from_main=_ms(b.get("first_paint_main_seconds")),
                          from_proc=_ms(b.get("first_paint_proc_seconds")),
                          full=_ms(b.get("first_full_paint_main_seconds"))),
                        _le(first, T_OPEN_S), _status(_le(first, T_OPEN_S))))
        rate = b.get("index_mb_per_s")
        ok_rate = None if rate is None else rate >= T_INDEX_MBPS
        ok_gap = _le(b.get("index_gap_max_s"), T_GAP_MAX_S)
        ok = _all(ok_rate, ok_gap)
        rows.append(Row(f"{name}_index", section, act_index, t("bench.gui.row.index.target"),
                        t("bench.gui.row.index.result", rate=rb.fmt_rate(rate), size=size,
                          seconds=_ms(b.get("index_seconds")), records=rb.group(b.get("record_count")),
                          p95=_ms(b.get("index_gap_p95_s")), max=_ms(b.get("index_gap_max_s"))),
                        ok, _status(ok)))
        rows.append(Row(f"{name}_scroll", "extra",
                        t("bench.gui.row.scroll", file=file_of(op.data), records=rb.group(b.get("record_count"))),
                        t("bench.gui.no_target"),
                        t("bench.gui.row.scroll.result", ms=_ms(b.get("scroll_end_seconds")),
                          mid=_ms(b.get("scroll_mid_seconds"))), None, _status(None)))
    if "node_1m" in ops:
        o = ops["node_1m"]
        act = t("bench.gui.row.node", file=file_of("node_1m"))
        b = o.get("best")
        if b is None:
            failed("node_1m", act, t("bench.gui.target.node"))
        else:
            ok = _all(_le(b.get("cached_seconds"), T_NODE_S), _le(b.get("first_after_index_seconds"), T_NODE_S))
            rows.append(Row("node_1m", "main", act, t("bench.gui.target.node"),
                            t("bench.gui.row.node.result", cached=_ms(b.get("cached_seconds")),
                              index=_ms(b.get("first_index_seconds")), after=_ms(b.get("first_after_index_seconds")),
                              total=_ms(b.get("first_total_seconds")), last=_ms(b.get("last_child_seconds")),
                              children=rb.group(b.get("child_count"))), ok, _status(ok)))
            if b.get("text_pane_seconds") is not None:
                rows.append(Row("node_1m_select", "extra", t("bench.gui.row.node_select", file=file_of("node_1m")),
                                t("bench.gui.no_target"),
                                t("bench.gui.row.node_select.result", seconds=_ms(b.get("text_pane_seconds")),
                                  before=rb.fmt_mb(b.get("rss_before_select")),
                                  after=rb.fmt_mb(b.get("rss_after_select")), p95=_ms(b.get("select_gap_p95_s")),
                                  max=_ms(b.get("select_gap_max_s"))), None, _status(None)))
    if "search_2g" in ops:
        o = ops["search_2g"]
        b = o.get("best")
        if b is None:
            failed("search_2g", t("bench.gui.row.search", query=RARE_TEXT, file=file_of("syn_2g")),
                   t("bench.gui.target.search"))
        else:
            for k, x in enumerate(b.get("searches") or []):
                rate, first = x.get("mb_per_s"), x.get("first_hit_painted_seconds")
                ok = _all(None if rate is None else rate >= T_SEARCH_MBPS, _le(first, T_FIRST_HIT_S))
                rows.append(Row(f"search_2g_{k}", "main" if k == 0 else "extra",
                                t("bench.gui.row.search", query=x.get("query"), file=file_of("syn_2g")),
                                t("bench.gui.target.search"),
                                t("bench.gui.row.search.result", rate=rb.fmt_rate(rate), seconds=_ms(x.get("seconds")),
                                  hits=rb.group(x.get("hit_count")), first=_ms(first), p95=_ms(x.get("gap_p95_s")),
                                  max=_ms(x.get("gap_max_s"))), ok, _status(ok)))
    if "filter_10m" in ops:
        o = ops["filter_10m"]
        b = o.get("best")
        act = t("bench.gui.row.filter", expr=FILTER_EXPR, file=file_of("syn_10m"),
                records=rb.group((b or {}).get("record_count")))
        if b is None:
            failed("filter_10m", act, t("bench.gui.no_target"), "extra")
        else:
            rows.append(Row("filter_10m", "extra", act, t("bench.gui.no_target"),
                            t("bench.gui.row.filter.result", first=_ms(b.get("first_rows_painted_seconds")),
                              total=_ms(b.get("seconds")), rate=rb.fmt_rate(b.get("mb_per_s")),
                              matched=rb.group(b.get("matched")), p95=_ms(b.get("filter_gap_p95_s")),
                              max=_ms(b.get("filter_gap_max_s"))), None, _status(None)))
    if "sort_export_10m" in ops:
        o = ops["sort_export_10m"]
        b = o.get("best")
        act_sort = t("bench.gui.row.sort", records=rb.group((b or {}).get("record_count")), key=SORT_KEY,
                     file=file_of("syn_10m"))
        act_exp = t("bench.gui.row.export", file=file_of("syn_10m"))
        if b is None:
            failed("sort_export_10m", act_sort, t("bench.gui.target.sort"))
        else:
            peak = o.get("peak_rss")
            ok = _all(_le(b.get("sort_seconds"), T_SORT_S), _le(peak, T_SORT_RSS))
            rows.append(Row("sort_10m", "main", act_sort, t("bench.gui.target.sort"),
                            t("bench.gui.row.sort.result", seconds=_ms(b.get("sort_seconds")), peak=rb.fmt_mb(peak),
                              phase=rb.fmt_mb(b.get("sort_phase_rss")), p95=_ms(b.get("sort_gap_p95_s")),
                              max=_ms(b.get("sort_gap_max_s")), painted=_ms(b.get("sorted_rows_painted_seconds"))),
                            ok, _status(ok)))
            rate = b.get("export_mb_per_s")
            ok = _all(None if rate is None else rate >= T_EXPORT_MBPS, _le(b.get("export_phase_rss"), T_EXPORT_RSS))
            rows.append(Row("export_sorted_10m", "main", act_exp, t("bench.gui.row.export.target"),
                            t("bench.gui.row.export.result", rate=rb.fmt_rate(rate), seconds=_ms(b.get("export_seconds")),
                              records=rb.group(b.get("export_records")), size=rb.fmt_mb(b.get("export_bytes")),
                              phase=rb.fmt_mb(b.get("export_phase_rss")), start=rb.fmt_mb(b.get("export_start_rss")),
                              max=_ms(b.get("export_gap_max_s"))), ok, _status(ok)))
    if "session_2g" in ops:
        o = ops["session_2g"]
        b = o.get("best")
        act = t("bench.gui.row.session", file=file_of("syn_2g"))
        if b is None:
            failed("session_2g", act, t("bench.gui.target.memory"))
        else:
            others = [(n, x.get("peak_rss")) for n, x in ops.items()
                      if x.get("peak_rss") and not OP_BY_NAME[n].is_sort]
            top = max(others, key=lambda z: z[1]) if others else (None, None)
            peak = o.get("peak_rss")
            ok = _all(_le(peak, T_TOTAL_RSS), _le(top[1], T_TOTAL_RSS))
            rows.append(Row("memory", "main", act, t("bench.gui.target.memory"),
                            t("bench.gui.row.session.result", peak=rb.fmt_mb(peak), max_all=rb.fmt_mb(top[1]),
                              op=top[0] or "?"), ok, _status(ok)))
            rows.append(Row("rss_after_close", "extra", t("bench.gui.row.close", file=file_of("syn_2g")),
                            t("bench.gui.no_target"),
                            t("bench.gui.row.close.result", after=rb.fmt_mb(b.get("rss_after_close")),
                              base=rb.fmt_mb(b.get("rss_before_open")), peak=rb.fmt_mb(peak)), None, _status(None)))
    return rows


def _ui_rows(results: dict[str, Any]) -> list[str]:
    """The heartbeat table: per measured phase of the best run (the one with the smallest
    max gap) of every operation."""
    lines = [t("bench.gui.md.ui_head"), "|---|---:|---|---:|---:|"]
    names = {"open": "bench.gui.phase.open", "index": "bench.gui.phase.index", "scroll": "bench.gui.phase.scroll",
             "select": "bench.gui.phase.select", "expand1": "bench.gui.phase.expand1", "expand2": "bench.gui.phase.expand2",
             "filter": "bench.gui.phase.filter", "sort": "bench.gui.phase.sort", "export": "bench.gui.phase.export"}
    for name, o in results.get("ops", {}).items():
        b = o.get("best") or {}
        items: list[tuple[str, dict[str, Any]]] = []
        for phase, key in names.items():
            g = b.get(f"{phase}_gaps")
            if isinstance(g, dict) and g.get("n"):
                items.append((t(key), g))
        for x in b.get("searches") or []:
            if (x.get("gaps") or {}).get("n"):
                items.append((t("bench.gui.phase.search", query=x.get("query")), x["gaps"]))
        for label, g in items:
            lines.append(f"| {name}: {label} | {_ms(g.get('seconds'))} | {g.get('p50_ms')} / {g.get('p95_ms')} / "
                         f"{g.get('p99_ms')} / {g.get('max_ms')} ms | {g.get('over100')} | {g.get('over250')} |")
    return lines


def render_markdown(results: dict[str, Any], rows: list[Row], json_name: str | None = None) -> str:
    rb = _rb()
    m = results.get("machine", {})
    cond = results.get("conditions", {})
    out: list[str] = [f"# {t('bench.gui.md.title')}", ""]
    out.append(t("bench.gui.md.meta", date=str(results.get("started", "")).replace("T", " "),
                 duration=rb.fmt_s(results.get("duration_seconds")), mode=results.get("mode"), json=json_name or "?"))
    out.append("")
    if str(results.get("mode", "")).startswith("quick"):
        files = ", ".join(f"{d.get('file')} {rb.fmt_mb(d.get('bytes'))}" for d in results.get("data", {}).values())
        out += [t("bench.gui.md.quick", files=files), ""]
    main = [r for r in rows if r.section == "main"]
    extra = [r for r in rows if r.section == "extra"]
    out += [f"## {t('bench.gui.md.main')}", ""] + rb._table(main) + ["", t("bench.gui.md.legend"), ""]
    if extra:
        out += [f"## {t('bench.gui.md.extra')}", ""] + rb._table(extra) + [""]
    out += [f"## {t('bench.gui.md.ui')}", ""] + _ui_rows(results) + [""]
    misses = [r for r in rows if r.ok is False]
    out += [f"## {t('bench.gui.md.misses')}", ""]
    if not misses:
        out.append(t("bench.gui.md.no_misses"))
    memory_keys = {"export_sorted_10m": ("sort_export_10m", "memory_after_export"),
                   "sort_10m": ("sort_export_10m", "memory_after_sort"), "node_1m": ("node_1m", "memory_after_expand")}
    for r in misses:
        out.append("- " + t("bench.gui.miss.line", action=r.action, result=r.result, target=r.target))
        op_name, mem_key = memory_keys.get(r.id, (r.id, None))
        mem = ((results.get("ops", {}).get(op_name) or {}).get("best") or {}).get(mem_key) if mem_key else None
        if isinstance(mem, dict):
            maps = ", ".join(f"{name} {rb.fmt_mb(size)}" for name, size in mem.get("maps") or []) or "?"
            out.append("  - " + t("bench.gui.miss.memory", rss=rb.fmt_mb(mem.get("rss")),
                                   private=rb.fmt_mb(mem.get("private")), maps=maps))
        out.append("  - " + t("bench.gui.miss.detail", json=json_name or "?", op=op_name))
    out.append("")
    out += [f"## {t('bench.gui.md.data')}", ""]
    for info in results.get("data", {}).values():
        out.append("- " + t("bench.gui.md.data_line", name=info.get("file"), size=rb.fmt_mb(info.get("bytes")),
                             records=rb.group(info.get("records")) if info.get("records") else "?",
                             how=t("bench.gui.md.generated") if info.get("generated") else t("bench.gui.md.reused")))
    out.append("")
    disk = m.get("disk") or {}
    split = t("bench.md.pe_split", p=m.get("p_cores"), e=m.get("e_cores")) if m.get("p_cores") else ""
    versions = m.get("versions") or {}
    out += [f"## {t('bench.gui.md.machine')}", ""]
    out.append("- " + t("bench.md.cpu", cpu=m.get("cpu"), cores=m.get("cores_physical"), logical=m.get("cores_logical"),
                         split=split))
    out.append("- " + t("bench.md.ram", ram=rb.fmt_mb(m.get("ram_total"))))
    out.append("- " + t("bench.md.disk", model=disk.get("model") or "?", bus=disk.get("bus") or "?",
                         media=disk.get("media") or "?", drive=disk.get("drive") or "?", fs=disk.get("fs") or "?",
                         free=rb.fmt_mb(disk.get("free"))))
    out.append("- " + t("bench.md.os", os=m.get("os"), power=m.get("power_plan") or "?"))
    out.append("- " + t("bench.md.python", python=m.get("python"), backend=m.get("core_backend"),
                         orjson=versions.get("orjson") or "?", ijson=versions.get("ijson") or "?",
                         ijson_backend=m.get("ijson_backend") or "?", version=m.get("app_version") or "?")
               + f"; PySide6 {versions.get('PySide6') or '?'}")
    out.append("")
    out += [f"## {t('bench.gui.md.conditions')}", ""]
    for key in ("bench.gui.cond.process", "bench.gui.cond.offscreen", "bench.gui.cond.start",
                "bench.gui.cond.heartbeat", "bench.gui.cond.memory"):
        out.append("- " + t(key))
    out.append("- " + t("bench.gui.cond.repeat", repeats=cond.get("repeat")))
    lb = cond.get("load_before")
    if cond.get("wait_for_quiet") and lb:
        out.append("- " + t("bench.gui.cond.load", limit=cond.get("quiet_load_limit"), timeout=cond.get("quiet_timeout"),
                             min=lb.get("min"), max=lb.get("max")))
    elif not cond.get("wait_for_quiet"):
        out.append("- " + t("bench.gui.cond.no_wait"))
    out.append("- " + t("bench.gui.cond.cache"))
    out.append("")
    out += [f"## {t('bench.gui.md.not_measured')}", ""]
    for key in ("bench.gui.nm.screen", "bench.gui.nm.laptop", "bench.gui.nm.edit"):
        out.append("- " + t(key, cpu=m.get("cpu")))
    out += ["", t("bench.gui.md.rerun"), ""]
    return "\n".join(out)


def write_results(results: dict[str, Any], rows: list[Row], results_dir: Path, cfg: GuiConfig) -> dict[str, str]:
    results_dir.mkdir(parents=True, exist_ok=True)
    run_id = results["run_id"]
    jpath = results_dir / f"gui_{run_id}.json"
    mpath = results_dir / f"gui_{run_id}.md"
    files = {"json": str(jpath), "md": str(mpath)}
    latest = cfg.write_latest if cfg.write_latest is not None else (not cfg.quick and not cfg.only)
    if latest:
        files["latest"] = str(results_dir / "gui_latest.md")
    results["files"] = files
    md = render_markdown(results, rows, jpath.name)
    jpath.write_bytes((json.dumps(results, ensure_ascii=False, indent=1, default=str) + "\n").encode("utf-8"))
    mpath.write_bytes(md.encode("utf-8"))
    if latest:
        (results_dir / "gui_latest.md").write_bytes(md.encode("utf-8"))
    return files


# ----------------------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gui_bench.py", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--quick", action="store_true", help="generated ~200 MB files in the work folder (~1-2 min)")
    ap.add_argument("--only", help="comma-separated measurement names (see --list)")
    ap.add_argument("--list", action="store_true", help="list the measurements and exit")
    ap.add_argument("--repeat", type=int, default=2, help="runs per measurement (default 2; the best is kept)")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--work-dir", type=Path, default=None)
    ap.add_argument("--quiet-load", type=float, default=35.0, help="percent of all CPUs")
    ap.add_argument("--quiet-timeout", type=float, default=60.0, help="seconds")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for a quiet machine")
    ap.add_argument("--keep-work", action="store_true", help="keep generated files, homes and outputs")
    ap.add_argument("--timeout", type=float, default=CHILD_TIMEOUT, help="seconds per run")
    ap.add_argument("--lang", choices=["uz", "en"], default="uz")
    ap.add_argument("--latest", action="store_true", help="also write gui_latest.md (quick / partial runs)")
    ap.add_argument("--child", metavar="SCENARIO", choices=sorted(SCENARIOS), help=argparse.SUPPRESS)
    return ap


def config_from_args(argv: list[str] | None = None) -> tuple[GuiConfig, argparse.Namespace]:
    args = build_parser().parse_args(argv)
    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    cfg = GuiConfig(quick=args.quick, only=only, repeat=max(1, args.repeat), data_dir=args.data_dir,
                    results_dir=args.results_dir, work_dir=args.work_dir, wait=not args.no_wait,
                    quiet_load=args.quiet_load, quiet_timeout=args.quiet_timeout, keep_work=args.keep_work,
                    lang=args.lang, timeout=args.timeout, write_latest=True if args.latest else None,
                    command=["gui_bench.py", *(argv if argv is not None else sys.argv[1:])])
    return cfg, args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg, args = config_from_args(argv)
    if args.child:
        return child_main(args.child)
    if args.list:
        for op in OPS:
            spec = (QUICK_DATA if cfg.quick else FULL_DATA).get(op.data)
            sys.stdout.write(f"{op.name:16} {op.scenario:12} {spec.filename if spec else '-':20}"
                             f"{'' if op.quick else '  [full only]'}\n")
        return 0
    results = run(cfg)
    set_language(cfg.lang)
    for r in results["rows"]:
        sys.stdout.write(f"{r['status']} {r['action']}: {r['result']}\n")
    sys.stdout.write(t("bench.gui.log.written", json=results["files"]["json"], md=results["files"]["md"]) + "\n")
    return 0 if results["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
