"""StatsPanel (docs/ARCHITECTURE.md §14.9; scratchpad s5/map_stats.md §5.1-§5.3) with real DocSession objects.

Field list + completer (``$key`` for keyed documents), scope current view vs all records, compute / cancel /
exact, live partial results, the rows (presence, types, distinct, numbers as exact texts, string lengths, top
values with the records of multi-valued fields), every click -> filter condition against a brute-force count,
"Daraxtda ko'rsatish" (concrete paths, wildcards resolved by a silent task, ``..``, ``$key``, single documents),
the stale marker after an edit / a view change / a reopen, the duplicates button, keyboard activation and the
Uzbek texts. Both core backends where the statistics engine runs.
"""

from __future__ import annotations

import threading
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, Context, Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from jsonreshape import stats
from jsonreshape.gui import statspanel, theme
from jsonreshape.gui.dupesdialog import DupesDialog
from jsonreshape.gui.session import FilterSpec, NodeRef
from jsonreshape.gui.statspanel import StatsPanel, field_for_path, locate_value
from jsonreshape.i18n import t
from jsonreshape.paths import MISSING, Wild
from stats_gui_helpers import (  # noqa: F401 - fixtures
    CITIES,
    KIND_CASES,
    SCORES,
    TIMEOUT,
    app,
    backend,
    count_matching,
    opened,
    reset,
    wait_view,
    write_keyed,
    write_kind,
    write_records,
)

pytestmark = pytest.mark.gui

EXACT = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)


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


@pytest.fixture
def records_path(tmp_path: Path) -> Path:
    return write_records(tmp_path / "records.json")


def compute(qtbot, p: StatsPanel, field: str, scope: str = "all", **kw):
    p.set_field_text(field)
    p.set_scope(scope)
    task = p.compute(**kw)
    assert task is not None, p.status_text()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    fs = p.result()
    assert fs is not None and not fs.partial
    return fs


def top_index(p: StatsPanel, text: str) -> int:
    for i in range(p.top_count()):
        if p.top_value(i).text == text:
            return i
    raise AssertionError(f"{text!r} not among {[p.top_value(i).text for i in range(p.top_count())]}")


# ----------------------------------------------------------------------------------
# field list, scope, compute
# ----------------------------------------------------------------------------------
def test_field_list_completer_and_key_field(qtbot, opened, panel, records_path: Path, tmp_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    assert p.field_items() == []                                # a hidden panel asks for no schema
    p.show()
    qtbot.waitUntil(lambda: "city" in p.field_items(), timeout=TIMEOUT)
    items = p.field_items()
    assert {"id", "score", "note", "orders", "orders[*].sum", "tags[*]", "mix"} <= set(items)
    assert "$key" not in items
    assert p.field_model.stringList() == items
    p.completer.setCompletionPrefix("sum")                     # "contains" matching, any case
    found = [p.completer.completionModel().index(i, 0).data() for i in range(p.completer.completionCount())]
    assert found == ["orders[*].sum"]
    p.set_field_text("city")
    k = opened(write_keyed(tmp_path / "keyed.json"))
    p.set_session(k)
    qtbot.waitUntil(lambda: "$key" in p.field_items(), timeout=TIMEOUT)
    assert p.field_items()[0] == "$key" and "grp" in p.field_items()
    assert p.field_text() == ""                                 # inputs are kept per session
    p.set_field_text("$key")
    p.set_session(s)
    assert p.field_text() == "city"
    p.set_session(k)
    assert p.field_text() == "$key"


def test_invalid_field_and_not_ready(qtbot, opened, panel, records_path: Path) -> None:
    p = panel()
    assert p.compute() is None and p.status_text() == t("gui.statspanel.status.no_document")
    s = opened(records_path)
    p.set_session(s)
    p.set_field_text("orders[")
    assert p.field_error.isVisibleTo(p) and p.field_error.text()
    assert not p.compute_button.isEnabled()
    assert p.compute() is None
    p.set_field_text("")
    assert p.compute() is None and p.status_text() == t("gui.statspanel.field.empty")
    p.set_field_text("city")
    assert not p.field_error.isVisibleTo(p) and p.compute_button.isEnabled()


def test_scope_current_view_vs_all_records(qtbot, opened, panel, records_path: Path, backend) -> None:
    s = opened(records_path)
    s.set_filter(FilterSpec('city == "Toshkent"'))
    wait_view(qtbot, s, 15)
    p = panel()
    p.set_session(s)
    assert p.scope() == "view"                                    # docs §14.0: the current view by default
    assert p.scope_combo.itemText(0) == t("gui.statspanel.scope.view", count="15", label=s.view_label())
    assert p.scope_combo.itemText(1) == t("gui.statspanel.scope.all", count="60")
    fs = compute(qtbot, p, "city", "view")
    assert fs.scanned == 15 and fs.distinct == 1 and fs.top[0].text == '"Toshkent"' and fs.top[0].count == 15
    assert t("gui.statspanel.header.scope", label=p.scope_combo.itemText(0)) in p.header_label.text()
    fs = compute(qtbot, p, "city", "all")
    assert fs.scanned == 60 and fs.distinct == 4 and fs.distinct_exact
    assert sorted(v.text for v in fs.top) == sorted(f'"{c}"' for c in CITIES)
    assert all(v.count == 15 for v in fs.top)
    assert p.summary_text("distinct") == t("gui.statspanel.distinct.exact", count="4")
    assert not p.exact_button.isEnabled()


def test_rows_exact_numbers(qtbot, opened, panel, records_path: Path, backend) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "score")
    kinds = p.summary_kinds()
    assert kinds[:4] == ["present", "null", "missing", "type:number"]
    assert {"distinct", "numbers", "min", "max", "mean", "sum"} <= set(kinds)
    assert p.summary_text("present") == "60" and p.summary_text("missing") == "0" and p.summary_text("null") == "0"
    assert p.summary_share("present") == "100%"
    nm = fs.number
    assert (p.summary_text("min"), p.summary_text("max")) == ("-0.0", "1e400")    # token texts, never floats
    assert p.summary_text("sum") == nm.sum_text and p.summary_text("mean") == nm.mean_text
    total = Decimal(0)
    for tok in SCORES:
        total = EXACT.add(total, Decimal(tok))
    expected = EXACT.multiply(total, Decimal(12))
    assert Decimal(nm.sum_text) == expected                                           # exact sum
    assert p.summary_text("numbers") == t("gui.statspanel.value.numbers", count="60", integers="36")
    texts = sorted(p.top_value(i).text for i in range(p.top_count()))
    assert texts == sorted(SCORES)                                                    # first spellings
    assert all(p.top_value(i).count == 12 for i in range(p.top_count()))
    assert p.top_table.item(top_index(p, "12345678901234567890"), 1).text() == "12345678901234567890"
    assert p.top_table.item(0, 4).text() == "20%"
    assert not p.records_column_visible()
    assert p.types_bar.parts() == [("number", 60)]


def test_rows_strings_presence_and_multi_valued(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "note")
    assert (fs.present, fs.nulls, fs.missing) == (50, 10, 10)
    assert p.summary_text("present") == "50" and p.summary_text("null") == "10" and p.summary_text("missing") == "10"
    assert "type:string" in p.summary_kinds() and "type:null" in p.summary_kinds()
    assert p.summary_text("str_min") == t("gui.statspanel.value.chars", count="0")
    assert p.summary_text("str_max") == t("gui.statspanel.value.chars", count="3")
    assert p.summary_text("empty") == "10"
    fs = compute(qtbot, p, "orders[*].sum")
    assert fs.multi and fs.context == "orders[*]" and p.records_column_visible()
    assert t("gui.statspanel.header.context", context="orders[*]", count="60") in p.header_label.text()
    assert p.summary_text("values") == "60"
    assert [p.top_value(i).text for i in range(3)] == ["1.10", "7", "44.83"]       # ties: first occurrence
    assert [(p.top_value(i).count, p.top_value(i).records) for i in range(3)] == [(20, 20)] * 3
    assert p.top_table.item(0, 3).text() == "20"
    fs = compute(qtbot, p, "tags[*]")
    assert fs.multi and fs.values == 90
    assert p.summary_filters("str_min") == [] and p.summary_filters("str_max") == []   # len() reads one value
    assert p.top_value(0).count == 30 and p.top_value(0).records == 30


# ----------------------------------------------------------------------------------
# click -> filter
# ----------------------------------------------------------------------------------
def test_every_click_filter_selects_the_right_records(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    applied: list = []
    p.filter_applied.connect(lambda _s, text: applied.append(text))

    def check(text: str, quick: dict | None = None) -> None:
        expected = count_matching(s, text)
        wait_view(qtbot, s, expected)
        if quick is None:
            assert s.filter is not None and s.filter.text == text, (s.filter, text)
        else:
            assert s.quick_filters == quick
        assert applied[-1] == text
        reset(qtbot, s)

    compute(qtbot, p, "city")
    i = top_index(p, '"Toshkent"')
    eq, ne = p.top_filters(i)
    assert (eq.text, ne.text) == ('city == "Toshkent"', 'city != "Toshkent"')
    assert p.activate_top(i)
    check('city == "Toshkent"')
    assert s.filter is None
    assert p.activate_top(i, negate=True)
    check('city != "Toshkent"')
    i = top_index(p, '"Ўзбекистон"')
    assert p.activate_top(i)
    check('city == "Ўзбекистон"')

    compute(qtbot, p, "note")
    for kind, text in (("present", "exists(note)"), ("null", "note is null"), ("missing", "missing(note)"),
                       ("str_max", "len(note) == 3"), ("empty", 'note == ""')):
        assert p.activate_summary(kind), kind
        check(text)
    assert p.activate_summary("type:string")
    check('type(note) == "string"', {"note": 'type(note) == "string"'})
    assert count_matching(s, 'type(note) == "string"') == 40

    compute(qtbot, p, "score")
    assert p.activate_summary("min")
    check("score == -0.0")
    assert count_matching(s, "score == -0.0") == 12
    assert p.activate_summary("max")
    check("score == 1e400")
    i = top_index(p, "12345678901234567890")
    assert p.activate_top(i)
    check("score == 12345678901234567890")
    assert count_matching(s, "score == 12345678901234567890") == 12

    compute(qtbot, p, "orders[*].sum")
    i = top_index(p, "44.83")
    assert p.activate_top(i)
    check("orders[*].sum == 44.83")
    assert count_matching(s, "orders[*].sum == 44.83") == p.top_value(i).records == 20

    compute(qtbot, p, "mix")
    assert p.summary_filters("str_min")[0].text == 'len(mix) == 3 and type(mix) == "string"'
    assert p.activate_summary("str_min")
    check('len(mix) == 3 and type(mix) == "string"', {"mix": 'len(mix) == 3 and type(mix) == "string"'})
    assert count_matching(s, 'len(mix) == 3 and type(mix) == "string"') == 20
    assert p.activate_summary("arr_max")
    check('len(mix) == 3 and type(mix) == "array"', {"mix": 'len(mix) == 3 and type(mix) == "array"'})


def test_filters_extend_the_builder_and_follow_jmespath(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    compute(qtbot, p, "city")
    assert p.activate_top(top_index(p, '"Toshkent"'))
    wait_view(qtbot, s, 15)
    compute(qtbot, p, "note", scope="all")
    assert p.activate_summary("null")                       # AND-ed into the builder group
    text = 'city == "Toshkent" and note is null'
    wait_view(qtbot, s, count_matching(s, text))
    assert s.filter.text == text and len(s.filter.builder["items"]) == 2
    reset(qtbot, s)
    s.set_filter(FilterSpec("id < `30`", lang="jmespath"))
    wait_view(qtbot, s, 30)
    compute(qtbot, p, "city", scope="all")
    assert p.activate_top(top_index(p, '"Toshkent"'))       # JMESPath main filter: a quick filter of the field
    wait_view(qtbot, s, count_matching(s, 'id < 30 and city == "Toshkent"'))
    assert s.filter.lang == "jmespath" and s.quick_filters == {"city": 'city == "Toshkent"'}


def test_keyboard_enter_and_context_menu(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    p.show()
    qtbot.waitExposed(p)
    compute(qtbot, p, "city")
    i = top_index(p, '"Samarqand"')
    p.top_table.setFocus()
    p.top_table.setCurrentCell(i, 1)
    qtbot.keyClick(p.top_table, Qt.Key.Key_Return)
    wait_view(qtbot, s, 15)
    assert s.filter.text == 'city == "Samarqand"'
    reset(qtbot, s)
    menu = p.top_menu(i)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == [t("gui.statspanel.menu.filter", expr='city == "Samarqand"'),
                     t("gui.statspanel.menu.filter", expr='city != "Samarqand"'),
                     t("gui.statspanel.menu.reveal"), t("gui.statspanel.menu.copy")]
    menu.actions()[1].trigger()
    wait_view(qtbot, s, 45)
    reset(qtbot, s)
    menu.actions()[-1].trigger()                            # copy: the exact JSON text
    from PySide6.QtWidgets import QApplication

    assert QApplication.clipboard().text() == '"Samarqand"'
    menu = p.summary_menu("present")
    assert menu.actions()[0].text() == t("gui.statspanel.menu.filter", expr="exists(city)")


# ----------------------------------------------------------------------------------
# show in tree
# ----------------------------------------------------------------------------------
def test_reveal_concrete_wildcard_descendants_and_minmax(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    compute(qtbot, p, "city")
    i = top_index(p, '"Ўзбекистон"')
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        ref = p.reveal_top(i)
    assert ref == NodeRef(2, ("city",)) and sig.args[0] == ref and s.current == ref
    compute(qtbot, p, "orders[*].sum")
    i = top_index(p, "44.83")                                # first in record 2, element 1
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        task = p.reveal_top(i)
        assert task is not None and not isinstance(task, NodeRef)    # a silent task resolves the element
    got = sig.args[0]
    assert (got.record, got.path) == (2, ("orders", 1, "sum"))
    assert got.ipath == s.resolve(NodeRef(2, ("orders", 1, "sum"))).ipath
    compute(qtbot, p, "..sum")
    i = top_index(p, "44.83")
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        p.reveal_top(i)
    assert (sig.args[0].record, sig.args[0].path) == (2, ("orders", 1, "sum"))
    compute(qtbot, p, "score")
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        p.reveal_summary("min")
    assert sig.args[0] == NodeRef(3, ("score",))           # the first "-0.0"
    compute(qtbot, p, "tags[*]")
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        p.reveal_top(top_index(p, '"bb"'))
    assert (sig.args[0].record, sig.args[0].path) == (1, ("tags", 1))


def test_keyed_document_key_field(qtbot, opened, panel, tmp_path: Path, backend) -> None:
    s = opened(write_keyed(tmp_path / "keyed.json"))
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "$key")
    assert fs.is_key and fs.distinct == 12 and fs.top[0].text == '"k0"'
    assert p.activate_top(0)
    wait_view(qtbot, s, 1)
    assert s.filter.text == '$key == "k0"'
    reset(qtbot, s)
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        p.reveal_top(5)
    assert sig.args[0] == NodeRef(5, ())
    fs = compute(qtbot, p, "name")
    assert fs.top[0].count == 3 and p.activate_top(0)
    wait_view(qtbot, s, 3)


def test_single_document(qtbot, opened, panel, tmp_path: Path) -> None:
    path = tmp_path / "single.json"
    path.write_text('{"b": "x", "a": [5, 7, 7, 1.10]}', encoding="utf-8")
    s = opened(path, format="single")
    assert s.doc.kind == "single"
    p = panel()
    p.set_session(s)
    assert not p.scope_combo.isEnabled()
    assert p.scope_combo.itemText(0) == t("gui.statspanel.scope.single")
    fs = compute(qtbot, p, "a[*]", scope="view")
    assert fs.scanned == 1 and fs.top[0].text == "7" and fs.top[0].count == 2
    assert p.top_filters(0) == [] and p.summary_filters("present") == []
    assert p.activate_top(0) is False
    assert t("gui.statspanel.why.single") in p.top_table.item(0, 1).toolTip()
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        p.reveal_top(top_index(p, "1.10"))
    assert (sig.args[0].record, sig.args[0].path) == (None, ("a", 3))
    compute(qtbot, p, "b")
    assert p.reveal_top(0) == NodeRef(None, ("b",))


# ----------------------------------------------------------------------------------
# live results, cancel, exact
# ----------------------------------------------------------------------------------
@pytest.fixture
def gated(monkeypatch):
    """``field_stats`` of the panel holds its worker after the first live snapshot until ``gate`` is set."""
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


def test_partial_results_then_done(qtbot, opened, panel, records_path: Path, gated, backend) -> None:
    gate, first = gated
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    partials: list = []
    done: list = []
    p.partial_received.connect(lambda _s, fs: partials.append(fs))
    p.computed.connect(lambda _s, fs: done.append(fs))
    p.set_field_text("city")
    p.set_scope("all")
    assert p.compute() is not None
    qtbot.waitUntil(lambda: bool(partials), timeout=TIMEOUT)
    assert first.is_set() and p.status() == "running"
    fs = p.result()
    assert fs.partial and fs.scanned < 60
    assert p.top_title.text() == t("gui.statspanel.top.title_partial")
    assert p.status_text() == t("gui.statspanel.status.partial", done=str(fs.scanned), total="60")
    assert p.cancel_button.isVisibleTo(p) and not p.compute_button.isVisibleTo(p)
    assert p.summary_text("distinct") == t("gui.statspanel.distinct.partial", count=str(fs.distinct))
    assert p.top_count() > 0 and p.activate_top(0)           # a live value can be filtered by already
    gate.set()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    assert len(done) == 1 and not p.result().partial and p.result().scanned == 60
    wait_view(qtbot, s, 15)


def test_cancel_keeps_the_partial_result(qtbot, opened, panel, records_path: Path, gated) -> None:
    gate, first = gated
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    p.set_field_text("code")
    assert p.compute() is not None
    qtbot.waitUntil(lambda: p.result() is not None, timeout=TIMEOUT)
    p.cancel()
    gate.set()
    qtbot.waitUntil(lambda: p.status() == "cancelled", timeout=TIMEOUT)
    assert p.result() is not None and p.result().partial
    assert p.status_text() == t("gui.statspanel.status.cancelled")
    assert p.compute_button.isVisibleTo(p) and not p.exact_button.isEnabled()


def test_exact_recount_of_an_approximate_result(qtbot, opened, panel, records_path: Path, backend) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    p.memory_limit = 1500                                    # a few entries: sketches (HyperLogLog, Misra-Gries)
    fs = compute(qtbot, p, "id")
    assert not fs.distinct_exact and p.summary_text("distinct").startswith("≈")
    assert p.exact_button.isEnabled()
    p.set_field_text("")                                         # "exact" recounts the result shown
    p.set_scope("view")
    assert p.exact_button.isEnabled() and not p.compute_button.isEnabled()
    p.exact_button.click()
    assert p.field_text() == "id" and p.scope() == "all"
    qtbot.waitUntil(lambda: p.status() == "done" and p.result().distinct_exact, timeout=TIMEOUT)
    assert p.result().distinct == 60 and p.summary_text("distinct") == t("gui.statspanel.distinct.exact", count="60")
    assert not p.exact_button.isEnabled()


# ----------------------------------------------------------------------------------
# stale marker
# ----------------------------------------------------------------------------------
def test_stale_after_view_change_and_edit(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path, edit=True)
    p = panel()
    p.set_session(s)
    compute(qtbot, p, "city", scope="view")
    assert p.stale() is None and not p.stale_frame.isVisibleTo(p)
    s.set_filter(FilterSpec("id < 10"))
    wait_view(qtbot, s, 10)
    assert p.stale() == "view" and p.stale_frame.isVisibleTo(p)
    assert p.stale_label.text() == t("gui.statspanel.stale.view")
    assert isinstance(p.reveal_top(0), NodeRef)               # positions are still valid after a view change
    p.refresh_button.click()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    assert p.stale() is None and p.result().scanned == 10
    reset(qtbot, s)
    compute(qtbot, p, "city", scope="all")
    s.set_filter(FilterSpec("id < 5"))
    wait_view(qtbot, s, 5)
    assert p.stale() is None                                  # all records: a view change does not matter
    got: list = []
    assert s.edit.set_value(NodeRef(0, ("city",)), '"Xiva"', on_done=got.append) is not False
    qtbot.waitUntil(lambda: bool(got) and p.stale() == "data", timeout=TIMEOUT)
    assert p.stale_label.text() == t("gui.statspanel.stale.data")
    assert p.reveal_top(0) is None                            # record numbers may point elsewhere now
    assert t("gui.statspanel.why.stale") in [a.toolTip() for a in p.top_menu(0).actions()]
    p.refresh()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    assert p.stale() is None and '"Xiva"' in [v.text for v in p.result().top]


def test_result_computed_during_an_edit_arrives_stale(qtbot, opened, panel, records_path: Path, gated) -> None:
    gate, first = gated
    s = opened(records_path, edit=True)
    p = panel()
    p.set_session(s)
    p.set_field_text("city")
    p.set_scope("all")
    assert p.compute() is not None
    qtbot.waitUntil(first.is_set, timeout=TIMEOUT)
    got: list = []
    assert s.edit.set_value(NodeRef(1, ("city",)), '"Nukus"', on_done=got.append) is not False
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    gate.set()
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    assert p.stale() == "data"


def test_reopen_marks_stale_and_stops(qtbot, opened, panel, records_path: Path, gated) -> None:
    gate, first = gated
    s = opened(records_path)
    p = panel()
    p.show()
    p.set_session(s)
    qtbot.waitUntil(lambda: "city" in p.field_items(), timeout=TIMEOUT)
    p.set_field_text("city")
    assert p.compute() is not None
    qtbot.waitUntil(first.is_set, timeout=TIMEOUT)
    with qtbot.waitSignal(s.state_changed, timeout=TIMEOUT, check_params_cb=lambda v: v == "opening"):
        s.reopen()
    assert p.field_items() == []                                # the fields of the old document are gone
    qtbot.waitUntil(lambda: s.state == "ready", timeout=TIMEOUT)
    gate.set()
    assert p.status() == "idle" and p.status_text() == t("gui.statspanel.status.reopened")
    assert p.stale() == "data"
    qtbot.waitUntil(lambda: "city" in p.field_items(), timeout=TIMEOUT)
    compute(qtbot, p, "city")
    assert p.stale() is None


# ----------------------------------------------------------------------------------
# integration bits
# ----------------------------------------------------------------------------------
def test_duplicates_button(qtbot, opened, panel, records_path: Path) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    compute(qtbot, p, "id")
    assert not p.dupes_button.isEnabled()                    # no value repeats
    compute(qtbot, p, "orders[*].sum")
    assert not p.dupes_button.isEnabled() and p.dupes_button.toolTip() == t("gui.statspanel.why.dupes_wild")
    compute(qtbot, p, "city")
    assert p.dupes_button.isEnabled()
    calls: list = []
    p.dupes_opener = lambda session, key: calls.append((session, key))
    p.dupes_button.click()
    assert calls == [(s, "city")]
    p.dupes_opener = None
    dlg = p.open_dupes()
    try:
        assert isinstance(dlg, DupesDialog) and dlg.key_text() == "city" and dlg.scope() == "all"
        assert DupesDialog.instance(s) is dlg
    finally:
        dlg.close()


def test_show_field_from_a_node_path(qtbot, opened, panel, records_path: Path) -> None:
    assert field_for_path(("orders", 1, "sum")) == "orders[*].sum"
    assert field_for_path(()) == "$" and field_for_path(" $key ") == "$key"
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    task = p.show_field(("orders", 0, "sum"), scope="all")
    assert task is not None and p.field_text() == "orders[*].sum"
    qtbot.waitUntil(lambda: p.status() == "done", timeout=TIMEOUT)
    assert p.result().field == "orders[*].sum"
    assert p.show_field("city", compute=False) is None and p.status() == "done"


def test_session_switch_and_close(qtbot, opened, panel, records_path: Path, tmp_path: Path) -> None:
    s = opened(records_path)
    k = opened(write_keyed(tmp_path / "keyed.json"))
    p = panel()
    p.set_session(s)
    compute(qtbot, p, "city")
    p.set_session(k)
    assert p.result() is None and p.summary.rowCount() == 0
    p.set_session(s)
    assert p.result() is not None and p.result().field == "city" and p.summary.rowCount() > 0
    k.close()
    s.close()
    qtbot.waitUntil(lambda: p.session() is None, timeout=TIMEOUT)
    assert p.status_text() == t("gui.statspanel.status.no_document")


def test_theme_change_and_uzbek_texts(qtbot, opened, panel, records_path: Path, lang_uz) -> None:
    s = opened(records_path)
    p = panel()
    p.set_session(s)
    assert p.compute_button.text() == "Hisoblash" and p.exact_button.text() == "Aniq hisoblash"
    compute(qtbot, p, "note")
    labels = [p.summary.item(r, 0).text() for r in range(p.summary.rowCount())]
    assert labels[:3] == ["Mavjud", "null", "Yo'q"] and "Bo'sh satrlar" in labels
    assert p.top_title.text().startswith("Eng ko'p uchragan")
    assert "Joriy ko'rinish" in p.scope_combo.itemText(0)
    theme.notifier().theme_changed.emit()
    p.repaint()
    assert p.status_text().startswith("Tayyor")


# ----------------------------------------------------------------------------------
# locate_value (Qt-free part)
# ----------------------------------------------------------------------------------
def test_locate_value_dict_semantics_and_exact_numbers(qtbot, opened, tmp_path: Path) -> None:
    path = tmp_path / "loc.json"
    path.write_text('[{"m": {"a": 1, "b": 2.50, "a": 3}, "l": [{"x": 1e400}, {"x": -0.0}]},'
                    ' {"m": {"z": {"k": [1, 2]}}}]', encoding="utf-8")
    s = opened(path)
    doc = s.doc.pinned()
    from jsonreshape import codec

    assert locate_value(doc, 0, ("m", Wild.ANY_KEY), codec.loads("1")) is None    # the first "a" lost (dict rule)
    ref = locate_value(doc, 0, ("m", Wild.ANY_KEY), codec.loads("3"))
    assert ref.path == ("m", "a") and ref.ipath[1] == 2
    assert locate_value(doc, 0, ("m", Wild.ANY_KEY), codec.loads("2.5")).path == ("m", "b")   # exact value
    assert locate_value(doc, 0, ("l", Wild.ANY_INDEX, "x"), codec.loads("0")).path == ("l", 1, "x")
    assert locate_value(doc, 0, ("l", Wild.ANY_INDEX, "x"), codec.loads("1E+400")).path == ("l", 0, "x")
    assert locate_value(doc, 1, (Wild.DESCENDANTS, "k"), [1, 2]).path == ("m", "z", "k")
    assert locate_value(doc, 1, ("m", Wild.ANY_KEY), MISSING) is None


# ----------------------------------------------------------------------------------
# document kinds and encodings
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, opts, open_kw", KIND_CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(KIND_CASES)])
def test_document_kinds_and_encodings(qtbot, opened, panel, tmp_path: Path, fmt, opts, open_kw) -> None:
    s = opened(write_kind(tmp_path, fmt, opts), **open_kw)
    assert s.doc.kind == fmt and s.doc.record_count == 24
    p = panel()
    p.set_session(s)
    fs = compute(qtbot, p, "city")
    assert fs.distinct == 4 and all(v.count == 6 for v in fs.top)
    i = top_index(p, '"Ўзбекистон"')                       # Cyrillic, also when written as \u escapes
    assert p.activate_top(i)
    wait_view(qtbot, s, 6)
    assert s.filter.text == 'city == "Ўзбекистон"' and count_matching(s, s.filter.text) == 6
    reset(qtbot, s)
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        p.reveal_top(i)
    assert sig.args[0] == NodeRef(2, ("city",))
    compute(qtbot, p, "v")
    assert (p.summary_text("min"), p.summary_text("max")) == ("-0.0", "1e400")
    assert sorted(p.top_value(k).text for k in range(p.top_count())) == sorted(SCORES)


def test_menu_key_opens_the_menu_of_the_current_row(qtbot, opened, panel, records_path: Path) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QApplication

    s = opened(records_path)
    p = panel()
    p.set_session(s)
    p.show()
    qtbot.waitExposed(p)
    compute(qtbot, p, "city")
    i = p.top_count() - 1
    p.top_table.setFocus()
    p.top_table.setCurrentCell(i, 1)
    value = p.top_value(i).text
    key_menu = QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, QPoint(2, 2), QPoint(2, 2))
    QApplication.sendEvent(p.top_table, key_menu)
    menu = p.last_menu
    assert menu is not None and menu.actions()[0].text() == t("gui.statspanel.menu.filter", expr=f"city == {value}")
    menu.close()
    p.summary.setFocus()
    p.summary.setCurrentCell(p.summary_kinds().index("missing"), 0)
    QApplication.sendEvent(p.summary, QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, QPoint(2, 2), QPoint(2, 2)))
    assert p.last_menu.actions()[0].text() == t("gui.statspanel.menu.filter", expr="missing(city)")
    p.last_menu.close()
    p.summary.setCurrentCell(p.summary_kinds().index("distinct"), 0)
    QApplication.sendEvent(p.summary, QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, QPoint(2, 2), QPoint(2, 2)))
    assert [a.text() for a in p.last_menu.actions() if not a.isSeparator()] == [t("gui.statspanel.menu.copy")]
    p.last_menu.close()
    p.top_table.setFocus()
    qtbot.keyClick(p.top_table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == value
