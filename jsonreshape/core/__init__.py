"""Byte-level core: Rust extension (``_rscore``) with a pure-Python fallback (``_pycore``).

Both backends expose the same API (see docs/ARCHITECTURE.md §1) and must return identical
results. The active backend is chosen once at import time:

* ``JSONRESHAPE_BACKEND=python`` forces the pure-Python reference implementation;
* ``JSONRESHAPE_BACKEND=rust`` requires the Rust extension (ImportError otherwise);
* default (``auto``): Rust when importable, else Python.
"""

from __future__ import annotations

import importlib
import logging
import os
from types import ModuleType

from .constants import *  # noqa: F401,F403  (re-exported constants)
from .constants import (
    DEFAULT_MAX_DEPTH,
    MODE_BRACE_LIST,
    MODE_CONTAINER,
    MODE_LINES,
    MODE_SINGLE,
    MODE_STREAM,
)

log = logging.getLogger(__name__)

_API = ("Scanner", "Reformatter", "MultiFinder", "PathExtractor", "numbers_plain")

_backends: dict[str, ModuleType] = {}
_import_errors: dict[str, str] = {}


def _rust_disabled() -> bool:
    # JSONRESHAPE_BACKEND=python means "never load the Rust extension" (also keeps the
    # .pyd unlocked on Windows while it is being rebuilt).
    return os.environ.get("JSONRESHAPE_BACKEND", "").strip().lower() == "python"


def _load(name: str) -> ModuleType | None:
    if name in _backends:
        return _backends[name]
    if name == "rust" and _rust_disabled():
        _import_errors[name] = "disabled by JSONRESHAPE_BACKEND=python"
        return None
    modname = {"python": "._pycore", "rust": "._rscore"}[name]
    try:
        mod = importlib.import_module(modname, __name__)
    except ImportError as exc:  # pragma: no cover - depends on build
        _import_errors[name] = str(exc)
        return None
    missing = [a for a in _API if not hasattr(mod, a)]
    if missing:
        _import_errors[name] = f"missing API: {', '.join(missing)}"
        return None
    _backends[name] = mod
    return mod


def available_backends() -> list[str]:
    """Names of backends that import successfully and expose the full API."""
    return [n for n in ("rust", "python") if _load(n) is not None]


def get_backend(name: str) -> ModuleType:
    """Return a specific backend module (``"rust"`` or ``"python"``)."""
    mod = _load(name)
    if mod is None:
        raise ImportError(f"jsonreshape core backend {name!r} unavailable: {_import_errors.get(name)}")
    return mod


def backend_import_error(name: str) -> str | None:
    _load(name)
    return _import_errors.get(name)


def _select() -> tuple[str, ModuleType | None]:
    choice = os.environ.get("JSONRESHAPE_BACKEND", "auto").strip().lower() or "auto"
    if choice == "python":
        return "python", get_backend("python")
    if choice == "rust":
        return "rust", get_backend("rust")
    mod = _load("rust")
    if mod is not None:
        return "rust", mod
    log.warning("Rust core unavailable (%s); using pure-Python fallback", _import_errors.get("rust"))
    mod = _load("python")
    if mod is not None:
        return "python", mod
    log.error("No core backend available: %s", _import_errors)
    return "none", None


def _unavailable(name: str):
    def _raise(*_args, **_kwargs):
        raise ImportError(f"jsonreshape core backend unavailable ({name}): {_import_errors}")

    _raise.__name__ = name
    return _raise


BACKEND, _active = _select()

Scanner = getattr(_active, "Scanner", None) or _unavailable("Scanner")
Reformatter = getattr(_active, "Reformatter", None) or _unavailable("Reformatter")
MultiFinder = getattr(_active, "MultiFinder", None) or _unavailable("MultiFinder")
PathExtractor = getattr(_active, "PathExtractor", None) or _unavailable("PathExtractor")
numbers_plain = getattr(_active, "numbers_plain", None) or _unavailable("numbers_plain")

__all__ = [
    "BACKEND",
    "DEFAULT_MAX_DEPTH",
    "MODE_BRACE_LIST",
    "MODE_CONTAINER",
    "MODE_LINES",
    "MODE_SINGLE",
    "MODE_STREAM",
    "MultiFinder",
    "PathExtractor",
    "Reformatter",
    "Scanner",
    "available_backends",
    "backend_import_error",
    "get_backend",
    "numbers_plain",
]
