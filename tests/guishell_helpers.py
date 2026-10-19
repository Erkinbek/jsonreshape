"""Shared helpers of the application-shell GUI tests (test_gui_mainwindow / doctab /
navigation / statusbar / taskspanel / app / shell_edit / doctab_edit): stub views and
panels that record the calls the shell makes, a stub edit controller with the
ARCHITECTURE §12.9 surface, data file writers and wait helpers."""

from __future__ import annotations

import json
import time
import weakref
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QLineEdit, QVBoxLayout, QWidget

from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.widgets import ViewBase

TIMEOUT = 30000   # ms


# ----------------------------------------------------------------------------------------
# stub views
# ----------------------------------------------------------------------------------------
class StubView(ViewBase):
    """Records every hook the shell calls; has a focusable line edit."""

    view_id = "stub"
    instances: list[StubView] = []

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(session, parent)
        self.calls: list[tuple] = []
        self.selected = 1
        self.edit = QLineEdit(self)
        self.edit.setObjectName("stub_edit")
        layout = QVBoxLayout(self)
        layout.addWidget(self.edit)
        self.setFocusProxy(self.edit)
        StubView.instances.append(self)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def activate(self) -> None:
        self.calls.append(("activate",))

    def zoom(self, steps: int) -> None:
        self.calls.append(("zoom", steps))

    def quick_find(self) -> None:
        self.calls.append(("quick_find",))

    def find_next(self, backwards: bool = False) -> None:
        self.calls.append(("find_next", backwards))

    def copy_value(self, fmt: str = "json") -> None:
        self.calls.append(("copy_value", fmt))

    def copy_path(self, style: str = "dotted") -> None:
        self.calls.append(("copy_path", style))

    def copy_key(self) -> None:
        self.calls.append(("copy_key",))

    def selection_count(self) -> int:
        return self.selected

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        super().shutdown()


class TreeStub(StubView):
    view_id = "tree"

    def expand_all_current(self) -> None:
        self.calls.append(("expand_all_current",))

    def collapse_all(self) -> None:
        self.calls.append(("collapse_all",))

    def expand_to_level(self, n: int) -> None:
        self.calls.append(("expand_to_level", n))

    def toggle_sort_keys(self, on: bool) -> None:
        self.calls.append(("toggle_sort_keys", on))


class TableStub(StubView):
    view_id = "table"
    columns = ["id", "name"]

    def add_column(self, path: str) -> None:
        self.calls.append(("add_column", path))

    def visible_column_paths(self) -> list[str]:
        return list(self.columns)


class TextStub(StubView):
    view_id = "text"
    open_raw_requested = Signal(int, int)


class RawStub(StubView):
    view_id = "raw"

    def goto_offset(self, offset: int, length: int = 1) -> None:
        self.calls.append(("goto_offset", offset, length))

    def goto_line(self, line: int) -> None:
        self.calls.append(("goto_line", line))


class SchemaStub(StubView):
    view_id = "schema"
    add_column_requested = Signal(str)
    filter_field_requested = Signal(str)
    mapping_requested = Signal()


class MappingStub(StubView):
    """Stands in for gui.mapview.MapView: an undo history of its own (``undo`` / ``redo`` +
    ``can_undo`` / ``can_redo`` / ``undo_text``) and the export with the mapping."""

    view_id = "mapping"
    history_changed = Signal()
    can_undo = False
    can_redo = False
    undo_text = ""
    redo_text = ""

    def undo(self) -> bool:
        self.calls.append(("undo",))
        return True

    def redo(self) -> bool:
        self.calls.append(("redo",))
        return True

    def export_with_mapping(self) -> str:
        self.calls.append(("export_with_mapping",))
        return "mapping-export"

    def set_history(self, *, can_undo: bool = False, can_redo: bool = False, text: str = "") -> None:
        self.can_undo, self.can_redo = can_undo, can_redo
        self.undo_text = text if can_undo else ""
        self.redo_text = text if can_redo else ""
        self.history_changed.emit()


def stub_registry() -> dict[str, type]:
    return {"tree": TreeStub, "table": TableStub, "text": TextStub, "raw": RawStub, "schema": SchemaStub}


def mapping_registry() -> dict[str, type]:
    """The stub views + :class:`MappingStub` for the structure builder."""
    reg = stub_registry()
    reg["mapping"] = MappingStub
    return reg


class EditTreeStub(TreeStub):
    """A tree stub implementing the view edit protocol of ``gui/actions.py`` (records calls;
    ``states`` maps a command name to the ``(enabled, reason)`` its ``edit_command_state``
    returns)."""

    states: dict[str, tuple[bool, str]] = {}

    def edit_value(self) -> None:
        self.calls.append(("edit_value",))

    def edit_as_json(self) -> None:
        self.calls.append(("edit_as_json",))

    def rename_key(self) -> None:
        self.calls.append(("rename_key",))

    def insert_node(self, where: str) -> None:
        self.calls.append(("insert_node", where))

    def duplicate_selection(self) -> None:
        self.calls.append(("duplicate_selection",))

    def delete_selection(self) -> None:
        self.calls.append(("delete_selection",))

    def move_selection(self, delta: int) -> None:
        self.calls.append(("move_selection", delta))

    def change_type(self, kind: str) -> None:
        self.calls.append(("change_type", kind))

    def paste_json(self) -> None:
        self.calls.append(("paste_json",))

    def edit_command_state(self, name: str) -> tuple[bool, str] | None:
        return type(self).states.get(name)


def edit_registry() -> dict[str, type]:
    reg = stub_registry()
    reg["tree"] = EditTreeStub
    return reg


# ----------------------------------------------------------------------------------------
# stub edit controller (ARCHITECTURE §12.9 surface of gui.editing.EditController)
# ----------------------------------------------------------------------------------------
class StubEditController(QObject):
    """Records the calls the shell makes; its state is set by the test (:meth:`set`).

    ``answer`` decides :meth:`ask_close`: ``"save"`` / ``"discard"`` (clean afterwards, then
    ``on_done(True)``), ``"keep"`` (the layer stays, ``on_done(True)`` -- the headless default),
    ``"cancel"`` (``on_done(False)``) or ``None``: the question stays open in :attr:`questions`
    until the test calls :meth:`answer_question`."""

    changed = Signal(object)
    state_changed = Signal()
    pending_found = Signal(object)

    instances: list[StubEditController] = []

    def __init__(self, session: Any = None, **state: Any) -> None:
        super().__init__()
        self.session = session
        self.available = True
        self.reason = ""
        self.busy = False
        self.dirty = False
        self.can_undo = False
        self.can_redo = False
        self.undo_text = ""
        self.redo_text = ""
        self.steps = 0
        self.applied_gen = 0
        self.pending: list[Any] = []
        self.calls: list[tuple] = []
        self.answer: str | None = "keep"
        self.questions: list[tuple[Any, Callable[[bool], Any], str]] = []
        self.detached = False
        for key, value in state.items():
            setattr(self, key, value)
        StubEditController.instances.append(self)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def set(self, **state: Any) -> None:
        for key, value in state.items():
            setattr(self, key, value)
        self.state_changed.emit()

    # -- questions ---------------------------------------------------------------------
    def ask_close(self, parent: Any, on_done: Callable[[bool], Any], *, why: str) -> None:
        self.calls.append(("ask_close", why))
        if self.answer is None:
            self.questions.append((parent, on_done, why))
            return
        self._answer(self.answer, on_done)

    def answer_question(self, answer: str, index: int = 0) -> None:
        _parent, on_done, _why = self.questions.pop(index)
        self._answer(answer, on_done)

    def _answer(self, answer: str, on_done: Callable[[bool], Any]) -> None:
        self.calls.append(("answered", answer))
        if answer == "save":           # like the real one: saved, the edit session ended (no reopen)
            self.set(dirty=False, steps=0, can_undo=False, can_redo=False, available=False, reason="closed")
            on_done(True)
        elif answer == "discard":
            self.set(dirty=False, steps=0, can_undo=False, can_redo=False)
            on_done(True)
        elif answer == "keep":
            on_done(True)
        else:
            on_done(False)

    # -- commands ----------------------------------------------------------------------
    def save(self, *, on_done: Any = None) -> None:
        self.calls.append(("save",))

    def save_as(self, path: Any, *, on_done: Any = None) -> None:
        self.calls.append(("save_as", str(path)))

    def undo(self) -> None:
        self.calls.append(("undo",))

    def redo(self) -> None:
        self.calls.append(("redo",))

    def discard(self, parent: Any) -> None:
        self.calls.append(("discard", parent))

    def restore_pending(self, info: Any) -> None:
        self.calls.append(("restore_pending", info))

    def discard_pending(self, info: Any, parent: Any) -> None:
        self.calls.append(("discard_pending", info, parent))

    def export_pending(self, info: Any, path: Any) -> None:
        self.calls.append(("export_pending", info, str(path)))

    def roll_pending(self, info: Any) -> None:
        self.calls.append(("roll_pending", info))

    def ignore_pending(self, info: Any) -> None:
        self.calls.append(("ignore_pending", info))
        self.pending = [p for p in self.pending if p is not info]
        self.pending_found.emit(list(self.pending))

    def find(self, infos: list[Any]) -> None:
        """What the real controller does when it found layers: remember and emit them."""
        self.pending = list(infos)
        self.pending_found.emit(list(infos))

    def import_patch(self, path: Any) -> None:
        self.calls.append(("import_patch", str(path)))

    def export_patch(self, path: Any, *, diff: bool = True) -> None:
        self.calls.append(("export_patch", str(path), diff))

    def waiters(self) -> list[Any]:
        return []

    def detach(self, timeout: float = 2.0) -> None:
        self.detached = True
        self.calls.append(("detach",))


def attach_edit(session: Any, edit: Any) -> Any:
    """Make ``edit`` the session's controller the way the session does (state forwarded,
    ``edit_changed`` emitted)."""
    session.edit = edit
    forward = getattr(session, "_on_edit_state", None)
    if edit is not None and callable(forward):
        edit.state_changed.connect(forward)
    signal = getattr(session, "edit_changed", None)
    if signal is not None:
        signal.emit()
    return edit


def install_stub_edit(monkeypatch: Any, **state: Any) -> list[StubEditController]:
    """Make every DocSession get a :class:`StubEditController` (with ``state``) instead of
    the real controller when its index is ready; returns the list of the stubs made."""
    from jsonreshape.gui.session import DocSession

    made: list[StubEditController] = []

    def create(self: Any) -> None:
        stub = StubEditController(self, **state)
        self.edit = stub
        forward = getattr(self, "_on_edit_state", None)
        if callable(forward):
            stub.state_changed.connect(forward)
        made.append(stub)
        signal = getattr(self, "edit_changed", None)
        if signal is not None:
            signal.emit()

    monkeypatch.setattr(DocSession, "_create_edit", create)
    return made


def pending_info(tmp_path: Path, **kw: Any) -> Any:
    """A :class:`jsonreshape.editstore.PendingInfo` for recovery-bar tests."""
    from jsonreshape.editstore import PendingInfo

    now = time.time()
    data: dict[str, Any] = {"dir": tmp_path / "layer", "key": "k" * 32, "steps": 3, "created": now - 60,
                            "updated": now, "label": "Qiymat o'zgartirildi", "matches": True, "locked": False,
                            "damaged": None, "saving": None, "source": str(tmp_path / "a.json")}
    data.update(kw)
    return PendingInfo(**data)


# ----------------------------------------------------------------------------------------
# stub panels / dialogs
# ----------------------------------------------------------------------------------------
def _weak(session: Any) -> Any:
    """What the stub panels record for a session: a weak reference (a recorded call must
    not keep a closed tab's session alive -- the leak tests check that it is freed)."""
    return None if session is None else weakref.ref(session)


class StubSearchPanel(QWidget):
    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.calls: list[tuple] = []
        self.session = None

    def set_session(self, session: Any) -> None:
        self.session = session
        self.calls.append(("set_session", _weak(session)))

    def focus_search(self, text: str | None = None) -> None:
        self.calls.append(("focus_search", text))

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))


class StubFilterPanel(QWidget):
    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.calls: list[tuple] = []
        self.session = None

    def set_session(self, session: Any) -> None:
        self.session = session
        self.calls.append(("set_session", _weak(session)))

    def add_condition(self, field: str, op: str, value: Any) -> None:
        self.calls.append(("add_condition", field, op, value))

    def focus_expression(self) -> None:
        self.calls.append(("focus_expression",))

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))


class StubDialog(QDialog):
    made: list[StubDialog] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        parent = args[1] if len(args) > 1 else kwargs.get("parent")
        super().__init__(parent)
        self.args = args
        self.kwargs = kwargs
        StubDialog.made.append(self)


class StubBulkDialog(QDialog):
    """Stands in for gui.bulkdialog.BulkDialog(session, scope_default=..., parent=...)."""

    made: list[StubBulkDialog] = []

    def __init__(self, session: Any, scope_default: str = "all", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.scope_default = scope_default
        StubBulkDialog.made.append(self)


def stub_panels() -> dict[str, Any]:
    return {"search": StubSearchPanel, "filter": StubFilterPanel, "sort": StubDialog, "export": StubDialog,
            "settings": StubDialog, "bulk": StubBulkDialog}


# ----------------------------------------------------------------------------------------
# app / files / waiting
# ----------------------------------------------------------------------------------------
def make_app(tmp_path: Path) -> AppContext:
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(20)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    return ctx


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def records(n: int) -> list[dict]:
    return [{"id": i, "name": f"n{i % 7}", "contact": {"address": f"street {i}", "zip": 1000 + i}, "tags": [i, "x"]}
            for i in range(n)]


def wrapped_file(path: Path, n: int = 200) -> Path:
    return write_json(path, {"meta": {"version": 1, "tags": ["a", "b"]}, "data": records(n),
                             "other": [{"k": 1}, {"k": 2}, {"k": 3}]})


def jsonl_with_broken(path: Path, n_good: int = 20, broken: tuple[int, ...] = (3, 7, 12)) -> Path:
    lines = []
    for i in range(n_good):
        if i in broken:
            lines.append('{"id": %d, "bad": }' % i)
        else:
            lines.append(json.dumps({"id": i}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def wait_ready(qtbot: Any, session: Any, timeout: int = TIMEOUT) -> None:
    qtbot.waitUntil(lambda: session.state in ("ready", "failed"), timeout=timeout)


def wait_state(qtbot: Any, session: Any, state: str, timeout: int = TIMEOUT) -> None:
    qtbot.waitUntil(lambda: session.state == state, timeout=timeout)
