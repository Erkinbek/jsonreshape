"""L3 item 4: the statistics panel and the R5-fix-analysis statistics.

* ``NumberStats.sum_skipped`` (numbers beyond the exact range left out of the sum and the mean, warning
  ``stats.warn.sum_skipped``): the sum row said "≈ … (50 significant digits)" -- what a ROUNDED sum says --
  and nothing told how many numbers were left out; the mean may now be "—" (none left).
* Live snapshots of the exact (spill) mode and of the sketches are estimates (``distinct_rse`` /
  ``top_bound`` set): labelled "≈" with their error while computing; exact-so-far snapshots are not.
* The rendering paths that do not involve smart values (no numbers, no values, values too long to keep,
  strings that are no longer images after R5A-12) still render.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import stats
from jsonreshape.gui import statspanel
from jsonreshape.gui.statspanel import StatsPanel, summary_rows
from jsonreshape.i18n import t
from jsonreshape.paths import MISSING
from jsonreshape.stats import FieldStats, NumberStats, ValueCount
from stats_gui_helpers import TIMEOUT, app, backend, opened  # noqa: F401 - fixtures

pytestmark = pytest.mark.gui

HUGE = "1e99999999999999999999"            # beyond Decimal's exponent range: left out of the sum


@pytest.fixture
def panel(qtbot, app):
    made: list[StatsPanel] = []

    def make() -> StatsPanel:
        p = StatsPanel(app)
        qtbot.addWidget(p)
        p.resize(520, 760)
        made.append(p)
        return p

    yield make
    for p in made:
        p.set_session(None)
        p.shutdown()


def compute(qtbot, p: StatsPanel, field: str, **kw: Any) -> FieldStats:
    p.set_field_text(field)
    p.set_scope("all")
    assert p.compute(**kw) is not None, p.status_text()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    fs = p.result()
    assert fs is not None and not fs.partial
    return fs


def write_jsonl(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def fake_stats(*, number: NumberStats | None = None, warnings: tuple = (), partial: bool = False,
               distinct_rse: float | None = None, top_bound: int = 0, top: list | None = None,
               distinct: int = 3) -> FieldStats:
    return FieldStats(field="x", path=("x",), is_key=False, scanned=10, skipped=0, base=10, present=10, missing=0,
                      values=10, nulls=0, types={"number": 10}, distinct=distinct,
                      distinct_exact=not partial and distinct_rse is None, distinct_rse=distinct_rse,
                      top=top or [], top_exact=not partial and not top_bound, top_bound=top_bound, number=number,
                      string=None, arr_len=None, obj_keys=None, gen=None, elapsed=0.1, warnings=warnings,
                      partial=partial)


def numbers(*, sum_text: str = "12", mean_text: str = "6", exact: bool = True, skipped: int = 0) -> NumberStats:
    return NumberStats(count=3, integers=2, min_text="5", max_text=HUGE, min_first=0, max_first=1, sum_text=sum_text,
                       mean_text=mean_text, exact_sum=exact, sum_skipped=skipped)


def row(fs: FieldStats, kind: str) -> Any:
    return next(r for r in summary_rows(fs) if r.kind == kind)


# ----------------------------------------------------------------------------------
# rows (pure)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("lang", ["uz", "en"])
def test_sum_and_mean_rows_say_what_happened(lang: str) -> None:
    from jsonreshape import i18n

    old = i18n.get_language()
    i18n.set_language(lang)
    try:
        exact = fake_stats(number=numbers())
        assert (row(exact, "sum").value, row(exact, "mean").value, row(exact, "sum").tip) == ("12", "6", "")
        rounded = fake_stats(number=numbers(exact=False), warnings=("stats.warn.sum_approximate",))
        assert row(rounded, "sum").value == t("gui.statspanel.sum.approx", sum="12")
        assert "≈" in row(rounded, "sum").value and row(rounded, "mean").value == "6"
        # left out, not rounded: the R5A-11 wording, with the count; the tip is the warning
        skipped = fake_stats(number=numbers(exact=False, skipped=1), warnings=("stats.warn.sum_skipped",))
        assert row(skipped, "sum").value == t("gui.statspanel.sum.skipped.one", sum="12", count="1")
        assert "≈" not in row(skipped, "sum").value and "50" not in row(skipped, "sum").value
        assert row(skipped, "mean").value == t("gui.statspanel.mean.skipped.one", mean="6", count="1")
        assert row(skipped, "sum").tip == row(skipped, "mean").tip == t("stats.warn.sum_skipped.one", count="1")
        assert row(skipped, "sum").copy == "12" and row(skipped, "mean").copy == "6"     # the numbers themselves
        both = fake_stats(number=numbers(exact=False, skipped=2),
                          warnings=("stats.warn.sum_skipped", "stats.warn.sum_approximate"))
        assert row(both, "sum").value == t("gui.statspanel.sum.approx_skipped", sum="12", count="2")
        # nothing left: the mean is "—"
        none = fake_stats(number=numbers(exact=False, skipped=3, sum_text="0", mean_text="—"),
                          warnings=("stats.warn.sum_skipped",))
        assert row(none, "mean").value == "—"
        assert row(none, "sum").value == t("gui.statspanel.sum.skipped", sum="0", count="3")
        # an inexact sum without a known reason keeps the rounded wording
        odd = fake_stats(number=numbers(exact=False))
        assert row(odd, "sum").value == t("gui.statspanel.sum.approx", sum="12")
    finally:
        i18n.set_language(old)


def test_distinct_of_live_snapshots(lang_uz) -> None:
    so_far = fake_stats(partial=True, distinct=42)
    assert row(so_far, "distinct").value == t("gui.statspanel.distinct.partial", count="42") == "hozircha 42"
    est = fake_stats(partial=True, distinct=40000, distinct_rse=stats.HLL_RSE, top_bound=7)
    text = row(est, "distinct").value
    assert text == t("gui.statspanel.distinct.partial_approx", count=statspanel._count(40000),
                     error=statspanel._count(round(40000 * stats.HLL_RSE)), rse="0,8")
    assert text.startswith("hozircha ≈") and "HyperLogLog" in text and "±325" in text
    final = fake_stats(distinct=40000, distinct_rse=stats.HLL_RSE)
    assert row(final, "distinct").value.startswith("≈")


# ----------------------------------------------------------------------------------
# the panel with real computations
# ----------------------------------------------------------------------------------
def test_left_out_numbers_in_the_panel(qtbot, opened, panel, tmp_path: Path, backend, lang_uz) -> None:
    path = write_jsonl(tmp_path / "nums.jsonl", ['{"x": 5}', '{"x": %s}' % HUGE, '{"x": 7}', '{"y": 1}',
                                                 '{"z": %s}' % HUGE, '{"z": -%s}' % HUGE])
    s = opened(path)
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "x")
    assert fs.number is not None and fs.number.sum_skipped == 1 and fs.number.sum_text == "12"
    assert p.summary_text("sum") == "12 (1 ta son qo'shilmadi)"
    assert p.summary_text("mean") == t("gui.statspanel.mean.skipped.one", mean=fs.number.mean_text, count="1")
    assert p.summary_text("min") == "5" and p.summary_text("max") == HUGE
    warning = t("stats.warn.sum_skipped.one", count="1")
    assert warning in p.warnings_label.text() and p.warnings_label.isVisibleTo(p)
    kinds = p.summary_kinds()
    item = p.summary.item(kinds.index("sum"), 1)
    assert warning in item.toolTip()
    # every number left out: no mean
    fs = compute(qtbot, p, "z")
    assert fs.number.sum_skipped == 2 and fs.number.mean_text == "—"
    assert p.summary_text("mean") == "—"
    assert p.summary_text("sum") == t("gui.statspanel.sum.skipped", sum=fs.number.sum_text, count="2")
    assert t("stats.warn.sum_skipped", count="2") in p.warnings_label.text()


def test_rounded_and_left_out(qtbot, opened, panel, tmp_path: Path, lang_en) -> None:
    path = write_jsonl(tmp_path / "mixed.jsonl", ['{"x": 1e999999}', '{"x": 1}', '{"x": %s}' % HUGE])
    s = opened(path)
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "x")
    assert set(fs.warnings) >= {"stats.warn.sum_skipped", "stats.warn.sum_approximate"}
    assert p.summary_text("sum") == t("gui.statspanel.sum.approx_skipped.one", sum=fs.number.sum_text, count="1")
    assert p.summary_text("sum").startswith("≈ ") and p.summary_text("sum").endswith("1 number left out)")


@pytest.fixture
def held(monkeypatch):
    """``field_stats`` of the panel holds its worker after the first live snapshot that satisfies ``want``."""
    gate = threading.Event()
    seen = threading.Event()
    real = stats.field_stats
    box: dict[str, Any] = {"want": lambda fs: True}

    def fake(doc, fields, source=None, **kw):
        on_partial = kw["on_partial"]

        def partial(snaps):
            on_partial(snaps)
            if not seen.is_set() and box["want"](snaps[0]):
                seen.set()
                gate.wait(30)

        kw["on_partial"] = partial
        return real(doc, fields, source, **kw)

    monkeypatch.setattr(statspanel, "field_stats", fake)
    monkeypatch.setattr(stats, "PARTIAL_SECONDS", 0.0)
    monkeypatch.setattr(stats, "PARTIAL_COST_FACTOR", 0.0)
    monkeypatch.setattr(stats, "CHUNK_RECORDS", 8)
    yield gate, seen, box
    gate.set()


@pytest.mark.parametrize("exact", [True, False], ids=["spill", "sketch"])
def test_live_estimates_are_marked(qtbot, opened, panel, tmp_path: Path, held, exact: bool, lang_uz) -> None:
    gate, seen, box = held
    n = 2400
    path = write_jsonl(tmp_path / "ids.jsonl", ['{"id": %d, "g": %d}' % (i, i % 3) for i in range(n)])
    s = opened(path)
    p = panel()
    p.set_session(s)
    p.memory_limit = 1500                                   # a few entries: spills / sketches at once
    box["want"] = lambda fs: bool(fs.distinct_rse) and fs.top_bound > 0
    p.set_field_text("id")
    p.set_scope("all")
    assert p.compute(exact=exact) is not None
    qtbot.waitUntil(lambda: seen.is_set() and p.result() is not None and p.result().top_bound > 0, timeout=TIMEOUT)
    fs = p.result()
    assert fs.partial and fs.distinct_rse and p.status() == "running"
    text = p.summary_text("distinct")
    assert text == t("gui.statspanel.distinct.partial_approx", count=statspanel._count(fs.distinct),
                     error=statspanel._count(round(fs.distinct * fs.distinct_rse)), rse="0,8")
    assert text.startswith("hozircha ≈")
    assert p.top_title.text() == t("gui.statspanel.top.title_partial_approx", bound=statspanel._count(fs.top_bound))
    assert p.top_count() > 0 and p.top_table.item(0, 2).text().startswith("≈")
    assert not p.warnings_label.isVisibleTo(p)               # no warnings for a live snapshot
    gate.set()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    final = p.result()
    assert not final.partial and final.scanned == n
    if exact:
        assert final.distinct == n and p.summary_text("distinct") == t("gui.statspanel.distinct.exact",
                                                                       count=statspanel._count(n))
    else:
        assert not final.distinct_exact and p.summary_text("distinct").startswith("≈")


def test_exact_so_far_snapshots_have_no_estimate_mark(qtbot, opened, panel, tmp_path: Path, held, lang_uz) -> None:
    gate, seen, box = held
    path = write_jsonl(tmp_path / "small.jsonl", ['{"c": "%s"}' % "abc"[i % 3] for i in range(64)])
    s = opened(path)
    p = panel()
    p.set_session(s)
    p.set_field_text("c")
    p.set_scope("all")
    assert p.compute() is not None
    qtbot.waitUntil(lambda: seen.is_set() and p.result() is not None, timeout=TIMEOUT)
    fs = p.result()
    assert fs.partial and fs.distinct_rse is None and fs.top_bound == 0
    assert p.summary_text("distinct") == t("gui.statspanel.distinct.partial", count=str(fs.distinct))
    assert "≈" not in p.summary_text("distinct")
    assert p.top_title.text() == t("gui.statspanel.top.title_partial")
    assert all(not p.top_table.item(i, 2).text().startswith("≈") for i in range(p.top_count()))
    gate.set()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)


def test_results_without_numbers_values_or_smart_values_render(qtbot, opened, panel, tmp_path: Path) -> None:
    """Shapes the panel must render: a field without values, strings only (no number rows), a value too
    long to keep (``ValueCount.value`` MISSING), base64 texts that are no longer images (R5A-12)."""
    long_text = "q" * (stats.VALUE_DECODE_BYTES + 10)
    path = write_jsonl(tmp_path / "shapes.jsonl", ['{"s": "iVBORw0KGgo=", "l": "%s"}' % long_text,
                                                   '{"s": "iVBORw0KGgo=", "l": "%s"}' % long_text,
                                                   '{"s": "/9j/4AAQ"}'])
    s = opened(path)
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "s")
    assert fs.number is None and "mean" not in p.summary_kinds() and "sum" not in p.summary_kinds()
    assert p.top_value(0).text == '"iVBORw0KGgo="' and p.top_value(0).count == 2
    assert p.top_table.item(0, 1).text() == '"iVBORw0KGgo="'
    fs = compute(qtbot, p, "l")
    assert p.top_count() == 1 and p.top_value(0).value is MISSING and p.top_filters(0) == []
    fs = compute(qtbot, p, "nothing")
    assert fs.values == 0 and p.top_count() == 0 and p.top_title.text() == t("gui.statspanel.top.empty")
    assert p.summary_text("distinct") == t("gui.statspanel.distinct.exact", count="0")
    # a synthetic live snapshot of a long value renders as well
    vc = ValueCount(MISSING, '"qqq…"', 3, 3, 0, 2)
    rows = summary_rows(fake_stats(partial=True, distinct=5, distinct_rse=stats.HLL_RSE, top_bound=2, top=[vc]))
    assert [r.kind for r in rows][:3] == ["present", "null", "missing"]
