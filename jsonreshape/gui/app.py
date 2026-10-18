"""Start the desktop viewer: ``jsonreshape-gui [files...]`` / ``python -m jsonreshape.gui``
(docs/ARCHITECTURE.md §11.1).

:func:`main` checks that PySide6 is installed (a friendly message otherwise), sets up
logging (rotating file in the logs folder) and the uncaught-exception hooks, logs the
environment check (in the background), chooses the language (``JSONRESHAPE_LANG`` >
``Settings.language`` > ``uz``), creates the :class:`QApplication` (High-DPI rounding
policy ``PassThrough``, an application icon drawn in code, the Windows AppUserModelID so
the taskbar groups the windows), applies the theme and the UI font size from the GUI
state, creates the
:class:`~jsonreshape.gui.context.AppContext` and the
:class:`~jsonreshape.gui.mainwindow.MainWindow`, opens the files given on the command line
and runs the event loop. Closing the window closes the documents, cancels the background
tasks and saves the GUI state.

Unexpected exceptions (in Qt slots, callbacks, worker threads) do not end the program:
:func:`install_qt_excepthook` logs them and shows the error window
(``AppContext.report_error``) in the GUI thread.

Runtime policy of the GUI process (:func:`main`; both undone when the window closes --
the automatic collector only once no worker thread runs any more):

* **Cyclic garbage collection only in the GUI thread** (:func:`install_gc_policy`). The
  automatic collector runs in whichever thread happens to allocate when a threshold is
  reached -- usually a busy worker (filter, schema scan). Collecting traverses and frees
  PySide wrappers while the GUI thread runs Qt C++ code on the same objects without the
  GIL (layouting, painting), which crashed the viewer with an access violation (review
  T1: 12 of 12 runs of two JMESPath filters). So the automatic collector is disabled
  (``gc.disable()``), the objects that exist after start-up are frozen (``gc.freeze()``,
  never scanned again) and a GUI-thread timer (:class:`GcPolicy`, every
  :data:`GC_TICK_MS` ms) collects:

  - the **young generation** (``gc.collect(0)``) once ``gc.get_count()`` shows as many new
    objects as the automatic collector would wait for: its cost is proportional to the new
    objects only (~0.1 ms per 1000; a view that just built a million rows: ~140 ms, which
    the automatic collector would have spent inside that operation), and it frees the
    cyclic garbage of the workers at once. Python 3.14's incremental steps
    (``gc.collect(1)``) are not used: their pauses are unpredictable (0 to 500 ms
    measured with a 3-million-object heap);
  - a **full collection** (``gc.collect(2)``, proportional to the whole heap) only when
    enough objects survived young collections since the last one
    (:data:`GC_FULL_MIN_PROMOTED`, or a quarter of the heap), at most every
    :data:`GC_FULL_MIN_INTERVAL_S` s and :data:`GC_FULL_MAX_SHARE` of the time;
  - a **full collection a few seconds after a tab was closed**
    (:meth:`GcPolicy.request_full`, :data:`GC_AFTER_CLOSE_S`; tabs closed in a row: one
    collection after the last): a closed tab's session, views and models form reference
    cycles (Qt signal connections, closures), so only a full collection frees them --
    waiting for enough promotions could keep hundreds of MB of a closed document for
    minutes. Its cost is bounded like the others: at most :data:`GC_FULL_MAX_SHARE` of
    the time since the previous full collection (a 0.2 s collection: 10 s apart), not
    while the user is busy (at most :data:`GC_FULL_MAX_DEFER_S` s), and the heap that
    existed at start-up is frozen;
  - collections that would be long -- a full one, or a young one of at least
    :data:`GC_DEFER_YOUNG` objects -- wait while the user is busy with mouse or keyboard
    (young: at most :data:`GC_MAX_DEFER_S` s, full: :data:`GC_FULL_MAX_DEFER_S` s).

  Reference counting still frees everything without cycles at once, in any thread. When
  the window closes the timer stops and the start-up objects are unfrozen, but the
  automatic collector is enabled again only when no worker thread runs any more (a task
  that did not stop within the shutdown wait could otherwise trigger a collection in its
  own thread while Qt tears the widgets down -- the T1 crash window): at once when none
  is alive, else by a small watcher thread once the threads alive at that moment have
  ended (never, if one runs until the process exits). The schema read-ahead pool's idle
  threads (``jsonreshape-schema-io``) are not waited for: they only work for a schema
  task, whose own thread is.
* **GIL switch interval** :data:`SWITCH_INTERVAL_S` (Python's default is 5 ms): with a
  Python-heavy worker running, every Qt-to-Python call of the GUI thread waits for the GIL.
  Measured in the viewer (syn_10m.jsonl, 10 ms heartbeat): one JMESPath filter p95
  133 -> 19 ms, two full schema scans p95 3.1 s -> 21 ms, two JMESPath filters p95
  918 -> 20 ms; one filter / sort / schema scan / indexing keep their throughput (0-3 %
  less). 1 ms is not enough on Windows (the GIL wait is rounded to the 15.6 ms timer
  tick: two filters p95 762 ms); any value below 1 ms means "hand the GIL over at the
  next check" there. Two Python-heavy workers at once lose about a third of their
  throughput to the frequent hand-overs -- the heavy tasks of
  :mod:`jsonreshape.gui.qtasks` therefore run at most
  :data:`~jsonreshape.gui.qtasks.MAX_HEAVY_TASKS` (two: a filter stays usable while a full
  schema scan runs) at a time; further ones wait in a queue.

This module does not import Qt at import time, so :func:`main` can report a missing
PySide6 installation.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from typing import Any, Callable

from jsonreshape.errors import UserInputError
from jsonreshape.gui import require_qt
from jsonreshape.i18n import SUPPORTED_LANGUAGES, set_language

log = logging.getLogger(__name__)

__all__ = ["APP_USER_MODEL_ID", "GC_AFTER_CLOSE_S", "GC_TICK_MS", "SWITCH_INTERVAL_S", "GcPolicy", "apply_ui_size",
           "choose_language", "gc_policy", "install_gc_policy", "install_qt_excepthook", "main", "make_app_icon",
           "set_app_user_model_id", "set_switch_interval", "user_busy"]

APP_USER_MODEL_ID = "jsonreshape.viewer"
ORGANIZATION = "jsonreshape"
APPLICATION = "jsonreshape"
#: At most this many error windows per :data:`ERROR_WINDOW_S` seconds (the rest is only logged).
MAX_ERROR_DIALOGS = 3
ERROR_WINDOW_S = 10.0

#: GIL switch interval of the GUI process (seconds; Python's default is 0.005).
SWITCH_INTERVAL_S = 0.0005
#: The GUI-thread garbage collector checks ``gc.get_count()`` this often.
GC_TICK_MS = 50
#: Full collections: when at least this many objects (or a quarter of the heap) survived
#: young collections since the last one, at most once per GC_FULL_MIN_INTERVAL_S seconds
#: and at most GC_FULL_MAX_SHARE of the time.
GC_FULL_MIN_PROMOTED = 200_000
GC_FULL_MIN_INTERVAL_S = 10.0
GC_FULL_MAX_SHARE = 0.02
#: A young collection of at least this many new objects waits while the user is busy
#: (input in the last GC_USER_IDLE_MS ms) -- at most GC_MAX_DEFER_S seconds; a full
#: collection at most GC_FULL_MAX_DEFER_S seconds.
GC_DEFER_YOUNG = 100_000
GC_USER_IDLE_MS = 400
GC_MAX_DEFER_S = 2.0
GC_FULL_MAX_DEFER_S = 30.0
#: A full collection this many seconds after a tab was closed (GcPolicy.request_full).
GC_AFTER_CLOSE_S = 3.0
#: Threads not waited for before the automatic collector is enabled again: idle pool
#: threads that only work for a task whose own thread is waited for, and the watchers of
#: an earlier window that wait to enable it.
GC_IGNORED_THREADS = ("jsonreshape-schema-io", "jsonreshape-gc-restore")


def choose_language() -> str:
    """``JSONRESHAPE_LANG`` > ``Settings.language`` > ``uz`` (like the CLI)."""
    env = (os.environ.get("JSONRESHAPE_LANG") or "").strip().lower()
    if env in SUPPORTED_LANGUAGES:
        return env
    try:
        from jsonreshape.settings import get_settings

        language = get_settings().language
        if language in SUPPORTED_LANGUAGES:
            return language
    except Exception:
        log.warning("could not read the language setting", exc_info=True)
    return "uz"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Windows: give the process its own AppUserModelID (taskbar icon / grouping); no-op
    elsewhere. True when it was set."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        log.debug("SetCurrentProcessExplicitAppUserModelID failed", exc_info=True)
        return False


def make_app_icon() -> Any:
    """The application icon (a blue rounded square with ``{ }``), drawn in several sizes."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            rect = QRectF(0.5, 0.5, size - 1.0, size - 1.0)
            radius = size * 0.22
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#2a57c4"))
            painter.drawRoundedRect(rect, radius, radius)
            font = QFont()
            font.setBold(True)
            font.setPixelSize(max(6, int(size * 0.5)))
            painter.setFont(font)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), "{ }")
        finally:
            painter.end()
        icon.addPixmap(pixmap)
    return icon


def apply_ui_size(app: Any, size: Any) -> bool:
    """Use ``size`` points for the application font (the ``ui_size`` GUI setting; 0 /
    invalid = keep the system UI font). True when the font was changed."""
    try:
        points = float(size or 0)
    except (TypeError, ValueError):
        return False
    if points <= 0 or app is None:
        return False
    from PySide6.QtGui import QFont

    font = QFont(app.font())
    font.setPointSizeF(min(points, 72.0))
    app.setFont(font)
    return True


def install_qt_excepthook(app_ctx: Any) -> Callable[[], None]:
    """Report uncaught exceptions of the main thread (``sys.excepthook``, which PySide
    calls for exceptions in slots) and of other threads (``threading.excepthook``) with
    ``app_ctx.report_error`` in the GUI thread instead of ending the program. Every
    exception is logged; at most :data:`MAX_ERROR_DIALOGS` windows per
    :data:`ERROR_WINDOW_S` seconds are shown. Returns a function that restores the
    previous hooks."""
    from PySide6.QtCore import QObject, Qt, Signal

    class _Bridge(QObject):
        raised = Signal(object)

    hook_log = logging.getLogger("jsonreshape.uncaught")
    bridge = _Bridge(app_ctx)
    shown: list[float] = []

    def report(exc: Any) -> None:
        now = time.monotonic()
        shown[:] = [x for x in shown if now - x < ERROR_WINDOW_S]
        if len(shown) >= MAX_ERROR_DIALOGS:
            if not getattr(app_ctx, "headless", False):
                try:   # (already logged by the hook) counted in the "N more errors" notice
                    from jsonreshape.gui.widgets import count_hidden_error

                    count_hidden_error(None, exc)
                except Exception:
                    hook_log.exception("counting a hidden uncaught exception failed")
            return
        shown.append(now)
        try:
            app_ctx.report_error(exc)
        except Exception:
            hook_log.exception("reporting an uncaught exception failed")

    bridge.raised.connect(report, Qt.ConnectionType.QueuedConnection)
    previous = sys.excepthook
    previous_thread = threading.excepthook

    def excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        try:
            hook_log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
            if exc is not None and exc.__traceback__ is None and tb is not None:
                exc = exc.with_traceback(tb)
            bridge.raised.emit(exc if exc is not None else exc_type())
        except Exception:  # pragma: no cover - never fail inside the hook
            pass

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is None or issubclass(args.exc_type, (SystemExit, KeyboardInterrupt)):
            previous_thread(args)
            return
        try:
            name = args.thread.name if args.thread is not None else "?"
            hook_log.critical("uncaught exception in thread %s", name,
                              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            bridge.raised.emit(args.exc_value)
        except Exception:  # pragma: no cover
            pass

    excepthook._jsonreshape_qt_hook = True  # type: ignore[attr-defined]
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook

    def uninstall() -> None:
        if sys.excepthook is excepthook:
            sys.excepthook = previous
        if threading.excepthook is thread_excepthook:
            threading.excepthook = previous_thread
        try:
            bridge.raised.disconnect(report)
        except (RuntimeError, TypeError):
            pass

    return uninstall


def _input_idle_ms() -> float | None:
    """Milliseconds since the last keyboard / mouse input of the session (Windows:
    ``GetLastInputInfo``); ``None`` when unknown."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = _LastInput()
        info.cbSize = ctypes.sizeof(_LastInput)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return float((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF)
    except Exception:  # pragma: no cover - no user32
        return None


def user_busy() -> bool:
    """The user is interacting right now: input in the last :data:`GC_USER_IDLE_MS` ms
    (Windows), or a mouse button is held (dragging, everywhere)."""
    idle = _input_idle_ms()
    if idle is not None and idle < GC_USER_IDLE_MS:
        return True
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication.mouseButtons() != Qt.MouseButton.NoButton
    except Exception:  # pragma: no cover
        return False


class GcPolicy:
    """The GUI-thread garbage collection of :func:`install_gc_policy` (see the module
    docstring). :meth:`tick` is what the timer calls. Diagnostics: ``collections`` per
    generation, ``max_pause`` / ``total_pause`` (seconds), ``deferred`` (ticks a collection
    waited for the user), ``promoted`` (objects that survived young collections since the
    last full one), ``requested`` (full collections asked for by :meth:`request_full`),
    ``last_full_pause`` (seconds)."""

    def __init__(self, *, young_threshold: int | None = None,
                 user_active: Callable[[], bool] = user_busy) -> None:
        threshold = gc.get_threshold()[0]
        self.young_threshold = max(1, int(young_threshold if young_threshold is not None else threshold or 700))
        self.user_active = user_active
        self.collections = [0, 0, 0]
        self.max_pause = 0.0
        self.total_pause = 0.0
        self.deferred = 0
        self.promoted = 0
        self.heap_after_full = 0
        self.requested = 0
        self.last_full_pause = 0.0
        self._deferred_since: float | None = None
        self._next_full = time.monotonic() + GC_FULL_MIN_INTERVAL_S
        self._full_wanted: float | None = None     # a requested full collection is due then
        self._full_first: float | None = None      # ... asked for first then
        self._next_requested = 0.0                 # requested ones: not before (time share)
        self._timer: Any = None

    def request_full(self, delay: float | None = None) -> None:
        """Ask for a full collection ``delay`` seconds (default :data:`GC_AFTER_CLOSE_S`)
        from now (a tab was closed: its session and views are cyclic garbage only a full
        collection frees). Requests in
        a row are merged: the collection runs ``delay`` after the last one, but at most
        :data:`GC_FULL_MAX_DEFER_S` after the first; it also waits until the previous
        full collection is :data:`GC_FULL_MAX_SHARE` of the time ago, and while the user
        is busy (bounded, see :meth:`tick`)."""
        now = time.monotonic()
        wait = GC_AFTER_CLOSE_S if delay is None else delay
        if self._full_first is None:
            self._full_first = now
        self._full_wanted = min(now + max(0.0, float(wait)), self._full_first + GC_FULL_MAX_DEFER_S)
        self.requested += 1

    def full_due(self) -> bool:
        """A requested full collection is due, or enough objects were promoted since the
        last full collection and its time has come."""
        now = time.monotonic()
        wanted = self._full_wanted
        if wanted is not None and now >= wanted and now >= self._next_requested:
            return True
        return (self.promoted >= max(GC_FULL_MIN_PROMOTED, self.heap_after_full // 4)
                and now >= self._next_full)

    def choose(self) -> int | None:
        """The generation to collect now (``None``: nothing to do)."""
        if self.full_due():
            return 2
        if gc.get_count()[0] >= self.young_threshold:
            return 0
        return None

    def collect(self, generation: int) -> int:
        young = gc.get_count()[0]
        started = time.perf_counter()
        found = gc.collect(generation)
        took = time.perf_counter() - started
        self.collections[generation] += 1
        self.max_pause = max(self.max_pause, took)
        self.total_pause += took
        if generation == 2:
            self.promoted = 0
            self.heap_after_full = len(gc.get_objects())
            self.last_full_pause = took
            now = time.monotonic()
            self._next_full = now + max(GC_FULL_MIN_INTERVAL_S, took / GC_FULL_MAX_SHARE)
            self._next_requested = now + took / GC_FULL_MAX_SHARE
            self._full_wanted = self._full_first = None
        else:
            self.promoted += max(0, young - found)
        if took > 0.05:
            log.debug("gc.collect(%d) took %.0f ms (%d unreachable)", generation, took * 1000, found)
        return found

    def tick(self) -> None:
        try:
            generation = self.choose()
            if generation is None:
                self._deferred_since = None
                return
            if generation == 2 or gc.get_count()[0] >= GC_DEFER_YOUNG:
                # a long collection: not while the user scrolls / types (bounded wait)
                now = time.monotonic()
                if self._deferred_since is None:
                    self._deferred_since = now
                limit = GC_FULL_MAX_DEFER_S if generation == 2 else GC_MAX_DEFER_S
                if now - self._deferred_since < limit and self.user_active():
                    self.deferred += 1
                    return
            self._deferred_since = None
            self.collect(generation)
        except Exception:  # pragma: no cover - never let the timer die
            log.exception("garbage collection failed")


_gc_policy: GcPolicy | None = None
_gc_enable_pending = False      # a watcher enables the automatic collector when its threads end


def _worker_threads() -> list[threading.Thread]:
    """The threads besides this one and the main thread that may still run Python code
    (:data:`GC_IGNORED_THREADS` excepted)."""
    me = threading.current_thread()
    main_thread = threading.main_thread()
    return [th for th in threading.enumerate()
            if th is not me and th is not main_thread and th.is_alive() and not th.name.startswith(GC_IGNORED_THREADS)]


def _enable_gc_after_workers() -> threading.Thread | None:
    """``gc.enable()`` -- now when no worker thread runs, else once the threads alive now
    have ended (a daemon watcher thread; returned), unless a policy was installed again
    meanwhile (it then decides). Never while one of them may still allocate: the
    automatic collector would run in that thread (T1)."""
    global _gc_enable_pending
    workers = _worker_threads()
    if not workers:
        _gc_enable_pending = False
        gc.enable()
        return None
    _gc_enable_pending = True
    log.debug("automatic garbage collection stays off until %d threads end: %s", len(workers),
              ", ".join(th.name for th in workers))

    def watch() -> None:
        global _gc_enable_pending
        for th in workers:
            th.join()
        if _gc_enable_pending and _gc_policy is None:
            _gc_enable_pending = False
            gc.enable()
            log.debug("automatic garbage collection enabled again (worker threads ended)")

    watcher = threading.Thread(target=watch, name="jsonreshape-gc-restore", daemon=True)
    watcher.start()
    return watcher


def install_gc_policy(app: Any, *, interval_ms: int = GC_TICK_MS, freeze: bool = True) -> Callable[[], None]:
    """Run the cyclic garbage collector only in the GUI thread (see the module docstring):
    ``gc.disable()``, ``gc.freeze()`` of everything that exists now (``freeze``), and a
    ``QTimer`` (parent ``app``, every ``interval_ms``) that runs :meth:`GcPolicy.tick`.
    Call it in the GUI thread. Returns the function that undoes it (timer stopped,
    objects unfrozen; automatic collection enabled again if it was -- once no worker
    thread runs any more, see :func:`_enable_gc_after_workers`)."""
    global _gc_policy, _gc_enable_pending
    from PySide6.QtCore import QTimer

    was_enabled = gc.isenabled() or _gc_enable_pending   # an earlier window's deferred enable
    _gc_enable_pending = False
    if freeze:
        gc.collect()
        gc.freeze()
    gc.disable()
    policy = GcPolicy()
    timer = QTimer(app)
    timer.setObjectName("jsonreshape-gc")
    timer.setInterval(max(1, int(interval_ms)))
    timer.timeout.connect(policy.tick)
    timer.start()
    policy._timer = timer
    _gc_policy = policy
    log.debug("garbage collection runs in the GUI thread (every %d ms, young threshold %d)", interval_ms,
              policy.young_threshold)
    done = False

    def uninstall() -> None:
        global _gc_policy
        nonlocal done
        if done:
            return
        done = True
        try:
            timer.stop()
            timer.deleteLater()
        except RuntimeError:   # the application (timer parent) is already gone
            pass
        if _gc_policy is policy:
            _gc_policy = None
        if freeze:
            gc.unfreeze()
        if was_enabled:
            _enable_gc_after_workers()

    return uninstall


def gc_policy() -> GcPolicy | None:
    """The installed :class:`GcPolicy` (``None`` when the automatic collector is used)."""
    return _gc_policy


def _collect_after_tab_close(_tab: Any = None) -> None:
    """``MainWindow.tab_closed``: a full collection soon (:meth:`GcPolicy.request_full`)."""
    policy = _gc_policy
    if policy is not None:
        policy.request_full()


def set_switch_interval(seconds: float = SWITCH_INTERVAL_S) -> Callable[[], None]:
    """``sys.setswitchinterval(seconds)``; returns the function that restores the old
    interval."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(seconds)

    def restore() -> None:
        if sys.getswitchinterval() == seconds:
            sys.setswitchinterval(previous)

    return restore


def _log_environment() -> None:
    try:
        from jsonreshape.env import check_environment

        check_environment(log_result=True)
    except Exception:
        log.exception("environment check failed")


def main(argv: list[str] | None = None, *, run: bool = True) -> Any:
    """Start the viewer with the files in ``argv`` (default: ``sys.argv[1:]``).

    Returns the exit code; with ``run=False`` (tests) the shown
    :class:`~jsonreshape.gui.mainwindow.MainWindow` is returned before the event loop
    would start (closing it shuts the application context down)."""
    try:
        require_qt()
    except UserInputError as exc:
        logging.getLogger(__name__).error("%s", exc.message)
        stream = sys.stderr
        if stream is not None:
            stream.write(exc.message + "\n" + (exc.hint or "") + "\n")
        return 2

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from jsonreshape import __version__
    from jsonreshape.gui import theme
    from jsonreshape.gui.context import AppContext
    from jsonreshape.gui.mainwindow import MainWindow
    from jsonreshape.log import install_excepthooks, setup_logging

    args = list(sys.argv[1:] if argv is None else argv)
    setup_logging()
    install_excepthooks()
    log.info("starting the viewer (jsonreshape %s)", __version__)
    threading.Thread(target=_log_environment, name="jsonreshape-env-check", daemon=True).start()
    set_language(choose_language())

    app = QApplication.instance()
    created = app is None
    if created:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        set_app_user_model_id()
        app = QApplication([sys.argv[0] if sys.argv and sys.argv[0] else "jsonreshape-gui"] + args)
        files = list(app.arguments()[1:])   # Qt removed its own options (-style ...)
    else:
        files = args
    app.setOrganizationName(ORGANIZATION)
    app.setApplicationName(APPLICATION)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(make_app_icon())

    app_ctx = AppContext()
    theme.apply_theme(app, str(app_ctx.state.get("theme") or "system"))
    apply_ui_size(app, app_ctx.state.get("ui_size"))
    uninstall_hooks = install_qt_excepthook(app_ctx)
    restore_switch = set_switch_interval(SWITCH_INTERVAL_S)
    window = MainWindow(app_ctx)
    # after start-up (the window exists), before any document is opened: what is frozen now
    # lives as long as the process
    uninstall_gc = install_gc_policy(app)
    window.tab_closed.connect(_collect_after_tab_close)

    def uninstall() -> None:
        uninstall_hooks()
        restore_switch()
        uninstall_gc()

    window.closed.connect(uninstall)
    paths = []
    for item in files:
        if item.startswith("-") and not os.path.exists(item):
            log.warning("ignoring the unknown option %r", item)
            continue
        paths.append(item)
    # empty arguments ignored, duplicates opened once, folders skipped with a message
    window.open_command_line(paths)
    window.show()
    if not run:
        return window
    try:
        code = app.exec()
    finally:
        if not window._closing:
            window.close()
        uninstall()
    log.info("the viewer exited with code %s", code)
    return int(code)
