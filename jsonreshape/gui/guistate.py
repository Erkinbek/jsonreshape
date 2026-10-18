"""Persistent GUI state: ``app_home()/gui_state.json`` (docs/ARCHITECTURE.md §11.6).

Qt-free. Holds the interface settings (theme ``system|light|dark``, monospace font family
and size, UI font size, main window geometry/state, last directories, ...), the recent
files (most recent first, at most :data:`RECENT_MAX`, deduplicated by normalized path --
case-insensitively on Windows), bookmarks per normalized source path and table column
layouts per file (at most :data:`LAYOUTS_MAX` files, least recently used dropped).

Several windows/processes may use the file at the same time, so :meth:`GuiState.save`
reads, merges and writes it under an inter-process lock (:func:`jsonreshape.query.file_lock`):
only the entries this instance changed are written over the file's current content (the
last writer wins per key: per setting key, per file's bookmarks, per file's layout); the
recent-files changes (add/remove/clear) are replayed on the file's list, so two windows
adding files do not lose each other's entries. Writes are atomic
(:func:`jsonreshape.fileio.atomic_write`). A corrupt file is logged, kept as
``gui_state.json.bad`` and replaced by defaults. Nothing is saved automatically: callers
call :meth:`GuiState.save` (e.g. after a bookmark change, when the main window closes).

:meth:`GuiState.save` may run in a worker thread (the GUI saves through
``AppContext.save_state()``, a silent background task: the inter-process lock may wait up
to :data:`LOCK_TIMEOUT` seconds). The file I/O does not hold the instance lock: the
changes are taken out first (``get``/``set`` in the GUI thread never wait for the disk),
written, and changes made meanwhile stay pending for the next save. Saves are serialized.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from jsonreshape.errors import JsonReshapeError
from jsonreshape.fileio import atomic_write, long_path, open_shared

log = logging.getLogger(__name__)

__all__ = [
    "Bookmark",
    "DEFAULTS",
    "GuiState",
    "LAYOUTS_MAX",
    "RECENT_MAX",
    "STATE_FILE",
    "THEMES",
    "norm_path",
]

STATE_FILE = "gui_state.json"
STATE_VERSION = 1
RECENT_MAX = 20
LAYOUTS_MAX = 200
BOOKMARK_FILES_MAX = 1000          # files with bookmarks kept (least recently changed dropped beyond)
MAX_FILE_BYTES = 16 << 20          # a larger state file is treated as corrupt
LOCK_TIMEOUT = 2.0                 # seconds to wait for another window saving the file
THEMES = ("system", "light", "dark")

#: Defaults returned by :meth:`GuiState.get` when a key is not set and no default is given.
DEFAULTS: dict[str, Any] = {
    "theme": "system",
    "mono_font": "",        # "" = the platform's fixed-pitch font
    "mono_size": 0,         # 0 = default size
    "ui_size": 0,           # 0 = the system UI font size
    "geometry": None,       # base64 text of QWidget.saveGeometry()
    "window_state": None,   # base64 text of QMainWindow.saveState()
    "last_dir": "",
}


def norm_path(path: str | os.PathLike[str]) -> str:
    """Absolute, normalized form of ``path`` used to compare files (case-insensitive on
    Windows: ``os.path.normcase``)."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _json_ok(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _clean_path_list(path: Iterable[Any]) -> list[str | int]:
    out: list[str | int] = []
    for seg in path:
        if isinstance(seg, bool) or not isinstance(seg, (str, int)):
            raise ValueError(f"path segments must be str or int, not {type(seg).__name__}")
        out.append(seg)
    return out


@dataclass
class Bookmark:
    """A named node: ``record`` (record number, ``None`` = outside the records) and
    ``path`` (relative to the record value, absolute when ``record`` is ``None``) as a
    list of keys (str) and array indices (int); ``created``: ``time.time()``."""

    name: str
    record: int | None
    path: list = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.name = str(self.name)
        if self.record is not None and (isinstance(self.record, bool) or not isinstance(self.record, int)
                                        or self.record < 0):
            raise ValueError(f"bad bookmark record: {self.record!r}")
        self.path = _clean_path_list(self.path)
        self.created = float(self.created)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "record": self.record, "path": list(self.path), "created": self.created}

    @classmethod
    def from_dict(cls, data: Any) -> Bookmark:
        """Raises ``ValueError`` / ``TypeError`` for malformed data."""
        if not isinstance(data, dict):
            raise TypeError("bookmark must be an object")
        name = data.get("name")
        if not isinstance(name, str):
            raise TypeError("bookmark name must be a string")
        path = data.get("path", [])
        if not isinstance(path, list):
            raise TypeError("bookmark path must be a list")
        created = data.get("created", 0.0)
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            created = 0.0
        return cls(name=name, record=data.get("record"), path=path, created=float(created))


def _empty() -> dict[str, Any]:
    return {"settings": {}, "recent": [], "bookmarks": {}, "layouts": {}}


class GuiState:
    """The GUI state file (see the module docstring). Thread-safe; Qt-free."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            from jsonreshape.appdirs import app_home

            path = app_home() / STATE_FILE
        self.path = Path(path)
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()   # one save at a time (the I/O runs without _lock)
        self._data = _empty()
        # changes not saved yet (merged into the file's content by save())
        self._dirty_settings: set[str] = set()
        self._dirty_bookmarks: set[str] = set()
        self._dirty_layouts: set[str] = set()
        self._recent_ops: list[tuple[str, str]] = []

    # -- loading ---------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> GuiState:
        """Read the state file (missing → defaults; corrupt → defaults, file kept as ``.bad``)."""
        state = cls(path)
        with state._lock:
            state._data = state._read_file()
        return state

    def reload(self) -> None:
        """Re-read the file; unsaved changes of this instance are kept on top."""
        with self._lock:
            self._data = self._merged(self._read_file())

    def _read_file(self) -> dict[str, Any]:
        try:
            with open_shared(self.path, "rb") as f:
                raw = f.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return _empty()
        except Exception as exc:
            if os.path.exists(long_path(self.path)):
                log.warning("cannot read the GUI state %s: %s", self.path, exc)
            return _empty()
        if not raw.strip():
            return _empty()
        try:
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("file too large")
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            self._keep_bad(exc)
            return _empty()
        return self._sanitize(data)

    def _keep_bad(self, exc: BaseException) -> None:
        bad = self.path.with_name(self.path.name + ".bad")
        log.warning("GUI state file %s is corrupt (%s); using defaults, the file is kept as %s", self.path, exc,
                    bad.name)
        try:
            os.replace(long_path(self.path), long_path(bad))
        except OSError as err:
            log.warning("could not rename the corrupt GUI state file: %s", err)

    @staticmethod
    def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
        out = _empty()
        settings = data.get("settings")
        if isinstance(settings, dict):
            out["settings"] = {k: v for k, v in settings.items() if isinstance(k, str)}
        recent = data.get("recent")
        if isinstance(recent, list):
            seen: set[str] = set()
            for p in recent:
                if isinstance(p, str) and p:
                    key = norm_path(p)
                    if key not in seen:
                        seen.add(key)
                        out["recent"].append(p)
            del out["recent"][RECENT_MAX:]
        marks = data.get("bookmarks")
        if isinstance(marks, dict):
            for key, entry in marks.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                items = entry.get("items")
                if not isinstance(items, list):
                    continue
                clean = []
                for item in items:
                    try:
                        clean.append(Bookmark.from_dict(item).to_dict())
                    except (TypeError, ValueError):
                        continue
                changed = entry.get("changed", 0.0)
                changed = float(changed) if isinstance(changed, (int, float)) and not isinstance(changed, bool) else 0.0
                out["bookmarks"][key] = {"items": clean, "changed": changed}
        layouts = data.get("layouts")
        if isinstance(layouts, dict):
            for key, entry in layouts.items():
                if not isinstance(key, str) or not isinstance(entry, dict) or not isinstance(entry.get("layout"), dict):
                    continue
                used = entry.get("used", 0.0)
                used = float(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else 0.0
                out["layouts"][key] = {"layout": entry["layout"], "used": used}
        return out

    # -- saving ----------------------------------------------------------------------------
    def save(self, *, raise_errors: bool = False) -> bool:
        """Merge this instance's changes into the file and write it (atomic, under the
        inter-process lock). Returns True on success; failures are logged (and raised,
        localized, with ``raise_errors``) and the changes stay pending. Afterwards the
        instance reflects the file (changes of other windows included) plus the changes
        made while it was written. Any thread; the instance lock is not held during I/O."""
        from jsonreshape.fileio import make_dirs
        from jsonreshape.query import file_lock

        with self._save_lock:
            with self._lock:
                changes = self._take_changes()
            try:
                make_dirs(self.path.parent)
                with file_lock(self.path, "gui.state.busy", timeout=LOCK_TIMEOUT):
                    written = _apply_changes(self._read_file(), *changes)
                    payload = {"version": STATE_VERSION, **written}
                    data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8", "backslashreplace")
                    with atomic_write(self.path, "wb", fsync=False) as f:
                        f.write(data + b"\n")
            except (JsonReshapeError, OSError, ValueError, TypeError) as exc:
                with self._lock:
                    self._restore_changes(changes)
                log.warning("could not save the GUI state %s: %s", self.path, exc)
                if raise_errors:
                    raise
                return False
            with self._lock:
                self._data = self._merged(written)   # + what changed while writing
            return True

    def _take_changes(self) -> tuple[dict, dict, dict, list]:
        """Copies of the unsaved changes (cleared: changes made from now on are new)."""
        mine = self._data
        settings = {k: (k in mine["settings"], _copy(mine["settings"].get(k))) for k in self._dirty_settings}
        marks = {k: _copy(mine["bookmarks"].get(k)) for k in self._dirty_bookmarks}
        layouts = {k: _copy(mine["layouts"].get(k)) for k in self._dirty_layouts}
        ops = list(self._recent_ops)
        self._dirty_settings.clear()
        self._dirty_bookmarks.clear()
        self._dirty_layouts.clear()
        self._recent_ops.clear()
        return settings, marks, layouts, ops

    def _restore_changes(self, changes: tuple[dict, dict, dict, list]) -> None:
        """A failed save: its changes are pending again (the current values are kept)."""
        settings, marks, layouts, ops = changes
        self._dirty_settings.update(settings)
        self._dirty_bookmarks.update(marks)
        self._dirty_layouts.update(layouts)
        self._recent_ops[:0] = ops

    @property
    def dirty(self) -> bool:
        """Unsaved changes exist."""
        with self._lock:
            return bool(self._dirty_settings or self._dirty_bookmarks or self._dirty_layouts or self._recent_ops)

    def _merged(self, disk: dict[str, Any]) -> dict[str, Any]:
        """``disk`` with this instance's unsaved changes applied (last writer wins per key)."""
        mine = self._data
        settings = {k: (k in mine["settings"], mine["settings"].get(k)) for k in self._dirty_settings}
        marks = {k: mine["bookmarks"].get(k) for k in self._dirty_bookmarks}
        layouts = {k: mine["layouts"].get(k) for k in self._dirty_layouts}
        return _apply_changes(disk, settings, marks, layouts, self._recent_ops)

    # -- settings ----------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """The value of setting ``key``; when unset: ``default``, else :data:`DEFAULTS`."""
        with self._lock:
            settings = self._data["settings"]
            if key in settings:
                return settings[key]
        return default if default is not None else DEFAULTS.get(key)

    def set(self, key: str, value: Any) -> None:
        """Set a setting (a JSON value; ``None`` removes it). ``theme`` must be one of
        :data:`THEMES`. ``ValueError`` / ``TypeError`` for invalid values."""
        if not isinstance(key, str) or not key:
            raise TypeError("setting keys must be non-empty strings")
        if key == "theme" and value is not None and value not in THEMES:
            raise ValueError(f"theme must be one of {THEMES}, not {value!r}")
        if value is not None and not _json_ok(value):
            raise TypeError(f"setting {key!r} must be a JSON value")
        with self._lock:
            settings = self._data["settings"]
            if value is None:
                if key not in settings:
                    return
                settings.pop(key)
            else:
                if settings.get(key, _MISSING) == value:
                    return
                settings[key] = json.loads(json.dumps(value, ensure_ascii=False))   # a private copy
            self._dirty_settings.add(key)

    def keys(self) -> list[str]:
        """Setting keys that are set."""
        with self._lock:
            return list(self._data["settings"])

    # -- recent files --------------------------------------------------------------------------
    def recent_files(self) -> list[str]:
        """Recent files, most recent first."""
        with self._lock:
            return list(self._data["recent"])

    def add_recent(self, path: str | os.PathLike[str]) -> None:
        """Put ``path`` first in the recent files (an existing entry for the same file moves)."""
        p = os.path.abspath(os.fspath(path))
        self._recent_op("add", p)

    def remove_recent(self, path: str | os.PathLike[str]) -> None:
        self._recent_op("remove", os.path.abspath(os.fspath(path)))

    def clear_recent(self) -> None:
        self._recent_op("clear", "")

    def _recent_op(self, op: str, p: str) -> None:
        with self._lock:
            self._data["recent"] = _recent_apply(self._data["recent"], op, p)
            self._recent_ops.append((op, p))

    # -- bookmarks -------------------------------------------------------------------------------
    def bookmarks(self, path: str | os.PathLike[str]) -> list[Bookmark]:
        """The bookmarks of the file ``path`` (copies)."""
        with self._lock:
            entry = self._data["bookmarks"].get(norm_path(path))
            items = list(entry["items"]) if entry else []
        out = []
        for item in items:
            try:
                out.append(Bookmark.from_dict(item))
            except (TypeError, ValueError):  # pragma: no cover - sanitized on load
                continue
        return out

    def set_bookmarks(self, path: str | os.PathLike[str], bookmarks: Iterable[Bookmark]) -> None:
        """Replace the bookmarks of the file ``path`` (an empty list removes them)."""
        items = []
        for bm in bookmarks:
            if not isinstance(bm, Bookmark):
                raise TypeError(f"expected Bookmark, not {type(bm).__name__}")
            items.append(bm.to_dict())
        key = norm_path(path)
        with self._lock:
            if items:
                self._data["bookmarks"][key] = {"items": items, "changed": time.time()}
            elif key in self._data["bookmarks"]:
                del self._data["bookmarks"][key]
            else:
                return
            self._dirty_bookmarks.add(key)

    # -- table layouts ----------------------------------------------------------------------------
    def table_layout(self, path: str | os.PathLike[str]) -> dict | None:
        """The saved table layout of the file ``path`` (a copy), ``None`` when there is none."""
        key = norm_path(path)
        with self._lock:
            entry = self._data["layouts"].get(key)
            if entry is None:
                return None
            entry["used"] = time.time()   # LRU: saved with the next save()
            self._dirty_layouts.add(key)
            return json.loads(json.dumps(entry["layout"], ensure_ascii=False))

    def set_table_layout(self, path: str | os.PathLike[str], layout: dict | None) -> None:
        """Remember (``None``: forget) the table layout of the file ``path`` (a JSON object)."""
        key = norm_path(path)
        if layout is not None:
            if not isinstance(layout, dict) or not _json_ok(layout):
                raise TypeError("a table layout must be a JSON object")
        with self._lock:
            layouts = self._data["layouts"]
            if layout is None:
                if key not in layouts:
                    return
                del layouts[key]
            else:
                layouts[key] = {"layout": json.loads(json.dumps(layout, ensure_ascii=False)), "used": time.time()}
                trimmed = _trim_layouts(layouts)
                for dropped in set(layouts) - set(trimmed):
                    self._dirty_layouts.add(dropped)
                self._data["layouts"] = trimmed
            self._dirty_layouts.add(key)


_MISSING = object()


def _copy(value: Any) -> Any:
    return None if value is None else json.loads(json.dumps(value, ensure_ascii=False))


def _apply_changes(disk: dict[str, Any], settings: dict[str, tuple[bool, Any]], marks: dict[str, Any],
                   layouts: dict[str, Any], recent_ops: list[tuple[str, str]]) -> dict[str, Any]:
    """``disk`` with the given changes applied: ``settings`` key -> (present, value),
    ``marks`` / ``layouts`` key -> entry (``None``: removed), the recent-files operations
    replayed in order."""
    out = {
        "settings": dict(disk["settings"]),
        "recent": list(disk["recent"]),
        "bookmarks": dict(disk["bookmarks"]),
        "layouts": dict(disk["layouts"]),
    }
    for key, (present, value) in settings.items():
        if present:
            out["settings"][key] = value
        else:
            out["settings"].pop(key, None)
    for op, p in recent_ops:
        out["recent"] = _recent_apply(out["recent"], op, p)
    for key, entry in marks.items():
        if entry is not None:
            out["bookmarks"][key] = entry
        else:
            out["bookmarks"].pop(key, None)
    if len(out["bookmarks"]) > BOOKMARK_FILES_MAX:
        keep = sorted(out["bookmarks"].items(), key=lambda kv: kv[1].get("changed", 0.0), reverse=True)
        out["bookmarks"] = dict(keep[:BOOKMARK_FILES_MAX])
    for key, entry in layouts.items():
        if entry is not None:
            out["layouts"][key] = entry
        else:
            out["layouts"].pop(key, None)
    out["layouts"] = _trim_layouts(out["layouts"])
    return out


def _recent_apply(recent: list[str], op: str, p: str) -> list[str]:
    if op == "clear":
        return []
    key = norm_path(p)
    out = [x for x in recent if norm_path(x) != key]
    if op == "add":
        out.insert(0, p)
    return out[:RECENT_MAX]


def _trim_layouts(layouts: dict[str, Any]) -> dict[str, Any]:
    if len(layouts) <= LAYOUTS_MAX:
        return layouts
    keep = sorted(layouts.items(), key=lambda kv: kv[1].get("used", 0.0), reverse=True)[:LAYOUTS_MAX]
    return dict(keep)

