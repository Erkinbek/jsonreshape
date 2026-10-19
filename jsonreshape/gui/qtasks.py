"""Background tasks for the GUI (docs/ARCHITECTURE.md §11.2).

:class:`TaskManager` runs every task function in its own daemon ``threading.Thread``
(``fn(ctx)`` with a :class:`TaskContext`) and hands everything back to the GUI thread:

* items the worker posted with :meth:`TaskContext.post` are delivered **in order** to
  ``on_item(item)``;
* once the worker thread has ended **and** every posted item has been delivered, exactly
  one of ``on_done(result)``, ``on_error(exc)`` (no handler: the ``error`` signal) or
  ``on_cancel()`` is called. A task counts as cancelled when ``fn`` raised
  :class:`~jsonreshape.errors.Cancelled`, or raised anything after its token was
  cancelled (errors caused by a cancellation are not errors); a function that returned
  normally despite a cancel request is done;
* ``task_updated`` is emitted for every running (non-silent) task by a 100 ms ``QTimer``
  (running only while tasks exist), so a tasks panel can show :meth:`GuiTask.info`.

Delivery latency: a worker that posts an item or ends wakes the GUI thread through a queued
signal (coalesced: at most one wake-up is pending, and deliveries are at least
:data:`WAKE_MIN_INTERVAL_MS` apart), so results arrive within about a millisecond instead of
waiting for the next poll; the poll timer is only the safety net and the progress clock.

Callbacks run in the GUI thread. An exception raised by a callback never stops the
delivery: it is logged (``logging.exception``) and sent through the ``error`` signal.
A callback may open a modal loop (``QDialog.exec()``): while it runs, the other tasks keep
being delivered (only the task whose callback is still running waits, so its order holds).
:meth:`TaskManager.detach` cancels the tasks of an owner (a closed tab) and drops their
callbacks, so nothing calls back into the closed owner; the threads finish on their own.

``silent`` tasks (short helper jobs) are invisible to panels: no ``task_added`` /
``task_updated`` / ``task_finished`` signals and not listed by :meth:`TaskManager.tasks`
unless ``include_silent=True``; their failures without ``on_error`` still reach the
``error`` signal.

``heavy`` tasks (long pure-Python bulk work: a full filter / sort / schema scan / export)
run at most :data:`MAX_HEAVY_TASKS` at a time; further ones wait in a queue (state
``running``, :attr:`GuiTask.queued` True, a "waiting" progress message) and start in
order. Python code holds the GIL, so two such tasks never run faster together than one
after the other -- but together they hand the GIL back and forth all the time, which costs
about a third of their throughput with the GUI's short switch interval (measured: two
JMESPath filters 70 -> 46 MB/s in total, two full schema scans 34 -> 23 MB/s). A queued
task that is cancelled ends at once (``on_cancel``) without running.

Object lifetime: the worker thread never holds a reference to the :class:`GuiTask`
``QObject`` (it only sees a plain-Python :class:`_TaskCore`), and it drops its references
to the task function before it reports the end. So every ``GuiTask``, task function and
callback (which may capture widgets) is released by the GUI thread -- a ``QObject`` must
never be destroyed in a worker thread. Another thread that must wait for a task's end
(``DocSession`` closing a document after its tasks) waits on :attr:`GuiTask.done_event`, a
plain ``threading.Event``, never on the ``GuiTask`` itself; a finished task forgets its
``owner`` (it may be a ``QObject`` such as the edit controller), so a finished task kept
somewhere (the tasks panel, a list of document users) does not keep its owner alive.

Threading rules: :meth:`TaskManager.run`, :meth:`~TaskManager.detach`,
:meth:`~TaskManager.cancel_all`, :meth:`~TaskManager.wait_idle` are called in the GUI
thread (the thread owning the manager). The worker may only use its ``ctx``.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
import weakref
from collections import deque
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Qt, QTimer, Signal

from jsonreshape.errors import Cancelled
from jsonreshape.i18n import t
from jsonreshape.tasks import CancelToken, Progress, ProgressInfo

log = logging.getLogger(__name__)

__all__ = ["GuiTask", "TaskContext", "TaskManager", "POLL_MS", "DRAIN_BUDGET_S", "MAX_HEAVY_TASKS",
           "WAKE_MIN_INTERVAL_MS", "state_text"]

POLL_MS = 100              # progress / safety-net timer interval while tasks exist
DRAIN_BUDGET_S = 0.05      # at most this long delivering posted items of one task per delivery
WAKE_MIN_INTERVAL_MS = 15  # deliveries triggered by workers are at least this far apart
MAX_HEAVY_TASKS = 2        # heavy tasks running at the same time (the others wait): two keep a
                           # filter usable while a full schema scan runs; more only split the GIL

_ids = itertools.count(1)


def state_text(state: str) -> str:
    """Localized label of a task state (``running`` / ``done`` / ``failed`` / ``cancelled``)."""
    keys = {
        "running": "gui.tasks.state.running",
        "done": "gui.tasks.state.done",
        "failed": "gui.tasks.state.failed",
        "cancelled": "gui.tasks.state.cancelled",
    }
    key = keys.get(state)
    return t(key) if key else state


# ----------------------------------------------------------------------------------------
# waking the GUI thread
# ----------------------------------------------------------------------------------------
class _Waker(QObject):
    """Lives in the GUI thread for the whole process; workers emit :attr:`wake` (queued)."""

    wake = Signal(object)    # _WakeHandle


_waker: _Waker | None = None


def _fire(handle: _WakeHandle) -> None:    # GUI thread (queued slot)
    handle.fire()


def _get_waker() -> _Waker:
    """The process-wide waker (created by the first TaskManager, in the GUI thread). It
    has no parent and is never deleted, so a worker can always emit on it."""
    global _waker
    waker = _waker
    if waker is None or not shiboken6.isValid(waker):
        waker = _Waker()
        waker.wake.connect(_fire, Qt.ConnectionType.QueuedConnection)
        _waker = waker
    return waker


class _WakeHandle:
    """Coalesced wake-up of one :class:`TaskManager` (plain Python; any thread may call
    :meth:`request`). ``pending`` stays True until the manager's next delivery starts, so
    a busy worker emits at most one signal per delivery."""

    __slots__ = ("pending", "_manager", "_waker")

    def __init__(self, manager: TaskManager, waker: _Waker) -> None:
        self.pending = False
        self._manager = weakref.ref(manager)
        self._waker = waker

    def request(self) -> None:
        if self.pending:
            return
        self.pending = True
        try:
            self._waker.wake.emit(self)
        except RuntimeError:   # pragma: no cover - the application is being torn down
            pass

    def fire(self) -> None:
        manager = self._manager()
        if manager is not None and shiboken6.isValid(manager):
            manager._on_wake()


# ----------------------------------------------------------------------------------------
# task objects
# ----------------------------------------------------------------------------------------
class _Reported:
    """Progress callback that only records that the task reported progress at least once
    (it references nothing, so the Progress does not keep anything alive)."""

    __slots__ = ("seen",)

    def __init__(self) -> None:
        self.seen = False

    def __call__(self, _info: ProgressInfo) -> None:
        self.seen = True


class _TaskCore:
    """The part of a task the worker thread sees (plain Python, no Qt)."""

    __slots__ = ("fn", "items", "ended", "outcome", "token", "progress", "reported", "detached", "wake",
                 "__weakref__")

    def __init__(self, fn: Callable[[TaskContext], Any], unit: str, wake: _WakeHandle) -> None:
        self.fn: Callable[[TaskContext], Any] | None = fn
        self.items: deque[Any] = deque()
        self.ended = threading.Event()
        self.outcome: tuple[Any, BaseException | None] = (None, None)
        self.token = CancelToken()
        self.reported = _Reported()
        self.progress = Progress(unit=unit, callback=self.reported)
        self.detached = False
        self.wake = wake

    def run(self) -> None:
        """Worker thread body: run the function, store the outcome, wake the GUI thread."""
        fn = self.fn
        self.fn = None
        ctx = TaskContext(self)
        try:
            assert fn is not None
            self.outcome = (fn(ctx), None)
        except BaseException as exc:  # noqa: BLE001 - handed to the GUI thread
            self.outcome = (None, exc)
        finally:
            # the GUI thread holds the last references to the function (and what it captured)
            fn = None
            ctx = None  # noqa: F841
            self.ended.set()
            self.wake.request()


class TaskContext:
    """What the task function receives (it runs in the worker thread).

    ``progress``: a :class:`~jsonreshape.tasks.Progress` (unit given to ``run()``) read by
    :meth:`GuiTask.info`; ``cancel``: the task's :class:`~jsonreshape.tasks.CancelToken`;
    :meth:`post` hands an item to ``on_item`` in the GUI thread (in order).
    """

    __slots__ = ("progress", "cancel", "_core")

    def __init__(self, core: _TaskCore) -> None:
        self._core = core
        self.progress = core.progress
        self.cancel = core.token

    def post(self, item: Any) -> None:
        """Deliver ``item`` to ``on_item`` in the GUI thread (thread-safe, ordered).
        Items of a detached task are dropped."""
        core = self._core
        if not core.detached:
            core.items.append(item)
            core.wake.request()

    def check(self) -> None:
        """``cancel.check()``: raises :class:`~jsonreshape.errors.Cancelled` when cancelled."""
        self.cancel.check()

    @property
    def cancelled(self) -> bool:
        return self.cancel.cancelled


class GuiTask(QObject):
    """One background task (created by :meth:`TaskManager.run`).

    ``state``: ``running`` | ``done`` | ``failed`` | ``cancelled``; ``result`` (done),
    ``error`` (failed). Read the attributes in the GUI thread.
    """

    def __init__(self, *, title: str, kind: str, owner: Any, silent: bool, core: _TaskCore,
                 on_done: Callable[[Any], Any] | None, on_error: Callable[[BaseException], Any] | None,
                 on_item: Callable[[Any], Any] | None, on_cancel: Callable[[], Any] | None,
                 heavy: bool = False, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.id: int = next(_ids)
        self.title = title
        self.kind = kind
        self.owner = owner
        self.silent = bool(silent)
        self.heavy = bool(heavy)
        self.queued = False           # a heavy task waiting for its turn (thread not started)
        self.state = "running"
        self.result: Any = None
        self.error: BaseException | None = None
        self.started = time.monotonic()
        self.finished_at: float | None = None
        self._core = core
        self._fn = core.fn            # released in the GUI thread when the task finishes
        self._on_done = on_done
        self._on_error = on_error
        self._on_item = on_item
        self._on_cancel = on_cancel
        self._dispatching = False     # one of its callbacks is running (maybe in a modal loop)
        self._final_info: ProgressInfo | None = None
        self._thread: threading.Thread | None = None

    def __repr__(self) -> str:
        return f"<GuiTask #{self.id} {self.kind} {self.title!r} {self.state}>"

    # -- public ------------------------------------------------------------------------
    def info(self) -> ProgressInfo | None:
        """The task's current progress (``None`` before the first report); once the task
        has finished, the progress as it was at the end."""
        if self.state != "running":
            return self._final_info
        core = self._core
        if self.queued:
            return ProgressInfo(done=0, total=None, fraction=None, rate=0.0, eta=None, elapsed=0.0,
                                message=t("gui.tasks.queued"), unit=core.progress.unit)
        return core.progress.info() if core.reported.seen else None

    def cancel(self) -> None:
        """Ask the task to stop (its function sees ``ctx.cancel``; a queued heavy task
        ends without running)."""
        self._core.token.cancel()
        if self.queued:
            self._core.wake.request()

    @property
    def cancel_requested(self) -> bool:
        return self._core.token.cancelled

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def thread_alive(self) -> bool:
        """The worker thread has not ended yet (callbacks may still be pending)."""
        return not self._core.ended.is_set()

    @property
    def done_event(self) -> threading.Event:
        """Set once the worker thread has ended (a queued task that was cancelled: once it was
        dropped). A plain ``threading.Event``: another thread may wait on it without holding
        this ``QObject`` (review CL-8)."""
        return self._core.ended

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker thread to end (not for the GUI-thread callbacks); True when
        it has ended. Never call this in the GUI thread with a long timeout."""
        return self._core.ended.wait(timeout)


# ----------------------------------------------------------------------------------------
# manager
# ----------------------------------------------------------------------------------------
class TaskManager(QObject):
    """Runs and tracks :class:`GuiTask` objects (see the module docstring)."""

    task_added = Signal(object)       # GuiTask (not for silent tasks)
    task_updated = Signal(object)     # GuiTask, every poll while it runs (not for silent tasks)
    task_finished = Signal(object)    # GuiTask with its final state (not for silent tasks)
    error = Signal(object, object)    # (exception, GuiTask | None): failures without on_error, callback errors

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tasks: list[GuiTask] = []
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._wake = _WakeHandle(self, _get_waker())
        self._wake_timer = QTimer(self)
        self._wake_timer.setSingleShot(True)
        self._wake_timer.timeout.connect(self._deliver)
        self._last_delivery = 0.0
        self._heavy_queue: deque[GuiTask] = deque()
        self._heavy_active: list[GuiTask] = []

    # -- running -------------------------------------------------------------------------
    def run(self, fn: Callable[[TaskContext], Any], *, title: str, kind: str = "other", owner: Any = None,
            on_done: Callable[[Any], Any] | None = None, on_error: Callable[[BaseException], Any] | None = None,
            on_item: Callable[[Any], Any] | None = None, on_cancel: Callable[[], Any] | None = None,
            silent: bool = False, unit: str = "bytes", heavy: bool = False) -> GuiTask:
        """Start ``fn(ctx)`` in a new daemon thread and return its :class:`GuiTask`.

        ``title`` is shown in the tasks panel (already localized); ``kind`` e.g. ``open``,
        ``index``, ``filter``, ``sort``, ``search``, ``export``; ``owner`` groups tasks for
        :meth:`detach` / :meth:`cancel_all`; ``unit`` is the unit of ``ctx.progress``;
        ``heavy``: long pure-Python bulk work that waits while :data:`MAX_HEAVY_TASKS`
        other heavy tasks run (see the module docstring). A heavy task must never wait for
        another heavy task.
        """
        core = _TaskCore(fn, unit, self._wake)
        task = GuiTask(title=title, kind=kind, owner=owner, silent=silent, core=core, on_done=on_done,
                       on_error=on_error, on_item=on_item, on_cancel=on_cancel, heavy=heavy)
        self._tasks.append(task)
        if task.heavy and self._heavy_busy():
            task.queued = True
            self._heavy_queue.append(task)
        else:
            self._start_thread(task)
        if not self._timer.isActive():
            self._timer.start()
        if not task.silent:
            self._emit(self.task_added, task)
        return task

    def _start_thread(self, task: GuiTask) -> None:
        task.queued = False
        core = task._core
        core.progress.restart_clock()
        thread = threading.Thread(target=core.run, name=f"jsonreshape-gui-{task.kind}-{task.id}", daemon=True)
        task._thread = thread
        if task.heavy:
            self._heavy_active.append(task)
        thread.start()

    def _heavy_busy(self) -> bool:
        self._heavy_active = [task for task in self._heavy_active if not task._core.ended.is_set()]
        return len(self._heavy_active) >= MAX_HEAVY_TASKS

    def _start_queued(self) -> None:
        """Start waiting heavy tasks while slots are free; a cancelled waiting task ends
        at once without running."""
        queue = self._heavy_queue
        for task in [task for task in queue if task._core.token.cancelled]:
            queue.remove(task)
            task.queued = False
            core = task._core
            core.fn = None
            core.outcome = (None, Cancelled())
            core.ended.set()
        while queue and not self._heavy_busy():
            self._start_thread(queue.popleft())

    def set_poll_interval(self, ms: int) -> None:
        """Change the poll interval (default :data:`POLL_MS`; tests use a shorter one)."""
        self._timer.setInterval(max(1, int(ms)))

    def tasks(self, owner: Any = None, *, include_silent: bool = False) -> list[GuiTask]:
        """Tasks that have not finished yet (of ``owner`` when given; silent ones only
        with ``include_silent``). Detached tasks whose thread still runs are included."""
        return [task for task in self._tasks
                if (owner is None or task.owner is owner) and (include_silent or not task.silent)]

    def cancel_all(self, owner: Any = None) -> None:
        """Cancel every unfinished task (of ``owner`` when given); callbacks still run."""
        for task in list(self._tasks):
            if owner is None or task.owner is owner:
                task.cancel()

    def detach(self, owner: Any) -> None:
        """Cancel ``owner``'s tasks and forget their callbacks: nothing calls back into
        ``owner`` any more (e.g. its tab was closed). Posted items are discarded."""
        if owner is None:
            return
        for task in list(self._tasks):
            if task.owner is owner:
                task._core.detached = True
                task._on_done = task._on_error = task._on_item = task._on_cancel = None
                task._core.items.clear()
                task.cancel()

    def wait_idle(self, timeout: float) -> bool:
        """Process Qt events (and poll) until no task is left or ``timeout`` seconds
        passed; True when idle. For tests and shutdown."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self._poll()
            if not self._tasks:
                return True
            if time.monotonic() >= deadline:
                return False
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            alive = [t for t in self._tasks if not t._core.ended.is_set()]
            if alive:
                alive[0]._core.ended.wait(0.01)

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Cancel every task and wait (processing events) until they ended."""
        self.cancel_all()
        idle = self.wait_idle(timeout)
        if not idle:
            log.warning("%d background tasks still running at shutdown", len(self._tasks))
        return idle

    # -- delivery (GUI thread) -------------------------------------------------------------
    def _on_wake(self) -> None:
        """A worker posted an item or ended: deliver now, or at most
        :data:`WAKE_MIN_INTERVAL_MS` after the previous delivery."""
        if self._wake_timer.isActive():
            return
        since_ms = (time.monotonic() - self._last_delivery) * 1000.0
        if since_ms < WAKE_MIN_INTERVAL_MS:
            self._wake_timer.start(max(1, int(WAKE_MIN_INTERVAL_MS - since_ms) + 1))
            return
        self._deliver()

    def _poll(self) -> None:
        """Timer tick: deliver and emit ``task_updated`` for the running tasks."""
        self._deliver(updates=True)

    def _deliver(self, updates: bool = False) -> None:
        # Re-entrant: a callback may run a modal loop that delivers again. A task whose
        # callback is still running is skipped (its items stay in order); every other
        # task goes on.
        self._wake.pending = False          # posts from now on wake the GUI thread again
        self._last_delivery = time.monotonic()
        if self._heavy_queue:
            self._start_queued()
        backlog = False
        try:
            for task in list(self._tasks):
                if task._dispatching or task not in self._tasks:   # busy / finished by a nested delivery
                    continue
                try:
                    if not self._deliver_task(task, updates):
                        backlog = True
                except Exception as exc:  # pragma: no cover - defensive: never stop delivering
                    log.exception("delivering task %r failed", task)
                    self._emit_error(exc, task)
        finally:
            if not self._tasks and self._timer.isActive():
                self._timer.stop()
        if backlog:
            self._wake.request()            # the rest after the event loop had its turn

    def _deliver_task(self, task: GuiTask, updates: bool) -> bool:
        """Deliver ``task``'s items (and its end); False when items are left over."""
        ended = task._core.ended.is_set()   # before draining: every item of an ended task is queued
        drained = self._drain(task)
        if ended and drained:
            self._finish(task)
        elif updates and not task.silent and not task._core.detached:
            self._emit(self.task_updated, task)
        return drained

    def _drain(self, task: GuiTask) -> bool:
        """Deliver queued items to ``on_item``; True when the queue is empty."""
        items = task._core.items
        if not items:
            return True
        deadline = time.monotonic() + DRAIN_BUDGET_S
        task._dispatching = True
        try:
            while items:
                item = items.popleft()
                callback = task._on_item
                if callback is None:
                    continue
                try:
                    callback(item)
                except Exception as exc:
                    log.exception("on_item callback of task %r failed", task)
                    self._emit_error(exc, task)
                if time.monotonic() >= deadline and items:
                    return False
        finally:
            task._dispatching = False
        return True

    def _finish(self, task: GuiTask) -> None:
        if task.state != "running":   # pragma: no cover - defensive
            return
        core = task._core
        result, exc = core.outcome
        core.outcome = (None, None)
        if task in self._tasks:
            self._tasks.remove(task)
        if task in self._heavy_active:   # free the slot now (a finished task must not pin its owner)
            self._heavy_active.remove(task)
        task.finished_at = time.monotonic()
        task._final_info = core.progress.info() if core.reported.seen else None
        task._fn = None
        callback: Callable[..., Any] | None
        args: tuple[Any, ...]
        if exc is None:
            task.state = "done"
            task.result = result
            callback, args = task._on_done, (result,)
        elif isinstance(exc, Cancelled) or core.token.cancelled:
            if not isinstance(exc, Cancelled):
                log.debug("task %r failed after it was cancelled: %r", task, exc)
            task.state = "cancelled"
            callback, args = task._on_cancel, ()
        else:
            task.state = "failed"
            task.error = exc
            if core.detached:
                log.info("detached task %r failed: %r", task, exc)
                callback, args = None, ()
            elif task._on_error is not None:
                log.info("task %r failed: %r", task, exc)
                callback, args = task._on_error, (exc,)
            else:
                log.error("task %r failed", task, exc_info=(type(exc), exc, exc.__traceback__))
                callback, args = None, ()
                self._emit_error(exc, task)
        task._on_done = task._on_error = task._on_item = task._on_cancel = None
        result = exc = None
        if callback is not None:
            try:
                callback(*args)
            except Exception as cb_exc:
                log.exception("%s callback of task %r failed", task.state, task)
                self._emit_error(cb_exc, task)
        if not task.silent:
            self._emit(self.task_finished, task)
        # a finished task kept somewhere (tasks panel, document users) must not keep its owner (e.g. an
        # edit controller) alive: the owner is only needed while the task can still be detached
        task.owner = None

    # -- signal helpers ------------------------------------------------------------------------
    def _emit(self, signal: Any, task: GuiTask) -> None:
        try:
            signal.emit(task)
        except Exception as exc:  # pragma: no cover - slots normally do not raise into emit
            log.exception("task signal handler failed")
            self._emit_error(exc, task)

    def _emit_error(self, exc: BaseException, task: GuiTask | None) -> None:
        try:
            self.error.emit(exc, task)
        except Exception:  # pragma: no cover
            log.exception("error signal handler failed")
