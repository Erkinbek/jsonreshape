"""bench/edit_bench.py (the stage-3 editing benchmark): its texts are complete in both languages and the report of
a results file (with a baseline, and merged with a later partial run) has the §12 rows with the right statuses."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from jsonreshape import i18n

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import edit_bench as eb  # noqa: E402


def test_every_text_key_has_uz_and_en() -> None:
    tree = ast.parse((BENCH / "edit_bench.py").read_bytes().decode("utf-8"))
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t" and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            used.add(node.args[0].value)
    for op in ("export_json", "export_jsonl", "export_csv", "save_as", "patch_export", "patch_import", "restore",
               "undo_redo"):
        used.add(f"eb.op.{op}")
    for scen in eb.SCENARIOS:
        used.add(f"eb.scen.{scen}")
    for kind in eb.BULK_KINDS:
        used.add(f"eb.bulk.what.{kind}")
    for cat in ("csv", "export", "save", "bulk", "memory"):
        used |= {f"eb.cause.{cat}", f"eb.opts.{cat}"}
    for what in ("data", "work"):
        used.add(f"eb.machine.disk.{what}")
    assert len(used) > 60
    missing = [k for k in used if k not in eb.EDIT_BENCH_MESSAGES]
    assert not missing, missing
    for key, entry in eb.EDIT_BENCH_MESSAGES.items():
        assert entry.get("uz") and entry.get("en"), key


def _run(op: str, scen: str, phase: str, seconds: float, rss: float, nbytes: int = 2_000_000_000, **more) -> dict:
    ph = {"seconds": seconds, "rss_peak_mb": rss, "private_peak_mb": rss * 0.8, "workers": 0, "fsync_s": 0.0,
          "bytes": nbytes, **more}
    return {"op": op, "scen": scen, "phases": {"open": {"seconds": 0.01, "rss_peak_mb": 40, "private_peak_mb": 30,
                                                         "records": 6513235}, phase: ph},
            "effective_bytes": nbytes, "backend": "rust", "wall": seconds + 1, "load_before": 10.0,
            "load_mean": 12.0, "load_max": 20.0}


@pytest.mark.parametrize("lang", ["uz", "en"])
def test_report_rows_statuses_baseline_and_merge(tmp_path: Path, lang: str) -> None:
    old_lang = i18n.get_language()
    try:
        before = {"started": 1.0, "args": {"repeat": "2"}, "runs": [
            _run("export_json", "third", "op", 40.0, 180.0), _run("make", "all", "bulk_preview", 120.0, 290.0)]}
        after = {"started": 2.0, "args": {"repeat": "2", "data": str(tmp_path / "x.json")}, "runs": [
            _run("export_json", "third", "op", 30.0, 120.0), _run("export_csv", "third", "op", 70.0, 130.0),
            _run("make", "all", "bulk_preview", 20.0, 300.0, workers=4),
            _run("patch_import", "all", "op", 300.0, 900.0)]}
        later = {"started": 3.0, "args": {}, "runs": [_run("patch_import", "all", "op", 150.0, 330.0)]}
        paths = []
        for name, data in (("before.json", before), ("after.json", after), ("later.json", later)):
            p = tmp_path / name
            p.write_text(json.dumps(data), encoding="utf-8")
            paths.append(p)
        md = eb.write_report([paths[1], paths[2]], paths[0], lang, tmp_path / "r.md")
        text = md.read_text(encoding="utf-8")
        i18n.set_language(lang)
        rows = {line.split(" | ")[0].lstrip("| "): line for line in text.splitlines() if line.startswith("| ")}
        json_third = f"{i18n.t('eb.op.export_json')}, {i18n.t('eb.scen.third')}"
        csv_third = f"{i18n.t('eb.op.export_csv')}, {i18n.t('eb.scen.third')}"
        assert rows[json_third].endswith("| ✅ |") and "66.7 MB/s" in rows[json_third] and "50.0 MB/s" in rows[json_third]
        assert rows[csv_third].endswith("| ❌ |")                         # 28.6 MB/s < 50
        imp = f"{i18n.t('eb.op.patch_import')}, {i18n.t('eb.scen.all')}"
        assert rows[imp].endswith("| ✅ |") and "150 s" in rows[imp]      # the later run replaced the first
        bulk = i18n.t("eb.op.make_bulk", scen=i18n.t("eb.scen.all"))
        assert rows[bulk].endswith("| ✅ |") and i18n.t("eb.val.workers", n=4) in rows[bulk]
        for sec in ("eb.sec.main", "eb.sec.extra", "eb.sec.misses", "eb.sec.machine", "eb.sec.conditions"):
            assert i18n.t(sec) in text
        assert i18n.t("eb.cause.csv") in text and "later.json" in text and "before.json" in text
    finally:
        i18n.set_language(old_lang)
