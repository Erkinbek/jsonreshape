"""Shared helpers of the application-shell GUI tests (test_gui_mainwindow / doctab /
navigation / statusbar / taskspanel / app): stub views and panels that record the calls
the shell makes, data file writers and wait helpers."""

from __future__ import annotations

import json
import weakref
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
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


def stub_registry() -> dict[str, type]:
    return {"tree": TreeStub, "table": TableStub, "text": TextStub, "raw": RawStub, "schema": SchemaStub}


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


def stub_panels() -> dict[str, Any]:
    return {"search": StubSearchPanel, "filter": StubFilterPanel, "sort": StubDialog, "export": StubDialog,
            "settings": StubDialog}


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
