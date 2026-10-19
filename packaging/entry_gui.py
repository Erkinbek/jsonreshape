"""Entry script of ``jsonreshape-gui.exe`` -- the windowed viewer -- in the PyInstaller build
(``packaging/jsonreshape.spec``).

``multiprocessing.freeze_support()`` runs first (a worker process of the frozen program is this exe started again;
the call runs the worker and exits), then :func:`jsonreshape.gui.app.main` with the command line.

Smoke-test hook (``tests/test_packaging.py``, ``scripts/build_exe.ps1``; nothing a user sets): with
``JSONRESHAPE_SMOKE_RESULT=<file>`` the program starts as usual with the files of its command line, waits until
every tab finished opening (at most ``JSONRESHAPE_SMOKE_TIMEOUT_MS``, default 60000), writes a JSON report to that
file -- the start-up times counted from the creation of the process, the Qt platform, version, image formats and
styles, the core and ijson backends, the tabs and their record counts, the network modules loaded (none) -- closes
the window like a user would and exits with 0 (3 when it timed out or a file did not open, 4 when the window did
not start).
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import time

SMOKE_ENV = "JSONRESHAPE_SMOKE_RESULT"
SMOKE_TIMEOUT_ENV = "JSONRESHAPE_SMOKE_TIMEOUT_MS"
#: Modules that would mean network access (the program is offline, AGENT_PROMPT §1.6). ``socket`` itself is not
#: listed: the standard library's ``logging.handlers`` imports it (no connection is made).
NETWORK_MODULES = ("ssl", "_ssl", "urllib.request", "http.client", "PySide6.QtNetwork", "PySide6.QtWebEngineCore",
                   "PySide6.QtWebSockets", "PySide6.QtHttpServer")


def _process_started() -> float:
    """When this process was created (epoch seconds; ``psutil``), else now."""
    try:
        import psutil

        return float(psutil.Process().create_time())
    except Exception:  # noqa: BLE001 - a measurement only
        return time.time()


def _write(path: str, report: dict) -> None:
    import json

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def smoke(result_path: str, argv: list[str]) -> int:
    """Start the viewer on ``argv``, report and close it (see the module docstring)."""
    started = _process_started()
    t_entry = time.time()
    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import QTimer, qVersion
    from PySide6.QtGui import QGuiApplication, QImageReader
    from PySide6.QtWidgets import QApplication, QStyleFactory

    import jsonreshape
    from jsonreshape.gui.app import main as gui_main

    t_imported = time.time()

    def ms(t: float) -> float:
        return round((t - started) * 1000.0, 1)

    report: dict = {"ok": False, "version": jsonreshape.__version__, "frozen": bool(getattr(sys, "frozen", False)),
                    "executable": sys.executable, "python": sys.version.split()[0], "pyside": pyside_version,
                    "files": list(argv)}
    window = gui_main(list(argv), run=False)
    if not hasattr(window, "tab_widgets"):
        report["error"] = f"the window did not start: {window!r}"
        _write(result_path, report)
        return 4
    t_window = time.time()
    app = QApplication.instance()
    timeout = max(1.0, float(os.environ.get(SMOKE_TIMEOUT_ENV) or 60000) / 1000.0)
    deadline = time.monotonic() + timeout
    outcome = {"code": 3}

    def finish(complete: bool) -> None:
        from jsonreshape import bulk, core
        from jsonreshape.gui import osintegration, theme

        t_ready = time.time()
        tabs = window.tab_widgets()
        try:
            import ijson

            ijson_backend = str(getattr(ijson, "backend", ""))
        except Exception as exc:  # noqa: BLE001
            ijson_backend = f"error: {exc}"
        report.update({
            "ok": complete and all(tab.session.state == "ready" for tab in tabs),
            "timed_out": not complete,
            "times_ms": {"entry": ms(t_entry), "imported": ms(t_imported), "window": ms(t_window),
                         "ready": ms(t_ready)},
            "tabs": [{"name": tab.session.name, "state": tab.session.state,
                      "records": getattr(tab.session.doc, "record_count", None)} for tab in tabs],
            "platform": QGuiApplication.platformName(),
            "qt": qVersion(),
            "styles": list(QStyleFactory.keys()),
            "image_formats": sorted(bytes(f).decode("ascii", "replace") for f in QImageReader.supportedImageFormats()),
            "core_backend": str(getattr(core, "BACKEND", "none")),
            "ijson_backend": ijson_backend,
            "theme": theme.current_scheme(),
            # (look the names up: iterating sys.modules races with the background threads' imports)
            "network_modules": sorted(m for m in NETWORK_MODULES if m in sys.modules),
            # what Settings ▸ General would register for "Open with" (nothing is written here)
            "open_with_command": osintegration.launch_command(),
            # automatic worker processes (bulk previews, patch export, mapping export)
            "auto_workers": bool(bulk._spawn_safe()),
        })
        outcome["code"] = 0 if report["ok"] else 3
        _write(result_path, report)
        window.close()
        app.quit()

    def check() -> None:
        tabs = window.tab_widgets()
        complete = bool(tabs or not argv) and all(tab.session.state in ("ready", "failed") for tab in tabs)
        if complete or time.monotonic() > deadline:
            timer.stop()
            finish(complete)

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(check)
    timer.start()
    app.exec()
    return int(outcome["code"])


def main() -> int:
    result = os.environ.get(SMOKE_ENV)
    if result:
        return smoke(result, sys.argv[1:])
    from jsonreshape.gui.app import main as gui_main

    return int(gui_main())


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
