"""DocSession integration of editing (jsonreshape/gui/session.py; ARCHITECTURE §12.9).

ChangeSets applied in one GUI-thread call: the overlay state, ``data_gen``, the view record sets remapped by the edit
worker (filtered, sorted and shown-records views after inserts, deletes, moves, dedupe), the ``REMAP_INLINE_MAX``
fallback to all records with ``view_outdated`` + :meth:`refresh_view`, resets, a running pipeline restarted, the
signal order (``data_changed`` before ``view_changed``), record-set gens, rid-based bookmarks, ``reopen`` with unsaved
changes, save in place (reopen with the report's options, bookmarks converted, no "file changed" alarm) and Save As
(``switch_path``: path, name, bookmarks, layouts, recent files), save failures, the change-check pause, the schema
cache staleness and ``snapshot`` / ``keep_document_for``.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from jsonreshape.errors import EditError
from jsonreshape.gui import fields, widgets
from jsonreshape.gui import session as session_mod
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import Bookmark, GuiState
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.recordset import RecordSet
from jsonreshape.sortkeys import SortKey

pytestmark = pytest.mark.gui

TIMEOUT = 30000


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
    """Every widgets.ask / confirm question is answered "yes" (the answers are recorded)."""
    asked: list[str] = []

    def handler(_parent, text, _title):
        asked.append(text)
        return True

    monkeypatch.setattr(widgets, "confirm_handler", handler)
    return asked


def open_editable(qtbot, make, path, **kw) -> DocSession:
    s = make(path, **kw)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
    return s


def run(qtbot, fn, *args, **kw):
    got: list = []
    ok = fn(*args, on_done=got.append, **kw)
    assert ok is not False, "refused"
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


def wait_view(qtbot, s: DocSession) -> None:
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)


def view_ids(s: DocSession) -> list:
    doc = s.doc.pinned()
    return [doc.record(s.record_at(p))["id"] for p in range(s.view_len())]


def write(path: Path, value) -> Path:
    path.write_text(json.dumps(value, indent=1), encoding="utf-8")
    return path


@pytest.fixture
def array_file(tmp_path: Path) -> Path:
    return write(tmp_path / "a.json", [{"id": i, "g": i % 3, "name": f"n{i:02d}"} for i in range(30)])


# ----------------------------------------------------------------------------------------
# applying ChangeSets: view sets
# ----------------------------------------------------------------------------------------
def test_filtered_and_sorted_views_follow_record_moves(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    ctl = s.edit
    s.set_filter(FilterSpec("g == 1"))
    s.set_sort([SortKey("name", descending=True)])
    wait_view(qtbot, s)
    expected = [i for i in range(29, -1, -1) if i % 3 == 1]
    assert view_ids(s) == expected
    view_before = s.view_recordset()
    assert view_before.gen == s.data_gen
    order: list[str] = []
    s.data_changed.connect(lambda cs: order.append("data"))
    s.view_changed.connect(lambda: order.append("view"))
    run(qtbot, ctl.insert, None, 0, '{"id": "new", "g": 1, "name": "zz"}')
    # the view keeps its records (the new one is not in it until Refresh), at their new positions
    assert view_ids(s) == expected
    assert s.view_recordset() is not view_before and s.view_recordset().gen == s.data_gen
    assert order == ["data"] and not s.view_outdated
    run(qtbot, ctl.delete, NodeRef(s.record_at(0), ()))      # deletes id 28
    assert view_ids(s) == expected[1:]
    run(qtbot, ctl.move, [NodeRef(5, ())], None, 0)
    assert view_ids(s) == expected[1:]
    assert s.app.errors == []
    s.refresh_view()
    wait_view(qtbot, s)
    assert view_ids(s)[0] == "new"


def test_content_change_retags_the_view(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    s.set_filter(FilterSpec("g == 0"))
    wait_view(qtbot, s)
    rs = s.view_recordset()
    run(qtbot, s.edit.set_value, NodeRef(s.record_at(0), ("name",)), '"x"')
    assert s.view_recordset() is rs and rs.gen == s.data_gen == s.doc.gen
    pinned, view = s.snapshot()
    assert view is rs and pinned.gen == rs.gen and pinned.origin is s.doc


def test_shown_records_follow_and_reset_drops_them(qtbot, make, array_file, yes):
    s = open_editable(qtbot, make, array_file)
    s.show_records(RecordSet.from_indices([2, 4, 6], gen=s.data_gen), "Qidiruv")
    run(qtbot, s.edit.delete_records, [0, 1])
    assert [s.record_at(p) for p in range(s.view_len())] == [0, 2, 4]
    assert s.shown_records is not None and s.shown_records.gen == s.data_gen
    outdated: list = []
    s.view_outdated_changed.connect(outdated.append)
    cs = run(qtbot, s.edit._discard_now)
    assert cs.reset
    assert s.view_is_base and s.shown_records is None and s.view_outdated and outdated == [True]
    assert s.current is None and s.history() == []
    s.refresh_view()
    assert not s.view_outdated and s.view_is_base


def test_stale_shown_records_are_refused(qtbot, make, app, array_file):
    s = open_editable(qtbot, make, array_file)
    old_gen = s.data_gen
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), "100")
    s.show_records(RecordSet.from_indices([1, 2], gen=old_gen), "eski")
    assert s.shown_records is None
    assert app.errors and getattr(app.errors[-1], "key", "") == "edits.error.stale_scope"
    app.errors.clear()


def test_remap_fallback_to_all_records(qtbot, make, array_file, monkeypatch):
    monkeypatch.setattr(session_mod, "REMAP_INLINE_MAX", 3)
    monkeypatch.setattr(session_mod, "REMAP_GUI_MAX", 3)
    s = open_editable(qtbot, make, array_file)
    s.set_filter(FilterSpec("g == 2"))
    wait_view(qtbot, s)
    assert s.view_len() == 10
    events: list = []
    s.data_changed.connect(lambda cs: events.append("data"))
    s.view_changed.connect(lambda: events.append("view"))
    s.view_outdated_changed.connect(lambda v: events.append(("outdated", v)))
    run(qtbot, s.edit.insert, None, 0, '{"id": "new", "g": 2, "name": "a"}')
    assert s.view_is_base and s.view_outdated and s.view_len() == 31
    assert s.filter is not None and s.filter.text == "g == 2"         # the specs are kept
    assert events[:3] == ["data", "view", ("outdated", True)]
    assert "Filtr" in s.view_label()
    s.refresh_view()
    wait_view(qtbot, s)
    assert not s.view_outdated and s.view_len() == 11
    assert view_ids(s)[0] == "new"


def test_running_pipeline_is_restarted(qtbot, make, tmp_path, monkeypatch):
    path = tmp_path / "big.json"
    path.write_bytes(b"[" + b",".join(b'{"id":%d,"g":%d}' % (i, i % 2) for i in range(20000)) + b"]")
    s = open_editable(qtbot, make, path)
    orig = session_mod.filter_records
    # the filter waits until the insert's ChangeSet was applied (review3 CL-6: a sleep made the test depend on
    # how long the commit takes -- a slow commit let the filter finish first)
    gate = threading.Event()

    def held(doc, flt, source=None, **kw):
        assert gate.wait(30)
        return orig(doc, flt, source, **kw)

    monkeypatch.setattr(session_mod, "filter_records", held)
    s.set_filter(FilterSpec("g == 1"))
    assert s.view_busy
    try:
        run(qtbot, s.edit.insert, None, 0, '{"id": -1, "g": 1}')    # applied while the filter runs: restarted
    finally:
        gate.set()
    wait_view(qtbot, s)
    assert s.view_len() == 10001 and s.record_at(0) == 0       # computed on the new state
    assert s.view_recordset().gen == s.data_gen


def test_dedupe_and_undo_remap_views(qtbot, make, tmp_path):
    from jsonreshape.bulk import BulkSpec

    path = write(tmp_path / "d.json", [{"k": i % 4, "id": i} for i in range(16)])
    s = open_editable(qtbot, make, path)
    s.set_sort([SortKey("id", descending=True)])
    wait_view(qtbot, s)
    previews: list = []
    s.edit.bulk_preview(BulkSpec("dedupe", {"key": "k"}), None, previews.append)
    qtbot.waitUntil(lambda: bool(previews), timeout=TIMEOUT)
    run(qtbot, s.edit.bulk_apply, previews[0])
    assert view_ids(s) == [3, 2, 1, 0]
    run(qtbot, s.edit.undo)
    assert view_ids(s) == [3, 2, 1, 0]            # the sorted view keeps its records; Refresh shows the rest
    s.refresh_view()
    wait_view(qtbot, s)
    assert view_ids(s) == list(range(15, -1, -1))


# ----------------------------------------------------------------------------------------
# bookmarks
# ----------------------------------------------------------------------------------------
def test_bookmarks_follow_records_and_layers(qtbot, make, app, array_file, yes):
    s = open_editable(qtbot, make, array_file)
    s.add_bookmark("uch", NodeRef(3, ("name",)))
    run(qtbot, s.edit.insert, None, 0, '{"id": "new"}')
    assert s.bookmark_ref(0) == NodeRef(4, ("name",))
    s.add_bookmark("yangi", NodeRef(0, ()))
    stored = app.state.bookmarks(s.path)
    assert stored[0].record == 3 and stored[0].layer is None
    assert stored[1].layer == s.edit.layer_key and stored[1].record == 30      # rid of the new record
    assert stored[1].to_dict()["record"] == {"rid": 30, "layer": s.edit.layer_key}
    assert s.bookmark_location(1) == "#0"
    run(qtbot, s.edit.delete_records, [4])
    assert s.bookmark_ref(0) is None and s.bookmark_location(0) == "(o'chirilgan)"
    run(qtbot, s.edit.undo)
    assert s.bookmark_ref(0) == NodeRef(4, ("name",))
    run(qtbot, s.edit._discard_now)
    assert [bm.name for bm in s.bookmarks] == ["uch"]            # the new record's bookmark is gone
    assert s.bookmark_ref(0) == NodeRef(3, ("name",))


def test_bookmark_of_an_inactive_layer_has_no_position(qtbot, make, app, array_file):
    app.state.set_bookmarks(str(array_file), [Bookmark("eski", 31, [], layer="0" * 32)])
    s = open_editable(qtbot, make, array_file)
    assert s.bookmark_ref(0) is None


# ----------------------------------------------------------------------------------------
# reopen, save, Save As
# ----------------------------------------------------------------------------------------
def test_reopen_with_unsaved_changes(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), "99")
    with pytest.raises(EditError) as err:
        s.reopen()
    assert err.value.key == "edits.error.dirty"
    old = s.edit
    changes: list = []
    s.edit_changed.connect(lambda: changes.append(s.edit))
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(discard_edits=True)
    assert old.closed and changes[0] is None
    # the layer was kept: the new controller offers it for recovery
    qtbot.waitUntil(lambda: s.edit.reason == "recovery_pending", timeout=TIMEOUT)
    infos = s.edit.pending
    assert len(infos) == 1 and infos[0].steps == 1 and infos[0].matches


def test_save_in_place(qtbot, make, app, tmp_path, yes):
    path = write(tmp_path / "s.json", [{"id": i, "name": f"n{i}"} for i in range(10)])
    original = path.read_bytes()
    s = open_editable(qtbot, make, path)
    s.set_filter(FilterSpec("id >= 5"))
    wait_view(qtbot, s)
    s.select(NodeRef(7, ("name",)))
    s.add_bookmark("yetti", NodeRef(7, ()))
    run(qtbot, s.edit.set_value, NodeRef(7, ("name",)), '"Toshkent \\u0027"')
    run(qtbot, s.edit.delete_records, [0, 1])
    run(qtbot, s.edit.insert, None, 0, '{"id": -1}')
    assert s.bookmark_ref(0) == NodeRef(6, ())
    changed: list = []
    s.file_changed.connect(changed.append)
    old_ctl = s.edit
    reports: list = []
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        old_ctl.save(on_done=reports.append)
    report = reports[0]
    assert report is not None and report.in_place and report.records == 9
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [r["id"] for r in data] == [-1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert data[6]["name"] == "Toshkent '"
    assert (tmp_path / "s.json.bak").read_bytes() == original
    assert old_ctl.closed and s.edit is not old_ctl
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
    assert not s.dirty and not s.edit.dirty and s.edit.pending == []
    assert s.format == "array"
    # the view specs, the history and the bookmarks follow the saved file
    wait_view(qtbot, s)
    assert s.filter is not None and [s.doc.record(s.record_at(p))["id"] for p in range(s.view_len())] == [5, 6, 7, 8, 9]
    assert s.bookmarks[0].record == 6 and s.bookmarks[0].layer is None
    assert s.bookmark_ref(0) == NodeRef(6, ())
    assert app.state.bookmarks(str(path))[0].record == 6
    # no "file changed" alarm for the file we wrote ourselves
    qtbot.wait(300)
    s._check_change()
    qtbot.wait(300)
    assert changed == []
    assert s.file_state is None


def test_save_as_switches_the_session(qtbot, make, app, tmp_path, yes):
    path = write(tmp_path / "orig.json", [{"id": i} for i in range(5)])
    original = path.read_bytes()
    app.state.set_table_layout(str(path), {"columns": ["id"]})
    s = open_editable(qtbot, make, path)
    s.add_bookmark("base", NodeRef(2, ()))
    run(qtbot, s.edit.insert, None, 0, '{"id": "yangi"}')
    s.add_bookmark("new", NodeRef(0, ()))
    target = tmp_path / "copy.json"
    paths: list = []
    s.path_changed.connect(paths.append)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.edit.save_as(target)
    assert paths == [str(target)] and s.path == str(target) and s.name == "copy.json"
    assert path.read_bytes() == original                      # the source is untouched
    assert [r["id"] for r in json.loads(target.read_text(encoding="utf-8"))] == ["yangi", 0, 1, 2, 3, 4]
    assert app.state.recent_files()[0] == str(target)
    assert app.state.table_layout(str(target)) == {"columns": ["id"]}
    # bookmarks: the copy gets positions of the new file, the original keeps its own records
    new_marks = {bm.name: bm for bm in app.state.bookmarks(str(target))}
    assert new_marks["base"].record == 3 and new_marks["new"].record == 0
    assert [bm.name for bm in app.state.bookmarks(str(path))] == ["base"]
    assert app.state.bookmarks(str(path))[0].record == 2
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
    assert not s.dirty


def test_save_failure_keeps_the_edits(qtbot, make, app, tmp_path, monkeypatch):
    path = write(tmp_path / "ro.json", [{"id": i} for i in range(4)])
    s = open_editable(qtbot, make, path)
    run(qtbot, s.edit.set_value, NodeRef(1, ("id",)), "10")
    os.chmod(path, stat.S_IREAD)
    asked: list = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda _p, text, _t: asked.append(text) or False)
    try:
        reports: list = []
        s.edit.save(on_done=reports.append)
        qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    assert reports == [None]
    assert app.errors and getattr(app.errors[-1], "key", "") == "fileio.error.read_only"
    app.errors.clear()
    assert asked                       # Save As was offered (declined)
    assert s.dirty and s.edit.dirty and not s.edit.busy and s.edit.available
    assert s._change_paused == 0 and s._change_timer.isActive()
    assert json.loads(path.read_text(encoding="utf-8"))[1]["id"] == 1


def test_save_questions_brace_list_and_broken_lines(qtbot, make, tmp_path, yes):
    braces = tmp_path / "b.json"
    braces.write_text('{{"id": 1}, {"id": 2}}', encoding="utf-8")
    s = open_editable(qtbot, make, braces)
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), "5")
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.edit.save()
    assert json.loads(braces.read_text(encoding="utf-8")) == [{"id": 5}, {"id": 2}]
    assert len(yes) == 1
    lines = tmp_path / "l.jsonl"
    lines.write_text('{"id": 1}\nbroken line\n{"id": 2}\n', encoding="utf-8")
    t = open_editable(qtbot, make, lines)
    assert t.doc.broken_count == 1
    run(qtbot, t.edit.set_value, NodeRef(1, ("id",)), "7")
    with qtbot.waitSignal(t.index_finished, timeout=TIMEOUT):
        t.edit.save()
    assert lines.read_text(encoding="utf-8") == '{"id": 1}\n{"id": 7}\n'
    assert len(yes) == 2


def test_nothing_to_save(qtbot, make, app, array_file):
    s = open_editable(qtbot, make, array_file)
    got: list = []
    s.edit.save(on_done=got.append)
    assert got == [None] and app.statuses


# ----------------------------------------------------------------------------------------
# change check, schema, snapshot
# ----------------------------------------------------------------------------------------
def test_change_check_pause(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    changed: list = []
    s.file_changed.connect(changed.append)
    s.pause_change_check()
    assert not s._change_timer.isActive()
    with open(array_file, "r+b") as f:
        f.seek(5)
        f.write(b"9")
    s._check_change()                       # paused: nothing runs
    qtbot.wait(200)
    assert changed == []
    s.resume_change_check()
    assert s._change_timer.isActive()
    s._check_change()
    qtbot.waitUntil(lambda: changed == ["changed"], timeout=TIMEOUT)
    qtbot.waitUntil(lambda: s.edit.reason == "file_changed", timeout=TIMEOUT)


def test_schema_is_stale_after_edits_and_forgotten_after_bulk(qtbot, make, array_file):
    from jsonreshape.bulk import BulkSpec

    s = open_editable(qtbot, make, array_file)
    schemas: list = []
    fields.get_schema(s, schemas.append)
    qtbot.waitUntil(lambda: bool(schemas), timeout=TIMEOUT)
    assert not fields.is_stale(s) and fields.schema_gen(s) == s.data_gen
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), '"x"')
    assert fields.is_stale(s) and fields.cached_schema(s) is schemas[0]
    previews: list = []
    s.edit.bulk_preview(BulkSpec("delete_field", {"path": "name"}), None, previews.append)
    qtbot.waitUntil(lambda: bool(previews), timeout=TIMEOUT)
    run(qtbot, s.edit.bulk_apply, previews[0])
    assert fields.cached_schema(s) is None and not fields.is_stale(s)


def test_keep_document_for_with_a_pinned_document(qtbot, make, app, array_file):
    s = open_editable(qtbot, make, array_file)
    pinned, view = s.snapshot()
    done: list = []
    task = app.tasks.run(lambda ctx: time.sleep(0.2) or pinned.record(0), title="t", kind="export",
                         on_done=done.append)
    s.keep_document_for(task, sets=[view], doc=pinned)
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), "77")
    qtbot.waitUntil(lambda: bool(done), timeout=TIMEOUT)
    assert done[0]["id"] == 0                    # the pinned state: before the edit
    assert s.doc.pinned().record(0)["id"] == 77


def test_edit_layer_notices_join_the_document_warnings(qtbot, make, array_file, monkeypatch):
    from jsonreshape import editstore

    notes = ["Tahrir qatlami fayl yonida saqlanmadi: ilova papkasi ishlatiladi."]

    def take() -> list:        # like editstore.notice_texts: each notice once
        out = list(notes)
        notes.clear()
        return out

    monkeypatch.setattr(editstore, "notice_texts", take)
    seen: list = []
    s = make(array_file)
    s.warnings_changed.connect(lambda: seen.append(list(s.warning_items())))
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: ("edit", "Tahrir qatlami fayl yonida saqlanmadi: ilova papkasi ishlatiladi.")
                    in s.warning_items(), timeout=TIMEOUT)
    assert any(("edit", t_) in items for items in seen for t_ in [
        "Tahrir qatlami fayl yonida saqlanmadi: ilova papkasi ishlatiladi."])


def test_close_after_waits_for_saves_without_a_limit():
    import threading

    joined: list = []
    done = threading.Event()

    class FakeTask:
        def __init__(self, kind):
            self.kind = kind
            self.thread_alive = True

        def join(self, timeout=None):
            joined.append((self.kind, timeout))
            return True

    class FakeSet:
        def close(self):
            done.set()

    lease = session_mod._Lease()
    assert lease.thread_alive and lease.kind == "lease"
    lease.release()
    assert not lease.thread_alive and lease.join(0)
    session_mod._close_after([FakeTask("save"), FakeTask("export"), FakeTask("edit")], [], [FakeSet()])
    assert done.wait(10)
    assert joined == [("save", None), ("export", None), ("edit", 120)]


def test_edit_signals_across_reopen(qtbot, make, array_file):
    s = open_editable(qtbot, make, array_file)
    states: list = []
    s.edit_state_changed.connect(lambda: states.append(s.edit.busy if s.edit is not None else None))
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), "1")
    assert True in states and False in states
    run(qtbot, s.edit.undo)
    first = s.edit
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    assert first.closed and s.edit is not None and s.edit is not first
    qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
    n = len(states)
    run(qtbot, s.edit.set_value, NodeRef(0, ("id",)), "2")
    assert len(states) > n
