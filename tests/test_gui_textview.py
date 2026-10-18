"""TextView (jsonreshape/gui/textview.py; ARCHITECTURE §11.7 "Text", AGENT_PROMPT §4.3).

Real DocSessions over generated files: byte-exact numbers and strings, indent 2 / 4 / tab
(remembered), folding (markers, nested folds, fold all / to level, placeholders), the
text cap notice and ``open_raw_requested``, the JSON Lines root notice, a JSON error
inside the node, quick find (highlights, F3 / Shift+F3, revealing folded matches),
following ``session.select`` only while visible, a node with 100 k+ lines (chunked
insertion, lazy colors), long lines, copy, wrap, zoom, keyed members and scalars; plus
the pure helpers (``scan_folds`` against a naive bracket matcher, ``line_tokens``,
``find_matches``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape.core import Reformatter  # noqa: E402
from jsonreshape.gui import textview  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.quickfind import make_matcher  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402
from jsonreshape.gui.textview import TextView, find_matches, line_tokens, scan_folds  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 20000

EXACT = (b'[{"id": 1, "n": 1.10, "big": 1e400, "neg": -0.0, "huge": 12345678901234567890, "e": 2E-7,'
         b' "esc": "\\u00e9\\n\\"q\\"", "uz": "o\xca\xbbzbek \xd0\x96\xd1\x83\xd0\xba \xf0\x9f\x98\x80",'
         b' "t": true, "f": false, "z": null, "empty": {}, "arr": [], "nested": {"a": [1, {"b": [2, 3]}]}},'
         b' {"id": 2}]')


# ----------------------------------------------------------------------------------
# fixtures / helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def make_session(app, qtbot):
    made: list[DocSession] = []

    def factory(path: Path, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        return s

    yield factory
    for s in made:
        s.close()
    app.tasks.wait_idle(10)


@pytest.fixture
def make_view(qtbot):
    views: list[TextView] = []

    def factory(session: DocSession, *, show: bool = True) -> TextView:
        v = TextView(session)
        qtbot.addWidget(v)
        v.resize(900, 500)
        views.append(v)
        if show:
            v.show()
            qtbot.waitExposed(v)
        return v

    yield factory
    for v in views:
        v.shutdown()


def write(tmp_path: Path, name: str, data: bytes | str) -> Path:
    p = tmp_path / name
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


def wait_loaded(qtbot, v: TextView) -> None:
    qtbot.waitUntil(lambda: not v.is_loading() and (v.result() is not None or v.message() is not None),
                    timeout=TIMEOUT)


def show(qtbot, v: TextView, s: DocSession, ref: NodeRef) -> None:
    s.select(ref)
    wait_loaded(qtbot, v)


def reformat(raw: bytes, indent) -> str:
    f = Reformatter(indent=indent)
    return (f.feed(raw) + f.finish()).decode("utf-8")


def visible_lines(v: TextView) -> list[int]:
    """Source lines in the editor's document (folded lines are left out of it)."""
    ed = v.editor
    qtbot_wait_idle(v)
    lines = [ed.line_of_block(i) for i in range(ed.document().blockCount())]
    assert lines == ed.shown_lines()
    return lines


def qtbot_wait_idle(v: TextView, timeout: float = 20.0) -> None:
    """Folding / insertion steps of big texts run on a timer: wait for them."""
    end = time.monotonic() + timeout
    while v.editor.busy() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.002)
    assert not v.editor.busy()


def pump(n: int = 5) -> None:
    for _ in range(n):
        QApplication.processEvents()
        time.sleep(0.003)


# ----------------------------------------------------------------------------------
# pure helpers
# ----------------------------------------------------------------------------------
def naive_folds(text: str) -> dict[int, int]:
    """Opening line -> closing line by matching brackets outside strings."""
    out: dict[int, int] = {}
    stack: list[int] = []
    line = 0
    in_str = esc = False
    for ch in text:
        if ch == "\n":
            line += 1
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(line)
        elif ch in "}]":
            o = stack.pop()
            if line > o:
                out[o] = line
    return out


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(alphabet=st.characters(blacklist_categories=("Cs",)),
                                                        max_size=6),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(
        st.text(alphabet="ab{[}]\"\\:, ", max_size=4), children, max_size=4),
    max_leaves=25)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(value=json_values, indent=st.sampled_from([2, 4, "\t"]))
def test_scan_folds_matches_brackets(value, indent) -> None:
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    text = reformat(raw, indent)
    model, balanced = scan_folds(text)
    assert balanced
    expected = naive_folds(text)
    assert {o: c for o, c, _d in model.regions()} == expected
    assert model.lines == text.count("\n") + 1
    for o, c, d in model.regions():
        assert model.open_of(c) == o and model.close_of(o) == c
        lead = len(text.split("\n")[o]) - len(text.split("\n")[o].lstrip(" \t"))
        unit = 1 if indent == "\t" else indent
        assert d == lead // unit
    # a cut text is unbalanced
    if expected:
        o = min(expected)
        cut = "\n".join(text.split("\n")[:expected[o]])
        assert not scan_folds(cut)[1]


def test_line_tokens_roles() -> None:
    line = '    "key \\"x\\"": [1.5e3, -2, "str", true, false, null, {}],'
    roles = [(line[s:e], r) for s, e, r in line_tokens(line)]
    assert ('"key \\"x\\""', "key") in roles
    assert ("1.5e3", "number") in roles and ("-2", "number") in roles
    assert ('"str"', "string") in roles
    assert ("true", "bool") in roles and ("false", "bool") in roles and ("null", "null") in roles
    assert (":", "punct") in roles and ("[", "punct") in roles
    cut = '  "long": "abcdef…'
    assert ('"abcdef…', "string") in [(cut[s:e], r) for s, e, r in line_tokens(cut)]


def test_find_matches_lines_and_columns() -> None:
    text = "abc\nxx abc abc\n\nABC"
    m = find_matches(text, make_matcher("abc"))
    assert list(zip(m.lines, m.cols, m.lens)) == [(0, 0, 3), (1, 3, 3), (1, 7, 3), (3, 0, 3)]
    capped = find_matches(text, make_matcher("abc"), cap=2)
    assert len(capped) == 2 and capped.capped


# ----------------------------------------------------------------------------------
# content
# ----------------------------------------------------------------------------------
def test_numbers_and_strings_stay_byte_exact(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "exact.json", EXACT)
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(0, ()))
    node = s.doc.record_node(0)
    raw = EXACT[node.start:node.end]
    assert v.text() == reformat(raw, 2)
    text = v.text()
    for token in ('"n": 1.10', '"big": 1e400', '"neg": -0.0', '"huge": 12345678901234567890', '"e": 2E-7',
                  '"esc": "\\u00e9\\n\\"q\\""', '"oʻzbek Жук \U0001F600"', '"empty": {}',
                  '"arr": []'):
        assert token in text, token
    assert v.path_label.full_text() == "#0"
    assert v.message() is None
    assert v.result().kind == "object" and v.result().size == len(raw)


def test_indent_switch_is_remembered(make_session, make_view, qtbot, app, tmp_path) -> None:
    p = write(tmp_path, "exact.json", EXACT)
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(0, ()))
    node = s.doc.record_node(0)
    raw = EXACT[node.start:node.end]
    v.editor.fold(v.editor.folds.opens[1])            # a fold survives the switch (same lines)
    folded = set(v.editor.folded)
    v.set_indent(4)
    wait_loaded(qtbot, v)
    assert v.text() == reformat(raw, 4)
    assert v.editor.folded == folded
    v.set_indent("\t")
    wait_loaded(qtbot, v)
    assert v.text() == reformat(raw, "\t")
    assert "\n\t\"id\": 1" in v.text()
    assert app.state.get(textview.STATE_INDENT) == "tab"
    v2 = make_view(s)
    assert v2.indent() == "\t"
    v.set_indent(2)
    wait_loaded(qtbot, v)
    assert v.text() == reformat(raw, 2) and app.state.get(textview.STATE_INDENT) == "2"


def test_folding_hides_and_shows_blocks(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "exact.json", EXACT)
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(0, ()))
    ed = v.editor
    lines = v.text().split("\n")
    folds = {o: c for o, c, _d in ed.folds.regions()}
    assert folds == naive_folds(v.text())
    nested = lines.index('  "nested": {')
    inner = lines.index('    "a": [')
    assert folds[nested] > folds[inner] > inner > nested
    all_lines = list(range(len(lines)))
    # fold the inner region, then the outer one; unfolding the outer keeps the inner folded
    assert ed.fold(inner)
    hidden_inner = set(range(inner + 1, folds[inner] + 1))
    assert set(visible_lines(v)) == set(all_lines) - hidden_inner
    assert ed.fold(nested)
    assert set(visible_lines(v)) == set(all_lines) - set(range(nested + 1, folds[nested] + 1))
    assert ed.unfold(nested)
    assert set(visible_lines(v)) == set(all_lines) - hidden_inner
    assert ed.toggle_fold(inner) and visible_lines(v) == all_lines
    # fold all keeps the outermost container open; fold to level 2; unfold all
    ed.fold_all()
    top_level = [o for o, _c, d in ed.folds.regions() if d == 1]
    assert top_level and set(top_level) <= ed.folded
    shown = visible_lines(v)
    assert 0 in shown and len(lines) - 1 in shown and inner not in shown
    ed.fold_to_level(2)
    assert nested in visible_lines(v) and inner in visible_lines(v) and inner + 1 not in visible_lines(v)
    ed.unfold_all()
    assert visible_lines(v) == all_lines and not ed.folded
    # a scalar line cannot be folded
    assert not ed.fold(1)
    # gutter click on a marker toggles; the placeholder click unfolds
    v.editor.verticalScrollBar().setValue(0)
    pump()
    rect = ed.blockBoundingGeometry(ed.document().findBlockByNumber(nested)).translated(ed.contentOffset())
    fx, fw = ed._fold_column()
    ed.gutter_clicked(QPoint(fx + fw // 2, round(rect.center().y())))
    assert ed.is_folded(nested)
    v.grab()                                      # paints the placeholder
    rects = ed.placeholder_rects()
    assert [line for _r, line in rects] == [nested]
    QTest.mouseClick(ed.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     rects[0][0].center().toPoint())
    assert not ed.is_folded(nested)
    # the fold menu actions
    names = {a.objectName() for a in v.fold_menu.actions()}
    assert {"fold_all", "unfold_all", "fold_level_1", "fold_level_5"} <= names


def test_cap_notice_and_open_raw_requested(make_session, make_view, qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(textview, "TEXT_CAP", 3000)
    rows = [{"i": i, "v": "x" * 20} for i in range(500)]
    p = write(tmp_path, "big.json", json.dumps(rows))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    r = v.result()
    assert r.truncated and r.shown_bytes <= 3000
    assert len(v.text().encode()) <= 3000 and v.text().count("\n") >= 20
    assert (json.dumps(rows, indent=2) + "\n").startswith(v.text() + "\n")    # whole lines of the beginning
    assert v.message() is not None and v.info_bar.kind() == "warning"
    root = s.doc.root()
    with qtbot.waitSignal(v.open_raw_requested, timeout=2000) as blocker:
        v.info_bar.buttons()[0].click()
    assert blocker.args == [root.start, root.size]


def test_jsonl_root_shows_a_notice(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "rows.jsonl", "".join(json.dumps({"i": i}) + "\n" for i in range(300)))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    assert v.result().virtual and v.text() == ""
    assert "300" in v.message().replace(" ", "")
    with qtbot.waitSignal(v.open_raw_requested, timeout=2000) as blocker:
        v.info_bar.buttons()[0].click()
    assert blocker.args[0] == 0 and blocker.args[1] == s.doc.data_size
    # a record of the same file is shown normally
    show(qtbot, v, s, NodeRef(42, ()))
    assert v.text() == '{\n  "i": 42\n}' and v.message() is None


@pytest.mark.parametrize("content, valid", [
    ('{"meta": {"x": 1}, "a": [1, 2', '{\n  "meta": {\n    "x": 1\n  },\n  "a": [\n    1,\n    2'),
    ('[{"a": 1}, {"b": 1 2}, {"c": 3}]', '[\n  {\n    "a": 1\n  },\n  {\n    "b": 1'),
])
def test_json_error_inside_the_node(make_session, make_view, qtbot, tmp_path, content, valid) -> None:
    p = write(tmp_path, "broken.json", content)
    s = make_session(p)
    assert s.doc.error is not None
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    assert v.text() == valid
    assert v.info_bar.kind() == "error"
    assert s.doc.error.message in v.message()
    with qtbot.waitSignal(v.open_raw_requested, timeout=2000) as blocker:
        v.info_bar.buttons()[0].click()
    assert blocker.args == [s.doc.error_data_offset, 1]
    # a complete record before the error has no error notice
    show(qtbot, v, s, NodeRef(0, ()))
    assert v.message() is None or v.info_bar.kind() != "error"


# ----------------------------------------------------------------------------------
# following the session
# ----------------------------------------------------------------------------------
def test_follows_select_only_when_active(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "rows.json", json.dumps([{"i": i} for i in range(50)]))
    s = make_session(p)
    v = make_view(s, show=False)
    s.select(NodeRef(3, ()))
    pump(10)
    assert v.shown_ref() is None and not v.is_loading() and v.text() == ""
    v.show()
    v.activate()
    wait_loaded(qtbot, v)
    assert v.shown_ref() == NodeRef(3, ()) and v.text() == '{\n  "i": 3\n}'
    s.select(NodeRef(4, ()))
    wait_loaded(qtbot, v)
    qtbot.waitUntil(lambda: v.text() == '{\n  "i": 4\n}', timeout=TIMEOUT)
    v.hide()
    s.select(NodeRef(5, ()))
    pump(10)
    assert v.text() == '{\n  "i": 4\n}'
    v.show()
    qtbot.waitUntil(lambda: v.text() == '{\n  "i": 5\n}', timeout=TIMEOUT)
    # its own selections are ignored (origin), the text view never selects
    s.select(NodeRef(6, ()), origin=v)
    pump(10)
    assert v.text() == '{\n  "i": 5\n}'
    assert s.current == NodeRef(6, ())
    s.select(None)
    qtbot.waitUntil(lambda: v.text() == "", timeout=TIMEOUT)


# ----------------------------------------------------------------------------------
# quick find
# ----------------------------------------------------------------------------------
def test_quick_find_highlights_and_moves(make_session, make_view, qtbot, tmp_path) -> None:
    rows = [{"i": i, "name": "Ali" if i % 3 == 0 else "Vali", "inner": {"who": "Ali"}} for i in range(30)]
    p = write(tmp_path, "names.json", json.dumps(rows))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    expected = v.text().count('"Ali"')
    v.quick_find()
    v.find_bar._edit.setText("ali")
    v.find_bar.flush()
    assert v.match_count() == v.text().lower().count("ali")
    assert v.match_count() > expected
    assert v.current_match() == 0
    assert v.find_bar.status_text().startswith("1")
    pump()
    v.editor.update_extras()
    match_bg = v.editor._color("match")
    current_bg = v.editor._color("current_match")
    sels = v.editor.extraSelections()
    assert any(sel.format.background().color() == current_bg for sel in sels)
    assert any(sel.format.background().color() == match_bg for sel in sels)
    assert v.editor.textCursor().selectedText().lower() == "ali"
    v.find_next()
    assert v.current_match() == 1
    v.find_next(backwards=True)
    v.find_next(backwards=True)
    assert v.current_match() == v.match_count() - 1          # wraps around
    # a match inside a folded region is revealed
    v.editor.fold_all()
    target = v.match_count() // 2
    v._goto_match(target)
    line = v._matches.lines[target]
    assert v.editor.block_of_line(line) is not None              # in the document again
    assert v.editor.textCursor().selectedText().lower() == "ali"
    v.find_bar.close_bar()
    assert v.match_count() == 0


def test_quick_find_in_a_big_text_runs_in_a_task(make_session, make_view, qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(textview, "SYNC_FIND_CHARS", 100)
    p = write(tmp_path, "names.json", json.dumps([{"i": i, "n": "needle" if i % 10 == 0 else "hay"}
                                                  for i in range(500)]))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    v.quick_find()
    v.find_bar._edit.setText("needle")
    v.find_bar.flush()
    qtbot.waitUntil(lambda: v.match_count() == 50, timeout=TIMEOUT)
    assert v.current_match() == 0


# ----------------------------------------------------------------------------------
# big texts
# ----------------------------------------------------------------------------------
def test_node_with_many_lines_loads_in_chunks(make_session, make_view, qtbot, tmp_path) -> None:
    rows = [{"i": i, "tags": ["a", "b"], "o": {"k": i}} for i in range(16000)]
    p = write(tmp_path, "many.json", json.dumps(rows))
    s = make_session(p)
    v = make_view(s)
    s.select(NodeRef(None, ()))
    qtbot.waitUntil(lambda: v.result() is not None, timeout=TIMEOUT)
    assert v.editor.loading                        # the first chunk is shown, the rest follows
    first_lines = v.editor.loaded_lines()
    assert 1000 < first_lines < 150_000
    qtbot.waitUntil(lambda: not v.is_loading(), timeout=TIMEOUT)
    lines = v.editor.document().blockCount()
    assert lines == v.result().folds.lines > 100_000
    assert v.text() == v.result().text
    # colors were applied lazily: only the painted blocks were formatted
    v.grab()
    doc = v.editor.document()
    formatted = sum(1 for i in range(0, lines, 7) if doc.findBlockByNumber(i).userState() > 0)
    assert formatted < 100
    t0 = time.monotonic()
    v.editor.fold_all()
    qtbot_wait_idle(v)
    fold_time = time.monotonic() - t0
    assert len(visible_lines_fast(v)) == 16000 + 2
    assert v.editor.document().blockCount() == 16000 + 2        # folded lines left the document
    v.editor.unfold_all()
    qtbot_wait_idle(v)
    assert fold_time < 10
    assert v.editor.document().blockCount() == lines
    assert v.editor.shown_lines()[-1] == lines - 1
    # jump far down and paint: blocks there get colors (a big document is replaced by a
    # new one when the shown lines are inserted again)
    doc = v.editor.document()
    v.editor.setTextCursor(QTextCursor(doc.findBlockByNumber(lines - 50)))
    v.editor.ensureCursorVisible()
    v.grab()
    assert doc.findBlockByNumber(lines - 50).userState() > 0


def visible_lines_fast(v: TextView) -> list[int]:
    return v.editor.shown_lines()


def test_long_lines_are_shortened_on_screen(make_session, make_view, qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(textview, "MAX_LINE_CHARS", 500)
    long = "Ж" * 3000
    p = write(tmp_path, "long.json", json.dumps([{"a": long, "b": 1}], ensure_ascii=False))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(0, ()))
    line = v.text().split("\n")[1]
    assert line.endswith("…") and len(line) == 501
    assert v.result().cut_lines == 1 and v.message() is not None
    assert v.copy_text() == json.dumps({"a": long, "b": 1}, ensure_ascii=False, indent=2)
    assert QGuiApplication.clipboard().text() == v.result().full


def test_control_characters_do_not_break_lines(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "ctrl.json", '[{"a": "x\u2028y\u2029z", "b": "tab\\there"}]'.encode("utf-8"))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(0, ()))
    assert v.editor.document().blockCount() == 4
    assert v.result().full is not None and "\u2028" in v.result().full
    assert '"tab\\there"' in v.text()


# ----------------------------------------------------------------------------------
# small features
# ----------------------------------------------------------------------------------
def test_copy_wrap_zoom_status_and_menu(make_session, make_view, qtbot, app, tmp_path) -> None:
    p = write(tmp_path, "exact.json", EXACT)
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(0, ()))
    assert v.copy_text() == v.text()
    cursor = v.editor.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(10, QTextCursor.MoveMode.KeepAnchor)
    v.editor.setTextCursor(cursor)
    assert v.copy_text() == v.text()[:10]
    QGuiApplication.clipboard().setText("")
    v.copy_value("json")
    assert QGuiApplication.clipboard().text() == v.text()[:10]
    v.set_wrap(True)
    assert v.editor.lineWrapMode() == v.editor.LineWrapMode.WidgetWidth
    assert app.state.get(textview.STATE_WRAP) is True
    v.wrap_button.click()
    assert not v.wrap() and app.state.get(textview.STATE_WRAP) is False
    size = v.font_point_size()
    v.zoom(3)
    assert v.font_point_size() == size + 3
    v.zoom(0)
    assert v.font_point_size() == size
    cursor = QTextCursor(v.editor.document().findBlockByNumber(2))
    v.editor.setTextCursor(cursor)
    assert "3" in v.status_label.text()
    menu = v.build_context_menu()
    names = {a.objectName() for a in menu.actions()}
    assert {"text_copy_selection", "text_copy_all", "text_select_all"} <= names
    assert any(a.menu() is not None and a.text() == textview.t("gui.text.menu.node") for a in menu.actions())
    menu.deleteLater()


def test_bracket_matching(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "exact.json", EXACT)
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(0, ()))
    lines = v.text().split("\n")
    nested = lines.index('  "nested": {')
    close = v.editor.folds.close_of(nested)
    block = v.editor.document().findBlockByNumber(nested)
    cursor = QTextCursor(block)
    cursor.setPosition(block.position() + len(lines[nested]))
    v.editor.setTextCursor(cursor)
    v.editor.update_extras()
    soft = v.editor._color("accent_soft")
    marks = [(sel.cursor.blockNumber(), sel.cursor.selectedText()) for sel in v.editor.extraSelections()
             if sel.format.background().color() == soft]
    assert sorted(marks) == [(nested, "{"), (close, "}")]


def test_keyed_member_and_scalar_nodes(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "keyed.json", json.dumps({f"k{i}": {"v": i, "s": "x"} for i in range(5)}))
    s = make_session(p)
    assert s.doc.member_records
    v = make_view(s)
    show(qtbot, v, s, NodeRef(2, ()))
    assert v.text() == '{\n  "v": 2,\n  "s": "x"\n}'          # the value, not the "k2": member
    show(qtbot, v, s, NodeRef(2, ("s",)))
    assert v.text() == '"x"' and v.result().kind == "string"
    show(qtbot, v, s, NodeRef(2, ("missing",)))
    assert v.info_bar.kind() == "error" and v.text() == ""


def test_shutdown_drops_late_results(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "rows.json", json.dumps([{"i": i} for i in range(100)]))
    s = make_session(p)
    v = make_view(s)
    s.select(NodeRef(1, ()))
    v.shutdown()
    assert s.app.tasks.wait_idle(10)
    assert v.result() is None


def test_empty_state_shows_a_hint(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "rows.json", json.dumps([{"i": i} for i in range(5)]))
    s = make_session(p)
    v = make_view(s)
    pump()
    assert v.editor.is_empty() and v.editor.empty_text() == textview.t("gui.text.empty")
    v.editor.update_extras()
    assert v.editor.extraSelections() == []             # no current-line bar on an empty pane
    v.grab()
    show(qtbot, v, s, NodeRef(1, ()))
    assert v.editor.empty_text() == "" and not v.editor.is_empty()
    s.select(None)
    qtbot.waitUntil(lambda: v.editor.is_empty(), timeout=TIMEOUT)
    assert v.editor.empty_text() == textview.t("gui.text.empty")


def test_selected_text_and_signal(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "rows.json", json.dumps([{"i": i} for i in range(5)]))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(2, ()))
    assert v.selected_text() == ""
    with qtbot.waitSignal(v.selection_changed, timeout=2000):
        v.editor.selectAll()
    assert v.selected_text() == '{\n  "i": 2\n}'
