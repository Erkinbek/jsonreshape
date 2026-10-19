"""GUI garbage-collection policy (round-2 fix R2-19, ``jsonreshape.gui.app``).

* a closed tab's session is cyclic garbage that only a full collection frees: the policy
  runs one a few seconds after ``tab_closed`` (merged for tabs closed in a row; bounded by
  the time share, the user's activity and a maximum wait) -- without it the session stays;
* the automatic collector is never enabled again while a worker thread may still run:
  only once the threads alive at uninstall time have ended (idle schema read-ahead pool
  threads excepted); a policy installed again meanwhile keeps the original setting;
* the module docstring states the real heavy-task limit.
"""

from __future__ import annotations

import gc
import threading
import time
import weakref

import pytest

from guishell_helpers import records, wait_ready, write_json
from jsonreshape.gui import app as app_mod
from jsonreshape.gui import qtasks

pytestmark = pytest.mark.gui


@pytest.fixture
def gc_state(monkeypatch):
    """Restore the collector afterwards; never wait for pytest-timeout's timer thread."""
    was = gc.isenabled()
    monkeypatch.setattr(app_mod, "GC_IGNORED_THREADS", app_mod.GC_IGNORED_THREADS + ("pytest_timeout",))
    yield
    app_mod._gc_enable_pending = False
    if was:
        gc.enable()
    else:
        gc.disable()


@pytest.fixture
def clean_process(qapp, monkeypatch):
    """main() installs process-wide hooks, logging, language, font, theme: restore them."""
    import sys

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from jsonreshape import i18n
    from jsonreshape.gui import theme

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    old_lang = i18n.get_language()
    old_font = QFont(QApplication.font())
    old_switch = sys.getswitchinterval()
    yield
    sys.setswitchinterval(old_switch)
    QApplication.setFont(old_font)
    from jsonreshape.log import shutdown_logging

    shutdown_logging()
    i18n.set_language(old_lang)
    theme.apply_theme(qapp, "system")
    theme._reset_for_tests()


class _Cycle:
    """Cyclic garbage that survived young collections (like a closed session)."""

    def __init__(self) -> None:
        self.me = self


def _old_cycle() -> weakref.ref:
    obj = _Cycle()
    ref = weakref.ref(obj)
    gc.collect(0)      # still referenced: it survives the young collection (an old object now)
    del obj            # garbage now -- that only a full collection finds
    return ref


# ----------------------------------------------------------------------------------------
# GcPolicy.request_full
# ----------------------------------------------------------------------------------------
def test_requested_full_collection_frees_old_cycles(gc_state, monkeypatch) -> None:
    gc.disable()
    monkeypatch.setattr(app_mod, "GC_FULL_MIN_PROMOTED", 10 ** 12)   # never due by promotions
    pol = app_mod.GcPolicy(user_active=lambda: False)
    ref = _old_cycle()
    gc.collect(0)
    pol.tick()
    assert ref() is not None and pol.collections[2] == 0           # young collections do not free it
    pol.request_full(0.05)
    pol.tick()
    assert ref() is not None and pol.collections[2] == 0           # not before the delay
    time.sleep(0.07)
    pol.tick()
    assert ref() is None and pol.collections[2] == 1 and pol.requested == 1
    assert pol.last_full_pause > 0
    pol.tick()
    assert pol.collections[2] == 1                                 # done: no second one


def test_requests_in_a_row_are_merged_and_bounded(gc_state, monkeypatch) -> None:
    gc.disable()
    monkeypatch.setattr(app_mod, "GC_FULL_MIN_PROMOTED", 10 ** 12)
    monkeypatch.setattr(app_mod, "GC_FULL_MAX_DEFER_S", 0.3)
    pol = app_mod.GcPolicy(user_active=lambda: False)
    for _ in range(3):                    # three tabs closed in a row: one collection
        pol.request_full(0.1)
        time.sleep(0.04)
        pol.tick()
    assert pol.collections[2] == 0
    time.sleep(0.12)
    pol.tick()
    pol.tick()
    assert pol.collections[2] == 1 and pol.requested == 3
    # requests that keep coming: at most GC_FULL_MAX_DEFER_S after the first one
    pol._next_requested = 0.0
    start = time.monotonic()
    while pol.collections[2] == 1 and time.monotonic() - start < 2.0:
        pol.request_full(0.1)
        time.sleep(0.02)
        pol.tick()
    # the bound is on when the collection STARTS (GC_FULL_MAX_DEFER_S after the first request), not on how long a full
    # collection of this test process' heap takes (it grows with the number of tests run before this one)
    assert pol.collections[2] == 2
    assert time.monotonic() - start - pol.last_full_pause < 0.6


def test_requested_collection_respects_the_time_share_and_the_user(gc_state, monkeypatch) -> None:
    gc.disable()
    monkeypatch.setattr(app_mod, "GC_FULL_MIN_PROMOTED", 10 ** 12)
    monkeypatch.setattr(app_mod, "GC_FULL_MAX_SHARE", 0.5)
    busy = [False]
    pol = app_mod.GcPolicy(user_active=lambda: busy[0])
    pol.request_full(0.0)
    pol.tick()
    assert pol.collections[2] == 1
    # the next requested one waits until the last one is GC_FULL_MAX_SHARE of the time ago
    pol._next_requested = time.monotonic() + 0.15     # (as after a 75 ms collection)
    pol.request_full(0.0)
    pol.tick()
    assert pol.collections[2] == 1
    time.sleep(0.16)
    # ... and while the user is busy -- at most GC_FULL_MAX_DEFER_S
    monkeypatch.setattr(app_mod, "GC_FULL_MAX_DEFER_S", 0.15)
    busy[0] = True
    pol.tick()
    pol.tick()
    assert pol.collections[2] == 1 and pol.deferred >= 2
    time.sleep(0.17)
    pol.tick()
    assert pol.collections[2] == 2


def test_closed_tab_session_is_freed_soon(qtbot, qapp, tmp_path, monkeypatch, gc_state, clean_process) -> None:
    """End to end: main() connects ``tab_closed`` to the policy; the closed document's
    session (cyclic garbage) is freed by the requested full collection -- and only by it."""
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(app_mod, "GC_FULL_MIN_PROMOTED", 10 ** 12)   # nothing else collects fully
    monkeypatch.setattr(app_mod, "GC_FULL_MIN_INTERVAL_S", 10 ** 6)
    monkeypatch.setattr(app_mod, "user_busy", lambda: False)
    a = write_json(tmp_path / "a.json", records(50))
    b = write_json(tmp_path / "b.json", records(60))
    window = app_mod.main([str(a), str(b)], run=False)
    qtbot.addWidget(window)
    try:
        policy = app_mod.gc_policy()
        assert policy is not None
        policy.user_active = lambda: False
        tabs = window.tab_widgets()
        for tab in tabs:
            wait_ready(qtbot, tab.session)
        del tab
        gc.collect(0)      # the sessions are old objects now (young collections do not free them)
        # control: with a far-away request the closed session stays (cycles)
        monkeypatch.setattr(app_mod, "GC_AFTER_CLOSE_S", 1000.0)
        first = weakref.ref(tabs[0].session)
        window.close_tab(window.tabs.indexOf(tabs[0]))
        del tabs
        for _ in range(5):
            qtbot.wait(100)
            QApplication.processEvents()
        assert first() is not None and policy.collections[2] == 0 and policy.requested == 1
        # the requested collection a little later frees it
        monkeypatch.setattr(app_mod, "GC_AFTER_CLOSE_S", 0.2)
        second_tab = window.tab_widgets()[0]
        second = weakref.ref(second_tab.session)
        window.close_tab(window.tabs.indexOf(second_tab))
        del second_tab
        qtbot.waitUntil(lambda: second() is None and first() is None, timeout=5000)
        assert policy.collections[2] >= 1 and policy.requested == 2
    finally:
        window.close()
        window.app.tasks.wait_idle(30)


# ----------------------------------------------------------------------------------------
# enabling the automatic collector again
# ----------------------------------------------------------------------------------------
def test_gc_stays_off_while_a_worker_runs_after_uninstall(qtbot, qapp, gc_state) -> None:
    gc.enable()
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="jsonreshape-gui-sort-99", daemon=True)
    worker.start()
    uninstall = app_mod.install_gc_policy(qapp, interval_ms=10)
    assert not gc.isenabled()
    uninstall()
    assert app_mod.gc_policy() is None and gc.get_freeze_count() == 0
    time.sleep(0.2)
    assert not gc.isenabled() and app_mod._gc_enable_pending       # the worker still runs
    # a policy installed meanwhile keeps the original setting and decides itself
    again = app_mod.install_gc_policy(qapp, interval_ms=10, freeze=False)
    release.set()
    worker.join(5)
    time.sleep(0.2)
    assert not gc.isenabled()                                      # the new policy is in charge
    again()
    assert gc.isenabled()                                          # no worker any more: at once


def test_gc_enabled_when_the_workers_end(qtbot, qapp, gc_state) -> None:
    gc.enable()
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="jsonreshape-raw-pages", daemon=True)
    pool_like = threading.Thread(target=threading.Event().wait, args=(5,), name="jsonreshape-schema-io_0",
                                 daemon=True)
    worker.start()
    pool_like.start()
    uninstall = app_mod.install_gc_policy(qapp, interval_ms=10)
    uninstall()
    assert not gc.isenabled()
    release.set()
    qtbot.waitUntil(gc.isenabled, timeout=5000)                     # the idle pool thread is not waited for
    assert pool_like.is_alive() and not app_mod._gc_enable_pending


def test_the_edit_layer_reaper_is_not_waited_for(qtbot, qapp, gc_state) -> None:
    # regression: the edit layer's file reaper (a daemon living as long as the process) kept the
    # collector off for good after the first window with an edit layer had closed
    from jsonreshape import editstore
    editstore._start_reaper()
    reaper = editstore._REAPER
    assert reaper is not None and reaper.is_alive()
    assert reaper not in app_mod._worker_threads()
    gc.enable()
    uninstall = app_mod.install_gc_policy(qapp, interval_ms=10)
    uninstall()
    qtbot.waitUntil(gc.isenabled, timeout=5000)
    assert reaper.is_alive() and not app_mod._gc_enable_pending


def test_docstring_states_the_heavy_task_limit() -> None:
    doc = app_mod.__doc__ or ""
    assert "one at a time" not in doc and "MAX_HEAVY_TASKS" in doc
    assert qtasks.MAX_HEAVY_TASKS == 2 and "(two:" in doc
