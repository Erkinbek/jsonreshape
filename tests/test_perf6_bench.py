"""bench/edit_bench.py additions of the stage-6 performance work P6: the table-export operations in the plan, the
report of the table exports (``bench/results/export_latest.md``) with merged baselines and its texts in both
languages, and the memory sampler that lists the child processes only every ``CHILD_REFRESH`` seconds (listing
them walks every process of the system while holding the GIL: every 20 ms it slowed the measured export)."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import i18n

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import edit_bench as eb  # noqa: E402


def test_texts_of_the_export_report_exist_in_both_languages() -> None:
    keys = {"eb.x.title", "eb.x.sec.main", "eb.x.what", "eb.x.method", "eb.x.rerun"}
    for op in eb.TABLE_REPORT_OPS:
        keys |= {f"eb.op.{op}", f"eb.x.cause.{op}", f"eb.x.opts.{op}"}
    missing = [k for k in keys if k not in eb.EDIT_BENCH_MESSAGES]
    assert not missing, missing
    for key in keys:
        entry = eb.EDIT_BENCH_MESSAGES[key]
        assert entry.get("uz") and entry.get("en"), key


def test_the_plan_has_the_table_exports() -> None:
    steps = eb.plan(argparse.Namespace())
    for scen in eb.SCENARIOS:
        assert ("export_csv_auto", scen) in steps and ("export_csv", scen) in steps
    for op in eb.TABLE_SLOW_OPS:
        assert sorted(s for o, s in steps if o == op) == sorted(eb.TABLE_SLOW_SCENARIOS)
    assert eb._selected("export_csv_auto", "all", ["export_csv_auto:all"])
    assert not eb._selected("export_csv", "all", ["export_csv_auto:all"])


def _run(op: str, scen: str, seconds: float, rss: float, nbytes: int = 2_000_000_000, workers: int = 0,
         phase: str = "op") -> dict:
    ph = {"seconds": seconds, "rss_peak_mb": rss, "private_peak_mb": rss * 0.8, "workers": workers, "fsync_s": 0.0,
          "bytes": nbytes}
    return {"op": op, "scen": scen, "phases": {"open": {"seconds": 0.01, "rss_peak_mb": 40, "private_peak_mb": 30,
                                                         "records": 6513235}, phase: ph},
            "effective_bytes": nbytes, "backend": "rust", "wall": seconds + 1, "load_before": 10.0,
            "load_mean": 12.0, "load_max": 20.0}


@pytest.mark.parametrize("lang", ["uz", "en"])
def test_export_report_rows_statuses_and_merged_baselines(tmp_path: Path, lang: str) -> None:
    old = i18n.get_language()
    try:
        before1 = {"started": 1.0, "args": {"repeat": "2"}, "runs": [_run("export_csv", "base", 50.0, 105.0)]}
        before2 = {"started": 1.5, "args": {"repeat": "2"}, "runs": [_run("export_csv_auto", "all", 150.0, 112.0),
                                                                      _run("restore", "all", 1.7, 45.0, phase="restore")]}
        after = {"started": 2.0, "args": {"repeat": "2", "data": str(tmp_path / "x.json")}, "runs": [
            _run("export_csv", "base", 10.0, 250.0, workers=4), _run("export_csv_auto", "all", 25.0, 290.0, workers=4),
            _run("export_xlsx", "base", 180.0, 120.0), _run("export_sqlite", "all", 140.0, 150.0),
            _run("restore", "all", 0.6, 46.0, phase="restore")]}
        paths = []
        for name, data in (("b1.json", before1), ("b2.json", before2), ("after.json", after)):
            p = tmp_path / name
            p.write_text(json.dumps(data), encoding="utf-8")
            paths.append(p)
        md = eb.write_export_report([paths[2]], [paths[0], paths[1]], lang, tmp_path / "x.md")
        text = md.read_text(encoding="utf-8")
        i18n.set_language(lang)
        rows = {line.split(" | ")[0].lstrip("| "): line for line in text.splitlines() if line.startswith("| ")}
        csv_base = f"{i18n.t('eb.op.export_csv')}, {i18n.t('eb.scen.base')}"
        auto_all = f"{i18n.t('eb.op.export_csv_auto')}, {i18n.t('eb.scen.all')}"
        xlsx_base = f"{i18n.t('eb.op.export_xlsx')}, {i18n.t('eb.scen.base')}"
        sqlite_all = f"{i18n.t('eb.op.export_sqlite')}, {i18n.t('eb.scen.all')}"
        assert rows[csv_base].endswith("| ✅ |") and "200 MB/s" in rows[csv_base] and "40.0 MB/s" in rows[csv_base]
        assert i18n.t("eb.val.workers", n=4) in rows[csv_base]
        assert rows[auto_all].endswith("| ✅ |") and "13.3 MB/s" in rows[auto_all]        # the second baseline
        assert rows[xlsx_base].endswith("| ❌ |") and i18n.t("eb.val.missing") in rows[xlsx_base]
        assert rows[sqlite_all].endswith("| ❌ |")
        restore_all = f"{i18n.t('eb.op.restore')}, {i18n.t('eb.scen.all')}"
        assert rows[restore_all].endswith("| — |") and "1.70 s" in rows[restore_all] and "600 ms" in rows[restore_all]
        assert text.count(i18n.t("eb.x.cause.export_xlsx")) == 1 and i18n.t("eb.x.opts.export_sqlite") in text
        assert i18n.t("eb.x.cause.export_csv") not in text                       # CSV met the target
        for name in ("after.json", "b1.json", "b2.json"):
            assert name in text
        for sec in ("eb.x.sec.main", "eb.sec.misses", "eb.sec.machine", "eb.sec.conditions"):
            assert i18n.t(sec) in text
    finally:
        i18n.set_language(old)


def test_edit_report_takes_notes(tmp_path: Path) -> None:
    old = i18n.get_language()
    try:
        after = {"started": 2.0, "args": {"repeat": "2"}, "runs": [_run("export_csv", "base", 10.0, 250.0)]}
        p = tmp_path / "a.json"
        p.write_text(json.dumps(after), encoding="utf-8")
        md = eb.write_report([p], None, "uz", tmp_path / "e.md", notes=["eb.cond.p6"])
        i18n.set_language("uz")
        assert i18n.t("eb.cond.p6") in md.read_text(encoding="utf-8")
        eb.main(["--report", str(p), "--note", "eb.cond.p6", "--lang", "en", "--md", str(tmp_path / "f.md")])
        i18n.set_language("en")
        assert i18n.t("eb.cond.p6") in (tmp_path / "f.md").read_text(encoding="utf-8")
    finally:
        i18n.set_language(old)


def test_sampler_lists_child_processes_only_now_and_then(monkeypatch) -> None:
    calls = {"children": 0}

    class Mem:
        rss = 10_000_000
        private = 8_000_000

    class Proc:
        pid = 4242

        def memory_info(self) -> Any:
            return Mem()

        def children(self, recursive: bool = False) -> list:
            calls["children"] += 1
            return [Proc()]

    sampler = eb._Sampler.__new__(eb._Sampler)          # (no sampling thread)
    sampler.proc = Proc()
    sampler.lock = threading.Lock()
    sampler.children = {}
    sampler.kids = []
    sampler.kids_at = 0.0
    clock = {"t": 100.0}
    monkeypatch.setattr(eb.time, "monotonic", lambda: clock["t"])
    for _ in range(10):
        assert sampler._measure() == (20_000_000, 16_000_000, 1)       # this process + its child
    assert calls["children"] == 1
    clock["t"] += eb.CHILD_REFRESH
    sampler._measure()
    assert calls["children"] == 2
    sampler._measure(refresh=True)                                      # (start / stop of a phase)
    assert calls["children"] == 3
