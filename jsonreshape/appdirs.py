"""Application data directories.

Windows: ``%LOCALAPPDATA%\\jsonreshape``; elsewhere ``~/.local/share/jsonreshape``.
``JSONRESHAPE_HOME`` overrides the base directory (tests use it to stay isolated).

Every folder is created on demand with long-path support (``fileio.long_path``: paths
beyond ``MAX_PATH`` work without the Windows ``LongPathsEnabled`` policy) and checked
once per process for write access (a probe file is created and removed). A folder that
cannot be created or written -- a read-only profile, an ACL that denies writing, a file
in the way, a cache folder on an unplugged drive -- does not stop the program: a
fallback is used instead and one notice is recorded per fallback (logged, and taken by
:func:`take_notices`; the reader shows them as warnings of the next opened document):

* the app home -> ``<system temp>/jsonreshape`` (``fileio.warning.app_dir_fallback``);
* a sub-folder of the app home (``index``, ``logs``, ``tmp``, ...) -> the same sub-folder
  of the temporary home;
* ``Settings.cache_dir`` -> the default index folder (:func:`index_dir`) or work folder
  (:func:`work_dir`) (``fileio.warning.cache_dir_fallback``).

Only when no candidate works a localized
:class:`~jsonreshape.errors.JsonReshapeError` (``fileio.error.no_work_dir``) is raised --
never a raw ``OSError``.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

APP_NAME = "jsonreshape"

log = logging.getLogger(__name__)

_lock = threading.RLock()
_resolved: dict[tuple[str, str], Path] = {}     # (role, requested path) -> folder in use
_notices: list[tuple[str, dict[str, Any]]] = []  # recorded fallbacks not taken yet
_noticed: set[tuple[str, str]] = set()           # fallbacks already recorded (once per process)


def _long(p: Path) -> str:
    from .fileio import long_path  # fileio imports this module: import at call time

    return long_path(p)


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(p)))


def _probe(path: Path) -> None:
    """Create ``path`` (parents too) and check that files can be created in it;
    ``OSError`` otherwise."""
    target = _long(path)
    os.makedirs(target, exist_ok=True)
    if not os.path.isdir(target):  # pragma: no cover - makedirs raised for a file in the way
        raise NotADirectoryError(20, "not a directory", os.fspath(path))
    probe = os.path.join(target, f".{APP_NAME}-probe-{os.getpid()}-{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    fd = os.open(probe, flags, 0o666)
    os.close(fd)
    try:
        os.remove(probe)
    except OSError:  # pragma: no cover - e.g. a scanner holds the new file
        pass


def _detail(exc: OSError) -> str:
    """The cause as an i18n reason code (``@permission``, ...) when it is a common one --
    localized when the notice is taken, after the UI language is known -- else the OS text."""
    winerror = getattr(exc, "winerror", None)
    if isinstance(exc, PermissionError) or winerror in (5, 19):
        return "@permission"
    if isinstance(exc, (FileExistsError, NotADirectoryError)) or winerror in (183, 267):
        return "@not_a_folder"
    if isinstance(exc, FileNotFoundError) or winerror in (2, 3, 21, 53, 67, 161):
        return "@not_found"
    if exc.errno == 28 or winerror in (39, 112):
        return "@disk_full"
    return exc.strerror or str(exc)


def _localized(params: dict[str, Any]) -> dict[str, Any]:
    detail = params.get("detail", "")
    if isinstance(detail, str) and detail.startswith("@"):
        from .i18n import t

        params = dict(params, detail=t(f"fileio.reason.{detail[1:]}"))
    return params


def _usable(role: str, requested: Path, fallbacks: list[tuple[Callable[[], Path], str]]) -> Path:
    """The first usable folder of ``requested`` and ``fallbacks`` (``(folder factory,
    notice key)`` pairs, tried in order only when needed); the choice is remembered per
    process (the folder is re-created when it was removed meanwhile)."""
    key = (role, _norm(requested))
    with _lock:
        chosen = _resolved.get(key)
        if chosen is not None:
            try:
                os.makedirs(_long(chosen), exist_ok=True)
                return chosen
            except OSError:
                _resolved.pop(key, None)
        first_error: OSError | None = None
        candidates: list[tuple[Callable[[], Path], str]] = [(lambda: requested, ""), *fallbacks]
        for factory, notice in candidates:
            try:
                candidate = factory()
                if notice and _norm(candidate) == _norm(requested):
                    continue
                _probe(candidate)
            except OSError as exc:
                log.debug("folder for %s cannot be used", requested, exc_info=True)
                if first_error is None:
                    first_error = exc
                continue
            _resolved[key] = candidate
            if notice and key not in _noticed:
                _noticed.add(key)
                detail = _detail(first_error) if first_error is not None else ""
                params = {"path": os.fspath(requested), "fallback": os.fspath(candidate), "detail": detail}
                _notices.append((notice, params))
                log.warning("folder %s cannot be used (%s); using %s instead", requested, first_error, candidate)
            return candidate
    from .errors import JsonReshapeError

    params = _localized({"detail": _detail(first_error) if first_error is not None else ""})
    raise JsonReshapeError("fileio.error.no_work_dir", path=os.fspath(requested), detail=params["detail"])


def _configured_home() -> Path:
    override = os.environ.get("JSONRESHAPE_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / APP_NAME
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / APP_NAME


def _temp_home() -> Path:
    return Path(tempfile.gettempdir()) / APP_NAME


def app_home() -> Path:
    """The application data folder (created on demand; see the module docstring for the
    fallback when it cannot be used)."""
    return _usable("home", _configured_home(), [(_temp_home, "fileio.warning.app_dir_fallback")])


def _sub(name: str) -> Path:
    home = app_home()
    return _usable("sub", home / name, [(lambda: _temp_home() / name, "fileio.warning.app_dir_fallback")])


def index_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Index cache directory (``override`` comes from Settings.cache_dir; when it cannot
    be used the default index folder is used, with a notice)."""
    if override:
        return _usable("cache", Path(override), [(lambda: _sub("index"), "fileio.warning.cache_dir_fallback")])
    return _sub("index")


def logs_dir() -> Path:
    return _sub("logs")


def tmp_dir() -> Path:
    return _sub("tmp")


def work_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Folder for big temporary/work files (prepared gzip/UTF-16 copies, sort runs,
    XLSX staging): ``<cache_dir>/tmp`` when the user configured a cache folder
    (``override`` = Settings.cache_dir), else :func:`tmp_dir`. Created on demand; an
    unusable cache folder falls back to :func:`tmp_dir` (with a notice)."""
    if override:
        return _usable("work", Path(override) / "tmp", [(tmp_dir, "fileio.warning.cache_dir_fallback")])
    return tmp_dir()


def recordsets_dir() -> Path:
    return _sub("recordsets")


def settings_file() -> Path:
    return app_home() / "settings.json"


def take_notices() -> list[tuple[str, dict[str, Any]]]:
    """Fallbacks recorded since the last call, as ``(i18n key, parameters)`` pairs (each
    fallback is reported once per process; ``detail`` -- the cause -- is localized in the
    current UI language)."""
    with _lock:
        out = list(_notices)
        _notices.clear()
    return [(key, _localized(params)) for key, params in out]


def notice_texts() -> list[str]:
    """:func:`take_notices` as localized texts."""
    from .i18n import t

    return [t(key, **params) for key, params in take_notices()]


def _reset_for_tests() -> None:
    """Forget the folders chosen so far and the notices (tests)."""
    with _lock:
        _resolved.clear()
        _notices.clear()
        _noticed.clear()
