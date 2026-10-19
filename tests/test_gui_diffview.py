"""DiffView (jsonreshape/gui/diffview.py; ARCHITECTURE §14.9 "Compare"): rows coloured by theme
roles, F7 / Shift+F7, "only differences", unchanged subtrees collapsed, array matching from the
toolbar (index / key with moves / unordered), numbers as written, swap, the text mode (aligned
lines, filler lines, colours, synchronized scrolling, navigation), big unordered arrays laid out
in a worker, the context menu (copy, show in tree), load errors, a superseded load, the theme
roles in light and dark, Uzbek texts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape import codec  # noqa: E402
from jsonreshape.diff import DiffOptions  # noqa: E402
from jsonreshape.errors import UserInputError  # noqa: E402
from jsonreshape.gui import difftree, theme  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.diffview import DiffView, PairSource, SideText, ValuePairSource, align_lines  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000
K = Qt.Key
M = Qt.KeyboardModifier


def J(text: str) -> Any:
    return codec.loads(text)


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.statuses = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    assert ctx.tasks.shutdown(30)
    assert not ctx.errors, ctx.errors


@pytest.fixture
def themed(qapp):
    palette = QPalette(qapp.palette())
    style = qapp.style().name()
    yield qapp
    theme._reset_for_tests()
    qapp.styleHints().unsetColorScheme()
    qapp.setStyle(style)
    qapp.setPalette(palette)
    theme.invalidate()


@pytest.fixture
def make_view(qtbot, app):
    made: list[DiffView] = []

    def make(**kw: Any) -> DiffView:
        v = DiffView(app, **kw)
        qtbot.addWidget(v)
        v.resize(1000, 600)
        v.show()
        qtbot.waitExposed(v)
        made.append(v)
        return v

    yield make
    for v in made:
        v.shutdown()


def load(qtbot, view: DiffView, source: PairSource | None, **kw: Any) -> DiffView:
    view.set_source(source, **kw)
    qtbot.waitUntil(lambda: not view.is_loading(), timeout=TIMEOUT)
    return view


def wait_loaded(qtbot, view: DiffView) -> None:
    qtbot.waitUntil(lambda: not view.is_loading() and view.model() is not None, timeout=TIMEOUT)


def labels(view: DiffView) -> list[tuple[str, str]]:
    return [(r.key_label, r.status) for r in view.model().iter_rows()]


A = J('{"id": 7, "name": "Ali", "fax": "123", "tags": ["a", "b"], "o": {"x": 1, "y": 2}, "n": 1.10}')
B = J('{"id": 7, "name": "Vali", "tags": ["b"], "o": {"x": 1, "y": 2}, "n": 1.1, "new": true}')


# ----------------------------------------------------------------------------- theme roles
@pytest.mark.parametrize("scheme", theme.SCHEMES)
def test_diff_theme_roles_are_readable(scheme) -> None:
    from test_gui_theme import contrast

    c = theme.colors(scheme)
    roles = ("diff_added", "diff_removed", "diff_changed", "diff_moved")
    assert set(roles) <= set(theme.ROLES)
    tints = [c[r] for r in roles]
    assert len({x.name() for x in tints}) == 4
    for role in roles:
        tint = c[role]
        assert tint != c["panel"] and tint != c["selection"]
        assert contrast(c["fg"], tint) >= 7, role
        for kind in ("key", "string", "number", "bool", "object", "array", "muted"):
            assert contrast(c[kind], tint) >= 4.0, (role, kind)
        assert contrast(c["null"], tint) >= 3.0, role
        # the tint stays a tint: close to the panel lightness, clearly not the text colour
        assert abs(tint.lightness() - c["panel"].lightness()) < 60, role


# ----------------------------------------------------------------------------- rows and colours
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_rows_are_tinted_by_status(qtbot, make_view, themed, scheme) -> None:
    theme.apply_theme(themed, scheme)
    view = load(qtbot, make_view(), ValuePairSource(A, B, a_name="a.json", b_name="b.json"))
    m = view.model()
    rows = {r.key_label: r for r in m.iter_rows()}
    assert (rows["name"].role, rows["fax"].role, rows["new"].role) == ("diff_changed", "diff_removed", "diff_added")
    assert rows["id"].role is None and rows["o"].role is None
    view.grid.set_current(-1)
    view.grid.clearFocus()
    QApplication.processEvents()
    image = view.grid.viewport().grab().toImage()
    for label, role in (("name", "diff_changed"), ("fax", "diff_removed"), ("new", "diff_added")):
        rect = view.grid.cell_rect(rows[label].row, 2)
        assert image.pixelColor(rect.right() - 2, rect.center().y()) == theme.color(role), label
    # the gutter repeats the status (colour is not the only cue)
    src = view.grid.source()
    assert [src.row_label(rows[k].row) for k in ("name", "fax", "new", "id")] == ["~", "−", "+", None]
    assert src.row_label(0) == "•"


def test_numbers_and_strings_are_shown_exactly(qtbot, make_view) -> None:
    a = J('{"big": 12345678901234567890, "e": 1e400, "z": -0.0, "s": "Oʻzbekiston \\u0027a\\nb"}')
    b = J('{"big": 12345678901234567891, "e": 1e400, "z": 0, "s": "Oʻzbekiston \\u0027a\\nb"}')
    view = load(qtbot, make_view(), ValuePairSource(a, b))
    rows = {r.key_label: r for r in view.model().iter_rows()}
    assert (rows["big"].a_text, rows["big"].b_text, rows["big"].status) == (
        "12345678901234567890", "12345678901234567891", "changed")
    assert rows["big"].a_spans == ((19, 20),)
    assert (rows["e"].status, rows["z"].status) == ("equal", "equal")     # -0 = 0
    assert rows["z"].a_text == "-0.0" and rows["z"].b_text == "0"
    assert rows["s"].a_text == '"Oʻzbekiston \'a\\nb"'
    cell = view.grid.source().cell(rows["big"].row, view.grid.columns()[1])
    assert cell.spans == ((19, 20),) and cell.role == "number"


def test_next_prev_with_f7_keys(qtbot, make_view) -> None:
    view = load(qtbot, make_view(), ValuePairSource(A, B))
    m = view.model()
    first = view.current_row()
    assert first is not None and first.key_label == "name"       # the first difference is selected
    view.grid.setFocus()
    qtbot.waitUntil(view.grid.hasFocus, timeout=3000)
    seen = [view.current_row().path]
    for _ in range(4):
        QTest.keyClick(view.grid, K.Key_F7)
        seen.append(view.current_row().path)
    assert seen == [("name",), ("fax",), ("tags", 0), ("new",), ("new",)]     # stops at the last one
    assert any("farq yo'q" in s.lower() for s in view.app.statuses)
    QTest.keyClick(view.grid, K.Key_F7, M.ShiftModifier)
    assert view.current_row().path == ("tags", 0)
    view.prev_button.click()
    assert view.current_row().path == ("fax",)
    view.next_button.click()
    assert view.current_row().path == ("tags", 0)
    assert m is view.model()


def test_unchanged_subtrees_are_collapsed_and_expandable(qtbot, make_view) -> None:
    view = load(qtbot, make_view(), ValuePairSource(A, B))
    m = view.model()
    o = next(r for r in m.iter_rows() if r.key_label == "o")
    assert o.status == "equal" and o.expander == 1 and o.summary
    assert o.a_text == "{…} 2 ta maydon, o'zgarishsiz"
    cell = view.grid.source().cell(o.row, view.grid.columns()[1])
    assert cell.italic and cell.role == "muted"
    view.grid.set_current(o.row, 0)
    view.grid.setFocus()
    QTest.keyClick(view.grid, K.Key_Right)
    assert m.row(o.row).expander == 2
    assert [r.key_label for r in m.iter_rows(o.row + 1, o.row + 3)] == ["x", "y"]
    QTest.keyClick(view.grid, K.Key_Left)
    assert m.row(o.row).expander == 1
    QTest.keyClick(view.grid, K.Key_Return)
    assert m.row(o.row).expander == 2
    view.collapse_all()
    assert [r.key_label for r in m.iter_rows()] == ["$", "id", "name", "fax", "tags", "o", "n", "new"]
    view.expand_all()
    assert "[1 → 0]" in [r.key_label for r in m.iter_rows()]


def test_only_differences(qtbot, make_view, lang_en) -> None:
    a = {"items": [{"id": i, "v": i} for i in range(30)], "meta": {"x": 1}}
    b = codec.loads(codec.dumps(a))
    b["items"][12]["v"] = -1
    view = load(qtbot, make_view(), ValuePairSource(a, b))
    full = view.model().row_count()
    view.only_check.setChecked(True)
    got = labels(view)
    assert got == [("$", "contains"), ("items", "contains"), ("… 12 unchanged", "gap"), ("[12]", "contains"),
                   ("… 1 unchanged", "gap"), ("v", "changed"), ("… 17 unchanged", "gap"), ("… 1 unchanged", "gap")]
    assert view.current_row().key_label == "v"                  # the selection stays on its node
    gap = view.model().row(2)
    view.toggle(gap.row)                                         # opening a gap shows everything again
    assert not view.only_check.isChecked() and view.model().row_count() == full


def test_array_matching_from_the_toolbar(qtbot, make_view) -> None:
    a = J('{"list": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]}')
    b = J('{"list": [{"id": 3, "v": "c"}, {"id": 1, "v": "a"}, {"id": 2, "v": "B"}]}')
    view = load(qtbot, make_view(), ValuePairSource(a, b))
    auto = labels(view)
    assert ("[2]", "removed") in auto and ("[0]", "added") in auto       # content alignment: 3 moved = -/+

    def choose(mode: str) -> None:
        i = view.match_combo.findData(mode)
        view.match_combo.setCurrentIndex(i)
        view.match_combo.activated.emit(i)

    choose("index")
    wait_loaded(qtbot, view)
    assert view.options().array_match == "index"
    elements = [(r.key_label, r.status) for r in view.model().iter_rows() if r.depth == 2]
    assert elements == [("[0]", "contains"), ("[1]", "contains"), ("[2]", "contains")]
    choose("key")                                                 # needs a field: "id" is suggested
    assert view.key_combo.isVisible()
    assert view.key_combo.currentText() == "id" and "v" in [view.key_combo.itemText(i)
                                                            for i in range(view.key_combo.count())]
    view.key_combo.lineEdit().returnPressed.emit()
    wait_loaded(qtbot, view)
    got = labels(view)
    assert view.options().array_match == "key"
    assert ("[2 → 0]", "moved") in got and ("[1 → 2]", "contains") in got
    choose("unordered")
    wait_loaded(qtbot, view)
    got = labels(view)
    assert ("[1 → 2]", "contains") not in got and ("[2 → 0]", "equal") in got


def test_numbers_as_written(qtbot, make_view) -> None:
    a = J('{"price": 1.10, "n": [1, 2.0]}')
    b = J('{"price": 1.1, "n": [1.0, 2]}')
    view = load(qtbot, make_view(), ValuePairSource(a, b))
    assert view.model().equal and "Farq yo'q" in view.message()
    view.numbers_check.setChecked(True)
    wait_loaded(qtbot, view)
    rows = {r.key_label: r for r in view.model().iter_rows()}
    assert rows["price"].status == "changed" and rows["price"].a_spans == ((3, 4),)
    assert view.options().numbers == "text"


def test_swap(qtbot, make_view) -> None:
    view = load(qtbot, make_view(), ValuePairSource(A, B, a_name="a.json", b_name="b.json"))
    assert [c.title for c in view.grid.columns()] == ["Kalit", "A: a.json", "B: b.json"]
    view.swap_button.click()
    wait_loaded(qtbot, view)
    assert view.swapped()
    rows = {r.key_label: r.status for r in view.model().iter_rows()}
    assert rows["fax"] == "added" and rows["new"] == "removed"
    assert [c.title for c in view.grid.columns()] == ["Kalit", "A: b.json", "B: a.json"]


def test_text_mode(qtbot, make_view, themed) -> None:
    theme.apply_theme(themed, "light")
    a = {"items": [{"id": i, "name": f"n{i}"} for i in range(40)]}
    b = codec.loads(codec.dumps(a))
    b["items"][5]["name"] = "changed"
    del b["items"][20]
    b["items"].append({"id": 99, "name": "new"})
    view = load(qtbot, make_view(), ValuePairSource(a, b))
    view.text_button.click()
    assert view.text_mode()
    qtbot.waitUntil(view.text_ready, timeout=TIMEOUT)
    st = view.line_statuses()
    ta, tb = view.text_editors()
    assert ta.document().blockCount() == tb.document().blockCount() == len(st)
    assert st.count("changed") == 1 and st.count("removed") >= 4 and st.count("added") >= 4
    i = st.index("changed")
    assert "n5" in ta.document().findBlockByNumber(i).text()
    assert "changed" in tb.document().findBlockByNumber(i).text()
    assert ta.document().findBlockByNumber(i).blockFormat().background().color() == theme.color("diff_changed")
    r = st.index("removed")
    assert tb.document().findBlockByNumber(r).text() == ""                  # a filler line on B
    assert tb.document().findBlockByNumber(r).blockFormat().background().style() == Qt.BrushStyle.BDiagPattern
    assert "lines differ" in view.text_status.full_text() or "qator farq qiladi" in view.text_status.full_text()
    # synchronized scrolling
    ta.verticalScrollBar().setValue(ta.verticalScrollBar().maximum())
    assert tb.verticalScrollBar().value() == ta.verticalScrollBar().value()
    # F7 in the text mode goes from run to run of differing lines
    ta.moveCursor(ta.textCursor().MoveOperation.Start)
    first = view.next_change()
    assert first == i
    second = view.next_change()
    assert second is not None and second > first and st[second] != "equal"
    assert view.prev_change() == first
    view.text_button.click()
    assert not view.text_mode()


def test_align_lines() -> None:
    la, lb, st = align_lines("a\nb\nc\nd", "a\nB\nc\nd\ne")
    assert st == ["equal", "changed", "equal", "equal", "added"]
    assert la == ["a", "b", "c", "d", ""] and lb == ["a", "B", "c", "d", "e"]
    assert align_lines("", "x") == ([""], ["x"], ["added"])


def test_big_unordered_array_is_laid_out_in_a_worker(qtbot, make_view) -> None:
    n = difftree.SYNC_UNORDERED_MAX
    a = {"x": list(range(n))}
    b = {"x": list(reversed(range(n)))}
    b["x"][0] = -1
    view = load(qtbot, make_view(), ValuePairSource(a, b), opts=DiffOptions(array_match="unordered"))
    m = view.model()
    x = next(r for r in m.iter_rows() if r.key_label == "x")
    assert x.expander == 1
    view.toggle(x.row)
    qtbot.waitUntil(lambda: m.row(x.row).expander == 2, timeout=TIMEOUT)
    assert m.row_count() == 2 + n + 1
    assert view.next_change() is not None


def test_context_menu_copy_and_reveal(qtbot, make_view) -> None:
    allowed: list[tuple[str, str]] = []

    def can(side: str, row: Any) -> bool:
        allowed.append((side, row.key_label))
        return side == "b"

    view = load(qtbot, make_view(reveal=can), ValuePairSource(A, B))
    m = view.model()
    tags = next(r for r in m.iter_rows() if r.key_label == "tags")
    menu = view.build_menu(tags.row)
    acts = {a.objectName(): a for a in menu.actions() if a.objectName()}
    assert acts["diff_copy_a"].isEnabled() and acts["diff_copy_b"].isEnabled()
    QGuiApplication.clipboard().setText("")
    acts["diff_copy_a"].trigger()
    qtbot.waitUntil(lambda: QGuiApplication.clipboard().text() != "", timeout=TIMEOUT)
    assert codec.loads(QGuiApplication.clipboard().text()) == ["a", "b"]
    acts["diff_copy_path"].trigger()
    assert QGuiApplication.clipboard().text() == "tags"
    assert not acts["diff_reveal_a"].isEnabled() and acts["diff_reveal_b"].isEnabled()
    with qtbot.waitSignal(view.reveal_requested, timeout=3000) as blocker:
        acts["diff_reveal_b"].trigger()
    assert blocker.args[0] == "b" and blocker.args[1].b_path == ("tags",)
    new = next(r for r in m.iter_rows() if r.key_label == "new")
    acts = {a.objectName(): a for a in view.build_menu(new.row).actions() if a.objectName()}
    assert not acts["diff_copy_a"].isEnabled() and not acts["diff_reveal_a"].isEnabled()


class FailingSource(PairSource):
    def load(self, opts, swapped, cancel):
        raise UserInputError("diff.error.too_many_keys", count=5, max=1)

    def texts(self, swapped, cancel):
        return SideText(""), SideText("")


def test_load_error_is_shown(qtbot, make_view) -> None:
    view = load(qtbot, make_view(), FailingSource())
    assert view.model() is None and view.error() is not None
    assert "Taqqoslab bo'lmadi" in view.message() and "a'zolar juda ko'p" in view.message()


def test_a_newer_source_supersedes_an_older_load(qtbot, make_view) -> None:
    view = make_view()
    view.set_source(ValuePairSource({"a": 1}, {"a": 2}))
    view.set_source(ValuePairSource({"b": 1}, {"b": 1}))
    wait_loaded(qtbot, view)
    QApplication.processEvents()
    assert [r.key_label for r in view.model().iter_rows()] == ["$", "b"]
    view.set_source(None)
    assert view.model() is None and view.grid.row_count() == 0


def test_uzbek_texts(qtbot, make_view, lang_uz) -> None:
    view = load(qtbot, make_view(), ValuePairSource(A, B, a_name="a.json", b_name="b.json"))
    assert view.only_check.text() == "Faqat farqlar"
    assert view.text_button.text() == "Matn"
    assert view.match_combo.itemText(view.match_combo.findData("index")) == "Indeks"
    assert view.match_combo.itemText(view.match_combo.findData("key")) == "Kalit bo'yicha"
    assert "F7" in view.next_button.toolTip() and "Shift+F7" in view.prev_button.toolTip()
    assert view.message() == "1 ta o'zgargan, 1 ta qo'shilgan, 2 ta o'chirilgan, 0 ta ko'chirilgan"
    name = next(r for r in view.model().iter_rows() if r.key_label == "name")
    assert "O'zgargan" in view.row_tooltip(name)


class ChangesOnlySource(PairSource):
    """A pair whose values are "not loaded" (as for big nodes): a changes-only model."""

    def __init__(self, a: Any, b: Any) -> None:
        self.a, self.b = a, b

    def load(self, opts, swapped, cancel):
        from jsonreshape.diff import diff_values

        return difftree.DiffTreeModel(difftree.UNKNOWN, difftree.UNKNOWN, diff_values(self.a, self.b, opts),
                                      opts=opts, a_kind="object", b_kind="object")

    def texts(self, swapped, cancel):
        return SideText(codec.dumps(self.a, indent=2).decode()), SideText(codec.dumps(self.b, indent=2).decode())


def test_changes_only_pair_forces_only_differences_for_that_pair(qtbot, make_view) -> None:
    view = make_view()
    load(qtbot, view, ChangesOnlySource(A, B))
    assert view.only_check.isChecked() and not view.only_check.isEnabled()
    assert "faqat farqli yo'llar" in view.message()
    assert [r.key_label for r in view.model().iter_rows()] == ["$", "name", "fax", "tags", "[0]", "new"]
    load(qtbot, view, ValuePairSource(A, B))         # the next (complete) pair is shown in full
    assert not view.only_check.isChecked() and view.only_check.isEnabled()
    assert "id" in [r.key_label for r in view.model().iter_rows()]
    view.only_check.setChecked(True)                 # the user's choice is kept for the next pair
    load(qtbot, view, ValuePairSource(B, A))
    assert view.only_check.isChecked() and view.model().only_changes
