"""The chart area of the statistics panel (stage 7; gui/statspanel.py module docstring, "Diagramma").

A numeric field shows the histogram of its numbers (kind ``hist``), a string field its length histogram
(``lens``), every field offers the top values as bars (``top``); hovering a bar puts its range, count and share
in the tooltip and the info line; clicking a bar filters by its range (brute-force counts), top-value bars by the
value; the chart hides without values; a sampled histogram is marked "≈"; the user's kind choice is kept; dark
mode renders with theme roles only (pixels checked, the static colour scanner of ``tests/test_theme_colours.py``);
live snapshots show the chart already; the Uzbek texts.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QHelpEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from jsonreshape import stats
from jsonreshape.gui import statspanel, theme
from jsonreshape.gui.statspanel import HIST_BINS, StatsPanel, chart_data, chart_kinds, default_chart_kind
from jsonreshape.i18n import t
from stats_gui_helpers import (  # noqa: F401 - fixtures
    TIMEOUT,
    app,
    count_matching,
    opened,
    reset,
    wait_view,
    write_records,
)
from test_theme_colours import scan_source

pytestmark = pytest.mark.gui


@pytest.fixture
def panel(qtbot, app):
    made: list[StatsPanel] = []

    def make() -> StatsPanel:
        p = StatsPanel(app)
        qtbot.addWidget(p)
        p.resize(520, 820)
        made.append(p)
        return p

    yield make
    for p in made:
        p.set_session(None)
        p.shutdown()


@pytest.fixture
def records_path(tmp_path: Path) -> Path:
    return write_records(tmp_path / "records.json")


@pytest.fixture
def themed(qapp):
    """``use(scheme)`` applies the scheme; the palette, style and theme mode are restored afterwards."""
    from PySide6.QtGui import QPalette

    palette = QPalette(qapp.palette())
    style = qapp.style().name()

    def use(scheme: str) -> None:
        theme.apply_theme(qapp, scheme)

    yield use
    theme._reset_for_tests()
    try:
        qapp.styleHints().unsetColorScheme()
    except AttributeError:  # pragma: no cover
        pass
    qapp.setStyle(style)
    qapp.setPalette(palette)
    theme.invalidate()


def compute(qtbot, p: StatsPanel, field: str, scope: str = "all", **kw):
    p.set_field_text(field)
    p.set_scope(scope)
    task = p.compute(**kw)
    assert task is not None, p.status_text()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    fs = p.result()
    assert fs is not None and not fs.partial
    return fs


def hover(chart, pos: QPoint) -> None:
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), QPointF(chart.mapToGlobal(pos)), Qt.MouseButton.NoButton,
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(chart, event)


# ----------------------------------------------------------------------------------
# numbers: histogram, hover, click
# ----------------------------------------------------------------------------------
def test_numeric_field_histogram_hover_and_click(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    p.show()
    qtbot.waitExposed(p)
    applied: list = []
    p.filter_applied.connect(lambda _s, text: applied.append(text))
    fs = compute(qtbot, p, "id")
    assert p.chart_visible() and p.chart_kind() == "hist" and p.chart_kinds() == ["hist", "top"]
    assert chart_kinds(fs) == ["hist", "top"] and default_chart_kind(fs, ["hist", "top"]) == "hist"
    bars = p.chart_bars()
    assert 1 < len(bars) <= HIST_BINS and sum(b.count for b in bars) == 60
    h = fs.histogram(HIST_BINS)
    assert h.exact and h.edges == list(range(0, 61, 2)) and len(bars) == 30
    share = statspanel._share_text(2, 60)
    assert bars[0].label == "0 – 1" and bars[2].label == "4 – 5" and bars[0].count == 2 and bars[0].share_text == share
    assert p.chart_title.text() == t("gui.statspanel.chart.title.hist", bins="30", count="60")
    assert p.chart_info_text() == t("gui.statspanel.chart.info.idle", count="30")
    assert p.chart.shown_bars() == 30 and all(not p.chart.bar_rect(i).isEmpty() for i in range(30))
    # hover: the info line and the tooltip name the bin, its count, share and the filter
    rect = p.chart.bar_rect(2)
    hover(p.chart, rect.center())
    assert p.chart.hover_index() == 2
    assert p.chart_info_text() == t("gui.statspanel.chart.info.hover", range="4 – 5", count="2", share=share,
                                    action="id >= 4 and id <= 5")
    assert p.chart.accessibleDescription() == p.chart_info_text()
    assert p.chart.tip_at(2) == t("gui.statspanel.chart.bar.tip", range="4 – 5", count="2", share=share) + \
        "\nid >= 4 and id <= 5"
    assert p.chart.tip_at(-1) == t("gui.statspanel.chart.tip.click")
    help_event = QHelpEvent(QEvent.Type.ToolTip, rect.center(), p.chart.mapToGlobal(rect.center()))
    assert QApplication.sendEvent(p.chart, help_event)
    hover(p.chart, QPoint(1, 1))
    assert p.chart.hover_index() == -1 and p.chart_info_text() == t("gui.statspanel.chart.info.idle", count="30")
    # click: the quick filter of the field with the bin's range
    qtbot.mouseClick(p.chart, Qt.MouseButton.LeftButton, pos=rect.center())
    text = "id >= 4 and id <= 5"
    assert applied[-1] == text and count_matching(s, text) == 2
    wait_view(qtbot, s, 2)
    assert s.quick_filters == {"id": text}
    assert p.activate_bar(29)                                             # another bin replaces it (keyed by the field)
    wait_view(qtbot, s, 2)
    assert s.quick_filters == {"id": "id >= 58 and id <= 59"}
    reset(qtbot, s)
    # exact tokens in the ranges: the score field spans -0.0 .. 1e400
    fs = compute(qtbot, p, "score")
    bars = p.chart_bars()
    assert bars[0].filters[0].text == "score >= 0 and score < 5E+398" and bars[-1].filters[0].text.endswith("<= 1E+400")
    assert bars[0].count == 48 and bars[-1].count == 12 and sum(b.count for b in bars) == 60
    assert p.activate_bar(len(bars) - 1)
    wait_view(qtbot, s, 12)
    assert count_matching(s, bars[-1].filters[0].text) == 12
    reset(qtbot, s)
    assert p.activate_bar(0)
    wait_view(qtbot, s, 48)


def test_string_field_length_histogram(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "note")
    assert p.chart_kind() == "lens" and p.chart_kinds() == ["lens", "top"]
    bars = p.chart_bars()
    assert [b.label for b in bars] == [t("gui.statspanel.chart.len.label", range=str(n)) for n in range(4)]
    assert [b.count for b in bars] == [10, 10, 10, 10]
    assert p.chart_title.text() == t("gui.statspanel.chart.title.lens", bins="4", count="40")
    assert bars[2].filters[0].text == "len(note) == 2"
    assert p.activate_bar(2)
    wait_view(qtbot, s, 10)
    assert s.quick_filters == {"note": "len(note) == 2"} and count_matching(s, "len(note) == 2") == 10
    reset(qtbot, s)
    # a multi-valued field: len() reads the first value only -> no length filters, the reason is flashed
    compute(qtbot, p, "tags[*]")
    assert p.chart_kind() == "lens"
    bars = p.chart_bars()
    assert bars and all(not b.filters and b.why == t("gui.statspanel.why.multi_len") for b in bars)
    assert p.activate_bar(0) is False and p.app.statuses[-1] == t("gui.statspanel.why.multi_len")
    assert t("gui.statspanel.why.multi_len") in p.chart.tip_at(0)


def test_mixed_field_type_guard_kind_switch_and_preference(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "mix")                                        # string / number / array, 20 each
    assert p.chart_kinds() == ["hist", "lens", "top"]
    assert p.chart_kind() == "hist"                                      # ties: no leading number / string type
    bars = p.chart_bars()
    assert len(bars) == 1 and bars[0].label == "42" and bars[0].filters[0].text == "mix == 42" and bars[0].count == 20
    p.set_chart_kind("lens")
    bars = p.chart_bars()
    assert p.chart_kind() == "lens" and len(bars) == 1
    assert bars[0].filters[0].text == 'len(mix) == 3 and type(mix) == "string"'
    assert p.activate_bar(0)
    wait_view(qtbot, s, 20)
    assert count_matching(s, bars[0].filters[0].text) == 20
    reset(qtbot, s)
    p.set_chart_kind("top")
    bars = p.chart_bars()
    assert p.chart_kind() == "top" and [b.label for b in bars] == ['"abc"', "42", "[1,2,3]"]
    assert bars[1].filters[0].text == "mix == 42" and bars[0].share_text == statspanel._share_text(20, 60)
    assert p.chart_title.text() == t("gui.statspanel.chart.title.top", count="3")
    assert p.activate_bar(0)
    wait_view(qtbot, s, 20)
    assert s.filter is not None and s.filter.text == 'mix == "abc"'      # a top value: the builder condition
    reset(qtbot, s)
    compute(qtbot, p, "id")                                              # the user's kind is kept while offered
    assert p.chart_kind() == "top" and p.chart_bars()[0].label == "0"
    compute(qtbot, p, "note")
    assert p.chart_kind() == "top"
    p.set_chart_kind("lens")
    compute(qtbot, p, "id")                                              # not offered: the default again
    assert p.chart_kind() == "hist"
    with pytest.raises(ValueError):
        p.set_chart_kind("lens")
    with pytest.raises(ValueError):
        chart_data(fs, "pie")


def test_chart_hides_without_values_and_without_a_result(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    assert not p.chart_visible() and p.chart_kind() is None and p.chart_bars() == []
    compute(qtbot, p, "missing.x")
    assert not p.chart_visible() and p.chart_kinds() == [] and p.chart_info_text() == ""
    compute(qtbot, p, "id")
    assert p.chart_visible()
    p.set_session(None)
    assert not p.chart_visible() and p.chart_kind() is None
    p.set_session(s)
    assert p.chart_visible() and p.chart_kind() == "hist"


def test_single_document_bars_offer_no_filter(qtbot, opened, panel, tmp_path: Path) -> None:
    path = tmp_path / "single.json"
    path.write_text('{"b": "x", "a": [5, 7, 7, 1.10]}', encoding="utf-8")
    s = opened(path, format="single")
    p = panel()
    p.set_session(s)
    compute(qtbot, p, "a[*]", scope="view")
    assert p.chart_kind() == "hist"
    bars = p.chart_bars()
    assert sum(b.count for b in bars) == 4 and all(not b.filters and b.why == t("gui.statspanel.why.single") for b in bars)
    assert p.activate_bar(0) is False


def test_sampled_histogram_is_marked(qtbot, opened, panel, tmp_path: Path) -> None:
    path = tmp_path / "many.jsonl"
    path.write_text("\n".join('{"v": %d}' % ((i * 7919) % 100_003) for i in range(3000)) + "\n", encoding="utf-8")
    s = opened(path)
    p = panel()
    p.set_session(s)
    p.memory_limit = 1500                                                # k = HIST_MIN < 3 000 distinct numbers
    fs = compute(qtbot, p, "v")
    h = fs.histogram(HIST_BINS)
    assert not h.exact and h.sample_size == stats.HIST_MIN
    total, sample = statspanel._count(3000), statspanel._count(stats.HIST_MIN)
    assert p.chart_title.text().startswith("≈")
    assert p.chart_title.text() == t("gui.statspanel.chart.title.hist_sampled", bins=str(h.bins), count=total,
                                     sample=sample)
    assert t("gui.statspanel.chart.tip.sampled", sample=sample, count=total) in p.chart_title.toolTip()
    bars = p.chart_bars()
    assert all(b.approx and b.count_text.startswith("≈") for b in bars) and abs(sum(b.count for b in bars) - 3000) <= len(bars)
    assert p.chart.tip_at(-1) == p.chart_title.toolTip()


# ----------------------------------------------------------------------------------
# live snapshots
# ----------------------------------------------------------------------------------
@pytest.fixture
def gated(monkeypatch):
    gate = threading.Event()
    first = threading.Event()
    real = stats.field_stats

    def fake(doc, fields, source=None, **kw):
        on_partial = kw["on_partial"]

        def partial(snaps):
            on_partial(snaps)
            if not first.is_set():
                first.set()
                gate.wait(20)

        kw["on_partial"] = partial
        return real(doc, fields, source, **kw)

    monkeypatch.setattr(statspanel, "field_stats", fake)
    monkeypatch.setattr(stats, "PARTIAL_SECONDS", 0.0)
    monkeypatch.setattr(stats, "CHUNK_RECORDS", 8)
    yield gate, first
    gate.set()


def test_live_snapshot_shows_the_chart(qtbot, opened, panel, records_path: Path, gated) -> None:
    gate, first = gated
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    p.set_field_text("id")
    p.set_scope("all")
    assert p.compute() is not None
    qtbot.waitUntil(lambda: p.result() is not None, timeout=TIMEOUT)
    fs = p.result()
    assert fs.partial and fs.scanned < 60 and p.chart_visible() and p.chart_kind() == "hist"
    assert sum(b.count for b in p.chart_bars()) == fs.scanned         # the dict folded into the live histogram
    gate.set()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    assert sum(b.count for b in p.chart_bars()) == 60


# ----------------------------------------------------------------------------------
# themes and texts
# ----------------------------------------------------------------------------------
def test_dark_theme_paints_with_role_colours(qtbot, qapp, opened, panel, records_path: Path, themed) -> None:
    themed("dark")
    dark = theme.colors("dark")
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    p.show()
    qtbot.waitExposed(p)
    compute(qtbot, p, "score")                                           # bins of unequal height
    bars = p.chart_bars()
    tall, short = 0, len(bars) - 1
    assert bars[tall].count == 48 and bars[short].count == 12
    p.chart.set_hover(short)
    image = p.chart.grab().toImage()
    rect = p.chart.bar_rect(short)
    assert image.pixelColor(rect.center().x(), rect.bottom() - 2).name() == dark["accent"].name()   # the bar
    assert image.pixelColor(rect.center().x(), rect.top() + 2).name() == dark["hover"].name()       # hovered column
    assert image.pixelColor(1, 1).name() == dark["panel"].name()                                    # background
    rect = p.chart.bar_rect(tall)
    assert image.pixelColor(rect.center().x(), rect.top() + 2).name() == dark["accent"].name()
    assert image.pixelColor(rect.center().x(), rect.bottom() + 1).name() == dark["line"].name()     # baseline
    themed("light")
    light = theme.colors("light")
    image = p.chart.grab().toImage()
    rect = p.chart.bar_rect(short)
    assert image.pixelColor(rect.center().x(), rect.bottom() - 2).name() == light["accent"].name()
    assert image.pixelColor(1, 1).name() == light["panel"].name()
    assert p.chart_info.styleSheet() == f"color: {light['muted'].name()};"
    p.set_chart_kind("top")                                              # the horizontal chart paints too
    image = p.chart.grab().toImage()
    assert not image.isNull() and p.chart.shown_bars() >= 1
    assert scan_source(Path(statspanel.__file__).read_text(encoding="utf-8"), "jsonreshape/gui/statspanel.py") == []


def test_uzbek_texts(qtbot, opened, panel, records_path: Path, lang_uz) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    compute(qtbot, p, "id")
    combo = p.chart_kind_combo
    assert [combo.itemText(i) for i in range(combo.count())] == ["Sonlar taqsimoti", "Eng ko'p uchragan qiymatlar"]
    assert p.chart_title.text().startswith("Sonlar taqsimoti: 30 ta oraliq, 60 ta son")
    assert p.chart_info_text() == "30 ta ustun · ustun bosilsa filtr qo'yiladi"
    assert combo.toolTip().startswith("Nimani chizish") and p.chart.accessibleName() == "Diagramma"
    compute(qtbot, p, "note")
    assert combo.itemText(0) == "Satr uzunliklari" and p.chart_bars()[1].label == "1 belgi"
