"""Application entry point (jsonreshape/gui/app.py; ARCHITECTURE §11.1): ``main(argv,
run=False)`` opens the command-line files in a shown window (logging, language, theme,
icon), the event loop path, the missing-PySide6 message, the Qt-aware excepthook (slots,
callbacks, worker threads → the error window, rate-limited, uninstalled on close), the
language choice, the Windows AppUserModelID and the drawn icon."""

from __future__ import annotations

import gc
import sys
import threading
import time
import weakref

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from guishell_helpers import TIMEOUT, make_app, records, wait_ready, write_json
from jsonreshape import i18n
from jsonreshape.errors import UserInputError
from jsonreshape.gui import app as app_mod
from jsonreshape.gui import theme
from jsonreshape.gui.mainwindow import MainWindow

pytestmark = pytest.mark.gui


@pytest.fixture
def clean_process(qapp, monkeypatch):
    """main() installs process-wide hooks and logging: restore them afterwards."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    old_lang = i18n.get_language()
    old_font = QFont(QApplication.font())
    old_switch = sys.getswitchinterval()
    gc_was_enabled = gc.isenabled()
    yield
    sys.setswitchinterval(old_switch)
    if gc_was_enabled:
        gc.enable()
    QApplication.setFont(old_font)
    from jsonreshape.log import shutdown_logging

    shutdown_logging()
    i18n.set_language(old_lang)
    theme.apply_theme(qapp, "system")
    theme._reset_for_tests()


def close_window(qtbot, window: MainWindow) -> None:
    window.close()
    assert window.app.tasks.wait_idle(30)


@pytest.fixture
def no_test_timer_wait(monkeypatch):
    """pytest-timeout runs a Timer thread for the whole test: like the schema pool's idle
    threads it never allocates for the app, so the GC policy need not wait for it before
    enabling the automatic collector again (R2-19)."""
    monkeypatch.setattr(app_mod, "GC_IGNORED_THREADS", app_mod.GC_IGNORED_THREADS + ("pytest_timeout",))


def test_main_opens_the_command_line_files(qtbot, clean_process, tmp_path, no_test_timer_wait) -> None:
    a = write_json(tmp_path / "a.json", records(12))
    b = tmp_path / "b.jsonl"
    b.write_text('{"x": 1}\n{"x": 2}\n', encoding="utf-8")
    hook_before = sys.excepthook
    switch_before = sys.getswitchinterval()
    gc_before = gc.isenabled()
    window = app_mod.main([str(a), "--unknown-flag", str(b)], run=False)
    qtbot.addWidget(window)
    try:
        assert isinstance(window, MainWindow) and window.isVisible()
        assert [t.session.name for t in window.tab_widgets()] == ["a.json", "b.jsonl"]
        for tab in window.tab_widgets():
            wait_ready(qtbot, tab.session)
            assert tab.session.state == "ready"
        assert window.tab_widgets()[0].session.doc.record_count == 12
        assert window.windowTitle() == "b.jsonl — JSON Reshape"
        qapp = QApplication.instance()
        assert not qapp.windowIcon().isNull()
        assert qapp.organizationName() == "jsonreshape"
        assert i18n.get_language() == "uz"
        assert theme.mode() == "system"
        assert getattr(sys.excepthook, "_jsonreshape_qt_hook", False)
        from jsonreshape.log import log_path

        assert log_path().exists()
        # runtime policy (T1, T2): GC only in the GUI thread, short GIL switch interval
        assert not gc.isenabled() and app_mod.gc_policy() is not None and gc.get_freeze_count() > 0
        assert sys.getswitchinterval() == app_mod.SWITCH_INTERVAL_S
    finally:
        close_window(qtbot, window)
    assert sys.excepthook is not None and not getattr(sys.excepthook, "_jsonreshape_qt_hook", False)
    assert hook_before is not None
    assert app_mod.gc_policy() is None and gc.get_freeze_count() == 0
    # R2-19: the automatic collector comes back (if it was on) once the worker threads have
    # ended (loaders, the documents' closing thread ...), not while one may still run
    if gc_before:
        qtbot.waitUntil(gc.isenabled, timeout=15000)
    else:
        assert not gc.isenabled()
    assert sys.getswitchinterval() == switch_before


def test_main_skips_empty_duplicate_and_folder_arguments(qtbot, clean_process, tmp_path) -> None:
    """L10: an empty argument does not open the current folder, a file given twice opens
    one tab, a folder opens nothing."""
    a = write_json(tmp_path / "a.json", records(3))
    folder = tmp_path / "dir"
    folder.mkdir()
    window = app_mod.main(["", str(a), str(a), "  ", str(folder)], run=False)
    qtbot.addWidget(window)
    try:
        assert [t.session.name for t in window.tab_widgets()] == ["a.json"]
        wait_ready(qtbot, window.tab_widgets()[0].session)
    finally:
        close_window(qtbot, window)


def test_main_with_a_missing_file_does_not_fail(qtbot, clean_process, tmp_path) -> None:
    window = app_mod.main([str(tmp_path / "missing.json")], run=False)
    qtbot.addWidget(window)
    try:
        tab = window.current_tab()
        wait_ready(qtbot, tab.session)
        assert tab.session.state == "failed" and tab.info_bar("open_failed") is not None
    finally:
        close_window(qtbot, window)


def test_non_ascii_arguments_pass_qt_as_placeholders() -> None:
    """Qt rebuilds its arguments from local 8-bit bytes (the ANSI code page on Windows): a name outside it opened as
    "ma?lumot ...". Non-ASCII arguments go to Qt as ASCII placeholders and come back as they were."""
    args = ["-style", "fusion", "maʻlumot ўзбек.json", "plain.json", "δεδομένα 数据.json"]
    qt_args, originals = app_mod.qt_safe_arguments(args)
    assert qt_args[:2] == ["-style", "fusion"] and qt_args[3] == "plain.json"
    assert all(a.isascii() and not a.startswith("-") for a in qt_args[2:])
    assert [originals.get(a, a) for a in qt_args] == args
    assert app_mod.qt_safe_arguments(["a.json"]) == (["a.json"], {})


def test_main_applies_the_saved_theme(qtbot, clean_process, tmp_path) -> None:
    from jsonreshape.gui.guistate import GuiState

    state = GuiState.load()
    state.set("theme", "dark")
    state.set("ui_size", 13)
    state.save()
    window = app_mod.main([], run=False)
    qtbot.addWidget(window)
    try:
        assert theme.mode() == "dark"
        assert QApplication.font().pointSizeF() == 13.0
        assert window.action("theme_dark").isChecked()
        assert window.current_tab() is None
    finally:
        close_window(qtbot, window)


def test_main_runs_the_event_loop(qtbot, clean_process, monkeypatch, tmp_path) -> None:
    """The event loop path (``app.exec()`` replaced: quitting the real application loop
    would break the event loops of the following tests in this process)."""
    a = write_json(tmp_path / "a.json", records(3))
    qapp = QApplication.instance()
    seen: list[MainWindow] = []

    def fake_exec() -> int:
        windows = [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()]
        seen.extend(windows)
        tab = windows[-1].current_tab()
        wait_ready(qtbot, tab.session)
        return 3

    monkeypatch.setattr(qapp, "exec", fake_exec)
    assert app_mod.main([str(a)]) == 3
    window = seen[-1]
    assert window._closing and not window.isVisible()        # closed after the loop ended
    assert window.app.tasks.wait_idle(30)
    assert not getattr(sys.excepthook, "_jsonreshape_qt_hook", False)


def test_apply_ui_size(qapp) -> None:
    old = QFont(QApplication.font())
    try:
        assert app_mod.apply_ui_size(qapp, 0) is False
        assert app_mod.apply_ui_size(qapp, "x") is False
        assert app_mod.apply_ui_size(qapp, None) is False
        assert QApplication.font() == old
        assert app_mod.apply_ui_size(qapp, 11) is True
        assert QApplication.font().pointSizeF() == 11.0
    finally:
        QApplication.setFont(old)


def test_main_without_pyside(monkeypatch, capsys) -> None:
    def missing() -> None:
        raise UserInputError("gui.app.no_qt")

    monkeypatch.setattr(app_mod, "require_qt", missing)
    assert app_mod.main([]) == 2
    err = capsys.readouterr().err
    assert "PySide6" in err and "pip install" in err


# ----------------------------------------------------------------------------------------
# the Qt-aware excepthook
# ----------------------------------------------------------------------------------------
@pytest.fixture
def ctx(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    c = make_app(tmp_path)
    yield c
    assert c.tasks.shutdown(30)


def test_excepthook_reports_instead_of_crashing(qtbot, ctx) -> None:
    previous = sys.excepthook
    previous_thread = threading.excepthook
    uninstall = app_mod.install_qt_excepthook(ctx)
    try:
        err = ValueError("boom")
        sys.excepthook(ValueError, err, None)
        qtbot.waitUntil(lambda: err in ctx.errors, timeout=TIMEOUT)

        def slot() -> None:
            raise ZeroDivisionError("in a slot")

        QTimer.singleShot(0, slot)
        qtbot.waitUntil(lambda: any(isinstance(e, ZeroDivisionError) for e in ctx.errors), timeout=TIMEOUT)

        def worker() -> None:
            raise KeyError("in a thread")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        qtbot.waitUntil(lambda: any(isinstance(e, KeyError) for e in ctx.errors), timeout=TIMEOUT)
    finally:
        uninstall()
    assert sys.excepthook is previous and threading.excepthook is previous_thread


def test_excepthook_rate_limit(qtbot, ctx) -> None:
    uninstall = app_mod.install_qt_excepthook(ctx)
    try:
        for i in range(20):
            sys.excepthook(RuntimeError, RuntimeError(f"e{i}"), None)
        qtbot.wait(200)
        assert len(ctx.errors) == app_mod.MAX_ERROR_DIALOGS
    finally:
        uninstall()


def test_excepthook_flood_shows_three_windows_and_one_notice(qtbot, qapp, tmp_path, monkeypatch) -> None:
    """M6: a flood of uncaught exceptions opens at most three error windows; the rest is
    counted in one "N more errors" notice (and logged)."""
    from jsonreshape.gui import widgets
    from jsonreshape.gui.context import AppContext
    from jsonreshape.gui.guistate import GuiState

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    widgets.reset_error_limit()
    real = AppContext(state=GuiState.load(tmp_path / "s.json"), headless=False)
    uninstall = app_mod.install_qt_excepthook(real)

    def windows():
        tops = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
        return ([w for w in tops if isinstance(w, widgets.ErrorDialog)],
                [w for w in tops if isinstance(w, widgets.ErrorNotice)])

    try:
        for i in range(12):
            sys.excepthook(RuntimeError, RuntimeError(f"e{i}"), None)
        qtbot.waitUntil(lambda: len(windows()[1]) == 1 and windows()[1][0].count == 9, timeout=TIMEOUT)
        assert len(windows()[0]) == 3
    finally:
        uninstall()
        dialogs, notices = windows()
        for w in dialogs + notices:
            w.close()
        real.tasks.shutdown(5)
        widgets.reset_error_limit()


def test_excepthook_task_callback_errors_reach_the_context(qtbot, ctx) -> None:
    uninstall = app_mod.install_qt_excepthook(ctx)
    try:
        def done(_result) -> None:
            raise RuntimeError("callback bug")

        ctx.tasks.run(lambda c: 1, title="x", on_done=done)
        qtbot.waitUntil(lambda: any(isinstance(e, RuntimeError) for e in ctx.errors), timeout=TIMEOUT)
    finally:
        uninstall()


# ----------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------
def test_choose_language(monkeypatch) -> None:
    from jsonreshape import settings as settings_mod

    monkeypatch.setenv("JSONRESHAPE_LANG", "en")
    assert app_mod.choose_language() == "en"
    monkeypatch.setenv("JSONRESHAPE_LANG", "fr")
    monkeypatch.setattr(settings_mod, "get_settings", lambda: settings_mod.Settings(language="en"))
    assert app_mod.choose_language() == "en"
    monkeypatch.delenv("JSONRESHAPE_LANG")
    monkeypatch.setattr(settings_mod, "get_settings", lambda: settings_mod.Settings(language="uz"))
    assert app_mod.choose_language() == "uz"


def test_app_user_model_id(monkeypatch) -> None:
    if sys.platform == "win32":
        assert app_mod.set_app_user_model_id() is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert app_mod.set_app_user_model_id() is False


def test_app_icon(qapp) -> None:
    icon = app_mod.make_app_icon()
    sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert {(16, 16), (32, 32), (256, 256)} <= sizes
    image = icon.pixmap(64, 64).toImage()
    assert image.pixelColor(32, 2).alpha() > 0 and image.pixelColor(0, 0).alpha() < 255


def test_gui_package_entry_point() -> None:
    import jsonreshape.gui.__main__ as entry

    assert entry.main is app_mod.main


# ----------------------------------------------------------------------------------------
# runtime policy: garbage collection only in the GUI thread (T1), switch interval (T2)
# ----------------------------------------------------------------------------------------
@pytest.fixture
def gc_restored():
    was = gc.isenabled()
    yield
    if was:
        gc.enable()
    else:  # pragma: no cover
        gc.disable()


def test_gc_policy_collects_worker_garbage_in_the_gui_thread(qtbot, qapp, gc_restored, no_test_timer_wait) -> None:
    """Cyclic garbage made by a worker thread is collected -- by the GUI thread only."""
    starts: list[str] = []

    def on_gc(phase, info) -> None:
        if phase == "start":
            starts.append("main" if threading.current_thread() is threading.main_thread() else "worker")

    gc_before = gc.isenabled()
    uninstall = app_mod.install_gc_policy(qapp, interval_ms=10)
    gc.callbacks.append(on_gc)
    try:
        assert not gc.isenabled() and gc.get_freeze_count() > 0
        assert app_mod.gc_policy() is not None
        probe: list = []

        class Node:
            pass

        def worker() -> None:
            for i in range(60_000):    # 30 thresholds' worth of cycles
                a, b = Node(), Node()
                a.other, b.other = b, a
                if i == 0:
                    probe.append(weakref.ref(a))
            del a, b

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert starts == []                      # the worker never collected
        qtbot.waitUntil(lambda: probe[0]() is None, timeout=5000)
        assert starts and set(starts) == {"main"}
        assert sum(app_mod.gc_policy().collections) >= 1
    finally:
        gc.callbacks.remove(on_gc)
        uninstall()
    assert gc.isenabled() == gc_before and gc.get_freeze_count() == 0 and app_mod.gc_policy() is None   # no worker
    uninstall()                                  # idempotent


def _grow_young(n: int) -> list:
    return [[] for _ in range(n)]


def test_gc_policy_generations_and_deferral(monkeypatch, gc_restored) -> None:
    """Young collections when enough new objects exist; a full collection only after
    enough promotions (and its interval); long collections wait while the user is busy --
    a bounded time."""
    gc.collect()
    gc.disable()
    busy = [False]
    monkeypatch.setattr(app_mod, "GC_FULL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(app_mod, "GC_FULL_MIN_PROMOTED", 5000)
    pol = app_mod.GcPolicy(young_threshold=1000, user_active=lambda: busy[0])
    pol._next_full = 0.0
    gc.collect(0)
    assert pol.choose() is None
    keep = _grow_young(1500)
    assert pol.choose() == 0
    pol.tick()
    assert pol.collections == [1, 0, 0] and gc.get_count()[0] < 1000
    assert 1400 <= pol.promoted <= 1600              # the 1500 lists survived
    # young garbage is freed by the young collection right away
    probe: list = []

    class Node:
        pass

    for i in range(2000):
        x, y = Node(), Node()
        x.o, y.o = y, x
        if i == 0:
            probe.append(__import__("weakref").ref(x))
    del x, y
    pol.tick()
    assert probe[0]() is None and pol.collections == [2, 0, 0]
    # enough promotions: a full collection
    keep += _grow_young(4000)
    pol.tick()
    assert pol.collections[0] == 3 and pol.promoted >= 5000
    assert pol.choose() == 2
    pol.tick()
    assert pol.collections[2] == 1 and pol.promoted == 0 and pol.heap_after_full > 0
    assert pol.max_pause > 0 and pol.total_pause >= pol.max_pause
    # a long young collection waits while the user is busy -- at most GC_MAX_DEFER_S
    monkeypatch.setattr(app_mod, "GC_DEFER_YOUNG", 5000)
    monkeypatch.setattr(app_mod, "GC_MAX_DEFER_S", 0.2)
    monkeypatch.setattr(app_mod, "GC_FULL_MIN_PROMOTED", 10**9)
    keep += _grow_young(6000)
    busy[0] = True
    before = list(pol.collections)
    pol.tick()
    pol.tick()
    assert pol.collections == before and pol.deferred == 2
    time.sleep(0.25)
    pol.tick()
    assert pol.collections[0] == before[0] + 1          # waited long enough
    keep += _grow_young(6000)
    busy[0] = False
    pol.tick()
    assert pol.collections[0] == before[0] + 2 and pol.deferred == 2
    # small collections never wait
    keep += _grow_young(1500)
    busy[0] = True
    pol.tick()
    assert pol.collections[0] == before[0] + 3
    # a due full collection waits for the user too (GC_FULL_MAX_DEFER_S)
    monkeypatch.setattr(app_mod, "GC_FULL_MIN_PROMOTED", 1)
    monkeypatch.setattr(app_mod, "GC_FULL_MAX_DEFER_S", 0.2)
    pol.heap_after_full = 0          # (a quarter of the heap would be needed otherwise)
    pol._next_full = 0.0             # (and the 2 % time share since the last one)
    full_before = pol.collections[2]
    pol.tick()
    assert pol.collections[2] == full_before and pol.deferred == 3
    busy[0] = False
    pol.tick()
    assert pol.collections[2] == full_before + 1
    del keep


def test_set_switch_interval_restores() -> None:
    before = sys.getswitchinterval()
    restore = app_mod.set_switch_interval(0.0005)
    try:
        assert sys.getswitchinterval() == 0.0005
    finally:
        restore()
    assert sys.getswitchinterval() == before


def test_user_busy_is_cheap_and_boolean(qapp) -> None:
    started = time.perf_counter()
    for _ in range(100):
        value = app_mod.user_busy()
    assert isinstance(value, bool)
    assert time.perf_counter() - started < 1.0
