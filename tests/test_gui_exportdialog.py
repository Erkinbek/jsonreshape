"""ExportDialog (jsonreshape/gui/exportdialog.py; ARCHITECTURE §8, §11.8; AGENT_PROMPT §10).

Every format writes exactly what ``writer.export`` writes for the same options; the
widgets map to the options; scope view (filter + sort) / all / node (JSON byte-exact
numbers, JSON Lines of a 150 000-element array, the synthetic root of a JSON Lines file);
the source file is refused; ``as_keyed`` round trip keyed -> JSON Lines -> keyed; the
report window (records, files, warnings, open folder); format <-> extension; remembered
options; closing the dialog while the export runs; cancelling; errors; inline validation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from jsonreshape import core, writer
from jsonreshape.errors import ExportError
from jsonreshape.gui import exportdialog as exportdialog_mod
from jsonreshape.gui import widgets
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.exportdialog import ExportDialog, ExportOutcome, export_node
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.sortkeys import SortKey
from jsonreshape.writer import ExportOptions

pytestmark = pytest.mark.gui

TIMEOUT = 30000
openpyxl = pytest.importorskip("openpyxl")


# ----------------------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------------------
def record_text(i: int) -> str:
    return ('{"id": %d, "name": "Ism %d", "city": "%s", "grp": %d, '
            '"nums": [1.10, 1e400, 12345678901234567890, -0.0, 0.1], '
            '"tags": ["t%d", "x"], "nested": {"x": %d, "y": [1, 2]}}'
            % (i, i, ["Toshkent", "Oʻzbekiston", "Самарқанд"][i % 3], i % 3, i % 4, i * 2))


def write_array(path: Path, n: int = 120) -> Path:
    path.write_text("[\n" + ",\n".join(record_text(i) for i in range(n)) + "\n]", encoding="utf-8")
    return path


def write_keyed(path: Path, n: int = 40) -> Path:
    path.write_text("{\n" + ",\n".join(f'"id{i}": {record_text(i)}' for i in range(n)) + "\n}", encoding="utf-8")
    return path


def write_wrapped(path: Path, n: int = 30) -> Path:
    path.write_text('{"meta": {"v": 1.50}, "data": [' + ",".join(record_text(i) for i in range(n))
                    + '], "tail": true}', encoding="utf-8")
    return path


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def opened(qtbot, app):
    made: list[DocSession] = []

    def factory(path: Path, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        assert s.state == "ready", s.open_error
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def dialog(qtbot):
    made: list[ExportDialog] = []

    def factory(session: DocSession, **kw) -> ExportDialog:
        dlg = ExportDialog(session, **kw)
        qtbot.addWidget(dlg)
        made.append(dlg)
        return dlg

    yield factory


def run_export(qtbot, dlg: ExportDialog) -> ExportOutcome:
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT) as blocker:
        assert dlg.start_export() is not None, dlg.error_text()
    return blocker.args[0]


def xlsx_values(path: Path) -> list[list[list]]:
    wb = openpyxl.load_workbook(path)
    return [[list(row) for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets]


def sqlite_dump(path: Path) -> list[str]:
    con = sqlite3.connect(str(path))
    try:
        return list(con.iterdump())
    finally:
        con.close()


def same_output(fmt: str, got: Path, want: Path) -> None:
    if fmt == "xlsx":
        assert xlsx_values(got) == xlsx_values(want)
    elif fmt == "sqlite":
        assert sqlite_dump(got) == sqlite_dump(want)
    else:
        assert got.read_bytes() == want.read_bytes()


# ----------------------------------------------------------------------------------------
# formats and options
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv", "xlsx", "sqlite"])
def test_each_format_writes_what_writer_export_writes(qtbot, app, opened, dialog, tmp_path, fmt) -> None:
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all")
    out = tmp_path / f"out{exportdialog_mod.EXTENSIONS[fmt]}"
    dlg.set_output_path(out)
    assert dlg.format() == fmt and dlg.scope() == "all"
    outcome = run_export(qtbot, dlg)
    opts = dlg.export_options()
    assert opts == ExportOptions.from_settings(fmt)            # the defaults
    want = tmp_path / f"want{exportdialog_mod.EXTENSIONS[fmt]}"
    report = writer.export(s.doc, want, opts, None)
    same_output(fmt, out, want)
    assert outcome.report.records == report.records == 120
    assert outcome.report.files == [out] and outcome.file_sizes == [out.stat().st_size]
    if fmt == "json":
        text = out.read_text(encoding="utf-8")
        assert "1e400" in text and "12345678901234567890" in text and "-0.0" in text and "1.10" in text
        assert "Самарқанд" in text                               # never \u-escaped


def test_widgets_map_to_options(qtbot, app, opened, dialog, tmp_path) -> None:
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all", columns=["id", "name", "nested.x", "tags"])
    # JSON: indent 4, keys sorted alphabetically
    dlg.set_output_path(tmp_path / "a.json")
    dlg.indent_combo.setCurrentIndex(1)
    dlg.json_sort_combo.setCurrentIndex(1)
    assert dlg.export_options() == ExportOptions("json", indent=4, sort_keys="alpha")
    run_export(qtbot, dlg)
    writer.export(s.doc, tmp_path / "a_want.json", ExportOptions("json", indent=4, sort_keys="alpha"))
    same_output("json", tmp_path / "a.json", tmp_path / "a_want.json")
    # CSV: the table's columns, rows per array element, ';' delimiter, no BOM, '|' joins
    dlg.set_output_path(tmp_path / "b.csv")
    assert dlg.format() == "csv" and not dlg.auto_columns_check.isChecked()
    assert dlg.column_list() == ["id", "name", "nested.x", "tags"]
    dlg.array_mode_combo.setCurrentIndex(1)
    dlg.delimiter_combo.setCurrentIndex(1)
    dlg.bom_check.setChecked(False)
    dlg.join_sep_edit.setText("|")
    want = ExportOptions("csv", columns=["id", "name", "nested.x", "tags"], array_mode="rows", csv_delimiter=";",
                         csv_bom=False, join_sep="|")
    assert dlg.export_options() == want
    run_export(qtbot, dlg)
    writer.export(s.doc, tmp_path / "b_want.csv", want)
    same_output("csv", tmp_path / "b.csv", tmp_path / "b_want.csv")
    assert (tmp_path / "b.csv").read_text(encoding="utf-8").splitlines()[0] == "id;name;nested.x;tags"
    # XLSX: split into files of 50 rows
    dlg.set_output_path(tmp_path / "c.xlsx")
    dlg.set_columns(None)
    dlg.array_mode_combo.setCurrentIndex(0)
    dlg.split_combo.setCurrentIndex(1)
    dlg.max_rows_spin.setValue(50)
    want = ExportOptions.from_settings("xlsx", xlsx_split="files", xlsx_max_rows=50, join_sep="|")
    assert dlg.export_options() == want
    outcome = run_export(qtbot, dlg)
    assert len(outcome.report.files) == 3                     # 120 rows / 49 data rows per file
    assert len(outcome.file_sizes) == 3 and all(n > 0 for n in outcome.file_sizes)


def test_keep_wrapper_only_for_wrapped_documents(qtbot, app, opened, dialog, tmp_path) -> None:
    plain = opened(write_array(tmp_path / "p.json", 5))
    assert not dialog(plain).keep_wrapper_check.isEnabled()
    s = opened(write_wrapped(tmp_path / "w.json"))
    dlg = dialog(s, scope="all")
    assert dlg.keep_wrapper_check.isEnabled() and dlg.keep_wrapper_check.isChecked()
    dlg.set_output_path(tmp_path / "keep.json")
    run_export(qtbot, dlg)
    assert json.loads((tmp_path / "keep.json").read_text(encoding="utf-8").replace("1e400", "1"))["meta"] == {"v": 1.5}
    assert '"v": 1.50' in (tmp_path / "keep.json").read_text(encoding="utf-8")
    dlg.keep_wrapper_check.setChecked(False)
    dlg.set_output_path(tmp_path / "bare.json")
    run_export(qtbot, dlg)
    assert (tmp_path / "bare.json").read_text(encoding="utf-8").startswith("[")
    dlg.as_keyed_check.setChecked(True)
    assert not dlg.keep_wrapper_check.isEnabled()              # no effect with as_keyed


# ----------------------------------------------------------------------------------------
# scopes
# ----------------------------------------------------------------------------------------
def test_scope_view_exports_the_filtered_sorted_view(qtbot, app, opened, dialog, tmp_path, lang_uz) -> None:
    s = opened(write_array(tmp_path / "people.json"))
    s.set_filter(FilterSpec("grp != 1"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    s.set_sort([SortKey("nested.x", descending=True)])
    qtbot.waitUntil(lambda: not s.view_busy and len(s.sort_keys) == 1, timeout=TIMEOUT)
    view = [s.record_at(i) for i in range(s.view_len())]
    assert len(view) == 80 and view[0] == 119
    dlg = dialog(s)
    assert dlg.scope() == "view"
    assert "80" in dlg.scope_buttons["view"].text() and "Filtr" in dlg.scope_buttons["view"].text()
    assert "120" in dlg.scope_buttons["all"].text()
    for fmt in ("jsonl", "csv"):
        out = tmp_path / f"view.{fmt}"
        dlg.set_output_path(out)
        outcome = run_export(qtbot, dlg)
        assert outcome.scope == "view" and outcome.report.records == 80
        writer.export(s.doc, tmp_path / f"want.{fmt}", dlg.export_options(), view)
        same_output(fmt, out, tmp_path / f"want.{fmt}")
    assert json.loads((tmp_path / "view.jsonl").read_text(encoding="utf-8").splitlines()[0]
                      .replace("1e400", "1"))["id"] == 119


def test_scope_node_json_is_byte_exact(qtbot, app, opened, dialog, tmp_path) -> None:
    path = write_array(tmp_path / "people.json")
    s = opened(path)
    s.select(NodeRef(7, ("nums",)))
    dlg = dialog(s, scope="node")
    dlg.set_output_path(tmp_path / "nums.csv")
    assert dlg.format() == "json" and "(.csv)" in dlg.warning_label.text()   # a node is never CSV
    assert dlg.scope() == "node" and "#7.nums" in dlg.scope_buttons["node"].text()
    # only JSON / JSON Lines for a node
    model = dlg.format_combo.model()
    assert [model.item(i).isEnabled() for i in range(5)] == [True, True, False, False, False]
    dlg.set_format("csv")
    assert dlg.format() == "csv" and dlg.error_text()            # a disabled choice is refused
    dlg.set_format("json")
    dlg.set_output_path(tmp_path / "nums.json")
    outcome = run_export(qtbot, dlg)
    raw = path.read_bytes()
    start = raw.index(b'{"id": 7,')
    a = raw.index(b"[1.10", start)
    b = raw.index(b"]", a) + 1
    rf = core.Reformatter(2)
    want = rf.feed(raw[a:b]) + rf.finish()
    got = (tmp_path / "nums.json").read_bytes()
    assert got == want
    assert got == b"[\n  1.10,\n  1e400,\n  12345678901234567890,\n  -0.0,\n  0.1\n]"
    assert outcome.report.records == 1 and outcome.scope == "node"
    # the whole record, compact, as one JSON Lines line
    s.select(NodeRef(7, ()))
    dlg.set_format("jsonl")
    dlg.set_output_path(tmp_path / "rec.jsonl")
    run_export(qtbot, dlg)
    line = (tmp_path / "rec.jsonl").read_bytes()
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert line.startswith(b'{"id":7,"name":"Ism 7"') and b"1e400" in line


def test_scope_node_jsonl_of_a_large_array(qtbot, app, opened, dialog, tmp_path) -> None:
    n = 150_000
    path = tmp_path / "big.json"
    path.write_text('[{"id": 0, "big": [' + ", ".join(
        ('{"k": %d, "v": 1.%d0}' % (i, i % 7)) if i % 5 == 0 else str(i) for i in range(n)) + "]}]",
        encoding="utf-8")
    s = opened(path)
    s.select(NodeRef(0, ("big",)))
    dlg = dialog(s, scope="node")
    dlg.set_format("jsonl")
    dlg.set_output_path(tmp_path / "big.jsonl")
    outcome = run_export(qtbot, dlg)
    lines = (tmp_path / "big.jsonl").read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == n + 1
    assert outcome.report.records == n
    assert lines[0] == b'{"k":0,"v":1.00}' and lines[1] == b"1" and lines[5] == b'{"k":5,"v":1.50}'
    assert lines[n - 1] == str(n - 1).encode()


def test_export_node_function_and_virtual_root(qtbot, app, opened, tmp_path) -> None:
    path = tmp_path / "lines.jsonl"
    path.write_text("\n".join(record_text(i) for i in range(10)) + "\n", encoding="utf-8")
    s = opened(path)
    assert s.doc.kind == "jsonl"
    out = tmp_path / "all.json"
    done = threading.Event()
    result = {}

    def work():
        result["report"] = export_node(s, NodeRef(None, ()), out, "json", indent=2)
        done.set()

    threading.Thread(target=work).start()
    assert done.wait(30)
    writer.export(s.doc, tmp_path / "want.json", ExportOptions("json", indent=2))
    assert out.read_bytes() == (tmp_path / "want.json").read_bytes()
    assert result["report"].records == 10
    with pytest.raises(Exception) as info:
        export_node(s, NodeRef(0, ()), tmp_path / "x.csv", "csv")
    assert getattr(info.value, "key", "") == "gui.export.node_formats"
    with pytest.raises(Exception) as info:
        export_node(s, NodeRef(0, ()), path, "json")
    assert getattr(info.value, "key", "") == "writer.error.same_file"


# ----------------------------------------------------------------------------------------
# safety, as_keyed, report
# ----------------------------------------------------------------------------------------
def test_refuses_the_source_file(qtbot, app, opened, dialog, tmp_path, lang_uz) -> None:
    path = write_array(tmp_path / "people.json", 5)
    s = opened(path)
    dlg = dialog(s, scope="all")
    before = path.read_bytes()
    upper = Path(str(path).replace("people.json", "PEOPLE.JSON"))
    for target in (path, upper, tmp_path / "sub" / ".." / "people.json"):
        dlg.set_output_path(target)
        assert "manba" in dlg.error_text(), target
        assert not dlg.export_button.isEnabled()
        assert dlg.start_export() is None
    assert path.read_bytes() == before
    dlg.set_output_path(tmp_path / "ok.json")
    assert dlg.error_text() == "" and dlg.export_button.isEnabled()


def test_as_keyed_round_trip(qtbot, app, opened, dialog, tmp_path) -> None:
    keyed = opened(write_keyed(tmp_path / "keyed.json"))
    assert keyed.doc.kind == "keyed"
    dlg = dialog(keyed, scope="all")
    dlg.set_output_path(tmp_path / "keyed.jsonl")
    assert not dlg.as_keyed_check.isVisibleTo(dlg) or dlg.format() != "json"
    run_export(qtbot, dlg)
    first = (tmp_path / "keyed.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith('{"id0":{"id":0,')
    lines = opened(tmp_path / "keyed.jsonl")
    assert lines.doc.kind == "jsonl"
    back = dialog(lines, scope="all")
    back.set_output_path(tmp_path / "rebuilt.json")
    back.as_keyed_check.setChecked(True)
    assert back.export_options().as_keyed
    run_export(qtbot, back)
    writer.export(keyed.doc, tmp_path / "original.json", ExportOptions("json", indent=2))
    assert (tmp_path / "rebuilt.json").read_bytes() == (tmp_path / "original.json").read_bytes()
    # not for a node, and the option is gone for other formats
    back.set_format("jsonl")
    assert not back.export_options().as_keyed


def test_as_keyed_error_is_reported_and_nothing_written(qtbot, app, opened, dialog, tmp_path, lang_uz) -> None:
    s = opened(write_array(tmp_path / "people.json", 5))
    dlg = dialog(s, scope="all")
    dlg.set_output_path(tmp_path / "bad.json")
    dlg.as_keyed_check.setChecked(True)
    with qtbot.waitSignal(dlg.export_failed, timeout=TIMEOUT) as blocker:
        dlg.start_export()
    assert isinstance(blocker.args[0], ExportError)
    assert app.errors and isinstance(app.errors[-1], ExportError)
    assert not (tmp_path / "bad.json").exists()
    assert dlg.report_dialog is None and dlg.export_button.isEnabled()
    assert "#0" in dlg.status_label.full_text()


def test_report_window(qtbot, app, opened, dialog, tmp_path, monkeypatch, lang_uz) -> None:
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all", columns=["id", "no_such_field"])
    dlg.set_output_path(tmp_path / "r.csv")
    outcome = run_export(qtbot, dlg)
    report = dlg.report_dialog
    assert report is not None and report.isVisible() and report.outcome is outcome
    text = report.report_text()
    assert "120 ta yozuv" in text and str(tmp_path / "r.csv") in text
    assert outcome.report.warnings and report.warnings_view is not None
    assert "no_such_field" in report.warnings_view.toPlainText()
    opened_paths = []
    monkeypatch.setattr(exportdialog_mod, "open_local_path", lambda p: opened_paths.append(p) or True)
    assert report.open_folder() and opened_paths == [str(tmp_path)]
    assert "Tayyor" in dlg.status_label.full_text()
    report.close()


# ----------------------------------------------------------------------------------------
# format, memory, running in the background
# ----------------------------------------------------------------------------------------
def test_format_follows_the_extension_and_back(qtbot, app, opened, dialog, tmp_path) -> None:
    s = opened(write_array(tmp_path / "people.json", 5))
    dlg = dialog(s)
    assert dlg.output_path().endswith("people-export.json") and dlg.format() == "json"
    dlg.set_output_path(tmp_path / "x.XLSX")
    assert dlg.format() == "xlsx" and dlg.pages.currentIndex() == 2
    assert not dlg.table_form.isRowVisible(dlg.delimiter_combo) and dlg.table_form.isRowVisible(dlg.split_combo)
    dlg.set_format("csv")
    assert dlg.output_path() == str(tmp_path / "x.csv")
    assert dlg.table_form.isRowVisible(dlg.delimiter_combo) and not dlg.table_form.isRowVisible(dlg.split_combo)
    dlg.set_output_path(tmp_path / "noext")
    assert dlg.format() == "csv"
    dlg.set_format("sqlite")
    assert dlg.output_path() == str(tmp_path / "noext.sqlite") and dlg.pages.currentIndex() == 3
    dlg.set_output_path(tmp_path / "data.ndjson")
    assert dlg.format() == "jsonl"


def test_last_format_and_options_are_remembered(qtbot, app, opened, dialog, tmp_path) -> None:
    s = opened(write_array(tmp_path / "people.json", 5))
    dlg = dialog(s)
    dlg.set_output_path(tmp_path / "m.csv")
    dlg.delimiter_combo.setCurrentIndex(2)          # tab
    dlg.bom_check.setChecked(False)
    run_export(qtbot, dlg)
    assert app.tasks.wait_idle(10)                  # the state is saved in a silent task
    saved = GuiState.load(app.state.path).get("export_last")
    assert saved["fmt"] == "csv" and saved["csv_delimiter"] == "\t" and saved["csv_bom"] is False
    again = dialog(s)
    assert again.format() == "csv" and again.export_options().csv_delimiter == "\t"
    assert not again.bom_check.isChecked()
    assert Path(again.output_path()).parent == tmp_path and again.output_path().endswith(".csv")


def test_the_dialog_can_close_while_the_export_runs(qtbot, app, opened, dialog, tmp_path, monkeypatch) -> None:
    gate = threading.Event()
    real = writer.export

    def slow(doc, out, opts, source=None, *, progress=None, cancel=None):
        assert gate.wait(20)
        return real(doc, out, opts, source, progress=progress, cancel=cancel)

    monkeypatch.setattr(writer, "export", slow)
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all")
    dlg.show()
    dlg.set_output_path(tmp_path / "bg.jsonl")
    task = dlg.start_export()
    # the task is not owned by the session (review finding T5: a reopen / tab close detached
    # session-owned exports silently); the application-wide runner reports its outcome
    assert task.kind == "export" and task.owner is dlg.job and dlg.job.session is s and dlg.running
    assert not dlg.export_button.isEnabled() and dlg.cancel_task_button.isVisibleTo(dlg)
    dlg.reject()
    assert not dlg.isVisible()
    assert task in app.tasks.tasks()                # still listed in the tasks panel
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT):
        gate.set()
    assert (tmp_path / "bg.jsonl").read_text(encoding="utf-8").count("\n") == 120
    assert dlg.report_dialog is not None and dlg.report_dialog.isVisible()


def test_cancel_leaves_no_file(qtbot, app, opened, dialog, tmp_path, monkeypatch, lang_uz) -> None:
    gate = threading.Event()
    real = writer.export

    def slow(doc, out, opts, source=None, *, progress=None, cancel=None):
        gate.wait(20)
        return real(doc, out, opts, source, progress=progress, cancel=cancel)

    monkeypatch.setattr(writer, "export", slow)
    s = opened(write_array(tmp_path / "people.json"))
    dlg = dialog(s, scope="all")
    dlg.set_output_path(tmp_path / "c.json")
    task = dlg.start_export()
    dlg.cancel_export()
    gate.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert task.state == "cancelled"
    assert "To'xtatildi" in dlg.status_label.full_text()
    assert not (tmp_path / "c.json").exists() and dlg.report_dialog is None and app.errors == []


def test_overwrite_asks_first(qtbot, app, opened, dialog, tmp_path, monkeypatch) -> None:
    s = opened(write_array(tmp_path / "people.json", 5))
    out = tmp_path / "exists.json"
    out.write_text("old", encoding="utf-8")
    dlg = dialog(s, scope="all")
    dlg.set_output_path(out)
    asked = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: asked.append(text) or False)
    assert dlg.start_export() is None and out.read_text(encoding="utf-8") == "old"
    assert asked and str(out) in asked[0]
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)
    run_export(qtbot, dlg)
    assert out.read_text(encoding="utf-8").startswith("[")


def test_invalid_options_are_shown_inline(qtbot, app, opened, dialog, tmp_path, lang_uz) -> None:
    s = opened(write_array(tmp_path / "people.json", 5))
    dlg = dialog(s, scope="all")
    dlg.set_output_path(tmp_path / "v.csv")
    assert dlg.error_text() == "" and dlg.export_button.isEnabled()
    dlg.auto_columns_check.setChecked(False)
    assert "Ustunlar" in dlg.error_text() and not dlg.export_button.isEnabled()
    dlg.columns_edit.setPlainText("id\nname[")
    assert "name[" in dlg.error_text()
    dlg.columns_edit.setPlainText("id\nname")
    assert dlg.error_text() == ""
    dlg.delimiter_combo.setEditText('"')
    assert dlg.error_text() and dlg.start_export() is None
    dlg.delimiter_combo.setCurrentIndex(0)
    dlg.join_sep_edit.setText("")
    assert dlg.error_text()
    dlg.join_sep_edit.setText(";")
    dlg.explode_edit.setText("tags[")
    assert dlg.error_text()
    dlg.explode_edit.setText("")
    dlg.set_output_path("")
    assert "faylini tanlang" in dlg.error_text()
    dlg.set_output_path(tmp_path / "v.csv")
    assert dlg.error_text() == "" and dlg.export_button.isEnabled()


def test_node_scope_needs_a_current_node(qtbot, app, opened, dialog, tmp_path) -> None:
    s = opened(write_array(tmp_path / "people.json", 5))
    dlg = dialog(s, scope="node")
    assert dlg.scope() == "view" and not dlg.scope_buttons["node"].isEnabled()
    s.select(NodeRef(1, ("tags",)))
    assert dlg.scope_buttons["node"].isEnabled() and "#1.tags" in dlg.scope_buttons["node"].text()
    dlg.set_scope("node")
    dlg.set_output_path(tmp_path / "tags.json")
    run_export(qtbot, dlg)
    assert (tmp_path / "tags.json").read_text(encoding="utf-8") == '[\n  "t1",\n  "x"\n]'
    s.select(None)
    assert dlg.scope() == "view"
