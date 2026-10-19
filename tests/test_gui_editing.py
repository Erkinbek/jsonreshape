"""EditController (jsonreshape/gui/editing.py; ARCHITECTURE §12.9): commands on real sessions.

Every document kind gets a controller when its index is finished; the edit worker opens the edit session and the
GUI installs its initial state. Commands go through the controller with GUI NodeRefs and come back as ChangeSets
applied in the GUI thread (``data_changed`` then ``changed``); undo / redo; one command in flight; stale refs;
record tasks (delete / duplicate records); bulk preview + apply; the pure ref helpers (``remap_ref``,
``to_edit_ref`` / ``from_edit_ref``) against the ChangeSets of real steps.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from jsonreshape import codec, edits
from jsonreshape.errors import EditError
from jsonreshape.gui import editing
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.editing import EDIT_SELECTION_MAX, from_edit_ref, remap_ref, to_edit_ref
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.overlay import ChangeSet, NodeChange, OverlayState, Remap

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


def open_editable(qtbot, make, path, **kw) -> DocSession:
    s = make(path, **kw)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    assert s.edit is not None
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    return s


def run(qtbot, fn, *args, **kw):
    """Call a controller command and wait for its ``on_done``; returns what it got."""
    got: list = []
    ok = fn(*args, on_done=got.append, **kw)
    assert ok is not False, "the command was refused"
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


def value(s: DocSession, pos: int):
    """The parsed value of record ``pos`` in the state the session shows."""
    return s.doc.pinned().record(pos)


def values(s: DocSession) -> list:
    v = s.doc.pinned()
    return [v.record(i) for i in range(v.record_count)]


def write(path: Path, data: str) -> Path:
    path.write_text(data, encoding="utf-8")
    return path


def recs(n: int) -> list[dict]:
    return [{"id": i, "name": f"n{i}", "tags": [f"t{i}", "x"], "o": {"v": i}} for i in range(n)]


@pytest.fixture
def array_file(tmp_path: Path) -> Path:
    return write(tmp_path / "a.json", json.dumps(recs(20), indent=2))


DOCS = {
    "array": ('[{"id": 0, "a": [1, 2, 3]}, {"id": 1, "a": []}, {"id": 2, "a": [4]}]', {}),
    "jsonl": ('{"id": 0, "a": [1, 2, 3]}\n{"id": 1, "a": []}\n{"id": 2, "a": [4]}\n', {}),
    "keyed": ('{"k0": {"id": 0, "a": [1, 2, 3]}, "k1": {"id": 1, "a": []}, "k2": {"id": 2, "a": [4]}}',
              {"format": "keyed"}),
    "wrapped": ('{"meta": {"v": 1, "who": "Ali"}, "data": [{"id": 0, "a": [1, 2, 3]}, {"id": 1, "a": []}, '
                '{"id": 2, "a": [4]}], "tail": [1, 2]}', {"format": "wrapped", "record_path": "data"}),
    "brace_list": ('{{"id": 0, "a": [1, 2, 3]}, {"id": 1, "a": []}, {"id": 2, "a": [4]}}', {}),
    "stream": ('{"id": 0, "a": [1, 2, 3]}\n\n{"id": 1, "a": []}\n{"id": 2, "a": [4]}\n',
               {"format": "stream"}),
    "single": ('{"id": 0, "a": [1, 2, 3], "o": {"x": "O\'zbekiston"}}', {"format": "single"}),
}


def doc_file(tmp_path: Path, kind: str) -> tuple[Path, dict]:
    text, kw = DOCS[kind]
    ext = ".jsonl" if kind == "jsonl" else ".json"
    return write(tmp_path / f"{kind}{ext}", text), dict(kw)


# ----------------------------------------------------------------------------------------
# lifecycle
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", sorted(DOCS))
def test_every_kind_gets_an_available_controller(qtbot, make, tmp_path, kind):
    path, kw = doc_file(tmp_path, kind)
    s = open_editable(qtbot, make, path, **kw)
    ctl = s.edit
    assert ctl.reason == "" and ctl.reason_text == ""
    assert not ctl.busy and not ctl.dirty and not ctl.can_undo and not ctl.can_redo and ctl.steps == 0
    # the edit session's initial state is installed lazily: opening does not change data_gen
    assert ctl.applied_gen != 0 and s.data_gen == s.doc.gen == 0
    assert s.doc.overlay is None and not s.doc.edited
    assert ctl.layer_key
    assert ctl.pending == []
    assert not s.dirty


def test_initial_state_is_installed_lazily(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    s.set_filter(FilterSpec("id >= 15"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    view = s.view_recordset()
    assert view.gen == 0 and s.data_gen == 0
    # a record set of the plain file as an edit scope: the state is installed, the session's sets re-tagged
    cs = run(qtbot, ctl.delete_records, view)
    assert cs is not None and s.doc.record_count == 15
    assert s.data_gen == s.doc.gen == cs.gen == ctl.applied_gen
    # the first change of another document installs its own state
    t2 = open_editable(qtbot, make, write(array_file.parent / "b.json", json.dumps(recs(3))))
    gen0 = t2.edit.applied_gen
    cs = run(qtbot, t2.edit.set_value, NodeRef(1, ("id",)), "10")
    assert t2.doc.gen == t2.data_gen == cs.gen != gen0 and t2.doc.overlay is cs.state


def test_partial_document_is_read_only(qtbot, make, tmp_path):
    path = write(tmp_path / "bad.json", '[{"a": 1}, {"a": 2}, {"a": ]')
    s = make(path)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    assert s.doc.error is not None
    qtbot.waitUntil(lambda: s.edit.reason == "partial", timeout=TIMEOUT)
    assert not s.edit.available
    assert s.edit.reason_text
    assert s.edit.set_value(NodeRef(0, ("a",)), "5") is False
    assert s.doc.overlay is None and not s.dirty


def test_refused_while_not_available_reports_status(qtbot, make, app, tmp_path):
    path = write(tmp_path / "bad.json", '[{"a": 1}, {"a": ]')
    s = make(path)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    qtbot.waitUntil(lambda: s.edit.reason == "partial", timeout=TIMEOUT)
    app.statuses.clear()
    got = []
    assert s.edit.delete(NodeRef(0, ("a",)), on_done=got.append) is False
    assert got == [None]
    assert app.statuses and s.edit.reason_text in app.statuses[-1]


def test_failed_open_has_no_controller(qtbot, make, tmp_path):
    s = make(tmp_path / "missing.json")
    with qtbot.waitSignal(s.open_failed, timeout=TIMEOUT):
        s.start()
    assert s.edit is None


def test_huge_single_document_is_read_only(qtbot, make, app, tmp_path):
    import dataclasses

    path = write(tmp_path / "s.json", json.dumps({"x": "y" * 4000}))
    app.settings = dataclasses.replace(app.settings, large_node_bytes=1024)
    s = make(path, format="single")
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    qtbot.waitUntil(lambda: s.edit.reason == "huge_single", timeout=TIMEOUT)
    assert not s.edit.available


# ----------------------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------------------
def test_set_value_undo_redo_array(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    seen: list = []
    s.data_changed.connect(lambda cs: seen.append(("data", cs.gen, s.data_gen, ctl.applied_gen)))
    ctl.changed.connect(lambda cs: seen.append(("changed", cs.gen)))
    dirty: list = []
    s.dirty_changed.connect(dirty.append)
    gen0 = ctl.applied_gen
    cs = run(qtbot, ctl.set_value, NodeRef(3, ("name",)), '"O\'zbekiston 0.1"')
    assert cs is not None and cs.kind == "edit" and cs.records == frozenset({3})
    assert value(s, 3)["name"] == "O'zbekiston 0.1"
    assert seen == [("data", cs.gen, cs.gen, cs.gen), ("changed", cs.gen)]
    assert ctl.applied_gen == s.data_gen == s.doc.gen == cs.gen != gen0
    assert dirty == [True] and s.dirty and ctl.dirty and ctl.steps == 1 and ctl.can_undo and not ctl.can_redo
    assert ctl.undo_text and not ctl.redo_text
    # exact numbers survive
    run(qtbot, ctl.set_value, NodeRef(4, ("o", "v")), "12345678901234567890")
    run(qtbot, ctl.set_value, NodeRef(5, ("o",)), '{"n": 1e400, "m": -0.0, "k": 1.10}')
    raw = s.doc.pinned().record_bytes(5)
    assert b"1e400" in raw and b"-0.0" in raw and b"1.10" in raw
    assert value(s, 4)["o"]["v"] == 12345678901234567890
    assert ctl.steps == 3
    cs = run(qtbot, ctl.undo)
    assert cs.kind == "undo"
    cs = run(qtbot, ctl.undo)
    cs = run(qtbot, ctl.undo)
    assert value(s, 3)["name"] == "n3" and not ctl.dirty and not s.dirty and ctl.can_redo
    assert dirty[-1] is False
    run(qtbot, ctl.redo)
    assert value(s, 3)["name"] == "O'zbekiston 0.1" and ctl.dirty and ctl.steps == 1
    assert run(qtbot, ctl.redo) is not None
    assert run(qtbot, ctl.redo) is not None
    assert run(qtbot, ctl.redo) is None             # nothing left
    assert s.edit.reason == ""


def test_node_commands(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    # rename, insert, paste, duplicate, move_by, change_type, delete, set_path
    run(qtbot, ctl.rename_key, NodeRef(0, ("name",)), "nom")
    assert list(value(s, 0)) == ["id", "nom", "tags", "o"]
    run(qtbot, ctl.insert, NodeRef(0, ("tags",)), 0, '"first"')
    assert value(s, 0)["tags"] == ["first", "t0", "x"]
    run(qtbot, ctl.paste, NodeRef(0, ("o",)), '{"p": [1, 2]}')
    assert value(s, 0)["o"] == {"v": 0, "pasted": {"p": [1, 2]}}
    run(qtbot, ctl.duplicate, [NodeRef(1, ("tags", 0))])
    assert value(s, 1)["tags"] == ["t1", "t1", "x"]
    run(qtbot, ctl.move_by, [NodeRef(1, ("tags", 2))], -1)
    assert value(s, 1)["tags"] == ["t1", "x", "t1"]
    run(qtbot, ctl.change_type, [NodeRef(2, ("id",))], "string")
    assert value(s, 2)["id"] == "2"
    run(qtbot, ctl.delete, NodeRef(2, ("tags", 0)))
    assert value(s, 2)["tags"] == ["x"]
    run(qtbot, ctl.set_path, NodeRef(3, ("new", "deep")), "true")
    assert value(s, 3)["new"] == {"deep": True}
    run(qtbot, ctl.move, [NodeRef(4, ("name",))], NodeRef(4, ("o",)), None)
    assert value(s, 4) == {"id": 4, "tags": ["t4", "x"], "o": {"v": 4, "name": "n4"}}
    assert ctl.steps == 9
    assert s.app.errors == []


def test_record_level_commands_remap_current_and_history(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    s.select(NodeRef(5, ("name",), (1,)))
    s.select(NodeRef(7, ("tags", 1), (2, 1)))
    s.select(NodeRef(2, ()))
    assert s.history() == [NodeRef(5, ("name",), (1,)), NodeRef(7, ("tags", 1), (2, 1)), NodeRef(2, ())]
    run(qtbot, ctl.insert, None, 0, '{"id": "new"}')
    assert value(s, 0) == {"id": "new"} and s.doc.record_count == 21
    # the history follows the records; the current node becomes the step's selection (the new record),
    # without a history entry
    assert s.current == NodeRef(0, (), ())
    assert s.history() == [NodeRef(6, ("name",), (1,)), NodeRef(8, ("tags", 1), (2, 1)), NodeRef(3, ())]
    # deleting an element before the one in the history shifts its index; deleting the record drops it
    run(qtbot, ctl.delete, NodeRef(8, ("tags", 0), (2, 0)))
    assert NodeRef(8, ("tags", 0), (2, 0)) in s.history()
    run(qtbot, ctl.delete, NodeRef(6, ()))
    hist = s.history()
    assert NodeRef(6, ("name",), (1,)) not in hist and len(hist) == 2
    assert s.can_back
    run(qtbot, ctl.undo)
    run(qtbot, ctl.undo)
    run(qtbot, ctl.undo)
    assert values(s) == recs(20)


def test_duplicate_and_move_records(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    run(qtbot, ctl.duplicate, [NodeRef(0, ()), NodeRef(2, ())])
    ids = [r["id"] for r in values(s)]
    assert ids[:6] == [0, 0, 1, 2, 2, 3]
    run(qtbot, ctl.move, [NodeRef(0, ())], None, None)
    assert [r["id"] for r in values(s)][-1] == 0
    run(qtbot, ctl.move_by, [NodeRef(0, ())], 1)
    assert [r["id"] for r in values(s)][:2] == [1, 0]


@pytest.mark.parametrize("kind", ["jsonl", "keyed", "wrapped", "brace_list", "stream"])
def test_commands_on_kinds(qtbot, make, tmp_path, kind):
    path, kw = doc_file(tmp_path, kind)
    s = open_editable(qtbot, make, path, **kw)
    ctl = s.edit
    run(qtbot, ctl.set_value, NodeRef(1, ("a",)), '["Тошкент", "O‘zbekiston"]')
    assert value(s, 1)["a"] == ["Тошкент", "O‘zbekiston"]
    if kind == "keyed":
        run(qtbot, ctl.insert, None, 1, '{"id": 9}', key="k9")
        assert s.doc.pinned().record_key(1) == "k9"
        run(qtbot, ctl.rename_key, NodeRef(1, ()), "k10")
        assert s.doc.pinned().record_key(1) == "k10"
    else:
        run(qtbot, ctl.insert, None, 1, '{"id": 9}')
        assert value(s, 1) == {"id": 9}
    run(qtbot, ctl.delete, NodeRef(0, ("a", 1)))
    assert value(s, 0)["a"] == [1, 3]
    if kind == "wrapped":
        run(qtbot, ctl.set_value, NodeRef(None, ("meta", "who")), '"Vali"')
        assert s.doc.pinned().node(("meta", "who")).value() == "Vali"
    assert ctl.steps == (4 if kind in ("keyed", "wrapped") else 3)
    for _ in range(ctl.steps):
        run(qtbot, ctl.undo)
    assert not ctl.dirty and ctl.steps == 0
    assert value(s, 1)["a"] == []


def test_single_document_refs(qtbot, make, tmp_path):
    path, kw = doc_file(tmp_path, "single")
    s = open_editable(qtbot, make, path, **kw)
    ctl = s.edit
    s.select(NodeRef(None, ("a", 2), (1, 2)))
    cs = run(qtbot, ctl.delete, NodeRef(None, ("a", 0), (1, 0)))
    assert cs.nodes and cs.nodes[0].record == 0          # core convention
    assert value(s, 0)["a"] == [2, 3]
    assert s.history() == [NodeRef(None, ("a", 1), (1, 1))]   # remapped with single=True
    assert s.current == NodeRef(None, ("a", 0), (1, 0))       # the step's selection (the neighbour)
    run(qtbot, ctl.set_value, NodeRef(None, ("o", "x")), '"Ўзбекистон"')
    assert value(s, 0)["o"]["x"] == "Ўзбекистон"
    got: list = []
    s.app.statuses.clear()
    assert ctl.insert(None, 0, "1", on_done=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == [None]                                  # single documents have no record level
    # an expected refusal of a user command is told on the status bar, not in an error window (review3 L8)
    assert s.app.errors == []
    assert s.app.statuses and EditError("edits.error.single_root").message in s.app.statuses[-1]


def test_record_tasks_delete_and_duplicate(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    kinds: list = []
    s.app.tasks.task_added.connect(lambda task: kinds.append(task.kind))
    cs = run(qtbot, ctl.delete_records, range(0, 20, 2))
    assert cs is not None and s.doc.record_count == 10
    assert [r["id"] for r in values(s)] == list(range(1, 20, 2))
    assert "edit" in kinds
    cs = run(qtbot, ctl.duplicate_records, [0, 9])
    assert [r["id"] for r in values(s)][:3] == [1, 1, 3] and s.doc.record_count == 12
    # a RecordSet of the view as positions
    s.set_filter(None)
    cs = run(qtbot, ctl.delete_records, s.view_recordset())
    assert s.doc.record_count == 0
    run(qtbot, ctl.undo)
    assert s.doc.record_count == 12


def test_one_command_in_flight(qtbot, make, app, array_file, monkeypatch):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    gate = threading.Event()
    orig = edits.EditSession.set_value

    def slow(self, *a, **kw):
        gate.wait(10)
        return orig(self, *a, **kw)

    monkeypatch.setattr(edits.EditSession, "set_value", slow)
    first: list = []
    assert ctl.set_value(NodeRef(0, ("id",)), "100", on_done=first.append)
    assert ctl.busy
    app.statuses.clear()
    refused: list = []
    assert ctl.set_value(NodeRef(1, ("id",)), "101", on_done=refused.append) is False
    assert ctl.undo() is False
    assert refused == [None] and app.statuses
    gate.set()
    qtbot.waitUntil(lambda: bool(first), timeout=TIMEOUT)
    assert not ctl.busy
    assert value(s, 0)["id"] == 100 and value(s, 1)["id"] == 1


def test_stale_refs_are_refused(qtbot, make, app, array_file):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    ctl._applied_gen = ctl._applied_gen + 1000         # refs of a state the session is not in
    app.statuses.clear()
    got = run(qtbot, ctl.set_value, NodeRef(0, ("id",)), "5")
    assert got is None
    assert any("o'zgardi" in x or "changed" in x for x in app.statuses)
    assert not ctl.dirty and value(s, 0)["id"] == 0
    assert app.errors == []


def test_too_many_nodes_point_to_bulk(qtbot, make, app, array_file):
    s = open_editable(qtbot, make, array_file)
    refs = [NodeRef(0, ("tags", 0))] * (EDIT_SELECTION_MAX + 1)
    app.statuses.clear()
    assert s.edit.delete(refs) is False
    assert app.statuses and "Ctrl+H" in app.statuses[-1]


def test_command_errors_are_reported(qtbot, make, app, array_file):
    # review3 L8: an expected refusal (a key that exists) is told on the status bar -- never an error window
    s = open_editable(qtbot, make, array_file)
    app.statuses.clear()
    got = run(qtbot, s.edit.rename_key, NodeRef(0, ("name",)), "id")    # the key exists
    assert got is None
    assert app.errors == []
    message = EditError("edits.error.key_exists", key="id").message
    assert app.statuses and message in app.statuses[-1]
    assert not s.edit.busy and s.edit.available
    # a command's on_error callback takes the refusal instead (a view shows it in place)
    refused: list = []
    app.statuses.clear()
    got = run(qtbot, s.edit.rename_key, NodeRef(0, ("name",)), "id", on_error=refused.append)
    assert got is None and [getattr(e, "key", "") for e in refused] == ["edits.error.key_exists"]
    assert app.errors == [] and not any(message in x for x in app.statuses)


def test_bulk_preview_and_apply(qtbot, make, array_file):
    from jsonreshape.bulk import BulkSpec

    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    previews: list = []
    task = ctl.bulk_preview(BulkSpec("replace", {"find": "x", "replacement": "Y"}), None, previews.append)
    assert task is not None and task.kind == "bulk"
    qtbot.waitUntil(lambda: bool(previews), timeout=TIMEOUT)
    preview = previews[0]
    assert preview.gen == ctl.applied_gen and preview.values_changed == 20
    cs = run(qtbot, ctl.bulk_apply, preview)
    assert cs.kind == "bulk"
    assert all(r["tags"][1] == "Y" for r in values(s))
    # a preview of an older state cannot be applied
    previews.clear()
    ctl.bulk_preview(BulkSpec("replace", {"find": "Y", "replacement": "Z"}), None, previews.append)
    qtbot.waitUntil(lambda: bool(previews), timeout=TIMEOUT)
    run(qtbot, ctl.undo)
    assert ctl.bulk_apply(previews[0]) is False
    previews[0].discard()


def test_bulk_dedupe_remaps_the_view(qtbot, make, tmp_path):
    from jsonreshape.bulk import BulkSpec

    path = write(tmp_path / "d.json", json.dumps([{"k": i % 5, "i": i} for i in range(30)]))
    s = open_editable(qtbot, make, path)
    s.set_filter(FilterSpec("i >= 10"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 20, timeout=TIMEOUT)
    previews: list = []
    s.edit.bulk_preview(BulkSpec("dedupe", {"key": "k"}), None, previews.append)
    qtbot.waitUntil(lambda: bool(previews), timeout=TIMEOUT)
    cs = run(qtbot, s.edit.bulk_apply, previews[0])
    assert cs.remap is not None and s.doc.record_count == 5
    assert s.view_len() == 0 and not s.view_outdated      # every filtered record was a duplicate


# ----------------------------------------------------------------------------------------
# refs (pure)
# ----------------------------------------------------------------------------------------
def _cs(remap=None, nodes=(), reset=False):
    state = OverlayState.initial(10, None)
    return ChangeSet(state, "edit", "", frozenset(), remap, tuple(nodes), reset=reset)


def test_remap_ref_record_and_nodes():
    cs = _cs(Remap([("ins", 0, 2)]), [NodeChange(5, (1,), Remap([("del", 0, 1)]), 2)])
    assert remap_ref(NodeRef(3, ("a", 1), (1, 1)), cs) == NodeRef(5, ("a", 0), (1, 0))
    assert remap_ref(NodeRef(3, ("a", 0), (1, 0)), cs) is None           # the deleted element
    assert remap_ref(NodeRef(3, ("b", "c"), (2, 0)), cs) == NodeRef(5, ("b", "c"), (2, 0))
    assert remap_ref(NodeRef(3, ("a",), (1,)), cs) == NodeRef(5, ("a",), (1,))   # the container itself
    assert remap_ref(NodeRef(4, ("a", 1), (1, 1)), cs) == NodeRef(6, ("a", 1), (1, 1))  # another record
    deleted = _cs(Remap([("dels", [2, 4])]))
    assert remap_ref(NodeRef(4, ()), deleted) is None
    assert remap_ref(NodeRef(5, ("x",)), deleted) == NodeRef(3, ("x",))
    assert remap_ref(NodeRef(None, ("meta",)), deleted) == NodeRef(None, ("meta",))
    assert remap_ref(NodeRef(1, ()), _cs(reset=True)) is None
    assert remap_ref(None, cs) is None


def test_remap_ref_without_ipath():
    # NodeChange inside record 0, container ipath (2,) = the third member (unknown by key)
    cs = _cs(None, [NodeChange(0, (2,), Remap([("ins", 0, 1)]), 4)])
    # key path below an unknown member index: the element index may have shifted -> cut to the member
    assert remap_ref(NodeRef(0, ("tags", 1)), cs) == NodeRef(0, ("tags",))
    # keys only: unchanged
    assert remap_ref(NodeRef(0, ("o", "v")), cs) == NodeRef(0, ("o", "v"))
    # a partial ipath that tells the member index: exact
    assert remap_ref(NodeRef(0, ("tags", 1), (2,)), cs) == NodeRef(0, ("tags", 2), (2,))
    assert remap_ref(NodeRef(0, ("other", 1), (1,)), cs) == NodeRef(0, ("other", 1), (1,))
    # array indices are known by value
    cs2 = _cs(None, [NodeChange(0, (), Remap([("del", 0, 1)]), 2), NodeChange(0, (0,), Remap([("ins", 0, 1)]), 3)])
    assert remap_ref(NodeRef(0, (1, 0)), cs2) == NodeRef(0, (0, 1))
    assert remap_ref(NodeRef(0, (0, 5)), cs2) is None


def test_remap_ref_single_and_wrapper():
    cs = _cs(None, [NodeChange(0, (1,), Remap([("del", 0, 1)]), 2)])
    ref = NodeRef(None, ("a", 1), (1, 1))
    assert remap_ref(ref, cs, single=True) == NodeRef(None, ("a", 0), (1, 0))
    assert remap_ref(ref, cs) == ref                       # a wrapper ref: record-0 changes do not apply
    wrapper = _cs(None, [NodeChange(None, (0,), Remap([("del", 0, 1)]), 2)])
    assert remap_ref(NodeRef(None, ("meta", "x"), (0, 1)), wrapper) == NodeRef(None, ("meta", "x"), (0, 0))


def test_edit_ref_conversion(qtbot, make, tmp_path, array_file):
    path, kw = doc_file(tmp_path, "single")
    single = open_editable(qtbot, make, path, **kw)
    arr = open_editable(qtbot, make, array_file)
    assert to_edit_ref(single, NodeRef(None, ("a",), (1,))) == edits.Ref(0, ("a",), (1,))
    assert to_edit_ref(arr, NodeRef(None, ("a",))) == edits.Ref(None, ("a",), None)
    assert to_edit_ref(arr, NodeRef(2, ("a", 0), (1,))) == edits.Ref(2, ("a", 0), None)   # partial ipath dropped
    assert from_edit_ref(single, (0, ("a",), (1,))) == NodeRef(None, ("a",), (1,))
    assert from_edit_ref(arr, (3, ["x"], [0])) == NodeRef(3, ("x",), (0,))
    assert from_edit_ref(arr, None) is None


def test_reason_texts_exist_in_both_languages(lang_en):
    from jsonreshape.i18n import set_language

    for reason in editing.REASONS:
        en = editing.reason_text(reason)
        set_language("uz")
        uz = editing.reason_text(reason)
        set_language("en")
        assert en and uz and not en.startswith("gui.edit") and not uz.startswith("gui.edit"), reason
    assert editing.reason_text("") == ""
