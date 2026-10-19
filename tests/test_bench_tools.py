"""The benchmark machinery added in stage 6 (bench/ops.py ``cli``, run_bench's whole-program memory, the mapping
rows, bench/tools_bench.py): fast tests on tiny files and fake results.

* ``ops.py cli`` runs a jsonreshape command line in its process like the user runs it: the exit code is checked,
  the index time is measured apart (``Document.build_index``), engine.apply's report (workers, passes) and the
  captured tool reports are returned, the output is removed afterwards;
* ``run_bench.program_peak`` judges an operation with worker processes by the sum over its processes, a single
  process by its exact peak; the mapping rows of the §12 table use it (≥ 50 MB/s, RAM < 300 MB);
* ``tools_bench.prepare_variants`` writes the prepared inputs exactly as described (changed / removed / inserted
  records, 1 % broken records that repair fixes, the ~190 MiB pair under the align limit), the rows and the report.
"""

from __future__ import annotations

import json
import string
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import ops  # noqa: E402
import run_bench as rb  # noqa: E402
import tools_bench as tb  # noqa: E402

from jsonreshape import i18n  # noqa: E402


@pytest.fixture
def tiny(tmp_path) -> Path:
    import make_synthetic

    path = tmp_path / "tiny.json"
    make_synthetic.generate(str(path), size=None, records=2000, fmt="array", seed=5, quiet=True)
    return path


def _run_op(capsys, argv: list[str]) -> dict:
    code = ops.main(argv)
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and res["ok"] is True, (res.get("error"), res.get("traceback"))
    return res


# ----------------------------------------------------------------------------------
# ops.py cli
# ----------------------------------------------------------------------------------
def test_cli_op_applies_the_bench_mapping(tmp_path, capsys, tiny) -> None:
    out = tmp_path / "out" / "mapped.json"
    res = _run_op(capsys, ["cli", str(tiny), str(out), "--keep", "--", "apply", str(rb.MAPPING_FILE), "{in}", "{out}",
                           "-q", "--workers", "1"])
    assert res["command"] == "apply" and res["exit_code"] == 0 and res["indexes"] == 1
    assert res["records_in"] == res["items_out"] == res["record_count"] == 2000
    assert res["workers"] == 1 and res["passes"] == 1 and res["unmapped_records"] == 0
    assert res["total_seconds"] >= res["index_seconds"] + 0.0 and res["seconds"] > 0 and res["index_seconds"] > 0
    assert res["input_bytes"] == tiny.stat().st_size and res["bytes_written"] == out.stat().st_size
    first = json.loads(out.read_bytes())[1]
    # the 6 steps of the mapping: city first, contact unwrapped, oid gathered, tags gone, ball, holat group
    assert list(first)[:4] == ["id", "oid", "city", "name"] and "tags" not in first and "contact" not in first
    assert "ball" in first and first["holat"] == {"active": first["holat"]["active"]}
    assert "city" not in first["address"] and isinstance(first["oid"], list)


def test_cli_op_checks_the_exit_code_and_removes_the_output(tmp_path, capsys, tiny) -> None:
    out = tmp_path / "parts"
    res = _run_op(capsys, ["cli", str(tiny), str(out), "--mkdir", "--capture", "filetools:split", "--", "split",
                           "{in}", "--parts", "3", "-o", "{out}", "-q"])
    assert res["files"] == 3 and res["bytes_written"] > 0.9 * tiny.stat().st_size
    assert not out.exists()                                     # removed without --keep
    assert res["captured"]["filetools:split"][0]["records"] == 2000
    # an unexpected exit code is a failure (the runner reports it); an expected one is not
    code = ops.main(["cli", str(tiny), str(tmp_path / "x"), "--", "validate", "{in}", "--schema",
                     str(tmp_path / "missing.schema.json"), "-q"])
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1 and res["ok"] is False and "exit code 1" in res["error"]


def test_cli_op_captures_reports(tmp_path, capsys, tiny) -> None:
    res = _run_op(capsys, ["cli", str(tiny), str(tmp_path / "unused"), "--capture", "stats:field_stats", "--",
                           "stats", "{in}", "--field", "contact.address.city", "--field", "id", "-q"])
    got = res["captured"]["stats:field_stats"][0]
    assert [x["field"] for x in got] == ["contact.address.city", "id"]
    assert got[1]["distinct"] == 2000 and got[1]["distinct_exact"] is True
    res = _run_op(capsys, ["cli", str(tiny), str(tmp_path / "unused"), "--inputs", str(tiny), "--capture",
                           "cli:_diff_summary@0", "--", "diff", "{in}", str(tiny), "--key", "id", "--summary", "-q"])
    diff = res["captured"]["cli:_diff_summary@0"][0]
    assert diff["equal"] == 2000 and diff["changed"] == diff["added"] == diff["removed"] == 0
    assert diff["identical"] is True and res["exit_code"] == 0      # (the index came from the cache)


def test_scalars_keep_counts_only() -> None:
    from jsonreshape.filetools import MergeReport
    from jsonreshape.writer import ExportReport

    rep = MergeReport(report=ExportReport(fmt="json", records=7), inputs=[], duplicates=3, groups=2)
    got = ops._scalars(rep)
    assert got["duplicates"] == 3 and got["records"] == 7 and "inputs" not in got and "report" not in got
    assert ops._scalars([rep, rep])[1]["groups"] == 2


# ----------------------------------------------------------------------------------
# run_bench: the whole program's memory, the mapping rows
# ----------------------------------------------------------------------------------
def test_program_peak() -> None:
    assert rb.program_peak(None) is None
    assert rb.program_peak({"peak_rss": 100}) == 100
    assert rb.program_peak({"peak_rss": 186, "peak_rss_sum": 312}) == 312      # 4 workers + the parent
    assert rb.program_peak({"peak_rss": 186, "peak_rss_sum": 120}) == 186      # a short peak between two samples


def _apply_op(name: str, *, rate: float, peak: int, total: int | None, procs: int, workers: int) -> dict:
    spec = rb.SPEC_BY_NAME[name]
    best = {"mb_per_s": rate, "seconds": 2147.5 / rate, "total_seconds": 2147.5 / rate + 3.0, "index_seconds": 3.0,
            "workers": workers, "apply_mb_per_s": rate, "bytes_written": 3_300_000_000, "items_out": 6513235,
            "records_in": 6513235, "passes": 1}
    return {"spec": {"name": name, "op": spec.op, "data": spec.data, "is_sort": False, "args": list(spec.args),
                     "out_ext": spec.out_ext},
            "runs": [{"ok": True, "child": best}], "ok": True, "best": best, "peak_rss": peak, "peak_rss_sum": total,
            "peak_private_sum": total, "processes_max": procs}


def test_mapping_rows(lang_uz) -> None:
    import test_bench as tbase

    MB = 1_000_000
    res = tbase.fake_results(
        apply_2g=_apply_op("apply_2g", rate=121.0, peak=186 * MB, total=312 * MB, procs=5, workers=4),
        apply_2g_w1=_apply_op("apply_2g_w1", rate=55.0, peak=150 * MB, total=None, procs=1, workers=1),
        flatten_2g=_apply_op("flatten_2g", rate=40.0, peak=140 * MB, total=None, procs=1, workers=1))
    rows = {r.id: r for r in rb.build_rows(res)}
    main = [r.id for r in rb.build_rows(res) if r.section == "main"]
    assert main.index("apply_2g") == main.index("export_json2") + 1 and main[-1] == "memory"
    assert rows["apply_2g"].ok is False                         # 312 MB for the whole program > 300 MB
    assert "5 ta jarayon birga" in rows["apply_2g"].result and "avtomatik (4 ta)" in rows["apply_2g"].action
    assert rows["apply_2g_w1"].ok is True and "--workers 1" in rows["apply_2g_w1"].action
    assert rows["flatten_2g"].ok is False and rows["flatten_2g"].section == "extra"   # 40 MB/s
    assert rows["memory"].details["peak_rss"] == 312 * MB and rows["memory"].ok is True
    md = rb.render_markdown(res, rb.build_rows(res), "x.json")
    assert "`bench/mapping_syn.json`" in md and "jarayon" in md


def test_mapping_file_is_valid() -> None:
    from jsonreshape import mapping

    tree, info = mapping.load_mapping(rb.MAPPING_FILE)
    assert tree.root_kind == "records" and info["kind"] == "array"
    keys = [c.key for c in tree.find("[]").children]
    assert keys[:4] == ["id", "oid", "city", "name"] and "holat" in keys and "tags" not in keys


# ----------------------------------------------------------------------------------
# tools_bench
# ----------------------------------------------------------------------------------
def test_tools_texts_complete() -> None:
    def fields(text: str) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}

    for key, entry in tb.TOOLS_MESSAGES.items():
        assert key.startswith("bench.tools."), key
        assert set(entry) == {"uz", "en"} and entry["uz"].strip() and entry["en"].strip(), key
        assert fields(entry["uz"]) == fields(entry["en"]), key
    for spec in tb.SPECS:
        assert i18n.has(f"bench.tools.row.{spec.name}"), spec.name
    assert [s.name for s in tb.select_specs(["diff_key", "stats_1"])] == ["stats_1", "diff_key"]
    with pytest.raises(SystemExit):
        tb.select_specs(["nope"])


def test_prepare_variants(tmp_path, tiny) -> None:
    from jsonreshape import repair
    from jsonreshape.reader import Document

    paths = tb.prepare_variants(tiny, tmp_path / "inputs", align_bytes=150_000)
    recs = [json.loads(line) for line in tb._record_lines(tiny)]
    n = len(recs)
    changed = json.loads(paths["changed"].read_bytes())
    removed = sum(1 for i in range(n) if i % 997 == 13)
    inserted = sum(1 for i in range(n) if i % 1000 == 500)
    assert len(changed) == n - removed + inserted
    by_id = {r["id"]: r for r in changed}
    assert by_id[7]["score"] == -1.25 and by_id[8] == recs[8] and 13 not in by_id
    assert 100_000_500 in by_id and by_id[100_000_500]["name"] == "Yangi yozuv"
    inplace = json.loads(paths["inplace"].read_bytes())
    assert len(inplace) == n and inplace[107]["score"] == -1.25 and inplace[108] == recs[108]
    a, b = paths["a190"], paths["b190"]
    assert 0 < a.stat().st_size <= 150_000 and b.stat().st_size <= 150_000
    assert json.loads(a.read_bytes()) == recs[:len(json.loads(a.read_bytes()))]
    # the broken file: every 100th record broken, all of it repaired, the other records unchanged
    with pytest.raises(Exception):
        json.loads(paths["broken1"].read_bytes())
    out = tmp_path / "fixed.json"
    rep = repair.repair_file(paths["broken1"], out, repair.RepairOptions())
    try:
        assert rep.records == n and rep.total >= n // 100
    finally:
        rep.close()
    fixed = json.loads(out.read_bytes())
    assert len(fixed) == n
    assert all(fixed[i] == recs[i] for i in range(n) if i % 100 != 0)
    assert fixed[100]["id"] == 100 and fixed[300]["id"] == 300 and fixed[0]["score"] is None    # NaN -> null
    with Document.open(paths["changed"]) as doc:
        doc.build_index()
        assert doc.record_count == len(changed)


def test_tools_rows_and_report(tmp_path, lang_uz) -> None:
    MB = 1_000_000

    def op(name: str, best: dict | None, peak: int, *, total: int | None = None, procs: int = 1) -> dict:
        spec = tb.SPEC_BY_NAME[name]
        from dataclasses import asdict

        return {"spec": asdict(spec), "runs": [{"ok": best is not None}], "ok": best is not None,
                "error": None if best else "boom", "best": best, "peak_rss": peak, "peak_rss_sum": total,
                "peak_private_sum": total, "processes_max": procs, "repeats_done": 2}

    stats = {"seconds": 10.0, "total_seconds": 13.0, "index_seconds": 3.0, "indexes": 1, "mb_per_s": 214.7,
             "record_count": 6513235, "captured": {"stats:field_stats": [[{"field": "contact.address.city",
                                                                            "distinct": 17, "distinct_exact": True,
                                                                            "values": 6513235}]]}}
    diff = {"seconds": 40.0, "total_seconds": 46.0, "index_seconds": 6.0, "indexes": 2, "mb_per_s": 107.0,
            "captured": {"cli:_diff_summary@0": [{"equal": 6400000, "changed": 65000, "added": 6500,
                                                   "removed": 6500}]}}
    results = {"run_id": "20261019-000000", "mode": "full", "started": "2026-10-19T00:00:00",
               "duration_seconds": 100.0, "machine": {"cpu": "Test CPU"}, "conditions": {"wait_for_quiet": False},
               "data": {"syn_2g": {"path": "bench/data/syn_2g.json", "bytes": 2147483913}},
               "ops": {"stats_1": op("stats_1", stats, 120 * MB), "diff_key": op("diff_key", diff, 600 * MB),
                       "dupes": op("dupes", None, 50 * MB)}}
    rows = {r.id: r for r in tb.build_rows(results)}
    assert rows["stats_1"].ok is True and "contact.address.city: 17 xil" in rows["stats_1"].result
    assert rows["diff_key"].ok is False and "65 000 ta o'zgargan" in rows["diff_key"].result
    assert rows["dupes"].ok is False and "boom" in rows["dupes"].result
    cfg = tb.ToolsConfig(results_dir=tmp_path)
    files = tb.write_results(results, list(rows.values()), tmp_path, cfg)
    md = Path(files["md"]).read_bytes().decode("utf-8")
    assert Path(files["latest"]).name == "tools_latest.md" and md == Path(files["latest"]).read_text(encoding="utf-8")
    assert "| Amal | Maqsad | Natija | Holat |" in md and "## Maqsadga yetilmagan joylar" in md
    assert "6 513 235 ta yozuv" in md
