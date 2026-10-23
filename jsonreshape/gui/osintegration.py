"""Windows "Open with" registration (docs/ARCHITECTURE.md §15.0 "Open with on Windows"; AGENT_PROMPT §11).

Settings ▸ General shows (on Windows only) the rows of :class:`OpenWithRows`: the current state and the buttons
"Windows: .json fayllarni «Open with» ro'yxatiga qo'shish" / "olib tashlash". They act at once, after a
confirmation -- independent of the dialog's "Saqlash" / "Bekor qilish": a registry change is an action, not a
setting.

What is written (:func:`plan`) -- ONLY per-user keys under ``HKEY_CURRENT_USER\\Software\\Classes`` (paths below
are relative to it):

* ``jsonreshape.document`` (default value: the file type name; ``JsonReshapeCreatedKeys``: see below) with
  ``shell\\open\\command`` (default value) = the launch command;
* ``Applications\\jsonreshape-gui.exe`` with ``FriendlyAppName`` = ``JSON Reshape``,
  ``shell\\open\\command`` = the launch command and ``SupportedTypes`` (``.json .jsonl .ndjson .geojson .gz``,
  empty strings);
* ``<ext>\\OpenWithProgids`` value ``jsonreshape.document`` (``REG_NONE``, empty) for each of those extensions.

The default program is never changed: no default value of an extension key, no ``UserChoice``, no
``OpenWithList`` is written (Windows does not allow it silently; the user picks the program in "Open with").
The launch command (:func:`launch_command`) is ``"<exe>" "%1"`` for the packaged ``jsonreshape-gui.exe``
and, for development, ``"<venv>\\pythonw.exe" -m jsonreshape.gui "%1"`` (``pythonw.exe`` beside
``sys.executable``); anything else (another frozen exe, no ``pythonw.exe``, not Windows) cannot register
(:func:`unavailable_reason`).

Removal (:func:`unregister`) deletes exactly what registration wrote: its values (by name), the keys named
after the application (``jsonreshape.document\\...``, ``Applications\\jsonreshape-gui.exe\\...``) once they
are empty, and the shared keys (``.geojson``, ``.json\\OpenWithProgids``, ``Applications``, ...) only when
registration created them -- recorded in the ``REG_MULTI_SZ`` value ``JsonReshapeCreatedKeys`` of the ProgID
key, written before anything else -- and only when they are empty afterwards. A value or subkey written by
anybody else is never touched, and a key that still holds one stays. ``SHChangeNotify(SHCNE_ASSOCCHANGED)``
tells Explorer after every change.

The registry is an injected :class:`RegistryBackend`: :class:`WinregBackend` (``winreg``, confined to
``HKCU\\Software\\Classes``) in production, :class:`MemoryRegistry` (dict based, case-insensitive like the real
one) in tests. Safety net: while pytest runs a test (``PYTEST_CURRENT_TEST`` is set) :func:`default_backend`
returns a process-wide :class:`MemoryRegistry` and :func:`default_notifier` a no-op, so no test -- not even one
that clicks every button of the settings dialog with an auto-"yes" ``confirm_handler`` -- can write the real
registry.

The registration logic (plan, register, unregister, status, launch command) uses no Qt object (no application
needed); :class:`OpenWithRows` is the settings dialog's part (:func:`add_settings_rows`). Registry calls take
milliseconds and run in the GUI thread; ``SHChangeNotify`` is called without ``SHCNF_FLUSH`` (it does not wait
for the other windows).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from jsonreshape.errors import JsonReshapeError
from jsonreshape.gui import theme
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = [
    "APP_EXE_NAME", "APP_KEY", "CLASSES_ROOT", "CREATED_VALUE", "EXTENSIONS", "FRIENDLY_APP_NAME", "MemoryRegistry",
    "OpenWithError", "OpenWithRows", "Outcome", "PROG_ID", "REG_MULTI_SZ", "REG_NONE", "REG_SZ", "RegValue",
    "RegistryBackend", "Status", "WinregBackend", "add_settings_rows", "backend_factory", "default_backend",
    "default_notifier", "launch_command", "notifier_factory", "notify_shell", "own_keys", "plan", "register",
    "shared_keys", "status", "unavailable_reason", "unregister",
]

# registry value types (the winreg numbers; defined here so the module needs no winreg off Windows)
REG_NONE = 0
REG_SZ = 1
REG_MULTI_SZ = 7

#: Everything lives under this key of ``HKEY_CURRENT_USER`` (per user; no administrator rights).
CLASSES_ROOT = "Software\\Classes"
APP_EXE_NAME = "jsonreshape-gui.exe"
APP_KEY = "Applications\\" + APP_EXE_NAME
PROG_ID = "jsonreshape.document"
FRIENDLY_APP_NAME = "JSON Reshape"
EXTENSIONS: tuple[str, ...] = (".json", ".jsonl", ".ndjson", ".geojson", ".gz")
#: ``REG_MULTI_SZ`` value of the ProgID key: the shared keys registration created (removal deletes only those).
CREATED_VALUE = "JsonReshapeCreatedKeys"

SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000


class OpenWithError(JsonReshapeError):
    """Writing or removing the registration failed (``detail``: the system's message)."""

    key = "gui.os.error.add"


# ==================================================================================
# backends
# ==================================================================================
class RegistryBackend(Protocol):
    """The registry operations the registration needs. Paths are relative to ``HKCU\\Software\\Classes``
    (``""`` = that key); value name ``""`` is the key's default value. Key and value names are
    case-insensitive."""

    def key_exists(self, path: str) -> bool: ...

    def create_key(self, path: str) -> None: ...          # the parent must exist

    def get_value(self, path: str, name: str) -> tuple[Any, int] | None: ...

    def set_value(self, path: str, name: str, data: Any, kind: int) -> None: ...    # the key must exist

    def delete_value(self, path: str, name: str) -> bool: ...

    def delete_key(self, path: str) -> None: ...          # a key without subkeys

    def subkeys(self, path: str) -> list[str]: ...

    def value_names(self, path: str) -> list[str]: ...


def _check_path(path: str) -> str:
    text = str(path)
    if text == "":
        return text
    parts = text.split("\\")
    if any(p in ("", ".", "..") for p in parts) or text.upper().startswith("HKEY_"):
        raise ValueError(f"invalid registry path: {path!r}")
    return text


def _parent(path: str) -> str:
    return path.rpartition("\\")[0]


class MemoryRegistry:
    """A dict-based registry with the semantics :class:`WinregBackend` relies on (case-insensitive names,
    ``create_key`` needs the parent, ``delete_key`` refuses a key with subkeys and drops its values) -- the
    test double, and the safety net under pytest (see the module docstring)."""

    def __init__(self, keys: Iterable[str] = ()) -> None:
        self._keys: dict[str, str] = {"": ""}                          # folded path -> path as created
        self._values: dict[str, dict[str, tuple[str, Any, int]]] = {"": {}}
        self.writes = 0                                                 # every changing call (tests)
        for key in keys:
            self.ensure(key)

    # -- helpers ----------------------------------------------------------------------
    @staticmethod
    def _fold(path: str) -> str:
        return _check_path(path).casefold()

    def ensure(self, path: str) -> None:
        """Create ``path`` and every missing parent (test set-up)."""
        parts = _check_path(path).split("\\") if path else []
        for i in range(1, len(parts) + 1):
            sub = "\\".join(parts[:i])
            if not self.key_exists(sub):
                self.create_key(sub)

    def dump(self) -> dict[str, dict[str, tuple[Any, int]]]:
        """``{path: {value name: (data, kind)}}`` of every key (original spelling), for comparisons."""
        return {self._keys[k]: {name: (data, kind) for name, data, kind in self._values[k].values()}
                for k in sorted(self._keys) if k}

    # -- RegistryBackend --------------------------------------------------------------
    def key_exists(self, path: str) -> bool:
        return self._fold(path) in self._keys

    def create_key(self, path: str) -> None:
        folded = self._fold(path)
        if folded in self._keys:
            return
        if _parent(folded) not in self._keys:
            raise FileNotFoundError(f"parent key missing: {path!r}")
        self._keys[folded] = path
        self._values[folded] = {}
        self.writes += 1

    def get_value(self, path: str, name: str) -> tuple[Any, int] | None:
        values = self._values.get(self._fold(path))
        if values is None:
            return None
        found = values.get(name.casefold())
        return None if found is None else (found[1], found[2])

    def set_value(self, path: str, name: str, data: Any, kind: int) -> None:
        values = self._values.get(self._fold(path))
        if values is None:
            raise FileNotFoundError(f"key missing: {path!r}")
        old = values.get(name.casefold())
        values[name.casefold()] = (old[0] if old else name, list(data) if isinstance(data, list) else data, int(kind))
        self.writes += 1

    def delete_value(self, path: str, name: str) -> bool:
        values = self._values.get(self._fold(path))
        if values is None or name.casefold() not in values:
            return False
        del values[name.casefold()]
        self.writes += 1
        return True

    def delete_key(self, path: str) -> None:
        folded = self._fold(path)
        if not folded:
            raise PermissionError("the root key cannot be deleted")
        if folded not in self._keys:
            raise FileNotFoundError(f"key missing: {path!r}")
        if self.subkeys(path):
            raise PermissionError(f"key has subkeys: {path!r}")
        del self._keys[folded]
        del self._values[folded]
        self.writes += 1

    def subkeys(self, path: str) -> list[str]:
        folded = self._fold(path)
        prefix = folded + "\\" if folded else ""
        return sorted(self._keys[k].rpartition("\\")[2] for k in self._keys
                      if k and k.startswith(prefix) and "\\" not in k[len(prefix):])

    def value_names(self, path: str) -> list[str]:
        values = self._values.get(self._fold(path))
        return [] if values is None else [entry[0] for entry in values.values()]


class WinregBackend:
    """The real registry through :mod:`winreg`: ``HKEY_CURRENT_USER`` below ``root`` (``Software\\Classes``)
    only -- nothing else can be reached through it. ``hive`` replaces ``HKEY_CURRENT_USER`` (tests: a private
    application hive file loaded with ``RegLoadAppKeyW``, never the user's registry)."""

    def __init__(self, root: str = CLASSES_ROOT, *, hive: Any = None) -> None:
        import winreg  # Windows only

        self._w = winreg
        self.hive = winreg.HKEY_CURRENT_USER if hive is None else hive
        self.root = _check_path(root)

    def _full(self, path: str) -> str:
        path = _check_path(path)
        return self.root + "\\" + path if path else self.root

    def _open(self, path: str, access: int) -> Any:
        return self._w.OpenKey(self.hive, self._full(path), 0, access)

    def key_exists(self, path: str) -> bool:
        try:
            with self._open(path, self._w.KEY_READ):
                return True
        except FileNotFoundError:
            return False

    def create_key(self, path: str) -> None:
        if not self.key_exists(_parent(path)):
            raise FileNotFoundError(f"parent key missing: {path!r}")
        self._w.CreateKeyEx(self.hive, self._full(path), 0, self._w.KEY_WRITE).Close()

    def get_value(self, path: str, name: str) -> tuple[Any, int] | None:
        try:
            with self._open(path, self._w.KEY_READ) as key:
                data, kind = self._w.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return data, int(kind)

    def set_value(self, path: str, name: str, data: Any, kind: int) -> None:
        with self._open(path, self._w.KEY_SET_VALUE) as key:
            self._w.SetValueEx(key, name, 0, int(kind), data)

    def delete_value(self, path: str, name: str) -> bool:
        try:
            with self._open(path, self._w.KEY_SET_VALUE) as key:
                self._w.DeleteValue(key, name)
        except FileNotFoundError:
            return False
        return True

    def delete_key(self, path: str) -> None:
        if not _check_path(path):
            raise PermissionError("the root key cannot be deleted")
        self._w.DeleteKey(self.hive, self._full(path))

    def _enum(self, path: str, values: bool) -> list[str]:
        out: list[str] = []
        try:
            with self._open(path, self._w.KEY_READ) as key:
                i = 0
                while True:
                    try:
                        item = self._w.EnumValue(key, i)[0] if values else self._w.EnumKey(key, i)
                    except OSError:          # ERROR_NO_MORE_ITEMS
                        break
                    out.append(item)
                    i += 1
        except FileNotFoundError:
            return []
        return out

    def subkeys(self, path: str) -> list[str]:
        return self._enum(path, False)

    def value_names(self, path: str) -> list[str]:
        return self._enum(path, True)


_safety_registry: MemoryRegistry | None = None


def _under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def default_backend() -> RegistryBackend:
    """:class:`WinregBackend` on Windows; a process-wide :class:`MemoryRegistry` elsewhere and while pytest
    runs a test (the safety net of the module docstring)."""
    global _safety_registry
    if sys.platform == "win32" and not _under_pytest():
        return WinregBackend()
    if _safety_registry is None:
        _safety_registry = MemoryRegistry()
    return _safety_registry


def notify_shell() -> bool:
    """``SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST)``: Explorer re-reads the associations (asynchronous:
    it does not wait for the windows to answer). False off Windows or on failure."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        return True
    except Exception:  # noqa: BLE001 - a missing shell32 must not break the settings dialog
        log.debug("SHChangeNotify failed", exc_info=True)
        return False


def default_notifier() -> Callable[[], Any]:
    """:func:`notify_shell`, or a no-op while pytest runs a test."""
    return (lambda: False) if _under_pytest() else notify_shell


# ==================================================================================
# launch command
# ==================================================================================
def _quote(path: str) -> str:
    return '"' + path + '"'


def launch_command(*, frozen: bool | None = None, executable: str | os.PathLike[str] | None = None,
                   platform: str | None = None) -> str | None:
    """The command Explorer runs for a file: ``"<jsonreshape-gui.exe>" "%1"`` when this process is the
    packaged GUI exe, ``"<pythonw.exe>" -m jsonreshape.gui "%1"`` when it runs from Python (development) and
    ``pythonw.exe`` lies beside ``sys.executable``; ``None`` when this process cannot register (see
    :func:`unavailable_reason`). The parameters override ``sys.frozen`` / ``sys.executable`` / ``sys.platform``
    (tests)."""
    return _launch(frozen, executable, platform)[0]


def unavailable_reason(*, frozen: bool | None = None, executable: str | os.PathLike[str] | None = None,
                       platform: str | None = None) -> str | None:
    """Why this process cannot register (a localized text), ``None`` when it can."""
    return _launch(frozen, executable, platform)[1]


def _launch(frozen: bool | None, executable: str | os.PathLike[str] | None,
            platform: str | None) -> tuple[str | None, str | None]:
    if (platform or sys.platform) != "win32":
        return None, t("gui.os.unavailable.platform")
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    exe = Path(os.fspath(executable if executable is not None else (sys.executable or "")))
    if frozen:
        # the name as Windows reads it (the platform may be given for a test on another system)
        if PureWindowsPath(exe).name.casefold() != APP_EXE_NAME:
            return None, t("gui.os.unavailable.exe", name=APP_EXE_NAME)
        return f"{_quote(str(exe))} \"%1\"", None
    pythonw = exe.with_name("pythonw.exe") if exe.name.casefold() in ("python.exe", "pythonw.exe") else None
    if pythonw is None or not pythonw.is_file():
        return None, t("gui.os.unavailable.python", path=str(exe.parent / "pythonw.exe"))
    return f"{_quote(str(pythonw))} -m jsonreshape.gui \"%1\"", None


# ==================================================================================
# plan, register, unregister, status
# ==================================================================================
@dataclass(frozen=True)
class RegValue:
    """One value the registration writes (``key`` relative to ``HKCU\\Software\\Classes``; ``name`` ``""`` =
    the default value)."""

    key: str
    name: str
    data: Any
    kind: int


def _prog_command_key() -> str:
    return PROG_ID + "\\shell\\open\\command"


def _app_command_key() -> str:
    return APP_KEY + "\\shell\\open\\command"


def plan(command: str, *, type_name: str | None = None) -> list[RegValue]:
    """Every value :func:`register` writes for ``command`` (in writing order, the manifest excepted)."""
    out = [
        RegValue(PROG_ID, "", type_name if type_name is not None else t("gui.os.type_name"), REG_SZ),
        RegValue(_prog_command_key(), "", command, REG_SZ),
        RegValue(APP_KEY, "FriendlyAppName", FRIENDLY_APP_NAME, REG_SZ),
        RegValue(_app_command_key(), "", command, REG_SZ),
    ]
    out += [RegValue(APP_KEY + "\\SupportedTypes", ext, "", REG_SZ) for ext in EXTENSIONS]
    out += [RegValue(ext + "\\OpenWithProgids", PROG_ID, None, REG_NONE) for ext in EXTENSIONS]
    return out


def own_keys() -> list[str]:
    """The keys named after the application (deleted by :func:`unregister` once empty), parents first."""
    return [PROG_ID, PROG_ID + "\\shell", PROG_ID + "\\shell\\open", _prog_command_key(),
            APP_KEY, APP_KEY + "\\shell", APP_KEY + "\\shell\\open", _app_command_key(), APP_KEY + "\\SupportedTypes"]


def shared_keys() -> list[str]:
    """The keys other programs use too (parents first): deleted only when registration created them."""
    out = ["Applications"]
    for ext in EXTENSIONS:
        out += [ext, ext + "\\OpenWithProgids"]
    return out


def _read_manifest(backend: RegistryBackend) -> list[str]:
    found = backend.get_value(PROG_ID, CREATED_VALUE)
    if found is None or found[1] != REG_MULTI_SZ or not isinstance(found[0], list):
        return []
    allowed = {k.casefold(): k for k in shared_keys()}
    return [allowed[str(k).casefold()] for k in found[0] if str(k).casefold() in allowed]


def _write_manifest(backend: RegistryBackend, keys: Iterable[str]) -> None:
    order = {k.casefold(): i for i, k in enumerate(shared_keys())}
    unique = sorted({k.casefold(): k for k in keys}.values(), key=lambda k: order.get(k.casefold(), 0))
    backend.set_value(PROG_ID, CREATED_VALUE, unique, REG_MULTI_SZ)


def _ensure(backend: RegistryBackend, key: str, created: list[str]) -> None:
    parts = key.split("\\")
    for i in range(1, len(parts) + 1):
        sub = "\\".join(parts[:i])
        if not backend.key_exists(sub):
            backend.create_key(sub)
            created.append(sub)


@dataclass
class Outcome:
    """What :func:`register` / :func:`unregister` changed (for the status line and tests)."""

    values: list[tuple[str, str]] = field(default_factory=list)      # (key, value name) written / removed
    keys: list[str] = field(default_factory=list)                    # keys created / deleted
    notified: bool = False


def register(backend: RegistryBackend, command: str, *, notify: Callable[[], Any] | None = None,
             type_name: str | None = None) -> Outcome:
    """Write the registration of :func:`plan` for ``command`` (again: values are overwritten, so a moved
    program is registered under its new path). The shared keys created are recorded first; on a failure the
    record is corrected to what was really created and :class:`OpenWithError` is raised (what was written
    stays: "remove" deletes it). Tells Explorer (``notify``) when anything was written."""
    if not command or '"%1"' not in command:
        raise ValueError(f"not a launch command: {command!r}")
    entries = plan(command, type_name=type_name)
    old_record = _read_manifest(backend) if backend.key_exists(PROG_ID) else []
    shared = {k.casefold() for k in shared_keys()}
    missing_shared = [k for k in shared_keys() if not backend.key_exists(k)]
    outcome = Outcome()
    created: list[str] = []
    try:
        _ensure(backend, PROG_ID, created)
        _write_manifest(backend, old_record + missing_shared)        # before any shared key is created
        for entry in entries:
            _ensure(backend, entry.key, created)
            backend.set_value(entry.key, entry.name, entry.data, entry.kind)
            outcome.values.append((entry.key, entry.name))
    except (OSError, ValueError) as exc:
        really = [k for k in created if k.casefold() in shared]
        try:
            if backend.key_exists(PROG_ID):
                _write_manifest(backend, old_record + really)
        except OSError:
            log.warning("could not correct the record of created keys", exc_info=True)
        outcome.keys = created
        _notify(notify, outcome)
        log.warning("Open with: registration failed after %d values: %s", len(outcome.values), exc)
        raise OpenWithError("gui.os.error.add", detail=_detail(exc)) from exc
    outcome.keys = created
    _notify(notify, outcome)
    log.info("Open with: registered %s (%d values, %d new keys)", command, len(outcome.values), len(created))
    return outcome


def unregister(backend: RegistryBackend, *, notify: Callable[[], Any] | None = None) -> Outcome:
    """Delete exactly what :func:`register` writes (see the module docstring): the values by name, the
    application's own keys once empty, the shared keys it created (its record) once empty. Foreign values and
    keys stay. :class:`OpenWithError` when the registry refuses."""
    outcome = Outcome()
    try:
        record = _read_manifest(backend)
        names = [(e.key, e.name) for e in plan('"x" "%1"', type_name="")] + [(PROG_ID, CREATED_VALUE)]
        for key, name in names:
            if backend.key_exists(key) and backend.delete_value(key, name):
                outcome.values.append((key, name))
        for key in sorted(own_keys() + record, key=lambda k: -k.count("\\")):     # children before parents
            if backend.key_exists(key) and not backend.subkeys(key) and not backend.value_names(key):
                backend.delete_key(key)
                outcome.keys.append(key)
    except OSError as exc:
        _notify(notify, outcome)
        log.warning("Open with: removal failed: %s", exc)
        raise OpenWithError("gui.os.error.remove", detail=_detail(exc)) from exc
    _notify(notify, outcome)
    log.info("Open with: removed %d values and %d keys", len(outcome.values), len(outcome.keys))
    return outcome


def _notify(notify: Callable[[], Any] | None, outcome: Outcome) -> None:
    if not (outcome.values or outcome.keys):
        return
    try:
        outcome.notified = bool((notify or default_notifier())())
    except Exception:  # noqa: BLE001 - informing Explorer is best effort
        log.debug("notifying the shell failed", exc_info=True)


def _detail(exc: BaseException) -> str:
    text = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    return str(text)


@dataclass(frozen=True)
class Status:
    """The registration found in the registry: ``state`` is ``"none"`` (nothing of it), ``"current"`` (complete,
    for ``command``), ``"other"`` (complete, for another command -- another copy of the program) or
    ``"partial"``; ``command`` is the registered ProgID command (``None`` when absent)."""

    state: str
    command: str | None
    extensions: tuple[str, ...] = ()


def status(backend: RegistryBackend, command: str | None = None) -> Status:
    """Read the registration (``OSError`` when the registry cannot be read)."""
    found = backend.get_value(_prog_command_key(), "")
    registered = found[0] if found is not None and isinstance(found[0], str) else None
    exts = tuple(ext for ext in EXTENSIONS
                 if backend.get_value(ext + "\\OpenWithProgids", PROG_ID) is not None)
    expected = plan(registered or '"x" "%1"', type_name="")
    present = [e for e in expected if e.key != PROG_ID and backend.get_value(e.key, e.name) is not None]
    if registered is None and not exts and not present and not backend.key_exists(PROG_ID):
        return Status("none", None, ())
    complete = registered is not None and len(present) == len(expected) - 1
    if complete:
        app_cmd = backend.get_value(_app_command_key(), "")
        complete = app_cmd is not None and app_cmd[0] == registered
    if not complete:
        return Status("partial", registered, exts)
    if command is not None and registered.casefold() != command.casefold():
        return Status("other", registered, exts)
    return Status("current", registered, exts)


# ==================================================================================
# the settings dialog's rows
# ==================================================================================
#: ``fn() -> RegistryBackend`` / ``fn() -> notifier`` used by :class:`OpenWithRows` (tests may replace them).
backend_factory: Callable[[], RegistryBackend] = default_backend
notifier_factory: Callable[[], Callable[[], Any]] = default_notifier


class OpenWithRows(QWidget):
    """State line + "add" / "remove" buttons (Settings ▸ General, Windows only). ``changed`` is emitted after
    a registration or removal (also a failed one: something may have been written)."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, app: Any = None,
                 backend: RegistryBackend | None = None, notify: Callable[[], Any] | None = None,
                 command: str | None = None, reason: str | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.backend = backend if backend is not None else backend_factory()
        self.notify = notify if notify is not None else notifier_factory()
        if command is None and reason is None:
            command, reason = launch_command(), unavailable_reason()
        self.command = command
        self.reason = reason
        self.state: Status | None = None
        self._message: tuple[str, str] | None = None          # (text, role) of the last action
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout()
        self.add_button = QPushButton(t("gui.os.add"), self)
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(self.ask_add)
        buttons.addWidget(self.add_button)
        self.remove_button = QPushButton(t("gui.os.remove"), self)
        self.remove_button.setAutoDefault(False)
        self.remove_button.setToolTip(t("gui.os.remove.tip"))
        self.remove_button.clicked.connect(self.ask_remove)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.refresh()

    # -- state ------------------------------------------------------------------------
    def refresh(self) -> Status | None:
        """Re-read the registry and update the line and the buttons."""
        try:
            self.state = status(self.backend, self.command)
            error = None
        except OSError as exc:
            self.state = None
            error = _detail(exc)
        if error is not None:
            text, role = t("gui.os.status.unknown", error=error), "danger"
        elif self._message is not None:
            text, role = self._message
        else:
            text, role = self._state_text(), "muted"
        self.status_label.setText(text)
        self.status_label.setProperty("role", role)
        theme.bind_style(self.status_label, f"color: ${role};")
        registered = self.state is not None and self.state.state != "none"
        self.add_button.setEnabled(self.command is not None)
        self.add_button.setToolTip(self.reason or t("gui.os.add.tip", command=self.command or ""))
        self.remove_button.setEnabled(registered)
        return self.state

    def _state_text(self) -> str:
        st = self.state
        if st is None or st.state == "none":
            text = t("gui.os.status.none")
        elif st.state == "current":
            text = t("gui.os.status.current")
        elif st.state == "other":
            text = t("gui.os.status.other", command=st.command or "")
        else:
            text = t("gui.os.status.partial")
        if self.reason and (st is None or st.state != "current"):
            text += "\n" + self.reason
        return text

    # -- actions ----------------------------------------------------------------------
    def ask_add(self) -> None:
        """Confirm, then :meth:`add`."""
        if self.command is None:
            return
        from jsonreshape.gui.widgets import ask

        text = t("gui.os.confirm.add", command=self.command, extensions=" ".join(EXTENSIONS))
        ask(self, text, lambda yes: self.add() if yes else None, title=t("gui.os.confirm.title"))

    def ask_remove(self) -> None:
        """Confirm, then :meth:`remove`."""
        from jsonreshape.gui.widgets import ask

        ask(self, t("gui.os.confirm.remove"), lambda yes: self.remove() if yes else None,
            title=t("gui.os.confirm.title"))

    def add(self) -> bool:
        """Register for :attr:`command` (no question); False when it failed (reported)."""
        if self.command is None:
            return False
        try:
            register(self.backend, self.command, notify=self.notify)
        except (OpenWithError, ValueError) as exc:
            return self._failed(exc)
        self._message = (t("gui.os.done.add"), "ok")
        self.refresh()
        self.changed.emit()
        return True

    def remove(self) -> bool:
        """Delete the registration (no question); False when it failed (reported)."""
        try:
            unregister(self.backend, notify=self.notify)
        except OpenWithError as exc:
            return self._failed(exc)
        self._message = (t("gui.os.done.remove"), "ok")
        self.refresh()
        self.changed.emit()
        return True

    def _failed(self, exc: BaseException) -> bool:
        message = exc.message if isinstance(exc, JsonReshapeError) else str(exc)
        self._message = (message, "danger")
        self.refresh()
        self.changed.emit()
        report = getattr(self.app, "report_error", None)
        if report is not None and isinstance(exc, JsonReshapeError):
            report(exc, self.window())
        return False


def add_settings_rows(page: QWidget, form: QFormLayout, *, app: Any = None,
                      platform: str | None = None) -> OpenWithRows | None:
    """Add the "Open with" rows to the settings dialog's General page; nothing (``None``) off Windows."""
    if (platform or sys.platform) != "win32":
        return None
    rows = OpenWithRows(page, app=app)
    form.addRow(t("gui.os.settings.label"), rows)
    return rows
