"""Shared fixtures / helpers of the view editing tests (tests/test_gui_table_edit.py, test_gui_text_edit.py,
test_gui_views_edited.py): real :class:`~jsonreshape.gui.session.DocSession` objects whose
:class:`~jsonreshape.gui.editing.EditController` is available, waits for applied ChangeSets, a probe that records data
reads made in the GUI thread, small document builders.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession
from jsonreshape.reader import Document

TIMEOUT = 30000
GUI_READ_MAX = 64 * 1024


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.statuses = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def no_logged_errors(caplog):
    """No ERROR log record of the GUI package (swallowed exceptions are logged)."""
    caplog.set_level(logging.WARNING)
    yield
    records = caplog.get_records("setup") + caplog.get_records("call")
    bad = [r for r in records if r.name.startswith("jsonreshape") and r.levelno >= logging.ERROR]
    assert not bad, [r.getMessage() for r in bad]


@pytest.fixture
def opened(qtbot, app):
    """``opened(path, **kw) -> DocSession`` with an available edit controller."""
    made: list[DocSession] = []

    def open_file(path: Path, **kw: Any) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        assert s.edit is not None
        qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)
    assert not app.errors, app.errors


@pytest.fixture
def views(qtbot):
    """``views(cls, session, **kw)`` -> a shown, activated view of ``cls``; shut down at the end."""
    made: list[Any] = []

    def make(cls: Any, session: DocSession, *, width: int = 1300, height: int = 700) -> Any:
        view = cls(session)
        qtbot.addWidget(view)
        view.resize(width, height)
        view.show()
        qtbot.waitExposed(view)
        view.activate()
        made.append(view)
        return view

    yield make
    for v in made:
        v.shutdown()


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def write_json(path: Path, value: Any, *, indent: int | None = 2) -> Path:
    return write(path, json.dumps(value, ensure_ascii=False, indent=indent))


def records(s: DocSession) -> list:
    """The parsed records of the state the session shows (exact numbers)."""
    v = s.doc.pinned()
    return [v.record(i) for i in range(v.record_count)]


def record_text(s: DocSession, pos: int) -> str:
    return s.doc.pinned().record_bytes(pos).decode("utf-8")


@contextlib.contextmanager
def applied(qtbot: Any, s: DocSession, *, count: int = 1, timeout: int = TIMEOUT) -> Iterator[list]:
    """Wait for ``count`` ChangeSets applied to ``s`` (``data_changed``) after the block; yields them."""
    got: list = []
    s.data_changed.connect(got.append)
    try:
        yield got
        qtbot.waitUntil(lambda: len(got) >= count, timeout=timeout)
    finally:
        try:
            s.data_changed.disconnect(got.append)
        except (RuntimeError, TypeError):
            pass
    qtbot.waitUntil(lambda: not s.edit.busy, timeout=timeout)


@contextlib.contextmanager
def keep_selection(s: DocSession) -> Iterator[None]:
    """While the block runs, the session does not select the ChangeSets' nodes (``cs.select``): a change made
    elsewhere (a bulk operation, another view whose own node is current) leaves the views on their nodes."""
    real = s.select

    def select(ref: Any, *, origin: Any = None, history: bool = True, reveal: bool = False) -> None:
        if origin is not None or reveal:
            real(ref, origin=origin, history=history, reveal=reveal)

    s.select = select                 # type: ignore[method-assign]
    try:
        yield
    finally:
        del s.select


def run(qtbot: Any, fn: Any, *args: Any, **kw: Any) -> Any:
    """Call a controller command and wait for its ``on_done``; returns what it got."""
    got: list = []
    ok = fn(*args, on_done=got.append, **kw)
    assert ok is not False, "the command was refused"
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


class GuiReads:
    """Records the bytes read from a document in the GUI (main) thread: ``Document._read``, ``node_chunks``
    (``open_data`` readers read in worker threads only); :meth:`big` lists main-thread reads above the limit."""

    def __init__(self, monkeypatch: Any) -> None:
        self.main = threading.main_thread()
        self.reads: list[tuple[str, int]] = []
        real_read = Document._read
        real_chunks = Document.node_chunks
        real_open = Document.open_data
        probe = self

        def _read(doc: Any, start: int, end: int, *a: Any, **k: Any) -> bytes:
            data = real_read(doc, start, end, *a, **k)
            if threading.current_thread() is probe.main:
                probe.reads.append(("read", len(data)))
            return data

        def node_chunks(doc: Any, node: Any, **k: Any) -> Any:
            it = real_chunks(doc, node, **k)
            if threading.current_thread() is not probe.main:
                return it

            def gen() -> Iterator[bytes]:
                total = 0
                for piece in it:
                    total += len(piece)
                    yield piece
                probe.reads.append(("node_chunks", total))
            return gen()

        def open_data(doc: Any, *a: Any, **k: Any) -> Any:
            if threading.current_thread() is probe.main:
                probe.reads.append(("open_data", 0))
            return real_open(doc, *a, **k)

        monkeypatch.setattr(Document, "_read", _read)
        monkeypatch.setattr(Document, "node_chunks", node_chunks)
        monkeypatch.setattr(Document, "open_data", open_data)

    def big(self, limit: int = GUI_READ_MAX) -> list[tuple[str, int]]:
        return [r for r in self.reads if r[1] > limit or r[0] == "open_data"]
