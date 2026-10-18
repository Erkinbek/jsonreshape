"""RawView (jsonreshape/gui/rawview.py; ARCHITECTURE §11.7 "Raw", AGENT_PROMPT §4.3).

Real DocSessions over generated files (pretty multi-line JSON with Uzbek / Cyrillic /
emoji text, a few-MB single-line minified file, JSON Lines, a broken file, gzip): go to
offset / line, scrolling down then up, the scrollbar end, bounded rows of a long line,
multi-byte characters at row boundaries, the line index, the JSON error marker, copy,
quick find, following the current node only while visible, and no file reads in the GUI
thread at all.
"""

from __future__ import annotations

import bisect
import gzip
import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape.gui import rawdata, rawview  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.rawview import COPY_CAP, PageStore, RawView, parse_line, parse_offset  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402
from jsonreshape.reader import Document  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 20000


# ----------------------------------------------------------------------------------
# fixtures / helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
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
    views: list[RawView] = []

    def factory(session: DocSession, *, show: bool = True, height: int = 420) -> RawView:
        v = RawView(session)
        qtbot.addWidget(v)
        v.resize(900, height)
        views.append(v)
        if show:
            v.show()
            qtbot.waitExposed(v)
            settle(qtbot, v)
        return v

    yield factory
    for v in views:
        v.shutdown()


def settle(qtbot, v: RawView) -> None:
    qtbot.waitUntil(v.is_settled, timeout=TIMEOUT)


def records(n: int) -> list[dict]:
    return [{"id": i, "name": f"oʻzbek Жук {i} \U0001F600", "n": 1.10 if i % 2 else -0.0,
             "tags": ["a", {"x": [i, i * 2]}]} for i in range(n)]


@pytest.fixture
def pretty_file(tmp_path: Path) -> Path:
    p = tmp_path / "pretty.json"
    p.write_bytes(json.dumps(records(3000), indent=2, ensure_ascii=False).encode("utf-8"))
    return p


@pytest.fixture
def minified_file(tmp_path: Path) -> Path:
    """One line of ~2.5 MB with multi-byte characters everywhere."""
    p = tmp_path / "minified.json"
    p.write_bytes(json.dumps(records(26000), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return p


def shown_rows(v: RawView) -> list[tuple[int, int]]:
    return v.area.rows()


def true_rows(data: bytes, v: RawView) -> list[int]:
    """Every row start of the file by the view's current rule (the whole file walked)."""
    return rawdata.walk_rows(data, 0, 0, len(data), 10 ** 9, v.area.rule())


def row_containing(rows: list[tuple[int, int]], offset: int) -> tuple[int, int] | None:
    for r in rows:
        if r[0] <= offset < r[1]:
            return r
    return None


def page_rows(v: RawView) -> int:
    return v.area.page_rows()


# ----------------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------------
def test_parse_offset_and_line() -> None:
    assert parse_offset("123456") == 123456
    assert parse_offset(" 123 456 ") == 123456
    assert parse_offset("1_000") == 1000
    assert parse_offset("0x1E240") == 0x1E240
    assert parse_offset("0X1e240") == 0x1E240
    for bad in ("", "abc", "-5", "0x", "0xZZ", "12.5", "1e3"):
        with pytest.raises(ValueError):
            parse_offset(bad)
    assert parse_line("17") == 17
    for bad in ("0", "-1", "x", "", "1.5"):
        with pytest.raises(ValueError):
            parse_line(bad)


def test_page_store_is_bounded_and_reports_missing_pages() -> None:
    store = PageStore(10 * 64 * 1024 + 5, page_bytes=64 * 1024, capacity=4)
    assert store.window(10, 20) is None and store.urgent == [0]
    store.put({0: b"a" * 65536, 1: b"b" * 65536})
    data, base = store.window(65530, 65540)
    assert base == 65530 and data == b"a" * 6 + b"b" * 4
    store.put({k: bytes([k]) * 65536 for k in range(2, 8)})
    assert len(store) == 4 and store.nbytes() <= 4 * 65536
    assert store.window(0, 10) is None              # evicted (LRU)
    store.prefetch(7 * 65536, 10 * 65536 + 5)
    assert store.take(10) == [0, 8, 9]               # urgent first; prefetch capped at half the cache
    region = store.region_around(6 * 65536 + 3)
    assert region is not None and region[0] == 4 * 65536 and len(region[1]) == 4 * 65536


# ----------------------------------------------------------------------------------
# rows, navigation, scrolling
# ----------------------------------------------------------------------------------
def test_first_page_shows_the_lines(make_session, make_view, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    data = pretty_file.read_bytes()
    rows = shown_rows(v)
    assert rows[0][0] == 0 and len(rows) >= page_rows(v)
    lines = data.split(b"\n")
    texts = v.area.row_texts()
    for i, text in enumerate(texts[:page_rows(v)]):
        assert text == lines[i].decode("utf-8")
    assert v.position_label.text()
    # line numbers are known from the file start (and the small file got a line index)
    assert v.area.line_of_row(rows[3][0]) == 4


@pytest.mark.parametrize("offset_kind", ["start", "middle", "page_edge", "last", "past_end"])
def test_goto_offset_shows_the_row_containing_it(make_session, make_view, pretty_file, offset_kind) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    data = pretty_file.read_bytes()
    size = len(data)
    offset = {"start": 0, "middle": size // 2 + 7, "page_edge": 3 * 65536 + 1, "last": size - 1,
              "past_end": size + 100}[offset_kind]
    v.goto_offset(offset, 5)
    settle_view(v)
    expect = min(offset, size - 1)
    rows = shown_rows(v)
    row = row_containing(rows[:page_rows(v)], expect)
    assert row is not None and row[0] <= expect
    truth = true_rows(data, v)
    assert row[0] == truth[bisect.bisect_right(truth, expect) - 1]
    sel = v.area.selection()
    clamped = min(offset, size)
    assert sel == ((clamped, min(size, clamped + 5)) if clamped < size else None)


def settle_view(v: RawView, timeout: float = TIMEOUT / 1000) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not v.is_settled():
        QApplication.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError("raw view did not settle")
        threading.Event().wait(0.002)


@pytest.mark.parametrize("which", ["pretty", "minified"])
def test_scrolling_down_then_up_returns_the_same_rows(make_session, make_view, pretty_file, minified_file,
                                                      which) -> None:
    path = pretty_file if which == "pretty" else minified_file
    s = make_session(path)
    v = make_view(s)
    data = path.read_bytes()
    v.goto_offset(len(data) // 3)
    settle_view(v)
    start_rows = shown_rows(v)
    history = [v.area.top()]
    for step in (1, 3, 40, 200, 700):            # crosses several 64 KiB pages
        v.area.scroll_rows(step)
        settle_view(v)
        history.append(v.area.top())
    truth = true_rows(data, v)
    for top in history:
        assert top in truth                       # always a real row start
    tops = [truth.index(top) for top in history]
    assert tops == sorted(tops) and tops[-1] - tops[0] == 1 + 3 + 40 + 200 + 700
    for step in (700, 200, 40, 3, 1):
        v.area.scroll_rows(-step)
        settle_view(v)
    assert v.area.top() == history[0]
    assert shown_rows(v) == start_rows


def test_keys_and_wheel_move_rows(make_session, make_view, qtbot, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    truth = true_rows(pretty_file.read_bytes(), v)
    v.area.setFocus()
    QTest.keyClick(v.area, Qt.Key.Key_Down)
    settle_view(v)
    assert v.area.top() == truth[1]
    QTest.keyClick(v.area, Qt.Key.Key_PageDown)
    settle_view(v)
    assert v.area.top() == truth[1 + page_rows(v) - 1]
    QTest.keyClick(v.area, Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
    settle_view(v)
    assert v.area.top() == 0
    pos = QPointF(50, 50)
    wheel = QWheelEvent(pos, v.area.mapToGlobal(pos), QPoint(0, 0), QPoint(0, -240), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(v.area.viewport(), wheel)
    settle_view(v)
    assert v.area.top() == truth[6]               # two notches = 6 rows
    QTest.keyClick(v.area, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    settle_view(v)
    assert shown_rows(v)[-1][1] == len(pretty_file.read_bytes())


@pytest.mark.parametrize("which", ["pretty", "minified"])
def test_scrollbar_end_shows_the_end_of_the_file(make_session, make_view, pretty_file, minified_file, which) -> None:
    path = pretty_file if which == "pretty" else minified_file
    s = make_session(path)
    v = make_view(s)
    # the automatic line count widens the gutter when it ends (other rows, the end is read
    # again): let it finish first -- pages now arrive fast enough to race it
    import time

    deadline = time.monotonic() + TIMEOUT / 1000
    while not v.line_index.complete and time.monotonic() < deadline:
        QApplication.processEvents()
    settle_view(v)
    size = len(path.read_bytes())
    bar = v.area.verticalScrollBar()
    bar.setValue(bar.maximum())
    settle_view(v)
    rows = shown_rows(v)
    assert rows[-1][1] == size
    assert len(rows) == page_rows(v)              # the last page is full: no empty space below
    assert v.area.max_top() == v.area.top()
    assert bar.value() == bar.maximum()
    # dragging back to the middle lands on the row containing the mapped offset
    bar.setValue(bar.maximum() // 2)
    settle_view(v)
    truth = true_rows(path.read_bytes(), v)
    mapped = v.area._value_to_offset(bar.maximum() // 2)
    assert abs(mapped - v.area.max_top() // 2) <= 2
    assert v.area.top() == truth[bisect.bisect_right(truth, mapped) - 1]
    # scrolling down at the end stays at the end
    bar.setValue(bar.maximum())
    settle_view(v)
    end_top = v.area.top()
    v.area.scroll_rows(10)
    settle_view(v)
    assert v.area.top() == end_top


def test_long_single_line_is_split_into_bounded_rows(make_session, make_view, minified_file) -> None:
    s = make_session(minified_file)
    v = make_view(s)
    data = minified_file.read_bytes()
    assert data.count(b"\n") == 0 and len(data) > 2_000_000
    for offset in (0, 1_000_003, len(data) - 10):
        v.goto_offset(offset)
        settle_view(v)
        # the row width follows the viewport, which may still change while the first layout
        # settles on a loaded machine: compare with the rows of the width now in use
        truth = true_rows(data, v)
        rows = shown_rows(v)
        assert all(0 < e - b <= v.area.row_bytes() + 3 for b, e in rows)
        i = truth.index(rows[0][0])
        assert [b for b, _e in rows] == truth[i:i + len(rows)]


def test_multibyte_characters_are_never_broken(make_session, make_view, tmp_path) -> None:
    p = tmp_path / "multibyte.json"
    unit = "oʻzbek Жуков \U0001F600\U0001F680 é"
    p.write_bytes(json.dumps([unit * 3] * 3000, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    s = make_session(p)
    v = make_view(s)
    data = p.read_bytes()
    for start in (0, 65536 - 7, len(data) // 2, len(data) - 3000):
        v.area.goto_top_offset(start)
        settle_view(v)
        rows = shown_rows(v)
        texts = v.area.row_texts()
        for (b, e), text in zip(rows, texts):
            assert "\ufffd" not in text
            assert text == data[b:e].decode("utf-8")
        assert "".join(texts) == data[rows[0][0]:rows[-1][1]].decode("utf-8")
    # scrolling across page boundaries keeps the characters whole too
    v.area.goto_top_offset(65536 - 300)
    settle_view(v)
    for _ in range(8):
        v.area.scroll_rows(3)
        settle_view(v)
        assert all("\ufffd" not in t for t in v.area.row_texts())


# ----------------------------------------------------------------------------------
# lines
# ----------------------------------------------------------------------------------
def naive_line_start(data: bytes, line: int) -> int:
    pos = 0
    for _ in range(line - 1):
        pos = data.index(b"\n", pos) + 1
    return pos


def test_goto_line_equals_the_true_line_start(make_session, make_view, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    data = pretty_file.read_bytes()
    total = data.count(b"\n") + 1
    for line in (1, 2, 777, 20000, total):
        v.goto_line(line)
        start = naive_line_start(data, line)
        qtbot_wait(lambda: v.area.selection() is not None and v.area.selection()[0] == start)
        settle_view(v)
        row = row_containing(shown_rows(v), start) if start < len(data) else shown_rows(v)[-1]
        assert row is not None
        assert v.area.line_of_row(row[0]) == line
    idx = v.line_index
    assert idx is not None and idx.complete and idx.line_count == total
    # line numbers of the shown rows come from the index
    v.goto_offset(len(data) // 2)
    settle_view(v)
    qtbot_wait(v.area.anchor_usable)
    for b, _e in shown_rows(v):
        if rawdata.is_line_start(data, 0, b):
            assert v.area.line_of_row(b) == data.count(b"\n", 0, b) + 1


def qtbot_wait(cond, timeout: float = TIMEOUT / 1000) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not cond():
        QApplication.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        threading.Event().wait(0.002)


def test_goto_line_builds_the_line_index_first(make_session, make_view, app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rawview, "AUTO_LINE_INDEX_BYTES", 0)       # no automatic counting
    p = tmp_path / "lines.jsonl"
    p.write_bytes(b"".join(b'{"i": %d, "pad": "%s"}\n' % (i, b"x" * (i % 50)) for i in range(60000)))
    s = make_session(p)
    v = make_view(s)
    assert v.line_index is not None and not v.line_index.complete
    assert v.lines_button.isVisibleTo(v)
    added = []
    app.tasks.task_added.connect(added.append)
    data = p.read_bytes()
    v.goto_line(45678)
    start = naive_line_start(data, 45678)
    qtbot_wait(lambda: v.area.selection() is not None and v.area.selection()[0] == start)
    assert any(task.kind == "lineindex" for task in added)       # visible in the tasks panel
    assert v.line_index.complete
    settle_view(v)
    assert row_containing(shown_rows(v), start) is not None
    assert not v.lines_button.isVisibleTo(v)
    # a line past the end is reported
    v.goto_line(10 ** 6)
    qtbot_wait(lambda: v.message() is not None and "1 000 000" in v.message().replace(",", " "))
    assert v.info_bar.kind() == "warning"


def test_line_and_offset_fields(make_session, make_view, qtbot, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    data = pretty_file.read_bytes()
    v.offset_edit.setText("0x100")
    QTest.keyClick(v.offset_edit, Qt.Key.Key_Return)
    settle_view(v)
    assert v.area.selection() == (256, 257)
    v.offset_edit.setText("nope")
    QTest.keyClick(v.offset_edit, Qt.Key.Key_Return)
    assert v.message() and "nope" in v.message()
    v.offset_edit.setText(str(len(data) + 50))
    QTest.keyClick(v.offset_edit, Qt.Key.Key_Return)
    settle_view(v)
    assert v.area.selection() == (len(data) - 1, len(data))
    v.line_edit.setText("x1")
    QTest.keyClick(v.line_edit, Qt.Key.Key_Return)
    assert v.info_bar.kind() == "warning"
    v.line_edit.setText("10")
    QTest.keyClick(v.line_edit, Qt.Key.Key_Return)
    start = naive_line_start(data, 10)
    qtbot_wait(lambda: v.area.selection() == (start, start + 1))


# ----------------------------------------------------------------------------------
# error marker, formats
# ----------------------------------------------------------------------------------
def test_json_error_marker(make_session, make_view, tmp_path) -> None:
    p = tmp_path / "broken.json"
    good = ",\n".join(json.dumps({"i": i, "t": "matn"}) for i in range(2000))
    p.write_bytes(("[\n" + good + ',\n{"bad": 1 2}\n]').encode())
    s = make_session(p)
    assert s.doc.error is not None
    v = make_view(s)
    eo = s.doc.error_data_offset
    assert v.area.error_offset() == eo
    assert v.error_button.isVisibleTo(v)
    v.goto_error()
    settle_view(v)
    assert row_containing(shown_rows(v), eo) is not None
    assert v.area.selection() == (eo, eo + 1)
    assert p.read_bytes()[eo:eo + 1] == b"2"
    v.grab()                                       # paints the marker without errors


def test_jsonl_and_gzip_sources(make_session, make_view, tmp_path) -> None:
    lines = [json.dumps({"i": i, "s": "Ж" * (i % 7)}, ensure_ascii=False) for i in range(500)]
    p = tmp_path / "data.jsonl"
    p.write_bytes(("\n".join(lines) + "\n").encode())
    s = make_session(p)
    v = make_view(s)
    assert v.area.row_texts()[:5] == lines[:5]
    assert not v.prepared_label.isVisibleTo(v)
    gz = tmp_path / "data.json.gz"
    gz.write_bytes(gzip.compress(json.dumps(records(50), indent=1, ensure_ascii=False).encode()))
    s2 = make_session(gz)
    v2 = make_view(s2)
    assert v2.prepared_label.isVisibleTo(v2)
    assert "gzip" in v2.prepared_label.toolTip()
    assert v2.area.row_texts()[0] == "["
    assert v2.area.size == s2.doc.data_size


def test_empty_and_unopened(make_view, app, tmp_path) -> None:
    s = DocSession(app, tmp_path / "never.json")
    v = make_view(s, show=False)
    v.show()
    QApplication.processEvents()
    assert v.placeholder_text() == v.placeholder_text()     # no document: placeholder, no crash
    assert v.area.rows() == []
    v.goto_offset(10)
    v.goto_line(3)
    v.grab()
    v.area.reset(0)
    assert v.area.is_settled() and v.placeholder_text()
    s.close()


# ----------------------------------------------------------------------------------
# current node, activation, no GUI-thread reads
# ----------------------------------------------------------------------------------
def test_follows_the_current_node_only_while_visible(make_session, make_view, qtbot, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    node = s.doc.record_node(1500)
    s.select(NodeRef(1500, ()))
    qtbot.waitUntil(lambda: v.area.node_span() == (node.start, node.end), timeout=TIMEOUT)
    settle_view(v)
    assert row_containing(shown_rows(v), node.start) is not None
    v.hide()
    other = s.doc.record_node(10)
    s.select(NodeRef(10, ()))
    for _ in range(20):
        QApplication.processEvents()
        threading.Event().wait(0.005)
    assert v.area.node_span() == (node.start, node.end)        # hidden: nothing loaded
    v.show()
    v.activate()
    qtbot.waitUntil(lambda: v.area.node_span() == (other.start, other.end), timeout=TIMEOUT)
    settle_view(v)
    assert row_containing(shown_rows(v), other.start) is not None


def test_no_file_reads_in_the_gui_thread(make_session, make_view, qtbot, minified_file, monkeypatch) -> None:
    s = make_session(minified_file)
    calls: list[tuple[str, bool]] = []
    main = threading.main_thread()
    real_open = Document.open_data
    real_read = Document._read

    def open_data(self, *a, **kw):
        calls.append(("open", threading.current_thread() is main))
        return real_open(self, *a, **kw)

    def _read(self, start, end, *a, **kw):
        calls.append(("read", threading.current_thread() is main))
        return real_read(self, start, end, *a, **kw)

    monkeypatch.setattr(Document, "open_data", open_data)
    monkeypatch.setattr(Document, "_read", _read)
    sizes: list[int] = []
    real_pages = rawview.read_pages

    def read_pages(doc, pages, cancel=None, **kw):
        assert threading.current_thread() is not main
        out = real_pages(doc, pages, cancel, **kw)
        sizes.extend(len(b) for b in out.values())
        return out

    monkeypatch.setattr(rawview, "read_pages", read_pages)
    v = make_view(s)
    size = len(minified_file.read_bytes())
    for offset in (size // 2, 17, size - 1):
        v.goto_offset(offset)
        settle_view(v)
        v.area.scroll_rows(-30)
        settle_view(v)
    bar = v.area.verticalScrollBar()
    bar.setValue(bar.maximum() // 3)
    settle_view(v)
    s.select(NodeRef(5, ()))
    qtbot.waitUntil(lambda: v.area.node_span() is not None, timeout=TIMEOUT)
    v.build_line_index()
    qtbot.waitUntil(lambda: v.line_index.complete, timeout=TIMEOUT)
    v.goto_line(1)
    settle_view(v)
    v.area.set_selection(0, 3 * 65536)
    v.copy_selection()
    assert s.app.tasks.wait_idle(10)
    assert calls and not any(in_main for _kind, in_main in calls)
    assert sizes and max(sizes) <= rawview.PAGE_BYTES
    assert v.area.store.nbytes() <= rawview.MAX_PAGES * rawview.PAGE_BYTES


# ----------------------------------------------------------------------------------
# selection, copy, find, gutter
# ----------------------------------------------------------------------------------
def test_copy_selection(make_session, make_view, qtbot, minified_file) -> None:
    s = make_session(minified_file)
    v = make_view(s)
    data = minified_file.read_bytes()
    assert len(data) > COPY_CAP
    v.area.set_selection(10, 200)
    text = v.copy_selection()
    assert text == data[10:200].decode("utf-8")
    assert QGuiApplication.clipboard().text() == text
    # copy_value with a selection copies the bytes too (Ctrl+C of the main window)
    QGuiApplication.clipboard().setText("")
    v.copy_value("json")
    assert QGuiApplication.clipboard().text() == text
    # far away (not cached) and capped at 1 MiB
    v.area.set_selection(0, len(data))
    task = v.copy_selection()
    assert task is not None and not isinstance(task, str)
    qtbot.waitUntil(lambda: QGuiApplication.clipboard().text() != text, timeout=TIMEOUT)
    copied = QGuiApplication.clipboard().text()
    assert len(copied.encode("utf-8")) <= COPY_CAP
    assert data.decode("utf-8").startswith(copied) and len(copied.encode()) > COPY_CAP - 4
    assert v.message() is not None


def test_mouse_selection_and_ctrl_c(make_session, make_view, qtbot, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    data = pretty_file.read_bytes()
    vp = v.area.viewport()
    rh = v.area.row_height()
    x0 = v.area.gutter_width() + 20
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x0, rh // 2))
    QTest.mouseMove(vp, QPoint(x0 + 60, rh * 3 + rh // 2))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x0 + 60, rh * 3 + rh // 2))
    sel = v.area.selection()
    assert sel is not None and sel[0] < sel[1]
    rows = shown_rows(v)
    assert rows[0][0] <= sel[0] < rows[1][0] and rows[3][0] <= sel[1] <= rows[4][0]
    v.area.setFocus()
    QTest.keyClick(v.area, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == data[sel[0]:sel[1]].decode("utf-8")
    menu = v.build_context_menu(sel[0])
    names = {a.objectName() for a in menu.actions()}
    assert {"raw_copy", "raw_copy_offset"} <= names


def test_quick_find_in_loaded_pages(make_session, make_view, qtbot, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    v.quick_find()
    assert v.find_bar.isVisible()
    v.find_bar._edit.setText("Жук 12")
    v.find_bar.flush()
    matches = v.area.matches()
    region = v.area.store.region_around(0)
    assert region is not None
    text = region[1].decode("utf-8", errors="ignore")
    assert len(matches) == text.count("Жук 12") and len(matches) > 1
    data = pretty_file.read_bytes()
    for a, b in matches:
        assert data[a:b].decode("utf-8") == "Жук 12"
    first = v.area.selection()
    assert first == matches[0]
    assert v.find_bar.status_text().startswith("1")
    v.find_next()
    settle_view(v)
    assert v.area.selection() == matches[1]
    assert row_containing(shown_rows(v), matches[1][0]) is not None
    v.find_next(backwards=True)
    assert v.area.selection() == matches[0]
    v.grab()
    v.find_bar._edit.setText("no such text anywhere")
    v.find_bar.flush()
    assert v.area.matches() == []


def test_hex_gutter_zoom_and_paint(make_session, make_view, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    assert v.area.offset_text(255) == "255"
    v.set_hex(True)
    assert v.hex_button.isChecked()
    assert v.area.offset_text(255).endswith("FF") and v.area.offset_text(255).startswith("0")
    size = v.area.font_point_size()
    v.zoom(2)
    assert v.area.font_point_size() == size + 2
    settle_view(v)
    v.zoom(0)
    assert v.area.font_point_size() == size
    settle_view(v)
    assert shown_rows(v)[0][0] == 0
    img = v.grab()
    assert not img.isNull()


def test_reopen_resets_the_view(make_session, make_view, qtbot, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    v.goto_offset(100000)
    settle_view(v)
    old_doc = s.doc
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    assert s.doc is not old_doc
    settle_view(v)
    assert v.area.size == s.doc.data_size
    assert shown_rows(v)[0][0] == 0


# ----------------------------------------------------------------------------------
# a synthetic 5 GB single-line "file" (no disk): scaled scrollbar, bounded work
# ----------------------------------------------------------------------------------
PATTERN = ('{"k":"oʻzbek Жук \U0001F600","v":[1,2.5]},').encode("utf-8")


def synthetic(start: int, end: int) -> bytes:
    """Bytes [start, end) of an endless repetition of PATTERN (one line, no newline)."""
    n = len(PATTERN)
    first = start % n
    reps = (end - start + first) // n + 2
    return (PATTERN * reps)[first:first + (end - start)]


class FakeDoc:
    def __init__(self, size: int) -> None:
        self.data_size = size
        self.size = size
        self.error = None
        self.error_data_offset = None
        self.source_path = Path("synthetic.json")

        class Prepared:
            converted = False

        self.prepared = Prepared()

    def open_data(self, buffering: int = -1):   # pragma: no cover - read_pages is replaced
        raise AssertionError("no real reads")


class FakeSession:
    def __init__(self, app: AppContext, doc: FakeDoc) -> None:
        from PySide6.QtCore import QObject, Signal

        class Signals(QObject):
            current_changed = Signal(object, object)
            opened = Signal()
            view_changed = Signal()
            index_finished = Signal()
            state_changed = Signal(str)

        self._signals = Signals()
        for name in ("current_changed", "opened", "view_changed", "index_finished", "state_changed"):
            setattr(self, name, getattr(self._signals, name))
        self.app = app
        self.doc = doc
        self.current = None
        self.name = "synthetic.json"


def test_five_gigabyte_line_with_a_scaled_scrollbar(app, qtbot, monkeypatch) -> None:
    """Deterministic: a fixed font and size (no dependence on fonts or the application font
    other tests may leave behind), every wait is on the view's own state, and the reads are
    checked by locality (each page read lies next to a position the view showed)."""
    from jsonreshape.gui import theme

    size = 5 * 1000 ** 3 + 17
    pages_read: list[int] = []

    def read_pages(doc, pages, cancel=None, *, page_bytes=rawview.PAGE_BYTES):
        out = {}
        for p in pages:
            start = p * page_bytes
            if start < doc.data_size:
                out[p] = synthetic(start, min(doc.data_size, start + page_bytes))
                pages_read.append(p)
        return out

    monkeypatch.setattr(rawview, "read_pages", read_pages)
    s = FakeSession(app, FakeDoc(size))
    v = RawView(s)
    qtbot.addWidget(v)
    v.area.set_font(theme.mono_font(theme.default_mono_family(), 10.0))
    v.resize(900, 400)
    v.show()
    qtbot.waitExposed(v)
    settle_view(v)
    visited = [v.area.top()]
    bar = v.area.verticalScrollBar()
    assert bar.maximum() == 1 << 30                   # scaled: the byte range does not fit a C int
    for target in (4_500_000_123, 2 ** 31 + 5, 123_456_789):
        v.goto_offset(target, 3)
        settle_view(v)
        visited.append(v.area.top())
        rows = shown_rows(v)
        row = row_containing(rows, target)
        assert row is not None and row[0] <= target
        rule = v.area.rule()
        lo = rows[0][0] - rule.lookback - 16
        window = synthetic(lo, rows[-1][1] + 16)
        expect = rawdata.walk_rows(window, lo, rows[0][0], size, len(rows), rule)
        assert [b for b, _e in rows] == expect[:len(rows)]
        assert all(0 < e - b <= rule.w + 3 for b, e in rows)
        assert all("\ufffd" not in t for t in v.area.row_texts())
        vmax = v.area.max_top() or size
        assert abs(bar.value() - v.area.top() * (1 << 30) // vmax) <= 1
    bar.setValue(bar.maximum())
    settle_view(v)
    visited.append(v.area.top())
    end_top = v.area.top()
    assert shown_rows(v)[-1][1] == size and v.area.max_top() == end_top
    v.area.scroll_rows(-25)
    settle_view(v)
    visited.append(v.area.top())
    v.area.scroll_rows(25)
    settle_view(v)
    assert v.area.top() == end_top and shown_rows(v)[-1][1] == size
    bar.setValue(1000)
    settle_view(v)
    visited.append(v.area.top())
    mapped = v.area._value_to_offset(1000)
    assert 4000 < mapped < 5000
    rows = shown_rows(v)
    assert rows[0][0] <= mapped < rows[1][0]          # the first row contains the mapped offset
    # reads stay next to what was shown: never a scan of the 5 GB
    page = rawview.PAGE_BYTES
    reach = 3 * page + rawdata.need_back(v.area.page_rows(), v.area.rule())
    for p in pages_read:
        assert any(top - reach <= p * page <= top + reach for top in visited), p
    assert len(pages_read) <= len(visited) * (rawview.MAX_PAGES // 2 + 6)
    assert v.line_index is not None and not v.line_index.building     # never counted automatically
    v.shutdown()


# ----------------------------------------------------------------------------------
# rows follow the window width
# ----------------------------------------------------------------------------------
def test_row_width_follows_the_viewport(make_session, make_view, qtbot, minified_file) -> None:
    s = make_session(minified_file)
    v = make_view(s)
    data = minified_file.read_bytes()
    area = v.area

    def check_fit() -> None:
        w = area.row_bytes()
        assert w >= rawdata.MIN_ROW_BYTES
        assert (w + 1) * area._cw <= area.text_width() or w == rawdata.MIN_ROW_BYTES
        assert not area.horizontalScrollBar().isVisible()     # ASCII-wide text fits the pane
        rows = shown_rows(v)
        assert all(0 < e - b <= w + 3 for b, e in rows)
        truth = true_rows(data, v)
        i = truth.index(rows[0][0])
        assert [b for b, _e in rows] == truth[i:i + len(rows)]

    v.goto_offset(1_000_000)
    settle_view(v)
    check_fit()
    wide = area.row_bytes()
    first = area.top()
    v.resize(500, 420)
    qtbot.waitUntil(lambda: area.row_bytes() < wide, timeout=TIMEOUT)
    settle_view(v)
    check_fit()
    assert area.top() <= first < shown_rows(v)[0][1]          # the old first byte stays on the top row
    first = area.top()
    v.zoom(4)
    settle_view(v)
    check_fit()
    assert area.top() <= first < shown_rows(v)[0][1]
    narrow = area.row_bytes()
    v.set_hex(True)                                          # the gutter changes width too
    settle_view(v)
    check_fit()
    v.zoom(0)
    v.resize(1400, 420)
    qtbot.waitUntil(lambda: area.row_bytes() > narrow, timeout=TIMEOUT)
    settle_view(v)
    check_fit()
    # scrolling stays consistent with the new width in both directions
    start = shown_rows(v)
    v.area.scroll_rows(300)
    settle_view(v)
    v.area.scroll_rows(-300)
    settle_view(v)
    assert shown_rows(v) == start


def test_tiny_window_uses_the_minimum_row_width(make_session, make_view, qtbot, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    v.zoom(40)                                   # huge glyphs: fewer than 32 fit the pane
    assert v.area.text_width() / v.area._cw < rawdata.MIN_ROW_BYTES
    assert v.area.row_bytes() == rawdata.MIN_ROW_BYTES
    settle_view(v)
    rows = shown_rows(v)
    assert rows and all(e - b <= rawdata.MIN_ROW_BYTES + 3 for b, e in rows)
    v.grab()                                     # wider than the pane: the horizontal scrollbar appears
    QApplication.processEvents()
    assert v.area.horizontalScrollBar().maximum() > 0


def test_explicit_goto_wins_over_following_the_node(make_session, make_view, qtbot, pretty_file) -> None:
    """The document tab switches a pane to the raw view and calls goto_offset (JSON error,
    "open in raw view"): the current node resolved afterwards must not move the view away."""
    s = make_session(pretty_file)
    v = make_view(s, show=False)
    far = s.doc.record_node(2500)
    s.select(NodeRef(2500, ()))
    v.goto_offset(300, 4)
    v.show()
    v.activate()
    qtbot.waitUntil(lambda: v.area.node_span() == (far.start, far.end), timeout=TIMEOUT)
    settle_view(v)
    assert row_containing(shown_rows(v), 300) is not None
    assert row_containing(shown_rows(v), far.start) is None
    assert v.area.selection() == (300, 304)
    # a later selection elsewhere is followed again
    near = s.doc.record_node(2000)
    s.select(NodeRef(2000, ()))
    qtbot.waitUntil(lambda: v.area.node_span() == (near.start, near.end), timeout=TIMEOUT)
    settle_view(v)
    assert row_containing(shown_rows(v), near.start) is not None
    # scrolling while a node is being resolved keeps the user's position
    target = s.doc.record_node(100)
    s.select(NodeRef(100, ()))
    v.area.scroll_rows(5)
    qtbot.waitUntil(lambda: v.area.node_span() == (target.start, target.end), timeout=TIMEOUT)
    settle_view(v)
    assert row_containing(shown_rows(v), target.start) is None


def test_selected_text_and_signal(make_session, make_view, qtbot, pretty_file) -> None:
    s = make_session(pretty_file)
    v = make_view(s)
    data = pretty_file.read_bytes()
    assert v.selected_text() == ""
    with qtbot.waitSignal(v.selection_changed, timeout=2000):
        v.area.set_selection(4, 40)
    assert v.selected_text() == data[4:40].decode("utf-8")
    v.area.set_selection(0, len(data))                       # too big / not loaded: no prefill
    assert v.selected_text() == ""
