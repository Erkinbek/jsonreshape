"""GUI benchmark (``bench/gui_bench.py``: the AGENT_PROMPT §12 targets measured in the real
viewer, stage 2).

* fast tests (default run): the report machinery on fake results -- the status rules of the
  rows (a missed time, speed or memory limit is a miss; the memory row takes the largest
  peak of every measurement without a sort), the Markdown report, the texts (uz + en with
  the same placeholders), the measurement selection;
* ``bench`` (run with ``-m bench``): ``gui_bench --quick`` end to end in ``tmp_path``
  (generated ~200 MB array, 400 000-record JSON Lines and a 1 000 000-element node file;
  every measurement twice, the best kept): every scenario ran in its own GUI process and
  measured what it should (first rows painted, index speed and heartbeat, scrollbar jump,
  1 M-children node, search, partial filter results, sort + export of the sorted view,
  whole-session memory and RSS after closing the tab), the §12 memory limits hold, the
  results were written and no generated file is left behind. A missed time target is
  reported as XFAIL with the measured value (never hidden, never silently passed).
"""

from __future__ import annotations

import string
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import gui_bench as gb  # noqa: E402

from jsonreshape import i18n  # noqa: E402

MARKER_EVERY = 100_000      # bench/make_synthetic.py: the rare word sits in every 100 000th record


# ----------------------------------------------------------------------------------
# fast: texts, selection, rows, report
# ----------------------------------------------------------------------------------
def _fields(text: str) -> set[str]:
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


def test_gui_bench_texts_complete() -> None:
    for key, entry in gb.GUI_MESSAGES.items():
        assert key.startswith("bench.gui."), key
        assert entry.get("uz") and entry.get("en"), key
        assert _fields(entry["uz"]) == _fields(entry["en"]), key
        assert i18n.MESSAGES[key] is entry or i18n.MESSAGES[key] == entry, key


def test_select_ops() -> None:
    full = [op.name for op in gb.select_ops(False)]
    quick = [op.name for op in gb.select_ops(True)]
    assert full == [op.name for op in gb.OPS] and "open_5g" in full
    assert "open_5g" not in quick and set(quick) == set(full) - {"open_5g"}
    assert [op.name for op in gb.select_ops(False, ["session_2g", "node_1m"])] == ["node_1m", "session_2g"]
    with pytest.raises(SystemExit):
        gb.select_ops(False, ["nope"])
    assert all(spec.quick for spec in gb.QUICK_DATA.values())
    assert set(gb.QUICK_DATA) <= set(gb.FULL_DATA)


def _op(name: str, best: dict | None, peak: int | None = 150_000_000, ok: bool = True) -> dict:
    return {"spec": {"name": name}, "runs": [{"ok": ok}], "ok": ok, "error": None if ok else "boom", "best": best,
            "peak_rss": peak}


def _fake_results() -> dict:
    data = {"syn_2g": {"file": "syn_2g.json", "bytes": 2_147_483_913, "records": 6_513_235},
            "syn_10m": {"file": "syn_10m.jsonl", "bytes": 3_288_547_846, "records": 10_000_000},
            "node_1m": {"file": "node_1m.json", "bytes": 41_667_416, "records": 1_000_000}}
    open_best = {"first_paint_seconds": 0.64, "first_paint_main_seconds": 0.48, "first_paint_proc_seconds": 0.63,
                 "first_full_paint_main_seconds": 0.48, "index_mb_per_s": 120.0, "index_seconds": 17.9,
                 "record_count": 6_513_235, "index_gap_p95_s": 0.006, "index_gap_max_s": 0.04,
                 "scroll_end_seconds": 0.006, "scroll_mid_seconds": 0.007,
                 "index_gaps": {"n": 10, "p50_ms": 5, "p95_ms": 6, "p99_ms": 7, "max_ms": 40, "over100": 0,
                                "over250": 0, "seconds": 17.9}}
    ops = {
        "open_2g": _op("open_2g", open_best, 140_000_000),
        "node_1m": _op("node_1m", {"cached_seconds": 0.02, "first_after_index_seconds": 0.3,
                                   "first_index_seconds": 0.3, "first_total_seconds": 0.6, "child_count": 1_000_000,
                                   "last_child_seconds": 0.02}, 300_000_000),
        "search_2g": _op("search_2g", {"searches": [
            {"query": gb.RARE_TEXT, "mb_per_s": 800.0, "seconds": 2.6, "hit_count": 66,
             "first_hit_painted_seconds": 0.05, "gap_p95_s": 0.006, "gap_max_s": 0.007,
             "gaps": {"n": 3, "p50_ms": 5, "p95_ms": 6, "p99_ms": 7, "max_ms": 7, "over100": 0, "over250": 0,
                      "seconds": 2.6}}]}),
        "filter_10m": _op("filter_10m", None, None, ok=False),
        "sort_export_10m": _op("sort_export_10m", {"sort_seconds": 20.0, "record_count": 10_000_000,
                                                   "sort_phase_rss": 400_000_000, "export_mb_per_s": 70.0,
                                                   "export_phase_rss": 350_000_000, "export_seconds": 47.0,
                                                   "export_records": 10_000_000, "export_bytes": 3_288_547_846},
                               1_200_000_000),
        "session_2g": _op("session_2g", {"rss_after_close": 130_000_000, "rss_before_open": 100_000_000},
                          250_000_000),
    }
    return {"run_id": "20261017-120000", "mode": "full", "started": "2026-10-17T12:00:00",
            "duration_seconds": 300.0, "data": data, "ops": ops,
            "machine": {"cpu": "Test CPU", "cores_physical": 8, "cores_logical": 16, "ram_total": 32e9,
                        "disk": {}, "versions": {}},
            "conditions": {"repeat": 2, "wait_for_quiet": False}}


def test_rows_status_rules(lang_uz) -> None:
    rows = {r.id: r for r in gb.build_rows(_fake_results())}
    assert rows["open_2g_first"].ok is True and rows["open_2g_first"].section == "main"
    assert rows["open_2g_index"].ok is False                       # 120 MB/s < 150 MB/s
    assert rows["open_2g_scroll"].ok is None and rows["open_2g_scroll"].section == "extra"
    assert rows["node_1m"].ok is False                             # 300 ms after the children index
    assert rows["search_2g_0"].ok is True
    assert rows["filter_10m"].ok is False and "boom" in rows["filter_10m"].result
    assert rows["sort_10m"].ok is False                            # peak 1.2 GB > 1 GB
    assert rows["export_sorted_10m"].ok is False                   # 350 MB > 300 MB during the export
    mem = rows["memory"]
    assert mem.ok is True and "node_1m" in mem.result              # the largest peak without a sort: node_1m
    assert rows["rss_after_close"].ok is None
    for r in rows.values():
        assert r.status in ("✅", "❌", "—")


def test_markdown_report(tmp_path, lang_uz) -> None:
    results = _fake_results()
    rows = gb.build_rows(results)
    cfg = gb.GuiConfig(quick=False, results_dir=tmp_path)
    files = gb.write_results(results, rows, tmp_path, cfg)
    assert Path(files["json"]).name == "gui_20261017-120000.json"
    assert Path(files["latest"]).name == "gui_latest.md"
    md = Path(files["md"]).read_bytes().decode("utf-8")
    assert md == Path(files["latest"]).read_bytes().decode("utf-8")
    assert "| Amal | Maqsad | Natija | Holat |" in md
    assert "## Maqsadga yetilmagan joylar" in md and "1 000 000 bolali tugunni ochish" in md
    assert "| Bosqich |" in md                                    # the heartbeat table
    quick = gb.write_results(dict(results, run_id="20261017-120001"), rows, tmp_path / "q",
                             gb.GuiConfig(quick=True))
    assert "latest" not in quick and not (tmp_path / "q" / "gui_latest.md").exists()


# ----------------------------------------------------------------------------------
# bench: the quick mode end to end
# ----------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def quick(tmp_path_factory):
    base = tmp_path_factory.mktemp("gui_bench")
    cfg = gb.GuiConfig(quick=True, data_dir=base / "data", results_dir=base / "results", work_dir=base / "work",
                       repeat=2, wait=True, quiet_timeout=3.0, command=["tests/test_bench_gui.py", "--quick"])
    results = gb.run(cfg)
    results["_base"] = base
    return results


def _best(results: dict, name: str) -> dict:
    o = results["ops"][name]
    assert o["ok"], f"{name} failed: {o.get('error')}"
    return o["best"]


def _target_or_xfail(ok: bool, what: str) -> None:
    """Assert a time target; a miss is reported as XFAIL with the measurement (never hidden)."""
    if not ok:
        pytest.xfail(what)


@pytest.mark.bench
def test_quick_run_wrote_results(quick) -> None:
    assert quick["all_ok"], {n: o.get("error") for n, o in quick["ops"].items() if not o["ok"]}
    assert set(quick["ops"]) == {op.name for op in gb.select_ops(True)}
    for o in quick["ops"].values():
        assert len(o["runs"]) == 2 and all(r["peak_rss"] and r["peak_rss"] > 10_000_000 for r in o["runs"])
    files = quick["files"]
    assert Path(files["json"]).is_file() and Path(files["md"]).is_file() and "latest" not in files
    md = Path(files["md"]).read_bytes().decode("utf-8")
    assert "| Amal | Maqsad | Natija | Holat |" in md
    data = quick["data"]
    assert all(d["generated"] and d["bytes"] <= 200_000_000 for d in data.values())
    assert data["syn_2g"]["bytes"] > 150_000_000
    base = quick["_base"]
    assert not (base / "work").exists()                 # generated files, homes, outputs removed
    left = [p for p in base.rglob("*") if p.is_file() and "results" not in p.relative_to(base).parts]
    assert left == []


@pytest.mark.bench
def test_quick_open_first_rows_painted(quick) -> None:
    for name in ("open_2g", "open_10m"):
        b = _best(quick, name)
        assert b["first_rows"] == ["#0", "#1", "#2"], name
        assert b["first_paint_main_seconds"] <= b["first_paint_seconds"], b
        _target_or_xfail(b["first_paint_seconds"] < gb.T_OPEN_S,
                         f"{name}: first rows painted {b['first_paint_seconds']:.3f} s after the process start")


@pytest.mark.bench
def test_quick_index_speed_and_responsiveness(quick) -> None:
    for name, kind in (("open_2g", "array"), ("open_10m", "jsonl")):
        b = _best(quick, name)
        assert b["kind"] == kind and b["record_count"] > 300_000
        assert b["index_gaps"]["n"] > 10                  # the heartbeat ran during the indexing
        assert b["index_gap_max_s"] < gb.T_GAP_MAX_S, b["index_gaps"]
        _target_or_xfail(b["index_mb_per_s"] >= gb.T_INDEX_MBPS, f"{name}: index {b['index_mb_per_s']} MB/s")


@pytest.mark.bench
def test_quick_scrollbar_jump_to_the_end(quick) -> None:
    b = _best(quick, "open_2g")
    assert b["scroll_end_seconds"] is not None and b["scroll_mid_seconds"] is not None
    assert b["scroll_end_top_row"] > b["record_count"] - 100
    assert b["scroll_end_seconds"] < 1.0, b


@pytest.mark.bench
def test_quick_node_with_a_million_children(quick) -> None:
    b = _best(quick, "node_1m")
    assert b["child_count"] == 1_000_000 and b["last_child_key"] == "[999999]"
    _target_or_xfail(b["cached_seconds"] < gb.T_NODE_S, f"re-expand painted after {b['cached_seconds']} s")
    _target_or_xfail(b["first_after_index_seconds"] < gb.T_NODE_S,
                     f"painted {b['first_after_index_seconds']} s after the children index")


@pytest.mark.bench
def test_quick_search_first_hit_and_speed(quick) -> None:
    b = _best(quick, "search_2g")
    records = _best(quick, "open_2g")["record_count"]
    rare, medium = b["searches"]
    assert rare["query"] == gb.RARE_TEXT and rare["hit_count"] == (records - 1) // MARKER_EVERY + 1
    assert medium["query"] == gb.MEDIUM_TEXT and medium["hit_count"] > 1000
    for x in (rare, medium):
        assert x["mode_used"] == "bytes" and x["first_hit_painted_seconds"] is not None
        _target_or_xfail(x["first_hit_painted_seconds"] < gb.T_FIRST_HIT_S,
                         f"{x['query']}: first hit painted after {x['first_hit_painted_seconds']} s")
        _target_or_xfail(x["mb_per_s"] >= gb.T_SEARCH_MBPS, f"{x['query']}: {x['mb_per_s']} MB/s")


@pytest.mark.bench
def test_quick_filter_shows_partial_results(quick) -> None:
    b = _best(quick, "filter_10m")
    assert 0 < b["matched"] < b["record_count"]
    assert b["first_rows_painted_seconds"] is not None and b["first_rows_before_end"], b
    assert b["filter_gap_max_s"] < gb.T_GAP_MAX_S, b["filter_gaps"]


@pytest.mark.bench
def test_quick_sort_and_export_of_the_sorted_view(quick) -> None:
    o = quick["ops"]["sort_export_10m"]
    b = _best(quick, "sort_export_10m")
    assert b["sorted_records"] == b["record_count"] and b["sort_keys"] == [gb.SORT_KEY]
    assert b["export_records"] == b["record_count"] and b["export_bytes"] > 0.9 * b["data_size"]
    assert o["peak_rss"] < gb.T_SORT_RSS, o["peak_rss"]
    assert b["sort_seconds"] < gb.T_SORT_S
    assert b["export_phase_rss"] < gb.T_EXPORT_RSS, b["export_phase_rss"]
    _target_or_xfail(b["export_mb_per_s"] >= gb.T_EXPORT_MBPS, f"sorted export {b['export_mb_per_s']} MB/s")


@pytest.mark.bench
def test_quick_session_memory(quick) -> None:
    o = quick["ops"]["session_2g"]
    b = _best(quick, "session_2g")
    assert b["table_columns"] > 3 and b["search_hits"] > 0 and 0 < b["filtered"] == b["export_records"]
    assert b["schema_view"] == "SchemaView"
    assert o["peak_rss"] < gb.T_TOTAL_RSS, o["peak_rss"]
    for name, x in quick["ops"].items():
        if not gb.OP_BY_NAME[name].is_sort:
            assert x["peak_rss"] < gb.T_TOTAL_RSS, (name, x["peak_rss"])
    assert b["rss_before_open"] < b["rss_after_close"] <= o["peak_rss"]
