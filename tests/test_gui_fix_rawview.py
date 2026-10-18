"""Fix round for the raw view (jsonreshape/gui/rawview.py).

* P-F6: pages are read by the view's reader thread and delivered at once by a queued
  signal (not task-poll latency): a scrollbar drag paints the positions it passes, the
  first show settles without waiting for the task manager;
* L2: a row scroll larger than the page cache walks in cache-sized steps and settles;
* T6: a reopen cancels the old line count, so go to line works on the new document;
* C7: the data font follows ``app.settings_changed`` keeping the zoom.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape.gui import rawdata, rawview  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.rawview import RawView  # noqa: E402
from jsonreshape.gui.session import DocSession  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 20000


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
    views: list[RawView] = []

    def factory(session: DocSession, *, width: int = 900, height: int = 420) -> RawView:
        v = RawView(session)
        qtbot.addWidget(v)
        v.resize(width, height)
        views.append(v)
        v.show()
        qtbot.waitExposed(v)
        qtbot.waitUntil(v.is_settled, timeout=TIMEOUT)
        return v

    yield factory
    for v in views:
        v.shutdown()


@pytest.fixture
def one_line_file(tmp_path: Path) -> Path:
    """A ~4 MB single-line file."""
    p = tmp_path / "one.json"
    p.write_bytes(json.dumps([{"i": i, "s": "abcdefgh" * 3} for i in range(90_000)]).encode())
    return p


def expected_top(data: bytes, top: int, n: int, rule) -> int:
    buf_lo = max(0, top - rule.lookback - 8)
    hi = min(len(data), top + rawdata.need_forward(n, rule))
    walked = rawdata.walk_rows(data[buf_lo:hi], buf_lo, top, len(data), n, rule)
    return walked[-1]


def test_a_long_row_scroll_settles(make_session, make_view, qtbot, one_line_file) -> None:
    s = make_session(one_line_file)
    v = make_view(s, width=1800)
    area = v.area
    data = one_line_file.read_bytes()
    area.goto_top_offset(1_000_000)
    qtbot.waitUntil(v.is_settled, timeout=TIMEOUT)
    rule = area.rule()
    n = 4 * area.max_walk_rows() + 7                      # far more than the page cache holds
    assert rawdata.need_forward(n, rule) > rawview.MAX_PAGES * rawview.PAGE_BYTES
    top0 = area.top()
    reads = []
    real = rawview.read_pages
    rawview.read_pages = lambda *a, **k: (reads.append(1), real(*a, **k))[1]
    try:
        area.scroll_rows(n)
        qtbot.waitUntil(v.is_settled, timeout=5000)
    finally:
        rawview.read_pages = real
    assert area.top() == expected_top(data, top0, n, rule)
    assert len(reads) < 60                               # no page thrash
    # upwards as well, then back where it started
    area.scroll_rows(-n)
    qtbot.waitUntil(v.is_settled, timeout=5000)
    assert area.top() == top0


def test_pages_arrive_without_task_poll_latency(make_session, make_view, qtbot, app, one_line_file) -> None:
    s = make_session(one_line_file)
    app.tasks.set_poll_interval(2000)                    # task results would take 2 s
    v = make_view(s)
    area = v.area
    main = threading.main_thread()
    threads: list[str] = []
    real = rawview.read_pages

    def read_pages(*a, **k):
        assert threading.current_thread() is not main
        threads.append(threading.current_thread().name)
        return real(*a, **k)

    rawview.read_pages = read_pages
    try:
        t0 = time.monotonic()
        v.goto_offset(3_000_000)
        qtbot.waitUntil(v.is_settled, timeout=5000)
        assert time.monotonic() - t0 < 1.0
        assert area.rows() and area.rows()[0][0] <= 3_000_000 < area.rows()[-1][1]
        # a scrollbar drag paints the positions it passes
        bar = area.verticalScrollBar()
        shown = set()
        for i in range(20):
            bar.setValue(bar.maximum() * i // 20)
            deadline = time.monotonic() + 0.5
            while not area.rows() or area.is_settled() is False:
                QApplication.processEvents()
                if time.monotonic() > deadline:
                    break
            if area.rows():
                shown.add(area.top())
        assert len(shown) >= 18
    finally:
        rawview.read_pages = real
    assert threads and all(name == "jsonreshape-raw-pages" for name in threads)    # never a task


def test_reopen_cancels_the_old_line_count(make_session, make_view, qtbot, app, tmp_path, monkeypatch) -> None:
    p = tmp_path / "lines.jsonl"
    p.write_bytes(b"".join(b'{"i": %d}\n' % i for i in range(5000)))
    s = make_session(p)
    release = threading.Event()
    real_build = rawdata.LineIndex._build
    started = threading.Event()

    def slow_build(self, f, progress, cancel, block_bytes, on_block):
        if not release.is_set():
            started.set()
            while not release.is_set():          # the old count "takes forever" until cancelled
                if cancel is not None:
                    cancel.check()
                time.sleep(0.005)
        return real_build(self, f, progress, cancel, block_bytes, on_block)

    monkeypatch.setattr(rawdata.LineIndex, "_build", slow_build)
    monkeypatch.setattr(rawview, "AUTO_LINE_INDEX_BYTES", 0)
    v = make_view(s)
    v.goto_line(4000)
    assert started.wait(5)
    old_task = v._index_task
    assert old_task is not None
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    qtbot.waitUntil(lambda: s.doc is not None and v._doc is s.doc, timeout=TIMEOUT)
    assert v._index_task is not old_task
    qtbot.waitUntil(lambda: old_task.state == "cancelled", timeout=TIMEOUT)
    release.set()
    v.goto_line(20)
    qtbot.waitUntil(lambda: v.area.selection() is not None and v.is_settled(), timeout=TIMEOUT)
    sel = v.area.selection()
    assert sel is not None and p.read_bytes()[sel[0]:sel[0] + 9] == b'{"i": 19}'


def test_data_font_follows_settings_keeping_zoom(make_session, make_view, app, tmp_path) -> None:
    p = tmp_path / "a.json"
    p.write_text('{"a": 1}')
    s = make_session(p)
    v = make_view(s)
    v.zoom(3)
    app.state.set("mono_size", 14)
    app.settings_changed.emit()
    assert v.area.font_point_size() == 17
    v.zoom(0)
    assert v.area.font_point_size() == 14
