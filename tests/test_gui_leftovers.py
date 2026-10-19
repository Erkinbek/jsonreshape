"""L2 leftovers -- GUI follow-ups of the stage-5 rounds (jsonreshape/gui/rawview.py, validatepanel.py, editors.py,
filetoolsdialog.py, diffview.py, searchpanel.py, repairdialog.py).

1. Raw view (review CL-9): the current node is resolved in the document state pinned when it was asked for -- a
   ChangeSet applied before the worker starts never makes it read another state.
2. The JSON Schema generate dialog stores its scan in the shared schema cache (``fields.store``): a full scan as the
   full analysis, a sample as the sample; a scan of data edited meanwhile is not stored.
3. ``InlineEditor`` skips the text area the grid reports publicly (``VirtualGrid.text_rect``).
4. Split: "Eski qismlarni o'chirish" (``SplitOptions.remove_stale``, off by default, never remembered) and the removed
   files in the report; the plan's part names come from ``filetools.part_names``; ``filetools.error.part_open_edited``
   of a split / merge is told in the status bar and the dialog, never in an error window.
5. ``diffview.align_lines`` uses the public ``diff.align`` / ``OP_*``.
6. Outputs onto a file another tab shows (review S3, ``editing.target_conflict``): split parts, the merge output, the
   search-hit export, the issue export, the generated schema, the repair target and the saved repair report, the
   diff report -- refused while that tab has unsaved changes or another window of the program edits the file;
   asked (yes / no) when the tab shows it unchanged, and that tab opens the new file afterwards. A file whose unsaved
   changes are kept for recovery (no tab, no lock) gets the verdict ``pending`` and is refused too (Save As and every
   tool output).

Real sessions with edit controllers (offscreen, headless app); questions answered through ``widgets.confirm_handler``;
slow parts held on ``threading.Event`` gates, never on sleeps.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from jsonreshape import diff, filetools, jschema  # noqa: E402
from jsonreshape.edits import EditSession  # noqa: E402
from jsonreshape.editstore import LayerLock, file_lock_key  # noqa: E402
from jsonreshape.gui import comparewindow as cw  # noqa: E402
from jsonreshape.gui import diffview, editing, fields, rawview, widgets  # noqa: E402
from jsonreshape.gui import filetoolsdialog as ftd  # noqa: E402
from jsonreshape.gui import validatepanel as vp  # noqa: E402
from jsonreshape.gui.editors import InlineEditor  # noqa: E402
from jsonreshape.gui.rawview import RawView  # noqa: E402
from jsonreshape.gui.repairdialog import RepairDialog  # noqa: E402
from jsonreshape.gui.searchpanel import SearchPanel, target_conflict_text  # noqa: E402
from jsonreshape.gui.session import NodeRef  # noqa: E402
from jsonreshape.gui.validatepanel import GenerateSchemaDialog, ValidatePanel  # noqa: E402
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid  # noqa: E402
from jsonreshape.i18n import t  # noqa: E402
from jsonreshape.reader import Document  # noqa: E402
from jsonreshape.schema import infer_schema  # noqa: E402

from views_edit_helpers import (  # noqa: E402,F401
    TIMEOUT,
    app,
    applied,
    keep_selection,
    no_logged_errors,
    opened,
    records,
    views,
    write,
    write_json,
)

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("no_logged_errors")]

NUMBERS = ("1.10", "1e400", "12345678901234567890", "-0.0", "0.1")


# ----------------------------------------------------------------------------------------
# data, fixtures, helpers
# ----------------------------------------------------------------------------------------
def people_text(n: int, src: str = "A") -> str:
    """JSON text of ``n`` records with Uzbek Latin / Cyrillic texts, an apostrophe and exact number tokens."""
    rows = []
    for i in range(n):
        body = json.dumps({"id": i, "name": f"Ism {i} oʻzbek", "src": src, "city": ["Toshkent", "Самарқанд",
                                                                                    "Farg'ona"][i % 3]},
                          ensure_ascii=False)
        rows.append(body[:-1] + f', "n": {NUMBERS[i % len(NUMBERS)]}}}')
    return "[\n" + ",\n".join(rows) + "\n]"


def people(path: Path, n: int, src: str = "A") -> Path:
    return write(path, people_text(n, src))


@pytest.fixture(autouse=True)
def _windows(app, monkeypatch):
    """Fast split plans; report windows kept alive by the runners are closed before the app shuts down."""
    monkeypatch.setattr(ftd, "PLAN_DELAY_MS", 5)
    yield
    for w in list(widgets._kept_alive):
        w.close()


class Questions:
    """``widgets.confirm_handler``: every question is recorded; the answers are popped (default yes)."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.answers: list[bool] = []


@pytest.fixture
def questions(monkeypatch) -> Questions:
    q = Questions()

    def handler(_parent: Any, text: str, _title: str) -> bool:
        q.asked.append(text)
        return q.answers.pop(0) if q.answers else True

    monkeypatch.setattr(widgets, "confirm_handler", handler)
    return q


def refusal(what: str, path: Path) -> str:
    return target_conflict_text(what, path)


def open_question(path: Path) -> str:
    return t("gui.edit.target.open", path=os.path.normpath(str(path)))


def wait_reloaded(qtbot: Any, s: Any, old_doc: Any) -> None:
    """``s`` opened its file again (a new document, indexed) and can be edited."""
    qtbot.waitUntil(lambda: s.doc is not None and s.doc is not old_doc and s.state == "ready", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)


def make_dirty(qtbot: Any, s: Any) -> None:
    """An unsaved change in the tab ``s`` (its records hold a ``name`` member)."""
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(0, ("name",)), '"saqlanmagan oʻzgarish"')
    assert s.dirty


def pending_layer(path: Path) -> Document:
    """``path`` gets an edit layer with one change kept for recovery (no tab shows it, nobody holds its lock)."""
    doc = Document.open(path)
    doc.build_index()
    sess = EditSession.open(doc)
    sess.set_value((0, ("name",)), '"tiklash uchun saqlangan"')
    sess.close()                     # the layer stays (pending)
    return doc


def drop_layer(doc: Document) -> None:
    sess = EditSession.open(doc)     # restores the pending layer
    sess.discard()
    sess.close()
    doc.close()


def held_lock(path: Path) -> LayerLock:
    """The edit lock of ``path`` held as another window of the program would hold it."""
    lock = LayerLock(file_lock_key(path))
    assert lock.try_acquire()
    return lock


# -- split / merge dialogs ---------------------------------------------------------------------------------
def split_dialog(qtbot: Any, s: Any, out: Path) -> ftd.SplitDialog:
    dlg = ftd.SplitDialog(s, open_folder=lambda _p: True, open_file=lambda _p: True, ask_folder=lambda *_a: None)
    qtbot.addWidget(dlg)
    dlg.set_out_dir(out)
    return dlg


def wait_plan(qtbot: Any, dlg: ftd.SplitDialog) -> ftd.SplitPlan | None:
    qtbot.waitUntil(lambda: not dlg.plan_pending, timeout=TIMEOUT)
    return dlg.plan


def run_split(qtbot: Any, dlg: ftd.SplitDialog) -> ftd.SplitOutcome:
    plan = wait_plan(qtbot, dlg)
    assert plan is not None, dlg.plan_errors()
    with qtbot.waitSignal(dlg.split_finished, timeout=TIMEOUT) as blocker:
        assert dlg.start_split() is not None, dlg.error_text()
    return blocker.args[0]


def merge_dialog(qtbot: Any, app: Any, inputs: list[Path], out: Path) -> ftd.MergeDialog:
    dlg = ftd.MergeDialog(app, None, open_folder=lambda _p: True, open_file=lambda _p: True,
                          ask_files=lambda *_a: [], ask_output=lambda *_a: None)
    qtbot.addWidget(dlg)
    dlg.add_files(inputs)
    dlg.set_output_path(out)
    qtbot.waitUntil(lambda: not dlg.probing and all(i.info is not None or i.error for i in dlg.inputs()),
                    timeout=TIMEOUT)
    assert dlg._validate(), dlg.error_text()
    return dlg


def part_files(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.glob("people_*.json"))}


# ==================================================================================
# 1. raw view: the node of a request is resolved in the state of the request (CL-9)
# ==================================================================================
def test_cl9_raw_view_resolves_the_node_in_the_state_of_its_request(qtbot, opened, views, tmp_path,
                                                                     monkeypatch) -> None:
    s = opened(write_json(tmp_path / "r.json", [{"id": i, "s": f"qiymat {i}"} for i in range(40)]))
    v = views(RawView, s)
    file_node = s.doc.pinned().record_node(30)
    calls: list[dict[str, Any]] = []
    entered, release = threading.Event(), threading.Event()
    real = rawview.task_node

    def late(session: Any, ref: Any, cancel: Any = None, *, doc: Any = None) -> Any:
        call: dict[str, Any] = {}
        calls.append(call)
        entered.set()                    # the worker started; it reads only after the edit was applied
        assert release.wait(20)
        node = real(session, ref, None, doc=doc)       # (the view drops this request when it follows the edit)
        call["span"] = (int(node.start), int(node.end))
        return node

    monkeypatch.setattr(rawview, "task_node", late)
    s.select(NodeRef(30, ()))
    assert entered.wait(20)
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(30, ("s",)), '"Ўзбекистон oʻzgardi"')      # record 30 lies in the edit layer now
    release.set()
    qtbot.waitUntil(lambda: "span" in calls[0], timeout=TIMEOUT)
    assert calls[0]["span"] == (file_node.start, file_node.end)     # the state the request was made in
    # the view followed the edit: the current record is an edited one -- the note, nothing highlighted
    qtbot.waitUntil(v.edited_note_shown, timeout=TIMEOUT)
    assert v.area.node_span() is None


# ==================================================================================
# 2. the generate dialog feeds the shared schema cache
# ==================================================================================
def gen_dialog(qtbot: Any, s: Any) -> GenerateSchemaDialog:
    d = GenerateSchemaDialog(s)
    qtbot.addWidget(d)
    d.show()
    return d


def generate(qtbot: Any, d: GenerateSchemaDialog, path: Path) -> Any:
    with qtbot.waitSignal(d.generated, timeout=TIMEOUT) as blocker:
        assert d.generate(path) is not None, d.error_text()
    return blocker.args[0]


def test_generate_full_scan_feeds_the_shared_schema_cache(qtbot, app, opened, tmp_path) -> None:
    s = opened(people(tmp_path / "odamlar.json", 25))
    assert fields.cached_schema(s) is None
    d = gen_dialog(qtbot, s)
    assert not d.source_buttons["cached"].isEnabled()
    d.set_source_choice("full")
    outcome = generate(qtbot, d, tmp_path / "odamlar.schema.json")
    assert outcome.source == "full" and outcome.complete and outcome.scanned == 25
    cached = fields.cached_schema(s, full=True)
    assert cached is not None and cached.complete and cached.scanned == 25 and not fields.is_stale(s)
    want = infer_schema(s.doc.pinned(), None, sample=None)
    assert [(f.text, f.count) for f in cached.fields()] == [(f.text, f.count) for f in want.fields()]
    # cached: asking for it starts no scan, and the dialog offers it for the next generation
    assert fields.get_schema(s, full=True) is cached
    assert not [task for task in app.tasks.tasks(include_silent=True) if task.kind == "schema"]
    d.refresh_sources()
    assert d.source_buttons["cached"].isEnabled() and "25" in d.source_buttons["cached"].text()


def test_generate_sample_feeds_the_sample_cache_only(qtbot, app, opened, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app.settings, "sample_size", 5)
    s = opened(people(tmp_path / "odamlar.json", 25))
    d = gen_dialog(qtbot, s)
    d.set_source_choice("sample")
    outcome = generate(qtbot, d, tmp_path / "namuna.schema.json")
    assert outcome.source == "sample" and not outcome.complete and outcome.scanned == 5
    sample = fields.cached_schema(s)
    assert sample is not None and not sample.complete and sample.scanned == 5
    assert fields.cached_schema(s, full=True) is None             # never taken for a full analysis


def test_generate_scan_of_data_edited_meanwhile_is_not_cached(qtbot, app, opened, tmp_path, monkeypatch) -> None:
    s = opened(people(tmp_path / "odamlar.json", 25))
    entered, release = threading.Event(), threading.Event()
    real = vp.infer_schema

    def late(*args: Any, **kw: Any) -> Any:
        entered.set()
        assert release.wait(20)
        return real(*args, **kw)

    monkeypatch.setattr(vp, "infer_schema", late)
    d = gen_dialog(qtbot, s)
    d.set_source_choice("full")
    out = tmp_path / "eski.schema.json"
    task = d.generate(out)
    assert task is not None and entered.wait(20)
    make_dirty(qtbot, s)                       # the scan reads the pinned state from before this edit
    release.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert task.state == "done" and out.exists()
    assert fields.cached_schema(s) is None


# ==================================================================================
# 3. the inline editor's inset is the grid's public text rect
# ==================================================================================
class _Rows:
    def row_count(self) -> int:
        return 40

    def cell(self, row: int, column: Column) -> Cell | None:
        if row == 7:
            return None                              # not loaded yet
        if column.id == "key":
            return Cell(text=f"kalit{row}", depth=3, badge="string", expander=0, swatch=(200, 30, 30, 255))
        return Cell(text=f"qiymat {row}")


class _ShiftedGrid(VirtualGrid):
    """A grid whose text starts further right (as a grid drawing more before the text would report it)."""

    SHIFT = 40

    def text_rect(self, row: int, col: int) -> Any:
        rect = super().text_rect(row, col)
        return rect if rect.isNull() else rect.translated(self.SHIFT, 0)


def test_inline_editor_inset_is_the_grids_text_rect(qtbot) -> None:
    grid = _ShiftedGrid()
    grid.set_source(_Rows())
    grid.set_columns([Column("key", "Kalit", 260), Column("value", "Qiymat", 220)])
    grid.set_tree_column(0)
    grid.resize(560, 360)
    qtbot.addWidget(grid)
    grid.show()
    qtbot.waitExposed(grid)
    made: list[InlineEditor] = []

    def editor(row: int, col: int, **kw: Any) -> InlineEditor:
        ed = InlineEditor(grid, row, col, "key", f"kalit{row}", lambda _v: None, **kw)
        made.append(ed)
        return ed

    text = grid.text_rect(2, 0)
    drawn = VirtualGrid.text_rect(grid, 2, 0)                      # what the base grid computes
    assert not text.isNull() and text.x() == drawn.x() + _ShiftedGrid.SHIFT > grid.cell_rect(2, 0).x()
    assert editor(2, 0).geometry().x() == text.x() - 3            # the badge / swatch / indentation stay visible
    assert editor(3, 1).geometry().x() == grid.cell_rect(3, 1).x()  # not the tree column: no inset
    assert editor(4, 0, inset=5).geometry().x() == grid.cell_rect(4, 0).x() + 5   # an explicit inset wins
    assert editor(7, 0).geometry().x() == grid.cell_rect(7, 0).x()  # no text rect (not loaded): no inset
    for ed in made:
        ed.cancel()


# ==================================================================================
# 4. split: the core's part names, "remove the old parts", user errors
# ==================================================================================
def test_split_plan_names_come_from_the_core(qtbot, app, opened, tmp_path, questions, monkeypatch) -> None:
    calls: list[list[Path]] = []
    real = filetools.part_names

    def spy(doc: Any, opts: Any, source: Any = None, *, count: int) -> list[Path]:
        names = real(doc, opts, source, count=count)
        calls.append(list(names))
        return names

    monkeypatch.setattr(filetools, "part_names", spy)
    s = opened(people(tmp_path / "people.json", 30))
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(qtbot, s, out)
    dlg.set_parts(12, "records")
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and calls and plan.names == calls[-1]
    assert [p.name for p in plan.names][:2] == ["people_01.json", "people_02.json"]
    dlg.set_format("jsonl")                                         # the writer's parts: their format's extension
    plan = wait_plan(qtbot, dlg)
    assert plan.names == calls[-1] and plan.names[0].name == "people_01.jsonl"
    outcome = run_split(qtbot, dlg)
    assert outcome.report.files == plan.names


def test_split_removes_the_old_parts_only_when_asked(qtbot, app, opened, tmp_path, questions) -> None:
    s = opened(people(tmp_path / "people.json", 30))
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(qtbot, s, out)
    check = dlg.remove_stale_check
    assert not check.isChecked() and check.toolTip() == t("gui.filetools.split.remove_stale.tip")
    assert check.text() == t("gui.filetools.split.remove_stale")
    dlg.set_parts(4, "records")
    run_split(qtbot, dlg)
    assert sorted(part_files(out)) == [f"people_{k}.json" for k in range(1, 5)]
    # two parts without the option: the rest of the earlier split stays and is named in a warning
    dlg.set_parts(2, "records")
    kept = run_split(qtbot, dlg)
    assert kept.report.removed == [] and [p.name for p in kept.report.stale] == ["people_3.json", "people_4.json"]
    assert "filetools.warn.stale_parts" in kept.report.warning_keys
    assert dlg.report_dialog.removed() == [] and dlg.report_dialog.removed_view is None
    # with the option: deleted in the same commit, listed in the report
    check.setChecked(True)
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and plan.opts.remove_stale
    removed = run_split(qtbot, dlg)
    assert [p.name for p in removed.report.removed] == ["people_3.json", "people_4.json"]
    assert sorted(part_files(out)) == ["people_1.json", "people_2.json"]
    rep = dlg.report_dialog
    assert rep.removed() == [out / "people_3.json", out / "people_4.json"]
    assert rep.removed_view is not None and str(out / "people_4.json") in rep.removed_view.toPlainText()
    assert t("gui.filetools.report.removed", count=2) in rep.report_text()
    rows = [r for name in ("people_1.json", "people_2.json") for r in json.loads((out / name).read_text("utf-8"))]
    assert [r["id"] for r in rows] == list(range(30))
    # deleting files is never remembered: a new dialog starts with the option off
    dlg.close()
    again = split_dialog(qtbot, s, out)
    assert not again.remove_stale_check.isChecked() and not again.split_options().remove_stale


def test_split_onto_a_part_with_pending_edits_is_a_user_error(qtbot, app, opened, tmp_path, questions) -> None:
    s = opened(people(tmp_path / "people.json", 20))
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(qtbot, s, out)
    dlg.set_parts(2, "records")
    run_split(qtbot, dlg)
    before = part_files(out)
    held = pending_layer(out / "people_1.json")
    try:
        dlg.set_parts(2, "bytes")
        plan = wait_plan(qtbot, dlg)
        assert plan is not None and plan.existing and plan.locked == []     # no tab, no lock: the core refuses
        with qtbot.waitSignal(dlg.split_failed, timeout=TIMEOUT) as blocker:
            assert dlg.start_split() is not None, dlg.error_text()
        exc = blocker.args[0]
        assert exc.key == "filetools.error.part_open_edited" and "people_1.json" in exc.message
        assert app.errors == []                                            # no error window
        assert app.statuses[-1] == t("gui.filetools.status.refused", error=exc.message)
        status = dlg.status_label.text()
        assert status.startswith(t("gui.filetools.status.refused", error=exc.message))
        assert t("filetools.error.part_open_edited.hint") in status
        assert part_files(out) == before
    finally:
        drop_layer(held)


def test_merge_refused_by_the_core_is_a_user_error(qtbot, app, tmp_path, questions, monkeypatch) -> None:
    """The output got unsaved edits after the dialog checked it (simulated: the check saw nothing): the core
    refuses (``part_open_edited``) -- told in the status bar and the dialog, no error window."""
    a = people(tmp_path / "a.json", 3)
    out = people(tmp_path / "out.json", 2, "OUT")
    before = out.read_bytes()
    held = pending_layer(out)
    monkeypatch.setattr(ftd, "open_target_check", lambda *_args, **_kw: [])
    try:
        dlg = merge_dialog(qtbot, app, [a], out)
        with qtbot.waitSignal(dlg.merge_failed, timeout=TIMEOUT) as blocker:
            assert dlg.start_merge() is not None, dlg.error_text()
        exc = blocker.args[0]
        assert exc.key == "filetools.error.part_open_edited"
        assert app.errors == [] and app.statuses[-1] == t("gui.filetools.status.refused", error=exc.message)
        assert dlg.status_label.text().startswith(t("gui.filetools.status.refused", error=exc.message))
        assert out.read_bytes() == before
    finally:
        drop_layer(held)


# ==================================================================================
# 5. diffview: the public alignment
# ==================================================================================
def test_align_lines_uses_the_public_alignment(monkeypatch) -> None:
    def private(*_args: Any, **_kw: Any) -> Any:
        raise AssertionError("the private alias diff._align is used")

    monkeypatch.setattr(diff, "_align", private)
    la, lb, st = diffview.align_lines("a\nb\nc\nd", "a\nB\nc\nd\ne")
    assert st == ["equal", "changed", "equal", "equal", "added"]
    assert la == ["a", "b", "c", "d", ""] and lb == ["a", "B", "c", "d", "e"]
    assert diffview.align_lines("x\nOʻzbek\ny", "x\ny") == (["x", "Oʻzbek", "y"], ["x", "", "y"],
                                                          ["equal", "removed", "equal"])


# ==================================================================================
# 6. outputs onto a file another tab shows (review S3)
# ==================================================================================
def test_split_parts_shown_in_another_tab(qtbot, app, opened, tmp_path, questions) -> None:
    s = opened(people(tmp_path / "people.json", 20))
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(qtbot, s, out)
    dlg.set_parts(2, "records")
    run_split(qtbot, dlg)
    part1 = out / "people_1.json"
    tab = opened(part1)                                             # shows part 1, unchanged
    old_doc = tab.doc
    dlg.set_parts(4, "records")
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and plan.locked == [part1]              # (its edit session holds the file's lock)
    question = t("gui.filetools.split.overwrite_open", count=2, names="people_1.json, people_2.json", open=1,
                 tabs="people_1.json")
    # "no": nothing is written
    questions.asked.clear()
    questions.answers = [False]
    before = part_files(out)
    assert dlg.start_split() is None
    assert questions.asked == [question] and part_files(out) == before and tab.doc is old_doc
    # "yes": one question (it replaces the overwrite question); the tab opens the new part 1
    questions.asked.clear()
    with qtbot.waitSignal(dlg.split_finished, timeout=TIMEOUT):
        assert dlg.start_split() is not None, dlg.error_text()
    assert questions.asked == [question]
    wait_reloaded(qtbot, tab, old_doc)
    assert [r["id"] for r in records(tab)] == [0, 1, 2, 3, 4]
    # unsaved changes in that tab: refused, nothing asked or written
    make_dirty(qtbot, tab)
    dlg.set_parts(2, "records")
    wait_plan(qtbot, dlg)
    questions.asked.clear()
    before = part_files(out)
    assert dlg.start_split() is None
    assert refusal("unsaved", part1) in dlg.error_text() and questions.asked == []
    assert part_files(out) == before and tab.dirty and not dlg.running
    # the refusal goes when the inputs change
    dlg.set_parts(3, "records")
    wait_plan(qtbot, dlg)
    assert refusal("unsaved", part1) not in dlg.error_text()


def test_split_part_edited_in_another_window_is_refused(qtbot, app, opened, tmp_path, questions) -> None:
    s = opened(people(tmp_path / "people.json", 20))
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(qtbot, s, out)
    dlg.set_parts(2, "records")
    run_split(qtbot, dlg)
    part2 = out / "people_2.json"
    before = part_files(out)
    lock = held_lock(part2)
    try:
        dlg.plan_now()
        plan = wait_plan(qtbot, dlg)
        assert plan is not None and plan.locked == [part2]
        questions.asked.clear()
        assert dlg.start_split() is None
        assert refusal("locked", part2) in dlg.error_text() and questions.asked == []
        assert part_files(out) == before
    finally:
        lock.release()


def test_merge_output_shown_in_another_tab(qtbot, app, opened, tmp_path, questions) -> None:
    a = people(tmp_path / "a.json", 3, "A")
    out = people(tmp_path / "out.json", 2, "OUT")
    tab = opened(out)
    old_doc = tab.doc
    dlg = merge_dialog(qtbot, app, [a], out)
    questions.answers = [False]
    assert dlg.start_merge() is None
    assert questions.asked == [open_question(out)] and tab.doc is old_doc
    questions.asked.clear()
    with qtbot.waitSignal(dlg.merge_finished, timeout=TIMEOUT):
        assert dlg.start_merge() is not None, dlg.error_text()
    assert questions.asked == [open_question(out)]                  # no "replace the file?" question after it
    wait_reloaded(qtbot, tab, old_doc)
    assert [r["src"] for r in records(tab)] == ["A", "A", "A"]
    # unsaved changes in that tab: refused
    make_dirty(qtbot, tab)
    qtbot.waitUntil(lambda: dlg.run_button.isEnabled(), timeout=TIMEOUT)
    questions.asked.clear()
    before = out.read_bytes()
    assert dlg.start_merge() is None
    assert refusal("unsaved", out) in dlg.error_text() and questions.asked == []
    assert out.read_bytes() == before and tab.dirty
    # another window edits the output: refused
    other = people(tmp_path / "other.json", 1, "X")
    lock = held_lock(other)
    try:
        dlg.set_output_path(other)
        assert dlg._validate()
        assert dlg.start_merge() is None and refusal("locked", other) in dlg.error_text()
    finally:
        lock.release()


def test_search_export_onto_a_file_another_tab_shows(qtbot, app, opened, tmp_path, questions) -> None:
    s = opened(people(tmp_path / "data.json", 30))
    target = write_json(tmp_path / "hits.json", [{"eski": True}])
    tab = opened(target)
    old_doc = tab.doc
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    try:
        assert app.tasks.wait_idle(10)
        panel.set_session(s)
        panel.set_options({"text": "Toshkent", "mode": "plain"})
        with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
            assert panel.run_search() is not None, panel.error_text()
        questions.answers = [False]
        assert panel.export_hits_to(str(target)) is None
        assert questions.asked == [open_question(target)] and tab.doc is old_doc
        with qtbot.waitSignal(panel.export_finished, timeout=TIMEOUT):
            assert panel.export_hits_to(str(target)) is not None, panel.error_text()
        hits = json.loads(target.read_text(encoding="utf-8"))
        assert len(hits) == 10
        wait_reloaded(qtbot, tab, old_doc)
        assert [r["text"] for r in records(tab)] == [h["text"] for h in hits]
        with keep_selection(tab), applied(qtbot, tab):          # unsaved changes in that tab: refused
            tab.edit.set_value(NodeRef(0, ("text",)), '"oʻzgardi"')
        questions.asked.clear()
        before = target.read_bytes()
        assert panel.export_hits_to(str(target)) is None
        assert refusal("unsaved", target) in panel.error_text() and questions.asked == []
        assert target.read_bytes() == before
    finally:
        panel.shutdown()


def test_issue_export_onto_a_file_another_tab_shows(qtbot, app, opened, tmp_path, questions) -> None:
    s = opened(people(tmp_path / "data.json", 12))
    schema = write_json(tmp_path / "talab.schema.json", {"type": "object", "required": ["yoq"]})
    target = write(tmp_path / "xatolar.jsonl", '{"eski": 1}\n')
    tab = opened(target)
    old_doc = tab.doc
    panel = ValidatePanel(app)
    qtbot.addWidget(panel)
    try:
        panel.set_session(s)
        panel.set_schema_path(schema)
        questions.answers = [False]
        assert panel.export_issues_to(target) is None
        assert questions.asked == [open_question(target)] and tab.doc is old_doc
        with qtbot.waitSignal(panel.export_finished, timeout=TIMEOUT) as blocker:
            assert panel.export_issues_to(target) is not None, panel.error_text()
        assert blocker.args[1] == 12
        wait_reloaded(qtbot, tab, old_doc)
        rows = records(tab)
        assert len(rows) == 12 and {r["keyword"] for r in rows} == {"required"}
        with keep_selection(tab), applied(qtbot, tab):
            tab.edit.set_value(NodeRef(0, ("keyword",)), '"oʻzgardi"')
        questions.asked.clear()
        before = target.read_bytes()
        assert panel.export_issues_to(target) is None
        assert refusal("unsaved", target) in panel.error_text() and questions.asked == []
        assert target.read_bytes() == before
    finally:
        panel.shutdown()


def test_generated_schema_onto_a_file_another_tab_shows(qtbot, app, opened, tmp_path, questions) -> None:
    s = opened(people(tmp_path / "odamlar.json", 10))
    target = write_json(tmp_path / "odamlar.schema.json", {"title": "eski"})
    tab = opened(target)
    old_doc = tab.doc
    d = gen_dialog(qtbot, s)
    d.set_source_choice("full")
    questions.answers = [False]
    assert d.generate(target) is None
    assert questions.asked == [open_question(target)] and tab.doc is old_doc and d.task is None
    questions.asked.clear()
    generate(qtbot, d, target)
    assert questions.asked == [open_question(target)]
    assert jschema.load_schema(target)["$comment"] == jschema.SCOPE_DOCUMENT
    wait_reloaded(qtbot, tab, old_doc)
    shown = str(records(tab))                     # (the new file may open as another kind: the text decides)
    assert "eski" not in shown and "city" in shown and tab.doc.data_size == target.stat().st_size
    # a tab with unsaved changes: refused
    busy = people(tmp_path / "band.json", 3)
    busy_tab = opened(busy)
    make_dirty(qtbot, busy_tab)
    questions.asked.clear()
    before = busy.read_bytes()
    assert d.generate(busy) is None
    assert refusal("unsaved", busy) in d.error_text() and questions.asked == []
    assert busy.read_bytes() == before
    # another window edits the file: refused
    other = write_json(tmp_path / "boshqa.schema.json", {})
    lock = held_lock(other)
    try:
        assert d.generate(other) is None and refusal("locked", other) in d.error_text()
        assert other.read_bytes() == b"{}"
    finally:
        lock.release()


def finish(qtbot: Any, d: RepairDialog, start: Any) -> Any:
    ended: list = []
    signals = (d.run_finished, d.run_failed, d.run_cancelled)
    for sig in signals:
        sig.connect(ended.append)
    try:
        assert start() is not None, d.error_text()
        qtbot.waitUntil(lambda: bool(ended), timeout=TIMEOUT)
    finally:
        for sig in signals:
            sig.disconnect(ended.append)
    return d.report


def test_repair_target_and_report_shown_in_other_tabs(qtbot, app, opened, tmp_path, questions) -> None:
    broken = write(tmp_path / "buzuq.json", '[{"a": 1.10,}, {"b": "Самарқанд"}]')
    target = write_json(tmp_path / "buzuq.repaired.json", [{"eski": True}])
    tab = opened(target)
    old_doc = tab.doc
    d = RepairDialog(app, str(broken), target=str(target))
    qtbot.addWidget(d)
    d.show()
    try:
        questions.answers = [False]
        assert d.repair() is None
        assert questions.asked == [open_question(target)] and tab.doc is old_doc
        questions.asked.clear()
        rep = finish(qtbot, d, d.repair)
        assert rep is not None and rep.written
        assert questions.asked == [open_question(target)]           # instead of the overwrite question
        wait_reloaded(qtbot, tab, old_doc)
        rows = records(tab)
        assert len(rows) == 2 and list(rows[0]) == ["a"] and rows[1] == {"b": "Самарқанд"}
        assert target.read_bytes().count(b"1.10") == 1                # the exact token
        # the saved report onto a file another tab shows
        report_file = write(tmp_path / "hisobot.jsonl", '{"eski": 1}\n')
        report_tab = opened(report_file)
        old_report_doc = report_tab.doc
        questions.asked.clear()
        with qtbot.waitSignal(d.report_saved, timeout=TIMEOUT):
            assert d.save_report(report_file) is not None, d.error_text()
        assert questions.asked == [open_question(report_file)]
        wait_reloaded(qtbot, report_tab, old_report_doc)
        fixes = records(report_tab)
        assert len(fixes) == rep.total >= 1 and all("code" in f and "eski" not in f for f in fixes)
        # unsaved changes in those tabs: refused, nothing written
        with keep_selection(report_tab), applied(qtbot, report_tab):
            report_tab.edit.set_value(NodeRef(0, ("code",)), '"oʻzgardi"')
        questions.asked.clear()
        before = report_file.read_bytes()
        assert d.save_report(report_file) is None
        assert refusal("unsaved", report_file) in d.error_text() and questions.asked == []
        assert report_file.read_bytes() == before
        with keep_selection(tab), applied(qtbot, tab):
            tab.edit.set_value(NodeRef(0, ("a",)), "2.50")
        before = target.read_bytes()
        assert d.repair() is None
        assert refusal("unsaved", target) in d.error_text() and questions.asked == []
        assert target.read_bytes() == before
    finally:
        d.done(0)


def test_diff_report_onto_a_file_another_tab_shows(qtbot, app, opened, tmp_path, questions) -> None:
    a = people(tmp_path / "a.json", 6)
    b = write(tmp_path / "b.json", people_text(6).replace('"Ism 2 oʻzbek"', '"oʻzgardi"'))
    win = cw.CompareWindow(app)
    win.resize(1000, 700)
    win.show()
    try:
        with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT):
            assert win.compare_files(cw.CompareSpec(cw.FileSide(path=str(a)), cw.FileSide(path=str(b)),
                                                    mode="key", key="id")) is not None
        target = write_json(tmp_path / "farq.json", [{"eski": True}])
        tab = opened(target)
        old_doc = tab.doc
        questions.answers = [False]
        assert win.export_report(str(target)) is None
        assert questions.asked == [open_question(target)] and tab.doc is old_doc
        with qtbot.waitSignal(win.export_finished, timeout=TIMEOUT) as blocker:
            assert win.export_report(str(target)) is not None, win.summary.full_text()
        assert blocker.args[0] == 1
        wait_reloaded(qtbot, tab, old_doc)
        shown = str(records(tab))
        assert "eski" not in shown and "changed" in shown and "oʻzgardi" in shown
        # a tab with unsaved changes / changes kept for recovery: refused, nothing asked
        busy = people(tmp_path / "band.json", 2)
        make_dirty(qtbot, opened(busy))
        kept = people(tmp_path / "kept.json", 2)
        held = pending_layer(kept)
        try:
            questions.asked.clear()
            assert win.export_report(str(busy)) is None
            assert refusal("unsaved", busy) in win.summary.full_text()
            assert win.export_report(str(kept)) is None
            assert refusal("pending", kept) in win.summary.full_text() and questions.asked == []
        finally:
            drop_layer(held)
    finally:
        win.close()


# ----------------------------------------------------------------------------------------
# 6b. unsaved changes kept for recovery (no tab, no lock): editing.target_conflict's verdict "pending"
# ----------------------------------------------------------------------------------------
def test_target_conflict_sees_changes_kept_for_recovery(qtbot, opened, tmp_path) -> None:
    kept = people(tmp_path / "kept.json", 3)
    assert editing.target_conflict(kept) is None
    held = pending_layer(kept)
    try:
        assert editing.target_conflict(kept) == ("pending", [])
        assert editing.target_conflict(tmp_path / "yoq.json") is None           # (no such file)
    finally:
        drop_layer(held)
    assert editing.target_conflict(kept) is None
    # a tab: unchanged -> "open"; changed -> "unsaved"; every change undone, its edit layer is still there (the
    # writer would refuse the file) -> "unsaved", naming that tab
    path = people(tmp_path / "tab.json", 3)
    tab = opened(path)
    assert editing.target_conflict(path) == ("open", [tab])
    make_dirty(qtbot, tab)
    assert editing.target_conflict(path) == ("unsaved", [tab])
    with keep_selection(tab), applied(qtbot, tab):
        tab.edit.undo()
    assert not tab.dirty
    assert editing.target_conflict(path) == ("unsaved", [tab])
    assert editing.target_conflict(path, exclude=tab) is None                 # the document's own file


def test_save_as_onto_a_file_with_changes_kept_for_recovery_is_refused(qtbot, app, opened, tmp_path) -> None:
    a = opened(people(tmp_path / "a.json", 4))
    make_dirty(qtbot, a)
    kept = people(tmp_path / "kept.json", 2, "KEPT")
    before = kept.read_bytes()
    held = pending_layer(kept)
    try:
        reports: list = []
        a.edit.save_as(kept, on_done=reports.append)
        qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
        assert reports == [None] and kept.read_bytes() == before and a.dirty
        assert [getattr(e, "key", "") for e in app.errors] == ["gui.edit.target.pending"]
        assert os.path.normpath(str(kept)) in app.errors[0].message
        app.errors.clear()
    finally:
        drop_layer(held)


def test_tool_outputs_with_changes_kept_for_recovery_are_refused(qtbot, app, opened, tmp_path, questions) -> None:
    """Search-hit export, issue export, generated schema, repair target and report, merge output: refused before
    anything starts (most of them are written without the writer, which would refuse such a file itself)."""
    s = opened(people(tmp_path / "data.json", 12))
    kept = people(tmp_path / "kept.json", 2, "KEPT")
    before = kept.read_bytes()
    text = refusal("pending", kept)
    held = pending_layer(kept)
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    vpanel = ValidatePanel(app)
    qtbot.addWidget(vpanel)
    repair = RepairDialog(app, str(write(tmp_path / "buzuq.json", '[{"a": 1,}]')), target=str(kept))
    qtbot.addWidget(repair)
    try:
        assert app.tasks.wait_idle(10)
        panel.set_session(s)
        panel.set_options({"text": "Toshkent", "mode": "plain"})
        with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
            assert panel.run_search() is not None, panel.error_text()
        assert panel.export_hits_to(str(kept)) is None and text in panel.error_text()
        vpanel.set_session(s)
        vpanel.set_schema_path(write_json(tmp_path / "s.schema.json", {"type": "object"}))
        assert vpanel.export_issues_to(kept, "jsonl") is None and text in vpanel.error_text()
        d = gen_dialog(qtbot, s)
        assert d.generate(kept) is None and text in d.error_text() and d.task is None
        repair.show()
        assert repair.repair() is None and text in repair.error_text()
        finish(qtbot, repair, repair.check)                      # a report to save (nothing written)
        assert repair.save_report(kept) is None and text in repair.error_text()
        merge = merge_dialog(qtbot, app, [people(tmp_path / "m.json", 2)], kept)
        assert merge.start_merge() is None and text in merge.error_text() and not merge.running
        assert questions.asked == [] and kept.read_bytes() == before and app.errors == []
    finally:
        panel.shutdown()
        vpanel.shutdown()
        repair.done(0)
        drop_layer(held)


def test_export_dialog_refuses_a_file_with_changes_kept_for_recovery(qtbot, app, opened, tmp_path,
                                                                      questions) -> None:
    # the export dialog used the "open in another tab -- overwrite?" question for this verdict (then the writer
    # refused with an error window): it now refuses up front with the text every tool output uses
    from jsonreshape.gui.exportdialog import ExportDialog

    a = opened(people(tmp_path / "a.json", 4))
    kept = people(tmp_path / "kept.json", 2, "KEPT")
    before = kept.read_bytes()
    held = pending_layer(kept)
    try:
        dlg = ExportDialog(a, None, scope="all")
        qtbot.addWidget(dlg)
        dlg.set_output_path(kept)
        assert dlg.start_export() is None
        assert refusal("pending", kept) in dlg.error_text()
        assert questions.asked == [] and kept.read_bytes() == before and app.errors == []
    finally:
        drop_layer(held)
