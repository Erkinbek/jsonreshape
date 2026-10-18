"""TaskManager / GuiTask / TaskContext (jsonreshape/gui/qtasks.py, ARCHITECTURE §11.2).

Ordering (every posted item before the final callback, in order), exactly one final
callback, cancellation, errors (on_error / error signal), callback exceptions, detach,
silent tasks, progress info, wait_idle, the poll timer stopping when idle.
"""

from __future__ import annotations

import threading
import time

import pytest

from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.gui.qtasks import GuiTask, TaskManager, state_text

pytestmark = pytest.mark.gui

TIMEOUT = 20000   # ms: generous for loaded machines


@pytest.fixture
def tm(qapp):
    manager = TaskManager()
    yield manager
    manager.cancel_all()
    assert manager.wait_idle(20)


def test_done_result_signals_and_idle_timer(qtbot, tm: TaskManager) -> None:
    added, finished, results = [], [], []
    tm.task_added.connect(added.append)
    tm.task_finished.connect(finished.append)
    task = tm.run(lambda ctx: 6 * 7, title="answer", kind="calc", on_done=results.append)
    assert isinstance(task, GuiTask)
    assert task.state == "running" and task.title == "answer" and task.kind == "calc"
    assert tm.tasks() == [task]
    assert added == [task]
    qtbot.waitUntil(lambda: results == [42], timeout=TIMEOUT)
    assert task.state == "done" and task.result == 42 and task.error is None
    assert finished == [task]
    assert tm.tasks() == []
    qtbot.waitUntil(lambda: not tm._timer.isActive(), timeout=TIMEOUT)


def test_items_are_delivered_in_order_before_done(qtbot, tm: TaskManager) -> None:
    events: list = []
    n = 5000

    def fn(ctx):
        for i in range(n):
            ctx.post(i)
            if i % 1000 == 0:
                time.sleep(0.01)
        return "end"

    tm.run(fn, title="items", on_item=events.append, on_done=lambda r: events.append(("done", r)))
    qtbot.waitUntil(lambda: bool(events) and events[-1] == ("done", "end"), timeout=TIMEOUT)
    assert events[:-1] == list(range(n))
    assert events.count(("done", "end")) == 1


def test_items_before_cancel_and_error_callbacks(qtbot, tm: TaskManager) -> None:
    events: list = []

    def failing(ctx):
        ctx.post("a")
        ctx.post("b")
        raise ValueError("boom")

    def cancelled(ctx):
        ctx.post("x")
        raise Cancelled()

    tm.run(failing, title="f", on_item=events.append, on_error=lambda e: events.append(("error", str(e))))
    qtbot.waitUntil(lambda: ("error", "boom") in events, timeout=TIMEOUT)
    assert events == ["a", "b", ("error", "boom")]
    events.clear()
    tm.run(cancelled, title="c", on_item=events.append, on_cancel=lambda: events.append("cancel"),
           on_error=lambda e: events.append("error"))
    qtbot.waitUntil(lambda: "cancel" in events, timeout=TIMEOUT)
    assert events == ["x", "cancel"]


def test_cancel_calls_on_cancel_only(qtbot, tm: TaskManager) -> None:
    calls: list = []
    started = threading.Event()

    def fn(ctx):
        started.set()
        while True:
            ctx.check()
            time.sleep(0.005)

    errors: list = []
    tm.error.connect(lambda exc, task: errors.append(exc))
    task = tm.run(fn, title="loop", on_done=lambda r: calls.append("done"), on_error=lambda e: calls.append("error"),
                  on_cancel=lambda: calls.append("cancel"))
    assert started.wait(10)
    assert not task.cancel_requested
    task.cancel()
    assert task.cancel_requested
    qtbot.waitUntil(lambda: calls == ["cancel"], timeout=TIMEOUT)
    assert task.state == "cancelled"
    assert errors == []


def test_error_after_cancel_counts_as_cancelled(qtbot, tm: TaskManager) -> None:
    calls: list = []
    go = threading.Event()

    def fn(ctx):
        go.wait(10)
        raise OSError("handle closed while cancelling")

    task = tm.run(fn, title="t", on_error=lambda e: calls.append("error"), on_cancel=lambda: calls.append("cancel"))
    task.cancel()
    go.set()
    qtbot.waitUntil(lambda: calls == ["cancel"], timeout=TIMEOUT)


def test_result_despite_cancel_request_is_done(qtbot, tm: TaskManager) -> None:
    calls: list = []
    go = threading.Event()

    def fn(ctx):
        go.wait(10)
        return 1

    task = tm.run(fn, title="t", on_done=calls.append, on_cancel=lambda: calls.append("cancel"))
    task.cancel()
    go.set()
    qtbot.waitUntil(lambda: calls == [1], timeout=TIMEOUT)
    assert task.state == "done"


def test_failure_without_handler_goes_to_error_signal(qtbot, tm: TaskManager) -> None:
    errors: list = []
    tm.error.connect(lambda exc, task: errors.append((exc, task)))
    exc = UserInputError("gui.session.single_no_view")

    def fn(ctx):
        raise exc

    task = tm.run(fn, title="fails")
    qtbot.waitUntil(lambda: len(errors) == 1, timeout=TIMEOUT)
    assert errors[0] == (exc, task)
    assert task.state == "failed" and task.error is exc
    # with on_error, the signal stays silent
    handled: list = []
    tm.run(fn, title="handled", on_error=handled.append)
    qtbot.waitUntil(lambda: handled == [exc], timeout=TIMEOUT)
    assert len(errors) == 1


def test_callback_exceptions_do_not_stop_the_timer(qtbot, tm: TaskManager) -> None:
    errors: list = []
    tm.error.connect(lambda exc, task: errors.append(exc))

    def bad_item(item):
        if item == 1:
            raise RuntimeError("item handler bug")

    seen: list = []

    def fn(ctx):
        for i in range(3):
            ctx.post(i)
        return "r"

    def on_item(item):
        seen.append(item)
        bad_item(item)

    def on_done(result):
        raise KeyError("done handler bug")

    tm.run(fn, title="bugs", on_item=on_item, on_done=on_done)
    qtbot.waitUntil(lambda: len(errors) == 2, timeout=TIMEOUT)
    assert seen == [0, 1, 2]
    assert isinstance(errors[0], RuntimeError) and isinstance(errors[1], KeyError)
    # the manager still works
    results: list = []
    tm.run(lambda ctx: "next", title="next", on_done=results.append)
    qtbot.waitUntil(lambda: results == ["next"], timeout=TIMEOUT)


def test_detach_drops_callbacks_and_cancels(qtbot, tm: TaskManager) -> None:
    owner_a, owner_b = object(), object()
    calls: list = []
    release = threading.Event()

    def fn(ctx):
        ctx.post("early")
        release.wait(10)
        ctx.post("late")
        ctx.check()
        return "x"

    ta = tm.run(fn, title="a", owner=owner_a, on_item=lambda i: calls.append(("a", i)),
                on_done=lambda r: calls.append(("a", "done")), on_cancel=lambda: calls.append(("a", "cancel")),
                on_error=lambda e: calls.append(("a", "error")))
    tb = tm.run(fn, title="b", owner=owner_b, on_item=lambda i: calls.append(("b", i)),
                on_done=lambda r: calls.append(("b", "done")))
    assert tm.tasks(owner_a) == [ta] and tm.tasks(owner_b) == [tb]
    tm.detach(owner_a)
    assert ta.cancel_requested and not tb.cancel_requested
    release.set()
    qtbot.waitUntil(lambda: ("b", "done") in calls, timeout=TIMEOUT)
    assert tm.wait_idle(20)
    assert [c for c in calls if c[0] == "a"] == []
    assert [c for c in calls if c[0] == "b"] == [("b", "early"), ("b", "late"), ("b", "done")]
    assert ta.state == "cancelled"


def test_cancel_all_by_owner(qtbot, tm: TaskManager) -> None:
    owner = object()
    calls: list = []

    def fn(ctx):
        while True:
            ctx.check()
            time.sleep(0.005)

    tm.run(fn, title="1", owner=owner, on_cancel=lambda: calls.append(1))
    tm.run(fn, title="2", owner=owner, on_cancel=lambda: calls.append(2))
    other = tm.run(lambda ctx: (time.sleep(0.05), "ok")[1], title="3", on_done=lambda r: calls.append(r))
    tm.cancel_all(owner)
    assert not other.cancel_requested
    qtbot.waitUntil(lambda: sorted(map(str, calls)) == ["1", "2", "ok"], timeout=TIMEOUT)


def test_silent_tasks_are_not_announced(qtbot, tm: TaskManager) -> None:
    added, updated, finished, results = [], [], [], []
    tm.task_added.connect(added.append)
    tm.task_updated.connect(updated.append)
    tm.task_finished.connect(finished.append)
    go = threading.Event()
    task = tm.run(lambda ctx: go.wait(10), title="quiet", silent=True, on_done=results.append)
    assert task.silent
    assert tm.tasks() == []
    assert tm.tasks(include_silent=True) == [task]
    qtbot.wait(250)
    go.set()
    qtbot.waitUntil(lambda: results == [True], timeout=TIMEOUT)
    assert added == [] and updated == [] and finished == []


def test_progress_info_and_updates(qtbot, tm: TaskManager) -> None:
    updated: list = []
    tm.task_updated.connect(updated.append)
    step = threading.Event()
    go = threading.Event()

    def fn(ctx):
        ctx.progress.set_total(1000)
        ctx.progress.set_message("working")
        ctx.progress.update(400)
        step.set()
        go.wait(10)
        ctx.progress.finish()
        return None

    task = tm.run(fn, title="p", unit="records")
    assert step.wait(10)
    qtbot.waitUntil(lambda: task.info() is not None and task.info().message == "working", timeout=TIMEOUT)
    info = task.info()
    assert info.total == 1000 and info.unit == "records"
    qtbot.waitUntil(lambda: task in updated, timeout=TIMEOUT)
    go.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert task.info().finished and task.info().done == 1000


def test_wait_idle_timeout_and_join(qtbot, tm: TaskManager) -> None:
    go = threading.Event()
    task = tm.run(lambda ctx: go.wait(10), title="blocked")
    assert tm.wait_idle(0.2) is False
    assert task.thread_alive and not task.join(0.01)
    go.set()
    assert task.join(10)
    assert tm.wait_idle(20) is True
    assert task.state == "done"


def test_many_tasks_keep_their_own_order(qtbot, tm: TaskManager) -> None:
    got: dict[int, list] = {}
    done: list = []

    def make(k):
        def fn(ctx):
            for i in range(200):
                ctx.post((k, i))
            return k
        return fn

    for k in range(20):
        got[k] = []
        tm.run(make(k), title=f"t{k}", on_item=lambda item: got[item[0]].append(item[1]), on_done=done.append)
    qtbot.waitUntil(lambda: len(done) == 20, timeout=TIMEOUT)
    assert all(got[k] == list(range(200)) for k in range(20))


def test_shutdown_cancels_everything(qtbot, qapp) -> None:
    tm = TaskManager()
    calls: list = []

    def fn(ctx):
        while True:
            ctx.check()
            time.sleep(0.005)

    for _ in range(3):
        tm.run(fn, title="loop", on_cancel=lambda: calls.append("c"))
    assert tm.shutdown(20) is True
    assert calls == ["c", "c", "c"]
    assert tm.tasks(include_silent=True) == []


def test_state_text(lang_en) -> None:
    assert state_text("running") == "Running"
    assert state_text("cancelled") == "Cancelled"
    assert state_text("other") == "other"


# ----------------------------------------------------------------------------------------
# fix round: wake-up delivery (P-F9), fresh progress (L1), modal callbacks (L3/T7),
# object lifetime (T10)
# ----------------------------------------------------------------------------------------
def test_results_are_delivered_without_waiting_for_the_poll(qtbot, tm: TaskManager) -> None:
    """A worker that posts or ends wakes the GUI thread (queued signal): with a 60 s poll
    interval the items and the result still arrive at once, in order, items first."""
    tm.set_poll_interval(60_000)
    events: list = []
    ended_at: list[float] = []

    def fn(ctx):
        for i in range(50):
            ctx.post(i)
        ended_at.append(time.perf_counter())
        return "r"

    tm.run(fn, title="fast", on_item=events.append, on_done=lambda r: events.append(("done", r, time.perf_counter())))
    qtbot.waitUntil(lambda: bool(events) and isinstance(events[-1], tuple), timeout=5000)
    assert events[:-1] == list(range(50))
    latency = events[-1][2] - ended_at[0]
    assert latency < 1.0, latency            # ~1 ms in practice; the old poll needed up to 100 ms (here 60 s)
    # a lone result after an idle period
    got: list = []
    t0 = time.perf_counter()
    tm.run(lambda ctx: 1, title="one", on_done=lambda r: got.append(time.perf_counter() - t0))
    qtbot.waitUntil(lambda: bool(got), timeout=5000)
    assert got[0] < 1.0


def test_wake_delivery_backlog_continues_without_the_poll(qtbot, tm: TaskManager) -> None:
    """Items left over when the drain budget runs out are delivered by the next wake-up
    (not only by the poll timer)."""
    from jsonreshape.gui import qtasks

    tm.set_poll_interval(60_000)
    seen: list = []
    n = int(qtasks.DRAIN_BUDGET_S / 0.002) * 3 + 10   # needs several deliveries

    def slow_item(item):
        seen.append(item)
        time.sleep(0.002)

    def fn(ctx):
        for i in range(n):
            ctx.post(i)
        return "end"

    done: list = []
    tm.run(fn, title="backlog", on_item=slow_item, on_done=done.append)
    qtbot.waitUntil(lambda: done == ["end"], timeout=15000)
    assert seen == list(range(n))


def test_info_is_fresh_and_final(qtbot, tm: TaskManager) -> None:
    """info() is the current progress (not the snapshot of the throttled callback) and,
    after the end, the final progress even without progress.finish()."""
    step = threading.Event()
    go = threading.Event()

    def fn(ctx):
        ctx.progress.set_total(1000)
        ctx.progress.update(1)          # reported (first callback)
        ctx.progress.update(996)        # within the 100 ms throttle: no callback
        step.set()
        go.wait(10)
        ctx.progress.update(1000)       # last update, no finish()
        return None

    task = tm.run(fn, title="p")
    assert step.wait(10)
    assert task.info() is not None and task.info().done == 996
    go.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    final = task.info()
    assert final.done == 1000 and final.fraction == 1.0
    time.sleep(0.05)
    assert task.info() is final          # frozen: the rate does not decay after the end


def test_info_is_none_before_the_first_report(qtbot, tm: TaskManager) -> None:
    go = threading.Event()
    task = tm.run(lambda ctx: go.wait(10), title="quiet")
    assert task.info() is None
    go.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert task.info() is None


def test_modal_loop_in_a_callback_does_not_freeze_other_tasks(qtbot, tm: TaskManager) -> None:
    """A callback that runs a modal loop (like QMessageBox.exec in an on_item) must not
    stop the delivery of the other tasks; the busy task's own items wait (order kept)."""
    from PySide6.QtCore import QEventLoop, QTimer

    events: list = []
    b_go = threading.Event()
    a_more = threading.Event()

    def fn_a(ctx):
        ctx.post("a1")
        a_more.wait(10)
        ctx.post("a2")
        return "A"

    def fn_b(ctx):
        b_go.wait(10)
        ctx.post("b1")
        return "B"

    def modal_on_item(item):
        events.append(item)
        if item != "a1":
            return
        loop = QEventLoop()
        a_more.set()                          # a2 is queued while a1's callback is still running
        b_go.set()
        QTimer.singleShot(10_000, loop.quit)  # safety net

        def check() -> None:
            if ("done", "B") in events:
                QTimer.singleShot(50, loop.quit)   # a2 had time to be (wrongly) delivered
            else:
                QTimer.singleShot(5, check)

        check()
        loop.exec()
        events.append("modal closed")

    tm.run(fn_a, title="a", on_item=modal_on_item, on_done=lambda r: events.append(("done", r)))
    tm.run(fn_b, title="b", on_item=events.append, on_done=lambda r: events.append(("done", r)))
    qtbot.waitUntil(lambda: ("done", "A") in events, timeout=TIMEOUT)
    assert events.index(("done", "B")) < events.index("modal closed"), events
    assert events.index("modal closed") < events.index("a2") < events.index(("done", "A")), events
    assert events[0] == "a1" and "b1" in events


def test_modal_loop_in_on_done_keeps_task_updates_flowing(qtbot, tm: TaskManager) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    tm.set_poll_interval(10)
    updates: list = []
    tm.task_updated.connect(lambda task: updates.append(task.title))
    stop = threading.Event()

    def busy(ctx):
        while not stop.is_set():
            time.sleep(0.005)

    other = tm.run(busy, title="busy")
    counted: list = []

    def on_done(_r):
        loop = QEventLoop()
        before = updates.count("busy")
        QTimer.singleShot(300, loop.quit)
        loop.exec()
        counted.append(updates.count("busy") - before)

    tm.run(lambda ctx: 1, title="asks", on_done=on_done)
    qtbot.waitUntil(lambda: bool(counted), timeout=TIMEOUT)
    stop.set()
    assert counted[0] >= 3        # the old poll guard froze every task while the modal loop ran
    qtbot.waitUntil(lambda: other.state == "done", timeout=TIMEOUT)


def test_finished_tasks_and_their_closures_are_freed_in_the_gui_thread(qtbot, tm: TaskManager,
                                                                         monkeypatch) -> None:
    """No reference cycle keeps a GuiTask alive (freed by reference counting, with the
    cyclic GC off), and neither the GuiTask nor objects captured by the task function are
    released by the worker thread."""
    import gc
    import logging
    import weakref

    from PySide6.QtCore import QObject

    # pytest keeps the log records of a test (with their arguments: the task and its
    # exception) until the test ends -- they would keep the failed task alive here
    monkeypatch.setattr(logging.getLogger("jsonreshape.gui.qtasks"), "disabled", True)
    where: list[str] = []

    def record(label: str) -> None:
        main = threading.current_thread() is threading.main_thread()
        where.append(label + (":main" if main else ":worker"))

    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        outcomes: list = []

        def start(kind: str) -> None:
            captured = QObject()
            weakref.finalize(captured, record, f"captured-{kind}")

            def fn(ctx, captured=captured):
                ctx.progress.update(1)
                ctx.post(captured.objectName())
                if kind == "fail":
                    raise ValueError("boom")
                if kind == "cancel":
                    raise Cancelled()
                return kind

            task = tm.run(fn, title=kind, on_done=outcomes.append, on_error=lambda e: outcomes.append("err"),
                          on_cancel=lambda: outcomes.append("cancelled"), on_item=lambda item: None)
            weakref.finalize(task, record, f"task-{kind}")

        for kind in ("done", "fail", "cancel"):
            start(kind)
        qtbot.waitUntil(lambda: len(outcomes) == 3, timeout=TIMEOUT)
        assert tm.wait_idle(20)
        for thread in threading.enumerate():
            if thread.name.startswith("jsonreshape-gui-"):
                thread.join(10)
        qtbot.wait(20)
        assert sorted(where) == sorted(["captured-done:main", "captured-fail:main", "captured-cancel:main",
                                        "task-done:main", "task-fail:main", "task-cancel:main"]), where
    finally:
        if was_enabled:
            gc.enable()


def test_worker_never_references_the_qobject(qtbot, tm: TaskManager) -> None:
    import gc

    seen: list = []
    go = threading.Event()

    def fn(ctx):
        seen.append(ctx)
        go.wait(10)

    task = tm.run(fn, title="x")
    qtbot.waitUntil(lambda: bool(seen), timeout=TIMEOUT)
    ctx = seen.pop()
    reachable = gc.get_referents(ctx._core) + [ctx.progress, ctx.cancel]
    assert all(obj is not task for obj in reachable)
    assert task._thread is not None and task._thread._target.__self__ is ctx._core
    del ctx
    go.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)


def test_manager_deleted_while_a_task_runs(qtbot, qapp) -> None:
    """A worker that ends after its manager was deleted wakes nobody (no crash)."""
    import shiboken6

    manager = TaskManager()
    go = threading.Event()
    task = manager.run(lambda ctx: go.wait(10), title="orphan")
    core = task._core
    shiboken6.delete(manager)
    del manager, task
    go.set()
    assert core.ended.wait(10)
    qtbot.wait(50)


# ----------------------------------------------------------------------------------------
# heavy tasks (T2): at most MAX_HEAVY_TASKS run at once, the others wait in order
# ----------------------------------------------------------------------------------------
def test_heavy_tasks_run_one_after_the_other(qtbot, tm: TaskManager, lang_en, monkeypatch) -> None:
    from jsonreshape.gui import qtasks

    assert qtasks.MAX_HEAVY_TASKS >= 1
    monkeypatch.setattr(qtasks, "MAX_HEAVY_TASKS", 1)  # the queue order is easiest to see with one slot
    heavy_running: list[str] = []
    most: list[int] = []
    order: list[str] = []
    release = threading.Event()
    lock = threading.Lock()

    def make(name, heavy):
        def fn(ctx):
            with lock:
                order.append(name)
                if heavy:
                    heavy_running.append(name)
                    most.append(len(heavy_running))
            if name == "h1":
                release.wait(10)
            ctx.progress.update(5)
            time.sleep(0.02)
            with lock:
                if heavy:
                    heavy_running.remove(name)
            return name
        return fn

    done: list = []
    h1 = tm.run(make("h1", True), title="h1", kind="filter", heavy=True, on_done=done.append)
    h2 = tm.run(make("h2", True), title="h2", kind="sort", heavy=True, on_done=done.append)
    h3 = tm.run(make("h3", True), title="h3", kind="schema", heavy=True, on_done=done.append)
    tm.run(make("light", False), title="light", on_done=done.append)
    qtbot.waitUntil(lambda: "light" in done, timeout=TIMEOUT)     # not heavy: not queued
    assert not h1.queued and h2.queued and h3.queued
    assert h2._thread is None and h2.running and h2.thread_alive
    assert h2.info() is not None and h2.info().message.startswith("Queued")
    assert tm.tasks()[:3] == [h1, h2, h3]
    release.set()
    qtbot.waitUntil(lambda: done == ["light", "h1", "h2", "h3"], timeout=TIMEOUT)
    assert [name for name in order if name != "light"] == ["h1", "h2", "h3"]
    assert max(most) == 1
    assert not h2.queued and h2.info().done == 5


def test_queued_heavy_task_cancel_and_detach(qtbot, tm: TaskManager, monkeypatch) -> None:
    from jsonreshape.gui import qtasks

    monkeypatch.setattr(qtasks, "MAX_HEAVY_TASKS", 1)  # one blocker fills the only slot
    release = threading.Event()
    ran: list[str] = []
    calls: list = []
    owner = object()

    def blocker(ctx):
        release.wait(10)
        return "b"

    def never(name):
        def fn(ctx):
            ran.append(name)
        return fn

    tm.run(blocker, title="b", heavy=True, on_done=calls.append)
    q1 = tm.run(never("q1"), title="q1", heavy=True, on_cancel=lambda: calls.append("q1 cancelled"),
                on_done=lambda r: calls.append("q1 done"))
    q2 = tm.run(never("q2"), title="q2", heavy=True, owner=owner, on_cancel=lambda: calls.append("q2 cancelled"))
    assert q1.queued and q2.queued
    tm.set_poll_interval(60_000)
    q1.cancel()
    qtbot.waitUntil(lambda: "q1 cancelled" in calls, timeout=5000)   # at once, not after the blocker
    assert q1.state == "cancelled" and q2.queued
    tm.detach(owner)
    release.set()
    qtbot.waitUntil(lambda: "b" in calls, timeout=TIMEOUT)
    assert tm.wait_idle(20)
    assert ran == [] and calls == ["q1 cancelled", "b"] and q2.state == "cancelled"


def test_shutdown_ends_queued_heavy_tasks(qtbot, qapp) -> None:
    manager = TaskManager()
    calls: list = []

    def loop(ctx):
        while True:
            ctx.check()
            time.sleep(0.005)

    for i in range(3):
        manager.run(loop, title=f"h{i}", heavy=True, on_cancel=lambda i=i: calls.append(i))
    assert manager.shutdown(20) is True
    assert sorted(calls) == [0, 1, 2]
