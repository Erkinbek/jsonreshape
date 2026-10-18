"""TasksPanel (jsonreshape/gui/taskspanel.py; ARCHITECTURE §11.8, AGENT_PROMPT §11 "Fon
vazifalari paneli"): running tasks with progress / rate / ETA / state, indeterminate
progress, per-task cancel, cancel all (silent tasks untouched), finished tasks kept for a
while (failed in red with the error text), the "keep finished" toggle and "clear"."""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import Qt

from guishell_helpers import TIMEOUT, make_app
from jsonreshape.errors import UserInputError
from jsonreshape.gui import taskspanel as tp
from jsonreshape.gui.qtasks import state_text
from jsonreshape.gui.taskspanel import TasksPanel, error_text, progress_texts
from jsonreshape.tasks import ProgressInfo

pytestmark = pytest.mark.gui


@pytest.fixture
def app(qapp, tmp_path):
    ctx = make_app(tmp_path)
    yield ctx
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def panel(qtbot, app):
    p = TasksPanel(app)
    qtbot.addWidget(p)
    return p


def info(done=0, total=None, rate=0.0, eta=None, unit="bytes", message="", finished=False) -> ProgressInfo:
    fraction = None if not total else min(1.0, done / total)
    return ProgressInfo(done=done, total=total, fraction=fraction, rate=rate, eta=eta, elapsed=1.0,
                        message=message, unit=unit, finished=finished)


# ----------------------------------------------------------------------------------------
# pure helpers
# ----------------------------------------------------------------------------------------
def test_progress_texts_bytes_with_total(lang_uz) -> None:
    done, rate, eta = progress_texts(info(done=12_300_000, total=1_200_000_000, rate=45_000_000, eta=26.0))
    assert done == "12.3 MB / 1.2 GB"
    assert rate == "45.0 MB/s"
    assert eta == "26 s"


def test_progress_texts_unknown_total_and_counts(lang_uz) -> None:
    done, rate, eta = progress_texts(info(done=5_000_000, total=None, rate=1_000_000))
    assert done == "5.0 MB" and rate == "1.0 MB/s" and eta == ""
    done, rate, eta = progress_texts(info(done=1234567, total=2000000, rate=1500.4, eta=500, unit="records"))
    assert done == "1 234 567 / 2 000 000"
    assert rate == "1 500/s"
    assert eta == "8:20"
    assert progress_texts(None) == ("", "", "")
    # finished: no ETA; nothing done yet: no rate
    assert progress_texts(info(done=10, total=10, rate=5, eta=0.0, finished=True))[2] == ""
    assert progress_texts(info(done=0, total=10, rate=0.0))[1] == ""


def test_error_text(lang_uz) -> None:
    assert error_text(None) == ""
    assert error_text(UserInputError("gui.session.not_open", name="a.json")) == "a.json fayli hali ochilmagan."
    assert error_text(ValueError("boom")) == "ValueError: boom"
    assert error_text(KeyError()) == "KeyError"


# ----------------------------------------------------------------------------------------
# the panel
# ----------------------------------------------------------------------------------------
def _progress_task(app, gate: threading.Event, *, total: int | None, title: str = "Ishlov", unit: str = "bytes",
                   silent: bool = False, fail: bool = False):
    def fn(ctx):
        if total is not None:
            ctx.progress.set_total(total)
        ctx.progress.update((total or 1000) // 4)
        ctx.progress.set_message("bosqich 1")      # forces a report (updates are throttled)
        while not gate.is_set():
            ctx.check()
            time.sleep(0.005)
        if fail:
            raise UserInputError("gui.session.not_open", name="x.json")
        return 1

    return app.tasks.run(fn, title=title, kind="test", unit=unit, silent=silent, on_error=lambda exc: None)


def test_running_task_shows_progress_and_finishes(qtbot, app, panel, lang_uz) -> None:
    gate = threading.Event()
    task = _progress_task(app, gate, total=4_000_000)
    item = panel.item_for(task)
    assert item is not None and panel.row_count() == 1 and panel.running_count() == 1
    assert not panel.empty_label.isVisibleTo(panel)
    bar = panel.progress_bar(task)
    qtbot.waitUntil(lambda: bar.maximum() == tp._BAR_STEPS and bar.value() == tp._BAR_STEPS // 4, timeout=TIMEOUT)
    assert item.text(tp.COL_DONE) == "1.0 MB / 4.0 MB"
    assert item.text(tp.COL_STATE) == state_text("running")
    assert item.text(tp.COL_TITLE) == "Ishlov — bosqich 1"
    assert item.text(tp.COL_RATE).endswith("/s")
    assert panel.cancel_all_button.isEnabled() and not panel.clear_button.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert item.text(tp.COL_STATE) == state_text("done")
    assert bar.value() == tp._BAR_STEPS
    assert panel.cancel_button(task).isHidden()
    assert panel.running_count() == 0 and panel.row_count() == 1   # kept for a while
    assert panel.clear_button.isEnabled() and not panel.cancel_all_button.isEnabled()


def test_unknown_total_is_indeterminate(qtbot, app, panel) -> None:
    gate = threading.Event()
    task = _progress_task(app, gate, total=None)
    qtbot.waitUntil(lambda: task.info() is not None, timeout=TIMEOUT)
    panel._on_updated(task)
    assert panel.progress_bar(task).maximum() == 0          # busy indicator
    assert panel.item_for(task).text(tp.COL_ETA) == ""
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert panel.progress_bar(task).maximum() == tp._BAR_STEPS


def test_cancel_button_cancels_the_task(qtbot, app, panel, lang_uz) -> None:
    gate = threading.Event()
    task = _progress_task(app, gate, total=100)
    button = panel.cancel_button(task)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert task.cancel_requested
    assert not button.isEnabled()
    assert panel.item_for(task).text(tp.COL_STATE) == "Bekor qilinmoqda…"
    qtbot.waitUntil(lambda: task.state == "cancelled", timeout=TIMEOUT)
    assert panel.item_for(task).text(tp.COL_STATE) == state_text("cancelled")


def test_cancel_all_skips_silent_tasks(qtbot, app, panel) -> None:
    gate = threading.Event()
    a = _progress_task(app, gate, total=100, title="A")
    b = _progress_task(app, gate, total=None, title="B")
    silent = _progress_task(app, gate, total=100, title="S", silent=True)
    assert set(panel.task_ids()) == {a.id, b.id}        # the silent one is not listed
    panel.cancel_all_button.click()
    assert a.cancel_requested and b.cancel_requested
    assert not silent.cancel_requested
    qtbot.waitUntil(lambda: a.state == "cancelled" and b.state == "cancelled", timeout=TIMEOUT)
    gate.set()
    qtbot.waitUntil(lambda: silent.state == "done", timeout=TIMEOUT)


def test_failed_task_is_red_with_the_error_text(qtbot, app, panel, lang_uz) -> None:
    gate = threading.Event()
    task = _progress_task(app, gate, total=10, fail=True)
    gate.set()
    qtbot.waitUntil(lambda: task.state == "failed", timeout=TIMEOUT)
    item = panel.item_for(task)
    assert item.text(tp.COL_STATE) == state_text("failed")
    assert item.toolTip(tp.COL_STATE) == "x.json fayli hali ochilmagan."
    assert "x.json fayli hali ochilmagan." in item.toolTip(tp.COL_TITLE)
    assert item.foreground(tp.COL_STATE).color() == panel._danger_color()


def test_finished_rows_disappear_after_a_while(qtbot, app, panel, monkeypatch) -> None:
    monkeypatch.setattr(tp, "KEEP_FINISHED_MS", 50)
    gate = threading.Event()
    gate.set()
    tasks = [_progress_task(app, gate, total=10, title=f"T{i}") for i in range(3)]
    qtbot.waitUntil(lambda: all(t.state == "done" for t in tasks), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: panel.row_count() == 0, timeout=TIMEOUT)
    assert panel.empty_label.isVisibleTo(panel)


def test_keep_finished_and_clear(qtbot, app, panel, monkeypatch) -> None:
    monkeypatch.setattr(tp, "KEEP_FINISHED_MS", 10)
    panel.keep_check.setChecked(True)
    assert panel.keep_finished() and app.state.get("tasks_keep_finished") is True
    gate = threading.Event()
    gate.set()
    task = _progress_task(app, gate, total=10)
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    qtbot.wait(150)
    assert panel.row_count() == 1                        # kept
    panel.clear_button.click()
    assert panel.row_count() == 0
    # a second panel starts with the remembered choice
    other = TasksPanel(app)
    qtbot.addWidget(other)
    assert other.keep_finished()


def test_tasks_running_before_the_panel_are_listed(qtbot, app) -> None:
    gate = threading.Event()
    task = _progress_task(app, gate, total=100, title="early")
    panel = TasksPanel(app)
    qtbot.addWidget(panel)
    assert panel.task_ids() == [task.id]
    assert panel.item_for(task).text(tp.COL_TITLE).startswith("early")
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)


def test_count_changed_signal(qtbot, app, panel) -> None:
    seen: list[int] = []
    panel.count_changed.connect(seen.append)
    gate = threading.Event()
    task = _progress_task(app, gate, total=10)
    assert seen and seen[-1] == 1
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert seen[-1] == 0


def test_many_tasks(qtbot, app, panel) -> None:
    gate = threading.Event()
    tasks = [_progress_task(app, gate, total=1000, title=f"T{i}") for i in range(40)]
    assert panel.row_count() == 40
    gate.set()
    qtbot.waitUntil(lambda: all(t.state == "done" for t in tasks), timeout=TIMEOUT)
    assert panel.running_count() == 0
