"""StatusBar (jsonreshape/gui/statusbar.py; AGENT_PROMPT §11 "Status qatori"): file size
(exact size in the tooltip), live record count (indexing percentage, filtered "shown /
total", partial documents, single values), format + record path, encoding (+ gzip,
UTF-16), current path, selection, the byte range of the current node (resolved in a
task), process memory, running tasks (click → tasks dock), temporary messages, tab
switching."""

from __future__ import annotations

import codecs
import gzip
import json
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt, Signal

from guishell_helpers import TIMEOUT, make_app, records, stub_registry, wait_ready, wrapped_file, write_json
from jsonreshape.gui.doctab import DocumentTab
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.gui.statusbar import StatusBar, encoding_text

pytestmark = pytest.mark.gui


@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    yield ctx
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def bar(qtbot, app):
    sb = StatusBar(app)
    qtbot.addWidget(sb)
    yield sb
    sb.shutdown()


@pytest.fixture
def make_tab(qtbot, app):
    tabs: list[DocumentTab] = []

    def factory(path, **kw) -> DocumentTab:
        session = DocSession(app, path, use_cache=False, **kw)
        tab = DocumentTab(session, view_registry=stub_registry())
        qtbot.addWidget(tab)
        tabs.append(tab)
        session.start()
        wait_ready(qtbot, session)
        return tab

    yield factory
    for tab in tabs:
        tab.shutdown()
    assert app.tasks.wait_idle(30)


# ----------------------------------------------------------------------------------------
def test_encoding_text(lang_uz) -> None:
    assert encoding_text("utf-8") == "UTF-8"
    assert encoding_text("utf-8-bom") == "UTF-8 BOM"
    assert encoding_text("utf-16-le", None) == "UTF-16 LE"
    assert encoding_text("utf-8", "gzip") == "UTF-8 · gzip"


def test_document_texts(qtbot, app, bar, make_tab, tmp_path) -> None:
    path = write_json(tmp_path / "a.json", records(1234))
    tab = make_tab(path)
    bar.set_tab(tab)
    size = path.stat().st_size
    assert bar.size_label.full_text() == f"{size / 1000:.1f} KB"
    assert f"{size:,}".replace(",", " ") in bar.size_label.tip()
    assert bar.count_label.full_text() == "1 234 yozuv"
    assert bar.count_label.tip() == "Barcha yozuvlar"
    assert bar.format_label.full_text() == "JSON massiv"
    assert "avtomatik" in bar.format_label.tip()
    assert bar.encoding_label.full_text() == "UTF-8"
    assert bar.path_label.full_text() == ""
    assert not bar.offset_label.isVisibleTo(bar)
    assert not bar.selection_label.isVisibleTo(bar)


def test_wrapped_format_shows_the_record_path(qtbot, app, bar, make_tab, tmp_path) -> None:
    tab = make_tab(wrapped_file(tmp_path / "w.json", 50))
    bar.set_tab(tab)
    assert bar.format_label.full_text() == "Obyekt ichidagi massiv · data"
    assert bar.count_label.full_text() == "50 yozuv"


def test_single_value(qtbot, app, bar, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "s.json", {"a": 1, "b": [1, 2]}))
    bar.set_tab(tab)
    assert bar.count_label.full_text() == "Bitta qiymat"


def test_gzip_and_utf16(qtbot, app, bar, make_tab, tmp_path) -> None:
    gz = tmp_path / "g.json.gz"
    raw = json.dumps(records(30)).encode()
    gz.write_bytes(gzip.compress(raw))
    tab = make_tab(gz)
    bar.set_tab(tab)
    assert bar.encoding_label.full_text() == "UTF-8 · gzip"
    assert bar.encoding_label.tip()                      # the working copy
    assert "O'qilayotgan" in bar.size_label.tip()        # decompressed size differs
    u16 = tmp_path / "u.json"
    u16.write_bytes(codecs.BOM_UTF16_LE + json.dumps(records(5)).encode("utf-16-le"))
    tab2 = make_tab(u16)
    bar.set_tab(tab2)
    assert bar.encoding_label.full_text() == "UTF-16 LE"
    assert bar.count_label.full_text() == "5 yozuv"


def test_filtered_view_count(qtbot, app, bar, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(300)))
    bar.set_tab(tab)
    s = tab.session
    s.set_filter(FilterSpec("id < 25"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 25, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: bar.count_label.full_text() == "25 / 300 yozuv", timeout=TIMEOUT)
    assert bar.count_label.tip() == "Filtr: id < 25"
    s.reset_view()
    assert bar.count_label.full_text() == "300 yozuv"


def test_partial_document(qtbot, app, bar, make_tab, tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('[{"a": 1}, {"a": 2}, {"a": 3}, {"a": ]', encoding="utf-8")
    tab = make_tab(path)
    bar.set_tab(tab)
    assert bar.count_label.full_text() == "3 yozuv (xatogacha)"


def test_current_path_and_offset(qtbot, app, bar, make_tab, tmp_path) -> None:
    path = write_json(tmp_path / "a.json", records(40))
    tab = make_tab(path)
    bar.set_tab(tab)
    s = tab.session
    s.select(NodeRef(7, ("contact", "address")))
    assert bar.path_label.full_text() == "#7.contact.address"
    qtbot.waitUntil(lambda: bar.offset_span() is not None, timeout=TIMEOUT)
    start, end = bar.offset_span()
    data = path.read_bytes()
    assert data[start:end] == b'"street 7"'
    assert bar.offset_label.full_text().startswith("Bayt ")
    assert bar.path_label.tip() == "[7].contact.address"
    # a node that does not exist: no range
    s.select(NodeRef(7, ("nope",)))
    qtbot.waitUntil(lambda: bar.offset_span() is None, timeout=TIMEOUT)
    assert bar.path_label.full_text() == "#7.nope"
    s.select(None)
    assert bar.path_label.full_text() == "" and bar.offset_span() is None


def test_rapid_selection_changes_resolve_the_last_one(qtbot, app, bar, make_tab, tmp_path) -> None:
    path = write_json(tmp_path / "a.json", records(200))
    tab = make_tab(path)
    bar.set_tab(tab)
    for i in range(100):
        tab.session.select(NodeRef(i, ("id",)))
    qtbot.waitUntil(lambda: bar.offset_span() is not None, timeout=TIMEOUT)
    start, end = bar.offset_span()
    assert path.read_bytes()[start:end] == b"99"


def test_selection_count(qtbot, app, bar, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    bar.set_tab(tab)
    view = tab.active_view()
    view.selected = 5
    tab.selection_changed.emit()
    assert bar.selection_label.full_text() == "5 ta tanlangan"
    assert bar.selection_label.isVisibleTo(bar)
    view.selected = 1
    tab.active_view_changed.emit()
    assert not bar.selection_label.isVisibleTo(bar)


def test_memory_label(qtbot, app, bar) -> None:
    # psutil is imported in a silent task since R2-22 (not on the GUI thread at start-up)
    qtbot.waitUntil(lambda: bar._process is not None, timeout=TIMEOUT)
    bar.refresh_memory()
    text = bar.memory_label.full_text()
    assert text.startswith("RAM ") and text.endswith("B")
    assert "Xotira chegarasi" in bar.memory_label.tip()
    limit_text = f"{app.settings.memory_limit_bytes / 1e6:.1f} MB"
    assert limit_text in bar.memory_label.tip()


def test_tasks_button(qtbot, app, bar) -> None:
    gate = threading.Event()

    def fn(ctx):
        ctx.progress.set_total(100)
        ctx.progress.update(40)
        ctx.progress.set_message("x")
        while not gate.is_set():
            ctx.check()
            time.sleep(0.005)

    assert not bar.tasks_button.isVisibleTo(bar)
    task = app.tasks.run(fn, title="T", kind="test")
    qtbot.waitUntil(lambda: bar.tasks_button.text() == "1 ta vazifa · 40 %", timeout=TIMEOUT)
    assert bar.tasks_button.isVisibleTo(bar)
    with qtbot.waitSignal(bar.tasks_clicked, timeout=TIMEOUT):
        qtbot.mouseClick(bar.tasks_button, Qt.MouseButton.LeftButton)
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not bar.tasks_button.isVisibleTo(bar), timeout=TIMEOUT)


def test_status_message(qtbot, app, bar) -> None:
    app.show_status("Nusxalandi", 3000)
    assert bar.currentMessage() == "Nusxalandi"


class FakeSession(QObject):
    state_changed = Signal(str)
    opened = Signal()
    index_finished = Signal()
    warnings_changed = Signal()
    count_changed = Signal()
    view_changed = Signal()
    view_busy_changed = Signal(bool)
    current_changed = Signal(object, object)

    def __init__(self, app, *, state="indexing", count=1234567, view=1234567, base=True, busy=False,
                 error=None, kind="array") -> None:
        super().__init__()
        self.app = app
        self.state = state
        self.view_busy = busy
        self.view_is_base = base
        self._view = view
        self.current = None
        info = SimpleNamespace(label="JSON massiv", record_path=None, record_path_text=None, auto=True,
                               encoding="utf-8", compression=None)
        self.doc = SimpleNamespace(record_count=count, kind=kind, error=error, size=10**9, data_size=10**9,
                                   info=info, appended_bytes=0, broken_count=0, data_path="a", source_path="a")

    def view_len(self) -> int:
        return self._view

    def view_label(self) -> str:
        return "Barcha yozuvlar"


def _fake_tab(session):
    return SimpleNamespace(session=session, active_view=lambda: None)


def test_count_text_while_indexing(qtbot, app, bar) -> None:
    s = FakeSession(app)
    bar.set_tab(_fake_tab(s))
    assert bar.count_label.full_text() == "1 234 567 yozuv (indekslanmoqda…)"
    gate = threading.Event()

    def fn(ctx):
        ctx.progress.set_total(1000)
        ctx.progress.update(450)
        ctx.progress.set_message("i")
        gate.wait(10)

    task = app.tasks.run(fn, title="index", kind="index", owner=s)
    qtbot.waitUntil(lambda: bar.count_label.full_text() == "1 234 567 yozuv (indekslanmoqda 45 %)", timeout=TIMEOUT)
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    s.state = "ready"
    s.view_is_base = False
    s._view = 12345
    s.count_changed.emit()
    assert bar.count_label.full_text() == "12 345 / 1 234 567 yozuv"
    s.view_busy = True
    s.view_busy_changed.emit(True)
    assert bar.count_label.full_text() == "12 345 / 1 234 567 yozuv · hisoblanmoqda…"
    assert bar.size_label.full_text() == "1.0 GB"


def test_opening_and_failed_states(qtbot, app, bar) -> None:
    s = FakeSession(app, state="opening")
    s.doc = None
    bar.set_tab(_fake_tab(s))
    assert bar.count_label.full_text() == "Ochilmoqda…"
    assert not bar.size_label.isVisibleTo(bar)
    s.state = "failed"
    s.state_changed.emit("failed")
    assert bar.count_label.full_text() == "Ochilmadi"


def test_tab_switching_disconnects_the_old_session(qtbot, app, bar, make_tab, tmp_path) -> None:
    a = make_tab(write_json(tmp_path / "a.json", records(10)))
    b = make_tab(write_json(tmp_path / "b.json", records(20)))
    bar.set_tab(a)
    assert bar.count_label.full_text() == "10 yozuv"
    bar.set_tab(b)
    assert bar.count_label.full_text() == "20 yozuv"
    a.session.select(NodeRef(1, ()))
    assert bar.path_label.full_text() == ""          # the old tab no longer updates the bar
    b.session.select(NodeRef(2, ()))
    assert bar.path_label.full_text() == "#2"
    bar.set_tab(None)
    assert bar.count_label.full_text() == "" and not bar.format_label.isVisibleTo(bar)
