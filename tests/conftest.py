"""Shared pytest fixtures.

* every test runs with ``JSONRESHAPE_HOME`` pointing to a private temp dir (no writes to
  the real ``%LOCALAPPDATA%``);
* ``core_backend`` parametrizes a test over every available core backend;
* ``both_backends`` gives ``(python_module, rust_module_or_None)`` for equivalence tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


@pytest.fixture(scope="session", autouse=True)
def _session_app_home(tmp_path_factory):
    """Safety net: between tests and after a test's teardown (a worker thread that outlives its
    test) the app still writes into a temp folder, never into the real ``%LOCALAPPDATA%``."""
    mp = pytest.MonkeyPatch()
    mp.setenv("JSONRESHAPE_HOME", str(tmp_path_factory.mktemp("jsonreshape_session_home")))
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolated_app_home(tmp_path_factory, _session_app_home):
    home = tmp_path_factory.mktemp("jsonreshape_home")
    # a private MonkeyPatch: a test calling its own ``monkeypatch.undo()`` must not drop the
    # isolation (it did with the shared fixture: such tests wrote into the real app folder)
    mp = pytest.MonkeyPatch()
    mp.setenv("JSONRESHAPE_HOME", str(home))
    try:  # reset cached settings between tests when the module exists
        from jsonreshape import settings as _settings  # noqa: WPS433

        reset = getattr(_settings, "_reset_for_tests", None)
        if reset is not None:
            reset()
    except Exception:
        pass
    try:
        yield home
    finally:
        mp.undo()


# test modules that install / check the app's GC policy themselves
_GC_SELF_MANAGED = {"test_gui_app", "test_gui_fix_gcpolicy", "test_gui_e2e"}


@pytest.fixture(autouse=True)
def _gui_thread_gc(request):
    """GUI tests run cyclic garbage collection only on the main (GUI) thread, like the
    app does (``jsonreshape.gui.app.install_gc_policy``): a collection triggered in a worker
    thread can destroy Qt objects off their thread and crash the process (review T1).
    Automatic GC is off during the test and a full collection runs before and after it."""
    module = getattr(request.module, "__name__", "").rpartition(".")[2]
    if "gui" not in request.keywords or module in _GC_SELF_MANAGED:
        yield
        return
    import gc

    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.collect()
        if was_enabled:
            gc.enable()


def _available_backends() -> list[str]:
    from jsonreshape import core

    return core.available_backends()


def pytest_generate_tests(metafunc):
    if "core_backend" in metafunc.fixturenames:
        names = _available_backends() or ["python"]
        metafunc.parametrize("core_backend", names, indirect=True)


@pytest.fixture
def core_backend(request):
    """The core backend module for the parametrized name ("rust" / "python")."""
    from jsonreshape import core

    return core.get_backend(request.param)


@pytest.fixture
def both_backends():
    """(python_backend, rust_backend) — skips when the Rust extension is unavailable."""
    from jsonreshape import core

    names = core.available_backends()
    if "rust" not in names or "python" not in names:
        pytest.skip("both core backends are required for equivalence tests")
    return core.get_backend("python"), core.get_backend("rust")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: slow tests (seconds)")
    config.addinivalue_line("markers", "bench: big-file benchmark (excluded by default)")
    config.addinivalue_line("markers", "gui: pytest-qt GUI tests")


@pytest.fixture
def lang_uz():
    from jsonreshape import i18n

    old = i18n.get_language()
    i18n.set_language("uz")
    yield
    i18n.set_language(old)


@pytest.fixture
def lang_en():
    from jsonreshape import i18n

    old = i18n.get_language()
    i18n.set_language("en")
    yield
    i18n.set_language(old)


def pytest_collection_modifyitems(config, items):
    # 'bench' tests only run when explicitly selected with -m bench
    markexpr = config.getoption("-m") or ""
    if "bench" in markexpr:
        return
    skip = pytest.mark.skip(reason="benchmark: run with -m bench")
    for item in items:
        if "bench" in item.keywords:
            item.add_marker(skip)


os.environ.setdefault("JSONRESHAPE_LANG", "uz")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # GUI tests never need a display
os.environ.setdefault("QT_FORCE_STDERR_LOGGING", "1")  # fatal Qt messages reach stderr (not the debugger channel)
