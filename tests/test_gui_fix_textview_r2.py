"""Second fix round for the text view (jsonreshape/gui/textview.py).

* R2-1: copying from the editor hands Qt a ``QMimeData`` made by Qt itself (a Python-made
  one is deleted by the clipboard after the interpreter finished: the process crashed at
  exit) -- a subprocess copies and must exit with code 0;
* R2-2: every character a ``QTextDocument`` turns into a block / frame boundary (CR,
  U+2029, U+FDD0, U+FDD1; checked exhaustively) is shown as a visible symbol, so document
  blocks and source lines stay one to one (folding, copy, gutter, caret);
* R2-6: :func:`scan_folds` searches in windows of whole lines (the GIL is free between
  them) and looks only at the ends of a line longer than a window; the long-line cut
  happens before the character mapping -- both give the same results as before;
* R2-9: a deferred bulk fold is not lost by a fold / unfold in the same event-loop turn;
  an indent change keeps a position still being restored, also when the text is cut at
  the size limit;
* R2-10: the node at the caret carries its child indices (from the fold structure and
  the shown node's resolved indices): the exact member among duplicate keys.
"""

from __future__ import annotations

import gc
import json
import os
import random
import re
import subprocess
import sys
import textwrap
from array import array
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("PySide6")

import shiboken6  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QTextCursor, QTextDocument  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPlainTextDocumentLayout  # noqa: E402

from jsonreshape.core import Reformatter  # noqa: E402
from jsonreshape.gui import textview  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402
from jsonreshape.gui.textview import FoldModel, TextView, scan_folds  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 20000
ROOT = Path(__file__).resolve().parents[1]
#: Characters a QTextDocument splits blocks on when they are inserted (besides LF).
QT_SEPARATORS = "\r\u2029\ufdd0\ufdd1"


# ----------------------------------------------------------------------------------
# fixtures / helpers
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _collect_on_the_gui_thread():
    """Cyclic garbage of these tests (closed sessions, views and their wrappers) is
    collected here, on the GUI thread, not later by a worker thread that happens to
    trigger a collection (Qt objects must be deleted on their own thread)."""
    yield
    QApplication.processEvents()          # deleteLater() of the closed widgets
    gc.collect()


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


def write(path: Path, data) -> Path:
    if isinstance(data, (bytes, bytearray)):
        path.write_bytes(bytes(data))
    else:
        path.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return path


def wait_idle(qtbot, v: TextView) -> None:
    qtbot.waitUntil(lambda: not v.is_loading() and (v.result() is not None or v.message() is not None),
                    timeout=TIMEOUT)


def show(qtbot, v: TextView, s: DocSession, ref: NodeRef) -> None:
    s.select(ref)
    wait_idle(qtbot, v)


def pump(n: int = 5) -> None:
    for _ in range(n):
        QApplication.processEvents()


def reformat(raw: bytes, indent=2) -> str:
    f = Reformatter(indent=indent)
    return (f.feed(raw) + f.finish()).decode("utf-8")


def doc_lines(ed) -> list[str]:
    d = ed.document()
    return [d.findBlockByNumber(i).text() for i in range(d.blockCount())]


def check_sync(ed, text: str) -> None:
    """The document holds exactly the source lines not hidden by outer folds."""
    lines = text.split("\n")
    hidden: set[int] = set()
    for o in ed.folded:
        c = ed.folds.close_of(o)
        if c is not None:
            hidden |= set(range(o + 1, c + 1))
    expected = [i for i in range(len(lines)) if i not in hidden]
    assert ed.document().blockCount() == len(expected), (ed.document().blockCount(), len(expected))
    assert doc_lines(ed) == [lines[i] for i in expected]
    assert ed.shown_lines() == expected


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
# R2-1: copy hands Qt a Qt-made QMimeData; the process exits cleanly
# ----------------------------------------------------------------------------------
_COPY_SCRIPT = textwrap.dedent('''
    import json, os, sys, time
    sys.path.insert(0, {root!r})
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from jsonreshape.gui.context import AppContext
    from jsonreshape.gui.guistate import GuiState
    from jsonreshape.gui.session import DocSession, NodeRef
    from jsonreshape.gui.textview import TextView

    def run_until(cond):
        t0 = time.monotonic()
        while not cond():
            app.processEvents()
            time.sleep(0.002)
            if time.monotonic() - t0 > 60:
                raise SystemExit("timeout")

    ctx = AppContext(state=GuiState.load(os.path.join({home!r}, "gs.json")), headless=True)
    ctx.tasks.set_poll_interval(10)
    s = DocSession(ctx, {path!r}, use_cache=False)
    done = []
    s.index_finished.connect(lambda *a: done.append(1))
    s.start()
    run_until(lambda: done)
    v = TextView(s)
    v.resize(600, 300)
    v.show()
    s.select(NodeRef(None, ()))
    run_until(lambda: v.result() is not None and not v.is_loading())
    ed = v.editor
    full = v.result().copy_text
    clip = QGuiApplication.clipboard()
    ed.setFocus()
    ed.selectAll()
    ed.copy()                                    # the editor's own copy (createMimeDataFromSelection)
    ok = [clip.text() == full]
    clip.setText("")
    QTest.keyClick(ed, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)    # Ctrl+C in the editor
    ok.append(clip.text() == full and clip.mimeData().formats() == ["text/plain"])
    ed.copy()                                    # replaces (deletes) the previous mime data
    ok.append(clip.text() == full)
    print("COPIED", all(ok), ok, flush=True)
    v.shutdown()
    s.close()
    ctx.tasks.shutdown(10)
    # the clipboard keeps the last mime data until the process ends
''')


def test_copy_from_the_editor_exits_cleanly(tmp_path) -> None:
    data = write(tmp_path / "copy.json", {"blob": "B" * (textview.MAX_LINE_CHARS + 500), "nel": "a\u0085b",
                                          "sep": "x\u2029y\ufdd0z", "tail": [1, 2]})
    home = tmp_path / "home"
    home.mkdir()
    script = _COPY_SCRIPT.format(root=str(ROOT), home=str(home), path=str(data))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", JSONRESHAPE_HOME=str(home))
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=180,
                          cwd=str(tmp_path))
    assert "COPIED True" in proc.stdout, (proc.stdout, proc.stderr[-2000:])
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr[-2000:])


def test_the_mime_data_of_a_copy_is_made_by_qt(make_session, make_view, qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(textview, "MAX_LINE_CHARS", 50)
    obj = {"blob": "B" * 300, "z": [1]}
    s = make_session(write(tmp_path / "m.json", obj))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    ed.selectAll()
    data = ed.createMimeDataFromSelection()
    try:
        assert not shiboken6.createdByPython(data)            # Qt's own object: safe to delete any time
        assert data.formats() == ["text/plain"]
        assert data.text() == v.result().copy_text and json.loads(data.text()) == obj
    finally:
        shiboken6.delete(data)
    plain = textview.qt_mime_data("abc")
    try:
        assert not shiboken6.createdByPython(plain) and plain.text() == "abc" and plain.formats() == ["text/plain"]
    finally:
        shiboken6.delete(plain)


# ----------------------------------------------------------------------------------
# R2-2: Qt block / frame separators
# ----------------------------------------------------------------------------------
def test_display_text_keeps_every_character_in_one_qt_block(qapp) -> None:
    """Exhaustive: no code point of the display text splits (or changes) a block."""
    for ch in QT_SEPARATORS + "\u2028":
        shown, _ = textview._display_text("a" + ch + "b", 100)
        assert shown[1] != ch and len(shown) == 3
    cps = [c for c in range(0x110000) if c != 0x0A and not 0xD800 <= c < 0xE000]
    step = 1 << 15
    for i in range(0, len(cps), step):
        raw = "".join(map(chr, cps[i:i + step]))
        shown, cut = textview._display_text(raw, len(raw) + 1)
        assert cut == 0 and len(shown) == len(raw)
        doc = QTextDocument()
        doc.setDocumentLayout(QPlainTextDocumentLayout(doc))
        QTextCursor(doc).insertText(shown)
        assert doc.blockCount() == 1, hex(cps[i])
        assert doc.firstBlock().text() == shown, hex(cps[i])


_SPECIAL = ["\u2028", "\u2029", "\ufdd0", "\ufdd1", "\ufdef", "\ufffe", "\uffff", "\U0001fffe", "\x85", "\ufeff",
            "￼", "\U0001F600", "a", " ", "\\", '"', "é"]
_special_text = st.lists(st.sampled_from(_SPECIAL), max_size=6).map("".join)
_special_values = st.recursive(
    st.none() | st.integers(-3, 3) | _special_text,
    lambda children: st.lists(children, max_size=3) | st.dictionaries(_special_text, children, max_size=3),
    max_leaves=10)


@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(value=_special_values)
def test_qt_separators_in_the_data_keep_lines_and_nodes_in_sync(app, qtbot, tmp_path_factory, value) -> None:
    path = write(tmp_path_factory.mktemp("sep") / "v.json", value)
    s = DocSession(app, path, use_cache=False)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    v = TextView(s)
    qtbot.addWidget(v)
    v.resize(800, 400)
    v.show()
    try:
        show(qtbot, v, s, NodeRef(None, ()))
        r = v.result()
        ed = v.editor
        disp = r.text.split("\n")
        full = r.copy_text.split("\n")
        assert len(disp) == len(full) == ed.line_count()
        assert not any(ch in r.text for ch in QT_SEPARATORS + "\u2028")
        check_sync(ed, r.text)
        # every line -> its node (path and child indices)
        want = reference_paths(value)
        assert len(want) == len(disp)
        for line, (path, idx) in enumerate(want):
            ref = v.ref_at_line(line)
            assert ref is not None and (tuple(ref.path), tuple(ref.ipath)) == (path, idx), (line, disp[line])
        # folding each region and unfolding it keeps the document in sync
        for o, _c, _d in list(ed.folds.regions()):
            assert ed.fold(o)
            qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
            check_sync(ed, r.text)
            assert ed.unfold(o)
            qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
            check_sync(ed, r.text)
        ed.fold_all()
        qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
        check_sync(ed, r.text)
        ed.unfold_all()
        qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
        check_sync(ed, r.text)
        # copying: the whole text, and single lines, are the complete (unmapped) text
        ed.selectAll()
        assert v.copy_text() == r.copy_text and json.loads(r.copy_text) == value
        for line in range(len(disp)):
            block = ed.block_for_line(line)
            c = QTextCursor(block)
            c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            ed.setTextCursor(c)
            if c.hasSelection():
                assert v.copy_text() == full[line]
    finally:
        v.shutdown()
        s.close()
        v.close()
        v.deleteLater()
        QApplication.processEvents()
        gc.collect()                       # this example's garbage, on the GUI thread


@pytest.mark.parametrize("ch", ["\ufdd0", "\ufdd1", "\u2029"])
def test_a_frame_marker_in_a_string_keeps_caret_copy_and_folds_right(make_session, make_view, qtbot, tmp_path,
                                                                      ch) -> None:
    obj = {"a": "x" + ch + "y", "b": [1, 2, 3], "c": {"d": 1}, "e": 5}
    s = make_session(write(tmp_path / "nc.json", obj))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    text = v.text()
    lines = text.split("\n")
    assert ed.document().blockCount() == len(lines) == 12
    e = lines.index('  "e": 5')
    ed.setTextCursor(QTextCursor(ed.block_for_line(e)))
    assert ed.current_line() == e and v.ref_at_line(e) == NodeRef(None, ("e",), (3,))
    c = QTextCursor(ed.block_for_line(e))
    c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(c)
    assert v.copy_text() == '  "e": 5'
    b = lines.index('  "b": [')
    ed.fold(b)
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    check_sync(ed, text)
    ed.unfold(b)
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    check_sync(ed, text)
    ed.selectAll()
    assert json.loads(v.copy_text()) == obj


# ----------------------------------------------------------------------------------
# R2-6: scan_folds in windows; display text cut before mapping
# ----------------------------------------------------------------------------------
_REF_FOLD_RE = re.compile(r"(?P<o>[\[{])$|^[ \t]*(?P<c>[\]}])", re.M)


def reference_scan(text: str) -> tuple[FoldModel, bool]:
    """The single-pass scan (one regular-expression search over the whole text)."""
    opens, closes, depths = array("i"), array("i"), array("i")
    close_lines, close_opens = array("i"), array("i")
    parents, sib_before, kinds = array("i"), array("i"), bytearray()
    stack: list[int] = []
    spans = [0]
    line = 0
    last = 0
    for m in _REF_FOLD_RE.finditer(text):
        s = m.start()
        line += text.count("\n", last, s)
        last = s
        if m.lastgroup == "o":
            parents.append(stack[-1] if stack else -1)
            sib_before.append(spans[-1])
            kinds.append(1 if m.group("o") == "[" else 0)
            stack.append(len(opens))
            spans.append(0)
            opens.append(line)
            closes.append(-1)
            depths.append(len(stack) - 1)
        elif stack:
            i = stack.pop()
            spans.pop()
            closes[i] = line
            spans[-1] += line - opens[i]
            close_lines.append(line)
            close_opens.append(opens[i])
    return FoldModel(opens, closes, depths, close_lines, close_opens, text.count("\n") + 1, parents, sib_before,
                     kinds), not stack


def same_folds(a: tuple[FoldModel, bool], b: tuple[FoldModel, bool]) -> bool:
    (fa, oka), (fb, okb) = a, b
    return oka == okb and all(getattr(fa, k) == getattr(fb, k) for k in FoldModel.__slots__)


_fold_pieces = st.lists(st.sampled_from(["{", "[", "}", "]", "  ", "\t", "x", "\n", "\n", '"a": [', "],", "},",
                                         "\n  }", "\n]", "x" * 30, "[" * 3, "\n" + " " * 20 + "]"]), max_size=60)


@settings(max_examples=300, deadline=None)
@given(parts=_fold_pieces, window=st.integers(1, 40))
def test_scan_folds_in_windows_matches_a_single_pass(parts, window) -> None:
    text = "".join(parts)
    old = textview.FOLD_WINDOW
    textview.FOLD_WINDOW = window
    try:
        got = scan_folds(text)
    finally:
        textview.FOLD_WINDOW = old
    assert same_folds(got, reference_scan(text)), (text, window)


_json_values = st.recursive(
    st.none() | st.integers() | st.text(alphabet="ab[]{} \t\"\\", max_size=40),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(alphabet="k[{ ", max_size=30),
                                                                      children, max_size=4),
    max_leaves=25)


@settings(max_examples=150, deadline=None)
@given(value=_json_values, indent=st.sampled_from([2, 4, "\t"]), window=st.integers(1, 64))
def test_scan_folds_of_pretty_json_in_windows(value, indent, window) -> None:
    text = reformat(json.dumps(value).encode(), indent)
    old = textview.FOLD_WINDOW
    textview.FOLD_WINDOW = window
    try:
        got = scan_folds(text)
    finally:
        textview.FOLD_WINDOW = old
    assert same_folds(got, reference_scan(text))
    assert same_folds(scan_folds(text), reference_scan(text))


def test_scan_folds_searches_at_most_a_window_per_call(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    real = textview._FOLD_RE

    class Spy:
        def finditer(self, text, pos=0, endpos=None):
            end = len(text) if endpos is None else endpos
            calls.append((pos, end))
            return real.finditer(text, pos, end)

    monkeypatch.setattr(textview, "_FOLD_RE", Spy())
    flat = reformat(json.dumps(list(range(400_000))).encode())        # ~3.5 MB, one region
    folds, ok = scan_folds(flat)
    assert ok and list(folds.opens) == [0] and folds.closes[0] == flat.count("\n")
    assert len(calls) > 10 and max(e - s for s, e in calls) <= textview.FOLD_WINDOW
    # lines longer than a window: never searched, only their ends looked at
    calls.clear()
    big = textview.FOLD_WINDOW * 5
    text = reformat(json.dumps({"k" * big: [1, {"s": "v" * big}], "z": ["w" * big]}).encode())
    folds, ok = scan_folds(text)
    assert ok and same_folds((folds, ok), reference_scan(text))
    assert list(folds.opens) == [0, 1, 3, 7]
    assert max(e - s for s, e in calls) <= textview.FOLD_WINDOW


def _old_display_text(text: str, limit: int) -> tuple[str, int]:
    """Characters mapped first, then long lines cut (the previous order)."""
    mapped = text.translate(textview._DISPLAY_TRANS)
    pat = re.compile(r"(?m)^[^\n]{%d}" % (limit + 1))
    parts: list[str] = []
    pos = n = 0
    for m in pat.finditer(mapped):
        s = m.start()
        if s < pos:
            continue
        e = mapped.find("\n", s)
        e = len(mapped) if e < 0 else e
        parts.append(mapped[pos:s + limit])
        parts.append(textview.ELLIPSIS)
        pos = e
        n += 1
    if not n:
        return mapped, 0
    parts.append(mapped[pos:])
    return "".join(parts), n


@settings(max_examples=300, deadline=None)
@given(parts=st.lists(st.sampled_from(["a", "\n", "\x01", "\u2029", "\ufdd0", "\U0001F600", "xxxxxxxx", "\n\n"]),
                      max_size=50),
       limit=st.integers(1, 12), step=st.integers(1, 16))
def test_display_text_cuts_before_mapping_with_the_same_result(parts, limit, step) -> None:
    text = "".join(parts)
    old = textview._STARTS_STEP
    textview._STARTS_STEP = step
    try:
        got = textview._display_text(text, limit)
    finally:
        textview._STARTS_STEP = old
    assert got == _old_display_text(text, limit)


# ----------------------------------------------------------------------------------
# R2-9: deferred bulk folds; indent changes keep a pending position
# ----------------------------------------------------------------------------------
def test_a_deferred_fold_all_survives_a_fold_in_the_same_turn(make_session, make_view, qtbot, tmp_path,
                                                               monkeypatch) -> None:
    monkeypatch.setattr(textview, "DEFER_AFTER_S", -1.0)          # bulk changes always deferred
    s = make_session(write(tmp_path / "v.json", [{"k": [i, {"x": i}]} for i in range(10)]))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    text = v.text()
    ed.fold_all()                              # deferred to the next turn
    bulk = set(ed.folded)
    assert bulk and ed.busy()
    inner = next(o for o, _c, d in ed.folds.regions() if d == 2)
    ed.fold(inner)                             # same turn
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    pump()
    assert ed.folded == bulk | {inner}
    check_sync(ed, text)
    assert ed.document().blockCount() == 12    # "[", ten folded records, "]"


def test_a_deferred_level_fold_survives_an_unfold_in_the_same_turn(make_session, make_view, qtbot, tmp_path,
                                                                    monkeypatch) -> None:
    monkeypatch.setattr(textview, "DEFER_AFTER_S", -1.0)
    s = make_session(write(tmp_path / "v.json", [{"k": [i, {"x": [i]}]} for i in range(10)]))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    text = v.text()
    ed.fold_to_level(2)
    deep = sorted(ed.folded)
    ed.unfold(deep[0])
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    pump()
    assert ed.folded == set(deep[1:])
    check_sync(ed, text)
    assert ed._hidden.ranges() == ed._target.ranges()


def _random_value(rnd: random.Random, depth: int = 0):
    if depth > 3 or rnd.random() < 0.25:
        return rnd.choice([1, "s", None, True, [], {}, 1.5, "x" * rnd.randint(0, 60)])
    if rnd.random() < 0.5:
        return [_random_value(rnd, depth + 1) for _ in range(rnd.randint(0, 5))]
    return {f"k{i}": _random_value(rnd, depth + 1) for i in range(rnd.randint(0, 5))}


@pytest.mark.parametrize("seed", [6, 11, 23])
def test_random_fold_sequences_with_deferred_bulk_changes(make_session, make_view, qtbot, tmp_path, monkeypatch,
                                                          seed) -> None:
    rnd = random.Random(seed)
    monkeypatch.setattr(textview, "CHUNK_CHARS", rnd.choice([40, 200]))
    monkeypatch.setattr(textview, "SLICE_LINES", rnd.choice([1, 7]))
    monkeypatch.setattr(textview, "REBUILD_RANGES", rnd.choice([2, 64]))
    monkeypatch.setattr(textview, "SYNC_BUDGET_S", 0.0)
    monkeypatch.setattr(textview, "TICK_BUDGET_S", 0.0)
    monkeypatch.setattr(textview, "DEFER_AFTER_S", -1.0)
    s = make_session(write(tmp_path / "v.json", [_random_value(rnd) for _ in range(25)]))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    text = v.text()
    opens = [o for o, _c, _d in ed.folds.regions()]
    for _ in range(40):
        op = rnd.random()
        if op < 0.45:
            ed.toggle_fold(rnd.choice(opens))
        elif op < 0.6:
            ed.fold_all()
        elif op < 0.7:
            ed.unfold_all()
        elif op < 0.85:
            ed.fold_to_level(rnd.randint(1, 3))
        else:
            ed.set_folded(set(rnd.sample(opens, k=min(len(opens), rnd.randint(0, 6)))))
        for _ in range(rnd.randint(0, 2)):
            QApplication.processEvents()
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    pump()
    check_sync(ed, text)
    assert ed._hidden.ranges() == ed._target.ranges()


def test_a_second_indent_change_keeps_the_position_being_restored(make_session, make_view, qtbot, tmp_path,
                                                                  monkeypatch) -> None:
    monkeypatch.setattr(textview, "SYNC_BUDGET_S", 0.0)
    monkeypatch.setattr(textview, "TICK_BUDGET_S", 0.0)
    s = make_session(write(tmp_path / "rows.json", [{"i": i, "o": {"k": i}} for i in range(6000)]))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    target = v.text().split("\n").index('    "i": 5500,')
    folded = ed.folds.opens[3]
    ed.fold(folded)
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    ed.setTextCursor(QTextCursor(ed.block_for_line(target)))
    v.set_indent(4)
    qtbot.waitUntil(lambda: v.result() is not None and v.result().text.startswith("[\n    {"), timeout=TIMEOUT)
    assert ed.goal_pending()                   # the restored position waits for its line
    v.set_indent("tab")                        # before it is reached
    qtbot.waitUntil(lambda: v.result() is not None and v.result().text.startswith("[\n\t{"), timeout=TIMEOUT)
    wait_idle(qtbot, v)
    qtbot.waitUntil(lambda: not ed.goal_pending(), timeout=TIMEOUT)
    assert ed.current_line() == target and ed.folded == {folded}


def test_an_indent_change_keeps_the_position_in_a_cut_text(make_session, make_view, qtbot, tmp_path,
                                                            monkeypatch) -> None:
    monkeypatch.setattr(textview, "TEXT_CAP", 40_000)
    s = make_session(write(tmp_path / "rows.json", [{"i": i, "o": {"k": i}} for i in range(5000)]))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))
    ed = v.editor
    assert v.result().truncated
    n2 = ed.line_count()
    target = n2 // 2
    region = next(o for o in ed.folds.opens if o > target + 20 and ed.folds.close_of(o) is not None)
    ed.fold(region)
    qtbot.waitUntil(lambda: not ed.busy(), timeout=TIMEOUT)
    ed.setTextCursor(QTextCursor(ed.block_for_line(target)))
    v.set_indent(4)
    qtbot.waitUntil(lambda: v.result() is not None and v.result().text.startswith("[\n    {"), timeout=TIMEOUT)
    wait_idle(qtbot, v)
    qtbot.waitUntil(lambda: not ed.goal_pending(), timeout=TIMEOUT)
    assert ed.line_count() != n2                # the cut falls on another line
    assert ed.current_line() == target and ed.folded == {region}


# ----------------------------------------------------------------------------------
# R2-10: the exact member among duplicate keys
# ----------------------------------------------------------------------------------
DUP = b'{"dup": 1, "a": [0], "dup": {"x": [7, 8]}, "z": 0}'


def test_the_caret_selects_the_exact_duplicate_member(make_session, make_view, qtbot, tmp_path) -> None:
    s = make_session(write(tmp_path / "dup.json", DUP))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ()))           # shown without child indices
    lines = v.text().split("\n")
    first, second = lines.index('  "dup": 1,'), lines.index('  "dup": {')
    assert v.result().ipath == ()
    assert v.ref_at_line(first) == NodeRef(None, ("dup",), (0,))
    assert v.ref_at_line(second) == NodeRef(None, ("dup",), (2,))
    seven = lines.index("      7,")
    assert v.ref_at_line(seven) == NodeRef(None, ("dup", "x", 0), (2, 0, 0))
    assert s.node(v.ref_at_line(first)).raw() == b"1"
    assert s.node(v.ref_at_line(seven)).raw() == b"7"
    # the keyboard: the first duplicate (a path alone resolves to the last one)
    ed = v.editor
    ed.setTextCursor(QTextCursor(ed.block_for_line(0)))
    ed.setFocus()
    QTest.keyClick(ed, Qt.Key.Key_Down)
    qtbot.waitUntil(lambda: s.current is not None and tuple(s.current.path) == ("dup",), timeout=3000)
    assert s.current.ipath == (0,) and s.node(s.current).raw() == b"1"


def test_a_shown_member_without_child_indices_gets_them_resolved(make_session, make_view, qtbot, tmp_path) -> None:
    s = make_session(write(tmp_path / "rec.json", b'[{"a": 1, "a": {"b": [5]}}, {"a": 2}]'))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(0, ("a",)))          # the path resolves to the last "a"
    assert v.text() == '{\n  "b": [\n    5\n  ]\n}'
    assert v.result().ipath == (1,)
    assert v.ref_at_line(1) == NodeRef(0, ("a", "b"), (1, 0))
    assert v.ref_at_line(2) == NodeRef(0, ("a", "b", 0), (1, 0, 0))
    assert s.node(v.ref_at_line(2)).raw() == b"5"


def test_duplicate_members_are_different_texts(make_session, make_view, qtbot, tmp_path) -> None:
    s = make_session(write(tmp_path / "dup.json", DUP))
    v = make_view(s)
    show(qtbot, v, s, NodeRef(None, ("dup",), (0,)))
    assert v.text() == "1"
    show(qtbot, v, s, NodeRef(None, ("z",), (3,)))
    show(qtbot, v, s, NodeRef(None, ("dup",), (2,)))    # same path, the other member: not the cached text
    assert v.text() == '{\n  "x": [\n    7,\n    8\n  ]\n}'
    show(qtbot, v, s, NodeRef(None, ("z",), (3,)))
    show(qtbot, v, s, NodeRef(None, ("dup",), (0,)))
    assert v.text() == "1"
