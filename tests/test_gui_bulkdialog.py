"""gui/bulkdialog.py: the bulk operations dialog (docs/ARCHITECTURE.md §12.9, §12.6).

The dialog is driven against a faithful stand-in of the §12.9 controller API (``FakeEdit``: a REAL
:class:`~jsonreshape.edits.EditSession` on a real indexed document; ``bulk_preview`` runs as a heavy task of the
application's TaskManager, ``bulk_apply`` commits asynchronously and announces the ChangeSet through the session's
``data_changed`` like ``DocSession._apply_change`` does) and a minimal session (``FakeSession``) -- so every rule of the
dialog is checked deterministically: inline validation of every operation kind, the scope choices (all records, the
current view leased with ``keep_document_for``, selections resolved lazily with :class:`SelectionScope`, stale
selections refused), Preview with progress / Stop, the exact counts / examples / problems shown, Apply only while the
preview's gen is the applied gen, staleness after another edit (files discarded at once), Close discarding the preview
(also a late result of a cancelled preview), unavailable / busy editing, one dialog per session, the huge-record note,
the remembered operation and revealing an example. ``tests/test_gui_bulkdialog_real.py`` repeats the main flow with the
real ``DocSession`` and ``EditController``.
"""

from __future__ import annotations

import threading
from array import array
from pathlib import Path
from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from jsonreshape import codec
from jsonreshape.bulk import KINDS, BulkPreview, BulkSpec
from jsonreshape.edits import EditSession, Ref
from jsonreshape.errors import Cancelled
from jsonreshape.gui.bulkdialog import BulkDialog, SelectionScope, open_bulk_dialog
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.i18n import t
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet, filter_records
from jsonreshape.settings import Settings

pytestmark = pytest.mark.gui

TIMEOUT = 30000
CITIES = ["Toshkent", "Samarqand", "Buxoro", "O'zbekiston"]


def make_text(n: int = 40) -> bytes:
    lines = []
    for i in range(n):
        city = CITIES[i % 4]
        extra = ', "ism": "x", "name2": 1' if i % 10 == 3 else ""
        lines.append(f'  {{"id": {i % 25}, "name": "{city}", "n": 1.10, "big": 12345678901234567890, '
                     f'"tags": ["a", " b  c "]{extra}}}')
    return ("[\n" + ",\n".join(lines) + "\n]\n").encode("utf-8")


# ======================================================================================
# stand-ins of the session and the controller (the §12.9 API surface the dialog uses)
# ======================================================================================
class FakeSession(QObject):
    data_changed = Signal(object)
    edit_changed = Signal()
    edit_state_changed = Signal()
    state_changed = Signal(str)
    view_changed = Signal()
    count_changed = Signal()
    index_finished = Signal()

    def __init__(self, app: Any, doc: Document, name: str = "doc.json") -> None:
        super().__init__()
        self.app = app
        self.doc = doc
        self.name = name
        self.edit: Any = None
        self.view_is_base = True
        self.view_busy = False
        self._view: RecordSet | None = None
        self.data_gen = 0
        self.kept: list[tuple] = []
        self.selected: list[tuple] = []

    def view_len(self) -> int:
        return len(self._view) if self._view is not None else self.doc.record_count

    def view_recordset(self) -> RecordSet | None:
        return self._view

    def snapshot(self) -> tuple[Any, Any]:
        return self.doc.pinned(), self._view

    def keep_document_for(self, task: Any, sets: Any = (), doc: Any = None) -> None:
        self.kept.append((task, tuple(sets), doc))

    def select(self, ref: Any, *, origin: Any = None, history: bool = True, reveal: bool = False) -> None:
        self.selected.append((ref, reveal))

    def set_view(self, rs: RecordSet | None) -> None:
        self._view = rs
        self.view_is_base = rs is None
        self.view_changed.emit()


class FakeEdit(QObject):
    """The §12.9 controller API (as gui/editing.py implements it: ``bulk_preview(spec, scope, on_done, *,
    on_failed)`` -> task | None, ``bulk_apply(preview, *, on_done) -> bool``, ``reason_text``) over a real
    EditSession; commands run in the GUI thread for simplicity, ``bulk_apply`` answers asynchronously."""

    state_changed = Signal()
    changed = Signal(object)

    def __init__(self, fs: FakeSession) -> None:
        super().__init__()
        self.fs = fs
        self.es = EditSession.open(fs.doc)
        self.available = True
        self.reason = ""
        self.reason_text = ""
        self.busy = False
        self.applied_gen = self.es.state.gen
        fs.data_gen = self.applied_gen
        self.preview_calls: list[tuple] = []
        self.apply_calls: list[Any] = []
        self.gate: threading.Event | None = None      # previews wait for it
        self.ignore_cancel = False                    # ... and then finish even when cancelled
        self.refuse = False                           # bulk_preview is refused (returns None)
        self.refuse_apply = False                     # bulk_apply is refused (returns False)

    def bulk_preview(self, spec: BulkSpec, scope: Any, on_done: Any, *, on_failed: Any = None) -> Any:
        self.preview_calls.append((spec, scope))
        if self.refuse or not self.available:
            if on_failed is not None:
                on_failed(None)
            return None
        es, gate, ignore = self.es, self.gate, self.ignore_cancel

        def work(ctx: Any) -> BulkPreview:
            if gate is not None:
                while not gate.wait(0.01):
                    if not ignore:
                        ctx.check()
            return es.bulk_preview(spec, scope, progress=ctx.progress, cancel=None if ignore else ctx.cancel)

        def failed(exc: BaseException) -> None:
            if not isinstance(exc, Cancelled):
                self.fs.app.report_error(exc)
            if on_failed is not None:
                on_failed(exc)

        def cancelled() -> None:
            if on_failed is not None:
                on_failed(None)

        return self.fs.app.tasks.run(work, title="bulk", kind="bulk", owner=self, heavy=True, on_done=on_done,
                                     on_error=failed, on_cancel=cancelled)

    def bulk_apply(self, preview: BulkPreview, *, on_done: Any = None) -> bool:
        self.apply_calls.append(preview)
        if self.refuse_apply or self.busy or getattr(preview, "gen", None) != self.applied_gen:
            if on_done is not None:
                on_done(None)
            return False
        self._busy(True)

        def run() -> None:
            try:
                cs = self.es.bulk_apply(preview)
            except Exception as exc:  # the real controller reports it too
                self._busy(False)
                self.fs.app.report_error(exc)
                if on_done is not None:
                    on_done(None)
                return
            self.apply_cs(cs)
            if on_done is not None:
                on_done(cs)

        QTimer.singleShot(0, run)
        return True

    def apply_cs(self, cs: Any) -> None:
        self.applied_gen = cs.gen
        self.fs.data_gen = cs.gen
        self.fs.data_changed.emit(cs)
        self.changed.emit(cs)
        self._busy(False)

    def set_value(self, ref: Ref, text: str) -> None:
        self.apply_cs(self.es.set_value(ref, text))

    def _busy(self, on: bool) -> None:
        self.busy = on
        self.state_changed.emit()
        self.fs.edit_state_changed.emit()


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def rig(app, tmp_path: Path, qtbot):
    made: list[tuple] = []

    def make(content: bytes | None = None, *, settings: Settings | None = None, edit: bool = True,
             name: str = "doc.json") -> tuple[FakeSession, FakeEdit | None]:
        path = tmp_path / f"{len(made)}_{name}"
        path.write_bytes(make_text() if content is None else content)
        doc = Document.open(path, settings=settings)
        doc.build_index()
        fs = FakeSession(app, doc, name)
        fe = FakeEdit(fs) if edit else None
        fs.edit = fe
        made.append((fs, fe, doc))
        return fs, fe

    yield make
    for fs, fe, doc in made:
        if fe is not None and fe.gate is not None:
            fe.gate.set()
    assert app.tasks.wait_idle(30)
    for fs, fe, doc in made:
        if fe is not None:
            fe.es.close()
        doc.close()


@pytest.fixture
def dialog(qtbot):
    def make(fs: FakeSession, scope_default: str = "all", **kw: Any) -> BulkDialog:
        dlg = BulkDialog(fs, scope_default, **kw)
        qtbot.addWidget(dlg)
        return dlg

    return make


def set_params(dlg: BulkDialog, kind: str, **params: Any) -> None:
    dlg.set_spec(BulkSpec(kind, params))


def run_preview(dlg: BulkDialog, qtbot) -> BulkPreview:
    task = dlg.preview()
    assert task is not None, dlg.status_label.text()
    qtbot.waitUntil(lambda: dlg.preview_result is not None and dlg.task is None, timeout=TIMEOUT)
    return dlg.preview_result


def table(tb) -> list[list[str]]:
    return [[tb.item(r, c).text() for c in range(tb.columnCount())] for r in range(tb.rowCount())]


def record(doc: Document, pos: int) -> Any:
    return codec.loads(doc.record_bytes(pos))


def layer_file(fe: FakeEdit, pv: BulkPreview) -> Path:
    return Path(fe.es.store.dir) / str(pv.layer)


# ======================================================================================
# the form
# ======================================================================================
@pytest.mark.parametrize("kind, bad, error_key, param, good", [
    ("replace", {"find": ""}, "bulk.error.find_empty", "find", {"find": "Toshkent", "replacement": "Tashkent"}),
    ("replace", {"find": "(", "mode": "regex"}, None, None, {"find": "(T)", "replacement": r"\1", "mode": "regex"}),
    ("replace", {"find": "a", "replacement": r"\9", "mode": "regex"}, "bulk.error.replacement", "replacement",
     {"find": "a", "replacement": "b", "path": "..name"}),
    ("rename_key", {"old": "name", "new": "name"}, "bulk.error.same_key", "new", {"old": "name", "new": "ism"}),
    ("delete_field", {"path": ""}, "bulk.error.path_required", "path", {"path": "..tags"}),
    ("set_field", {"path": "a", "value": "{oops"}, "bulk.error.value", "value",
     {"path": "meta.source", "value": '"Ўзбек"', "mode": "if_missing"}),
    ("set_field", {"path": "a[*]", "value": "1"}, None, None, {"path": "a", "value": "1"}),
    ("clean_strings", {"trim": False, "collapse_spaces": False}, "bulk.error.clean_nothing", "trim",
     {"trim": True, "collapse_spaces": False, "path": "tags"}),
    ("convert_type", {"path": "", "to": "number"}, "bulk.error.path_required", "path", {"path": "n", "to": "string"}),
    ("dedupe", {"key": "", "keep": "first"}, None, None, {"key": "id", "keep": "last"}),
])
def test_inline_validation(rig, dialog, kind: str, bad: dict, error_key: str | None, param: str | None,
                           good: dict) -> None:
    fs, _fe = rig()
    dlg = dialog(fs)
    set_params(dlg, kind, **bad)
    err = dlg.spec_error()
    assert err is not None and dlg.error_label.isVisibleTo(dlg) and dlg.error_label.text().startswith(err.message)
    if error_key is not None:
        assert err.key == error_key
    if param is not None:
        widget = dlg.fields[kind][param]
        if isinstance(widget, QLineEdit):
            assert "border" in widget.styleSheet()                     # the offending field is outlined
    assert not dlg.preview_button.isEnabled() and dlg.summary_label.text() == ""
    assert dlg.preview() is None
    set_params(dlg, kind, **good)
    assert dlg.spec_error() is None and not dlg.error_label.isVisibleTo(dlg)
    assert dlg.summary_label.text() == dlg.spec().label() and dlg.preview_button.isEnabled()


def test_spec_round_trip(rig, dialog) -> None:
    fs, _fe = rig()
    dlg = dialog(fs)
    specs = [
        BulkSpec("replace", {"find": "O'zbek", "replacement": "Oʻzbek", "mode": "regex", "case_sensitive": True,
                             "whole_word": True, "path": "..name"}),
        BulkSpec("rename_key", {"old": "name", "new": "ism", "path": "items[*]"}),
        BulkSpec("delete_field", {"path": "..tags"}),
        BulkSpec("set_field", {"path": "a.b", "value": "[1.10, 1e400]", "mode": "if_exists"}),
        BulkSpec("clean_strings", {"trim": False, "collapse_spaces": True, "path": None}),
        BulkSpec("convert_type", {"path": "$", "to": "array"}),
        BulkSpec("dedupe", {"key": "$key", "keep": "last"}),
    ]
    assert [s.kind for s in specs] == list(KINDS)
    for spec in specs:
        dlg.set_spec(spec)
        assert dlg.kind() == spec.kind and dlg.pages.currentIndex() == KINDS.index(spec.kind)
        assert dlg.spec().normalized() == spec.normalized()
    with pytest.raises(ValueError):
        dlg.set_kind("magic")


def test_scope_labels(rig, dialog) -> None:
    fs, _fe = rig()
    dlg = dialog(fs)
    assert dlg.scope_choice() == "all"
    assert dlg.scope_buttons["all"].text() == t("gui.bulk.scope.all", count="40")
    assert dlg.scope_buttons["view"].text() == t("gui.bulk.scope.view_base")
    assert not dlg.scope_buttons["selected"].isEnabled()
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected_none")
    fs.set_view(RecordSet.from_indices([1, 5, 9], gen=fs.data_gen))
    assert dlg.scope_buttons["view"].text() == t("gui.bulk.scope.view", count="3")
    dlg.set_selection_source(lambda: [2, 4])
    assert dlg.scope_buttons["selected"].isEnabled()
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected", count="2")
    dlg.set_scope_choice("selected")
    dlg.set_selection_source(None)
    assert dlg.scope_choice() == "all"
    with pytest.raises(ValueError):
        dlg.set_scope_choice("visible")


# ======================================================================================
# preview, apply, stale, stop, close
# ======================================================================================
def test_preview_shows_exact_counts_and_examples(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "replace", find="toshkent", replacement="Tashkent")
    pv = run_preview(dlg, qtbot)
    assert (pv.scanned, pv.records_changed, pv.values_changed) == (40, 10, 10)
    assert dlg.counts_label.text().splitlines()[0] == t("gui.bulk.result.counts", scanned="40", records="10",
                                                        values="10")
    rows = table(dlg.examples_table)
    assert rows[0] == ["#0", "name", '"Toshkent"', '"Tashkent"'] and len(rows) == 5
    assert [r[0] for r in rows] == ["#0", "#4", "#8", "#12", "#16"]
    assert dlg.problems_table.isHidden() and dlg.status_label.text() == t("gui.bulk.status.done", label=pv.spec.label())
    assert dlg.can_apply() and dlg.apply_button.isEnabled()
    assert fs.kept and fs.kept[-1][0] is not None                    # the task's document is leased
    assert fe.preview_calls[-1][1] is None                           # all records


def test_problems_and_warnings_are_listed(rig, dialog, qtbot) -> None:
    fs, _fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "rename_key", old="name", new="name2")
    pv = run_preview(dlg, qtbot)
    assert pv.conflicts == 4 and pv.problems
    assert not dlg.problems_table.isHidden()
    rows = table(dlg.problems_table)
    assert rows[0][0] == "#3" and rows[0][3] == pv.problems[0].after
    assert t("gui.bulk.result.conflicts", count="4") in dlg.counts_label.text()


def test_apply_commits_one_step(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    applied: list = []
    dlg.applied.connect(applied.append)
    set_params(dlg, "set_field", path="meta.ok", value='{"v": 1.10, "s": "Ўзбек"}')
    pv = run_preview(dlg, qtbot)
    assert pv.records_changed == 40
    assert dlg.apply() is True
    assert not dlg.apply_button.isEnabled() and not dlg.preview_button.isEnabled()
    assert dlg.status_label.text() == t("gui.bulk.status.applying")
    qtbot.waitUntil(lambda: bool(applied), timeout=TIMEOUT)
    assert applied[0].kind == "bulk" and fe.apply_calls == [pv]
    assert dlg.status_label.text() == t("gui.bulk.status.applied", label=pv.spec.label())
    assert record(fs.doc, 7)["meta"] == {"ok": {"v": codec.loads("1.10"), "s": "Ўзбек"}}
    assert codec.dumps(record(fs.doc, 7)["big"]) == b"12345678901234567890"
    assert dlg.preview_result is None and not dlg.can_apply() and dlg.preview_button.isEnabled()
    assert layer_file(fe, pv).exists()                               # the step owns the files now
    fe.es.undo()
    assert "meta" not in record(fs.doc, 7)


def test_stale_preview_cannot_be_applied(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "clean_strings", trim=True, collapse_spaces=True)
    pv = run_preview(dlg, qtbot)
    assert pv.values_changed == 40 and dlg.can_apply()
    path = layer_file(fe, pv)
    assert path.exists()
    fe.set_value(Ref(0, ("name",)), '"boshqa"')                      # another edit lands
    assert not dlg.can_apply() and not dlg.apply_button.isEnabled()
    assert dlg.status_label.text() == t("gui.bulk.status.stale")
    assert pv.discarded and not path.exists()                        # its files are gone at once
    assert dlg.apply() is False and fe.apply_calls == []
    pv2 = run_preview(dlg, qtbot)                                    # preview again: fresh
    assert pv2.gen == fe.applied_gen and dlg.can_apply()


def test_preview_newer_than_the_applied_state_waits(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "convert_type", path="n", to="string")
    pv = run_preview(dlg, qtbot)
    fe.applied_gen = pv.gen - 1                                      # the GUI has not installed that state yet
    dlg._refresh_state()
    assert not dlg.can_apply() and not pv.discarded
    fe.applied_gen = pv.gen
    dlg._refresh_state()
    assert dlg.can_apply()


def test_stop_cancels_the_preview(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    fe.gate = threading.Event()
    dlg = dialog(fs)
    failed: list = []
    dlg.preview_failed.connect(failed.append)
    set_params(dlg, "delete_field", path="tags")
    task = dlg.preview()
    assert task is not None and dlg.task is task
    assert not dlg.stop_button.isHidden() and not dlg.preview_button.isEnabled() and not dlg.kind_combo.isEnabled()
    dlg.stop()
    qtbot.waitUntil(lambda: dlg.task is None, timeout=TIMEOUT)
    assert dlg.status_label.text() == t("gui.bulk.status.cancelled") and isinstance(failed[0], Cancelled)
    assert dlg.preview_result is None and dlg.stop_button.isHidden() and dlg.preview_button.isEnabled()


def test_close_discards_the_preview(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "delete_field", path="tags")
    pv = run_preview(dlg, qtbot)
    path = layer_file(fe, pv)
    assert path.exists()
    dlg.reject()
    assert pv.discarded and not path.exists() and BulkDialog.instance(fs) is None


def test_a_late_result_after_close_is_discarded(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    fe.gate = threading.Event()
    fe.ignore_cancel = True                                          # finishes although cancelled
    dlg = dialog(fs)
    set_params(dlg, "delete_field", path="tags")
    task = dlg.preview()
    seen: list = []
    fs.app.tasks.task_finished.connect(lambda tk: seen.append(tk.result) if tk is task else None)
    dlg.reject()
    assert task.cancel_requested
    fe.gate.set()
    qtbot.waitUntil(lambda: bool(seen), timeout=TIMEOUT)
    pv = seen[0]
    assert isinstance(pv, BulkPreview) and pv.records_changed == 40
    assert pv.discarded and not layer_file(fe, pv).exists()


def test_a_newer_preview_discards_the_older_one(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "delete_field", path="tags")
    pv1 = run_preview(dlg, qtbot)
    path1 = layer_file(fe, pv1)
    set_params(dlg, "delete_field", path="big")
    pv2 = run_preview(dlg, qtbot)
    assert pv1.discarded and not path1.exists() and not pv2.discarded


def test_empty_preview_cannot_be_applied(rig, dialog, qtbot) -> None:
    fs, _fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "replace", find="Andijon", replacement="x")
    pv = run_preview(dlg, qtbot)
    assert pv.empty and not dlg.can_apply()
    assert dlg.counts_label.text().splitlines()[-1] == t("gui.bulk.result.nothing")


def test_dedupe_preview_and_apply(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "dedupe", key="id", keep="first")
    pv = run_preview(dlg, qtbot)
    assert pv.records_changed == 15
    assert dlg.counts_label.text().splitlines()[0] == t("gui.bulk.result.counts_dedupe", scanned="40", records="15")
    assert table(dlg.examples_table)[0][3] == t("bulk.example.deleted")
    applied: list = []
    dlg.applied.connect(applied.append)
    assert dlg.apply()
    qtbot.waitUntil(lambda: bool(applied), timeout=TIMEOUT)
    assert fs.doc.record_count == 25 and dlg.scope_buttons["all"].text() == t("gui.bulk.scope.all", count="25")


# ======================================================================================
# scopes
# ======================================================================================
def test_current_view_scope(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    view = filter_records(fs.doc.pinned(), 'name == "Buxoro"')
    assert len(view) == 10 and view.gen == fe.applied_gen
    fs.set_view(view)
    dlg = dialog(fs, "view")
    set_params(dlg, "set_field", path="checked", value="true")
    pv = run_preview(dlg, qtbot)
    assert pv.scanned == 10 and pv.records_changed == 10
    assert fe.preview_calls[-1][1] is view and fs.kept[-1][1] == (view,)
    fs.view_busy = True
    assert dlg.preview() is None and dlg.status_label.text() == t("gui.bulk.scope.view_busy")
    fs.view_busy = False
    fs.set_view(None)                                                # no filter: all records
    run_preview(dlg, qtbot)
    assert fe.preview_calls[-1][1] is None
    view.close()


def test_selected_records_scope(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    chosen: list[Any] = [[3, 1, 3]]                                  # unordered, repeated: each record once
    dlg = dialog(fs, "selected", selection=lambda: chosen[0])
    assert dlg.scope_choice() == "selected"
    set_params(dlg, "set_field", path="picked", value="1")
    pv = run_preview(dlg, qtbot)
    assert pv.scanned == 2 and pv.records_changed == 2
    scope = fe.preview_calls[-1][1]
    assert isinstance(scope, SelectionScope) and scope.gen == fe.applied_gen   # wrapped with the shown gen
    chosen[0] = SelectionScope(rows=[(0, 4), (10, 11)], gen=fe.applied_gen)
    dlg.refresh_scope()
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected", count="7")
    pv = run_preview(dlg, qtbot)
    assert pv.scanned == 7
    chosen[0] = []
    assert dlg.preview() is None and dlg.status_label.text() == t("gui.bulk.scope.no_selection")
    chosen[0] = None
    assert dlg.preview() is None


def test_a_selection_of_an_older_state_is_refused(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    old = SelectionScope(positions=[0, 1], gen=fe.applied_gen)
    fe.set_value(Ref(5, ("n",)), "2")                                 # the state moves on
    dlg = dialog(fs, "selected", selection=lambda: old)
    failed: list = []
    dlg.preview_failed.connect(failed.append)
    set_params(dlg, "set_field", path="x", value="1")
    assert dlg.preview() is not None
    qtbot.waitUntil(lambda: bool(failed), timeout=TIMEOUT)
    assert getattr(failed[0], "key", "") == "bulk.error.stale_scope"
    assert dlg.status_label.text().startswith(t("gui.bulk.status.failed", error=failed[0].message))
    assert dlg.preview_result is None and not dlg.can_apply()


class _Grid:
    def __init__(self, ranges: list) -> None:
        self.ranges = ranges

    def selected_ranges(self) -> list:
        return list(self.ranges)


class _View:
    def __init__(self, session: Any, view_id: str, **kw: Any) -> None:
        self.session = session
        self.view_id = view_id
        self.__dict__.update(kw)


class _Window(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.view: Any = None

    def active_view(self) -> Any:
        return self.view


def test_selection_of_the_parent_windows_active_view(rig, qtbot) -> None:
    fs, fe = rig()
    window = _Window()
    qtbot.addWidget(window)
    dlg = BulkDialog(fs, parent=window)                              # no selection callback given
    qtbot.addWidget(dlg)
    assert dlg.scope_buttons["selected"].isEnabled()
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected_none")
    window.view = _View(fs, "table", grid=_Grid([(1, 3, 0, 2), (2, 5, 1, 1), (9, 9, 0, 0)]))
    dlg.refresh_scope()
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected", count="6")   # rows 1-5 and 9
    window.view = _View(fs, "tree", selected_keypaths=lambda: [(4, "name"), (4, "n"), ("wrapper",), (7,)])
    dlg.refresh_scope()
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected", count="2")
    dlg.set_scope_choice("selected")
    dlg.set_spec(BulkSpec("set_field", {"path": "x", "value": "1"}))
    task = dlg.preview()
    assert task is not None
    qtbot.waitUntil(lambda: dlg.preview_result is not None, timeout=TIMEOUT)
    scope = fe.preview_calls[-1][1]
    assert list(scope.positions) == [4, 7] and dlg.preview_result.scanned == 2
    window.view = _View(fs, "text", bulk_selection=lambda: [0, 1, 2])
    dlg.refresh_scope()
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected", count="3")
    other, _ = rig()
    window.view = _View(other, "table", grid=_Grid([(0, 9, 0, 0)]))  # another document's view: not ours
    dlg.refresh_scope()
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected_none")
    lone = BulkDialog(fs)                                            # no parent window, no callback
    qtbot.addWidget(lone)
    assert not lone.scope_buttons["selected"].isEnabled()


def test_selection_scope_for_view_rows(rig) -> None:
    fs, fe = rig()
    base = SelectionScope.for_view_rows(fs, [(2, 4, 0, 3), (8, 8, 1, 1)])
    assert list(base.rows) == [(2, 4), (8, 8)] and base.source is None and base.gen == fe.applied_gen
    merged = SelectionScope.for_view_rows(fs, [(5, 9, 0, 0), (1, 3, 0, 0), (4, 4, 1, 1), (7, 12, 2, 2)])
    assert list(merged.rows) == [(1, 12)] and len(merged) == 12
    assert [list(c) for c in base.iter_chunks()] == [[2, 3, 4], [8]] and len(base) == 4
    view = RecordSet.from_indices([30, 10, 20, 5], gen=fe.applied_gen)
    fs.set_view(view)
    rows = SelectionScope.for_view_rows(fs, [(1, 2)])
    assert rows.source is view and [list(c) for c in rows.iter_chunks()] == [[10, 20]]
    assert SelectionScope.for_view_rows(fs, []) is None
    chunked = SelectionScope(rows=[(0, 9)], positions=array("Q", [40, 41]))
    assert [list(c) for c in chunked.iter_chunks(size=4)] == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9], [40, 41]]
    gen_scope = SelectionScope(positions=iter([7, 8, 9]))
    assert [list(c) for c in gen_scope.iter_chunks(size=2)] == [[7, 8], [9]]
    view.close()


# ======================================================================================
# availability, registry, huge note, remembered spec, reveal
# ======================================================================================
def test_unavailable_and_busy_editing(rig, dialog) -> None:
    from jsonreshape.gui.editing import reason_text

    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "delete_field", path="tags")
    assert dlg.preview_button.isEnabled() and dlg.unavailable_label.isHidden()
    fe.available, fe.reason, fe.reason_text = False, "locked", reason_text("locked")
    fs.edit_state_changed.emit()
    expected = reason_text("locked")                                 # the controller's sentence
    assert expected and not dlg.preview_button.isEnabled() and dlg.unavailable_label.text() == expected
    assert dlg.preview() is None and dlg.status_label.text() == expected
    fe.reason_text = ""                                              # no sentence: a generic one
    fs.edit_state_changed.emit()
    assert dlg.unavailable_label.text() == t("gui.bulk.unavailable")
    fe.available, fe.busy = True, True
    fs.edit_state_changed.emit()
    assert not dlg.preview_button.isEnabled() and dlg.unavailable_label.isHidden()
    assert dlg.preview() is None and dlg.status_label.text() == t("gui.bulk.status.busy")
    fe.busy = False
    fe.refuse = True                                                 # the controller refuses (busy meanwhile)
    fs.edit_state_changed.emit()
    assert dlg.preview() is None and dlg.status_label.text() == t("gui.bulk.status.busy")
    fs2, _ = rig(edit=False)
    dlg2 = dialog(fs2)
    assert dlg2.unavailable_label.text() == t("gui.bulk.unavailable") and not dlg2.preview_button.isEnabled()
    fs2.edit = FakeEdit(fs2)                                         # editing became possible
    fs2.edit_changed.emit()
    set_params(dlg2, "delete_field", path="tags")
    assert dlg2.preview_button.isEnabled() and dlg2.unavailable_label.isHidden()
    fs2.edit.es.close()


def test_one_dialog_per_session(rig, qtbot) -> None:
    fs, _fe = rig()
    dlg = open_bulk_dialog(fs, scope_default="view")                 # WA_DeleteOnClose: deleted when closed
    assert dlg.isVisible() and dlg.scope_choice() == "view" and BulkDialog.instance(fs) is dlg
    again = BulkDialog.show_for(fs, selection=lambda: [1], scope_default="selected")
    assert again is dlg and dlg.scope_choice() == "selected"
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected", count="1")
    fs2, _ = rig()
    other = open_bulk_dialog(fs2)
    assert other is not dlg and BulkDialog.instance(fs2) is other
    dlg.close()
    assert BulkDialog.instance(fs) is None
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(dlg)
    fresh = open_bulk_dialog(fs)
    assert fresh is not dlg and BulkDialog.instance(fs) is fresh
    fs.state_changed.emit("closed")                                  # the session closes: so does the dialog
    assert BulkDialog.instance(fs) is None
    other.close()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(fresh) and not shiboken6.isValid(other)


def test_huge_record_note(rig, dialog, qtbot) -> None:
    big = b'[{"a": "' + b"x" * 400 + b'"}, {"a": "small"}]'
    fs, fe = rig(big, settings=Settings(large_node_bytes=200))
    dlg = dialog(fs)
    qtbot.waitUntil(lambda: dlg.huge_records is not None, timeout=TIMEOUT)
    assert dlg.huge_records and not dlg.huge_label.isHidden()
    assert dlg.huge_label.text() == t("gui.bulk.huge_note", limit="200 B")
    set_params(dlg, "replace", find="small", replacement="kichik")
    pv = run_preview(dlg, qtbot)
    assert pv.skipped_huge == 1 and t("gui.bulk.result.huge", count="1") in dlg.counts_label.text()
    assert pv.warnings and dlg.warnings_label.text() == pv.warnings[0]
    fs2, _ = rig(b'[{"a": "' + b"x" * 150 + b'"}, {"a": "' + b"y" * 150 + b'"}]',
                 settings=Settings(large_node_bytes=200))
    dlg2 = dialog(fs2)
    qtbot.waitUntil(lambda: dlg2.huge_records is not None, timeout=TIMEOUT)
    assert dlg2.huge_records is False and dlg2.huge_label.isHidden()   # the file is bigger, no record is
    fs3, _ = rig()
    dlg3 = dialog(fs3)
    assert dlg3.huge_records is False                                # small file: no scan at all


def test_last_operation_is_remembered(rig, dialog, qtbot) -> None:
    fs, _fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "rename_key", old="name", new="nomi", path=None)
    run_preview(dlg, qtbot)
    dlg.reject()
    again = dialog(fs)
    assert again.kind() == "rename_key"
    assert again.spec().normalized() == BulkSpec("rename_key", {"old": "name", "new": "nomi"}).normalized()


def test_double_click_reveals_the_record(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "replace", find="Samarqand", replacement="Samarkand")
    run_preview(dlg, qtbot)
    dlg.examples_table.cellDoubleClicked.emit(1, 2)
    ref, reveal = fs.selected[-1]
    assert (ref.record, ref.path, reveal) == (5, ("name",), True)
    fe.set_value(Ref(0, ("n",)), "3")                                 # stale: no more revealing
    dlg.examples_table.cellDoubleClicked.emit(0, 0)
    assert len(fs.selected) == 1


def test_failed_apply_is_reported_and_discarded(rig, dialog, qtbot, monkeypatch) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "delete_field", path="tags")
    pv = run_preview(dlg, qtbot)

    def broken(preview: Any) -> Any:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(fe.es, "bulk_apply", broken)
    assert dlg.apply()
    qtbot.waitUntil(lambda: dlg.status_label.text() == t("gui.bulk.status.not_applied"), timeout=TIMEOUT)
    assert pv.discarded and not layer_file(fe, pv).exists() and dlg.preview_button.isEnabled()
    assert len(fs.app.errors) == 1 and dlg.preview_result is None and not dlg.can_apply()


def test_a_refused_apply_keeps_a_fresh_preview(rig, dialog, qtbot) -> None:
    fs, fe = rig()
    dlg = dialog(fs)
    set_params(dlg, "delete_field", path="tags")
    pv = run_preview(dlg, qtbot)
    fe.refuse_apply = True
    assert dlg.apply() is False
    assert dlg.status_label.text() == t("gui.bulk.status.not_applied")
    assert not pv.discarded and layer_file(fe, pv).exists() and dlg.can_apply()   # try again later
    fe.refuse_apply = False
    applied: list = []
    dlg.applied.connect(applied.append)
    assert dlg.apply() is True
    qtbot.waitUntil(lambda: bool(applied), timeout=TIMEOUT)
    assert "tags" not in record(fs.doc, 0)
