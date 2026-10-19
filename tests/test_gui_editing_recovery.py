"""EditController recovery, locks and closing (jsonreshape/gui/editing.py; ARCHITECTURE §12.9).

* A layer left by a killed SUBPROCESS: ``pending_found`` -> editing unavailable (``recovery_pending``) -> Restore
  (the state comes back, undo works) / Discard (asks; the layer is gone, editing starts fresh) / Export as JSON Patch.
* An interrupted save journal is rolled first (back -> Restore works; completed -> the stale layer is deleted).
* A layer locked by a live subprocess or by a second session of this process: ``reason == "locked"``, retried until
  the holder lets go.
* ``ask_close`` through ``ask_save_handler``: cancel / discard / save / Save As / JSON Patch (file changed on disk);
  headless without a handler keeps the layer; nothing to save answers at once.
* ``detach`` while a command runs: the command finishes, the layer keeps the step, no task is left.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from jsonreshape import edits, fileio, saver
from jsonreshape.editstore import EditStore, find_pending
from jsonreshape.gui import editing, widgets
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession, NodeRef
from jsonreshape.reader import Document

pytestmark = pytest.mark.gui

TIMEOUT = 30000
ROOT = Path(__file__).resolve().parents[1]

CHILD = r'''
import os, sys
sys.path[:0] = [{root!r}]
from jsonreshape.reader import Document
from jsonreshape.edits import EditSession, Ref
doc = Document.open(sys.argv[1])
doc.build_index()
s = EditSession.open(doc)
s.set_value(Ref(0, ("name",)), '"tiklandi O\'zbekiston"')
s.insert(None, 0, '{{"id": "child", "n": 12345678901234567890}}')
print("done", s.state.version, flush=True)
if sys.argv[2] == "hold":
    sys.stdin.readline()
    s.close()
    print("closed", flush=True)
    sys.stdin.readline()
    os._exit(0)
os._exit(3)      # killed: the session is never closed
'''


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
def answers(monkeypatch):
    """``ask_save_handler`` answering from a list; the questions (name, choices) are recorded."""
    state = {"answers": [], "asked": []}

    def handler(_parent, name, choices):
        state["asked"].append((name, tuple(choices)))
        return state["answers"].pop(0)

    monkeypatch.setattr(editing, "ask_save_handler", handler)
    return state


def recs_file(path: Path, n: int = 5) -> Path:
    path.write_text(json.dumps([{"id": i, "name": f"n{i}"} for i in range(n)], indent=1), encoding="utf-8")
    return path


def child(path: Path, mode: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", CHILD.format(root=str(ROOT)), str(path), mode],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=dict(os.environ))


def killed_layer(path: Path) -> None:
    p = child(path, "kill")
    out, err = p.communicate(timeout=120)
    assert p.returncode == 3, err
    assert out.startswith("done 2"), (out, err)


def open_session(qtbot, make, path, **kw) -> DocSession:
    s = make(path, **kw)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    assert s.edit is not None
    return s


def run(qtbot, fn, *args, **kw):
    got: list = []
    ok = fn(*args, on_done=got.append, **kw)
    assert ok is not False, "refused"
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


def values(s: DocSession) -> list:
    v = s.doc.pinned()
    return [v.record(i) for i in range(v.record_count)]


# ----------------------------------------------------------------------------------------
# a layer left by a killed process
# ----------------------------------------------------------------------------------------
def test_restore_a_layer_left_by_a_killed_process(qtbot, make, tmp_path):
    path = recs_file(tmp_path / "r.json")
    killed_layer(path)
    found: list = []
    s = make(path)
    s.edit_changed.connect(lambda: s.edit is not None and s.edit.pending_found.connect(found.append))
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    assert found and len(found[-1]) == 1
    info = s.edit.pending[0]
    assert info.matches and not info.locked and info.steps == 2 and info.damaged is None and not info.saving
    assert not s.edit.available and s.edit.set_value(NodeRef(0, ("id",)), "1") is False
    changes: list = []
    s.data_changed.connect(changes.append)
    cs = run(qtbot, s.edit.restore_pending, info)
    assert cs is not None and cs.reset and cs.kind == "restore" and changes == [cs]
    assert s.edit.available and s.edit.dirty and s.edit.steps == 2 and s.dirty
    assert s.edit.pending == []
    vals = values(s)
    assert vals[0] == {"id": "child", "n": 12345678901234567890}
    assert vals[1]["name"] == "tiklandi O'zbekiston"
    run(qtbot, s.edit.undo)
    run(qtbot, s.edit.undo)
    assert values(s) == [{"id": i, "name": f"n{i}"} for i in range(5)] and not s.edit.dirty
    run(qtbot, s.edit.redo)                       # the first step: the value of record 0
    assert values(s)[0]["name"] == "tiklandi O'zbekiston" and len(values(s)) == 5
    run(qtbot, s.edit.redo)
    assert values(s)[0]["id"] == "child" and s.edit.steps == 2


def test_discard_a_pending_layer(qtbot, make, tmp_path, yes):
    path = recs_file(tmp_path / "d.json")
    killed_layer(path)
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    info = s.edit.pending[0]
    got: list = []
    s.edit.discard_pending(info, None, on_done=got.append)
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    assert yes and s.edit.pending == [] and not s.edit.dirty
    assert values(s)[0] == {"id": 0, "name": "n0"}
    doc = Document.open(path)
    doc.build_index()
    try:
        assert find_pending(doc) == [] or all(p.locked for p in find_pending(doc))
    finally:
        doc.close()
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), "42")     # a fresh layer works
    assert values(s)[0]["id"] == 42


def test_discard_pending_asks_first(qtbot, make, tmp_path, monkeypatch):
    path = recs_file(tmp_path / "n.json")
    killed_layer(path)
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: False)
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    got: list = []
    s.edit.discard_pending(s.edit.pending[0], None, on_done=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == [None] and s.edit.reason == "recovery_pending" and len(s.edit.pending) == 1


def test_export_a_pending_layer_as_json_patch(qtbot, make, tmp_path):
    from jsonreshape import jsonpatch

    path = recs_file(tmp_path / "e.json")
    killed_layer(path)
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    target = tmp_path / "changes.patch.json"
    count = run(qtbot, s.edit.export_pending, s.edit.pending[0], target)
    assert count and count >= 2 and target.exists()
    ops = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(ops, list) and len(ops) == count
    # the layer is still there (Discard deletes it) and the patch reproduces the edits on the original
    assert s.edit.reason == "recovery_pending" and len(s.edit.pending) == 1
    copy = tmp_path / "copy.json"
    copy.write_bytes(path.read_bytes())
    doc = Document.open(copy)
    doc.build_index()
    es = edits.EditSession.open(doc)
    try:
        jsonpatch.apply_patch(es, str(target))
        got = [doc.pinned().record(i) for i in range(doc.record_count)]
    finally:
        es.discard()
        es.close()
        doc.close()
    assert got[0] == {"id": "child", "n": 12345678901234567890}
    assert got[1]["name"] == "tiklandi O'zbekiston"


def _layer_dir(path: Path) -> Path:
    doc = Document.open(path)
    doc.build_index()
    try:
        infos = find_pending(doc)
        assert len(infos) == 1
        return infos[0].dir
    finally:
        doc.close()


def test_damaged_step_restores_up_to_the_last_good_one(qtbot, make, tmp_path, yes):
    import sqlite3

    path = recs_file(tmp_path / "dmg.json")
    killed_layer(path)
    con = sqlite3.connect(str(_layer_dir(path) / "layer.sqlite"))
    try:
        con.execute("UPDATE steps SET data = '{' WHERE seq = 2")    # the insert step cannot be read
        con.commit()
    finally:
        con.close()
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    info = s.edit.pending[0]
    assert info.damaged is None                       # the light check cannot see it
    s.edit.restore_pending(info)
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    assert any("2" in q and "1" in q for q in yes)    # "damaged at step 2 -- restore up to step 1?"
    assert s.edit.steps == 1 and len(values(s)) == 5
    assert values(s)[0]["name"] == "tiklandi O'zbekiston"


def test_damaged_layer_offers_delete_only(qtbot, make, app, tmp_path, yes):
    path = recs_file(tmp_path / "trunc.json")
    killed_layer(path)
    seg = _layer_dir(path) / "seg-00000.dat"
    seg.write_bytes(b"")                              # the committed bytes are gone
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    info = s.edit.pending[0]
    assert info.damaged
    app.statuses.clear()
    got: list = []
    s.edit.restore_pending(info, on_done=got.append)
    assert got == [None] and app.statuses           # not restorable: Export / Delete only
    s.edit.discard_pending(info, None)
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    assert s.edit.pending == [] and not (_layer_dir_exists(info.dir))


def _layer_dir_exists(d: Path) -> bool:
    return (Path(d) / "layer.sqlite").exists()


def test_stale_layer_does_not_block_and_can_be_ignored(qtbot, make, tmp_path):
    path = recs_file(tmp_path / "old.json")
    killed_layer(path)
    path.write_text(json.dumps([{"id": "new version"}]), encoding="utf-8")     # another version of the file
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.available and bool(s.edit.pending), timeout=TIMEOUT)
    info = s.edit.pending[0]
    assert not info.matches and info.steps == 2
    got: list = []
    s.edit.restore_pending(info, on_done=got.append)
    assert got == [None]                               # an older version cannot be restored
    emitted: list = []
    s.edit.pending_found.connect(emitted.append)
    s.edit.ignore_pending(info)
    assert s.edit.pending == [] and emitted == [[]]
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), '"edited"')


def test_bookmark_layer_form_survives_the_state_file(tmp_path):
    from jsonreshape.gui.guistate import Bookmark

    state = GuiState.load(tmp_path / "st.json")
    marks = [Bookmark("a", 3, ["x", 0]), Bookmark("b", 41, [], layer="ab" * 16), Bookmark("w", None, ["meta"])]
    state.set_bookmarks(tmp_path / "f.json", marks)
    assert state.save(raise_errors=True)
    raw = json.loads((tmp_path / "st.json").read_text(encoding="utf-8"))
    items = next(iter(raw["bookmarks"].values()))["items"]
    assert items[1]["record"] == {"rid": 41, "layer": "ab" * 16} and items[0]["record"] == 3
    again = GuiState.load(tmp_path / "st.json").bookmarks(tmp_path / "f.json")
    assert [(b.name, b.record, b.layer, b.path) for b in again] == [
        ("a", 3, None, ["x", 0]), ("b", 41, "ab" * 16, []), ("w", None, None, ["meta"])]
    with pytest.raises(ValueError):
        Bookmark("bad", None, [], layer="k")
    with pytest.raises(TypeError):
        Bookmark.from_dict({"name": "x", "record": {"rid": 1, "layer": 5}})


def _journal_back(path: Path) -> dict:
    """The journal of an in-place save of ``path`` that stopped before any rename (the file is unchanged)."""
    temp = path.parent / f".{path.name}.0123abcd{saver.TEMP_SUFFIX}"
    temp.write_bytes(b'[{"id": "saved"}]')
    old = fileio.file_identity(path)
    new = fileio.file_identity(temp)
    return {"v": 1, "op": "save", "source": str(path), "temp": str(temp), "backup": None, "had_backup": False,
            "old_backup": None, "aside": str(path.parent / f".{path.name}.22222222.old"),
            "old_identity": old.to_dict(), "new": {"size": new.size, "hashes": list(new.hashes)},
            "time": time.time()}


def test_interrupted_save_is_rolled_back_then_restored(qtbot, make, app, tmp_path):
    path = recs_file(tmp_path / "j.json")
    doc = Document.open(path)
    doc.build_index()
    es = edits.EditSession.open(doc)
    es.set_value(edits.Ref(1, ("name",)), '"jurnal"')
    journal = _journal_back(path)
    es.store.journal(journal)
    es.close()                    # keeps the layer: head 1 + the journal
    doc.close()
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    info = s.edit.pending[0]
    assert info.saving and info.saving["temp"] == journal["temp"]
    app.statuses.clear()
    cs = run(qtbot, s.edit.restore_pending, info)
    assert cs is not None and cs.reset
    assert not Path(journal["temp"]).exists()                     # rolled back: the temp file is gone
    assert any(journal["source"] in x for x in app.statuses)      # the roll was told
    assert values(s)[1]["name"] == "jurnal" and s.edit.steps == 1 and s.edit.available


def test_completed_save_journal_deletes_the_stale_layer(qtbot, make, app, tmp_path):
    path = recs_file(tmp_path / "c.json")
    doc = Document.open(path)
    doc.build_index()
    es = edits.EditSession.open(doc)
    es.set_value(edits.Ref(1, ("name",)), '"jurnal"')
    journal = _journal_back(path)
    es.store.journal(journal)
    es.close()
    doc.close()
    os.replace(journal["temp"], path)                  # the save had finished: the file is the new one
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: bool(s.edit.pending) or s.edit.available, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: bool(s.edit.pending), timeout=TIMEOUT)
    info = s.edit.pending[0]
    assert info.saving and not info.matches and s.edit.available     # a stale layer does not block editing
    run(qtbot, s.edit.roll_pending, info)
    assert s.edit.pending == [] and s.edit.available
    assert values(s) == [{"id": "saved"}]


# ----------------------------------------------------------------------------------------
# locks
# ----------------------------------------------------------------------------------------
def test_layer_locked_by_another_process(qtbot, make, tmp_path, monkeypatch):
    monkeypatch.setattr(editing, "LOCK_RETRY_MS", 100)
    path = recs_file(tmp_path / "l.json")
    p = child(path, "hold")
    try:
        assert p.stdout.readline().startswith("done")
        s = open_session(qtbot, make, path)
        qtbot.waitUntil(lambda: s.edit.reason == "locked", timeout=TIMEOUT)
        assert not s.edit.available and s.edit.reason_text
        p.stdin.write("\n")
        p.stdin.flush()
        assert p.stdout.readline().startswith("closed")
        # the child closed its session (its layer stays for recovery): the retry finds it
        qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
        p.stdin.write("\n")
        p.stdin.flush()
        p.wait(60)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(30)


def test_second_session_in_this_process_is_locked(qtbot, make, tmp_path, monkeypatch):
    monkeypatch.setattr(editing, "LOCK_RETRY_MS", 100)
    path = recs_file(tmp_path / "two.json")
    first = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: first.edit.available, timeout=TIMEOUT)
    second = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: second.edit.reason == "locked", timeout=TIMEOUT)
    first.close()
    qtbot.waitUntil(lambda: second.edit.available, timeout=TIMEOUT)
    run(qtbot, second.edit.set_value, NodeRef(0, ("id",)), "9")


# ----------------------------------------------------------------------------------------
# ask_close
# ----------------------------------------------------------------------------------------
def dirty_session(qtbot, make, path) -> DocSession:
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    run(qtbot, s.edit.set_path, NodeRef(0, ("name",)), '"o\'zgardi"')
    return s


def ask(qtbot, s: DocSession, why: str = "close") -> bool:
    got: list = []
    s.edit.ask_close(None, got.append, why=why)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


def test_ask_close_not_dirty_answers_at_once(qtbot, make, tmp_path, answers):
    s = open_session(qtbot, make, recs_file(tmp_path / "a.json"))
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    got: list = []
    s.edit.ask_close(None, got.append, why="close")
    assert got == [True] and answers["asked"] == []


def test_ask_close_cancel_and_discard(qtbot, make, tmp_path, answers):
    path = recs_file(tmp_path / "a.json")
    s = dirty_session(qtbot, make, path)
    answers["answers"] = ["cancel"]
    assert ask(qtbot, s) is False and s.edit.dirty
    assert answers["asked"] == [("a.json", ("save", "discard", "cancel"))]
    answers["answers"] = ["discard"]
    assert ask(qtbot, s) is True
    assert not s.edit.dirty and not s.dirty
    s.close()
    doc = Document.open(path)
    doc.build_index()
    try:
        assert find_pending(doc) == []          # "Don't save" deletes the layer
    finally:
        doc.close()


def test_ask_close_save(qtbot, make, tmp_path, answers):
    path = recs_file(tmp_path / "s.json")
    s = dirty_session(qtbot, make, path)
    answers["answers"] = ["save"]
    assert ask(qtbot, s, "close") is True
    assert json.loads(path.read_text(encoding="utf-8"))[0]["name"] == "o'zgardi"
    assert not s.dirty and s.edit.reason == "closed"
    s.reopen()                                  # the caller reopens: not dirty any more


def test_ask_close_headless_keeps_the_layer(qtbot, make, tmp_path):
    path = recs_file(tmp_path / "h.json")
    s = dirty_session(qtbot, make, path)
    assert editing.ask_save_handler is None and s.app.headless
    got: list = []
    s.edit.ask_close(None, got.append, why="close")
    assert got == [True]
    s.close()
    doc = Document.open(path)
    doc.build_index()
    try:
        infos = find_pending(doc)
        assert len(infos) == 1 and infos[0].steps == 1     # kept for recovery
    finally:
        doc.close()


def test_ask_close_file_changed_offers_a_patch(qtbot, make, tmp_path, answers, monkeypatch):
    path = recs_file(tmp_path / "p.json")
    s = dirty_session(qtbot, make, path)
    with open(path, "r+b") as f:
        f.seek(10)
        f.write(b"7")
    s._check_change()
    qtbot.waitUntil(lambda: s.edit.reason == "file_changed", timeout=TIMEOUT)
    target = tmp_path / "out.patch.json"
    monkeypatch.setattr(editing, "ask_path_handler", lambda _p, purpose, _s: str(target) if purpose == "patch" else None)
    answers["answers"] = ["patch"]
    assert ask(qtbot, s, "reopen") is True
    assert answers["asked"][-1][1] == ("patch", "discard", "cancel")
    assert target.exists() and json.loads(target.read_text(encoding="utf-8"))


def test_ask_close_file_grown_offers_save_as(qtbot, make, tmp_path, answers, monkeypatch):
    path = tmp_path / "g.jsonl"
    path.write_text('{"id": 0}\n{"id": 1}\n', encoding="utf-8")
    s = dirty_session(qtbot, make, path)
    with open(path, "ab") as f:
        f.write(b'{"id": 2}\n')
    s._check_change()
    qtbot.waitUntil(lambda: s.edit.reason == "file_grown", timeout=TIMEOUT)
    target = tmp_path / "copy.jsonl"
    monkeypatch.setattr(editing, "ask_path_handler", lambda _p, purpose, _s: str(target))
    answers["answers"] = ["save_as"]
    assert ask(qtbot, s, "close") is True
    assert answers["asked"][-1][1] == ("save_as", "discard", "cancel")
    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [{"id": 0, "name": "o'zgardi"}, {"id": 1}]   # the snapshot
    assert path.read_text(encoding="utf-8").endswith('{"id": 2}\n')          # the source is untouched


def test_save_in_place_refused_when_the_file_grew(qtbot, make, tmp_path, monkeypatch):
    path = tmp_path / "gr.jsonl"
    path.write_text('{"id": 0}\n', encoding="utf-8")
    s = dirty_session(qtbot, make, path)
    with open(path, "ab") as f:
        f.write(b'{"id": 1}\n')
    s._check_change()
    qtbot.waitUntil(lambda: s.edit.reason == "file_grown", timeout=TIMEOUT)
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: False)    # Save As offered, declined
    got: list = []
    s.edit.save(on_done=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == [None] and s.edit.dirty


# ----------------------------------------------------------------------------------------
# detach
# ----------------------------------------------------------------------------------------
def test_detach_during_a_running_command(qtbot, make, app, tmp_path, monkeypatch):
    path = recs_file(tmp_path / "x.json")
    s = open_session(qtbot, make, path)
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    started = threading.Event()
    orig = edits.EditSession.set_value

    def slow(self, *a, **kw):
        started.set()
        time.sleep(0.5)
        return orig(self, *a, **kw)

    monkeypatch.setattr(edits.EditSession, "set_value", slow)
    callbacks: list = []
    ctl = s.edit
    assert ctl.set_value(NodeRef(2, ("name",)), '"oxirgi"', on_done=callbacks.append)
    assert started.wait(10)
    t0 = time.monotonic()
    s.close()                                  # detach: the command finishes, the session closes
    assert time.monotonic() - t0 < 5
    assert ctl.closed
    # detach waits up to 2 s; under load the commit may take longer, so the worker's end is waited for
    # (review3 CL-6: the test must not depend on how long the commit takes)
    qtbot.waitUntil(lambda: not ctl._worker.thread_alive, timeout=TIMEOUT)
    qtbot.wait(100)
    assert callbacks == []                     # nothing calls back into a closed tab
    assert app.tasks.wait_idle(30)
    doc = Document.open(path)
    doc.build_index()
    try:
        infos = find_pending(doc)
        assert len(infos) == 1 and infos[0].steps == 1      # the step was committed and kept
    finally:
        doc.close()
