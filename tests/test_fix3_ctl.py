"""Fix round 3, part 2 -- the edit controller and the session (jsonreshape/gui/editing.py, session.py, qtasks.py;
review3 CL-3, CL-4, CL-5, CL-7, CL-8, CL-11, CL-12, S1 (GUI half), S3, S6 (GUI), L8, request (a) and the gzip
question of S9).

Real sessions and controllers (offscreen, headless app); questions are answered through the controller's test seams
(``editing.ask_choice_handler``, ``editing.ask_save_handler``, ``widgets.confirm_handler``). Every test waits on
conditions or events -- the slow parts are held on ``threading.Event`` gates, never on sleeps.
"""

from __future__ import annotations

import dataclasses
import gc
import gzip
import json
import threading
import time
import weakref
from pathlib import Path

import pytest

from jsonreshape import edits, fileio, saver, writer
from jsonreshape.editstore import LayerLock, file_lock_key, find_pending
from jsonreshape.errors import EditError, FileChangedError, UserInputError
from jsonreshape.gui import editing, widgets
from jsonreshape.gui import session as session_mod
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.reader import Document

pytestmark = pytest.mark.gui

TIMEOUT = 30000


# ----------------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------------
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
def make(app):
    made: list[DocSession] = []

    def factory(path, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)
    assert not app.errors, app.errors


@pytest.fixture
def yes(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda _p, text, _t: asked.append(text) or True)
    return asked


@pytest.fixture
def choices(monkeypatch):
    """``editing.ask_choice_handler`` answering from ``choices.answers`` (purpose -> choice); every question is
    recorded as ``(purpose, name, choices)``."""

    class Answers:
        def __init__(self) -> None:
            self.answers: dict[str, str] = {}
            self.asked: list[tuple[str, str, tuple]] = []

    got = Answers()

    def handler(_parent, purpose, name, options):
        got.asked.append((purpose, name, tuple(options)))
        return got.answers.get(purpose, "cancel")

    monkeypatch.setattr(editing, "ask_choice_handler", handler)
    return got


def write_recs(path: Path, n: int = 8, *, indent: int | None = None) -> Path:
    path.write_text(json.dumps([{"id": i, "name": f"n{i}", "g": i % 2} for i in range(n)], indent=indent),
                    encoding="utf-8")
    return path


def open_editable(qtbot, make, path, **kw) -> DocSession:
    s = make(path, **kw)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    assert s.edit is not None
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    return s


def run(qtbot, fn, *args, timeout: int = TIMEOUT, **kw):
    """A controller command; waits for its ``on_done`` and returns what it got."""
    got: list = []
    ok = fn(*args, on_done=got.append, **kw)
    assert ok is not False, "the command was refused"
    qtbot.waitUntil(lambda: bool(got), timeout=timeout)
    return got[0]


def values(s: DocSession) -> list:
    v = s.doc.pinned()
    return [v.record(i) for i in range(v.record_count)]


def wait_idle(qtbot, s: DocSession) -> None:
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available and not s.edit.busy, timeout=TIMEOUT)


class Gate:
    """A reader task kept by ``keep_document_for`` (like an export) that waits on an event, then checks the
    document the way exports do (``ensure_unchanged``) and reads a record."""

    def __init__(self, app, s: DocSession, *, kind: str = "export", title: str = "Eksport: chiqish.json") -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.outcome: list = []
        pinned, _view = s.snapshot()

        def work(ctx):
            self.entered.set()
            while not self.release.wait(0.02):
                ctx.check()
            pinned.ensure_unchanged()          # what writer.export / engine.apply do before they commit
            return pinned.record(1)

        self.task = app.tasks.run(work, title=title, kind=kind, owner=object(), heavy=True,
                                  on_done=lambda r: self.outcome.append(("done", r)),
                                  on_error=lambda exc: self.outcome.append(("failed", exc)),
                                  on_cancel=lambda: self.outcome.append(("cancelled", None)))
        s.keep_document_for(self.task, doc=pinned)


# ----------------------------------------------------------------------------------------
# CL-5: an exception in the GUI-thread apply path
# ----------------------------------------------------------------------------------------
def test_a_failing_remap_never_leaves_the_controller_busy(qtbot, make, app, tmp_path, monkeypatch, yes) -> None:
    """The skeptic's injection: a remap helper of the session raises while a step is followed. The session follows
    the step like a reset (instead of stopping half-way), the job ends, the error is reported -- and the controller
    goes on: commands, Discard and closing work."""
    s = open_editable(qtbot, make, write_recs(tmp_path / "k.json"))
    ctl = s.edit
    s.select(NodeRef(3, ("name",)))
    real = DocSession._remap_positions
    calls: list = []

    def boom(self, fn):
        if not calls:
            calls.append(1)
            raise RuntimeError("injected")
        return real(self, fn)

    monkeypatch.setattr(DocSession, "_remap_positions", boom)
    seen: list = []
    s.data_changed.connect(seen.append)
    current: list = []
    s.current_changed.connect(lambda ref, _origin: current.append(ref))
    cs = run(qtbot, ctl.delete, [NodeRef(0, ())])
    assert calls == [1]
    assert cs is not None and not ctl.busy and ctl.available
    assert seen and seen[-1].reset and seen[-1].gen == cs.gen        # the views got a reset: they reload
    assert s.current is None and s.history() == [] and current == [None]
    assert s.data_gen == s.doc.gen == ctl.applied_gen == cs.gen
    assert [type(e).__name__ for e in app.errors] == ["RuntimeError"]      # reported once
    app.errors.clear()
    assert [r["id"] for r in values(s)] == [1, 2, 3, 4, 5, 6, 7]
    # the controller goes on
    cs2 = run(qtbot, ctl.set_value, NodeRef(0, ("name",)), '"after"')
    assert cs2 is not None and values(s)[0]["name"] == "after"
    assert run(qtbot, ctl.discard, None) is not None
    assert not s.dirty and len(values(s)) == 8


def test_an_exception_escaping_the_apply_path_is_recovered(qtbot, make, app, tmp_path, monkeypatch) -> None:
    """``_apply_change`` itself raises (any bug on the way): the job still ends (``on_done(None)``), the error is
    reported and the session shows the edit session's state through a reset -- the controller is not left busy."""
    s = open_editable(qtbot, make, write_recs(tmp_path / "e.json"))
    ctl = s.edit
    real = DocSession._apply_change
    calls: list = []

    def boom(self, cs, remapped=None):
        if not calls:
            calls.append(cs)
            raise RuntimeError("injected")
        return real(self, cs, remapped)

    monkeypatch.setattr(DocSession, "_apply_change", boom)
    got: list = []
    assert ctl.set_value(NodeRef(1, ("name",)), '"x"', on_done=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == [None]
    assert not ctl.busy and ctl.available
    assert s.data_gen == s.doc.gen == ctl.applied_gen == calls[0].gen
    assert values(s)[1]["name"] == "x" and s.dirty
    assert [type(e).__name__ for e in app.errors] == ["RuntimeError"]
    app.errors.clear()
    cs = run(qtbot, ctl.undo)
    assert cs is not None and values(s)[1]["name"] == "n1" and not s.dirty


def test_an_exception_applying_a_task_result_ends_the_task(qtbot, make, app, tmp_path, monkeypatch) -> None:
    """The task path (records tasks, patch import): the result's apply raises -> the task still ends."""
    s = open_editable(qtbot, make, write_recs(tmp_path / "t.json"))
    ctl = s.edit
    real = editing.EditController._apply
    calls: list = []

    def boom(self, cs, remapped, session):
        if not calls:
            calls.append(cs)
            raise RuntimeError("injected")
        return real(self, cs, remapped, session)

    monkeypatch.setattr(editing.EditController, "_apply", boom)
    got: list = []
    assert ctl.delete_records([1, 2], on_done=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == [None] and not ctl.busy and ctl.available
    assert s.data_gen == ctl.applied_gen == calls[0].gen
    assert [r["id"] for r in values(s)] == [0, 3, 4, 5, 6, 7]
    assert [type(e).__name__ for e in app.errors] == ["RuntimeError"]
    app.errors.clear()


# ----------------------------------------------------------------------------------------
# request (a): the step's selection without a current_changed(None) right before it
# ----------------------------------------------------------------------------------------
def test_the_steps_selection_is_the_only_current_change(qtbot, make, app, tmp_path, monkeypatch) -> None:
    s = open_editable(qtbot, make, write_recs(tmp_path / "a.json"))
    s.select(NodeRef(3, ()))
    current: list = []
    order: list = []
    s.current_changed.connect(lambda ref, _origin: (current.append(ref), order.append("current")))
    s.data_changed.connect(lambda _cs: order.append("data"))
    cs = run(qtbot, s.edit.delete, [NodeRef(3, ())])
    assert cs is not None and cs.select is not None
    assert None not in current and len(current) == 1           # no None right before the step's selection
    assert current[0] == s.current
    assert order == ["data", "current"]                         # selected AFTER data_changed (the views' rule)
    # a step that selects nothing still tells that the current node was deleted
    real = DocSession._apply_change
    monkeypatch.setattr(DocSession, "_apply_change",
                        lambda self, cs, remapped=None: real(self, dataclasses.replace(cs, select=None), remapped))
    s.select(NodeRef(0, ("name",)))
    current.clear()
    cs = run(qtbot, s.edit.delete_records, [0])
    assert cs is not None and s.current is None and current == [None]


# ----------------------------------------------------------------------------------------
# L8: expected refusals on the status bar
# ----------------------------------------------------------------------------------------
def test_user_mistakes_and_real_failures() -> None:
    assert editing.user_mistake(EditError("edits.error.key_exists", key="a"))
    assert editing.user_mistake(EditError("edits.error.invalid_json", line=1, column=2, reason="x"))
    assert editing.user_mistake(EditError("edits.error.convert", source="object", target="number"))
    assert editing.user_mistake(UserInputError("gui.edit.pending.gone"))
    for exc in (EditError("edits.error.internal"), EditError("edits.error.layer_corrupt"),
                EditError("editstore.error.disk_full"), EditError("patch.error.json"),
                FileChangedError(path="x"), RuntimeError("bug"), OSError("disk")):
        assert not editing.user_mistake(exc), exc


def test_a_command_refusal_goes_to_the_status_bar(qtbot, make, app, tmp_path) -> None:
    s = open_editable(qtbot, make, write_recs(tmp_path / "m.json"))
    app.statuses.clear()
    assert run(qtbot, s.edit.change_type, [NodeRef(1, ())], "number") is None      # an object cannot be a number
    assert app.errors == [] and app.statuses
    prefix = editing.t("gui.edit.refused", reason="\x00").split("\x00")[0]
    assert prefix and app.statuses[-1].startswith(prefix) and len(app.statuses[-1]) > len(prefix)
    assert not s.edit.busy and s.edit.available and not s.dirty


# ----------------------------------------------------------------------------------------
# CL-7: a running bulk preview and Save / Discard
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["discard", "save"])
def test_save_or_discard_cancel_a_running_preview(qtbot, make, app, tmp_path, monkeypatch, yes, action) -> None:
    from jsonreshape import bulk
    from jsonreshape.bulk import BulkSpec

    gate = threading.Event()
    entered = threading.Event()
    real_block = bulk._Pass.block

    def held(self, blk):
        entered.set()
        assert gate.wait(30)
        return real_block(self, blk)

    monkeypatch.setattr(bulk._Pass, "block", held)
    s = open_editable(qtbot, make, write_recs(tmp_path / "p.json"))
    ctl = s.edit
    assert run(qtbot, ctl.set_value, NodeRef(1, ("name",)), '"edited"') is not None
    done: list = []
    failed: list = []
    task = ctl.bulk_preview(BulkSpec("replace", {"find": "n", "replacement": "X", "mode": "text"}), None,
                            done.append, on_failed=failed.append)
    assert task is not None and entered.wait(10)
    try:
        if action == "discard":
            assert run(qtbot, ctl.discard, None) is not None
        else:
            saved: list = []
            ctl.save(on_done=saved.append)
            qtbot.waitUntil(lambda: bool(saved), timeout=TIMEOUT)
            assert saved[0] is not None
        assert task.cancel_requested            # cancelled when the discard / save started
    finally:
        gate.set()
    qtbot.waitUntil(lambda: not task.thread_alive, timeout=TIMEOUT)
    assert app.tasks.wait_idle(30)
    assert app.errors == [] and done == []
    if action == "discard":
        assert failed == [None]                 # told as cancelled, not as an error


def test_a_preview_failing_on_a_closed_layer_counts_as_cancelled(qtbot, make, app, tmp_path, monkeypatch) -> None:
    """Even when the preview was not cancelled in time: ``editstore.error.closed`` / ``.discarded`` void it."""
    from jsonreshape.bulk import BulkSpec

    s = open_editable(qtbot, make, write_recs(tmp_path / "q.json"))
    for key in ("editstore.error.closed", "editstore.error.discarded"):
        def broken(self, *_a, _key=key, **_kw):
            raise EditError(_key)

        monkeypatch.setattr(edits.EditSession, "bulk_preview", broken)
        failed: list = []
        task = s.edit.bulk_preview(BulkSpec("replace", {"find": "n", "replacement": "X", "mode": "text"}), None,
                                   lambda _p: None, on_failed=failed.append)
        assert task is not None
        qtbot.waitUntil(lambda: bool(failed), timeout=TIMEOUT)
        assert failed == [None] and app.errors == []


# ----------------------------------------------------------------------------------------
# CL-8: the close thread holds no task QObject
# ----------------------------------------------------------------------------------------
def test_the_close_thread_never_finalizes_tasks_or_the_controller(qtbot, make, app, tmp_path) -> None:
    """A controller task still runs when the session closes and an export-like task keeps the close thread waiting:
    the GuiTask and the EditController (its owner, with a QTimer child) are released in the GUI thread -- never in
    ``jsonreshape-gui-close``."""
    s = open_editable(qtbot, make, write_recs(tmp_path / "o.json"))
    ctl = s.edit
    in_gui: dict[str, list[bool]] = {"ctl": [], "task": []}
    weakref.finalize(ctl, lambda: in_gui["ctl"].append(threading.current_thread() is threading.main_thread()))
    gate = threading.Event()
    long_task = app.tasks.run(lambda ctx: gate.wait(30), title="long", kind="export", owner=object())
    s.keep_document_for(long_task)
    started = threading.Event()
    stop = threading.Event()

    def short_work(ctx):
        started.set()
        assert stop.wait(30)          # still running when the session samples the tasks to wait for
        return 2

    short = app.tasks.run(short_work, title="short", kind="bulk", owner=ctl)
    weakref.finalize(short, lambda: in_gui["task"].append(threading.current_thread() is threading.main_thread()))
    assert started.wait(10)
    s.close()
    stop.set()
    qtbot.waitUntil(lambda: short.state != "running", timeout=TIMEOUT)      # ended and delivered (GUI thread)
    assert short.owner is None                                               # a finished task forgets its owner
    del ctl, short
    gc.collect()
    qtbot.waitUntil(lambda: bool(in_gui["ctl"]) and bool(in_gui["task"]), timeout=TIMEOUT)
    assert in_gui == {"ctl": [True], "task": [True]}
    gate.set()
    qtbot.waitUntil(lambda: long_task.state != "running", timeout=TIMEOUT)


def test_close_after_waits_on_plain_objects_only() -> None:
    """``_end_waiter`` gives the close thread the event / thread a task ends with, never the task."""
    lease = session_mod._Lease()
    wait, limit, _label = session_mod._end_waiter(lease)
    assert getattr(wait, "__self__", None) is lease.done_event and limit == 120.0
    thread = threading.Thread(target=lambda: None)
    thread.start()

    class Worker:
        kind = "edit"
        thread_alive = True

        def __init__(self) -> None:
            self.thread = thread

        def join(self, timeout=None):     # pragma: no cover - must not be used
            raise AssertionError("the close thread must not call the task")

    wait, limit, _label = session_mod._end_waiter(Worker())
    assert wait(5) is True and limit == 120.0


# ----------------------------------------------------------------------------------------
# CL-11: released leases leave the held sets
# ----------------------------------------------------------------------------------------
def test_released_leases_leave_the_held_sets(qtbot, make, tmp_path) -> None:
    s = open_editable(qtbot, make, write_recs(tmp_path / "g.json", 40))
    s.set_filter(FilterSpec("g == 1"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 20, timeout=TIMEOUT)
    s.set_sort([])
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    for i in range(30):
        wait_idle(qtbot, s)
        assert run(qtbot, s.edit.set_value, NodeRef(1, ("name",)), f'"v{i}"') is not None
    assert s._held == []                 # one entry per view set per edit was left behind before


# ----------------------------------------------------------------------------------------
# CL-12 (GUI half): closing interrupts a command that waits for the record-key index
# ----------------------------------------------------------------------------------------
def test_closing_interrupts_a_command_waiting_for_the_key_index(qtbot, make, tmp_path, monkeypatch) -> None:
    path = tmp_path / "keyed.json"
    path.write_text(json.dumps({f"k{i}": {"id": i, "name": f"n{i}"} for i in range(20)}), encoding="utf-8")
    gate = threading.Event()
    entered = threading.Event()
    real = edits.KeyIndex._build

    def held(self, *a, **kw):
        entered.set()
        gate.wait(30)                    # the snapshot of a huge keyed file takes long
        return real(self, *a, **kw)

    monkeypatch.setattr(edits.KeyIndex, "_build", held)
    try:
        s = open_editable(qtbot, make, path, format="keyed")
        ctl = s.edit
        got: list = []
        assert ctl.rename_key(NodeRef(3, ()), "k3_new", on_done=got.append)    # a record key: needs the index
        assert entered.wait(10)
        s.close()
        qtbot.waitUntil(lambda: not ctl._worker.thread_alive, timeout=10000)   # not when the index is built
        assert got == []
    finally:
        gate.set()


# ----------------------------------------------------------------------------------------
# CL-3: an in-place save while tasks read the file
# ----------------------------------------------------------------------------------------
def test_in_place_save_waits_for_a_running_export(qtbot, make, app, tmp_path, choices) -> None:
    path = write_recs(tmp_path / "c.json")
    s = open_editable(qtbot, make, path)
    ctl = s.edit
    assert run(qtbot, ctl.set_value, NodeRef(1, ("name",)), '"edited"') is not None
    reader = Gate(app, s)
    assert reader.entered.wait(10)
    choices.answers["readers"] = "wait"
    saved: list = []
    ctl.save(on_done=saved.append)
    qtbot.waitUntil(lambda: bool(choices.asked), timeout=TIMEOUT)
    purpose, name, options = choices.asked[-1]
    assert (purpose, name, options) == ("readers", "c.json", ("wait", "stop", "cancel"))
    qtbot.waitUntil(lambda: ctl.busy and ctl.reason == "saving", timeout=TIMEOUT)
    assert needs_answer(ctl)
    original = path.read_bytes()
    assert b"edited" not in original and saved == []           # nothing replaced while the export reads
    reader.release.set()
    qtbot.waitUntil(lambda: bool(saved), timeout=TIMEOUT)
    assert saved[0] is not None
    assert reader.outcome and reader.outcome[0][0] == "done" and reader.outcome[0][1]["name"] == "edited"
    assert json.loads(path.read_text(encoding="utf-8"))[1]["name"] == "edited"
    assert app.errors == []


def needs_answer(ctl) -> bool:
    return bool(ctl.needs_close_answer)


def test_in_place_save_can_stop_the_readers_or_be_cancelled(qtbot, make, app, tmp_path, choices) -> None:
    path = write_recs(tmp_path / "d.json")
    s = open_editable(qtbot, make, path)
    ctl = s.edit
    assert run(qtbot, ctl.set_value, NodeRef(1, ("name",)), '"edited"') is not None
    reader = Gate(app, s)
    assert reader.entered.wait(10)
    original = path.read_bytes()
    # cancel: no save, the reader goes on
    choices.answers["readers"] = "cancel"
    saved: list = []
    ctl.save(on_done=saved.append)
    qtbot.waitUntil(lambda: bool(saved), timeout=TIMEOUT)
    assert saved == [None] and path.read_bytes() == original and s.dirty and reader.task.state == "running"
    # stop: the reader is cancelled (no error), the save goes on at once
    choices.answers["readers"] = "stop"
    saved.clear()
    ctl.save(on_done=saved.append)
    qtbot.waitUntil(lambda: bool(saved), timeout=TIMEOUT)
    assert saved[0] is not None and json.loads(path.read_text(encoding="utf-8"))[1]["name"] == "edited"
    qtbot.waitUntil(lambda: bool(reader.outcome), timeout=TIMEOUT)
    assert reader.outcome[0][0] == "cancelled" and app.errors == []


def test_a_save_waiting_for_readers_can_be_cancelled(qtbot, make, app, tmp_path, choices) -> None:
    path = write_recs(tmp_path / "w.json")
    s = open_editable(qtbot, make, path)
    ctl = s.edit
    assert run(qtbot, ctl.set_value, NodeRef(2, ("name",)), '"x"') is not None
    reader = Gate(app, s)
    assert reader.entered.wait(10)
    choices.answers["readers"] = "wait"
    saved: list = []
    ctl.save(on_done=saved.append)
    qtbot.waitUntil(lambda: ctl.busy, timeout=TIMEOUT)
    waiting = ctl._busy_task
    assert waiting is not None and waiting.kind == "save" and not waiting.heavy     # never holds a heavy slot
    waiting.cancel()
    qtbot.waitUntil(lambda: bool(saved), timeout=TIMEOUT)
    assert saved == [None] and not ctl.busy and s.dirty and b'"x"' not in path.read_bytes()
    reader.release.set()
    qtbot.waitUntil(lambda: bool(reader.outcome), timeout=TIMEOUT)
    assert reader.outcome[0][0] == "done" and app.errors == []


def test_save_as_does_not_wait_for_readers(qtbot, make, app, tmp_path, choices) -> None:
    """A Save As never touches the file the readers read: no question."""
    s = open_editable(qtbot, make, write_recs(tmp_path / "v.json"))
    assert run(qtbot, s.edit.set_value, NodeRef(1, ("name",)), '"y"') is not None
    reader = Gate(app, s)
    assert reader.entered.wait(10)
    target = tmp_path / "v_copy.json"
    saved: list = []
    s.edit.save_as(target, on_done=saved.append)
    qtbot.waitUntil(lambda: bool(saved), timeout=TIMEOUT)
    assert saved[0] is not None and choices.asked == []
    assert json.loads(target.read_text(encoding="utf-8"))[1]["name"] == "y"
    reader.release.set()
    qtbot.waitUntil(lambda: bool(reader.outcome), timeout=TIMEOUT)
    assert reader.outcome[0][0] == "done" and app.errors == []


# ----------------------------------------------------------------------------------------
# CL-4: closing while a long operation of a clean document runs
# ----------------------------------------------------------------------------------------
def held_save(monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    real = saver.save

    def held(*a, **kw):
        entered.set()
        assert gate.wait(30)
        return real(*a, **kw)

    monkeypatch.setattr(saver, "save", held)
    return gate, entered


@pytest.mark.parametrize("answer", ["wait", "stop", "cancel"])
def test_ask_close_during_save_as_of_a_clean_document(qtbot, make, app, tmp_path, monkeypatch, choices,
                                                      answer) -> None:
    gate, entered = held_save(monkeypatch)
    s = open_editable(qtbot, make, write_recs(tmp_path / "h.json"))
    ctl = s.edit
    target = tmp_path / "h_copy.json"
    reports: list = []
    ctl.save_as(target, on_done=reports.append)
    try:
        assert entered.wait(10)
        assert not ctl.dirty and ctl.busy and ctl.needs_close_answer
        choices.answers["busy"] = answer
        closing: list = []
        ctl.ask_close(None, closing.append, why="close")
        qtbot.waitUntil(lambda: bool(choices.asked), timeout=TIMEOUT)
        assert choices.asked[-1] == ("busy", "h.json", ("wait", "stop", "cancel"))
        if answer == "cancel":
            qtbot.waitUntil(lambda: bool(closing), timeout=TIMEOUT)
            assert closing == [False] and ctl.busy          # the Save As goes on
        elif answer == "wait":
            qtbot.wait(50)
            assert closing == []                            # answered once the Save As ended
    finally:
        gate.set()
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    if answer == "stop":
        assert reports == [None] and not target.exists()
    else:
        assert reports[0] is not None and target.exists()
    if answer != "cancel":
        qtbot.waitUntil(lambda: bool(closing), timeout=TIMEOUT)
        assert closing == [True]
    assert app.errors == []


def test_ask_close_while_saving_a_dirty_document_asks_too(qtbot, make, app, tmp_path, monkeypatch,
                                                          choices) -> None:
    gate, entered = held_save(monkeypatch)
    s = open_editable(qtbot, make, write_recs(tmp_path / "s.json"))
    ctl = s.edit
    assert run(qtbot, ctl.set_value, NodeRef(1, ("name",)), '"saved"') is not None
    saved: list = []
    ctl.save(on_done=saved.append)
    try:
        assert entered.wait(10)
        choices.answers["busy"] = "wait"
        closing: list = []
        ctl.ask_close(None, closing.append, why="quit")
        qtbot.waitUntil(lambda: bool(choices.asked), timeout=TIMEOUT)
        assert choices.asked[-1][0] == "busy"
    finally:
        gate.set()
    qtbot.waitUntil(lambda: bool(closing), timeout=TIMEOUT)
    assert saved and saved[0] is not None and closing == [True]    # waited for the save: nothing left to ask


def test_headless_close_during_a_save_still_refuses(qtbot, make, app, tmp_path, monkeypatch) -> None:
    """Nobody can answer (headless, no handler): a running save refuses the close as before; another long
    operation of a clean document goes on closing."""
    assert editing.ask_choice_handler is None and app.headless
    gate, entered = held_save(monkeypatch)
    s = open_editable(qtbot, make, write_recs(tmp_path / "n.json"))
    ctl = s.edit
    reports: list = []
    ctl.save_as(tmp_path / "n2.json", on_done=reports.append)
    try:
        assert entered.wait(10)
        closing: list = []
        ctl.ask_close(None, closing.append)
        assert closing == [False] and app.statuses[-1] == editing.t("gui.edit.save_wait")
    finally:
        gate.set()
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)


def test_detach_answers_the_questions_waiting_for_idle(qtbot, make, app, tmp_path, monkeypatch, choices) -> None:
    """A close question waiting ("wait") for a save that reopens the document (Ctrl+S) is answered once the
    controller is detached by that reopen -- a close flow never hangs."""
    gate, entered = held_save(monkeypatch)
    s = open_editable(qtbot, make, write_recs(tmp_path / "r.json"))
    ctl = s.edit
    assert run(qtbot, ctl.set_value, NodeRef(1, ("name",)), '"z"') is not None
    ctl.save()                                   # reopens the session when done
    try:
        assert entered.wait(10)
        choices.answers["busy"] = "wait"
        closing: list = []
        ctl.ask_close(None, closing.append)
        qtbot.waitUntil(lambda: bool(choices.asked), timeout=TIMEOUT)
    finally:
        gate.set()
    qtbot.waitUntil(lambda: bool(closing), timeout=TIMEOUT)
    assert closing == [True] and ctl.closed


# ----------------------------------------------------------------------------------------
# S1 (GUI half): a roll that claims "completed" while the file is still the old version
# ----------------------------------------------------------------------------------------
def journal_back(path: Path) -> dict:
    """A save journal whose temp file holds a NEW version that never replaced ``path``."""
    temp = path.parent / f".{path.name}.11111111.jrsave"
    temp.write_text(json.dumps([{"id": "saved"}]), encoding="utf-8")
    old = fileio.file_identity(path)
    new = fileio.file_identity(temp)
    return {"v": 1, "op": "save", "source": str(path), "temp": str(temp), "backup": None, "had_backup": False,
            "old_backup": None, "aside": str(path.parent / f".{path.name}.22222222.old"),
            "old_identity": old.to_dict(), "new": {"size": new.size, "hashes": list(new.hashes)},
            "time": time.time()}


def test_a_roll_claiming_completed_never_deletes_a_matching_layer(qtbot, make, app, tmp_path, monkeypatch) -> None:
    path = write_recs(tmp_path / "j.json", 5, indent=1)
    doc = Document.open(path)
    doc.build_index()
    es = edits.EditSession.open(doc)
    es.set_value(edits.Ref(1, ("name",)), '"jurnal"')
    es.store.journal(journal_back(path))
    es.close()                     # the layer stays: one step + the journal
    doc.close()
    original = path.read_bytes()
    s = make(path)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    info = s.edit.pending[0]
    assert info.saving and info.matches

    def wrong(journal, *, store=None):      # the saver's mistake the GUI must survive (review S1)
        if store is not None:
            store.journal(None)
        return saver.RollResult("the save had finished", "completed")

    monkeypatch.setattr(saver, "roll_journal", wrong)
    result = run(qtbot, s.edit.roll_pending, info)
    assert getattr(result, "action", None) == "kept"
    assert [getattr(e, "key", "") for e in app.errors] == ["gui.edit.pending.roll_unconfirmed"]
    app.errors.clear()
    assert path.read_bytes() == original
    qtbot.waitUntil(lambda: bool(s.edit.pending), timeout=TIMEOUT)
    kept = s.edit.pending[0]
    assert kept.matches and kept.steps == 1 and not kept.saving       # the layer is offered for Restore
    cs = run(qtbot, s.edit.restore_pending, kept)
    assert cs is not None and values(s)[1]["name"] == "jurnal"


# ----------------------------------------------------------------------------------------
# S3: Save As onto a file another tab shows
# ----------------------------------------------------------------------------------------
def test_save_as_onto_a_tab_with_unsaved_changes_is_refused(qtbot, make, app, tmp_path, yes) -> None:
    a = open_editable(qtbot, make, write_recs(tmp_path / "a.json"))
    b_path = write_recs(tmp_path / "b.json", 6)
    b = open_editable(qtbot, make, b_path)
    assert run(qtbot, a.edit.set_value, NodeRef(0, ("name",)), '"A"') is not None
    assert run(qtbot, b.edit.set_value, NodeRef(1, ("name",)), '"B unsaved"') is not None
    b_original = b_path.read_bytes()
    reports: list = []
    a.edit.save_as(b_path, on_done=reports.append)
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    assert reports == [None] and b_path.read_bytes() == b_original
    assert [getattr(e, "key", "") for e in app.errors] == ["gui.edit.target.unsaved"]
    app.errors.clear()
    assert b.dirty and b.edit.reason == "" and a.dirty and yes == []


def test_save_as_onto_an_unchanged_tab_asks_and_reloads_it(qtbot, make, app, tmp_path, monkeypatch) -> None:
    a = open_editable(qtbot, make, write_recs(tmp_path / "a.json"))
    b_path = write_recs(tmp_path / "b.json", 6)
    b = open_editable(qtbot, make, b_path)
    assert run(qtbot, a.edit.set_value, NodeRef(0, ("name",)), '"A"') is not None
    answers = [False, True]
    asked: list[str] = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda _p, text, _t: asked.append(text) or answers.pop(0))
    b_original = b_path.read_bytes()
    reports: list = []
    a.edit.save_as(b_path, on_done=reports.append)          # "no": nothing happens
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    assert reports == [None] and b_path.read_bytes() == b_original
    assert asked == [editing.t("gui.edit.target.open", path=str(b_path))]
    reports.clear()
    old_doc = b.doc
    a.edit.save_as(b_path, on_done=reports.append)          # "yes": saved, and the other tab opens the new file
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    assert reports[0] is not None
    qtbot.waitUntil(lambda: b.doc is not None and b.doc is not old_doc and b.state == "ready", timeout=TIMEOUT)
    assert len(values(b)) == 8 and values(b)[0]["name"] == "A"
    assert app.errors == []


def test_save_as_onto_a_file_another_program_edits_is_refused(qtbot, make, app, tmp_path) -> None:
    a = open_editable(qtbot, make, write_recs(tmp_path / "a.json"))
    c_path = write_recs(tmp_path / "c.json", 3)
    c_original = c_path.read_bytes()
    lock = LayerLock(file_lock_key(c_path))           # another window's edit session holds the file's lock
    assert lock.try_acquire()
    try:
        reports: list = []
        a.edit.save_as(c_path, on_done=reports.append)
        qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
        assert reports == [None] and c_path.read_bytes() == c_original
        assert [getattr(e, "key", "") for e in app.errors] == ["gui.edit.target.locked"]
        app.errors.clear()
    finally:
        lock.release()


def test_target_conflict() -> None:
    assert editing.target_conflict("Z:/no/such/file.json") is None


def test_export_onto_a_tab_with_unsaved_changes_is_refused(qtbot, make, app, tmp_path, yes) -> None:
    import os

    from jsonreshape.gui.exportdialog import ExportDialog

    a = open_editable(qtbot, make, write_recs(tmp_path / "a.json"))
    b_path = write_recs(tmp_path / "b.json", 6)
    b = open_editable(qtbot, make, b_path)
    assert run(qtbot, b.edit.set_value, NodeRef(1, ("name",)), '"B unsaved"') is not None
    original = b_path.read_bytes()
    dlg = ExportDialog(a, None, scope="all")
    qtbot.addWidget(dlg)
    dlg.set_output_path(b_path)
    assert dlg.start_export() is None
    assert editing.t("gui.edit.target.unsaved", path=os.path.normpath(str(b_path))) in dlg.error_text()
    assert b_path.read_bytes() == original and b.dirty and yes == []


def test_export_onto_an_unchanged_tab_asks_once_and_reloads_it(qtbot, make, app, tmp_path, monkeypatch) -> None:
    import os

    from jsonreshape.gui.exportdialog import ExportDialog

    a = open_editable(qtbot, make, write_recs(tmp_path / "a.json"))
    b_path = write_recs(tmp_path / "b.json", 6)
    b = open_editable(qtbot, make, b_path)
    asked: list[str] = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda _p, text, _t: asked.append(text) or True)
    old_doc = b.doc
    dlg = ExportDialog(a, None, scope="all")
    qtbot.addWidget(dlg)
    dlg.set_output_path(b_path)
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT):
        assert dlg.start_export() is not None, dlg.error_text()
    # the open-tab question replaces the overwrite question: asked once
    assert asked == [editing.t("gui.edit.target.open", path=os.path.normpath(str(b_path)))]
    qtbot.waitUntil(lambda: b.doc is not None and b.doc is not old_doc and b.state == "ready", timeout=TIMEOUT)
    assert len(values(b)) == 8 and values(b)[7]["name"] == "n7"
    assert app.errors == []


# ----------------------------------------------------------------------------------------
# S9 (gzip trailing data) through saver.save_questions; S6: a cleanup warning of a successful save
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("answer", [False, True])
def test_gzip_trailing_data_is_asked_before_an_in_place_save(qtbot, make, app, tmp_path, monkeypatch,
                                                             answer) -> None:
    path = tmp_path / "z.json.gz"
    data = json.dumps([{"id": i, "name": f"n{i}"} for i in range(5)]).encode("utf-8")
    path.write_bytes(gzip.compress(data) + b"trailing bytes after the stream")
    app.settings = dataclasses.replace(app.settings, backup_on_save=False)
    asked: list[str] = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda _p, text, _t: asked.append(text) or answer)
    s = open_editable(qtbot, make, path)
    assert run(qtbot, s.edit.set_value, NodeRef(1, ("name",)), '"gz"') is not None
    original = path.read_bytes()
    reports: list = []
    s.edit.save(on_done=reports.append)
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    assert len(asked) == 1 and "z.json.gz" in asked[0]
    if answer:
        assert reports[0] is not None and path.read_bytes() != original
        assert json.loads(gzip.decompress(path.read_bytes()))[1]["name"] == "gz"
    else:
        assert reports == [None] and path.read_bytes() == original and s.dirty


def test_a_cleanup_failure_after_saving_is_a_warning_of_a_successful_save(qtbot, make, app, tmp_path,
                                                                           monkeypatch) -> None:
    from jsonreshape import editstore

    path = write_recs(tmp_path / "w6.json")
    s = open_editable(qtbot, make, path)
    assert run(qtbot, s.edit.set_value, NodeRef(1, ("name",)), '"w"') is not None

    def full(self, *a, **kw):
        raise EditError("editstore.error.disk_full", path=str(self.dir))

    monkeypatch.setattr(editstore.EditStore, "discard", full)
    app.statuses.clear()
    reports: list = []
    s.edit.save(on_done=reports.append)
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    assert reports[0] is not None and json.loads(path.read_text(encoding="utf-8"))[1]["name"] == "w"
    assert app.errors == []                       # a successful save, never a failed one (review S6)
    saved_prefix = editing.t("gui.edit.saved", path="\x00", records=0).split("\x00")[0]
    saved = [x for x in app.statuses if x.startswith(saved_prefix)]
    warning = editing.t("edits.warn.layer_left", detail="\x00").split("\x00")[0].strip(" (")
    assert saved and warning and warning in saved[-1]
