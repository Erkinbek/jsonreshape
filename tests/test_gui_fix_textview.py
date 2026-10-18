"""Fix round for the text view (jsonreshape/gui/textview.py).

* P-F1: folding removes the folded lines from the document in bounded slices (never a
  Python loop over all blocks, never a hit test after a big change) -- a big region folds
  and unfolds with bounded steps and the document shrinks;
* P-F7 / T8: the text is inserted in chunks of at most CHUNK_CHARS characters; an indent
  change, fold all while loading and a quick-find target beyond the loaded part never
  insert the rest synchronously; pretty texts are cached and shared between views;
* M2: copying maps the selection to the complete text (shortened lines, control characters);
* C6: moving the caret selects the node at the caret (debounced);
* C7: the data font follows ``app.settings_changed`` keeping the zoom;
* pure helpers: ``line_starts``, ``LineRanges`` (against a set model), ``node_path_at``
  (against a reference walk of the JSON value).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

from jsonreshape.core import Reformatter  # noqa: E402
from jsonreshape.gui import textview  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402
from jsonreshape.gui.textview import (  # noqa: E402
    LineRanges,
    PrettyText,
    TextView,
    line_starts,
    node_path_at,
    scan_folds,
)

pytestmark = pytest.mark.gui

TIMEOUT = 20000


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

    def factory(session: DocSession) -> TextView:
        v = TextView(session)
        qtbot.addWidget(v)
        v.resize(900, 500)
        views.append(v)
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


def wait_idle(qtbot, v: TextView) -> None:
    qtbot.waitUntil(lambda: not v.is_loading() and (v.result() is not None or v.message() is not None),
                    timeout=TIMEOUT)


def show(qtbot, v: TextView, s: DocSession, ref: NodeRef) -> None:
    s.select(ref)
    wait_idle(qtbot, v)


def reformat(raw: bytes, indent=2) -> str:
    f = Reformatter(indent=indent)
    return (f.feed(raw) + f.finish()).decode("utf-8")


def pretty_of(value, indent=2) -> PrettyText:
    text = reformat(json.dumps(value, ensure_ascii=False).encode("utf-8"), indent)
    folds, _ = scan_folds(text)
    return PrettyText(text=text, folds=folds, kind="object", starts=line_starts(text))


def reference_paths(value) -> list[tuple[tuple, tuple]]:
    """Per pretty-text line: the (path, child indices) of the value written on it."""
    out: list[tuple[tuple, tuple]] = []

    def emit(v, path, idx):
        if isinstance(v, dict) and v:
            out.append((path, idx))
            for i, (k, x) in enumerate(v.items()):
                emit(x, path + (k,), idx + (i,))
            out.append((path, idx))
        elif isinstance(v, list) and v:
            out.append((path, idx))
            for i, x in enumerate(v):
                emit(x, path + (i,), idx + (i,))
            out.append((path, idx))
        else:
            out.append((path, idx))

    emit(value, (), ())
    return out


# ----------------------------------------------------------------------------------
# pure helpers
# ----------------------------------------------------------------------------------
@settings(max_examples=200, deadline=None)
@given(st.text(alphabet="ab\n Ж", max_size=60))
def test_line_starts_match_split(text) -> None:
    starts = line_starts(text)
    lines = text.split("\n")
    assert len(starts) == len(lines) + 1
    for i, line in enumerate(lines):
        assert text[starts[i]:starts[i + 1] - 1] == line


def test_line_starts_across_pieces(monkeypatch) -> None:
    monkeypatch.setattr(textview, "_STARTS_STEP", 7)
    text = "\n".join("x" * (i % 13) for i in range(500))
    starts = line_starts(text)
    lines = text.split("\n")
    assert [text[starts[i]:starts[i + 1] - 1] for i in range(len(lines))] == lines


def _model(ranges: LineRanges) -> set[int]:
    return {x for a, b in ranges.ranges() for x in range(a, b + 1)}


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.tuples(st.integers(0, 80), st.integers(0, 12), st.lists(st.tuples(st.integers(0, 12),
                                                                                       st.integers(0, 4)),
                                                                             max_size=3)), max_size=12))
def test_line_ranges_behave_like_a_set(ops) -> None:
    r = LineRanges()
    hidden: set[int] = set()
    for lo, width, inner in ops:
        hi = lo + width
        starts: list[int] = []
        ends: list[int] = []
        pos = lo
        for gap, length in inner:          # sorted, disjoint ranges inside lo..hi
            a = pos + gap
            b = a + length
            if b > hi:
                break
            starts.append(a)
            ends.append(b)
            pos = b + 2
        r.replace(lo, hi, starts, ends)
        hidden -= set(range(lo, hi + 1))
        for a, b in zip(starts, ends):
            hidden |= set(range(a, b + 1))
        assert _model(r) == hidden
        pairs = r.ranges()
        assert all(b1 + 1 < a2 for (_a1, b1), (a2, _b2) in zip(pairs, pairs[1:]))      # merged, sorted
        shown = [x for x in range(120) if x not in hidden]
        for x in range(120):
            assert (r.find(x) >= 0) == (x in hidden)
            assert r.before(x) == sum(1 for y in hidden if y < x)
            state, end = r.run_at(x)
            assert state == (x in hidden)
            assert all((y in hidden) == state for y in range(x, min(end, 130) + 1))
        for d, x in enumerate(shown):
            assert r.to_source(d) == x
            if d + 1 < len(shown):
                assert r.next_free(x) == shown[d + 1]
        ca, cb = r.clip(10, 40)
        assert {y for a, b in zip(ca, cb) for y in range(a, b + 1)} == {y for y in hidden if 10 <= y <= 40}
    appended = LineRanges()
    appended.append(3, 5)
    appended.append(6, 8)        # adjacent: merged
    appended.append(20, 21)
    assert appended.ranges() == [(3, 8), (20, 21)] and appended.total() == 8
    assert LineRanges.from_ranges([(1, 2), (3, 4), (9, 9)]).ranges() == [(1, 4), (9, 9)]


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(alphabet='ab"\\\u0085 é', max_size=5),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(
        st.text(alphabet='ab"\\:{[  ', max_size=4), children, max_size=4),
    max_leaves=30)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(value=json_values, indent=st.sampled_from([2, "\t"]))
def test_node_path_at_matches_a_reference_walk(value, indent) -> None:
    pretty = pretty_of(value, indent)
    want = reference_paths(value)
    assert pretty.line_count() == len(want)
    for line, expected in enumerate(want):
        assert node_path_at(pretty, line) == expected, (line, pretty.line_text(line))


def test_node_path_at_in_a_cut_text_and_with_long_keys(monkeypatch) -> None:
    value = {"a": [{"k": i, "deep": {"x": [i, i]}} for i in range(40)], "z": 1}
    text = reformat(json.dumps(value).encode())
    cut = "\n".join(text.split("\n")[:150])           # unclosed regions at the end
    folds, balanced = scan_folds(cut)
    assert not balanced
    pretty = PrettyText(text=cut, folds=folds, starts=line_starts(cut))
    want = reference_paths(value)
    for line in range(1, 149):
        if cut.split("\n")[line].strip() in ("}", "},", "]", "],"):
            continue                                    # a closing line: its container
        assert node_path_at(pretty, line) == want[line]
    # a key longer than the display limit: the key comes from the complete text
    monkeypatch.setattr(textview, "MAX_LINE_CHARS", 50)
    long_key = "K" * 300
    full = reformat(json.dumps({"a": 1, long_key: {"b": 2}}).encode())
    display, n = textview._display_text(full, 50)
    assert n == 1
    folds, _ = scan_folds(full)
    pretty = PrettyText(text=display, full=full, folds=folds, starts=line_starts(display),
                        full_starts=line_starts(full), cut_lines=n)
    assert node_path_at(pretty, 3) == ((long_key, "b"), (1, 0))


# ----------------------------------------------------------------------------------
# P-F1: folding big regions in bounded steps
# ----------------------------------------------------------------------------------
def test_folding_a_big_region_runs_in_bounded_steps(make_session, make_view, qtbot, tmp_path, monkeypatch) -> None:
    rows = [{"i": i, "v": [i, i + 1], "s": "x"} for i in range(9000)]       # ~90k lines
    p = write(tmp_path, "big.json", json.dumps({"head": 1, "items": rows, "tail": 2}))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    total = ed.line_count()
    assert total > 70_000 and ed.document().blockCount() == total
    removed: list[int] = []
    inserted: list[int] = []
    orig_remove, orig_insert = ed._remove_blocks, ed._insert_after
    monkeypatch.setattr(ed, "_remove_blocks", lambda d0, d1: (removed.append(d1 - d0 + 1), orig_remove(d0, d1)))
    monkeypatch.setattr(ed, "_insert_after", lambda n, text: (inserted.append(len(text)), orig_insert(n, text)))

    def no_hit_test(*_a):
        raise AssertionError("hit test while folding")

    monkeypatch.setattr(ed, "cursorForPosition", no_hit_test)
    items = v.text().split("\n").index('  "items": [')
    close = ed.folds.close_of(items)
    t0 = time.monotonic()
    assert ed.fold(items)
    assert time.monotonic() - t0 < 1.0                  # the call itself does one bounded slice
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    ed.update_extras()
    assert sum(removed) == close - items and max(removed) <= textview.SLICE_LINES and len(removed) > 1
    assert ed.document().blockCount() == total - (close - items)
    assert ed.block_of_line(items + 1) is None and ed.block_of_line(close + 1) == items + 1
    v.grab()                                            # paints: placeholder after the folded line
    assert [line for _r, line in ed.placeholder_rects()] == [items]
    assert ed.unfold(items)
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    assert ed.document().blockCount() == total and ed.document().toPlainText() == v.text()
    assert max(inserted) <= textview.CHUNK_CHARS + 1 and len(inserted) > 1


def test_nested_folds_and_partial_steps_keep_the_document_in_sync(make_session, make_view, qtbot, tmp_path,
                                                                   monkeypatch) -> None:
    monkeypatch.setattr(textview, "SLICE_LINES", 7)
    monkeypatch.setattr(textview, "CHUNK_CHARS", 64)
    rows = [{"i": i, "o": {"a": [1, 2, {"b": i}]}} for i in range(30)]
    p = write(tmp_path, "rows.json", json.dumps(rows))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    lines = v.text().split("\n")
    opens = [o for o, _c, _d in ed.folds.regions()]
    import random
    rnd = random.Random(5)
    for _ in range(40):
        o = rnd.choice(opens)
        ed.toggle_fold(o)
        if rnd.random() < 0.3:
            qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    hidden = set()
    for o in ed.folded:
        hidden |= set(range(o + 1, ed.folds.close_of(o) + 1))
    expected = [i for i in range(len(lines)) if i not in hidden]
    assert ed.shown_lines() == expected
    assert ed.document().toPlainText() == "\n".join(lines[i] for i in expected)


# ----------------------------------------------------------------------------------
# P-F7 / T8: chunked loading, nothing synchronous, cached and shared texts
# ----------------------------------------------------------------------------------
def test_loading_is_chunked_and_follow_ups_wait_for_it(make_session, make_view, qtbot, tmp_path, monkeypatch) -> None:
    rows = [{"i": i, "name": f"row {i}", "tags": ["a", "b"]} for i in range(12000)]
    p = write(tmp_path, "many.json", json.dumps(rows))
    s = make_session(p)
    v = make_view(s)
    ed = v.editor
    sizes: list[int] = []
    orig = ed._load_chunk

    def chunk():
        before = ed.document().characterCount() if ed.loaded_lines() else 0
        orig()
        sizes.append(ed.document().characterCount() - before)

    monkeypatch.setattr(ed, "_load_chunk", chunk)
    monkeypatch.setattr(textview, "SYNC_BUDGET_S", 0.0)        # one step per user action
    s.select(NodeRef(None, ()))
    qtbot.waitUntil(lambda: v.result() is not None, timeout=TIMEOUT)
    assert ed.loading
    # fold all while loading: no synchronous insertion of the rest; folded lines are skipped
    n = len(sizes)
    ed.fold_all()
    assert ed.busy() and len(sizes) == n + 1
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    assert max(sizes) <= textview.CHUNK_CHARS + 2
    assert ed.document().blockCount() == len(rows) + 2
    ed.unfold_all()
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    assert ed.document().toPlainText() == v.text()
    # quick find of the last row while the text is being inserted again: selected once there
    v.set_indent(4)
    qtbot.waitUntil(lambda: v.result() is not None and ed.loading, timeout=TIMEOUT)
    v.quick_find()
    v.find_bar._edit.setText("row 11999")
    v.find_bar.flush()
    qtbot.waitUntil(lambda: v.match_count() == 1, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not ed.busy() and not ed.goal_pending(), timeout=TIMEOUT)
    assert ed.textCursor().selectedText() == "row 11999"
    assert v.text().split("\n")[ed.current_line()].strip() == '"name": "row 11999",'


def test_indent_change_restores_position_and_folds_asynchronously(make_session, make_view, qtbot, tmp_path) -> None:
    rows = [{"i": i, "o": {"k": i}} for i in range(8000)]
    p = write(tmp_path, "rows.json", json.dumps(rows))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    lines = v.text().split("\n")
    target = lines.index('    "i": 7000,')
    region = ed.folds.opens[5]
    ed.fold(region)
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    ed.setTextCursor(QTextCursor(ed.block_for_line(target)))
    folded = set(ed.folded)
    v.set_indent(4)
    qtbot.waitUntil(lambda: v.result() is not None and v.result().text.startswith('[\n    {'), timeout=TIMEOUT)
    assert ed.loading                                  # the call returned before the text is all in
    wait_idle(qtbot, v)
    assert ed.folded == folded and ed.current_line() == target


def test_pretty_texts_are_cached_and_shared(make_session, make_view, qtbot, tmp_path, monkeypatch) -> None:
    calls: list = []
    orig = textview.build_pretty

    def counting(session, ref, *a, **k):
        calls.append(tuple(ref.path) if ref.record is None else ref.record)
        return orig(session, ref, *a, **k)

    monkeypatch.setattr(textview, "build_pretty", counting)
    p = write(tmp_path, "rows.json", json.dumps([{"i": i} for i in range(20)]))
    s = make_session(p)
    v1 = make_view(s)
    v2 = make_view(s)
    show(qtbot, v1, s, NodeRef(1, ()))
    wait_idle(qtbot, v2)
    assert calls == [1] and v1.text() == v2.text() == '{\n  "i": 1\n}'      # two views, one build
    show(qtbot, v1, s, NodeRef(2, ()))
    show(qtbot, v1, s, NodeRef(1, ()))
    wait_idle(qtbot, v2)
    assert calls == [1, 2]                                                   # switching back: cached
    show(qtbot, v1, s, NodeRef(3, ()))
    show(qtbot, v1, s, NodeRef(4, ()))
    show(qtbot, v1, s, NodeRef(1, ()))
    assert calls == [1, 2, 3, 4, 1]                                          # at most PRETTY_CACHE kept


def test_a_shared_build_continues_when_one_view_leaves(make_session, make_view, qtbot, tmp_path) -> None:
    p = write(tmp_path, "rows.json", json.dumps([{"i": i} for i in range(20)]))
    s = make_session(p)
    v1 = make_view(s)
    v2 = make_view(s)
    s.select(NodeRef(5, ()))
    v1.shutdown()                                       # v1 leaves while the text is formatted
    wait_idle(qtbot, v2)
    assert v2.text() == '{\n  "i": 5\n}'


# ----------------------------------------------------------------------------------
# M2: copy maps the selection to the complete text
# ----------------------------------------------------------------------------------
def test_copy_takes_the_complete_text(make_session, make_view, qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(textview, "MAX_LINE_CHARS", 200)
    obj = {"blob": "B" * 2000, "nel": "a\u0085b", "ls": "x y", "tail": [1, 2]}
    p = write(tmp_path, "copy.json", json.dumps(obj, ensure_ascii=False))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    assert "…" in v.text() and v.result().full is not None
    # everything selected (Ctrl+A, Ctrl+C in the editor): the complete text, valid JSON
    ed.setFocus()
    QTest.keyClick(ed, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QGuiApplication.clipboard().setText("")
    QTest.keyClick(ed, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    clip = QGuiApplication.clipboard().text()
    assert json.loads(clip) == obj and clip == v.result().full
    assert v.copy_text() == v.result().full
    # a selection ending in the shortened tail takes the whole line
    lines = v.text().split("\n")
    blob = next(i for i, line in enumerate(lines) if '"blob"' in line)
    block = ed.block_for_line(blob)
    c = QTextCursor(block)
    c.setPosition(block.position() + 2)
    c.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)   # through the …
    ed.setTextCursor(c)
    assert v.copy_text() == '"blob": "' + "B" * 2000 + '",'
    assert v.selected_text() == v.copy_text()
    # a selection over a folded region copies the folded lines too
    tail = next(i for i, line in enumerate(lines) if '"tail"' in line)
    ed.fold(tail)
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    start = ed.block_for_line(tail)
    c = QTextCursor(start)
    c.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(c)
    full = v.result().full.split("\n")
    assert v.copy_text() == "\n".join(full[tail:])
    # the editor's own copy (context menu) uses the same mapping
    QGuiApplication.clipboard().setText("")
    ed.copy()
    assert QGuiApplication.clipboard().text() == "\n".join(full[tail:])


# ----------------------------------------------------------------------------------
# C6: the caret selects its node
# ----------------------------------------------------------------------------------
def test_caret_selects_the_node_at_the_caret(make_session, make_view, qtbot, tmp_path) -> None:
    rows = [{"id": i, "contact": {"city": "Xiva", "tags": ["a", "b"]}} for i in range(5)]
    p = write(tmp_path, "rows.json", json.dumps(rows))
    s = make_session(p)
    v = make_view(s)
    show(qtbot, v, s, NodeRef(2, ()))
    ed = v.editor
    lines = v.text().split("\n")
    # a programmatic caret move selects nothing
    ed.setTextCursor(QTextCursor(ed.block_for_line(lines.index('    "city": "Xiva",'))))
    QTest.qWait(textview.CARET_DEBOUNCE_MS + 150)
    assert s.current == NodeRef(2, ())
    # the keyboard: Down twice from the top -> "contact" -> "city"
    ed.setTextCursor(QTextCursor(ed.block_for_line(0)))
    ed.setFocus()
    QTest.keyClick(ed, Qt.Key.Key_Down)
    QTest.keyClick(ed, Qt.Key.Key_Down)
    QTest.keyClick(ed, Qt.Key.Key_Down)
    qtbot.waitUntil(lambda: s.current is not None and tuple(s.current.path) == ("contact", "city"), timeout=3000)
    assert s.current.record == 2
    assert v.shown_ref() == NodeRef(2, ())             # the text keeps showing the record
    # an array element by the mouse; the closing line of a container is the container
    elem = lines.index('      "b"')
    rect = ed.blockBoundingGeometry(ed.block_for_line(elem)).translated(ed.contentOffset())
    QTest.mouseClick(ed.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     rect.center().toPoint())
    qtbot.waitUntil(lambda: tuple(s.current.path) == ("contact", "tags", 1), timeout=3000)
    # the refs carry the child indices (R2-10: the exact member, also among duplicate keys)
    assert v.ref_at_line(lines.index("  }")) == NodeRef(2, ("contact",), (1,))
    assert v.ref_at_line(0) == NodeRef(2, (), ())


# ----------------------------------------------------------------------------------
# C7: the data font follows the settings, keeping the zoom
# ----------------------------------------------------------------------------------
def test_data_font_follows_settings_keeping_zoom(make_session, make_view, qtbot, app, tmp_path) -> None:
    p = write(tmp_path, "rows.json", json.dumps([{"i": 1}]))
    s = make_session(p)
    v = make_view(s)
    v.zoom(2)
    app.state.set("mono_size", 15)
    app.settings_changed.emit()
    assert v.font_point_size() == 17
    assert v.editor.document().defaultFont().pointSizeF() == 17
    v.zoom(0)
    assert v.font_point_size() == 15
    assert isinstance(v.editor, QPlainTextEdit)
    QApplication.processEvents()
