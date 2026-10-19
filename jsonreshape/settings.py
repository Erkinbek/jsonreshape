"""User settings (docs/ARCHITECTURE.md §4, AGENT_PROMPT §11 "Sozlamalar").

:class:`Settings` is a dataclass persisted as JSON in ``appdirs.settings_file()``.
Values are validated when an instance is created (``UserInputError`` with the localized
key ``settings.invalid``). Loading never fails: a missing file gives the defaults, a
corrupt file or an invalid value gives the defaults for what could not be used, and a
warning is logged. :func:`get_settings` returns the process-wide instance.

Treat the shared instance as read-only; to change settings build a new one with
``dataclasses.replace(get_settings(), ...)`` (validated), then :func:`set_settings` and
:meth:`Settings.save`.

Editing (stage 3, docs/ARCHITECTURE.md §12.9) adds three fields at the end of the dataclass
(the field order is pinned by tests; a settings file written before them simply gives their
defaults):

* ``edits_location`` -- where the edit layer of a document is *created*: ``"appdata"``
  (``appdirs.edits_dir()/<key>``, the default) or ``"beside"`` (``<name>.jredit`` next to
  the file; an unusable folder falls back to the app folder, §12.2). Only the exact two
  words are accepted (stripped, case-insensitive): an unknown value is never guessed, so a
  typo in the file falls back to the safe default instead of a surprising location.
  Pending layers are searched in both places whatever this says (§12.2 ``find_pending``).
* ``backup_on_save`` -- keep ``<file>.bak`` when Save replaces a file (§12.7). A bool only
  (``"yes"``/``1`` are rejected like for ``csv_bom``): the backup of the user's data must
  never be switched by a value that merely looks true or false.
* ``undo_limit`` -- how many steps can be undone (AGENT_PROMPT §1.5: at least 200). An
  integer >= 200 without an upper bound in the core (the settings dialog offers
  200..100000 and keeps a larger value from the file as long as the field is not edited).

The tools (stage 5, docs/ARCHITECTURE.md §14.0) add one more field at the end:

* ``diff_align_max_bytes`` -- two files are compared in full without a key (records aligned
  by content, ``diff.py`` "align" mode) only while each holds at most this many bytes of
  records (default 200 MiB, AGENT_PROMPT §4.5 "200 MB gacha to'liq"); bigger files need a key
  or the position mode. A byte size >= 1 (``"500MB"`` accepted).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Union

from .appdirs import settings_file
from .errors import JsonReshapeError, UserInputError
from .i18n import SUPPORTED_LANGUAGES, t

log = logging.getLogger(__name__)

__all__ = ["EDITS_LOCATIONS", "SETTINGS_VERSION", "UNDO_LIMIT_MIN", "Settings", "get_settings", "set_settings"]

KiB = 1 << 10
MiB = 1 << 20
GiB = 1 << 30

SETTINGS_VERSION = 1
XLSX_MAX_ROWS = 1048576          # Excel's hard limit of rows per sheet
EDITS_LOCATIONS = ("appdata", "beside")   # Settings.edits_location: the app folder / next to the file
UNDO_LIMIT_MIN = 200             # AGENT_PROMPT §1.5: undo/redo of at least 200 steps
_MAX_SETTINGS_FILE = 1 << 20      # a settings file larger than this is treated as corrupt

StrPath = Union[str, "os.PathLike[str]"]


@dataclass
class Settings:
    """Application settings (defaults per docs/ARCHITECTURE.md §4).

    Accepted values (anything else raises ``UserInputError("settings.invalid")``):

    * ``sample_size``, ``memory_limit_bytes``, ``sort_memory_bytes``, ``large_node_bytes``,
      ``large_node_children``, ``head_bytes``: integers >= 1 (small values are allowed on
      purpose: tests use them to exercise the large-file code paths);
    * ``cache_limit_bytes``: integer >= 0 -- one budget for the whole cache: index files
      and prepared (gunzipped / transcoded) copies together, least recently used first;
      files in use and prepared copies used within the last hour are kept and do not
      count against it (``jsonreshape.fileio.evict_entries``), so the cache can be larger
      by their size;
    * ``cache_dir``: ``None`` (default folders under the app home) or a non-empty path
      string: the "Kesh papkasi". Index files go there, big work files (prepared copies,
      sort runs, XLSX staging) to ``<cache_dir>/tmp`` (``appdirs.work_dir``); a folder
      that cannot be used falls back to the default one with a warning;
    * ``language``: ``"uz"`` or ``"en"``;
    * ``export_indent``: ``None`` (compact), an integer 0..16 or ``"\\t"``;
    * ``csv_bom``: bool; ``csv_delimiter``: one character other than ``"``, CR, LF;
    * ``array_join_sep``: 1..16 characters without CR/LF;
    * ``xlsx_max_rows``: integer 2..1048576;
    * ``edits_location``: ``"appdata"`` or ``"beside"`` (where new edit layers are kept);
    * ``backup_on_save``: bool (keep ``<file>.bak`` when saving);
    * ``undo_limit``: integer >= 200 (undoable steps);
    * ``diff_align_max_bytes``: integer >= 1 (the largest files compared in full without a key).

    Byte sizes may also be given as strings such as ``"512MB"`` (see
    :func:`jsonreshape.tasks.parse_size`), ``export_indent`` as ``"tab"``; they are
    normalized on creation.
    """

    sample_size: int = 10000
    cache_dir: str | None = None
    cache_limit_bytes: int = 20 * GiB
    memory_limit_bytes: int = 512 * MiB
    sort_memory_bytes: int = 256 * MiB
    large_node_bytes: int = 50 * MiB
    large_node_children: int = 10000
    head_bytes: int = 4 * MiB
    language: str = "uz"
    export_indent: int | str | None = 2
    csv_bom: bool = True
    csv_delimiter: str = ","
    array_join_sep: str = ";"
    xlsx_max_rows: int = XLSX_MAX_ROWS
    edits_location: str = "appdata"
    backup_on_save: bool = True
    undo_limit: int = 1000
    diff_align_max_bytes: int = 200 * MiB

    def __post_init__(self) -> None:
        for f in fields(self):
            setattr(self, f.name, _normalize(f.name, getattr(self, f.name)))

    # -- (de)serialization ---------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-compatible dict of every setting."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, strict: bool = False) -> Settings:
        """Build settings from a dict; unknown keys are ignored.

        ``strict=False`` (used for files): an invalid value is replaced by its default and
        a warning is logged. ``strict=True``: the first invalid value raises
        ``UserInputError("settings.invalid")``.
        """
        if not isinstance(data, dict):
            raise TypeError("settings data must be a dict")
        known = {f.name for f in fields(cls)}
        values: dict[str, Any] = {}
        for name, value in data.items():
            if name not in known:
                if name != "version":
                    log.debug("ignoring unknown setting %r", name)
                continue
            if strict:
                values[name] = _normalize(name, value)
                continue
            try:
                values[name] = _normalize(name, value)
            except UserInputError:
                log.warning("invalid setting %s=%r; using the default", name, value)
        return cls(**values)

    # -- persistence ---------------------------------------------------------------------
    @classmethod
    def load(cls, path: StrPath | None = None) -> Settings:
        """Read settings from ``path`` (default ``settings_file()``); never raises.

        A missing file gives the defaults (logged at INFO: normal on first start); an
        unreadable or corrupt file (invalid JSON, not an object, too large) gives the
        defaults with a logged warning; invalid single values fall back to their defaults
        (warning per value).
        """
        from .fileio import open_shared

        try:
            p = Path(path) if path is not None else settings_file()
        except (JsonReshapeError, OSError) as exc:  # no usable data folder at all
            log.warning("no settings folder (%s); using defaults", exc)
            return cls()
        try:
            with open_shared(p, "rb") as f:
                raw = f.read(_MAX_SETTINGS_FILE + 1)
        except JsonReshapeError as exc:
            if exc.key == "error.file_not_found":
                log.info("settings file %s not found; using defaults", p)
            else:
                log.warning("cannot read settings file %s (%s); using defaults", p, exc)
            return cls()
        except OSError as exc:
            log.warning("cannot read settings file %s (%s); using defaults", p, exc)
            return cls()
        if len(raw) > _MAX_SETTINGS_FILE:
            log.warning("settings file %s is too large; using defaults", p)
            return cls()
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            log.warning("settings file %s is corrupt (%s); using defaults", p, exc)
            return cls()
        if not isinstance(data, dict):
            log.warning("settings file %s does not hold a JSON object; using defaults", p)
            return cls()
        return cls.from_dict(data)

    def save(self, path: StrPath | None = None) -> Path:
        """Write the settings atomically to ``path`` (default ``settings_file()``)."""
        from .fileio import atomic_write, make_dirs, os_error

        p = Path(path) if path is not None else settings_file()
        try:
            make_dirs(p.parent)
        except OSError as exc:
            raise os_error(exc, p.parent, writing=True) from exc
        payload = {"version": SETTINGS_VERSION, **self.to_dict()}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with atomic_write(p) as f:
            f.write(text.encode("utf-8"))
        log.info("settings saved to %s", p)
        return p

    def validate(self) -> None:
        """Re-check every value (e.g. after attributes were assigned directly)."""
        for f in fields(self):
            _normalize(f.name, getattr(self, f.name))


# ----------------------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------------------
_SIZE_FIELDS = frozenset(
    {"cache_limit_bytes", "memory_limit_bytes", "sort_memory_bytes", "large_node_bytes", "head_bytes",
     "diff_align_max_bytes"}
)
_MIN_INT = {
    "sample_size": 1,
    "cache_limit_bytes": 0,
    "memory_limit_bytes": 1,
    "sort_memory_bytes": 1,
    "large_node_bytes": 1,
    "large_node_children": 1,
    "head_bytes": 1,
    "undo_limit": UNDO_LIMIT_MIN,
    "diff_align_max_bytes": 1,
}


def _invalid(name: str, value: Any, expected: str, **params: Any) -> UserInputError:
    return UserInputError(
        "settings.invalid",
        name=name,
        label=t(f"settings.field.{name}"),
        value=repr(value),
        expected=t(expected, **params),
    )


def _as_int(name: str, value: Any, expected: str, **params: Any) -> int:
    if isinstance(value, bool):
        raise _invalid(name, value, expected, **params)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and name in _SIZE_FIELDS:
        from .tasks import parse_size

        try:
            return parse_size(value)
        except (ValueError, OverflowError):
            raise _invalid(name, value, expected, **params) from None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise _invalid(name, value, expected, **params) from None
    raise _invalid(name, value, expected, **params)


def _normalize(name: str, value: Any) -> Any:
    """Validated, normalized value of setting ``name`` (``UserInputError`` if invalid)."""
    if name in _MIN_INT:
        lo = _MIN_INT[name]
        n = _as_int(name, value, "settings.expected.int_min", min=lo)
        if n < lo:
            raise _invalid(name, value, "settings.expected.int_min", min=lo)
        return n
    if name == "xlsx_max_rows":
        n = _as_int(name, value, "settings.expected.int_range", min=2, max=XLSX_MAX_ROWS)
        if not 2 <= n <= XLSX_MAX_ROWS:
            raise _invalid(name, value, "settings.expected.int_range", min=2, max=XLSX_MAX_ROWS)
        return n
    if name == "cache_dir":
        if value is None:
            return None
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        if isinstance(value, str):
            value = value.strip()
            return value or None
        raise _invalid(name, value, "settings.expected.path")
    if name == "language":
        if isinstance(value, str) and value.strip().lower() in SUPPORTED_LANGUAGES:
            return value.strip().lower()
        raise _invalid(name, value, "settings.expected.language")
    if name == "export_indent":
        if value is None:
            return None
        if isinstance(value, str):
            s = value.strip().lower() if value != "\t" else value
            if s in ("\t", "tab", "\\t"):
                return "\t"
            if s in ("", "none", "compact"):
                return None
            if s.isdigit() and int(s) <= 16:
                return int(s)
            raise _invalid(name, value, "settings.expected.indent")
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 16:
            return int(value)
        raise _invalid(name, value, "settings.expected.indent")
    if name in ("csv_bom", "backup_on_save"):
        if isinstance(value, bool):
            return value
        raise _invalid(name, value, "settings.expected.bool")
    if name == "edits_location":
        if isinstance(value, str) and value.strip().lower() in EDITS_LOCATIONS:
            return value.strip().lower()
        raise _invalid(name, value, "settings.expected.edits_location")
    if name == "csv_delimiter":
        if isinstance(value, str) and len(value) == 1 and value not in "\"\r\n":
            return value
        raise _invalid(name, value, "settings.expected.delimiter")
    if name == "array_join_sep":
        if isinstance(value, str) and 1 <= len(value) <= 16 and "\r" not in value and "\n" not in value:
            return value
        raise _invalid(name, value, "settings.expected.separator")
    raise KeyError(name)  # pragma: no cover - every field is handled above


# ----------------------------------------------------------------------------------
# process-wide instance
# ----------------------------------------------------------------------------------
_lock = threading.Lock()
_current: Settings | None = None


def get_settings() -> Settings:
    """The process-wide settings, loaded from ``settings_file()`` on first use."""
    global _current
    s = _current
    if s is None:
        with _lock:
            if _current is None:
                _current = Settings.load()
            s = _current
    return s


def set_settings(settings: Settings) -> None:
    """Replace the process-wide settings (does not save them)."""
    global _current
    if not isinstance(settings, Settings):
        raise TypeError("settings must be a Settings instance")
    settings.validate()
    with _lock:
        _current = settings


def _reset_for_tests() -> None:
    """Forget the cached instance (tests/conftest.py calls this before every test)."""
    global _current
    with _lock:
        _current = None
