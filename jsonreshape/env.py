"""Environment check (docs/ARCHITECTURE.md §4, AGENT_PROMPT §2).

:func:`check_environment` reports the Python version, the platform, the active core
backend (and why the Rust extension is not used, if it is not), the ijson backend --
which must be ``yajl2_c`` ("ishga tushganda tekshirilsin": checked at start-up, a
warning is logged otherwise) -- and the versions of the libraries the program uses.
:func:`format_environment` renders the result as localized text (CLI, About dialog).

Nothing here touches the network: package versions are read from installed metadata
(falling back to the module's ``__version__``), never from an online index.
"""

from __future__ import annotations

import logging
import os
import platform
import struct
import sys
from typing import Any

from .i18n import t

log = logging.getLogger(__name__)

__all__ = ["PACKAGES", "REQUIRED_PACKAGES", "check_environment", "format_environment"]

# display name -> (distribution name, import name)
PACKAGES: dict[str, tuple[str, str]] = {
    "orjson": ("orjson", "orjson"),
    "ijson": ("ijson", "ijson"),
    "xlsxwriter": ("XlsxWriter", "xlsxwriter"),
    "jmespath": ("jmespath", "jmespath"),
    "psutil": ("psutil", "psutil"),
    "PySide6": ("PySide6", "PySide6"),
}
REQUIRED_PACKAGES = ("orjson", "ijson", "xlsxwriter", "jmespath", "psutil")  # PySide6: GUI only
EXPECTED_IJSON_BACKEND = "yajl2_c"
MIN_PYTHON = (3, 11)


def _package_version(dist: str, module: str) -> str | None:
    """Installed version of a package, or ``None`` when it is missing."""
    try:
        from importlib import metadata

        return metadata.version(dist)
    except Exception:  # PackageNotFoundError, or no metadata in a frozen build
        pass
    try:
        import importlib

        mod = importlib.import_module(module)
    except Exception:
        return None
    version = getattr(mod, "__version__", None)
    return str(version) if version is not None else None


def _platform_info() -> tuple[str, str]:
    """(platform text, machine) without ``platform.uname()``: that one resolves the host
    name and so imports ``socket``, which this module promises not to do."""
    if sys.platform == "win32":
        release, version, csd, _ptype = platform.win32_ver()
        text = "-".join(part for part in ("Windows", release, version, csd) if part)
        machine = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", "")
        return text, machine
    if hasattr(os, "uname"):
        u = os.uname()
        return f"{u.sysname}-{u.release}-{u.machine}", u.machine
    return sys.platform, ""  # pragma: no cover


def _core_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "core_backend": "none",
        "core_backends": [],
        "core_error": None,
        "rust_error": None,
        "backend_env": os.environ.get("JSONRESHAPE_BACKEND", "").strip().lower() or "auto",
    }
    try:
        from . import core
    except Exception as exc:  # no backend importable (or JSONRESHAPE_BACKEND=rust without it)
        info["core_error"] = f"{type(exc).__name__}: {exc}"
        return info
    info["core_backend"] = str(getattr(core, "BACKEND", "none"))
    try:
        info["core_backends"] = list(core.available_backends())
    except Exception as exc:  # pragma: no cover - defensive
        info["core_error"] = f"{type(exc).__name__}: {exc}"
    if info["core_backend"] != "rust":
        try:
            info["rust_error"] = core.backend_import_error("rust")
        except Exception as exc:  # pragma: no cover - defensive
            info["rust_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _ijson_info() -> tuple[str | None, str | None]:
    """(backend name, error) of ijson's default backend."""
    try:
        import ijson
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    backend = getattr(ijson, "backend", None)
    if backend is not None and not isinstance(backend, str):
        backend = getattr(backend, "backend", None) or getattr(backend, "__name__", str(backend))
    error = None
    if backend != EXPECTED_IJSON_BACKEND:
        try:
            ijson.get_backend(EXPECTED_IJSON_BACKEND)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    return backend, error


def check_environment(*, log_result: bool = True) -> dict[str, Any]:
    """Inspect the runtime environment and return a JSON-compatible dict.

    Keys: ``app_version``, ``python``, ``python_ok``, ``implementation``, ``bits``,
    ``byteorder``, ``executable``, ``frozen``, ``platform``, ``machine``,
    ``core_backend`` (``rust``/``python``/``none``), ``core_backends``, ``core_error``,
    ``rust_error`` (why Rust is not active, ``None`` when it is), ``backend_env``,
    ``ijson_backend``, ``ijson_ok``, ``ijson_error``, ``versions`` (package -> version or
    ``None``), ``warnings`` (i18n keys) and ``warning_params`` (their parameters, same
    order). With ``log_result`` every warning is logged at WARNING level and the summary
    at INFO level.
    """
    from . import __version__

    platform_text, machine = _platform_info()
    info: dict[str, Any] = {
        "app_version": __version__,
        "python": platform.python_version(),
        "python_ok": sys.version_info[:2] >= MIN_PYTHON,
        "implementation": platform.python_implementation(),
        "bits": struct.calcsize("P") * 8,
        "byteorder": sys.byteorder,
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": platform_text,
        "machine": machine,
    }
    info.update(_core_info())
    backend, ijson_error = _ijson_info()
    info["ijson_backend"] = backend
    info["ijson_ok"] = backend == EXPECTED_IJSON_BACKEND
    info["ijson_error"] = ijson_error
    info["versions"] = {name: _package_version(dist, mod) for name, (dist, mod) in PACKAGES.items()}

    warnings: list[tuple[str, dict[str, Any]]] = []
    if not info["python_ok"]:
        warnings.append(("env.warn.python_version",
                         {"version": info["python"], "required": ".".join(map(str, MIN_PYTHON))}))
    if info["bits"] < 64:
        warnings.append(("env.warn.32bit", {}))
    if info["byteorder"] != "little":
        warnings.append(("env.warn.big_endian", {}))
    if info["core_backend"] == "none":
        warnings.append(("env.warn.core_unavailable", {"error": info["core_error"] or "?"}))
    elif info["core_backend"] != "rust" and info["backend_env"] != "python":
        warnings.append(("env.warn.rust_unavailable", {"error": info["rust_error"] or "?"}))
    if not info["ijson_ok"]:
        warnings.append(("env.warn.ijson_backend",
                         {"backend": backend or t("env.report.missing"), "expected": EXPECTED_IJSON_BACKEND}))
    for name in REQUIRED_PACKAGES:
        if info["versions"].get(name) is None:
            warnings.append(("env.warn.missing_package", {"package": name}))
    info["warnings"] = [key for key, _ in warnings]
    info["warning_params"] = [params for _, params in warnings]

    if log_result:
        log.info(
            "environment: jsonreshape %s, Python %s (%s, %d-bit), %s; core=%s; ijson=%s; versions=%s",
            info["app_version"], info["python"], info["implementation"], info["bits"], info["platform"],
            info["core_backend"], backend, info["versions"],
        )
        if info["rust_error"]:
            log.info("Rust core not active: %s", info["rust_error"])
        for key, params in warnings:
            if key == "env.warn.ijson_backend":
                log.warning("ijson backend is %r, expected %r (%s); streaming of large nodes will be slow",
                            backend, EXPECTED_IJSON_BACKEND, ijson_error)
            else:
                log.warning("environment check: %s %s", key, params)
    return info


def format_environment(info: dict[str, Any] | None = None) -> str:
    """Localized multi-line report of :func:`check_environment` (computed when ``None``)."""
    if info is None:
        info = check_environment(log_result=False)
    missing = t("env.report.missing")
    backend_key = {"rust": "env.backend.rust", "python": "env.backend.python"}.get(info.get("core_backend"),
                                                                                  "env.backend.none")
    lines = [
        t("env.report.title"),
        t("env.report.version", version=info.get("app_version")),
        t("env.report.python", version=info.get("python"), implementation=info.get("implementation"),
          bits=info.get("bits")),
        t("env.report.platform", platform=info.get("platform")),
        t("env.report.core", backend=t(backend_key)),
        t("env.report.ijson", backend=info.get("ijson_backend") or missing),
    ]
    for name, version in (info.get("versions") or {}).items():
        lines.append(t("env.report.package", name=name, version=version or missing))
    params_list = info.get("warning_params") or [{} for _ in info.get("warnings", ())]
    for key, params in zip(info.get("warnings", ()), params_list):
        lines.append(t("env.report.warning", text=t(key, **params)))
    return "\n".join(lines)
